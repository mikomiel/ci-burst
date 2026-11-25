# Running ci-burst in a container

This document is the operator contract for the `Dockerfile`, `.dockerignore`
and the systemd units in `deploy/`. It does not authorize a deployment, a
cloud account change or a pilot; [operations.md](operations.md) remains the
product contract and [SECURITY.md](../SECURITY.md) remains the security
boundary.

The image and units are source artifacts. CI builds the image from the
repository root on every push to `main` and every pull request to prove the
`Dockerfile` still builds, using the workflow's read-only token. That job
publishes nothing: no registry login, no push, no release and no deployment.
The only tag it creates is a local one inside the ephemeral runner, and a green
build is neither a release nor a deployment. A local build downloads the base
image and `boto3`, so it needs network access, as does the CI build; neither
needs a credential.

## What the image is

- One command per process: `plan`, `apply`, `status` or `reap --apply`, with
  `--config` pointing at the mounted TOML. The optional warm-image build adds
  the `image` group (`image plan`, `image run`, `image status`,
  `image promote`), run the same way as one-off containers; see
  [Run the image commands](#run-the-image-commands).
- Non-root: the process runs as UID/GID 10001.
- No daemon, no published ports, no healthcheck. A run finishes with the
  controller's exit code: `0` completed decision, `2` refusal with nothing
  mutated, `1` provider or cleanup failure. A cleanup pass that refused an owned
  resource (`cleanup_degraded`) is a cleanup failure and exits `1`. The exit
  code is the only completion signal. The `image` group differs: its exit `2`
  names what the run declined, and the run may have changed state before it
  declined ([exit codes](operations.md#image-commands)).
- The units run it with a read-only root filesystem and a tmpfs for `/tmp`.
  Every durable write belongs in the mounted state directory; the tmpfs covers
  transient files.
- Each unit names its container (`ci-burst-apply`, `ci-burst-reap`) and
  force-removes it in `ExecStopPost`. The image starts the CLI directly and the
  CLI has no signal handler, so without an init it would run as container PID 1
  and ignore SIGTERM; the units therefore pass `--init`, but a stopped or
  timed-out unit is still not by itself proof that the run ended.

The container is not a scheduler. Forgejo owns job assignment and results;
ci-burst launches up to the configured `controller.max_instances` Spot instances
per deployment (an integer in 1..12, default 1).

## Prerequisites

- Rootful podman with the `catatonit` init binary (the units run
  `podman run --init`) and systemd on both the scaler host and the cleanup
  host. The commands and units assume the default podman user namespace:
  container UID/GID 10001 is host UID/GID 10001.
- A named podman network, `ci-burst`, created with the MTU the host's path
  supports (see below). The units and the refresh smoke run on it; the default
  bridge is not used.
- A deployment TOML prepared from [operations.md](operations.md), including
  the `forgejo.qualification` decision. `fixtures-only` still blocks `apply`.
- A scaling identity and a separate cleanup identity, each provided as a
  read-only credentials directory. Never place credentials in the TOML.
- Outbound network access to the Forgejo server and the AWS endpoints. The
  container publishes no port and needs no inbound access.

### The container network

Create the network once on every host that runs a unit:

```sh
podman network create --opt mtu=1300 ci-burst
```

The MTU is why this network exists. A container bridge defaults to 1500 bytes,
and where the host's path cannot carry that, large TLS responses stall with no
error: the connection hangs instead of failing, and only the requests with small
responses come back. That asymmetry is the dangerous part — `plan` can look
healthy while `apply` times out against the provider and the Forgejo API, which
is what happened on the first live host before the network was named here.
Nothing clamps MSS on such a path, so the container's own limit has to match it.
`1300` is the value used in these examples. Measure the path on the host: use
the measured value when it is lower than 1500, and omit `--opt mtu` only when
the path carries the default 1500.

Do not work around it with `--network=host` for a controller unit: the units keep
their own network namespace, so the loopback warning below stays true and a
loopback address in the deployment TOML still names the container, not the
Forgejo server. The one legitimate exception is the cleanup host, where `reap`
talks only to AWS and the host's podman networking has no egress; that host
installs `deploy/ci-burst-reap-host-network.conf` and nothing else changes.

## Build the image

From a checkout of this repository:

```sh
podman build --network=host --tag localhost/ci-burst:latest .
```

The build context is default-deny: only `pyproject.toml`, `README.md`,
`LICENSE`, `ci_burst/` and `deploy/runner/image/warm-cache.sh` enter it.
`pip install .` installs the CLI, and the warm script is copied to
`/srv/ci-burst/share/warm-cache.sh` for automated image builds. The tag stays
local; nothing is pushed.

Both hosts run the same local tag. Build it on each host, or build once and
copy it with `podman save` and `podman load`. No registry is involved.

## Prepare the host paths

The scaler host runs `apply`; a separate cleanup host runs `reap --apply`.
[operations.md](operations.md) requires cleanup outside the scaler's host
failure boundary, so the two installs are separate. Both hosts need the image
and the deployment TOML; only the scaler host needs the state directory, the
scaling credentials and the token env-file; only the cleanup host needs the
cleanup credentials.

| Path | Host | Access in container | Purpose |
| --- | --- | --- | --- |
| `/etc/ci-burst/deployment.toml` | both | read-only file | deployment TOML |
| `/var/lib/ci-burst` | scaler | writable directory | SQLite ledger, `.lock`, rollback journal |
| `/etc/ci-burst/aws-scaling` | scaler | read-only directory | scaling credentials, launch-capable profile |
| `/etc/ci-burst/aws-cleanup` | cleanup | read-only directory | cleanup credentials, describe/terminate only |
| `/etc/ci-burst/forgejo.env` | scaler | podman env-file, not mounted | Forgejo token |
| `/var/lib/ci-burst-warm-image` | scaler, image commands only | writable directory | image state file, its lock, the event file, the metrics textfile |
| `/etc/ci-burst/aws-image` | scaler, image commands only | read-only directory | image role profile: the source identity and the assume-role profile, no scaling or cleanup keys |
| `/etc/ci-burst/forgejo-image.env` | scaler, image commands only | podman env-file, not mounted | image token, a different variable from the Forgejo token |
| `/etc/ci-burst-deployment` | scaler, image commands only | read-only directory; writable for `promote --write` | the dedicated directory of the deployment TOML |

The last four rows are optional and belong to the image commands.
[deployment.md](deployment.md#the-image-build-paths-optional) shows how to create
them and [Run the image commands](#run-the-image-commands) mounts them. The image
state directory is not inside `/var/lib/ci-burst`, so no image container sees the
scaling ledger.

Prepare the scaler host:

```sh
install -d -o root -g root -m 0755 /etc/ci-burst
install -o root -g root -m 0644 deployment.toml /etc/ci-burst/deployment.toml
install -d -o 10001 -g 10001 -m 0700 /var/lib/ci-burst
install -d -o root -g 10001 -m 0750 /etc/ci-burst/aws-scaling
install -o root -g 10001 -m 0640 scaling-credentials /etc/ci-burst/aws-scaling/credentials
install -o root -g root -m 0600 forgejo.env /etc/ci-burst/forgejo.env
```

Prepare the independent cleanup host:

```sh
install -d -o root -g root -m 0755 /etc/ci-burst
install -o root -g root -m 0644 deployment.toml /etc/ci-burst/deployment.toml
install -d -o root -g 10001 -m 0750 /etc/ci-burst/aws-cleanup
install -o root -g 10001 -m 0640 cleanup-credentials /etc/ci-burst/aws-cleanup/credentials
```

The cleanup host needs no state directory, no scaling credentials and no token
env-file.

The AWS SDK reads `/etc/ci-burst/aws/credentials` and, if present,
`/etc/ci-burst/aws/config` inside the container. The mount target is
`/etc/ci-burst/aws`; each unit mounts only its own source directory.

The two credential installs take different source files on purpose. Each
destination is mounted into a different unit, so the scaling unit never sees
the cleanup file and the cleanup unit never sees the scaling file. Both files
must define the profile that `aws.profile` names, or `default` when that key is
unset: the mount selects the file, not the profile, and the shared TOML names
the same profile for both units. The scaling file holds the launch-capable
scaling identity. The cleanup file holds the cleanup identity, which keeps
`DescribeInstances` and `TerminateInstances` without launch permission. Never
copy the scaling file into the cleanup directory: cleanup would inherit launch
permission, and a broken scaling identity would take cleanup with it.

The image also sets `AWS_EC2_METADATA_DISABLED=true`, so a missing or unreadable
credentials file fails instead of falling back to a reachable instance role.
Override that variable only if you deliberately use an instance profile, which
is not the two mounted identities this shape expects.

Under rootless podman the mapping differs on both hosts. Add
`--userns=keep-id:uid=10001,gid=10001` to the run command so container UID/GID
10001 maps to your user, and make the files and directories each host uses
readable by that user instead of 10001.

### Deployment TOML for a container

Two values are specific to this shape:

```toml
[forgejo]
base_url = "https://forgejo.example.com"
token_env = "CI_BURST_FORGEJO_TOKEN"

[state]
database = "/var/lib/ci-burst/state.sqlite3"
```

`[state] database` must be absolute and inside `/var/lib/ci-burst`. A relative
path resolves against the TOML file's directory, which is a read-only mount, so
the run would fail before reaching a decision. Keep the ledger, its `.lock` and
its rollback journal in that one directory.

`forgejo.base_url` must be the Forgejo server's routable HTTPS name. See the
loopback warning below.

### Forgejo token

The scaler host supplies the token with an env-file, which is visible in
`podman inspect`:

```sh
CI_BURST_FORGEJO_TOKEN=replace-with-an-owner-scoped-token
```

Anyone who can inspect the container can read that value, and the value is also
in the container process environment. Treat container-inspect access on this
host as token access, keep the file mode `0600`, and rotate the token if that
access changes hands. A token-file option is a follow-up, not part of this
change. Do not put AWS keys in this file; cloud credentials are mounted files.

The image commands use a separate read-only token in `forgejo-image.env`, never
this one; [deployment.md](deployment.md#the-image-token) owns it.

## Run one command manually

The scheduled units run the same shape with their fixed container name. Run
the `apply` invocation on the scaler host and the `reap` invocation on the
cleanup host. To run `apply` by hand as root:

```sh
podman run --rm --init --network ci-burst --name ci-burst-manual \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
  --volume /etc/ci-burst/deployment.toml:/etc/ci-burst/deployment.toml:ro \
  --volume /var/lib/ci-burst:/var/lib/ci-burst:rw \
  --volume /etc/ci-burst/aws-scaling:/etc/ci-burst/aws:ro \
  --env-file /etc/ci-burst/forgejo.env \
  localhost/ci-burst:latest apply --config /etc/ci-burst/deployment.toml
```

For `plan` or `status`, replace `apply` with the command name and keep the
mounts. Both are observation paths: they open an existing ledger read-only and
never create or migrate it. Run `plan` before the first `apply`; it performs no
durable write and no cloud mutation.

To request expiry termination by hand, use the cleanup identity and drop the
token and the state directory, which `reap` does not need:

```sh
podman run --rm --init --network ci-burst --name ci-burst-reap-manual \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
  --volume /etc/ci-burst/deployment.toml:/etc/ci-burst/deployment.toml:ro \
  --volume /etc/ci-burst/aws-cleanup:/etc/ci-burst/aws:ro \
  localhost/ci-burst:latest reap --apply --config /etc/ci-burst/deployment.toml
```

Every run prints one JSON document with stable reason codes. There is no
daemon to restart: each run reads the TOML, the credentials and the env-file
fresh.

A manual run is named `ci-burst-manual` (`ci-burst-reap-manual` for `reap`).
If it does not exit after `Ctrl-C`, remove it by name with
`podman rm --force ci-burst-manual`; a survivor blocks the next manual run and
can still be inside a provider call.

On a host with SELinux enforcing, add a relabel option to each bind mount. The
deployment TOML is mounted by the timer and by manual runs, so it can be in use
by two containers at the same time; give it the shared label:
`.../deployment.toml:ro,z`. Use the private label `:Z` only for a source one
unit uses alone, such as
`/var/lib/ci-burst:rw,Z` or `/etc/ci-burst/aws-scaling:ro,Z`. `:Z` relabels the
source to one container's private category and breaks any other container using
it at that moment, so do not mix `:z` and `:Z` on one source. Where relabeling
is not permitted, apply a `container_file_t` label to the paths once with
`chcon` instead.

## Run the image commands

The `image` commands belong to the optional automated warm-image build.
[operations.md](operations.md#image-commands) owns their meaning, exit codes and
reason codes, and [deployment.md](deployment.md#the-image-tables-optional) owns the
`[image]` tables. No unit or timer runs them: you start each one as a one-off
container on the scaler host, and the apply, reap and export units stay as they
are. The reaper host takes none of this. While `[image]` is absent or
`enabled = false`, each line below answers `disabled` before it opens a state file
or builds a client.

To build an image, run the build line. A build lasts longer than one pass, so
`--until-done` repeats passes in the one container until the build ends:

```sh
podman run --rm --init --network ci-burst --name ci-burst-image-manual \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
  --volume /etc/ci-burst-deployment:/etc/ci-burst-deployment:ro \
  --volume /var/lib/ci-burst-warm-image:/var/lib/ci-burst-warm-image:rw \
  --volume /etc/ci-burst/aws-image:/etc/ci-burst/aws:ro \
  --env-file /etc/ci-burst/forgejo-image.env \
  localhost/ci-burst:latest image run --apply --until-done --config /etc/ci-burst-deployment/deployment.toml
```

The container gets the dedicated TOML directory read-only
([deployment.md](deployment.md#the-dedicated-toml-directory) holds the TOML in
it), the image role profile directory read-only, the image state directory writable
at the path `[image].state_directory` names, and the image token env-file. It gets
no scaling credential, no cleanup credential, no scaling ledger and no
`forgejo.env`. For `image plan` and `image run` without `--apply`, replace the
command and keep everything else. `image status --refresh` reads only the provider
with the image role and nothing from the forge, so keep the mounts and leave out the
env-file. `image status` without `--refresh` makes no provider call, so it needs
neither the profile directory nor the token.

The line mounts the directory, never the file. `promote --write` replaces the TOML
atomically, and a mount of the single file keeps serving the file that was there
when the container started. With the directory mounted, retention reads the replacement
file that a promotion wrote on its next pass: retention reloads the
TOML on every pass, to find the pins it must keep. The rest of a run keeps the
configuration that the container loaded when it started. So finish or stop a running
`image run --apply --until-done` container before `promote --write`,
`promote --rollback --write` or a hand edit of the pin, and start a fresh container
afterwards: the state file keeps the active build, and the fresh container resumes
it. A hand edit of the pin takes no lock, so before one also pause any scheduler
of image runs and wait until no `image run --apply` is running: a pass that has
already planned its deletions does not read the pin again, and can remove the
image you are about to pin. Do not widen the mount to `/etc/ci-burst`, which holds
the credential directories.

While any image command runs, do not edit a setting of the file other than the pin,
replace or re-link the file, swap a mount or move the state directory. The one
exception is `enabled = false`, which stops the image path. It does not stop a
running command: follow [Rolling back an image](operations.md#rolling-back-an-image)
and wait until no `image run --apply` runs.
[Operator contract while an image command runs](operations.md#operator-contract-while-an-image-command-runs)
says what ci-burst checks and what it does not promise.

To promote a candidate, or to roll back, run the promotion line. Before the final
preview, pause any scheduler of image runs and wait until no `image run --apply`
of any kind is running, and keep it so through the write. Name the candidate with
`--version N` in both the preview and the write, and canary the image of that
version from a separate test launch template
([warm-image.md](warm-image.md#canary-and-cutover)): without `--version`, each command selects the newest verified
candidate again. `promote` is local: it makes
no AWS or forge call and holds no credential, so its container has no network, no
credential mount and no env-file, and a read-only root filesystem:

```sh
podman run --rm --init --network none --name ci-burst-promote-manual \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
  --volume /etc/ci-burst-deployment:/etc/ci-burst-deployment:rw \
  --volume /var/lib/ci-burst-warm-image:/var/lib/ci-burst-warm-image:rw \
  localhost/ci-burst:latest image promote --config /etc/ci-burst-deployment/deployment.toml --version N --write
```

The line has two writable host mounts and no more. The dedicated TOML directory is
writable because `--write` creates a backup, a lock file and a temporary file there
and replaces the TOML. The image state directory is writable because the promotion
records its journal row and its event there. Everything else that the container can
write is a tmpfs that vanishes with it: the explicit one on `/tmp`, and those that
Podman adds to a read-only container by default (`--read-only-tmpfs` covers `/dev`,
`/dev/shm`, `/run` and `/var/tmp`). `promote` writes its own files only to the two
directories. The explicit option stays because Podman mounts a writable tmpfs on
`/tmp` of a read-only container anyway, and the option caps that one at 64 MiB and
forbids execution in it.

Leave out `--write`, and keep `--version N`, to print the evidence and the one-line
diff with the same mounts. For a rollback, use `--rollback` in place of
`--version N`. The directory must hold
only the TOML, its `.bak.*` backups, its lock file and its promote temporary file,
or the command refuses with `PROMOTE_DIRECTORY_NOT_DEDICATED`
([deployment.md](deployment.md#the-dedicated-toml-directory)). The shipped units
still mount the TOML as one file: change only the host side of the `--volume` that
each unit uses for it, so that it reads the file in this directory; the container
side and `--config` stay as they are.

A manual run is named `ci-burst-image-manual` or `ci-burst-promote-manual`. If one
does not exit after `Ctrl-C`, remove it by name with `podman rm --force`; a
survivor blocks the next run of the same name and can still be inside a provider
call. With SELinux enforcing, the rule above holds: a source that two lines mount
takes the shared `:z`, and only a source that one line uses alone may take `:Z`.
Both lines mount the image state directory and the dedicated TOML directory, so
both take `:z`.

## Scheduled operation with systemd

Install only the apply unit and timer on the scaler host:

```sh
# scaler host
podman network create --opt mtu=1300 ci-burst   # once, see "The container network"
install -m 0644 \
  deploy/ci-burst-apply.service deploy/ci-burst-apply.timer \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ci-burst-apply.timer
systemctl list-timers 'ci-burst-*'
```

The observation unit is optional and independent of admission. It runs the same
image with no token env-file, writes its textfile into the state directory, and
publishes that file into the collector's directory from `ExecStartPost`:

```sh
# scaler host, optional
install -m 0644 \
  deploy/ci-burst-export.service deploy/ci-burst-export.timer \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ci-burst-export.timer
```

The container runs as uid 10001 while the collector's textfile directory is
root-owned, which is why publishing is a root step in the unit rather than a
container mount: the container writes
`/var/lib/ci-burst/export/ci-burst.prom`, and two `ExecStartPost` lines stage it
beside the collector's file and rename it into place. A rename inside one
directory is atomic, so a scrape sees either the previous sample set or the new
one. Point both lines at your collector's directory if it is not
`/var/lib/prometheus/node-exporter`.

Install only the reap unit and timer on the independent cleanup host:

```sh
# independent cleanup host
podman network create --opt mtu=1300 ci-burst   # once, see "The container network"
install -m 0644 \
  deploy/ci-burst-reap.service deploy/ci-burst-reap.timer \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ci-burst-reap.timer
systemctl list-timers 'ci-burst-*'
```

The cleanup host must be outside the scaler's failure domain: a reboot, a
failed upgrade or a full disk on the scaler must not stop expiry termination.
[operations.md](operations.md) owns that rule. The reap unit needs no Forgejo
token and no state directory, so it keeps working when admission is stopped,
credentials for the scaler are broken, or the ledger is unavailable.

`ci-burst-apply.timer` starts one `apply` run 60 seconds after the previous run
finished; the effective period is the run duration plus about 60 seconds. The
timer never starts a second copy of the unit, and the ledger's writer lock would
refuse one anyway. Disabling the timer does not stop a run that is already
executing: stop the service and remove its named container before state is
inspected or moved. `systemctl is-active` describes the launcher only, never a
surviving container.

The apply unit's `TimeoutStartSec=300` is the outer bound. The controller's own
`controller.pass_deadline_seconds` (default 240) is shorter, so a pass that
reaches its own budget exits with `pass_deadline_exceeded` and
`phases_deferred` before systemd stops the unit at 300 seconds. The worker
lifetime (`aws.max_lifetime_seconds`, default 3600) is the instance's lifetime,
not this pass bound. The reap unit has its own `TimeoutStartSec` and timer, and
does not use the scaler's pass deadline.

`ci-burst-reap.timer` runs `reap --apply` five minutes after the previous run
finished, on the cleanup host.

Each run is recorded by systemd with its exit code. On the scaler host read:

```sh
journalctl -u ci-burst-apply.service -n 50
systemctl status ci-burst-apply.service
```

On the cleanup host read:

```sh
journalctl -u ci-burst-reap.service -n 50
systemctl status ci-burst-reap.service
```

A failed run does not stop the timers; the next scheduled run tries again. A
persistent refusal is a signal to read the JSON reason code and `status`, not
to raise the run frequency or remove the lock.

The cleanup host is monitored from the journal, because `reap` has no exporter,
textfile or healthcheck. The exit status says whether systemd saw the run
complete; the document says what that run established. The CLI pretty-prints
one JSON document over several lines, so asking for a single line (`-n 1`)
shows a fragment or an unrelated systemd line. Read a bounded time window
instead:

```sh
journalctl -u ci-burst-reap.service --since -10min -o cat
```

That window shows the recent runs' journal output; a run's document appears in
full because the CLI writes it when the run ends. Find it by its top-level
`decision`, then read the `cleanup` object in the same document for that run's
counts:

- `decision: planned` with exit `0` is a dry run.
- `decision: termination_requested` with exit `0` means the pass refused
  nothing. Read its `cleanup` block: `requested` is not `terminated`,
  `inventory_verified: true` with zero counts means one complete read returned
  no owned records, and neither is a zero AWS bill.
- `decision: cleanup_degraded`, exit `1`, means at least one owned instance was
  refused and is still unresolved; `refused_resources` names it. The graded
  counts stay in `cleanup`.
- `decision: cleanup_failed`, exit `1`, means the provider refused the
  termination call; `denied` counts the requested identifiers.

An absent or stale `ci-burst-reap` journal entry proves nothing by itself:
journald rotation, a down cleanup host and a timer that was never installed all
look the same. Alert on the scheduled run's absence or nonzero exit, never on a
missing or zero-count document.

## Keep the image current

The image is a local tag, so a merged controller change reaches a host only when
that host builds it. `ci-burst-refresh.timer` does that on the scaler host: one
run compares the deployed commit with `origin/main` and does nothing when they
match; otherwise it builds a candidate, smoke-tests it and only then promotes it.

Install it beside the apply unit, from a clone of this repository:

```sh
# scaler host
git clone https://github.com/mikomiel/ci-burst.git /opt/ci-burst-src
install -d -m 0700 /var/lib/ci-burst-image
install -m 0755 deploy/ci-burst-refresh.sh /usr/local/sbin/ci-burst-refresh
install -m 0755 deploy/ci-burst-state.py /usr/local/sbin/ci-burst-state
podman network create --opt mtu=1300 ci-burst   # once, see "The container network"
install -m 0644 \
  deploy/ci-burst-refresh.service deploy/ci-burst-refresh.timer \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ci-burst-refresh.timer
systemctl list-timers 'ci-burst-*'
```

The script defaults to `/opt/ci-burst-src`, `/var/lib/ci-burst-image`,
`/etc/ci-burst/deployment.toml`, `/etc/ci-burst/aws-scaling` and
`/etc/ci-burst/forgejo.env`; `CI_BURST_SOURCE`, `CI_BURST_IMAGE_STATE`,
`CI_BURST_CONFIG`, `CI_BURST_AWS_DIR` and `CI_BURST_TOKEN_ENV` override them.
`CI_BURST_SMOKE_COMMAND` (default `plan`), `CI_BURST_SMOKE_NETWORK` (default
`ci-burst`) and `CI_BURST_UNITS` adapt the smoke and the drift list to a host
whose role is cleanup-only.

`ci-burst-refresh.service` runs `ci-burst-state` as its `ExecStopPost`, so every
attempt writes `/var/lib/ci-burst-image/deployment-state.json`: the deployed and
channel commits, the image ID, the last success, and any unit drift against the
deployed commit. The document declares `schema: deployment-state.v1` and is
written for any external metrics collector; no credential, host address or
private path is included.

To freeze the serving image without stopping observation, write
`/var/lib/ci-burst-image/hold` with a short reason. The next refresh still
fetches the channel ref but skips the build and promotion, and the state document
reports `pinned` with the distance behind. The installed unit sets the logical
`CI_BURST_TARGET` (`controller`; the cleanup drop-in sets `reaper`), so the
document never carries a hostname. The channel observation is a separate
`source-sha` marker written only after a successful fetch; until it exists the
document reports unknown source freshness rather than inferring one from
`FETCH_HEAD`.

To roll back, follow the "Rollback" procedure below: it disables the timer and
stops the service under the refresh lock before retagging, so an in-flight run
cannot promote over the rollback. Write the hold before restarting the timer.
Removing the hold later rebuilds the channel image because the serving revision
label no longer matches the channel, rather than silently reporting the old
`deployed-sha` as current.

A cleanup-only host runs the same unit and script with a drop-in, because its
scheduled operation is `reap` rather than `apply`:

```sh
# cleanup host
git clone https://github.com/mikomiel/ci-burst.git /opt/ci-burst-src
install -d -m 0700 /var/lib/ci-burst-image
install -m 0755 deploy/ci-burst-refresh.sh /usr/local/sbin/ci-burst-refresh
install -m 0755 deploy/ci-burst-state.py /usr/local/sbin/ci-burst-state
install -m 0644 \
  deploy/ci-burst-refresh.service deploy/ci-burst-refresh.timer \
  /etc/systemd/system/
install -d -m 0755 /etc/systemd/system/ci-burst-refresh.service.d
install -m 0644 deploy/ci-burst-refresh-cleanup.conf \
  /etc/systemd/system/ci-burst-refresh.service.d/cleanup.conf
systemctl daemon-reload
systemctl enable --now ci-burst-refresh.timer
systemctl list-timers 'ci-burst-*'
```

If the host cannot use podman's named network, install
`deploy/ci-burst-reap-host-network.conf` beside the reap unit at the same time and
keep `CI_BURST_SMOKE_NETWORK=host` in the refresh drop-in; see the networking
The cleanup drop-in also sets `CI_BURST_TARGET=reaper` and its own
`CI_BURST_UNITS` list; do not install it on the controller.

paragraph below. Without that drop-in, remove the line so the smoke uses the
shipped unit's network.

The drop-in is what makes the same unit safe on both hosts: the smoke runs
`reap` without `--apply` (it reads this host's configuration, reaches the cleanup
identity and selects expired instances without requesting their termination — a
dry run may list them), the cleanup credentials mount where the scaling
credentials mount on the controller host, the Forgejo token path is emptied so no
`--env-file` is passed, and the drift report compares that host's own units. The
accepted decision there is `planned`; `wait` or `would_launch` would mean the
smoke ran the wrong command and is refused.

**The smoke network must match the installed reap unit.** A host that keeps the
shipped `ci-burst-reap.service` keeps the script's default (`ci-burst`) and must
not carry the `CI_BURST_SMOKE_NETWORK=host` line. A host whose podman networking
has no egress installs `deploy/ci-burst-reap-host-network.conf`, which moves the
scheduled unit to host networking; the same host sets `host` for the smoke. A
mismatch promotes an image the scheduled unit cannot run, which is the failure
this gate exists to catch.

What a promotion does and does not do:

- it retags `localhost/ci-burst:latest`, which the scheduled units resolve on
  every run, plus an immutable `localhost/ci-burst:sha-<12>`. No run needs a
  restart, and a container already running keeps the layer it started with;
- before `latest` moves, it tags whatever `latest` pointed at as
  `localhost/ci-burst:previous`. That is the rollback handle for the first
  refresh too, when the serving image is a hand-built one with no `sha-<12>` tag
  of its own;
- after the serving tag has moved, it removes the build tags it no longer owes.
  Every merged commit used to leave one `sha-<12>` tag at about 150 MB forever,
  which filled a 7.8 GB host. The retained set is `latest`, `previous`, the tag
  of the deployed commit recorded on the host, the tag of every 40-hex ref the
  deployment-state document names, and the newest `CI_BURST_RETAIN_BUILDS`
  builds overall (default 3, by image creation time): the window is computed
  independently and then united with the named refs. Only `sha-<12>` tags this deployment created
  are candidates. The whole eviction plan is computed first, and each removal
  names its image and that one full `IMAGE:TAG` name: an image that another tag
  still names keeps that tag. A tag whose image cannot be inspected is kept and
  reported, and a retained ref that cannot be read fails closed — a present but
  unparsable state document, or a corrupted `deployed-sha`, removes nothing. The
  step runs only where a run has proven the serving image is the promoted one:
  the promote path, or the already-current path so a refused cleanup is retried.
  The prune is scoped by the deployment's own `org.ci-burst.build=refresh` label,
  not by the standard OCI revision label, and it runs on every such pass so a
  refused prune is retried rather than waiting for the next eviction. The step
  logs `retention: removed <n> build tags, pruned owned dangling images`,
  `retention_kept: <tag> cannot be inspected` or `retention: kept <n> tags
  unverified`. A refusal logs `retention_failed: <reason>; <n> tags left` on
  stderr and, when `logger` is present, at `user.warning` priority so it is
  visible at warning level in the refresh unit's journal; a refusal changes
  neither the exit status nor the promotion, because the serving image is
  already correct. Nothing alerts on that line yet: an alert would read the
  journal or the `job=ci-burst-worker` stream, and that stream is still unproven
  (HB-3339);
- it records the deployed commit in `/var/lib/ci-burst-image/deployed-sha`, and
  writes an authoritative build record after the smoke passes and before
  `latest` moves, at
  `/var/lib/ci-burst-image/builds/images/<image-id>.json` (the image ID without
  its `sha256:` prefix): the build inputs it resolved — the `Dockerfile` sha256
  and its `FROM` line, the base-image digest, the built image ID and the Python
  distribution set read from the candidate with `--network=none` — plus a
  bounded list of attestations, one per source commit that produced those
  bytes, each with the source and tree SHA, the podman version and a UTC
  timestamp; the JSON carries `schema: ci-burst.build-inputs.v2`. A
  source-keyed reference, `/var/lib/ci-burst-image/builds/sha-<12>.json`, names
  the record that source currently builds; it carries
  `schema: ci-burst.build-inputs-ref.v1` with the source SHA, the image ID and
  the record path, and never the inputs themselves. The run logs
  `build_inputs: <record>` for a new record,
  `build_inputs_attested: <record> source=<sha>` when it appends a second source
  to an existing one, `build_inputs_kept: <record>` when the source is already
  attested, and `build_inputs_ref: <reference>`. A record or reference that
  cannot be written fails the run before promotion, so an image that serves is
  an image whose inputs were recorded;
- it **reports** unit drift instead of installing it: the repository's
  `deploy/` units are compared with the installed ones, and a difference logs
  `units_drift: <names>` (or `units_current`). Installing a changed unit stays
  the manual `install` step in § "Scheduled operation with systemd", because
  installing scheduler units unattended needs atomic generations, rollback
  pairing and admission control — a transaction whose failure modes cost more
  than the rare manual step it would remove. The report exists so the
  difference never grows silently. `CI_BURST_UNITS` narrows the comparison to the
  units a host actually runs, so a cleanup-only host does not report the
  controller's units as missing on every run;
- the smoke proves the role the host actually runs: `plan` on a controller host,
  `reap` without `--apply` on a cleanup-only host. A candidate that cannot read
  this host's configuration or reach this host's own identity never becomes
  `latest`, which is what stops a stale image from silently disabling expiry
  cleanup after a configuration change;
- it publishes nothing: no registry, no git tag, no release, no announcement. A
  merge deploys the controller; [CONTRIBUTING.md](../CONTRIBUTING.md) § Releases
  owns the release boundary, and a merge is still neither a release nor a
  deployment of a released artifact;
- the build context is a tar archive of the fetched commit, not the working
  tree, so an edited or untracked file in the clone can never enter the image
  under a clean commit's name. The clone is only a fetch cache and is never
  reset, cleaned or checked out;
- the build runs with host networking (`podman build --network=host`). The base
  image and the package install are large transfers too, and a bridge whose MTU
  is above the host's path MTU stalls them the same way it stalls the
  controller's own responses. The smoke run uses the same network as the
  scheduled unit on that host — the named `ci-burst` network by default, or host
  networking where the reap unit needs the versioned override — so the gate
  exercises exactly the networking that unit uses;
- a fetch, archive, build or smoke failure keeps the previous image in place and
  logs one line with a reason code (`fetch_failed`, `archive_failed`,
  `unpack_failed`, `build_failed`, `build_inputs_failed`,
  `build_inputs_full`, `smoke_failed`, `tag_failed`, `inspect_failed`,
  `exists_failed`, `previous_tag_failed`, `promote_failed`, plus the
  informational `units_drift`, `units_not_installed`, `units_current`,
  `build_inputs_attested` and `build_inputs_kept`) in
  `journalctl -u ci-burst-refresh.service`. Only a confirmed absent
  `localhost/ci-burst:latest` (podman's "absent" status) may skip the rollback
  handle; a failed inspection or a storage error fails the run instead;
- the smoke run is one execution of the command this host's scheduled unit runs,
  from the candidate, against this host's own configuration and its own
  credentials: `plan` on a controller host (it writes nothing and launches
  nothing), `reap` without `--apply` on a cleanup-only host (it selects expired
  instances but requests no termination). It must **complete with a decision from
  the accepted set for that command** — `wait` or `would_launch` for `plan`,
  `planned` for `reap` — because a refusal document means the candidate cannot
  read this host's configuration, which is exactly what the next scheduled run
  would hit. A new decision is rejected until the script's accepted set is
  extended deliberately. The document is validated with the host's `python3`, which the
  lifecycle test below already requires. One transient failure retries the run
  once after 20 seconds (`CI_BURST_SMOKE_RETRY_SECONDS`), because an unreachable
  Forgejo or AWS is not evidence about the image; a second failure keeps the
  previous image.

### Source identity and image identity

Three identities describe a deployment, and none substitutes for another:

- the **release version** — the source history recorded in `CHANGELOG.md`
  and named by the immutable lightweight release tag;
- the **deployed source SHA** — `/var/lib/ci-burst-image/deployed-sha` and the
  `localhost/ci-burst:sha-<12>` tag. It identifies the source commit the build
  context came from;
- the **image identity** — the image ID that `podman image inspect` reports and
  the authoritative build record under `builds/images/`. It identifies the
  bytes that serve.

An unchanged source SHA can still build different bytes: the base image was a
moving `python:3.12-slim` tag before the digest pin, and the dependency range
`boto3>=1.34,<2` resolves at build time. The base is now pinned by index digest,
and the build record captures the dependency set the candidate actually
resolved, so an image can be traced to its inputs. That is recording, not
reproducibility: a rebuild of the same source is expected to produce a new
record, and it may produce a different image ID. If the rebuild resolves
exactly the same image from the same source, that source is already attested,
the record is kept as it is — including its original timestamps — and only the
source-keyed reference moves.

A base-image digest bump or a dependency change is therefore a reviewed input
change in a pull request. It is not a relabel of an unchanged source SHA onto
new bytes; the `sha-<12>` tag always means "built from this commit", never
"built from these inputs". Rollback retags an image that already exists, so it
restores the bytes that tag names without changing the recorded inputs.

The image-keyed record is authoritative, and its attestation list is
append-only. One image ID can come from more than one source commit: a commit
that changes only `docs/`, `tests/` or `deploy/` files is excluded by
`.dockerignore`, and the Dockerfile copies only the application and packaging
inputs and the one warm script, so a build of it returns the bytes already
recorded under a different source. The exception is
`deploy/runner/image/warm-cache.sh`: the image ships it for automated image
builds, so a commit that changes only that file changes the image. When the build inputs match, the run appends an attestation with the
new source SHA, tree SHA, engine version and timestamp and promotes normally;
the identical source and tree is a no-op that leaves the file byte-for-byte
unchanged. When the recorded build inputs differ — the `Dockerfile` sha256, the
`FROM` line, the base-image digest or the Python distribution set — the image
identity and the recorded inputs disagree: the run keeps the first record,
writes `<image-id>.json.conflict` beside it with the candidate, and refuses
promotion. Resolving that contradiction is a human decision rather than a
silent overwrite.

The attestation list is capped at 8 sources. A new source for an already
recorded image is refused with `build_inputs_full` once the list is full,
rather than silently dropping an older attestation that a `sha-<12>` tag or an
earlier reference may still name; the operator prunes the record deliberately
and rebuilds. A repeated source and tree never consumes a slot.

Records are retained, not replaced, and nothing prunes them automatically. Each
record is a few kilobytes of JSON, so the accumulation cost is one file per
image ID; the pruning cost is operator time, because a deleted record cannot be
reconstructed from the image. They are pruned manually, in the same sweep as
unused image tags, keeping the record for every image ID that `latest`,
`previous` or a `sha-<12>` tag still names. The attestation cap bounds one
record; at the cap the operator prunes that record's list, or the whole record
once no tag names its image, and the run never chooses a source to discard. The
source-keyed reference is safe to overwrite: it is a pointer, and after a
failed promotion it can name a candidate that never served, which is why the
serving image is identified by `podman image inspect` rather than by the
reference.

Rollback retags a previous image, and it must not race a refresh: the timer only
schedules, and a run that started before the operator acted would move `latest`
again. Disable and stop the timer first, then the service, then retag under the
script's own lock.

```sh
systemctl disable --now ci-burst-refresh.timer   # no new runs, from here on
systemctl stop ci-burst-refresh.service          # waits for a run in progress
flock /var/lib/ci-burst-image/refresh.lock sh -c \
  'podman tag localhost/ci-burst:previous localhost/ci-burst:latest &&
   printf "%s\n" "rollback: <reason>" > /var/lib/ci-burst-image/hold'
systemctl enable --now ci-burst-refresh.timer   # honors the hold; no promotion
```

`previous` is whatever `latest` pointed at before the last promotion, including
the hand-built image a fresh install starts with. A recent promoted commit is
also available as `localhost/ci-burst:sha-<12>`, within the retained window
described above; an older one has to be rebuilt from its commit:

```sh
flock /var/lib/ci-burst-image/refresh.lock \
  podman tag localhost/ci-burst:sha-<12> localhost/ci-burst:latest
```

A rollback restores bytes that an existing image tag already names; it does not
rebuild, so it cannot change the recorded inputs. The record for the bytes that
serve is the one keyed by the image ID that
`podman image inspect localhost/ci-burst:latest` reports, under
`builds/images/`; the source-keyed `builds/sha-<12>.json` only points at it.
If that image-keyed record is absent (an image built by hand before this record
did), treat the source SHA as unknown provenance for those bytes rather than
assuming it describes them.

`systemctl is-active ci-burst-refresh.service` reports the launcher, not a
build: a promotion that already finished has already moved `latest`, so roll
back after it, not against it.

The script is installed by hand from the checkout, so a change to the script
needs that install step repeated; a change to the controller does not. The
cleanup host runs the same unit with the drop-in above, so it keeps the image
current without a human copying one across. Its smoke is its own: a candidate
that cannot read that host's configuration or reach the cleanup identity is never
promoted there, even when the controller host promoted it.

### Verify the container cleanup

The lifecycle test is opt-in; run it on the scaler host. It needs root,
podman, systemd and a local `localhost/ci-burst:latest` image:

```sh
CI_BURST_RUN_CONTAINER_TESTS=1 \
  python3 -m unittest discover -s tests -p 'test_container_lifecycle.py' -v
```

It stops an activating unit, lets `TimeoutStartSec` expire, and requires the
named container to be gone and the name to be reusable. It is skipped in the
credential-free CI job, which has no such host.

## Loopback is not the Forgejo server

Inside the container, `localhost` and `127.0.0.1` name the container itself.
The units publish no port and use the named `ci-burst` network, so
`https://localhost` in `forgejo.base_url` makes the controller call itself and
fail. It does not reach the Forgejo server on the host, even when both run on
the same machine.

Use the server's routable HTTPS name, one that resolves from the container and
matches the certificate the server presents, for example
`https://forgejo.example.com`. Do not work around this with a published port,
host networking or a copied certificate: the configuration refuses a non-HTTPS
URL, and HTTPS verifies the certificate against that name.

## The state directory is not a file to copy

The SQLite ledger is one database file plus its `.lock` and its rollback
journal, and those must stay together in the mounted state directory. One
deployment, one controller (scaler) host and one ledger copy is the supported
topology.

- Do not copy `state.sqlite3`, or the directory, to run a second controller.
  A copied file is a second ledger with its own cost commitment: each copy can
  admit an instance the other cannot see.
- Do not mount the directory into two containers that may run `apply`. The
  writer lock is a file lock on one host, not a distributed lock, and the
  commitment budget is local.
- A writable run upgrades a version-1, version-2, version-3 or version-4
  ledger to schema 5 once, while holding the writer lock. Each upgrade takes
  one copy of the file, checks the copy's size, then migrates in one
  transaction, so no file takes two copies or two commits: a version-1, -2 or
  -3 file is copied to `state.sqlite3.pre-v4.<epoch>` and lands on schema 5
  directly, and a version-4 file is copied to `state.sqlite3.pre-v5.<epoch>`
  and gains one column. If the copy fails the upgrade refuses before its first
  schema write, and a failed transaction rolls back whole, so a failed upgrade
  leaves the file, byte for byte, readable by the release that wrote it. A
  version-1 or version-2 file first gets the schema-3 columns
  `admitted_incidental_per_hour_micros`, `admitted_buffer_micros` and
  `admitted_inputs_backfilled`, an integer that defaults to `0`. Terminal
  operations keep their stored settled cost, a zero marker and no admitted cost
  inputs; nonterminal operations record the configured incidental rate and
  non-compute buffer as a best-effort backfill, because the inputs their
  admission actually used are not recoverable from an earlier file, and are
  marked `admitted_inputs_backfilled = 1`. The mark keeps such a row's stored
  reservation as its commitment floor and is never inferred from the current
  configuration. An upgrade from version 1, 2 or 3 also creates the
  `fleet_observations` table; a version-3 file has no rows to backfill. Every
  upgrade adds the nullable `operations.admitted_lifetime_seconds` column, which records the
  `aws.max_lifetime_seconds` an intent was admitted under, and backfills
  nothing: every existing row keeps `NULL`, so an unresolved intent from an
  earlier schema is refused with `unresolved_intent_lifetime_mismatch` until an
  operator resolves it. Drain before the upgrade: stop admission as in rollback
  step 1, run `status`, and replace the image only when `state.operations` lists
  no operation in state `intent` or `uncertain`; a refused legacy intent has no
  supported recovery in the product. A host whose `ci-burst-refresh.timer`
  follows `origin/main` replaces the image by itself within minutes of a merge,
  so on such a host either drain before the change that brings schema 5 is
  merged, with admission stopped from the final check until the new image is
  promoted, or hold refresh before the merge as the rollback does:
  `systemctl disable --now ci-burst-refresh.timer`, so a reboot does not arm
  it again, then `systemctl stop ci-burst-refresh.service`, which also cancels
  a start that is queued, and confirm the service is inactive. Verify the
  drain, keep admission stopped from that check until the new image is
  promoted, and only then `systemctl enable --now ci-burst-refresh.timer`.
  Keep cleanup running either way. If one is found after the upgrade, keep
  admission stopped, keep cleanup running, do not delete or edit the row, and
  escalate: an empty owned inventory does not prove that its request was never
  sent. `plan`, `status` and `export` accept versions 1
  through 5 and never create or migrate the file, so they keep reading an
  earlier schema until the first writable run. The previous release refuses a
  schema it does not know, so rolling the image back after an upgrade needs
  the matching copy: `.pre-v5.` returns the schema-4 file, and `.pre-v4.`
  returns the schema-1, -2 or -3 file as it was found. A
  copy holds nothing written after it. The `apply` pass that performs the
  upgrade counts: it goes on to reconcile and can commit and send a launch in
  the same pass. Restore a copy only while admission is stopped and the
  evidence shows that no `apply`, the upgrading pass included, wrote an
  operation or called the provider after the copy, so `state.operations` still
  lists exactly the rows the copy holds. Once an `apply` has admitted or
  settled an operation, the copy would drop that launch and its spend: keep
  admission stopped, keep the current ledger and the upgraded image, and
  escalate instead of rolling back. The version-1 upgrade also changes the
  operator-visible budget shape: the JSON `budget` block reports `spend`,
  `committed` and `remaining` instead of `consumed`, and
  `ci_burst_ledger_consumed_micros` is replaced by `ci_burst_spend_micros` plus
  commitment, allowance and remaining gauges. A consumer that reads the old key
  or series must be updated with the upgrade.
- `reap` never needs this directory, which is why cleanup keeps working when
  the ledger is unavailable.
- To migrate hosts, stop admission as in rollback step 1, including the
  container, and confirm `podman ps -a --filter name=ci-burst-apply` lists
  nothing before moving the whole directory instead of copying it. A process
  that still holds the ledger can keep writing to it while it is moved. Start
  admission only on the new host.

## Rollback

The product rollback order is in
[operations.md](operations.md#scheduler-cleanup-and-rollback). With the units
installed it becomes:

1. Stop admission completely, including the run already in progress.
   Disabling the timer stops future triggers only; a run that is executing can
   still reach the provider, and it can outlive its launcher:

   ```sh
   systemctl disable --now ci-burst-apply.timer
   systemctl stop ci-burst-apply.service
   systemctl is-active ci-burst-apply.service          # launcher state only
   podman rm --force --ignore --time 5 ci-burst-apply  # explicit, idempotent
   podman ps -a --filter name=ci-burst-apply           # must list nothing
   ```

   `inactive` or `failed` describes the launcher, not the container. The CLI
   has no signal handler and would be container PID 1 without an init, so a
   container can outlive a stopped or timed-out unit. The unit passes `--init`
   and also runs the same `podman rm` in `ExecStopPost`, so the explicit command
   is normally a no-op; keep it for a failed cleanup, a manual run, or a systemd
   crash. A container removed here may already have sent a launch request;
   reconcile it in step 4.

2. Keep `ci-burst-reap.timer` enabled and running on the cleanup host.
   Stopping cleanup is not part of rollback; expiry termination is the path
   that removes expired instances while admission is off.
3. Inspect what remains with a manual `status` run using the apply mounts, then
   the ledger if needed: `state`, `slot` and `cloud` in the JSON.
4. Reconcile a launch request that may already have been sent. `apply` commits
   its intent before the provider call, so a run stopped mid-request leaves an
   unresolved operation, not a lost one. If `state.operations` shows an intent
   or an `uncertain` operation, or `cloud.instances` lists an owned instance:
   do not launch a replacement, do not delete the intent and do not start
   `apply` again. `reap --apply` terminates the instance once its expiry tag
   has passed, so keep cleanup running and confirm the terminal state with
   `status`. Removing an owned instance before its expiry needs an operator
   action with the cleanup identity; the product has no ad-hoc terminate
   command. Terminate only resources the evidence proves are owned.
5. Revert the source change through a reviewed pull request. Do not rewrite
   published history.

The image pin is not part of this order. A routine rollback of a warm image moves
the pin back ([operations.md](operations.md#rolling-back-an-image)). When the old
image must not run again, treat it as an incident: a recorded launch intent can
still retry on it, so stop admission as above as well.

### Retiring the cleanup host

Do not remove the last cleanup scheduler on the strength of one manual
`reap --apply` run. `reap` terminates only instances whose expiry tag has
already passed, so that run can succeed without terminating an instance that is
still inside its lifetime. Retiring the host then leaves the instance without
recurring expiry enforcement, for example when its bootstrap failed and it
cannot shut itself down.

Before the cleanup host is retired, do one of these:

- install `ci-burst-reap.service` and `ci-burst-reap.timer` on the replacement
  host, start them, and verify the timer fires and a run succeeds there before
  stopping the old host's timer; or
- resolve every outstanding launch request and confirm with `status` that every
  owned instance is `terminated` (or has aged out of the provider's retained
  visibility) and no operation is unresolved, then remove the last scheduler.

Removing the last cleanup scheduler is safe only when no owned instance can
outlive it. A manual `reap --apply` run does not replace the recurring schedule.

## Not included

- A published or signed image, a registry push, a release tag, or an image
  artifact that outlives the CI job. CI builds the image only to prove it still
  builds.
- A token-file option; the env-file route is inspect-visible by design today.
- A unit or timer that runs the `image` commands, and automatic promotion. The
  image commands are one-off containers that you start.
- A daemon, published ports, a healthcheck, or readiness probing. One run, one
  exit code.
- Signal-driven shutdown as the cleanup guarantee. The units force-remove their
  named container; `--init` only makes a stop quicker.
- Ledger replication, coordinated failover or a distributed lock.
- Forgejo TLS termination or reverse-proxy configuration. The container only
  needs a name it can resolve and a certificate it can verify.
