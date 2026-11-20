"""Tests for the failure bounds of an image build.

``record_failure``, ``record_success``, ``build_deadline`` and
``blocked_explanation`` are pure, so they run on plain values. The daily cap,
``blocked``, its two recoveries and the cooldown are tested through
``image.build_decision``, with the facts that the state file gives it: the
counts of builds, the last failed build's digest and the control row the
functions here return.

Each decision test names the one condition it is about and passes every other
fact in the clear (no build in the last day, no recent build), so it asserts
the same refusal whichever condition ``build_decision`` checks first. A test
that needs a particular refusal asserts ``start`` and ``reason`` and nothing of
how a refusal asks for a clearing.
"""

import dataclasses
import tempfile
import unittest
from pathlib import Path

from ci_burst import image
from ci_burst.image_state import Control, ImageState

from support import Clock
from test_image import load_image

NOW = 1_800_000_000.0
HOUR = 3600
DAY = 86_400
DEPLOYMENT = "example-deployment"
OTHER_DEPLOYMENT = "example-other-deployment"
FAILED_DIGEST = "f" * 64


def control(**overrides):
    values = {"consecutive_failures": 0, "cooldown_until": None, "blocked": False, "last_check_at": None}
    values.update(overrides)
    return Control(**values)


class BoundsCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = load_image()
        limits = (
            cls.config.max_builds_per_day,
            cls.config.min_build_interval_seconds,
            cls.config.failure_cooldown_seconds,
            cls.config.max_consecutive_failures,
            cls.config.build_deadline_seconds,
        )
        assert limits == (2, DAY, 6 * HOUR, 3, 2700), limits

    def decide(self, **overrides):
        """A due build with nothing in the way, unless the test says otherwise."""
        facts = {
            "digest": "digest-new",
            "pin_digest": "digest-pinned",
            "now": NOW,
            "last_build_started_at": None,
            "builds_last_day": 0,
            "control": control(),
            "active": False,
            "config": self.config,
        }
        facts.update(overrides)
        return image.build_decision(**facts)


