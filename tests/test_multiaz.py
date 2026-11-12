"""Ordered availability-zone fallback exercised with mocked providers.

These tests cover the placement contract only: every configured candidate is
priced, the first eligible one is selected, and only a definite capacity
rejection advances the same admission to the next candidate in order. No test
here uses a network, a cloud account or a Forgejo token.
"""

import json
import tempfile
import unittest
from pathlib import Path

from ci_burst.aws import SpotQuote
from ci_burst.config import load
from ci_burst.controller import Controller
from ci_burst.errors import ConfigRefusal, ProviderRefusal
from ci_burst.ledger import Ledger, current_window

from support import Clock, FakeAws, FakeObserver, observation_demand, template

CANDIDATES = (
    {"zone": "eu-central-1a", "subnet_id": "subnet-0123456789abcdef0"},
    {"zone": "eu-central-1b", "subnet_id": "subnet-0123456789abcdef1"},
)


def deployment_toml(candidates=CANDIDATES, *, aws=None, max_instances=None):
    """Render a deployment configuration with an ordered candidate list."""
    aws_values = {
        "region": "eu-central-1",
        "launch_template_name": "ci-burst-runner",
        "launch_template_version": 3,
        "max_spot_price": "0.0400",
        "max_lifetime_seconds": 3600,
    }
    aws_values.update(aws or {})
    lines = [
        'deployment = "test-deployment"',
        "",
        "[forgejo]",
        'base_url = "https://forgejo.example"',
        'repository = "owner/repo"',
        'token_env = "CI_BURST_TEST_TOKEN"',
        'contract = "forgejo-17.0.1-runners-jobs"',
        'qualification = "live-verified"',
        "",
        "[worker]",
        'labels = ["ci-burst:host", "homelab:host"]',
        "",
    ]
    if max_instances is not None:
        lines += ["[controller]", f"max_instances = {max_instances}", ""]
    lines += [
        "[budget]",
        "window_seconds = 86400",
        'allowance = "10.00"',
        'non_compute_allowance = "0.50"',
        "",
        "[state]",
        'database = "state.sqlite3"',
        "",
        "[aws]",
    ]
    for key, value in aws_values.items():
        lines.append(f"{key} = {_toml(value)}")
    lines.append("")
    for candidate in candidates or ():
        lines.append("[[aws.availability_zones]]")
        for key, value in candidate.items():
            lines.append(f"{key} = {_toml(value)}")
        lines.append("")
    return "\n".join(lines)


