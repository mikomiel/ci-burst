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
SPOT_WATCH_PID=""
on_exit() {
  rc=$?
  if [ -n "${SPOT_WATCH_PID}" ]; then
    kill "$SPOT_WATCH_PID" 2>/dev/null || true
  fi
  echo "bootstrap exiting rc=$rc"
  cleanup_token
  sync
  shutdown -h now
}
trap on_exit EXIT
systemd-run --on-active=3600 --unit=runner-lifetime /sbin/shutdown -h now

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
cleanup_token
# The registration id is read now, while the file is still root-owned and before
# the job account can touch it. Reading it at notice time would let job code
# replace the file with a FIFO and block the watcher forever, and the bound keeps
# an oversized file from being parsed. The id is published in the marker; the
# file also holds the registration token, which is never read or printed.
RUNNER_UUID=$(head -c 4096 /var/lib/forgejo-runner/.runner 2>/dev/null \
  | jq -ser 'if length == 1
               and (.[0] | type) == "object"
               and (.[0].uuid | type) == "string"
               and (.[0].uuid | test("^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"))
               and (.[0].uuid | test("[\n\r]") | not)
             then .[0].uuid
             else empty
             end' 2>/dev/null) || RUNNER_UUID=""
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
# this console and ends the one job; the controller turns that marker into a
# worker-event. The marker is deliberately not a job step: the head controls
# its own steps, so the evidence has to come from the worker.
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
        # still ends the job instead of signalling nothing. The bound is the
        # notice window, and a runner that never starts ends with the instance.
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
timeout --signal=TERM --kill-after=60 3600 \
  runuser -u runner -- env \
    HOME=/var/lib/forgejo-runner \
    XDG_CACHE_HOME=/var/lib/forgejo-runner/cache \
    UV_CACHE_DIR=/var/lib/forgejo-runner/cache/uv \
    NPM_CONFIG_CACHE=/var/lib/forgejo-runner/cache/npm \
    npm_config_cache=/var/lib/forgejo-runner/cache/npm \
    PIP_CACHE_DIR=/var/lib/forgejo-runner/cache/pip \
    CI_PROFILE_CACHE_ROOT=/var/lib/forgejo-runner/cache/validation \
    /usr/local/bin/forgejo-runner one-job --wait -c /etc/forgejo-runner/config.yml || JOB_RC=$?
echo "STEP job"
# The runner account owns this directory, so job code can replace the
# registration file with something `rm -f` refuses. The removal must not
# decide the exit status: under `set -e` it would exit here, skipping the
# one-job marker and reporting this cleanup's failure as the job result.
rm -f /var/lib/forgejo-runner/.runner || echo "WARNING: runner registration file not removed"
case "$JOB_RC" in
  0) echo "bootstrap one-job completed rc=0" ;;
  124|137) echo "bootstrap one-job timeout rc=$JOB_RC" ;;
  *) echo "bootstrap one-job failed rc=$JOB_RC" ;;
esac
exit "$JOB_RC"
