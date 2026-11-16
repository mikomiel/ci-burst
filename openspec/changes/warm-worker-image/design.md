# Design

## Decision

A warm worker image is the base worker image plus one extra layer: the package
and tool caches a consumer's jobs would otherwise download. The layer is built by
one generic script that the operator runs on a builder launched from the current
worker image. The script never decides what to warm. The consumer supplies that.

Image creation stays an operator action. ci-burst creates no image
(`openspec/specs/spot-runner-lifecycle`), so the script ends at a ready marker and
the operator calls the image-creation API.

## Terms

| Term | Meaning |
|---|---|
| Base image | The image `provision.sh` builds. It starts cold. |
| Warm layer | The extra cache content under the job account's `cache` directory. |
| Job account | The unprivileged `runner` account, home `/var/lib/forgejo-runner`. |
| Cache environment | The variables the job account runs with: `XDG_CACHE_HOME`, `UV_CACHE_DIR`, `NPM_CONFIG_CACHE`, `npm_config_cache`, `PIP_CACHE_DIR`, `CI_PROFILE_CACHE_ROOT`, all under `/var/lib/forgejo-runner/cache`. |
| Source archive | A consumer archive, fetched by URL and checked against a supplied sha256. It holds the lockfiles the warm commands read. |
| Warm commands | Consumer commands that fill the caches. They run from the extracted source. |
| Offline gate | An optional consumer command that proves the warm layer is enough, run with downloads forbidden. |
| Ready marker | The fixed line `IMAGE_READY`, printed only after every step has passed. |

## Inputs

The script takes its inputs as named arguments or environment variables, never
from the archive:

- source archive URL and its sha256 (both required),
- warm commands (required),
- offline gate command (optional),
- builder lifetime in seconds (bounded; a default applies).

The script refuses to run when the sha256 is missing, when the fetched bytes do
not match it, or when the warm commands are empty.

## The sequence

1. Arm a lifetime timer (`systemd-run --on-active`) that powers the builder off.
   It is armed first so that no later step can leave a builder running.
2. Fetch the archive, verify the sha256 and extract it into a fresh directory
   outside the runner home.
3. Run the warm commands as the job account:
   `runuser -u runner -- env -i HOME=... PATH=... LC_ALL=C.UTF-8 <cache
   environment> bash -c 'umask 0022; cd SRC && CMD'`. `env -i` keeps the root
   environment, and any credential in it, out of consumer commands. The umask
   matches the job's.
4. If an offline gate command is supplied, run it the same way with downloads
   forbidden. The mechanism that forbids downloads is the consumer's command to
   choose (see open questions).
5. Remove the extracted source, `/var/lib/forgejo-runner/.runner` and every entry
   in the runner home other than `cache`. Run `cloud-init clean --logs`.
6. Print `IMAGE_READY`.

Any failing step exits non-zero before step 6. The ready marker is the only signal
the operator acts on, so a failed warm or a failed gate never becomes an image. On
a non-zero exit the script still removes the source and shuts the builder down
through an exit trap, as `provision.sh` does.

The warm layer is the only thing that survives step 5, and only under `cache`. The
script does not clear `cache` between the warm commands and the image, so what the
commands wrote is what jobs read.

## One cache environment

Jobs read the cache through these variables, so a warm step with different paths
yields a cache nobody reads. The values therefore have one owner: a shared file in
`deploy/runner/`.

`bootstrap.sh` is published user data: the exact bytes of a launch-template
version, with a hash pinned in `tests/test_runner_bootstrap.py`. It cannot read a
repository file at boot, and sourcing a file from the image would change those
bytes and break an older image that lacks the file. So:

- `provision.sh` installs the shared file into the image, and its probe and the
  warm layer source it. Those two copies disappear.
- `bootstrap.sh` keeps its inline assignments, because it is self-contained user
  data. A test parses the assignments out of `bootstrap.sh`, and out of
  `provision.sh` if any remain, and fails when a name or a value differs from the
  shared file or when a name is missing.

A later bootstrap edit may source the installed file. That is a new template
version and its own change.

The tests also assert that every path in the cache environment lies under
`/var/lib/forgejo-runner/cache`, because the clean-up keeps only that directory.

## Tag and traceability

The operator tags the created image with the source archive's sha256, so the
lockfile basis of a warm image is traceable. The script prints the digest next to
the ready marker for that purpose. Tagging is the operator's step because the
script does not create images.

## Rebuild and rollout

Rebuild when a lockfile the warm commands read changes. A stale cache is slower,
never wrong: the job still resolves its own lockfile and downloads only what is
missing. Rollout is the existing flow and this change does not alter it: register
the image in a new launch-template version, run a canary, then the operator pins
that version in the deployment configuration. The controller keeps launching from
the pinned version.

## Instance-type guidance

Documentation only. `aws.instance_types` already makes the controller type-major:
the first type is preferred and a later type is used only on a capacity rejection.
The guidance:

- put the newest generation of the same shape first and an older generation after
  it as the capacity fallback,
- change the order only with a measured comparison: job duration per instance type
  and cost per job, before and after (instance type in worker events is issue
  #156),
- treat the measured speed-up as one consumer's small-sample result, not a
  guarantee.

The examples in `docs/deployment.md` and `docs/operations.md` list the older type
first. They are reordered newest-first. The order sentence in `docs/operations.md`
is checked so it still describes the example.

## Failure behaviour

| Failure | Result |
|---|---|
| Missing or mismatched sha256, empty warm commands | Refusal before any consumer command runs. No marker. |
| A warm command fails | Non-zero exit. No marker. No image. |
| The offline gate fails | Non-zero exit. No marker. No image. |
| The builder outlives its work | The lifetime timer powers it off. |
| A clean-up step fails | Non-zero exit. No marker. The image is not safe to create. |

## Credential boundary

The script holds no cloud credential and calls no cloud API. The archive is fetched
without credentials, so a private source needs a pre-signed or otherwise
public-to-the-builder URL, and that choice is the consumer's. Consumer commands run
as the job account with an empty environment apart from the named variables. The
clean-up removes the runner registration file so an image never carries one, as
`provision.sh` already requires.

## Alternatives

- **Enable the runner's own cache.** Rejected: it is per-run and for a different
  layer, and it is a runner configuration change.
- **Warm at boot from a shared store.** Rejected for this change: it adds a store,
  credentials and a network dependency to the boot path.
- **Put consumer commands in ci-burst.** Rejected: they would leak one consumer's
  layout into a generic tool.
- **Call CreateImage from the script.** Rejected: ci-burst creates no images, and it
  would need a cloud credential on the builder.

## Evidence gaps

- The 2x slowdown and the 16-23% generation difference are measurements from one
  consumer with small samples. They motivate the change and do not set a target.
- One test image passed an offline gate. No warm image has run a canary through the
  controller yet.
- The 1.8 GB cache size is one consumer's. It is a statement of order of
  magnitude in the docs, not a limit.
- Whether the warm cache cuts job duration on the workers' real disk and network
  path is unmeasured until the live section of `tasks.md` runs.

## Open questions

1. Whether `bootstrap.sh` should source the installed shared file in a later
   template version. This change chooses a test over sourcing so the published
   bytes stay as they are.
2. How the offline gate forbids downloads. The proven run used a consumer command
   with downloads forbidden. This change leaves the mechanism to the consumer's
   command and documents one generic example. A builder-level egress block would be
   stronger and is not designed here.
3. Whether a private source archive needs a supported credential path. This change
   supports an URL that the builder can fetch without credentials only.
