import io
import dataclasses
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ci_burst.aws import MAX_CONSOLE_BYTES, Aws
from ci_burst.config import load
from ci_burst.errors import ProviderRefusal
from ci_burst.export import (
    EVENT_SCHEMA,
    MAX_CONSOLE_READS,
    MAX_EVENTS_BYTES,
    POST_REGISTRATION_STEPS,
    STATE_VERSION,
    STEP_NAMES,
    Exporter,
    default_record_path,
    load_state,
    parse_console,
    prune_state,
)
from ci_burst.launch_evidence import FUTILE, RECORD_FIELDS, REGISTERED, classify, read_launch_evidence

from support import (
    FakeAws,
    futile_record,
    instance,
    ledger_holding,
    operation_hex,
    registered_record,
    write_config,
    write_observed,
)

NOW = 1_800_000_000.0

CLEAN_BOOT = """\
[    9.7] cloud-init[400]: STEP mount-options
[   10.7] cloud-init[400]: STEP register
[   10.8] cloud-init[400]: runner: burst-ip-10-0-0-1, with version: v1, ephemeral: true, declared successfully
[   10.9] cloud-init[400]: STEP job
[   24.0] cloud-init[400]: bootstrap exiting rc=0
"""

#: A worker whose claim window passed with no job: the claim-timeout marker,
#: then the exit marker with status 0. No `STEP job` and no one-job marker,
#: because no job attempt happened.
IDLE_WORKER = """\
[    9.7] cloud-init[400]: STEP mount-options
[   10.7] cloud-init[400]: STEP register
[   10.8] cloud-init[400]: runner: burst-ip-10-0-0-1, with version: v1, ephemeral: true, declared successfully
[  311.0] cloud-init[400]: Error: no task received
[  311.1] cloud-init[400]: STEP claim-timeout
[  311.2] cloud-init[400]: bootstrap exiting rc=0
"""

FAILED_PING = """\
[   12.1] cloud-init[477]: level=error msg="Cannot ping the Forgejo instance server" error="deadline_exceeded: Post \\"https://forgejo.example/api\\": context deadline exceeded"
[   12.2] cloud-init[477]: bootstrap exiting rc=1
"""

SSM_TIMEOUT = """\
[    9.9] cloud-init[410]: level=error msg="Connect timeout on endpoint URL: \\"https://ssm.eu-central-1.amazonaws.com/\\""
"""

NOISE = """\
[    1.0] kernel: random line that matches nothing
[    1.1] kernel: another line
"""

ONE_JOB_COMPLETED = "cloud-init: bootstrap one-job completed rc=0\n"
ONE_JOB_FAILED = "cloud-init: bootstrap one-job failed rc=42\n"
ONE_JOB_TIMEOUT = "cloud-init: bootstrap one-job timeout rc=124\n"
ONE_JOB_UNSHIPPED = """\
cloud-init: bootstrap one-job cancelled rc=3
cloud-init: bootstrap one-job completed rc=1
cloud-init: bootstrap one-job timeout rc=9
cloud-init: bootstrap one-job failed rc=0
cloud-init: bootstrap one-job failed rc=124
"""

SPOT_NOTICE = """\
cloud-init: STEP job
cloud-init: bootstrap spot-interruption action=terminate host=ip-10-0-0-1 runner=0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b
cloud-init: bootstrap one-job failed rc=143
cloud-init: bootstrap exiting rc=143
"""


class ParseConsoleTests(unittest.TestCase):
    def test_clean_boot_yields_steps_and_clean_exit(self):
        facts = parse_console(CLEAN_BOOT)
        self.assertEqual(facts.steps, ("mount-options", "register", "job"))
        self.assertEqual(facts.exit_code, 0)
        self.assertEqual(facts.errors, ())
        self.assertTrue(facts.registered)
        self.assertIsNone(facts.job_process)

    def test_each_one_job_process_marker_keeps_its_outcome_and_status(self):
        for text, expected in (
            (ONE_JOB_COMPLETED, ("completed", 0)),
            (ONE_JOB_FAILED, ("failed", 42)),
            (ONE_JOB_TIMEOUT, ("timeout", 124)),
        ):
            with self.subTest(text=text.strip()):
                facts = parse_console(text)
                self.assertEqual(facts.job_process, expected)
                self.assertIsNone(facts.exit_code)
                self.assertEqual(facts.steps, ())

    def test_an_unshipped_one_job_outcome_is_ignored(self):
        # A closed vocabulary, not a pattern: a forged outcome or a status the
        # bootstrap cannot pair with it is not a marker.
        facts = parse_console(ONE_JOB_UNSHIPPED)
        self.assertIsNone(facts.job_process)
        self.assertIsNone(facts.exit_code)

    def test_the_process_marker_is_distinct_from_the_exit_marker(self):
        facts = parse_console(CLEAN_BOOT + ONE_JOB_FAILED)
        self.assertEqual(facts.exit_code, 0)
        self.assertEqual(facts.job_process, ("failed", 42))
        self.assertEqual(facts.steps.count("job"), 1)

    def test_a_spot_notice_keeps_its_action_worker_host_and_registration(self):
        facts = parse_console(SPOT_NOTICE)
        self.assertEqual(
            facts.spot_interruption, ("terminate", "ip-10-0-0-1", "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b")
        )
        # The notice is not a step: it says nothing about which job step ran.
        self.assertEqual(facts.steps, ("job",))

    def test_the_notice_action_host_and_runner_are_a_closed_vocabulary(self):
        runner = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
        for line, expected in (
            (
                f"bootstrap spot-interruption action=stop host=ip-10-0-0-1 runner={runner}",
                ("stop", "ip-10-0-0-1", runner),
            ),
            (
                f"bootstrap spot-interruption action=hibernate host=ip-10-0-0-1 runner={runner}",
                ("hibernate", "ip-10-0-0-1", runner),
            ),
            (
                # The shipped fallbacks: a notice with no documented action, and
                # one that arrives before the runner registered.
                f"bootstrap spot-interruption action=unknown host=ip-10-0-0-1 runner={runner}",
                ("unknown", "ip-10-0-0-1", runner),
            ),
            (
                "bootstrap spot-interruption action=terminate host=ip-10-0-0-1 runner=unknown",
                ("terminate", "ip-10-0-0-1", None),
            ),
        ):
            with self.subTest(line=line):
                self.assertEqual(parse_console(line).spot_interruption, expected)

    def test_an_unshipped_notice_value_is_not_a_marker(self):
        # Every published value is validated rather than trusted: a forged
        # action, an uppercase action, an empty or traversing host and a
        # malformed registration id are not markers, and the line contributes
        # nothing.
        for line in (
            "bootstrap spot-interruption action=evacuate host=ip-10-0-0-1 runner=unknown",
            "bootstrap spot-interruption action=TERMINATE host=ip-10-0-0-1 runner=unknown",
            "bootstrap spot-interruption action=terminate host= runner=unknown",
            "bootstrap spot-interruption action=terminate host=../../etc/passwd runner=unknown",
            "bootstrap spot-interruption action=terminate host=ip-10-0-0-1 runner=not-a-uuid",
            "bootstrap spot-interruption action=terminate host=ip-10-0-0-1",
            "bootstrap spot-interruption action=terminate",
        ):
            with self.subTest(line=line):
                facts = parse_console(line)
                self.assertIsNone(facts.spot_interruption)
                self.assertEqual(facts.steps, ())

    def test_duplicated_serial_stream_is_deduplicated(self):
        facts = parse_console(CLEAN_BOOT + CLEAN_BOOT)
        self.assertEqual(facts.steps.count("register"), 1)
        self.assertEqual(facts.exit_code, 0)

    def test_ping_failure_names_the_kind_and_publishes_no_line(self):
        facts = parse_console(FAILED_PING)
        self.assertEqual(facts.errors, ("forgejo_ping",))
        self.assertEqual(facts.exit_code, 1)
        self.assertFalse(facts.registered)

    def test_ssm_timeout_is_classified(self):
        self.assertEqual(parse_console(SSM_TIMEOUT).errors, ("ssm_timeout",))

    def test_unrelated_text_is_ignored(self):
        facts = parse_console(NOISE)
        self.assertEqual(facts.steps, ())
        self.assertEqual(facts.errors, ())
        self.assertIsNone(facts.exit_code)

    def test_an_unshipped_step_name_is_not_a_marker(self):
        # A closed set, not a pattern: an image cannot publish an arbitrary name.
        facts = parse_console("cloud-init: STEP credential=synthetic-value\n")
        self.assertEqual(facts.steps, ())
        self.assertFalse(facts.registered)

    def test_a_claim_timeout_is_a_known_step_and_not_a_failure(self):
        facts = parse_console(IDLE_WORKER)
        self.assertEqual(facts.steps, ("mount-options", "register", "claim-timeout"))
        self.assertTrue(facts.registered)
        self.assertEqual(facts.exit_code, 0)
        # The runner's no-task line is not a failure kind, and no job ran.
        self.assertEqual(facts.errors, ())
        self.assertIsNone(facts.job_process)

    def test_a_lookalike_of_the_claim_timeout_step_is_not_a_marker(self):
        facts = parse_console(
            "cloud-init: STEP claim-timeouts\ncloud-init: STEP claim-timeout-x\n"
        )
        self.assertEqual(facts.steps, ())

    def test_error_kinds_are_bounded_and_deduplicated(self):
        text = "\n".join(
            [
                "level=error msg=\"Cannot ping the Forgejo instance server\"",
                "level=error msg=\"Connect timeout on endpoint URL\"",
                "level=error msg=\"Unable to locate package wireguard-tools\"",
                "level=error msg=\"Failed to register runner\"",
                "level=error msg=\"something else\"",
                "level=error msg=\"Cannot ping the Forgejo instance server\"",
            ]
        )
        facts = parse_console(text)
        self.assertEqual(len(facts.errors), 3)
        self.assertEqual(len(set(facts.errors)), 3)

    def test_empty_console_has_no_facts(self):
        facts = parse_console("")
        self.assertEqual(facts.steps, ())
        self.assertEqual(facts.errors, ())
        self.assertIsNone(facts.exit_code)
        self.assertFalse(facts.registered)


class ExportCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.events = self.root / "state" / "export" / "worker-events.ndjson"
        self.metrics = self.root / "textfile" / "ci-burst.prom"
        self.state_file = self.root / "state" / "export" / "observed.json"

    def build(self, instances, consoles, ledger=None, max_instances=None):
        extra = {"controller": {"max_instances": max_instances}} if max_instances is not None else {}
        config = load(
            write_config(
                self.root,
                config={"state": {"database": str(self.root / "state" / "state.sqlite3")}} | extra,
            )
        )
        aws = FakeAws(instances=instances)
        aws.consoles = consoles
        return Exporter(config, aws=aws, ledger=ledger, clock=lambda: NOW)

    def console_read_calls(self, exporter):
        return [call[1] for call in exporter.aws.calls if call[0] == "console_output"]

    def run_export(self, exporter, now=NOW):
        return exporter.run(
            events_file=self.events,
            metrics_file=self.metrics,
            state_file=self.state_file,
            now=now,
        )

    def events_read(self):
        return [json.loads(line) for line in self.events.read_text().splitlines() if line]


