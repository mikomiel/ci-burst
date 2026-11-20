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
#   WARM_SRC_STRIP_COMPONENTS
#                          optional  leading path components that extraction
#                                    drops from every archive member, 0 or 1,
#                                    default 0; a forge archive wraps the
#                                    source in one directory, so it needs 1
#
# The extraction and every command run from the extracted source root as the job
# account, each in its own transient systemd unit:
#   systemd-run --wait --pipe --collect --expand-environment=no \
#     --uid=runner --gid=runner --property=KillMode=control-group \
#     env -i HOME=... PATH=... LC_ALL=C.UTF-8 <cache env> \
#     bash -c 'umask 0022; cd SRC && COMMAND'
# `env -i` inside the unit keeps root's environment, and any credential in it,
# out of consumer commands, and drops the variables systemd adds for a user
# service. --expand-environment=no stops the service manager from replacing
# ${NAME} and $NAME in the arguments before bash sees them (it would use its
# own, empty value), so a command can write "${UV_CACHE_DIR}" literally. The unit's control group ends with its main process, so a background
# process a command leaves behind dies with it; the script then fails closed
# unless no process of the job account remains, before the clean-up and again
# before the marker. The archive is extracted by the job account, never by root,
# so an archive cannot make a device node or a root-owned file. Command output
# goes to a log, never to this script's output: only this script prints
# IMAGE_READY, and only after every step passed. A failure prints a tail of the
# log with every line framed by "  | ".
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
WARM_SRC_STRIP_COMPONENTS=${WARM_SRC_STRIP_COMPONENTS-0}

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
  # LOG is empty until the inputs validate; rm diagnostics never reach the console.
  rm -rf -- "$SRC_DIR" "$ARCHIVE" >>"${LOG:-/dev/null}" 2>&1 || true
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
case $WARM_SRC_STRIP_COMPONENTS in
  0 | 1) ;;
  *) refuse "WARM_SRC_STRIP_COMPONENTS must be 0 or 1" ;;
esac
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
  *[\"\\]*) refuse "WARM_SRC_URL must not contain a quote or a backslash" ;;
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
# The archive URL, or its query, shows as "[archive-url]" (ENVIRON is literal).
print_log_tail() {
  tail -n "$TAIL_LINES" "$LOG" | LC_ALL=C tr -c '\t\n -~' '?' |
    WARM_TAIL_SECRET="$WARM_SRC_URL" LC_ALL=C awk '
      function cut(s, t,   out, k) {
        out = ""
        while (t != "" && (k = index(s, t)) > 0) {
          out = out substr(s, 1, k - 1) "[archive-url]"
          s = substr(s, k + length(t))
        }
        return out s
      }
      BEGIN {
        url = ENVIRON["WARM_TAIL_SECRET"]
        k = index(url, "?")
        query = k ? substr(url, k + 1) : ""
      }
      { print "  | " cut(cut($0, url), query) }'
}

# Run a command as the job account in its own transient unit, with the standard
# input taken from $2 and the output sent to the log. No link-local address is
# reachable, so no command can read the user data (the IPv6 metadata endpoint
# stays disabled in the build template). A unit that ends takes every
# process in its control group with it. On failure the framed tail of the log is
# the only consumer text this script prints.
unit_count=0
run_in_unit() {
  local label=$1 stdin=$2 started=$SECONDS rc=0
  shift 2
  unit_count=$((unit_count + 1))
  printf '### %s\n' "$label" >>"$LOG"
  systemd-run --wait --pipe --collect --quiet --expand-environment=no \
    --unit="warm-job-${unit_count}" --uid=runner --gid=runner \
    --property=KillMode=control-group --property=TimeoutStopSec=10 \
    --property=IPAddressDeny=link-local \
    env -i \
    HOME="$RUNNER_HOME" \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    LC_ALL=C.UTF-8 \
    "${CACHE_ENV[@]}" \
    "$@" <"$stdin" >>"$LOG" 2>&1 || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "WARM FAIL ${label} rc=${rc} secs=$((SECONDS - started)); last ${TAIL_LINES} log lines follow, framed"
    print_log_tail
    exit 1
  fi
  echo "WARM ok ${label} secs=$((SECONDS - started))"
}

run_consumer() {
  run_in_unit "$1" /dev/null bash -c 'umask 0022; cd "$1" && eval "$2"' warm-cache "$SRC_DIR" "$2"
}

# True while a process still holds the job account's uid as its real, effective,
# saved or filesystem uid. The uid is read from each status file, not from the
# owner of the process directory. A process table or an account that cannot be
# read counts as alive, so a doubt fails the build.
job_account_alive() {
  local uid status key real effective saved fsuid seen=no
  uid=$(id -u runner) || return 0
  for status in /proc/[0-9]*/status; do
    [ -r "$status" ] || continue
    while read -r key real effective saved fsuid _; do
      if [ "$key" = "Uid:" ]; then
        seen=yes
        if [ "$real" = "$uid" ] || [ "$effective" = "$uid" ] \
          || [ "$saved" = "$uid" ] || [ "$fsuid" = "$uid" ]; then
          return 0
        fi
        break
      fi
    done 2>/dev/null <"$status"
  done
  [ "$seen" = yes ] || return 0
  return 1
}

# Fail closed unless nothing of the job account runs. Every unit has ended, so
# nothing should. Look again after a short settle; if something remains, kill the
# units and look once more. A survivor could write into the image after the
# clean-up, so any survivor ends the build without the marker.
assert_job_account_idle() {
  if ! job_account_alive; then
    sleep 2
    job_account_alive || return 0
  fi
  systemctl kill --kill-whom=all --signal=SIGKILL 'warm-job-*.service' >>"$LOG" 2>&1 || true
  systemctl stop 'warm-job-*.service' >>"$LOG" 2>&1 || true
  sleep 2
  if job_account_alive; then
    echo "WARM FAIL a process of the job account is still running after its unit ended"
    exit 1
  fi
}

# The clean-up left only the cache. Used after the sweep and again at the end.
assert_clean() {
  if [ ! -d "$CACHE_DIR" ] || [ -L "$CACHE_DIR" ]; then
    echo "WARM FAIL the cache directory is missing or is not a plain directory"
    exit 1
  fi
  local remaining
  remaining=$(ls -A "$RUNNER_HOME")
  if [ "$remaining" != cache ] || [ -e "$SRC_DIR" ] || [ -e "$ARCHIVE" ]; then
    echo "WARM FAIL clean-up left something behind"
    exit 1
  fi
}

# Fetch without credentials. The URL reaches curl on stdin, never in argv;
# --disable (first) skips every curl config file, all curl output is dropped, and
# --proto keeps a redirect on https.
fetch_rc=0
printf 'url = "%s"\n' "$WARM_SRC_URL" |
  curl --disable -fsS --proto '=https' --max-time 600 -o "$ARCHIVE" --config - >/dev/null 2>&1 || fetch_rc=$?
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

# Root prepares an empty directory the job account owns, and feeds the archive on
# standard input: the job account never needs to read the root-owned archive, and
# it extracts as itself, so it cannot create a device node or keep a foreign owner.
rm -rf -- "$SRC_DIR"
install -d -m 0755 "$SRC_DIR"
chown runner:runner "$SRC_DIR"
run_in_unit extract "$ARCHIVE" tar -xzf - -C "$SRC_DIR" --no-same-owner --no-same-permissions --strip-components="$WARM_SRC_STRIP_COMPONENTS"
rm -f -- "$ARCHIVE"
install -d -m 0755 "$CACHE_DIR"
chown runner:runner "$CACHE_DIR"

index=0
for command in "${warm_commands[@]}"; do
  index=$((index + 1))
  run_consumer "warm ${index}/${#warm_commands[@]}" "$command"
done
if [ -n "$WARM_GATE_COMMAND" ]; then
  run_consumer "gate" "$WARM_GATE_COMMAND"
fi

# Nothing of the job account may run while the image is cleaned.
assert_job_account_idle

# Clean-up. Every step is fatal: an image with a leftover is not safe to create.
# The registration file lives in the runner home, so the sweep removes it too;
# the explicit removal says why. The steps are one && chain, because errexit does
# not apply inside an `if` condition, and their errors go to the log, which may
# name consumer files.
if ! { rm -rf -- "$SRC_DIR" "$ARCHIVE" \
  && rm -f -- "$RUNNER_HOME/.runner" \
  && find "$RUNNER_HOME" -mindepth 1 -maxdepth 1 ! -name cache -exec rm -rf -- {} +; } >>"$LOG" 2>&1; then
  echo "WARM FAIL clean-up; last ${TAIL_LINES} log lines follow, framed"
  print_log_tail
  exit 1
fi
assert_clean
# This removes the saved user data too, which holds the archive URL.
cloud-init clean --logs >>"$LOG" 2>&1 || {
  echo "WARM FAIL cloud-init clean; last ${TAIL_LINES} log lines follow, framed"
  print_log_tail
  exit 1
}
echo "STEP cleaned"

# Again, right before the image can be created: a process that started late must
# not have written into the image since the clean-up.
assert_job_account_idle
assert_clean

cache_size=$(du -sh "$CACHE_DIR" 2>/dev/null | cut -f1) || cache_size=unknown
echo "CACHE size=${cache_size:-unknown}"
sync
echo "WARM_SOURCE_SHA256 ${actual_sha256}"
echo "IMAGE_READY"
sleep "$WARM_SNAPSHOT_SECONDS" || true
exit 0
