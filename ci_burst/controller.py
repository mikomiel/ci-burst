"""The bounded controller: observe, admit, launch, reconcile and expire.

The controller is a capacity decision, not a scheduler. It never assigns jobs,
retries jobs or records a CI result. Forgejo remains the authority for work and
results; AWS remains the authority for instances. Every mutating path requires
an explicit apply call and a writable ledger.

Placement is an ordered list of availability-zone candidates. Admission prices
every candidate and selects the first eligible one; a candidate above the price
ceiling is skipped rather than fatal. Demand drives a bounded fleet: each
admitted worker gets its own operation, reservation and client token, and the
batch is capped by the configured limit and the remaining allowance. Only a
definite, capacity-related provider rejection may advance the same operation to
the next candidate, in order, because that rejection proves no instance was
created.

A launch intent is durable before the cloud request. While an intent has no
confirmed instance, apply resumes that same frozen request and client token
instead of allocating a replacement; EC2 idempotency makes the retry safe and
the 24-hour client-token validity bounds how long that is attempted.
"""

import hashlib
import json
import math
import time
import uuid
from dataclasses import dataclass, replace

from .aws import (
    DEPLOYMENT_TAG,
    EXPIRES_TAG,
    MANAGED_TAG,
    OPERATION_TAG,
    Aws,
    SpotQuote,
    Template,
    is_capacity_failure,
)
from .config import ZoneCandidate
from .cost import CostModel
from .errors import ProviderRefusal, Refusal, StateRefusal
from .forgejo import ForgejoObserver
from .ledger import Ledger, current_window
from .money import format_money

WRITER_TOPOLOGY = "one deployment, one controller host, one durable ledger, one writer"

#: EC2 documents a client token as valid for 24 hours. Beyond that a retry is
#: no longer idempotent, so the unresolved intent blocks and needs an operator.
CLIENT_TOKEN_MAX_AGE_SECONDS = 24 * 60 * 60

#: EC2 retains a terminated instance record for roughly an hour. Absence from a
#: by-id read inside that window is not proof of termination; the operation
#: stays outstanding until the record could have disappeared.
INSTANCE_RETENTION_SECONDS = 60 * 60

COST_SCOPE_NOTE = (
    "Tracked cost estimates compute at the Spot price taken when the launch was accepted, plus "
    "one configured hourly incidental rate; the forward commitment prices unlived time at the "
    "configured maximum Spot price. The estimate does not bound EBS, data transfer, log delivery, "
    "minimum billing increments, failed cleanup or price movement, and it is not a guaranteed cap "
    "on the AWS bill."
)


