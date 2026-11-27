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

import hashlib
import json
import math
from dataclasses import dataclass

from .money import MICROS_PER_UNIT, format_money, hourly_cost_micros

#: EC2 bills an instance for at least one minute even when it exits sooner.
MINIMUM_BILLABLE_SECONDS = 60
TERMINAL_STATES = ("terminal", "failed")
#: A stored hourly price above one million units is no usable price, so no stored text can push the cost arithmetic
#: or what the export renders past the interpreter's digit limit.
MAX_STORED_PRICE_MICROS = 10**12


def _stored_price_micros(value):
    """Exact micros of one stored price, or zero when it is not the text admission writes.

    Admission writes ``MaxPrice`` with ``format_money``, so a usable price is a
    string that ``format_money`` writes back unchanged, up to ``MAX_STORED_PRICE_MICROS``.
    Any other value, a JSON number, leading zeros, another fraction length, a
    digit outside ASCII or a larger price included, is no usable price. A malformed stored request must never raise
    while cost is reported, so it contributes nothing and the other bounds apply.
    """
    if not isinstance(value, str):
        return 0
    whole, _, fraction = value.partition(".")
    try:
        micros = int(whole) * MICROS_PER_UNIT + int(fraction)
        canonical = 0 < micros <= MAX_STORED_PRICE_MICROS and format_money(micros) == value
    except ValueError:
        # ``int`` and ``str`` refuse a number past the interpreter's digit limit.
        return 0
    return micros if canonical else 0


def request_damaged(operation):
    """Whether an operation's stored launch request is not the one admission froze.

    Admission stores the request as canonical JSON with the sha256 of that text,
    and writes a ``MaxPrice`` into every request. A stored request whose text no
    longer hashes to the stored ``request_sha256``, or that carries no usable
    ``MaxPrice``, is damaged: a resume must not send it, and its price must not
    lower the ceiling admission committed.
    """
    text = operation.request_json
    if not isinstance(text, str):
        return True
    try:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeError:
        return True
    return digest != operation.request_sha256 or not _frozen_max_price_micros(text)


#: ``_decoded_request``'s answer for a frozen request that does not decode.
UNDECODABLE = object()


def _decoded_request(request_json):
    """The frozen request, or ``UNDECODABLE``.

    ``ValueError`` covers text that is not JSON and bytes that are not UTF-8,
    ``TypeError`` a value that is not text, and ``RecursionError`` text nested
    past the decoder's limit. None of them may stop a pass that prices the rows
    of the window, terminal ones too.
    """
    try:
        return json.loads(request_json)
    except (TypeError, ValueError, RecursionError):
        return UNDECODABLE


def _frozen_max_price_micros(request_json):
    """The ``MaxPrice`` frozen in an operation's request, or zero.

    The ceiling admission committed is already stored in the request the
    provider received; a schema-3 row does not need a second column for it. A
    request that has no usable ``MaxPrice`` (it does not decode, or the price is
    missing, null or malformed) gives zero here, and an operation that still
    needs its ceiling keeps its reservation instead (``reservation_floor_micros``).
    """
    request = _decoded_request(request_json)
    try:
        value = request["InstanceMarketOptions"]["SpotOptions"]["MaxPrice"]
    except (TypeError, KeyError, IndexError):
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
        """The reservation kept for a row whose admitted inputs were backfilled.

        The schema-3 upgrade persists ``admitted_inputs_backfilled`` on every
        non-terminal row it backfills, because their real inputs are unknown.
        The stored reservation is then the only record of the worst case
        admission committed, so it is the floor. The decision is read from the
        row, never inferred from a whole-life estimate: an estimate includes the
        current configured ceiling, so raising it could reach the reservation,
        switch an inferred floor off and release allowance admission had
        committed.

        The floor is the reservation itself, which is the admitted worst case,
        so no further bound applies: it cannot exceed what the row promised.
        Every normally admitted row carries a zero flag and therefore no floor,
        and its commitment may fall below the reservation as its minutes are
        accepted under the ceiling.

        A non-terminal row whose stored request is damaged (``request_damaged``:
        it no longer hashes to its stored hash, or has no usable ``MaxPrice``)
        keeps the same floor. Its price is no longer the ceiling admission
        committed, and the configured maximum, which may since have been
        lowered, and the accepted price would price the remaining life below that
        ceiling. A terminal row needs no ceiling for the remaining life, so a
        damaged request gives it no floor.
        """
        damaged = operation.state not in TERMINAL_STATES and request_damaged(operation)
        if not (operation.admitted_inputs_backfilled or damaged):
            return 0
        return operation.reservation_micros or 0

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
            # made even when the current configuration prices its remaining life
            # lower. A raised ceiling can only add to this, never subtract.
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
