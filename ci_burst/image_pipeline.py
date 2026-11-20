"""One automated warm-image build, from the source sha to an inert launch-template version.

``ImagePipeline`` orchestrates the building blocks of the image path and owns no
rule of its own: ``image`` decides what a build is made of and whether one may
start, ``image_console`` reads the builder's verdict, ``image_template`` creates
and checks the candidate version, ``image_deletion`` is the one deletion rule, and
``ImageAws``, ``ForgejoSource`` and ``ImageState`` are the only places that touch
the provider, the forge and the state file. This module decides the order of the
steps and what a pass writes down.

Three entry points make up the Python API (the command line is task 6.1):

* ``plan`` reads and writes nothing. It resolves the sha of the protected
  branch, hashes the basis files, reads the pin, the build template and the
  state file (never creating it), and says whether a build would start.
* ``run_pass(apply=True)`` runs ONE bounded pass of ``PASS_DEADLINE_SECONDS``.
  A build takes longer than a pass, so every pass resumes the build the state
  file names.
* ``run_until_done`` repeats passes with a fixed sleep until the build ends.

A pass holds the state file's writer lock and runs, in this order:

1. reconcile: terminate every builder that is not the active build's, delete
   stray archives, adopt what a crash left behind, record an image that has no
   row as an unverified candidate
2. observe the configured pin
3. with no active build, decide and, when a build is due, start it; with one,
   advance it (``building`` to ``imaging`` to ``candidate`` or ``failed``)
4. retention: the one deletion rule, then the telemetry files

The pass that first sees the marker calls ``CreateImage`` before it does any
other step. The builder is terminated only after the image is ``available``.

Secrets. The presigned archive URL and the rendered user data live in local
variables of ``_Pass._launch`` and nowhere else: not in the state file, not in an
event, not in the metrics, not in an exception detail. Events pass through a
whitelist of fields and values (``_Telemetry.event``), so a value that is not an
id, a short digest, a count or a reason code is refused instead of written. Only
the failed-build event carries console text, and only the framed, sanitized tail
that ``image_console.framed_tail`` returns.

Files written into the state directory besides ``image-state.sqlite``:
``image-events.ndjson`` (append-only), ``image-metrics.prom`` (replaced
atomically) and ``image-telemetry.json`` (the counters that cannot be derived
from the state file, replaced atomically).
"""

import json
import math
import os
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import image, image_console, image_deletion
from .aws import BASIS_DIGEST_TAG, BUILD_ID_TAG, BUILDER_STATES, DEPLOYMENT_TAG, IMAGE_BUILD_TAG
from .errors import DeadlineRefusal, ImageRefusal, ProviderRefusal, Refusal
from .export import _label, _write_atomic
from .forgejo import require_protected
from .image_state import ACTIVE_STATES, BUILD_STATES, Control
from .image_template import CLEAN, TEMPLATE_DRIFT, create_candidate_version

#: One pass is bounded to this many seconds, and the console and the image are
#: polled every ``CONSOLE_POLL_SECONDS`` inside it. ``CLEANUP_RESERVE_SECONDS``
#: before the end of the pass, polling stops so that retention still runs under
#: the same deadline. ``SUPERVISOR_SLEEP_SECONDS`` is the fixed sleep between the
#: passes of ``run_until_done``.
PASS_DEADLINE_SECONDS = 240
CONSOLE_POLL_SECONDS = 30
CLEANUP_RESERVE_SECONDS = 30
SUPERVISOR_SLEEP_SECONDS = 30

#: An object under the staging prefix that is not the active archive goes once it
#: is strictly older than this.
STRAY_ARCHIVE_AGE_SECONDS = 3600

#: EC2 can answer "no such instance" for a few seconds after ``RunInstances``. A
#: builder that is not listed within this long of being recorded is not treated
#: as gone yet.
BUILDER_VISIBILITY_GRACE_SECONDS = 60

#: The decisions of a result. ``building``, ``imaging``, ``candidate`` and
#: ``failed`` name the state a build is in after the pass; ``start`` and
#: ``digest_unchanged`` come from ``plan``; ``refused`` carries a ``reason``.
DISABLED = "disabled"
REFUSED = "refused"
IN_PROGRESS = ("building", "imaging")

#: Reason codes that this module adds. ``IMAGE_ORPHAN_RECORDED`` is the reason of
#: a row that records an image nobody built here; it is not a failure.
IMAGE_GATE_REQUIRED = "IMAGE_GATE_REQUIRED"
IMAGE_BUILD_TEMPLATE_UNSAFE = "IMAGE_BUILD_TEMPLATE_UNSAFE"
IMAGE_BUILDER_GONE = "IMAGE_BUILDER_GONE"
IMAGE_NOT_AVAILABLE = "IMAGE_NOT_AVAILABLE"
IMAGE_ORPHAN_RECORDED = "IMAGE_ORPHAN_RECORDED"
IMAGE_BUILD_ALREADY_ACTIVE = "IMAGE_BUILD_ALREADY_ACTIVE"

#: Tag keys that ``CreateImage`` adds to the ones the adapter sets. The build
#: policy (``deploy/iam/image-build-policy.json``) allows exactly these keys, and
#: a test pins the two together.
SOURCE_SHA_TAG = "ci-burst:source-sha"
SCRIPT_SHA_TAG = "ci-burst:script-sha"
BASE_IMAGE_TAG = "ci-burst:base-image"

EVENTS_FILE = "image-events.ndjson"
METRICS_FILE = "image-metrics.prom"
TELEMETRY_FILE = "image-telemetry.json"

#: The kinds of leaked resource that reconcile finds and removes.
LEAK_KINDS = ("builder", "archive", "image", "snapshot")

#: Image states that end a build. ``pending`` and ``transient`` keep it waiting,
#: and so does an image the provider does not list yet (a read right after
#: ``CreateImage`` may not see it).
_IMAGE_FAILED_STATES = frozenset({"failed", "error", "invalid", "deregistered", "disabled"})

DAY_SECONDS = 86_400

#: A real presigned URL of an assumed role carries a session token and is a few
#: thousand characters long. ``plan`` sizes the user data with a URL of this
#: length made of plain characters, so no secret-shaped literal exists here.
SYNTHETIC_URL_CHARS = 4096


