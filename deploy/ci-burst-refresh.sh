#!/bin/sh
# Deploy the current ci-burst main to this host as the ci-burst image.
#
# One run compares the deployed commit with origin/main and does nothing when
# they match. Otherwise it builds the image from a tar archive of exactly that
# commit (never from the working tree, so an edited checkout cannot be deployed
# under a clean commit's name), smoke-tests the candidate, and only then moves
# the tag the scheduled units resolve.
#
# The smoke run is one execution from the candidate against this host's own
# configuration and its own credentials, and it must complete with a decision
# rather than refuse. Which command it runs depends on the host's role:
#   * a controller host runs `plan` (observation only: writes nothing, allocates
#     nothing, launches nothing) with the scaling credentials;
#   * a cleanup-only host runs `reap` without `--apply` (inventory and expiry
#     selection, never a termination) with the cleanup credentials and no
#     Forgejo token.
# The distinction matters because a reaper whose image cannot read this host's
# configuration keeps a green timer while expiry cleanup has stopped, which is
# precisely the failure this smoke exists to catch.
#
# Rollback handles are created before the serving tag moves, so a failure never
# leaves the host with no working image and no way back:
#   * localhost/ci-burst:sha-<12>  the promoted commit, immutable;
#   * localhost/ci-burst:previous  whatever `latest` pointed at before this
#                                 promotion, including the hand-built image a
#                                 fresh install starts with.
#
# No credential is stored by or passed to this script: the clone is a public
# HTTPS clone. Host python3 is required to validate the smoke document, as it
# already is for the documented lifecycle test in docs/container.md.
#
# Installed from the checkout:
#   install -m 0755 deploy/ci-burst-refresh.sh /usr/local/sbin/ci-burst-refresh
set -eu

SOURCE=${CI_BURST_SOURCE:-/opt/ci-burst-src}
STATE=${CI_BURST_IMAGE_STATE:-/var/lib/ci-burst-image}
CONFIG=${CI_BURST_CONFIG:-/etc/ci-burst/deployment.toml}
AWS_CREDENTIALS=${CI_BURST_AWS_DIR:-/etc/ci-burst/aws-scaling}
# No colon: an explicitly empty value must stay empty, because a cleanup-only
# host opts out of the token file rather than falling back to one it may
# have inherited from an earlier role.
TOKEN_ENV=${CI_BURST_TOKEN_ENV-/etc/ci-burst/forgejo.env}
IMAGE=localhost/ci-burst
SMOKE_RETRY_SECONDS=${CI_BURST_SMOKE_RETRY_SECONDS:-20}
SMOKE_COMMAND=${CI_BURST_SMOKE_COMMAND:-plan}
SMOKE_NETWORK=${CI_BURST_SMOKE_NETWORK:-ci-burst}
UNIT_DIR=${CI_BURST_UNIT_DIR:-/etc/systemd/system}

# The units this deployment installs by hand. They are compared, never written:
# installing scheduler units unattended needs atomic generations and admission
# control that are disproportionate to how rarely a unit changes. A host with a
# different role overrides the list, so its drift report describes that host
# instead of reporting the other role's units as missing.
UNITS=${CI_BURST_UNITS:-"ci-burst-apply.service ci-burst-apply.timer ci-burst-refresh.service ci-burst-refresh.timer"}

log() { printf 'ci-burst-refresh: %s\n' "$*"; }

# The accepted decision depends on the role's command: `plan` reports `wait` or
# `would_launch` when it observed and decided to do nothing; `reap` without
# `--apply` reports `planned` after selecting expired instances without
# requesting their termination, so a nonempty `expired` list is still `planned`.
# A refusal document, an `error`, or any other decision fails the smoke. A
# command this script does not know refuses before anything is built, rather
# than guessing an accepted set for it.
case "$SMOKE_COMMAND" in
    plan) ACCEPTED_DECISIONS="wait would_launch" ;;
    reap) ACCEPTED_DECISIONS="planned" ;;
    *) log "smoke_command_invalid: $SMOKE_COMMAND"; exit 2 ;;
esac


install -d -m 0700 "$STATE"
exec 9>"$STATE/refresh.lock"
if ! flock -n 9; then
    log 'busy: another refresh holds the lock'
    exit 0
fi

recorded=$(cat "$STATE/deployed-sha" 2>/dev/null || true)

if ! git -C "$SOURCE" fetch --quiet --depth=1 origin main; then
    log 'fetch_failed'
    exit 2
fi
want=$(git -C "$SOURCE" rev-parse FETCH_HEAD) || { log 'resolve_failed'; exit 2; }

if [ "$want" = "$recorded" ]; then
    log "current: $want"
    exit 0
fi

work=$(mktemp -d)
record_tmp=
trap 'rm -rf "$work"; [ -z "$record_tmp" ] || rm -f "$record_tmp"' EXIT INT TERM
mkdir "$work/src"
if ! git -C "$SOURCE" archive --format=tar --output "$work/tree.tar" "$want"; then
    log "archive_failed: $want"
    exit 2
