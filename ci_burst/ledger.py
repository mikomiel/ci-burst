"""Durable local state for one deployment.

One SQLite file records frozen launch intent, operation identity, tracked cost
and the budget commitment built from it. One OS-backed writer lock excludes a
second mutating process; the supported topology is a single controller host with
a single ledger, and resource tags are ownership metadata rather than a
distributed lock.
"""

import fcntl
import json
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import StateRefusal
from .money import hourly_cost_micros

SCHEMA_VERSION = 2
#: Read-only commands accept the previous schema so a running deployment keeps
#: observing while the next writable run migrates the file.
READABLE_SCHEMA_VERSIONS = (1, 2)
NONTERMINAL = ("intent", "starting", "running", "terminating", "uncertain")
TERMINAL = ("terminal", "failed")


@dataclass(frozen=True)
class Operation:
    id: str
    client_token: str
    state: str
    created_at: float
    updated_at: float
    expires_at: float
    reservation_micros: int
    reservation_window: float
    region: str
    account: str
    request_json: str
    request_sha256: str
    instance_id: str | None
    detail: str
    #: Spot price of the request in flight, in micros; zero when the row predates
    #: cost tracking, in which case the configured maximum is the conservative
    #: substitute.
    price_micros: int = 0
    #: Provider launch time, recorded on the first reconcile that observes it.
    launch_time: float | None = None
    #: Final tracked cost, written once when the operation reaches a terminal
    #: state.
    settled_micros: int | None = None
    settled_at: float | None = None

    def request(self):
        return json.loads(self.request_json)

    def as_dict(self):
        return {
            "operation": self.id,
            "state": self.state,
            "region": self.region,
            "account": self.account,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "reservation_micros": self.reservation_micros,
            "instance_id": self.instance_id,
            "detail": self.detail,
        }