class ExporterTests(ExportCase):
    def test_first_run_emits_one_event_per_marker_with_a_stable_identity(self):
        exporter = self.build(
            [instance("i-0123456789abcdef0", zone="eu-central-1a"), instance("i-0123456789abcdef1", state="terminated", zone="eu-central-1b")],
            {"i-0123456789abcdef0": CLEAN_BOOT, "i-0123456789abcdef1": FAILED_PING},
        )
        result = self.run_export(exporter)
        self.assertEqual(result["decision"], "reported")
        events = self.events_read()
        identities = [event["id"] for event in events]
        self.assertIn("i-0123456789abcdef0:step:register", identities)
        self.assertIn("i-0123456789abcdef1:error:forgejo_ping", identities)
        self.assertEqual(len(identities), len(set(identities)))
        register = next(event for event in events if event.get("step") == "register")
        self.assertEqual(register["zone"], "eu-central-1a")
        self.assertEqual(result["events_emitted"], len(events))

    def test_an_instance_type_is_published_on_every_event_kind(self):
        typed = dataclasses.replace(
            instance("i-0123456789abcdef0", state="terminated", zone="eu-central-1a"),
            instance_type="m8g.xlarge",
        )
        failed = dataclasses.replace(
            instance("i-0123456789abcdef1", state="terminated", zone="eu-central-1b"),
            instance_type="m8g.xlarge",
        )
        exporter = self.build(
            [typed, failed],
            {"i-0123456789abcdef0": SPOT_NOTICE, "i-0123456789abcdef1": FAILED_PING},
        )
        self.run_export(exporter)
        events = self.events_read()
        self.assertEqual({event["event"] for event in events}, set(EVENT_SCHEMA))
        for event in events:
            with self.subTest(event=event["id"]):
                self.assertEqual(event["instance_type"], "m8g.xlarge")
        record = json.loads(self.state_file.read_text())["instances"]["i-0123456789abcdef0"]
        self.assertEqual(record["instance_type"], "m8g.xlarge")

    def test_a_type_reported_after_the_events_were_emitted_adds_no_event(self):
        untyped = instance("i-0123456789abcdef0", zone="eu-central-1a")
        consoles = {"i-0123456789abcdef0": CLEAN_BOOT}
        self.run_export(self.build([untyped], consoles))
        before = self.events_read()
        self.assertTrue(before)
        self.assertTrue(all("instance_type" not in event for event in before))
        emitted = json.loads(self.state_file.read_text())["instances"]["i-0123456789abcdef0"]["emitted"]
        self.assertTrue(emitted)
        typed = dataclasses.replace(untyped, instance_type="m8g.xlarge")
        result = self.run_export(self.build([typed], consoles))
        self.assertEqual(result["events_emitted"], 0)
        self.assertEqual([event["id"] for event in self.events_read()], [event["id"] for event in before])
        record = json.loads(self.state_file.read_text())["instances"]["i-0123456789abcdef0"]
        self.assertEqual(record["emitted"], emitted)
        self.assertEqual(record["instance_type"], "m8g.xlarge")

    def test_every_configurable_instance_type_is_published_and_retained(self):
        for value in ("m7i.metal-24xl", "u-6tb1.112xlarge", "m8g.xlarge"):
            with self.subTest(value=value):
                self.setUp()
                typed = dataclasses.replace(instance("i-0123456789abcdef0"), instance_type=value)
                self.run_export(self.build([typed], {"i-0123456789abcdef0": CLEAN_BOOT}))
                events = self.events_read()
                self.assertTrue(events)
                self.assertTrue(all(event["instance_type"] == value for event in events))
                record = json.loads(self.state_file.read_text())["instances"]["i-0123456789abcdef0"]
                self.assertEqual(record["instance_type"], value)
                # A rerun reads the retained lines back; none may be dropped.
                self.run_export(self.build([typed], {"i-0123456789abcdef0": CLEAN_BOOT}))
                self.assertEqual(self.events_read(), events)

    def test_an_over_long_instance_type_is_not_published(self):
        value = "m" + "a" * 60 + ".xlarge"
        typed = dataclasses.replace(instance("i-0123456789abcdef0"), instance_type=value)
        self.run_export(self.build([typed], {"i-0123456789abcdef0": CLEAN_BOOT}))
        self.assertTrue(all("instance_type" not in event for event in self.events_read()))

    def test_an_instance_without_a_type_publishes_no_instance_type_key(self):
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        self.run_export(exporter)
        events = self.events_read()
        self.assertTrue(events)
        for event in events:
            self.assertNotIn("instance_type", event)
        record = json.loads(self.state_file.read_text())["instances"]["i-0123456789abcdef0"]
        self.assertIsNone(record["instance_type"])

    def test_a_malformed_instance_type_is_not_published(self):
        for value in ("SYNTHETIC-CONSOLE-SECRET-123", "m8g", "m8g.xlarge\n", "M8g.xlarge"):
            with self.subTest(value=value):
                bad = dataclasses.replace(instance("i-0123456789abcdef0"), instance_type=value)
                self.run_export(self.build([bad], {"i-0123456789abcdef0": CLEAN_BOOT}))
                for event in self.events_read():
                    self.assertNotIn("instance_type", event)
                self.assertNotIn("SYNTHETIC-CONSOLE-SECRET-123", self.events.read_text())
                self.events.unlink()
                self.state_file.unlink()

    def test_retained_event_lines_with_and_without_an_instance_type(self):
        self.events.parent.mkdir(parents=True, exist_ok=True)
        common = {
            "ts": "2026-11-11T00:00:00Z",
            "deployment": "test-deployment",
            "zone": "eu-central-1a",
            "operation": None,
            "event": "step",
            "step": "register",
        }

        def line(suffix, **extra):
            instance_id = f"i-0123456789abcde{suffix}"
            return dict(common, instance=instance_id, id=f"{instance_id}:step:register", **extra)

        old = line("f1")
        typed = line("f2", instance_type="m8g.xlarge")
        poisoned = [
            line("f3", instance_type="SYNTHETIC-CONSOLE-SECRET-123"),
            line("f4", instance_type=None),
            line("f5", instance_type=["m8g.xlarge"]),
            line("f6", instance_type="m8g.xlarge", surprise="x"),
        ]
        self.events.write_text("".join(json.dumps(item) + "\n" for item in [old, typed, *poisoned]))
        result = self.run_export(self.build([], {}))
        self.assertIn("events_pruned", result["notes"])
        retained = {event["instance"]: event for event in self.events_read()}
        self.assertEqual(set(retained), {"i-0123456789abcdef1", "i-0123456789abcdef2"})
        self.assertNotIn("instance_type", retained["i-0123456789abcdef1"])
        self.assertEqual(retained["i-0123456789abcdef2"]["instance_type"], "m8g.xlarge")
        self.assertNotIn("SYNTHETIC-CONSOLE-SECRET-123", self.events.read_text())

    def test_a_persisted_record_without_an_instance_type_loads_unchanged(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "console_reads": 1,
                    "totals": {"launches": 1, "registered": 1, "unregistered": 0},
                    "instances": {
                        "i-0123456789abcdef0": {
                            "first_seen": NOW,
                            "seen": NOW,
                            "state": "running",
                            "zone": None,
                            "operation": None,
                            "steps": ["register"],
                            "errors": [],
                            "exit_code": None,
                            "job_process": None,
                            "spot_interruption": None,
                            "registered": True,
                            "emitted": ["step:register"],
                            "counted_registered": True,
                            "counted_unregistered": False,
                        }
                    },
                }
            )
        )
        state, note = load_state(self.state_file)
        self.assertIsNone(note)
        self.assertIsNone(state["instances"]["i-0123456789abcdef0"]["instance_type"])

    def test_a_persisted_malformed_instance_type_is_dropped(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "console_reads": 0,
                    "totals": {"launches": 1, "registered": 0, "unregistered": 1},
                    "instances": {
                        "i-0123456789abcdef0": {
                            "seen": NOW,
                            "instance_type": "SYNTHETIC-CONSOLE-SECRET-123",
                            "emitted": [],
                        }
                    },
                }
            )
        )
        state, _ = load_state(self.state_file)
        self.assertIsNone(state["instances"]["i-0123456789abcdef0"]["instance_type"])
        self.assertNotIn("SYNTHETIC-CONSOLE-SECRET-123", json.dumps(state))

    def test_second_run_emits_nothing_new(self):
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        self.run_export(exporter)
        first = len(self.events_read())
        result = self.run_export(exporter)
        self.assertEqual(result["events_emitted"], 0)
        self.assertEqual(len(self.events_read()), first)

    def test_totals_distinguish_registered_from_unregistered(self):
        exporter = self.build(
            [instance("i-0123456789abcdef0"), instance("i-0123456789abcdef1", state="terminated")],
            {"i-0123456789abcdef0": CLEAN_BOOT, "i-0123456789abcdef1": FAILED_PING},
        )
        result = self.run_export(exporter)
        self.assertEqual(result["totals"], {"launches": 2, "registered": 1, "unregistered": 1})

    def test_a_later_empty_console_does_not_unlearn_registration(self):
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        self.run_export(exporter)
        ended = self.build([instance("i-0123456789abcdef0", state="terminated")], {"i-0123456789abcdef0": ""})
        result = self.run_export(ended)
        self.assertEqual(result["totals"]["registered"], 1)
        self.assertEqual(result["totals"]["unregistered"], 0)

    def test_counters_do_not_fall_when_records_are_pruned(self):
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        self.run_export(exporter)
        later = self.build([], {})
        result = self.run_export(later, now=NOW + 8 * 86400)
        self.assertEqual(result["totals"]["launches"], 1)
        self.assertEqual(result["totals"]["registered"], 1)
        self.assertIn("ci_burst_worker_registered_total", self.metrics.read_text())

    def test_metrics_textfile_names_the_deployment_and_is_complete(self):
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        self.run_export(exporter)
        text = self.metrics.read_text()
        self.assertIn("# TYPE ci_burst_worker_launches_total counter", text)
        self.assertIn("# TYPE ci_burst_worker_registered_total counter", text)
        self.assertIn('deployment="test-deployment"', text)
        self.assertIn("ci_burst_export_last_run_timestamp_seconds", text)
        self.assertFalse(list(self.metrics.parent.glob(".ci-burst-*")))

    def test_ledger_series_appear_when_the_ledger_exists(self):
        from ci_burst.ledger import Ledger

        ledger = Ledger(self.root / "state" / "state.sqlite3", clock=lambda: NOW)
        ledger.connect()
        ledger.close()
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertEqual(result["spend_micros"], 0)
        self.assertEqual(result["committed_micros"], 0)
        self.assertIn("ci_burst_spend_micros", self.metrics.read_text())
        self.assertNotIn("ledger_omitted", result["notes"])

    def test_fleet_gauges_come_from_the_ledger_snapshot(self):
        from ci_burst.ledger import Ledger

        path = self.root / "state" / "state.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        ledger = Ledger(path, clock=lambda: NOW)
        ledger.connect()
        ledger.record_fleet_observation(
            observed_at=NOW,
            contract="forgejo-17.0.1-runners",
            running_attempts=2,
            active_registrations=3,
            offline_registrations=1,
        )
        ledger.close()
        exporter = self.build(
            [instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT}
        )
        result = self.run_export(exporter)
        self.assertEqual(result["fleet"]["active_registrations"], 3)
        self.assertEqual(result["fleet"]["running_attempts"], 2)
        self.assertNotIn("fleet_omitted", result["notes"])
        text = self.metrics.read_text()
        self.assertIn("ci_burst_fleet_observation_timestamp_seconds", text)
        self.assertIn('ci_burst_fleet_active_registrations{deployment="test-deployment"} 3', text)
        self.assertIn('ci_burst_fleet_running_attempts{deployment="test-deployment"} 2', text)
        self.assertIn('ci_burst_fleet_idle_registrations{deployment="test-deployment"} 1', text)

    def test_a_stale_fleet_snapshot_publishes_only_the_timestamp(self):
        from ci_burst.ledger import Ledger

        path = self.root / "state" / "state.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        ledger = Ledger(path, clock=lambda: NOW)
        ledger.connect()
        ledger.record_fleet_observation(
            observed_at=NOW - 901,
            contract="forgejo-17.0.1-runners",
            running_attempts=1,
            active_registrations=1,
            offline_registrations=0,
        )
        ledger.close()
        exporter = self.build(
            [instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT}
        )
        result = self.run_export(exporter)
        self.assertIn("fleet_stale", result["notes"])
        text = self.metrics.read_text()
        self.assertIn("ci_burst_fleet_observation_timestamp_seconds", text)
        self.assertNotIn("ci_burst_fleet_active_registrations", text)
        self.assertNotIn("ci_burst_fleet_running_attempts", text)
        self.assertNotIn("ci_burst_fleet_idle_registrations", text)

    def test_a_snapshot_exactly_fifteen_minutes_old_is_still_current(self):
        from ci_burst.ledger import Ledger

        path = self.root / "state" / "state.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        ledger = Ledger(path, clock=lambda: NOW)
        ledger.connect()
        ledger.record_fleet_observation(
            observed_at=NOW - 900,
            contract="forgejo-17.0.1-runners",
            running_attempts=0,
            active_registrations=1,
            offline_registrations=0,
        )
        ledger.close()
        exporter = self.build(
            [instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT}
        )
        result = self.run_export(exporter)
        self.assertNotIn("fleet_stale", result["notes"])
        self.assertIn("ci_burst_fleet_active_registrations", self.metrics.read_text())

    def test_an_unreadable_ledger_reports_both_omissions(self):
        path = self.root / "state" / "state.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a sqlite database")
        exporter = self.build(
            [instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT}
        )
        result = self.run_export(exporter)
        self.assertIn("ledger_omitted", result["notes"])
        self.assertIn("fleet_omitted", result["notes"])
        self.assertIn("ci_burst_worker_launches_total", self.metrics.read_text())

    def test_ledger_commitment_is_reported(self):
        from ci_burst.ledger import Ledger, current_window

        path = self.root / "state" / "state.sqlite3"
        path.parent.mkdir(parents=True, exist_ok=True)
        ledger = Ledger(path, clock=lambda: NOW)
        ledger.acquire_writer()
        ledger.connect()
        with ledger.transaction():
            ledger.new_operation(
                operation_id="op-1",
                client_token="ci-burst-op-1",
                request={"seed": True},
                request_sha256="0" * 64,
                expires_at=NOW + 3600,
                reservation_micros=540_000,
                window_start=current_window(NOW, 86400),
                region="eu-central-1",
                account="111122223333",
                price_micros=20_000,
                admitted_lifetime_seconds=3600,
            )
        ledger.set_state("op-1", "running", instance_id="i-0123456789abcdef0")
        ledger.close()
        exporter = self.build(
            [instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT}
        )
        result = self.run_export(exporter, now=NOW + 600)
        # 600 seconds at the accepted 0.02 price; the commitment adds the
        # remaining life at the configured cap plus the buffer.
        self.assertEqual(result["spend_micros"], 3_334)
        self.assertGreater(result["committed_micros"], result["spend_micros"])
        self.assertEqual(result["allowance_micros"], 10_000_000)
        self.assertIn("ci_burst_committed_micros", self.metrics.read_text())

    def test_absent_ledger_is_reported_not_fatal(self):
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertIsNone(result["spend_micros"])
        self.assertIsNone(result["committed_micros"])
        self.assertIn("ledger_omitted", result["notes"])
        self.assertIn("fleet_omitted", result["notes"])
        text = self.metrics.read_text()
        self.assertNotIn("ci_burst_spend_micros", text)
        self.assertIn("ci_burst_worker_launches_total", text)

    def test_turnover_beyond_the_read_budget_is_deferred_not_refused(self):
        owned = [
            instance(f"i-0123456789abcde{index:02x}", state="terminated")
            for index in range(MAX_CONSOLE_READS + 8)
        ]
        exporter = self.build(owned, {})
        result = self.run_export(exporter)
        self.assertEqual(result["decision"], "reported")
        self.assertIn("console_read_deferred", result["notes"])
        self.assertEqual(result["console_reads"], MAX_CONSOLE_READS)
        self.assertTrue(self.metrics.exists())
        self.assertEqual(len(self.console_read_calls(exporter)[0]), MAX_CONSOLE_READS)

    def test_a_deferred_record_is_read_on_the_next_run(self):
        owned = [
            instance(f"i-0123456789abcde{index:02x}", state="terminated")
            for index in range(MAX_CONSOLE_READS + 2)
        ]
        consoles = {item.instance_id: CLEAN_BOOT for item in owned}
        exporter = self.build(owned, consoles)
        self.assertEqual(self.run_export(exporter)["console_reads"], MAX_CONSOLE_READS)
        second = self.run_export(exporter)
        self.assertEqual(second["console_reads"], 2)
        self.assertNotIn("console_read_deferred", second["notes"])
        self.assertEqual(second["totals"]["launches"], MAX_CONSOLE_READS + 2)

    def test_a_record_that_left_the_live_states_is_not_read_again(self):
        exporter = self.build(
            [instance("i-0123456789abcdef0", state="terminated")],
            {"i-0123456789abcdef0": CLEAN_BOOT},
        )
        self.assertEqual(self.run_export(exporter)["console_reads"], 1)
        again = self.build(
            [instance("i-0123456789abcdef0", state="terminated")],
            {"i-0123456789abcdef0": CLEAN_BOOT},
        )
        result = self.run_export(again)
        self.assertEqual(result["console_reads"], 0)
        self.assertEqual(self.console_read_calls(again), [])
        self.assertEqual(result["events_emitted"], 0)

    def test_a_terminating_worker_is_read_until_a_final_state_is_observed(self):
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": NOISE})
        self.run_export(exporter)
        # Terminating is not finished: the console and the lifecycle carry on.
        for _ in range(2):
            ending = self.build(
                [instance("i-0123456789abcdef0", state="shutting-down")],
                {"i-0123456789abcdef0": CLEAN_BOOT},
            )
            self.assertEqual(self.run_export(ending)["console_reads"], 1)
        final = self.build(
            [instance("i-0123456789abcdef0", state="terminated")],
            {"i-0123456789abcdef0": CLEAN_BOOT},
        )
        result = self.run_export(final)
        self.assertEqual(result["console_reads"], 1)
        self.assertEqual(result["totals"]["registered"], 1)
        settled = self.build(
            [instance("i-0123456789abcdef0", state="terminated")],
            {"i-0123456789abcdef0": CLEAN_BOOT},
        )
        self.assertEqual(self.run_export(settled)["console_reads"], 0)

    def test_an_exit_marker_while_running_does_not_finish_the_record(self):
        running = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": FAILED_PING})
        self.assertEqual(self.run_export(running)["console_reads"], 1)
        still_running = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": FAILED_PING})
        self.assertEqual(self.run_export(still_running)["console_reads"], 1)
        final = self.build(
            [instance("i-0123456789abcdef0", state="terminated")],
            {"i-0123456789abcdef0": FAILED_PING},
        )
        self.assertEqual(self.run_export(final)["console_reads"], 1)

    def test_a_claim_timeout_marker_does_not_finish_the_record(self):
        running = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": IDLE_WORKER})
        self.assertEqual(self.run_export(running)["console_reads"], 1)
        # The marker is what the worker printed, not the end of the instance: the
        # provider still reports it live, so the record stays a candidate.
        still_running = self.build(
            [instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": IDLE_WORKER}
        )
        self.assertEqual(self.run_export(still_running)["console_reads"], 1)
        final = self.build(
            [instance("i-0123456789abcdef0", state="terminated")],
            {"i-0123456789abcdef0": IDLE_WORKER},
        )
        self.assertEqual(self.run_export(final)["console_reads"], 1)
        settled = self.build(
            [instance("i-0123456789abcdef0", state="terminated")],
            {"i-0123456789abcdef0": IDLE_WORKER},
        )
        self.assertEqual(self.run_export(settled)["console_reads"], 0)

    def test_a_claim_timeout_is_one_step_event_with_no_console_text(self):
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": IDLE_WORKER})
        self.run_export(exporter)
        events = self.events_read()
        timeouts = [event for event in events if event.get("step") == "claim-timeout"]
        self.assertEqual(len(timeouts), 1)
        self.assertEqual(timeouts[0]["id"], "i-0123456789abcdef0:step:claim-timeout")
        self.assertEqual(timeouts[0]["event"], "step")
        self.assertEqual(
            set(timeouts[0]),
            {"id", "ts", "deployment", "instance", "zone", "operation", "event", "step"},
        )
        # The worker exited with status 0, and nothing in the stream reads as a
        # failure or as a job result.
        self.assertEqual([event["rc"] for event in events if event["event"] == "exit"], [0])
        self.assertEqual([event for event in events if event["event"] in ("error", "job-process")], [])
        for text in ("no task received", "declared successfully", "burst-ip"):
            self.assertNotIn(text, self.events.read_text())
        self.assertEqual(self.run_export(exporter)["events_emitted"], 0)

    def test_a_worker_that_registered_and_timed_out_is_registered_once(self):
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": IDLE_WORKER})
        expected = {"launches": 1, "registered": 1, "unregistered": 0}
        self.assertEqual(self.run_export(exporter)["totals"], expected)
        self.assertEqual(self.run_export(exporter)["totals"], expected)
        ended = self.build(
            [instance("i-0123456789abcdef0", state="terminated")],
            {"i-0123456789abcdef0": IDLE_WORKER},
        )
        self.assertEqual(self.run_export(ended)["totals"], expected)

    def test_a_persisted_record_holding_claim_timeout_survives_sanitising(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "console_reads": 1,
                    "totals": {"launches": 1, "registered": 1, "unregistered": 0},
                    "instances": {
                        "i-0123456789abcdef0": {
                            "first_seen": NOW,
                            "seen": NOW,
                            "state": "running",
                            "zone": None,
                            "operation": None,
                            "steps": ["register", "claim-timeout"],
                            "errors": [],
                            "exit_code": 0,
                            "job_process": None,
                            "spot_interruption": None,
                            "registered": True,
                            "emitted": ["step:register", "step:claim-timeout", "exit:0"],
                            "counted_registered": True,
                            "counted_unregistered": False,
                        }
                    },
                }
            )
        )
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": NOISE})
        result = self.run_export(exporter)
        self.assertNotIn("state_rebuilt", result["notes"])
        record = json.loads(self.state_file.read_text())["instances"]["i-0123456789abcdef0"]
        self.assertEqual(record["steps"], ["register", "claim-timeout"])
        self.assertIn("step:claim-timeout", record["emitted"])
        # The marker was already published, so nothing is published twice.
        self.assertEqual(result["events_emitted"], 0)

    def test_a_persisted_lookalike_of_claim_timeout_is_dropped(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "console_reads": 1,
                    "totals": {"launches": 1, "registered": 1, "unregistered": 0},
                    "instances": {
                        "i-0123456789abcdef0": {
                            "seen": NOW,
                            "steps": ["register", "claim-timeouts"],
                            "errors": [],
                            "exit_code": None,
                            "registered": True,
                            "emitted": ["step:register", "step:claim-timeouts"],
                        }
                    },
                }
            )
        )
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": NOISE})
        result = self.run_export(exporter)
        self.assertIn("state_rebuilt", result["notes"])
        self.assertNotIn("claim-timeouts", self.state_file.read_text())
        if self.events.exists():
            self.assertNotIn("claim-timeouts", self.events.read_text())

    def test_a_retained_claim_timeout_line_validates_and_a_lookalike_is_dropped(self):
        self.events.parent.mkdir(parents=True, exist_ok=True)
        common = {
            "ts": "2026-11-11T00:00:00Z",
            "deployment": "test-deployment",
            "instance": "i-0123456789abcdef1",
            "zone": None,
            "operation": None,
            "event": "step",
        }
        valid = dict(common, id="i-0123456789abcdef1:step:claim-timeout", step="claim-timeout")
        lookalike = dict(common, id="i-0123456789abcdef1:step:claim-timeouts", step="claim-timeouts")
        self.events.write_text(
            json.dumps(valid, sort_keys=True) + "\n" + json.dumps(lookalike, sort_keys=True) + "\n"
        )
        result = self.run_export(self.build([], {}))
        self.assertIn("events_pruned", result["notes"])
        body = self.events.read_text()
        self.assertIn("i-0123456789abcdef1:step:claim-timeout\"", body)
        self.assertNotIn("claim-timeouts", body)

    def test_a_shutting_down_worker_counts_toward_the_configured_limit(self):
        owned = [
            instance("i-0123456789abcdef0"),
            instance("i-0123456789abcdef1", state="shutting-down"),
        ]
        exporter = self.build(owned, {}, max_instances=1)
        result = self.run_export(exporter)
        self.assertEqual(result["decision"], "reported")
        self.assertIn("above_configured_limit", result["notes"])
        self.assertEqual(result["console_reads"], 2)

    def test_a_mixed_inventory_reads_every_live_worker_before_history(self):
        live = [instance(f"i-0123456789abc0{index:02x}") for index in range(12)]
        history = [instance(f"i-0123456789abc1{index:02x}", state="terminated") for index in range(16)]
        exporter = self.build(live + history, {}, max_instances=12)
        result = self.run_export(exporter)
        self.assertEqual(result["console_reads"], MAX_CONSOLE_READS)
        read = set(self.console_read_calls(exporter)[0])
        self.assertEqual(read & {item.instance_id for item in live}, {item.instance_id for item in live})
        self.assertIn("console_read_deferred", result["notes"])

    def test_structurally_corrupt_records_are_rebuilt_and_reported(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "console_reads": 3,
                    "totals": {"launches": 1, "registered": 0, "unregistered": 0},
                    "instances": {"i-0123456789abcdef0": {"emitted": None, "steps": None, "seen": NOW}},
                }
            )
        )
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertIn("state_rebuilt", result["notes"])
        self.assertTrue(self.events.exists())
        # Three steps and one exit, re-emitted because the record was repaired.
        self.assertEqual(result["events_emitted"], 4)

    def test_undecodable_state_file_is_rebuilt(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_bytes(b"\xff\xfe\x00broken")
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertIn("state_rebuilt", result["notes"])

    def test_events_file_is_rotated_at_its_bound(self):
        # Seed with VALID historical events: an invalid line would be pruned by
        # the schema check, which would make this test pass without rotating.
        self.events.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {
                "id": "i-0123456789abcdef1:step:register",
                "ts": "2026-11-11T00:00:00Z",
                "deployment": "test-deployment",
                "instance": "i-0123456789abcdef1",
                "zone": "eu-central-1a",
                "operation": None,
                "event": "step",
                "step": "register",
            },
            sort_keys=True,
        )
        count = MAX_EVENTS_BYTES // len(line) + 200
        self.events.write_text((line + "\n") * count)
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        self.run_export(exporter)
        self.assertLessEqual(self.events.stat().st_size, MAX_EVENTS_BYTES)
        body = self.events.read_text()
        self.assertIn('"instance": "i-0123456789abcdef0"', body)
        self.assertLess(body.count(json.dumps(json.loads(line), sort_keys=True)), count)

    def test_a_lost_record_repeats_events_with_the_same_identity(self):
        # At-least-once: a crash between publishing an event and persisting the
        # record repeats the event, and its identity is what lets a consumer
        # drop the repeat.
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        self.run_export(exporter)
        first = {event["id"] for event in self.events_read()}
        self.state_file.unlink()
        self.run_export(exporter)
        events = self.events_read()
        self.assertGreater(len(events), len(first))
        self.assertEqual({event["id"] for event in events}, first)

    def test_prune_drops_records_past_retention_and_keeps_totals(self):
        state = {
            "version": 2,
            "console_reads": 0,
            "totals": {"launches": 2, "registered": 1, "unregistered": 0},
            "instances": {
                "i-0123456789abcdefa": {"seen": NOW - 8 * 86400, "emitted": []},
                "i-0123456789abcdefb": {"seen": NOW - 60, "emitted": []},
            },
        }
        pruned = prune_state(state, NOW)
        self.assertNotIn("i-0123456789abcdefa", pruned["instances"])
        self.assertIn("i-0123456789abcdefb", pruned["instances"])
        self.assertEqual(pruned["totals"]["launches"], 2)


    def test_persisted_state_is_validated_against_the_closed_vocabulary(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "console_reads": 1,
                    "totals": {"launches": True, "registered": 0, "unregistered": 0},
                    "instances": {
                        "i-0123456789abcdef0": {
                            "seen": NOW,
                            "emitted": ["step:secret-step", "error:arbitrary"],
                            "steps": ["secret-step"],
                            "errors": ["arbitrary"],
                            "exit_code": "boom",
                            "registered": False,
                        }
                    },
                }
            )
        )
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertIn("state_rebuilt", result["notes"])
        published = self.events.read_text()
        self.assertNotIn("secret-step", published)
        self.assertNotIn("arbitrary", published)
        metrics = self.metrics.read_text()
        self.assertIn('ci_burst_worker_launches_total{deployment="test-deployment"}', metrics)
        self.assertNotIn("True", metrics)

    def test_error_kind_limit_holds_across_runs(self):
        first = "\n".join(
            [
                "level=error msg=\"Cannot ping the Forgejo instance server\"",
                "level=error msg=\"Connect timeout on endpoint URL\"",
                "level=error msg=\"Unable to locate package wireguard-tools\"",
            ]
        )
        second = "\n".join(
            ["level=error msg=\"Failed to register runner\"", "level=error msg=\"something else\""]
        )
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": first})
        self.run_export(exporter)
        later = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": second})
        self.run_export(later)
        error_events = [event for event in self.events_read() if event["event"] == "error"]
        self.assertEqual(len(error_events), 3)

    def test_a_poisoned_record_reaches_no_output(self):
        secret = "SYNTHETIC-CONSOLE-SECRET-123"
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "console_reads": True,
                    "totals": {
                        "launches": 0,
                        "registered": 0,
                        "unregistered": 0,
                        "detail": secret,
                    },
                    "instances": {
                        "i-0123456789abcdef0": {
                            "seen": NOW,
                            "emitted": ["step:register", "step:secret-step", f"error:{secret}"],
                            "steps": ["register", "secret-step"],
                            "errors": ["arbitrary"],
                            "exit_code": "boom",
                            "registered": True,
                            "detail": secret,
                        }
                    },
                }
            )
        )
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertEqual(set(result["totals"]), {"launches", "registered", "unregistered"})
        for surface, text in (
            ("events", self.events.read_text()),
            ("metrics", self.metrics.read_text()),
            ("result", json.dumps(result)),
            ("state", self.state_file.read_text()),
        ):
            with self.subTest(surface=surface):
                self.assertNotIn(secret, text)
                self.assertNotIn("secret-step", text)
                self.assertNotIn("arbitrary", text)

    def test_no_console_text_reaches_any_output(self):
        secret = "SYNTHETIC-CONSOLE-SECRET-123"
        console = f"level=error token={secret} msg=\"Cannot ping the Forgejo instance server\"\nSTEP {secret}\n"
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": console})
        result = self.run_export(exporter)
        for surface, text in (
            ("events", self.events.read_text()),
            ("metrics", self.metrics.read_text()),
            ("result", json.dumps(result)),
            ("state", self.state_file.read_text()),
        ):
            with self.subTest(surface=surface):
                self.assertNotIn(secret, text)

    def test_persisted_metadata_and_foreign_keys_reach_no_output(self):
        secret = "SYNTHETIC-CONSOLE-SECRET-123"
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "console_reads": 0,
                    "totals": {"launches": 1, "registered": 0, "unregistered": 0},
                    "instances": {
                        secret: {"seen": NOW, "state": secret, "zone": secret, "operation": secret},
                        "i-0123456789abcdef0": {
                            "seen": NOW,
                            "state": secret,
                            "zone": secret,
                            "operation": secret,
                            "emitted": [],
                        },
                    },
                }
            )
        )
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertIn("state_rebuilt", result["notes"])
        for surface, body in (
            ("events", self.events.read_text()),
            ("metrics", self.metrics.read_text()),
            ("result", json.dumps(result)),
            ("state", self.state_file.read_text()),
        ):
            with self.subTest(surface=surface):
                self.assertNotIn(secret, body)

    def test_malformed_exit_identities_are_dropped_not_fatal(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "console_reads": 0,
                    "totals": {"launches": 1, "registered": 0, "unregistered": 0},
                    "instances": {
                        "i-0123456789abcdef0": {
                            "seen": NOW,
                            "emitted": ["exit:\u00b2", "exit:9999", "exit:000", "exit:0"],
                            "steps": [],
                            "errors": [],
                            "state": "running",
                            "exit_code": None,
                            "registered": False,
                        }
                    },
                }
            )
        )
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertIn("state_rebuilt", result["notes"])
        persisted = json.loads(self.state_file.read_text())["instances"]["i-0123456789abcdef0"]
        # Only the canonical identity the parser would generate survives.
        self.assertIn("exit:0", persisted["emitted"])
        self.assertNotIn("exit:000", persisted["emitted"])
        for event in self.events_read():
            self.assertNotIn(":exit:000", event["id"])

    def test_a_one_job_process_status_is_published_without_a_job_verdict(self):
        console = "STEP job\nbootstrap one-job failed rc=42\nbootstrap exiting rc=42\n"
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": console})
        result = self.run_export(exporter)
        process = [event for event in self.events_read() if event["event"] == "job-process"]
        self.assertEqual(len(process), 1)
        event = process[0]
        self.assertEqual(event["id"], "i-0123456789abcdef0:job-process:failed:42")
        # The shape is exact: the marker names the worker process outcome and
        # its status, and carries no console text, job verdict or success field.
        self.assertEqual(
            set(event),
            {"id", "ts", "deployment", "instance", "zone", "operation", "event", "outcome", "rc"},
        )
        self.assertEqual(event["outcome"], "failed")
        self.assertEqual(event["rc"], 42)
        for forbidden in ("success", "verdict", "job_result", "conclusion"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, json.dumps(event))
                self.assertNotIn(forbidden, self.metrics.read_text())
                self.assertNotIn(forbidden, json.dumps(result))
        self.assertNotIn("job_process", self.metrics.read_text())
        # The marker is deduplicated by its stable identity like any other.
        self.assertEqual(self.run_export(exporter)["events_emitted"], 0)

    def test_a_spot_interruption_publishes_the_worker_identity_and_no_job_verdict(self):
        exporter = self.build(
            [instance("i-0123456789abcdef0", state="terminated")],
            {"i-0123456789abcdef0": SPOT_NOTICE},
        )
        result = self.run_export(exporter)
        notices = [event for event in self.events_read() if event["event"] == "spot-interruption"]
        self.assertEqual(len(notices), 1)
        event = notices[0]
        self.assertEqual(
            event["id"],
            "i-0123456789abcdef0:spot-interruption:terminate:ip-10-0-0-1:0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
        )
        self.assertEqual(
            set(event),
            {
                "id",
                "ts",
                "deployment",
                "instance",
                "zone",
                "operation",
                "event",
                "reason",
                "action",
                "host",
                "runner",
            },
        )
        # reason is the stable value a consumer filters on; event names the
        # stream kind, and the notice's own action stays separate.
        self.assertEqual(event["reason"], "spot_interruption")
        self.assertEqual(event["action"], "terminate")
        self.assertEqual(event["host"], "ip-10-0-0-1")
        self.assertEqual(event["runner"], "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b")
        self.assertEqual(event["instance"], "i-0123456789abcdef0")
        # The marker records the observed notice and the signal attempt; it is
        # never a job verdict or proof that the job ended.
        for forbidden in ("success", "verdict", "job_result", "conclusion"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, json.dumps(event))
                self.assertNotIn(forbidden, self.metrics.read_text())
                self.assertNotIn(forbidden, json.dumps(result))
        self.assertEqual(self.run_export(exporter)["events_emitted"], 0)

    def test_a_retained_spot_line_validates_and_a_forged_one_is_dropped(self):
        self.events.parent.mkdir(parents=True, exist_ok=True)
        common = {
            "ts": "2026-11-11T00:00:00Z",
            "deployment": "test-deployment",
            "instance": "i-0123456789abcdef1",
            "zone": None,
            "operation": None,
            "event": "spot-interruption",
            "reason": "spot_interruption",
            "host": "ip-10-0-0-1",
            "runner": "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
        }
        valid = dict(
            common,
            id="i-0123456789abcdef1:spot-interruption:terminate:ip-10-0-0-1:0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
            action="terminate",
        )
        forged_action = dict(
            valid,
            id="i-0123456789abcdef1:spot-interruption:evacuate:ip-10-0-0-1:0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
            action="evacuate",
        )
        # A line whose reason or host disagrees with the contract is not this
        # command's line, and neither is one whose identity disagrees with its
        # own values.
        wrong_reason = dict(valid, reason="spot-interruption")
        mismatched_host = dict(
            valid,
            id="i-0123456789abcdef1:spot-interruption:terminate:ip-10-0-0-2:0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
        )
        forged_runner = dict(valid, runner="not-a-uuid")
        self.events.write_text(
            "".join(
                json.dumps(line, sort_keys=True) + "\n"
                for line in (valid, forged_action, wrong_reason, mismatched_host, forged_runner)
            )
        )
        result = self.run_export(self.build([], {}))
        self.assertIn("events_pruned", result["notes"])
        body = self.events.read_text()
        self.assertIn(
            ":spot-interruption:terminate:ip-10-0-0-1:0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b",
            body,
        )
        self.assertNotIn("evacuate", body)
        self.assertNotIn("ip-10-0-0-2", body)
        self.assertNotIn('"reason": "spot-interruption"', body)

    def test_a_retained_spot_line_carries_null_or_a_uuid_only(self):
        # The console spelling "unknown" is translated at the parsing boundary.
        # A retained line that carries it, or an empty string, is not one this
        # command could have written and must not be republished.
        self.events.parent.mkdir(parents=True, exist_ok=True)
        common = {
            "ts": "2026-11-11T00:00:00Z",
            "deployment": "test-deployment",
            "instance": "i-0123456789abcdef1",
            "zone": None,
            "operation": None,
            "event": "spot-interruption",
            "reason": "spot_interruption",
            "action": "terminate",
            "host": "ip-10-0-0-1",
            "id": "i-0123456789abcdef1:spot-interruption:terminate:ip-10-0-0-1:unknown",
        }
        valid_null = dict(common, runner=None)
        literal_unknown = dict(common, runner="unknown")
        empty_runner = dict(common, runner="")
        self.events.write_text(
            "".join(
                json.dumps(line, sort_keys=True) + "\n"
                for line in (valid_null, literal_unknown, empty_runner)
            )
        )
        result = self.run_export(self.build([], {}))
        self.assertIn("events_pruned", result["notes"])
        body = self.events.read_text()
        self.assertIn(":spot-interruption:terminate:ip-10-0-0-1:unknown", body)
        self.assertNotIn('"runner": "unknown"', body)
        self.assertNotIn('"runner": ""', body)

    def test_a_persisted_runner_value_is_normalised_to_the_published_schema(self):
        # "" and the console spelling "unknown" are not registration ids this
        # command publishes; a rewrite drops them rather than carrying them on.
        for stored in ("", "unknown"):
            with self.subTest(stored=stored):
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.state_file.write_text(
                    json.dumps(
                        {
                            "version": 2,
                            "console_reads": 1,
                            "totals": {"launches": 1, "registered": 1, "unregistered": 0},
                            "instances": {
                                "i-0123456789abcdef0": {
                                    "seen": NOW,
                                    "emitted": [
                                        "spot-interruption:terminate:ip-10-0-0-1:unknown"
                                    ],
                                    "steps": [],
                                    "errors": [],
                                    "exit_code": None,
                                    "job_process": None,
                                    "spot_interruption": ["terminate", "ip-10-0-0-1", stored],
                                    "registered": True,
                                }
                            },
                        }
                    )
                )
                exporter = self.build(
                    [instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": NOISE}
                )
                result = self.run_export(exporter)
                self.assertIn("state_rebuilt", result["notes"])
                record = json.loads(self.state_file.read_text())["instances"][
                    "i-0123456789abcdef0"
                ]
                self.assertEqual(
                    record["spot_interruption"], ["terminate", "ip-10-0-0-1", None]
                )

    def test_a_persisted_spot_record_is_validated_against_the_closed_vocabulary(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "console_reads": 1,
                    "totals": {"launches": 1, "registered": 1, "unregistered": 0},
                    "instances": {
                        "i-0123456789abcdef0": {
                            "seen": NOW,
                            "emitted": ["spot-interruption:evacuate:ip-10-0-0-1"],
                            "steps": [],
                            "errors": [],
                            "exit_code": None,
                            "job_process": None,
                            "spot_interruption": ["evacuate", "../../etc/passwd", "not-a-uuid"],
                            "registered": True,
                        }
                    },
                }
            )
        )
        exporter = self.build(
            [instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": NOISE}
        )
        result = self.run_export(exporter)
        self.assertIn("state_rebuilt", result["notes"])
        self.assertNotIn("passwd", self.state_file.read_text())
        self.assertNotIn("evacuate", self.state_file.read_text())
        if self.events.exists():
            self.assertNotIn("evacuate", self.events.read_text())

    def test_a_pre_change_exit_event_line_still_validates(self):
        self.events.parent.mkdir(parents=True, exist_ok=True)
        historical = {
            "id": "i-0123456789abcdef1:exit:1",
            "ts": "2026-11-11T00:00:00Z",
            "deployment": "test-deployment",
            "instance": "i-0123456789abcdef1",
            "zone": None,
            "operation": None,
            "event": "exit",
            "rc": 1,
        }
        self.events.write_text(json.dumps(historical, sort_keys=True) + "\n")
        result = self.run_export(self.build([], {}))
        self.assertNotIn("events_pruned", result["notes"])
        self.assertIn("i-0123456789abcdef1:exit:1", self.events.read_text())

    def test_a_retained_job_process_line_validates_and_a_forged_one_is_dropped(self):
        self.events.parent.mkdir(parents=True, exist_ok=True)
        common = {
            "ts": "2026-11-11T00:00:00Z",
            "deployment": "test-deployment",
            "instance": "i-0123456789abcdef1",
            "zone": None,
            "operation": None,
            "event": "job-process",
        }
        valid = dict(common, id="i-0123456789abcdef1:job-process:timeout:124", outcome="timeout", rc=124)
        forged = dict(common, id="i-0123456789abcdef1:job-process:passed:1", outcome="passed", rc=1)
        self.events.write_text(json.dumps(valid, sort_keys=True) + "\n" + json.dumps(forged, sort_keys=True) + "\n")
        result = self.run_export(self.build([], {}))
        self.assertIn("events_pruned", result["notes"])
        body = self.events.read_text()
        self.assertIn("i-0123456789abcdef1:job-process:timeout:124", body)
        self.assertNotIn(":job-process:passed:", body)

    def test_a_foreign_operation_tag_is_not_published(self):
        secret = "SYNTHETIC-CONSOLE-SECRET-123"
        poisoned = instance("i-0123456789abcdef0", **{"ci-burst:operation": secret})
        exporter = self.build([poisoned], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        for surface, body in (
            ("events", self.events.read_text()),
            ("result", json.dumps(result)),
            ("state", self.state_file.read_text()),
        ):
            with self.subTest(surface=surface):
                self.assertNotIn(secret, body)

    def test_a_foreign_ledger_state_is_bucketed_not_published(self):
        import sqlite3

        from ci_burst.ledger import Ledger

        secret = "SYNTHETIC-CONSOLE-SECRET-123"
        database = self.root / "state" / "state.sqlite3"
        ledger = Ledger(database, clock=lambda: NOW)
        ledger.connect()
        ledger.new_operation(
            operation_id="a" * 32,
            client_token="token-1",
            request={"MinCount": 1},
            request_sha256="0" * 64,
            expires_at=NOW + 3600,
            reservation_micros=1000,
            window_start=0.0,
            region="eu-central-1",
            account="111122223333",
            admitted_lifetime_seconds=3600,
        )
        ledger.close()
        connection = sqlite3.connect(database)
        connection.execute("UPDATE operations SET state = ?", (secret,))
        connection.commit()
        connection.close()
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertIn("ledger_state_unknown", result["notes"])
        self.assertEqual(result["ledger_operations"], {"invalid": 1})
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn(secret, self.metrics.read_text())

    def test_a_poisoned_historical_event_line_is_dropped(self):
        self.events.parent.mkdir(parents=True, exist_ok=True)
        valid = {
            "id": "i-0123456789abcdef1:step:register",
            "ts": "2026-11-11T00:00:00Z",
            "deployment": "test-deployment",
            "instance": "i-0123456789abcdef1",
            "zone": "eu-central-1a",
            "operation": None,
            "event": "step",
            "step": "register",
        }
        self.events.write_text(
            json.dumps(valid) + "\n" + '{"id":"i-0123456789abcdef0:step:register","detail":"SYNTHETIC-CONSOLE-SECRET-123"}\n'
        )
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertIn("events_pruned", result["notes"])
        body = self.events.read_text()
        self.assertNotIn("SYNTHETIC-CONSOLE-SECRET-123", body)
        self.assertIn("i-0123456789abcdef1:step:register", body)


    def test_historical_event_values_are_validated_per_field(self):
        secret = "SYNTHETIC-CONSOLE-SECRET-123"
        self.events.parent.mkdir(parents=True, exist_ok=True)
        base = {
            "id": "i-0123456789abcdef1:step:register",
            "ts": "2026-11-11T00:00:00Z",
            "deployment": "test-deployment",
            "instance": "i-0123456789abcdef1",
            "zone": "eu-central-1a",
            "operation": None,
            "event": "step",
            "step": "register",
        }
        poisoned = [
            dict(base, operation=secret),
            dict(base, deployment=secret),
            dict(base, zone=secret),
            dict(base, ts=secret),
            dict(base, kind=secret),
            dict(base, event="exit", rc=0),
        ]
        self.events.write_text("".join(json.dumps(item) + "\n" for item in poisoned))
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertIn("events_pruned", result["notes"])
        body = self.events.read_text()
        self.assertNotIn(secret, body)
        retained = [json.loads(line) for line in body.splitlines() if line]
        # None of the poisoned history survives, including the kind-mismatched one.
        self.assertEqual([event for event in retained if event["instance"] == "i-0123456789abcdef1"], [])

    def test_malformed_nested_values_degrade_instead_of_failing(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "console_reads": 0,
                    "totals": {"launches": 1, "registered": 0, "unregistered": 0},
                    "instances": {
                        "i-0123456789abcdef0": {"seen": NOW, "state": [], "zone": [], "operation": [], "emitted": []}
                    },
                }
            )
        )
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.events.write_text('{"event":[]}\n{"instance":42}\n')
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertIn("state_rebuilt", result["notes"])
        self.assertIn("events_pruned", result["notes"])

    def test_a_non_finite_timestamp_is_dropped_for_an_absent_instance(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            '{"version": 2, "console_reads": 0, "totals": {"launches": 1, "registered": 0, "unregistered": 0},'
            ' "instances": {"i-0123456789abcdef9": {"seen": 1e309, "emitted": []}}}'
        )
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertIn("state_rebuilt", result["notes"])
        state = json.loads(self.state_file.read_text())
        self.assertNotIn("i-0123456789abcdef9", state["instances"])
        self.assertNotIn("Infinity", self.state_file.read_text())

    def test_an_unknown_provider_state_is_normalized(self):
        exporter = self.build([instance("i-0123456789abcdef0", state="synthetic-state")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertEqual(result["worker_states"], {"unknown": 1})
        self.assertIn('state="unknown"', self.metrics.read_text())


    def test_a_retained_event_missing_a_required_field_is_dropped(self):
        self.events.parent.mkdir(parents=True, exist_ok=True)
        complete = {
            "id": "i-0123456789abcdef1:step:register",
            "ts": "2026-11-11T00:00:00Z",
            "deployment": "test-deployment",
            "instance": "i-0123456789abcdef1",
            "zone": "eu-central-1a",
            "operation": None,
            "event": "step",
            "step": "register",
        }
        # The complete line survives first, so a removal is the only variable.
        self.events.write_text(json.dumps(complete) + "\n")
        result = self.run_export(self.build([], {}))
        self.assertNotIn("events_pruned", result["notes"])
        self.assertIn("i-0123456789abcdef1", self.events.read_text())
        for missing in ("id", "ts", "deployment", "instance", "zone", "operation", "event", "step"):
            with self.subTest(missing=missing):
                line = dict(complete)
                line.pop(missing, None)
                self.events.write_text(json.dumps(line) + "\n")
                result = self.run_export(self.build([], {}))
                self.assertIn("events_pruned", result["notes"])
                self.assertNotIn("i-0123456789abcdef1", self.events.read_text())

    def test_an_oversized_persisted_counter_is_reset(self):
        huge = "9" * 4300
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            '{"version": 2, "console_reads": ' + huge + ","
            ' "totals": {"launches": ' + huge + ', "registered": 0, "unregistered": 0},'
            ' "instances": {}}'
        )
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertIn("state_rebuilt", result["notes"])
        self.assertEqual(result["totals"]["launches"], 1)
        self.assertTrue(self.metrics.exists())
        self.assertLess(len(self.state_file.read_text()), 4096)

    def test_an_oversized_integer_timestamp_is_dropped(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            '{"version": 2, "console_reads": 0, "totals": {"launches": 1, "registered": 0, "unregistered": 0},'
            ' "instances": {"i-0123456789abcdef9": {"seen": ' + str(10**400) + ', "emitted": []}}}'
        )
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertIn("state_rebuilt", result["notes"])
        self.assertNotIn("i-0123456789abcdef9", json.loads(self.state_file.read_text())["instances"])

    def test_deeply_nested_json_degrades_instead_of_raising(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text("[" * 2000 + "0" + "]" * 2000)
        self.events.parent.mkdir(parents=True, exist_ok=True)
        self.events.write_text("[" * 2000 + "0" + "]" * 2000 + "\n")
        exporter = self.build([instance("i-0123456789abcdef0")], {"i-0123456789abcdef0": CLEAN_BOOT})
        result = self.run_export(exporter)
        self.assertIn("state_rebuilt", result["notes"])
        self.assertIn("events_pruned", result["notes"])


    def test_a_trailing_newline_in_a_persisted_key_is_rejected(self):
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "console_reads": 0,
                    "totals": {"launches": 1, "registered": 0, "unregistered": 0},
                    "instances": {
                        "i-0123456789abcdef0\n": {"seen": NOW, "state": "running", "emitted": []},
                    },
                }
            )
        )
        exporter = self.build([], {})
        result = self.run_export(exporter)
        self.assertIn("state_rebuilt", result["notes"])
        # Compare against the parsed state: JSON would escape a newline even if
        # the key survived, so a substring check on the file proves nothing.
        self.assertEqual(json.loads(self.state_file.read_text())["instances"], {})

    def test_one_unusable_counter_keeps_the_other_two(self):
        huge = "9" * 4300
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            '{"version": 2, "console_reads": 0, "totals": {"launches": 7, "registered": 5, "unregistered": ' + huge + '},'
            ' "instances": {}}'
        )
        exporter = self.build([], {})
        result = self.run_export(exporter)
        self.assertIn("state_rebuilt", result["notes"])
        self.assertEqual(result["totals"], {"launches": 7, "registered": 5, "unregistered": 0})

    def test_counters_saturate_at_their_bound(self):
        from ci_burst.export import MAX_COUNTER

        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "console_reads": MAX_COUNTER,
                    "totals": {"launches": MAX_COUNTER, "registered": MAX_COUNTER, "unregistered": MAX_COUNTER},
                    "instances": {},
                }
            )
        )
        exporter = self.build(
            [instance("i-0123456789abcdef0"), instance("i-0123456789abcdef1", state="terminated")],
            {"i-0123456789abcdef0": CLEAN_BOOT, "i-0123456789abcdef1": FAILED_PING},
        )
        result = self.run_export(exporter)
        # Every increment path is exercised: launch, registration and the
        # ended-unregistered counter.
        self.assertEqual(
            result["totals"],
            {"launches": MAX_COUNTER, "registered": MAX_COUNTER, "unregistered": MAX_COUNTER},
        )
        state = json.loads(self.state_file.read_text())
        self.assertEqual(state["console_reads"], MAX_COUNTER)

    def test_missing_state_starts_empty_without_a_note(self):
        state, note = load_state(self.state_file)
        self.assertIsNone(note)
        self.assertEqual(state["instances"], {})
        self.assertEqual(state["totals"], {"launches": 0, "registered": 0, "unregistered": 0})