fi
if ! tar -xf "$work/tree.tar" -C "$work/src"; then
    log "unpack_failed: $want"
    exit 2
fi

# Host networking is for the build only: the base image and the package
# install download far more than one segment, and a container bridge whose
# MTU is larger than the host path MTU stalls those transfers with no error.
# The smoke run below uses the same named network as the scheduled units, so it
# exercises exactly the networking they use.
if ! podman build --quiet --network=host --tag "$IMAGE:candidate" "$work/src" >/dev/null; then
    log "build_failed: $want"
    exit 2
fi

attempt=1
while :; do
    smoke_rc=0
    set -- run --rm --init --replace --network "$SMOKE_NETWORK" --name ci-burst-refresh-smoke \
        --read-only \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
        --volume "$CONFIG:/etc/ci-burst/deployment.toml:ro" \
        --volume "$AWS_CREDENTIALS:/etc/ci-burst/aws:ro"
    # A cleanup-only host has no Forgejo token, and its reaper must not need one:
    # the reaper is deliberately independent of Forgejo. An empty or absent token
    # path drops the flag instead of failing the run.
    if [ -n "$TOKEN_ENV" ] && [ -f "$TOKEN_ENV" ]; then
        set -- "$@" --env-file "$TOKEN_ENV"
    fi
    set -- "$@" "$IMAGE:candidate" "$SMOKE_COMMAND" --config /etc/ci-burst/deployment.toml
    smoke=$(podman "$@" 2>/dev/null) || smoke_rc=$?
    if [ "$smoke_rc" -eq 0 ]; then
        break
    fi
    if [ "$attempt" -ge 2 ]; then
        log "smoke_failed: $want exit $smoke_rc"
        exit 2
    fi
    # One retry, and only for the run itself: an unreachable Forgejo or AWS is
    # not evidence about this image, and it is exactly the case a second look a
    # few seconds later resolves. A second failure still keeps the old image.
    log "smoke_retry: $want exit $smoke_rc"
    attempt=$((attempt + 1))
    sleep "$SMOKE_RETRY_SECONDS"
done
# The smoke gate accepts only a completed, non-refusal decision for this host's
# role. A refusal document (`decision: refused`, or `error`) means the candidate
# cannot read this host's configuration, which is exactly what the next scheduled
# run would hit. A new decision needs this list updated deliberately, in the same
# change that introduces it.
if ! printf '%s' "$smoke" | ACCEPTED_DECISIONS="$ACCEPTED_DECISIONS" python3 -c '
import json
import os
import sys

document = json.load(sys.stdin)
if not isinstance(document, dict):
    raise SystemExit(1)
if "error" in document:
    raise SystemExit(1)
accepted = set(os.environ.get("ACCEPTED_DECISIONS", "").split())
if document.get("decision") not in accepted:
    raise SystemExit(1)
' 2>/dev/null; then
    log "smoke_failed: $want did not print an accepted $SMOKE_COMMAND decision"
    exit 2
fi

short=$(printf '%s' "$want" | cut -c1-12)

# A source commit identifies the source, not the bytes. The base image and the
# Python dependency set are resolved when the image is built, so two builds of
# one commit can differ. Record what this build actually resolved, and do it
# before any promotion tag moves, so the image on the host can be traced to its
# inputs instead of to the commit alone.
candidate=$(podman image inspect --format '{{.Id}}' "$IMAGE:candidate") || {
    log "inspect_failed: $IMAGE:candidate"
    exit 2
}
if [ -z "$candidate" ]; then
    log "inspect_failed: $IMAGE:candidate"
    exit 2
fi

tree=$(git -C "$SOURCE" rev-parse "$want^{tree}") || {
    log "build_inputs_failed: cannot resolve the tree of $want"
    exit 2
}
dockerfile_sha256=$(sha256sum "$work/src/Dockerfile" | awk '{print $1}')
if [ -z "$dockerfile_sha256" ]; then
    log "build_inputs_failed: cannot hash the build's Dockerfile"
    exit 2
fi
from_line=$(sed -n '/^FROM /{p;q;}' "$work/src/Dockerfile" 2>/dev/null) || from_line=
if [ -z "$from_line" ]; then
    log "build_inputs_failed: no FROM line in the build's Dockerfile"
    exit 2
fi
from_ref=$(printf '%s\n' "$from_line" | awk '{print $NF}')
case "$from_ref" in
    *@sha256:*)
        # A digest-pinned FROM carries the base digest itself.
        base_digest=${from_ref##*@}
        ;;
    *)
        # For an unpinned line the engine may still report what it resolved.
        # Record nothing rather than inventing a digest when it cannot.
        base_digest=$(podman image inspect --format '{{.Digest}}' "$from_ref" 2>/dev/null || true)
        ;;