def _toml(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    return json.dumps(value)


def zone_quote(zone, price_micros, clock, timestamp=None, observed_at=None, instance_type="t4g.small"):
    observed = clock.now if observed_at is None else observed_at
    return SpotQuote(
        price_micros=price_micros,
        timestamp=observed if timestamp is None else timestamp,
        price_display=f"{price_micros / 1_000_000:.6f}",
        instance_type=instance_type,
        availability_zone=zone,
        observed_at=observed,
    )


class MultiZoneAws(FakeAws):
    """FakeAws that answers per zone and returns version-accurate templates."""

    def __init__(self, *, quotes=None, run_errors=None, **kwargs):
        super().__init__(**kwargs)
        self.quotes = dict(quotes or {})
        self.run_errors = dict(run_errors or {})

    def template(self, name, version):
        self.calls.append(("template", name, version))
        return template(version=version)

    def spot_quote(self, instance_type, availability_zone, *, deadline=None):
        self.calls.append(("spot_quote", instance_type, availability_zone))
        value = self.quotes.get(
            (instance_type, availability_zone), self.quotes.get(availability_zone)
        )
        if isinstance(value, Exception):
            raise value
        return value

    def run_spot(self, request):
        self.calls.append(("run_spot", json.loads(json.dumps(request))))
        if self.on_run is not None:
            self.on_run(request)
        placement = request["Placement"]["AvailabilityZone"]
        error = self.run_errors.get(
            (request.get("InstanceType"), placement), self.run_errors.get(placement)
        )
        if isinstance(error, list):
            error = error.pop(0) if error else None
        if error is not None:
            raise error
        return self.next_instance()


class MultiZoneCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.clock = Clock()

    def config(self, candidates=CANDIDATES, **kwargs):
        path = self.root / "deployment.toml"
        path.write_text(deployment_toml(candidates, **kwargs), encoding="utf-8")
        return load(path)

    def build(
        self, *, config=None, quotes=None, run_errors=None, observation=None, aws_class=MultiZoneAws
    ):
        self.config_value = config or self.config()
        self.aws = aws_class(
            quotes=quotes
            if quotes is not None
            else {
                "eu-central-1a": zone_quote("eu-central-1a", 20_000, self.clock),
                "eu-central-1b": zone_quote("eu-central-1b", 30_000, self.clock),
            },
            run_errors=run_errors,
        )
        return Controller(
            self.config_value,
            observer=FakeObserver(observation or observation_demand()),
            aws=self.aws,
            ledger=Ledger(self.config_value.state.database, clock=self.clock),
            clock=self.clock,
        )

    def reopened(self):
        ledger = Ledger(self.config_value.state.database, clock=self.clock).connect()
        self.addCleanup(ledger.close)
        return ledger

    def run_requests(self):
        return [call[1] for call in self.aws.calls if call[0] == "run_spot"]


class PlanTests(MultiZoneCase):
    def test_plan_reports_ordered_candidates_prices_and_the_selection(self):
        config = self.config(
            candidates=(
                {"zone": "eu-central-1a", "subnet_id": "subnet-0123456789abcdef0"},
                {"zone": "eu-central-1b", "subnet_id": "subnet-0123456789abcdef1"},
                {"zone": "eu-central-1c", "subnet_id": "subnet-0123456789abcdef2"},
            )
        )
        controller = self.build(
            config=config,
            quotes={
                "eu-central-1a": zone_quote("eu-central-1a", 20_000, self.clock),
                "eu-central-1b": zone_quote("eu-central-1b", 50_000, self.clock),
                "eu-central-1c": zone_quote("eu-central-1c", 30_000, self.clock),
            },
        )

        result = controller.plan()

        self.assertEqual(result["decision"], "would_launch")
        self.assertEqual(
            [item["zone"] for item in result["candidates"]],
            ["eu-central-1a", "eu-central-1b", "eu-central-1c"],
        )
        self.assertEqual(
            [item["status"] for item in result["candidates"]],
            ["eligible", "skipped", "eligible"],
        )
        self.assertEqual(result["candidates"][1]["reason"], "spot_price_above_cap")
        self.assertEqual(result["candidates"][0]["price"]["price_display"], "0.020000")
        self.assertEqual(result["candidates"][2]["price"]["price_display"], "0.030000")
        self.assertEqual(result["selected"]["zone"], "eu-central-1a")
        self.assertEqual(result["selected"]["subnet_id"], "subnet-0123456789abcdef0")
        self.assertEqual(result["selected"]["launch_template_version"], 3)
        self.assertEqual(result["price"]["availability_zone"], "eu-central-1a")
        self.assertEqual(result["price"]["cap"], "0.040000")
        self.assertEqual(result["budget"]["required_reservation"], "0.540000")
        self.assertEqual(
            [call[2] for call in self.aws.calls if call[0] == "spot_quote"],
            ["eu-central-1a", "eu-central-1b", "eu-central-1c"],
        )
        self.assertEqual(self.aws.mutations(), [])
        self.assertFalse(self.config_value.state.database.exists())

    def test_plan_selects_the_first_eligible_candidate_in_order(self):
        controller = self.build(
            quotes={
                "eu-central-1a": zone_quote("eu-central-1a", 50_000, self.clock),
                "eu-central-1b": zone_quote("eu-central-1b", 30_000, self.clock),
            }
        )

        result = controller.plan()

        self.assertEqual(result["decision"], "would_launch")
        self.assertEqual(result["candidates"][0]["reason"], "spot_price_above_cap")
        self.assertEqual(result["selected"]["zone"], "eu-central-1b")
        self.assertEqual(result["price"]["availability_zone"], "eu-central-1b")

    def test_plan_skips_stale_missing_and_wrong_scope_quotes(self):
        config = self.config(
            candidates=(
                {"zone": "eu-central-1a", "subnet_id": "subnet-0123456789abcdef0"},
                {"zone": "eu-central-1b", "subnet_id": "subnet-0123456789abcdef1"},
                {"zone": "eu-central-1c", "subnet_id": "subnet-0123456789abcdef2"},
                {"zone": "eu-central-1d", "subnet_id": "subnet-0123456789abcdef3"},
            )
        )
        controller = self.build(
            config=config,
            quotes={
                "eu-central-1a": zone_quote("eu-central-1a", 20_000, self.clock, observed_at=self.clock.now - 901),
                "eu-central-1b": None,
                "eu-central-1c": zone_quote("eu-central-1d", 20_000, self.clock),
                "eu-central-1d": zone_quote("eu-central-1d", 30_000, self.clock),
            },
        )

        result = controller.plan()

        self.assertEqual(result["decision"], "would_launch")
        self.assertEqual(
            [item.get("reason") for item in result["candidates"]],
            ["spot_price_stale", "spot_price_unavailable", "spot_price_wrong_scope", None],
        )
        self.assertEqual(result["selected"]["zone"], "eu-central-1d")
        self.assertEqual(result["price"]["availability_zone"], "eu-central-1d")

    def test_plan_uses_the_launch_template_version_pinned_by_a_candidate(self):
        config = self.config(
            candidates=(
                {"zone": "eu-central-1a", "subnet_id": "subnet-0123456789abcdef0"},
                {"zone": "eu-central-1b", "subnet_id": "subnet-0123456789abcdef1", "launch_template_version": 4},
            )
        )

        result = self.build(config=config).plan()

        self.assertIn(("template", "ci-burst-runner", 3), self.aws.calls)
        self.assertIn(("template", "ci-burst-runner", 4), self.aws.calls)
        self.assertEqual(result["candidates"][1]["launch_template_version"], 4)

    def test_a_price_read_failure_refuses_instead_of_skipping(self):
        controller = self.build(
            quotes={
                "eu-central-1a": ProviderRefusal("AWS_REFUSED", "AccessDenied"),
                "eu-central-1b": zone_quote("eu-central-1b", 30_000, self.clock),
            }
        )

        result = controller.plan()

        self.assertEqual(result["decision"], "refused")
        self.assertIn("spot_price_unavailable", result["reasons"])
        self.assertEqual(result["refusal"], {"reason": "AWS_REFUSED", "detail": "AccessDenied"})
        # Price evidence is unavailable for the whole decision, so no later
        # candidate is silently selected.
        self.assertEqual(
            [call[2] for call in self.aws.calls if call[0] == "spot_quote"],
            ["eu-central-1a"],
        )

    def test_plan_refuses_when_no_candidate_is_eligible(self):
        controller = self.build(
            quotes={
                "eu-central-1a": zone_quote("eu-central-1a", 50_000, self.clock),
                "eu-central-1b": zone_quote("eu-central-1b", 60_000, self.clock),
            }
        )

        result = controller.plan()

        self.assertEqual(result["decision"], "refused")
        self.assertIn("spot_price_above_cap", result["reasons"])
        self.assertIn("no_eligible_candidate", result["reasons"])
        self.assertNotIn("selected", result)
        self.assertEqual(self.aws.mutations(), [])


class LaunchFallbackTests(MultiZoneCase):
    def test_a_capacity_rejection_launches_the_next_candidate_once(self):
        config = self.config(
            candidates=(
                {"zone": "eu-central-1a", "subnet_id": "subnet-0123456789abcdef0"},
                {"zone": "eu-central-1b", "subnet_id": "subnet-0123456789abcdef1", "launch_template_version": 4},
            )
        )
        controller = self.build(
            config=config,
            run_errors={
                "eu-central-1a": ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity"),
            },
        )

        result = controller.apply()

        self.assertEqual(result["decision"], "launched")
        self.assertEqual([item["zone"] for item in result["attempts"]], ["eu-central-1a", "eu-central-1b"])
        self.assertEqual(result["attempts"][0]["outcome"], "capacity_rejected")
        self.assertEqual(result["attempts"][0]["detail"], "InsufficientInstanceCapacity")
        self.assertEqual(result["attempts"][1]["outcome"], "accepted")
        runs = self.run_requests()
        self.assertEqual(len(runs), 2)
        self.assertEqual(
            [run["Placement"] for run in runs],
            [
                {"AvailabilityZone": "eu-central-1a"},
                {"AvailabilityZone": "eu-central-1b"},
            ],
        )
        # RunInstances takes the subnet at the top level, not inside Placement.
        self.assertEqual(
            [run["SubnetId"] for run in runs],
            ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"],
        )
        self.assertEqual(runs[1]["LaunchTemplate"]["Version"], "4")
        self.assertEqual((runs[0]["MinCount"], runs[0]["MaxCount"]), (1, 1))
        # A different placement needs its own idempotency token; reusing one
        # across changed parameters is what EC2 refuses.
        self.assertNotEqual(runs[0]["ClientToken"], runs[1]["ClientToken"])
        ledger = self.reopened()
        operations = ledger.operations()
        self.assertEqual(len(operations), 1)
        operation = operations[0]
        self.assertEqual(operation.state, "starting")
        self.assertEqual(operation.instance_id, "i-0123456789abcdef0")
        self.assertEqual(operation.request()["Placement"]["AvailabilityZone"], "eu-central-1b")
        self.assertEqual(operation.client_token, runs[1]["ClientToken"])
        # One admission: one forward commitment, counted once.
        self.assertEqual(operation.reservation_micros, 540_000)
        window = current_window(self.clock.now, config.budget.window_seconds)
        self.assertEqual(ledger.window_totals(window, controller.cost, self.clock.now)["committed"], 539_834)

    def test_each_candidate_is_attempted_at_most_once_in_order(self):
        config = self.config(
            candidates=(
                {"zone": "eu-central-1a", "subnet_id": "subnet-0123456789abcdef0"},
                {"zone": "eu-central-1b", "subnet_id": "subnet-0123456789abcdef1"},
                {"zone": "eu-central-1c", "subnet_id": "subnet-0123456789abcdef2"},
            )
        )
        controller = self.build(
            config=config,
            quotes={
                "eu-central-1a": zone_quote("eu-central-1a", 20_000, self.clock),
                "eu-central-1b": zone_quote("eu-central-1b", 20_000, self.clock),
                "eu-central-1c": zone_quote("eu-central-1c", 20_000, self.clock),
            },
            run_errors={
                "eu-central-1a": ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity"),
                "eu-central-1b": ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity"),
            },
        )

        result = controller.apply()

        self.assertEqual(result["decision"], "launched")
        self.assertEqual(
            [item["zone"] for item in result["attempts"]],
            ["eu-central-1a", "eu-central-1b", "eu-central-1c"],
        )
        self.assertEqual(
            [run["Placement"]["AvailabilityZone"] for run in self.run_requests()],
            ["eu-central-1a", "eu-central-1b", "eu-central-1c"],
        )
        self.assertEqual(len(self.reopened().operations()), 1)

    def test_resume_retries_the_candidate_the_intent_advanced_to(self):
        controller = self.build(
            run_errors={
                "eu-central-1a": ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity"),
                "eu-central-1b": ProviderRefusal("AWS_UNCERTAIN", "ReadTimeout", uncertain=True),
            }
        )

        first = controller.apply()

        self.assertEqual(first["decision"], "uncertain")
        self.assertEqual([item["zone"] for item in first["attempts"]], ["eu-central-1a", "eu-central-1b"])
        first_run = self.run_requests()[-1]
        # A fresh controller and ledger model a restarted process.
        second = self.build().apply()
        self.assertEqual(second["decision"], "resumed")
        resumed_run = self.run_requests()[-1]
        self.assertEqual(resumed_run["Placement"]["AvailabilityZone"], "eu-central-1b")
        self.assertEqual(resumed_run["ClientToken"], first_run["ClientToken"])
        self.assertEqual(second["operation"], first["operation"])
        self.assertEqual(len(self.reopened().operations()), 1)

    def test_a_fallback_candidate_is_not_sent_on_stale_price_evidence(self):
        config = self.config(
            candidates=(
                {"zone": "eu-central-1a", "subnet_id": "subnet-0123456789abcdef0"},
                {"zone": "eu-central-1b", "subnet_id": "subnet-0123456789abcdef1"},
            )
        )
        controller = self.build(
            config=config,
            quotes={
                "eu-central-1a": zone_quote("eu-central-1a", 20_000, self.clock),
                # 899 s old at admission: still eligible. Freshness is checked
                # again before this candidate is frozen and sent.
                "eu-central-1b": zone_quote(
                    "eu-central-1b", 30_000, self.clock, observed_at=self.clock.now - 899
                ),
            },
            run_errors={
                "eu-central-1a": ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity"),
            },
        )
        self.aws.on_run = lambda request: self.clock.advance(2)

        result = controller.apply()

        self.assertEqual(result["decision"], "refused")
        self.assertIn("spot_price_stale", result["reasons"])
        self.assertEqual(len(self.run_requests()), 1)
        self.assertEqual(
            [(item["zone"], item["outcome"]) for item in result["attempts"]],
            [("eu-central-1a", "capacity_rejected"), ("eu-central-1b", "skipped")],
        )
        self.assertEqual(result["attempts"][1]["reason"], "spot_price_stale")
        operations = self.reopened().operations()
        # One admission only, and the slot is released rather than left with an
        # intent that could only be sent on stale evidence.
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0].state, "failed")
        self.assertIsNone(operations[0].instance_id)
        self.assertEqual(operations[0].request()["Placement"]["AvailabilityZone"], "eu-central-1a")
        # Nothing was created, so the admission releases its allowance instead
        # of charging the window for an attempt that never reached the provider.
        window = current_window(self.clock.now, controller.config.budget.window_seconds)
        totals = self.reopened().window_totals(window, controller.cost, self.clock.now)
        self.assertEqual(totals["committed"], 0)

    def test_an_uncertain_first_attempt_does_not_fall_through(self):
        controller = self.build(
            run_errors={
                "eu-central-1a": ProviderRefusal("AWS_UNCERTAIN", "ReadTimeout", uncertain=True),
            }
        )

        result = controller.apply()

        self.assertEqual(result["decision"], "uncertain")
        self.assertEqual(len(self.run_requests()), 1)
        self.assertEqual(
            result["attempts"],
            [
                {
                    "zone": "eu-central-1a",
                    "instance_type": "t4g.small",
                    "outcome": "uncertain",
                    "reason": "AWS_UNCERTAIN",
                    "detail": "ReadTimeout",
                }
            ],
        )
        self.assertNotIn("capacity_exhausted", result["reasons"])
        operation = self.reopened().operations()[0]
        self.assertEqual(operation.state, "uncertain")
        self.assertIsNone(operation.instance_id)

    def test_a_non_capacity_rejection_does_not_fall_through(self):
        controller = self.build(
            run_errors={
                "eu-central-1a": ProviderRefusal("AWS_REFUSED", "UnauthorizedOperation"),
            }
        )

        result = controller.apply()

        self.assertEqual(result["decision"], "launch_failed")
        self.assertEqual(len(self.run_requests()), 1)
        self.assertEqual(
            result["attempts"],
            [
                {
                    "zone": "eu-central-1a",
                    "instance_type": "t4g.small",
                    "outcome": "rejected",
                    "reason": "AWS_REFUSED",
                    "detail": "UnauthorizedOperation",
                }
            ],
        )
        self.assertEqual(result["refusal"], {"reason": "AWS_REFUSED", "detail": "UnauthorizedOperation"})
        self.assertNotIn("capacity_exhausted", result["reasons"])
        operation = self.reopened().operations()[0]
        self.assertEqual(operation.state, "failed")
        self.assertIsNone(operation.instance_id)

    def test_exhausting_every_candidate_leaves_the_slot_free(self):
        controller = self.build(
            run_errors={
                "eu-central-1a": ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity"),
                "eu-central-1b": ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity"),
            }
        )

        result = controller.apply()

        self.assertEqual(result["decision"], "launch_failed")
        self.assertIn("capacity_exhausted", result["reasons"])
        self.assertEqual(
            [item["zone"] for item in result["attempts"]],
            ["eu-central-1a", "eu-central-1b"],
        )
        self.assertEqual(
            [item["outcome"] for item in result["attempts"]],
            ["capacity_rejected", "capacity_rejected"],
        )
        self.assertNotIn("instance_id", result)
        self.assertEqual(len(self.run_requests()), 2)
        operation = self.reopened().operations()[0]
        self.assertEqual(operation.state, "failed")
        self.assertIsNone(operation.instance_id)
        # The definite rejections created nothing, so the slot is free.
        self.assertFalse(controller.status()["slot"]["busy"])