class StateCase(BoundsCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.clock = Clock(NOW)
        self.state = ImageState(Path(self._directory.name) / "image", clock=self.clock).connect()
        self.addCleanup(self.state.close)

    def start_build(self, build_id, at, *, state="failed", digest=FAILED_DIGEST, deployment=DEPLOYMENT):
        self.clock.now = at
        with self.state.transaction():
            self.state.insert_build(
                build_id=build_id,
                deployment=deployment,
                client_token="token-" + build_id,
                basis_digest=digest,
                source_sha="b" * 40,
                deadline_at=image.build_deadline(at, self.config),
            )
            if state != "building":
                self.state.advance_build(build_id, state=state)

    def fail(self, now):
        """Record one failed build in the control row, through the state file, as a caller does."""
        outcome = image.record_failure(self.state.control_get(), now, self.config)
        self.state.control_set(**dataclasses.asdict(outcome["control"]))
        return outcome

    def block(self):
        """Three failed builds of one digest, an hour apart: the row ends blocked."""
        for number in range(3):
            at = NOW + number * HOUR
            self.start_build(f"build-{number}", at)
            outcome = self.fail(at)
        self.assertTrue(outcome["newly_blocked"])
        return self.state.control_get()

    def clear(self):
        """What a caller writes when ``build_decision`` asks for a clearing."""
        return self.state.control_set(blocked=False, cooldown_until=None)

    def failed_digest(self):
        return self.state.last_failed_build(DEPLOYMENT).basis_digest


class RecordFailureTests(BoundsCase):
    def test_a_failure_counts_and_starts_the_cooldown(self):
        outcome = image.record_failure(control(), NOW, self.config)
        self.assertEqual(outcome["control"], control(consecutive_failures=1, cooldown_until=NOW + 6 * HOUR))
        self.assertFalse(outcome["newly_blocked"])

    def test_the_result_has_exactly_the_documented_fields(self):
        outcome = image.record_failure(control(), NOW, self.config)
        self.assertEqual(set(outcome), {"control", "newly_blocked"})
        self.assertIsInstance(outcome["control"], Control)
        self.assertIsInstance(outcome["newly_blocked"], bool)

    def test_the_given_row_is_not_changed(self):
        row = control(consecutive_failures=1)
        image.record_failure(row, NOW, self.config)
        self.assertEqual(row, control(consecutive_failures=1))

    def test_the_cooldown_follows_the_configuration(self):
        quick = dataclasses.replace(self.config, failure_cooldown_seconds=600)
        self.assertEqual(image.record_failure(control(), NOW, quick)["control"].cooldown_until, NOW + 600)

    def test_each_failure_restarts_the_cooldown_from_its_own_time(self):
        first = image.record_failure(control(), NOW, self.config)["control"]
        second = image.record_failure(first, NOW + HOUR, self.config)["control"]
        self.assertEqual(second.cooldown_until, NOW + HOUR + 6 * HOUR)
        self.assertEqual(second.consecutive_failures, 2)

    def test_blocked_begins_exactly_at_the_limit(self):
        row = control()
        for failures in (1, 2, 3):
            outcome = image.record_failure(row, NOW, self.config)
            row = outcome["control"]
            with self.subTest(failures=failures):
                self.assertEqual(row.consecutive_failures, failures)
                self.assertEqual(row.blocked, failures == 3)
                self.assertEqual(outcome["newly_blocked"], failures == 3)

    def test_the_limit_follows_the_configuration(self):
        for limit in (1, 2, 10):
            config = dataclasses.replace(self.config, max_consecutive_failures=limit)
            row = control()
            for failures in range(1, limit + 1):
                outcome = image.record_failure(row, NOW, config)
                row = outcome["control"]
                with self.subTest(limit=limit, failures=failures):
                    self.assertEqual(row.blocked, failures == limit)
                    self.assertEqual(outcome["newly_blocked"], failures == limit)

    def test_a_blocked_row_stays_blocked_and_is_not_newly_blocked_again(self):
        outcome = image.record_failure(control(consecutive_failures=3, blocked=True), NOW, self.config)
        self.assertTrue(outcome["control"].blocked)
        self.assertEqual(outcome["control"].consecutive_failures, 4)
        self.assertFalse(outcome["newly_blocked"])

    def test_a_failure_keeps_the_last_check_time(self):
        outcome = image.record_failure(control(last_check_at=NOW - 5), NOW, self.config)
        self.assertEqual(outcome["control"].last_check_at, NOW - 5)

    def test_the_first_failure_after_a_clearing_blocks_the_row_again(self):
        # A clearing writes blocked false and no cooldown and leaves the count at the limit.
        cleared = control(consecutive_failures=3, cooldown_until=None, blocked=False)
        outcome = image.record_failure(cleared, NOW, self.config)
        self.assertTrue(outcome["control"].blocked)
        self.assertTrue(outcome["newly_blocked"])
        self.assertEqual(outcome["control"].consecutive_failures, 4)


class RecordSuccessTests(BoundsCase):
    def test_a_success_ends_the_failures_the_cooldown_and_blocked(self):
        row = control(consecutive_failures=3, cooldown_until=NOW + 1, blocked=True, last_check_at=NOW)
        self.assertEqual(image.record_success(row), control(last_check_at=NOW))

    def test_a_clean_row_stays_clean(self):
        self.assertEqual(image.record_success(control()), control())

    def test_the_given_row_is_not_changed(self):
        row = control(consecutive_failures=2)
        image.record_success(row)
        self.assertEqual(row, control(consecutive_failures=2))

    def test_after_a_success_the_count_starts_over(self):
        row = image.record_failure(control(consecutive_failures=2), NOW, self.config)["control"]
        self.assertTrue(row.blocked)
        row = image.record_success(row)
        outcome = image.record_failure(row, NOW + DAY, self.config)
        self.assertEqual(outcome["control"].consecutive_failures, 1)
        self.assertFalse(outcome["control"].blocked)
        self.assertFalse(outcome["newly_blocked"])


class BuildDeadlineTests(BoundsCase):
    def test_the_deadline_is_the_start_plus_the_configured_seconds(self):
        self.assertEqual(image.build_deadline(NOW, self.config), NOW + 2700)

    def test_the_deadline_follows_the_configuration(self):
        short = dataclasses.replace(self.config, build_deadline_seconds=900)
        self.assertEqual(image.build_deadline(NOW, short), NOW + 900)

    def test_a_later_start_gives_a_later_deadline(self):
        self.assertEqual(image.build_deadline(NOW + 60, self.config) - image.build_deadline(NOW, self.config), 60)


class DailyCapTests(StateCase):
    """The count of the last day comes from the state file, and ``--force`` never skips the cap."""

    def last_day(self, now=NOW):
        return self.state.builds_started_since(DEPLOYMENT, now - DAY)

    def forced(self, now=NOW):
        return self.decide(force=True, now=now, builds_last_day=self.last_day(now))

    def test_two_builds_in_the_last_day_refuse_a_forced_build(self):
        self.start_build("build-0", NOW - 2 * HOUR)
        self.start_build("build-1", NOW - HOUR, state="candidate")
        self.assertEqual(self.last_day(), 2)
        result = self.forced()
        self.assertFalse(result["start"])
        self.assertEqual(result["reason"], "IMAGE_DAILY_CAP")

    def test_one_build_in_the_last_day_leaves_room_for_a_forced_build(self):
        self.start_build("build-0", NOW - HOUR)
        self.assertEqual(self.last_day(), 1)
        result = self.forced()
        self.assertTrue(result["start"])
        self.assertIsNone(result["reason"])

    def test_no_build_leaves_room(self):
        self.assertEqual(self.last_day(), 0)
        self.assertTrue(self.forced()["start"])

    def test_every_state_counts_toward_the_cap(self):
        for number, state in enumerate(("failed", "candidate", "promoted")):
            self.start_build(f"build-{number}", NOW - (number + 1) * HOUR, state=state)
        self.assertEqual(self.last_day(), 3)
        self.start_build("build-3", NOW - 30, state="building")
        self.assertEqual(self.last_day(), 4)

    def test_the_cap_follows_the_configuration(self):
        roomy = dataclasses.replace(self.config, max_builds_per_day=3)
        self.start_build("build-0", NOW - 2 * HOUR)
        self.start_build("build-1", NOW - HOUR)
        self.assertTrue(self.decide(force=True, builds_last_day=self.last_day(), config=roomy)["start"])
        self.start_build("build-2", NOW - 60)
        refused = self.decide(force=True, builds_last_day=self.last_day(), config=roomy)
        self.assertEqual((refused["start"], refused["reason"]), (False, "IMAGE_DAILY_CAP"))

    def test_a_build_that_started_a_full_day_ago_no_longer_counts(self):
        self.start_build("build-0", NOW - DAY - 1)
        self.start_build("build-1", NOW - DAY)
        self.start_build("build-2", NOW - DAY + 1)
        self.assertEqual(self.last_day(), 1)
        self.assertTrue(self.forced()["start"])

    def test_the_oldest_build_ageing_out_reopens_the_cap(self):
        self.start_build("build-0", NOW - 2 * HOUR)
        self.start_build("build-1", NOW - HOUR)
        reopens = NOW - 2 * HOUR + DAY
        before = self.forced(now=reopens - 1)
        self.assertEqual((before["start"], before["reason"]), (False, "IMAGE_DAILY_CAP"))
        self.assertTrue(self.forced(now=reopens)["start"])

    def test_another_deployments_builds_do_not_count(self):
        self.start_build("build-0", NOW - 2 * HOUR, deployment=OTHER_DEPLOYMENT)
        self.start_build("build-1", NOW - HOUR, deployment=OTHER_DEPLOYMENT)
        self.assertEqual(self.last_day(), 0)
        self.assertTrue(self.forced()["start"])

    def test_force_does_not_skip_the_cap_on_a_blocked_row_either(self):
        self.start_build("build-0", NOW - 2 * HOUR)
        self.start_build("build-1", NOW - HOUR)
        row = control(consecutive_failures=3, blocked=True, cooldown_until=NOW - 1)
        result = self.decide(force=True, control=row, builds_last_day=self.last_day())
        self.assertFalse(result["start"])
        self.assertEqual(result["reason"], "IMAGE_DAILY_CAP")


class BlockedTests(StateCase):
    def after_cooldown(self, row):
        """A time when the cooldown of ``row`` is over, so that only ``blocked`` is in the way."""
        return row.cooldown_until + 1

    def test_the_limit_blocks_the_row_and_the_state_keeps_it(self):
        self.start_build("build-0", NOW)
        self.assertFalse(self.fail(NOW)["control"].blocked)
        self.start_build("build-1", NOW + HOUR)
        self.assertFalse(self.fail(NOW + HOUR)["control"].blocked)
        self.start_build("build-2", NOW + 2 * HOUR)
        third = self.fail(NOW + 2 * HOUR)
        self.assertTrue(third["newly_blocked"])
        row = self.state.control_get()
        self.assertEqual((row.consecutive_failures, row.blocked), (3, True))

    def test_a_blocked_row_refuses_the_digest_that_failed(self):
        row = self.block()
        result = self.decide(
            control=row,
            now=self.after_cooldown(row),
            digest=self.failed_digest(),
            last_failed_digest=self.failed_digest(),
        )
        self.assertFalse(result["start"])
        self.assertEqual(result["reason"], "IMAGE_BLOCKED")

    def test_a_blocked_row_refuses_when_no_failed_digest_is_known(self):
        row = self.block()
        result = self.decide(control=row, now=self.after_cooldown(row), last_failed_digest=None)
        self.assertEqual((result["start"], result["reason"]), (False, "IMAGE_BLOCKED"))

    def test_recovery_by_a_basis_digest_change(self):
        row = self.block()
        result = self.decide(
            control=row,
            now=self.after_cooldown(row),
            digest="digest-changed",
            last_failed_digest=self.failed_digest(),
        )
        self.assertTrue(result["start"])
        self.assertIsNone(result["reason"])
        self.assertTrue(result["clear_control"])
        cleared = self.clear()
        self.assertEqual((cleared.blocked, cleared.cooldown_until), (False, None))
        self.assertEqual(cleared.consecutive_failures, 3, "a clearing leaves the count to a success")

    def test_recovery_by_force(self):
        row = self.block()
        result = self.decide(
            control=row,
            now=self.after_cooldown(row),
            force=True,
            digest=self.failed_digest(),
            last_failed_digest=self.failed_digest(),
        )
        self.assertTrue(result["start"])
        self.assertTrue(result["clear_control"])

    def test_force_recovers_a_blocked_row_whose_cooldown_is_still_running(self):
        row = self.block()
        result = self.decide(control=row, now=row.cooldown_until - 1, force=True, last_failed_digest=None)
        self.assertTrue(result["start"])
        self.assertTrue(result["clear_control"])

    def test_a_recovered_build_that_fails_blocks_the_row_again(self):
        self.block()
        self.clear()
        self.start_build("build-3", NOW + 5 * HOUR)
        outcome = self.fail(NOW + 5 * HOUR)
        self.assertTrue(outcome["newly_blocked"])
        self.assertTrue(self.state.control_get().blocked)

    def test_a_recovered_build_that_succeeds_ends_the_count(self):
        self.block()
        self.clear()
        self.state.control_set(**dataclasses.asdict(image.record_success(self.state.control_get())))
        row = self.state.control_get()
        self.assertEqual((row.consecutive_failures, row.cooldown_until, row.blocked), (0, None, False))
        self.start_build("build-3", NOW + 5 * HOUR)
        outcome = self.fail(NOW + 5 * HOUR)
        self.assertFalse(outcome["control"].blocked)
        self.assertEqual(outcome["control"].consecutive_failures, 1)


class StatusExplanationTests(StateCase):
    def test_the_explanation_names_both_ways_to_clear_blocked(self):
        self.assertEqual(
            image.blocked_explanation(),
            {"blocked": True, "clear_by": ["a basis digest change", "image run --apply --force"]},
        )

    def test_each_call_returns_a_fresh_value(self):
        first = image.blocked_explanation()
        first["clear_by"].append("something else")
        self.assertEqual(len(image.blocked_explanation()["clear_by"]), 2)

    def test_every_way_it_names_clears_a_blocked_row(self):
        row = self.block()
        later = row.cooldown_until + 1
        ways = {
            "a basis digest change": {"digest": "digest-changed", "last_failed_digest": self.failed_digest()},
            "image run --apply --force": {"force": True, "last_failed_digest": self.failed_digest()},
        }
        self.assertEqual(set(ways), set(image.blocked_explanation()["clear_by"]))
        for way, facts in ways.items():
            with self.subTest(way):
                result = self.decide(control=row, now=later, **facts)
                self.assertTrue(result["start"])
                self.assertTrue(result["clear_control"])

    def test_a_row_that_is_not_blocked_needs_no_explanation_to_start(self):
        self.assertTrue(self.decide(control=control(consecutive_failures=2))["start"])


class CooldownTests(StateCase):
    def cooling(self):
        self.start_build("build-0", NOW)
        return self.fail(NOW)["control"]

    def test_a_failure_starts_a_cooldown_that_refuses_until_it_ends(self):
        row = self.cooling()
        self.assertFalse(row.blocked)
        ends = NOW + 6 * HOUR
        self.assertEqual(row.cooldown_until, ends)
        for now in (NOW, NOW + HOUR, ends - 1):
            with self.subTest(now=now):
                result = self.decide(control=row, now=now)
                self.assertEqual((result["start"], result["reason"]), (False, "IMAGE_FAILURE_COOLDOWN"))

    def test_a_cooldown_that_ends_now_is_over(self):
        row = self.cooling()
        self.assertTrue(self.decide(control=row, now=row.cooldown_until)["start"])
        self.assertTrue(self.decide(control=row, now=row.cooldown_until + 1)["start"])

    def test_the_cooldown_follows_the_configuration(self):
        quick = dataclasses.replace(self.config, failure_cooldown_seconds=600)
        row = image.record_failure(control(), NOW, quick)["control"]
        self.assertFalse(self.decide(control=row, now=NOW + 599, config=quick)["start"])
        self.assertTrue(self.decide(control=row, now=NOW + 600, config=quick)["start"])

    def test_force_ends_a_cooldown(self):
        row = self.cooling()
        result = self.decide(control=row, now=NOW + 1, force=True)
        self.assertTrue(result["start"])
        self.assertTrue(result["clear_control"])

    def test_a_digest_change_alone_does_not_end_a_cooldown_that_is_not_blocked(self):
        row = self.cooling()
        result = self.decide(control=row, now=NOW + 1, last_failed_digest=FAILED_DIGEST)
        self.assertEqual((result["start"], result["reason"]), (False, "IMAGE_FAILURE_COOLDOWN"))

    def test_a_success_ends_the_cooldown(self):
        row = image.record_success(self.cooling())
        self.assertTrue(self.decide(control=row, now=NOW + 1)["start"])


if __name__ == "__main__":
    unittest.main()
