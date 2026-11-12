import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from ci_burst.errors import StateRefusal
from ci_burst.ledger import SCHEMA_VERSION, TERMINAL, Ledger, current_window


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "state.sqlite3"
        self.clock_now = 1_800_000_000.0
        self.ledger = Ledger(self.path, clock=lambda: self.clock_now).connect()

    def tearDown(self):
        self.ledger.close()

    def add(self, **overrides):
        operation_id = overrides.pop("operation_id", "op-" + str(len(self.ledger.operations())))
        values = {
            "operation_id": operation_id,
            "client_token": overrides.pop("client_token", "ci-burst-" + operation_id),
            "request": {"LaunchTemplate": {"Version": "3"}},
            "request_sha256": "0" * 64,
            "expires_at": self.clock_now + 3600,
            "reservation_micros": 20_000_000,
            "window_start": current_window(self.clock_now, 86400),
            "region": "eu-central-1",
            "account": "111122223333",
        }
        values.update(overrides)
        with self.ledger.transaction():
            return self.ledger.new_operation(**values)


class SchemaTests(LedgerTestCase):
    def test_round_trip(self):
        operation = self.add()
        self.assertEqual(operation.state, "intent")
        self.assertEqual(operation.region, "eu-central-1")
        self.assertEqual(operation.account, "111122223333")
        self.assertEqual(self.ledger.operation(operation.id).client_token, "ci-burst-op-0")
        self.assertEqual(self.ledger.operation_by_token("ci-burst-op-0").id, operation.id)

    def test_a_new_admission_stores_an_explicit_zero_backfill_flag(self):
        operation = self.add()
        self.assertEqual(operation.admitted_inputs_backfilled, 0)
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        row = connection.execute(
            "SELECT admitted_inputs_backfilled FROM operations WHERE id = ?", (operation.id,)
        ).fetchone()
        self.assertEqual(row[0], 0)

    def test_restart_preserves_operations(self):
        operation = self.add()
        self.ledger.set_state(operation.id, "running", instance_id="i-1")
        path, clock = self.path, (lambda: self.clock_now)
        self.ledger.close()
        reopened = Ledger(path, clock=clock).connect()
        self.addCleanup(reopened.close)
        restored = reopened.operation(operation.id)
        self.assertEqual(restored.state, "running")
        self.assertEqual(restored.instance_id, "i-1")

    def test_missing_database_is_not_created_for_reading(self):
        path = Path(self._directory.name) / "absent.sqlite3"
        ledger = Ledger(path).connect(create=False)
        self.assertFalse(ledger.available)
        self.assertFalse(path.exists())

    def test_read_only_connection_uses_the_same_file_for_special_characters(self):
        path = Path(self._directory.name) / "state#pilot.sqlite3"
        writable = Ledger(path, clock=lambda: self.clock_now).connect()
        with writable.transaction():
            writable.new_operation(
                operation_id="special",
                client_token="ci-burst-special",
                request={"seed": True},
                request_sha256="0" * 64,
                expires_at=self.clock_now + 60,
                reservation_micros=1,
                window_start=current_window(self.clock_now, 86400),
                region="eu-central-1",
                account="111122223333",
            )
        writable.close()
        reader = Ledger(path, clock=lambda: self.clock_now).connect(create=False, read_only=True)
        self.addCleanup(reader.close)
        self.assertEqual([operation.id for operation in reader.operations()], ["special"])

    def test_read_only_observation_writes_no_files(self):
        path = Path(self._directory.name) / "existing.sqlite3"
        writable = Ledger(path, clock=lambda: self.clock_now).connect()
        with writable.transaction():
            writable.new_operation(
                operation_id="one",
                client_token="ci-burst-one",
                request={},
                request_sha256="0" * 64,
                expires_at=self.clock_now + 60,
                reservation_micros=1,
                window_start=current_window(self.clock_now, 86400),
                region="eu-central-1",
                account="111122223333",
            )
        writable.close()
        before = sorted(item.name for item in Path(self._directory.name).iterdir())
        reader = Ledger(path, clock=lambda: self.clock_now).connect(create=False, read_only=True)
        reader.operations()
        reader.close()
        self.assertEqual(before, sorted(item.name for item in Path(self._directory.name).iterdir()))

    def test_corrupt_database_is_refused(self):
        bad = Path(self._directory.name) / "corrupt.sqlite3"
        bad.write_bytes(b"not a database")
        with self.assertRaises(StateRefusal) as raised:
            Ledger(bad).connect()
        self.assertEqual(raised.exception.reason, "STATE_UNREADABLE")

    def test_unsupported_schema_version_is_refused(self):
        versioned = Path(self._directory.name) / "versioned.sqlite3"
        connection = sqlite3.connect(versioned)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.commit()
        connection.close()
        with self.assertRaises(StateRefusal) as raised:
            Ledger(versioned).connect()
        self.assertEqual(raised.exception.reason, "STATE_VERSION_UNSUPPORTED")


