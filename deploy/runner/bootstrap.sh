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
on_exit() {
  rc=$?
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
chown -R runner:runner /var/lib/forgejo-runner
echo "STEP register"

iptables -A OUTPUT -m owner --uid-owner runner -d 169.254.169.254 -j REJECT
iptables -A OUTPUT -m owner --uid-owner runner -d 169.254.170.2 -j REJECT

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
