#!/bin/sh
# Deploy the current ci-burst main to this host as the ci-burst image.
#
# One run compares the deployed commit with origin/main and promotes nothing
# when they match. Otherwise it builds the image from a tar archive of exactly
# that commit (never from the working tree, so an edited checkout cannot be
# deployed under a clean commit's name), smoke-tests the candidate, and only
# then moves the tag the scheduled units resolve.
#
# A run that has proven the serving image is the promoted one also reconciles
# this host's build tags: one sha-<12> tag per merged commit accumulates at about
# 150 MB each, which is how a 7.8 GB host ran out of disk. The retained set and
# the deletion order are documented at the retention step below.
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
UNITS=${CI_BURST_UNITS:-"ci-burst-apply.service ci-burst-apply.timer ci-burst-refresh.service ci-burst-refresh.timer ci-burst-export.service ci-burst-export.timer"}

log() { printf 'ci-burst-refresh: %s\n' "$*"; }
# A retention refusal is worth flagging in the unit's journal: stdout lands at
# info level, so a warning-priority entry is written as well when logger exists.
# Nothing in this script alerts on it; that stays a separate decision because
# the worker-event stream is still unproven (HB-3339).
warn() {
    printf 'ci-burst-refresh: %s\n' "$*" >&2
    if command -v logger >/dev/null 2>&1; then
        logger -t ci-burst-refresh -p user.warning -- "$*" 2>/dev/null || true
    fi
}

# The standard revision label every build carries, and a label of this
# deployment's own. Retention prunes on the second: the first is a convention
# many products follow, so it cannot establish that an image is ours.
REVISION_LABEL=org.opencontainers.image.revision
OWNERSHIP_LABEL=org.ci-burst.build
# How many of the most recent sha-<12> builds to keep, in addition to every tag a
# rollback or the deployment-state document can still name. The number is a host
# property, so it is an explicit variable rather than a tuned constant: 0 keeps
# only the named tags, and a value outside 0-99 is a configuration error that
# refuses before anything is built, not one that silently keeps everything.
RETAIN_BUILDS=${CI_BURST_RETAIN_BUILDS:-3}
case "$RETAIN_BUILDS" in
    0|[1-9]|[1-9][0-9]) ;;
    *)
        log "retained_builds_invalid: ${RETAIN_BUILDS}"
        exit 2
        ;;
esac

# podman renders `.Id` with and without the `sha256:` prefix depending on the
# engine build, so normalise it once, before it becomes a path component, a
# record field or a comparison. A full digest gains the prefix; any other form
# the engine reports keeps its own spelling. Either way the value must stay a
# safe path component: sanitising a different shape could map two identities
# onto one record.
image_identity() {
    case "$1" in
        sha256:*) key=${1#sha256:} ;;
        *) key=$1 ;;
    esac
    case "$key" in
        ""|*[!A-Za-z0-9._-]*) return 1 ;;
    esac
    [ "${#key}" -le 128 ] || return 1
    if [ "${#key}" -eq 64 ]; then
        case "$key" in
            *[!0-9a-f]*) ;;
            *)
                printf 'sha256:%s\n' "$key"
                return 0
                ;;
        esac
    fi
    printf '%s\n' "$1"
}

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


# --- image tag retention -----------------------------------------------------
# Every merged commit leaves one more localhost/ci-burst:sha-<12> tag, at about
# 150 MB. Nothing removed them until this step existed, and a host with a 7.8 GB
# root disk filled after about forty-five builds. Deleting a build is the only
# irreversible act in this script, so the step is deliberately narrow:
#
#   * it runs only when this run has proven the serving image is the promoted
#     one - the promote path, or the already-current path - never before the
#     serving tag has moved;
#   * the retained set is latest, previous (the only rollback handle), the
#     sha-<12> of the deployed ref recorded on this host, the sha-<12> of every
#     40-hex ref the deployment-state document names, and the newest
#     RETAIN_BUILDS builds overall;
#   * it removes one name at a time, and only names this deployment created. A
#     build that shares its image with another tag keeps that other tag: an
#     untag removes the named tag, never the image;
#   * a tag whose image cannot be inspected is kept and reported;
#   * a retained ref that cannot be read fails closed: a present document that
#     does not parse, or a corrupted deployed ref, removes nothing;
#   * a refusal changes neither the exit status nor the promotion, because the
#     serving image is already correct. The log line is the durable signal.

