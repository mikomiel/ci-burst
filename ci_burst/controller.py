"""The bounded controller: observe, admit, launch, reconcile and expire.

The controller is a capacity decision, not a scheduler. It never assigns jobs,
retries jobs or records a CI result. Forgejo remains the authority for work and
results; AWS remains the authority for instances. Every mutating path requires
an explicit apply call and a writable ledger.

Placement is an ordered list of availability-zone candidates. Admission prices
every candidate and selects the first eligible one; a candidate above the price
ceiling is skipped rather than fatal. The launch still admits at most one
instance: one operation, one reservation and one slot. Only a definite,
capacity-related provider rejection may advance the same operation to the next
candidate, in order, because that rejection proves no instance was created.

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
from .errors import ProviderRefusal, Refusal, StateRefusal
from .forgejo import ForgejoObserver
from .ledger import Ledger, current_window
from .money import format_money, hourly_cost_micros

WRITER_TOPOLOGY = "one deployment, one controller host, one durable ledger, one writer"

#: EC2 documents a client token as valid for 24 hours. Beyond that a retry is
#: no longer idempotent, so the unresolved intent blocks and needs an operator.
CLIENT_TOKEN_MAX_AGE_SECONDS = 24 * 60 * 60

#: EC2 retains a terminated instance record for roughly an hour. Absence from a
#: by-id read inside that window is not proof of termination; the operation
#: stays outstanding until the record could have disappeared.
INSTANCE_RETENTION_SECONDS = 60 * 60

COST_SCOPE_NOTE = (
    "The reservation estimates compute at the configured maximum Spot price for the configured "
    "maximum lifetime, plus the configured non-compute allowance. It does not bound EBS, data "
    "transfer, public addressing, log delivery, minimum billing increments, failed cleanup or "
    "other charges, and it is not a guaranteed cap on the AWS bill."
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
        self.ledger.connect(create=True)
        self.ledger.acquire_writer()
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
        try:
            instances = self.aws.owned_instances(self.config.deployment)
        except Refusal as refusal:
            result["cloud"] = {"available": False, "refusal": refusal.as_dict()}
        else:
            result["cloud"] = {
                "available": True,
                "instances": [instance.as_dict() for instance in instances],
            }
            result["slot"]["discrepancies"] = self._discrepancies(state.get("operations", []), instances)
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
        return {"busy": bool(busy), "operations": busy, "discrepancies": []}

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
        decision = {"reasons": reasons, "notes": notes}
        if slot.get("discrepancies"):
            decision["decision"] = "refused"
            reasons.append("owned_state_discrepancy")
            return decision, ()
        if slot["busy"]:
            decision["decision"] = "wait"
            reasons.append("slot_busy")
            return decision, ()
        if observation.demand == 0:
            decision["decision"] = "wait"
            reasons.append("no_runnable_demand")
            return decision, ()
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
        reservation = hourly_cost_micros(
            self.config.aws.max_spot_price, self.config.aws.max_lifetime_seconds
        ) + self.config.budget.non_compute_allowance
        consumed = self.ledger.consumed_micros(window_start) if self.ledger.available else 0
        decision["budget"] = {
            "window_start": window_start,
            "window_seconds": self.config.budget.window_seconds,
            "allowance": format_money(self.config.budget.allowance),
            "consumed": format_money(consumed),
            "required_reservation": format_money(reservation),
            "remaining_after": format_money(self.config.budget.allowance - consumed - reservation),
        }
        if consumed + reservation > self.config.budget.allowance:
            decision["decision"] = "refused"
            reasons.append("budget_allowance_exhausted")
            return decision, plans
        selected = eligible[0]
        decision["price"] = selected.quote.as_dict() | {"cap": format_money(self.config.aws.max_spot_price)}
        decision["selected"] = selected.as_dict()
        decision["template"] = selected.template.as_dict()
        decision["decision"] = "launch_ready"
        decision["reservation_micros"] = reservation
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
        now = self.clock()
        if decision["expires_at"] <= now:
            result["decision"] = "refused"
            result.setdefault("reasons", []).append("admission_expired")
            return result
        eligible = [plan for plan in plans if plan.eligible]
        operation_id = uuid.uuid4().hex
        request = self._request(eligible[0], decision["expires_at"], operation_id)
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
            )
        # Intent is committed, so a lost response is recoverable and never a
        # reason to allocate a second operation for the same demand.
        self._send_candidates(operation, eligible, result, decision)
        return result

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
        """Retry the one unresolved launch intent instead of replacing it."""
        pending = [operation for operation in slot["operations"] if not operation.get("instance_id")]
        if not pending:
            return False
        if len(pending) > 1:
            result["decision"] = "refused"
            result.setdefault("reasons", []).append("unresolved_intent_conflict")
            return True
        operation_id = pending[0]["operation"]
        operation = self.ledger.operation(operation_id)
        now = self.clock()
        if now - operation.created_at > CLIENT_TOKEN_MAX_AGE_SECONDS:
            result["decision"] = "refused"
            result.setdefault("reasons", []).append("unresolved_intent_expired")
            result["operation"] = operation.id
            return True
        if operation.expires_at <= now:
            # Sending an intent whose lifetime already elapsed would create an
            # instance that is expired before it boots. Keep the unresolved
            # ownership and block admission instead.
            result["decision"] = "refused"
            result.setdefault("reasons", []).append("unresolved_intent_lifetime_elapsed")
            result["operation"] = operation.id
            return True
        self._send(operation, operation.request(), result, "resumed", first_attempt=False)
        return True

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
        for instance in instances:
            operation_id = instance.tags.get(OPERATION_TAG)
            if not operation_id or operation_id == "pending":
                discrepancies.append({"instance_id": instance.instance_id, "reason": "OWNED_INSTANCE_WITHOUT_OPERATION"})
                continue
            if operation_id not in operations:
                discrepancies.append(
                    {
                        "instance_id": instance.instance_id,
                        "operation": operation_id,
                        "reason": "UNRECORDED_OWNED_INSTANCE",
                    }
                )
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
        return slot, discrepancies

    def _reconcile_present(self, operation, instance):
        state = instance.state
        if state == "terminated":
            self.ledger.set_state(operation.id, "terminal", instance_id=instance.instance_id, detail="instance_terminated")
        elif state in ("shutting-down", "stopping", "stopped"):
            self.ledger.set_state(operation.id, "terminating", instance_id=instance.instance_id)
        elif operation.state == "terminating":
            self.ledger.set_state(operation.id, "terminating", instance_id=instance.instance_id)
        elif state == "running":
            self.ledger.set_state(operation.id, "running", instance_id=instance.instance_id)
        else:
            self.ledger.set_state(operation.id, "starting", instance_id=instance.instance_id)

    def _discrepancies(self, operations, instances):
        known = {
            operation["operation"]: (operation["state"], operation["region"]) for operation in operations
        }
        discrepancies = []
        seen_live = set()
        for instance in instances:
            operation_id = instance.tags.get(OPERATION_TAG)
            if not operation_id or operation_id == "pending":
                discrepancies.append(
                    {"instance_id": instance.instance_id, "operation": operation_id, "reason": "OWNED_INSTANCE_WITHOUT_OPERATION"}
                )
                continue
            if operation_id not in known:
                discrepancies.append(
                    {"instance_id": instance.instance_id, "operation": operation_id, "reason": "UNRECORDED_OWNED_INSTANCE"}
                )
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
        return discrepancies


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
