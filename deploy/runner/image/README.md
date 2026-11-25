# Worker image

The worker image is an operator-owned prerequisite. This directory carries the
recipe that reproduces it, so a rebuild starts from source rather than from an
old launch-template version.

| File | Purpose |
|---|---|
| [`provision.sh`](provision.sh) | The build recipe: OS packages, the pinned toolchain, the unprivileged runner account and a post-install inventory check. Run it as root on a fresh Debian 13 arm64 builder. |
| [`warm-cache.sh`](warm-cache.sh) | Optional warm-cache layer, run as the user data of a builder launched from the current worker image. It warms the job account's cache from a consumer's source archive and commands and prints `IMAGE_READY`. [`docs/warm-image.md`](../../../docs/warm-image.md) owns the runbook and the examples. |
| [`../cache-env.sh`](../cache-env.sh) | The one definition of the job-account cache environment. `provision.sh` installs a byte-identical copy at `/etc/ci-burst/cache-env.sh`, and a test fails when `bootstrap.sh`, `provision.sh` or `warm-cache.sh` differs from it. |
| [`../bootstrap.sh`](../bootstrap.sh) | The recorded deployment's captured runtime user data, published as its launch-template version. It installs nothing; it reads the registration token, registers one ephemeral runner, runs one job and terminates. |

## Pins

### Portable prerequisites

An independent operator needs nothing outside this directory for these:

| Input | Value |
|---|---|
| Base image | a fresh Debian 13 arm64 instance with cloud-init |
| Privilege | root on the builder; the recipe installs packages and writes under `/usr/local` |
| Egress | the builder's Debian apt repositories, `github.com`, `nodejs.org`, `code.forgejo.org`, and the Python package index (`pypi.org` and `files.pythonhosted.org`) |

The package index is an input because the recipe installs ruff with `uv tool
install`, which resolves the release from the index and downloads it from the
artifact host on a builder that has no cached wheel. Redirect targets of the
listed hosts are included — the GitHub release downloads in particular resolve
to GitHub's asset hosts — so allow the redirect host as well as the URL the
recipe names. A build that cannot reach one of these fails at that step, and
the exit trap shuts the builder down before `IMAGE_READY`.

### Inputs and pinned versions

| Item | Value |
|---|---|
| `RUFF_VERSION` | the ruff release to install, for example `0.17.4`; the build exits when it is unset |
| uv | 0.13.7 |
| node | 24.22.1 |
| promtool | 3.16.0 |
| forgejo-runner | 13.2.1 |

Set `RUFF_VERSION` to the ruff release this image should carry and record it
with the image, so a rebuild does not depend on a value held somewhere else.
The recipe refuses to guess it, because a hard-coded value is how an image
drifts from the version its jobs were tested against. The four tool downloads
are literals in [`provision.sh`](provision.sh), each beside its release
checksum.

The apt packages are not pinned: they resolve from the builder's configured
Debian repositories at build time, so a later rebuild can carry different OS
package versions even with the same tool pins. Pin a Debian snapshot
repository if the OS layer must be reproducible.

## What the image contains

This section lists the tools that a CI job may rely on in the worker image. A CI
group may rely only on the tools listed here. A tool that is not listed can be
present, for example as a dependency of a listed package, but nothing guarantees
it.

