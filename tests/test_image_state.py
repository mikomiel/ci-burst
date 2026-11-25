import hashlib
import os
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from ci_burst import image_state
from ci_burst.config import load
from ci_burst.controller import Controller
from ci_burst.errors import StateRefusal
from ci_burst.image_state import (
    PIN_RECORD_PREFIX,
    PIN_RECORD_REASON,
    SCHEMA_VERSION,
    STATE_FILE,
    Build,
    ImageState,
    PinObservation,
)
from ci_burst.ledger import SCHEMA_VERSION as LEDGER_SCHEMA_VERSION
from ci_burst.ledger import Ledger, current_window

from support import Clock, FakeAws, FakeObserver, observation_demand, quote, write_config

PACKAGE = Path(image_state.__file__).parent


def sha256_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ImageStateCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.directory = self.root / "image"
        self.clock = Clock()
        self.state = ImageState(self.directory, clock=self.clock).connect()
        self.addCleanup(self.state.close)

    def add_build(self, build_id="build-1", **overrides):
        values = {
            "build_id": build_id,
            "deployment": "example-deployment",
            "client_token": "token-" + build_id,
            "basis_digest": "a" * 64,
            "source_sha": "b" * 40,
            "deadline_at": self.clock.now + 2700,
        }
        values.update(overrides)
        with self.state.transaction():
            return self.state.insert_build(**values)

    def add_journal(self, **overrides):
        values = {
            "kind": "promotion",
            "old_pin": 3,
            "new_pin": 4,
            "old_file_sha256": "1" * 64,
            "new_file_sha256": "2" * 64,
            "candidate_id": "build-1",
            "backup_name": "config.toml.bak.20260101T000000Z",
            "backup_sha256": "1" * 64,
            "contained_pin": 3,
        }
        values.update(overrides)
        with self.state.transaction():
            return self.state.journal_insert(**values)


class SchemaTests(ImageStateCase):
    def test_the_file_has_its_own_name_and_schema_version(self):
        self.assertEqual(self.state.path, self.directory / "image-state.sqlite")
        self.assertEqual(STATE_FILE, "image-state.sqlite")
        connection = sqlite3.connect(self.state.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"image_builds", "pin_log", "promotion_journal", "control"} <= tables)

    def test_a_build_round_trips_and_advances(self):
        build = self.add_build()
        self.assertEqual(build.state, "building")
        self.assertEqual(self.state.build("build-1"), build)
        self.clock.advance(60)
        with self.state.transaction():
            advanced = self.state.advance_build(
                "build-1", state="imaging", builder_id="i-0123456789abcdef0", archive_sha256="c" * 64
            )
        self.assertEqual(advanced.state, "imaging")
        self.assertEqual(advanced.builder_id, "i-0123456789abcdef0")
        self.assertEqual(advanced.created_at + 60, advanced.updated_at)
        self.assertEqual(self.state.active_build("example-deployment"), advanced)

    def test_an_unknown_state_or_column_is_refused(self):
        self.add_build()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.state.transaction():
                self.state.advance_build("build-1", state="running")
        with self.assertRaises(ValueError):
            with self.state.transaction():
                self.state.advance_build("build-1", client_token="other")
        with self.assertRaises(StateRefusal) as raised:
            with self.state.transaction():
                self.state.advance_build("missing", state="failed")
        self.assertEqual(raised.exception.reason, "IMAGE_BUILD_UNKNOWN")

    def test_the_active_build_is_none_without_one(self):
        self.assertIsNone(self.state.active_build("example-deployment"))
        self.add_build()
        with self.state.transaction():
            self.state.advance_build("build-1", state="candidate")
        self.assertIsNone(self.state.active_build("example-deployment"))

    def test_the_journal_inserts_and_resolves_once(self):
        row = self.add_journal()
        self.assertEqual((row.kind, row.status, row.contained_pin), ("promotion", "pending", 3))
        self.assertEqual(self.state.pending_journal(), [row])
        self.clock.advance(5)
        with self.state.transaction():
            done = self.state.journal_complete(row.id)
        self.assertEqual(done.status, "complete")
        self.assertEqual(self.state.pending_journal(), [])
        for resolve in (self.state.journal_complete, self.state.journal_abort):
            with self.assertRaises(StateRefusal) as raised:
                with self.state.transaction():
                    resolve(row.id)
            self.assertEqual(raised.exception.reason, "IMAGE_JOURNAL_NOT_PENDING")
        self.assertEqual(self.state.journal(row.id).status, "complete")

    def test_the_journal_aborts_and_records_a_rollback(self):
        row = self.add_journal(kind="rollback", candidate_id=None)
        with self.state.transaction():
            aborted = self.state.journal_abort(row.id)
        self.assertEqual((aborted.kind, aborted.status, aborted.candidate_id), ("rollback", "aborted", None))

    def test_the_journal_refuses_an_unknown_kind(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_journal(kind="rewrite")

    def test_a_pending_journal_row_survives_a_reopen(self):
        row = self.add_journal()
        self.state.close()
        reopened = ImageState(self.directory, clock=self.clock).connect()
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.pending_journal(), [row])

    def test_control_has_one_row_with_defaults(self):
        control = self.state.control_get()
        self.assertEqual((control.consecutive_failures, control.blocked), (0, False))
        self.assertIsNone(control.cooldown_until)
        self.assertIsNone(control.last_check_at)
        updated = self.state.control_set(consecutive_failures=3, blocked=True, cooldown_until=12.5)
        self.assertEqual((updated.consecutive_failures, updated.blocked, updated.cooldown_until), (3, True, 12.5))
        with self.assertRaises(ValueError):
            self.state.control_set(id=2)
        connection = sqlite3.connect(self.state.path)
        self.addCleanup(connection.close)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO control (id) VALUES (2)")


