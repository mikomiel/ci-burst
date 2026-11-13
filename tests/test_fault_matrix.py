"""Cross-cutting fault and accounting invariants (audit E05, #95).

The per-finding regressions prove one boundary each. This file feeds the
failures those boundaries share -- malformed or partial provider responses,
uncertain launch outcomes, and elapsed time without provider evidence -- through
the real adapter, the controller, the ledger and the console parser, and checks
that an unresolved operation never silently becomes free capacity.

Every client here is a plain object shaped like the EC2 API, the clock is the
support fake, and nothing needs a network, an account, a credential or a
container engine.
"""

import base64
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ci_burst.aws import Aws
from ci_burst.config import load
from ci_burst.controller import INSTANCE_RETENTION_SECONDS, Controller
from ci_burst.errors import ProviderRefusal
from ci_burst.export import parse_console
from ci_burst.ledger import Ledger, current_window
from ci_burst.money import format_money

from support import (
    Clock,
    FakeAws,
    FakeObserver,
    instance,
    observation_demand,
    observation_empty,
    quote,
    write_config,
)


def history(price="0.020000", zone="eu-central-1a", instance_type="t4g.small", product="Linux/UNIX"):
    return {
        "InstanceType": instance_type,
        "AvailabilityZone": zone,
        "ProductDescription": product,
        "SpotPrice": price,
        "Timestamp": datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc),
    }


def launch_template(**overrides):
    data = {
        "ImageId": "ami-0123456789abcdef0",
        "InstanceType": "t4g.small",
        "InstanceInitiatedShutdownBehavior": "terminate",
        "UserData": "AAAA",
    }
    data.update(overrides)
    return {
        "LaunchTemplateId": "lt-0123456789abcdef0",
        "VersionNumber": 3,
        "LaunchTemplateData": data,
    }


class ProviderClient:
    """A scripted EC2 client: the real adapter, no SDK and no network.

    Each queued entry is returned to the next call; an exception entry is
    raised instead, which is how a stub reports a provider failure.
    """

    def __init__(self, entries):
        self.entries = list(entries)
        self.calls = []

    def _respond(self, name, kwargs):
        self.calls.append((name, dict(kwargs)))
        entry = self.entries.pop(0)
        if isinstance(entry, Exception):
            raise entry
        return entry

    def describe_instances(self, **kwargs):
        return self._respond("describe_instances", kwargs)

    def describe_launch_template_versions(self, **kwargs):
        return self._respond("describe_launch_template_versions", kwargs)

    def describe_spot_price_history(self, **kwargs):
        return self._respond("describe_spot_price_history", kwargs)

    def get_console_output(self, **kwargs):
        return self._respond("get_console_output", kwargs)

    def run_instances(self, **kwargs):
        return self._respond("run_instances", kwargs)

    def names(self):
        return [name for name, _ in self.calls]


class FaultMatrixCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.clock = Clock()
        self.config = load(write_config(self.root))

    def build(self, *, observation=None, aws=None, config=None):
        if config is not None:
            self.config = config
        self.aws = aws or FakeAws(spot_quote=quote(clock=self.clock))
        self.observer = FakeObserver(observation or observation_empty())
        self.ledger = Ledger(self.config.state.database, clock=self.clock)
        return Controller(
            self.config,
            observer=self.observer,
            aws=self.aws,
            ledger=self.ledger,
            clock=self.clock,
        )

    def adapter(self, entries):
        self.client = ProviderClient(entries)
        return Aws(self.config, client=self.client, clock=self.clock, account="111122223333")

    def reopened(self):
        ledger = Ledger(self.config.state.database, clock=self.clock).connect()
        self.addCleanup(ledger.close)
        return ledger

    def totals(self, controller):
        window = current_window(self.clock.now, controller.config.budget.window_seconds)
        return self.reopened().window_totals(window, controller.cost, self.clock.now)

    def seed(
        self,
        *,
        state="running",
        operation_id="existing-op",
        expires_at=None,
        reservation=1_000_000,
        instance_id="i-seed",
    ):
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

    def zone_config(self, *, max_instances=1):
        return load(
            write_config(
                self.root,
                config={
                    "controller": {"max_instances": max_instances},
                    "aws": {
                        "availability_zones": [
                            {"zone": "eu-central-1a", "subnet_id": "subnet-0123456789abcdef0"},
                            {"zone": "eu-central-1b", "subnet_id": "subnet-0123456789abcdef1"},
                        ]
                    },
                },
            )
        )


