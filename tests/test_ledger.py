import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from ci_burst.errors import StateRefusal
from ci_burst.ledger import FLEET_RETENTION_SECONDS, SCHEMA_VERSION, TERMINAL, Ledger, current_window


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
            "admitted_lifetime_seconds": 3600,
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

    def test_a_new_operation_records_the_lifetime_it_was_admitted_under(self):
        operation = self.add(admitted_lifetime_seconds=1800)
        self.assertEqual(operation.admitted_lifetime_seconds, 1800)
        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        row = connection.execute(
            "SELECT admitted_lifetime_seconds FROM operations WHERE id = ?", (operation.id,)
        ).fetchone()
        self.assertEqual(row[0], 1800)
        self.assertEqual(self.add().admitted_lifetime_seconds, 3600)

    def test_a_new_operation_must_state_its_lifetime(self):
        with self.assertRaises(TypeError):
            with self.ledger.transaction():
                self.ledger.new_operation(
                    operation_id="no-lifetime",
                    client_token="ci-burst-no-lifetime",
                    request={},
                    request_sha256="0" * 64,
                    expires_at=self.clock_now + 60,
                    reservation_micros=1,
                    window_start=current_window(self.clock_now, 86400),
                    region="eu-central-1",
                    account="111122223333",
                )

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
                admitted_lifetime_seconds=3600,
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
                admitted_lifetime_seconds=3600,
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

V3_SCHEMA = """
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
        settled_at REAL,
        admitted_incidental_per_hour_micros INTEGER,
        admitted_buffer_micros INTEGER,
        admitted_inputs_backfilled INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX operations_state ON operations (state);
    CREATE INDEX operations_window ON operations (reservation_window);
    PRAGMA user_version = 3;
"""

V4_SCHEMA = V3_SCHEMA.replace("PRAGMA user_version = 3;", "") + """
    CREATE TABLE fleet_observations (
        observed_at REAL PRIMARY KEY,
        contract TEXT NOT NULL,
        running_attempts INTEGER NOT NULL,
        active_registrations INTEGER NOT NULL,
        offline_registrations INTEGER NOT NULL
    );
    PRAGMA user_version = 4;
"""

