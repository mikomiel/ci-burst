import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ci_burst.aws import MAX_CONSOLE_BYTES, Aws
from ci_burst.config import load
from ci_burst.errors import ProviderRefusal
from ci_burst.export import (
    MAX_CONSOLE_READS,
    MAX_EVENTS_BYTES,
    Exporter,
    load_state,
    parse_console,
    prune_state,
)

from support import FakeAws, instance, write_config

NOW = 1_800_000_000.0

CLEAN_BOOT = """\
[    9.7] cloud-init[400]: STEP mount-options
[   10.7] cloud-init[400]: STEP register
[   10.8] cloud-init[400]: runner: burst-ip-10-0-0-1, with version: v1, ephemeral: true, declared successfully
[   10.9] cloud-init[400]: STEP job
[   24.0] cloud-init[400]: bootstrap exiting rc=0
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