class MalformedProviderResponseTests(FaultMatrixCase):
    """Partial DescribeInstances / template / Spot responses at the adapter edge."""

    def test_a_reservation_without_instances_refuses_the_inventory(self):
        # A reservation that carries no Instances key is a malformed response,
        # not a reservation with zero members: since the inventory-integrity
        # fix, the parser refuses rather than silently dropping the sibling
        # reservation (a partial inventory must never read as a complete one).
        aws = self.adapter(
            [
                {
                    "Reservations": [
                        {"ReservationId": "r-empty"},
                        {"ReservationId": "r-one", "Instances": [{"InstanceId": "i-1", "State": {"Name": "running"}}]},
                    ]
                }
            ]
        )
        with self.assertRaises(ProviderRefusal) as raised:
            aws.owned_instances("test-deployment")
        self.assertEqual(raised.exception.reason, "AWS_INVENTORY_INCOMPLETE")

        # An explicit empty Instances list is the valid "no members" shape.
        aws = self.adapter([{"Reservations": [{"ReservationId": "r-empty", "Instances": []}]}])
        self.assertEqual(aws.owned_instances("test-deployment"), [])

    def test_an_unknown_instance_state_is_never_terminal_evidence(self):
        # An unknown state is not a terminal state. The controller keeps the
        # slot occupied and does not terminate or replace the worker.
        operation_id = self.seed(state="running")
        controller = self.build(observation=observation_empty())
        self.aws.instances_result = [
            instance(instance_id="i-seed", operation=operation_id, state="windows-booting")
        ]
        result = controller.apply()
        self.assertEqual(result["decision"], "wait")
        self.assertIn("slot_busy", result["reasons"])
        self.assertEqual(self.reopened().operation(operation_id).state, "starting")
        self.assertEqual(self.aws.mutations(), [])
        self.assertEqual(result["slot"]["instances"], 1)

    def test_a_template_response_without_versions_is_refused(self):
        for response in ({"LaunchTemplateVersions": []}, {}):
            with self.subTest(response=response):
                aws = self.adapter([response])
                with self.assertRaises(ProviderRefusal) as raised:
                    aws.template("ci-burst-runner", 3)
                self.assertEqual(raised.exception.reason, "TEMPLATE_NOT_FOUND")

        # The same refusal reaches the controller as an unusable template, and
        # no instance request is made.
        self.adapter([{"Reservations": []}, {"LaunchTemplateVersions": []}])
        controller = self.build(
            observation=observation_demand(),
            aws=Aws(self.config, client=self.client, clock=self.clock, account="111122223333"),
        )
        result = controller.apply()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("template_unusable", result["reasons"])
        self.assertEqual(result["template_error"]["reason"], "TEMPLATE_NOT_FOUND")
        self.assertNotIn("run_instances", self.client.names())

    def test_an_empty_spot_history_is_unavailable_not_zero_price(self):
        aws = self.adapter([{"SpotPriceHistory": []}])
        self.assertIsNone(aws.spot_quote("t4g.small", "eu-central-1a"))

        # The controller refuses the plan; it never substitutes zero or an
        # unrelated price and never reaches a launch request.
        self.adapter([{"Reservations": []}, {"LaunchTemplateVersions": [launch_template()]}, {"SpotPriceHistory": []}])
        controller = self.build(
            observation=observation_demand(),
            aws=Aws(self.config, client=self.client, clock=self.clock, account="111122223333"),
        )
        result = controller.apply()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("spot_price_unavailable", result["reasons"])
        self.assertIsNone(result["candidates"][0]["price"])
        self.assertNotIn("run_instances", self.client.names())

    def test_a_wrong_scope_spot_record_is_not_substituted(self):
        for entry in (
            history(zone="eu-central-1b"),
            history(instance_type="m5.large"),
            history(product="Windows"),
        ):
            with self.subTest(entry=entry):
                aws = self.adapter([{"SpotPriceHistory": [entry]}])
                self.assertIsNone(aws.spot_quote("t4g.small", "eu-central-1a"))

        # A permissive fake can hand back a quote in another scope; the
        # controller reports the scope mismatch instead of pricing with it.
        wrong = quote(clock=self.clock, availability_zone="us-east-1a")
        result = self.build(observation=observation_demand(), aws=FakeAws(spot_quote=wrong)).plan()
        self.assertEqual(result["decision"], "refused")
        self.assertIn("spot_price_wrong_scope", result["reasons"])
        self.assertEqual(result["candidates"][0]["status"], "skipped")


