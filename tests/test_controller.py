import json
import tempfile
import unittest
from pathlib import Path

from ci_burst.config import load
from ci_burst.controller import Controller
from ci_burst.errors import ConfigRefusal, ProviderRefusal
from ci_burst.aws import SpotQuote
from ci_burst.ledger import Ledger, current_window

from support import (
    Clock,
    FakeAws,
    FakeObserver,
    instance,
    observation_demand,
    observation_empty,
    quote,
    template,
    write_config,
)


class ControllerCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.clock = Clock()
        self.config = load(write_config(self.root))

    def build(self, *, observation=None, aws=None, config=None, ledger=None):
        self.config = config or self.config
        self.aws = aws or FakeAws(spot_quote=quote(clock=self.clock))
        self.observer = FakeObserver(observation or observation_empty())
        self.ledger = ledger or Ledger(self.config.state.database, clock=self.clock)
        return Controller(
            self.config,
            observer=self.observer,
            aws=self.aws,
            ledger=self.ledger,
            clock=self.clock,
        )

    def reopened(self):
        ledger = Ledger(self.config.state.database, clock=self.clock).connect()
        self.addCleanup(ledger.close)
        return ledger

    def seed(self, *, state="running", operation_id="existing-op", expires_at=None, reservation=1_000_000, instance_id="i-seed"):
        ledger = Ledger(self.config.state.database, clock=self.clock).connect()
        with ledger.transaction():
            ledger.new_operation(
                operation_id=operation_id,
                client_token="ci-burst-" + operation_id,
                request={"seed": True},
                request_sha256="0" * 64,
                expires_at=expires_at if expires_at is not None else self.clock.now + 3600,
                reservation_micros=reservation,
                window_start=current_window(self.clock.now, self.config.budget.window_seconds),
            )
        if state != "intent" or instance_id:
            ledger.set_state(operation_id, state, instance_id=instance_id)
        ledger.close()
        return operation_id


