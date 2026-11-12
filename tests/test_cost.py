import unittest

from ci_burst.cost import MINIMUM_BILLABLE_SECONDS, CostModel
from ci_burst.ledger import Operation

CAP = 200_000
INCIDENTAL = 0
LIFETIME = 3600
BUFFER = 50_000


def operation(**overrides):
    values = {
        "id": "op",
        "client_token": "ci-burst-op",
        "state": "running",
        "created_at": 1_000_000.0,
        "updated_at": 1_000_000.0,
        "expires_at": 1_003_600.0,
        "reservation_micros": 250_000,
        "reservation_window": 999_936_000.0,
        "region": "eu-central-1",
        "account": "111122223333",
        "request_json": "{}",
        "request_sha256": "0" * 64,
        "instance_id": "i-1",
        "detail": "",
        "price_micros": 100_000,
        "launch_time": 1_000_000.0,
    }
    values.update(overrides)
    return Operation(**values)


def model(incidental=INCIDENTAL):
    return CostModel(
        price_cap_micros=CAP,
        incidental_per_hour_micros=incidental,
        max_lifetime_seconds=LIFETIME,
        buffer_micros=BUFFER,
    )


class AccrualTests(unittest.TestCase):
    def test_accrual_uses_the_accepted_price_and_elapsed_time(self):
        # 600 seconds at 0.10 per hour is 16_666.67 micros, rounded up.
        self.assertEqual(model().accrued_micros(operation(), 1_000_600.0), 16_667)

    def test_billing_minimum_applies_to_a_short_lived_worker(self):
        self.assertEqual(MINIMUM_BILLABLE_SECONDS, 60)
        self.assertEqual(model().accrued_micros(operation(), 1_000_010.0), 1_667)

    def test_elapsed_time_is_capped_at_the_configured_lifetime(self):
        self.assertEqual(model().accrued_micros(operation(), 1_007_200.0), 100_000)

    def test_a_terminal_operation_stops_accruing(self):
        settled = operation(state="terminal", updated_at=1_000_300.0)
        self.assertEqual(model().accrued_micros(settled, 1_003_600.0), 8_334)

    def test_no_instance_has_no_accrued_cost(self):
        self.assertEqual(model().accrued_micros(operation(instance_id=None), 1_000_600.0), 0)

    def test_an_unknown_price_falls_back_to_the_configured_cap(self):
        self.assertEqual(model().accrued_micros(operation(price_micros=0), 1_000_600.0), 33_334)

    def test_incidentals_accrue_at_the_configured_hourly_rate(self):
        # 600 seconds at 0.10 plus 5_000 micros per hour of incidentals.
        self.assertEqual(model(incidental=5_000).accrued_micros(operation(), 1_000_600.0), 17_501)

    def test_launch_time_wins_over_creation_time(self):
        # The operation was created early; the provider launched it 600s ago.
        late = operation(created_at=999_000.0, launch_time=1_000_000.0)
        self.assertEqual(model().accrued_micros(late, 1_000_600.0), 16_667)


class CommitmentTests(unittest.TestCase):
    def test_remaining_life_is_priced_at_the_cap(self):
        # 3_000 seconds left at the configured maximum of 0.20 per hour.
        self.assertEqual(model().remaining_micros(operation(), 1_000_600.0), 166_667)

    def test_an_unresolved_intent_commits_its_whole_worst_case(self):
        intent = operation(instance_id=None, state="intent")
        self.assertEqual(model().remaining_micros(intent, 1_000_600.0), CAP)
        self.assertEqual(model().committed_micros(intent, 1_000_600.0), CAP + BUFFER)

    def test_a_live_operation_commits_spend_plus_remaining_plus_buffer(self):
        self.assertEqual(model().committed_micros(operation(), 1_000_600.0), 16_667 + 166_667 + BUFFER)

    def test_a_settled_operation_commits_exactly_its_settled_cost(self):
        settled = operation(state="terminal", settled_micros=12_345)
        self.assertEqual(model().committed_micros(settled, 1_003_600.0), 12_345)

    def test_a_lowered_ceiling_does_not_shrink_a_live_commitment(self):
        low = CostModel(
            price_cap_micros=100_000,
            incidental_per_hour_micros=0,
            max_lifetime_seconds=LIFETIME,
            buffer_micros=BUFFER,
        )
        live = operation(price_micros=200_000)
        # 3_000 seconds left at the accepted 0.20, not at the lowered 0.10.
        self.assertEqual(low.remaining_micros(live, 1_000_600.0), 166_667)

    def test_full_lifetime_and_per_launch_worst_case(self):
        self.assertEqual(model().full_lifetime_micros(), CAP)
        self.assertEqual(model().per_launch_micros(), CAP + BUFFER)


class ConfigModelTests(unittest.TestCase):
    def test_model_reads_the_deployment_configuration(self):
        class Section:
            pass

        class Config:
            pass

        config = Config()
        config.aws = Section()
        config.aws.max_spot_price = CAP
        config.aws.max_lifetime_seconds = LIFETIME
        config.budget = Section()
        config.budget.non_compute_allowance = BUFFER
        config.budget.incidental_per_hour = 4_000
        built = CostModel.from_config(config)
        self.assertEqual(built.price_cap_micros, CAP)
        self.assertEqual(built.incidental_per_hour_micros, 4_000)
        self.assertEqual(built.max_lifetime_seconds, LIFETIME)
        self.assertEqual(built.buffer_micros, BUFFER)


if __name__ == "__main__":
    unittest.main()
