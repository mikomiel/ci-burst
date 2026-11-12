"""Bounded worker console evidence.

An instance that never registers leaves no trace in the queue: the next
controller decision sees only a busy slot. The instance console is the one
channel that exists for a worker which never reached the network, so this module
reads it for the instances a deployment owns, extracts a small fixed vocabulary
of progress and failure markers, and writes two files:

* an append-only stream of observations, one JSON object per line, so a log
  collector can ship it without parsing a provider payload;
* one Prometheus textfile, so launches and registrations can be compared over a
  window instead of only for instances that still exist.

The command mutates nothing, holds no queue credential, and copies no console
text at all. A line contributes only a marker *name* from a fixed list: a step
name from the shipped first-boot script, an exit status, or one failure kind.
Truncation is not redaction, so nothing else from the console is published.

Delivery is at least once. The event line carries a stable ``id`` that is a
function of the instance and the marker, so a consumer can deduplicate the
repeat that follows a crash between publishing an event and persisting the
dedupe record; the alternative — persisting first — would lose a failure event
instead of repeating it.

The dedupe record is derived observation, not decision state. Deleting it loses
the emitted history and the cumulative counters; it never influences admission,
launch or termination.
"""

import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .aws import Aws
from .cost import CostModel
from .errors import ProviderRefusal
from .ledger import NONTERMINAL, TERMINAL, Ledger, current_window

#: One run reads at most this many consoles. The budget is spent on records that
#: can still carry new evidence, live workers first; a run with more candidates
#: than this defers the rest to the next run rather than refusing. The configured
#: ceiling is inside the budget, so every live worker is read in every run.
MAX_CONSOLE_READS = 16

#: A console never changes again once a read observes the provider in one of
#: these states. ``shutting-down``, ``stopping`` and ``stopped`` are termination
#: in progress for the controller and their consoles are still read; only a
#: ``terminated`` instance frees its slot.
CONSOLE_FINAL_STATES = frozenset({"terminated", "stopped"})

MAX_ERROR_KINDS = 3
MAX_EVENTS_BYTES = 4 * 1024 * 1024
STATE_RETENTION_SECONDS = 7 * 86400
STATE_VERSION = 2

#: A persisted counter is incremented and re-serialized on every run. Python
#: refuses to convert an extremely long integer back to a decimal string, so an
#: unbounded counter read from disk could fail the write that persists it. No real
#: deployment approaches this bound.
MAX_COUNTER = 10**9

#: Instance states in which no further console line can arrive.
TERMINAL_STATES = frozenset({"terminated", "shutting-down", "stopping", "stopped"})

#: The controller generates an operation id from ``uuid.uuid4().hex``. An
#: instance tag carrying anything else is not one of ours, and the value is
#: published in every event, so it is validated rather than trusted.
OPERATION_PATTERN = re.compile(r"^[0-9a-f]{32}$")

#: An EC2 instance identifier, and an availability-zone name. Both are published
#: in events; a tag or a record that fails these patterns is not one of ours.
INSTANCE_PATTERN = re.compile(r"^i-[0-9a-f]{8,17}$")
ZONE_PATTERN = re.compile(r"^[a-z]{2}-[a-z]+-[0-9][a-z]$")

#: The provider states a worker can be in. Anything else is not published.
INSTANCE_STATES = frozenset(
    {"pending", "running", "shutting-down", "terminated", "stopping", "stopped"}
)

#: The ledger's own lifecycle vocabulary, imported from its owner.
LEDGER_STATES = frozenset(NONTERMINAL + TERMINAL)

#: Every exit identity the command can produce. A persisted identity is matched
#: against this set, so a Unicode digit, an oversized number or a noncanonical
#: zero-padded form is rejected by comparison rather than by parsing.
EXIT_IDENTITIES = frozenset(f"exit:{status}" for status in range(256))