# -- small helpers -------------------------------------------------------------


def _hex12(value):
    return value[:12] if isinstance(value, str) and value else None


def _safe_name(value):
    """The deployment name as an image name accepts it (no ``:``)."""
    return re.sub(r"[^A-Za-z0-9._-]", "-", value)


def _summary(build):
    """The part of a build row that a result may carry: ids, short digests and codes."""
    if build is None:
        return None
    return {
        "id": build.id,
        "state": build.state,
        "reason_code": build.reason_code or None,
        "basis_digest": _hex12(build.basis_digest),
        "source_sha": _hex12(build.source_sha),
        "builder_id": build.builder_id,
        "image_id": build.image_id,
        "template_version": build.template_version,
        "created_at": build.created_at,
        "deadline_at": build.deadline_at,
        "verified_at": build.verified_at,
    }


def _pin_summary(pin):
    return {"version": pin.version, "image_id": pin.image_id, "basis_digest": _hex12(pin.digest)}


def _control_summary(control):
    return {
        "consecutive_failures": control.consecutive_failures,
        "blocked": control.blocked,
        "cooldown_until": control.cooldown_until,
    }


@dataclass(frozen=True)
class _Pin:
    """What the configured pin names: its image and that image's basis-digest tag."""

    version: int
    image_id: str | None
    digest: str | None
    created_at: float | None


@dataclass
class _Facts:
    """Everything a decision reads from the state file."""

    control: Control
    last_build: object
    last_failed: object
    builds_last_day: int
    active: object
    candidate: object
    previous_pin: int | None
    pending: list
    builds: list


def _read_facts(state, deployment, now):
    """The state file's answers, or the answers of a new file when there is none."""
    if not state.available:
        return _Facts(Control(0, None, False, None), None, None, 0, None, None, None, [], [])
    journal = state.journal_last_complete()
    return _Facts(
        control=state.control_get(),
        last_build=state.last_build(deployment),
        last_failed=state.last_failed_build(deployment),
        builds_last_day=state.builds_started_since(deployment, now - DAY_SECONDS),
        active=state.active_build(deployment),
        # The newest candidate that was verified, not the newest candidate: an
        # orphan image is recorded as a candidate that nobody verified.
        candidate=state.verified_candidate(deployment),
        previous_pin=journal.old_pin if journal is not None else None,
        pending=state.pending_journal(),
        builds=state.builds(deployment),
    )


def _template_problems(data):
    """The names of the properties of a build template that are not safe. Names only, never values."""
    problems = []
    if data.get("IamInstanceProfile"):
        problems.append("IamInstanceProfile")
    # The builder request sends no market option, so a template that carries one
    # would launch a spot builder, and a builder is on-demand only.
    if data.get("InstanceMarketOptions"):
        problems.append("InstanceMarketOptions")
    options = data.get("MetadataOptions")
    options = options if isinstance(options, Mapping) else {}
    if options.get("HttpTokens") != "required":
        problems.append("MetadataOptions.HttpTokens")
    hop_limit = options.get("HttpPutResponseHopLimit")
    if type(hop_limit) is not int or hop_limit != 1:
        problems.append("MetadataOptions.HttpPutResponseHopLimit")
    if data.get("InstanceInitiatedShutdownBehavior") != "terminate":
        problems.append("InstanceInitiatedShutdownBehavior")
    image_id = data.get("ImageId")
    if not isinstance(image_id, str) or not image_id.startswith("ami-"):
        problems.append("ImageId")
    devices = _ebs_devices(data)
    # With no EBS mapping the root volume comes from the base image, so neither
    # its type nor its deletion is established by the template: fail closed.
    if not devices:
        problems.append("BlockDeviceMappings")
    if any(device.get("VolumeType") != "gp3" for device in devices):
        problems.append("BlockDeviceMappings.Ebs.VolumeType")
    if any(device.get("DeleteOnTermination") is not True for device in devices):
        problems.append("BlockDeviceMappings.Ebs.DeleteOnTermination")
    return problems


def _ebs_devices(data):
    mappings = data.get("BlockDeviceMappings")
    if not isinstance(mappings, list):
        return []
    return [item["Ebs"] for item in mappings if isinstance(item, Mapping) and isinstance(item.get("Ebs"), Mapping)]


def _volume_gib(data):
    """The size of the first EBS volume, reported and never refused on."""
    for device in _ebs_devices(data):
        size = device.get("VolumeSize")
        if type(size) is int:
            return size
    return None


def _deletion_counts(report):
    """Counts of a deletion report, and the resources that the ``orphan`` path removed."""
    counts = {"deregistered": 0, "snapshots_deleted": 0, "failed": 0, "skipped": 0, "planned": 0}
    leaked = {"image": 0, "snapshot": 0}
    for row in report:
        result = row["result"]
        if result == image_deletion.DEREGISTERED:
            counts["deregistered"] += 1
            leaked["image"] += row["path"] == image_deletion.ORPHAN
        elif result == image_deletion.SNAPSHOT_DELETED:
            counts["snapshots_deleted"] += 1
            leaked["snapshot"] += row["path"] == image_deletion.ORPHAN
        elif result in (
            image_deletion.DEREGISTER_FAILED,
            image_deletion.DEREGISTER_UNCONFIRMED,
            image_deletion.SNAPSHOT_FAILED,
        ):
            counts["failed"] += 1
        elif result == image_deletion.SKIPPED:
            counts["skipped"] += 1
        elif result == image_deletion.PLANNED:
            counts["planned"] += 1
    return counts, leaked


# -- events, telemetry and metrics --------------------------------------------

#: The fields each event may carry besides ``ts``, ``deployment`` and ``event``.
#: A field outside its event's list is a programming error and is refused.
_EVENT_FIELDS = {
    "image_check": frozenset(
        {"decision", "reason", "forced", "build_id", "digest", "pin_digest", "pin_version", "source_sha"}
    ),
    "image_build_started": frozenset({"build_id", "builder_id", "digest", "source_sha", "forced", "deadline_at"}),
    "image_marker_seen": frozenset({"build_id", "builder_id", "latency_seconds"}),
    "image_created": frozenset({"build_id", "builder_id", "image_id"}),
    "image_candidate_ready": frozenset(
        {"build_id", "image_id", "template_version", "digest", "duration_seconds"}
    ),
    "image_build_failed": frozenset(
        {"build_id", "reason", "image_id", "digest", "duration_seconds", "drift_fields", "framed_tail"}
    ),
    "image_reconciled": frozenset(
        {"builders_terminated", "archives_deleted", "images_recorded", "builder_adopted", "image_adopted", "build_id"}
    ),
    "image_pruned": frozenset({"deregistered", "snapshots_deleted", "failed", "skipped"}),
    "image_blocked": frozenset({"consecutive_failures", "build_id"}),
}

