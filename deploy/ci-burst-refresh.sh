#!/bin/sh
# Deploy the current ci-burst main to this host as the controller image.
#
# One run compares the deployed commit with origin/main and does nothing when
# they match. Otherwise it builds a candidate, smoke-tests it with a `plan` run
# (which writes nothing, allocates nothing and launches nothing), and only then
# promotes the tag the scheduled units resolve. A fetch, build or smoke failure
# leaves the previous image in place: every scheduled run resolves
# localhost/ci-burst:latest fresh, and an in-flight container keeps the layer it
# started with, so nothing needs restarting after a promotion.
#
# No credential is stored by or passed to this script: the clone is a public
# HTTPS clone. The smoke run mounts this host's own deployment TOML and scaling
# credentials read-only, exactly as the apply unit does, so a broken mount or a
# configuration the new image cannot read is caught before the schedule sees it.
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

if ! git -C "$SOURCE" checkout --quiet --detach "$want"; then
    log "checkout_failed: $want"
    exit 2
fi

if ! podman build --quiet --tag "$IMAGE:candidate" "$SOURCE" >/dev/null; then
    log "build_failed: $want"
    exit 2
fi

# Every controller run prints one JSON document and exits 0 for a completed
# decision or 2 for a refusal with nothing mutated, so either exit is accepted
# as long as the document is there. A crash exits 1 and prints nothing, which is
# the case this gate exists to catch. `plan` is the safe command: it is an
# observation path.
smoke_rc=0
smoke=$(podman run --rm --read-only \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
    --volume "$CONFIG:/etc/ci-burst/deployment.toml:ro" \
    --volume "$AWS_CREDENTIALS:/etc/ci-burst/aws:ro" \
    --env-file "$TOKEN_ENV" \
    "$IMAGE:candidate" plan --config /etc/ci-burst/deployment.toml 2>/dev/null) || smoke_rc=$?
case "$smoke_rc" in
    0 | 2) ;;
    *)
        log "smoke_failed: $want exit $smoke_rc"
        exit 2
        ;;
esac
case "$smoke" in
    '{'*) ;;
    *)
        log "smoke_failed: $want printed no decision document"
        exit 2
        ;;
esac

short=$(printf '%s' "$want" | cut -c1-12)
podman tag "$IMAGE:candidate" "$IMAGE:latest" || { log "promote_failed: $want"; exit 2; }
# The immutable tag is the rollback handle: `podman tag localhost/ci-burst:sha-<12> latest`.
podman tag "$IMAGE:candidate" "$IMAGE:sha-$short" || { log "tag_failed: $want"; exit 2; }
podman rmi "$IMAGE:candidate" >/dev/null 2>&1 || true

printf '%s\n' "$want" >"$STATE/deployed-sha.tmp"
mv "$STATE/deployed-sha.tmp" "$STATE/deployed-sha"
log "promoted: $want"
