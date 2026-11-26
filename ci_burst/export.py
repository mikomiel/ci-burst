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
The single exception is the Spot interruption marker, which contributes three
validated values and no free text: the notice's action, one of a closed set; the
worker hostname, matched against a fixed pattern; and the runner's registration
id, matched against the identifier's pattern. Truncation is not redaction, so
nothing else from the console is published.

Delivery is at least once. The event line carries a stable ``id`` that is a
function of the instance and the marker, so a consumer can deduplicate the
repeat that follows a crash between publishing an event and persisting the
dedupe record; the alternative — persisting first — would lose a failure event
instead of repeating it.

Runs that share a file take turns. A run holds an exclusive lock on the
directory of the record, of the event file and of the metrics textfile, from
before its first read of the clock, the record or the event file until its last
write. Runs that share any of these files, or only a directory, are serialized,
so a manual run waits for the timer's run and the other way round, and no run
saves totals from an older snapshot or replaces lines that another run added.
The lock leaves no file behind (``_directory_locks``). This holds while each of
those directories stays the same directory during a run.

The counter contract sets the order of the last writes (``_publish``). The
cumulative counters are saved in the record, and a collector must never read a
total that a later run with the same record lowers, so a run saves the record
before it replaces the metrics textfile. Runs that write one textfile must use
one record: the counters of two records are unrelated. A textfile that a run
writes never carries a total that the record on disk lacks, whether it is the
copy that the shipped unit publishes after the command exits 0 or the file that
a collector reads from ``--metrics-file`` itself, and whether a write fails or
the process stops. Counters fall only when
the record is lost, damaged or unreadable: deleted, or replaced by
``load_state`` because it cannot read or use it. The counters then start again,
and until a run replaces the textfile, the textfile of an earlier run can hold
more than the record.

