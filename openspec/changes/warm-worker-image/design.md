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
3. Run the warm commands as the job account, each in its own transient unit:
   `systemd-run --wait --pipe --collect --expand-environment=no --uid=runner
   --gid=runner --property=KillMode=control-group env -i HOME=... PATH=... LC_ALL=C.UTF-8
   <cache environment> bash -c 'umask 0022; cd SRC && CMD'`. `env -i` keeps the
   root environment, and any credential in it, out of consumer commands. The umask
   matches the job's. The unit's control group ends with its command, so a
   background process dies with it. The archive is extracted the same way, by the
   job account and never by root, with the archive on standard input. Before the
   clean-up and again before the marker the script fails unless no process of the
   job account remains.
4. If an offline gate command is supplied, run it the same way with downloads
   forbidden. The mechanism that forbids downloads is the consumer's command to
   choose (see open questions).
5. Remove the extracted source, `/var/lib/forgejo-runner/.runner` and every entry
   in the runner home other than `cache`. Run `cloud-init clean --logs`.
6. Print `IMAGE_READY`, then keep the builder running for the snapshot window
   (the lifetime timer still bounds it).

Warm and gate output goes to a log file, never to the script's own output (see
"The readiness channel"). Any failing step exits non-zero before step 6. The ready marker is the only signal
the operator acts on, so a failed warm or a failed gate never becomes an image.

On success the builder must stay up: the operator creates the image from a running
builder (`provision.sh` prints `IMAGE_READY`, syncs and then sleeps for the same
reason). The script therefore does not power off after the marker. It waits for a
bounded snapshot window, and the lifetime timer armed in step 1 is the outer bound.
On failure the exit trap removes the source, holds briefly so the console output can
be read, and powers the builder off. The builder is launched with
terminate-on-shutdown and delete-on-termination storage, as
`deploy/runner/image/README.md` requires, so a power-off removes it.

## The readiness channel

The operator reads `IMAGE_READY` from the builder's console. Consumer commands must
not be able to forge it. Their stdout and stderr go to a log file, not to the
script's output. On failure the script prints the last lines of the log with every
line prefixed by a fixed frame (`  | `), so no consumer line can start with the
marker. Only the script prints the marker, and only on success. A consumer command
that prints the marker and then fails, or prints it and succeeds, leaves no
unframed marker.

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

- `provision.sh` embeds a byte-identical copy of the shared file and installs it
  at `/etc/ci-burst/cache-env.sh`; its probe reads the cache variables from that
  installed file.
- `warm-cache.sh` keeps an inline copy of the assignments. It runs as lone user
  data on a builder whose image may predate the installed file, so it cannot
  source it.
- `bootstrap.sh` keeps its inline assignments, because it is self-contained user
  data.
- `tests/test_cache_environment.py` parses the assignments out of every copy
  (`bootstrap.sh`, the `provision.sh` heredoc, the `warm-cache.sh` array) and
  fails, naming the variable, when a name or a value differs from the shared file
  or when a name is missing. It also asserts that the heredoc is byte-identical.

A later bootstrap edit may source the installed file. That is a new template
version and its own change.

The tests also assert that every path in the cache environment lies under
`/var/lib/forgejo-runner/cache`, because the clean-up keeps only that directory.

## Runbook and examples

`docs/warm-image.md` is the one owner of the end-to-end runbook and the examples;
`docs/runner-setup.md` and the image README link to it and carry no second copy. The
runbook steps: make the source archive and its sha256; host it at a URL the builder
can fetch without credentials (for example a short-lived presigned URL, which is
never logged or committed); launch a builder from the base worker image with the
script as user data, terminate-on-shutdown and the lifetime bound; watch the console
for the trusted marker; create the image, tagged with the source digest; publish a
new launch-template version; run a canary; the operator moves the configured version.
Commands are AWS CLI with comments and placeholders (`<base-image-id>`,
`<subnet-id>`, `<bucket>`), never real identifiers.

The examples pair each warm command with an offline gate: uv (`uv sync --frozen` per
project; gate with `UV_OFFLINE=1`), npm (`npm ci`; gate `npm ci --offline`), pip
(`pip download -r requirements.txt -d <dir under cache>` or an install into a
throwaway venv; gate `--no-index`), Playwright (install browsers into a path under
`cache` and set `PLAYWRIGHT_BROWSERS_PATH` for warm and gate; the job environment is
the bootstrap's, so the consumer's workflow sets it for jobs), and a generic monorepo
check runner with a prime mode and an offline mode. A test requires that every cache
variable or path in the examples lies under the shared cache directory.

## Tag and traceability

The operator tags the created image with the source archive's sha256, so the
lockfile basis of a warm image is traceable. The script prints the digest next to
the ready marker for that purpose. Tagging is the operator's step because the
script does not create images.

## Rebuild and rollout

Rebuild when a lockfile the warm commands read changes. For content-addressed
package and download caches (uv, npm, pip, browser downloads) a stale cache costs
only time: the job resolves its own lockfile and downloads what is missing. That
claim does not extend to any other cache a consumer places under `cache/`, such as
a validation or result cache. The consumer must key such a cache on its source,
lockfiles, toolchain and environment, and ci-burst does not guarantee its validity.

Rollout is the existing operator flow in `deploy/runner/image/README.md` and this
change does not alter it: create the image, create a new launch-template version
with the new image, move the deployment TOML's `launch_template_version` to it, and
run the toolchain check on a launched worker. A canary job before that move is an
operator obligation. The controller reads the configured version and checks no
canary evidence, so it enforces nothing here.

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

`docs/deployment.md` currently advises keeping the template's type first so the
default landing place does not change. The new guidance supersedes that sentence
for an operator who has measured the newer type: the template's type stays first
when it is already the newest, and otherwise the operator moves the template to the
newest type, or accepts that the first listed type becomes the preferred one. The
text must say which, not leave both statements standing.

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
| A failed build | The script holds briefly for the console, then powers the builder off. |
| A consumer command prints the marker | The text goes to the log; a failure prints it framed only. |
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