retained_tag_suffixes() {
    printf '%s\n' latest previous
    if [ -e "$STATE/deployed-sha" ]; then
        deployed=$(cat "$STATE/deployed-sha" 2>/dev/null || true)
        case "$deployed" in
            *[!0-9a-f]*|"") return 1 ;;
        esac
        [ "${#deployed}" -eq 40 ] || return 1
        printf 'sha-%s\n' "$(printf '%s' "$deployed" | cut -c1-12)"
    fi
    state_document=$STATE/deployment-state.json
    # Only a genuinely absent path is an absent protection list. A directory, a
    # device or a dangling symlink at that path exists and cannot be read, so it
    # fails this function rather than silently authorising deletion.
    if [ ! -e "$state_document" ] && [ ! -L "$state_document" ]; then
        return 0
    fi
    [ -f "$state_document" ] || return 1
    # The document names commits, never tags, and its shape is owned elsewhere.
    # Only a full 40-hex string is read as a ref. A document that exists but
    # cannot be read fails here rather than returning an empty set: an unreadable
    # protection list must not authorise deletions.
    python3 - "$state_document" <<'PY'
import json
import re
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        document = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(1)


def strings(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, str):
        yield value


for value in strings(document):
    if re.fullmatch(r"[0-9a-f]{40}", value):
        print(f"sha-{value[:12]}")
PY
}


# The refusal line names the reason and the number of tags still on the host,
# so an operator reading the journal sees the size of the backlog and not just
# that something failed.
retention_failed() {
    remaining=unknown
    if [ -n "${retention_work:-}" ] \
        && podman images --format '{{.Tag}}' "$IMAGE" >"$retention_work/current" 2>/dev/null; then
        # Counted now, not from the listing taken before the removals: a warning
        # that overstates the backlog is worse than one with no number.
        remaining=$(wc -l <"$retention_work/current" 2>/dev/null || printf 'unknown')
    fi
    warn "retention_failed: $1; ${remaining} tags left"
}

