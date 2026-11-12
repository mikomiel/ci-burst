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
  `--config` pointing at the mounted TOML.
- Non-root: the process runs as UID/GID 10001.
- No daemon, no published ports, no healthcheck. A run finishes with the
  controller's exit code: `0` completed decision, `2` refusal with nothing
  mutated, `1` provider or cleanup failure. A cleanup pass that refused an owned
  resource (`cleanup_degraded`) is a cleanup failure and exits `1`. The exit
  code is the only completion signal.
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
`LICENSE` and `ci_burst/` enter it, and `pip install .` installs the CLI. The
tag stays local; nothing is pushed.

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

A cleanup-only host runs the same unit and script with a drop-in, because its
scheduled operation is `reap` rather than `apply`:

```sh
# cleanup host
git clone https://github.com/mikomiel/ci-burst.git /opt/ci-burst-src
install -d -m 0700 /var/lib/ci-burst-image
install -m 0755 deploy/ci-burst-refresh.sh /usr/local/sbin/ci-burst-refresh
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
- it records the deployed commit in `/var/lib/ci-burst-image/deployed-sha`;
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
  `unpack_failed`, `build_failed`, `smoke_failed`, `tag_failed`,
  `inspect_failed`, `exists_failed`, `previous_tag_failed`, `promote_failed`,
  plus the informational `units_drift`, `units_not_installed` and
  `units_current`) in `journalctl -u ci-burst-refresh.service`. Only a confirmed absent
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

Rollback retags a previous image, and it must not race a refresh: the timer only
schedules, and a run that started before the operator acted would move `latest`
again. Disable and stop the timer first, then the service, then retag under the
script's own lock.

```sh
systemctl disable --now ci-burst-refresh.timer   # no new runs, from here on
systemctl stop ci-burst-refresh.service          # waits for a run in progress
flock /var/lib/ci-burst-image/refresh.lock \
  podman tag localhost/ci-burst:previous localhost/ci-burst:latest
```

`previous` is whatever `latest` pointed at before the last promotion, including
the hand-built image a fresh install starts with. Any promoted commit is also
available as `localhost/ci-burst:sha-<12>`:

```sh
flock /var/lib/ci-burst-image/refresh.lock \
  podman tag localhost/ci-burst:sha-<12> localhost/ci-burst:latest
```

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
- A writable run upgrades a version-1 or version-2 ledger to schema 3 once,
  while holding the writer lock: it copies the file to
  `state.sqlite3.pre-v3.<epoch>`, checks the copy's size, then migrates
  version 1 through version 2 to version 3 in one transaction. If the copy
  fails it refuses before the first schema write, so a failed upgrade leaves the
  file readable by the release that wrote it. Schema 3 adds three columns:
  `admitted_incidental_per_hour_micros`, `admitted_buffer_micros` and
  `admitted_inputs_backfilled`, an integer that defaults to `0`. Terminal
  operations keep their stored settled cost, a zero marker and no admitted cost
  inputs; nonterminal operations record the configured incidental rate and
  non-compute buffer as a best-effort backfill, because the inputs their
  admission actually used are not recoverable from an earlier file, and are
  marked `admitted_inputs_backfilled = 1`. The mark keeps such a row's stored
  reservation as its commitment floor and is never inferred from the current
  configuration. `plan`, `status` and `export` accept
  versions 1, 2 and 3 and never create or migrate the file. Rolling the image
  back after an upgrade requires restoring the `.pre-v3.` copy first, because
  the previous release refuses schema 3. The version-1 upgrade also changes the
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
- A daemon, published ports, a healthcheck, or readiness probing. One run, one
  exit code.
- Signal-driven shutdown as the cleanup guarantee. The units force-remove their
  named container; `--init` only makes a stop quicker.
- Ledger replication, coordinated failover or a distributed lock.
- Forgejo TLS termination or reverse-proxy configuration. The container only
  needs a name it can resolve and a certificate it can verify.
