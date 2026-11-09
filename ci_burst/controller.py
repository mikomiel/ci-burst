"""The bounded controller: observe, admit, launch, reconcile and expire.

The controller is a capacity decision, not a scheduler. It never assigns jobs,
retries jobs or records a CI result. Forgejo remains the authority for work and
results; AWS remains the authority for instances. Every mutating path requires
an explicit apply call and a writable ledger.

A launch intent is durable before the cloud request. While an intent has no
confirmed instance, apply resumes that same frozen request and client token
instead of allocating a replacement; EC2 idempotency makes the retry safe and
the 24-hour client-token validity bounds how long that is attempted.
"""

import hashlib
import json
import math
import time

from .aws import DEPLOYMENT_TAG, EXPIRES_TAG, MANAGED_TAG, OPERATION_TAG, Aws
from .errors import ProviderRefusal, Refusal, StateRefusal
from .forgejo import ForgejoObserver
from .ledger import Ledger, current_window
from .money import format_money, hourly_cost_micros

WRITER_TOPOLOGY = "one deployment, one controller host, one durable ledger, one writer"

#: EC2 documents a client token as valid for 24 hours. Beyond that a retry is
#: no longer idempotent, so the unresolved intent blocks and needs an operator.
CLIENT_TOKEN_MAX_AGE_SECONDS = 24 * 60 * 60

COST_SCOPE_NOTE = (
    "The reservation estimates compute at the configured maximum Spot price for the configured "
    "maximum lifetime, plus the configured non-compute allowance. It does not bound EBS, data "
    "transfer, public addressing, log delivery, minimum billing increments, failed cleanup or "
    "other charges, and it is not a guaranteed cap on the AWS bill."
)


