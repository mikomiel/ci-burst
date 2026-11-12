# Worker image

The worker image is an operator-owned prerequisite. This directory carries the
recipe that reproduces it, so a rebuild starts from source rather than from an
old launch-template version.

| File | Purpose |
|---|---|
| [`provision.sh`](provision.sh) | The build recipe: OS packages, the pinned toolchain, the unprivileged runner account and a post-install inventory check. Run it as root on a fresh Debian 13 arm64 builder. |
| [`../bootstrap.sh`](../bootstrap.sh) | The runtime user data published as the launch-template version. It installs nothing; it reads the registration token, registers one ephemeral runner, runs one job and terminates. |

## Provenance

Recovered on 2026-11-12 from the deployment's launch-template version that
built the 2026-11-10 worker image. The 2026-11-11 image is that image plus
`jq`, `podman`, `make`, `gcc` and `libc6-dev`; the two incremental build
scripts were not recorded. `provision.sh` reconstructs the full recipe,
including those additions. It does not reproduce the earlier image's warm CI
caches: a from-scratch build starts cold, which is slower but correct.

## Pins

| Tool | Version |
|---|---|
| uv | 0.13.7 |
| node | 24.22.1 |
| ruff | input — the persistent fleet's `ruff_version` |
| promtool | 3.16.0 |
| forgejo-runner | 13.2.1 |

The ruff pin is an input, not a literal. Set it from the persistent fleet's
source pin (`ansible-homelab/inventory/group_vars/all.yml`, `ruff_version`) so
the image and the fleet cannot drift silently. The current image carries
0.17.3; the source pin after the 2026-11-11 bump is 0.17.4.

## Build

1. Launch a disposable builder matching the worker architecture (the current
   deployment uses Debian 13 arm64) in the deployment's subnet. The provision
   script needs no Forgejo credential and no instance profile; the worker's
   registration is the runtime bootstrap's job, after the image is published.
2. Build the user data with the ruff pin prepended, publish it, and wait for
   `IMAGE_READY` on the serial console:

   ```sh
   { printf 'export RUFF_VERSION=0.17.4\n'; cat deploy/runner/image/provision.sh; } \
     > /tmp/worker-image-user-data.sh
   ```

   Use the persistent fleet's current `ruff_version`; 0.17.4 is the source pin,
   0.17.3 reproduces the current image. The script prints `STEP` markers and a
   `PROBE end have=N miss=M` line; any `MISS` fails the build. The builder
   self-terminates after 3600 s, so snapshot it while it is still running.
3. Create the image without rebooting the builder:

   ```sh
   aws ec2 create-image --instance-id <builder> \
     --name "ci-burst-worker-arm64-<YYYYMMDD-HHMM>" \
     --description "ci-burst worker: Debian 13 arm64, pinned toolchain" \
     --no-reboot
   ```

4. Create a new launch-template version from the current one with the new
   `ImageId`, then move the deployment TOML's `launch_template_version` to it.
   The user data is unchanged: [`../bootstrap.sh`](../bootstrap.sh) is the
   published bytes.
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
