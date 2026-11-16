# Tasks

Sections 1 to 5 are product-source tasks. They authorize no deployment, no
credential, no image creation and no launch-template change. Section 6 lists the
operator steps that need their own approval, and no source task stands in for
them. No task changes controller behaviour, IAM or the router.

## 1. Shared cache environment

- [x] 1.1 Add one shared file under `deploy/runner/` that defines the job-account
  cache environment: `XDG_CACHE_HOME`, `UV_CACHE_DIR`, `NPM_CONFIG_CACHE`,
  `npm_config_cache`, `PIP_CACHE_DIR` and `CI_PROFILE_CACHE_ROOT`, all under
  `/var/lib/forgejo-runner/cache`.
- [x] 1.2 In `deploy/runner/image/provision.sh`, install the shared file into the
  image and use it in the post-install probe instead of the inline copy. The probe
  currently lacks `npm_config_cache`; the shared file adds it.
- [x] 1.3 Leave `deploy/runner/bootstrap.sh` byte-identical. It is published user
  data with a pinned hash and cannot read a repository file.
- [x] 1.4 Add a test (in `tests/test_runner_bootstrap.py` or a new file in the same
  style) that parses the cache assignments from `bootstrap.sh` and `provision.sh`
  and fails, naming the variable, when a name is missing or a value differs from the
  shared file. Add a test that every value lies under the `cache` directory. Prove
  the divergence test can fail by mutating a copy of the input inside the test.

## 2. Warm-cache layer script

- [x] 2.1 Add the script under `deploy/runner/image/`. Inputs: source archive URL,
  sha256, warm commands, optional offline gate command, builder lifetime. Refuse a
  missing sha256 or empty warm commands before any consumer command runs.
- [x] 2.2 Arm the lifetime timer (`systemd-run --on-active`) first. On success,
  print the marker, sync and wait for a bounded snapshot window; do not power off.
  On failure, hold briefly for the console and then power off from the exit trap.
  Match `provision.sh`'s wait after `IMAGE_READY` and the terminate-on-shutdown
  contract in `deploy/runner/image/README.md`.
- [x] 2.2a Send the stdout and stderr of every warm command and the gate to a log
  file, not the script's output. On failure print a tail of the log with every line
  prefixed by a fixed frame (`  | `). Only the script prints `IMAGE_READY`.
- [x] 2.3 Fetch the archive, verify the sha256 before extracting, and extract it to a
  directory outside the runner home.
- [x] 2.4 Run the warm commands and the optional gate as the job account with
  `runuser -u runner -- env -i HOME=... PATH=... LC_ALL=C.UTF-8 <shared cache
  environment> bash -c 'umask 0022; cd SRC && CMD'`.
- [x] 2.5 Remove the source, `/var/lib/forgejo-runner/.runner` and every runner-home
  entry other than `cache`; run `cloud-init clean --logs`. Print `IMAGE_READY` and
  the source digest only after every step has passed. Any failure exits non-zero
  without the marker.
- [x] 2.6 Do not call a cloud API and do not take a credential.

## 3. Tests for the script

- [x] 3.1 Add tests in `tests/` in the style of `tests/test_runner_bootstrap.py`:
  read the committed script and run it against stubs (`runuser`, `systemd-run`,
  `cloud-init`, `curl`, `shutdown`) so no builder, account or network is needed.
- [x] 3.2 Cover: missing sha256 and empty commands refuse before any consumer
  command; a digest mismatch stops before extraction; a failing warm command and a
  failing gate each exit non-zero with no marker; success prints the marker last with
  the digest; the timer is armed before the first consumer command; on success the builder
  stays alive for the snapshot window and the script does not power off; on
  failure it holds briefly and then shuts down; a warm command or the gate that
  prints `IMAGE_READY` and exits non-zero produces no unframed marker; a successful
  command that prints `IMAGE_READY` does not appear unframed either; a failure
  prints a framed log tail; the consumer commands receive exactly the shared cache
  environment and an empty inherited environment; clean-up removes the source, the
  registration file and non-`cache` runner-home entries and keeps `cache`; a failing
  clean-up step prevents the marker.
- [x] 3.3 Assert the script contains no consumer-specific command, hostname or
  account identifier, and that `tests/test_public_content_scan.py` still passes.

