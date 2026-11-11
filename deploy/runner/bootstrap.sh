#!/bin/bash
# Launch-template user data for one ci-burst worker.
#
# This file is the repository's only copy of the bootstrap;
# docs/runner-setup.md references it instead of carrying a second copy. It runs
# as root through cloud-init, prepares the unprivileged runner account,
# registers one ephemeral Forgejo runner, blocks the metadata service for the
# job account, runs exactly one job and always terminates the instance.
#
# Replace every deployment-specific placeholder before using this file as
# template user data:
#
#   RUNNER_SHA     sha256 of the pinned release binary for ARCH; copy it from
#                  the release page, never from a copy of this file
#   RUNNER_LABELS  replace the label set with the one your deployment uses
#                  deployment configures in [worker].labels; the linux-arm64
#                  label is fixed by the arm64 image the template boots
#   AWS_REGION     your Region
#   FORGEJO_URL    your Forgejo base URL
#   TOKEN_PARAM    the SSM SecureString parameter holding the token
#
# A change here changes the template's user data: publish a new launch-template
# version. An approved version is never edited in place; docs/operations.md
# § Worker image and launch template owns that rule.
#
# The pinned runner still accepts `register` but marks it deprecated.
# docs/runner-setup.md § Runner pin and the deprecated `register` command owns
# the version boundary and the migration constraint.
set -euo pipefail
umask 077
exec > >(tee -a /dev/ttyS0) 2>&1 || true
export DEBIAN_FRONTEND=noninteractive

# Deployment-specific values (placeholders; see the header).
AWS_REGION=eu-central-1
FORGEJO_URL=https://forgejo.example.com
TOKEN_PARAM=/forgejo/runner/registration-token

# A failed bootstrap must not leave a running instance. Every exit path,
# successful or not, removes the token file and terminates the instance.
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
# Independent watchdog for a hung step. Do not ignore a failure: without it a
# hung apt or download would never reach the exit trap.
systemd-run --on-active=3600 --unit=runner-lifetime /sbin/shutdown -h now

apt-get update -y
apt-get install -y --no-install-recommends curl ca-certificates awscli iptables

# Pin the runner. Replace the checksum placeholder with the combination you
# verified from the release page for ARCH.
RUNNER_VERSION=13.2.1
RUNNER_SHA="<sha256-for-your-architecture>"
ARCH=arm64
# This pinned version still accepts `register`, and prints:
#   `register` has been deprecated; declare connections in the runner configuration instead
# A future major version may remove it, so the pin is part of the contract:
# change the version only together with a bootstrap that declares the
# connection in the runner configuration. docs/runner-setup.md owns the
# version boundary and the migration constraint.
curl -fsSL --max-time 60 -o /tmp/forgejo-runner \
  "https://code.forgejo.org/forgejo/runner/releases/download/v${RUNNER_VERSION}/forgejo-runner-${RUNNER_VERSION}-linux-${ARCH}"
echo "${RUNNER_SHA}  /tmp/forgejo-runner" | sha256sum -c -
install -m 0755 /tmp/forgejo-runner /usr/local/bin/forgejo-runner
rm -f /tmp/forgejo-runner

# Dedicated unprivileged account: jobs run here, never as root.
id runner >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin runner
install -d -o runner -g runner -m 0700 /var/lib/forgejo-runner
install -d -m 0755 /etc/forgejo-runner
cat > /etc/forgejo-runner/config.yml <<'CFG'
runner:
  file: /var/lib/forgejo-runner/.runner
  capacity: 1
cache:
  enabled: false
CFG
chown root:runner /etc/forgejo-runner/config.yml
chmod 0640 /etc/forgejo-runner/config.yml

# Pin Forgejo's name only if your DNS does not already resolve it.
# echo '192.0.2.10 forgejo.example.com' >> /etc/hosts

TOKEN_FILE=$(mktemp /run/runner-token.XXXXXX)
chmod 600 "$TOKEN_FILE"

aws ssm get-parameter --region "$AWS_REGION" --name "$TOKEN_PARAM" \
  --with-decryption --query Parameter.Value --output text > "$TOKEN_FILE"

# The advertised label set, comma-separated, in the runner's `name:executor`
# form. The part before `:` is the label ci-burst matches; `host` selects the
# host executor instead of the default Docker executor. Replace the
# deployment-specific `cloud-ci` routing label; the linux-arm64 label is fixed
# by the arm64 image and changes only with the image. Every advertised label is
# a routing claim to qualify against the image and machine before a job relies
# on it (docs/operations.md § Labels owns the rule).
RUNNER_LABELS="cloud-ci:host,strong-ci:host,linux-arm64:host"

# The stock CLI accepts the registration token only as an argument, and only
# non-interactive mode can set --ephemeral. This is the one place it is read.
# The registration finishes before the runner starts, so no job code exists
# during that window; host root and exec auditing can still observe it.
/usr/local/bin/forgejo-runner register --no-interactive \
  -c /etc/forgejo-runner/config.yml \
  --instance "$FORGEJO_URL" \
  --token "$(cat "$TOKEN_FILE")" \
  --name "burst-$(hostname)" \
  --labels "${RUNNER_LABELS}" \
  --ephemeral
cleanup_token

# The runner account needs its own registration file, not the registration token.
chown -R runner:runner /var/lib/forgejo-runner

# Job code must not be able to use the instance profile to mint credentials.
iptables -A OUTPUT -m owner --uid-owner runner -d 169.254.169.254 -j REJECT
iptables -A OUTPUT -m owner --uid-owner runner -d 169.254.170.2 -j REJECT

# One job, bound in time; shutdown happens in the EXIT trap.
install -d -o runner -g runner -m 0700 /var/lib/forgejo-runner/work
cd /var/lib/forgejo-runner
# The job must see the file modes the persistent fleet produces. This script
# raised its own umask to 077 for the credential files above and never restored
# it, so a runner started from here would create 0600/0700 where the fleet's
# systemd unit (UMask=0022) creates 0644/0755, and a mode-sensitive job would
# fail only on cloud capacity. Restore 0022 for the job itself and keep 077 for
# everything that touches the registration token.
timeout --signal=TERM --kill-after=60 3600 \
  runuser -u runner -- sh -c 'umask 0022; exec /usr/local/bin/forgejo-runner one-job -c /etc/forgejo-runner/config.yml' || true
