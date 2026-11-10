#!/bin/sh
# Deploy the current ci-burst main to this host: the controller image and the
# units that schedule it.
#
# One run compares the deployed commit with origin/main and does nothing when
# they match. Otherwise it builds the image from a tar archive of exactly that
# commit (never from the working tree, so an edited checkout cannot be deployed
# under a clean commit's name), smoke-tests the candidate, installs the units
# from the same archive, and only then moves the tag the units resolve.
#
# Refreshing the units keeps the host in step with the repository instead of
# drifting from it: a merged change to a unit or a timer takes effect on the
# next run, exactly like a merged change to the controller. The cleanup host has
# no refresh loop by design (it must survive a scaler failure), so its reap unit
# stays a manual install. Removal is manual too: this script installs and
# updates, it never deletes a unit.
#
# The smoke run is a `plan` execution from the candidate against this host's own
# configuration and scaling credentials. It must complete with a decision, not
# refuse: `plan` writes nothing, allocates nothing and launches nothing.
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
UNIT_DIR=${CI_BURST_UNIT_DIR:-/etc/systemd/system}
SYSTEMCTL=${CI_BURST_SYSTEMCTL:-/usr/bin/systemctl}

# The units this host schedules itself with. The reap unit is not here on
# purpose: it belongs to the independent cleanup host, which has no refresh
# loop, so installing it here would create a second, unintended scheduler on
# this host.
UNITS="ci-burst-apply.service ci-burst-apply.timer ci-burst-refresh.service ci-burst-refresh.timer"

log() { printf 'ci-burst-refresh: %s\n' "$*"; }

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
    smoke=$(podman run --rm --init --replace --network ci-burst --name ci-burst-refresh-smoke \
        --read-only \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
        --volume "$CONFIG:/etc/ci-burst/deployment.toml:ro" \
        --volume "$AWS_CREDENTIALS:/etc/ci-burst/aws:ro" \
        --env-file "$TOKEN_ENV" \
        "$IMAGE:candidate" plan --config /etc/ci-burst/deployment.toml 2>/dev/null) || smoke_rc=$?
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
# The smoke gate accepts only a completed, non-refusal plan decision. A refusal
# document (`decision: refused`, or `error`) means the candidate cannot read this
# host's configuration, which is exactly what the next scheduled apply would hit.
# The accepted decisions are the plan results that mean "observed, decided
# nothing": `wait` and `would_launch`. A new plan decision needs this list
# updated deliberately, in the same change that introduces it.
if ! printf '%s' "$smoke" | python3 -c '
import json
import sys

document = json.load(sys.stdin)
if not isinstance(document, dict):
    raise SystemExit(1)
if "error" in document:
    raise SystemExit(1)
if document.get("decision") not in ("wait", "would_launch"):
    raise SystemExit(1)
' 2>/dev/null; then
    log "smoke_failed: $want did not print a completed plan decision"
    exit 2
fi

short=$(printf '%s' "$want" | cut -c1-12)
if ! podman tag "$IMAGE:candidate" "$IMAGE:sha-$short"; then
    log "tag_failed: $want"
    exit 2
fi

# --- units: stage, verify, install, restore on failure -----------------------
#
# A deployment advances the units and the image together or neither. Every unit
# is staged and verified before anything on the host is touched; the set that is
# installed before this deployment is snapshotted as the rollback generation;
# admission is paused across the transition so a scheduled apply cannot run new
# units against the old image; and any later failure restores the snapshot and
# resumes admission.

SNAPSHOT="$STATE/units"
STAGE="$SNAPSHOT/$want"
ROLLBACK="$SNAPSHOT/previous"
APPLY_TIMER=ci-burst-apply.timer
admission_was_active=no

stage_units() {
    rm -rf "$STAGE"
    install -d -m 0700 "$STAGE" || { log 'unit_stage_failed'; return 2; }
    for unit in $UNITS; do
        source_file="$work/src/deploy/$unit"
        if [ ! -f "$source_file" ]; then
            log "unit_missing: $unit"
            return 2
        fi
        if ! systemd-analyze verify "$source_file" >/dev/null 2>&1; then
            log "unit_invalid: $unit"
            return 2
        fi
        if ! install -m 0600 "$source_file" "$STAGE/$unit"; then
            log "unit_stage_failed: $unit"
            return 2
        fi
    done
}