The list covers what [`provision.sh`](provision.sh) installs and what its
inventory probe requires. [Why the inventory check exists](#why-the-inventory-check-exists)
describes the probe. A job may also rely on the shell and core utilities of the
Debian 13 base, such as `bash` and the GNU core utilities, because no job runs
without them. The recipe neither installs nor checks them, so this list does not
name them one by one.

The list describes the recipe, not one built image. The recipe does not pin the
apt packages. [Build inputs](#build-inputs) records what one build resolved.

If this section and `provision.sh` disagree, `provision.sh` is right. A change
to an install step in `provision.sh` updates this section in the same change.

### Base recipe

Every number in the last column is a line of `provision.sh`. "Pin" marks the line
that sets the version.

| Tool | Installed as | Version pin | Source line |
|---|---|---|---|
| `uv`, `uvx` | pinned download | 0.13.7 | 134 (pin), 144-152 |
| `node`, `npm`, `npx` | pinned download; `npm` and `npx` come with the node release | node 24.22.1 | 136 (pin), 154-163 |
| `ruff` | package from the Python package index, installed with `uv tool install` | the `RUFF_VERSION` input; it is required and has no default and no checksum | 54-58 (pin), 172-175 |
| `promtool` (this binary only, not the Prometheus server) | pinned download | 3.16.0 | 140 (pin), 177-183 |
| `forgejo-runner` | pinned download | 13.2.1 | 138 (pin), 185-190 |
| `git`, `curl`, `rsync`, `unzip`, `xz` (`xz-utils`), `file`, `jq`, `zsh`, `bats`, `shellcheck` | package (Debian apt) | not pinned | 66 |
| `make`, `gcc`, `libc6-dev` (so that `gcc` can link) | package (Debian apt) | not pinned | 67; link check 230-237 |
| `podman` and the Quadlet generator `/usr/libexec/podman/quadlet` | package (Debian apt) | not pinned | 67; Quadlet check 223-229 |
| `python3` and its `yaml` module (`python3-yaml`); no `pip` ([warm-image.md](../../../docs/warm-image.md#pip)) | package (Debian apt) | not pinned | 68 |
| `aws` (`awscli`), `iptables`; the bootstrap uses both as root | package (Debian apt) | not pinned | 68 |
| `postgres` and `initdb` in `/usr/lib/postgresql/*/bin/`, not on `PATH`; `psql` | package (Debian apt); `psql` arrives as a dependency of `postgresql-17` | major version 17 in the package name, minor version not pinned | 69; checks 204-205, 214-222 |
| `Xvfb`, `xvfb-run` (`xvfb`) | package (Debian apt) | not pinned | 69 |
| `ca-certificates`, `zlib1g`, fonts (`fonts-*`, `xfonts-scalable`) and shared libraries (`lib*`) for a browser; no browser is installed | package (Debian apt) | not pinned | 66, 69-75 |
| `tar`, `flock`, `systemd-tmpfiles`, `dpkg-query` | Debian 13 base image; the recipe does not install them, and its probe fails the build when one is missing | not pinned | 204 |

### Optional warm layer

[`warm-cache.sh`](warm-cache.sh) installs no package and no tool. It creates the
cache directory `/var/lib/forgejo-runner/cache` (lines 324-325). It runs the
consumer's warm commands as the `runner` account (lines 327-331). The clean-up
then keeps only that directory in the runner home (lines 339-351).

What the warm commands leave in the cache is consumer-defined, so this section
lists none of it. Warm-layer contents are not on the list above, so a job may not
rely on them. [`docs/warm-image.md`](../../../docs/warm-image.md) owns the layer.

### Qualify a heavy job

To qualify a heavy job, run it once on a burst worker. A burst worker is a worker
that ci-burst launched from this image. If the job needs a tool that is not
listed, add the tool to [`provision.sh`](provision.sh) and add its row to the
table in the same change. The tool reaches workers only through a new image and a
new launch-template version ([Build](#build)).

## Deployment-specific provenance

The recipe was recovered on 2026-11-12 from the deployment's launch-template
version that built the 2026-11-10 worker image. The 2026-11-11 image is that
image plus `jq`, `podman`, `make`, `gcc` and `libc6-dev`; the two incremental
build scripts were not recorded. `provision.sh` reconstructs the full recipe,
including those additions. It does not reproduce the earlier image's warm CI
caches: a from-scratch build starts cold, which is slower but correct. The
recorded 2026-11-10 image carries ruff 0.17.3; the deployment moved to 0.17.4
afterwards.

That history explains the recorded pins; it is not a requirement on your
build. A different deployment may use another Debian revision, choose another
ruff release and publish another bootstrap, and the prerequisites above still
describe a complete build.

The [`../bootstrap.sh`](../bootstrap.sh) here is the recorded deployment's
captured user data and the exact bytes its template pins. A fork that changes
the URL, token parameter or labels is the operator's own artefact: validate it,
version it with the launch-template version that carries it, and keep it with
that deployment. This repository does not maintain a second copy.

The job-account permission and inventory-check notes below record failure modes
from the same deployment; they describe what a build must keep, not an input
value.

## Build inputs

Before `IMAGE_READY`, the recipe writes `/etc/ci-burst/build-inputs.txt` inside
the image. It records the Debian version and architecture, the executed
recipe's sha256, the ruff/uv/node/forgejo-runner/promtool versions and the
sha256 of the installed binaries (plus the declared download checksum where the
recipe verifies one), the probe counts, and the full `dpkg-query -W` package
inventory. The apt layer is not pinned, so the file records what a particular
build resolved; it does not make a later build resolve the same set.

A build that cannot write the file exits non-zero and never prints
`IMAGE_READY`, so a ready builder is one whose inputs were recorded. The file
stays inside the image; the recipe sends it nowhere. Qualification remains
operator-run: [Build](#build) step 5 checks the toolchain on a launched worker,
and this file records what was installed, not that the check passed.

## Build

1. Launch a disposable builder matching the worker architecture — a fresh
   Debian 13 arm64 instance satisfies the recipe's base-image prerequisite — in
   the deployment's subnet with
   `InstanceInitiatedShutdownBehavior=terminate` and delete-on-termination
   storage. The recipe's watchdog and EXIT trap run `shutdown -h now`; with the
   default `stop`, a finished or failed build would stop the instance and keep
   its volume instead of disappearing. The provision script needs no Forgejo
   credential and no instance profile; the worker's registration is the runtime
   bootstrap's job, after the image is published.
2. Build the user data with the ruff pin prepended, publish it, and wait for
   `IMAGE_READY` on the serial console:

   ```sh
   sed '1a export RUFF_VERSION=0.17.4' deploy/runner/image/provision.sh \
     > /tmp/worker-image-user-data.sh
   ```

   Keep the `#!/bin/bash` shebang on line 1; the inserted `export` must follow
   it, or cloud-init runs the user data with `/bin/sh` and the recipe's process
   substitution fails.

   Choose the ruff release this image should carry; the example above uses
   0.17.4, and 0.17.3 reproduces the recorded 2026-11-10 image. The script
   prints `STEP` markers and a `PROBE end have=N miss=M` line; any `MISS` fails
   the build. The 3600 s watchdog and the EXIT trap run `shutdown -h now`; with
   the launch behaviour above that terminates the builder, so snapshot it while
   it is still running.
3. Create the image without rebooting the builder:

   ```sh
   aws ec2 create-image --instance-id <builder> \
     --name "ci-burst-worker-arm64-<YYYYMMDD-HHMM>" \
     --description "ci-burst worker: Debian 13 arm64, pinned toolchain" \
     --no-reboot
   ```

4. Create a new launch-template version from the current one with the new
   `ImageId`, then move the deployment TOML's `launch_template_version` to it.
   If you keep the recorded bootstrap, the user data is unchanged:
   [`../bootstrap.sh`](../bootstrap.sh) is the published bytes. A different
   bootstrap is your own fork; see § Deployment-specific provenance.
5. Run the toolchain check from the pilot checklist on a launched worker as the
   `runner` account, and record the result with the deployment. This
   repository's CI does not qualify an image.

The image build is operator-owned. `provision.sh` does not register a runner,
read a secret, create an image or publish anything; IAM and network changes
remain operator actions.

## Warm-cache layer

[`warm-cache.sh`](warm-cache.sh) is optional. An operator who never runs it keeps
the cold image that `provision.sh` builds. It adds the package and tool caches a
consumer's jobs would download, as one more layer on a finished image;
[`docs/warm-image.md`](../../../docs/warm-image.md) owns its inputs, runbook and
examples, and this section only places it in the build flow above.

- **Same builder contract.** Launch the builder with
  `InstanceInitiatedShutdownBehavior=terminate` and delete-on-termination
  storage, with no instance profile. The script arms its lifetime timer first and
  runs `shutdown -h now` after a failed build, as `provision.sh` does, so the
  default `stop` would again keep a failed builder and its volume.
- **Same snapshot rule.** On success the script prints `IMAGE_READY` and stays up
  for a bounded snapshot window (default 1800 s, at most 3600 s) instead of
  powering off, so [Build](#build) step 3 applies unchanged: create the image
  while the builder is running. `provision.sh` idles for up to 7200 s but its
  3600 s watchdog ends the builder sooner; the warm script's window is explicit
  and its lifetime timer is the outer bound.
- **Different base.** The builder starts from the current worker image, not a
  fresh Debian instance, and the script needs no `RUFF_VERSION` or other pin.
  It runs consumer commands, so it never prints their output unframed; only the
  script prints `IMAGE_READY`.
- **Same rollout.** The warm image is published and rolled out as in [Build](#build)
  steps 3 to 5: a new launch-template version, then the deployment TOML's
  `launch_template_version`, then the toolchain check on a launched worker. A
  canary before the move is an operator obligation that the controller does not
  enforce.

## Job account permissions

`uv tool install` creates the tool environment with the build's `umask` (077 in
this recipe). The launcher is then present for root but not traversable by the
`runner` account, and a job's `shutil.which("ruff")` returns `None` while a
root inventory check reports the tool present. The recipe makes the installed
trees world-readable and executes the `ruff`/`uv`/`node` checks as `runner`; a
future edit must keep both.

## Attribution

The image ships no journal. A job's journald records stay on the worker and are
lost when it terminates, so a run visible only on burst capacity has no central
attribution record.

## Why the inventory check exists

A missing tool does not reliably fail a job. Depending on the workflow it either
fails, or skips the checks that needed it and exits green, so a green result can
validate less than it claims. The probe names the tools a heavy group executes,
the guard set the qualification tests consult, the PostgreSQL server binaries and
the Quadlet generator, and it repeats the `ruff`/`uv`/`node` checks as the
`runner` account because a root-only path is the failure mode a root probe
cannot see.

The invariant test that fails when a test's `shutil.which(...)` guard names a
tool the image does not install is a maintainer-owned `tests/` change. This
recipe makes the image's tool set explicit so that test has a source to compare
against.
