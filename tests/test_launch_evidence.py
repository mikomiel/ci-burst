import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ci_burst.export import POST_REGISTRATION_STEPS, STATE_VERSION
from ci_burst.launch_evidence import (
    EVIDENCE_MAX_AGE_SECONDS,
    FUTILE,
    FUTILE_LAUNCH_COUNT,
    FUTILE_LAUNCH_WINDOW_SECONDS,
    REGISTERED,
    classify,
    read_launch_evidence,
)

from support import futile_record, operation_hex, registered_record, write_observed

NOW = 1_800_000_000.0


def futile(age=60, index=1, **overrides):
    return futile_record(NOW - age, index, **overrides)


def registered(age=60, index=1, **overrides):
    return registered_record(NOW - age, index, **overrides)


class ReaderCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "export" / "observed.json"

    def write(self, records=(), *, payload=None, raw=None, mtime=NOW):
        write_observed(self.path, records, payload=payload, raw=raw, mtime=mtime)

    def read(self, now=NOW):
        return read_launch_evidence(self.path, now)


class EvidenceStateTests(ReaderCase):
    def test_a_missing_file_is_missing(self):
        evidence = self.read()
        self.assertEqual(evidence.as_dict(), {"state": "missing", "considered": 0, "futile": 0})
        self.assertFalse(evidence.holds)

    def test_a_missing_directory_is_missing(self):
        evidence = read_launch_evidence(Path(self._directory.name) / "absent" / "observed.json", NOW)
        self.assertEqual(evidence.state, "missing")

    def test_a_path_that_cannot_be_read_is_unreadable(self):
        # A directory opens on Linux but cannot be read as a file. Tests run as
        # root in some places, so a mode bit cannot produce this state.
        self.path.mkdir(parents=True)
        evidence = self.read()
        self.assertEqual(evidence.as_dict(), {"state": "unreadable", "considered": 0, "futile": 0})
        self.assertFalse(evidence.holds)

    def test_a_permission_failure_is_unreadable(self):
        self.write([futile(index=index) for index in range(3)])
        with mock.patch("builtins.open", side_effect=PermissionError("denied")):
            self.assertEqual(self.read().state, "unreadable")

    def test_an_unforeseen_failure_is_unreadable_and_never_raises(self):
        self.write([futile(index=index) for index in range(3)])
        with mock.patch("ci_burst.launch_evidence.json.loads", side_effect=RuntimeError("boom")):
            evidence = self.read()
        self.assertEqual(evidence.as_dict(), {"state": "unreadable", "considered": 0, "futile": 0})

    def test_the_file_at_the_staleness_bound_is_fresh(self):
        self.write([futile(index=index) for index in range(3)], mtime=NOW - EVIDENCE_MAX_AGE_SECONDS)
        evidence = self.read()
        self.assertEqual(evidence.state, "fresh")
        self.assertTrue(evidence.holds)

    def test_the_file_past_the_staleness_bound_is_stale_whatever_it_says(self):
        self.write([futile(index=index) for index in range(3)], mtime=NOW - EVIDENCE_MAX_AGE_SECONDS - 1)
        evidence = self.read()
        self.assertEqual(evidence.as_dict(), {"state": "stale", "considered": 0, "futile": 0})
        self.assertFalse(evidence.holds)

    def test_a_file_dated_in_the_future_is_stale(self):
        self.write([futile(index=index) for index in range(3)], mtime=NOW + 1)
        self.assertEqual(self.read().state, "stale")

    def test_stale_is_tested_before_malformed(self):
        self.write(raw=b"\xff not json", mtime=NOW - EVIDENCE_MAX_AGE_SECONDS - 1)
        self.assertEqual(self.read().state, "stale")

    def test_unreadable_is_tested_before_stale(self):
        self.path.mkdir(parents=True)
        os.utime(self.path, (NOW - 7200, NOW - 7200))
        self.assertEqual(self.read().state, "unreadable")

    def test_a_file_that_is_not_utf8_json_is_malformed(self):
        for raw in (b"\xff\xfe", b"{not json", b"", b"[" * 100_000):
            with self.subTest(raw=raw[:8]):
                self.write(raw=raw)
                self.assertEqual(
                    self.read().as_dict(), {"state": "malformed", "considered": 0, "futile": 0}
                )

    def test_a_file_that_is_not_an_object_is_malformed(self):
        self.write(payload=[1, 2, 3])
        self.assertEqual(self.read().state, "malformed")

    def test_another_version_is_malformed(self):
        for version in (STATE_VERSION + 1, STATE_VERSION - 1, None, str(STATE_VERSION)):
            with self.subTest(version=version):
                self.write(payload={"version": version, "instances": {}})
                self.assertEqual(self.read().state, "malformed")

    def test_instances_that_are_not_an_object_are_malformed(self):
        for instances in ([futile()], None, "x"):
            with self.subTest(instances=instances):
                self.write(payload={"version": STATE_VERSION, "instances": instances})
                self.assertEqual(self.read().state, "malformed")

    def test_one_invalid_record_makes_the_whole_file_malformed(self):
        bad = {
            "a record that is not an object": "x",
            "seen as text": futile(seen="soon"),
            "seen as a boolean": futile(seen=True),
            "seen not finite": futile(seen=float("nan")),
            "seen infinite": futile(seen=float("inf")),
            "seen null": futile(seen=None),
            "state as a number": futile(state=3),
            "operation as a list": futile(operation=["x"]),
            "exit code as text": futile(exit_code="1"),
            "exit code as a boolean": futile(exit_code=True),
            "exit code above 255": futile(exit_code=256),
            "exit code negative": futile(exit_code=-1),
            "job process as text": futile(job_process="completed"),
            "interruption as an object": futile(spot_interruption={"action": "terminate"}),
            "steps as text": futile(steps="register"),
            "a step that is not text": futile(steps=["mount-options", 1]),
            "registered as text": futile(registered="false"),
        }
        for name, record in bad.items():
            with self.subTest(name):
                # Three valid futile records would hold on their own.
                self.write([futile(index=index) for index in range(3)] + [record])
                self.assertEqual(
                    self.read().as_dict(), {"state": "malformed", "considered": 0, "futile": 0}
                )

    def test_a_record_without_a_field_the_gate_reads_is_malformed(self):
        for key in ("seen", "state", "operation", "exit_code", "job_process", "spot_interruption", "steps", "registered"):
            with self.subTest(key=key):
                record = futile(index=9)
                del record[key]
                self.write([futile(index=index) for index in range(3)] + [record])
                self.assertEqual(self.read().state, "malformed")

    def test_a_seen_after_the_clock_makes_the_file_malformed(self):
        self.write([futile(index=index) for index in range(3)] + [futile(age=-1, index=9)])
        self.assertEqual(self.read().state, "malformed")

    def test_a_large_integer_seen_is_malformed(self):
        raw = (
            '{"version": %d, "instances": {"i-0123456789abcdef0": %s}}'
            % (STATE_VERSION, json.dumps(futile()).replace(f'"seen": {NOW - 60}', '"seen": 1' + "0" * 400))
        ).encode("utf-8")
        self.assertIn(b'"seen": 1000', raw)
        self.write(raw=raw)
        self.assertEqual(self.read().state, "malformed")

    def test_a_null_or_unknown_provider_state_is_valid_and_not_ended(self):
        for state in (None, "unknown", "rebooting"):
            with self.subTest(state=state):
                self.write(
                    [futile(index=1), futile(index=2), futile(index=3, state=state)]
                )
                evidence = self.read()
                self.assertEqual(evidence.state, "fresh")
                self.assertEqual(evidence.considered, 2)
                self.assertFalse(evidence.holds)

    def test_fields_the_gate_does_not_read_are_not_validated(self):
        self.write([futile(index=index, zone=7, first_seen=None, errors="x") for index in range(3)])
        self.assertTrue(self.read().holds)

    def test_the_reader_leaves_the_file_alone(self):
        self.write([futile(index=index) for index in range(3)], mtime=NOW - 30)
        before = (self.path.read_bytes(), os.stat(self.path).st_mtime)
        self.read()
        self.assertEqual((self.path.read_bytes(), os.stat(self.path).st_mtime), before)
        self.assertEqual(sorted(os.listdir(self.path.parent)), ["observed.json"])


