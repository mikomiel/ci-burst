#!/bin/bash
# Worker image build recipe.
#
# Run this as root on a fresh Debian 13 arm64 builder. It installs the OS
# packages and the pinned CI toolchain, prepares the unprivileged runner
# account, verifies the result on the serial console and then idles so the
# operator can create the image. It does not register a runner, read a secret
# or publish an image.
#
# The builder must use InstanceInitiatedShutdownBehavior=terminate: the EXIT
# trap and the 3600 s watchdog run `shutdown -h now`, and with the default
# `stop` that retains the instance and its storage after a finished or failed
# build.
#
# Provenance: recovered on 2026-11-12 from the deployment's launch-template
# version that built the 2026-11-10 worker image. The
# 2026-11-11 image is that image plus jq, podman, make, gcc and libc6-dev.
# The two incremental build scripts were not recorded, so this is a
# reconstruction of the full recipe, not a byte-for-byte capture. The warm CI
# caches in the earlier image are not reproduced; a from-scratch build starts
# cold.
#
# Every pin below is a deliberate value, not a "latest". A change is a new
# image and a new launch-template version, never an in-place edit.
set -euo pipefail
umask 077
exec > >(tee -a /dev/ttyS0) 2>&1 || true
export DEBIAN_FRONTEND=noninteractive

# A failed build must not leave a running instance. A successful build reaches
# the sleep below, which is the operator's window for the snapshot.
on_exit() {
  rc=$?
  echo "provision exiting rc=$rc"
  sync
  shutdown -h now
}
trap on_exit EXIT
systemd-run --on-active=3600 --unit=image-build-lifetime /sbin/shutdown -h now

mount -o remount,noatime / || echo "WARNING: noatime remount refused"

# Required input: the persistent fleet's ruff pin. The build fails rather than
# guessing, because a literal here is how the image drifted from the source
# pin. Read it from the deployment repository's ruff_version value.
RUFF_VERSION="${RUFF_VERSION:-}"
if [ -z "${RUFF_VERSION}" ]; then
  echo "provision: RUFF_VERSION must be set to the persistent fleet's ruff_version (ansible-homelab/inventory/group_vars/all.yml)" >&2
  exit 1
fi

apt-get update -y
# libc6-dev is explicit on purpose. --no-install-recommends suppresses the
# recommendation that would supply it, and gcc alone then fails to link with
# "cannot find Scrt1.o". jq, podman, make and gcc are the 2026-11-11 additions
# to the 2026-11-10 set.
apt-get install -y --no-install-recommends \
  ca-certificates curl git rsync unzip xz-utils file bats shellcheck zsh jq \
  make gcc libc6-dev podman \
  python3 python3-yaml iptables awscli \
  postgresql-17 zlib1g xvfb fonts-noto-color-emoji fonts-unifont libfontconfig1 \
  libfreetype6 xfonts-scalable fonts-liberation fonts-ipafont-gothic \
  fonts-wqy-zenhei fonts-tlwg-loma-otf fonts-freefont-ttf libasound2t64 \
  libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libcups2t64 \
  libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 \
  libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
  libxkbcommon0 libxrandr2

# The unprivileged account jobs run as. A baked image must never carry a
# previous registration; the runtime bootstrap removes any stale file before
# it registers, and this build never creates one.
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
rm -f /var/lib/forgejo-runner/.runner

# The pinned toolchain. Checksums are the release artifacts this recipe was
# verified against; update one only with a new image.
UV_VERSION=0.13.7
UV_SHA=71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc
NODE_VERSION=24.22.1
NODE_SHA=dbabf7a1cbf947e6059ffad2c27b80041db97dea1e553b62afdee7f13fca336e
RUNNER_VERSION=13.2.1
RUNNER_SHA=eeddb7902f79abe488972f4189fbe3ea02762ee827d1b406b469420bf0d62dd9
PROMTOOL_VERSION=3.16.0
PROMTOOL_SHA=fd8571b8162f4020232d886f17340bcf92e1722985ae3b4c1d096157826c0fbe
ARCH=arm64

curl -fsSL --max-time 60 -o /tmp/uv.tar.gz \
  "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${ARCH/arm64/aarch64}-unknown-linux-gnu.tar.gz"
echo "${UV_SHA}  /tmp/uv.tar.gz" | sha256sum -c -
install -d /usr/local/lib/uv-toolchain
tar -xzf /tmp/uv.tar.gz -C /usr/local/lib/uv-toolchain --strip-components=1
ln -sf /usr/local/lib/uv-toolchain/uv /usr/local/bin/uv
ln -sf /usr/local/lib/uv-toolchain/uvx /usr/local/bin/uvx
rm -f /tmp/uv.tar.gz
echo "STEP uv"

curl -fsSL --max-time 120 -o /tmp/node.tar.xz \
  "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${ARCH}.tar.xz"
echo "${NODE_SHA}  /tmp/node.tar.xz" | sha256sum -c -
install -d /usr/local/lib/node-toolchain
tar -xJf /tmp/node.tar.xz -C /usr/local/lib/node-toolchain --strip-components=1
ln -sf /usr/local/lib/node-toolchain/bin/node /usr/local/bin/node
ln -sf /usr/local/lib/node-toolchain/bin/npm /usr/local/bin/npm
ln -sf /usr/local/lib/node-toolchain/bin/npx /usr/local/bin/npx
rm -f /tmp/node.tar.xz
echo "STEP node"

