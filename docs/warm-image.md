# Warm worker image

A warm worker image is the base worker image plus one extra layer: the package
and tool caches that a consumer's jobs would otherwise download on every worker.
The layer is optional. An operator who never runs it keeps the cold base image
that [`deploy/runner/image/provision.sh`](../deploy/runner/image/provision.sh)
builds, and nothing else in this repository changes.

This document owns the runbook and the examples. The script is
[`deploy/runner/image/warm-cache.sh`](../deploy/runner/image/warm-cache.sh); the
image directory's [README](../deploy/runner/image/README.md) owns the base image
and the rollout of any image; [runner-setup.md](runner-setup.md) links here.

## What it is and what it is not

- **Generic.** The script knows no consumer. You supply a source archive, its
  sha256, the warm commands and optionally an offline gate command. No command
  that fills a cache lives in this repository.
- **Source only.** The script creates no image, calls no cloud API and holds no
  credential. It ends at a marker, and you create the image with the AWS CLI.
- **Downloads only.** A warm cache removes downloads. The job still resolves its
  own lockfile and runs its own commands, so it checks the same things as on a
  cold worker. It is not a shortcut around a check.
- **Optional and not enforced.** The controller does not know a
  worker image is warm. It uses the image that the launch-template version
  names, and it checks no canary evidence.

