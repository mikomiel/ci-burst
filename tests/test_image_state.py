import hashlib
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ci_burst import image_state
from ci_burst.config import load
from ci_burst.controller import Controller
from ci_burst.errors import StateRefusal
from ci_burst.image_state import SCHEMA_VERSION, STATE_FILE, ImageState
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


if __name__ == "__main__":
    unittest.main()