class ConfigRefusalTests(MultiZoneCase):
    def assert_refused(self, text, contains):
        path = self.root / "deployment.toml"
        path.write_text(text, encoding="utf-8")
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertEqual(raised.exception.reason, "ZONE_CANDIDATES_INVALID")
        self.assertIn(contains, raised.exception.detail)

    def test_empty_candidate_list_is_refused(self):
        self.assert_refused(
            deployment_toml(candidates=None, aws={"availability_zones": []}),
            "at least one candidate",
        )

    def test_duplicate_zone_is_refused(self):
        self.assert_refused(
            deployment_toml(
                candidates=(
                    {"zone": "eu-central-1a", "subnet_id": "subnet-0123456789abcdef0"},
                    {"zone": "eu-central-1a", "subnet_id": "subnet-0123456789abcdef1"},
                )
            ),
            "unique",
        )

    def test_candidate_without_a_subnet_is_refused(self):
        self.assert_refused(
            deployment_toml(candidates=({"zone": "eu-central-1a"},)),
            "subnet_id",
        )

    def test_malformed_subnet_is_refused(self):
        self.assert_refused(
            deployment_toml(candidates=({"zone": "eu-central-1a", "subnet_id": "subnet-xyz"},)),
            "subnet_id",
        )

    def test_both_supported_subnet_identifier_lengths_load(self):
        config = self.config(
            candidates=(
                {"zone": "eu-central-1a", "subnet_id": "subnet-0123456a"},
                {"zone": "eu-central-1b", "subnet_id": "subnet-0123456789abcdeff"},
            )
        )

        self.assertEqual(
            [candidate.subnet_id for candidate in config.aws.zones],
            ["subnet-0123456a", "subnet-0123456789abcdeff"],
        )

    def test_unsupported_subnet_identifier_lengths_are_refused(self):
        for length in range(9, 17):
            with self.subTest(length=length):
                self.assert_refused(
                    deployment_toml(
                        candidates=({"zone": "eu-central-1a", "subnet_id": "subnet-" + "a" * length},)
                    ),
                    "8 or 17 hexadecimal characters",
                )

    def test_unknown_candidate_key_is_refused(self):
        self.assert_refused(
            deployment_toml(
                candidates=(
                    {"zone": "eu-central-1a", "subnet_id": "subnet-0123456789abcdef0", "subnet": "subnet-1"},
                )
            ),
            "unknown key",
        )

    def test_missing_zone_is_refused(self):
        self.assert_refused(
            deployment_toml(candidates=({"subnet_id": "subnet-0123456789abcdef0"},)),
            ".zone",
        )

    def test_zone_outside_the_region_is_refused(self):
        self.assert_refused(
            deployment_toml(candidates=({"zone": "us-east-1a", "subnet_id": "subnet-0123456789abcdef0"},)),
            "configured Region",
        )

    def test_single_zone_and_candidate_list_together_are_refused(self):
        self.assert_refused(
            deployment_toml(aws={"availability_zone": "eu-central-1a"}),
            "mutually exclusive",
        )

    def test_non_table_candidate_is_refused(self):
        self.assert_refused(
            deployment_toml(candidates=None, aws={"availability_zones": ["eu-central-1a"]}),
            "must be a table",
        )

    def test_template_version_alias_in_a_candidate_is_refused(self):
        self.assert_refused(
            deployment_toml(
                candidates=(
                    {
                        "zone": "eu-central-1a",
                        "subnet_id": "subnet-0123456789abcdef0",
                        "launch_template_version": "$Latest",
                    },
                )
            ),
            "launch_template_version",
        )