No speed-up is promised here. One consumer measured burst jobs about twice as
slow as on persistent runners that keep warm caches, and a test image passed an
offline gate. Those are small samples from one consumer. Measure your own jobs
([Canary and cutover](#canary-and-cutover)).

## The cache the jobs read

Jobs run as the unprivileged `runner` account with the cache environment that
[`deploy/runner/bootstrap.sh`](../deploy/runner/bootstrap.sh) sets. The warm
commands run with the same variables, so what they write is what jobs read:

| Variable | Value |
|---|---|
| `XDG_CACHE_HOME` | `/var/lib/forgejo-runner/cache` |
| `UV_CACHE_DIR` | `/var/lib/forgejo-runner/cache/uv` |
| `NPM_CONFIG_CACHE`, `npm_config_cache` | `/var/lib/forgejo-runner/cache/npm` |
| `PIP_CACHE_DIR` | `/var/lib/forgejo-runner/cache/pip` |
| `CI_PROFILE_CACHE_ROOT` | `/var/lib/forgejo-runner/cache/validation` |

[`deploy/runner/cache-env.sh`](../deploy/runner/cache-env.sh) is the one
definition, and a repository test fails when `bootstrap.sh`, `provision.sh` or
`warm-cache.sh` carries a different name or value. The clean-up keeps only the
`cache` directory, so anything a warm command writes elsewhere in the runner home
is removed, and a tool that is not told to use the cache variables must be
pointed at a path under `/var/lib/forgejo-runner/cache`.

Jobs get exactly those variables and no others from the bootstrap. A tool that
needs another variable to find its cache (for example Playwright's browser path)
needs the consumer's workflow to set it for the job; see [Playwright
browsers](#playwright-browsers).

## Inputs

`warm-cache.sh` reads environment variables, never the archive. Render them in
front of the script ([step 3](#3-render-the-user-data)).

| Variable | Required | Meaning |
|---|---|---|
| `WARM_SRC_URL` | yes | `https` URL of a `.tar.gz` source archive that the builder can fetch without credentials. |
| `WARM_SRC_SHA256` | yes | The archive's sha256, 64 hex digits. The script refuses to run without it and stops when the fetched bytes differ. |
| `WARM_COMMANDS` | yes | The warm commands, one per line. Blank lines and lines that start with `#` are ignored. At least one is required. |
| `WARM_GATE_COMMAND` | no | One command that proves the warm layer is enough, run after the warm commands with downloads forbidden. Join steps with `&&` or `;`. |
| `WARM_LIFETIME_SECONDS` | no | Builder lifetime bound, 600 to 14400, default 5400. |
| `WARM_SNAPSHOT_SECONDS` | no | How long the builder stays up after the marker, 60 to 3600, default 1800. |
| `WARM_SRC_STRIP_COMPONENTS` | no | Leading path components that extraction drops, 0 or 1, default 0. A forge archive wraps the source in one directory and needs 1. Any other value stops the script before the fetch. |

Each command runs from the extracted source root, as the `runner` account, in its
own transient systemd unit:

```sh
systemd-run --wait --pipe --collect --expand-environment=no \
  --uid=runner --gid=runner --property=KillMode=control-group \
  env -i HOME=... PATH=... LC_ALL=C.UTF-8 <cache environment> \
  bash -c 'umask 0022; cd SRC && COMMAND'
```

`env -i` keeps root's environment, and any credential in it, out of consumer
commands, and drops the variables systemd adds for a user service.
`--expand-environment=no` stops systemd from replacing `${NAME}` and `$NAME` in
the arguments with its own (empty) values first, so a command may write
`"${UV_CACHE_DIR}"` or `$HOME` and bash resolves it from the variables above. The
umask matches a job's. A command sees only those variables. Its stdout and stderr go
to a log, never to the script's output. When a command ends, its unit's control
group ends with it, so a background process it started dies too.

## What the script does

1. Arms a lifetime timer (`systemd-run --on-active`) that powers the builder
   off. It is armed first, so no later step can leave a builder running.
2. Refuses before any fetch or consumer command when the sha256 or the warm
   commands are missing or malformed. Then adds firewall rules that reject the
   instance metadata addresses for the `runner` account, as the worker bootstrap
   does, and checks each rule, so no consumer command can read the user data and
   the archive URL in it; a rule that is not in place ends the build. DHCP
   renewals do not change firewall rules. A failure tail also shows the URL, or
   its query, as `[archive-url]`.
3. Fetches the archive and compares its sha256 with `WARM_SRC_SHA256`. Only then
   does the `runner` account, never root, extract it into a fresh directory
   outside the runner home. Root feeds the archive on standard input and the
   account unpacks it with `--no-same-owner --no-same-permissions`, so an archive
   cannot create a device node or a root-owned file: such an archive fails the
   extraction and the build.
4. Runs the warm commands in order, then the gate if one is set.
5. Fails unless no process of the `runner` account remains (it looks again after a
   short settle, then kills the units and looks once more). Then removes the
   extracted source, the runner registration file and every entry of the runner
   home except `cache`, and runs `cloud-init clean --logs`. The last step also
   removes the saved user data, which holds the archive URL. Every clean-up step
   is fatal, and the process check repeats right before the marker, so a late
   background writer cannot change the image after the clean-up.
6. Prints `CACHE size=...`, then `WARM_SOURCE_SHA256 <digest>`, then
   `IMAGE_READY` as its last line, and stays up for the snapshot window.

**Success** is the `IMAGE_READY` line. The script does not power the builder off,
because you create the image from the running builder. The lifetime timer still
ends it if you never do.

**Failure** prints `WARM FAIL ...`, then up to 60 lines of the consumer log with
every line prefixed by `  | `, holds for 300 seconds so the console can be read,
and runs `shutdown -h now`. No failure prints `IMAGE_READY`. Only the script
prints that marker, and a consumer command cannot forge it: its output is in the
log, and the framed tail first replaces every byte that is not a tab, a newline
or printable ASCII with `?`, so no carriage return, escape sequence or Unicode
line separator can start a console line of its own. Trust a line that is exactly
`IMAGE_READY` and nothing that starts with `  | `.

## Runbook

Every identifier below is a placeholder. Replace `<...>` with your own value.
Never commit a real identifier, and never commit or log a presigned URL.

### 1. Make the source archive and its digest

Archive what the warm commands read: the lockfiles, the manifests and whatever
else the commands need to resolve and install. Smaller is better, and it must not
contain a secret.

```sh
# Archive the source directory. The archive is extracted at the root of the
# directory the commands start in.
tar -czf source.tar.gz -C <source-dir> .
# The digest identifies the lockfile basis of the image. Keep it for the tag.
SRC_SHA256=$(sha256sum source.tar.gz | cut -d' ' -f1)
```

### 2. Host the archive at a credential-free URL

The builder fetches the archive without a credential. A short-lived presigned URL
on a private bucket does that. The URL grants read access to the archive until it
expires: keep the lifetime short, never log it, never put it in a file you commit,
and never paste it into a ticket or a chat.

```sh
# Upload to a private bucket (block public access stays on).
aws s3 cp source.tar.gz s3://<bucket>/warm/source.tar.gz
# Presign for 30 minutes. Keep it in a shell variable; do not echo it.
SRC_URL=$(aws s3 presign s3://<bucket>/warm/source.tar.gz --expires-in 1800)
```

The URL ends up in the builder's user data, which anyone who can describe the
instance attribute can read until the builder is gone. `cloud-init clean --logs`
removes the saved copy on the builder, and the expiry bounds the rest.

### 3. Render the user data

Put the inputs in front of a short stub that unpacks the script, keeping
`#!/bin/bash` on line 1. The `printf '%q'` form quotes any value, including a
multi-line command list. The stub powers the builder off if it cannot unpack.

```sh
# The warm commands, one per line, and the optional offline gate. Replace them
# with your own; see "Examples" below for each ecosystem.
WARM_COMMANDS='uv sync --frozen'
WARM_GATE_COMMAND='rm -rf .venv && UV_OFFLINE=1 uv sync --frozen'

# The file holds the presigned URL: owner-only, and delete it after the launch.
umask 077
{
  printf '#!/bin/bash\n'
  printf 'export WARM_SRC_URL=%q\n' "$SRC_URL"
  printf 'export WARM_SRC_SHA256=%q\n' "$SRC_SHA256"
  printf 'export WARM_COMMANDS=%q\n' "$WARM_COMMANDS"
  printf 'export WARM_GATE_COMMAND=%q\n' "$WARM_GATE_COMMAND"
  # The script travels compressed, as in the automated path, to fit the limit.
  cat <<'STUB'
d=$(mktemp --directory /run/warm-stub.XXXXXX) || { shutdown -h now; exit 1; }
base64 --decode <<'WARM_PAYLOAD_END' | gunzip >"$d/warm-cache.sh" || { shutdown -h now; exit 1; }
STUB
  gzip -9n < deploy/runner/image/warm-cache.sh | base64
  cat <<'STUB'
WARM_PAYLOAD_END
exec bash "$d/warm-cache.sh"
STUB
} > user-data.sh
bash -n user-data.sh
# EC2 refuses user data of 16384 bytes or more. An oversized file is removed,
# so the launch below fails instead of sending it; the check returns nonzero.
if [ "$(wc -c < user-data.sh)" -ge 16384 ]; then
  echo "user data too large: move commands into the archive" >&2
  rm -f user-data.sh
  false
fi
```

The rendered file begins like this, with your own values in place of the
placeholders:

```text
#!/bin/bash
export WARM_SRC_URL=<presigned-url>
export WARM_SRC_SHA256=<source-sha256>
export WARM_COMMANDS=<warm-commands>
export WARM_GATE_COMMAND=<gate-command>
```

EC2 user data is limited to 16 KB before base64 encoding. The script travels
gzip-compressed and base64-encoded, about 8 KB, and a presigned URL is often 1.5
to 2.5 KB, which leaves about 5 KB for the stub and the commands. Put the command lists in scripts inside the source archive, where
the digest covers them, and keep the inputs to one line each:

```sh
WARM_COMMANDS='bash warm.sh'
WARM_GATE_COMMAND='bash warm-gate.sh'
```

### 4. Launch the builder from the base image

Use the current worker image as `<base-image-id>`: the layer is added to it. The
builder must terminate on shutdown and delete its storage on termination, or a
failed build leaves a stopped instance and a volume behind. Launch it without an
instance profile: the script needs no cloud credential, and without a role the
metadata service has none to give to the warm commands.

```sh
# One on-demand instance of the worker architecture, in a subnet that can reach
# the archive URL and the package hosts your commands download from.
aws ec2 run-instances \
  --image-id <base-image-id> \
  --instance-type <instance-type> \
  --subnet-id <subnet-id> \
  --security-group-ids <security-group-id> \
  --instance-initiated-shutdown-behavior terminate \
  --block-device-mappings '[{"DeviceName":"<root-device-name>","Ebs":{"DeleteOnTermination":true,"VolumeSize":<size-gib>,"VolumeType":"gp3"}}]' \
  --metadata-options HttpTokens=required,HttpPutResponseHopLimit=1 \
  --user-data file://user-data.sh \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=warm-image-builder}]' \
  --query 'Instances[0].InstanceId' --output text
# Remove the rendered user data now: it holds the presigned URL.
rm -f user-data.sh
```

Size the volume for the base image, the extracted source and the cache. The
script's own timer, `WARM_LIFETIME_SECONDS`, bounds the builder's lifetime even
if a command hangs.

### 5. Watch the console for the marker

```sh
# Poll the serial console for up to about an hour. The console lags the builder by
# a few minutes. Stop only on the whole IMAGE_READY line or on a failure line:
# every failure, including a refusal, prints "warm exiting rc=". A line can carry
# the cloud-init prefix, for example "[  279.87] cloud-init[671]: IMAGE_READY".
P='^(\[ *[0-9.]+\] )?(cloud-init\[[0-9]+\]: )?'
for _ in $(seq 1 120); do
  aws ec2 get-console-output --instance-id <builder-instance-id> --latest --output text \
    | grep -E "${P}(warm exiting rc=|WARM FAIL|IMAGE_READY\$)" && break
  sleep 30
done
# After IMAGE_READY, read the digest line the script prints just above it.
aws ec2 get-console-output --instance-id <builder-instance-id> --latest --output text \
  | grep -E "${P}WARM_SOURCE_SHA256 "
```

- `IMAGE_READY` is the only signal to create an image. The line above it,
  `WARM_SOURCE_SHA256 <digest>`, must equal `$SRC_SHA256`.
- `warm exiting rc=` or `WARM FAIL` means no image: a refusal prints
  `warm: refused: ...` and a failure prints the framed tail. Fix the commands or the
  source, and start again from step 1. The builder holds for 300 seconds and
  powers itself off.
- The console keeps a bounded tail, so read the framed tail soon after a failure.
- The script copies its output to the serial port, and cloud-init copies it to
  the console with its own prefix. When the serial login prompt starts, the
  copy to the port can fail with `tee: /dev/ttyS0: Input/output error`. From
  then on every line appears only with the cloud-init prefix, which the pattern
  above accepts. The build is not affected.

### 6. Create the image, tagged with the source digest

Create the image while the builder is still running; the snapshot window is the
time you have. `--no-reboot` keeps the builder up, and the script synced before it
printed the marker.

```sh
aws ec2 create-image \
  --instance-id <builder-instance-id> \
  --name "ci-burst-worker-warm-<YYYYMMDD-HHMM>" \
  --description "ci-burst worker: base image plus warm cache" \
  --no-reboot \
  --tag-specifications \
    "ResourceType=image,Tags=[{Key=warm-source-sha256,Value=$SRC_SHA256}]" \
    "ResourceType=snapshot,Tags=[{Key=warm-source-sha256,Value=$SRC_SHA256}]" \
  --query ImageId --output text
aws ec2 wait image-available --image-ids <new-image-id>
# The builder has served its purpose. Termination also removes its volume.
aws ec2 terminate-instances --instance-ids <builder-instance-id>
```

The `warm-source-sha256` tag is the traceability record: it names the archive, and
so the lockfiles, that the layer was built from. Record the base image id beside
it in your own deployment notes.

If your tagging policy limits the tag keys allowed on create (for example with
`aws:TagKeys`), AWS refuses the whole `create-image` call when this key is not on
the list. Add the key to the policy, or put the digest in `--description` and keep
only the allowed tags.

### 7. Publish a launch-template version

```sh
# A new version from the current one with only the image changed. The user data,
# the instance profile and every other setting stay as they are.
aws ec2 create-launch-template-version \
  --launch-template-id <launch-template-id> \
  --source-version <current-version> \
  --version-description "warm image <source-sha256-prefix>" \
  --launch-template-data '{"ImageId":"<new-image-id>"}'
```

Do not change the default version, and do not move the deployment configuration
yet. ci-burst pins a numeric `launch_template_version`, so the controller keeps
using the version it is configured with.

### Canary and cutover

Before the move, run a canary: it is an operator obligation, and the controller
does not enforce it or check for its evidence.

1. Point a test identity or a test deployment at the new version and run a job
   from a test repository through it.
2. Confirm the job finds the warm cache and downloads little, and that its result
   matches a job on the current version.
3. Compare job duration for the same jobs on the cold and the warm version, and
   record it. Only then do the rollout in the image README: set the deployment
   configuration's `launch_template_version` to the new version and run the
   toolchain check on a launched worker ([Build](../deploy/runner/image/README.md#build)
   steps 4 and 5).

Moving the version back is the rollback.

## Examples

Each example pairs warm commands with an offline gate. The commands run in the
cache environment above, from the extracted source root, as the `runner`
account. They are generic starting points: adapt the paths to your layout and
check them on your own builder before you rely on them.

A gate forbids downloads in the way that tool offers, so a missing cache entry
fails the gate instead of being fetched. The script does not block the network;
the mechanism is the command's.

### uv

```sh
# WARM_COMMANDS: one line per project that has a lockfile.
uv sync --frozen
cd <project-dir> && uv sync --frozen

# WARM_GATE_COMMAND: remove the environment, then rebuild it from the cache alone.
rm -rf .venv && UV_OFFLINE=1 uv sync --frozen
```

`UV_CACHE_DIR` is already set, so the downloaded and built wheels land in the
shared cache.

### npm

```sh
# WARM_COMMANDS
npm ci

# WARM_GATE_COMMAND: reinstall from the cache with the network forbidden.
rm -rf node_modules && npm ci --offline
```

### pip

The base image installs `python3` without `pip`, so run pip through `uvx` or use
the one your project provides.

```sh
# WARM_COMMANDS: download every locked distribution into a directory under the cache.
uvx pip download -r requirements.txt -d "$PIP_CACHE_DIR/downloads"

# WARM_GATE_COMMAND: install from that directory alone, into a throwaway target
# inside the source tree, which the script removes.
uvx pip install --no-index --find-links "$PIP_CACHE_DIR/downloads" -r requirements.txt --target .gate-target
```

A job reads this directory only when its own `pip install` passes
`--find-links` for it. An alternative that a plain `pip install` finds by itself
is an install into a throwaway environment, which fills pip's HTTP cache under
`PIP_CACHE_DIR`. The gate above proves the directory form only.

### Playwright browsers

```sh
# WARM_COMMANDS: install the browser into a path under the cache.
npm ci
PLAYWRIGHT_BROWSERS_PATH="$XDG_CACHE_HOME/ms-playwright" npx playwright install chromium

# WARM_GATE_COMMAND: launch it from the cache; a missing browser fails.
PLAYWRIGHT_BROWSERS_PATH="$XDG_CACHE_HOME/ms-playwright" node -e "require('playwright').chromium.launch().then(b => b.close())"
```

The job environment is the bootstrap's, and the bootstrap does not set
`PLAYWRIGHT_BROWSERS_PATH`. A job that does not set it looks in the default
location, finds nothing and downloads again. The consumer's workflow therefore
sets the same path for the job:

```yaml
env:
  PLAYWRIGHT_BROWSERS_PATH: /var/lib/forgejo-runner/cache/ms-playwright
```

The system libraries a browser needs are part of the base image, not the warm
layer.

### A monorepo check runner

When a repository has its own check runner, give it a prime mode that fills
every cache the checks read and an offline mode that fails on any download. The
two flags below are placeholders for whatever your runner offers.

```sh
# WARM_COMMANDS: fill the caches for every check group.
./scripts/check --prime

# WARM_GATE_COMMAND: run one group with downloads forbidden.
./scripts/check --offline --group <group>
```

## Size

Expect the layer to be on the order of one to a few gigabytes. One consumer's
cache measured about 1.8 GB. That figure is an order of magnitude, not a limit:
the script prints `CACHE size=...` before the marker so you can record yours. The
snapshot stores the whole image, so a larger layer costs storage and a slower
first read on each new worker.

## Rebuild when a lockfile changes

Rebuild the layer when a lockfile that the warm commands read changes, and when
the base image changes, because the layer is added to one base image. Repeat the
runbook with a new archive. The new digest identifies the new image.

An older warm image does not break a job. For a content-addressed package or
download cache (uv, npm, pip, browser downloads) a stale entry costs only time:
the job resolves its own lockfile against the cache and downloads what is
missing.

That claim stops at those caches. Any other cache a consumer places under
`cache/`, such as a validation or result cache, is the consumer's to key on its
source, lockfiles, toolchain and environment. ci-burst does not guarantee that
such a cache is valid for the job that reads it, and nothing here says a stale
entry is harmless.