class ObservationRefusesOnlyWhenOwnedInstancesExceedTheLimitTests(ExportCase):
    def test_two_live_workers_above_a_lowered_limit_are_reported_with_evidence(self):
        owned = [instance("i-0123456789abcdef0"), instance("i-0123456789abcdef1")]
        exporter = self.build(
            owned, {item.instance_id: CLEAN_BOOT for item in owned}, max_instances=1
        )
        result = self.run_export(exporter)
        self.assertEqual(result["decision"], "reported")
        self.assertEqual(result["console_reads"], 2)
        self.assertIn("above_configured_limit", result["notes"])
        self.assertNotIn("console_read_deferred", result["notes"])
        self.assertEqual(len(self.console_read_calls(exporter)[0]), 2)
        self.assertEqual(result["totals"]["registered"], 2)
        self.assertTrue(self.events.exists())
        self.assertTrue(self.metrics.exists())
        self.assertIn("ci_burst_export_last_run_timestamp_seconds", self.metrics.read_text())

    def test_seventeen_live_workers_exceed_the_read_budget_and_refuse(self):
        owned = [
            instance(f"i-0123456789abcde{index:02x}")
            for index in range(MAX_CONSOLE_READS + 1)
        ]
        exporter = self.build(owned, {}, max_instances=1)
        with self.assertRaises(ProviderRefusal) as raised:
            self.run_export(exporter)
        self.assertEqual(raised.exception.reason, "console_read_cap_exceeded")
        self.assertIn("read budget", raised.exception.detail)
        self.assertEqual(self.console_read_calls(exporter), [])
        self.assertFalse(self.events.exists())
        self.assertFalse(self.metrics.exists())

    def test_twelve_live_workers_at_their_limit_are_unchanged(self):
        owned = [instance(f"i-0123456789abc0{index:02x}") for index in range(12)]
        exporter = self.build(owned, {}, max_instances=12)
        result = self.run_export(exporter)
        self.assertEqual(result["decision"], "reported")
        self.assertEqual(result["console_reads"], 12)
        self.assertNotIn("above_configured_limit", result["notes"])
        self.assertNotIn("console_read_deferred", result["notes"])

    def test_deferred_terminal_history_still_reports_the_deferral(self):
        live = [instance(f"i-0123456789abc0{index:02x}") for index in range(2)]
        history = [
            instance(f"i-0123456789abc1{index:02x}", state="terminated")
            for index in range(MAX_CONSOLE_READS + 4)
        ]
        exporter = self.build(live + history, {}, max_instances=2)
        result = self.run_export(exporter)
        self.assertEqual(result["decision"], "reported")
        self.assertEqual(result["console_reads"], MAX_CONSOLE_READS)
        self.assertIn("console_read_deferred", result["notes"])
        self.assertNotIn("above_configured_limit", result["notes"])
        read = set(self.console_read_calls(exporter)[0])
        self.assertEqual(read & {item.instance_id for item in live}, {item.instance_id for item in live})