#: Events are republished from a file this command also reads. A retired line
#: must match its exact kind schema, so each kind lists the keys it may carry;
#: a value from another kind is a reason to drop the line, not to copy it.
EVENT_SCHEMA = {
    "step": frozenset({"id", "ts", "deployment", "instance", "zone", "operation", "event", "step"}),
    "exit": frozenset({"id", "ts", "deployment", "instance", "zone", "operation", "event", "rc"}),
    "error": frozenset({"id", "ts", "deployment", "instance", "zone", "operation", "event", "kind"}),
}
TIMESTAMP_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
DEPLOYMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: The step markers the shipped first-boot script emits. A name outside this set
#: is not a marker: an image could print any `STEP ...` line, and the command
#: publishes names, so the set has to be closed rather than pattern-matched.
STEP_NAMES = frozenset({"mount-options", "register", "job"})

#: Ordered classification rules for console lines that are not a marker. The
#: first matching rule wins, so a specific failure is named before a generic one.
#: A rule contributes its kind only: the matched line itself is never published.
ERROR_RULES = (
    ("forgejo_ping", ("cannot ping the forgejo instance server",)),
    ("ssm_timeout", ("connect timeout on endpoint url",)),
    ("apt_failed", ("unable to locate package", "e: failed to fetch", "e: sub-process")),
    ("register_failed", ("failed to register", "registration failed")),
    ("runner_error", ("level=error",)),
)

#: Every kind the classifier can produce. Persisted state is validated against
#: it as well: a record is not a trusted input, and a kind is published.
KNOWN_ERROR_KINDS = frozenset(kind for kind, _ in ERROR_RULES)

STEP_PATTERN = re.compile(r"\bSTEP ([a-z][a-z0-9-]{0,31})\b")
EXIT_PATTERN = re.compile(r"\bbootstrap exiting rc=(\d{1,3})\b")


@dataclass(frozen=True)
class ConsoleFacts:
    """The recognised markers of one console, and nothing else."""

    steps: tuple
    exit_code: int | None
    errors: tuple

    @property
    def registered(self):
        return "register" in self.steps

    def as_dict(self):
        return {"steps": list(self.steps), "exit_code": self.exit_code, "errors": list(self.errors)}


def parse_console(text):
    """Extract the recognised markers from one console.

    The first-boot script duplicates its output to the serial console, so every
    line typically appears twice; markers are deduplicated by name, never by
    position.
    """
    steps, errors, exit_code = [], [], None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = EXIT_PATTERN.search(line)
        if match is not None:
            status = int(match.group(1))
            if 0 <= status <= 255:
                exit_code = status
            continue
        match = STEP_PATTERN.search(line)
        if match is not None:
            name = match.group(1)
            if name in STEP_NAMES and name not in steps:
                steps.append(name)
            continue
        if len(errors) >= MAX_ERROR_KINDS:
            continue
        lowered = line.lower()
        for kind, needles in ERROR_RULES:
            if any(needle in lowered for needle in needles):
                if kind not in errors:
                    errors.append(kind)
                break
    return ConsoleFacts(tuple(steps), exit_code, tuple(errors))


def _empty_totals():
    return {"launches": 0, "registered": 0, "unregistered": 0}


def _counter(value):
    """Return a counter value that is safely serializable, or ``None``."""
    return value if type(value) is int and 0 <= value <= MAX_COUNTER else None


def _increment(counter):
    return min(counter + 1, MAX_COUNTER)


def load_state(path):
    """Return ``(state, note)``; an unusable record is replaced, not repaired.

    Nested types are validated here rather than at first use: a record that
    loads but cannot be iterated would fail the whole run, and a metrics run is
    the wrong place to fail closed on derived observation.
    """
    empty = {"version": STATE_VERSION, "console_reads": 0, "totals": _empty_totals(), "instances": {}}
    try:
        raw = Path(path).read_bytes().decode("utf-8")
    except FileNotFoundError:
        return empty, None
    except (OSError, UnicodeDecodeError):
        return empty, "state_rebuilt"
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return empty, "state_rebuilt"
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return empty, "state_rebuilt"
    instances = payload.get("instances")
    if not isinstance(instances, dict):
        return empty, "state_rebuilt"
    rebuilt = False
    kept = {}
    for instance_id, record in instances.items():
        # The map key is published identity too: a foreign key is not an
        # instance this deployment ever owned.
        if not isinstance(record, dict) or _match(INSTANCE_PATTERN, instance_id) is None:
            rebuilt = True
            continue
        sanitized = _sanitize_record(record)
        if sanitized is None:
            rebuilt = True
            continue
        record_value, changed = sanitized
        # A record without a usable timestamp can never age out, so retention
        # would keep it forever. Drop it rather than leak it.
        if record_value["seen"] is None:
            rebuilt = True
            continue
        if changed:
            rebuilt = True
        kept[instance_id] = record_value
    totals = payload.get("totals")
    if not isinstance(totals, dict):
        totals = _empty_totals()
        rebuilt = True
    else:
        # Rebuild from the permitted keys only: a record may carry extra keys,
        # and a copy would republish whatever text they hold. One unusable
        # counter resets that counter alone; the others keep their history.
        counters = {}
        for key in ("launches", "registered", "unregistered"):
            value = _counter(totals.get(key))
            if value is None:
                rebuilt = True
                value = 0
            counters[key] = value
        totals = counters
    reads = _counter(payload.get("console_reads"))
    if reads is None and payload.get("console_reads") is not None:
        rebuilt = True
    state = {
        "version": STATE_VERSION,
        "console_reads": reads or 0,
        "totals": dict(totals),
        "instances": kept,
    }
    return state, ("state_rebuilt" if rebuilt else None)