class PlanTests(ControllerCase):
    def test_plan_with_demand_would_launch_without_mutation(self):
        controller = self.build(observation=observation_demand())
        result = controller.plan()
        self.assertEqual(result["decision"], "would_launch")
        self.assertEqual(result["observation"]["demand_attempts"], ["handle-0"])
        self.assertEqual(result["price"]["cap"], "0.040000")
        self.assertEqual(self.aws.mutations(), [])
        self.assertFalse(self.config.state.database.exists())

    def test_plan_without_demand_waits(self):
        result = self.build(observation=observation_empty()).plan()
        self.assertEqual(result["decision"], "wait")
        self.assertIn("no_runnable_demand", result["reasons"])
        self.assertEqual(self.aws.mutations(), [])

    def test_plan_with_busy_slot_waits(self):
        self.seed(state="running")
        result = self.build(observation=observation_demand()).plan()
        self.assertEqual(result["decision"], "wait")
        self.assertIn("slot_busy", result["reasons"])

    def test_plan_reports_unreadable_queue_as_refused(self):
        controller = self.build()
        controller.observer = FakeObserver(error=ProviderRefusal("FORGEJO_UNAVAILABLE"))
        result = controller.plan()
        self.assertEqual(result["decision"], "refused")
        self.assertEqual(result["refusal"]["reason"], "FORGEJO_UNAVAILABLE")
        self.assertEqual(self.aws.mutations(), [])

    def test_plan_refuses_price_above_cap(self):
        aws = FakeAws(spot_quote=quote(price_micros=50_000, clock=self.clock))
        result = self.build(observation=observation_demand(), aws=aws).plan()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("spot_price_above_cap", result["reasons"])

    def test_plan_refuses_stale_price(self):
        aws = FakeAws(spot_quote=quote(price_micros=20_000, timestamp=self.clock.now - 4000))
        result = self.build(observation=observation_demand(), aws=aws).plan()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("spot_price_stale", result["reasons"])

    def test_plan_refuses_missing_price(self):
        aws = FakeAws(spot_quote=None)
        result = self.build(observation=observation_demand(), aws=aws).plan()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("spot_price_unavailable", result["reasons"])

    def test_plan_refuses_exhausted_allowance(self):
        self.seed(state="terminal", operation_id="spent", reservation=9_800_000)
        result = self.build(observation=observation_demand()).plan()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("budget_allowance_exhausted", result["reasons"])
        self.assertEqual(result["budget"]["remaining_after"].startswith("-"), True)

    def test_plan_refuses_a_template_pinned_to_another_zone(self):
        aws = FakeAws(template_facts=template(placement_zone="us-east-1a"), spot_quote=quote(clock=self.clock))
        result = self.build(observation=observation_demand(), aws=aws).plan()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("template_placement_mismatch", result["reasons"])

    def test_plan_refuses_a_quote_from_another_zone(self):
        wrong = SpotQuote(
            price_micros=20_000,
            timestamp=self.clock.now,
            price_display="0.020000",
            instance_type="t4g.small",
            availability_zone="us-east-1a",
        )
        result = self.build(observation=observation_demand(), aws=FakeAws(spot_quote=wrong)).plan()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("spot_price_wrong_scope", result["reasons"])

    def test_plan_refuses_template_without_bootstrap(self):
        aws = FakeAws(template_facts=template(has_user_data=False), spot_quote=quote(clock=self.clock))
        result = self.build(observation=observation_demand(), aws=aws).plan()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("template_bootstrap_missing", result["reasons"])

    def test_plan_refuses_an_invalid_state_instead_of_computing_from_zero(self):
        import sqlite3

        sqlite3.connect(self.config.state.database).close()
        result = self.build(observation=observation_demand()).plan()
        self.assertEqual(result["decision"], "refused")
        self.assertEqual(result["refusal"]["reason"], "STATE_UNINITIALIZED")
        probe = sqlite3.connect(self.config.state.database)
        try:
            self.assertEqual(probe.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertIsNone(probe.execute("SELECT name FROM sqlite_master WHERE name='operations'").fetchone())
        finally:
            probe.close()

    def test_plan_reports_cost_residuals(self):
        result = self.build(observation=observation_demand()).plan()
        self.assertIn("does not bound EBS", result["cost_scope"])
        self.assertIn("not a guaranteed cap", result["cost_scope"])


class ApplyTests(ControllerCase):
    def test_launch_uses_the_pinned_template_and_one_spot_instance(self):
        controller = self.build(observation=observation_demand())
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        run = [call for call in self.aws.calls if call[0] == "run_spot"]
        self.assertEqual(len(run), 1)
        request = run[0][1]
        self.assertEqual(request["LaunchTemplate"], {"LaunchTemplateId": "lt-0123456789abcdef0", "Version": "3"})
        self.assertEqual((request["MinCount"], request["MaxCount"]), (1, 1))
        self.assertEqual(request["InstanceMarketOptions"]["MarketType"], "spot")
        self.assertEqual(request["InstanceMarketOptions"]["SpotOptions"]["SpotInstanceType"], "one-time")
        self.assertEqual(request["InstanceMarketOptions"]["SpotOptions"]["MaxPrice"], "0.040000")
        self.assertNotIn("OnDemand", json.dumps(request))
        tags = {tag["Key"]: tag["Value"] for tag in request["TagSpecifications"][0]["Tags"]}
        self.assertEqual(tags["ci-burst:managed"], "true")
        self.assertEqual(tags["ci-burst:deployment"], "test-deployment")
        self.assertIn("ci-burst:expires-at", tags)
        operation = self.reopened().operation_by_token(request["ClientToken"])
        self.assertEqual(tags["ci-burst:operation"], operation.id)
        self.assertEqual(operation.state, "starting")
        self.assertEqual(operation.instance_id, "i-0123456789abcdef0")

    def test_intent_is_committed_before_the_aws_call(self):
        controller = self.build(observation=observation_demand())
        observed = {}

        def check(request):
            probe = Ledger(self.config.state.database).connect()
            try:
                row = probe.operation_by_token(request["ClientToken"])
            finally:
                probe.close()
            observed["state"] = row.state
            observed["operation"] = row.id

        self.aws.on_run = check
        controller.apply()
        self.assertEqual(observed["state"], "intent")

    def test_apply_requires_live_qualification(self):
        config = load(write_config(self.root, config={"forgejo": {"qualification": "fixtures-only"}}))
        controller = self.build(observation=observation_demand(), config=config)
        with self.assertRaises(ConfigRefusal):
            controller.apply()
        self.assertEqual(self.aws.calls, [])
        self.assertFalse(self.config.state.database.exists())

    def test_second_apply_does_not_launch_into_a_busy_slot(self):
        controller = self.build(observation=observation_demand())
        first = controller.apply()
        self.aws.instances_result = [instance(operation=first["operation"], state="running")]
        result = controller.apply()
        self.assertEqual(result["decision"], "wait")
        self.assertIn("slot_busy", result["reasons"])
        self.assertEqual(len([call for call in self.aws.calls if call[0] == "run_spot"]), 1)

    def test_definite_launch_failure_frees_the_slot_but_keeps_the_reservation(self):
        controller = self.build(observation=observation_demand())
        self.aws.run_error = ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity")
        result = controller.apply()
        self.assertEqual(result["decision"], "launch_failed")
        self.assertEqual(result["refusal"], {"reason": "AWS_REFUSED", "detail": "InsufficientInstanceCapacity"})
        operation = self.reopened().operation(result["operation"])
        self.assertEqual(operation.state, "failed")
        window = current_window(self.clock.now, self.config.budget.window_seconds)
        self.assertEqual(self.reopened().consumed_micros(window), operation.reservation_micros)

    def test_lost_response_is_uncertain_then_adopted_without_a_second_launch(self):
        controller = self.build(observation=observation_demand())
        self.aws.run_error = ProviderRefusal("AWS_UNCERTAIN", "ReadTimeout", uncertain=True)
        first = controller.apply()
        self.assertEqual(first["decision"], "uncertain")
        operation = self.reopened().operation(first["operation"])
        self.assertEqual(operation.state, "uncertain")
        self.aws.run_error = None
        self.aws.instances_result = [instance(operation=operation.id, state="running")]
        second = controller.apply()
        self.assertEqual(second["decision"], "wait")
        self.assertIn("slot_busy", second["reasons"])
        self.assertEqual(len([call for call in self.aws.calls if call[0] == "run_spot"]), 1)
        self.assertEqual(self.reopened().operation(operation.id).state, "running")

    def test_uncertain_operation_is_resumed_with_the_same_client_token(self):
        controller = self.build(observation=observation_demand())
        self.aws.run_error = ProviderRefusal("AWS_UNCERTAIN", "ReadTimeout", uncertain=True)
        first = controller.apply()
        self.assertEqual(first["decision"], "uncertain")
        self.aws.run_error = None
        # A fresh controller and ledger model a restarted process.
        second = self.build(observation=observation_demand(), aws=self.aws).apply()
        self.assertEqual(second["decision"], "resumed")
        runs = [call for call in self.aws.calls if call[0] == "run_spot"]
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0][1], runs[1][1])
        self.assertEqual(second["operation"], first["operation"])
        ledger = self.reopened()
        self.assertEqual(len(ledger.operations()), 1)
        self.assertEqual(ledger.operation(first["operation"]).state, "starting")

    def test_rejected_resume_keeps_unresolved_ownership(self):
        controller = self.build(observation=observation_demand())
        self.aws.run_error = ProviderRefusal("AWS_UNCERTAIN", "ReadTimeout", uncertain=True)
        first = controller.apply()
        self.aws.run_error = ProviderRefusal("AWS_REFUSED", "Throttling")
        second = controller.apply()
        self.assertEqual(second["decision"], "uncertain")
        self.assertEqual(second["operation"], first["operation"])
        ledger = self.reopened()
        self.assertEqual(ledger.operation(first["operation"]).state, "uncertain")
        self.assertEqual(len(ledger.operations()), 1)
        self.aws.run_error = None
        third = self.build(observation=observation_demand(), aws=self.aws).apply()
        self.assertEqual(third["decision"], "resumed")
        self.assertEqual(third["operation"], first["operation"])

    def test_unresolved_intent_older_than_the_token_window_is_refused(self):
        self.seed(state="intent", instance_id=None, expires_at=self.clock.now + 30 * 3600)
        self.clock.advance(25 * 3600)
        controller = self.build(observation=observation_demand())
        result = controller.apply()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("unresolved_intent_expired", result["reasons"])
        self.assertEqual([call for call in self.aws.calls if call[0] == "run_spot"], [])

    def test_unresolved_intent_past_its_lifetime_is_not_sent(self):
        self.seed(state="intent", instance_id=None, expires_at=self.clock.now + 60)
        self.clock.advance(120)
        controller = self.build(observation=observation_demand())
        result = controller.apply()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("unresolved_intent_lifetime_elapsed", result["reasons"])
        self.assertEqual([call for call in self.aws.calls if call[0] == "run_spot"], [])

    def test_changed_placement_cannot_replace_an_unresolved_intent(self):
        controller = self.build(observation=observation_demand())
        self.aws.run_error = ProviderRefusal("AWS_UNCERTAIN", "ReadTimeout", uncertain=True)
        first = controller.apply()
        self.aws.run_error = None
        changed = load(write_config(self.root, config={"aws": {"availability_zone": "eu-central-1b"}}))
        result = self.build(observation=observation_demand(), aws=self.aws, config=changed).apply()
        self.assertEqual(result["decision"], "resumed")
        self.assertEqual(result["operation"], first["operation"])
        run = [call for call in self.aws.calls if call[0] == "run_spot"][-1][1]
        self.assertEqual(run["Placement"]["AvailabilityZone"], "eu-central-1a")
        self.assertEqual(len(self.reopened().operations()), 1)

    def test_shutting_down_instance_keeps_the_slot(self):
        operation_id = self.seed(state="running")
        controller = self.build(observation=observation_demand())
        self.aws.instances_result = [instance(instance_id="i-seed", operation=operation_id, state="shutting-down")]
        result = controller.apply()
        self.assertEqual(result["decision"], "wait")
        self.assertIn("slot_busy", result["reasons"])
        self.assertEqual(self.reopened().operation(operation_id).state, "terminating")
        self.assertEqual(self.aws.terminated, [])

    def test_terminal_operation_with_a_live_instance_blocks_admission(self):
        operation_id = self.seed(state="terminal")
        controller = self.build(observation=observation_demand())
        self.aws.instances_result = [instance(operation=operation_id, state="running")]
        result = controller.apply()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("owned_state_discrepancy", result["reasons"])
        self.assertEqual(self.aws.mutations(), [])

    def test_lingering_terminated_record_does_not_block_the_next_launch(self):
        self.seed(state="terminal", operation_id="old-operation")
        controller = self.build(observation=observation_demand())
        self.aws.instances_result = [instance(instance_id="i-gone", operation="old-operation", state="terminated")]
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")

    def test_duplicate_instances_for_one_operation_are_refused(self):
        operation_id = self.seed(state="running")
        controller = self.build(observation=observation_demand())
        self.aws.instances_result = [
            instance(instance_id="i-a", operation=operation_id),
            instance(instance_id="i-b", operation=operation_id),
        ]
        result = controller.apply()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("owned_state_discrepancy", result["reasons"])
        self.assertEqual(self.aws.mutations(), [])

    def test_unrecorded_owned_instance_blocks_a_launch(self):
        controller = self.build(observation=observation_demand())
        self.aws.instances_result = [instance(operation="unknown-operation")]
        result = controller.apply()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("owned_state_discrepancy", result["reasons"])
        self.assertEqual(self.aws.mutations(), [])

    def test_expired_instance_is_terminated_before_new_admission(self):
        operation_id = self.seed(state="running", expires_at=self.clock.now - 10)
        controller = self.build(observation=observation_demand())
        self.aws.instances_result = [
            instance(instance_id="i-seed", operation=operation_id, expires_at=self.clock.now - 10)
        ]
        result = controller.apply()
        self.assertEqual(self.aws.terminated, ["i-seed"])
        self.assertEqual(self.reopened().operation(operation_id).state, "terminating")
        self.assertIn("expiry_cleanup", result)
        self.assertEqual(len([call for call in self.aws.calls if call[0] == "run_spot"]), 0)

    def test_denied_expiry_cleanup_stays_unresolved(self):
        operation_id = self.seed(state="running", expires_at=self.clock.now - 10)
        controller = self.build(observation=observation_demand())
        self.aws.instances_result = [
            instance(instance_id="i-seed", operation=operation_id, expires_at=self.clock.now - 10)
        ]
        self.aws.terminate_error = ProviderRefusal("AWS_REFUSED", "UnauthorizedOperation")
        result = controller.apply()
        self.assertEqual(result["decision"], "cleanup_failed")
        self.assertEqual(self.reopened().operation(operation_id).state, "running")
        self.assertEqual(self.aws.mutations(), [("terminate", ["i-seed"])])

    def test_tag_stripped_instance_blocks_replacement(self):
        operation_id = self.seed(state="running")
        controller = self.build(observation=observation_demand())
        self.aws.instances_result = []
        self.aws.instances_by_id = {"i-seed": instance(instance_id="i-seed", operation=operation_id, state="running")}
        result = controller.apply()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("owned_state_discrepancy", result["reasons"])
        self.assertEqual(self.aws.mutations(), [])

    def test_terminated_instance_reconciles_to_terminal(self):
        operation_id = self.seed(state="running")
        controller = self.build(observation=observation_empty())
        self.aws.instances_result = [instance(operation=operation_id, state="terminated")]
        controller.apply()
        self.assertEqual(self.reopened().operation(operation_id).state, "terminal")

    def test_absent_confirmed_instance_reconciles_to_terminal(self):
        operation_id = self.seed(state="running")
        controller = self.build(observation=observation_empty())
        self.aws.instances_result = []
        controller.apply()
        self.assertEqual(self.reopened().operation(operation_id).state, "terminal")

    def test_no_secret_or_user_data_in_launch_output(self):
        controller = self.build(observation=observation_demand())
        result = controller.apply()
        dumped = json.dumps(result)
        self.assertNotIn("UserData", dumped)
        self.assertNotIn("Authorization", dumped)


class ReapTests(ControllerCase):
    def test_reap_plans_only_expired_owned_instances(self):
        controller = self.build()
        self.aws.instances_result = [
            instance(instance_id="i-expired", operation="a", expires_at=self.clock.now - 5),
            instance(instance_id="i-live", operation="b", expires_at=self.clock.now + 500),
            instance(instance_id="i-gone", operation="c", state="terminated", expires_at=self.clock.now - 5),
        ]
        result = controller.reap()
        self.assertEqual(result["decision"], "planned")
        self.assertEqual([item["instance_id"] for item in result["expired"]], ["i-expired"])
        self.assertEqual(self.aws.mutations(), [])
        self.assertEqual(self.observer.calls, 0)

    def test_reap_apply_requests_termination_and_does_not_claim_confirmation(self):
        controller = self.build()
        self.aws.instances_result = [instance(instance_id="i-expired", operation="a", expires_at=self.clock.now - 5)]
        result = controller.reap(apply=True)
        self.assertEqual(result["decision"], "termination_requested")
        self.assertEqual(self.aws.terminated, ["i-expired"])
        self.assertIn("not confirmed removal", result["confirmation"])

    def test_reap_refuses_an_instance_without_an_operation_identity(self):
        controller = self.build()
        self.aws.instances_result = [
            instance(instance_id="i-pending", operation="pending", expires_at=self.clock.now - 5)
        ]
        result = controller.reap()
        self.assertEqual(result["refused_resources"][0]["reason"], "OPERATION_TAG_INVALID")
        self.assertEqual(result["expired"], [])

    def test_reap_refuses_malformed_expiry(self):
        controller = self.build()
        self.aws.instances_result = [instance(instance_id="i-bad", operation="a")]
        result = controller.reap()
        self.assertEqual(result["refused_resources"][0]["reason"], "EXPIRY_TAG_INVALID")
        self.assertEqual(result["expired"], [])

    def test_reap_does_not_need_forgejo_or_the_ledger(self):
        controller = self.build()
        controller.observer = FakeObserver(error=ProviderRefusal("FORGEJO_UNAVAILABLE"))
        self.aws.instances_result = []
        result = controller.reap()
        self.assertEqual(result["decision"], "planned")
        self.assertFalse(self.config.state.database.exists())

    def test_reap_never_launches(self):
        controller = self.build()
        self.aws.instances_result = [instance(instance_id="i-expired", operation="a", expires_at=self.clock.now - 5)]
        controller.reap(apply=True)
        self.assertEqual([call[0] for call in self.aws.mutations()], ["terminate"])


class StatusTests(ControllerCase):
    def test_status_reports_cloud_unavailable_not_clean(self):
        controller = self.build()
        self.aws.error = ProviderRefusal("AWS_REFUSED", "AccessDenied")
        result = controller.status()
        self.assertEqual(result["decision"], "reported")
        self.assertFalse(result["cloud"]["available"])
        self.assertEqual(result["cloud"]["refusal"]["reason"], "AWS_REFUSED")

    def test_status_separates_instance_state_from_runner_readiness(self):
        controller = self.build()
        self.aws.instances_result = [instance(operation="op")]
        result = controller.status()
        self.assertTrue(result["cloud"]["available"])
        self.assertFalse(result["runner_readiness"]["known"])

    def test_status_reports_discrepancies(self):
        controller = self.build()
        self.aws.instances_result = [instance(operation="unrecorded")]
        result = controller.status()
        self.assertEqual(result["slot"]["discrepancies"][0]["operation"], "unrecorded")


class RedactionTests(ControllerCase):
    def test_reported_plan_contains_no_token_value(self):
        controller = self.build(observation=observation_demand())
        controller.observer = FakeObserver(observation=observation_demand())
        import os

        os.environ["CI_BURST_TEST_TOKEN"] = "synthetic-secret-value"
        self.addCleanup(os.environ.pop, "CI_BURST_TEST_TOKEN", None)
        result = controller.plan()
        self.assertNotIn("synthetic-secret-value", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
