#!/bin/bash
# Warm-cache layer for the worker image. Optional: an operator who never runs
# this script keeps the cold base image.
#
# Run it as root, as the user data of a builder launched from the current
# worker image (docs/warm-image.md is the runbook). It fetches a consumer's
# source archive, verifies its sha256, runs the consumer's warm commands and an
# optional offline gate as the job account with the shared cache environment,
# removes everything but the cache, and prints IMAGE_READY. The operator then
# creates the image from the running builder. This script creates no image,
# calls no cloud API and holds no credential. It knows no consumer: every
# command comes from the inputs below.
#
# Inputs are environment variables. Never read them from the archive. Render
# them in front of this script the way deploy/runner/image/README.md renders
# RUFF_VERSION, keeping the #!/bin/bash line first:
#
#   WARM_SRC_URL           required  https URL of the .tar.gz source archive,
#                                    fetchable without credentials
#   WARM_SRC_SHA256        required  the archive's sha256, 64 hex digits
#   WARM_COMMANDS          required  warm commands, one per line; blank lines
#                                    and lines starting with # are ignored
#   WARM_GATE_COMMAND      optional  one command that proves the warm layer is
#                                    enough, run with downloads forbidden
#   WARM_LIFETIME_SECONDS  optional  builder lifetime bound, 600..14400,
#                                    default 5400
#   WARM_SNAPSHOT_SECONDS  optional  how long the builder stays up after the
#                                    marker, 60..3600, default 1800
#
# Each command runs from the extracted source root as
#   runuser -u runner -- env -i HOME=... PATH=... LC_ALL=C.UTF-8 <cache env> \
#     bash -c 'umask 0022; cd SRC && COMMAND'
# so root's environment, and any credential in it, never reaches a consumer
# command. Their stdout and stderr go to a log, never to this script's output:
# only this script prints IMAGE_READY, and only after every step passed. A
# failure prints a tail of the log with every line framed by "  | ".
#
# The builder must use InstanceInitiatedShutdownBehavior=terminate and
# delete-on-termination storage. A failed build holds briefly for the console
# and then runs `shutdown -h now`; a successful build stays up for the snapshot
# window and is not powered off here. The lifetime timer, armed first, is the
# outer bound in both cases.
set -euo pipefail
umask 077
exec > >(tee -a /dev/ttyS0) 2>&1 || true

WARM_SRC_URL=${WARM_SRC_URL:-}
WARM_SRC_SHA256=${WARM_SRC_SHA256:-}
WARM_COMMANDS=${WARM_COMMANDS:-}
WARM_GATE_COMMAND=${WARM_GATE_COMMAND:-}
WARM_LIFETIME_SECONDS=${WARM_LIFETIME_SECONDS:-5400}
WARM_SNAPSHOT_SECONDS=${WARM_SNAPSHOT_SECONDS:-1800}

DEFAULT_LIFETIME_SECONDS=5400
FAIL_HOLD_SECONDS=300
TAIL_LINES=60

RUNNER_HOME=/var/lib/forgejo-runner
CACHE_DIR=$RUNNER_HOME/cache
# Outside the runner home, so the clean-up that empties the home cannot be the
# only thing that removes consumer source.
SRC_DIR=/var/tmp/warm-src
ARCHIVE=/var/tmp/warm-src.tar.gz
LOG_TEMPLATE=/run/warm-cache.XXXXXX

# The shared cache environment. deploy/runner/cache-env.sh owns the values and
# tests/test_cache_environment.py fails when this copy differs: this script runs
# as lone user data on a builder whose image may not carry that file.
CACHE_ENV=(
  XDG_CACHE_HOME=/var/lib/forgejo-runner/cache
  UV_CACHE_DIR=/var/lib/forgejo-runner/cache/uv
  NPM_CONFIG_CACHE=/var/lib/forgejo-runner/cache/npm
  npm_config_cache=/var/lib/forgejo-runner/cache/npm
  PIP_CACHE_DIR=/var/lib/forgejo-runner/cache/pip
  CI_PROFILE_CACHE_ROOT=/var/lib/forgejo-runner/cache/validation
)

LOG=

# A failed build must not leave a running instance, and a successful build must
# stay up. Status 0 is reached only after the snapshot window, so the trap powers
# off on failure alone and prints nothing on success: the marker stays the last
# output.
on_exit() {
  rc=$?
  trap - EXIT
  rm -rf -- "$SRC_DIR" "$ARCHIVE" || true
  if [ "$rc" -ne 0 ]; then
    echo "warm exiting rc=$rc; no image may be created from this builder"
    echo "warm failed; holding ${FAIL_HOLD_SECONDS} s so the console can be read"
    sleep "$FAIL_HOLD_SECONDS" || true
    sync || true
    shutdown -h now
  fi
}
trap on_exit EXIT

in_range() {
  case $1 in
    '' | *[!0-9]*) return 1 ;;
  esac
  [ "${#1}" -le 6 ] && [ "$1" -ge "$2" ] && [ "$1" -le "$3" ]
}

# Armed before anything else, so no later step can leave a builder running. An
# invalid value still gets the default timer, and the refusal below follows.
timer_seconds=$DEFAULT_LIFETIME_SECONDS
if in_range "$WARM_LIFETIME_SECONDS" 600 14400; then
  timer_seconds=$WARM_LIFETIME_SECONDS
fi
systemd-run --on-active="$timer_seconds" --unit=warm-build-lifetime /sbin/shutdown -h now

refuse() {
  echo "warm: refused: $1" >&2
  exit 2
}

# Refusals come before the first consumer command and before any fetch.
in_range "$WARM_LIFETIME_SECONDS" 600 14400 || refuse "WARM_LIFETIME_SECONDS must be a whole number from 600 to 14400"
in_range "$WARM_SNAPSHOT_SECONDS" 60 3600 || refuse "WARM_SNAPSHOT_SECONDS must be a whole number from 60 to 3600"
[ -n "$WARM_SRC_SHA256" ] || refuse "WARM_SRC_SHA256 is required"
expected_sha256=${WARM_SRC_SHA256,,}
[[ "$expected_sha256" =~ ^[0-9a-f]{64}$ ]] || refuse "WARM_SRC_SHA256 must be 64 hex digits"
[ -n "$WARM_SRC_URL" ] || refuse "WARM_SRC_URL is required"
case $WARM_SRC_URL in
  https://*) ;;
  *) refuse "WARM_SRC_URL must be an https URL" ;;
esac
case $WARM_SRC_URL in
  *[[:space:]]*) refuse "WARM_SRC_URL must not contain whitespace" ;;
esac
warm_commands=()
while IFS= read -r line; do
  trimmed=${line#"${line%%[![:space:]]*}"}
  case $trimmed in
    '' | '#'*) continue ;;
  esac
  warm_commands+=("$line")
done <<<"$WARM_COMMANDS"
[ "${#warm_commands[@]}" -gt 0 ] || refuse "WARM_COMMANDS is empty"
echo "STEP inputs ok commands=${#warm_commands[@]} gate=$([ -n "$WARM_GATE_COMMAND" ] && echo yes || echo no)"

LOG=$(mktemp "$LOG_TEMPLATE")

# Print the end of the log with every line framed. Every byte that is not a tab,
# a newline or printable ASCII becomes "?" first, so no consumer byte (a carriage
# return, a form feed, an escape sequence, a multi-byte line separator) can start
# a console line of its own. The frame is then the only way a line begins.
print_log_tail() {
  tail -n "$TAIL_LINES" "$LOG" | LC_ALL=C tr -c '\t\n -~' '?' | awk '{ print "  | " $0 }'
}

# Run one consumer command as the job account. Its output goes to the log. On
# failure the framed tail of the log is the only consumer text this script prints.
run_consumer() {
  local label=$1 command=$2 started=$SECONDS rc=0
  printf '### %s\n' "$label" >>"$LOG"
  runuser -u runner -- env -i \
    HOME="$RUNNER_HOME" \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LC_ALL=C.UTF-8 \
    "${CACHE_ENV[@]}" \
    bash -c 'umask 0022; cd "$1" && eval "$2"' warm-cache "$SRC_DIR" "$command" \
    >>"$LOG" 2>&1 </dev/null || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "WARM FAIL ${label} rc=${rc} secs=$((SECONDS - started)); last ${TAIL_LINES} log lines follow, framed"
    print_log_tail
    exit 1
  fi
  echo "WARM ok ${label} secs=$((SECONDS - started))"
}

# Fetch without credentials; the URL is never echoed. The error text of curl is
# dropped for the same reason, and --proto keeps a redirect on https.
fetch_rc=0
curl -fsS --proto '=https' --max-time 600 -o "$ARCHIVE" "$WARM_SRC_URL" 2>/dev/null || fetch_rc=$?
if [ "$fetch_rc" -ne 0 ]; then
  echo "WARM FAIL fetch rc=${fetch_rc}"
  exit 1
fi
actual_sha256=$(sha256sum "$ARCHIVE" | awk '{print $1}')
if [ "$actual_sha256" != "$expected_sha256" ]; then
  echo "WARM FAIL digest mismatch: expected ${expected_sha256} got ${actual_sha256}"
  exit 1
fi
echo "STEP source verified sha256=${actual_sha256}"

rm -rf -- "$SRC_DIR"
install -d -m 0755 "$SRC_DIR"
tar -xzf "$ARCHIVE" -C "$SRC_DIR" --no-same-owner
rm -f -- "$ARCHIVE"
chown -R runner:runner "$SRC_DIR"
install -d -m 0755 "$CACHE_DIR"
chown runner:runner "$CACHE_DIR"
echo "STEP extracted"

index=0
for command in "${warm_commands[@]}"; do
  index=$((index + 1))
  run_consumer "warm ${index}/${#warm_commands[@]}" "$command"
done
if [ -n "$WARM_GATE_COMMAND" ]; then
  run_consumer "gate" "$WARM_GATE_COMMAND"
fi

# Clean-up. Every step is fatal: an image with a leftover is not safe to create.
# The registration file lives in the runner home, so the sweep removes it too;
# the explicit removal says why.
rm -rf -- "$SRC_DIR" "$ARCHIVE"
rm -f -- "$RUNNER_HOME/.runner"
find "$RUNNER_HOME" -mindepth 1 -maxdepth 1 ! -name cache -exec rm -rf -- {} +
if [ ! -d "$CACHE_DIR" ] || [ -L "$CACHE_DIR" ]; then
  echo "WARM FAIL the cache directory is missing or is not a plain directory"
  exit 1
fi
remaining=$(ls -A "$RUNNER_HOME")
if [ "$remaining" != cache ] || [ -e "$SRC_DIR" ] || [ -e "$ARCHIVE" ]; then
  echo "WARM FAIL clean-up left something behind"
  exit 1
fi
# This removes the saved user data too, which holds the archive URL.
cloud-init clean --logs >>"$LOG" 2>&1 || {
  echo "WARM FAIL cloud-init clean; last ${TAIL_LINES} log lines follow, framed"
  print_log_tail
  exit 1
}
echo "STEP cleaned"

cache_size=$(du -sh "$CACHE_DIR" 2>/dev/null | cut -f1) || cache_size=unknown
echo "CACHE size=${cache_size:-unknown}"
sync
echo "WARM_SOURCE_SHA256 ${actual_sha256}"
echo "IMAGE_READY"
sleep "$WARM_SNAPSHOT_SECONDS" || true
exit 0
