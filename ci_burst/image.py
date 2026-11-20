"""The pure inputs of an automated warm-image build.

Three questions are answered here without a provider call, a forge call or a
state write: what a build is made of (``basis_digest``), whether one may start
(``build_decision``) and what the builder is told to run (``render_user_data``).
The callers read the facts and write the consequences, so every rule is
testable with plain values.

Nothing here logs. A refusal names the rule that failed, never a value: the
presigned archive URL and the warm commands reach this module as arguments and
leave it only inside the user data it returns.
"""

import base64
import gzip
import hashlib
import re
import shlex
from dataclasses import replace
from pathlib import Path

from .config import _no_control
from .errors import ConfigRefusal, ImageRefusal

#: Where the container build puts ``deploy/runner/image/warm-cache.sh``. The
#: Dockerfile ``COPY`` and this path are pinned together by tests/test_image.py.
DEFAULT_SCRIPT_PATH = Path("/srv/ci-burst/share/warm-cache.sh")

#: EC2 refuses user data of this many raw bytes or more (measured before the
#: SDK encodes it as base64).
USER_DATA_LIMIT = 16_384

_EXPORT_NAME = re.compile(r"WARM_[A-Z0-9_]+")

#: The script reads this export one command per line, so a line feed separates
#: its commands. It is the one control character an export may hold, and each
#: line is still checked on its own.
_LINE_SEPARATED = frozenset({"WARM_COMMANDS"})

#: The part of the user data after the exports. ``warm-cache.sh`` arms its own
#: lifetime timer first, so a stub that cannot hand over would leave a builder
#: with no timer: it powers the builder off itself instead. The payload is
#: base64, which never holds an underscore, so the here-document delimiter
#: cannot occur inside it.
_STUB_RUN = """\
set -o pipefail
stop() { echo "warm stub: $1" >&2; shutdown -h now; exit 1; }
d=$(mktemp -d /run/warm-stub.XXXXXX) || stop "no private directory"
trap 'rm -rf -- "$d"' EXIT
base64 -d <<'WARM_PAYLOAD_END' | gunzip >"$d/warm-cache.sh" || stop "the payload does not decode"
"""
_STUB_END = """\
WARM_PAYLOAD_END
bash "$d/warm-cache.sh"
exit $?
"""


def script_sha256(script_bytes):
    """The hex sha256 of the shipped script, as ``image plan`` shows it."""
    return hashlib.sha256(script_bytes).hexdigest()


def render_commands(commands, gate_command):
    """The ``WARM_COMMANDS`` and ``WARM_GATE_COMMAND`` values, as the builder reads them.

    This is the one encoding of the commands: the digest hashes these values
    and the user data exports them, so the two cannot disagree. The script
    reads one command per line. A command with a control character, a line
    feed included, is refused, so no command can split in two or hide a third.
    """
    if isinstance(commands, str) or not commands:
        raise ConfigRefusal("CONFIG_INVALID", "warm commands: must be a non-empty list of commands")
    for index, command in enumerate(commands):
        where = f"warm commands[{index}]"
        if not isinstance(command, str):
            raise ConfigRefusal("CONFIG_INVALID", f"{where}: must be a string")
        _no_control(command, where)
    if not isinstance(gate_command, str):
        raise ConfigRefusal("CONFIG_INVALID", "warm gate command: must be a string")
    _no_control(gate_command, "warm gate command")
    return {"WARM_COMMANDS": "\n".join(commands), "WARM_GATE_COMMAND": gate_command}


def basis_digest(files, warm_commands, gate_command, script_bytes):
    """The hex digest that says whether a build is due.

    ``files`` maps each basis path to the bytes read at the resolved sha. The
    digest covers the sorted lines ``path NUL sha256(content) LF``, then the
    hash of the rendered commands (``render_commands``, commands and gate, one
    value separated by a NUL) and the hash of the shipped script, each followed
    by a line feed. It never covers the archive hash: the same sha can produce
    different archive bytes, and the digest must not change with them.
    """
    entries = []
    for path, content in files.items():
        # A line feed or NUL in a path would let two file sets share one digest.
        _no_control(path, "basis path")
        entries.append((path.encode("utf-8"), hashlib.sha256(content).hexdigest().encode("ascii")))
    values = render_commands(warm_commands, gate_command)
    rendered = (values["WARM_COMMANDS"] + "\0" + values["WARM_GATE_COMMAND"]).encode("utf-8")
    digest = hashlib.sha256()
    for path, content_hash in sorted(entries):
        digest.update(path + b"\0" + content_hash + b"\n")
    digest.update(hashlib.sha256(rendered).hexdigest().encode("ascii") + b"\n")
    digest.update(script_sha256(script_bytes).encode("ascii") + b"\n")
    return digest.hexdigest()


