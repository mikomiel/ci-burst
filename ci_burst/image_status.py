"""``image status``: what is recorded, and the describe-only read that refreshes the evidence.

Plain ``status`` reads only local files (the image state file, the deployment
TOML and the backup that a journal row names) and makes no provider call, so it
builds no AWS client. It writes one thing, the check time
(``control.last_check_at``), and only when a state file exists and no pass
holds its writer lock: a status never creates the state file.

``refresh`` is ``status --refresh``. It makes describe-only reads with the image
role (``template_versions`` and ``image`` of the adapter, nothing else) and
persists only the verification evidence of the rows that a person decides
with: ``image_state``, ``template_version_exists``, ``drift_result`` and
``verified_at``, plus the observed pin. A pin that no build owns (one set by
hand, or the first pin before any candidate) has no row to carry that
evidence, so the refresh records a pin record for it (see
``ImageState.insert_pin_record``). That row is what lets a rollback verify its
target. The refresh holds the writer lock from before the first read to after the
last write, so no pass writes between what it read and what it records. All the
reads come before the writes, which are one transaction, so a read that fails
changes no row. The state file and its lock are created before the reads when
they are absent, because the lock must be held across them.
"""

import sqlite3

from . import image
from .errors import ProviderRefusal, StateRefusal
from .image_pipeline import _summary
from .image_promote import (
    MISSING_IMAGE,
    ROLLBACK_NO_PROMOTION,
    describe_rollback,
    evidence,
    pending_report,
    read_snapshot,
    try_rollback_plan,
)
from .image_state import PIN_RECORD_REASON
from .image_template import CLEAN, TEMPLATE_DRIFT, differing_fields

DAY_SECONDS = 86_400

#: What the provider says about a launch-template version that does not exist.
#: Any other refusal of a read leaves ``refresh`` with nothing persisted.
VERSION_NOT_FOUND = "InvalidLaunchTemplateId.VersionNotFound"


# -- plain status ---------------------------------------------------------------------


class _NoControl:
    """The counters of a state file that does not exist yet."""

    consecutive_failures = 0
    cooldown_until = None
    blocked = False
    last_check_at = None


def status(config, *, state, clock):
    """Report what the state file, the deployment file and the backup that a journal row names record.

    Only local files are read and no provider call is made.
    """
    now = clock()
    snapshot = read_snapshot(config)
    deployment = snapshot.config.deployment
    image_config = snapshot.config.image
    present = state.connect_readonly()
    try:
        if present:
            control = state.control_get()
            active = state.active_build(deployment)
            last = state.last_build(deployment)
            if last is not None and last.reason_code == PIN_RECORD_REASON:
                last = None  # a pin record is evidence, not a build
            candidate = state.verified_candidate(deployment)
            pending = pending_report(state, snapshot.sha256)
            rollback = describe_rollback(*try_rollback_plan(state, snapshot, now))
            builds_last_day = state.builds_started_since(deployment, now - DAY_SECONDS)
            protected = _protected(state, snapshot, candidate, active)
        else:
            control, active, last, candidate = _NoControl(), None, None, None
            pending, builds_last_day, protected = [], 0, _protected(None, snapshot, None, None)
            rollback = {
                "available": False,
                "reason": ROLLBACK_NO_PROMOTION,
                "detail": "no image state file exists",
            }
    finally:
        state.close()
    return {
        "decision": "status",
        "state": {"present": present},
        "active_build": _summary(active),
        "last_build": _summary(last),
        "candidate": evidence(candidate, now) if candidate is not None else None,
        "pin": {"version": snapshot.pin, "zone_pins": _own_zone_pins(snapshot.raw)},
        "rollback": rollback,
        "retention": {
            "retain": image_config.retain,
            "retain_min_age_seconds": image_config.retain_min_age_seconds,
            "protected": protected,
        },
        "counters": {
            "consecutive_failures": control.consecutive_failures,
            "cooldown_until": control.cooldown_until,
            "builds_last_day": builds_last_day,
            "max_builds_per_day": image_config.max_builds_per_day,
            "last_check_at": control.last_check_at,
        },
        "blocked": image.blocked_explanation() if control.blocked else {"blocked": False},
        "pending_journal": pending,
        "check_recorded": present and _record_check(state, now),
    }