#: An ordinary event value is an id, a digest, a code or a short list of names. A
#: URL has characters outside this set, so one cannot be written by accident.
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9._:,/ -]{0,200}")
_TAIL_FIELD = "framed_tail"
_TAIL_EVENT = "image_build_failed"


def _count(value):
    return value if type(value) is int and value >= 0 else 0


def _number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _check_event_value(name, key, value):
    if value is None or isinstance(value, (bool, int, float)):
        return
    if key == _TAIL_FIELD and name == _TAIL_EVENT and isinstance(value, str):
        return
    if isinstance(value, str) and _PLAIN_VALUE.fullmatch(value):
        return
    raise ValueError(f"event {name}: field {key} holds a value that is not an id, a digest, a count or a code")


def _append_line(path, line):
    """Append one line with one write, so a reader never sees half of it."""
    data = memoryview(line.encode("utf-8"))
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        while data:
            data = data[os.write(descriptor, data) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _Telemetry:
    """The event file, the counters that survive between passes and the metrics textfile."""

    def __init__(self, directory, deployment, clock):
        self._directory = Path(directory)
        self._deployment = deployment
        self._clock = clock
        self.data = self._load()

    def _load(self):
        """The saved counters; a missing or damaged file starts them again and never stops a pass."""
        try:
            raw = json.loads((self._directory / TELEMETRY_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        leaked = raw.get("leaked") if isinstance(raw.get("leaked"), dict) else {}
        outcomes = raw.get("last_outcome_at") if isinstance(raw.get("last_outcome_at"), dict) else {}
        return {
            "leaked": {kind: _count(leaked.get(kind)) for kind in LEAK_KINDS},
            "last_outcome_at": {kind: _number(outcomes.get(kind)) for kind in ("candidate", "failed")},
            "build_duration_seconds": _number(raw.get("build_duration_seconds")),
            "console_latency_seconds": _number(raw.get("console_latency_seconds")),
            "pin_image_created_at": _number(raw.get("pin_image_created_at")),
        }

    def event(self, name, **fields):
        allowed = _EVENT_FIELDS[name]
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"event {name} has no field {key}")
            _check_event_value(name, key, value)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock())),
            "deployment": self._deployment,
            "event": name,
            **fields,
        }
        self._directory.mkdir(parents=True, exist_ok=True)
        _append_line(self._directory / EVENTS_FILE, json.dumps(record, sort_keys=True) + "\n")

    def leak(self, kind, count):
        if count:
            self.data["leaked"][kind] += count

    def outcome(self, kind, build, now):
        self.data["last_outcome_at"][kind] = float(now)
        self.data["build_duration_seconds"] = max(0.0, float(now - build.created_at))

    def save(self):
        _write_atomic(self._directory / TELEMETRY_FILE, json.dumps(self.data, sort_keys=True) + "\n")

    def write_metrics(self, state, now):
        _write_atomic(self._directory / METRICS_FILE, self.render(state, now))

    def render(self, state, now):
        """One Prometheus textfile. Every series names the deployment."""
        lines = []

        def emit(name, kind, help_text, samples):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")
            for labels, value in samples:
                pairs = {"deployment": self._deployment, **labels}
                rendered = ",".join(f'{key}="{_label(item)}"' for key, item in pairs.items())
                lines.append(f"{name}{{{rendered}}} {value}")

        builds = state.builds(self._deployment) if state.available else []
        control = state.control_get() if state.available else Control(0, None, False, None)
        candidate = state.verified_candidate(self._deployment) if state.available else None
        data = self.data
        by_state = {name: 0 for name in BUILD_STATES}
        for build in builds:
            by_state[build.state] += 1
        emit(
            "ci_burst_image_builds",
            "gauge",
            "Image builds by state.",
            [({"state": name}, count) for name, count in by_state.items()],
        )
        emit(
            "ci_burst_image_last_build_timestamp_seconds",
            "gauge",
            "Unix time of the last build that ended with this outcome.",
            [({"outcome": kind}, int(at)) for kind, at in data["last_outcome_at"].items() if at is not None],
        )
        if data["build_duration_seconds"] is not None:
            emit(
                "ci_burst_image_build_duration_seconds",
                "gauge",
                "Seconds from the start to the end of the last build that ended.",
                [({}, round(data["build_duration_seconds"], 3))],
            )
        if data["console_latency_seconds"] is not None:
            emit(
                "ci_burst_image_console_latency_seconds",
                "gauge",
                "Seconds from the build start to the first sighting of the marker on the console.",
                [({}, round(data["console_latency_seconds"], 3))],
            )
        emit(
            "ci_burst_image_consecutive_failures",
            "gauge",
            "Builds that failed since the last build that reached a candidate.",
            [({}, control.consecutive_failures)],
        )
        emit(
            "ci_burst_image_blocked",
            "gauge",
            "1 while builds are blocked until the basis digest changes or a run is forced.",
            [({}, int(control.blocked))],
        )
        emit(
            "ci_burst_image_candidate_waiting_seconds",
            "gauge",
            "Seconds since the newest verified candidate was verified; 0 when none waits.",
            [({}, int(max(0.0, now - candidate.verified_at)) if candidate is not None else 0)],
        )
        if data["pin_image_created_at"] is not None:
            emit(
                "ci_burst_image_stale_seconds",
                "gauge",
                "Age of the image behind the configured pin.",
                [({}, int(max(0.0, now - data["pin_image_created_at"])))],
            )
        emit(
            "ci_burst_image_leaked_resources_total",
            "counter",
            "Leaked resources that reconcile found and removed, by kind.",
            [({"kind": kind}, data["leaked"][kind]) for kind in LEAK_KINDS],
        )
        emit(
            "ci_burst_image_last_run_timestamp_seconds",
            "gauge",
            "Unix time of the last completed image pass.",
            [({}, int(now))],
        )
        return "\n".join(lines) + "\n"


