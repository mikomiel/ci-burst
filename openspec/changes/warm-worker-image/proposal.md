# Proposal

## Why

Every burst job starts cold. `deploy/runner/image/provision.sh` builds the worker
image from scratch, states that caches are not reproduced, and configures the
runner with its own cache disabled, so each job downloads its toolchain and
dependencies again (GitHub issue #157). On a measured consumer the same jobs ran
about 2x slower on burst workers than on persistent warm runners, and every
worker pays the transfer again.

A warm image works. An operator-built test image, primed with a consumer's locked
dependencies, passed an offline gate (downloads forbidden) for a real check group.
But that warming lives outside this repository as an operator script, which leaves
three gaps:

- **No standard step.** Each consumer writes its own build and clean-up, and a
  missed clean-up can bake a runner registration or consumer source into an image.
- **The cache environment is duplicated.** The job-account cache variables are
  written out in `bootstrap.sh` and again in `provision.sh`. A warm step with
  different paths produces a cache that jobs never read, and nothing fails.
- **Instance-type choice is undocumented as guidance.** `aws.instance_types` is
  documented as configuration only, and the examples list the older generation
  first. One consumer measured the newer same-shape generation 16-23% faster
  (median, small sample) at about 7% lower spot price (issue #157; related: #156,
  instance type in worker events).

## What Changes

- Add an optional, generic warm-cache layer script (ci-burst provides it; running it is optional) under `deploy/runner/image/`.
  The consumer supplies a source archive URL and sha256, the warm commands and an
  optional offline gate command. The script runs the commands as the job account
  with the shared cache environment, runs the gate, removes the source, the runner
  registration and anything added to the runner home outside `cache`, cleans
  cloud-init state and prints a ready marker. Consumer output goes to a log, so only the script prints the ready
  marker. A warm or gate failure produces no ready marker, so no image is created.
  After success the builder stays up for a bounded snapshot window; after failure
  it shuts down. A lifetime timer bounds the builder.
- Define the job-account cache environment once in a shared file. The warm layer
  and `provision.sh` use it, and a test fails if `bootstrap.sh` or `provision.sh`
  carries a different value.
- Add one end-to-end runbook, in a new `docs/warm-image.md` that owns it, with
  copy-paste AWS CLI commands (placeholders only) and generic warm-command and
  offline-gate examples for uv, npm, pip, Playwright browsers and a monorepo check
  runner. `docs/runner-setup.md` links to it.
- Document when to rebuild (lockfile change), the expected size, a generic
  example, tagging the image with the source digest, and the unchanged rollout
  path in the image README: a new launch-template version, then the operator moves
  the configured version to it, with a canary as an operator obligation.
- Document instance-type selection: newest same-shape generation first, an older
  type as capacity fallback, verified with per-type job duration and cost per job.
  Reorder the documented examples newest-first.

## Impact

- Source-only: a new script, a shared file, tests and documentation. No change to
  controller behaviour, admission, placement, IAM or the router.
- The layer is optional. An operator who does not run it keeps today's cold image.
- A warm cache reduces downloads only. For content-addressed package and download
  caches (uv, npm, pip, browser downloads) a stale cache is slower, never wrong,
  because the job resolves its own lockfile. Any other cache a consumer places
  under `cache/` is the consumer's to key on source, lockfiles, toolchain and
  environment; ci-burst does not guarantee its validity.
- Rollout stays the operator flow in `deploy/runner/image/README.md`. A canary is
  an operator obligation; the controller enforces none.
- Non-goals: a cache shared between workers, enabling the runner's own cache,
  consumer-specific warm commands in this repository, automatic rebuilds on a
  lockfile change, calling CreateImage from the script, and any change to the
  launch-template or rollout mechanics.