def object_text(members):
    """Return the JSON text of an object from ``(name, value)`` pairs, where a name may occur twice.

    ``json.dumps`` writes a dictionary, which holds a key once, so it cannot write
    a repeated member. That is also why ``export`` cannot.
    """
    return "{" + ", ".join(f"{json.dumps(name)}: {json.dumps(value)}" for name, value in members) + "}"


class LaunchEvidenceContractTests(ExportCase):
    """The record that ``export`` writes is the one the launch evidence reader reads.

    These tests run the real exporter. A change to the record version, to a
    field the reader takes or to the write cadence fails here.
    """

    FUTILE_ID = "i-0123456789abcdea0"
    REGISTERED_ID = "i-0123456789abcdea1"
    KILLED_ID = "i-0123456789abcdea2"
    EMPTY_ID = "i-0123456789abcdea3"
    UNLABELLED_ID = "i-0123456789abcdea4"

    #: The three finished workers of the loader tests. The record says that none
    #: of them registered, and the provider lists all three as terminated. The
    #: console of the first shows registration and the other two never register.
    WORKER_IDS = ("i-0123456789abcdea0", "i-0123456789abcdea1", "i-0123456789abcdea2")

    def owned(self, instance_id, index, state="terminated"):
        return instance(instance_id, state=state, operation=operation_hex(index), zone="eu-central-1a")

    def records(self):
        return json.loads(self.state_file.read_text())["instances"]

    def test_the_default_record_path_is_the_one_beside_the_ledger(self):
        exporter = self.build([], {})
        self.assertEqual(default_record_path(exporter.config.state.database), self.state_file)

    def test_the_post_registration_steps_are_shipped_steps_after_the_register_step(self):
        self.assertTrue(POST_REGISTRATION_STEPS)
        self.assertLessEqual(POST_REGISTRATION_STEPS, STEP_NAMES)
        self.assertNotIn("register", POST_REGISTRATION_STEPS)

    def test_the_reader_classifies_what_the_exporter_writes(self):
        never_registered = (
            "cloud-init: STEP mount-options\n"
            "cloud-init: level=error msg=\"failed to register runner\"\n"
            "cloud-init: bootstrap exiting rc=1\n"
        )
        killed = "cloud-init: STEP mount-options\ncloud-init: STEP register\n"
        owned = [
            self.owned(self.FUTILE_ID, 1),
            self.owned(self.REGISTERED_ID, 2),
            self.owned(self.KILLED_ID, 3),
            self.owned(self.EMPTY_ID, 4),
            self.owned(self.UNLABELLED_ID, 5, state="rebooting"),
        ]
        consoles = {
            self.FUTILE_ID: never_registered,
            self.REGISTERED_ID: CLEAN_BOOT,
            self.KILLED_ID: killed,
            self.EMPTY_ID: "",
            self.UNLABELLED_ID: CLEAN_BOOT,
        }
        # Wall time, so that the exporter's own write is the heartbeat the
        # reader judges, with no modification time set by the test.
        self.run_export(self.build(owned, consoles), now=time.time())
        records = self.records()
        self.assertEqual(records[self.UNLABELLED_ID]["state"], "unknown")
        verdicts = {key: classify(record) for key, record in records.items()}
        self.assertEqual(
            verdicts,
            {
                self.FUTILE_ID: FUTILE,
                self.REGISTERED_ID: REGISTERED,
                self.KILLED_ID: REGISTERED,
                self.EMPTY_ID: None,
                self.UNLABELLED_ID: None,
            },
        )
        evidence = read_launch_evidence(self.state_file, time.time(), ledger_holding(self.state_file))
        self.assertEqual(evidence.as_dict(), {"state": "fresh", "considered": 3, "futile": 1})

        # Age the file, then run again with the unlabelled instance gone from
        # the provider. The run rewrites the record, so it is fresh again, and
        # the unlabelled state is now null.
        old = time.time() - 3600
        os.utime(self.state_file, (old, old))
        self.run_export(self.build(owned[:4], consoles), now=time.time())
        self.assertGreater(os.stat(self.state_file).st_mtime, old)
        records = self.records()
        self.assertIsNone(records[self.UNLABELLED_ID]["state"])
        self.assertIsNone(classify(records[self.UNLABELLED_ID]))
        evidence = read_launch_evidence(self.state_file, time.time(), ledger_holding(self.state_file))
        self.assertEqual(evidence.as_dict(), {"state": "fresh", "considered": 3, "futile": 1})

    def test_three_workers_that_never_registered_hold(self):
        console = "cloud-init: STEP mount-options\ncloud-init: bootstrap exiting rc=1\n"
        owned = [self.owned(f"i-0123456789abcdeb{index}", index) for index in range(3)]
        self.run_export(self.build(owned, {item.instance_id: console for item in owned}), now=time.time())
        evidence = read_launch_evidence(self.state_file, time.time(), ledger_holding(self.state_file))
        self.assertEqual(evidence.as_dict(), {"state": "fresh", "considered": 3, "futile": 3})
        self.assertTrue(evidence.holds)

    def test_the_reader_accepts_every_marker_the_exporter_writes(self):
        # A reader that is stricter than the writer would switch the gate off
        # without any test that refuses a record noticing it.
        runner = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
        notice = "cloud-init: bootstrap spot-interruption action={} host=ip-10-0-0-1 runner={}\n"
        consoles = {
            "i-0123456789abcdec0": notice.format("terminate", runner),
            "i-0123456789abcdec1": notice.format("stop", "unknown"),
            "i-0123456789abcdec2": ONE_JOB_COMPLETED,
            "i-0123456789abcdec3": ONE_JOB_FAILED,
            "i-0123456789abcdec4": ONE_JOB_TIMEOUT,
        }
        owned = [self.owned(key, index) for index, key in enumerate(consoles, start=1)]
        self.run_export(self.build(owned, consoles), now=time.time())
        records = self.records()
        self.assertEqual(
            [records[key]["spot_interruption"] for key in list(consoles)[:2]],
            [["terminate", "ip-10-0-0-1", runner], ["stop", "ip-10-0-0-1", None]],
        )
        self.assertEqual(
            [records[key]["job_process"] for key in list(consoles)[2:]],
            [["completed", 0], ["failed", 42], ["timeout", 124]],
        )
        evidence = read_launch_evidence(self.state_file, time.time(), ledger_holding(self.state_file))
        # The workers share one read time, so all five are considered.
        self.assertEqual(evidence.as_dict(), {"state": "fresh", "considered": 5, "futile": 0})

    def test_the_reader_accepts_exactly_what_the_loader_keeps(self):
        # The exporter's own loader drops a record that it did not write. The
        # reader must refuse the same keys and the same marker values, so both
        # sides are asked about one file.
        runner = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
        valid = "i-0123456789abcdef0"
        cases = (
            (valid, "job_process", None, True),
            (valid, "job_process", ["completed", 0], True),
            (valid, "job_process", ["failed", 42], True),
            (valid, "job_process", ["timeout", 137], True),
            (valid, "job_process", [], False),
            (valid, "job_process", ["completed", 1], False),
            (valid, "job_process", [["completed"], 0], False),
            (valid, "spot_interruption", None, True),
            (valid, "spot_interruption", ["terminate", "ip-10-0-0-1", runner], True),
            (valid, "spot_interruption", ["terminate", "ip-10-0-0-1", None], True),
            (valid, "spot_interruption", ["terminate", "ip-10-0-0-1", "unknown"], False),
            (valid, "spot_interruption", ["terminate", "ip-10-0-0-1", ""], False),
            (valid, "spot_interruption", [], False),
            ("i-01234567", "job_process", None, True),
            ("i-0123456789abcdef01", "job_process", None, False),
            ("not-an-instance", "job_process", None, False),
        )
        for key, field, value, accepted in cases:
            with self.subTest(key=key, field=field, value=value):
                write_observed(
                    self.state_file,
                    mtime=NOW,
                    payload={
                        "version": STATE_VERSION,
                        "console_reads": 1,
                        "totals": {"launches": 1, "registered": 0, "unregistered": 1},
                        "instances": {key: futile_record(NOW - 60, **{field: value})},
                    },
                )
                _, note = load_state(self.state_file)
                self.assertEqual(note is None, accepted)
                evidence = read_launch_evidence(self.state_file, NOW, ledger_holding(self.state_file))
                self.assertEqual(evidence.state == "fresh", accepted)

    def test_a_value_that_the_loader_drops_never_makes_the_reader_hold_alone(self):
        # ``state``, ``operation`` and ``steps`` are read leniently. A value of
        # the right type that ``export`` drops is no evidence to the reader, as
        # it is to the loader, and a value of the wrong type drops the file. So
        # the reader holds only where the record that ``export`` rewrites holds.
        older = {
            f"i-0123456789abcde{index:02x}": futile_record(NOW - 600 + 100 * index, index=index)
            for index in range(3)
        }
        bases = (
            lambda: futile_record(NOW - 50, index=9),
            lambda: registered_record(NOW - 50, index=9),
            lambda: registered_record(
                NOW - 50, index=9, job_process=None, registered=False, steps=["mount-options", "register"]
            ),
        )
        overrides = (
            ("steps", ["bogus"]),
            ("steps", ["Register"]),
            ("steps", ["register", "bogus"]),
            ("steps", ["mount-options", 5]),
            ("steps", "register"),
            ("state", "Terminated"),
            ("state", "running"),
            ("state", ""),
            ("state", ["terminated"]),
            ("operation", "x"),
            ("operation", operation_hex(9).upper()),
            ("operation", None),
            ("operation", 5),
        )
        for position, make in enumerate(bases):
            for field, value in overrides:
                with self.subTest(base=position, field=field, value=value):
                    record = make()
                    record[field] = value
                    write_observed(
                        self.state_file,
                        mtime=NOW,
                        payload={
                            "version": STATE_VERSION,
                            "console_reads": 4,
                            "totals": {"launches": 4, "registered": 0, "unregistered": 0},
                            "instances": {**older, "i-0123456789abcdef9": record},
                        },
                    )
                    raw = read_launch_evidence(self.state_file, NOW, ledger_holding(self.state_file))
                    kept, _ = load_state(self.state_file)
                    write_observed(self.state_file, mtime=NOW, payload=kept)
                    rewritten = read_launch_evidence(self.state_file, NOW, ledger_holding(self.state_file))
                    if raw.state == "fresh":
                        self.assertEqual(raw.as_dict(), rewritten.as_dict())
                    else:
                        self.assertEqual(raw.as_dict(), {"state": "malformed", "considered": 0, "futile": 0})

    def finished_workers(self, now, *, repeated):
        """Write the record of three finished workers and return its text and an exporter.

        The record says that none of them registered, and the exporter lists the
        same three as terminated. The console of the first shows registration, so a
        run that reads it again finds a worker that registered. When ``repeated``
        is true that worker is filed under its key twice, registered first and
        futile last, and ``json`` keeps the last.
        """
        first, second, third = self.WORKER_IDS
        members = [
            (first, futile_record(now - 300, index=1)),
            (second, futile_record(now - 240, index=2)),
            (third, futile_record(now - 180, index=3)),
        ]
        if repeated:
            members.insert(0, (first, registered_record(now - 300, index=1)))
        totals = json.dumps({"launches": 3, "registered": 1, "unregistered": 2})
        raw = (
            '{"version": %d, "console_reads": 3, "totals": %s, "instances": %s}'
            % (STATE_VERSION, totals, object_text(members))
        ).encode("utf-8")
        write_observed(self.state_file, raw=raw, mtime=now)
        never_registered = "cloud-init: STEP mount-options\ncloud-init: bootstrap exiting rc=1\n"
        owned = [self.owned(instance_id, index) for index, instance_id in enumerate(self.WORKER_IDS, start=1)]
        consoles = {first: CLEAN_BOOT, second: never_registered, third: never_registered}
        return raw, self.build(owned, consoles)

    def test_the_same_records_without_a_repeated_key_are_kept_and_hold(self):
        # The control of the next test: its file differs from this one by the
        # repeat only. The loader keeps finished records and does not read them
        # again, so the hold rests on the file.
        now = time.time()
        _, exporter = self.finished_workers(now, repeated=False)
        self.assertTrue(read_launch_evidence(self.state_file, now, ledger_holding(self.state_file)).holds)
        result = self.run_export(exporter, now=now)
        self.assertNotIn("state_rebuilt", result["notes"])
        self.assertEqual(result["console_reads"], 0)
        evidence = read_launch_evidence(self.state_file, time.time(), ledger_holding(self.state_file))
        self.assertEqual(evidence.as_dict(), {"state": "fresh", "considered": 3, "futile": 3})
        self.assertTrue(evidence.holds)

    def test_a_repeated_instance_key_cannot_turn_a_registered_worker_into_a_hold(self):
        # ``json`` keeps the last value of a repeated member. A loader that did so
        # would keep the futile record, skip the finished worker whose console
        # shows registration, and rewrite the file with a fresh heartbeat, so the
        # next pass would hold on evidence that nobody read. The loader refuses
        # the repeat as the reader does, so it cannot load the file, and the run
        # reads the three finished workers again.
        now = time.time()
        raw, exporter = self.finished_workers(now, repeated=True)
        first = self.WORKER_IDS[0]
        self.assertEqual(raw.count(f'"{first}":'.encode("ascii")), 2)
        self.assertFalse(json.loads(raw)["instances"][first]["registered"], "the last value is the futile one")
        self.assertEqual(read_launch_evidence(self.state_file, now, ledger_holding(self.state_file)).state, "malformed")

        result = self.run_export(exporter, now=now)

        # What the next pass sees: the worker registered, so nothing holds.
        evidence = read_launch_evidence(self.state_file, time.time(), ledger_holding(self.state_file))
        self.assertEqual(evidence.as_dict(), {"state": "fresh", "considered": 3, "futile": 2})
        self.assertFalse(evidence.holds)
        # Why: the file was not loaded, so every worker was read again.
        self.assertIn("state_rebuilt", result["notes"])
        self.assertEqual([set(call) for call in self.console_read_calls(exporter)], [set(self.WORKER_IDS)])
        self.assertEqual(result["console_reads"], 3)
        self.assertTrue(self.records()[first]["registered"])

    def test_a_nonstandard_json_constant_is_not_kept_and_rewritten_clean(self):
        # ``json`` accepts ``NaN``, ``Infinity`` and ``-Infinity``, but they are not
        # JSON and ``export`` never writes them. Each one sits in a field that the
        # loader drops, so a loader that accepted the word would keep the three
        # finished futile records and rewrite a clean file with a fresh heartbeat,
        # and the consoles, which show registration, would never be read.
        now = time.time()
        owned = [self.owned(instance_id, index) for index, instance_id in enumerate(self.WORKER_IDS, start=1)]
        consoles = {instance_id: CLEAN_BOOT for instance_id in self.WORKER_IDS}
        for word, constant in (("NaN", float("nan")), ("Infinity", float("inf")), ("-Infinity", float("-inf"))):
            for place in ("a field of a record", "a field of the top level"):
                with self.subTest(constant=word, place=place):
                    instances = {
                        instance_id: futile_record(now - 300 + 60 * index, index=index)
                        for index, instance_id in enumerate(self.WORKER_IDS, start=1)
                    }
                    payload = {
                        "version": STATE_VERSION,
                        "console_reads": 3,
                        "totals": {"launches": 3, "registered": 0, "unregistered": 3},
                        "instances": instances,
                    }
                    if place == "a field of the top level":
                        payload["console_reads"] = constant
                    else:
                        instances[self.WORKER_IDS[0]]["first_seen"] = constant
                    write_observed(self.state_file, mtime=now, payload=payload)
                    self.assertIn(word.encode("ascii"), self.state_file.read_bytes())
                    evidence = read_launch_evidence(self.state_file, now, ledger_holding(self.state_file))
                    self.assertEqual(evidence.state, "malformed")

                    result = self.run_export(self.build(owned, consoles), now=now)

                    evidence = read_launch_evidence(self.state_file, time.time(), ledger_holding(self.state_file))
                    self.assertEqual(evidence.as_dict(), {"state": "fresh", "considered": 3, "futile": 0})
                    self.assertFalse(evidence.holds)
                    self.assertIn("state_rebuilt", result["notes"])
                    self.assertEqual(result["console_reads"], 3)

    def test_the_loader_drops_a_record_that_names_a_member_twice_whole(self):
        # The loader takes the branch it takes for a file that is not JSON: it
        # returns an empty record and ``state_rebuilt``, whatever level repeats
        # and whatever the two values are. The reader calls the same file
        # ``malformed``, so neither side reads what the other refuses. Each file
        # is otherwise one that the loader keeps without a note.
        first = self.WORKER_IDS[0]
        record = futile_record(NOW - 60, index=1)
        totals = json.dumps({"launches": 1, "registered": 0, "unregistered": 1})
        instance_twice = object_text([(first, registered_record(NOW - 60, index=1)), (first, record)])
        zone_twice = object_text([*record.items(), ("zone", "eu-central-1b")])
        control = '{"version": %d, "console_reads": 1, "totals": %s, "instances": {"%s": %s}}'
        cases = (
            (
                "a member at the top level",
                '{"version": %d, "version": %d, "console_reads": 1, "totals": %s, "instances": {}}'
                % (STATE_VERSION, STATE_VERSION, totals),
            ),
            (
                "an instance key",
                '{"version": %d, "console_reads": 1, "totals": %s, "instances": %s}'
                % (STATE_VERSION, totals, instance_twice),
            ),
            ("a member of a record", control % (STATE_VERSION, totals, first, zone_twice)),
        )
        clean = control % (STATE_VERSION, totals, first, json.dumps(record))
        write_observed(self.state_file, raw=clean.encode("utf-8"), mtime=NOW)
        _, note = load_state(self.state_file)
        self.assertIsNone(note, "without a repeat the loader keeps the file as it is")
        empty = {
            "version": STATE_VERSION,
            "console_reads": 0,
            "totals": {"launches": 0, "registered": 0, "unregistered": 0},
            "instances": {},
        }
        for name, text in cases:
            with self.subTest(name):
                write_observed(self.state_file, raw=text.encode("utf-8"), mtime=NOW)
                self.assertEqual(load_state(self.state_file), (empty, "state_rebuilt"))
                evidence = read_launch_evidence(self.state_file, NOW, ledger_holding(self.state_file))
                self.assertEqual(evidence.state, "malformed")

    # -- a stored value that bears on registration -------------------------------------
    #: Each value is a damaged form of registration evidence: the field is present
    #: and the loader cannot keep it. The first worker of each record would read as
    #: registered if the field were intact, and as futile once the loader replaced it.
    DAMAGED_EVIDENCE = (
        ("job_process", ["completed", 1]),
        ("job_process", ["completed", "0"]),
        ("job_process", ["unknown", 0]),
        ("job_process", [["completed"], 0]),
        ("job_process", []),
        ("job_process", "completed"),
        ("job_process", 0),
        ("spot_interruption", ["evacuate", "../../etc/passwd", "not-a-uuid"]),
        ("spot_interruption", ["terminate", "ip-10-0-0-1", 5]),
        ("spot_interruption", []),
        ("spot_interruption", "terminate"),
        ("steps", None),
        ("steps", "register"),
        ("steps", ["register", 5]),
        ("steps", ["register", None]),
        ("registered", None),
        ("registered", 1),
        ("registered", "true"),
    )

    def write_workers(self, records, *, ids=None):
        """Write a record of ended workers, filed under ``ids`` (``WORKER_IDS``) by position."""
        ids = self.WORKER_IDS if ids is None else ids
        write_observed(
            self.state_file,
            mtime=NOW,
            payload={
                "version": STATE_VERSION,
                "console_reads": len(records),
                "totals": {"launches": len(records), "registered": 0, "unregistered": len(records)},
                "instances": dict(zip(ids, records)),
            },
        )

    def test_a_record_whose_registration_evidence_cannot_be_kept_is_dropped(self):
        # The reader refuses such a file whole, so the gate is open. The loader must
        # not answer with a repaired record: an ended worker is never read again, so
        # the run would write the record back under a fresh heartbeat as a futile
        # launch that nobody observed. It drops the record instead.
        others = [futile_record(NOW - 120, index=2), futile_record(NOW - 180, index=3)]
        for field, value in self.DAMAGED_EVIDENCE:
            with self.subTest(field=field, value=value):
                self.write_workers([futile_record(NOW - 60, index=1, **{field: value}), *others])
                evidence = read_launch_evidence(self.state_file, NOW, ledger_holding(self.state_file))
                self.assertEqual(evidence.state, "malformed")

                state, note = load_state(self.state_file)

                self.assertEqual(note, "state_rebuilt")
                self.assertEqual(sorted(state["instances"]), sorted(self.WORKER_IDS[1:]))

    def test_a_value_that_the_loader_keeps_with_its_evidence_does_not_drop_the_record(self):
        # The loader still repairs a value that it can keep without losing what the
        # reader decides from. A Spot notice is normalised, and a step that is not in
        # the vocabulary is dropped, as before. A field that gates a verdict is
        # replaced by null, which never gives the record a futile verdict.
        cases = (
            ("spot_interruption", ["terminate", "ip-10-0-0-1", ""], REGISTERED),
            ("spot_interruption", ["terminate", "ip-10-0-0-1", "unknown"], REGISTERED),
            ("steps", ["register", "bogus"], REGISTERED),
            ("steps", ["bogus"], FUTILE),
            ("exit_code", 300, None),
            ("exit_code", "1", None),
            ("exit_code", 1.5, None),
            ("state", "Terminated", None),
            ("operation", "not-an-operation", None),
        )
        for field, value, verdict in cases:
            with self.subTest(field=field, value=value):
                self.write_workers([futile_record(NOW - 60, index=1, **{field: value})])

                state, note = load_state(self.state_file)

                self.assertEqual(note, "state_rebuilt")
                self.assertEqual(classify(state["instances"][self.WORKER_IDS[0]]), verdict)

    #: The fields that show whether a worker registered. The reader requires each of them.
    REGISTRATION_FIELDS = ("registered", "steps", "job_process", "spot_interruption")

    def test_a_record_of_an_ended_worker_that_lacks_a_registration_field_is_dropped(self):
        # A record that an older version wrote lacks a field that the version did not
        # write yet. The reader refuses such a file whole, so the gate is open. The
        # loader must not answer with a default for that field. An ended worker is
        # never read again, so the run would write the record back under a fresh
        # heartbeat as a futile launch that nobody observed. It drops the record.
        others = [futile_record(NOW - 120, index=2), futile_record(NOW - 180, index=3)]
        for field in self.REGISTRATION_FIELDS:
            with self.subTest(field=field):
                first = futile_record(NOW - 60, index=1)
                del first[field]
                self.write_workers([first, *others])
                evidence = read_launch_evidence(self.state_file, NOW, ledger_holding(self.state_file))
                self.assertEqual(evidence.state, "malformed")

                state, note = load_state(self.state_file)

                self.assertEqual(note, "state_rebuilt")
                self.assertEqual(sorted(state["instances"]), sorted(self.WORKER_IDS[1:]))

    def test_a_record_of_a_worker_that_has_not_ended_may_lack_a_registration_field(self):
        # The loader keeps the record. While the provider lists the worker, the run
        # reads its console again, and the console fills the field. Before that the
        # reader never counts the record, as it counts only a worker that ended.
        first = self.WORKER_IDS[0]
        runner = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"
        console = (
            "cloud-init: STEP mount-options\n"
            "cloud-init: STEP register\n"
            "cloud-init: STEP job\n"
            "cloud-init: bootstrap one-job completed rc=0\n"
            f"cloud-init: bootstrap spot-interruption action=terminate host=ip-10-0-0-1 runner={runner}\n"
        )
        shown = {
            "registered": True,
            "steps": ["mount-options", "register", "job"],
            "job_process": ["completed", 0],
            "spot_interruption": ["terminate", "ip-10-0-0-1", runner],
        }
        for field in self.REGISTRATION_FIELDS:
            with self.subTest(field=field):
                record = futile_record(NOW - 60, index=1, state="running", exit_code=None)
                del record[field]
                self.write_workers([record])

                state, _ = load_state(self.state_file)

                self.assertEqual(sorted(state["instances"]), sorted(self.WORKER_IDS[:1]))
                self.assertIsNone(classify(state["instances"][first]))
                result = self.run_export(self.build([self.owned(first, 1, state="running")], {first: console}))
                self.assertEqual(result["console_reads"], 1)
                self.assertEqual(self.records()[first][field], shown[field])

    def test_a_record_that_lacks_a_field_the_reader_requires_is_never_loaded_as_futile(self):
        # The fields come from the reader, so a field that it starts to require is
        # covered here. The reader refuses a record that lacks one of them, and no
        # default that the loader gives may turn the refused record into a futile
        # launch: the loader drops the record, or the reader finds no evidence in it.
        for field in RECORD_FIELDS:
            with self.subTest(field=field):
                record = futile_record(NOW - 60, index=1)
                del record[field]
                self.write_workers([record])
                evidence = read_launch_evidence(self.state_file, NOW, ledger_holding(self.state_file))
                self.assertEqual(evidence.state, "malformed")

                state, _ = load_state(self.state_file)

                kept = state["instances"].get(self.WORKER_IDS[0])
                self.assertNotEqual(None if kept is None else classify(kept), FUTILE)

    def test_a_dropped_record_that_the_run_does_not_read_is_no_evidence(self):
        # The run reads at most ``MAX_CONSOLE_READS`` consoles. A dropped record
        # whose console waits for a later run is absent from the file that this run
        # writes, so it is no evidence, and nothing that the run kept is damaged.
        count = MAX_CONSOLE_READS + 4
        ids = [f"i-0123456789abcf{index:03x}" for index in range(count)]
        self.write_workers(
            [futile_record(NOW - 60, index=index + 1, job_process=["completed", 1]) for index in range(count)],
            ids=ids,
        )
        owned = [self.owned(instance_id, index + 1) for index, instance_id in enumerate(ids)]

        result = self.run_export(self.build(owned, {instance_id: CLEAN_BOOT for instance_id in ids}))

        self.assertEqual(result["console_reads"], MAX_CONSOLE_READS)
        self.assertIn("console_read_deferred", result["notes"])
        self.assertIn("state_rebuilt", result["notes"])
        self.assertEqual(sorted(self.records()), sorted(ids[:MAX_CONSOLE_READS]))
        for record in self.records().values():
            self.assertEqual(classify(record), REGISTERED)


