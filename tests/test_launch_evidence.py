import contextlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from ci_burst.export import POST_REGISTRATION_STEPS, STATE_VERSION
from ci_burst.launch_evidence import (
    EVIDENCE_MAX_AGE_SECONDS,
    FUTILE,
    FUTILE_LAUNCH_COUNT,
    FUTILE_LAUNCH_WINDOW_SECONDS,
    MAX_RECORD_BYTES,
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


class CountingStream:
    """Stand in for the stream that the reader opens and note each read it makes.

    ``reads`` gets one ``(requested, returned)`` pair per read. ``before_read``
    runs just before a read, which lets a test change the file after the reader
    has taken its size.
    """

    def __init__(self, stream, reads, before_read=None):
        self._stream = stream
        self._reads = reads
        self._before_read = before_read

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._stream.close()

    def fileno(self):
        return self._stream.fileno()

    def read(self, size=-1):
        if self._before_read is not None:
            self._before_read()
        data = self._stream.read(size)
        self._reads.append((size, len(data)))
        return data


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

    def test_a_nonstandard_json_constant_makes_the_file_malformed(self):
        # ``json`` accepts these three words, but they are not JSON and ``export``
        # never writes them. Each one sits in a field that the gate does not read,
        # so the decoder refuses the file and no field rule does.
        three = {f"i-0123456789abcde{index:02x}": futile(index=index) for index in range(3)}
        self.write(payload={"version": STATE_VERSION, "instances": three})
        self.assertTrue(self.read().holds, "without the constant the same records hold")
        for word, constant in (("NaN", float("nan")), ("Infinity", float("inf")), ("-Infinity", float("-inf"))):
            for place in ("an unread field of a record", "an unread top-level field"):
                with self.subTest(constant=word, place=place):
                    instances = dict(three)
                    payload = {"version": STATE_VERSION, "instances": instances}
                    if place == "an unread top-level field":
                        payload["console_reads"] = constant
                    else:
                        instances["i-0123456789abcdef9"] = futile(index=9, first_seen=constant)
                    self.write(payload=payload)
                    self.assertIn(word.encode("ascii"), self.path.read_bytes())
                    evidence = self.read()
                    self.assertEqual(evidence.as_dict(), {"state": "malformed", "considered": 0, "futile": 0})
                    self.assertFalse(evidence.holds)

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


class NonRegularPathTests(ReaderCase):
    """Only a regular file can be the record, and reading the path never blocks.

    A plain read-only open of a FIFO that has no writer blocks forever, and
    ``apply`` reads the record while it holds the ledger writer lock. A reader
    that regresses must fail these tests, not hang the run, so each read runs in
    a thread that the test abandons after a bound.
    """

    BOUND = 5.0

    def read_within_the_bound(self):
        outcome = []
        worker = threading.Thread(
            target=lambda: outcome.append(self.read()), name="reader-under-test", daemon=True
        )
        worker.start()
        worker.join(self.BOUND)
        if worker.is_alive():
            # Let the thread end: a writer wakes a reader that waits in the
            # open, and closing the writer again ends the read that follows.
            with contextlib.suppress(OSError):
                os.close(os.open(self.path, os.O_RDWR))
            worker.join(self.BOUND)
            self.fail(f"the reader was still blocked after {self.BOUND} seconds")
        return outcome[0]

    def assert_unreadable(self, evidence):
        self.assertEqual(evidence.as_dict(), {"state": "unreadable", "considered": 0, "futile": 0})
        self.assertFalse(evidence.holds)

    def test_a_fifo_with_no_writer_is_unreadable_and_never_blocks(self):
        self.path.parent.mkdir(parents=True)
        os.mkfifo(self.path)
        self.assert_unreadable(self.read_within_the_bound())

    def test_a_directory_is_unreadable_and_never_blocks(self):
        self.path.mkdir(parents=True)
        self.assert_unreadable(self.read_within_the_bound())

    def test_a_link_to_a_valid_record_is_unreadable(self):
        # ``export`` replaces the path by a rename of a regular file and never
        # leaves a link there, so a link is not its output.
        target = Path(self._directory.name) / "elsewhere.json"
        write_observed(target, [futile(index=index) for index in range(3)], mtime=NOW)
        self.assertTrue(read_launch_evidence(target, NOW).holds)
        self.path.parent.mkdir(parents=True)
        self.path.symlink_to(target)
        self.assert_unreadable(self.read_within_the_bound())

    def test_a_path_that_is_not_a_regular_file_is_unreadable_before_it_is_stale(self):
        self.path.parent.mkdir(parents=True)
        os.mkfifo(self.path)
        os.utime(self.path, (NOW - 7200, NOW - 7200))
        self.assert_unreadable(self.read_within_the_bound())


class BoundedReadTests(ReaderCase):
    """The reader decides from the open file before it reads any content.

    Each file here holds three futile records and trailing spaces. That is valid
    JSON, so a reader that parsed the file would hold, and the spaces make the
    size. ``CountingStream`` shows what the reader asked of the stream.
    """

    def write_padded(self, size, *, mtime=NOW):
        instances = {f"i-0123456789abcde{index:02x}": futile(index=index) for index in range(3)}
        raw = json.dumps({"version": STATE_VERSION, "instances": instances}).encode("utf-8")
        self.write(raw=raw + b" " * (size - len(raw)), mtime=mtime)

    def read_noting_reads(self, before_read=None):
        """Return the evidence and the ``(requested, returned)`` pair of each read."""
        reads = []
        real_open = open

        def noting_open(*args, **kwargs):
            return CountingStream(real_open(*args, **kwargs), reads, before_read)

        with mock.patch("ci_burst.launch_evidence.open", noting_open):
            return self.read(), reads

    def test_the_bound_is_sixteen_mebibytes(self):
        self.assertEqual(MAX_RECORD_BYTES, 16 * 1024 * 1024)

    def test_a_file_at_the_bound_is_read_by_a_read_that_stops_one_byte_past_it(self):
        self.write_padded(MAX_RECORD_BYTES)
        evidence, reads = self.read_noting_reads()
        self.assertEqual(evidence.as_dict(), {"state": "fresh", "considered": 3, "futile": 3})
        self.assertTrue(evidence.holds)
        self.assertEqual(reads, [(MAX_RECORD_BYTES + 1, MAX_RECORD_BYTES)])

    def test_a_fresh_file_above_the_bound_is_malformed_and_is_not_read(self):
        self.write_padded(MAX_RECORD_BYTES + 1)
        evidence, reads = self.read_noting_reads()
        self.assertEqual(evidence.as_dict(), {"state": "malformed", "considered": 0, "futile": 0})
        self.assertFalse(evidence.holds)
        self.assertEqual(reads, [])

    def test_a_stale_file_above_the_bound_is_stale_and_is_not_read(self):
        for name, mtime in (
            ("past the staleness bound", NOW - EVIDENCE_MAX_AGE_SECONDS - 1),
            ("dated in the future", NOW + 1),
        ):
            with self.subTest(name):
                self.write_padded(MAX_RECORD_BYTES + 1, mtime=mtime)
                evidence, reads = self.read_noting_reads()
                self.assertEqual(evidence.as_dict(), {"state": "stale", "considered": 0, "futile": 0})
                self.assertFalse(evidence.holds)
                self.assertEqual(reads, [])

    def test_a_file_that_grows_after_its_size_was_taken_is_malformed_and_read_only_to_the_bound(self):
        self.write_padded(MAX_RECORD_BYTES)

        def double():
            with open(self.path, "ab") as stream:
                stream.write(b" " * MAX_RECORD_BYTES)

        evidence, reads = self.read_noting_reads(before_read=double)
        self.assertEqual(evidence.as_dict(), {"state": "malformed", "considered": 0, "futile": 0})
        self.assertFalse(evidence.holds)
        self.assertEqual(reads, [(MAX_RECORD_BYTES + 1, MAX_RECORD_BYTES + 1)])


class ExporterShapeTests(ReaderCase):
    """The reader takes a record only in a shape that ``export`` writes.

    The instance key and the two markers that carry registration evidence decide
    a hold, so a value that ``export`` cannot have written makes the whole file
    ``malformed``. ``malformed`` is asserted, and not ``unreadable``: a value of
    the wrong type must be refused by the shape rule and not by the reader's
    last-resort handler.
    """

    RUNNER = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
    HOST = "ip-10-0-0-1"

    #: Each one fails the pattern that ``export`` applies to an instance key.
    INVALID_KEYS = (
        "",
        "i-",
        "i-0123456",  # fewer than 8 hexadecimal digits
        "i-0123456789abcdef01",  # more than 17
        "i-0123456789ABCDEF0",
        "i-0123456789abcdeg",
        "i-0123456789abcdef0\n",
        " i-0123456789abcdef0",
        "0123456789abcdef0",
        "instance-1",
    )

    #: Each one is a value that ``export`` cannot write for ``job_process``.
    INVALID_JOB_PROCESS = (
        [],
        ["completed"],
        ["completed", 0, 0],
        ["completed", 1],  # the outcome and the status are one vocabulary
        ["failed", 0],
        ["failed", 124],
        ["timeout", 1],
        ["cancelled", 3],
        [1, 0],
        [["completed"], 0],  # not hashable
        ["completed", True],
        ["completed", 0.0],
        ["completed", "0"],
        ["failed", 256],
        ["failed", -1],
        {"outcome": "completed", "rc": 0},
        "completed",
        0,
        False,
        "",
    )

    #: Each one is a value that ``export`` cannot write for ``spot_interruption``.
    INVALID_SPOT_INTERRUPTION = (
        [],
        ["terminate"],
        ["terminate", HOST],
        ["terminate", HOST, None, None],
        ["evacuate", HOST, None],
        ["terminate", "", None],
        ["terminate", "not a host", None],
        # ``export`` stores null for the console spelling and drops the others.
        ["terminate", HOST, "unknown"],
        ["terminate", HOST, ""],
        ["terminate", HOST, "not-a-uuid"],
        ["terminate", HOST, 7],
        [["terminate"], HOST, None],  # not hashable
        ["terminate", [HOST], None],
        {"action": "terminate"},
        "terminate",
        0,
        False,
        "",
    )

    VALID_JOB_PROCESS = (
        ["completed", 0],
        ["failed", 1],
        ["failed", 255],
        ["timeout", 124],
        ["timeout", 137],
    )

    VALID_SPOT_INTERRUPTION = (
        ["terminate", HOST, RUNNER],
        ["stop", HOST, RUNNER],
        ["hibernate", HOST, RUNNER],
        ["unknown", HOST, RUNNER],
        ["terminate", HOST, None],
        ["unknown", HOST, None],
    )

    def three_futile(self):
        return {f"i-0123456789abcde{index:02x}": futile(index=index) for index in range(3)}

    def write_instances(self, instances):
        self.write(payload={"version": STATE_VERSION, "instances": instances})

    def test_three_futile_records_under_keys_that_export_rejects_do_not_hold(self):
        self.write_instances(
            {key: futile(index=index) for index, key in enumerate(self.INVALID_KEYS[:3])}
        )
        evidence = self.read()
        self.assertEqual(evidence.as_dict(), {"state": "malformed", "considered": 0, "futile": 0})
        self.assertFalse(evidence.holds)

    def test_one_key_that_export_rejects_makes_the_whole_file_malformed(self):
        for key in self.INVALID_KEYS:
            with self.subTest(key=key):
                instances = self.three_futile()
                instances[key] = futile(index=9)
                self.write_instances(instances)
                self.assertEqual(
                    self.read().as_dict(), {"state": "malformed", "considered": 0, "futile": 0}
                )

    def test_keys_at_the_bounds_that_export_accepts_are_valid(self):
        self.write_instances(
            {
                "i-01234567": futile(index=1),  # 8 hexadecimal digits
                "i-0123456789abc": futile(index=2),
                "i-0123456789abcdef0": futile(index=3),  # 17
            }
        )
        evidence = self.read()
        self.assertEqual(evidence.as_dict(), {"state": "fresh", "considered": 3, "futile": 3})
        self.assertTrue(evidence.holds)

    def test_a_marker_that_export_cannot_write_makes_the_whole_file_malformed(self):
        cases = [("job_process", value) for value in self.INVALID_JOB_PROCESS]
        cases += [("spot_interruption", value) for value in self.INVALID_SPOT_INTERRUPTION]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                instances = self.three_futile()
                instances["i-0123456789abcdef9"] = futile(index=9, **{key: value})
                self.write_instances(instances)
                self.assertEqual(
                    self.read().as_dict(), {"state": "malformed", "considered": 0, "futile": 0}
                )

    def test_every_marker_shape_that_export_writes_is_registration_evidence(self):
        cases = [("job_process", value) for value in self.VALID_JOB_PROCESS]
        cases += [("spot_interruption", value) for value in self.VALID_SPOT_INTERRUPTION]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                instances = self.three_futile()
                instances["i-0123456789abcdef9"] = futile(index=9, **{key: value})
                self.write_instances(instances)
                # The fourth worker ties the third place, so it is considered
                # and, with its marker, it is registered and keeps the gate open.
                evidence = self.read()
                self.assertEqual(evidence.as_dict(), {"state": "fresh", "considered": 4, "futile": 3})
                self.assertFalse(evidence.holds)
                self.assertEqual(classify(futile(**{key: value})), REGISTERED)


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