def _sha256(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CandidatePlan:
    """One ordered placement candidate and the evidence gathered for it."""

    candidate: ZoneCandidate
    template: Template
    quote: SpotQuote | None
    reason: str | None

    @property
    def eligible(self):
        return self.reason is None

    def as_dict(self):
        data = {
            "zone": self.candidate.availability_zone,
            "subnet_id": self.candidate.subnet_id,
            "launch_template_version": self.candidate.launch_template_version,
            "status": "eligible" if self.eligible else "skipped",
            "price": self.quote.as_dict() if self.quote is not None else None,
        }
        if self.reason is not None:
            data["reason"] = self.reason
        return data


class Controller:
    def __init__(self, config, *, observer=None, aws=None, ledger=None, clock=time.time):
        self.config = config
        self.clock = clock
        self.observer = observer or ForgejoObserver(config)
        self.aws = aws or Aws(config, clock=clock)
        self.ledger = ledger or Ledger(config.state.database, clock=clock)
        self.cost = CostModel.from_config(config)

    # -- operation ------------------------------------------------------
    def plan(self):
        """Report the decision that an apply would take, without mutating."""
        try:
            return self._plan()
        finally:
            self.ledger.close()

    def _plan(self):
        result = self._base("plan")
        result["inventory_note"] = "apply reconciles the cloud inventory; plan reads only durable state"
        state = self._read_state(required=False)
        result["state"] = state
        if state.get("invalid"):
            result["refusal"] = {"reason": state["invalid"]}
            result["decision"] = "refused"
            return result
        slot = self._slot_from_state(state)
        if any(
            operation["region"] != self.config.aws.region for operation in slot["operations"]
        ):
            result["refusal"] = {"reason": "SCOPE_CHANGED"}
            result["decision"] = "refused"
            return result
        result["slot"] = slot
        observation = self._observe_or_refuse(result)
        if observation is None:
            result["decision"] = "refused"
            return result
        result["observation"] = observation.as_dict()
        decision, _ = self._decide(observation, slot)
        result.update(decision)
        if result["decision"] == "launch_ready":
            result["decision"] = "would_launch"
        return result

    def apply(self):
        """Take one bounded admission and launch decision."""
        result = self._base("apply")
        self.config.require_live_contract()
        # The writer lock precedes the connect: a version-1 file is copied and
        # upgraded by that connect, and that mutation may not race a second writer.
        self.ledger.acquire_writer()
        self.ledger.connect(
            create=True,
            backfill=(self.config.aws.max_spot_price, self.config.budget.incidental_per_hour),
        )
        try:
            account = self.aws.account_id()
            instances = self.aws.owned_instances(self.config.deployment)
            with self.ledger.transaction():
                slot, discrepancies = self._reconcile(instances, account)
            if discrepancies:
                slot["discrepancies"] = discrepancies
            result["slot"] = slot
            if discrepancies:
                result["decision"] = "refused"
                result.setdefault("reasons", []).append("owned_state_discrepancy")
                return result
            if self._resume(slot, result):
                return result
            if self._expire(slot, instances, result):
                return result
            observation = self._observe_or_refuse(result)
            if observation is None:
                return result
            result["observation"] = observation.as_dict()
            decision, plans = self._decide(observation, slot)
            result.update(decision)
            if decision["decision"] != "launch_ready":
                result["decision"] = decision["decision"]
                return result
            return self._launch(result, decision, plans, account)
        finally:
            self.ledger.close()

    def status(self):
        """Report known state and every evidence gap without mutating anything."""
        try:
            return self._status()
        finally:
            self.ledger.close()

    def _status(self):
        result = self._base("status")
        state = self._read_state(required=False)
        result["state"] = state
        result["slot"] = self._slot_from_state(state)
        window_start = current_window(self.clock(), self.config.budget.window_seconds)
        totals = (
            self.ledger.window_totals(window_start, self.cost, self.clock())
            if self.ledger.available
            else {"spend": 0, "committed": 0}
        )
        launches = self.ledger.window_launches(window_start) if self.ledger.available else 0
        result["controller"] = {"max_instances": self.config.controller.max_instances}
        result["budget"] = {
            "window_start": window_start,
            "window_seconds": self.config.budget.window_seconds,
            "allowance": format_money(self.config.budget.allowance),
            "spend": format_money(totals["spend"]),
            "committed": format_money(totals["committed"]),
            "remaining": format_money(self.config.budget.allowance - totals["committed"]),
            "launches": launches,
            "busy_instances": result["slot"]["instances"],
            "hourly_ceiling": format_money(
                (self.config.aws.max_spot_price + self.config.budget.incidental_per_hour)
                * self.config.controller.max_instances
            ),
        }
        try:
            instances = self.aws.owned_instances(self.config.deployment)
        except Refusal as refusal:
            result["cloud"] = {"available": False, "refusal": refusal.as_dict()}
        else:
            result["cloud"] = {
                "available": True,
                "instances": [instance.as_dict() for instance in instances],
            }
            discrepancies, ignored = self._discrepancies(state.get("operations", []), instances)
            result["slot"]["discrepancies"] = discrepancies
            if ignored:
                result["slot"]["ignored_discrepancies"] = ignored
            result["runner_readiness"] = {
                "known": False,
                "detail": "an EC2 running state is not proof of runner registration or readiness",
            }
        result["decision"] = "reported"
        return result

    def reap(self, apply=False):
        """Select expired owned instances without Forgejo or the scaling ledger."""
        result = self._base("reap")
        result["apply"] = bool(apply)
        now = self.clock()
        instances = self.aws.owned_instances(self.config.deployment)
        expired, refused = [], []
        for instance in instances:
            if instance.state in ("shutting-down", "terminated"):
                continue
            operation = instance.tags.get(OPERATION_TAG)
            if not isinstance(operation, str) or not operation or operation == "pending":
                refused.append({"instance_id": instance.instance_id, "reason": "OPERATION_TAG_INVALID"})
                continue
            expiry = _expiry(instance)
            if expiry is None:
                refused.append({"instance_id": instance.instance_id, "reason": "EXPIRY_TAG_INVALID"})
                continue
            if expiry <= now:
                expired.append(instance.as_dict())
        result["expired"] = expired
        result["refused_resources"] = refused
        if not apply:
            result["decision"] = "planned"
            result["termination_requested"] = []
            return result
        identifiers = [item["instance_id"] for item in expired]
        result["termination_requested"] = identifiers
        if identifiers:
            try:
                self.aws.terminate(identifiers)
            except ProviderRefusal as refusal:
                result["decision"] = "cleanup_failed"
                result["refusal"] = refusal.as_dict()
                return result
        result["decision"] = "termination_requested"
        result["confirmation"] = (
            "a termination request is not confirmed removal; re-run status or reap to observe terminal state"
        )
        return result

    # -- internals ------------------------------------------------------
    def _base(self, command):
        return {
            "command": command,
            "deployment": self.config.deployment,
            "region": self.config.aws.region,
            "writer_topology": WRITER_TOPOLOGY,
            "cost_scope": COST_SCOPE_NOTE,
        }

    def _read_state(self, required):
        if required:
            self.ledger.connect(create=False)
        else:
            if not self.config.state.database.exists():
                return {"available": False, "absent": True}
            try:
                self.ledger.connect(create=False, read_only=True)
            except StateRefusal as refusal:
                return {"available": False, "invalid": refusal.reason}
        if not self.ledger.available:
            if required:
                raise StateRefusal("STATE_UNAVAILABLE")
            return {"available": False, "operations": []}
        return {
            "available": True,
            "path": str(self.config.state.database),
            "operations": [operation.as_dict() for operation in self.ledger.operations()],
        }

    def _slot_from_state(self, state):
        operations = state.get("operations", [])
        busy = [operation for operation in operations if operation["state"] not in ("terminal", "failed")]
        return {
            "busy": bool(busy),
            "instances": len(busy),
            "limit": self.config.controller.max_instances,
            "operations": busy,
            "discrepancies": [],
        }

    def _observe_or_refuse(self, result):
        try:
            return self.observer.observe()
        except Refusal as refusal:
            result["refusal"] = refusal.as_dict()
            result["decision"] = "refused"
            return None

    def _templates(self, decision):
        """Read each distinct configured template version once."""
        templates = {}
        for candidate in self.config.aws.zones:
            version = candidate.launch_template_version
            if version in templates:
                continue
            try:
                templates[version] = self.aws.template(self.config.aws.launch_template_name, version)
            except Refusal as refusal:
                decision["template_error"] = refusal.as_dict()
                return None
        return templates

    def _candidate_plans(self, templates):
        """Price every candidate in order, before any time-sensitive check."""
        plans = []
        for candidate in self.config.aws.zones:
            template = templates[candidate.launch_template_version]
            if not template.has_user_data:
                plans.append(CandidatePlan(candidate, template, None, "template_bootstrap_missing"))
                continue
            if template.placement_zone is not None and template.placement_zone != candidate.availability_zone:
                plans.append(CandidatePlan(candidate, template, None, "template_placement_mismatch"))
                continue
            # A provider error here is not a candidate property: the price
            # evidence itself is unavailable, so the whole plan refuses rather
            # than silently dropping a configured candidate.
            quote = self.aws.spot_quote(template.instance_type, candidate.availability_zone)
            if quote is None:
                plans.append(CandidatePlan(candidate, template, None, "spot_price_unavailable"))
                continue
            plans.append(CandidatePlan(candidate, template, quote, None))
        return plans

    def _decide(self, observation, slot):
        """Return the decision block and the ordered candidate plans behind it."""
        reasons, notes = [], []
        limit = slot["limit"]
        instances = slot["instances"]
        free = limit - instances
        decision = {
            "reasons": reasons,
            "notes": notes,
            "demand": observation.demand,
            "free_instances": free,
            "launch_count": 0,
        }
        if instances > limit:
            # Lowering the limit never evicts a worker. It only stops admission
            # until enough workers terminate on their own lifetime.
            notes.append("above_configured_limit")
        if slot.get("discrepancies"):
            decision["decision"] = "refused"
            reasons.append("owned_state_discrepancy")
            return decision, ()
        if free <= 0:
            decision["decision"] = "wait"
            reasons.append("slot_busy")
            return decision, ()
        if observation.demand == 0:
            decision["decision"] = "wait"
            reasons.append("no_runnable_demand")
            return decision, ()
        requested = min(observation.demand, free)
        templates = self._templates(decision)
        if templates is None:
            decision["decision"] = "refused"
            reasons.append("template_unusable")
            return decision, ()
        try:
            plans = self._candidate_plans(templates)
        except Refusal as refusal:
            decision["decision"] = "refused"
            decision["refusal"] = refusal.as_dict()
            reasons.append("spot_price_unavailable")
            return decision, ()
        # Every quote read is synchronous and can be slow. Freshness, scope,
        # the cap, admission and the lifetime are evaluated against the clock
        # after the last read, so a slow provider cannot keep stale evidence
        # fresh or commit an already expired intent.
        now = self.clock()
        judged = []
        for plan in plans:
            if plan.reason is not None:
                judged.append(plan)
            elif now - plan.quote.observed_at > self.config.aws.price_max_age_seconds:
                judged.append(replace(plan, reason="spot_price_stale"))
            elif (
                plan.quote.availability_zone != plan.candidate.availability_zone
                or plan.quote.instance_type != plan.template.instance_type
            ):
                judged.append(replace(plan, reason="spot_price_wrong_scope"))
            elif plan.quote.price_micros > self.config.aws.max_spot_price:
                judged.append(replace(plan, reason="spot_price_above_cap"))
            else:
                judged.append(plan)
        plans = judged
        decision["candidates"] = [plan.as_dict() for plan in plans]
        eligible = [plan for plan in plans if plan.eligible]
        if not eligible:
            decision["decision"] = "refused"
            for plan in plans:
                if plan.reason is not None and plan.reason not in reasons:
                    reasons.append(plan.reason)
            reasons.append("no_eligible_candidate")
            return decision, plans
        window_start = current_window(now, self.config.budget.window_seconds)
        per_launch = self.cost.per_launch_micros()
        totals = (
            self.ledger.window_totals(window_start, self.cost, now)
            if self.ledger.available
            else {"spend": 0, "committed": 0}
        )
        committed = totals["committed"]
        # A commitment above the allowance permits no launch; the count of
        # affordable worst-case workers is never negative.
        affordable = max(0, (self.config.budget.allowance - committed) // per_launch)
        launch_count = min(requested, min(affordable, free))
        decision["launch_count"] = launch_count
        decision["budget"] = {
            "window_start": window_start,
            "window_seconds": self.config.budget.window_seconds,
            "allowance": format_money(self.config.budget.allowance),
            "spend": format_money(totals["spend"]),
            "committed": format_money(committed),
            "remaining": format_money(self.config.budget.allowance - committed),
            # Kept for consumers that predate the fleet: the worst case one
            # worker costs. ``per_launch_reservation`` is the clearer name.
            "required_reservation": format_money(per_launch),
            "per_launch_reservation": format_money(per_launch),
            "launch_count": launch_count,
            "remaining_after": format_money(self.config.budget.allowance - committed - per_launch),
        }
        if affordable < requested:
            notes.append("budget_capped_batch")
        if launch_count == 0:
            decision["decision"] = "refused"
            reasons.append("budget_allowance_exhausted")
            return decision, plans
        selected = eligible[0]
        decision["price"] = selected.quote.as_dict() | {"cap": format_money(self.config.aws.max_spot_price)}
        decision["selected"] = selected.as_dict()
        decision["template"] = selected.template.as_dict()
        decision["decision"] = "launch_ready"
        decision["reservation_micros"] = per_launch
        decision["window_start"] = window_start
        decision["expires_at"] = now + self.config.aws.max_lifetime_seconds
        return decision, plans

    def _request(self, plan, expires_at, operation_id):
        """Build the one launch request for a candidate."""
        request = {
            "LaunchTemplate": {
                "LaunchTemplateId": plan.template.template_id,
                "Version": str(plan.template.version),
            },
            "Placement": {"AvailabilityZone": plan.candidate.availability_zone},
            "InstanceMarketOptions": {
                "MarketType": "spot",
                "SpotOptions": {
                    "MaxPrice": format_money(self.config.aws.max_spot_price),
                    "SpotInstanceType": "one-time",
                    "InstanceInterruptionBehavior": "terminate",
                },
            },
            "MinCount": 1,
            "MaxCount": 1,
            "InstanceInitiatedShutdownBehavior": "terminate",
            "ClientToken": "ci-burst-" + uuid.uuid4().hex,
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": MANAGED_TAG, "Value": "true"},
                        {"Key": DEPLOYMENT_TAG, "Value": self.config.deployment},
                        {"Key": OPERATION_TAG, "Value": operation_id},
                        {"Key": EXPIRES_TAG, "Value": str(int(expires_at))},
                        {"Key": "Name", "Value": f"{self.config.deployment}-spot"},
                    ],
                }
            ],
        }
        # RunInstances takes the subnet as a top-level parameter; Placement has
        # no SubnetId member. The legacy single-zone form sends none and keeps
        # the network choice in the launch template.
        if plan.candidate.subnet_id is not None:
            request["SubnetId"] = plan.candidate.subnet_id
        return request

    def _launch(self, result, decision, plans, account):
        """Launch the admitted workers, one operation and token per worker.

        The candidate order is rotated cyclically by the worker index, and each
        worker starts at the first candidate whose quote is still fresh; earlier
        candidates in that rotation are recorded as skipped. A worker is
        committed only while the shared admission lifetime remains, so a slow
        earlier request cannot create an intent that is already expired. A
        launch that does not end accepted stops the rest of the batch; the
        workers already launched stay in place.
        """
        eligible = [plan for plan in plans if plan.eligible]
        launches = []
        result["launches"] = launches
        for index in range(decision["launch_count"]):
            if decision["expires_at"] <= self.clock():
                # The shared lifetime can elapse while an earlier worker's
                # request is in flight; committing another intent now would
                # create an instance that is expired on arrival.
                result["decision"] = "launched" if launches else "refused"
                result.setdefault("reasons", []).append("admission_expired")
                return result
            start = index % len(eligible)
            rotated = eligible[start:] + eligible[:start]
            position, skipped = self._first_fresh(rotated)
            if position is None:
                # No remaining worker can start on fresh evidence, and nothing
                # was committed for this one.
                result["decision"] = "launched" if launches else "refused"
                result.setdefault("reasons", []).append("spot_price_stale")
                result["attempts"] = skipped
                return result
            order = rotated[position:]
            selected = order[0]
            operation_id = uuid.uuid4().hex
            request = self._request(selected, decision["expires_at"], operation_id)
            request_sha256 = _sha256(request)
            with self.ledger.transaction():
                operation = self.ledger.new_operation(
                    operation_id=operation_id,
                    client_token=request["ClientToken"],
                    request=request,
                    request_sha256=request_sha256,
                    expires_at=decision["expires_at"],
                    reservation_micros=decision["reservation_micros"],
                    window_start=decision["window_start"],
                    region=self.config.aws.region,
                    account=account,
                    detail=request_sha256,
                    price_micros=selected.quote.price_micros,
                )
            # Intent is committed before the request, so a lost response is
            # recoverable and never a reason to allocate a second operation for
            # the same worker slot.
            attempt = {}
            self._send_candidates(operation, order, attempt, decision)
            attempts = skipped + attempt.get("attempts", [])
            launches.append(
                {
                    "operation": operation.id,
                    "instance_id": attempt.get("instance_id"),
                    "zone": self._accepted_zone(attempts, operation),
                    "reservation": attempt.get(
                        "reservation", format_money(operation.reservation_micros)
                    ),
                    "decision": attempt.get("decision"),
                    "attempts": attempts,
                }
            )
            if index == 0:
                # Existing consumers read the first launch at the top level.
                for key in ("operation", "instance_id", "expires_at", "reservation"):
                    if key in attempt:
                        result[key] = attempt[key]
                result["attempts"] = attempts
            if attempt.get("decision") != "launched":
                decision = attempt.get("decision", "launch_failed")
                if decision == "refused" and len(launches) > 1:
                    # A refusal means nothing was mutated. This batch already
                    # launched at least one worker, so report the stop as a
                    # launch failure instead.
                    decision = "launch_failed"
                result["decision"] = decision
                for key in ("reasons", "refusal"):
                    if key in attempt:
                        result[key] = attempt[key]
                result["attempts"] = attempts
                return result
        result["decision"] = "launched"
        return result

    def _first_fresh(self, plans):
        """Return the first fresh candidate's position and the skipped ones."""
        now = self.clock()
        skipped = []
        for position, plan in enumerate(plans):
            if now - plan.quote.observed_at > self.config.aws.price_max_age_seconds:
                skipped.append(
                    {
                        "zone": plan.candidate.availability_zone,
                        "outcome": "skipped",
                        "reason": "spot_price_stale",
                    }
                )
                continue
            return position, skipped
        return None, skipped

    def _accepted_zone(self, attempts, operation):
        """The placement that accepted this worker, else its final frozen request."""
        for attempt in attempts:
            if attempt.get("outcome") == "accepted":
                return attempt["zone"]
        final = self.ledger.operation(operation.id) or operation
        return final.request()["Placement"]["AvailabilityZone"]

    def _send_candidates(self, operation, plans, result, decision):
        """Attempt the ordered candidates within one admission.

        Only a definite capacity rejection, which proves that no instance was
        created, advances to the next candidate. The operation identity,
        reservation, expiry and admission window stay; the frozen request, its
        hash and its own idempotency token advance with the placement, so the
        same demand is never admitted twice.
        """
        attempts = []
        for index, plan in enumerate(plans):
            if index:
                now = self.clock()
                if decision["expires_at"] <= now:
                    # The next candidate would create an instance that is
                    # expired before it boots. Nothing was created, so the slot
                    # is released.
                    self.ledger.fail_without_reservation(operation.id, detail="admission_expired")
                    result["decision"] = "refused"
                    result.setdefault("reasons", []).append("admission_expired")
                    result["operation"] = operation.id
                    result["attempts"] = attempts
                    return
                if now - plan.quote.observed_at > self.config.aws.price_max_age_seconds:
                    # The quote that made this candidate eligible in _decide
                    # has aged out while the previous attempt was running. It
                    # must not be frozen or sent; a later candidate may still
                    # carry fresh evidence.
                    attempts.append(
                        {
                            "zone": plan.candidate.availability_zone,
                            "outcome": "skipped",
                            "reason": "spot_price_stale",
                        }
                    )
                    continue
                request = self._request(plan, decision["expires_at"], operation.id)
                with self.ledger.transaction():
                    self.ledger.advance_operation(
                        operation.id,
                        request=request,
                        request_sha256=_sha256(request),
                        client_token=request["ClientToken"],
                        detail="capacity_fallback:" + plans[index - 1].candidate.availability_zone,
                        price_micros=plan.quote.price_micros,
                    )
                operation = self.ledger.operation(operation.id)
            else:
                request = operation.request()
            outcome, refusal = self._send(operation, request, result, "launched", defer_capacity=True)
            if outcome == "accepted":
                attempts.append({"zone": plan.candidate.availability_zone, "outcome": "accepted"})
                result["attempts"] = attempts
                return
            attempt = {
                "zone": plan.candidate.availability_zone,
                "outcome": {"capacity": "capacity_rejected", "refused": "rejected"}.get(outcome, outcome),
            }
            attempt.update(refusal.as_dict())
            attempts.append(attempt)
            if outcome != "capacity":
                result["attempts"] = attempts
                return
            if index + 1 == len(plans):
                self.ledger.fail_without_reservation(operation.id, detail=refusal.reason)
                result["decision"] = "launch_failed"
                result["refusal"] = refusal.as_dict()
                result["operation"] = operation.id
                result["attempts"] = attempts
                result.setdefault("reasons", []).append("capacity_exhausted")
                return
        # Nothing was sent after the last definite rejection: every remaining
        # candidate was skipped because its price evidence expired during the
        # earlier attempt. Nothing was created, so release the slot instead of
        # leaving an intent that can only be sent on stale evidence.
        self.ledger.fail_without_reservation(operation.id, detail="spot_price_stale")
        result["decision"] = "refused"
        result.setdefault("reasons", []).append("spot_price_stale")
        result["operation"] = operation.id
        result["attempts"] = attempts

    def _resume(self, slot, result):
        """Retry every unresolved launch intent instead of replacing it.

        Each intent is sent into its own mapping so a later failure can never
        inherit an earlier success's instance identity. The top-level result
        describes the intent that decided the outcome; ``resumes`` keeps one
        entry per attempted intent in creation order.
        """
        pending = [operation for operation in slot["operations"] if not operation.get("instance_id")]
        if not pending:
            return False
        resumes = []
        result["resumes"] = resumes
        for entry in pending:
            operation = self.ledger.operation(entry["operation"])
            # Every outcome carries the durable operation's own expiry and
            # reservation; only the instance identity depends on a confirmed
            # send, and the candidate walk stays empty for a resume.
            attempt = {
                "operation": operation.id,
                "expires_at": operation.expires_at,
                "reservation": format_money(operation.reservation_micros),
                "attempts": [],
            }
            now = self.clock()
            if now - operation.created_at > CLIENT_TOKEN_MAX_AGE_SECONDS:
                attempt["decision"] = "refused"
                attempt["reasons"] = ["unresolved_intent_expired"]
            elif operation.expires_at <= now:
                # Sending an intent whose lifetime already elapsed would create
                # an instance that is expired before it boots. Keep the
                # unresolved ownership and block admission instead.
                attempt["decision"] = "refused"
                attempt["reasons"] = ["unresolved_intent_lifetime_elapsed"]
            else:
                self._send(operation, operation.request(), attempt, "resumed", first_attempt=False)
            record = {
                "operation": operation.id,
                "decision": attempt["decision"],
                "reasons": attempt.get("reasons", []),
                "instance_id": attempt.get("instance_id"),
                "expires_at": attempt["expires_at"],
                "reservation": attempt["reservation"],
                "attempts": attempt["attempts"],
            }
            if "refusal" in attempt:
                record["refusal"] = attempt["refusal"]
            resumes.append(record)
            if record["decision"] != "resumed":
                self._report_resume(result, attempt, operation.id)
                return True
        self._report_resume(result, attempt, operation.id)
        return True

    def _report_resume(self, result, attempt, operation_id):
        """Report one resumed intent at the top level without stale fields."""
        for key in ("operation", "instance_id", "expires_at", "reservation", "attempts", "refusal"):
            result.pop(key, None)
        result["operation"] = operation_id
        result["decision"] = attempt["decision"]
        result["expires_at"] = attempt["expires_at"]
        result["reservation"] = attempt["reservation"]
        if attempt.get("instance_id") is not None:
            result["instance_id"] = attempt["instance_id"]
        if attempt.get("reasons"):
            result["reasons"] = attempt["reasons"]
        else:
            result.pop("reasons", None)
        if "refusal" in attempt:
            result["refusal"] = attempt["refusal"]

    def _send(self, operation, request, result, success_decision, first_attempt=True, *, defer_capacity=False):
        """Send one frozen request and report what it proved.

        Returns an outcome string and, for a deferred capacity rejection, the
        refusal the caller may still act on. Every other outcome is already
        final in ``result``.
        """
        try:
            instance_ids = self.aws.run_spot(request)
        except ProviderRefusal as refusal:
            # A rejected retry does not prove the original request created
            # nothing, so it keeps unresolved ownership. Only a definite
            # rejection of the first attempt may release the slot.
            if refusal.uncertain or not first_attempt:
                self.ledger.set_state(operation.id, "uncertain", detail=refusal.reason)
                result["decision"] = "uncertain"
                outcome = "uncertain"
            elif defer_capacity and is_capacity_failure(refusal):
                return "capacity", refusal
            else:
                self.ledger.fail_without_reservation(operation.id, detail=refusal.reason)
                result["decision"] = "launch_failed"
                outcome = "refused"
            result["refusal"] = refusal.as_dict()
            result["operation"] = operation.id
            return outcome, refusal
        self.ledger.set_state(operation.id, "starting", instance_id=instance_ids[0])
        result["decision"] = success_decision
        result["operation"] = operation.id
        result["instance_id"] = instance_ids[0]
        result["expires_at"] = operation.expires_at
        result["reservation"] = format_money(operation.reservation_micros)
        return "accepted", None

    def _expire(self, slot, instances, result):
        """Terminate only an expired instance whose current ownership is verified."""
        now = self.clock()
        expired = [operation for operation in slot["operations"] if operation["expires_at"] <= now]
        if not expired:
            return False
        inventory = {instance.tags.get(OPERATION_TAG): instance for instance in instances}
        targets, refused = [], []
        for operation in expired:
            instance = inventory.get(operation["operation"])
            if instance is not None and instance.state in ("shutting-down", "terminated"):
                instance = None
            expiry = _expiry(instance) if instance is not None else None
            if instance is None or expiry is None or expiry > now:
                refused.append({"operation": operation["operation"], "reason": "EXPIRY_EVIDENCE_INVALID"})
                continue
            targets.append(instance.instance_id)
        if refused:
            result["expiry_refusals"] = refused
        if not targets:
            return False
        try:
            self.aws.terminate(targets)
        except ProviderRefusal as refusal:
            result["decision"] = "cleanup_failed"
            result["refusal"] = refusal.as_dict()
            return True
        with self.ledger.transaction():
            for operation in expired:
                if operation["instance_id"] in targets:
                    self.ledger.set_state(operation["operation"], "terminating", detail="expired")
        result["expiry_cleanup"] = targets
        result["decision"] = "wait"
        result.setdefault("reasons", []).append("expiry_cleanup_requested")
        return True

    def _reconcile(self, instances, account):
        """Reconcile durable operations with the complete owned inventory."""
        operations = {operation.id: operation for operation in self.ledger.operations()}
        nonterminal = {operation.id: operation for operation in self.ledger.nonterminal_operations()}
        grouped = {}
        discrepancies = []
        ignored = []
        for instance in instances:
            operation_id = instance.tags.get(OPERATION_TAG)
            if not operation_id or operation_id == "pending":
                discrepancies.append({"instance_id": instance.instance_id, "reason": "OWNED_INSTANCE_WITHOUT_OPERATION"})
                continue
            if operation_id not in operations:
                entry = {
                    "instance_id": instance.instance_id,
                    "operation": operation_id,
                    "reason": "UNRECORDED_OWNED_INSTANCE",
                }
                if instance.state == "terminated":
                    # Inventory noise: the record is gone from EC2's world
                    # except for its retained visibility, it holds no capacity
                    # and costs nothing. Report it, never let it stop launches.
                    ignored.append(entry)
                else:
                    discrepancies.append(entry)
                continue
            grouped.setdefault(operation_id, []).append(instance)
        by_operation = {}
        for operation_id, group in grouped.items():
            live = [instance for instance in group if instance.state != "terminated"]
            if len(live) > 1:
                discrepancies.append(
                    {
                        "instance_id": live[1].instance_id,
                        "operation": operation_id,
                        "reason": "DUPLICATE_OPERATION_INSTANCES",
                    }
                )
            chosen = (live or group)[0]
            operation = operations[operation_id]
            if operation_id in nonterminal and operation.instance_id and chosen.instance_id != operation.instance_id:
                # A different record carrying the operation tag cannot replace
                # a confirmed instance identity; the recorded instance may
                # still be running.
                discrepancies.append(
                    {
                        "instance_id": chosen.instance_id,
                        "operation": operation_id,
                        "reason": "INSTANCE_ID_CONFLICT",
                    }
                )
                continue
            by_operation[operation_id] = chosen
            if operation_id not in nonterminal and chosen.state != "terminated":
                discrepancies.append(
                    {
                        "instance_id": chosen.instance_id,
                        "operation": operation_id,
                        "reason": "TERMINAL_OPERATION_STILL_OWNS_INSTANCE",
                    }
                )
        now = self.clock()
        scoped = {}
        for operation in nonterminal.values():
            if operation.region != self.config.aws.region or operation.account != account:
                discrepancies.append(
                    {
                        "operation": operation.id,
                        "reason": "SCOPE_CHANGED",
                        "region": operation.region,
                        "account": operation.account,
                    }
                )
                continue
            scoped[operation.id] = operation
        missing = [operation for operation in scoped.values() if operation.id not in by_operation]
        identifiers = [operation.instance_id for operation in missing if operation.instance_id]
        by_id = {}
        if identifiers:
            by_id = {instance.instance_id: instance for instance in self.aws.instances(identifiers)}
        for operation in missing:
            instance = by_id.get(operation.instance_id) if operation.instance_id else None
            if operation.instance_id and instance is not None:
                self._reconcile_present(operation, instance)
                if instance.state != "terminated":
                    discrepancies.append(
                        {
                            "instance_id": instance.instance_id,
                            "operation": operation.id,
                            "reason": "OWNERSHIP_TAGS_MISSING",
                        }
                    )
            elif operation.instance_id:
                if now - operation.updated_at >= INSTANCE_RETENTION_SECONDS:
                    self._settle_unretained(operation)
                    self.ledger.set_state(operation.id, "terminal", detail="instance_not_retained")
                # Inside the retained-visibility window, absence is not yet
                # proof of termination: keep the slot occupied.
            # An operation without an instance id stays outstanding; apply
            # resumes its frozen request instead of creating a replacement.
        for operation in scoped.values():
            instance = by_operation.get(operation.id)
            if instance is not None:
                self._reconcile_present(operation, instance)
        slot = self._slot_from_state(self._read_state(required=True))
        if ignored:
            slot["ignored_discrepancies"] = ignored
        return slot, discrepancies

    def _reconcile_present(self, operation, instance):
        # A provider launch time is a real timestamp; zero or absent means the
        # adapter could not read one, and creation time stays the substitute.
        if instance.launch_time and operation.launch_time is None:
            self.ledger.set_launch_time(operation.id, instance.launch_time)
            operation = self.ledger.operation(operation.id)
        state = instance.state
        if state == "terminated":
            self._settle(operation)
            self.ledger.set_state(operation.id, "terminal", instance_id=instance.instance_id, detail="instance_terminated")
        elif state in ("shutting-down", "stopping", "stopped"):
            self.ledger.set_state(operation.id, "terminating", instance_id=instance.instance_id)
        elif operation.state == "terminating":
            self.ledger.set_state(operation.id, "terminating", instance_id=instance.instance_id)
        elif state == "running":
            self.ledger.set_state(operation.id, "running", instance_id=instance.instance_id)
        else:
            self.ledger.set_state(operation.id, "starting", instance_id=instance.instance_id)

    def _settle(self, operation):
        """Write the final tracked cost for an operation whose instance ended."""
        if operation.settled_micros is not None:
            return
        now = self.clock()
        self.ledger.settle(operation.id, self.cost.accrued_micros(operation, now), now)

    def _settle_unretained(self, operation):
        """Charge a full lifetime when the provider record aged out unobserved.

        The worker was last seen non-terminal at least the retention window ago
        and its own lifetime is shorter than that, so the full configured
        lifetime is the only defensible estimate; it never under-counts.
        """
        if operation.settled_micros is not None:
            return
        self.ledger.settle(operation.id, self.cost.full_lifetime_micros(), self.clock())

    def _discrepancies(self, operations, instances):
        known = {
            operation["operation"]: (operation["state"], operation["region"]) for operation in operations
        }
        discrepancies = []
        ignored = []
        seen_live = set()
        for instance in instances:
            operation_id = instance.tags.get(OPERATION_TAG)
            if not operation_id or operation_id == "pending":
                discrepancies.append(
                    {"instance_id": instance.instance_id, "operation": operation_id, "reason": "OWNED_INSTANCE_WITHOUT_OPERATION"}
                )
                continue
            if operation_id not in known:
                entry = {
                    "instance_id": instance.instance_id,
                    "operation": operation_id,
                    "reason": "UNRECORDED_OWNED_INSTANCE",
                }
                # A terminated record is inventory noise: report it without
                # presenting it as a reason the deployment cannot launch.
                if instance.state == "terminated":
                    ignored.append(entry)
                else:
                    discrepancies.append(entry)
                continue
            if known[operation_id][1] != self.config.aws.region:
                discrepancies.append(
                    {"instance_id": instance.instance_id, "operation": operation_id, "reason": "SCOPE_CHANGED"}
                )
                continue
            if instance.state == "terminated":
                continue
            if operation_id in seen_live:
                discrepancies.append(
                    {"instance_id": instance.instance_id, "operation": operation_id, "reason": "DUPLICATE_OPERATION_INSTANCES"}
                )
                continue
            seen_live.add(operation_id)
            if known[operation_id][0] in ("terminal", "failed"):
                discrepancies.append(
                    {
                        "instance_id": instance.instance_id,
                        "operation": operation_id,
                        "reason": "TERMINAL_OPERATION_STILL_OWNS_INSTANCE",
                    }
                )
        return discrepancies, ignored


def _expiry(instance):
    raw = instance.tags.get(EXPIRES_TAG)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value