class RecordCadenceTests(ExportCase):
    """The reader takes the record's modification time as a heartbeat."""

    def owned(self):
        return [instance("i-0123456789abcdef0", state="terminated", operation=operation_hex(1))]

    def test_a_run_with_nothing_new_still_rewrites_the_record(self):
        consoles = {"i-0123456789abcdef0": CLEAN_BOOT}
        self.run_export(self.build(self.owned(), consoles))
        # The file system stamps wall time, which the fake clock is not.
        old = time.time() - 3600
        os.utime(self.state_file, (old, old))
        result = self.run_export(self.build(self.owned(), consoles), now=NOW + 60)
        self.assertEqual(result["events_emitted"], 0)
        self.assertEqual(result["console_reads"], 0)
        self.assertGreater(os.stat(self.state_file).st_mtime, old)

    def test_a_refused_run_leaves_the_record_alone(self):
        self.run_export(self.build(self.owned(), {"i-0123456789abcdef0": CLEAN_BOOT}))
        old = NOW - 3600
        os.utime(self.state_file, (old, old))
        before = self.state_file.read_bytes()
        live = [instance(f"i-0123456789abcd{index:02x}") for index in range(MAX_CONSOLE_READS + 1)]
        with self.assertRaises(ProviderRefusal) as raised:
            self.run_export(self.build(live, {}, max_instances=1), now=NOW + 60)
        self.assertEqual(raised.exception.reason, "console_read_cap_exceeded")
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertEqual(os.stat(self.state_file).st_mtime, old)

    def fail_the_textfile(self, consoles):
        """Write a record, age it, and return a run that cannot write its textfile.

        A directory where the textfile belongs cannot be replaced by a file. The
        second worker is new evidence, so a record that the run wrote would
        differ from the one on disk.
        """
        self.run_export(self.build(self.owned(), consoles))
        old = time.time() - 3600
        os.utime(self.state_file, (old, old))
        self.metrics.unlink()
        self.metrics.mkdir()
        consoles["i-0123456789abcdef1"] = CLEAN_BOOT
        owned = self.owned() + [
            instance("i-0123456789abcdef1", state="terminated", operation=operation_hex(2))
        ]
        return old, self.build(owned, consoles)

    def test_a_run_that_cannot_write_its_textfile_leaves_the_record_alone(self):
        # The record's modification time is the launch evidence heartbeat, and
        # a run that fails must not refresh it.
        old, exporter = self.fail_the_textfile({"i-0123456789abcdef0": CLEAN_BOOT})
        before = self.state_file.read_bytes()
        with self.assertRaises(OSError):
            self.run_export(exporter, now=NOW + 60)
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertEqual(os.stat(self.state_file).st_mtime, old)
        # The failed replacement leaves no temporary file behind.
        self.assertEqual(os.listdir(self.metrics.parent), [self.metrics.name])
        self.assertEqual(sorted(os.listdir(self.state_file.parent)), ["observed.json", "worker-events.ndjson"])

    def test_the_events_of_a_run_that_failed_repeat_under_the_same_identities(self):
        # The record is the last file a run writes, so a run that fails earlier
        # has already published its events. Delivery stays at least once: the
        # next run re-emits the markers it still finds, and the identity lets a
        # consumer drop the repeat.
        old, exporter = self.fail_the_textfile({"i-0123456789abcdef0": CLEAN_BOOT})
        with self.assertRaises(OSError):
            self.run_export(exporter, now=NOW + 60)
        published = [event["id"] for event in self.events_read()]
        self.assertTrue(any(key.startswith("i-0123456789abcdef1:") for key in published))
        self.metrics.rmdir()
        self.run_export(exporter, now=NOW + 120)
        repeated = [event["id"] for event in self.events_read()]
        self.assertEqual(set(repeated), set(published))
        self.assertGreater(len(repeated), len(published))
        self.assertGreater(os.stat(self.state_file).st_mtime, old)

    def test_a_deleted_record_is_rebuilt_with_the_rebuilding_run_time(self):
        consoles = {"i-0123456789abcdef0": CLEAN_BOOT}
        self.run_export(self.build(self.owned(), consoles))
        self.assertEqual(json.loads(self.state_file.read_text())["instances"]["i-0123456789abcdef0"]["seen"], NOW)
        self.state_file.unlink()
        self.run_export(self.build(self.owned(), consoles), now=NOW + 500)
        record = json.loads(self.state_file.read_text())["instances"]["i-0123456789abcdef0"]
        self.assertEqual(record["seen"], NOW + 500)
        self.assertEqual(record["first_seen"], NOW + 500)


class ConsoleReadTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.config = load(write_config(self.root))

    def test_passes_plaintext_through_and_bounds_it(self):
        class Client:
            def __init__(self):
                self.calls = []

            def get_console_output(self, **kwargs):
                self.calls.append(kwargs)
                return {"Output": "x" * (MAX_CONSOLE_BYTES + 100)}

        client = Client()
        consoles = Aws(self.config, client=client).console_output(["i-0123456789abcdef0"])
        self.assertEqual(len(consoles["i-0123456789abcdef0"]), MAX_CONSOLE_BYTES)
        self.assertTrue(consoles["i-0123456789abcdef0"].startswith("x"))
        self.assertEqual(client.calls[0]["InstanceId"], "i-0123456789abcdef0")
        self.assertTrue(client.calls[0]["Latest"])

    def test_aged_out_instance_is_skipped(self):
        class Client:
            def get_console_output(self, **kwargs):
                error = RuntimeError("gone")
                error.response = {"Error": {"Code": "InvalidInstanceID.NotFound"}}
                raise error

        self.assertEqual(Aws(self.config, client=Client()).console_output(["i-0123456789abcdefa"]), {})

    def test_empty_or_non_string_output_is_empty_evidence(self):
        class Client:
            def __init__(self, value):
                self.value = value

            def get_console_output(self, **kwargs):
                return {"Output": self.value}

        for value in ("", None, b"bytes-not-a-string"):
            with self.subTest(value=value):
                self.assertEqual(Aws(self.config, client=Client(value)).console_output(["i-0123456789abcdef9"]), {"i-0123456789abcdef9": ""})


class ExportCliTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)

    def test_metrics_file_is_required(self):
        from ci_burst.cli import main

        with mock.patch("sys.stdout"):
            with self.assertRaises(SystemExit) as raised:
                main(["export", "--config", str(write_config(self.root))])
        self.assertEqual(raised.exception.code, 2)

    def test_export_exits_zero_on_a_reported_decision(self):
        from ci_burst.cli import main

        stub = type(
            "StubExporter",
            (),
            {"__init__": lambda self, config: None, "run": lambda self, **kwargs: {"decision": "reported"}},
        )
        buffer = io.StringIO()
        with mock.patch("ci_burst.cli.Exporter", stub), redirect_stdout(buffer):
            code = main(
                [
                    "export",
                    "--config",
                    str(write_config(self.root, config={"state": {"database": str(self.root / "db.sqlite3")}})),
                    "--metrics-file",
                    str(self.root / "ci-burst.prom"),
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["decision"], "reported")

    def test_the_exporter_builds_a_live_adapter_when_none_is_injected(self):
        config = load(write_config(self.root))
        exporter = Exporter(config)
        self.assertIsInstance(exporter.aws, Aws)


if __name__ == "__main__":
    unittest.main()