class SingleFlightTests(ImageStateCase):
    def test_the_index_is_partial_and_keyed_on_the_deployment(self):
        connection = sqlite3.connect(self.state.path)
        self.addCleanup(connection.close)
        (sql,) = connection.execute("SELECT sql FROM sqlite_master WHERE name = 'one_active'").fetchone()
        normalized = " ".join(sql.split())
        self.assertIn("UNIQUE INDEX one_active ON image_builds (deployment)", normalized)
        self.assertIn("WHERE state IN ('building', 'imaging')", normalized)

    def test_a_second_active_row_fails_on_the_index(self):
        self.add_build("build-1")
        with self.assertRaises(StateRefusal) as raised:
            self.add_build("build-2")
        self.assertEqual(raised.exception.reason, "IMAGE_BUILD_ALREADY_ACTIVE")
        self.assertIsNone(self.state.build("build-2"))
        # The index itself, not the accessor, enforces it.
        connection = sqlite3.connect(self.state.path)
        self.addCleanup(connection.close)
        with self.assertRaises(sqlite3.IntegrityError) as raw:
            connection.execute(
                "INSERT INTO image_builds (id, deployment, client_token, basis_digest, source_sha, state,"
                " created_at, updated_at, deadline_at) VALUES ('raw', 'example-deployment', 'raw', 'd', 's',"
                " 'imaging', 1, 1, 2)"
            )
        self.assertIn("image_builds.deployment", str(raw.exception))

    def test_a_candidate_row_does_not_block_a_new_build(self):
        self.add_build("build-1")
        with self.state.transaction():
            self.state.advance_build("build-1", state="candidate")
        second = self.add_build("build-2")
        self.assertEqual(second.state, "building")
        self.assertEqual(self.state.active_build("example-deployment").id, "build-2")
        for terminal in ("failed", "promoted"):
            with self.state.transaction():
                self.state.advance_build("build-2", state=terminal)
            self.add_build("build-after-" + terminal)
            with self.state.transaction():
                self.state.advance_build("build-after-" + terminal, state="candidate")

    def test_a_terminal_build_cannot_return_to_an_active_state_beside_another(self):
        self.add_build("build-1")
        with self.state.transaction():
            self.state.advance_build("build-1", state="failed")
        self.add_build("build-2")
        with self.assertRaises(StateRefusal) as raised:
            with self.state.transaction():
                self.state.advance_build("build-1", state="building")
        self.assertEqual(raised.exception.reason, "IMAGE_BUILD_ALREADY_ACTIVE")
        self.assertEqual(self.state.build("build-1").state, "failed")

    def test_another_deployment_may_have_its_own_active_build(self):
        self.add_build("build-1")
        other = self.add_build("build-2", deployment="example-other")
        self.assertEqual(other.state, "building")

    def test_a_duplicate_client_token_is_not_reported_as_an_active_build(self):
        self.add_build("build-1")
        with self.state.transaction():
            self.state.advance_build("build-1", state="candidate")
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_build("build-2", client_token="token-build-1")