class UncertainLaunchTests(FaultMatrixCase):
    """Every uncertain or definite outcome keeps or releases the operation by rule."""

    def test_a_transport_error_after_the_send_keeps_the_operation_and_commitment(self):
        controller = self.build(observation=observation_demand())
        self.aws.run_error = ProviderRefusal("AWS_UNCERTAIN", "ReadTimeout", uncertain=True)
        first = controller.apply()
        self.assertEqual(first["decision"], "uncertain")
        operation = self.reopened().operation(first["operation"])
        self.assertGreater(operation.reservation_micros, 0)
        self.assertEqual(len([call for call in self.aws.calls if call[0] == "run_spot"]), 1)
        self.assertEqual(self.totals(controller)["committed"], controller.cost.per_launch_micros())

        # A retry of the same intent is still not proof that nothing exists,
        # so the slot and the commitment stay occupied and only one operation
        # exists.
        second = controller.apply()
        self.assertEqual(second["decision"], "uncertain")
        self.assertEqual(second["operation"], operation.id)
        self.assertEqual(len(self.reopened().operations()), 1)
        self.assertEqual(self.totals(controller)["committed"], controller.cost.per_launch_micros())

    def test_a_definite_rejection_on_the_first_attempt_releases_the_slot(self):
        controller = self.build(observation=observation_demand())
        self.aws.run_error = ProviderRefusal("AWS_REFUSED", "UnauthorizedOperation")
        result = controller.apply()
        self.assertEqual(result["decision"], "launch_failed")
        operation = self.reopened().operation(result["operation"])
        self.assertEqual(operation.state, "failed")
        self.assertEqual(operation.reservation_micros, 0)
        self.assertEqual(self.totals(controller)["committed"], 0)

        # Nothing was created, so the next admission may use the free slot.
        self.aws.run_error = None
        again = self.build(observation=observation_demand(), aws=self.aws).apply()
        self.assertEqual(again["decision"], "launched")
        self.assertNotEqual(again["operation"], operation.id)

    def test_a_definite_rejection_on_a_retry_keeps_unresolved_ownership(self):
        controller = self.build(observation=observation_demand())
        self.aws.run_error = ProviderRefusal("AWS_UNCERTAIN", "ReadTimeout", uncertain=True)
        first = controller.apply()
        self.assertEqual(first["decision"], "uncertain")
        frozen = self.reopened().operation(first["operation"])

        # InvalidParameterValue is definite for a first attempt, but this is a
        # retry: rejecting it does not prove the original request created
        # nothing.
        self.aws.run_error = ProviderRefusal("AWS_REFUSED", "InvalidParameterValue")
        second = controller.apply()
        self.assertEqual(second["decision"], "uncertain")
        self.assertEqual(second["operation"], frozen.id)
        current = self.reopened().operation(frozen.id)
        self.assertEqual(current.state, "uncertain")
        self.assertEqual(current.reservation_micros, frozen.reservation_micros)
        self.assertEqual(current.client_token, frozen.client_token)
        self.assertEqual(current.request_json, frozen.request_json)
        self.assertEqual(current.request_sha256, frozen.request_sha256)
        sends = [call[1] for call in self.aws.calls if call[0] == "run_spot"]
        self.assertEqual(len(sends), 2)
        self.assertEqual(sends[0], sends[1])
        self.assertEqual(self.totals(controller)["committed"], controller.cost.per_launch_micros())

    def test_a_capacity_rejection_falls_back_within_one_operation(self):
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
        controller = self.build(observation=observation_demand(), aws=aws, config=config)
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        launch = result["launches"][0]
        self.assertEqual(launch["zone"], "eu-central-1b")
        self.assertEqual([attempt["outcome"] for attempt in launch["attempts"]], ["capacity_rejected", "accepted"])

        ledger = self.reopened()
        self.assertEqual(len(ledger.operations()), 1)
        operation = ledger.operation(launch["operation"])
        self.assertEqual(operation.state, "starting")
        self.assertEqual(operation.reservation_micros, controller.cost.per_launch_micros())
        self.assertEqual(operation.expires_at - operation.created_at, self.config.aws.max_lifetime_seconds)
        # The fallback replaces the frozen request and its own token together;
        # the operation, reservation and expiry stay the ones first admitted.
        self.assertEqual(operation.request()["Placement"]["AvailabilityZone"], "eu-central-1b")
        sends = [call[1] for call in self.aws.calls if call[0] == "run_spot"]
        self.assertEqual(len(sends), 2)
        self.assertEqual(operation.client_token, sends[1]["ClientToken"])
        self.assertNotEqual(sends[0]["ClientToken"], sends[1]["ClientToken"])

    def test_a_non_capacity_rejection_does_not_fall_back(self):
        config = self.zone_config(max_instances=1)
        quotes = {
            zone: quote(clock=self.clock, availability_zone=zone)
            for zone in ("eu-central-1a", "eu-central-1b")
        }
        aws = FakeAws(spot_quote=quotes)

        def reject_first(request):
            if request["Placement"]["AvailabilityZone"] == "eu-central-1a":
                raise ProviderRefusal("AWS_REFUSED", "UnauthorizedOperation")

        aws.on_run = reject_first
        controller = self.build(observation=observation_demand(), aws=aws, config=config)
        result = controller.apply()
        self.assertEqual(result["decision"], "launch_failed")
        sends = [call[1] for call in self.aws.calls if call[0] == "run_spot"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["Placement"]["AvailabilityZone"], "eu-central-1a")
        self.assertEqual(result["attempts"][0]["zone"], "eu-central-1a")
        self.assertEqual(result["attempts"][0]["outcome"], "rejected")
        self.assertEqual(len(self.aws.mutations()), 1)


