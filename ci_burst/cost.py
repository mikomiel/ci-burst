"""Tracked cost of one launch operation.

The controller cannot read the amount AWS will bill: the charge depends on the
Spot price over the instance's life, per-second rounding and charges it does not
observe. These functions therefore track an estimate in exact micro-units:
compute at the quote taken when the launch was accepted, plus the hourly
incidental rate the operation was admitted under (public IPv4, EBS). Time not yet
lived is priced at the highest price this operation's frozen request can still be
charged at, so a forward commitment can never under-count a worker that is still
running.

Admission records the incidental rate and the non-compute buffer on the
operation, so a later configuration edit prices new admissions only. The provider
bills at least one minute for every instance; that minute is counted once: the
billed duration is floored at it, and the forward commitment prices only the
lifetime beyond the billed duration.
"""

import json
import math
from dataclasses import dataclass

from .money import MICROS_PER_UNIT, hourly_cost_micros

#: EC2 bills an instance for at least one minute even when it exits sooner.
MINIMUM_BILLABLE_SECONDS = 60
TERMINAL_STATES = ("terminal", "failed")
#: ``format_money`` writes six fractional digits; a stored price may carry fewer.
PRICE_FRACTION_DIGITS = 6


def _stored_price_micros(value):
    """Exact micros of one stored price, or zero when it is not a usable price.

    A malformed stored request must never raise while cost is reported, so an
    unparseable price contributes nothing and the other bounds apply. A value
    with more precision than micros rounds up, because a ceiling may over-state
    but must not under-state.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value * MICROS_PER_UNIT if value > 0 else 0
    if isinstance(value, float):
        if not math.isfinite(value) or value <= 0:
            return 0
        value = f"{value:.{PRICE_FRACTION_DIGITS}f}"
    if not isinstance(value, str):
        return 0
    whole, dot, fraction = value.strip().partition(".")
    if not whole.isdigit() or (dot and (not fraction or not fraction.isdigit())):
        return 0
    micros = int(whole) * MICROS_PER_UNIT
    if dot:
        micros += int(fraction[:PRICE_FRACTION_DIGITS].ljust(PRICE_FRACTION_DIGITS, "0"))
        if fraction[PRICE_FRACTION_DIGITS:].strip("0"):
            micros += 1
    return micros if micros > 0 else 0


def _frozen_max_price_micros(request_json):
    """The ``MaxPrice`` frozen in an operation's request, or zero.

    The ceiling admission committed is already stored in the request the
    provider received; a schema-3 row does not need a second column for it.
    """
    try:
        request = json.loads(request_json)
        value = request["InstanceMarketOptions"]["SpotOptions"]["MaxPrice"]
    except (TypeError, ValueError, KeyError, IndexError):
        return 0
    return _stored_price_micros(value)


@dataclass(frozen=True)
class CostModel:
    """Prices one deployment's operations, in micro-units of currency."""

    price_cap_micros: int
    incidental_per_hour_micros: int
    max_lifetime_seconds: int
    buffer_micros: int

    @classmethod
    def from_config(cls, config):
        return cls(
            price_cap_micros=config.aws.max_spot_price,
            incidental_per_hour_micros=config.budget.incidental_per_hour,
            max_lifetime_seconds=config.aws.max_lifetime_seconds,
            buffer_micros=config.budget.non_compute_allowance,
        )

    # -- per-operation admitted inputs ---------------------------------
    def incidental_for(self, operation):
        """Hourly incidental rate recorded at admission, else the configured one."""
        if operation.admitted_incidental_per_hour_micros is not None:
            return operation.admitted_incidental_per_hour_micros
        return self.incidental_per_hour_micros

    def buffer_for(self, operation):
        """Non-compute buffer recorded at admission, else the configured one."""
        if operation.admitted_buffer_micros is not None:
            return operation.admitted_buffer_micros
        return self.buffer_micros

    def price_ceiling_micros(self, operation):
        """Highest hourly price this operation may still be charged at.

        The frozen request's ``MaxPrice`` is the ceiling admission committed;
        the configured maximum and the accepted price are the other bounds. A
        stored request that cannot be parsed contributes nothing.
        """
        return max(
            self.price_cap_micros,
            _frozen_max_price_micros(operation.request_json),
            operation.price_micros or 0,
        )

    def settlement_price_micros(self, operation):
        """Price to settle an operation whose instance was not observed ending.

        The accepted price when it was recorded; otherwise the ceiling frozen in
        the request admission sent; otherwise the configured maximum, which is
        the only bound a row without a request price has. A raised configuration
        never settles above the ceiling admission committed.
        """
        return (
            operation.price_micros
            or _frozen_max_price_micros(operation.request_json)
            or self.price_cap_micros
        )

    def reservation_floor_micros(self, operation):
        """The stored reservation, where the recorded basis cannot reproduce it.

        A row admitted under its recorded basis reproduces its own reservation,
        so this is zero for it and its commitment may fall below the reservation
        as the minutes it has lived were accepted at a price under the ceiling.
        A non-terminal row whose admitted inputs had to be backfilled can
        reproduce less than the reservation admission committed; keeping that
        reservation stops the configuration at upgrade time from releasing it.
        """
        reservation = operation.reservation_micros or 0
        if reservation <= 0:
            return 0
        reproducible = (
            self.full_lifetime_micros(
                self.price_ceiling_micros(operation),
                self.lifetime_seconds(operation),
                self.incidental_for(operation),
            )
            + self.buffer_for(operation)
        )
        return reservation if reservation > reproducible else 0

    # -- pricing --------------------------------------------------------
    def full_lifetime_micros(
        self, price_micros=0, lifetime_seconds=None, incidental_per_hour_micros=None
    ):
        """Cost of one whole lifetime at a price, cap by default.

        The age-out settlement uses the operation's own lifetime and price,
        because those are the only evidence it has; a row without a price uses
        the configured cap. That keeps the settlement inside the forward
        commitment, which prices unlived time at the operation's price ceiling.
        """
        seconds = self.max_lifetime_seconds if lifetime_seconds is None else lifetime_seconds
        price = price_micros or self.price_cap_micros
        incidental = (
            self.incidental_per_hour_micros
            if incidental_per_hour_micros is None
            else incidental_per_hour_micros
        )
        return hourly_cost_micros(price, seconds) + hourly_cost_micros(incidental, seconds)

    def per_launch_micros(self):
        """Worst case one newly admitted worker can cost, plus its buffer."""
        return self.full_lifetime_micros() + self.buffer_micros

    def lifetime_seconds(self, operation):
        """The operation's own lifetime bound, from its durable expiry window.

        A configuration change applies to the next launch, not to a worker that is
        already admitted: that worker and the reaper use the expiry it stored.
        Rounding up keeps the bound no shorter than the configured lifetime that
        produced it.
        """
        return max(
            MINIMUM_BILLABLE_SECONDS,
            int(math.ceil(operation.expires_at - operation.created_at)),
        )

    def elapsed_seconds(self, operation, now):
        """Seconds of instance life that may already be billed.

        The provider's launch time is used when it is known; until the first
        reconcile records it, the operation's creation time is the conservative
        substitute. A terminal operation stops at the moment its terminal state
        was observed, so a later status read does not grow its cost.
        """
        if operation.instance_id is None:
            return 0
        start = operation.launch_time if operation.launch_time is not None else operation.created_at
        end = operation.updated_at if operation.state in TERMINAL_STATES else min(now, operation.expires_at)
        seconds = end - start
        if seconds <= 0:
            return 0
        return int(math.ceil(min(seconds, self.lifetime_seconds(operation))))

    def billable_seconds(self, operation, now):
        """Billed duration so far, floored at the provider's one-minute minimum.

        The floor is the billed duration itself, not an extra interval: elapsed
        and remaining time share it, so the minimum is counted once.
        """
        return max(MINIMUM_BILLABLE_SECONDS, self.elapsed_seconds(operation, now))

    # -- cost -----------------------------------------------------------
    def accrued_micros(self, operation, now):
        """Tracked cost so far; zero until an instance may exist."""
        if operation.instance_id is None:
            return 0
        seconds = self.billable_seconds(operation, now)
        price = operation.price_micros or self.price_ceiling_micros(operation)
        incidental = self.incidental_for(operation)
        return hourly_cost_micros(price, seconds) + hourly_cost_micros(incidental, seconds)

    def remaining_micros(self, operation, now):
        """Worst-case cost of the life this worker has left.

        The billed duration is where the remaining life starts, so the minimum
        interval is never charged twice. The price is the highest of the
        configured maximum, the frozen request's ``MaxPrice`` and the accepted
        price: a later configuration change that lowers the ceiling must not
        shrink the commitment of an operation already accepted above it, and it
        cannot reprice the remaining life of one admitted above it either.
        """
        if operation.state in TERMINAL_STATES:
            return 0
        lifetime = self.lifetime_seconds(operation)
        if operation.instance_id is None:
            seconds = lifetime
        else:
            seconds = max(0, lifetime - self.billable_seconds(operation, now))
        price = self.price_ceiling_micros(operation)
        incidental = self.incidental_for(operation)
        remaining = hourly_cost_micros(price, seconds) + hourly_cost_micros(incidental, seconds)
        floor = self.reservation_floor_micros(operation)
        if floor:
            # The unaccrued part of the admitted worst case, less the buffer the
            # caller adds back: a backfilled row keeps the commitment admission
            # made even when the current configuration reproduces less.
            remaining = max(
                remaining,
                floor - self.accrued_micros(operation, now) - self.buffer_for(operation),
            )
        return remaining

    def committed_micros(self, operation, now):
        """Spend already made plus everything this operation can still cost."""
        if operation.state in TERMINAL_STATES:
            if operation.settled_micros is not None:
                return operation.settled_micros
            return self.accrued_micros(operation, now)
        return (
            self.accrued_micros(operation, now)
            + self.remaining_micros(operation, now)
            + self.buffer_for(operation)
        )
