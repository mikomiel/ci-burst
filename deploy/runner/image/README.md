# Worker image

The worker image is an operator-owned prerequisite. This directory carries the
recipe that reproduces it, so a rebuild starts from source rather than from an
old launch-template version.

| File | Purpose |
|---|---|
| [`provision.sh`](provision.sh) | The build recipe: OS packages, the pinned toolchain, the unprivileged runner account and a post-install inventory check. Run it as root on a fresh Debian 13 arm64 builder. |
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