def _sha256(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


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
        slot = self._slot_from_state(state)
        result["slot"] = slot
        observation = self._observe_or_refuse(result)
        if observation is None:
            result["decision"] = "refused"
            return result
        result["observation"] = observation.as_dict()
        template = self._template_or_refuse(result)
        if template is not None:
            result["template"] = template.as_dict()
        decision = self._decide(observation, slot, template)
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
            instances = self.aws.owned_instances(self.config.deployment)
            with self.ledger.transaction():
                slot, discrepancies = self._reconcile(instances)
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
            template = self._template_or_refuse(result)
            if template is not None:
                result["template"] = template.as_dict()
            decision = self._decide(observation, slot, template)
            result.update(decision)
            if decision["decision"] != "launch_ready":
                result["decision"] = decision["decision"]
                return result
            return self._launch(result, decision, template)
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
            try:
                self.ledger.connect(create=False, read_only=True)
            except StateRefusal as refusal:
                return {"available": False, "note": refusal.reason}
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

    def _template_or_refuse(self, result):
        try:
            return self.aws.template(self.config.aws.launch_template_name, self.config.aws.launch_template_version)
        except Refusal as refusal:
            result["template_error"] = refusal.as_dict()
            return None

    def _decide(self, observation, slot, template):
        """Return the decision block for this snapshot."""
        now = self.clock()
        reasons, notes = [], []
        decision = {"reasons": reasons, "notes": notes}
        if slot.get("discrepancies"):
            decision["decision"] = "refused"
            reasons.append("owned_state_discrepancy")
            return decision
        if slot["busy"]:
            decision["decision"] = "wait"
            reasons.append("slot_busy")
            return decision
        if observation.demand == 0:
            decision["decision"] = "wait"
            reasons.append("no_runnable_demand")
            return decision
        if template is None:
            decision["decision"] = "refused"
            reasons.append("template_unusable")
            return decision
        if not template.has_user_data:
            decision["decision"] = "refused"
            reasons.append("template_bootstrap_missing")
            return decision
        if template.placement_zone is not None and template.placement_zone != self.config.aws.availability_zone:
            decision["decision"] = "refused"
            reasons.append("template_placement_mismatch")
            return decision
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
        try:
            quote = self.aws.spot_quote(template.instance_type, self.config.aws.availability_zone)
        except Refusal as refusal:
            decision["decision"] = "refused"
            decision["refusal"] = refusal.as_dict()
            reasons.append("spot_price_unavailable")
            return decision
        if quote is None:
            decision["decision"] = "refused"
            reasons.append("spot_price_unavailable")
            return decision
        decision["price"] = quote.as_dict() | {"cap": format_money(self.config.aws.max_spot_price)}
        if now - quote.timestamp > self.config.aws.price_max_age_seconds:
            decision["decision"] = "refused"
            reasons.append("spot_price_stale")
            return decision
        if quote.availability_zone != self.config.aws.availability_zone or quote.instance_type != template.instance_type:
            decision["decision"] = "refused"
            reasons.append("spot_price_wrong_scope")
            return decision
        if quote.price_micros > self.config.aws.max_spot_price:
            decision["decision"] = "refused"
            reasons.append("spot_price_above_cap")
            return decision
        if consumed + reservation > self.config.budget.allowance:
            decision["decision"] = "refused"
            reasons.append("budget_allowance_exhausted")
            return decision
        decision["decision"] = "launch_ready"
        decision["reservation_micros"] = reservation
        decision["window_start"] = window_start
        decision["expires_at"] = now + self.config.aws.max_lifetime_seconds
        return decision

    def _launch(self, result, decision, template):
        import uuid

        now = self.clock()
        operation_id = uuid.uuid4().hex
        client_token = "ci-burst-" + uuid.uuid4().hex
        request = {
            "LaunchTemplate": {
                "LaunchTemplateId": template.template_id,
                "Version": str(template.version),
            },
            "Placement": {"AvailabilityZone": self.config.aws.availability_zone},
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
            "ClientToken": client_token,
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": MANAGED_TAG, "Value": "true"},
                        {"Key": DEPLOYMENT_TAG, "Value": self.config.deployment},
                        {"Key": OPERATION_TAG, "Value": operation_id},
                        {"Key": EXPIRES_TAG, "Value": str(int(decision["expires_at"]))},
                        {"Key": "Name", "Value": f"{self.config.deployment}-spot"},
                    ],
                }
            ],
        }
        request_sha256 = _sha256(request)
        with self.ledger.transaction():
            operation = self.ledger.new_operation(
                operation_id=operation_id,
                client_token=client_token,
                request=request,
                request_sha256=request_sha256,
                expires_at=decision["expires_at"],
                reservation_micros=decision["reservation_micros"],
                window_start=decision["window_start"],
                detail=request_sha256,
            )
        # Intent is committed, so a lost response is recoverable and never a
        # reason to allocate a second operation for the same demand.
        self._send(operation, request, result, "launched")
        return result

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
        if self.clock() - operation.created_at > CLIENT_TOKEN_MAX_AGE_SECONDS:
            result["decision"] = "refused"
            result.setdefault("reasons", []).append("unresolved_intent_expired")
            result["operation"] = operation.id
            return True
        self._send(operation, operation.request(), result, "resumed")
        return True

    def _send(self, operation, request, result, success_decision):
        try:
            instance_ids = self.aws.run_spot(request)
        except ProviderRefusal as refusal:
            state = "uncertain" if refusal.uncertain else "failed"
            self.ledger.set_state(operation.id, state, detail=refusal.reason)
            result["decision"] = "uncertain" if refusal.uncertain else "launch_failed"
            result["refusal"] = refusal.as_dict()
            result["operation"] = operation.id
            return
        self.ledger.set_state(operation.id, "starting", instance_id=instance_ids[0])
        result["decision"] = success_decision
        result["operation"] = operation.id
        result["instance_id"] = instance_ids[0]
        result["expires_at"] = operation.expires_at
        result["reservation"] = format_money(operation.reservation_micros)

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
        return False

    def _reconcile(self, instances):
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
            by_operation[operation_id] = chosen
            if operation_id not in nonterminal and chosen.state != "terminated":
                discrepancies.append(
                    {
                        "instance_id": chosen.instance_id,
                        "operation": operation_id,
                        "reason": "TERMINAL_OPERATION_STILL_OWNS_INSTANCE",
                    }
                )
        missing = [operation for operation in nonterminal.values() if operation.id not in by_operation]
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
                self.ledger.set_state(operation.id, "terminal", detail="instance_absent")
            # An operation without an instance id stays outstanding; apply
            # resumes its frozen request instead of creating a replacement.
        for operation in nonterminal.values():
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
        known = {operation["operation"]: operation["state"] for operation in operations}
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
            if instance.state == "terminated":
                continue
            if operation_id in seen_live:
                discrepancies.append(
                    {"instance_id": instance.instance_id, "operation": operation_id, "reason": "DUPLICATE_OPERATION_INSTANCES"}
                )
                continue
            seen_live.add(operation_id)
            if known[operation_id] in ("terminal", "failed"):
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