class SingleZoneCompatibilityTests(MultiZoneCase):
    def test_the_single_zone_key_loads_as_one_candidate_without_a_subnet(self):
        config = self.config(candidates=None, aws={"availability_zone": "eu-central-1a"})

        self.assertEqual(len(config.aws.zones), 1)
        candidate = config.aws.zones[0]
        self.assertEqual(candidate.availability_zone, "eu-central-1a")
        self.assertIsNone(candidate.subnet_id)
        self.assertEqual(candidate.launch_template_version, 3)

    def test_a_single_zone_launch_sends_no_subnet_override(self):
        config = self.config(candidates=None, aws={"availability_zone": "eu-central-1a"})
        controller = self.build(
            config=config,
            quotes={"eu-central-1a": zone_quote("eu-central-1a", 20_000, self.clock)},
        )

        result = controller.apply()

        self.assertEqual(result["decision"], "launched")
        request = self.run_requests()[0]
        self.assertEqual(request["Placement"], {"AvailabilityZone": "eu-central-1a"})
        self.assertNotIn("SubnetId", request)


class MixedTemplateVersionAws(MultiZoneAws):
    """Two candidates whose template versions pin different instance types."""

    TYPES_BY_VERSION = {3: "t4g.small", 4: "c7g.large"}

    def template(self, name, version):
        self.calls.append(("template", name, version))
        return template(version=version, instance_type=self.TYPES_BY_VERSION[version])