# -- the pipeline --------------------------------------------------------------


class ImagePipeline:
    """Plan, run and supervise the automated warm-image build of one deployment.

    ``config`` is the loaded ``Config`` and ``config_path`` the file it came from
    (the deletion rule reloads it to see the pins). ``aws_factory()`` returns a
    FRESH ``ImageAws`` and is called once per pass and once per ``plan``: the
    presigned archive URL dies with the role session, so a session is never
    reused across passes. ``source_factory()`` returns a ``ForgejoSource``.
    ``state`` is an ``ImageState``. ``clock`` and ``sleep`` are injectable, and
    ``script`` is the bytes of the warm script (the shipped one by default; its
    absence is ``IMAGE_SCRIPT_MISSING``).

    With ``[image]`` absent or disabled every entry point returns the decision
    ``disabled`` and calls neither factory.

    Refusals that mean "fix the configuration or the forge" (``IMAGE_GATE_REQUIRED``,
    ``IMAGE_SOURCE_FORBIDDEN``, ``IMAGE_BRANCH_UNPROTECTED``,
    ``IMAGE_BASIS_PATH_MISSING``, ``IMAGE_BUILD_TEMPLATE_UNSAFE``,
    ``IMAGE_USERDATA_TOO_LARGE`` in ``plan``, ``IMAGE_STATE_LOCKED`` and every
    provider read that fails) are raised. The answers of the trigger rules are
    returned as the decision ``refused`` with a ``reason``. A build that ends
    badly is returned as the decision ``failed`` with the reason code of the
    failure. A result holds ids, 12-character digests, counts and codes only.
    """

    def __init__(
        self,
        config,
        config_path,
        *,
        aws_factory,
        source_factory,
        state,
        clock=time.time,
        sleep=time.sleep,
        script=None,
    ):
        self._config = config
        self._image = config.image
        self._config_path = Path(config_path)
        self._aws_factory = aws_factory
        self._source_factory = source_factory
        self._state = state
        self._clock = clock
        self._sleep = sleep
        self._script = script

    # -- shared inputs ---------------------------------------------------------
    def _inputs(self):
        """The checks that need no network, then the warm script's bytes."""
        warm = self._image.warm
        if not warm.gate_command and not warm.allow_no_gate:
            raise ImageRefusal(
                IMAGE_GATE_REQUIRED, "image.warm.gate_command is empty and image.warm.allow_no_gate is false"
            )
        if self._script is None:
            self._script = image.shipped_script()
        return self._script

    def _read_basis(self, source):
        """The sha of the protected branch head and the basis files at that sha."""
        settings = self._image.source
        head = require_protected(source.branch(settings.branch))
        files = {path: source.raw_file(head.sha, path) for path in settings.basis_paths}
        return head.sha, files

    def _digest(self, files, script):
        warm = self._image.warm
        return image.basis_digest(files, warm.commands, warm.gate_command, script)

    def _pin_facts(self, aws, deadline):
        """The image behind the configured pin and that image's ``ci-burst:basis-digest`` tag."""
        name, version = self._config.aws.launch_template_name, self._config.aws.launch_template_version
        read = aws.template_versions(name, [version], deadline=deadline)
        if len(read) != 1 or read[0].version != version:
            raise ProviderRefusal(
                "AWS_INVENTORY_INCOMPLETE", f"the read did not return exactly version {version}", uncertain=True
            )
        image_id = read[0].data.get("ImageId")
        if not isinstance(image_id, str) or not image_id.startswith("ami-"):
            return _Pin(version, None, None, None)
        found = aws.image(image_id)
        if found is None:
            return _Pin(version, image_id, None, None)
        return _Pin(version, image_id, found.tags.get(BASIS_DIGEST_TAG), found.created_at)

    def _check_build_template(self, aws, deadline):
        """Read the build launch template version and refuse unless it is safe.

        The detail of a refusal names the failing properties, never a value.
        """
        build = self._image.build
        found = aws.launch_templates([build.launch_template_name], deadline=deadline)
        if not any(item.name == build.launch_template_name for item in found):
            raise ImageRefusal(IMAGE_BUILD_TEMPLATE_UNSAFE, "the build launch template was not found")
        read = aws.template_versions(build.launch_template_name, [build.launch_template_version], deadline=deadline)
        if len(read) != 1 or read[0].version != build.launch_template_version:
            raise ImageRefusal(IMAGE_BUILD_TEMPLATE_UNSAFE, "the build launch template version was not found")
        data = read[0].data
        problems = _template_problems(data)
        if problems:
            raise ImageRefusal(IMAGE_BUILD_TEMPLATE_UNSAFE, "unsafe properties: " + ", ".join(problems))
        return {
            "name": build.launch_template_name,
            "version": build.launch_template_version,
            "image_id": data["ImageId"],
            "volume_gib": _volume_gib(data),
        }

    def _decision(self, facts, digest, pin, now, force, active):
        last = facts.last_build
        failed = facts.last_failed
        return image.build_decision(
            digest=digest,
            pin_digest=pin.digest,
            now=now,
            last_build_started_at=last.created_at if last is not None else None,
            builds_last_day=facts.builds_last_day,
            control=facts.control,
            active=active,
            config=self._image,
            force=force,
            last_failed_digest=failed.basis_digest if failed is not None else None,
        )

    # -- plan --------------------------------------------------------------------
    def plan(self, *, force=False):
        """Say whether a build would start, and what a pass would delete. Writes nothing.

        The checks run in an order that keeps the promises of the design: the
        gate and the script (no network), then the forge (a 401 or 403 is
        ``IMAGE_SOURCE_FORBIDDEN`` before any provider call), then the provider.
        The state file is opened for reading only, and a missing file answers as a
        new one. ``force`` shows what a forced run would decide.
        """
        if not self._image.enabled:
            return {"decision": DISABLED}
        script = self._inputs()
        now = self._clock()
        deadline = now + PASS_DEADLINE_SECONDS
        settings = self._image.source
        source = self._source_factory()
        sha, files = self._read_basis(source)
        self._probe_archive(source, sha, deadline)
        digest = self._digest(files, script)
        aws = self._aws_factory()
        pin = self._pin_facts(aws, deadline)
        template = self._check_build_template(aws, deadline)
        size = self._user_data_bytes(script)
        builders = aws.builders(self._config.deployment, deadline=deadline)
        opened = not self._state.available
        present = self._state.connect_readonly()
        try:
            facts = _read_facts(self._state, self._config.deployment, now)
            decision = self._decision(facts, digest, pin, now, force, facts.active is not None or bool(builders))
            deletion = self._deletion_plan(aws, facts, now, deadline)
        finally:
            if opened:
                self._state.close()
        result = {
            "decision": decision["decision"],
            "reason": decision["reason"],
            "forced": force,
            "clear_control": decision["clear_control"],
            "source": {"branch": settings.branch, "sha": _hex12(sha), "protected": True, "basis_files": len(files)},
            "basis_digest": _hex12(digest),
            "pin": _pin_summary(pin),
            "script_sha256": image.script_sha256(script),
            "user_data": {"bytes": size, "limit": image.USER_DATA_LIMIT},
            "gate": {
                "configured": bool(self._image.warm.gate_command),
                "allow_no_gate": self._image.warm.allow_no_gate,
            },
            "build_template": template,
            "builders": [builder.instance_id for builder in builders],
            "state": {
                "present": present,
                "active_build": _summary(facts.active),
                "candidate": _summary(facts.candidate),
                "control": _control_summary(facts.control),
            },
            "deletion": deletion,
        }
        return result

    def _probe_archive(self, source, sha, deadline):
        """Read the first chunk of the archive: the plan tests the archive endpoint too."""
        chunks = source.archive(sha, self._image.source.max_archive_bytes, deadline=deadline)
        try:
            next(iter(chunks), None)
        finally:
            close = getattr(chunks, "close", None)
            if close is not None:
                close()

    def _user_data_bytes(self, script):
        """The size of the user data of a build, with a URL as long as a real presigned one."""
        url = "https://" + "x" * (SYNTHETIC_URL_CHARS - len("https://"))
        text = image.render_user_data(script, image.warm_exports(self._image, url, "0" * 64))
        return len(text.encode("utf-8"))

    def _deletion_plan(self, aws, facts, now, deadline):
        """What the deletion rule would remove now, as a dry run: nothing is called."""
        try:
            protected = image_deletion.protected_set(
                aws,
                self._config_path,
                previous_pin=facts.previous_pin,
                pending_journal=facts.pending,
                candidate=facts.candidate,
                active=facts.active,
                deadline=deadline,
            )
            deployment = self._config.deployment
            plan = image_deletion.plan_deletions(
                aws.images(deployment, deadline=deadline),
                aws.snapshots(deployment, deadline=deadline),
                protected,
                deployment=deployment,
                mode="all",
                now=now,
                config=self._image,
                builds=facts.builds,
            )
        except Refusal as refusal:
            return {"refused": refusal.reason}
        report = image_deletion.apply_deletions(aws, plan, apply=False)
        return {"items": report}

    # -- run ---------------------------------------------------------------------
    def run_pass(self, *, apply, force=False):
        """Run one bounded pass. Without ``apply`` this is ``plan``.

        With ``apply`` the pass takes the state file's writer lock (a pass that
        is already running gives ``IMAGE_STATE_LOCKED``) and releases it at the
        end. A state that the caller opened stays open.
        """
        if not self._image.enabled:
            return {"decision": DISABLED}
        if not apply:
            return self.plan(force=force)
        script = self._inputs()
        opened = not self._state.available
        self._state.connect()
        try:
            self._state.acquire_writer()
            return _Pass(self, script, force).run()
        finally:
            self._state.release_writer()
            if opened:
                self._state.close()

    def run_until_done(self, *, force=False):
        """Repeat passes, with a fixed sleep between them, until the build is over.

        It applies: there is no dry form. ``force`` counts for the first pass
        only, because the passes after it resume the build that pass started.
        Each pass builds its own provider session. A build that is ``candidate``
        or ``failed``, or a pass with nothing in flight (a refusal, an unchanged
        digest), ends the loop with that pass's result. A pass that raises ends
        the loop too, and rerunning resumes the same build from the state file.
        The loop ends because every state in flight fails at the build deadline.
        """
        if not self._image.enabled:
            return {"decision": DISABLED}
        first = True
        while True:
            result = self.run_pass(apply=True, force=force and first)
            first = False
            if result["decision"] not in IN_PROGRESS:
                return result
            self._sleep(SUPERVISOR_SLEEP_SECONDS)


