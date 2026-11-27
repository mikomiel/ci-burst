import hashlib
import json
import unittest

from ci_burst.cost import MAX_STORED_PRICE_MICROS, MINIMUM_BILLABLE_SECONDS, CostModel, _frozen_max_price_micros
from ci_burst.ledger import Operation
from ci_burst.money import format_money

FROZEN_REQUEST = json.dumps(
    {
        "InstanceMarketOptions": {
            "MarketType": "spot",
            "SpotOptions": {"MaxPrice": "0.040000", "SpotInstanceType": "one-time"},
        },
        "MinCount": 1,
        "MaxCount": 1,
    }
)

CAP = 200_000
INCIDENTAL = 0
LIFETIME = 3600
BUFFER = 50_000


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def operation(**overrides):
    """An operation row; its ``request_sha256`` is the hash of its request text, as admission stores it."""
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
        "instance_id": "i-1",
        "detail": "",
        "price_micros": 100_000,
        "launch_time": 1_000_000.0,
    }
    values.update(overrides)
    if "request_sha256" not in values:
        text = values["request_json"]
        values["request_sha256"] = sha(text) if isinstance(text, str) else "0" * 64
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


    def test_a_request_nested_past_the_json_limit_contributes_no_ceiling(self):
        # Every pass prices the rows of the budget window, terminal ones too. A
        # frozen request that the decoder cannot take contributes nothing, like
        # any other that does not parse, and must not stop the pass.
        nested = "[" * 100_000 + "]" * 100_000
        self.assertEqual(
            model().price_ceiling_micros(operation(request_json=nested)),
            model().price_ceiling_micros(operation()),
        )

class CommitmentTests(unittest.TestCase):
    def test_remaining_life_is_priced_at_the_cap(self):
        # 3_000 seconds left at the configured maximum of 0.20 per hour.
        self.assertEqual(model().remaining_micros(operation(request_json=FROZEN_REQUEST), 1_000_600.0), 166_667)

    def test_an_unresolved_intent_commits_its_whole_worst_case(self):
        intent = operation(instance_id=None, state="intent")
        self.assertEqual(model().remaining_micros(intent, 1_000_600.0), CAP)
        self.assertEqual(model().committed_micros(intent, 1_000_600.0), CAP + BUFFER)

    def test_a_live_operation_commits_spend_plus_remaining_plus_buffer(self):
        self.assertEqual(
            model().committed_micros(operation(request_json=FROZEN_REQUEST), 1_000_600.0), 16_667 + 166_667 + BUFFER
        )

    def test_a_settled_operation_commits_exactly_its_settled_cost(self):
        settled = operation(state="terminal", settled_micros=12_345)
        self.assertEqual(model().committed_micros(settled, 1_003_600.0), 12_345)

    def test_an_operation_keeps_the_lifetime_it_was_admitted_with(self):
        shorter = CostModel(
            price_cap_micros=200_000,
            incidental_per_hour_micros=0,
            max_lifetime_seconds=60,
            buffer_micros=0,
        )
        # A row admitted under the shorter model's own basis: its flag is 0, so
        # the admitted-worst-case floor does not apply.
        live = operation(
            expires_at=1_003_600.0, admitted_buffer_micros=0, reservation_micros=200_000, request_json=FROZEN_REQUEST
        )
        self.assertEqual(shorter.lifetime_seconds(live), 3600)
        self.assertEqual(shorter.accrued_micros(live, 1_000_600.0), 16_667)
        self.assertEqual(shorter.remaining_micros(live, 1_000_600.0), 166_667)
        self.assertEqual(
            shorter.full_lifetime_micros(100_000, shorter.lifetime_seconds(live)), 100_000
        )

    def test_a_shorter_stored_lifetime_bounds_a_longer_configuration(self):
        live = operation(expires_at=1_000_060.0, reservation_micros=53_334, request_json=FROZEN_REQUEST)
        self.assertEqual(model().lifetime_seconds(live), 60)
        # The stored 60-second lifetime bounds a 3_600-second configuration.
        self.assertEqual(model().accrued_micros(live, 1_000_600.0), 1_667)
        self.assertEqual(model().remaining_micros(live, 1_000_600.0), 0)

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