# Both directories, never only the bin directory: the launcher is a symlink
# into the tool environment, so an environment only root can traverse is a
# launcher the job account cannot execute. `uv tool install` honours the
# build's umask (077 here) and creates that environment 0700 root, which is
# exactly the "present to root, absent to the job" shape the runner check
# below exists to catch. This is the same lesson as the persistent fleet's
# uv-tool install.
install -d -m 0755 /usr/local/lib/uv-tools
UV_TOOL_DIR=/usr/local/lib/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin \
  uv tool install "ruff==${RUFF_VERSION}"
echo "STEP ruff"

curl -fsSL --max-time 120 -o /tmp/prometheus.tar.gz \
  "https://github.com/prometheus/prometheus/releases/download/v${PROMTOOL_VERSION}/prometheus-${PROMTOOL_VERSION}.linux-${ARCH}.tar.gz"
echo "${PROMTOOL_SHA}  /tmp/prometheus.tar.gz" | sha256sum -c -
tar -xzf /tmp/prometheus.tar.gz -C /tmp
install -m 0755 "/tmp/prometheus-${PROMTOOL_VERSION}.linux-${ARCH}/promtool" /usr/local/bin/promtool
rm -rf /tmp/prometheus.tar.gz "/tmp/prometheus-${PROMTOOL_VERSION}.linux-${ARCH}"
echo "STEP promtool"

curl -fsSL --max-time 60 -o /tmp/forgejo-runner \
  "https://code.forgejo.org/forgejo/runner/releases/download/v${RUNNER_VERSION}/forgejo-runner-${RUNNER_VERSION}-linux-${ARCH}"
echo "${RUNNER_SHA}  /tmp/forgejo-runner" | sha256sum -c -
install -m 0755 /tmp/forgejo-runner /usr/local/bin/forgejo-runner
rm -f /tmp/forgejo-runner
echo "STEP runner"

# The build runs with umask 077 to protect the eventual registration token.
# The tool environments must still be traversable by the job account, so make
# the installed trees world-readable and set the execute bit on directories and
# executables only.
chmod -R a+rX /usr/local/lib/uv-toolchain /usr/local/lib/node-toolchain /usr/local/lib/uv-tools

# Inventory check. The job toolchain is not optional: a missing tool either
# fails the job or makes it skip checks and exit green. The guard set is the
# one the repository's qualification tests consult, plus the tools the heavy
# groups execute.
have=0
miss=0
for tool in jq zsh ruff tar curl flock systemd-tmpfiles dpkg-query \
            uv node npm python3 git make gcc podman promtool forgejo-runner psql; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "HAVE $tool"
    have=$((have + 1))
  else
    echo "MISS $tool"
    miss=$((miss + 1))
  fi
done
for binary in /usr/lib/postgresql/*/bin/postgres /usr/lib/postgresql/*/bin/initdb; do
  if [ -x "$binary" ]; then
    echo "HAVE $binary"
    have=$((have + 1))
  else
    echo "MISS $binary"
    miss=$((miss + 1))
  fi
done
if [ -x /usr/libexec/podman/quadlet ]; then
  echo "HAVE quadlet"
  have=$((have + 1))
else
  echo "MISS quadlet"
  miss=$((miss + 1))
fi
if printf 'int main(void) { return 0; }\n' >/tmp/link-probe.c \
   && gcc -o /tmp/link-probe /tmp/link-probe.c; then
  echo "HAVE gcc-link"
  have=$((have + 1))
else
  echo "MISS gcc-link"
  miss=$((miss + 1))
fi
rm -f /tmp/link-probe.c /tmp/link-probe

# The checks above run as root. These repeat the ones whose failure mode is a
# root-owned path the job account cannot traverse.
for tool in ruff uv node; do
  if runuser -u runner -- "/usr/local/bin/${tool}" --version >/dev/null 2>&1; then
    echo "HAVE ${tool}-as-runner"
    have=$((have + 1))
  else
    echo "MISS ${tool}-as-runner"
    miss=$((miss + 1))
  fi
done

# The runtime bootstrap starts the runner with HOME and the cache variables
# set and the job's umask at 0022. Reproduce that shape and check PATH-based
# discovery, which is what a test's shutil.which("ruff") actually uses; the
# absolute-path checks above cannot see a PATH that omits /usr/local/bin.
if runuser -u runner -- env \
     HOME=/var/lib/forgejo-runner \
     PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
     XDG_CACHE_HOME=/var/lib/forgejo-runner/cache \
     UV_CACHE_DIR=/var/lib/forgejo-runner/cache/uv \
     NPM_CONFIG_CACHE=/var/lib/forgejo-runner/cache/npm \
     PIP_CACHE_DIR=/var/lib/forgejo-runner/cache/pip \
     CI_PROFILE_CACHE_ROOT=/var/lib/forgejo-runner/cache/validation \
     bash -c 'umask 0022; python3 -c "import shutil,sys; sys.exit(0 if shutil.which(\"ruff\") else 1)"' \
     >/dev/null 2>&1; then
  echo "HAVE ruff-on-job-path"
  have=$((have + 1))
else
  echo "MISS ruff-on-job-path"
  miss=$((miss + 1))
fi
echo "PROBE end have=${have} miss=${miss}"
if [ "$miss" -ne 0 ]; then
  echo "provision: missing required tools; fix the image before snapshotting" >&2
  exit 1
fi

echo "IMAGE_READY"
sync
sleep 7200