V1_COLUMNS = (
    "id, client_token, state, created_at, updated_at, expires_at, reservation_micros,"
    " reservation_window, region, account, request_json, request_sha256, instance_id, detail"
)
V2_COLUMNS = V1_COLUMNS + ", price_micros, launch_time, settled_micros, settled_at"
V3_COLUMNS = V2_COLUMNS + ", admitted_incidental_per_hour_micros, admitted_buffer_micros, admitted_inputs_backfilled"


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
        if version == 2:
            return [
                terminal + (30_000, self.now - 600, 12_345, self.now - 400),
                nonterminal + (20_000, self.now - 300, None, None),
            ], V2_COLUMNS
        return [
            terminal + (30_000, self.now - 600, 12_345, self.now - 400, None, None, 0),
            nonterminal + (20_000, self.now - 300, None, None, 5_000, 7_000, 1),
        ], V3_COLUMNS

    def _write(self, version):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        connection = sqlite3.connect(self.path)
        schema = {1: V1_SCHEMA, 2: V2_SCHEMA, 3: V3_SCHEMA, 4: V4_SCHEMA}[version]
        connection.executescript(schema)
        rows, columns = self._rows(version)
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
        return sorted(self.path.parent.glob("state.sqlite3.pre-v4.*"))

    def v5_backups(self):
        return sorted(self.path.parent.glob("state.sqlite3.pre-v5.*"))

    def shape(self, path):
        """Tables, columns and indexes of a ledger file, as one comparable value."""
        connection = sqlite3.connect(path)
        try:
            names = connection.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            columns = {
                table: connection.execute("PRAGMA table_info(%s)" % table).fetchall()
                for table in ("operations", "fleet_observations")
            }
            return names, columns, connection.execute("PRAGMA user_version").fetchone()[0]
        finally:
            connection.close()

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

    def tables(self):
        connection = sqlite3.connect(self.path)
        try:
            return {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
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

    def test_v1_upgrade_creates_one_v4_backup_and_backfills(self):
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

    def test_v3_upgrade_adds_the_fleet_table_once(self):
        self._write(3)
        ledger = self.writable()
        self.assertEqual(ledger.version, SCHEMA_VERSION)
        self.assertEqual(len(self.backups()), 1)
        # The schema-3 rows are untouched: the upgrade only creates a table.
        self.assertEqual(ledger.operation("old-op").settled_micros, 12_345)
        self.assertEqual(ledger.operation("live-op").admitted_buffer_micros, 7_000)
        self.assertIn("fleet_observations", self.tables())
        self.assertIsNone(ledger.latest_fleet_observation())

    def test_a_v3_copy_failure_leaves_the_database_unchanged(self):
        self._write(3)
        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        with mock.patch("ci_burst.ledger.shutil.copy2", side_effect=OSError("denied")):
            with self.assertRaises(StateRefusal) as raised:
                ledger.connect(create=True)
        self.assertEqual(raised.exception.reason, "STATE_BACKUP_FAILED")
        self.assertEqual(self.user_version(), 3)
        self.assertNotIn("fleet_observations", self.tables())
        self.assertEqual(self.backups(), [])
        # The refusal is retryable and the retry creates the table once.
        ledger.connect(create=True)
        self.assertEqual(ledger.version, SCHEMA_VERSION)
        ledger.close()
        reopened = Ledger(self.path, clock=lambda: self.now).connect(create=True)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.version, SCHEMA_VERSION)
        self.assertEqual(len(self.backups()), 1)

    def test_a_v3_size_mismatch_refuses_before_any_schema_write(self):
        self._write(3)

        def truncated(source, target):
            Path(target).write_bytes(b"")

        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        with mock.patch("ci_burst.ledger.shutil.copy2", side_effect=truncated):
            with self.assertRaises(StateRefusal) as raised:
                ledger.connect(create=True)
        self.assertEqual(raised.exception.reason, "STATE_BACKUP_FAILED")
        self.assertEqual(self.user_version(), 3)
        self.assertNotIn("fleet_observations", self.tables())

    def test_read_only_v3_reports_no_fleet_observation(self):
        self._write(3)
        ledger = Ledger(self.path, clock=lambda: self.now).connect(create=False, read_only=True)
        self.addCleanup(ledger.close)
        self.assertEqual(ledger.version, 3)
        # An older readable schema has no table: that is an absent observation,
        # not an unreadable ledger.
        self.assertIsNone(ledger.latest_fleet_observation())


    def test_v4_upgrade_adds_the_lifetime_column_once_and_leaves_rows_null(self):
        self._write(4)
        original = self.path.read_bytes()
        ledger = self.writable()
        self.assertEqual(ledger.version, 5)
        self.assertEqual(self.user_version(), 5)
        self.assertIn("admitted_lifetime_seconds", self.columns())
        # One copy, named for the schema it holds, with the version-4 bytes.
        self.assertEqual(self.backups(), [])
        (backup,) = self.v5_backups()
        self.assertEqual(backup.read_bytes(), original)
        # No backfill: a lifetime that was never recorded is not invented.
        self.assertIsNone(ledger.operation("old-op").admitted_lifetime_seconds)
        self.assertIsNone(ledger.operation("live-op").admitted_lifetime_seconds)
        self.assertEqual(ledger.operation("live-op").admitted_buffer_micros, 7_000)
        self.assertEqual(ledger.operation("old-op").settled_micros, 12_345)
        ledger.close()
        reopened = Ledger(self.path, clock=lambda: self.now).connect(create=True)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.version, 5)
        self.assertEqual(len(self.v5_backups()), 1)

    def test_every_earlier_schema_reaches_the_shape_of_a_fresh_one(self):
        fresh_path = self.path.parent / "fresh.sqlite3"
        fresh = Ledger(fresh_path, clock=lambda: self.now).connect()
        self.addCleanup(fresh.close)
        expected = self.shape(fresh_path)
        self.assertEqual(expected[2], 5)
        self.assertIn("admitted_lifetime_seconds", [column[1] for column in expected[1]["operations"]])
        for version in (1, 2, 3, 4):
            with self.subTest(version=version):
                self._write(version)
                original = self.path.read_bytes()
                ledger = Ledger(self.path, clock=lambda: self.now)
                ledger.acquire_writer()
                ledger.connect(create=True, backfill=(40_000, 5_000, 7_000))
                self.assertEqual(ledger.version, 5)
                self.assertEqual(self.shape(self.path), expected)
                self.assertIsNone(ledger.operation("live-op").admitted_lifetime_seconds)
                self.assertIsNone(ledger.operation("old-op").admitted_lifetime_seconds)
                # Every step copies before its own first write, so the earliest
                # copy still holds the file as it was found.
                (last,) = self.v5_backups()
                if version == 4:
                    self.assertEqual(last.read_bytes(), original)
                    self.assertEqual(self.backups(), [])
                else:
                    (first,) = self.backups()
                    self.assertEqual(first.read_bytes(), original)
                ledger.close()
                for leftover in self.path.parent.glob("state.sqlite3.pre-v*"):
                    leftover.unlink()

    def test_read_only_accepts_a_v4_file_without_the_lifetime_column(self):
        self._write(4)
        before = sorted(item.name for item in self.path.parent.iterdir())
        ledger = Ledger(self.path, clock=lambda: self.now).connect(create=False, read_only=True)
        self.addCleanup(ledger.close)
        self.assertEqual(ledger.version, 4)
        self.assertNotIn("admitted_lifetime_seconds", self.columns())
        operation = ledger.operation("live-op")
        self.assertIsNone(operation.admitted_lifetime_seconds)
        self.assertEqual(operation.admitted_buffer_micros, 7_000)
        self.assertEqual([item.id for item in ledger.operations()], ["old-op", "live-op"])
        self.assertEqual(self.user_version(), 4)
        self.assertEqual(before, sorted(item.name for item in self.path.parent.iterdir()))

    def test_a_v4_copy_failure_leaves_the_database_unchanged(self):
        self._write(4)
        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        with mock.patch("ci_burst.ledger.shutil.copy2", side_effect=OSError("denied")):
            with self.assertRaises(StateRefusal) as raised:
                ledger.connect(create=True)
        self.assertEqual(raised.exception.reason, "STATE_BACKUP_FAILED")
        self.assertEqual(self.user_version(), 4)
        self.assertNotIn("admitted_lifetime_seconds", self.columns())
        self.assertEqual(self.v5_backups(), [])
        # The refusal is retryable and the retry adds the column once.
        ledger.connect(create=True)
        self.assertEqual(ledger.version, 5)
        self.assertIn("admitted_lifetime_seconds", self.columns())

    def test_a_v4_size_mismatch_refuses_before_any_schema_write(self):
        self._write(4)

        def truncated(source, target):
            Path(target).write_bytes(b"")

        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        with mock.patch("ci_burst.ledger.shutil.copy2", side_effect=truncated):
            with self.assertRaises(StateRefusal) as raised:
                ledger.connect(create=True)
        self.assertEqual(raised.exception.reason, "STATE_BACKUP_FAILED")
        self.assertEqual(self.user_version(), 4)
        self.assertNotIn("admitted_lifetime_seconds", self.columns())

    def test_a_failed_v4_upgrade_rolls_back_and_can_be_retried(self):
        self._write(4)
        ledger = Ledger(self.path, clock=lambda: self.now)
        ledger.acquire_writer()
        self.addCleanup(ledger.close)
        real_connect = sqlite3.connect

        class FailingAlter(sqlite3.Connection):
            def execute(self, sql, *args):
                if sql.startswith("PRAGMA user_version = 5"):
                    raise sqlite3.OperationalError("injected")
                return super().execute(sql, *args)

        with mock.patch(
            "ci_burst.ledger.sqlite3.connect",
            side_effect=lambda *a, **k: real_connect(*a, factory=FailingAlter, **k),
        ):
            with self.assertRaises(StateRefusal) as raised:
                ledger.connect(create=True)
        self.assertEqual(raised.exception.reason, "STATE_MIGRATION_FAILED")
        # The added column rolled back with the failed version write.
        self.assertEqual(self.user_version(), 4)
        self.assertNotIn("admitted_lifetime_seconds", self.columns())
        ledger.connect(create=True)
        self.assertEqual(ledger.version, 5)
        self.assertIn("admitted_lifetime_seconds", self.columns())


class FleetObservationTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "state.sqlite3"
        self.now = 1_800_000_000.0
        self.ledger = Ledger(self.path, clock=lambda: self.now).connect()
        self.addCleanup(self.ledger.close)

    def _count(self):
        connection = sqlite3.connect(self.path)
        try:
            return connection.execute("SELECT COUNT(*) FROM fleet_observations").fetchone()[0]
        finally:
            connection.close()

    def test_records_and_reads_the_newest_snapshot(self):
        self.ledger.record_fleet_observation(
            observed_at=self.now,
            contract="forgejo-17.0.1-runners",
            running_attempts=2,
            active_registrations=3,
            offline_registrations=4,
        )
        self.assertEqual(
            self.ledger.latest_fleet_observation(),
            {
                "observed_at": self.now,
                "contract": "forgejo-17.0.1-runners",
                "running_attempts": 2,
                "active_registrations": 3,
                "offline_registrations": 4,
            },
        )
        self.ledger.record_fleet_observation(
            observed_at=self.now + 60,
            contract="forgejo-17.0.1-runners",
            running_attempts=0,
            active_registrations=1,
            offline_registrations=0,
        )
        self.assertEqual(self.ledger.latest_fleet_observation()["observed_at"], self.now + 60)
        self.assertEqual(self._count(), 2)

    def test_prunes_snapshots_older_than_the_retention(self):
        self.ledger.record_fleet_observation(
            observed_at=self.now,
            contract="forgejo-17.0.1-runners",
            running_attempts=1,
            active_registrations=1,
            offline_registrations=0,
        )
        self.ledger.record_fleet_observation(
            observed_at=self.now + FLEET_RETENTION_SECONDS + 1,
            contract="forgejo-17.0.1-runners",
            running_attempts=0,
            active_registrations=0,
            offline_registrations=0,
        )
        self.assertEqual(self._count(), 1)


if __name__ == "__main__":
    unittest.main()