class AccountingInvariantTests(FaultMatrixCase):
    """A seeded sequence: only the documented evidence releases ownership."""

    def test_unresolved_intent_keeps_slot_and_commitment_until_terminal_evidence(self):
        controller = self.build(observation=observation_demand())
        self.aws.run_error = ProviderRefusal("AWS_UNCERTAIN", "ReadTimeout", uncertain=True)
        first = controller.apply()
        self.assertEqual(first["decision"], "uncertain")
        operation_id = first["operation"]
        frozen = self.reopened().operation(operation_id)
        per_launch = controller.cost.per_launch_micros()

        # No provider evidence: the ledger, not elapsed time, owns the slot.
        status = controller.status()
        self.assertTrue(status["slot"]["busy"])
        self.assertEqual(status["slot"]["instances"], 1)
        self.assertEqual(status["budget"]["committed"], format_money(per_launch))
        plan = controller.plan()
        self.assertEqual(plan["decision"], "wait")
        self.assertIn("slot_busy", plan["reasons"])

        # Passing the admitted lifetime does not release it: the client token
        # is still valid and the request may have been accepted.
        self.clock.advance(3601)
        expired = controller.apply()
        self.assertEqual(expired["decision"], "refused")
        self.assertIn("unresolved_intent_lifetime_elapsed", expired["reasons"])
        self.assertEqual(self.reopened().operation(operation_id).state, "uncertain")
        self.assertEqual(self.reopened().operation(operation_id).reservation_micros, per_launch)

        # Passing the token window still does not release it; it needs an
        # operator, not replacement capacity.
        self.clock.advance(24 * 60 * 60)
        stale = controller.apply()
        self.assertEqual(stale["decision"], "refused")
        self.assertIn("unresolved_intent_expired", stale["reasons"])
        current = self.reopened().operation(operation_id)
        self.assertEqual(current.state, "uncertain")
        self.assertEqual(current.client_token, frozen.client_token)
        self.assertEqual(current.request_json, frozen.request_json)
        self.assertEqual(self.totals(controller)["committed"], per_launch)

        # Terminal provider evidence settles the operation and releases the
        # forward commitment. No new demand is present, so nothing is admitted.
        self.aws.run_error = None
        self.aws.instances_result = [instance(instance_id="i-late", operation=operation_id, state="terminated")]
        settled_controller = self.build(observation=observation_empty(), aws=self.aws)
        result = settled_controller.apply()
        self.assertEqual(result["decision"], "wait")
        operation = self.reopened().operation(operation_id)
        self.assertEqual(operation.state, "terminal")
        self.assertIsNotNone(operation.settled_micros)
        self.assertEqual(operation.instance_id, "i-late")
        self.assertEqual(self.totals(settled_controller)["committed"], 0)
        old_window = current_window(operation.created_at, self.config.budget.window_seconds)
        self.assertEqual(
            self.reopened().window_totals(old_window, settled_controller.cost, self.clock.now)["spend"],
            operation.settled_micros,
        )

    def test_absence_releases_only_after_the_retention_window(self):
        operation_id = self.seed(
            state="running", expires_at=self.clock.now + 3600, reservation=540_000, instance_id="i-seed"
        )
        controller = self.build(observation=observation_empty())
        self.aws.instances_by_id = {}
        self.aws.instances_result = []

        self.clock.advance(INSTANCE_RETENTION_SECONDS - 1)
        recent = controller.apply()
        self.assertEqual(recent["decision"], "wait")
        self.assertIn("slot_busy", recent["reasons"])
        self.assertEqual(self.reopened().operation(operation_id).state, "running")

        # The same absence is released only once the provider's retained
        # visibility window has elapsed: the provider may still have listed it.
        self.clock.advance(2)
        absent = controller.apply()
        self.assertEqual(absent["decision"], "wait")
        operation = self.reopened().operation(operation_id)
        self.assertEqual(operation.state, "terminal")
        self.assertEqual(operation.settled_micros, 40_000)
        totals = self.totals(controller)
        # The forward commitment is released; the settled spend stays counted.
        self.assertEqual(totals["committed"], totals["spend"])
        self.assertEqual(totals["spend"], 40_000)
        self.assertNotIn("expiry_cleanup", absent)
        self.assertEqual(self.aws.terminated, [])