The dedupe record is derived observation, not decision state. Deleting it loses
the emitted history and the cumulative counters. It influences admission in one
way only: ``apply`` and ``plan`` read it as a read-only brake that can withhold a
launch after repeated futile launches (``launch_evidence``). The brake reads the
file's modification time as a heartbeat, so every successful run advances it,
even when nothing changed, and does it last, after the textfile is replaced. The
record is saved under the time of the file that the run read, or under the epoch
when it read none, so a run that fails does not advance the modification time
and the record ages. The record never authorizes a launch and never influences
termination.
"""

import contextlib
import fcntl
import json
import math
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .aws import Aws
from .config import INSTANCE_TYPE_PATTERN, MAX_INSTANCE_TYPE_LENGTH
from .cost import CostModel
from .errors import ProviderRefusal
from .ledger import NONTERMINAL, TERMINAL, Ledger, current_window

#: One run reads at most this many consoles. The budget is spent on records that
#: can still carry new evidence, live workers first; a run with more candidates
#: than this defers the terminal surplus to the next run rather than refusing.
#: Only more unfinished live candidates than the budget refuses, because then the
#: live evidence itself cannot be covered in one run.
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

#: A fleet snapshot older than this is not published as current: the count
#: gauges are omitted and the run says so, while the timestamp series stays
#: readable so a consumer can alert on staleness itself.
FLEET_STALE_SECONDS = 15 * 60

#: The controller generates an operation id from ``uuid.uuid4().hex``. An
#: instance tag carrying anything else is not one of ours, and the value is
#: published in every event, so it is validated rather than trusted.
OPERATION_PATTERN = re.compile(r"^[0-9a-f]{32}$")

#: An EC2 instance identifier and an availability-zone name. Both are published
#: in events; a tag or a record that fails these patterns is not one of ours.
#: The instance type uses the configuration's own grammar (``_instance_type``).
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

#: Keys an event line may carry in addition to its kind schema. The key is
#: absent when the provider reported no type, and a retained line written before
#: the key existed has none, so it is optional rather than part of the schema.
OPTIONAL_EVENT_KEYS = frozenset({"instance_type"})

#: Events are republished from a file this command also reads. A retired line
#: must match its exact kind schema, so each kind lists the keys it may carry;
#: a value from another kind is a reason to drop the line, not to copy it.
EVENT_SCHEMA = {
    "step": frozenset({"id", "ts", "deployment", "instance", "zone", "operation", "event", "step"}),
    "exit": frozenset({"id", "ts", "deployment", "instance", "zone", "operation", "event", "rc"}),
    "job-process": frozenset(
        {"id", "ts", "deployment", "instance", "zone", "operation", "event", "outcome", "rc"}
    ),
    "error": frozenset({"id", "ts", "deployment", "instance", "zone", "operation", "event", "kind"}),
    "spot-interruption": frozenset(
        {
            "id",
            "ts",
            "deployment",
            "instance",
            "zone",
            "operation",
            "event",
            "reason",
            "action",
            "host",
            "runner",
        }
    ),
}
TIMESTAMP_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
DEPLOYMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: The step markers the shipped first-boot script emits. A name outside this set
#: is not a marker: an image could print any `STEP ...` line, and the command
#: publishes names, so the set has to be closed rather than pattern-matched.
STEP_NAMES = frozenset({"mount-options", "register", "claim-timeout", "job"})

#: The steps the shipped first-boot script prints only after the register step. A
#: record that shows one of them proves that the worker registered, even when the
#: registration line scrolled out of the console before the first read.
POST_REGISTRATION_STEPS = frozenset({"claim-timeout", "job"})

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

#: The bootstrap's own one-job process marker, matched before ``EXIT_PATTERN``.
#: It reports the runner process's outcome, never the Forgejo job's result: a
#: runner may exit 0 after a job that failed, and the job result stays with
#: Forgejo. ``completed`` is status 0, a timeout is the kernel's 124 or 137,
#: and every other status is a failure.
ONE_JOB_PATTERN = re.compile(r"\bbootstrap one-job (completed|failed|timeout) rc=(\d{1,3})\b")
ONE_JOB_OUTCOMES = frozenset({"completed", "failed", "timeout"})
ONE_JOB_TIMEOUT_CODES = frozenset({124, 137})


def _job_process(outcome, status):
    """Return True when ``(outcome, status)`` is a marker the bootstrap emits.

    The outcome and the status are one vocabulary, not two: the script routes 0
    to ``completed``, 124 and 137 to ``timeout`` and every other status to
    ``failed``, so a pair from another route is not a marker.
    """
    if outcome not in ONE_JOB_OUTCOMES or not 0 <= status <= 255:
        return False
    if outcome == "completed":
        return status == 0
    if outcome == "timeout":
        return status in ONE_JOB_TIMEOUT_CODES
    return status != 0 and status not in ONE_JOB_TIMEOUT_CODES


#: Every job-process identity the command can produce. Membership replaces
#: parsing for the same reason ``EXIT_IDENTITIES`` does: a Unicode digit, an
#: oversized number or a padded zero never matches.
JOB_PROCESS_IDENTITIES = frozenset(
    f"job-process:{outcome}:{status}"
    for outcome in sorted(ONE_JOB_OUTCOMES)
    for status in range(256)
    if _job_process(outcome, status)
)

#: The Spot interruption marker: the worker observed the two-minute notice and
#: attempted to end its one job within a bounded window. ``action`` is the
#: notice's own value, and ``unknown`` is the
#: shipped fallback for a notice whose body carried no documented action;
#: ``runner`` is the registration id the worker captured, or ``unknown`` when it
#: could not capture one. Every value is validated before it is published, and
#: the marker contributes no other console text.
SPOT_PATTERN = re.compile(
    r"\bbootstrap spot-interruption action=([a-z]{1,12}) "
    r"host=([A-Za-z0-9][A-Za-z0-9.-]{0,63}) "
    r"runner=([0-9a-f-]{36}|unknown)(?=$|\s)"
)
SPOT_ACTIONS = frozenset({"terminate", "stop", "hibernate", "unknown"})
HOST_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}")
RUNNER_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _spot_interruption(action, host, runner):
    """Return the triple when every value is one the bootstrap can emit.

    ``runner`` is ``None`` for the shipped ``unknown`` fallback: a worker that
    could not capture a registration id has none, and the event states that
    rather than inventing one.
    """
    if action not in SPOT_ACTIONS or _match(HOST_PATTERN, host) is None:
        return None
    if runner == "unknown":
        return (action, host, None)
    if _match(RUNNER_PATTERN, runner) is None:
        return None
    return (action, host, runner)


@dataclass(frozen=True)
class ConsoleFacts:
    """The recognised markers of one console, and nothing else."""

    steps: tuple
    exit_code: int | None
    errors: tuple
    job_process: tuple | None = None
    spot_interruption: tuple | None = None

    @property
    def registered(self):
        return "register" in self.steps

    def as_dict(self):
        return {
            "steps": list(self.steps),
            "exit_code": self.exit_code,
            "errors": list(self.errors),
            "job_process": list(self.job_process) if self.job_process is not None else None,
            "spot_interruption": (
                list(self.spot_interruption) if self.spot_interruption is not None else None
            ),
        }


def parse_console(text):
    """Extract the recognised markers from one console.

    The first-boot script duplicates its output to the serial console, so every
    line typically appears twice; markers are deduplicated by name, never by
    position.
    """
    steps, errors, exit_code, job_process = [], [], None, None
    spot_interruption = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = SPOT_PATTERN.search(line)
        if match is not None:
            pair = _spot_interruption(match.group(1), match.group(2), match.group(3))
            if pair is not None and spot_interruption is None:
                spot_interruption = pair
            continue
        match = ONE_JOB_PATTERN.search(line)
        if match is not None:
            outcome = match.group(1)
            status = int(match.group(2))
            if _job_process(outcome, status):
                job_process = (outcome, status)
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
    return ConsoleFacts(tuple(steps), exit_code, tuple(errors), job_process, spot_interruption)


def default_record_path(database):
    """Return the observation record's default path, beside the ledger.

    ``export`` writes the record here unless ``--state-file`` names another path,
    and the launch evidence reader reads it only here.
    """
    return Path(database).parent / "export" / "observed.json"


def _empty_totals():
    return {"launches": 0, "registered": 0, "unregistered": 0}


def _counter(value):
    """Return a counter value that is safely serializable, or ``None``."""
    return value if type(value) is int and 0 <= value <= MAX_COUNTER else None


def _increment(counter):
    return min(counter + 1, MAX_COUNTER)


def _refuse_constant(constant):
    """Refuse ``NaN``, ``Infinity`` and ``-Infinity`` while a record is decoded.

    ``json`` accepts the three words, but they are not JSON and this command never
    writes one. It is the decoder's ``parse_constant``, so it sees these words
    only: a number that overflows, such as ``1e309``, decodes to a float infinity,
    and the field rules drop it (``_number``, ``_counter`` and the integer
    checks). The error is a ``ValueError``.
    """
    raise ValueError(f"{constant} is not a JSON value")


def _refuse_repeated_member(pairs):
    """Refuse a JSON object that names one member twice, at any level.

    ``json`` keeps the last value of a repeated member, so a record with an invalid
    ``steps`` followed by a valid one would pass every field rule, and a key of
    ``instances`` that repeats would keep only its last record. This command
    cannot write either. It serialises its record with ``json.dumps`` from
    dictionaries whose keys are all text, the fixed field names and the instance
    identifiers that pass ``INSTANCE_PATTERN``, and a dictionary holds a key once.
    (``json.dumps`` can write one name twice only for keys of different types,
    such as ``1`` and ``"1"``, and this command has none.) The error is a
    ``ValueError``.

    This is the decoder's ``object_pairs_hook`` for both readers of the record:
    ``load_state`` and the launch evidence reader decode with it and with
    ``_refuse_constant``, so neither keeps a value that the other refuses. The
    reader calls the file ``malformed``. ``load_state`` takes its branch for a file
    that does not decode, and drops the whole file.
    """
    members = dict(pairs)
    if len(members) != len(pairs):
        raise ValueError("a JSON object names a member twice")
    return members


#: ``_read_record``'s content for a record that exists and cannot be read.
UNREADABLE_RECORD = object()


def _open_for_reading(path, flags):
    """Open ``path`` without blocking and without following a link.

    Both readers of the record open it through this function: ``load_state`` and
    the launch evidence reader. A plain read-only open of a FIFO that has no
    writer blocks forever, and ``apply`` reads the record while it holds the ledger
    writer lock. ``export`` replaces the record by a rename of a regular file, so a
    link at the path is not its output.
    """
    return os.open(path, flags | os.O_NONBLOCK | os.O_NOFOLLOW)


def _read_record(path):
    """Read the record at ``path`` once and return ``(content, times)``.

    ``content`` is the bytes of a regular file, ``None`` when there is no file, or
    ``UNREADABLE_RECORD`` when the path holds a link, a FIFO, a directory or
    another entry that is not a regular file, or when the read fails. ``times`` is
    the access and modification time, in nanoseconds, of the file that was read,
    and ``(0, 0)`` when none was: a run saves its record under these times
    (``_publish``), so it never gives the record a time that it did not read from
    the record itself.
    """
    try:
        with open(path, "rb", opener=_open_for_reading) as stream:
            status = os.fstat(stream.fileno())
            if not stat.S_ISREG(status.st_mode):
                return UNREADABLE_RECORD, (0, 0)
            return stream.read(), (status.st_atime_ns, status.st_mtime_ns)
    except FileNotFoundError:
        return None, (0, 0)
    except OSError:
        return UNREADABLE_RECORD, (0, 0)


def load_state(path, *, now=None):
    """Return ``(state, note)``; an unusable record is replaced, not repaired.

    Nested types are validated here rather than at first use: a record that
    loads but cannot be iterated would fail the whole run, and a metrics run is
    the wrong place to fail closed on derived observation.

    A file that does not decode is replaced whole, and the note is
    ``state_rebuilt``. Besides text that is not JSON, the decoder refuses
    ``NaN``, ``Infinity`` and ``-Infinity``, and a JSON object that names a member
    twice, at any level. This command writes none of them, and the launch
    evidence reader refuses the same files. A loader that kept the last value of a
    repeat would keep a record that the reader would not read, skip the finished
    workers in it and rewrite the file with a fresh heartbeat. The emitted history
    and the cumulative counters start again, as they do when the file is deleted,
    and the run reads again the consoles that it selects within
    ``MAX_CONSOLE_READS``.

    One record is dropped, and the note is ``state_rebuilt``, when what it shows of
    registration cannot be kept (``_registration_evidence_is_damaged``): a stored
    value that bears on registration and that the loader cannot keep, or a
    registration key that the record of an ended worker lacks.
    The reader decides from those values and the run does not read an ended
    worker again, so a record that lost one of them would be written back as a
    futile launch under a fresh heartbeat. Without the record, the run reads the
    console again while the provider lists the instance, and an instance that it
    no longer lists leaves no evidence. The cost is that of any dropped record:
    the cumulative totals count the worker again, and the run emits its markers
    again under their stable identities.

    One record is dropped, and the note is ``state_rebuilt``, when it is dated
    after ``now``, the clock of the run. The reader refuses a whole file for one
    record dated after its own clock, and a worker that ended is not read again,
    so a record that an earlier clock stamped ahead would keep every new file
    ``malformed`` until the clock caught up. Without the record, the run reads the
    console again while the provider lists the instance and stamps only what it
    read, and an instance that it no longer lists leaves no evidence. The run
    never gives an observation a time it did not make. The cost is that of any
    dropped record, and the evidence that a live worker's record held from earlier
    reads is lost with it: the record shows what the console shows now. Without
    ``now`` no record is judged against a clock.

    The file is read once, as ``_read_record`` reads it. An entry that exists and
    cannot be read is replaced as well, and the note is ``state_rebuilt``: a link,
    a FIFO, or a file whose read fails, even for a moment. The emitted history and
    the cumulative counters start again, as for a deleted file. A directory at the
    path reads the same way, and the run then fails when it saves the record.
    """
    return _state_from(_read_record(path)[0], now)


def _state_from(content, now):
    """``load_state`` for the content that ``_read_record`` returned."""
    empty = {"version": STATE_VERSION, "console_reads": 0, "totals": _empty_totals(), "instances": {}}
    if content is None:
        return empty, None
    if content is UNREADABLE_RECORD:
        return empty, "state_rebuilt"
    try:
        raw = content.decode("utf-8")
    except UnicodeDecodeError:
        return empty, "state_rebuilt"
    try:
        payload = json.loads(
            raw,
            parse_constant=_refuse_constant,
            object_pairs_hook=_refuse_repeated_member,
        )
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
        # The reader's own comparison (``seen > now``): a record that it would
        # refuse is not carried into a file that it would then refuse whole.
        if now is not None and record_value["seen"] > now:
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
    if prefix == "job-process":
        return key in JOB_PROCESS_IDENTITIES
    if prefix == "spot-interruption":
        action, _, rest = value.partition(":")
        host, _, runner = rest.partition(":")
        return _spot_interruption(action, host, runner) is not None
    return False


def _match(pattern, value):
    """Return the value only when it fullmatches; ``$`` alone accepts a newline."""
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _instance_type(value):
    """Return an instance type the configuration would accept, else ``None``."""
    if isinstance(value, str) and len(value) > MAX_INSTANCE_TYPE_LENGTH:
        return None
    return _match(INSTANCE_TYPE_PATTERN, value)


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


def _job_process_value(value):
    """Return ``value`` when it is the pair a record stores, else ``None``.

    The launch evidence reader applies the same rule: a stored value that this
    function does not return unchanged is one that the command cannot have
    written.
    """
    if (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and type(value[1]) is int
        and _job_process(value[0], value[1])
    ):
        return value
    return None


def _spot_interruption_value(value):
    """Return the list a record stores for a persisted notice, else ``None``.

    The stored value is the normalised triple, never the console spelling: an
    empty string or the literal "unknown" is not a registration id this command
    publishes, so it must not survive a rewrite. The launch evidence reader
    applies the same rule.
    """
    if (
        isinstance(value, list)
        and len(value) == 3
        and isinstance(value[0], str)
        and isinstance(value[1], str)
        and (value[2] is None or isinstance(value[2], str))
    ):
        normalized = _spot_interruption(value[0], value[1], value[2] or "unknown")
        if normalized is not None:
            return list(normalized)
    return None


def _registration_evidence_is_damaged(record):
    """Return True when what a record shows of registration cannot be kept.

    The launch evidence reader decides ``registered`` or ``futile`` from
    ``registered``, ``steps``, ``job_process`` and ``spot_interruption``, and a
    worker whose record shows none of them never registered. A worker that ended
    is not read again, so a loader that gave a default to a value that it cannot
    keep, or to a key that the record lacks, would keep a record that shows no
    registration, and the next run would write it back with a fresh heartbeat: a
    futile launch that nobody observed. Such a record is dropped instead
    (``load_state``), and the run reads the console again while the provider
    still lists the instance.

    Two things are damage. One is a value that is present and cannot be kept. The
    other is a key that the record of a worker that ended lacks, as the record of
    an older version does for a key that the version did not write yet. The record
    of a worker that has not ended may lack a key. While the provider lists the
    instance, the run reads its console again within its budget, and the console
    fills the key. Until then the reader counts no such worker.

    A value that the loader keeps with the evidence it carries is not damaged: a
    Spot notice whose registration identifier it normalises, and a ``steps`` list
    from which it drops a name it does not know. The reader decides the same
    verdict for the two spellings. The fields that gate a verdict (``state``,
    ``operation`` and ``exit_code``) are not here: the reader reads the value that
    the loader drops from one of them as no evidence for that record, so the
    repaired record cannot count as a futile launch.
    """
    state = record.get("state")
    if isinstance(state, str) and state in CONSOLE_FINAL_STATES:
        if any(field not in record for field in ("registered", "steps", "job_process", "spot_interruption")):
            return True
    for field, stored in (
        ("job_process", _job_process_value),
        ("spot_interruption", _spot_interruption_value),
    ):
        value = record.get(field)
        if value is not None and stored(value) is None:
            return True
    steps = record.get("steps", [])
    if not isinstance(steps, list) or not all(isinstance(name, str) for name in steps):
        return True
    return not isinstance(record.get("registered", False), bool)


def _sanitize_record(record):
    """Rebuild one persisted record from an explicit schema.

    The record is not a trusted input. Re-using it wholesale would carry unknown
    fields — and any text in them — into the rewritten state, and an arbitrary
    ``emitted`` key would suppress or forge a publication. Only the fields this
    command writes survive, and each is validated against the same closed
    vocabulary as fresh console input. Returns ``(record, changed)``, or
    ``None`` when the record is not usable at all. A record whose registration
    evidence cannot be kept is not usable (``_registration_evidence_is_damaged``).
    """
    if _registration_evidence_is_damaged(record):
        return None
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
    job_process = _job_process_value(record.get("job_process"))
    if job_process != record.get("job_process"):
        changed = True
    spot_interruption = _spot_interruption_value(record.get("spot_interruption"))
    if spot_interruption != record.get("spot_interruption"):
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
    instance_type = _instance_type(record.get("instance_type"))
    operation = _match(OPERATION_PATTERN, record.get("operation"))
    sanitized = {
        "first_seen": _number(record.get("first_seen")),
        "seen": _number(record.get("seen")),
        "state": state,
        "zone": zone,
        "instance_type": instance_type,
        "operation": operation,
        "steps": kept_steps,
        "errors": kept_kinds,
        "exit_code": exit_code,
        "job_process": job_process,
        "spot_interruption": spot_interruption,
        "registered": bool(registered),
        "emitted": emitted,
        "counted_registered": bool(record.get("counted_registered")),
        "counted_unregistered": bool(record.get("counted_unregistered")),
    }
    for key in ("first_seen", "seen", "state", "zone", "instance_type", "operation"):
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
    fleet: dict | None = None
    fleet_stale: bool = False
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
            "fleet": self.fleet,
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
        # The locks come before the clock: a run that waited would otherwise
        # judge the records of the run before it against an older clock.
        with _directory_locks(state_file, events_file, metrics_file):
            return self._run(events_file, metrics_file, state_file, now)

    def _run(self, events_file, metrics_file, state_file, now):
        started = self.clock()
        current = self.clock() if now is None else now
        summary = RunSummary(deployment=self.config.deployment)

        instances = self.aws.owned_instances(self.config.deployment)
        instances = [item for item in instances if _match(INSTANCE_PATTERN, item.instance_id) is not None]
        if not instances:
            summary.notes.append("no_owned_instances")
        # The console-read bound is a bound per run, not a bound on what the
        # provider still remembers: a terminating worker keeps its tags for about
        # an hour, so a busy fleet legitimately shows many terminal records. The
        # admission ceiling is not a read bound either: lowering it is a supported
        # drain that evicts nothing, so its surplus is reported, never refused.
        limit = self.config.controller.max_instances
        live = [item for item in instances if _normalize_state(item.state) != "terminated"]
        if len(live) > limit:
            summary.notes.append("above_configured_limit")

        # The record is read once. The state and the times that the record is
        # saved under come from the same read.
        content, times = _read_record(state_file)
        state, note = _state_from(content, current)
        if note is not None:
            summary.notes.append(note)
        prune_state(state, current)

        # A console carries evidence only while it can still change: a live
        # worker's buffer grows, and a terminal one is finished after the first
        # read that shows it left the live states. Finished records are never
        # read again, so normal turnover cannot spend the per-run budget.
        candidates = [item for item in instances if not _read_finished(state, item)]
        # Live workers first: their consoles are the ones still growing, so every
        # live worker fits whenever the live candidates fit the whole budget.
        # Termination history waits for the remaining capacity.
        candidates.sort(key=lambda item: _normalize_state(item.state) == "terminated")
        # The one condition worth refusing: more unfinished live candidates than
        # the whole budget means live evidence cannot be covered in one run.
        unread_live = [
            item for item in candidates if _normalize_state(item.state) != "terminated"
        ]
        if len(unread_live) > MAX_CONSOLE_READS:
            raise ProviderRefusal(
                "console_read_cap_exceeded",
                f"{len(unread_live)} unfinished non-terminal owned instances exceed the "
                f"per-run console read budget of {MAX_CONSOLE_READS}",
            )
        reading = candidates[:MAX_CONSOLE_READS]
        if len(candidates) > len(reading):
            # The run is partial: recording the deferral is what stops a consumer
            # from reading it as complete observation.
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
        record_text = json.dumps(state, sort_keys=True, indent=2)

        summary.duration = self.clock() - started
        # The record is saved before the textfile and its modification time
        # advances after it: ``_publish`` has the order and the reason for it.
        _publish(state_file, record_text, times, metrics_file, render_metrics(summary, current, state["console_reads"]))
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
            try:
                fleet = ledger.latest_fleet_observation()
            except Exception:  # noqa: BLE001 - auxiliary evidence, never the run
                fleet = None
                if "fleet_omitted" not in summary.notes:
                    summary.notes.append("fleet_omitted")
            else:
                if fleet is None:
                    if "fleet_omitted" not in summary.notes:
                        summary.notes.append("fleet_omitted")
                else:
                    summary.fleet = fleet
                    summary.fleet_stale = now - fleet["observed_at"] > FLEET_STALE_SECONDS
                    if summary.fleet_stale and "fleet_stale" not in summary.notes:
                        summary.notes.append("fleet_stale")
        except Exception:  # noqa: BLE001 - an absent ledger must not fail the run
            summary.spend_micros = None
            summary.committed_micros = None
            summary.allowance_micros = None
            summary.remaining_micros = None
            summary.ledger_operations = {}
            for note in ("ledger_omitted", "fleet_omitted"):
                if note not in summary.notes:
                    summary.notes.append(note)
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
        record = {
            "first_seen": now,
            "emitted": [],
            "steps": [],
            "errors": [],
            "registered": False,
            "exit_code": None,
            "job_process": None,
            "spot_interruption": None,
        }
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
    record["instance_type"] = _instance_type(instance.instance_type)
    record["operation"] = _operation_of(instance)
    for step in facts.steps:
        if step not in record["steps"]:
            record["steps"].append(step)
    for kind in facts.errors:
        if kind not in record["errors"] and len(record["errors"]) < MAX_ERROR_KINDS:
            record["errors"].append(kind)
    if facts.exit_code is not None and record.get("exit_code") is None:
        record["exit_code"] = facts.exit_code
    if facts.job_process is not None and record.get("job_process") is None:
        record["job_process"] = list(facts.job_process)
    if facts.spot_interruption is not None and record.get("spot_interruption") is None:
        record["spot_interruption"] = list(facts.spot_interruption)
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
    instance_type = _instance_type(instance.instance_type)
    if instance_type is not None:
        base["instance_type"] = instance_type
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
    job_process = record.get("job_process")
    if job_process is not None:
        outcome, status = job_process
        publish(
            f"job-process:{outcome}:{status}",
            {"event": "job-process", "outcome": outcome, "rc": status},
        )
    for kind in record["errors"]:
        publish("error:" + kind, {"event": "error", "kind": kind})
    spot_interruption = record.get("spot_interruption")
    if spot_interruption is not None:
        action, host, runner = spot_interruption
        publish(
            f"spot-interruption:{action}:{host}:{runner or 'unknown'}",
            {
                "event": "spot-interruption",
                "reason": "spot_interruption",
                "action": action,
                "host": host,
                "runner": runner,
            },
        )
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
    if set(event) - OPTIONAL_EVENT_KEYS != EVENT_SCHEMA[kind]:
        return False
    if "instance_type" in event and _instance_type(event["instance_type"]) is None:
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
    if kind == "job-process":
        outcome = event.get("outcome")
        status = event.get("rc")
        if not isinstance(outcome, str) or type(status) is not int:
            return False
        return marker == f"job-process:{outcome}:{status}" and marker in JOB_PROCESS_IDENTITIES
    if kind == "spot-interruption":
        action = event.get("action")
        host = event.get("host")
        runner = event.get("runner")
        if not isinstance(action, str) or not isinstance(host, str):
            return False
        if runner is not None and (
            not isinstance(runner, str) or _match(RUNNER_PATTERN, runner) is None
        ):
            return False
        if event.get("reason") != "spot_interruption":
            return False
        if action not in SPOT_ACTIONS or _match(HOST_PATTERN, host) is None:
            return False
        return marker == f"spot-interruption:{action}:{host}:{runner or 'unknown'}"
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

    Only a missing file is an empty stream. Any other error that reading an
    existing file raises fails the run here, before it replaces a file: a file
    that this run cannot read, such as one that another user owns, would
    otherwise be replaced by the lines of this run alone. A file that is read but
    is not UTF-8 is damaged content, and its lines are dropped.
    """
    path = Path(path)
    lines = "".join(json.dumps(event, sort_keys=True) + "\n" for event in events)
    try:
        existing = path.read_bytes().decode("utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
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


def _write_atomic(path, content, *, times=None):
    """Replace a file atomically, in its own directory.

    A collector may read the file at any moment; a partial write would be a torn
    sample, so the content (text, or bytes that are written as they are) is
    written beside it and renamed into place. ``times``, an access time and a
    modification time in nanoseconds, is set on the new file before the rename,
    so that the file is never seen under another time.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".ci-burst-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(content if isinstance(content, bytes) else content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        if times is not None:
            os.utime(temporary, ns=times)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def _directory_locks(*paths):
    """Hold an exclusive lock on the directory of each of ``paths`` while the block runs.

    A run reads or replaces the record, the event file and the metrics textfile,
    so runs that share any of them, or only a directory, take turns: a second run
    waits until the first releases its locks. The lock is on the directory and not
    on a file, so a run creates no file for it, any user who can read the
    directory can take it, and no output path can replace it. Runs whose files
    are in other directories run in parallel. The directories are created first
    and opened, and each is locked once, in the order of its device and inode
    numbers. Two runs that reach one directory under different paths, as a host
    and a container can, still lock in the same order, so they never wait for
    each other in a circle. The lock is held on the directories that the run
    opened here, and the files are named by path, so runs take turns only while
    each directory stays the same directory during a run. The launch evidence
    reader takes no lock.
    """
    opened = {}
    try:
        for path in paths:
            directory = Path(path).parent
            directory.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            status = os.fstat(descriptor)
            identity = (status.st_dev, status.st_ino)
            if identity in opened:
                os.close(descriptor)
            else:
                opened[identity] = descriptor
        for identity in sorted(opened):
            fcntl.flock(opened[identity], fcntl.LOCK_EX)
        yield
    finally:
        for descriptor in opened.values():
            os.close(descriptor)


def _publish(state_file, record_text, times, metrics_file, metrics_text):
    """Save the record, replace the metrics textfile, then advance the record's time.

    The cumulative counters live in the record and the textfile publishes them.
    A collector must never read a total that a later run lowers, so the record is
    saved first: the textfile never carries a total that the record on disk
    lacks, whether a write fails or the process stops. The record is saved under
    ``times``, the times of the record that the run read, or the epoch when it read
    none, because the modification time is the launch evidence heartbeat: a run
    that fails must not advance it, and the reader takes a file with the epoch for
    ``stale``. It is advanced last, after the textfile is replaced.

    A run that cannot replace the textfile leaves the record as it saved it, under
    those times. Its events were published before the record was saved, so the
    next run does not emit them again.
    """
    _write_atomic(state_file, record_text, times=times)
    _write_atomic(metrics_file, metrics_text)
    os.utime(state_file)


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
    if summary.fleet is not None:
        emit(
            "ci_burst_fleet_observation_timestamp_seconds",
            "gauge",
            "Unix time of the newest controller fleet snapshot.",
            [({}, int(summary.fleet["observed_at"]))],
        )
        if not summary.fleet_stale:
            running = summary.fleet["running_attempts"]
            active = summary.fleet["active_registrations"]
            emit(
                "ci_burst_fleet_running_attempts",
                "gauge",
                "Running job attempts with labels the configured worker supports, from the newest fleet snapshot.",
                [({}, running)],
            )
            emit(
                "ci_burst_fleet_active_registrations",
                "gauge",
                "Active ephemeral runner registrations in the newest fleet snapshot.",
                [({}, active)],
            )
            emit(
                "ci_burst_fleet_idle_registrations",
                "gauge",
                "Active ephemeral registrations with no running attempt to account for; the endpoints are not atomic, so this is an estimate.",
                [({}, max(0, active - running))],
            )
    emit(
        "ci_burst_export_last_run_timestamp_seconds",
        "gauge",
        "Unix time of the last observation run that wrote this file.",
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
