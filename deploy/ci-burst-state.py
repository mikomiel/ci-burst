#!/usr/bin/python3
"""Write the ci-burst deployment-state document after a refresh attempt.

Installed beside the refresh script and run from the refresh unit's
ExecStopPost, so it sees SERVICE_RESULT for both success and failure. It reads
only local artifacts: the deployed SHA file, the public checkout, the local
image and the installed units. It never contacts a forge or a cloud provider.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time

SCHEMA = "deployment-state.v1"


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


def _git_show(source, revision, path):
    result = _run(["git", "-C", source, "show", f"{revision}:{path}"])
    if result is None or result.returncode != 0:
        return ""
    return result.stdout


def _version(source, revision):
    text = _git_show(source, revision, "ci_burst/__init__.py")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else ""


def _image_id(image):
    result = _run(["podman", "image", "inspect", "--format", "{{.Id}}", image])
    if result is None or result.returncode != 0:
        return ""
    return result.stdout.strip()


def _drift(source, revision, units, unit_dir):
    drifted = []
    for unit in units:
        source_text = _git_show(source, revision, f"deploy/{unit}")
        if not source_text:
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


def build_document(*, dependency, target, deployed_ref, source_ref, deployed_version,
                   artifact_id, pinned, service_result, last_success, now, drift):
    behind = bool(deployed_ref and source_ref and deployed_ref != source_ref)
    failed = service_result not in ("success", "")
    if failed:
        result = "failed"
    elif behind and not pinned:
        result = "unknown"
    else:
        result = "current"
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
        "source_checked_at": now,
        "last_success": last_success,
        "last_attempt": now,
        "result": result,
        "behind": behind,
        "integrity_ok": bool(artifact_id),
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
    parser.add_argument("--refresh-unit", default="ci-burst-refresh.service")
    parser.add_argument("--unit-dir", default="/etc/systemd/system")
    parser.add_argument("--result", default=os.environ.get("SERVICE_RESULT", ""))
    parser.add_argument("--units", default=os.environ.get("CI_BURST_UNITS", ""))
    parser.add_argument("--dependency", default="ci-burst")
    parser.add_argument("--target", default=socket.gethostname().split(".")[0])
    parser.add_argument("--output", default=os.path.join(
        os.environ.get("CI_BURST_IMAGE_STATE", "/var/lib/ci-burst-image"), "deployment-state.json"))
    parser.add_argument("--now", type=int)
    arguments = parser.parse_args(argv)

    now = arguments.now if arguments.now is not None else int(time.time())
    deployed_ref = _sha(os.path.join(arguments.state_dir, "deployed-sha"))
    source_ref = _sha(os.path.join(arguments.state_dir, "source-sha"))
    if not source_ref:
        for reference in ("FETCH_HEAD", "origin/main"):
            result = _run(["git", "-C", arguments.source, "rev-parse", reference])
            if result is not None and result.returncode == 0 and result.stdout.strip():
                source_ref = result.stdout.strip()
                break
    artifact_id = _image_id(arguments.image)
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
        deployed_version=_version(arguments.source, deployed_ref) if deployed_ref else "",
        artifact_id=artifact_id,
        pinned=pinned,
        service_result=arguments.result,
        last_success=now if success and (deployed_ref == source_ref or pinned) else previous,
        now=now,
        drift=drift,
    )
    write_atomic(arguments.output, document)
    return 0


if __name__ == "__main__":
    sys.exit(main())