esac
engine=$(podman --version 2>/dev/null || true)

# Read the dependency set from inside the candidate, with no network path.
distributions="$work/python-distributions.txt"
if ! podman run --rm --network=none --entrypoint python "$IMAGE:candidate" \
        -m pip list --format=freeze >"$distributions" 2>/dev/null; then
    log "build_inputs_failed: cannot read the candidate's Python distributions"
    exit 2
fi

mkdir -p "$STATE/builds" || {
    log "build_inputs_failed: cannot create $STATE/builds"
    exit 2
}
record="$STATE/builds/sha-$short.json"
record_tmp=$(mktemp "$STATE/builds/.sha-$short.XXXXXX") || {
    log "build_inputs_failed: cannot create a temporary record"
    exit 2
}
if ! SOURCE_SHA="$want" TREE_SHA="$tree" DOCKERFILE_SHA256="$dockerfile_sha256" \
        FROM_LINE="$from_line" BASE_DIGEST="$base_digest" IMAGE_ID="$candidate" \
        ENGINE="$engine" CREATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        python3 - "$distributions" >"$record_tmp" <<'PY'
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    distributions = [line.strip() for line in handle if line.strip()]

document = {
    "schema": "ci-burst.build-inputs.v1",
    "source_sha": os.environ["SOURCE_SHA"],
    "tree_sha": os.environ["TREE_SHA"],
    "dockerfile_sha256": os.environ["DOCKERFILE_SHA256"],
    "from": os.environ["FROM_LINE"],
    "base_digest": os.environ["BASE_DIGEST"] or None,
    "image_id": os.environ["IMAGE_ID"],
    "engine": os.environ["ENGINE"] or "unknown",
    "python_distributions": distributions,
    "created_at": os.environ["CREATED_AT"],
}
json.dump(document, sys.stdout, indent=2, sort_keys=True)
sys.stdout.write("\n")
PY
then
    rm -f "$record_tmp"
    record_tmp=
    log "build_inputs_failed: cannot render $record"
    exit 2
fi
chmod 0644 "$record_tmp"
if ! mv -f "$record_tmp" "$record"; then
    log "build_inputs_failed: cannot publish $record"
    exit 2
fi
record_tmp=
log "build_inputs: $record"

if ! podman tag "$IMAGE:candidate" "$IMAGE:sha-$short"; then
    log "tag_failed: $want"
    exit 2
fi

# The way back is captured before the serving tag moves, and captured from
# `latest` rather than assumed: the first refresh after a manual install
# replaces an image that has no sha tag of its own. An inspection failure is
# not "no image": only a confirmed absent tag may skip the handle.
current=
exists_rc=0
podman image exists "$IMAGE:latest" || exists_rc=$?
case "$exists_rc" in
    0)
        current=$(podman image inspect --format '{{.Id}}' "$IMAGE:latest") || {
            log "inspect_failed: $IMAGE:latest"
            exit 2
        }
        if [ -z "$current" ]; then
            log "inspect_failed: $IMAGE:latest"
            exit 2
        fi
        ;;
    1)
        # `podman image exists` documents 1 as "absent"; that is the only status
        # that may skip the handle. Anything else is a storage error, and a
        # storage error is not an absent image.
        current=
        ;;
    *)
        log "exists_failed: $IMAGE:latest exit $exists_rc"
        exit 2
        ;;
esac
if [ -n "$current" ] && [ "$current" != "$candidate" ]; then
    if ! podman tag "$current" "$IMAGE:previous"; then
        log "previous_tag_failed: $want"
        exit 2
    fi
fi

# Report unit drift without touching a unit. A merged change to a unit is
# installed by hand (docs/container.md § "Scheduled operation with systemd"); the
# one thing this run must not do is let that difference stay invisible.
report_unit_drift() {
    drifted=""
    missing=""
    for unit in $UNITS; do
        source_file="$work/src/deploy/$unit"
        target_file="$UNIT_DIR/$unit"
        if [ ! -f "$source_file" ]; then
            continue
        fi
        if [ ! -f "$target_file" ]; then
            missing="$missing $unit"
        elif ! cmp -s "$source_file" "$target_file"; then
            drifted="$drifted $unit"
        fi
    done
    if [ -n "$drifted" ] || [ -n "$missing" ]; then
        log "units_drift:${drifted}${missing:+ missing:${missing}}"
        log "units_not_installed: run the install step in docs/container.md"
    else
        log 'units_current'
    fi
}

report_unit_drift

if ! podman tag "$IMAGE:candidate" "$IMAGE:latest"; then
    log "promote_failed: $want"
    exit 2
fi
podman rmi "$IMAGE:candidate" >/dev/null 2>&1 || true

printf '%s\n' "$want" >"$STATE/deployed-sha.tmp"
mv "$STATE/deployed-sha.tmp" "$STATE/deployed-sha"
log "promoted: $want"
