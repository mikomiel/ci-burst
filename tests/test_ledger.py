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
        buffer_micros = 0

        def accrued_micros(self, operation, now):
            return 0

        def remaining_micros(self, operation, now):
            return operation.reservation_micros

        def committed_micros(self, operation, now):
            if operation.state in TERMINAL:
                return self.accrued_micros(operation, now)
            return self.remaining_micros(operation, now) + self.buffer_micros

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

    def test_window_rollover_changes_the_anchor(self):
        self.assertNotEqual(
            current_window(1_800_000_000.0, 86400),
            current_window(1_800_000_000.0 + 86400, 86400),
        )


class MigrationTests(unittest.TestCase):
    """A version-1 file upgrades once, after a verified copy, without losing state."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "state.sqlite3"
        self.now = 1_800_000_000.0
        connection = sqlite3.connect(self.path)
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
        row = (
            "old-op", "ci-burst-old", "terminal", self.now - 600, self.now - 300, self.now - 60,
            170000, current_window(self.now, 86400), "eu-central-1", "111122223333", "{}", "0" * 64,
            "i-old", "",
        )
        connection.execute(
            "INSERT INTO operations (id, client_token, state, created_at, updated_at, expires_at,"
            " reservation_micros, reservation_window, region, account, request_json, request_sha256,"
            " instance_id, detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
        connection.commit()
        connection.close()

    def test_read_only_accepts_the_previous_schema(self):
        ledger = Ledger(self.path, clock=lambda: self.now).connect(create=False, read_only=True)
        self.addCleanup(ledger.close)
        self.assertEqual(ledger.version, 1)
        operation = ledger.operation("old-op")
        self.assertEqual(operation.price_micros, 0)
        self.assertIsNone(operation.launch_time)

    def test_writable_connect_migrates_with_a_backup_and_backfills(self):
        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        ledger.connect(create=True, backfill=(40_000, 0))
        self.assertEqual(ledger.version, SCHEMA_VERSION)
        operation = ledger.operation("old-op")
        self.assertEqual(operation.state, "terminal")
        # 300 seconds at 0.04 per hour, rounded up to one micro, plus no incidentals.
        self.assertEqual(operation.settled_micros, 3334)
        self.assertEqual(len(list(self.path.parent.glob("state.sqlite3.pre-v2.*"))), 1)

    def test_copy_failure_refuses_before_any_schema_write(self):
        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        with mock.patch("ci_burst.ledger.shutil.copy2", side_effect=OSError("denied")):
            with self.assertRaises(StateRefusal) as raised:
                ledger.connect(create=True, backfill=(40_000, 0))
        self.assertEqual(raised.exception.reason, "STATE_BACKUP_FAILED")
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(operations)")}
        self.assertNotIn("price_micros", columns)

    def test_size_mismatch_refuses_before_any_schema_write(self):
        def truncated(source, target):
            Path(target).write_bytes(b"")

        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        with mock.patch("ci_burst.ledger.shutil.copy2", side_effect=truncated):
            with self.assertRaises(StateRefusal) as raised:
                ledger.connect(create=True, backfill=(40_000, 0))
        self.assertEqual(raised.exception.reason, "STATE_BACKUP_FAILED")
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_a_refused_upgrade_can_be_retried(self):
        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        with mock.patch("ci_burst.ledger.shutil.copy2", side_effect=OSError("denied")):
            with self.assertRaises(StateRefusal):
                ledger.connect(create=True, backfill=(40_000, 0))
        ledger.connect(create=True, backfill=(40_000, 0))
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
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