class TransactionTests(LedgerTestCase):
    def test_rollback_removes_the_operation(self):
        with self.assertRaises(RuntimeError):
            with self.ledger.transaction():
                self.add(operation_id="rolled-back")
                raise RuntimeError("boom")
        self.assertIsNone(self.ledger.operation("rolled-back"))

    def test_second_writer_is_refused(self):
        self.ledger.acquire_writer()
        other = Ledger(self.path).connect()
        self.addCleanup(other.close)
        with self.assertRaises(StateRefusal) as raised:
            other.acquire_writer()
        self.assertEqual(raised.exception.reason, "WRITER_LOCKED")

    def test_writer_lock_is_released(self):
        self.ledger.acquire_writer()
        self.ledger.release_writer()
        other = Ledger(self.path).connect()
        self.addCleanup(other.close)
        other.acquire_writer()


class CommitmentTests(LedgerTestCase):
    """The window totals a fixed worst case until an operation is resolved."""

    class StubCost:
        #: A model-wide buffer that must never reach a window total: every
        #: operation carries the buffer recorded at its own admission.
        buffer_micros = 999_999_999

        def accrued_micros(self, operation, now):
            return 0

        def remaining_micros(self, operation, now):
            return operation.reservation_micros

        def buffer_for(self, operation):
            return operation.admitted_buffer_micros or 0

        def committed_micros(self, operation, now):
            if operation.state in TERMINAL:
                return self.accrued_micros(operation, now)
            return self.remaining_micros(operation, now) + self.buffer_for(operation)

    def totals(self, window):
        return self.ledger.window_totals(window, self.StubCost(), self.clock_now)

    def test_current_window_commitment_is_counted(self):
        window = current_window(self.clock_now, 86400)
        self.add(operation_id="a", reservation_micros=1_000_000, window_start=window)
        self.add(operation_id="b", reservation_micros=2_000_000, window_start=window)
        totals = self.totals(window)
        self.assertEqual(totals["committed"], 3_000_000)
        self.assertEqual(totals["spend"], 0)

    def test_completed_spend_from_an_earlier_window_is_not_consumed_again(self):
        earlier = current_window(self.clock_now, 86400) - 86400
        operation = self.add(operation_id="old", reservation_micros=5_000_000, window_start=earlier)
        # While the operation is outstanding its worst case is still carryover.
        self.assertEqual(self.totals(current_window(self.clock_now, 86400))["committed"], 5_000_000)
        self.ledger.set_state(operation.id, "terminal")
        self.assertEqual(self.totals(current_window(self.clock_now, 86400))["committed"], 0)

    def test_outstanding_operation_carries_into_the_next_window(self):
        earlier = current_window(self.clock_now, 86400) - 86400
        self.add(operation_id="carry", reservation_micros=5_000_000, window_start=earlier)
        self.assertEqual(self.totals(current_window(self.clock_now, 86400))["committed"], 5_000_000)

    def test_restart_does_not_reset_the_commitment(self):
        window = current_window(self.clock_now, 86400)
        self.add(operation_id="a", reservation_micros=4_000_000, window_start=window)
        path, clock = self.path, (lambda: self.clock_now)
        self.ledger.close()
        reopened = Ledger(path, clock=clock).connect()
        self.addCleanup(reopened.close)
        self.assertEqual(
            reopened.window_totals(window, self.StubCost(), self.clock_now)["committed"], 4_000_000
        )

    def test_a_real_model_keeps_accrued_cost_in_its_own_window(self):
        from ci_burst.cost import CostModel

        now = self.clock_now
        earlier = current_window(now, 86400) - 86400
        model = CostModel(
            price_cap_micros=200_000,
            incidental_per_hour_micros=0,
            max_lifetime_seconds=3600,
            buffer_micros=50_000,
        )
        operation = self.add(operation_id="live", reservation_micros=250_000, window_start=earlier)
        self.ledger.set_instance(operation.id, "i-live")
        self.ledger.set_launch_time(operation.id, now - 600)
        self.ledger.set_state(operation.id, "running", instance_id="i-live")
        earlier_totals = self.ledger.window_totals(earlier, model, now)
        self.assertEqual(earlier_totals["spend"], 33_334)
        self.assertEqual(earlier_totals["committed"], 250_001)
        current_totals = self.ledger.window_totals(current_window(now, 86400), model, now)
        self.assertEqual(current_totals["spend"], 0)
        self.assertEqual(current_totals["committed"], 216_667)

    def test_each_operation_carries_its_own_recorded_buffer(self):
        window = current_window(self.clock_now, 86400)
        self.add(
            operation_id="a", reservation_micros=1_000_000, window_start=window,
            admitted_buffer_micros=1_000,
        )
        self.add(
            operation_id="b", reservation_micros=2_000_000, window_start=window,
            admitted_buffer_micros=3_000,
        )
        self.assertEqual(self.totals(window)["committed"], 3_004_000)

    def test_an_operation_without_a_recorded_buffer_adds_none(self):
        window = current_window(self.clock_now, 86400)
        self.add(operation_id="a", reservation_micros=1_000_000, window_start=window)
        self.assertEqual(self.totals(window)["committed"], 1_000_000)

    def test_window_rollover_changes_the_anchor(self):
        self.assertNotEqual(
            current_window(1_800_000_000.0, 86400),
            current_window(1_800_000_000.0 + 86400, 86400),
        )