class LifecycleTests(ImageStateCase):
    def test_close_releases_the_writer_lock_even_when_the_connection_close_raises(self):
        self.state.acquire_writer()
        real = self.state._connection
        failing = mock.Mock()
        failing.close.side_effect = sqlite3.OperationalError("synthetic close failure")
        self.state._connection = failing
        self.addCleanup(real.close)
        with self.assertRaises(sqlite3.OperationalError):
            self.state.close()
        self.assertFalse(self.state.available)
        other = ImageState(self.directory, clock=self.clock)
        self.addCleanup(other.close)
        other.acquire_writer()  # refuses IMAGE_STATE_LOCKED if close had kept the lock

    def test_a_lost_file_is_recreated_without_error(self):
        self.add_build()
        self.state.close()
        self.state.path.unlink()
        reopened = ImageState(self.directory, clock=self.clock).connect()
        self.addCleanup(reopened.close)
        self.assertTrue(self.state.path.exists())
        self.assertIsNone(reopened.active_build("example-deployment"))
        self.assertEqual(reopened.control_get().consecutive_failures, 0)
        self.assertEqual(reopened.pending_journal(), [])
        # The recreated file works.
        with reopened.transaction():
            reopened.insert_build(
                build_id="again",
                deployment="example-deployment",
                client_token="again",
                basis_digest="a" * 64,
                source_sha="b" * 40,
                deadline_at=1.0,
            )

    def test_a_lost_directory_is_recreated(self):
        self.state.close()
        self.state.path.unlink()
        self.state.path.with_name(STATE_FILE + ".lock").unlink(missing_ok=True)
        self.directory.rmdir()
        reopened = ImageState(self.directory, clock=self.clock).connect()
        self.addCleanup(reopened.close)
        self.assertTrue(self.state.path.exists())

    def test_an_empty_file_is_initialised(self):
        self.state.close()
        self.state.path.write_bytes(b"")
        reopened = ImageState(self.directory, clock=self.clock).connect()
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.control_get().consecutive_failures, 0)

    def test_a_newer_schema_version_is_refused_without_a_change(self):
        self.state.close()
        connection = sqlite3.connect(self.state.path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.commit()
        connection.close()
        before = sha256_of(self.state.path)
        with self.assertRaises(StateRefusal) as raised:
            ImageState(self.directory).connect()
        self.assertEqual(raised.exception.reason, "IMAGE_STATE_VERSION_UNSUPPORTED")
        self.assertEqual(raised.exception.detail, f"schema {SCHEMA_VERSION + 1}")
        self.assertEqual(sha256_of(self.state.path), before)

    def test_a_corrupt_file_is_refused_by_name(self):
        self.state.close()
        self.state.path.write_bytes(b"not a database at all, but longer than a header")
        with self.assertRaises(StateRefusal) as raised:
            ImageState(self.directory).connect()
        self.assertEqual(raised.exception.reason, "IMAGE_STATE_UNREADABLE")

    def test_use_before_connect_is_refused(self):
        with self.assertRaises(StateRefusal) as raised:
            ImageState(self.directory).control_get()
        self.assertEqual(raised.exception.reason, "IMAGE_STATE_UNAVAILABLE")

    def test_a_rolled_back_transaction_leaves_nothing(self):
        with self.assertRaises(RuntimeError):
            with self.state.transaction():
                self.state.insert_build(
                    build_id="gone",
                    deployment="example-deployment",
                    client_token="gone",
                    basis_digest="a" * 64,
                    source_sha="b" * 40,
                    deadline_at=1.0,
                )
                raise RuntimeError("boom")
        self.assertIsNone(self.state.build("gone"))

    def test_a_second_writer_is_refused_and_the_lock_is_released(self):
        self.state.acquire_writer()
        other = ImageState(self.directory).connect()
        self.addCleanup(other.close)
        with self.assertRaises(StateRefusal) as raised:
            other.acquire_writer()
        self.assertEqual(raised.exception.reason, "IMAGE_STATE_LOCKED")
        self.state.release_writer()
        other.acquire_writer()

    def test_the_lock_file_is_the_image_files_own(self):
        self.state.acquire_writer()
        self.assertTrue((self.directory / "image-state.sqlite.lock").exists())
        self.assertFalse((self.directory / "state.sqlite3.lock").exists())


class FirstOpenRaceTests(unittest.TestCase):
    """Two first opens that both read version 0 must both succeed."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.directory = Path(self._directory.name) / "image"

    def test_a_loser_that_read_version_zero_sees_the_winners_file_as_valid(self):
        real_connect = sqlite3.connect
        winner = []
        calls = []

        class AfterFirstVersionRead:
            """Run the winner's whole first open right after the loser reads 0."""

            def __init__(self, connection):
                object.__setattr__(self, "_connection", connection)
                object.__setattr__(self, "_fired", False)

            def __getattr__(self, name):
                return getattr(self._connection, name)

            def __setattr__(self, name, value):
                setattr(self._connection, name, value)

            def execute(self, sql, *args):
                result = self._connection.execute(sql, *args)
                if "user_version" in sql and not self._fired:
                    object.__setattr__(self, "_fired", True)
                    self_row = result.fetchone()
                    self_version = self_row[0]
                    calls.append(self_version)
                    winner.append(ImageState(directory).connect())
                    return mock.Mock(fetchone=lambda: (self_version,))
                return result

        directory = self.directory

        def connect(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            return connection if calls else AfterFirstVersionRead(connection)

        with mock.patch.object(image_state.sqlite3, "connect", connect):
            loser = ImageState(directory).connect()
        self.addCleanup(loser.close)
        self.addCleanup(winner[0].close)
        self.assertEqual(calls, [0])
        self.assertTrue(loser.available)
        self.assertEqual(loser.control_get().consecutive_failures, 0)
        raw = real_connect(loser.path)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_a_file_that_turns_newer_before_the_exclusion_is_refused_unwritten(self):
        real_connect = sqlite3.connect
        self.directory.mkdir(parents=True)
        path = self.directory / STATE_FILE
        directory = self.directory

        class Newer:
            def __init__(self, connection):
                object.__setattr__(self, "_connection", connection)
                object.__setattr__(self, "_seen", 0)

            def __getattr__(self, name):
                return getattr(self._connection, name)

            def __setattr__(self, name, value):
                setattr(self._connection, name, value)

            def execute(self, sql, *args):
                if "user_version" in sql and sql.startswith("PRAGMA"):
                    seen = self._seen
                    object.__setattr__(self, "_seen", seen + 1)
                    if seen == 0:
                        return mock.Mock(fetchone=lambda: (0,))
                    return mock.Mock(fetchone=lambda: (SCHEMA_VERSION + 1,))
                return self._connection.execute(sql, *args)

        with mock.patch.object(image_state.sqlite3, "connect", lambda *a, **k: Newer(real_connect(*a, **k))):
            with self.assertRaises(StateRefusal) as raised:
                ImageState(directory).connect()
        self.assertEqual(raised.exception.reason, "IMAGE_STATE_VERSION_UNSUPPORTED")
        raw = real_connect(path)
        self.addCleanup(raw.close)
        self.assertEqual(raw.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)

    def test_simultaneous_first_opens_all_succeed(self):
        barrier = threading.Barrier(6)
        results = []

        def open_state():
            state = ImageState(self.directory)
            try:
                barrier.wait()
                state.connect()
                results.append(state.control_get().consecutive_failures)
            except BaseException as error:  # reported by the assertion below
                results.append(error)
            finally:
                state.close()

        threads = [threading.Thread(target=open_state) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, [0] * 6)


class ScalingLedgerSeparationTests(unittest.TestCase):
    """The image state file and the scaling ledger share nothing."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.clock = Clock()

    def test_the_image_file_never_touches_the_ledger_file_or_its_version(self):
        ledger_path = self.root / "state.sqlite3"
        ledger = Ledger(ledger_path, clock=self.clock).connect()
        ledger.close()
        before = (sha256_of(ledger_path), os.stat(ledger_path).st_mtime_ns)
        # Same directory on purpose: the strongest case for interference.
        state = ImageState(self.root, clock=self.clock).connect()
        self.addCleanup(state.close)
        state.acquire_writer()
        with state.transaction():
            state.insert_build(
                build_id="build-1",
                deployment="example-deployment",
                client_token="token",
                basis_digest="a" * 64,
                source_sha="b" * 40,
                deadline_at=1.0,
            )
        state.control_set(blocked=True)
        self.assertEqual((sha256_of(ledger_path), os.stat(ledger_path).st_mtime_ns), before)
        connection = sqlite3.connect(ledger_path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], LEDGER_SCHEMA_VERSION)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables, {"operations", "fleet_observations"})
        self.assertNotEqual(state.path, ledger_path)
        self.assertNotEqual(Path(str(state.path) + ".lock"), Path(str(ledger_path) + ".lock"))
        # The two schema versions are independent numbers on independent files.
        image = sqlite3.connect(state.path)
        self.addCleanup(image.close)
        self.assertEqual(image.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_a_newer_ledger_schema_leaves_the_image_file_openable(self):
        ledger_path = self.root / "state.sqlite3"
        connection = sqlite3.connect(ledger_path)
        connection.execute(f"PRAGMA user_version = {LEDGER_SCHEMA_VERSION + 1}")
        connection.commit()
        connection.close()
        state = ImageState(self.root, clock=self.clock).connect()
        self.addCleanup(state.close)
        self.assertEqual(state.control_get().consecutive_failures, 0)


class ScalingPathIgnoresImageStateTests(unittest.TestCase):
    """Test 3.4: the scaling ledger opens unchanged beside an image state file."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.clock = Clock()
        self.config = load(write_config(self.root))

    def seed_image_file(self):
        state = ImageState(self.config.state.database.parent, clock=self.clock).connect()
        with state.transaction():
            state.insert_build(
                build_id="build-1",
                deployment="example-deployment",
                client_token="token",
                basis_digest="a" * 64,
                source_sha="b" * 40,
                deadline_at=1.0,
            )
        state.acquire_writer()
        state.close()
        return state.path

    def test_the_image_file_lies_in_the_ledger_directory(self):
        path = self.seed_image_file()
        self.assertEqual(path.parent, self.config.state.database.parent)

    def test_the_ledger_opens_and_creates_its_schema_beside_the_image_file(self):
        image_path = self.seed_image_file()
        before = sha256_of(image_path)
        ledger = Ledger(self.config.state.database, clock=self.clock)
        ledger.acquire_writer()
        ledger.connect()
        self.addCleanup(ledger.close)
        self.assertEqual(ledger.version, LEDGER_SCHEMA_VERSION)
        self.assertEqual(ledger.operations(), [])
        self.assertEqual(sha256_of(image_path), before)

    def test_apply_runs_unchanged_beside_the_image_file(self):
        image_path = self.seed_image_file()
        before = sha256_of(image_path)
        aws = FakeAws(spot_quote=quote(clock=self.clock))
        controller = Controller(
            self.config,
            observer=FakeObserver(observation_demand()),
            aws=aws,
            ledger=Ledger(self.config.state.database, clock=self.clock),
            clock=self.clock,
        )
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        self.assertEqual(sha256_of(image_path), before)

    def test_an_unreadable_image_file_beside_the_ledger_does_not_matter(self):
        """A file the ledger cannot parse proves the ledger never reads it."""
        image_path = self.config.state.database.parent / STATE_FILE
        image_path.write_bytes(b"not a database at all, but longer than a header")
        (image_path.parent / (STATE_FILE + ".lock")).write_bytes(b"")
        controller = Controller(
            self.config,
            observer=FakeObserver(observation_demand()),
            aws=FakeAws(spot_quote=quote(clock=self.clock)),
            ledger=Ledger(self.config.state.database, clock=self.clock),
            clock=self.clock,
        )
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        self.assertEqual(image_path.read_bytes(), b"not a database at all, but longer than a header")

    def test_the_ledger_opens_no_path_of_the_image_file(self):
        image_path = self.seed_image_file()
        touched = []
        real_connect, real_open, real_os_open = sqlite3.connect, open, os.open

        def connect(database, *args, **kwargs):
            touched.append(str(database))
            return real_connect(database, *args, **kwargs)

        def opened(file, *args, **kwargs):
            touched.append(str(file))
            return real_open(file, *args, **kwargs)

        def os_opened(path, *args, **kwargs):
            touched.append(str(path))
            return real_os_open(path, *args, **kwargs)

        controller = Controller(
            self.config,
            observer=FakeObserver(observation_demand()),
            aws=FakeAws(spot_quote=quote(clock=self.clock)),
            ledger=Ledger(self.config.state.database, clock=self.clock),
            clock=self.clock,
        )
        with (
            mock.patch("sqlite3.connect", connect),
            mock.patch("builtins.open", opened),
            mock.patch("os.open", os_opened),
        ):
            controller.apply()
        self.assertTrue(any(str(self.config.state.database) in path for path in touched))
        self.assertFalse([path for path in touched if STATE_FILE in path], touched)
        self.assertTrue(image_path.exists())

    def test_the_scaling_modules_do_not_name_the_image_file(self):
        for name in ("ledger.py", "controller.py"):
            source = (PACKAGE / name).read_text()
            for forbidden in ("image_state", "image-state", "ImageState"):
                self.assertNotIn(forbidden, source, f"{name} mentions {forbidden}")

    def test_the_image_module_does_not_import_the_ledger(self):
        source = (PACKAGE / "image_state.py").read_text()
        for forbidden in ("ledger", "controller", "import boto3", "forgejo", "aws"):
            self.assertNotIn("import " + forbidden, source)
            self.assertNotIn("from ." + forbidden, source)


class BuildReadTests(ImageStateCase):
    """The read-only questions that the build and deletion passes ask of the builds."""

    DEPLOYMENT = "example-deployment"
    OTHER = "example-other-deployment"
    T0 = 1_800_000_000.0

    def started(self, build_id, at, *, state=None, deployment=None):
        """Insert a build that started at ``at``, then move it to ``state`` when one is given."""
        self.clock.now = at
        build = self.add_build(build_id, deployment=deployment or self.DEPLOYMENT)
        if state is not None:
            with self.state.transaction():
                build = self.state.advance_build(build_id, state=state)
        return build

    def ids(self, builds):
        return [build.id for build in builds]

    def test_builds_are_listed_oldest_first_for_one_deployment(self):
        self.started("b-new", self.T0 + 200, state="failed")
        self.started("b-old", self.T0, state="failed")
        self.started("b-mid", self.T0 + 100, state="candidate")
        self.started("o-1", self.T0 + 50, state="failed", deployment=self.OTHER)
        self.assertEqual(self.ids(self.state.builds(self.DEPLOYMENT)), ["b-old", "b-mid", "b-new"])
        self.assertEqual(self.ids(self.state.builds(self.OTHER)), ["o-1"])
        self.assertEqual(self.state.builds("example-unknown-deployment"), [])

    def test_builds_that_started_together_keep_their_insertion_order(self):
        self.started("b-first", self.T0, state="failed")
        self.started("b-second", self.T0, state="failed")
        self.assertEqual(self.ids(self.state.builds(self.DEPLOYMENT)), ["b-first", "b-second"])
        self.assertEqual(self.state.last_build(self.DEPLOYMENT).id, "b-second")
        self.assertEqual(self.state.last_failed_build(self.DEPLOYMENT).id, "b-second")

    def test_builds_started_since_counts_strictly_after_the_bound(self):
        self.started("b-1", self.T0 - 10, state="failed")
        self.started("b-2", self.T0, state="failed")
        self.started("b-3", self.T0 + 10, state="candidate")
        counts = {
            since: self.state.builds_started_since(self.DEPLOYMENT, since)
            for since in (self.T0 - 11, self.T0 - 10, self.T0 - 1, self.T0, self.T0 + 10)
        }
        self.assertEqual(counts, {self.T0 - 11: 3, self.T0 - 10: 2, self.T0 - 1: 2, self.T0: 1, self.T0 + 10: 0})

    def test_builds_started_since_counts_every_state(self):
        for number, state in enumerate(("failed", "candidate", "promoted")):
            self.started(f"b-{number}", self.T0 + number, state=state)
        self.assertEqual(self.state.builds_started_since(self.DEPLOYMENT, self.T0 - 1), 3)
        self.started("b-3", self.T0 + 3)
        self.assertEqual(self.state.builds(self.DEPLOYMENT)[-1].state, "building")
        self.assertEqual(self.state.builds_started_since(self.DEPLOYMENT, self.T0 - 1), 4)

    def test_builds_started_since_counts_one_deployment(self):
        self.started("b-1", self.T0, state="failed")
        self.started("o-1", self.T0, state="failed", deployment=self.OTHER)
        self.started("o-2", self.T0 + 1, state="candidate", deployment=self.OTHER)
        self.assertEqual(self.state.builds_started_since(self.DEPLOYMENT, self.T0 - 1), 1)
        self.assertEqual(self.state.builds_started_since(self.OTHER, self.T0 - 1), 2)
        self.assertEqual(self.state.builds_started_since("example-unknown-deployment", self.T0 - 1), 0)

    def test_the_last_build_is_the_newest_by_start_time_in_any_state(self):
        self.assertIsNone(self.state.last_build(self.DEPLOYMENT))
        # Inserted newest first, so the start time and not the insertion order decides.
        newest = self.started("b-newest", self.T0 + 100, state="promoted")
        self.started("b-older", self.T0 + 50, state="failed")
        self.started("o-1", self.T0 + 900, state="failed", deployment=self.OTHER)
        self.assertEqual(self.state.last_build(self.DEPLOYMENT), newest)
        self.assertEqual(self.state.last_build(self.OTHER).id, "o-1")
        self.assertIsNone(self.state.last_build("example-unknown-deployment"))

    def test_the_last_failed_build_skips_newer_builds_in_other_states(self):
        self.assertIsNone(self.state.last_failed_build(self.DEPLOYMENT))
        self.started("b-old-failed", self.T0, state="failed")
        failed = self.started("b-new-failed", self.T0 + 10, state="failed")
        self.started("b-candidate", self.T0 + 20, state="candidate")
        self.started("b-promoted", self.T0 + 30, state="promoted")
        self.started("b-building", self.T0 + 40)
        self.started("o-1", self.T0 + 50, state="failed", deployment=self.OTHER)
        self.assertEqual(self.state.last_failed_build(self.DEPLOYMENT), failed)
        self.assertEqual(self.state.last_build(self.DEPLOYMENT).id, "b-building")

    def test_no_failed_build_gives_none_even_with_other_builds(self):
        self.started("b-candidate", self.T0, state="candidate")
        self.started("b-promoted", self.T0 + 10, state="promoted")
        self.assertIsNone(self.state.last_failed_build(self.DEPLOYMENT))

    def test_the_candidate_is_the_newest_build_in_state_candidate(self):
        self.assertIsNone(self.state.candidate(self.DEPLOYMENT))
        self.started("b-old-candidate", self.T0, state="candidate")
        newest = self.started("b-new-candidate", self.T0 + 10, state="candidate")
        self.started("b-failed", self.T0 + 20, state="failed")
        self.started("b-promoted", self.T0 + 30, state="promoted")
        self.started("b-building", self.T0 + 40)
        self.started("o-1", self.T0 + 50, state="candidate", deployment=self.OTHER)
        self.assertEqual(self.state.candidate(self.DEPLOYMENT), newest)
        self.assertEqual(self.state.candidate(self.OTHER).id, "o-1")

    def test_only_builds_in_other_states_give_no_candidate(self):
        self.started("b-failed", self.T0, state="failed")
        self.started("b-promoted", self.T0 + 10, state="promoted")
        self.started("b-building", self.T0 + 20)
        self.assertIsNone(self.state.candidate(self.DEPLOYMENT))

    def test_the_reads_write_nothing(self):
        self.started("b-1", self.T0, state="failed")
        self.started("b-2", self.T0 + 10, state="candidate")
        self.started("b-3", self.T0 + 20)
        self.add_journal()
        before = self.state.path.read_bytes()
        self.state.builds(self.DEPLOYMENT)
        self.state.builds_started_since(self.DEPLOYMENT, self.T0)
        self.state.last_build(self.DEPLOYMENT)
        self.state.last_failed_build(self.DEPLOYMENT)
        self.state.candidate(self.DEPLOYMENT)
        self.assertEqual(self.state.path.read_bytes(), before)
        self.assertEqual(self.state.builds(self.DEPLOYMENT)[0].updated_at, self.T0)

    def test_the_reads_refuse_a_state_file_that_is_not_connected(self):
        unconnected = ImageState(self.root / "unconnected", clock=self.clock)
        reads = (
            lambda: unconnected.builds(self.DEPLOYMENT),
            lambda: unconnected.builds_started_since(self.DEPLOYMENT, self.T0),
            lambda: unconnected.last_build(self.DEPLOYMENT),
            lambda: unconnected.last_failed_build(self.DEPLOYMENT),
            lambda: unconnected.candidate(self.DEPLOYMENT),
        )
        for read in reads:
            with self.subTest(read=read):
                with self.assertRaises(StateRefusal) as raised:
                    read()
                self.assertEqual(raised.exception.reason, "IMAGE_STATE_UNAVAILABLE")
        self.assertFalse((self.root / "unconnected").exists())


class PromotionSupportTests(ImageStateCase):
    """What ``promote``, ``rollback`` and ``status --refresh`` ask of the state, beyond the build pass."""

    DEPLOYMENT = "example-deployment"
    OTHER = "example-other-deployment"
    T0 = 1_800_000_000.0
    IMAGE = "ami-0123456789abcdef0"
    OTHER_IMAGE = "ami-0fedcba9876543210"

    def owner(self, build_id, version, at, *, state="candidate", deployment=None):
        """A build that started at ``at``, launched template ``version`` and is now in ``state``."""
        self.clock.now = at
        self.add_build(build_id, deployment=deployment or self.DEPLOYMENT)
        with self.state.transaction():
            return self.state.advance_build(build_id, state=state, template_version=version, image_id=self.IMAGE)

    def pin_record(self, version=3, image_id=None):
        with self.state.transaction():
            return self.state.insert_pin_record(
                deployment=self.DEPLOYMENT, version=version, image_id=image_id or self.IMAGE
            )

    def verify(self, build_id, **changes):
        values = {
            "image_state": "available",
            "template_version_exists": 1,
            "drift_result": "clean",
            "verified_at": self.T0 + 500,
            **changes,
        }
        with self.state.transaction():
            return self.state.record_verification(build_id, **values)

    # -- version_owner -----------------------------------------------------------------
    def test_a_version_is_owned_by_its_newest_candidate_or_promoted_row(self):
        self.assertIsNone(self.state.version_owner(self.DEPLOYMENT, 5))
        self.owner("b-old", 5, self.T0, state="promoted")
        newest = self.owner("b-new", 5, self.T0 + 10)
        self.owner("b-other-version", 6, self.T0 + 20)
        self.assertEqual(self.state.version_owner(self.DEPLOYMENT, 5), newest)
        self.assertEqual(self.state.version_owner(self.DEPLOYMENT, 6).id, "b-other-version")
        self.assertIsNone(self.state.version_owner(self.DEPLOYMENT, 7))

    def test_rows_that_started_together_are_ordered_by_insertion(self):
        self.owner("b-first", 5, self.T0)
        self.owner("b-second", 5, self.T0)
        self.assertEqual(self.state.version_owner(self.DEPLOYMENT, 5).id, "b-second")

    def test_a_failed_or_a_running_build_never_owns_a_version(self):
        self.owner("b-failed", 5, self.T0, state="failed")
        self.owner("b-running", 6, self.T0 + 10, state="building")
        self.assertIsNone(self.state.version_owner(self.DEPLOYMENT, 5))
        self.assertIsNone(self.state.version_owner(self.DEPLOYMENT, 6))

    def test_a_version_has_an_owner_in_its_own_deployment_only(self):
        self.owner("o-1", 5, self.T0, deployment=self.OTHER)
        self.assertIsNone(self.state.version_owner(self.DEPLOYMENT, 5))
        self.assertEqual(self.state.version_owner(self.OTHER, 5).id, "o-1")

    # -- insert_pin_record -------------------------------------------------------------
    def test_a_pin_record_is_a_promoted_row_that_owns_the_version_and_proves_nothing_yet(self):
        self.clock.now = self.T0
        record = self.pin_record(3)
        self.assertEqual(
            record,
            Build(
                id=f"{PIN_RECORD_PREFIX}3",
                deployment=self.DEPLOYMENT,
                client_token="pin:3",
                basis_digest="",
                source_sha="",
                state="promoted",
                reason_code=PIN_RECORD_REASON,
                created_at=0.0,
                updated_at=self.T0,
                deadline_at=0.0,
                image_id=self.IMAGE,
                template_version=3,
            ),
        )
        self.assertEqual(record.id, "pin-3")
        self.assertEqual(self.state.version_owner(self.DEPLOYMENT, 3), record)
        self.assertEqual(self.state.build("pin-3"), record)

    def test_a_second_pin_record_for_the_same_version_returns_the_first_and_changes_nothing(self):
        self.clock.now = self.T0
        first = self.pin_record(3)
        self.clock.now = self.T0 + 60
        again = self.pin_record(3, image_id=self.OTHER_IMAGE)
        self.assertEqual(again, first)
        self.assertEqual(self.state.build("pin-3").image_id, self.IMAGE)
        self.assertEqual(len(self.state.builds(self.DEPLOYMENT)), 1)

    def test_a_pin_record_is_never_a_build_that_started_or_a_candidate(self):
        self.owner("b-running", 5, self.T0, state="building")
        self.pin_record(3)  # the one_active index does not meet it
        self.assertEqual(self.state.active_build(self.DEPLOYMENT).id, "b-running")
        self.assertEqual(self.state.builds_started_since(self.DEPLOYMENT, self.T0 - 86_400), 1)
        self.assertEqual(self.state.last_build(self.DEPLOYMENT).id, "b-running")
        self.assertIsNone(self.state.candidate(self.DEPLOYMENT))
        self.assertIsNone(self.state.verified_candidate(self.DEPLOYMENT))

    # -- record_verification -----------------------------------------------------------
    def test_record_verification_replaces_the_four_columns_and_nothing_else(self):
        before = self.owner("b-1", 5, self.T0)
        self.clock.now = self.T0 + 500
        after = self.verify("b-1")
        self.assertEqual(
            (after.image_state, after.template_version_exists, after.drift_result, after.verified_at),
            ("available", 1, "clean", self.T0 + 500),
        )
        blank = {"image_state": None, "template_version_exists": None, "drift_result": None, "verified_at": None}
        self.assertEqual(replace(after, **blank), replace(before, **blank))
        self.assertEqual(after.updated_at, before.updated_at, "the end of the build is not the time of a read")
        self.assertEqual(self.state.build("b-1"), after)

    def test_a_newer_verification_replaces_an_older_one_and_none_is_stored_as_null(self):
        self.owner("b-1", 5, self.T0)
        self.verify("b-1")
        later = self.verify(
            "b-1", image_state="missing", template_version_exists=0, drift_result=None, verified_at=self.T0 + 900
        )
        self.assertEqual(
            (later.image_state, later.template_version_exists, later.drift_result, later.verified_at),
            ("missing", 0, None, self.T0 + 900),
        )
        cleared = self.verify(
            "b-1", image_state=None, template_version_exists=None, drift_result=None, verified_at=None
        )
        self.assertEqual(
            (cleared.image_state, cleared.template_version_exists, cleared.drift_result, cleared.verified_at),
            (None, None, None, None),
        )

    def test_a_pin_record_takes_a_verification(self):
        self.pin_record(3)
        record = self.verify("pin-3")
        self.assertEqual((record.image_state, record.verified_at), ("available", self.T0 + 500))
        self.assertEqual(record.state, "promoted")

    def test_record_verification_of_an_unknown_build_is_refused(self):
        with self.assertRaises(StateRefusal) as raised:
            self.verify("b-unknown")
        self.assertEqual((raised.exception.reason, raised.exception.detail), ("IMAGE_BUILD_UNKNOWN", "b-unknown"))
        self.assertEqual(self.state.builds(self.DEPLOYMENT), [])

    # -- journal_last_complete ---------------------------------------------------------
    def test_the_last_complete_row_skips_pending_and_aborted_rows_and_may_be_asked_for_a_kind(self):
        self.assertIsNone(self.state.journal_last_complete())
        self.assertIsNone(self.state.journal_last_complete(kind="promotion"))
        promotion = self.add_journal(old_pin=3, new_pin=4)
        rollback = self.add_journal(kind="rollback", old_pin=4, new_pin=3)
        pending = self.add_journal(old_pin=3, new_pin=5)
        aborted = self.add_journal(old_pin=3, new_pin=6)
        with self.state.transaction():
            self.state.journal_complete(promotion.id)
            self.state.journal_complete(rollback.id)
            self.state.journal_abort(aborted.id)
        self.assertEqual(self.state.journal_last_complete().id, rollback.id)
        self.assertEqual(self.state.journal_last_complete(kind="promotion").id, promotion.id)
        self.assertEqual(self.state.journal_last_complete(kind="rollback").id, rollback.id)
        self.assertEqual([row.id for row in self.state.pending_journal()], [pending.id])

    def test_a_kind_without_a_complete_row_gives_none(self):
        pending = self.add_journal()
        rollback = self.add_journal(kind="rollback", old_pin=4, new_pin=3)
        with self.state.transaction():
            self.state.journal_complete(rollback.id)
        self.assertIsNone(self.state.journal_last_complete(kind="promotion"))
        self.assertEqual(self.state.journal_last_complete().id, rollback.id)
        self.assertEqual(self.state.journal(pending.id).status, "pending")

    # -- pin_for_version ---------------------------------------------------------------
    def test_a_version_is_found_by_its_newest_observation(self):
        self.assertIsNone(self.state.pin_for_version(3))
        for offset, version, image_id in ((0, 3, self.IMAGE), (10, 4, self.OTHER_IMAGE), (20, 3, self.OTHER_IMAGE)):
            self.clock.now = self.T0 + offset
            self.assertTrue(self.state.pin_observe(version, image_id))
        self.assertEqual(self.state.pin_for_version(3), PinObservation(3, self.OTHER_IMAGE, self.T0 + 20))
        self.assertEqual(self.state.pin_for_version(4), PinObservation(4, self.OTHER_IMAGE, self.T0 + 10))
        self.assertIsNone(self.state.pin_for_version(5))
        self.assertEqual(self.state.pin_last(), self.state.pin_for_version(3))

    def test_the_new_reads_and_writes_refuse_a_state_file_that_is_not_connected(self):
        unconnected = ImageState(self.root / "unconnected", clock=self.clock)
        calls = (
            lambda: unconnected.version_owner(self.DEPLOYMENT, 3),
            lambda: unconnected.journal_last_complete(),
            lambda: unconnected.pin_for_version(3),
            lambda: unconnected.insert_pin_record(deployment=self.DEPLOYMENT, version=3, image_id=self.IMAGE),
            lambda: unconnected.record_verification(
                "b-1", image_state=None, template_version_exists=None, drift_result=None, verified_at=None
            ),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(StateRefusal) as raised:
                    call()
                self.assertEqual(raised.exception.reason, "IMAGE_STATE_UNAVAILABLE")
        self.assertFalse((self.root / "unconnected").exists())


class DetachedCopyTests(ImageStateCase):
    """``detached_copy``: the open file as a state in memory, to resolve rows on and plan against without a write."""

    DEPLOYMENT = "example-deployment"

    def open_read_only(self):
        reader = ImageState(self.directory, clock=self.clock)
        self.addCleanup(reader.close)
        self.assertTrue(reader.connect_readonly())
        return reader

    def copy_of(self, state):
        copy = state.detached_copy()
        self.addCleanup(copy.close)
        return copy

    def on_disk(self):
        """What a copy must leave as it found it: the bytes of the file and the names beside it."""
        return self.state.path.read_bytes(), sorted(item.name for item in self.directory.iterdir())

    def test_a_copy_holds_the_rows_of_the_file_as_of_the_call(self):
        build = self.add_build()
        pending = self.add_journal()
        copy = self.copy_of(self.open_read_only())
        self.assertEqual(copy.builds(self.DEPLOYMENT), [build])
        self.assertEqual(copy.pending_journal(), [pending])
        self.assertEqual(copy.control_get(), self.state.control_get())
        self.state.control_set(blocked=1)  # the file moves on and the copy does not
        self.assertFalse(copy.control_get().blocked)

    def test_a_row_resolved_on_the_copy_is_resolved_there_and_nowhere_else(self):
        self.add_build()
        pending = self.add_journal()
        reader = self.open_read_only()
        before = self.on_disk()
        copy = self.copy_of(reader)
        with copy.transaction():
            copy.journal_complete(pending.id)
        self.assertEqual(copy.pending_journal(), [])
        self.assertEqual(copy.journal_last_complete().id, pending.id)
        self.assertEqual(reader.pending_journal(), [pending])
        self.assertIsNone(reader.journal_last_complete())
        self.assertEqual(self.state.pending_journal(), [pending])
        self.assertEqual(self.on_disk(), before)

    def test_a_copy_makes_no_file_and_neither_takes_nor_drops_the_writer_lock(self):
        self.state.acquire_writer()
        before = self.on_disk()
        copy = self.copy_of(self.open_read_only())
        self.assertIs(copy.acquire_writer(), copy)  # it asks for no lock, so the writer's lock is no obstacle
        with copy.transaction():
            copy.control_set(blocked=1)
        copy.close()
        self.assertEqual(self.on_disk(), before)
        other = ImageState(self.directory, clock=self.clock)
        self.addCleanup(other.close)
        with self.assertRaises(StateRefusal) as raised:  # and the writer still holds the lock
            other.acquire_writer()
        self.assertEqual(raised.exception.reason, "IMAGE_STATE_LOCKED")

    def test_closing_the_copy_leaves_the_state_it_was_made_from_open(self):
        self.add_build()
        reader = self.open_read_only()
        copy = self.copy_of(reader)
        copy.close()
        self.assertFalse(copy.available)
        self.assertTrue(reader.available)
        self.assertEqual([build.id for build in reader.builds(self.DEPLOYMENT)], ["build-1"])

    def test_a_closed_copy_stays_closed_and_does_not_open_the_file(self):
        copy = self.copy_of(self.open_read_only())
        copy.close()
        before = self.on_disk()
        self.assertIs(copy.connect(), copy)
        self.assertFalse(copy.connect_readonly(), "a closed copy does not open the file read-only either")
        self.assertFalse(copy.available)
        with self.assertRaises(StateRefusal) as raised:
            copy.control_get()
        self.assertEqual(raised.exception.reason, "IMAGE_STATE_UNAVAILABLE")
        self.assertEqual(self.on_disk(), before)

    def test_a_journal_row_with_a_pin_that_is_not_a_version_is_refused_and_not_echoed(self):
        self.add_journal()
        for value in ("x; not a pin", b"\x00\xff", 0, 2.5):
            with self.subTest(value=value):
                self.state.close()
                connection = sqlite3.connect(self.state.path)
                try:
                    connection.execute("UPDATE promotion_journal SET old_pin = ?", (value,))
                    connection.commit()
                finally:
                    connection.close()
                self.state.connect()
                for read in (self.state.pending_journal, lambda: self.state.journal(1), self.state.journal_since_complete):
                    with self.assertRaises(StateRefusal) as raised:
                        read()
                    self.assertEqual(raised.exception.reason, "IMAGE_STATE_UNREADABLE")
                    self.assertNotIn("not a pin", raised.exception.detail)

    def test_a_journal_row_with_a_blob_or_number_where_text_belongs_is_refused(self):
        self.add_journal()
        cases = (
            ("old_file_sha256", b"\x01"),
            ("new_file_sha256", b"\x02"),  # TEXT affinity turns a number into text: only a blob stays apart
            ("candidate_id", b"build-1"),
            ("created_at", "x; not a time"),
        )
        for column, value in cases:
            with self.subTest(column=column):
                self.state.close()
                connection = sqlite3.connect(self.state.path)
                try:
                    connection.execute(
                        "UPDATE promotion_journal SET old_file_sha256 = ?, new_file_sha256 = ?, candidate_id = ?,"
                        " created_at = 1.0",
                        ("1" * 64, "2" * 64, "build-1"),
                    )
                    connection.execute(f"UPDATE promotion_journal SET {column} = ?", (value,))
                    connection.commit()
                finally:
                    connection.close()
                self.state.connect()
                for read in (
                    self.state.pending_journal,
                    lambda: self.state.journal(1),
                    self.state.journal_check,
                    self.state.journal_since_complete,
                ):
                    with self.assertRaises(StateRefusal) as raised:
                        read()
                    self.assertEqual(raised.exception.reason, "IMAGE_STATE_UNREADABLE")

    def test_a_journal_row_with_a_kind_or_status_outside_the_schema_is_refused_and_not_echoed(self):
        self.add_journal()
        for column in ("kind", "status"):
            with self.subTest(column=column):
                self.state.close()
                connection = sqlite3.connect(self.state.path)
                try:
                    connection.execute("PRAGMA ignore_check_constraints = ON")  # what a damaged file can hold
                    connection.execute("UPDATE promotion_journal SET kind = 'promotion', status = 'pending'")
                    connection.execute(f"UPDATE promotion_journal SET {column} = ?", ("x; not a value",))
                    connection.commit()
                finally:
                    connection.close()
                self.state.connect()
                # The reads that select by status or kind refuse too: they would otherwise skip the damaged row.
                reads = (self.state.pending_journal, lambda: self.state.journal(1), self.state.journal_last_complete)
                for read in reads:
                    with self.assertRaises(StateRefusal) as raised:
                        read()
                    self.assertEqual(raised.exception.reason, "IMAGE_STATE_UNREADABLE")
                    self.assertNotIn("not a value", raised.exception.detail)

    def test_a_state_that_is_not_open_has_no_copy(self):
        self.state.close()
        with self.assertRaises(StateRefusal) as raised:
            self.state.detached_copy()
        self.assertEqual(raised.exception.reason, "IMAGE_STATE_UNAVAILABLE")

    def test_a_source_that_another_connection_holds_locked_is_refused_and_not_waited_for(self):
        reader = self.open_read_only()
        reader._connection.execute("PRAGMA busy_timeout = 100")
        holder = sqlite3.connect(self.state.path, timeout=1, isolation_level=None, check_same_thread=False)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        # ``backup`` alone retries a locked source until the lock is gone. If the copy waited, this timer would end
        # the wait and the copy would succeed: the test would then fail for the missing refusal and not hang.
        release = threading.Timer(3.0, holder.execute, ("ROLLBACK",))
        self.addCleanup(release.join)
        self.addCleanup(release.cancel)
        release.start()
        with self.assertRaises(StateRefusal) as raised:
            reader.detached_copy()
        self.assertEqual(raised.exception.reason, "IMAGE_STATE_UNREADABLE")
        self.assertEqual(raised.exception.detail, "OperationalError")
        self.assertTrue(reader.available)


if __name__ == "__main__":
    unittest.main()
