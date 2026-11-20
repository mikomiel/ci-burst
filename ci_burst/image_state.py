"""Durable local state for automated warm-image builds.

One SQLite file, ``image-state.sqlite``, records the image builds, the observed
pin history, the promotion journal and the build-control counters. It has its
own writer lock and its own schema version, and it lives in a directory that
the scaling code never opens. This module neither imports nor opens the scaling
ledger, so a controller rollback or a defect here cannot make ``apply`` refuse
its ledger. This module holds no AWS or Forgejo code.
"""

import fcntl
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import StateRefusal

SCHEMA_VERSION = 1
STATE_FILE = "image-state.sqlite"

BUILD_STATES = ("building", "imaging", "candidate", "failed", "promoted")
#: The states that count as one builder in flight. The ``one_active`` index and
#: ``active_build`` both read this tuple.
ACTIVE_STATES = ("building", "imaging")
JOURNAL_KINDS = ("promotion", "rollback")
JOURNAL_STATUSES = ("pending", "complete", "aborted")
#: A pin record is a ``promoted`` build row that owns a launch-template version
#: no build made (a pin an operator set by hand). It keeps the evidence that the
#: version and its image exist, so a rollback can verify its target. Its id is
#: ``pin-<version>``.
PIN_RECORD_PREFIX = "pin-"
PIN_RECORD_REASON = "IMAGE_PIN_RECORDED"

_ACTIVE_SQL = ", ".join(repr(state) for state in ACTIVE_STATES)

_SCHEMA_SQL = f"""
    CREATE TABLE image_builds (
        id TEXT PRIMARY KEY,
        deployment TEXT NOT NULL,
        client_token TEXT NOT NULL UNIQUE,
        basis_digest TEXT NOT NULL,
        source_sha TEXT NOT NULL,
        archive_key TEXT,
        archive_sha256 TEXT,
        builder_id TEXT,
        image_id TEXT,
        template_version INTEGER,
        state TEXT NOT NULL CHECK (state IN ({", ".join(repr(s) for s in BUILD_STATES)})),
        reason_code TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        deadline_at REAL NOT NULL,
        script_sha256 TEXT,
        gate_configured INTEGER,
        gate_passed INTEGER,
        image_state TEXT,
        template_version_exists INTEGER,
        drift_result TEXT,
        verified_at REAL,
        source_pin INTEGER
    );
    CREATE UNIQUE INDEX one_active ON image_builds (deployment)
        WHERE state IN ({_ACTIVE_SQL});
    CREATE TABLE pin_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pin_version INTEGER NOT NULL,
        image_id TEXT NOT NULL,
        observed_at REAL NOT NULL
    );
    CREATE TABLE promotion_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL CHECK (kind IN ({", ".join(repr(k) for k in JOURNAL_KINDS)})),
        status TEXT NOT NULL CHECK (status IN ({", ".join(repr(s) for s in JOURNAL_STATUSES)})),
        old_pin INTEGER NOT NULL,
        new_pin INTEGER NOT NULL,
        old_file_sha256 TEXT NOT NULL,
        new_file_sha256 TEXT NOT NULL,
        candidate_id TEXT,
        backup_name TEXT NOT NULL,
        backup_sha256 TEXT NOT NULL,
        contained_pin INTEGER NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE control (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        cooldown_until REAL,
        blocked INTEGER NOT NULL DEFAULT 0,
        last_check_at REAL
    );
    INSERT INTO control (id) VALUES (1);
    PRAGMA user_version = {SCHEMA_VERSION};
"""
#: ``executescript`` commits first, so creation runs these inside one open
#: transaction, one statement at a time. No statement holds a semicolon in a literal.
_SCHEMA_STATEMENTS = tuple(part.strip() for part in _SCHEMA_SQL.split(";") if part.strip())

#: Columns ``advance_build`` may change. The identity columns stay fixed.
_BUILD_UPDATABLE = (
    "archive_key",
    "archive_sha256",
    "builder_id",
    "image_id",
    "template_version",
    "reason_code",
    "deadline_at",
    "script_sha256",
    "gate_configured",
    "gate_passed",
    "image_state",
    "template_version_exists",
    "drift_result",
    "verified_at",
    "source_pin",
)
_CONTROL_UPDATABLE = ("consecutive_failures", "cooldown_until", "blocked", "last_check_at")


