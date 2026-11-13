#!/usr/bin/python3
"""Write the ci-burst deployment-state document after a refresh attempt.

Installed beside the refresh script and run from the refresh unit's
ExecStopPost, so it sees SERVICE_RESULT for both success and failure. The
serving image's OCI revision label is the source of truth for what is deployed;
``deployed-sha`` alone is not, because a rollback retags an older image without
rewriting it. It reads only local artifacts and never contacts a forge or a
cloud provider.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

SCHEMA = "deployment-state.v1"
# A logical, public-safe label. The installed units set CI_BURST_TARGET; a
# hostname is never a default and must never reach the document.
DEFAULT_TARGET = "controller"
REVISION_LABEL = "org.opencontainers.image.revision"
VERSION_LABEL = "org.opencontainers.image.version"


def _run(command, **kwargs):
    try:
        return subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=30, **kwargs
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _sha(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        return ""
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else ""


def _file_time(path):
    try:
        return int(os.path.getmtime(path))
    except OSError:
        return 0


def _git_show(source, revision, path):
    result = _run(["git", "-C", source, "show", f"{revision}:{path}"])
    if result is None or result.returncode != 0:
        return ""
    return result.stdout


def _version(source, revision):
    text = _git_show(source, revision, "ci_burst/__init__.py")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else ""


def _normalize_image_id(value):
    """Return the canonical ``sha256:<hex>`` identity for an engine image ID.

    podman 5 prints ``.Id`` with and without the prefix depending on the engine
    build. The document is compared across hosts, so both forms render the same
    way; anything that is not a full digest is passed through unchanged rather
    than guessed at.
    """
    value = value.strip()
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return f"sha256:{value}"
    return value


def _image_id(image):
    result = _run(["podman", "image", "inspect", "--format", "{{.Id}}", image])
    if result is None or result.returncode != 0:
        return ""
    return _normalize_image_id(result.stdout)


def _image_label(image, name):
    result = _run([
        "podman", "image", "inspect", "--format",
        '{{ index .Config.Labels "' + name + '" }}', image,
    ])
    if result is None or result.returncode != 0:
        return ""
    return result.stdout.strip()


def read_observation(state_dir):
    """The channel SHA and its observation time, from the success marker only.

    ``FETCH_HEAD`` is bookkeeping that a failed fetch can still rewrite, so it
    is never a freshness source. Absence of the marker is represented as an
    empty ref and a zero timestamp; the document then reports unknown source
    freshness rather than inventing one.
    """
    marker = os.path.join(state_dir, "source-sha")
    return _sha(marker), _file_time(marker)


def _drift(source, revision, units, unit_dir):
    drifted = []
    for unit in units:
        source_text = _git_show(source, revision, f"deploy/{unit}")
        if not source_text:
            drifted.append({"surface": "units", "detail": f"{unit}: source unavailable"})
            continue
        try:
            with open(os.path.join(unit_dir, unit), "r", encoding="utf-8") as handle:
                installed = handle.read()
        except OSError:
            drifted.append({"surface": "units", "detail": f"{unit} is missing"})
            continue
        if source_text != installed:
            drifted.append({"surface": "units", "detail": unit})
    return drifted


def choose_last_success(*, success, pinned, deployed_ref, source_ref, now, previous):
    """Advance only when the serving revision is the channel or deliberately held."""
    if success and (pinned or (deployed_ref and deployed_ref == source_ref)):
        return now
    return previous


def build_document(*, dependency, target, deployed_ref, source_ref, source_checked_at,
                   deployed_version, artifact_id, pinned, service_result,
                   last_success, now, drift):
    behind = bool(deployed_ref and source_ref and deployed_ref != source_ref)
    failed = service_result not in ("success", "")
    if failed:
        result = "failed"
    elif deployed_ref and (not behind or pinned):
        result = "current"
    else:
        result = "unknown"
    return {
        "schema": SCHEMA,
        "dependency": dependency,
        "target": target,
        "mode": "refresh",
        "channel": "main",
        "deployed_ref": deployed_ref,
        "deployed_version": deployed_version,
        "artifact_id": artifact_id,
        "source_ref": source_ref,
        "source_checked_at": source_checked_at,
        "last_success": last_success,
        "last_attempt": now,
        "result": result,
        "behind": behind,
        "integrity_ok": bool(artifact_id and deployed_ref),
        "pinned": pinned,
        "drift": drift,
    }


def _previous_success(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        value = document.get("last_success")
        return value if isinstance(value, int) else 0
    except (OSError, ValueError):
        return 0


def write_atomic(path, document):
    directory = os.path.dirname(path) or "."
    temporary = os.path.join(directory, f".{os.path.basename(path)}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=os.environ.get("CI_BURST_SOURCE", "/opt/ci-burst-src"))
    parser.add_argument("--state-dir", default=os.environ.get("CI_BURST_IMAGE_STATE", "/var/lib/ci-burst-image"))
    parser.add_argument("--image", default="localhost/ci-burst:latest")
    parser.add_argument("--unit-dir", default="/etc/systemd/system")
    parser.add_argument("--result", default=os.environ.get("SERVICE_RESULT", ""))
    parser.add_argument("--units", default=os.environ.get("CI_BURST_UNITS", ""))
    parser.add_argument("--dependency", default="ci-burst")
    parser.add_argument("--target", default=os.environ.get("CI_BURST_TARGET", DEFAULT_TARGET))
    parser.add_argument("--output", default=os.path.join(
        os.environ.get("CI_BURST_IMAGE_STATE", "/var/lib/ci-burst-image"), "deployment-state.json"))
    parser.add_argument("--now", type=int)
    arguments = parser.parse_args(argv)

    now = arguments.now if arguments.now is not None else int(time.time())
    source_ref, source_checked_at = read_observation(arguments.state_dir)
    artifact_id = _image_id(arguments.image)
    deployed_ref = _image_label(arguments.image, REVISION_LABEL)
    deployed_version = _image_label(arguments.image, VERSION_LABEL)
    if deployed_ref and not deployed_version:
        deployed_version = _version(arguments.source, deployed_ref)
    pinned = os.path.exists(os.path.join(arguments.state_dir, "hold"))
    units = [unit for unit in arguments.units.split() if unit]
    drift = _drift(arguments.source, deployed_ref, units, arguments.unit_dir) if deployed_ref else []
    previous = _previous_success(arguments.output)
    success = arguments.result == "success" or arguments.result == ""
    document = build_document(
        dependency=arguments.dependency,
        target=arguments.target,
        deployed_ref=deployed_ref,
        source_ref=source_ref,
        source_checked_at=source_checked_at,
        deployed_version=deployed_version,
        artifact_id=artifact_id,
        pinned=pinned,
        service_result=arguments.result,
        last_success=choose_last_success(
            success=success, pinned=pinned, deployed_ref=deployed_ref,
            source_ref=source_ref, now=now, previous=previous),
        now=now,
        drift=drift,
    )
    write_atomic(arguments.output, document)
    return 0


if __name__ == "__main__":
    sys.exit(main())