## 4. Documentation

- [x] 4.0 Create `docs/warm-image.md` as the single owner of the runbook and the
  examples. In `docs/runner-setup.md` ("The worker image") add a short pointer only.
  Write the runbook: make the archive and sha256; host it at a URL fetchable without
  credentials (short-lived presigned URL, never logged or committed); launch the
  builder from the base image with the script as user data, terminate-on-shutdown
  and the lifetime bound; watch the console for the marker; create the image tagged
  with the source digest; publish a launch-template version; canary; the operator
  moves the configured version. Give copy-paste AWS CLI commands with comments and
  placeholders (`<base-image-id>`, `<subnet-id>`, `<bucket>`) and no real ids.
- [x] 4.0a In `docs/warm-image.md` add generic warm and offline-gate examples: uv
  (`uv sync --frozen`; `UV_OFFLINE=1`), npm (`npm ci`; `npm ci --offline`), pip
  (`pip download -r requirements.txt -d <path under cache>` or a throwaway venv;
  `--no-index`), Playwright (browsers under `cache`, `PLAYWRIGHT_BROWSERS_PATH` in
  warm and gate, and a note that jobs get the bootstrap's environment so the
  consumer's workflow sets it), and a monorepo check runner with prime and offline
  modes. Name no consumer.
- [x] 4.0b Add a test that parses the cache variables and paths in the documented
  examples and fails when one lies outside the shared cache directory.
- [x] 4.1 In `docs/warm-image.md`: cover the inputs, a generic example with placeholder URL and digest, the ready
  marker, tagging the image with the source digest, when to rebuild (lockfile
  change), the expected size as an order of magnitude, why a warm cache only reduces
  downloads, that a stale content-addressed package or download cache (uv, npm,
  pip, browser downloads) is slower and never wrong, that any other cache under
  `cache/` is the consumer's to key on source, lockfiles, toolchain and
  environment, and the rollout as `deploy/runner/image/README.md` states it (new
  template version, move `launch_template_version`, toolchain check), with a canary
  as an operator obligation the controller does not enforce. Reconcile the README's
  build steps (terminate-on-shutdown builder, snapshot while running) with the new
  script, and add a task-level note if the README needs a line for the warm layer.
- [x] 4.2 `deploy/runner/image/README.md`: add the script and the shared file to the
  file table.
- [x] 4.3 `docs/deployment.md`: add the instance-type selection guidance (newest
  same-shape generation first, older as capacity fallback, verify with per-type job
  duration and cost per job, reference to issue #156) and reorder the example
  `instance_types` list newest-first. Reconcile the existing sentence near line 314
  ("keep the template's type first so the default landing place does not change")
  with the newest-first guidance: rewrite it so one rule stands, and say what to do
  when the template's type is not the newest.
- [x] 4.4 `docs/operations.md`: reorder its example the same way and re-read the
  type-major order sentence beside it so it still matches, and make sure it does
  not contradict the reconciled sentence in `docs/deployment.md`.
- [x] 4.5 Update any README that lists `deploy/runner/image/` contents, following
  `docs/readme-standard.md` if the repository has one.

## 5. Checks

- [x] 5.1 Run the repository test suite and `npm run check:spec`; both pass.
- [x] 5.2 Confirm the public content scan passes on every new file.

## 6. Live verification (operator approval required)

- [ ] 6.1 Launch a builder from the current worker image on a test identity, run the
  script with a test consumer's archive and commands, and record the cache size.
- [ ] 6.2 Confirm the offline gate passes, and that a deliberately broken warm
  command and a deliberately failing gate each produce no ready marker.
- [ ] 6.3 Create the image while the builder is still running, tag it with the
  source digest, and publish it in a new launch-template version. Do not move the
  configured version yet.
- [ ] 6.4 Run a canary job on a test repository through the new version and confirm
  it finds the warm cache and downloads little. This is an operator obligation; the
  controller does not check for it.
- [ ] 6.5 Compare job duration for the same jobs on the cold and the warm version, and
  record the result. Only then should the operator move the configured version.
- [ ] 6.6 With instance type in worker events (issue #156) available, compare per-type
  job duration and cost per job before and after the reorder in the guidance, and
  record the result.
