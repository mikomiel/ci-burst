"""Read-only launch evidence: a brake after repeated futile launches.

A worker that cannot register ends within minutes, its slot frees, and the job
it was launched for still waits, so the next pass launches again. This module
reads the observation record that ``export`` writes beside the ledger and tells
admission whether the most recent finished workers all ended without
registering.

The record can only withhold a launch. It never authorizes one, so a missing,
unreadable, stale or malformed record is no evidence and the gate stays open.
The reader opens one regular file read-only, without blocking and without
following a link. It never writes, repairs, locks or deletes it, makes no
provider, queue or ledger call, and never raises.

A worker counts only when the ledger records it for this deployment. The caller
passes the operations that its pass reads from the ledger, as a mapping from the
operation identifier to the instance identifier that the ledger holds for it, or
``None`` while it holds none, and it leaves out each row whose frozen request
names another deployment: a rename leaves such rows behind as history, and their
workers are no evidence for the new name. A record whose operation is not in the
mapping, or whose operation the ledger ties to another instance, is no evidence.
``export`` writes the operation tag of any owned instance that the provider
lists, and reconciliation ignores a terminated instance that no ledger operation
names, so the tag alone does not show a launch of this deployment.

The reader decides from the open descriptor before it reads a byte: a file that
is not a regular one is ``unreadable``, a file outside the freshness bound is
``stale`` whatever its size, and a file above ``MAX_RECORD_BYTES`` is
``malformed``. It then reads the descriptor itself, with no buffered stream in
between, and stops once it holds one byte more than that bound. A file that
grows past the bound after its size was taken is ``malformed`` as well, and the reader fetches
no more than that one byte past the bound from it: a buffered read of that last
byte would fetch a whole block more.

The reader takes only closed-vocabulary facts that ``export`` has already
validated, never console text. A field of the wrong type, an instance key that
``export`` rejects and a registration marker that ``export`` cannot write each
drop the whole file. So does a JSON constant that is not standard JSON
(``NaN``, ``Infinity``, ``-Infinity``), and so does a JSON object that names a
member twice, at any level: ``export`` writes neither, and the decoder hooks that
refuse them are ``export``'s own (``_refuse_repeated_member`` says why), so
``export`` drops the same file when it loads its record. A value of the right
type that ``export`` drops from ``state``, ``operation`` or ``steps`` is no
evidence, which is also what ``export`` makes of it when it loads its own record,
so it never creates a hold. A value that bears on registration and that
``export`` cannot keep drops its record there, and so does a registration key
that the record of an ended worker lacks (``_registration_evidence_is_damaged``).
The run reads the worker's console again while the provider lists the instance, and
leaves no record when it does not: ``export`` never writes back, under a fresh
heartbeat, a repaired record that shows no registration.
A record dated after the controller clock drops the whole file as well, so
``export`` drops such a record when it loads its own, against the clock of its
run (``load_state``). The file that a run writes then holds no record that this
reader refuses for its date, and a clock that ran backwards cannot keep it
``malformed``.
The fields it reads, the record version and the rules that decide a shape are
imported from ``export``; a contract test in ``tests/test_export.py`` fails when
the two drift apart.
"""

import json
import os
import stat
from dataclasses import dataclass

from .export import (
    CONSOLE_FINAL_STATES,
    INSTANCE_PATTERN,
    OPERATION_PATTERN,
    POST_REGISTRATION_STEPS,
    STATE_VERSION,
    _job_process_value,
    _match,
    _number,
    _open_for_reading,
    _refuse_constant,
    _refuse_repeated_member,
    _spot_interruption_value,
)

#: A hold needs at least this many considered workers, all futile.
FUTILE_LAUNCH_COUNT = 3

#: A worker is considered while the controller clock minus its ``seen`` is at
#: most this many seconds.
FUTILE_LAUNCH_WINDOW_SECONDS = 3600

#: The record is evidence while its modification time is at most this many
#: seconds before the controller clock. ``export`` advances its modification
#: time last on every successful run, so that time is a heartbeat.
EVIDENCE_MAX_AGE_SECONDS = 600

#: The reader accepts a record of at most this many bytes. ``export`` bounds the
#: record by age (``STATE_RETENTION_SECONDS``) and not by size, so this is the
#: reader's own bound. One instance serializes to about 0.7 to 1.3 KiB, so the
#: bound holds about 13,000 to 23,000 instances. A larger file is ``malformed``
#: and is not read, so the gate stays open.
MAX_RECORD_BYTES = 16 * 1024 * 1024