class NoTypeListTests(MultiZoneCase):
    """Without a configured list the candidate types come from the templates."""

    CANDIDATES = (
        {"zone": "eu-central-1a", "subnet_id": "subnet-0123456789abcdef0"},
        {
            "zone": "eu-central-1b",
            "subnet_id": "subnet-0123456789abcdef1",
            "launch_template_version": 4,
        },
    )

    def test_batch_rotation_still_alternates_across_differing_template_types(self):
        config = self.config(candidates=self.CANDIDATES, max_instances=2)
        quotes = {
            ("t4g.small", "eu-central-1a"): zone_quote(
                "eu-central-1a", 20_000, self.clock, instance_type="t4g.small"
            ),
            ("c7g.large", "eu-central-1b"): zone_quote(
                "eu-central-1b", 20_000, self.clock, instance_type="c7g.large"
            ),
        }
        controller = self.build(
            config=config,
            quotes=quotes,
            observation=observation_demand(2),
            aws_class=MixedTemplateVersionAws,
        )
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        self.assertEqual(
            [(launch["zone"], launch["instance_type"]) for launch in result["launches"]],
            [("eu-central-1a", "t4g.small"), ("eu-central-1b", "c7g.large")],
        )


class PoolTests(MultiZoneCase):
    """One ordered type list multiplying the configured zones into pools."""

    TYPES = ("t4g.small", "c7g.large")
    ZONES = ("eu-central-1a", "eu-central-1b")

    def typed_config(self):
        return self.config(aws={"instance_types": list(self.TYPES)})

    def pool_quotes(self, prices=None):
        prices = prices or {}
        return {
            (instance_type, zone): zone_quote(
                zone,
                prices.get((instance_type, zone), 20_000),
                self.clock,
                instance_type=instance_type,
            )
            for instance_type in self.TYPES
            for zone in self.ZONES
        }

    def test_pools_expand_type_major_with_one_price_each(self):
        controller = self.build(config=self.typed_config(), quotes=self.pool_quotes())
        result = controller.plan()
        self.assertEqual(result["decision"], "would_launch")
        self.assertEqual(
            [(candidate["instance_type"], candidate["zone"]) for candidate in result["candidates"]],
            [
                ("t4g.small", "eu-central-1a"),
                ("t4g.small", "eu-central-1b"),
                ("c7g.large", "eu-central-1a"),
                ("c7g.large", "eu-central-1b"),
            ],
        )
        self.assertEqual(
            [(candidate["instance_type"], candidate["zone"]) for candidate in result["candidates"]],
            [(call[1], call[2]) for call in self.aws.calls if call[0] == "spot_quote"],
        )
        self.assertEqual(result["selected"]["instance_type"], "t4g.small")
        self.assertEqual(result["selected"]["zone"], "eu-central-1a")

    def test_a_type_above_the_cap_is_skipped_while_the_next_pool_is_eligible(self):
        quotes = self.pool_quotes(
            {
                ("t4g.small", "eu-central-1a"): 50_000,
                ("t4g.small", "eu-central-1b"): 50_000,
            }
        )
        controller = self.build(config=self.typed_config(), quotes=quotes)
        result = controller.plan()
        self.assertEqual(result["decision"], "would_launch")
        self.assertEqual(
            [candidate.get("reason") for candidate in result["candidates"][:2]],
            ["spot_price_above_cap", "spot_price_above_cap"],
        )
        self.assertEqual(result["selected"]["instance_type"], "c7g.large")
        self.assertEqual(result["selected"]["zone"], "eu-central-1a")

    def test_a_capacity_rejection_advances_across_types(self):
        capacity = ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity")
        controller = self.build(
            config=self.typed_config(),
            quotes=self.pool_quotes(),
            run_errors={
                ("t4g.small", "eu-central-1a"): capacity,
                ("t4g.small", "eu-central-1b"): capacity,
            },
        )
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        launch = result["launches"][0]
        self.assertEqual((launch["zone"], launch["instance_type"]), ("eu-central-1a", "c7g.large"))
        self.assertEqual(
            [
                (attempt["zone"], attempt["instance_type"], attempt["outcome"])
                for attempt in launch["attempts"]
            ],
            [
                ("eu-central-1a", "t4g.small", "capacity_rejected"),
                ("eu-central-1b", "t4g.small", "capacity_rejected"),
                ("eu-central-1a", "c7g.large", "accepted"),
            ],
        )
        ledger = self.reopened()
        self.assertEqual(len(ledger.operations()), 1)
        request = ledger.operation(result["operation"]).request()
        self.assertEqual(request["InstanceType"], "c7g.large")
        self.assertEqual(request["Placement"]["AvailabilityZone"], "eu-central-1a")

    def test_every_pool_refusing_leaves_the_slot_free(self):
        capacity = ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity")
        controller = self.build(
            config=self.typed_config(),
            quotes=self.pool_quotes(),
            run_errors={(instance_type, zone): capacity for instance_type in self.TYPES for zone in self.ZONES},
        )
        result = controller.apply()
        self.assertEqual(result["decision"], "launch_failed")
        self.assertIn("capacity_exhausted", result["reasons"])
        self.assertEqual(
            len([call for call in self.aws.calls if call[0] == "run_spot"]), 4
        )
        ledger = self.reopened()
        self.assertEqual([operation.state for operation in ledger.operations()], ["failed"])

    def test_a_capacity_rejection_falls_back_within_the_preferred_type(self):
        capacity = ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity")
        config = self.config(aws={"instance_types": list(self.TYPES)}, max_instances=2)
        controller = self.build(
            config=config,
            quotes=self.pool_quotes(),
            run_errors={("t4g.small", "eu-central-1b"): capacity},
            observation=observation_demand(2),
        )
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        second = result["launches"][1]
        self.assertEqual((second["zone"], second["instance_type"]), ("eu-central-1a", "t4g.small"))
        self.assertEqual(
            [
                (attempt["zone"], attempt["instance_type"], attempt["outcome"])
                for attempt in second["attempts"]
            ],
            [
                ("eu-central-1b", "t4g.small", "capacity_rejected"),
                ("eu-central-1a", "t4g.small", "accepted"),
            ],
        )

    def test_evidence_expiring_mid_batch_stays_within_the_preferred_type(self):
        config = self.config(aws={"instance_types": list(self.TYPES)}, max_instances=2)
        quotes = self.pool_quotes()
        # Fresh at admission, stale by the time the rotated worker is sent: the
        # first launch advances the clock while the request is in flight.
        quotes[("t4g.small", "eu-central-1b")] = zone_quote(
            "eu-central-1b",
            20_000,
            self.clock,
            observed_at=self.clock.now - 850,
            instance_type="t4g.small",
        )
        controller = self.build(config=config, quotes=quotes, observation=observation_demand(2))
        self.aws.on_run = lambda request: self.clock.advance(100)
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        second = result["launches"][1]
        self.assertEqual((second["zone"], second["instance_type"]), ("eu-central-1a", "t4g.small"))
        self.assertEqual(
            [
                (attempt["zone"], attempt["instance_type"], attempt["outcome"])
                for attempt in second["attempts"]
            ],
            [
                ("eu-central-1b", "t4g.small", "skipped"),
                ("eu-central-1a", "t4g.small", "accepted"),
            ],
        )

    def test_expiring_evidence_does_not_start_a_worker_on_a_fallback_type(self):
        config = self.config(aws={"instance_types": list(self.TYPES)}, max_instances=2)
        quotes = self.pool_quotes()
        for zone in self.ZONES:
            quotes[("t4g.small", zone)] = zone_quote(
                zone,
                20_000,
                self.clock,
                observed_at=self.clock.now - 850,
                instance_type="t4g.small",
            )
        controller = self.build(config=config, quotes=quotes, observation=observation_demand(2))
        self.aws.on_run = lambda request: self.clock.advance(100)
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        self.assertEqual(len(result["launches"]), 1)
        self.assertIn("spot_price_stale", result["reasons"])
        self.assertEqual(
            [request["InstanceType"] for request in self.run_requests()], ["t4g.small"]
        )

    def test_the_fallback_detail_names_the_pool_it_left_not_a_stale_one(self):
        capacity = ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity")
        quotes = self.pool_quotes()
        quotes[("t4g.small", "eu-central-1b")] = zone_quote(
            "eu-central-1b",
            20_000,
            self.clock,
            observed_at=self.clock.now - 850,
            instance_type="t4g.small",
        )
        controller = self.build(
            config=self.config(aws={"instance_types": list(self.TYPES)}),
            quotes=quotes,
            run_errors={("t4g.small", "eu-central-1a"): capacity},
        )
        self.aws.on_run = lambda request: self.clock.advance(100)
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        self.assertEqual(
            [
                (attempt["zone"], attempt["instance_type"], attempt["outcome"])
                for attempt in result["launches"][0]["attempts"]
            ],
            [
                ("eu-central-1a", "t4g.small", "capacity_rejected"),
                ("eu-central-1b", "t4g.small", "skipped"),
                ("eu-central-1a", "c7g.large", "accepted"),
            ],
        )
        operation = self.reopened().operation(result["operation"])
        self.assertEqual(operation.detail, "capacity_fallback:eu-central-1a/t4g.small")

    def test_a_single_type_list_still_sends_the_type_explicitly(self):
        config = self.config(aws={"instance_types": ["t4g.small"]})
        controller = self.build(
            config=config,
            quotes={("t4g.small", zone): zone_quote(zone, 20_000, self.clock) for zone in self.ZONES},
        )
        result = controller.apply()
        self.assertEqual(result["decision"], "launched")
        self.assertEqual(self.run_requests()[0]["InstanceType"], "t4g.small")


if __name__ == "__main__":
    unittest.main()