class BillingMinimumTests(unittest.TestCase):
    """The provider's one billed minute is counted once, not in both directions."""

    @staticmethod
    def short_fixture():
        return CostModel(
            price_cap_micros=60_000,
            incidental_per_hour_micros=0,
            max_lifetime_seconds=60,
            buffer_micros=1,
        )

    @staticmethod
    def long_fixture():
        return CostModel(
            price_cap_micros=20_000,
            incidental_per_hour_micros=0,
            max_lifetime_seconds=3600,
            buffer_micros=0,
        )

    def test_each_second_boundary_commits_the_billed_minute_once(self):
        model = self.short_fixture()
        for elapsed in (0, 1, 59, 60, 61):
            with self.subTest(elapsed=elapsed):
                live = operation(
                    created_at=1_000_000.0,
                    expires_at=1_000_060.0,
                    price_micros=60_000,
                    reservation_micros=0,
                )
                now = 1_000_000.0 + elapsed
                self.assertEqual(model.accrued_micros(live, now), 1_000)
                self.assertEqual(model.remaining_micros(live, now), 0)
                self.assertEqual(model.committed_micros(live, now), 1_001)

    def test_a_long_lifetime_never_exceeds_the_whole_lifetime_by_more_than_one_micro(self):
        model = self.long_fixture()
        whole = model.per_launch_micros()
        for elapsed in (0, 1, 59, 60, 61):
            with self.subTest(elapsed=elapsed):
                live = operation(
                    created_at=1_000_000.0,
                    expires_at=1_003_600.0,
                    price_micros=20_000,
                    reservation_micros=0,
                )
                now = 1_000_000.0 + elapsed
                committed = model.committed_micros(live, now)
                self.assertGreaterEqual(committed, whole)
                self.assertLessEqual(committed, whole + 1)

    def test_a_terminal_short_lived_worker_keeps_the_billed_minute(self):
        model = self.short_fixture()
        ended = operation(
            created_at=1_000_000.0,
            updated_at=1_000_010.0,
            expires_at=1_000_060.0,
            price_micros=60_000,
            reservation_micros=0,
            state="terminal",
            settled_micros=1_000,
        )
        self.assertEqual(model.accrued_micros(ended, 1_000_600.0), 1_000)
        self.assertEqual(model.committed_micros(ended, 1_000_600.0), 1_000)


