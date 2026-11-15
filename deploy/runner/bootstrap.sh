#!/bin/bash
# Worker from the baked image: no package installs, no toolchain downloads.
set -euo pipefail
umask 077
exec > >(tee -a /dev/ttyS0) 2>&1 || true

TOKEN_FILE=""
cleanup_token() {
  if [ -n "$TOKEN_FILE" ] && [ -e "$TOKEN_FILE" ]; then
    shred -u "$TOKEN_FILE" 2>/dev/null || rm -f "$TOKEN_FILE"
  fi
}
# The capture holds a bounded copy of each poll's output, for classification only.
# A failed removal must not stop the exit trap before sync and shutdown.
CAPTURE=""
cleanup_capture() {
  if [ -n "$CAPTURE" ]; then
    rm -f "$CAPTURE" || true
  fi
}
SPOT_WATCH_PID=""
on_exit() {
  rc=$?
  if [ -n "${SPOT_WATCH_PID}" ]; then
    kill "$SPOT_WATCH_PID" 2>/dev/null || true
  fi
  echo "bootstrap exiting rc=$rc"
  cleanup_token
  cleanup_capture
  sync
  shutdown -h now
}
trap on_exit EXIT
systemd-run --on-active=3600 --unit=runner-lifetime /sbin/shutdown -h now
# A worker that gets no job ends itself: it polls for a task for the claim window,
# which starts when register returns, and shuts down when the window passes with
# none. These are single literals, not configuration. The status and the message
# are the pinned runner's own empty-poll result (see docs/runner-setup.md); a new
# runner version needs new values here. The capture bound limits what is read from
# a poll, never what the runner may write.
CLAIM_WINDOW_SECONDS=300
POLL_INTERVAL_SECONDS=10
NO_TASK_STATUS=2
NO_TASK_MESSAGE='Error: no task received'
CAPTURE_LIMIT_BYTES=65536

mount -o remount,noatime / || echo "WARNING: noatime remount refused"
echo "STEP mount-options"

# A baked image must never carry a previous registration.
rm -f /var/lib/forgejo-runner/.runner

PARAM=/ci-burst/runner/registration-token
TOKEN_FILE=$(mktemp /run/runner-token.XXXXXX)
chmod 600 "$TOKEN_FILE"
aws ssm get-parameter --region eu-central-1 --name "$PARAM" \
  --with-decryption --query Parameter.Value --output text > "$TOKEN_FILE"

/usr/local/bin/forgejo-runner register --no-interactive \
  -c /etc/forgejo-runner/config.yml \
  --instance https://forgejo.kiciateam.eu \
  --token "$(cat "$TOKEN_FILE")" \
  --name "burst-$(hostname)" \
  --labels cloud-ci:host,strong-ci:host,linux-arm64:host \
  --ephemeral