#: The reader asks the descriptor for at most this many bytes at a time. Each
#: request is also capped by what ``MAX_RECORD_BYTES`` still allows.
_READ_CHUNK_BYTES = 1024 * 1024

MISSING = "missing"
UNREADABLE = "unreadable"
STALE = "stale"
MALFORMED = "malformed"
FRESH = "fresh"

REGISTERED = "registered"
FUTILE = "futile"

#: The fields the gate reads in every record. Each one must be present: a
#: renamed field would otherwise read as null and could turn a registered
#: worker into a futile one.
RECORD_FIELDS = (
    "seen",
    "state",
    "operation",
    "exit_code",
    "job_process",
    "spot_interruption",
    "steps",
    "registered",
)


@dataclass(frozen=True)
class LaunchEvidence:
    """The evidence state and, when it is fresh, the considered and futile counts."""

    state: str
    considered: int = 0
    futile: int = 0

    @property
    def holds(self):
        """True when the considered workers are enough and every one is futile."""
        return (
            self.state == FRESH
            and self.considered >= FUTILE_LAUNCH_COUNT
            and self.futile == self.considered
        )

    def as_dict(self):
        return {"state": self.state, "considered": self.considered, "futile": self.futile}


def read_launch_evidence(path, now, recorded):
    """Return the launch evidence in the record at ``path`` against the clock ``now``.

    ``recorded`` maps each operation identifier that the ledger holds for this
    deployment to its instance identifier, or to ``None`` while the ledger holds
    none. A worker counts only when its operation is in it, under the instance
    that the record names when the ledger names one. The argument is required, so
    that a caller cannot count workers that no operation of this deployment
    launched.

    The states are tested in the order ``missing``, ``unreadable``, ``stale``,
    ``malformed``. A file above ``MAX_RECORD_BYTES`` is ``malformed``, and it is
    tested after its age, so a stale file is ``stale`` whatever its size. A
    failure that this function did not foresee is ``unreadable``, because a pass
    must not fail for an optional brake.
    """
    try:
        return _read(path, now, recorded)
    except Exception:  # noqa: BLE001 - an optional brake never fails the pass
        return LaunchEvidence(UNREADABLE)


def _read_to_bound(descriptor):
    """Return what ``descriptor`` holds, fetching at most ``MAX_RECORD_BYTES + 1`` bytes.

    The reads go straight to the descriptor, and each asks for no more than the
    bound still allows, so a file that grew after its size was taken costs one byte
    past the bound and no more. A buffered stream cannot promise that: to return
    the last byte it fetches a whole block and keeps the rest in its buffer.
    ``os.read`` is used, and not the stream's ``read``, because the descriptor is
    non-blocking: a read that finds no data raises ``BlockingIOError``, which the
    caller maps to ``unreadable``, where ``FileIO.read`` would return ``None``.
    """
    chunks = []
    fetched = 0
    while fetched <= MAX_RECORD_BYTES:
        chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, MAX_RECORD_BYTES + 1 - fetched))
        if not chunk:
            break
        chunks.append(chunk)
        fetched += len(chunk)
    return b"".join(chunks)


def _read(path, now, recorded):
    try:
        # The stream is unbuffered and only owns the descriptor: the content is
        # read from the descriptor itself, by ``_read_to_bound``.
        stream = open(path, "rb", buffering=0, opener=_open_for_reading)
    except FileNotFoundError:
        return LaunchEvidence(MISSING)
    except OSError:
        return LaunchEvidence(UNREADABLE)
    with stream:
        try:
            # The type, the time and the size come from the open descriptor, so a
            # replacement of the path by ``export`` cannot pair one file's time
            # with another's content, and nothing is read from a file that is
            # not a regular one, is not fresh or is above the bound.
            descriptor = stream.fileno()
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode):
                return LaunchEvidence(UNREADABLE)
            age = now - status.st_mtime
            if not 0 <= age <= EVIDENCE_MAX_AGE_SECONDS:
                return LaunchEvidence(STALE)
            if status.st_size > MAX_RECORD_BYTES:
                return LaunchEvidence(MALFORMED)
            # One byte more than the bound shows a file that grew after its size
            # was taken, without reading it whole.
            raw = _read_to_bound(descriptor)
        except OSError:
            return LaunchEvidence(UNREADABLE)
    if len(raw) > MAX_RECORD_BYTES:
        return LaunchEvidence(MALFORMED)
    records = _records(raw, now)
    if records is None:
        return LaunchEvidence(MALFORMED)
    return _evaluate(records, now, recorded)