def current_window(now, window_seconds):
    """Anchor a reservation window at a multiple of its own length."""
    return float(int(now) // int(window_seconds) * int(window_seconds))


class Ledger:
    """One durable ledger. Construct, then ``connect`` before use."""

    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self._clock = clock
        self._connection = None
        self._lock_fd = None
        self._in_transaction = False
        self._version = None

    # -- lifecycle -----------------------------------------------------
    def connect(self, create=True, read_only=False, backfill=None):
        """Open the ledger, upgrading a version-1 file when writing is allowed.

        ``backfill`` is ``(price_micros, incidental_per_hour_micros)`` and is
        required only to upgrade a version-1 file: historical terminal operations
        are then settled from their recorded timestamps at that price. The caller
        must hold the deployment writer lock before a writable connect, because
        the upgrade copies the file and rewrites its schema.
        """
        if self._connection is not None:
            return self
        if not self.path.exists():
            if not create:
                return self
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if read_only:
            return self._connect_read_only()
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            # Rollback-journal mode keeps read-only observation free of
            # -wal/-shm side effects; the supported topology has one writer, so
            # WAL's concurrent-reader benefit does not pay for that.
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA foreign_keys=ON")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self._create_schema(connection)
                version = SCHEMA_VERSION
            elif version == 1:
                if backfill is None:
                    raise StateRefusal(
                        "STATE_MIGRATION_PARAMETERS_REQUIRED",
                        "schema 1 needs the configured price and incidental rate",
                    )
                self._migrate_v1(connection, backfill)
                version = SCHEMA_VERSION
            elif version != SCHEMA_VERSION:
                raise StateRefusal("STATE_VERSION_UNSUPPORTED", f"schema {version}")
            self._version = version
        except sqlite3.DatabaseError as error:
            if connection is not None:
                connection.close()
            raise StateRefusal("STATE_UNREADABLE", type(error).__name__) from None
        except BaseException:
            if connection is not None:
                connection.close()
            raise
        self._connection = connection
        return self

    @property
    def version(self):
        """Schema version of the open file, or ``None`` when not connected."""
        return self._version

    def _connect_read_only(self):
        """Open an existing ledger without creating, migrating or writing it.

        The previous schema stays readable so a running deployment can keep
        observing between promotion and the first writable run; the rows it
        reports then carry cost defaults and are estimated conservatively.
        """
        try:
            uri = Path(self.path).resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            connection.row_factory = sqlite3.Row
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError as error:
            raise StateRefusal("STATE_UNREADABLE", type(error).__name__) from None
        if version == 0:
            connection.close()
            raise StateRefusal("STATE_UNINITIALIZED", str(self.path))
        if version not in READABLE_SCHEMA_VERSIONS:
            connection.close()
            raise StateRefusal("STATE_VERSION_UNSUPPORTED", f"schema {version}")
        self._version = version
        self._connection = connection
        return self

    def _create_schema(self, connection):
        connection.executescript(
            """
            CREATE TABLE operations (
                id TEXT PRIMARY KEY,
                client_token TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                reservation_micros INTEGER NOT NULL,
                reservation_window REAL NOT NULL,
                region TEXT NOT NULL,
                account TEXT NOT NULL,
                request_json TEXT NOT NULL,
                request_sha256 TEXT NOT NULL,
                instance_id TEXT,
                detail TEXT NOT NULL DEFAULT '',
                price_micros INTEGER NOT NULL DEFAULT 0,
                launch_time REAL,
                settled_micros INTEGER,
                settled_at REAL
            );
            CREATE INDEX operations_state ON operations (state);
            CREATE INDEX operations_window ON operations (reservation_window);
            PRAGMA user_version = 2;
            """
        )

    def _migrate_v1(self, connection, backfill):
        """Upgrade a version-1 file once, after a verified copy.

        The caller holds the deployment writer lock; this method takes no lock of
        its own. A failed copy refuses before the first schema write, and an
        interrupted upgrade rolls the whole transaction back, so the file stays
        readable by the version that wrote it.
        """
        price_micros, incidental_per_hour = backfill
        backup = Path(str(self.path) + ".pre-v2.%d" % int(self._clock()))
        try:
            shutil.copy2(self.path, backup)
            copied = backup.stat().st_size
        except OSError as error:
            raise StateRefusal("STATE_BACKUP_FAILED", type(error).__name__) from None
        if copied != self.path.stat().st_size:
            raise StateRefusal("STATE_BACKUP_FAILED", "copy size differs")
        now = self._clock()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("ALTER TABLE operations ADD COLUMN price_micros INTEGER NOT NULL DEFAULT 0")
            connection.execute("ALTER TABLE operations ADD COLUMN launch_time REAL")
            connection.execute("ALTER TABLE operations ADD COLUMN settled_micros INTEGER")
            connection.execute("ALTER TABLE operations ADD COLUMN settled_at REAL")
            connection.execute("CREATE INDEX operations_window ON operations (reservation_window)")
            placeholders = ",".join("?" * len(TERMINAL))
            rows = connection.execute(
                "SELECT id, state, created_at, updated_at, expires_at, instance_id"
                " FROM operations WHERE state IN (%s)" % placeholders,
                TERMINAL,
            ).fetchall()
            for row in rows:
                settled = _backfilled_cost(row, price_micros, incidental_per_hour)
                connection.execute(
                    "UPDATE operations SET settled_micros = ?, settled_at = ? WHERE id = ?",
                    (settled, now, row["id"]),
                )
            connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
            connection.execute("COMMIT")
        except sqlite3.DatabaseError as error:
            _rollback(connection)
            raise StateRefusal("STATE_MIGRATION_FAILED", type(error).__name__) from None
        except BaseException:
            _rollback(connection)
            raise

    def close(self):
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self.release_writer()

    @property
    def available(self):
        return self._connection is not None

    # -- writer exclusion ----------------------------------------------
    def acquire_writer(self):
        """Take the ledger's single writer lock or refuse without mutating."""
        if self._lock_fd is not None:
            return self
        lock_path = Path(str(self.path) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise StateRefusal("WRITER_LOCKED", str(lock_path)) from None
        self._lock_fd = fd
        return self

    def release_writer(self):
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    # -- transactions ---------------------------------------------------
    @contextmanager
    def transaction(self):
        if self._connection is None:
            raise StateRefusal("STATE_UNAVAILABLE")
        if self._in_transaction:
            yield self._connection
            return
        self._in_transaction = True
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield self._connection
            self._connection.execute("COMMIT")
        except BaseException:
            try:
                self._connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            self._in_transaction = False

    # -- operations -----------------------------------------------------
    def new_operation(
        self,
        *,
        operation_id,
        client_token,
        request,
        request_sha256,
        expires_at,
        reservation_micros,
        window_start,
        region,
        account,
        detail="",
        price_micros=0,
    ):
        """Insert frozen intent; the caller owns the enclosing transaction."""
        connection = self._require()
        now = self._clock()
        connection.execute(
            "INSERT INTO operations (id, client_token, state, created_at, updated_at, expires_at,"
            " reservation_micros, reservation_window, region, account, request_json, request_sha256,"
            " instance_id, detail, price_micros) VALUES (?, ?, 'intent', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                operation_id,
                client_token,
                now,
                now,
                expires_at,
                reservation_micros,
                window_start,
                region,
                account,
                json.dumps(request, sort_keys=True, separators=(",", ":")),
                request_sha256,
                detail,
                price_micros,
            ),
        )
        return self.operation(operation_id)

    def operation(self, operation_id):
        connection = self._require()
        row = connection.execute("SELECT * FROM operations WHERE id = ?", (operation_id,)).fetchone()
        return _operation(row) if row is not None else None

    def operation_by_token(self, client_token):
        connection = self._require()
        row = connection.execute("SELECT * FROM operations WHERE client_token = ?", (client_token,)).fetchone()
        return _operation(row) if row is not None else None

    def operations(self):
        connection = self._require()
        rows = connection.execute("SELECT * FROM operations ORDER BY created_at, id").fetchall()
        return [_operation(row) for row in rows]

    def nonterminal_operations(self):
        connection = self._require()
        rows = connection.execute(
            "SELECT * FROM operations WHERE state IN (%s) ORDER BY created_at, id" % ",".join("?" * len(NONTERMINAL)),
            NONTERMINAL,
        ).fetchall()
        return [_operation(row) for row in rows]

    def set_state(self, operation_id, state, *, instance_id=None, detail=None, updated_at=None):
        connection = self._require()
        now = self._clock() if updated_at is None else updated_at
        assignments = ["state = ?", "updated_at = ?"]
        values = [state, now]
        if instance_id is not None:
            assignments.append("instance_id = ?")
            values.append(instance_id)
        if detail is not None:
            assignments.append("detail = ?")
            values.append(detail)
        values.append(operation_id)
        connection.execute("UPDATE operations SET %s WHERE id = ?" % ", ".join(assignments), values)

    def set_launch_time(self, operation_id, launch_time):
        """Record the provider's launch time once, for every later cost read."""
        connection = self._require()
        connection.execute(
            "UPDATE operations SET launch_time = ? WHERE id = ? AND launch_time IS NULL",
            (launch_time, operation_id),
        )

    def settle(self, operation_id, micros, settled_at):
        """Write the final tracked cost once; a second call never rewrites it."""
        connection = self._require()
        connection.execute(
            "UPDATE operations SET settled_micros = ?, settled_at = ?"
            " WHERE id = ? AND settled_micros IS NULL",
            (int(micros), settled_at, operation_id),
        )

    def fail_without_reservation(self, operation_id, *, detail, updated_at=None):
        """Record a definite failure and release the allowance in one write.

        Only a call site that proved nothing was created may use this: the
        reservation is zeroed and the allowance is available again, while the
        row keeps its state and its reason as the audit record of the attempt.
        One statement, because a failure recorded without its release would
        charge the window for an attempt that created nothing and nothing later
        would complete it.
        """
        connection = self._require()
        now = self._clock() if updated_at is None else updated_at
        connection.execute(
            "UPDATE operations SET state = 'failed', detail = ?, reservation_micros = 0,"
            " updated_at = ? WHERE id = ?",
            (detail, now, operation_id),
        )

    def advance_operation(self, operation_id, *, request, request_sha256, client_token, detail="", price_micros=0):
        """Replace frozen launch parameters with the next ordered candidate.

        The caller only uses this after a definite provider rejection proved
        that the previous request created no instance. Operation identity,
        reservation, admission window and creation time stay unchanged; the
        request, its hash, its own idempotency token and the price that request
        will be charged advance together, so a later resume retries the
        candidate actually being attempted.
        """
        connection = self._require()
        connection.execute(
            "UPDATE operations SET request_json = ?, request_sha256 = ?, client_token = ?,"
            " price_micros = ?, detail = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(request, sort_keys=True, separators=(",", ":")),
                request_sha256,
                client_token,
                price_micros,
                detail,
                self._clock(),
                operation_id,
            ),
        )

    def window_totals(self, window_start, cost, now):
        """Spend and forward commitment for one anchored window.

        ``spend`` is the tracked cost of operations admitted in this window,
        settled where known and accrued otherwise. ``committed`` adds the
        worst-case remaining life of every non-terminal operation, in this window
        or carried over from an earlier one, plus its configured buffer. A
        launch is affordable only while ``committed`` plus one worst-case worker
        stays inside the allowance, so the promise that spend cannot exceed the
        allowance survives the move from reservations to tracked cost.
        """
        connection = self._require()
        placeholders = ",".join("?" * len(NONTERMINAL))
        rows = connection.execute(
            "SELECT * FROM operations WHERE reservation_window = ? OR state IN (%s)" % placeholders,
            (window_start, *NONTERMINAL),
        ).fetchall()
        spend = 0
        forward = 0
        for row in rows:
            operation = _operation(row)
            if operation.state in TERMINAL:
                if operation.reservation_window == window_start:
                    spend += cost.committed_micros(operation, now)
                continue
            if operation.reservation_window == window_start:
                spend += cost.accrued_micros(operation, now)
            forward += cost.remaining_micros(operation, now) + cost.buffer_micros
        return {"spend": spend, "committed": spend + forward}

    def window_launches(self, window_start):
        """Count the operations admitted in one anchored window."""
        connection = self._require()
        row = connection.execute(
            "SELECT COUNT(*) FROM operations WHERE reservation_window = ?",
            (window_start,),
        ).fetchone()
        return int(row[0])

    def _require(self):
        if self._connection is None:
            raise StateRefusal("STATE_UNAVAILABLE")
        return self._connection