# -- one pass ------------------------------------------------------------------


class _Pass:
    """One apply pass. The state file's writer lock is held for its whole life."""

    def __init__(self, pipeline, script, force):
        self.p = pipeline
        self.s = pipeline._state
        self.cfg = pipeline._image
        self.dep = pipeline._config.deployment
        self.script = script
        self.force = force
        self.clock = pipeline._clock
        self.sleep = pipeline._sleep
        self.started_at = self.clock()
        self.deadline = self.started_at + PASS_DEADLINE_SECONDS
        self.poll_until = self.deadline - CLEANUP_RESERVE_SECONDS
        self.tele = _Telemetry(self.s.directory, self.dep, self.clock)
        self.aws = None
        self.source = None
        self.notes = []
        self.terminated_foreign = 0
        #: True after a launch whose outcome is unknown: the build waits for the next pass.
        self.hold = False
        self.check = {}

    def run(self):
        try:
            result = self._run()
        except BaseException:
            # The counters and the metrics also describe a pass that raised. A
            # failure of their own must not hide the failure that ended the pass.
            try:
                self._publish(checked=False)
            except Exception:  # noqa: BLE001 - the original error is re-raised below
                pass
            raise
        self._publish(checked=True)
        return result

    def _publish(self, *, checked):
        """Write the check time (after a pass that finished), the counters and the metrics."""
        now = self.clock()
        if checked:
            self.s.control_set(last_check_at=now)
        self.tele.save()
        self.tele.write_metrics(self.s, now)

    def _run(self):
        self.aws = self.p._aws_factory()
        reconcile = self._reconcile()
        pin = self.p._pin_facts(self.aws, self.deadline)
        if pin.image_id is not None:
            self.s.pin_observe(pin.version, pin.image_id)
        self.tele.data["pin_image_created_at"] = pin.created_at
        build = self.s.active_build(self.dep)
        verdict = None
        if build is None:
            verdict, build = self._decide(pin)
        if build is not None and build.state in ACTIVE_STATES and not self.hold:
            build = self._advance(build)
        retention = self._retention()
        if build is not None:
            decision = build.state
            reason = build.reason_code or None if build.state == "failed" else None
        else:
            decision, reason = verdict["decision"], verdict["reason"]
        self._check_event(decision, reason, build, pin)
        return {
            "decision": decision,
            "reason": reason,
            "forced": self.force,
            "build": _summary(build),
            "pin": _pin_summary(pin),
            "reconcile": reconcile,
            "retention": retention,
            "control": _control_summary(self.s.control_get()),
            "notes": self.notes,
        }

    def _check_event(self, decision, reason, build, pin):
        self.tele.event(
            "image_check",
            decision=decision,
            reason=reason,
            forced=self.force,
            build_id=build.id if build is not None else None,
            pin_version=pin.version,
            pin_digest=_hex12(pin.digest),
            **self.check,
        )

    # -- reconcile ---------------------------------------------------------------
    def _reconcile(self):
        """Clean up first, before any transition. Nothing here depends on the forge."""
        s, aws, dep = self.s, self.aws, self.dep
        now = self.clock()
        summary = {
            "builders_terminated": 0,
            "archives_deleted": 0,
            "images_recorded": 0,
            "builder_adopted": False,
            "image_adopted": False,
            "failures": [],
        }
        active = s.active_build(dep)
        # Builders. The state machine owns the active build's builder, its
        # deadline included. Every other builder is terminated. A builder tagged
        # with the active build's id that the row does not know yet is the one a
        # crash between ``RunInstances`` and the state write left behind: adopt it.
        foreign = []
        for builder in aws.builders(dep, deadline=self.deadline):
            owned = (
                active is not None
                and builder.tags.get(BUILD_ID_TAG) == active.id
                and active.builder_id in (None, builder.instance_id)
            )
            if not owned:
                foreign.append(builder.instance_id)
            elif active.builder_id is None:
                active = s.advance_build(active.id, builder_id=builder.instance_id)
                summary["builder_adopted"] = True
        if foreign:
            aws.terminate(foreign)
            summary["builders_terminated"] = len(foreign)
            self.terminated_foreign = len(foreign)
            self.tele.leak("builder", len(foreign))
        # Stray archives: not the active archive, and older than an hour.
        storage = self.cfg.storage
        keep = {active.archive_key} if active is not None and active.archive_key else set()
        for stored in aws.list_objects(storage.bucket, storage.prefix, deadline=self.deadline):
            if stored.key in keep or stored.last_modified is None:
                continue
            if now - stored.last_modified <= STRAY_ARCHIVE_AGE_SECONDS:
                continue
            try:
                aws.delete_object(storage.bucket, stored.key)
            except ProviderRefusal as refusal:
                summary["failures"].append(refusal.reason)
            else:
                summary["archives_deleted"] += 1
        self.tele.leak("archive", summary["archives_deleted"])
        # Images. One that carries the active build's id and that the row does
        # not know yet was created by a crash after ``CreateImage``: adopt it.
        # An available image with no row at all is recorded and never pinned.
        rows = {build.id for build in s.builds(dep)}
        for found in aws.images(dep, deadline=self.deadline):
            tags = found.tags
            build_id = tags.get(BUILD_ID_TAG)
            if tags.get(IMAGE_BUILD_TAG) != "true" or tags.get(DEPLOYMENT_TAG) != dep or not build_id:
                continue
            if active is not None and build_id == active.id:
                if active.state == "building" and active.image_id is None:
                    active = s.advance_build(active.id, image_id=found.image_id, state="imaging")
                    summary["image_adopted"] = True
                continue
            if build_id in rows or found.state != "available":
                continue
            s.insert_orphan(
                build_id=build_id,
                deployment=dep,
                basis_digest=tags.get(BASIS_DIGEST_TAG) or "",
                source_sha=tags.get(SOURCE_SHA_TAG) or "",
                image_id=found.image_id,
                created_at=found.created_at if found.created_at is not None else 0.0,
                reason_code=IMAGE_ORPHAN_RECORDED,
            )
            rows.add(build_id)
            summary["images_recorded"] += 1
        if (
            summary["builders_terminated"]
            or summary["archives_deleted"]
            or summary["images_recorded"]
            or summary["builder_adopted"]
            or summary["image_adopted"]
        ):
            self.tele.event(
                "image_reconciled",
                builders_terminated=summary["builders_terminated"],
                archives_deleted=summary["archives_deleted"],
                images_recorded=summary["images_recorded"],
                builder_adopted=summary["builder_adopted"],
                image_adopted=summary["image_adopted"],
                build_id=active.id if active is not None else None,
            )
        return summary

    # -- decide and start --------------------------------------------------------
    def _source(self):
        if self.source is None:
            self.source = self.p._source_factory()
        return self.source

    def _decide(self, pin):
        """Read the forge, apply the trigger rules and, when a build is due, start it."""
        now = self.clock()
        sha, files = self.p._read_basis(self._source())
        digest = self.p._digest(files, self.script)
        facts = _read_facts(self.s, self.dep, now)
        # A builder that reconcile just terminated still counts as active for this
        # pass: the next pass starts clean, and a lost state file cannot start a
        # build on top of the one it forgot.
        decision = self.p._decision(facts, digest, pin, now, self.force, self.terminated_foreign > 0)
        self.check = {"digest": _hex12(digest), "source_sha": _hex12(sha)}
        if decision["clear_control"]:
            self.s.control_set(blocked=False, cooldown_until=None)
        if not decision["start"]:
            return decision, None
        return decision, self._start(sha, digest)

    def _start(self, sha, digest):
        """The start sequence. The row is written before any provider call that costs something."""
        s, aws, cfg = self.s, self.aws, self.cfg
        self.p._check_build_template(aws, self.deadline)
        now = self.clock()
        build_id, token = str(uuid.uuid4()), str(uuid.uuid4())
        build = s.insert_build(
            build_id=build_id,
            deployment=self.dep,
            client_token=token,
            basis_digest=digest,
            source_sha=sha,
            deadline_at=image.build_deadline(now, cfg),
            script_sha256=image.script_sha256(self.script),
            gate_configured=int(bool(cfg.warm.gate_command)),
        )
        try:
            return self._stage_and_launch(build)
        except Refusal as refusal:
            # Nothing was launched: a launch whose outcome is unknown is handled
            # inside ``_stage_and_launch`` and does not come here.
            return self._fail(build, refusal.reason, builder_alive=False)

    def _stage_and_launch(self, build):
        s, aws, cfg = self.s, self.aws, self.cfg
        key = f"{cfg.storage.prefix}{build.id}.tar.gz"
        chunks = self._source().archive(build.source_sha, cfg.source.max_archive_bytes, deadline=self.deadline)
        try:
            upload = aws.put_archive(
                cfg.storage.bucket, key, chunks, max_bytes=cfg.source.max_archive_bytes, deadline=self.deadline
            )
        finally:
            close = getattr(chunks, "close", None)
            if close is not None:
                close()
        build = s.advance_build(build.id, archive_key=upload.key, archive_sha256=upload.sha256)
        instance_id = self._launch(build, upload)
        if instance_id is None:
            self.notes.append("run_builder:uncertain")
            self.hold = True
            return build
        build = s.advance_build(build.id, builder_id=instance_id)
        self.tele.event(
            "image_build_started",
            build_id=build.id,
            builder_id=instance_id,
            digest=_hex12(build.basis_digest),
            source_sha=_hex12(build.source_sha),
            forced=self.force,
            deadline_at=build.deadline_at,
        )
        return build

    def _launch(self, build, upload):
        """Presign, render, check the tags and launch. The URL and the user data live only here.

        The result is the builder's id, or ``None`` when ``RunInstances`` ended
        with an unknown outcome: the builder may exist, and the next pass adopts
        it by its build-id tag or fails the build.
        """
        aws, cfg = self.aws, self.cfg
        url = aws.presign_get(cfg.storage.bucket, upload.key, cfg.source.presign_seconds)
        user_data = image.render_user_data(self.script, image.warm_exports(cfg, url, upload.sha256))
        if aws.builders(self.dep, deadline=self.deadline):
            raise ImageRefusal(IMAGE_BUILD_ALREADY_ACTIVE)
        try:
            return aws.run_builder(
                template_name=cfg.build.launch_template_name,
                template_version=cfg.build.launch_template_version,
                deployment=self.dep,
                build_id=build.id,
                expires_at=build.deadline_at,
                user_data=user_data,
                client_token=build.client_token,
            )
        except ProviderRefusal as refusal:
            if refusal.uncertain:
                return None
            raise

    # -- advance -----------------------------------------------------------------
    def _advance(self, build):
        try:
            if build.state == "building":
                build = self._building(build)
            if build.state == "imaging":
                build = self._imaging(build)
        except DeadlineRefusal:
            # The pass ran out of time inside a read. Nothing is lost: the next
            # pass resumes from whatever the row says.
            self.notes.append("pass_deadline_exceeded")
            build = self.s.build(build.id)
        return build

    def _wait(self):
        """Sleep one poll interval if the pass can still use it. False means the pass must end."""
        if self.clock() + CONSOLE_POLL_SECONDS > self.poll_until:
            return False
        self.sleep(CONSOLE_POLL_SECONDS)
        return True

    def _building(self, build):
        if build.builder_id is None or build.archive_sha256 is None:
            return self._fail(build, IMAGE_BUILDER_GONE, builder_alive=False)
        while True:
            now = self.clock()
            found = self.aws.instances([build.builder_id], deadline=self.deadline)
            alive = any(item.instance_id == build.builder_id and item.state in BUILDER_STATES for item in found)
            if not alive and now - build.updated_at < BUILDER_VISIBILITY_GRACE_SECONDS:
                alive = True  # a builder that was just recorded may not be listed yet
            console = self.aws.console_output([build.builder_id], deadline=self.deadline).get(build.builder_id)
            marker = image_console.read_marker(console, build.archive_sha256)
            outcome = marker["outcome"]
            if outcome == image_console.READY:
                self.tele.data["console_latency_seconds"] = max(0.0, now - build.created_at)
                self.tele.event(
                    "image_marker_seen",
                    build_id=build.id,
                    builder_id=build.builder_id,
                    latency_seconds=max(0.0, now - build.created_at),
                )
                if not alive:
                    return self._fail(build, IMAGE_BUILDER_GONE, console, builder_alive=False)
                return self._create_image(build, console)
            if outcome in (image_console.FAILED, image_console.MISMATCH):
                return self._fail(build, marker["reason"], console)
            if not alive:
                return self._fail(build, IMAGE_BUILDER_GONE, console, builder_alive=False)
            if now >= build.deadline_at:
                return self._fail(build, image_console.deadline_reason(outcome), console)
            if not self._wait():
                return build

    def _create_image(self, build, console):
        """``CreateImage`` in the pass that first sees the marker, before any other step."""
        tags = {
            SOURCE_SHA_TAG: build.source_sha,
            SCRIPT_SHA_TAG: build.script_sha256 or image.script_sha256(self.script),
        }
        base_image = self._base_image()
        if base_image is not None:
            tags[BASE_IMAGE_TAG] = base_image
        try:
            image_id = self.aws.create_image(
                build.builder_id,
                f"ci-burst-{_safe_name(self.dep)}-{build.id}",
                deployment=self.dep,
                build_id=build.id,
                basis_digest=build.basis_digest,
                extra_tags=tags,
            )
        except ProviderRefusal as refusal:
            # An unknown outcome may have created the image: the next pass adopts
            # it by its tags and does not ask again. A definite refusal, or an
            # unknown one that outlived the deadline, is the build's verdict.
            if refusal.uncertain and self.clock() < build.deadline_at:
                self.notes.append(f"create_image:{refusal.reason}")
                return build
            return self._fail(build, refusal.reason, console)
        build = self.s.advance_build(build.id, image_id=image_id, state="imaging")
        self.tele.event("image_created", build_id=build.id, builder_id=build.builder_id, image_id=image_id)
        return build

    def _base_image(self):
        """The cold base image of the builder: the image its (immutable) template version names."""
        settings = self.cfg.build
        try:
            read = self.aws.template_versions(
                settings.launch_template_name, [settings.launch_template_version], deadline=self.deadline
            )
        except Refusal:
            return None
        image_id = read[0].data.get("ImageId") if len(read) == 1 else None
        return image_id if isinstance(image_id, str) and image_id.startswith("ami-") else None

    def _imaging(self, build):
        while True:
            now = self.clock()
            found = self.aws.image(build.image_id)
            name = found.state if found is not None else None
            if name == "available":
                return self._complete(build)
            if name in _IMAGE_FAILED_STATES:
                return self._fail(build, IMAGE_NOT_AVAILABLE)
            if now >= build.deadline_at:
                return self._fail(build, IMAGE_NOT_AVAILABLE)
            if not self._wait():
                return build

    def _complete(self, build):
        """The image is available: end the builder, then create and check the candidate version."""
        if build.builder_id is not None:
            found = self.aws.instances([build.builder_id], deadline=self.deadline)
            if any(item.instance_id == build.builder_id and item.state in BUILDER_STATES for item in found):
                self.aws.terminate([build.builder_id])
        try:
            outcome = create_candidate_version(
                self.aws,
                template_name=self.p._config.aws.launch_template_name,
                source_pin=self.p._config.aws.launch_template_version,
                image_id=build.image_id,
                build_id=build.id,
                basis_digest=build.basis_digest,
                client_token=f"{build.client_token}-v",
            )
        except ProviderRefusal as refusal:
            # The version may exist already. The next pass asks again with the
            # same token and gets the same version, so no rebuild is needed.
            if self.clock() >= build.deadline_at:
                return self._fail(build, refusal.reason, builder_alive=False)
            self.notes.append(f"create_version:{refusal.reason}")
            return build
        if outcome["drift"] == TEMPLATE_DRIFT:
            return self._fail(
                build,
                TEMPLATE_DRIFT,
                builder_alive=False,
                drift_fields=",".join(outcome["fields"]),
                template_version=outcome["version"],
                drift_result=TEMPLATE_DRIFT,
            )
        return self._succeed(build, outcome["version"])

    # -- terminal states -----------------------------------------------------------
    def _succeed(self, build, version):
        s, now = self.s, self.clock()
        gate = int(bool(self.cfg.warm.gate_command))
        with s.transaction():
            build = s.advance_build(
                build.id,
                state="candidate",
                template_version=version,
                drift_result=CLEAN,
                template_version_exists=1,
                image_state="available",
                script_sha256=build.script_sha256 or image.script_sha256(self.script),
                gate_configured=gate,
                gate_passed=gate,
                verified_at=now,
                source_pin=self.p._config.aws.launch_template_version,
            )
            self._write_control(image.record_success(s.control_get()))
        self._delete_archive(build)
        self.tele.outcome("candidate", build, now)
        self.tele.event(
            "image_candidate_ready",
            build_id=build.id,
            image_id=build.image_id,
            template_version=version,
            digest=_hex12(build.basis_digest),
            duration_seconds=max(0.0, now - build.created_at),
        )
        return build

    def _fail(self, build, reason, console=None, *, builder_alive=True, drift_fields=None, **fields):
        """End a build as failed: stop its builder, record the verdict and the failure, drop the archive.

        ``fields`` are columns of the build row. ``drift_fields`` names the
        template fields that drifted and goes into the event only.
        """
        s, now = self.s, self.clock()
        if builder_alive and build.builder_id is not None:
            try:
                self.aws.terminate([build.builder_id])
            except ProviderRefusal:
                # The build is over either way. The builder is no longer the
                # active build's, so the next pass's reconcile terminates it.
                self.notes.append("terminate:failed")
        with s.transaction():
            build = s.advance_build(build.id, state="failed", reason_code=reason, **fields)
            recorded = image.record_failure(s.control_get(), now, self.cfg)
            self._write_control(recorded["control"])
        self._delete_archive(build)
        self.tele.outcome("failed", build, now)
        self.tele.event(
            "image_build_failed",
            build_id=build.id,
            reason=reason,
            image_id=build.image_id,
            digest=_hex12(build.basis_digest),
            duration_seconds=max(0.0, now - build.created_at),
            drift_fields=drift_fields,
            framed_tail=image_console.framed_tail(console),
        )
        if recorded["newly_blocked"]:
            self.tele.event(
                "image_blocked", consecutive_failures=recorded["control"].consecutive_failures, build_id=build.id
            )
        return build

    def _write_control(self, control):
        self.s.control_set(
            consecutive_failures=control.consecutive_failures,
            cooldown_until=control.cooldown_until,
            blocked=control.blocked,
        )

    def _delete_archive(self, build):
        """Delete the archive at a terminal state. A failure here is a stray object, not a failed build."""
        if not build.archive_key:
            return
        try:
            self.aws.delete_object(self.cfg.storage.bucket, build.archive_key)
        except ProviderRefusal:
            self.notes.append("archive:delete_failed")

    # -- retention -----------------------------------------------------------------
    def _retention(self):
        """The one deletion rule, at the end of the pass. A refusal deletes nothing and is reported."""
        facts = _read_facts(self.s, self.dep, self.clock())
        try:
            protected = image_deletion.protected_set(
                self.aws,
                self.p._config_path,
                previous_pin=facts.previous_pin,
                pending_journal=facts.pending,
                candidate=facts.candidate,
                active=facts.active,
                deadline=self.deadline,
            )
            plan = image_deletion.plan_deletions(
                self.aws.images(self.dep, deadline=self.deadline),
                self.aws.snapshots(self.dep, deadline=self.deadline),
                protected,
                deployment=self.dep,
                mode="all",
                now=self.clock(),
                config=self.cfg,
                builds=facts.builds,
            )
        except Refusal as refusal:
            return {"refused": refusal.reason}
        report = image_deletion.apply_deletions(self.aws, plan, apply=True)
        counts, leaked = _deletion_counts(report)
        for kind, count in leaked.items():
            self.tele.leak(kind, count)
        if counts["deregistered"] or counts["snapshots_deleted"] or counts["failed"]:
            self.tele.event(
                "image_pruned",
                deregistered=counts["deregistered"],
                snapshots_deleted=counts["snapshots_deleted"],
                failed=counts["failed"],
                skipped=counts["skipped"],
            )
        return counts