def _records(raw, now):
    """Return the validated ``(instance identifier, record)`` pairs, or ``None`` if malformed.

    One invalid record drops the whole file: skipping it could leave older futile
    records among the newest three.
    """
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            parse_constant=_refuse_constant,
            object_pairs_hook=_refuse_repeated_member,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return None
    instances = payload.get("instances")
    if not isinstance(instances, dict):
        return None
    records = []
    for instance_id, record in instances.items():
        # ``export`` drops a record under a key that is not an instance
        # identifier, so a record filed under one is not its output.
        if _match(INSTANCE_PATTERN, instance_id) is None or not _record_is_valid(record, now):
            return None
        records.append((instance_id, record))
    return records


def _record_is_valid(record, now):
    if not isinstance(record, dict) or any(key not in record for key in RECORD_FIELDS):
        return False
    # The exporter's own timestamp rule: a finite number that is not a boolean.
    seen = _number(record["seen"])
    # A record dated after the clock is not the output of a run that kept to it.
    # ``export`` applies the same comparison when it loads its record, so it never
    # carries such a record into a file that this reader would refuse whole.
    if seen is None or seen > now:
        return False
    for key in ("state", "operation"):
        if record[key] is not None and not isinstance(record[key], str):
            return False
    exit_code = record["exit_code"]
    if exit_code is not None and (type(exit_code) is not int or not 0 <= exit_code <= 255):
        return False
    # Registration evidence is read from these two markers, so each is null or
    # exactly the value that ``export`` stores: its own loader keeps that value
    # unchanged, and it drops any other.
    for key, stored in (
        ("job_process", _job_process_value),
        ("spot_interruption", _spot_interruption_value),
    ):
        if record[key] is not None and stored(record[key]) != record[key]:
            return False
    steps = record["steps"]
    if not isinstance(steps, list) or not all(isinstance(step, str) for step in steps):
        return False
    return isinstance(record["registered"], bool)


def classify(record):
    """Return ``registered``, ``futile`` or ``None`` (no evidence) for one valid record.

    Only a record that shows the instance ended and carries an operation of this
    deployment is classified. Registration evidence is the registration step, a
    step printed only after it, a one-job process outcome or a Spot interruption
    notice. Futile needs the exit marker, which shows the console was read after
    the script reached its end, and no registration evidence.
    """
    if record["state"] not in CONSOLE_FINAL_STATES:
        return None
    operation = record["operation"]
    if not isinstance(operation, str) or OPERATION_PATTERN.fullmatch(operation) is None:
        return None
    steps = set(record["steps"])
    if (
        record["registered"]
        or "register" in steps
        or steps & POST_REGISTRATION_STEPS
        or record["job_process"] is not None
        or record["spot_interruption"] is not None
    ):
        return REGISTERED
    if record["exit_code"] is not None:
        return FUTILE
    return None


def _is_recorded(instance_id, record, recorded):
    """Return True when the ledger holds the operation, under this instance if it names one."""
    operation = record["operation"]
    return operation in recorded and recorded[operation] in (None, instance_id)


def _evaluate(records, now, recorded):
    """Count the considered workers and the futile ones among them."""
    classified = []
    for instance_id, record in records:
        verdict = classify(record)
        if verdict is None or not _is_recorded(instance_id, record, recorded):
            continue
        seen = float(record["seen"])
        if now - seen <= FUTILE_LAUNCH_WINDOW_SECONDS:
            classified.append((seen, verdict))
    classified.sort(key=lambda item: item[0], reverse=True)
    if len(classified) > FUTILE_LAUNCH_COUNT:
        # Every worker that ties the last place is considered, so the verdict
        # never depends on the order of opaque instance identifiers.
        third = classified[FUTILE_LAUNCH_COUNT - 1][0]
        classified = [item for item in classified if item[0] >= third]
    futile = sum(1 for _, verdict in classified if verdict == FUTILE)
    return LaunchEvidence(FRESH, considered=len(classified), futile=futile)