def _own_zone_pins(raw):
    """The zone entries that carry their own ``launch_template_version`` (a promotion refuses these)."""
    zones = raw.get("aws", {}).get("availability_zones", [])
    return [
        {"zone_index": index, "launch_template_version": zone["launch_template_version"]}
        for index, zone in enumerate(zones)
        if isinstance(zone, dict) and "launch_template_version" in zone
    ]


def _protected(state, snapshot, candidate, active):
    """The images that no deletion path may remove, as far as the state file and the TOML know.

    This is the set of the deletion rule without its provider read: the
    ``[aws]`` pin and every zone pin come from the file, the previous pin and the
    pins of a pending row from the journal, and the candidate and the active
    build from the builds. The image of a version is the one its owner row (or
    the newest observation of that pin) records, ``None`` when nothing does.
    """
    config = snapshot.config
    items = [("pin", snapshot.pin, {})]
    for index, zone in enumerate(config.aws.zones):
        if zone.launch_template_version != snapshot.pin:
            items.append(("zone_pin", zone.launch_template_version, {"zone_index": index}))
    builds = []
    if state is not None:
        previous = state.journal_last_complete()
        if previous is not None:
            items.append(("previous_pin", previous.old_pin, {"journal_id": previous.id}))
        for row in state.pending_journal():
            items.append(("pending_journal", row.old_pin, {"journal_id": row.id}))
            items.append(("pending_journal", row.new_pin, {"journal_id": row.id}))
        builds = [("candidate", candidate), ("active_build", active)]
    report = [
        {"role": role, "version": version, "image_id": _image_of(state, config.deployment, version), **extra}
        for role, version, extra in items
    ]
    report.extend(
        {"role": role, "version": build.template_version, "image_id": build.image_id, "build_id": build.id}
        for role, build in builds
        if build is not None
    )
    return report


def _image_of(state, deployment, version):
    """The image id that the state file records for a launch-template version, or ``None``."""
    if state is None:
        return None
    owner = state.version_owner(deployment, version)
    if owner is not None and owner.image_id:
        return owner.image_id
    seen = state.pin_for_version(version)
    return seen.image_id if seen is not None else None


def _record_check(state, now):
    """Record the check time. A status that cannot (a pass holds the lock, a read-only directory) still reports.

    The write is best-effort, so a database error (for example a read-only state
    file) reports ``check_recorded: false`` and does not fail the read-only status.
    The answer reflects the write alone: nothing is read back, and a failure to close
    the connection or release the lock afterwards, which cannot undo a committed
    write, is ignored here too.
    """
    recorded = False
    try:
        state.acquire_writer()  # before the writable open, which rolls back a hot journal
        state.refuse_a_hot_journal()  # a read-only status never rolls a journal back
        state.connect()
        state.record_check_time(now)
        recorded = True
    except (StateRefusal, OSError, sqlite3.Error):
        pass
    finally:
        # Always runs, an interruption included, so the lock is never left held.
        try:
            state.close()
        except (OSError, sqlite3.Error):
            pass
    return recorded


# -- the describe-only refresh ----------------------------------------------------------


def refresh(config, *, state, aws, clock, candidates=True):
    """Read the provider (describe only) and persist the verification evidence it gives.

    The rows that are refreshed are the live pin's, the rollback target's and, with
    ``candidates``, the newest ``retain`` verified candidates. A pin that no build
    owns gets a pin record, even when the provider no longer knows its version,
    so a rollback to it refuses with the evidence instead of for want of a
    record. ``image run --apply`` refreshes the pins only (``candidates=False``),
    so a pin that a run observed is a usable rollback target without a separate
    command. A refusal of a read is raised and no row is changed. The state file
    is opened here (and created with its lock when absent, before the reads),
    locked for the whole refresh, and closed.
    """
    now = clock()
    snapshot = read_snapshot(config)
    deployment = snapshot.config.deployment
    try:
        state.acquire_writer()  # before the writable open, which rolls back a hot journal
        state.connect()
        reads = _Reads(aws, snapshot.config.aws.launch_template_name)
        planned = [
            _evidence_for(reads, state, deployment, version, row, now)
            for version, row in _targets(state, snapshot, candidates)
        ]
        observed = _observed_pin(reads, snapshot.pin)
        created, refreshed = [], []
        with state.transaction():
            for version, row, values, image_id in planned:
                if row is None:
                    row = state.insert_pin_record(deployment=deployment, version=version, image_id=image_id)
                    created.append(version)
                state.record_verification(row.id, **values)
                refreshed.append({"build_id": row.id, "template_version": version, **values})
            logged = observed is not None and state.pin_observe(snapshot.pin, observed)
    finally:
        state.close()
    missing = sorted(item["template_version"] for item in refreshed if not item["template_version_exists"])
    return {
        "decision": "refreshed",
        "refreshed": refreshed,
        "pin_records_created": created,
        "pin_observed": bool(logged),
        "missing_versions": missing,
    }


