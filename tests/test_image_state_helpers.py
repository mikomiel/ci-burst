"""The read helpers that the image pipeline adds to the image state file.

A read-only open that creates nothing, the pin log, the orphan candidate, the
verified candidate and the newest completed journal row. The schema and its
version are unchanged by all of them, which ``SchemaVersionTests`` pins.
"""

import sqlite3
import unittest

from ci_burst.errors import StateRefusal
from ci_burst.image_state import SCHEMA_VERSION, STATE_FILE, ImageState, PinObservation

from test_image_state import ImageStateCase

DEPLOYMENT = "example-deployment"


class ReadOnlyOpenTests(ImageStateCase):
    def reader(self, directory=None):
        state = ImageState(directory or self.directory, clock=self.clock)
        self.addCleanup(state.close)
        return state

    def listing(self, directory=None):
        directory = directory or self.directory
        return sorted((item.name, item.stat().st_size) for item in directory.iterdir())

    def test_an_absent_directory_is_not_created(self):
        missing = self.root / "never-created"
        state = self.reader(missing)
        self.assertFalse(state.connect_readonly())
        self.assertFalse(state.available)
        self.assertFalse(missing.exists())

    def test_an_absent_file_creates_neither_the_file_nor_a_lock(self):
        empty = self.root / "empty"
        empty.mkdir()
        state = self.reader(empty)
        self.assertFalse(state.connect_readonly())
        self.assertFalse(state.available)
        self.assertEqual(self.listing(empty), [])

    def test_an_empty_file_is_nothing_to_read_and_stays_empty(self):
        empty = self.root / "empty"
        empty.mkdir()
        (empty / STATE_FILE).write_bytes(b"")
        state = self.reader(empty)
        self.assertFalse(state.connect_readonly())
        self.assertFalse(state.available)
        self.assertEqual(self.listing(empty), [(STATE_FILE, 0)])

    def test_the_readers_refuse_on_a_state_that_was_not_opened(self):
        state = self.reader(self.root / "never-created")
        state.connect_readonly()
        with self.assertRaises(StateRefusal) as raised:
            state.control_get()
        self.assertEqual(raised.exception.reason, "IMAGE_STATE_UNAVAILABLE")

    def test_an_existing_file_reads_and_is_left_byte_for_byte_as_it_was(self):
        self.add_build()
        self.state.close()
        before = (self.state.path.read_bytes(), self.listing())
        reader = self.reader()
        self.assertTrue(reader.connect_readonly())
        self.assertTrue(reader.available)
        self.assertEqual([build.id for build in reader.builds(DEPLOYMENT)], ["build-1"])
        self.assertEqual(reader.control_get().consecutive_failures, 0)
        self.assertIsNone(reader.pin_last())
        reader.close()
        self.assertEqual((self.state.path.read_bytes(), self.listing()), before)
        self.assertNotIn(STATE_FILE + ".lock", [name for name, _ in self.listing()])

    def test_a_write_through_the_read_only_connection_fails(self):
        self.state.close()
        reader = self.reader()
        reader.connect_readonly()
        with self.assertRaises(sqlite3.OperationalError):
            reader.control_set(blocked=1)
        with self.assertRaises(sqlite3.OperationalError):
            reader.pin_observe(3, "ami-0123456789abcdef0")
        self.assertFalse(reader.control_get().blocked)

    def test_a_second_call_keeps_the_open_connection(self):
        self.state.close()
        reader = self.reader()
        self.assertTrue(reader.connect_readonly())
        connection = reader._connection
        self.assertTrue(reader.connect_readonly())
        self.assertIs(reader._connection, connection)

    def test_it_reads_while_another_process_holds_the_writer_lock(self):
        self.add_build()
        self.state.acquire_writer()
        reader = self.reader()
        self.assertTrue(reader.connect_readonly())
        self.assertEqual(len(reader.builds(DEPLOYMENT)), 1)

    def test_a_file_at_another_schema_version_is_refused_and_not_kept_open(self):
        self.state.close()
        connection = sqlite3.connect(self.state.path)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        connection.close()
        before = self.state.path.read_bytes()
        reader = self.reader()
        with self.assertRaises(StateRefusal) as raised:
            reader.connect_readonly()
        self.assertEqual(raised.exception.reason, "IMAGE_STATE_VERSION_UNSUPPORTED")
        self.assertFalse(reader.available)
        self.assertEqual(self.state.path.read_bytes(), before)

    def test_a_file_that_is_not_a_database_is_refused_as_unreadable(self):
        self.state.close()
        self.state.path.write_bytes(b"this is not a database file" * 20)
        reader = self.reader()
        with self.assertRaises(StateRefusal) as raised:
            reader.connect_readonly()
        self.assertEqual(raised.exception.reason, "IMAGE_STATE_UNREADABLE")
        self.assertFalse(reader.available)


class SchemaVersionTests(ImageStateCase):
    def test_the_helpers_add_no_table_and_leave_the_version_alone(self):
        connection = sqlite3.connect(self.state.path)
        self.addCleanup(connection.close)
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables - {"sqlite_sequence"}, {"image_builds", "pin_log", "promotion_journal", "control"})
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(SCHEMA_VERSION, 1)


class PinLogTests(ImageStateCase):
    IMAGE = "ami-0123456789abcdef0"
    OTHER = "ami-0123456789abcdef1"

    def log(self):
        rows = self.state._require().execute("SELECT pin_version, image_id, observed_at FROM pin_log ORDER BY id")
        return [tuple(row) for row in rows]

    def test_there_is_no_observation_before_the_first(self):
        self.assertIsNone(self.state.pin_last())

    def test_the_first_observation_is_logged_with_the_time(self):
        self.assertTrue(self.state.pin_observe(3, self.IMAGE))
        self.assertEqual(self.state.pin_last(), PinObservation(3, self.IMAGE, self.clock.now))
        self.assertEqual(self.log(), [(3, self.IMAGE, self.clock.now)])

    def test_the_same_pin_and_image_add_no_row(self):
        self.state.pin_observe(3, self.IMAGE)
        self.clock.advance(300)
        self.assertFalse(self.state.pin_observe(3, self.IMAGE))
        self.assertEqual(len(self.log()), 1)

    def test_a_new_pin_or_a_new_image_behind_the_same_pin_adds_a_row(self):
        self.state.pin_observe(3, self.IMAGE)
        self.clock.advance(300)
        self.assertTrue(self.state.pin_observe(4, self.IMAGE))
        self.clock.advance(300)
        self.assertTrue(self.state.pin_observe(4, self.OTHER))
        self.assertEqual([(pin, image) for pin, image, _ in self.log()], [(3, self.IMAGE), (4, self.IMAGE), (4, self.OTHER)])
        self.assertEqual(self.state.pin_last().image_id, self.OTHER)

    def test_going_back_to_an_earlier_pin_is_a_change(self):
        for pin, image in ((3, self.IMAGE), (4, self.OTHER), (3, self.IMAGE)):
            self.assertTrue(self.state.pin_observe(pin, image))
        self.assertEqual(len(self.log()), 3)

    def test_it_runs_inside_a_caller_transaction_and_rolls_back_with_it(self):
        with self.assertRaises(RuntimeError):
            with self.state.transaction():
                self.state.pin_observe(3, self.IMAGE)
                raise RuntimeError("stop")
        self.assertEqual(self.log(), [])