class ClassificationTests(ReaderCase):
    def test_an_ended_worker_with_the_exit_marker_and_no_registration_is_futile(self):
        for state in ("terminated", "stopped"):
            with self.subTest(state=state):
                self.assertEqual(classify(futile(state=state)), FUTILE)

    def test_the_registration_step_is_registered_with_or_without_the_exit_marker(self):
        self.assertEqual(classify(futile(steps=["mount-options", "register"])), REGISTERED)
        self.assertEqual(classify(futile(steps=["register"], exit_code=None)), REGISTERED)
        self.assertEqual(classify(futile(registered=True)), REGISTERED)

    def test_a_later_marker_is_registered_when_the_registration_line_was_lost(self):
        for name, overrides in (
            ("one-job outcome", {"job_process": ["completed", 0]}),
            ("interruption notice", {"spot_interruption": ["terminate", "ip-10-0-0-1", None]}),
            ("job step", {"steps": ["job"]}),
            ("claim-timeout step", {"steps": ["claim-timeout"]}),
        ):
            with self.subTest(name):
                self.assertEqual(classify(futile(**overrides)), REGISTERED)

    def test_no_exit_marker_and_no_registration_is_no_evidence(self):
        self.assertIsNone(classify(futile(exit_code=None)))
        self.assertIsNone(classify(futile(exit_code=None, steps=[])))

    def test_an_instance_that_has_not_ended_is_no_evidence(self):
        for state in ("pending", "running", "shutting-down", "stopping", None, "unknown"):
            with self.subTest(state=state):
                self.assertIsNone(classify(futile(state=state)))
                # A registered worker that still runs is no evidence either.
                self.assertIsNone(classify(registered(state=state)))

    def test_a_record_without_an_operation_of_this_deployment_is_no_evidence(self):
        for value in (None, "", "op", "A" * 32, operation_hex(1) + "\n", operation_hex(1)[:31], operation_hex(1) + "0"):
            with self.subTest(value=value):
                self.assertIsNone(classify(futile(operation=value)))

    def test_the_post_registration_steps_exclude_the_registration_step(self):
        self.assertNotIn("register", POST_REGISTRATION_STEPS)
        self.assertNotIn("mount-options", POST_REGISTRATION_STEPS)