CLAIM_DEADLINE=$(( $(date +%s) + CLAIM_WINDOW_SECONDS ))
cleanup_token
# The registration id is read now, while the file is still root-owned and before
# the job account can touch it. Reading it at notice time would let job code
# replace the file with a FIFO and block the watcher forever. A file over 4096
# bytes is rejected rather than truncated, so a malformed suffix cannot hide
# behind the bound. The id is published in the marker; the file also holds the
# registration token, which is never read or printed.
RUNNER_FILE=/var/lib/forgejo-runner/.runner
RUNNER_SIZE=$(stat -c '%s' "$RUNNER_FILE" 2>/dev/null) || RUNNER_SIZE=""
RUNNER_UUID=""
if [ -n "$RUNNER_SIZE" ] && [ "$RUNNER_SIZE" -le 4096 ]; then
  RUNNER_UUID=$(jq -ser 'if length == 1
               and (.[0] | type) == "object"
               and (.[0].uuid | type) == "string"
               and (.[0].uuid | test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"))
               and (.[0].uuid | test("[\n\r]") | not)
             then .[0].uuid
             else empty
             end' "$RUNNER_FILE" 2>/dev/null) || RUNNER_UUID=""
fi
RUNNER_UUID=${RUNNER_UUID:-}
if [[ ! "$RUNNER_UUID" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  RUNNER_UUID=unknown
fi
chown -R runner:runner /var/lib/forgejo-runner
echo "STEP register"

iptables -A OUTPUT -m owner --uid-owner runner -d 169.254.169.254 -j REJECT
iptables -A OUTPUT -m owner --uid-owner runner -d 169.254.170.2 -j REJECT

# AWS sends a two-minute notice before it reclaims a Spot instance. The job
# account cannot reach the metadata service (the REJECT rules above), so root
# reads the notice here. On a notice the worker publishes one fixed marker on
# this console and then tries to end the one job within a bounded window; the
# controller turns that marker into a worker-event. The marker says the notice
# was observed, not that the job ended: a successful signal does not prove
# termination, and the marker is printed before the first attempt. It is
# deliberately not a job step: the head controls its own steps, so the evidence
# has to come from the worker.
#
# A worker that cannot read the notice behaves exactly as before - the instance
# lifetime and the job timeout still bound it - so a failed metadata read is not
# fatal.
end_one_job() {
  local cmdline pid signalled=1
  for cmdline in /proc/[0-9]*/cmdline; do
    [ -r "$cmdline" ] || continue
    pid=${cmdline#/proc/}
    pid=${pid%/cmdline}
    [ "$(stat -c '%U' "/proc/$pid" 2>/dev/null)" = runner ] || continue
    grep -qa 'forgejo-runner' "$cmdline" 2>/dev/null || continue
    if kill -TERM "$pid" 2>/dev/null; then
      signalled=0
    fi
  done
  return "$signalled"
}
spot_watch() {
  local token body action host
  host=$(hostname)
  while :; do
    token=$(curl -fsS --max-time 2 -X PUT \
      -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
      http://169.254.169.254/latest/api/token) || token=""
    if [ -n "$token" ]; then
      if body=$(curl -fsS --max-time 2 -H "X-aws-ec2-metadata-token: $token" \
        http://169.254.169.254/latest/meta-data/spot/instance-action); then
        action=$(printf '%s' "$body" | jq -r '.action // empty' 2>/dev/null) || action=""
        case "$action" in
          terminate|stop|hibernate) ;;
          *) action=unknown ;;
        esac
        echo "bootstrap spot-interruption action=$action host=$host runner=$RUNNER_UUID"
        # The job may not be running yet: the watcher starts before it. Keep
        # trying until the runner exists, so a notice that arrives during setup
        # still attempts to end the job instead of attempting nothing. The bound
        # is the notice window, and a runner that never starts ends with the
        # instance.
        for _ in $(seq 1 120); do
          if end_one_job; then
            return 0
          fi
          sleep 1
        done
        return 0
      fi
    fi
    sleep 5
  done
}
spot_watch &
SPOT_WATCH_PID=$!

install -d -o runner -g runner -m 0700 /var/lib/forgejo-runner/work
cd /var/lib/forgejo-runner
umask 0022
# 0022 matches the persistent fleet's systemd UMask; the 077 above protects only the token
JOB_RC=0
# One poll runs the runner's one-job mode without its wait option: it asks the
# queue once and returns at once when no task is queued, so an idle worker never
# blocks in it. The job timeout covers the poll, not the pause between polls.
run_poll() {
  timeout --signal=TERM --kill-after=60 3600 \
    runuser -u runner -- env \
      HOME=/var/lib/forgejo-runner \
      XDG_CACHE_HOME=/var/lib/forgejo-runner/cache \
      UV_CACHE_DIR=/var/lib/forgejo-runner/cache/uv \
      NPM_CONFIG_CACHE=/var/lib/forgejo-runner/cache/npm \
      npm_config_cache=/var/lib/forgejo-runner/cache/npm \
      PIP_CACHE_DIR=/var/lib/forgejo-runner/cache/pip \
      CI_PROFILE_CACHE_ROOT=/var/lib/forgejo-runner/cache/validation \
      /usr/local/bin/forgejo-runner one-job -c /etc/forgejo-runner/config.yml
}
# True while a process still holds the job account's uid as its real, effective,
# saved or filesystem uid. The uid is read from each status file, not taken from
# the owner of the process directory: a process that clears its dumpable flag has
# a root-owned directory but keeps its own credentials. A process table that
# cannot be read at all counts as alive, so a doubt ends the claim loop.
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
    done 2>/dev/null < "$status"
  done
  [ "$seen" = yes ] || return 0
  return 1
}
# A poll is empty only when it ended with the documented status, its capture holds
# the documented message as a complete line (the message and one line feed, no
# other byte), and no process of the job account is left. The status must be from
# 2 to 123 because the runner of a claimed job can also end with 0, 1, 124 or a
# status of 128 or more. The capture is read here, after the pipeline has ended.
# Every other outcome, including a capture that is missing, oversized or
# unreadable, is a finished attempt: a doubt ends the loop and never extends it.
poll_is_empty() {
  local status=$1 size data LC_ALL=C
  [ "$status" -ge 2 ] && [ "$status" -le 123 ] || return 1
  [ "$status" = "$NO_TASK_STATUS" ] || return 1
  size=$(stat -c '%s' "$CAPTURE" 2>/dev/null) || return 1
  [ "$size" -le "$CAPTURE_LIMIT_BYTES" ] || return 1
  if job_account_alive; then
    return 1
  fi
  data=$(cat "$CAPTURE" 2>/dev/null && printf x) || return 1
  data=${data%x}
  # A byte count that differs from the file size means the read dropped a NUL.
  [ "${#data}" -eq "$size" ] || return 1
  case $'\n'"$data" in
    *$'\n'"$NO_TASK_MESSAGE"$'\n'*) return 0 ;;
  esac
  return 1
}
# The runner account owns this directory, so job code can replace the
# registration file with something `rm -f` refuses. The removal must not
# decide the exit status: under `set -e` it would exit here, skipping the
# one-job marker and reporting this cleanup's failure as the job result.
remove_registration() {
  rm -f /var/lib/forgejo-runner/.runner || echo "WARNING: runner registration file not removed"
}
# An unreadable clock counts as a passed deadline, so a doubt ends the loop.
claim_deadline_passed() {
  ! [ "$(date +%s)" -lt "$CLAIM_DEADLINE" ]
}
# The claim window passed with no task. No job ran, so this path prints neither the
# job step nor a one-job marker, and it never returns into the code after the loop.
# It leaves through the exit trap with status 0, so a window that ended on schedule
# does not read as a failure.
claim_timeout() {
  echo "STEP claim-timeout"
  remove_registration
  exit 0
}
# The claim loop. Descriptor 9 is a copy of the console. Each poll's output goes
# to the console while the poll runs, and to a capture that is read once the poll
# has ended. tee keeps draining after the capture reaches its bound, so the
# capture never stops or blocks the runner, and the runner does not inherit the
# copy. The poll's own status is the first pipeline status. The deadline is
# checked before the first poll, after an empty poll and again after the pause,
# never during a poll.
exec 9>&1
CAPTURE=$(mktemp /run/runner-capture.XXXXXX) || CAPTURE=""
claim_deadline_passed && claim_timeout
while :; do
  POLL_STATUSES=(0)
  run_poll 9>&- 2>&1 \
    | tee -pa /dev/fd/9 \
    | head -c "$((CAPTURE_LIMIT_BYTES + 1))" > "${CAPTURE:-/dev/null}" \
    || POLL_STATUSES=("${PIPESTATUS[@]}")
  JOB_RC=${POLL_STATUSES[0]:-1}
  poll_is_empty "$JOB_RC" || break
  claim_deadline_passed && claim_timeout
  sleep "$POLL_INTERVAL_SECONDS"
  claim_deadline_passed && claim_timeout
done
echo "STEP job"
remove_registration
case "$JOB_RC" in
  0) echo "bootstrap one-job completed rc=0" ;;
  124|137) echo "bootstrap one-job timeout rc=$JOB_RC" ;;
  *) echo "bootstrap one-job failed rc=$JOB_RC" ;;
esac
exit "$JOB_RC"