def _operation(row):
    keys = row.keys()

    def optional(name, default=None):
        return row[name] if name in keys else default

    return Operation(
        id=row["id"],
        client_token=row["client_token"],
        state=row["state"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        expires_at=row["expires_at"],
        reservation_micros=row["reservation_micros"],
        reservation_window=row["reservation_window"],
        region=row["region"],
        account=row["account"],
        request_json=row["request_json"],
        request_sha256=row["request_sha256"],
        instance_id=row["instance_id"],
        detail=row["detail"],
        price_micros=optional("price_micros", 0) or 0,
        launch_time=optional("launch_time"),
        settled_micros=optional("settled_micros"),
        settled_at=optional("settled_at"),
    )


def _backfilled_cost(row, price_micros, incidental_per_hour):
    """Estimate one version-1 operation's cost from its recorded timestamps.

    A version-1 file has no launch time and no accepted price, so creation time
    is the conservative start and the configured maximum is the conservative
    price. A failed operation created no instance and costs nothing.
    """
    if row["state"] != "terminal" or row["instance_id"] is None:
        return 0
    seconds = int(min(row["updated_at"], row["expires_at"]) - row["created_at"])
    seconds = max(60, seconds)
    return hourly_cost_micros(price_micros, seconds) + hourly_cost_micros(incidental_per_hour, seconds)


def _rollback(connection):
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass
