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
                region=self.config.aws.region,
                account="111122223333",
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
        # Staleness is the age of the observation, not of the provider's record.
        aws = FakeAws(
            spot_quote=quote(price_micros=20_000, observed_at=self.clock.now - 4000)
        )
        result = self.build(observation=observation_demand(), aws=aws).plan()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("spot_price_stale", result["reasons"])

    def test_plan_accepts_a_stable_price_observed_now(self):
        # A Spot price can be hours old and still current: the provider's
        # timestamp is when the price last changed.
        aws = FakeAws(
            spot_quote=quote(
                price_micros=20_000,
                timestamp=self.clock.now - 12_793,
                observed_at=self.clock.now,
            )
        )
        result = self.build(observation=observation_demand(), aws=aws).plan()
        self.assertEqual(result["decision"], "would_launch", result)
        self.assertNotIn("spot_price_stale", result.get("reasons", []))
        self.assertNotIn(
            "spot_price_stale",
            [candidate.get("reason") for candidate in result["candidates"]],
        )

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

    def test_admission_recomputes_time_after_a_slow_quote_read(self):
        config = load(write_config(self.root, config={"aws": {"max_lifetime_seconds": 60}}))
        aws = FakeAws(spot_quote=quote(clock=self.clock))
        aws.on_quote = lambda: self.clock.advance(70)
        controller = self.build(observation=observation_demand(), aws=aws, config=config)
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        operation = self.reopened().operation(result["operation"])
        self.assertGreater(operation.expires_at, self.clock.now)

    def test_a_slow_quote_read_cannot_keep_stale_price_evidence(self):
        aws = FakeAws(spot_quote=quote(clock=self.clock))
        aws.on_quote = lambda: self.clock.advance(1000)
        result = self.build(observation=observation_demand(), aws=aws).plan()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("spot_price_stale", result["reasons"])

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
            observed_at=self.clock.now,
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

    def test_definite_launch_failure_releases_the_slot_and_the_allowance(self):
        # A definite rejection proves no instance was created, so the attempt
        # must not consume the window: five capacity rejections used to exhaust
        # a pilot's whole allowance without launching anything.
        controller = self.build(observation=observation_demand())
        self.aws.run_error = ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity")
        result = controller.apply()
        self.assertEqual(result["decision"], "launch_failed")
        self.assertEqual(result["refusal"], {"reason": "AWS_REFUSED", "detail": "InsufficientInstanceCapacity"})
        operation = self.reopened().operation(result["operation"])
        self.assertEqual(operation.state, "failed")
        # One write records the failure and the release together, so a restart
        # between them cannot leave a failed operation still charging the window.
        self.assertEqual(operation.reservation_micros, 0)
        self.assertEqual(operation.detail, "AWS_REFUSED")
        window = current_window(self.clock.now, self.config.budget.window_seconds)
        self.assertEqual(self.reopened().consumed_micros(window), 0)

    def test_a_definite_non_capacity_refusal_releases_the_allowance(self):
        controller = self.build(observation=observation_demand())
        self.aws.run_error = ProviderRefusal("AWS_REFUSED", "UnauthorizedOperation")
        result = controller.apply()
        self.assertEqual(result["decision"], "launch_failed")
        window = current_window(self.clock.now, self.config.budget.window_seconds)
        self.assertEqual(self.reopened().consumed_micros(window), 0)

    def test_an_uncertain_launch_keeps_the_allowance(self):
        # The provider may have created the instance: the reservation stays
        # outstanding until the intent is resolved.
        controller = self.build(observation=observation_demand())
        self.aws.run_error = ProviderRefusal("AWS_UNCERTAIN", "ReadTimeout", uncertain=True)
        result = controller.apply()
        self.assertEqual(result["decision"], "uncertain")
        operation = self.reopened().operation(result["operation"])
        self.assertGreater(operation.reservation_micros, 0)
        window = current_window(self.clock.now, self.config.budget.window_seconds)
        self.assertEqual(
            self.reopened().consumed_micros(window), operation.reservation_micros
        )

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

    def test_recently_absent_instance_keeps_the_slot(self):
        operation_id = self.seed(state="running")
        controller = self.build(observation=observation_empty())
        self.aws.instances_result = []
        result = controller.apply()
        self.assertEqual(result["decision"], "wait")
        self.assertIn("slot_busy", result["reasons"])
        self.assertEqual(self.reopened().operation(operation_id).state, "running")

    def test_response_parser_failure_keeps_the_operation_and_token(self):
        import datetime as dt

        from ci_burst.aws import Aws

        class FlakyClient:
            def __init__(self):
                self.tokens = []
                self.fail = True

            def describe_instances(self, **kwargs):
                return {"Reservations": []}

            def describe_launch_template_versions(self, **kwargs):
                return {
                    "LaunchTemplateVersions": [
                        {
                            "LaunchTemplateId": "lt-1",
                            "VersionNumber": 3,
                            "LaunchTemplateData": {
                                "ImageId": "ami-1",
                                "InstanceType": "t4g.small",
                                "InstanceInitiatedShutdownBehavior": "terminate",
                                "UserData": "AAAA",
                            },
                        }
                    ]
                }

            def describe_spot_price_history(self, **kwargs):
                return {
                    "SpotPriceHistory": [
                        {
                            "InstanceType": "t4g.small",
                            "AvailabilityZone": "eu-central-1a",
                            "ProductDescription": "Linux/UNIX",
                            "SpotPrice": "0.020000",
                            "Timestamp": dt.datetime.fromtimestamp(self_clock.now, dt.timezone.utc),
                        }
                    ]
                }

            def run_instances(self, **kwargs):
                self.tokens.append(kwargs["ClientToken"])
                if self.fail:
                    self.fail = False
                    raise RuntimeError("ResponseParserError")
                return {"Instances": [{"InstanceId": "i-real"}]}

        self_clock = self.clock
        client = FlakyClient()
        aws = Aws(self.config, client=client, clock=self.clock, account="111122223333")
        controller = Controller(
            self.config,
            observer=FakeObserver(observation_demand()),
            aws=aws,
            ledger=Ledger(self.config.state.database, clock=self.clock),
            clock=self.clock,
        )
        first = controller.apply()
        self.assertEqual(first["decision"], "uncertain")
        second = self.build(observation=observation_demand(), aws=aws).apply()
        self.assertEqual(second["decision"], "resumed")
        self.assertEqual(second["operation"], first["operation"])
        self.assertEqual(len(client.tokens), 2)
        self.assertEqual(client.tokens[0], client.tokens[1])

    def test_region_change_while_an_operation_is_outstanding_is_refused(self):
        operation_id = self.seed(state="running")
        controller = self.build(observation=observation_demand())
        self.aws.instances_result = []
        self.clock.advance(3601)
        changed = load(write_config(self.root, config={"aws": {"region": "us-east-1", "availability_zone": "us-east-1a"}}))
        result = self.build(observation=observation_demand(), aws=self.aws, config=changed).apply()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("owned_state_discrepancy", result["reasons"])
        self.assertEqual(result["slot"]["discrepancies"][0]["reason"], "SCOPE_CHANGED")
        self.assertEqual(self.aws.mutations(), [])
        operation = self.reopened().operation(operation_id)
        self.assertEqual(operation.state, "running")
        self.assertEqual(operation.instance_id, "i-seed")

    def test_account_change_is_refused_while_an_operation_is_outstanding(self):
        operation_id = self.seed(state="running")
        controller = self.build(observation=observation_demand())
        self.aws.instances_result = []
        self.clock.advance(3601)
        self.aws.account = "999988887777"
        result = controller.apply()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("owned_state_discrepancy", result["reasons"])
        self.assertEqual(result["slot"]["discrepancies"][0]["reason"], "SCOPE_CHANGED")
        self.assertEqual(self.aws.mutations(), [])
        self.assertEqual(self.reopened().operation(operation_id).state, "running")

    def test_plan_refuses_a_region_change_and_retains_the_operation(self):
        operation_id = self.seed(state="running")
        changed = load(write_config(self.root, config={"aws": {"region": "us-east-1", "availability_zone": "us-east-1a"}}))
        result = self.build(observation=observation_demand(), config=changed).plan()
        self.assertEqual(result["decision"], "refused")
        self.assertEqual(result["refusal"]["reason"], "SCOPE_CHANGED")
        self.assertEqual(self.aws.mutations(), [])
        self.assertEqual(self.reopened().operation(operation_id).state, "running")

    def test_tagged_record_cannot_replace_a_confirmed_instance(self):
        operation_id = self.seed(state="running")
        controller = self.build(observation=observation_demand())
        self.aws.instances_result = [instance(instance_id="i-other", operation=operation_id, state="terminated")]
        self.aws.instances_by_id = {
            "i-seed": instance(instance_id="i-seed", operation=operation_id, state="running")
        }
        result = controller.apply()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("owned_state_discrepancy", result["reasons"])
        self.assertEqual(self.aws.mutations(), [])
        operation = self.reopened().operation(operation_id)
        self.assertEqual(operation.state, "running")
        self.assertEqual(operation.instance_id, "i-seed")

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
        self.aws.instances_result = [instance(instance_id="i-seed", operation="old-operation", state="terminated")]
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

    def test_a_terminated_unrecorded_instance_does_not_block_admission(self):
        # A tagged instance that the ledger never recorded, already terminated:
        # EC2 keeps listing it for a while, but it holds nothing. Report it and
        # launch, instead of refusing until the provider prunes the record.
        controller = self.build(observation=observation_demand())
        self.aws.instances_result = [
            instance(instance_id="i-gone", operation="unknown-operation", state="terminated")
        ]
        result = controller.apply()
        self.assertEqual(result["decision"], "launched", result)
        self.assertNotIn("owned_state_discrepancy", result.get("reasons", []))
        self.assertEqual(
            result["slot"]["ignored_discrepancies"][0]["reason"], "UNRECORDED_OWNED_INSTANCE"
        )
        self.assertEqual(result["slot"]["ignored_discrepancies"][0]["instance_id"], "i-gone")

    def test_a_terminated_unrecorded_instance_is_reported_by_status(self):
        controller = self.build()
        self.aws.instances_result = [
            instance(instance_id="i-gone", operation="unknown-operation", state="terminated")
        ]
        result = controller.status()
        self.assertEqual(result["decision"], "reported")
        self.assertEqual(result["slot"]["discrepancies"], [])
        self.assertEqual(
            result["slot"]["ignored_discrepancies"][0]["instance_id"], "i-gone"
        )

    def test_an_unusable_operation_tag_blocks_in_any_state(self):
        # The exception is narrow on purpose: a real operation id the ledger
        # never recorded, on a terminated instance. A missing, empty or
        # `pending` tag says nothing about ownership, so it blocks whichever
        # state the instance is in.
        for operation in (None, "", "pending"):
            for state in ("running", "terminated"):
                with self.subTest(operation=operation, state=state):
                    controller = self.build(observation=observation_demand())
                    self.aws.instances_result = [
                        instance(instance_id="i-odd", operation=operation, state=state)
                    ]
                    result = controller.apply()
                    self.assertEqual(result["decision"], "refused", result)
                    self.assertIn("owned_state_discrepancy", result["reasons"])
                    self.assertEqual(
                        result["slot"]["discrepancies"][0]["reason"],
                        "OWNED_INSTANCE_WITHOUT_OPERATION",
                    )
                    self.assertNotIn("ignored_discrepancies", result["slot"])
                    self.assertEqual(self.aws.mutations(), [])

    def test_status_reports_an_unusable_operation_tag_as_blocking(self):
        controller = self.build()
        self.aws.instances_result = [instance(instance_id="i-odd", operation="pending", state="terminated")]
        result = controller.status()
        self.assertEqual(
            result["slot"]["discrepancies"][0]["reason"], "OWNED_INSTANCE_WITHOUT_OPERATION"
        )
        self.assertNotIn("ignored_discrepancies", result["slot"])

    def test_a_live_unrecorded_instance_still_blocks_beside_a_terminated_one(self):
        controller = self.build(observation=observation_demand())
        self.aws.instances_result = [
            instance(instance_id="i-gone", operation="unknown-gone", state="terminated"),
            instance(instance_id="i-live", operation="unknown-live", state="running"),
        ]
        result = controller.apply()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("owned_state_discrepancy", result["reasons"])
        self.assertEqual(
            [entry["instance_id"] for entry in result["slot"]["discrepancies"]], ["i-live"]
        )
        self.assertEqual(
            [entry["instance_id"] for entry in result["slot"]["ignored_discrepancies"]],
            ["i-gone"],
        )
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
        self.aws.instances_result = [instance(instance_id="i-seed", operation=operation_id, state="terminated")]
        controller.apply()
        self.assertEqual(self.reopened().operation(operation_id).state, "terminal")

    def test_absent_confirmed_instance_reconciles_to_terminal(self):
        operation_id = self.seed(state="running")
        controller = self.build(observation=observation_empty())
        self.aws.instances_result = []
        self.clock.advance(3601)
        controller.apply()
        self.assertEqual(self.reopened().operation(operation_id).state, "terminal")

    def test_no_secret_or_user_data_in_launch_output(self):
        controller = self.build(observation=observation_demand())
        result = controller.apply()
        dumped = json.dumps(result)
        self.assertNotIn("UserData", dumped)
        self.assertNotIn("Authorization", dumped)


class FleetTests(ControllerCase):
    def fleet_config(self, max_instances=4):
        return load(write_config(self.root, config={"controller": {"max_instances": max_instances}}))

    def zone_config(self, *, max_instances=2, zones=("eu-central-1a", "eu-central-1b")):
        return load(
            write_config(
                self.root,
                config={
                    "controller": {"max_instances": max_instances},
                    "aws": {
                        "availability_zones": [
                            {
                                "zone": zone,
                                "subnet_id": f"subnet-0123456789abcdef{index:x}",
                            }
                            for index, zone in enumerate(zones)
                        ]
                    },
                },
            )
        )

    def test_plan_reports_the_batch_size_for_a_backlog(self):
        controller = self.build(observation=observation_demand(5), config=self.fleet_config(3))
        result = controller.plan()
        self.assertEqual(result["decision"], "would_launch")
        self.assertEqual(result["demand"], 5)
        self.assertEqual(result["free_instances"], 3)
        self.assertEqual(result["launch_count"], 3)
        self.assertEqual(result["budget"]["launch_count"], 3)
        self.assertEqual(self.aws.mutations(), [])

    def test_demand_two_within_a_four_slot_fleet_launches_two(self):
        controller = self.build(observation=observation_demand(2), config=self.fleet_config(4))
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        self.assertEqual(len(result["launches"]), 2)
        self.assertEqual(len(self.reopened().operations()), 2)

    def test_a_backlog_beyond_the_limit_launches_the_limit(self):
        controller = self.build(observation=observation_demand(6), config=self.fleet_config(4))
        result = controller.apply()
        self.assertEqual(len(result["launches"]), 4)
        self.assertEqual(len(self.reopened().operations()), 4)
        self.assertEqual(len([call for call in self.aws.calls if call[0] == "run_spot"]), 4)

    def test_existing_workers_leave_only_the_free_slots(self):
        self.seed(state="running", operation_id="busy-a", instance_id="i-busy-a")
        self.seed(state="running", operation_id="busy-b", instance_id="i-busy-b")
        controller = self.build(observation=observation_demand(4), config=self.fleet_config(4))
        result = controller.apply()
        self.assertEqual(result["launch_count"], 2)
        self.assertEqual(len(result["launches"]), 2)
        self.assertEqual(len(self.reopened().operations()), 4)

    def test_a_three_zone_batch_rotates_through_the_candidate_order(self):
        config = load(
            write_config(
                self.root,
                config={
                    "controller": {"max_instances": 3},
                    "aws": {
                        "availability_zones": [
                            {"zone": "eu-central-1a", "subnet_id": "subnet-0123456789abcdef0"},
                            {"zone": "eu-central-1b", "subnet_id": "subnet-0123456789abcdef1"},
                            {"zone": "eu-central-1c", "subnet_id": "subnet-0123456789abcdef2"},
                        ]
                    },
                },
            )
        )
        quotes = {
            zone: quote(clock=self.clock, availability_zone=zone)
            for zone in ("eu-central-1a", "eu-central-1b", "eu-central-1c")
        }
        controller = self.build(
            observation=observation_demand(3),
            aws=FakeAws(spot_quote=quotes),
            config=config,
        )
        result = controller.apply()
        runs = [call[1] for call in self.aws.calls if call[0] == "run_spot"]
        self.assertEqual(
            [run["Placement"]["AvailabilityZone"] for run in runs],
            ["eu-central-1a", "eu-central-1b", "eu-central-1c"],
        )
        self.assertEqual(
            [run["SubnetId"] for run in runs],
            ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1", "subnet-0123456789abcdef2"],
        )
        self.assertEqual(len({run["ClientToken"] for run in runs}), 3)
        self.assertEqual(len(result["launches"]), 3)
        self.assertEqual(
            [[attempt["zone"] for attempt in entry["attempts"]] for entry in result["launches"]],
            [["eu-central-1a"], ["eu-central-1b"], ["eu-central-1c"]],
        )

    def test_a_later_worker_is_not_committed_after_the_shared_expiry_elapses(self):
        config = load(
            write_config(
                self.root,
                config={
                    "controller": {"max_instances": 2},
                    "aws": {"max_lifetime_seconds": 60},
                },
            )
        )
        controller = self.build(observation=observation_demand(2), config=config)
        self.aws.on_run = lambda request: self.clock.advance(70)
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        self.assertIn("admission_expired", result["reasons"])
        self.assertEqual(len(result["launches"]), 1)
        self.assertEqual(len([call for call in self.aws.calls if call[0] == "run_spot"]), 1)
        self.assertEqual(len(self.reopened().operations()), 1)

    def test_a_stale_first_choice_skips_to_the_next_fresh_candidate(self):
        config = self.zone_config(max_instances=2)
        quotes = {
            "eu-central-1a": quote(clock=self.clock, availability_zone="eu-central-1a"),
            "eu-central-1b": quote(
                clock=self.clock,
                availability_zone="eu-central-1b",
                observed_at=self.clock.now - 100,
            ),
        }
        controller = self.build(
            observation=observation_demand(2),
            aws=FakeAws(spot_quote=quotes),
            config=config,
        )
        self.aws.on_run = lambda request: self.clock.advance(850)
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        self.assertEqual(len(result["launches"]), 2)
        second = result["launches"][1]
        self.assertEqual(second["zone"], "eu-central-1a")
        self.assertEqual(
            second["attempts"],
            [
                {"zone": "eu-central-1b", "outcome": "skipped", "reason": "spot_price_stale"},
                {"zone": "eu-central-1a", "outcome": "accepted"},
            ],
        )

    def test_a_capacity_fallback_reports_the_accepted_zone(self):
        config = self.zone_config(max_instances=1)
        quotes = {
            zone: quote(clock=self.clock, availability_zone=zone)
            for zone in ("eu-central-1a", "eu-central-1b")
        }
        aws = FakeAws(spot_quote=quotes)

        def reject_first(request):
            if request["Placement"]["AvailabilityZone"] == "eu-central-1a":
                raise ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity")

        aws.on_run = reject_first
        controller = self.build(observation=observation_demand(1), aws=aws, config=config)
        result = controller.apply()
        launch = result["launches"][0]
        self.assertEqual(launch["zone"], "eu-central-1b")
        self.assertEqual(
            launch["attempts"],
            [
                {
                    "zone": "eu-central-1a",
                    "outcome": "capacity_rejected",
                    "reason": "AWS_REFUSED",
                    "detail": "InsufficientInstanceCapacity",
                },
                {"zone": "eu-central-1b", "outcome": "accepted"},
            ],
        )
        operation = self.reopened().operation(result["operation"])
        self.assertEqual(operation.request()["Placement"]["AvailabilityZone"], "eu-central-1b")

    def test_four_workers_over_two_zones_start_alternately(self):
        config = self.zone_config(max_instances=4)
        quotes = {
            zone: quote(clock=self.clock, availability_zone=zone)
            for zone in ("eu-central-1a", "eu-central-1b")
        }
        controller = self.build(
            observation=observation_demand(4),
            aws=FakeAws(spot_quote=quotes),
            config=config,
        )
        result = controller.apply()
        runs = [call[1] for call in self.aws.calls if call[0] == "run_spot"]
        self.assertEqual(
            [run["Placement"]["AvailabilityZone"] for run in runs],
            ["eu-central-1a", "eu-central-1b", "eu-central-1a", "eu-central-1b"],
        )
        self.assertEqual(
            [entry["zone"] for entry in result["launches"]],
            ["eu-central-1a", "eu-central-1b", "eu-central-1a", "eu-central-1b"],
        )
        self.assertEqual(len({entry["instance_id"] for entry in result["launches"]}), 4)

    def test_allowance_caps_a_batch_and_notes_the_cap(self):
        config = load(
            write_config(
                self.root,
                config={
                    "controller": {"max_instances": 4},
                    "budget": {"allowance": "1.08"},
                },
            )
        )
        controller = self.build(observation=observation_demand(6), config=config)
        result = controller.apply()
        self.assertEqual(result["launch_count"], 2)
        self.assertEqual(len(result["launches"]), 2)
        self.assertIn("budget_capped_batch", result["notes"])
        window = current_window(self.clock.now, config.budget.window_seconds)
        self.assertEqual(self.reopened().consumed_micros(window), 1_080_000)
        self.assertEqual(self.reopened().window_launches(window), 2)

    def test_two_unresolved_intents_both_resume(self):
        self.seed(state="intent", instance_id=None, operation_id="first")
        self.seed(state="intent", instance_id=None, operation_id="second")
        controller = self.build(observation=observation_demand())
        result = controller.apply()
        self.assertEqual(result["decision"], "resumed")
        self.assertEqual(len([call for call in self.aws.calls if call[0] == "run_spot"]), 2)
        self.assertEqual(
            [entry["operation"] for entry in result["resumes"]], ["first", "second"]
        )
        self.assertEqual(
            [entry["decision"] for entry in result["resumes"]], ["resumed", "resumed"]
        )
        self.assertNotEqual(
            result["resumes"][0]["instance_id"], result["resumes"][1]["instance_id"]
        )
        self.assertEqual(result["operation"], "second")
        operations = self.reopened().operations()
        self.assertEqual({operation.client_token for operation in operations}, {"ci-burst-first", "ci-burst-second"})
        self.assertEqual({operation.state for operation in operations}, {"starting"})

    def test_resume_uncertainty_does_not_reuse_an_earlier_instance(self):
        self.seed(state="intent", instance_id=None, operation_id="first", reservation=540_000)
        self.seed(state="intent", instance_id=None, operation_id="second")
        self.seed(state="intent", instance_id=None, operation_id="third")
        controller = self.build(observation=observation_demand())
        runs = []

        def second_is_uncertain(request):
            runs.append(request)
            if len(runs) == 2:
                self.aws.run_error = ProviderRefusal("AWS_UNCERTAIN", "ReadTimeout", uncertain=True)

        self.aws.on_run = second_is_uncertain
        result = controller.apply()
        self.assertEqual(result["decision"], "uncertain")
        self.assertEqual(result["operation"], "second")
        self.assertNotIn("instance_id", result)
        self.assertEqual(result["refusal"]["reason"], "AWS_UNCERTAIN")
        # The third intent is not retried after the unsuccessful one.
        self.assertEqual(len(runs), 2)
        first, second = result["resumes"]
        self.assertEqual([first["operation"], second["operation"]], ["first", "second"])
        self.assertEqual(first["decision"], "resumed")
        self.assertIsNotNone(first["instance_id"])
        self.assertEqual(first["reservation"], "0.540000")
        self.assertIsNone(second["instance_id"])
        self.assertEqual(second["decision"], "uncertain")
        self.assertEqual(second["refusal"]["reason"], "AWS_UNCERTAIN")
        ledger = self.reopened()
        self.assertEqual(ledger.operation("first").state, "starting")
        self.assertEqual(ledger.operation("first").instance_id, first["instance_id"])
        self.assertEqual(ledger.operation("second").state, "uncertain")
        self.assertEqual(ledger.operation("third").state, "intent")

    def test_resume_stops_at_an_intent_whose_lifetime_elapsed(self):
        self.seed(state="intent", instance_id=None, operation_id="first", reservation=540_000)
        self.seed(
            state="intent",
            instance_id=None,
            operation_id="second",
            expires_at=self.clock.now + 60,
        )
        self.seed(state="intent", instance_id=None, operation_id="third")
        controller = self.build(observation=observation_demand())
        self.aws.on_run = lambda request: self.clock.advance(120)
        result = controller.apply()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("unresolved_intent_lifetime_elapsed", result["reasons"])
        self.assertEqual(result["operation"], "second")
        self.assertNotIn("instance_id", result)
        runs = [call for call in self.aws.calls if call[0] == "run_spot"]
        self.assertEqual(len(runs), 1)
        first, second = result["resumes"]
        self.assertEqual([first["operation"], second["operation"]], ["first", "second"])
        self.assertEqual(first["decision"], "resumed")
        self.assertIsNotNone(first["instance_id"])
        self.assertEqual(second["decision"], "refused")
        self.assertIsNone(second["instance_id"])
        ledger = self.reopened()
        self.assertEqual(ledger.operation("first").state, "starting")
        self.assertEqual(ledger.operation("second").state, "intent")
        self.assertEqual(ledger.operation("third").state, "intent")

    def test_lowered_limit_waits_and_never_terminates(self):
        self.seed(state="running", operation_id="busy-a", instance_id="i-busy-a")
        self.seed(state="running", operation_id="busy-b", instance_id="i-busy-b")
        controller = self.build(observation=observation_demand(4), config=self.fleet_config(1))
        self.aws.instances_result = [
            instance(instance_id="i-busy-a", operation="busy-a", expires_at=self.clock.now + 3000),
            instance(instance_id="i-busy-b", operation="busy-b", expires_at=self.clock.now + 3000),
        ]
        result = controller.apply()
        self.assertEqual(result["decision"], "wait")
        self.assertIn("slot_busy", result["reasons"])
        self.assertIn("above_configured_limit", result["notes"])
        self.assertEqual(self.aws.mutations(), [])
        self.assertEqual(self.aws.terminated, [])

    def test_a_failed_launch_stops_the_rest_of_the_batch(self):
        controller = self.build(observation=observation_demand(3), config=self.fleet_config(3))
        runs = []

        def fail_second(request):
            runs.append(request)
            if len(runs) == 2:
                self.aws.run_error = ProviderRefusal("AWS_REFUSED", "UnauthorizedOperation")

        self.aws.on_run = fail_second
        result = controller.apply()
        self.assertEqual(result["decision"], "launch_failed")
        self.assertEqual(len([call for call in self.aws.calls if call[0] == "run_spot"]), 2)
        self.assertEqual(
            [entry["decision"] for entry in result["launches"]],
            ["launched", "launch_failed"],
        )
        self.assertIsNone(result["launches"][1]["instance_id"])
        operations = self.reopened().operations()
        self.assertEqual(len(operations), 2)
        self.assertEqual({operation.state for operation in operations}, {"starting", "failed"})

    def test_status_reports_the_controller_limit_and_window_budget(self):
        config = self.fleet_config(4)
        controller = self.build(config=config)
        self.seed(state="running", reservation=540_000)
        result = controller.status()
        self.assertEqual(result["controller"], {"max_instances": 4})
        self.assertEqual(
            result["budget"]["window_start"],
            current_window(self.clock.now, config.budget.window_seconds),
        )
        self.assertEqual(result["budget"]["window_seconds"], 86400)
        self.assertEqual(result["budget"]["allowance"], "10.000000")
        self.assertEqual(result["budget"]["consumed"], "0.540000")
        self.assertEqual(result["budget"]["launches"], 1)
        self.assertEqual(result["budget"]["busy_instances"], 1)
        self.assertEqual(result["budget"]["hourly_ceiling"], "0.160000")


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

    def test_reap_reports_a_denied_termination_without_claiming_success(self):
        # The documented failure shape: the ids are filled before the provider
        # call, so a denial keeps them and leaves refused_resources empty. Only
        # the decision and the top-level refusal distinguish success.
        controller = self.build()
        self.aws.instances_result = [
            instance(instance_id="i-expired", operation="a", expires_at=self.clock.now - 5)
        ]
        self.aws.terminate_error = ProviderRefusal("AWS_REFUSED", "UnauthorizedOperation")
        result = controller.reap(apply=True)
        self.assertEqual(result["decision"], "cleanup_failed")
        self.assertEqual(result["termination_requested"], ["i-expired"])
        self.assertEqual(result["refused_resources"], [])
        self.assertEqual(result["refusal"]["reason"], "AWS_REFUSED")

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