def _targets(state, snapshot, candidates):
    """The ``(version, owner row or None)`` pairs to describe, the pin first, each version once."""
    config = snapshot.config
    deployment = config.deployment
    versions = [snapshot.pin]
    promotion = state.journal_last_complete(kind="promotion")
    if promotion is not None and promotion.new_pin == snapshot.pin:
        versions.append(promotion.old_pin)
    if candidates:
        verified = [
            build
            for build in state.builds(deployment)
            if build.state == "candidate" and build.verified_at is not None and build.template_version is not None
        ]
        versions.extend(build.template_version for build in reversed(verified[-config.image.retain :]))
    seen, targets = set(), []
    for version in versions:
        if version not in seen:
            seen.add(version)
            targets.append((version, state.version_owner(deployment, version)))
    return targets


class _Reads:
    """The describe reads of one refresh, each made once."""

    def __init__(self, aws, template_name):
        self._aws = aws
        self._name = template_name
        self._versions = {}
        self._images = {}

    def version(self, number):
        """The ``LaunchTemplateData`` of a version, or ``None`` when the provider says it does not exist.

        The adapter's fake answers an absent version with an empty list. The live
        adapter refuses a read with the provider's error code, and for a version
        that is not there the code is ``VERSION_NOT_FOUND``. Both are absence. Any
        other refusal stands: it says nothing about the version.
        """
        if number not in self._versions:
            try:
                read = self._aws.template_versions(self._name, [number])
            except ProviderRefusal as refusal:
                if refusal.detail != VERSION_NOT_FOUND:
                    raise
                read = []
            if read and (len(read) != 1 or read[0].version != number):
                raise ProviderRefusal(
                    "AWS_INVENTORY_INCOMPLETE", f"the read did not return exactly version {number}", uncertain=True
                )
            self._versions[number] = read[0].data if read else None
        return self._versions[number]

    def image(self, image_id):
        """The state of an image: its provider state, ``missing`` when unknown, ``None`` with no image id."""
        if image_id is None:
            return None
        if image_id not in self._images:
            found = self._aws.image(image_id)
            self._images[image_id] = MISSING_IMAGE if found is None else found.state
        return self._images[image_id]


def _literal_image(value):
    return value if isinstance(value, str) and value.startswith("ami-") else None


def _evidence_for(reads, state, deployment, version, row, now):
    """``(version, row, values, image id)``: what to record for one version and the image it concerns."""
    data = reads.version(version)
    image_id = _literal_image(row.image_id) if row is not None else None
    if image_id is None and data is not None:
        image_id = _literal_image(data.get("ImageId"))
    if image_id is None and row is None:
        seen = state.pin_for_version(version)
        image_id = seen.image_id if seen is not None else None
    drift = row.drift_result if row is not None else None
    if row is not None and row.source_pin is not None:
        source = reads.version(row.source_pin)
        if data is None or source is None:
            drift = None
        else:
            drift = CLEAN if not differing_fields(source, data, row.image_id) else TEMPLATE_DRIFT
    values = {
        "image_state": reads.image(image_id),
        "template_version_exists": int(data is not None),
        "drift_result": drift,
        "verified_at": now,
    }
    return version, row, values, image_id


def _observed_pin(reads, pin):
    """The literal image id that the live pin names, or ``None``."""
    data = reads.version(pin)
    return _literal_image(data.get("ImageId")) if data is not None else None