@dataclass(frozen=True)
class Build:
    id: str
    deployment: str
    client_token: str
    basis_digest: str
    source_sha: str
    state: str
    reason_code: str
    created_at: float
    updated_at: float
    deadline_at: float
    archive_key: str | None = None
    archive_sha256: str | None = None
    builder_id: str | None = None
    image_id: str | None = None
    template_version: int | None = None
    script_sha256: str | None = None
    #: The verification record that ``promote`` reads.
    gate_configured: int | None = None
    gate_passed: int | None = None
    image_state: str | None = None
    template_version_exists: int | None = None
    drift_result: str | None = None
    verified_at: float | None = None
    source_pin: int | None = None


@dataclass(frozen=True)
class Journal:
    id: int
    kind: str
    status: str
    old_pin: int
    new_pin: int
    old_file_sha256: str
    new_file_sha256: str
    candidate_id: str | None
    backup_name: str
    backup_sha256: str
    contained_pin: int
    created_at: float
    updated_at: float


@dataclass(frozen=True)
class Control:
    consecutive_failures: int
    cooldown_until: float | None
    blocked: bool
    last_check_at: float | None


@dataclass(frozen=True)
class PinObservation:
    pin_version: int
    image_id: str
    observed_at: float


class ImageState:
    """One durable image state file. Construct, then ``connect`` before use."""

    def __init__(self, directory, clock=time.time):
        self.directory = Path(directory)
        self.path = self.directory / STATE_FILE
        self._clock = clock
        self._connection = None
        self._lock_fd = None
        self._in_transaction = False

    # -- lifecycle -----------------------------------------------------
    def connect(self):
        """Open the file, creating it when it is absent or empty.

        A lost file is simply recreated: builds are rebuilt from provider tags
        during reconcile, not from this file. A file at any other schema
        version is refused before it is written, so a newer image release's
        file is never altered by an older one.
        """
        if self._connection is not None:
            return self
        self.directory.mkdir(parents=True, exist_ok=True)
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            connection.row_factory = sqlite3.Row
            version = _user_version(connection)
            if version == 0:
                # Two first opens may both read 0. Take the write lock, read the
                # version again inside it, and create the schema only if it is
                # still 0, so the loser sees the winner's file as valid.
                connection.execute("BEGIN IMMEDIATE")
                try:
                    version = _user_version(connection)
                    if version == 0:
                        for statement in _SCHEMA_STATEMENTS:
                            connection.execute(statement)
                        version = SCHEMA_VERSION
                    connection.execute("COMMIT")
                except BaseException:
                    try:
                        connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
            if version != SCHEMA_VERSION:
                raise StateRefusal("IMAGE_STATE_VERSION_UNSUPPORTED", f"schema {version}")
            connection.execute("PRAGMA journal_mode=DELETE")
        except sqlite3.DatabaseError as error:
            if connection is not None:
                connection.close()
            raise StateRefusal("IMAGE_STATE_UNREADABLE", type(error).__name__) from None
        except BaseException:
            if connection is not None:
                connection.close()
            raise
        self._connection = connection
        return self

    def connect_readonly(self):
        """Open the file for reading only and say whether there was a file to read.

        This creates nothing: no directory, no file, no lock file and no schema,
        so a read-only command leaves the state directory as it found it. An
        absent or empty file gives ``False`` and no connection, and the readers
        then refuse as they do on an unopened state; a caller that wants the
        answers of a new file supplies the defaults itself. A file at another
        schema version is refused as ``connect`` refuses it. A write through
        this connection fails in SQLite.
        """
        if self._connection is not None:
            return True
        if not self.path.is_file():
            return False
        connection = None
        try:
            connection = sqlite3.connect(
                self.path.absolute().as_uri() + "?mode=ro", uri=True, timeout=5, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            version = _user_version(connection)
            if version == 0:
                connection.close()
                return False
            if version != SCHEMA_VERSION:
                raise StateRefusal("IMAGE_STATE_VERSION_UNSUPPORTED", f"schema {version}")
        except sqlite3.DatabaseError as error:
            if connection is not None:
                connection.close()
            raise StateRefusal("IMAGE_STATE_UNREADABLE", type(error).__name__) from None
        except BaseException:
            if connection is not None:
                connection.close()
            raise
        self._connection = connection
        return True

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
        """Take this file's own writer lock or refuse without mutating."""
        if self._lock_fd is not None:
            return self
        lock_path = Path(str(self.path) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise StateRefusal("IMAGE_STATE_LOCKED", str(lock_path)) from None
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
        connection = self._require()
        if self._in_transaction:
            yield connection
            return
        self._in_transaction = True
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            self._in_transaction = False

    # -- builds ---------------------------------------------------------
    def insert_build(self, *, build_id, deployment, client_token, basis_digest, source_sha, deadline_at, **fields):
        """Insert a build in state ``building``; refuse a second active build."""
        _check_columns(fields, _BUILD_UPDATABLE)
        now = self._clock()
        values = {
            "id": build_id,
            "deployment": deployment,
            "client_token": client_token,
            "basis_digest": basis_digest,
            "source_sha": source_sha,
            "state": "building",
            "created_at": now,
            "updated_at": now,
            "deadline_at": deadline_at,
            **fields,
        }
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        with self._write_active_guard():
            self._require().execute(f"INSERT INTO image_builds ({columns}) VALUES ({marks})", tuple(values.values()))
        return self.build(build_id)

    def advance_build(self, build_id, *, state=None, **fields):
        """Change the state and any of the updatable columns of one build."""
        _check_columns(fields, _BUILD_UPDATABLE)
        values = dict(fields)
        if state is not None:
            values["state"] = state
        values["updated_at"] = self._clock()
        assignments = ", ".join(f"{column} = ?" for column in values)
        with self._write_active_guard():
            cursor = self._require().execute(
                f"UPDATE image_builds SET {assignments} WHERE id = ?", (*values.values(), build_id)
            )
        if cursor.rowcount != 1:
            raise StateRefusal("IMAGE_BUILD_UNKNOWN", build_id)
        return self.build(build_id)

    def build(self, build_id):
        row = self._require().execute("SELECT * FROM image_builds WHERE id = ?", (build_id,)).fetchone()
        return _build(row) if row else None

    def active_build(self, deployment):
        """The one build of ``deployment`` in an active state, or ``None``."""
        row = (
            self._require()
            .execute(
                f"SELECT * FROM image_builds WHERE deployment = ? AND state IN ({_ACTIVE_SQL})",
                (deployment,),
            )
            .fetchone()
        )
        return _build(row) if row else None

    def builds(self, deployment):
        """Every build of ``deployment``, oldest first."""
        rows = self._require().execute(
            "SELECT * FROM image_builds WHERE deployment = ? ORDER BY created_at, rowid", (deployment,)
        )
        return [_build(row) for row in rows]

    def builds_started_since(self, deployment, since):
        """How many builds of ``deployment``, in any state, started strictly after ``since``.

        ``since`` is epoch seconds. A build started exactly at ``since`` is not
        counted, which matches the minimum-interval check: a window that has
        run its full length is over. ``build_decision`` takes the count of the
        last day as ``builds_last_day``.
        """
        row = (
            self._require()
            .execute(
                "SELECT COUNT(*) FROM image_builds WHERE deployment = ? AND created_at > ?",
                (deployment, since),
            )
            .fetchone()
        )
        return row[0]

    def last_build(self, deployment):
        """The newest build of ``deployment`` by start time, in any state, or ``None``."""
        return self._newest_build(deployment)

    def last_failed_build(self, deployment):
        """The newest build of ``deployment`` in state ``failed``, or ``None``."""
        return self._newest_build(deployment, "failed")

    def candidate(self, deployment):
        """The newest build of ``deployment`` in state ``candidate``, or ``None``.

        This is the state alone. A build that an earlier pass marked unusable
        is not told apart here.
        """
        return self._newest_build(deployment, "candidate")

    def verified_candidate(self, deployment):
        """The newest ``candidate`` of ``deployment`` that has a ``verified_at``, or ``None``.

        An orphan image is recorded as a candidate that nobody verified, and it
        can be the newest one. A caller that needs a candidate that is fit to
        protect or to promote asks here, not at ``candidate``.
        """
        row = (
            self._require()
            .execute(
                "SELECT * FROM image_builds WHERE deployment = ? AND state = 'candidate'"
                " AND verified_at IS NOT NULL ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (deployment,),
            )
            .fetchone()
        )
        return _build(row) if row else None

    def insert_orphan(self, *, build_id, deployment, basis_digest, source_sha, image_id, created_at, reason_code):
        """Record an image that has no build row as a ``candidate`` that nobody verified.

        ``build_id`` is the id the image's tags name, so the same image is never
        recorded twice. ``created_at`` is the provider's creation time of the
        image (``0.0`` when it gave none): the row must not look like a build that
        started now, because the daily cap and the minimum interval count the
        start times of rows. The row is not active, so it cannot meet the
        ``one_active`` index, and ``verified_at`` stays empty, so nothing treats
        it as verified. The client token is derived from the id and cannot meet
        the token of a real build.
        """
        now = self._clock()
        values = {
            "id": build_id,
            "deployment": deployment,
            "client_token": f"orphan:{build_id}",
            "basis_digest": basis_digest,
            "source_sha": source_sha,
            "image_id": image_id,
            "state": "candidate",
            "reason_code": reason_code,
            "created_at": created_at,
            "updated_at": now,
            "deadline_at": created_at,
        }
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        self._require().execute(f"INSERT INTO image_builds ({columns}) VALUES ({marks})", tuple(values.values()))
        return self.build(build_id)

    def version_owner(self, deployment, version):
        """The newest ``candidate`` or ``promoted`` row that owns launch-template ``version``, or ``None``.

        A failed build never owns a version: a drifted version is unusable. A pin
        record (see ``insert_pin_record``) owns the version of a hand-set pin.
        """
        row = (
            self._require()
            .execute(
                "SELECT * FROM image_builds WHERE deployment = ? AND template_version = ?"
                " AND state IN ('candidate', 'promoted') ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (deployment, version),
            )
            .fetchone()
        )
        return _build(row) if row else None

    def insert_pin_record(self, *, deployment, version, image_id):
        """Record the image behind a pin that no build owns, as a ``promoted`` row; return the row.

        The row is ``pin-<version>``. It starts with no verification, so it
        proves nothing until ``record_verification`` stamps it. ``created_at`` is
        ``0.0``, so the daily cap and the minimum interval never count it (as
        with an orphan), and it is not active, so it cannot meet the
        ``one_active`` index. A second call for the same version returns the row
        that exists.
        """
        build_id = f"{PIN_RECORD_PREFIX}{version}"
        existing = self.build(build_id)
        if existing is not None:
            return existing
        values = {
            "id": build_id,
            "deployment": deployment,
            "client_token": f"pin:{version}",
            "basis_digest": "",
            "source_sha": "",
            "image_id": image_id,
            "template_version": version,
            "state": "promoted",
            "reason_code": PIN_RECORD_REASON,
            "created_at": 0.0,
            "updated_at": self._clock(),
            "deadline_at": 0.0,
        }
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        self._require().execute(f"INSERT INTO image_builds ({columns}) VALUES ({marks})", tuple(values.values()))
        return self.build(build_id)

    def record_verification(self, build_id, *, image_state, template_version_exists, drift_result, verified_at):
        """Replace the verification evidence of one row and nothing else.

        ``updated_at`` stays as it was: for a candidate it is the moment the
        build ended, which ``promote`` reports as the build duration.
        """
        cursor = self._require().execute(
            "UPDATE image_builds SET image_state = ?, template_version_exists = ?, drift_result = ?,"
            " verified_at = ? WHERE id = ?",
            (image_state, template_version_exists, drift_result, verified_at, build_id),
        )
        if cursor.rowcount != 1:
            raise StateRefusal("IMAGE_BUILD_UNKNOWN", build_id)
        return self.build(build_id)

    def _newest_build(self, deployment, state=None):
        clause, arguments = ("", (deployment,)) if state is None else (" AND state = ?", (deployment, state))
        row = (
            self._require()
            .execute(
                f"SELECT * FROM image_builds WHERE deployment = ?{clause} ORDER BY created_at DESC, rowid DESC LIMIT 1",
                arguments,
            )
            .fetchone()
        )
        return _build(row) if row else None

    @contextmanager
    def _write_active_guard(self):
        """Report the ``one_active`` index as a named refusal, not a raw error."""
        try:
            yield
        except sqlite3.IntegrityError as error:
            if "image_builds.deployment" in str(error):
                raise StateRefusal("IMAGE_BUILD_ALREADY_ACTIVE") from None
            raise

    # -- promotion journal ----------------------------------------------
    def journal_insert(
        self,
        *,
        kind,
        old_pin,
        new_pin,
        old_file_sha256,
        new_file_sha256,
        backup_name,
        backup_sha256,
        contained_pin,
        candidate_id=None,
    ):
        """Record a ``pending`` journal row. The caller commits it before the rename."""
        now = self._clock()
        cursor = self._require().execute(
            "INSERT INTO promotion_journal (kind, status, old_pin, new_pin, old_file_sha256,"
            " new_file_sha256, candidate_id, backup_name, backup_sha256, contained_pin,"
            " created_at, updated_at) VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind,
                old_pin,
                new_pin,
                old_file_sha256,
                new_file_sha256,
                candidate_id,
                backup_name,
                backup_sha256,
                contained_pin,
                now,
                now,
            ),
        )
        return self.journal(cursor.lastrowid)

    def journal_complete(self, journal_id):
        return self._journal_resolve(journal_id, "complete")

    def journal_abort(self, journal_id):
        return self._journal_resolve(journal_id, "aborted")

    def _journal_resolve(self, journal_id, status):
        cursor = self._require().execute(
            "UPDATE promotion_journal SET status = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
            (status, self._clock(), journal_id),
        )
        if cursor.rowcount != 1:
            raise StateRefusal("IMAGE_JOURNAL_NOT_PENDING", str(journal_id))
        return self.journal(journal_id)

    def journal(self, journal_id):
        row = self._require().execute("SELECT * FROM promotion_journal WHERE id = ?", (journal_id,)).fetchone()
        return Journal(**dict(row)) if row else None

    def pending_journal(self):
        """Every ``pending`` row, oldest first."""
        rows = self._require().execute("SELECT * FROM promotion_journal WHERE status = 'pending' ORDER BY id")
        return [Journal(**dict(row)) for row in rows]

    def journal_last_complete(self, kind=None):
        """The newest ``complete`` row, or ``None``: its ``old_pin`` is the pin it replaced.

        Without ``kind`` the row is of either kind, which is what the deletion
        rule needs. A rollback asks for ``kind="promotion"``.
        """
        clause, arguments = ("", ()) if kind is None else (" AND kind = ?", (kind,))
        row = (
            self._require()
            .execute(
                f"SELECT * FROM promotion_journal WHERE status = 'complete'{clause} ORDER BY id DESC LIMIT 1",
                arguments,
            )
            .fetchone()
        )
        return Journal(**dict(row)) if row else None

    # -- observed pins ----------------------------------------------------
    def pin_last(self):
        """The newest pin observation, or ``None`` before the first."""
        row = self._require().execute("SELECT * FROM pin_log ORDER BY id DESC LIMIT 1").fetchone()
        return PinObservation(row["pin_version"], row["image_id"], row["observed_at"]) if row else None

    def pin_for_version(self, pin_version):
        """The newest observation of ``pin_version``, or ``None`` when it was never observed."""
        row = (
            self._require()
            .execute("SELECT * FROM pin_log WHERE pin_version = ? ORDER BY id DESC LIMIT 1", (pin_version,))
            .fetchone()
        )
        return PinObservation(row["pin_version"], row["image_id"], row["observed_at"]) if row else None

    def pin_observe(self, pin_version, image_id):
        """Log that the pin ``pin_version`` names ``image_id``, unless the newest row already says so.

        The log records changes, not passes, so it stays as short as the pin's
        history. The result is true when a row was added.
        """
        with self.transaction() as connection:
            last = self.pin_last()
            if last is not None and (last.pin_version, last.image_id) == (pin_version, image_id):
                return False
            connection.execute(
                "INSERT INTO pin_log (pin_version, image_id, observed_at) VALUES (?, ?, ?)",
                (pin_version, image_id, self._clock()),
            )
            return True

    # -- control --------------------------------------------------------
    def control_get(self):
        row = self._require().execute("SELECT * FROM control WHERE id = 1").fetchone()
        return Control(
            consecutive_failures=row["consecutive_failures"],
            cooldown_until=row["cooldown_until"],
            blocked=bool(row["blocked"]),
            last_check_at=row["last_check_at"],
        )

    def control_set(self, **fields):
        _check_columns(fields, _CONTROL_UPDATABLE)
        if not fields:
            return self.control_get()
        assignments = ", ".join(f"{column} = ?" for column in fields)
        self._require().execute(f"UPDATE control SET {assignments} WHERE id = 1", tuple(fields.values()))
        return self.control_get()

    def _require(self):
        if self._connection is None:
            raise StateRefusal("IMAGE_STATE_UNAVAILABLE")
        return self._connection


def _user_version(connection):
    return connection.execute("PRAGMA user_version").fetchone()[0]


def _check_columns(fields, allowed):
    unknown = sorted(set(fields) - set(allowed))
    if unknown:
        raise ValueError("unknown image state column: " + ", ".join(unknown))


def _build(row):
    return Build(**dict(row))