class ConsideredSetTests(ReaderCase):
    def evidence(self, records, now=NOW):
        self.write(records)
        return self.read(now).as_dict()

    def test_three_futile_workers_hold(self):
        evidence = self.evidence([futile(age=60 * index, index=index) for index in range(1, 4)])
        self.assertEqual(evidence, {"state": "fresh", "considered": 3, "futile": 3})
        self.assertTrue(self.read().holds)

    def test_two_futile_and_one_registered_do_not_hold(self):
        evidence = self.evidence([futile(age=60, index=1), registered(age=120, index=2), futile(age=180, index=3)])
        self.assertEqual(evidence, {"state": "fresh", "considered": 3, "futile": 2})
        self.assertFalse(self.read().holds)

    def test_fewer_than_three_classified_workers_do_not_hold(self):
        evidence = self.evidence([futile(age=60, index=1), futile(age=120, index=2)])
        self.assertEqual(evidence, {"state": "fresh", "considered": 2, "futile": 2})
        self.assertFalse(self.read().holds)
        self.assertEqual(FUTILE_LAUNCH_COUNT, 3)

    def test_an_empty_record_does_not_hold(self):
        self.assertEqual(self.evidence([]), {"state": "fresh", "considered": 0, "futile": 0})

    def test_a_registered_worker_older_than_the_newest_three_does_not_clear_a_hold(self):
        records = [futile(age=60 * index, index=index) for index in range(1, 4)]
        records.append(registered(age=600, index=4))
        self.assertEqual(self.evidence(records), {"state": "fresh", "considered": 3, "futile": 3})
        self.assertTrue(self.read().holds)

    def test_the_newest_by_seen_decide_whatever_the_file_order(self):
        records = [registered(age=600, index=4)] + [futile(age=60 * index, index=index) for index in range(1, 4)]
        self.assertTrue(self.evidence(records) == {"state": "fresh", "considered": 3, "futile": 3})
        records = [futile(age=60 * index, index=index) for index in range(1, 4)] + [registered(age=30, index=4)]
        self.assertEqual(self.evidence(records), {"state": "fresh", "considered": 3, "futile": 2})

    def test_unclassified_records_do_not_take_a_place(self):
        records = [futile(age=60 * index, index=index) for index in range(1, 4)]
        records += [
            futile(age=10, index=5, exit_code=None),
            registered(age=10, index=6, state="running"),
            registered(age=10, index=7, operation=None),
        ]
        self.assertEqual(self.evidence(records), {"state": "fresh", "considered": 3, "futile": 3})

    def test_a_worker_at_the_window_edge_is_inside(self):
        records = [futile(age=60, index=1), futile(age=120, index=2), futile(age=FUTILE_LAUNCH_WINDOW_SECONDS, index=3)]
        self.assertEqual(self.evidence(records), {"state": "fresh", "considered": 3, "futile": 3})
        self.assertTrue(self.read().holds)

    def test_a_worker_past_the_window_edge_is_outside(self):
        records = [
            futile(age=60, index=1),
            futile(age=120, index=2),
            futile(age=FUTILE_LAUNCH_WINDOW_SECONDS + 1, index=3),
        ]
        self.assertEqual(self.evidence(records), {"state": "fresh", "considered": 2, "futile": 2})
        self.assertFalse(self.read().holds)

    def test_every_worker_aged_out_is_none_considered(self):
        records = [futile(age=FUTILE_LAUNCH_WINDOW_SECONDS + index, index=index) for index in range(1, 4)]
        self.assertEqual(self.evidence(records), {"state": "fresh", "considered": 0, "futile": 0})

    def test_first_seen_does_not_move_a_record_in_or_out_of_the_window(self):
        # Old first reads, recent last reads: every worker is inside.
        inside = [futile(age=100, index=index, first_seen=NOW - 7200) for index in range(1, 4)]
        self.assertEqual(self.evidence(inside), {"state": "fresh", "considered": 3, "futile": 3})
        # Recent first reads cannot pull an old last read back in.
        outside = [
            futile(age=FUTILE_LAUNCH_WINDOW_SECONDS + 1, index=index, first_seen=NOW - 10)
            for index in range(1, 4)
        ]
        self.assertEqual(self.evidence(outside), {"state": "fresh", "considered": 0, "futile": 0})

    def test_a_tie_at_the_third_place_considers_every_tied_worker(self):
        records = [futile(age=300, index=index) for index in range(1, 4)] + [registered(age=300, index=4)]
        self.assertEqual(self.evidence(records), {"state": "fresh", "considered": 4, "futile": 3})
        self.assertFalse(self.read().holds)

    def test_a_tie_of_futile_workers_still_holds(self):
        records = [futile(age=60, index=1)] + [futile(age=300, index=index) for index in range(2, 6)]
        self.assertEqual(self.evidence(records), {"state": "fresh", "considered": 5, "futile": 5})
        self.assertTrue(self.read().holds)

    def test_a_tie_below_the_third_place_is_not_considered(self):
        records = [futile(age=60 * index, index=index) for index in range(1, 4)]
        records += [registered(age=600, index=4), registered(age=600, index=5)]
        self.assertEqual(self.evidence(records), {"state": "fresh", "considered": 3, "futile": 3})


if __name__ == "__main__":
    unittest.main()