def _result(start, decision, reason, clear):
    return {"start": start, "decision": decision, "reason": reason, "clear_control": clear}


def build_decision(
    *,
    digest,
    pin_digest,
    now,
    last_build_started_at,
    builds_last_day,
    control,
    active,
    config,
    force=False,
    last_failed_digest=None,
):
    """Say whether a build may start and, if not, which condition stopped it.

    ``digest`` is the current ``basis_digest``. ``pin_digest`` is the
    ``ci-burst:basis-digest`` tag of the image behind the configured pin, or
    ``None`` when that image has no such tag. ``now`` and
    ``last_build_started_at`` (``None`` before the first build) are epoch
    seconds. ``builds_last_day`` counts builds started in the last 24 hours.
    ``control`` is the state file's control row. ``active`` is true while a
    builder or an active build exists. ``config`` is the enabled
    ``ImageConfig``. ``last_failed_digest`` is the basis digest of the last
    failed build (``Control`` has no such field), or ``None``.

    Clearing comes first and does not depend on the outcome. ``force`` clears
    ``blocked`` and any recorded failure cooldown, an expired one included. A basis digest that differs from
    ``last_failed_digest`` clears ``blocked`` and, with it, the cooldown; with
    no failed digest to compare, ``blocked`` stays. A digest change alone does
    not end a cooldown of a row that is not blocked.

    Then the first failing condition wins, in the spec's order:

    1. the digest equals ``pin_digest``: decision ``digest_unchanged``, which
       is an answer and not a refusal
    2. ``IMAGE_MIN_INTERVAL``
    3. ``IMAGE_DAILY_CAP``
    4. ``IMAGE_FAILURE_COOLDOWN``
    5. ``IMAGE_BLOCKED``
    6. ``IMAGE_BUILD_ALREADY_ACTIVE``

    ``force`` skips only conditions 1 and 2. It never skips the daily cap or
    an active builder.

    The result is ``{"start", "decision", "reason", "clear_control"}``. The
    decision is ``start``, ``digest_unchanged`` or ``refused``, and ``reason``
    is the code of a refusal, else ``None``. ``clear_control`` is true in every
    outcome in which a clearing applied: the caller then clears ``blocked``
    and the cooldown (``blocked`` false, ``cooldown_until`` none) when it
    persists the pass, even if no build starts. ``plan`` and ``run`` without
    ``--apply`` persist nothing. This function writes no state.
    """
    cooling = control.cooldown_until is not None and now < control.cooldown_until
    recovered = control.blocked and last_failed_digest is not None and digest != last_failed_digest
    # --force clears any recorded cooldown, an expired one too, so no stale timestamp stays behind.
    clear = (force and (control.blocked or control.cooldown_until is not None)) or recovered
    blocked = control.blocked and not clear
    cooling = cooling and not clear
    if not force:
        if pin_digest is not None and digest == pin_digest:
            return _result(False, "digest_unchanged", None, clear)
        if last_build_started_at is not None and now - last_build_started_at < config.min_build_interval_seconds:
            return _result(False, "refused", "IMAGE_MIN_INTERVAL", clear)
    if builds_last_day >= config.max_builds_per_day:
        return _result(False, "refused", "IMAGE_DAILY_CAP", clear)
    if cooling:
        return _result(False, "refused", "IMAGE_FAILURE_COOLDOWN", clear)
    if blocked:
        return _result(False, "refused", "IMAGE_BLOCKED", clear)
    if active:
        return _result(False, "refused", "IMAGE_BUILD_ALREADY_ACTIVE", clear)
    return _result(True, "start", None, clear)


def warm_exports(config, source_url, archive_sha256):
    """The ``WARM_*`` inputs of one build, from configuration and two build facts.

    ``config`` is the enabled ``ImageConfig``. ``source_url`` is the presigned
    archive URL and ``archive_sha256`` the hash of the staged archive. The
    names and encodings are the ones ``warm-cache.sh`` reads.
    """
    exports = render_commands(config.warm.commands, config.warm.gate_command)
    exports["WARM_SRC_URL"] = source_url
    exports["WARM_SRC_SHA256"] = archive_sha256
    exports["WARM_LIFETIME_SECONDS"] = str(config.warm.lifetime_seconds)
    exports["WARM_SNAPSHOT_SECONDS"] = str(config.warm.snapshot_seconds)
    exports["WARM_SRC_STRIP_COMPONENTS"] = str(config.source.strip_components)
    return exports