class ConsoleWireShapeTests(FaultMatrixCase):
    """The adapter does not decode the console; botocore already did."""

    def test_a_plaintext_console_is_preserved_and_parsed(self):
        text = "STEP register\nSTEP job\nbootstrap exiting rc=0\n"
        aws = self.adapter([{"Output": text}])
        output = aws.console_output(["i-1"])["i-1"]
        self.assertEqual(output, text)
        facts = parse_console(output)
        self.assertEqual(facts.steps, ("register", "job"))
        self.assertEqual(facts.exit_code, 0)
        self.assertTrue(facts.registered)

    def test_a_base64_looking_console_is_not_decoded_a_second_time(self):
        decoded = "STEP register\n"
        encoded = base64.b64encode(decoded.encode("utf-8")).decode("ascii")
        aws = self.adapter([{"Output": encoded}])
        output = aws.console_output(["i-1"])["i-1"]
        self.assertEqual(output, encoded)
        self.assertNotEqual(output, decoded)
        self.assertEqual(parse_console(output).steps, ())
        self.assertFalse(parse_console(output).registered)

    def test_a_non_string_console_output_yields_no_evidence(self):
        for value in (1234, None, b"STEP register\n", {}, []):
            with self.subTest(value=value):
                aws = self.adapter([{"Output": value}])
                output = aws.console_output(["i-1"])["i-1"]
                self.assertEqual(output, "")
                facts = parse_console(output)
                self.assertEqual(facts.steps, ())
                self.assertEqual(facts.errors, ())
                self.assertIsNone(facts.exit_code)

    def test_a_missing_console_output_yields_no_evidence(self):
        aws = self.adapter([{}])
        self.assertEqual(aws.console_output(["i-1"]), {"i-1": ""})


class ProviderReadFailureTests(FaultMatrixCase):
    """A provider read that fails is never evidence that an operation ended."""

    def test_a_provider_read_failure_never_releases_another_operation(self):
        running = self.seed(state="running", operation_id="running")
        uncertain = self.seed(state="uncertain", operation_id="uncertain", instance_id=None)
        controller = self.build(observation=observation_empty())
        self.aws.instances_error = ProviderRefusal("AWS_UNCERTAIN", "ReadTimeout", uncertain=True)
        with self.assertRaises(ProviderRefusal) as raised:
            controller.apply()
        self.assertEqual(raised.exception.reason, "AWS_UNCERTAIN")
        ledger = self.reopened()
        self.assertEqual(ledger.operation(running).state, "running")
        self.assertEqual(ledger.operation(uncertain).state, "uncertain")
        self.assertEqual(ledger.operation(uncertain).reservation_micros, 1_000_000)

    def test_an_inventory_read_failure_never_admits_a_replacement(self):
        running = self.seed(state="running", operation_id="running")
        controller = self.build(observation=observation_demand())
        self.aws.owned_instances_error = ProviderRefusal("AWS_UNCERTAIN", "ReadTimeout", uncertain=True)
        with self.assertRaises(ProviderRefusal):
            controller.apply()
        self.assertEqual(self.reopened().operation(running).state, "running")
        self.assertEqual(
            [call for call in self.aws.calls if call[0] == "run_spot"],
            [],
        )


if __name__ == "__main__":
    unittest.main()
