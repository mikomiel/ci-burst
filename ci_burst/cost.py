"""Tracked cost of one launch operation.

The controller cannot read the amount AWS will bill: the charge depends on the
Spot price over the instance's life, per-second rounding and charges it does not
observe. These functions therefore track an estimate in exact micro-units:
compute at the quote taken when the launch was accepted, plus one configured
hourly rate for the incidentals every worker carries (public IPv4, EBS). Time
not yet lived is priced at the configured Spot maximum, so a forward commitment
can never under-count a worker that is still running.
"""

import math
from dataclasses import dataclass

from .money import hourly_cost_micros

#: EC2 bills an instance for at least one minute even when it exits sooner.
MINIMUM_BILLABLE_SECONDS = 60
TERMINAL_STATES = ("terminal", "failed")


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

    def full_lifetime_micros(self, price_micros=0):
        """Cost of one whole configured lifetime at a price, cap by default.

        The age-out settlement uses the operation's accepted price, because that
        price is the only evidence it has; a row without one uses the cap. The
        caller passing the accepted price keeps the settlement inside the
        forward commitment, which prices unlived time at the cap.
        """
        price = price_micros or self.price_cap_micros
        return hourly_cost_micros(price, self.max_lifetime_seconds) + hourly_cost_micros(
            self.incidental_per_hour_micros, self.max_lifetime_seconds
        )

    def per_launch_micros(self):
        """Worst case one newly admitted worker can cost, plus its buffer."""
        return self.full_lifetime_micros() + self.buffer_micros

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
        return int(math.ceil(min(seconds, self.max_lifetime_seconds)))

    def accrued_micros(self, operation, now):
        """Tracked cost so far; zero until an instance may exist."""
        if operation.instance_id is None:
            return 0
        seconds = max(MINIMUM_BILLABLE_SECONDS, self.elapsed_seconds(operation, now))
        price = operation.price_micros or self.price_cap_micros
        return hourly_cost_micros(price, seconds) + hourly_cost_micros(
            self.incidental_per_hour_micros, seconds
        )

    def remaining_micros(self, operation, now):
        """Worst-case cost of the life this worker has left.

        The accepted price is a floor as well as the configured maximum: a later
        configuration change that lowers the ceiling must not shrink the
        commitment of an operation already accepted above it, because that
        operation still settles at its accepted price.
        """
        if operation.state in TERMINAL_STATES:
            return 0
        if operation.instance_id is None:
            seconds = self.max_lifetime_seconds
        else:
            seconds = max(0, self.max_lifetime_seconds - self.elapsed_seconds(operation, now))
        price = max(self.price_cap_micros, operation.price_micros or 0)
        return hourly_cost_micros(price, seconds) + hourly_cost_micros(
            self.incidental_per_hour_micros, seconds
        )

    def committed_micros(self, operation, now):
        """Spend already made plus everything this operation can still cost."""
        if operation.state in TERMINAL_STATES:
            if operation.settled_micros is not None:
                return operation.settled_micros
            return self.accrued_micros(operation, now)
        return (
            self.accrued_micros(operation, now)
            + self.remaining_micros(operation, now)
            + self.buffer_micros
        )