snapshot_installed() {
    rm -rf "$SNAPSHOT/next"
    install -d -m 0700 "$SNAPSHOT/next" || { log 'unit_snapshot_failed'; return 2; }
    for unit in $UNITS; do
        if [ -f "$UNIT_DIR/$unit" ]; then
            if ! cp -p "$UNIT_DIR/$unit" "$SNAPSHOT/next/$unit"; then
                log "unit_snapshot_failed: $unit"
                return 2
            fi
        fi
    done
    rm -rf "$ROLLBACK"
    if ! mv "$SNAPSHOT/next" "$ROLLBACK"; then
        log 'unit_snapshot_failed'
        return 2
    fi
}

install_units() {
    changed=""
    for unit in $UNITS; do
        if [ -f "$UNIT_DIR/$unit" ] && cmp -s "$STAGE/$unit" "$UNIT_DIR/$unit"; then
            continue
        fi
        if ! install -o root -g root -m 0644 "$STAGE/$unit" "$UNIT_DIR/$unit"; then
            log "unit_install_failed: $unit"
            return 2
        fi
        changed="$changed $unit"
    done
    if [ -n "$changed" ]; then
        if ! "$SYSTEMCTL" daemon-reload; then
            log 'daemon_reload_failed'
            return 2
        fi
        log "units_updated:$changed"
    else
        log 'units_unchanged'
    fi
}

restore_units() {
    restored=""
    for unit in $UNITS; do
        if [ -f "$ROLLBACK/$unit" ]; then
            if ! install -o root -g root -m 0644 "$ROLLBACK/$unit" "$UNIT_DIR/$unit"; then
                log "unit_restore_failed: $unit"
                return 2
            fi
            restored="$restored $unit"
        fi
    done
    if [ -n "$restored" ]; then
        if ! "$SYSTEMCTL" daemon-reload; then
            log 'daemon_reload_failed'
            return 2
        fi
        log "units_restored:$restored"
    fi
}

pause_admission() {
    if "$SYSTEMCTL" is-active --quiet "$APPLY_TIMER"; then
        admission_was_active=yes
        if ! "$SYSTEMCTL" stop "$APPLY_TIMER"; then
            log 'admission_stop_failed'
        fi
    fi
}

resume_admission() {
    if [ "$admission_was_active" = yes ]; then
        if ! "$SYSTEMCTL" start "$APPLY_TIMER"; then
            log 'admission_start_failed'
        fi
    fi
}

abort_deployment() {
    log "deploy_failed: $1"
    restore_units || true
    resume_admission
    exit 2
}

if ! stage_units; then
    log "units_failed: $want"
    exit 2
fi
pause_admission
if ! snapshot_installed; then
    resume_admission
    exit 2
fi
if ! install_units; then
    abort_deployment "units $want"
fi

# The way back is captured before the serving tag moves, and captured from
# `latest` rather than assumed: the first refresh after a manual install
# replaces an image that has no sha tag of its own. An inspection failure is
# not "no image": only a confirmed absent tag may skip the handle.
candidate=$(podman image inspect --format '{{.Id}}' "$IMAGE:candidate") || {
    abort_deployment "inspect $IMAGE:candidate"
}
if [ -z "$candidate" ]; then
    abort_deployment "inspect $IMAGE:candidate"
fi
current=
exists_rc=0
podman image exists "$IMAGE:latest" || exists_rc=$?
case "$exists_rc" in
    0)
        current=$(podman image inspect --format '{{.Id}}' "$IMAGE:latest") || {
            abort_deployment "inspect $IMAGE:latest"
        }
        if [ -z "$current" ]; then
            abort_deployment "inspect $IMAGE:latest"
        fi
        ;;
    1)
        # `podman image exists` documents 1 as "absent"; that is the only status
        # that may skip the handle. Anything else is a storage error, and a
        # storage error is not an absent image.
        current=
        ;;
    *)
        abort_deployment "exists $IMAGE:latest exit $exists_rc"
        ;;
esac
if [ -n "$current" ] && [ "$current" != "$candidate" ]; then
    if ! podman tag "$current" "$IMAGE:previous"; then
        abort_deployment "previous_tag $want"
    fi
fi

if ! podman tag "$IMAGE:candidate" "$IMAGE:latest"; then
    abort_deployment "promote $want"
fi
podman rmi "$IMAGE:candidate" >/dev/null 2>&1 || true

if ! printf '%s\n' "$want" >"$STATE/deployed-sha.tmp" || ! mv "$STATE/deployed-sha.tmp" "$STATE/deployed-sha"; then
    abort_deployment "record $want"
fi
resume_admission
log "promoted: $want"