class AdmittedBasisTests(unittest.TestCase):
    """A later configuration edit must not reprice an operation already admitted."""

    @staticmethod
    def admitted(**overrides):
        values = {
            "request_json": FROZEN_REQUEST,
            "price_micros": 20_000,
            "admitted_incidental_per_hour_micros": 50_000,
            "admitted_buffer_micros": 500_000,
            "reservation_micros": 590_000,
            "created_at": 1_000_000.0,
            "updated_at": 1_000_000.0,
            "expires_at": 1_003_600.0,
            "launch_time": 1_000_000.0,
            "state": "running",
        }
        values.update(overrides)
        return operation(**values)

    @staticmethod
    def original_model():
        return CostModel(
            price_cap_micros=40_000,
            incidental_per_hour_micros=50_000,
            max_lifetime_seconds=3600,
            buffer_micros=500_000,
        )

    @staticmethod
    def lowered_model():
        return CostModel(
            price_cap_micros=10_000,
            incidental_per_hour_micros=0,
            max_lifetime_seconds=3600,
            buffer_micros=0,
        )

    def test_a_lowered_configuration_does_not_reprice_an_admitted_operation(self):
        live = self.admitted()
        now = 1_000_600.0
        # The reproducer's numbers: an admitted 0.040000 ceiling, a 0.020000
        # accepted price, a 0.050000 hourly incidental rate and a 0.500000
        # buffer read the same before and after the configuration is lowered.
        for model in (self.original_model(), self.lowered_model()):
            self.assertEqual(model.accrued_micros(live, now), 11_668)
            self.assertEqual(model.remaining_micros(live, now), 75_001)
            self.assertEqual(model.committed_micros(live, now), 586_669)

    def test_a_missing_basis_uses_the_configured_rate_and_the_frozen_cap(self):
        # A row that predates schema 3 records no admitted inputs. The lowered
        # configured cap no longer prices it: the request's frozen MaxPrice does.
        legacy = operation(
            request_json=FROZEN_REQUEST,
            price_micros=0,
            reservation_micros=52_000,
        )
        model = CostModel(
            price_cap_micros=10_000,
            incidental_per_hour_micros=5_000,
            max_lifetime_seconds=3600,
            buffer_micros=7_000,
        )
        now = 1_000_600.0
        self.assertEqual(model.price_ceiling_micros(legacy), 40_000)
        self.assertEqual(model.incidental_for(legacy), 5_000)
        self.assertEqual(model.buffer_for(legacy), 7_000)
        self.assertEqual(model.accrued_micros(legacy, now), 7_501)
        self.assertEqual(model.remaining_micros(legacy, now), 37_501)
        self.assertEqual(model.committed_micros(legacy, now), 52_002)

    def test_a_new_admission_uses_the_new_configuration(self):
        model = self.lowered_model()
        self.assertEqual(model.per_launch_micros(), 10_000)
        fresh = self.admitted(
            request_json=json.dumps(
                {"InstanceMarketOptions": {"SpotOptions": {"MaxPrice": "0.010000"}}}
            ),
            price_micros=10_000,
            admitted_incidental_per_hour_micros=0,
            admitted_buffer_micros=0,
            reservation_micros=10_000,
        )
        now = 1_000_600.0
        self.assertEqual(model.accrued_micros(fresh, now), 1_667)
        self.assertEqual(model.remaining_micros(fresh, now), 8_334)
        self.assertEqual(model.committed_micros(fresh, now), 10_001)

    def test_a_backfilled_operation_keeps_the_admitted_worst_case(self):
        # A non-terminal row migrated while the configured rates are lower: the
        # upgrade marked it, so its reservation stays the floor.
        backfilled = self.admitted(
            admitted_incidental_per_hour_micros=0,
            admitted_buffer_micros=0,
            admitted_inputs_backfilled=1,
        )
        model = self.lowered_model()
        now = 1_000_600.0
        self.assertEqual(model.accrued_micros(backfilled, now), 3_334)
        self.assertEqual(model.remaining_micros(backfilled, now), 586_666)
        self.assertEqual(model.committed_micros(backfilled, now), 590_000)

    def test_settlement_uses_the_admitted_ceiling_not_a_raised_configuration(self):
        raised = CostModel(
            price_cap_micros=100_000,
            incidental_per_hour_micros=0,
            max_lifetime_seconds=3600,
            buffer_micros=0,
        )
        unpriced = operation(request_json=FROZEN_REQUEST, price_micros=0)
        self.assertEqual(raised.settlement_price_micros(unpriced), 40_000)
        accepted = operation(request_json=FROZEN_REQUEST, price_micros=20_000)
        self.assertEqual(raised.settlement_price_micros(accepted), 20_000)
        no_ceiling = operation(request_json="{}", price_micros=0)
        self.assertEqual(raised.settlement_price_micros(no_ceiling), 100_000)

    def test_only_a_backfilled_row_has_a_reservation_floor(self):
        admitted = self.admitted()
        self.assertEqual(admitted.admitted_inputs_backfilled, 0)
        self.assertEqual(self.original_model().reservation_floor_micros(admitted), 0)
        backfilled = self.admitted(admitted_inputs_backfilled=1, reservation_micros=590_000)
        self.assertEqual(self.original_model().reservation_floor_micros(backfilled), 590_000)

    def test_a_raised_ceiling_never_releases_a_backfilled_commitment(self):
        # The reviewer's reproducer: a backfilled row with reservation 590_000, a
        # frozen MaxPrice of 0.040000, an accepted price of 0.020000, zero
        # backfilled incidental and buffer, a 3_600-second lifetime and 600
        # seconds lived. An inferred floor compared the reservation with a
        # whole-life estimate that included the current ceiling; at 0.590000 the
        # estimate reached the reservation, the floor vanished and the
        # commitment fell 590_000 -> 495_001, releasing 94_999 of allowance.
        backfilled = self.admitted(
            admitted_incidental_per_hour_micros=0,
            admitted_buffer_micros=0,
            admitted_inputs_backfilled=1,
        )
        now = 1_000_600.0
        models = (
            CostModel(
                price_cap_micros=40_000,
                incidental_per_hour_micros=0,
                max_lifetime_seconds=3600,
                buffer_micros=0,
            ),
            CostModel(
                price_cap_micros=590_000,
                incidental_per_hour_micros=0,
                max_lifetime_seconds=3600,
                buffer_micros=0,
            ),
            # Far above the reservation: unlived time is priced higher, which may
            # raise the forward commitment but must never lower it.
            CostModel(
                price_cap_micros=1_000_000,
                incidental_per_hour_micros=0,
                max_lifetime_seconds=3600,
                buffer_micros=0,
            ),
        )
        low, raised, far = models
        self.assertEqual(low.committed_micros(backfilled, now), 590_000)
        self.assertEqual(raised.committed_micros(backfilled, now), 590_000)
        self.assertGreaterEqual(far.committed_micros(backfilled, now), 590_000)
        self.assertEqual(raised.reservation_floor_micros(backfilled), 590_000)

    def test_a_live_row_whose_request_no_longer_matches_its_hash_keeps_its_reservation(self):
        # A valid but lower MaxPrice written without its hash must not price the remaining life below the ceiling.
        lowered = FROZEN_REQUEST.replace('"0.040000"', '"0.010000"')
        self.assertNotEqual(lowered, FROZEN_REQUEST)
        stale = self.admitted(request_json=lowered, request_sha256=sha(FROZEN_REQUEST))
        self.assertEqual(self.original_model().reservation_floor_micros(stale), 590_000)
        self.assertEqual(self.original_model().reservation_floor_micros(self.admitted(request_json=lowered)), 0)
        ended = self.admitted(request_json=lowered, request_sha256=sha(FROZEN_REQUEST), state="terminal")
        self.assertEqual(self.original_model().reservation_floor_micros(ended), 0)

    def test_a_price_admission_would_not_write_is_no_price(self):
        for price in (
            "²", "0.²", "0.010000²", "٣.000000", "9" * 5000, "0.01" + "0" * 5000, "0.0400001", " 0.040000",
            "00.040000", "0.04", "1", "+0.040000", "0_0.040000", 0.04, 1,
        ):
            with self.subTest(price=repr(price)[:14]):
                request = json.dumps({"InstanceMarketOptions": {"SpotOptions": {"MaxPrice": price}}})
                self.assertEqual(_frozen_max_price_micros(request), 0)
        self.assertEqual(_frozen_max_price_micros(FROZEN_REQUEST), 40_000)
        for micros, expected in ((MAX_STORED_PRICE_MICROS, MAX_STORED_PRICE_MICROS), (MAX_STORED_PRICE_MICROS + 1, 0)):
            request = json.dumps({"InstanceMarketOptions": {"SpotOptions": {"MaxPrice": format_money(micros)}}})
            self.assertEqual(_frozen_max_price_micros(request), expected)

    def test_a_noncanonical_price_under_its_own_hash_keeps_the_reservation(self):
        # Admission writes "0.010000"; the same price with a leading zero is not a request it froze.
        request = FROZEN_REQUEST.replace('"0.040000"', '"00.010000"')
        self.assertNotEqual(request, FROZEN_REQUEST)
        self.assertEqual(self.original_model().reservation_floor_micros(self.admitted(request_json=request)), 590_000)

    def test_a_damaged_price_does_not_raise_the_ceiling(self):
        # Only the stored text changed; its hash is still the one admission stored for the 0.040000 request.
        for price in ("9.040000", "0.000001"):
            with self.subTest(price=price):
                text = FROZEN_REQUEST.replace('"0.040000"', '"' + price + '"')
                damaged = self.admitted(request_json=text, request_sha256=sha(FROZEN_REQUEST))
                trusted = self.admitted(request_json=None)
                for model in (self.original_model(), self.lowered_model()):
                    self.assertEqual(model.price_ceiling_micros(damaged), model.price_ceiling_micros(trusted))
        # The reservation stays the floor of the live row, so the commitment never falls below what was admitted.
        raised = FROZEN_REQUEST.replace('"0.040000"', '"9.040000"')
        live = self.admitted(request_json=raised, request_sha256=sha(FROZEN_REQUEST))
        self.assertEqual(self.lowered_model().reservation_floor_micros(live), 590_000)

    def test_an_oversized_price_does_not_price_the_ceiling(self):
        # A price past any real one, even under its own hash, must not reach the arithmetic or the export.
        huge = FROZEN_REQUEST.replace('"0.040000"', '"' + "9" * 4300 + '.000000"')
        damaged = self.admitted(request_json=huge)
        model = self.original_model()
        intact_bounds = self.admitted(request_json=None)
        self.assertEqual(model.price_ceiling_micros(damaged), model.price_ceiling_micros(intact_bounds))
        self.assertEqual(model.settlement_price_micros(damaged), model.settlement_price_micros(intact_bounds))

    def test_a_malformed_frozen_request_never_raises(self):
        model = CostModel(
            price_cap_micros=100_000,
            incidental_per_hour_micros=0,
            max_lifetime_seconds=3600,
            buffer_micros=0,
        )
        for request_json in (
            None,
            "not json",
            "{}",
            '{"InstanceMarketOptions": {}}',
            '{"InstanceMarketOptions": {"SpotOptions": {"MaxPrice": "not-a-price"}}}',
            '{"InstanceMarketOptions": {"SpotOptions": {"MaxPrice": 0.04}}}',
        ):
            with self.subTest(request_json=request_json):
                live = self.admitted(
                    request_json=request_json,
                    price_micros=0,
                    admitted_incidental_per_hour_micros=None,
                    admitted_buffer_micros=None,
                    reservation_micros=0,
                )
                self.assertEqual(model.price_ceiling_micros(live), 100_000)
                self.assertEqual(model.accrued_micros(live, 1_000_600.0), 16_667)


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