def _marker_key_is_valid(key):
    """Return True for an identity this command could have produced.

    Membership in finite sets, not parsing: a Unicode digit passes
    ``str.isdigit`` and then fails ``int()``, and a long digit string fails a
    second bound. Comparison has neither failure mode.
    """
    if not isinstance(key, str):
        return False
    prefix, _, value = key.partition(":")
    if prefix == "step":
        return value in STEP_NAMES
    if prefix == "error":
        return value in KNOWN_ERROR_KINDS
    if prefix == "exit":
        return key in EXIT_IDENTITIES
    return False


def _match(pattern, value):
    """Return the value only when it fullmatches; ``$`` alone accepts a newline."""
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _number(value):
    """Return a finite timestamp or ``None``.

    A JSON number can overflow to infinity (``1e309``); such a value can never
    satisfy an expiry comparison and cannot be written back as standard JSON.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        # A JSON integer may be arbitrarily long; retention arithmetic is
        # float-based, so a value that cannot be represented is unusable.
        return None
    return number if math.isfinite(number) else None


def _sanitize_record(record):
    """Rebuild one persisted record from an explicit schema.

    The record is not a trusted input. Re-using it wholesale would carry unknown
    fields — and any text in them — into the rewritten state, and an arbitrary
    ``emitted`` key would suppress or forge a publication. Only the fields this
    command writes survive, and each is validated against the same closed
    vocabulary as fresh console input. Returns ``(record, changed)``, or
    ``None`` when the record is not usable at all.
    """
    changed = False
    steps = record.get("steps")
    steps = [name for name in steps if isinstance(name, str)] if isinstance(steps, list) else []
    kept_steps = [name for name in steps if name in STEP_NAMES]
    if len(kept_steps) != len(steps) or not isinstance(record.get("steps"), list):
        changed = True
    kinds = record.get("errors")
    kinds = [kind for kind in kinds if isinstance(kind, str)] if isinstance(kinds, list) else []
    kept_kinds = [kind for kind in kinds if kind in KNOWN_ERROR_KINDS][:MAX_ERROR_KINDS]
    if len(kept_kinds) != len(kinds) or not isinstance(record.get("errors"), list):
        changed = True
    exit_code = record.get("exit_code")
    if exit_code is not None and (type(exit_code) is not int or not 0 <= exit_code <= 255):
        exit_code = None
        changed = True
    emitted = record.get("emitted")
    emitted = [key for key in emitted if _marker_key_is_valid(key)] if isinstance(emitted, list) else []
    if not isinstance(record.get("emitted"), list) or len(emitted) != len(record.get("emitted") or []):
        changed = True
    registered = record.get("registered")
    if not isinstance(registered, bool):
        changed = True
    state = record.get("state")
    state = state if isinstance(state, str) and state in INSTANCE_STATES else None
    zone = _match(ZONE_PATTERN, record.get("zone"))
    operation = _match(OPERATION_PATTERN, record.get("operation"))
    sanitized = {
        "first_seen": _number(record.get("first_seen")),
        "seen": _number(record.get("seen")),
        "state": state,
        "zone": zone,
        "operation": operation,
        "steps": kept_steps,
        "errors": kept_kinds,
        "exit_code": exit_code,
        "registered": bool(registered),
        "emitted": emitted,
        "counted_registered": bool(record.get("counted_registered")),
        "counted_unregistered": bool(record.get("counted_unregistered")),
    }
    for key in ("first_seen", "seen", "state", "zone", "operation"):
        if sanitized[key] != record.get(key):
            changed = True
    return sanitized, changed


def prune_state(state, now):
    """Drop per-instance records older than the retention window.

    The cumulative totals are deliberately untouched: they are the counter
    source, and a counter must not fall because a record expired.
    """
    kept = {}
    for instance_id, record in state["instances"].items():
        seen = record.get("seen")
        if not isinstance(seen, (int, float)):
            continue
        try:
            expired = now - seen > STATE_RETENTION_SECONDS
        except OverflowError:
            expired = True
        if expired:
            continue
        kept[instance_id] = record
    state["instances"] = kept
    return state


@dataclass
class RunSummary:
    """What one run observed, without the console text."""

    deployment: str
    observed: int = 0
    new_events: int = 0
    console_reads: int = 0
    states: dict = field(default_factory=dict)
    totals: dict = field(default_factory=_empty_totals)
    spend_micros: int | None = None
    committed_micros: int | None = None
    allowance_micros: int | None = None
    remaining_micros: int | None = None
    ledger_operations: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)
    duration: float = 0.0

    def as_dict(self):
        return {
            "deployment": self.deployment,
            "instances_observed": self.observed,
            "console_reads": self.console_reads,
            "events_emitted": self.new_events,
            "worker_states": dict(sorted(self.states.items())),
            "totals": dict(sorted(self.totals.items())),
            "spend_micros": self.spend_micros,
            "committed_micros": self.committed_micros,
            "allowance_micros": self.allowance_micros,
            "remaining_micros": self.remaining_micros,
            "ledger_operations": dict(sorted(self.ledger_operations.items())),
            "duration_seconds": round(self.duration, 3),
            "notes": list(self.notes),
        }


class Exporter:
    """One bounded observation run. Construct once per command."""

    def __init__(self, config, *, aws=None, ledger=None, clock=time.time):
        self.config = config
        self.clock = clock
        self.aws = aws if aws is not None else Aws(config, clock=clock)
        self._ledger = ledger

    def run(self, *, events_file, metrics_file, state_file, now=None):
        started = self.clock()
        current = self.clock() if now is None else now
        summary = RunSummary(deployment=self.config.deployment)

        instances = self.aws.owned_instances(self.config.deployment)
        instances = [item for item in instances if _match(INSTANCE_PATTERN, item.instance_id) is not None]
        if not instances:
            summary.notes.append("no_owned_instances")
        # The console-read bound is a bound per run, not a bound on what the
        # provider still remembers: a terminating worker keeps its tags for about
        # an hour, so a busy fleet legitimately shows many terminal records. Only
        # a deployment running more non-terminal workers than its own configured
        # limit is a disagreement worth refusing.
        limit = self.config.controller.max_instances
        live = [item for item in instances if _normalize_state(item.state) != "terminated"]
        if len(live) > limit:
            raise ProviderRefusal(
                "console_read_cap_exceeded",
                f"{len(live)} non-terminal owned instances exceeds the configured limit of {limit}",
            )

        state, note = load_state(state_file)
        if note is not None:
            summary.notes.append(note)
        prune_state(state, current)

        # A console carries evidence only while it can still change: a live
        # worker's buffer grows, and a terminal one is finished after the first
        # read that shows it left the live states. Finished records are never
        # read again, so normal turnover cannot spend the per-run budget.
        candidates = [item for item in instances if not _read_finished(state, item)]
        # Live workers first: their consoles are the ones still growing, and the
        # configured ceiling is inside the budget, so they always fit. Termination
        # history waits for the remaining capacity.
        candidates.sort(key=lambda item: _normalize_state(item.state) == "terminated")
        reading = candidates[:MAX_CONSOLE_READS]
        if len(candidates) > len(reading):
            summary.notes.append("console_read_deferred")
        read_ids = {item.instance_id for item in reading}
        consoles = self.aws.console_output([item.instance_id for item in reading]) if reading else {}

        events = []
        summary.observed = len(instances)
        for instance in instances:
            provider_state = _normalize_state(instance.state)
            summary.states[provider_state] = summary.states.get(provider_state, 0) + 1
            if instance.instance_id not in read_ids:
                continue
            facts = parse_console(consoles.get(instance.instance_id, ""))
            record, created = _record_for(state, instance.instance_id, current)
            if created:
                state["totals"]["launches"] = _increment(state["totals"]["launches"])
            _merge_record(record, instance, facts, current, provider_state)
            if record["registered"] and not record.get("counted_registered"):
                record["counted_registered"] = True
                state["totals"]["registered"] = _increment(state["totals"]["registered"])
            if not record["registered"] and _ended(record) and not record.get("counted_unregistered"):
                record["counted_unregistered"] = True
                state["totals"]["unregistered"] = _increment(state["totals"]["unregistered"])
            for event in _events_for(instance, record, current, self.config.deployment):
                events.append(event)
            summary.console_reads += 1

        summary.totals = dict(state["totals"])
        self._read_ledger(summary, current)

        if events or Path(events_file).exists():
            dropped = _append_events(events_file, events, self.config.deployment)
            if dropped:
                summary.notes.append("events_pruned")
        summary.new_events = len(events)

        state["console_reads"] = min(state.get("console_reads", 0) + summary.console_reads, MAX_COUNTER)
        _write_atomic(state_file, json.dumps(state, sort_keys=True, indent=2))

        summary.duration = self.clock() - started
        _write_atomic(metrics_file, render_metrics(summary, current, state["console_reads"]))
        result = summary.as_dict()
        result["decision"] = "reported"
        return result

    def _read_ledger(self, summary, now):
        """Attach budget series when the ledger can be read read-only."""
        ledger = self._ledger
        created = ledger is None
        if created:
            ledger = Ledger(self.config.state.database, clock=self.clock)
        try:
            if created:
                ledger.connect(create=False, read_only=True)
            window = current_window(now, self.config.budget.window_seconds)
            cost = CostModel.from_config(self.config)
            totals = ledger.window_totals(window, cost, now)
            summary.spend_micros = totals["spend"]
            summary.committed_micros = totals["committed"]
            summary.allowance_micros = self.config.budget.allowance
            summary.remaining_micros = self.config.budget.allowance - totals["committed"]
            counts = {}
            for operation in ledger.operations():
                # The ledger column is unrestricted text; only its own
                # vocabulary becomes a metric label.
                key = operation.state if operation.state in LEDGER_STATES else "invalid"
                counts[key] = counts.get(key, 0) + 1
            summary.ledger_operations = counts
            if "invalid" in counts and "ledger_state_unknown" not in summary.notes:
                summary.notes.append("ledger_state_unknown")
        except Exception:  # noqa: BLE001 - an absent ledger must not fail the run
            summary.spend_micros = None
            summary.committed_micros = None
            summary.allowance_micros = None
            summary.remaining_micros = None
            summary.ledger_operations = {}
            if "ledger_omitted" not in summary.notes:
                summary.notes.append("ledger_omitted")
        finally:
            if created:
                try:
                    ledger.close()
                except Exception:  # noqa: BLE001 - closing twice is harmless
                    pass


def _operation_of(instance):
    """Return the ownership tag only when it is one this deployment wrote."""
    return _match(OPERATION_PATTERN, instance.tags.get("ci-burst:operation"))


def _record_for(state, instance_id, now):
    record = state["instances"].get(instance_id)
    if record is None:
        record = {"first_seen": now, "emitted": [], "steps": [], "errors": [], "registered": False, "exit_code": None}
        state["instances"][instance_id] = record
        return record, True
    return record, False


def _normalize_state(state):
    """Return a provider state this board can label, or ``unknown``."""
    return state if isinstance(state, str) and state in INSTANCE_STATES else "unknown"


def _merge_record(record, instance, facts, now, provider_state):
    """Merge this run's facts into the record without ever un-learning one.

    A console buffer scrolls, an instance ages out, and a provider read can come
    back empty. Evidence that was observed once stays observed: absence now is
    not proof that the marker never existed.
    """
    record["seen"] = now
    record["state"] = provider_state
    record["zone"] = _match(ZONE_PATTERN, instance.availability_zone)
    record["operation"] = _operation_of(instance)
    for step in facts.steps:
        if step not in record["steps"]:
            record["steps"].append(step)
    for kind in facts.errors:
        if kind not in record["errors"] and len(record["errors"]) < MAX_ERROR_KINDS:
            record["errors"].append(kind)
    if facts.exit_code is not None and record.get("exit_code") is None:
        record["exit_code"] = facts.exit_code
    record["registered"] = bool(record.get("registered")) or facts.registered


def _ended(record):
    return record.get("state") in TERMINAL_STATES or record.get("exit_code") is not None


def _read_finished(state, instance):
    """True when this instance's console can no longer carry new evidence.

    Completion is a provider state, not a console marker: an exit line means the
    bootstrap finished, while the console and the lifecycle carry on and the
    instance may still be running. A record is finished only after a read that
    observed the provider in ``CONSOLE_FINAL_STATES``; a record this run has never
    seen is unfinished by definition.
    """
    record = state["instances"].get(instance.instance_id)
    return record is not None and record.get("state") in CONSOLE_FINAL_STATES


def _events_for(instance, record, now, deployment):
    """Return the event lines for markers not emitted before, and mark them."""
    events = []
    base = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "deployment": deployment,
        "instance": instance.instance_id,
        "zone": _match(ZONE_PATTERN, instance.availability_zone),
        "operation": _operation_of(instance),
    }
    emitted = record["emitted"]

    def publish(marker, payload):
        key = marker
        if key in emitted:
            return
        emitted.append(key)
        event = dict(base)
        event["id"] = f"{instance.instance_id}:{marker}"
        event.update(payload)
        events.append(event)

    for step in record["steps"]:
        publish("step:" + step, {"event": "step", "step": step})
    if record.get("exit_code") is not None:
        publish(f"exit:{record['exit_code']}", {"event": "exit", "rc": record["exit_code"]})
    for kind in record["errors"]:
        publish("error:" + kind, {"event": "error", "kind": kind})
    return events


def _event_line_is_valid(event, deployment):
    """Return True for an event this command could have written.

    Total by construction: every field is type-checked before it is used, so a
    malformed historical line is dropped rather than raising.
    """
    if not isinstance(event, dict):
        return False
    kind = event.get("event")
    if not isinstance(kind, str) or kind not in EVENT_SCHEMA:
        return False
    if set(event) != EVENT_SCHEMA[kind]:
        return False
    instance = event.get("instance")
    if not isinstance(instance, str) or not INSTANCE_PATTERN.fullmatch(instance):
        return False
    identity = event.get("id")
    if not isinstance(identity, str) or not identity.startswith(instance + ":"):
        return False
    marker = identity[len(instance) + 1 :]
    if not _marker_key_is_valid(marker):
        return False
    if _match(TIMESTAMP_PATTERN, event.get("ts")) is None:
        return False
    # A retained line must belong to THIS deployment: the configured name is the
    # only deployment string this command can vouch for, and a line from another
    # one is not this board's history.
    if event.get("deployment") != deployment:
        return False
    zone = event.get("zone")
    if zone is not None and _match(ZONE_PATTERN, zone) is None:
        return False
    operation = event.get("operation")
    if operation is not None and _match(OPERATION_PATTERN, operation) is None:
        return False
    if kind == "step":
        step = event.get("step")
        return isinstance(step, str) and step in STEP_NAMES and marker == f"step:{step}"
    if kind == "exit":
        status = event.get("rc")
        return type(status) is int and 0 <= status <= 255 and marker == f"exit:{status}"
    error_kind = event.get("kind")
    return isinstance(error_kind, str) and error_kind in KNOWN_ERROR_KINDS and marker == f"error:{error_kind}"


def _retained_events(existing, deployment):
    """Keep only historical lines that still match the event schema.

    The file is a persisted input: this command rewrites it on every run, so
    whatever it holds is republished. A line that no longer validates is
    dropped rather than carried forward, and the caller reports the drop.
    """
    kept, dropped = [], 0
    for line in existing.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            valid = _event_line_is_valid(event, deployment)
        except (TypeError, ValueError, RecursionError):
            valid = False
        if valid:
            kept.append(json.dumps(event, sort_keys=True))
        else:
            dropped += 1
    return "".join(line + "\n" for line in kept), dropped


def _append_events(path, events, deployment):
    """Publish new event lines, rotating the file when it outgrows its bound.

    The file is rewritten atomically on each run rather than appended inode-wise:
    a collector that reopens it by path sees a complete file, and the rewrite is
    what makes rotation and the size bound possible. The file is a bounded
    publication, not an unbounded WAL. Returns the number of historical lines
    dropped because they no longer match the schema.
    """
    path = Path(path)
    lines = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
    try:
        existing = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        existing = ""
    existing, dropped = _retained_events(existing, deployment)
    combined = existing + lines
    encoded = combined.encode("utf-8")
    if len(encoded) > MAX_EVENTS_BYTES:
        tail = encoded[-MAX_EVENTS_BYTES:]
        newline = tail.find(b"\n")
        combined = tail[newline + 1 :].decode("utf-8", "replace") if newline >= 0 else ""
    _write_atomic(path, combined)
    return dropped


def _write_atomic(path, text):
    """Replace a file atomically, in its own directory.

    A collector may read the file at any moment; a partial write would be a torn
    sample, so the content is written beside it and renamed into place.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".ci-burst-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def render_metrics(summary, now, console_reads_total):
    """Render one Prometheus textfile. Every series names the deployment."""
    lines = []

    def emit(name, kind, help_text, samples):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            pairs = {"deployment": summary.deployment}
            pairs.update(labels)
            rendered = ",".join(f'{key}="{_label(val)}"' for key, val in pairs.items())
            lines.append(f"{name}{{{rendered}}} {value}")

    def total(name):
        value = summary.totals.get(name)
        return value if isinstance(value, int) else 0

    emit(
        "ci_burst_worker_instances",
        "gauge",
        "Owned worker instances by provider state.",
        [({"state": state}, count) for state, count in summary.states.items()],
    )
    emit(
        "ci_burst_worker_launches_total",
        "counter",
        "Owned worker instances observed since the observation record was created.",
        [({}, total("launches"))],
    )
    emit(
        "ci_burst_worker_registered_total",
        "counter",
        "Workers whose console has ever shown the registration step.",
        [({}, total("registered"))],
    )
    emit(
        "ci_burst_worker_unregistered_total",
        "counter",
        "Workers that ended without ever showing the registration step.",
        [({}, total("unregistered"))],
    )
    if summary.spend_micros is not None:
        emit(
            "ci_burst_spend_micros",
            "gauge",
            "Tracked cost of operations admitted in the current budget window, in micros.",
            [({}, summary.spend_micros)],
        )
        emit(
            "ci_burst_committed_micros",
            "gauge",
            "Tracked window cost plus the worst-case remaining life of every non-terminal operation.",
            [({}, summary.committed_micros)],
        )
        emit(
            "ci_burst_allowance_micros",
            "gauge",
            "Configured allowance of the current budget window, in micros.",
            [({}, summary.allowance_micros)],
        )
        emit(
            "ci_burst_remaining_micros",
            "gauge",
            "Allowance minus the current commitment; admission needs this to stay positive.",
            [({}, summary.remaining_micros)],
        )
        emit(
            "ci_burst_ledger_operations",
            "gauge",
            "Ledger operations by state.",
            [({"state": state}, count) for state, count in summary.ledger_operations.items()],
        )
    emit(
        "ci_burst_export_last_run_timestamp_seconds",
        "gauge",
        "Unix time of the last completed observation run.",
        [({}, int(now))],
    )
    emit(
        "ci_burst_export_duration_seconds",
        "gauge",
        "Wall-clock duration of the last observation run.",
        [({}, round(summary.duration, 3))],
    )
    emit(
        "ci_burst_export_console_reads_total",
        "counter",
        "Console reads performed since the observation record was created.",
        [({}, console_reads_total)],
    )
    return "\n".join(lines) + "\n"


def _label(value):
    """Escape one label value for the Prometheus text format."""
    if value is None:
        return ""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