V1_SCHEMA = """
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

V2_SCHEMA = """
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

V1_COLUMNS = (
    "id, client_token, state, created_at, updated_at, expires_at, reservation_micros,"
    " reservation_window, region, account, request_json, request_sha256, instance_id, detail"
)
V2_COLUMNS = V1_COLUMNS + ", price_micros, launch_time, settled_micros, settled_at"


class MigrationTests(unittest.TestCase):
    """An earlier file upgrades once, after a verified copy, without losing state."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "state.sqlite3"
        self.now = 1_800_000_000.0
        self._write(1)

    def _rows(self, version):
        terminal = (
            "old-op", "ci-burst-old", "terminal", self.now - 600, self.now - 300, self.now - 60,
            170000, current_window(self.now, 86400), "eu-central-1", "111122223333", "{}", "0" * 64,
            "i-old", "",
        )
        nonterminal = (
            "live-op", "ci-burst-live", "running", self.now - 300, self.now - 100, self.now + 3300,
            170000, current_window(self.now, 86400), "eu-central-1", "111122223333", "{}", "0" * 64,
            "i-live", "",
        )
        if version == 1:
            return [terminal, nonterminal], V1_COLUMNS
        return [
            terminal + (30_000, self.now - 600, 12_345, self.now - 400),
            nonterminal + (20_000, self.now - 300, None, None),
        ], V2_COLUMNS

    def _write(self, version):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        connection = sqlite3.connect(self.path)
        connection.executescript(V1_SCHEMA if version == 1 else V2_SCHEMA)
        rows, columns = self._rows(version)
        for row in rows:
            connection.execute(
                "INSERT INTO operations (%s) VALUES (%s)" % (columns, ",".join("?" * len(row))),
                row,
            )
        connection.commit()
        connection.close()

    def writable(self, backfill=(40_000, 0, 0)):
        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        ledger.connect(create=True, backfill=backfill)
        self.addCleanup(ledger.close)
        return ledger

    def backups(self):
        return sorted(self.path.parent.glob("state.sqlite3.pre-v3.*"))

    def user_version(self):
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()

    def columns(self):
        connection = sqlite3.connect(self.path)
        try:
            return {row[1] for row in connection.execute("PRAGMA table_info(operations)")}
        finally:
            connection.close()

    def test_read_only_accepts_the_previous_schema(self):
        ledger = Ledger(self.path, clock=lambda: self.now).connect(create=False, read_only=True)
        self.addCleanup(ledger.close)
        self.assertEqual(ledger.version, 1)
        operation = ledger.operation("old-op")
        self.assertEqual(operation.price_micros, 0)
        self.assertIsNone(operation.launch_time)
        self.assertIsNone(operation.admitted_incidental_per_hour_micros)
        self.assertIsNone(operation.admitted_buffer_micros)
        # An unmigrated file cannot carry the persisted marker, so it reads as a
        # normally admitted row: observation only, never admission.
        self.assertEqual(operation.admitted_inputs_backfilled, 0)

    def test_v1_upgrade_creates_one_v3_backup_and_backfills(self):
        ledger = self.writable()
        self.assertEqual(ledger.version, SCHEMA_VERSION)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(list(self.path.parent.glob("state.sqlite3.pre-v2.*")), [])
        terminal = ledger.operation("old-op")
        self.assertEqual(terminal.state, "terminal")
        # 300 seconds at 0.04 per hour, rounded up to one micro, plus no incidentals.
        self.assertEqual(terminal.settled_micros, 3334)
        # Terminal history keeps NULL admitted inputs and a zero marker: it is
        # never repriced and never floored.
        self.assertIsNone(terminal.admitted_incidental_per_hour_micros)
        self.assertIsNone(terminal.admitted_buffer_micros)
        self.assertEqual(terminal.admitted_inputs_backfilled, 0)
        live = ledger.operation("live-op")
        # The nonterminal row records the basis supplied to connect(), best
        # effort, and is marked as backfilled so its reservation stays the floor.
        self.assertEqual(live.admitted_incidental_per_hour_micros, 0)
        self.assertEqual(live.admitted_buffer_micros, 0)
        self.assertEqual(live.admitted_inputs_backfilled, 1)
        self.assertEqual(live.price_micros, 0)
        self.assertIsNone(live.launch_time)

    def test_v2_upgrade_backfills_only_nonterminal_rows(self):
        self._write(2)
        ledger = self.writable(backfill=(40_000, 5_000, 7_000))
        self.assertEqual(ledger.version, SCHEMA_VERSION)
        self.assertEqual(len(self.backups()), 1)
        terminal = ledger.operation("old-op")
        # The stored settled cost and its timestamp are never rewritten.
        self.assertEqual(terminal.settled_micros, 12_345)
        self.assertEqual(terminal.settled_at, self.now - 400)
        self.assertIsNone(terminal.admitted_incidental_per_hour_micros)
        self.assertIsNone(terminal.admitted_buffer_micros)
        self.assertEqual(terminal.admitted_inputs_backfilled, 0)
        live = ledger.operation("live-op")
        self.assertEqual(live.state, "running")
        self.assertEqual(live.admitted_incidental_per_hour_micros, 5_000)
        self.assertEqual(live.admitted_buffer_micros, 7_000)
        self.assertEqual(live.admitted_inputs_backfilled, 1)
        self.assertIsNone(live.settled_micros)

    def test_a_reopened_v3_file_keeps_the_backfill_marker(self):
        self._write(2)
        ledger = self.writable(backfill=(40_000, 5_000, 7_000))
        self.assertEqual(ledger.version, SCHEMA_VERSION)
        ledger.close()
        reopened = Ledger(self.path, clock=lambda: self.now).connect()
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.version, SCHEMA_VERSION)
        self.assertEqual(reopened.operation("live-op").admitted_inputs_backfilled, 1)
        self.assertEqual(reopened.operation("old-op").admitted_inputs_backfilled, 0)

    def test_read_only_keeps_a_v2_database_unwritten(self):
        self._write(2)
        before = sorted(item.name for item in self.path.parent.iterdir())
        ledger = Ledger(self.path, clock=lambda: self.now).connect(create=False, read_only=True)
        self.addCleanup(ledger.close)
        self.assertEqual(ledger.version, 2)
        self.assertEqual(ledger.operation("old-op").settled_micros, 12_345)
        self.assertIsNone(ledger.operation("live-op").admitted_buffer_micros)
        self.assertEqual(ledger.operation("live-op").admitted_inputs_backfilled, 0)
        self.assertEqual([operation.id for operation in ledger.operations()], ["old-op", "live-op"])
        self.assertEqual(before, sorted(item.name for item in self.path.parent.iterdir()))

    def test_copy_failure_refuses_before_any_schema_write(self):
        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        with mock.patch("ci_burst.ledger.shutil.copy2", side_effect=OSError("denied")):
            with self.assertRaises(StateRefusal) as raised:
                ledger.connect(create=True, backfill=(40_000, 0, 0))
        self.assertEqual(raised.exception.reason, "STATE_BACKUP_FAILED")
        self.assertEqual(self.user_version(), 1)
        self.assertNotIn("price_micros", self.columns())
        self.assertNotIn("admitted_buffer_micros", self.columns())
        self.assertNotIn("admitted_inputs_backfilled", self.columns())

    def test_size_mismatch_refuses_before_any_schema_write(self):
        def truncated(source, target):
            Path(target).write_bytes(b"")

        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        with mock.patch("ci_burst.ledger.shutil.copy2", side_effect=truncated):
            with self.assertRaises(StateRefusal) as raised:
                ledger.connect(create=True, backfill=(40_000, 0, 0))
        self.assertEqual(raised.exception.reason, "STATE_BACKUP_FAILED")
        self.assertEqual(self.user_version(), 1)
        self.assertNotIn("price_micros", self.columns())
        ledger.connect(create=True, backfill=(40_000, 0, 0))
        self.assertEqual(ledger.version, SCHEMA_VERSION)

    def test_a_v2_copy_failure_leaves_the_database_unchanged(self):
        self._write(2)
        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        with mock.patch("ci_burst.ledger.shutil.copy2", side_effect=OSError("denied")):
            with self.assertRaises(StateRefusal) as raised:
                ledger.connect(create=True, backfill=(40_000, 5_000, 7_000))
        self.assertEqual(raised.exception.reason, "STATE_BACKUP_FAILED")
        self.assertEqual(self.user_version(), 2)
        self.assertNotIn("admitted_incidental_per_hour_micros", self.columns())
        self.assertNotIn("admitted_buffer_micros", self.columns())
        self.assertEqual(self.backups(), [])
        # The refusal is retryable and the retry upgrades in one transaction.
        ledger.connect(create=True, backfill=(40_000, 5_000, 7_000))
        self.assertEqual(ledger.version, SCHEMA_VERSION)
        self.assertEqual(ledger.operation("live-op").admitted_buffer_micros, 7_000)
        self.assertEqual(ledger.operation("old-op").settled_micros, 12_345)

    def test_a_failed_upgrade_rolls_the_whole_transaction_back(self):
        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        with mock.patch("ci_burst.ledger._backfilled_cost", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                ledger.connect(create=True, backfill=(40_000, 0, 0))
        # The version-2 shape and the version-3 columns are rolled back together.
        self.assertEqual(self.user_version(), 1)
        self.assertNotIn("price_micros", self.columns())
        self.assertNotIn("admitted_buffer_micros", self.columns())
        self.assertNotIn("admitted_inputs_backfilled", self.columns())
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM operations").fetchone()[0], 2
        )
        # A retry after the failure still migrates cleanly.
        ledger.connect(create=True, backfill=(40_000, 0, 0))
        self.assertEqual(ledger.version, SCHEMA_VERSION)
        self.assertEqual(ledger.operation("old-op").settled_micros, 3334)

    def test_a_refused_upgrade_can_be_retried(self):
        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        with mock.patch("ci_burst.ledger.shutil.copy2", side_effect=OSError("denied")):
            with self.assertRaises(StateRefusal):
                ledger.connect(create=True, backfill=(40_000, 0, 0))
        ledger.connect(create=True, backfill=(40_000, 0, 0))
        self.assertEqual(ledger.version, SCHEMA_VERSION)
        self.assertEqual(ledger.operation("old-op").settled_micros, 3334)

    def test_writable_connect_without_backfill_refuses(self):
        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        with self.assertRaises(StateRefusal) as raised:
            ledger.connect(create=True)
        self.assertEqual(raised.exception.reason, "STATE_MIGRATION_PARAMETERS_REQUIRED")
        # The refusal left the file at its original version.
        self.assertEqual(self.user_version(), 1)

    def test_restoring_the_backup_rolls_the_ledger_back(self):
        self._write(2)
        ledger = self.writable(backfill=(40_000, 5_000, 7_000))
        self.assertEqual(ledger.version, SCHEMA_VERSION)
        ledger.close()
        backup = self.backups()[0]
        shutil.copy2(backup, self.path)
        rolled_back = Ledger(self.path, clock=lambda: self.now).connect(create=False, read_only=True)
        self.addCleanup(rolled_back.close)
        self.assertEqual(rolled_back.version, 2)
        self.assertEqual(rolled_back.operation("old-op").settled_micros, 12_345)
        self.assertIsNone(rolled_back.operation("live-op").admitted_buffer_micros)


if __name__ == "__main__":
    unittest.main()
