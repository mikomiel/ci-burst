# Tasks

Sections 1 to 5 are product-source tasks. They authorize no deployment, no
credential, no image creation and no launch-template change. Section 6 lists the
operator steps that need their own approval, and no source task stands in for
them. No task changes controller behaviour, IAM or the router.

## 1. Shared cache environment

- [ ] 1.1 Add one shared file under `deploy/runner/` that defines the job-account
  cache environment: `XDG_CACHE_HOME`, `UV_CACHE_DIR`, `NPM_CONFIG_CACHE`,
  `npm_config_cache`, `PIP_CACHE_DIR` and `CI_PROFILE_CACHE_ROOT`, all under
  `/var/lib/forgejo-runner/cache`.
- [ ] 1.2 In `deploy/runner/image/provision.sh`, install the shared file into the
  image and use it in the post-install probe instead of the inline copy. The probe
  currently lacks `npm_config_cache`; the shared file adds it.
- [ ] 1.3 Leave `deploy/runner/bootstrap.sh` byte-identical. It is published user
  data with a pinned hash and cannot read a repository file.
- [ ] 1.4 Add a test (in `tests/test_runner_bootstrap.py` or a new file in the same
  style) that parses the cache assignments from `bootstrap.sh` and `provision.sh`
  and fails, naming the variable, when a name is missing or a value differs from the
  shared file. Add a test that every value lies under the `cache` directory. Prove
  the divergence test can fail by mutating a copy of the input inside the test.

## 2. Warm-cache layer script

- [ ] 2.1 Add the script under `deploy/runner/image/`. Inputs: source archive URL,
  sha256, warm commands, optional offline gate command, builder lifetime. Refuse a
  missing sha256 or empty warm commands before any consumer command runs.
- [ ] 2.2 Arm the lifetime timer (`systemd-run --on-active`) first and power off
  from an exit trap on every path.
- [ ] 2.3 Fetch the archive, verify the sha256 before extracting, and extract it to a
  directory outside the runner home.
- [ ] 2.4 Run the warm commands and the optional gate as the job account with
  `runuser -u runner -- env -i HOME=... PATH=... LC_ALL=C.UTF-8 <shared cache
  environment> bash -c 'umask 0022; cd SRC && CMD'`.
- [ ] 2.5 Remove the source, `/var/lib/forgejo-runner/.runner` and every runner-home
  entry other than `cache`; run `cloud-init clean --logs`. Print `IMAGE_READY` and
  the source digest only after every step has passed. Any failure exits non-zero
  without the marker.
- [ ] 2.6 Do not call a cloud API and do not take a credential.

## 3. Tests for the script

- [ ] 3.1 Add tests in `tests/` in the style of `tests/test_runner_bootstrap.py`:
  read the committed script and run it against stubs (`runuser`, `systemd-run`,
  `cloud-init`, `curl`, `shutdown`) so no builder, account or network is needed.
- [ ] 3.2 Cover: missing sha256 and empty commands refuse before any consumer
  command; a digest mismatch stops before extraction; a failing warm command and a
  failing gate each exit non-zero with no marker; success prints the marker last with
  the digest; the timer is armed before the first consumer command; the exit trap
  powers off on failure; the consumer commands receive exactly the shared cache
  environment and an empty inherited environment; clean-up removes the source, the
  registration file and non-`cache` runner-home entries and keeps `cache`; a failing
  clean-up step prevents the marker.
- [ ] 3.3 Assert the script contains no consumer-specific command, hostname or
  account identifier, and that `tests/test_public_content_scan.py` still passes.

## 4. Documentation

- [ ] 4.1 `docs/runner-setup.md`: add a warm image section under "The worker image".
  Cover the inputs, a generic example with placeholder URL and digest, the ready
  marker, tagging the image with the source digest, when to rebuild (lockfile
  change), the expected size as an order of magnitude, why a warm cache only reduces
  downloads and a stale cache is slower and never wrong, and the unchanged rollout
  (new template version, canary, operator pins it).
- [ ] 4.2 `deploy/runner/image/README.md`: add the script and the shared file to the
  file table.
- [ ] 4.3 `docs/deployment.md`: add the instance-type selection guidance (newest
  same-shape generation first, older as capacity fallback, verify with per-type job
  duration and cost per job, reference to issue #156) and reorder the example
  `instance_types` list newest-first.
- [ ] 4.4 `docs/operations.md`: reorder its example the same way and re-read the
  type-major order sentence beside it so it still matches.
- [ ] 4.5 Update any README that lists `deploy/runner/image/` contents, following
  `docs/readme-standard.md` if the repository has one.

## 5. Checks

- [ ] 5.1 Run the repository test suite and `npm run check:spec`; both pass.
- [ ] 5.2 Confirm the public content scan passes on every new file.

## 6. Live verification (operator approval required)

- [ ] 6.1 Launch a builder from the current worker image on a test identity, run the
  script with a test consumer's archive and commands, and record the cache size.
- [ ] 6.2 Confirm the offline gate passes, and that a deliberately broken warm
  command and a deliberately failing gate each produce no ready marker.
- [ ] 6.3 Create the image, tag it with the source digest, and publish it in a new
  launch-template version. Do not pin it yet.
- [ ] 6.4 Run a canary job on a test repository through the new version and confirm
  it finds the warm cache and downloads little.
- [ ] 6.5 Compare job duration for the same jobs on the cold and the warm version, and
  record the result. Only then may the operator pin the version.
- [ ] 6.6 With instance type in worker events (issue #156) available, compare per-type
  job duration and cost per job before and after the reorder in the guidance, and
  record the result.