class OrphanAndCandidateTests(ImageStateCase):
    def orphan(self, build_id="build-orphan", **overrides):
        values = dict(
            build_id=build_id,
            deployment=DEPLOYMENT,
            basis_digest="c" * 64,
            source_sha="d" * 40,
            image_id="ami-0123456789abcdef9",
            created_at=self.clock.now - 7200,
            reason_code="IMAGE_ORPHAN_RECORDED",
        )
        values.update(overrides)
        with self.state.transaction():
            return self.state.insert_orphan(**values)

    def verified(self, build_id, **overrides):
        self.add_build(build_id)
        values = dict(state="candidate", verified_at=self.clock.now, image_id="ami-0123456789abcdef8")
        values.update(overrides)
        with self.state.transaction():
            return self.state.advance_build(build_id, **values)

    def test_an_orphan_is_an_unverified_candidate_with_the_given_start_time(self):
        row = self.orphan()
        self.assertEqual(row.state, "candidate")
        self.assertEqual(row.reason_code, "IMAGE_ORPHAN_RECORDED")
        self.assertEqual(row.image_id, "ami-0123456789abcdef9")
        self.assertIsNone(row.verified_at)
        self.assertIsNone(row.gate_passed)
        self.assertIsNone(row.source_pin)
        self.assertEqual(row.created_at, self.clock.now - 7200)
        self.assertEqual(row.updated_at, self.clock.now)
        self.assertEqual(row.client_token, "orphan:build-orphan")
        self.assertEqual(self.state.build("build-orphan"), row)

    def test_an_orphan_does_not_meet_the_one_active_index(self):
        self.add_build("build-active")
        self.orphan()
        self.assertEqual(self.state.active_build(DEPLOYMENT).id, "build-active")

    def test_an_orphan_does_not_count_as_a_build_started_today(self):
        self.orphan(created_at=0.0)
        self.assertEqual(self.state.builds_started_since(DEPLOYMENT, self.clock.now - 86_400), 0)

    def test_the_same_image_is_not_recorded_twice(self):
        self.orphan()
        with self.assertRaises(sqlite3.IntegrityError):
            self.orphan()

    def test_the_verified_candidate_skips_an_orphan_even_when_the_orphan_is_newer(self):
        verified = self.verified("build-verified", )
        orphan = self.orphan(created_at=self.clock.now + 60)
        self.assertEqual(self.state.candidate(DEPLOYMENT).id, orphan.id)
        self.assertEqual(self.state.verified_candidate(DEPLOYMENT).id, verified.id)

    def test_there_is_no_verified_candidate_without_a_verification_time(self):
        self.assertIsNone(self.state.verified_candidate(DEPLOYMENT))
        self.orphan()
        self.assertIsNone(self.state.verified_candidate(DEPLOYMENT))
        self.add_build("build-plain")
        with self.state.transaction():
            self.state.advance_build("build-plain", state="candidate")
        self.assertIsNone(self.state.verified_candidate(DEPLOYMENT))

    def test_the_newest_verified_candidate_wins_and_only_candidates_count(self):
        self.verified("build-old")
        self.clock.advance(60)
        newest = self.verified("build-new")
        self.clock.advance(60)
        self.verified("build-promoted", state="promoted")
        self.assertEqual(self.state.verified_candidate(DEPLOYMENT).id, newest.id)

    def test_a_verified_candidate_belongs_to_one_deployment(self):
        self.verified("build-1")
        self.assertIsNone(self.state.verified_candidate("example-other-deployment"))


class JournalLastCompleteTests(ImageStateCase):
    def test_there_is_none_without_a_complete_row(self):
        self.assertIsNone(self.state.journal_last_complete())
        row = self.add_journal()
        self.assertIsNone(self.state.journal_last_complete())
        with self.state.transaction():
            self.state.journal_abort(row.id)
        self.assertIsNone(self.state.journal_last_complete())

    def test_the_newest_complete_row_of_either_kind_wins(self):
        first = self.add_journal(old_pin=3, new_pin=4, contained_pin=3)
        with self.state.transaction():
            self.state.journal_complete(first.id)
        self.assertEqual(self.state.journal_last_complete().old_pin, 3)
        second = self.add_journal(kind="rollback", old_pin=4, new_pin=3, contained_pin=3, candidate_id=None)
        with self.state.transaction():
            self.state.journal_complete(second.id)
        newest = self.state.journal_last_complete()
        self.assertEqual((newest.id, newest.kind, newest.old_pin), (second.id, "rollback", 4))

    def test_a_later_pending_or_aborted_row_does_not_replace_it(self):
        first = self.add_journal()
        with self.state.transaction():
            self.state.journal_complete(first.id)
        later = self.add_journal(old_pin=4, new_pin=5)
        self.assertEqual(self.state.journal_last_complete().id, first.id)
        with self.state.transaction():
            self.state.journal_abort(later.id)
        self.assertEqual(self.state.journal_last_complete().id, first.id)


if __name__ == "__main__":
    unittest.main()
