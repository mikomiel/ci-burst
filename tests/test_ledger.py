import sqlite3
import tempfile
import unittest
from pathlib import Path

from ci_burst.errors import StateRefusal
from ci_burst.ledger import SCHEMA_VERSION, Ledger, current_window


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
        }
        values.update(overrides)
        with self.ledger.transaction():
            return self.ledger.new_operation(**values)


class SchemaTests(LedgerTestCase):
    def test_round_trip(self):
        operation = self.add()
        self.assertEqual(operation.state, "intent")
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


class ReservationTests(LedgerTestCase):
    def test_current_window_spend_is_consumed(self):
        window = current_window(self.clock_now, 86400)
        self.add(operation_id="a", reservation_micros=1_000_000, window_start=window)
        self.add(operation_id="b", reservation_micros=2_000_000, window_start=window)
        self.assertEqual(self.ledger.consumed_micros(window), 3_000_000)

    def test_completed_spend_from_an_earlier_window_is_not_consumed_again(self):
        earlier = current_window(self.clock_now, 86400) - 86400
        operation = self.add(operation_id="old", reservation_micros=5_000_000, window_start=earlier)
        # While the operation is outstanding its reservation is still carryover.
        self.assertEqual(self.ledger.consumed_micros(current_window(self.clock_now, 86400)), 5_000_000)
        self.ledger.set_state(operation.id, "terminal")
        self.assertEqual(self.ledger.consumed_micros(current_window(self.clock_now, 86400)), 0)

    def test_outstanding_operation_carries_into_the_next_window(self):
        earlier = current_window(self.clock_now, 86400) - 86400
        self.add(operation_id="carry", reservation_micros=5_000_000, window_start=earlier)
        self.assertEqual(self.ledger.consumed_micros(current_window(self.clock_now, 86400)), 5_000_000)

    def test_restart_does_not_reset_consumption(self):
        window = current_window(self.clock_now, 86400)
        self.add(operation_id="a", reservation_micros=4_000_000, window_start=window)
        path, clock = self.path, (lambda: self.clock_now)
        self.ledger.close()
        reopened = Ledger(path, clock=clock).connect()
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.consumed_micros(window), 4_000_000)

    def test_window_rollover_changes_the_anchor(self):
        self.assertNotEqual(
            current_window(1_800_000_000.0, 86400),
            current_window(1_800_000_000.0 + 86400, 86400),
        )


if __name__ == "__main__":
    unittest.main()
