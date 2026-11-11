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
TOKEN_ENV=${CI_BURST_TOKEN_ENV:-/etc/ci-burst/forgejo.env}
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
# `--apply` reports `planned` when it selected nothing to terminate. A refusal
# document, an `error`, or any other decision fails the smoke. A command this
# script does not know refuses before anything is built, rather than guessing an
# accepted set for it.
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
trap 'rm -rf "$work"' EXIT INT TERM
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
if ! podman tag "$IMAGE:candidate" "$IMAGE:sha-$short"; then
    log "tag_failed: $want"
    exit 2
fi

# The way back is captured before the serving tag moves, and captured from
# `latest` rather than assumed: the first refresh after a manual install
# replaces an image that has no sha tag of its own. An inspection failure is
# not "no image": only a confirmed absent tag may skip the handle.
candidate=$(podman image inspect --format '{{.Id}}' "$IMAGE:candidate") || {
    log "inspect_failed: $IMAGE:candidate"
    exit 2
}
if [ -z "$candidate" ]; then
    log "inspect_failed: $IMAGE:candidate"
    exit 2
fi
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