reconcile_image_tags() (
    # A subshell: this function must not write to any variable the caller still
    # uses, in particular $work, which the EXIT trap removes after the run.
    retention_work=""
    if ! retention_work=$(mktemp -d 2>/dev/null) || [ -z "$retention_work" ]; then
        retention_work=""
        retention_failed 'cannot create a work directory'
        exit 0
    fi
    # List the tags first: a refusal below reports how many are still on the
    # host, and a retention failure is only useful with the size of the backlog.
    : >"$retention_work/tags"
    if ! : >"$retention_work/tags" \
        || ! podman images --format '{{.Tag}}' "$IMAGE" >"$retention_work/tags" 2>/dev/null; then
        retention_failed 'cannot list the image tags, removing nothing'
        rm -rf "$retention_work"
        exit 0
    fi
    if ! retained_tag_suffixes >"$retention_work/kept" 2>/dev/null; then
        retention_failed 'cannot read the retained refs, removing nothing'
        rm -rf "$retention_work"
        exit 0
    fi
    if ! : >"$retention_work/ranked"; then
        retention_failed 'cannot write a scratch file, removing nothing'
        rm -rf "$retention_work"
        exit 0
    fi

    # Rank every inspectable build, named or not, by creation time. The window is
    # computed independently as the newest RETAIN_BUILDS of that whole set, then
    # united with the named refs.
    unverified=0
    : >"$retention_work/ranked"
    while IFS= read -r tag; do
        # Only this deployment's immutable build tags are candidates. The shape
        # check also keeps a malformed tag out of a podman argument.
        case "$tag" in
            sha-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
            *) continue ;;
        esac
        created=$(podman image inspect --format '{{.Created}}' "$IMAGE:$tag" 2>/dev/null) || created=
        if [ -z "$created" ]; then
            log "retention_kept: $tag cannot be inspected"
            unverified=$((unverified + 1))
            continue
        fi
        if ! printf '%s|%s\n' "$created" "$tag" >>"$retention_work/ranked"; then
            retention_failed 'cannot write a scratch file, removing nothing'
            rm -rf "$retention_work"
            exit 0
        fi
    done <"$retention_work/tags"

    if ! : >"$retention_work/window"; then
        retention_failed 'cannot write a scratch file, removing nothing'
        rm -rf "$retention_work"
        exit 0
    fi
    if [ "$RETAIN_BUILDS" -gt 0 ] && [ -s "$retention_work/ranked" ]; then
        # Each stage is checked on its own. A pipeline reports only its last
        # command's status, so a failed `sort` would leave an empty window and
        # the deletion step would remove the newest builds the window protects.
        if ! sort -r "$retention_work/ranked" >"$retention_work/ranked.sorted"; then
            retention_failed 'cannot order the builds, removing nothing'
            rm -rf "$retention_work"
            exit 0
        fi
        if ! head -n "$RETAIN_BUILDS" "$retention_work/ranked.sorted" \
            >"$retention_work/window.rows"; then
            retention_failed 'cannot select the newest builds, removing nothing'
            rm -rf "$retention_work"
            exit 0
        fi
        if ! cut -d'|' -f2 "$retention_work/window.rows" >"$retention_work/window"; then
            retention_failed 'cannot write the retained window, removing nothing'
            rm -rf "$retention_work"
            exit 0
        fi
        if [ ! -s "$retention_work/window" ]; then
            retention_failed 'the retained window is empty, removing nothing'
            rm -rf "$retention_work"
            exit 0
        fi
    fi

    untagged=0
    refused=0
    while IFS='|' read -r created tag; do
        [ -n "$tag" ] || continue
        if grep -Fqx "$tag" "$retention_work/kept" 2>/dev/null; then
            continue
        fi
        if grep -Fqx "$tag" "$retention_work/window" 2>/dev/null; then
            continue
        fi
        # `podman untag` takes an image selector and then the names to remove;
        # with no name it removes every name on the image. Pass the full
        # reference twice - selector and name - so only this build's tag goes
        # and an image shared with latest or previous keeps those names. Short
        # names are expanded with localhost/ and :latest, so the name operand is
        # always the full reference.
        if podman untag "$IMAGE:$tag" "$IMAGE:$tag" >/dev/null 2>&1; then
            untagged=$((untagged + 1))
        else
            log "retention_kept: $tag could not be removed"
            refused=$((refused + 1))
        fi
    done <"$retention_work/ranked"

    # Prune on every run, not only when this run removed a name: a prune that was
    # refused must be retried on the next run, or the dangling images stay. The
    # filter is this deployment's own build label, so an image another product
    # built - even one carrying the standard OCI revision label - is never a
    # candidate, and images built before the label existed are left alone.
    if podman image prune -f --filter "label=$OWNERSHIP_LABEL=refresh" >/dev/null 2>&1; then
        log "retention: removed $untagged build tags, pruned owned dangling images"
    else
        retention_failed "prune refused after $untagged build tags were removed"
    fi
    if [ "$refused" -gt 0 ]; then
        retention_failed "$refused build tags could not be removed"
    fi
    if [ "$unverified" -gt 0 ]; then
        log "retention: kept $unverified tags unverified"
    fi
    rm -rf "$retention_work"
)
# --- end image tag retention -------------------------------------------------


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
# Record the channel observation only after a successful fetch. The state
# producer reads this file and its mtime, so a failed fetch can never present
# an old commit as freshly checked.
printf '%s\n' "$want" >"$STATE/source-sha.tmp"
mv "$STATE/source-sha.tmp" "$STATE/source-sha"

# A hold freezes the serving image without stopping the observation: the
# fetch above already refreshed the channel ref, so the deployment-state
# producer can still report how far behind the held artifact is. Removing the
# file resumes promotion on the next run.
if [ -f "$STATE/hold" ]; then
    log "hold: $(cat "$STATE/hold" 2>/dev/null || true)"
    exit 0
fi

if [ "$want" = "$recorded" ]; then
    serving=$(podman image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$IMAGE:latest" 2>/dev/null || true)
    if [ "$serving" = "$want" ]; then
        log "current: $want"
        if ! reconcile_image_tags; then
            warn 'retention_failed: unexpected failure, removing nothing further'
        fi
        exit 0
    fi
    # A rollback retags an older image without rewriting deployed-sha. The
    # serving label is the truth; rebuild the channel image rather than
    # reporting a deployed commit that is not actually serving.
    log "serving_mismatch: recorded $recorded serving ${serving:-unknown}"
fi

work=$(mktemp -d)
record_tmp=
candidate_record=
reference_tmp=
trap 'rm -rf "$work"; [ -z "$record_tmp" ] || rm -f "$record_tmp"; [ -z "$candidate_record" ] || rm -f "$candidate_record"; [ -z "$reference_tmp" ] || rm -f "$reference_tmp"' EXIT INT TERM
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
version=$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$work/src/ci_burst/__init__.py" 2>/dev/null | head -n 1)
set -- build --quiet --network=host --tag "$IMAGE:candidate" \
    --label "org.opencontainers.image.revision=$want" \
    --label "$OWNERSHIP_LABEL=refresh"
if [ -n "$version" ]; then
    set -- "$@" --label "org.opencontainers.image.version=$version"
fi
if ! podman "$@" "$work/src" >/dev/null; then
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

# The record is keyed by image identity, because one source SHA can be built
# more than once: a retry after an interrupted promotion is exactly that. A
# source-keyed record would let that retry replace the record of the image
# still serving. The engine generates the image ID but it becomes a path
# component, so it is normalised and validated as path-safe rather than
# sanitised: sanitising could map two different IDs onto one file and defeat
# the immutability this exists for.
raw_candidate=$candidate
candidate=$(image_identity "$raw_candidate") || {
    log "build_inputs_failed: unusable image identity: $raw_candidate"
    exit 2
}
image_key=${candidate#sha256:}

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

# The image-keyed record is the authoritative copy; the source-keyed file is a
# reference to whichever image this source most recently recorded, so a retry
# moves the reference and never the inputs. Both names are published by a
# rename inside their own directory and both are written before promotion.
#
# One image ID can be produced by more than one source commit: a commit that
# changes only files the build ignores leaves the image bytes identical. The
# record therefore carries a bounded list of attestations instead of one source
# identity, and appending one republishes the whole record atomically.
records="$STATE/builds/images"
if ! mkdir -p "$records"; then
    log "build_inputs_failed: cannot create $records"
    exit 2
fi
record="$records/$image_key.json"
reference="$STATE/builds/sha-$short.json"
candidate_record=$(mktemp "$records/.$image_key.candidate.XXXXXX") || {
    log "build_inputs_failed: cannot create a temporary candidate record"
    exit 2
}
reference_tmp=$(mktemp "$STATE/builds/.sha-$short.XXXXXX") || {
    rm -f "$candidate_record"
    candidate_record=
    log "build_inputs_failed: cannot create a temporary reference"
    exit 2
}
if ! SOURCE_SHA="$want" TREE_SHA="$tree" DOCKERFILE_SHA256="$dockerfile_sha256" \
        FROM_LINE="$from_line" BASE_DIGEST="$base_digest" IMAGE_ID="$candidate" \
        ENGINE="$engine" CREATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        python3 - "$distributions" >"$candidate_record" <<'PY'
import json
import os
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    distributions = [line.strip() for line in handle if line.strip()]

document = {
    "schema": "ci-burst.build-inputs.v2",
    "image_id": os.environ["IMAGE_ID"],
    "dockerfile_sha256": os.environ["DOCKERFILE_SHA256"],
    "from": os.environ["FROM_LINE"],
    "base_digest": os.environ["BASE_DIGEST"] or None,
    "python_distributions": distributions,
    "attestations": [
        {
            "source_sha": os.environ["SOURCE_SHA"],
            "tree_sha": os.environ["TREE_SHA"],
            "engine": os.environ["ENGINE"] or "unknown",
            "created_at": os.environ["CREATED_AT"],
        }
    ],
}
json.dump(document, sys.stdout, indent=2, sort_keys=True)
sys.stdout.write("\n")
PY
then
    rm -f "$candidate_record"
    candidate_record=
    log "build_inputs_failed: cannot render $record"
    exit 2
fi
chmod 0644 "$candidate_record"
if [ -e "$record" ]; then
    # An existing record contradicts this build only when the build inputs
    # differ. A different source commit that resolves the same inputs is not a
    # contradiction: the image ID is the same bytes, so append its attestation.
    # A source and tree already listed is the same build recorded again, and
    # an existing record at the cap refuses rather than dropping an older
    # attestation that a tag may still name.
    record_tmp=$(mktemp "$records/.$image_key.XXXXXX") || {
        rm -f "$candidate_record"
        candidate_record=
        log "build_inputs_failed: cannot create a temporary record"
        exit 2
    }
    merge_rc=0
    python3 - "$record" "$candidate_record" >"$record_tmp" <<'PY' || merge_rc=$?
import json
import sys

# The list is bounded so one record stays small and readable. A new source that
# would exceed it is refused (exit 12), not silently paid for by deleting an
# attestation an earlier reference may still name.
CAP = 8
INPUTS = (
    "image_id",
    "from",
    "base_digest",
    "dockerfile_sha256",
    "python_distributions",
)


def load(path):
    with open(path, encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or document.get("schema") != "ci-burst.build-inputs.v2":
        raise ValueError("not a build-inputs record")
    attestations = document.get("attestations")
    if not isinstance(attestations, list) or not all(
        isinstance(item, dict) for item in attestations
    ):
        raise ValueError("no attestation list")
    return document


# Statuses are deliberately not 1: an uncaught Python error exits 1, and an
# internal failure must land in the shell's named-failure branch, never be read
# as a conflict and written beside the record.
try:
    existing = load(sys.argv[1])
    candidate = load(sys.argv[2])
except (OSError, ValueError):
    raise SystemExit(2)

if any(key not in existing or key not in candidate for key in INPUTS):
    raise SystemExit(2)
if {key: existing[key] for key in INPUTS} != {key: candidate[key] for key in INPUTS}:
    raise SystemExit(10)

attestations = existing["attestations"]
new = candidate["attestations"][0]
if any(
    item.get("source_sha") == new.get("source_sha")
    and item.get("tree_sha") == new.get("tree_sha")
    for item in attestations
):
    raise SystemExit(11)
if len(attestations) >= CAP:
    raise SystemExit(12)

merged = {key: existing[key] for key in INPUTS}
merged["schema"] = existing["schema"]
merged["attestations"] = attestations + [new]
json.dump(merged, sys.stdout, indent=2, sort_keys=True)
sys.stdout.write("\n")
PY
    case "$merge_rc" in
        0)
            if ! mv -f "$record_tmp" "$record"; then
                rm -f "$record_tmp" "$candidate_record"
                record_tmp=
                candidate_record=
                log "build_inputs_failed: cannot publish $record"
                exit 2
            fi
            record_tmp=
            rm -f "$candidate_record"
            candidate_record=
            log "build_inputs_attested: $record source=$want"
            ;;
        10)
            conflict="$record.conflict"
            if ! mv -f "$candidate_record" "$conflict"; then
                rm -f "$record_tmp" "$candidate_record"
                record_tmp=
                candidate_record=
                log "build_inputs_failed: image record conflict at $record, and $conflict cannot be written"
                exit 2
            fi
            candidate_record=
            rm -f "$record_tmp"
            record_tmp=
            log "build_inputs_failed: image record conflict at $record; kept the first record, wrote $conflict"
            exit 2
            ;;
        11)
            rm -f "$record_tmp" "$candidate_record"
            record_tmp=
            candidate_record=
            log "build_inputs_kept: $record"
            ;;
        12)
            rm -f "$record_tmp" "$candidate_record"
            record_tmp=
            candidate_record=
            log "build_inputs_full: $record already attests 8 sources; prune it before recording another"
            exit 2
            ;;
        *)
            rm -f "$record_tmp" "$candidate_record"
            record_tmp=
            candidate_record=
            log "build_inputs_failed: cannot compare with the existing record at $record"
            exit 2
            ;;
    esac
else
    if ! mv -f "$candidate_record" "$record"; then
        rm -f "$candidate_record"
        candidate_record=
        log "build_inputs_failed: cannot publish $record"
        exit 2
    fi
    candidate_record=
    log "build_inputs: $record"
fi

if ! SOURCE_SHA="$want" IMAGE_ID="$candidate" RECORD="images/$image_key.json" \
        python3 - >"$reference_tmp" <<'PY'
import json
import os
import sys

document = {
    "schema": "ci-burst.build-inputs-ref.v1",
    "source_sha": os.environ["SOURCE_SHA"],
    "image_id": os.environ["IMAGE_ID"],
    "record": os.environ["RECORD"],
}
json.dump(document, sys.stdout, indent=2, sort_keys=True)
sys.stdout.write("\n")
PY
then
    rm -f "$reference_tmp"
    reference_tmp=
    log "build_inputs_failed: cannot render $reference"
    exit 2
fi
chmod 0644 "$reference_tmp"
if ! mv -f "$reference_tmp" "$reference"; then
    log "build_inputs_failed: cannot publish $reference"
    exit 2
fi
reference_tmp=
log "build_inputs_ref: $reference"

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
        raw_current=$(podman image inspect --format '{{.Id}}' "$IMAGE:latest") || {
            log "inspect_failed: $IMAGE:latest"
            exit 2
        }
        if [ -z "$raw_current" ]; then
            log "inspect_failed: $IMAGE:latest"
            exit 2
        fi
        # Normalise the same way as the candidate, so the previous tag is only
        # written when the serving image really is a different identity.
        current=$(image_identity "$raw_current") || current=$raw_current
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
if ! reconcile_image_tags; then
    warn 'retention_failed: unexpected failure, removing nothing further'
fi
