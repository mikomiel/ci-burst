"""Durable local state for one deployment.

One SQLite file records frozen launch intent, operation identity and consumed
budget allowance. One OS-backed writer lock excludes a second mutating process;
the supported topology is a single controller host with a single ledger, and
resource tags are ownership metadata rather than a distributed lock.
"""

import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import StateRefusal

SCHEMA_VERSION = 1
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

    # -- lifecycle -----------------------------------------------------
    def connect(self, create=True, read_only=False):
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
            elif version != SCHEMA_VERSION:
                raise StateRefusal("STATE_VERSION_UNSUPPORTED", f"schema {version}")
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

    def _connect_read_only(self):
        """Open an existing ledger without creating, migrating or writing it."""
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
        if version != SCHEMA_VERSION:
            connection.close()
            raise StateRefusal("STATE_VERSION_UNSUPPORTED", f"schema {version}")
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
                detail TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX operations_state ON operations (state);
            PRAGMA user_version = 1;
            """
        )

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
    ):
        """Insert frozen intent; the caller owns the enclosing transaction."""
        connection = self._require()
        now = self._clock()
        connection.execute(
            "INSERT INTO operations (id, client_token, state, created_at, updated_at, expires_at,"
            " reservation_micros, reservation_window, region, account, request_json, request_sha256,"
            " instance_id, detail) VALUES (?, ?, 'intent', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
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

    def advance_operation(self, operation_id, *, request, request_sha256, client_token, detail=""):
        """Replace frozen launch parameters with the next ordered candidate.

        The caller only uses this after a definite provider rejection proved
        that the previous request created no instance. Operation identity,
        reservation, admission window and creation time stay unchanged; the
        request, its hash and its own idempotency token advance together, so a
        later resume retries the candidate actually being attempted.
        """
        connection = self._require()
        connection.execute(
            "UPDATE operations SET request_json = ?, request_sha256 = ?, client_token = ?,"
            " detail = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(request, sort_keys=True, separators=(",", ":")),
                request_sha256,
                client_token,
                detail,
                self._clock(),
                operation_id,
            ),
        )

    def consumed_micros(self, window_start):
        """Allowance already committed: current-window spend plus carryover."""
        connection = self._require()
        row = connection.execute(
            "SELECT COALESCE(SUM(reservation_micros), 0) FROM operations"
            " WHERE reservation_window = ? OR state IN (%s)" % ",".join("?" * len(NONTERMINAL)),
            (window_start, *NONTERMINAL),
        ).fetchone()
        return int(row[0])

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
    )