def _check_export(name, value):
    if not isinstance(name, str) or not _EXPORT_NAME.fullmatch(name):
        raise ConfigRefusal("CONFIG_INVALID", "a user data export name must match WARM_[A-Z0-9_]+")
    where = f"user data export {name}"
    if not isinstance(value, str):
        raise ConfigRefusal("CONFIG_INVALID", f"{where}: must be a string")
    for part in value.split("\n") if name in _LINE_SEPARATED else (value,):
        _no_control(part, where)


def render_user_data(script_bytes, exports):
    """Render the builder's user data: a short stub that carries the script.

    The stub exports each value quoted for the shell, in name order, then
    decodes a gzip and base64 payload of ``script_bytes`` into a private
    directory, runs it with ``bash`` and exits with its status. The output is
    a function of the arguments alone: the gzip header has no timestamp.

    ``exports`` maps ``WARM_*`` names to strings. A name outside
    ``WARM_[A-Z0-9_]+`` or a value with a control character is refused with
    ``CONFIG_INVALID``, and a refusal never holds a value. The result is
    refused with ``IMAGE_USERDATA_TOO_LARGE`` when its UTF-8 size, the size
    EC2 measures before base64, is ``USER_DATA_LIMIT`` or more.
    """
    for name, value in exports.items():
        _check_export(name, value)
    lines = ["#!/bin/bash"]
    for name in sorted(exports):
        lines.append(f"export {name}={shlex.quote(exports[name])}")
    payload = base64.b64encode(gzip.compress(script_bytes, compresslevel=9, mtime=0)).decode("ascii")
    text = "\n".join(lines) + "\n" + _STUB_RUN + payload + "\n" + _STUB_END
    size = len(text.encode("utf-8"))
    if size >= USER_DATA_LIMIT:
        raise ImageRefusal(
            "IMAGE_USERDATA_TOO_LARGE",
            f"the rendered user data is {size} bytes; EC2 refuses {USER_DATA_LIMIT} or more",
        )
    return text


def shipped_script(path=DEFAULT_SCRIPT_PATH):
    """The bytes of the warm script the container ships."""
    try:
        return Path(path).read_bytes()
    except OSError as error:
        raise ImageRefusal("IMAGE_SCRIPT_MISSING", f"cannot read {path}: {type(error).__name__}") from None


# -- failure bounds ----------------------------------------------------------
# What a build leaves behind in the control row when it fails or succeeds, and
# when a build must be over. These are pure like the rest of this module: they
# take the control row the caller read and return the one the caller writes.
# ``build_decision`` reads that row, and its ``clear_control`` says when the
# caller writes the clearing: ``blocked`` false and ``cooldown_until`` none, with
# ``consecutive_failures`` left alone, because the count ends only with a success.


def build_deadline(started_at, config):
    """The epoch second by which a build that started at ``started_at`` must be over.

    ``config`` is the enabled ``ImageConfig``. A build with no verdict at this
    second ends as ``failed``.
    """
    return started_at + config.build_deadline_seconds


def record_failure(control, now, config):
    """The control row after one more failed build.

    ``control`` is the state file's control row, ``now`` is epoch seconds and
    ``config`` is the enabled ``ImageConfig``. The result is
    ``{"control", "newly_blocked"}``. ``control`` is a new row of the same type
    with the failure count one higher, the cooldown ending
    ``failure_cooldown_seconds`` after ``now``, and ``blocked`` true once the
    count reaches ``max_consecutive_failures`` (a row that is already blocked
    stays blocked). ``newly_blocked`` is true only when this failure is the one
    that blocked the row, so the caller writes its ``image_blocked`` event once.

    The count does not restart when a recovery clears ``blocked`` (the design
    clears ``blocked`` and the cooldown and nothing else), so the first failure
    after a recovery blocks the row again: a recovered build has one try. Only
    ``record_success`` ends the count. The argument is not changed.
    """
    failures = control.consecutive_failures + 1
    blocked = control.blocked or failures >= config.max_consecutive_failures
    updated = replace(
        control,
        consecutive_failures=failures,
        cooldown_until=now + config.failure_cooldown_seconds,
        blocked=blocked,
    )
    return {"control": updated, "newly_blocked": blocked and not control.blocked}


def record_success(control):
    """The control row after a build that reached ``candidate``: no failures, no cooldown, not blocked."""
    return replace(control, consecutive_failures=0, cooldown_until=None, blocked=False)


def blocked_explanation():
    """What ``image status`` says about a blocked control row: the two ways to clear it.

    Both are the clearings ``build_decision`` implements: a basis digest that
    differs from the last failed build's, and ``--force``.
    """
    return {"blocked": True, "clear_by": ["a basis digest change", "image run --apply --force"]}
