# Running ci-burst in a container

This document is the operator contract for the `Dockerfile`, `.dockerignore`
and the systemd units in `deploy/`. It does not authorize a deployment, a
cloud account change or a pilot; [operations.md](operations.md) remains the
product contract and [SECURITY.md](../SECURITY.md) remains the security
boundary.

The image and units are source artifacts. No image is published, no CI job
builds one, and no deployment is performed here. A local build downloads the
base image and `boto3`, so it needs network access and stays outside the
credential-free validation CI.

## What the image is

- One command per process: `plan`, `apply`, `status` or `reap --apply`, with
  `--config` pointing at the mounted TOML.
- Non-root: the process runs as UID/GID 10001.
- No daemon, no published ports, no healthcheck. A run finishes with the
  controller's exit code: `0` completed decision, `2` refusal with nothing
  mutated, `1` provider or cleanup failure. The exit code is the only
  completion signal.
- The units run it with a read-only root filesystem and a tmpfs for `/tmp`.
  Every durable write belongs in the mounted state directory; the tmpfs covers
  transient files.

The container is not a scheduler. Forgejo owns job assignment and results;
ci-burst launches at most one Spot instance per deployment.

## Prerequisites

- Rootful podman and systemd on the controller host. The commands and units
  assume the default podman user namespace: container UID/GID 10001 is host
  UID/GID 10001.
- A deployment TOML prepared from [operations.md](operations.md), including
  the `forgejo.qualification` decision. `fixtures-only` still blocks `apply`.
- A scaling identity and a separate cleanup identity, each provided as a
  read-only credentials directory. Never place credentials in the TOML.
- Outbound network access to the Forgejo server and the AWS endpoints. The
  container publishes no port and needs no inbound access.

## Build the image

On the controller host, from a checkout of this repository:

```sh
podman build --tag localhost/ci-burst:latest .
```

The build context is default-deny: only `pyproject.toml`, `README.md` and
`ci_burst/` enter it, and `pip install .` installs the CLI. The tag stays
local; nothing is pushed.

If cleanup runs on a separate host, build the same tag there or copy the image
with `podman save` and `podman load`. No registry is involved.

## Prepare the host paths

All paths are fixed by the units. Create them before the first run.

| Path | Access in container | Purpose |
| --- | --- | --- |
| `/etc/ci-burst/deployment.toml` | read-only file | deployment TOML |
| `/var/lib/ci-burst` | writable directory | SQLite ledger, `.lock`, rollback journal |
| `/etc/ci-burst/aws-scaling` | read-only directory | scaling credentials, launch-capable profile |
| `/etc/ci-burst/aws-cleanup` | read-only directory | cleanup credentials, describe/terminate only |
| `/etc/ci-burst/forgejo.env` | podman env-file, not mounted | Forgejo token |

```sh
install -d -o root -g root -m 0755 /etc/ci-burst
install -o root -g root -m 0644 deployment.toml /etc/ci-burst/deployment.toml

install -d -o 10001 -g 10001 -m 0700 /var/lib/ci-burst

install -d -o root -g 10001 -m 0750 /etc/ci-burst/aws-scaling /etc/ci-burst/aws-cleanup
install -o root -g 10001 -m 0640 scaling-credentials /etc/ci-burst/aws-scaling/credentials
install -o root -g 10001 -m 0640 cleanup-credentials /etc/ci-burst/aws-cleanup/credentials

install -o root -g root -m 0600 forgejo.env /etc/ci-burst/forgejo.env
```

The AWS SDK reads `/etc/ci-burst/aws/credentials` and, if present,
`/etc/ci-burst/aws/config` inside the container. The mount target is
`/etc/ci-burst/aws`; each unit mounts only its own source directory.

The two `install` lines take different source files on purpose. Each destination
is mounted into a different unit, so the scaling unit never sees the cleanup
file and the cleanup unit never sees the scaling file. Both files must define
the profile that `aws.profile` names, or `default` when that key is unset: the
mount selects the file, not the profile, and the shared TOML names the same
profile for both units. The scaling file holds the launch-capable scaling
identity. The cleanup file holds the cleanup identity, which keeps
`DescribeInstances` and `TerminateInstances` without launch permission. Never
copy the scaling file into the cleanup directory: cleanup would inherit launch
permission, and a broken scaling identity would take cleanup with it.

Under rootless podman the mapping differs. Add
`--userns=keep-id:uid=10001,gid=10001` to the run command so container UID/GID
10001 maps to your user, and make the state directory, credential files and
env-file readable by that user instead of 10001.

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

The token is supplied with an env-file, which is visible in
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

The scheduled units run the same invocation. To run `apply` by hand as root:

```sh
podman run --rm \
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
podman run --rm \
  --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
  --volume /etc/ci-burst/deployment.toml:/etc/ci-burst/deployment.toml:ro \
  --volume /etc/ci-burst/aws-cleanup:/etc/ci-burst/aws:ro \
  localhost/ci-burst:latest reap --apply --config /etc/ci-burst/deployment.toml
```

Every run prints one JSON document with stable reason codes. There is no
daemon to restart: each run reads the TOML, the credentials and the env-file
fresh.

On a host with SELinux enforcing, add a relabel option to each bind mount. The
deployment TOML is shared by both units and can be in use by two containers at
the same time, so give it the shared label: `.../deployment.toml:ro,z`. Use the
private label `:Z` only for a source one unit uses alone, such as
`/var/lib/ci-burst:rw,Z` or `/etc/ci-burst/aws-scaling:ro,Z`. `:Z` relabels the
source to one container's private category and breaks any other container using
it at that moment, so do not mix `:z` and `:Z` on one source. Where relabeling
is not permitted, apply a `container_file_t` label to the paths once with
`chcon` instead.

## Scheduled operation with systemd

Install the units and start both timers:

```sh
install -m 0644 \
  deploy/ci-burst-apply.service deploy/ci-burst-apply.timer \
  deploy/ci-burst-reap.service deploy/ci-burst-reap.timer \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ci-burst-apply.timer
systemctl enable --now ci-burst-reap.timer
systemctl list-timers 'ci-burst-*'
```

`ci-burst-apply.timer` starts one `apply` run 60 seconds after the previous run
finished; the effective period is the run duration plus about 60 seconds. The
timer never starts a second copy of the unit, and the ledger's writer lock would
refuse one anyway. Disabling the timer does not stop a run that is already
executing; the rollback procedure quiesces the service before state is
inspected or moved.

`ci-burst-reap.timer` runs `reap --apply` five minutes after the previous run
finished, on its own unit.
That separation is deliberate: cleanup must not share the scaler's failure
domain. [operations.md](operations.md) states the rule; install the reap unit
where a failing, hung or disabled scaler cannot stop it. The reap unit needs no
Forgejo token and no state directory, so it keeps working when admission is
stopped, credentials for the scaler are broken, or the ledger is unavailable.

Each run is recorded by systemd with its exit code. Read the JSON output with:

```sh
journalctl -u ci-burst-apply.service -n 50
journalctl -u ci-burst-reap.service -n 50
systemctl status ci-burst-apply.service
```

A failed run does not stop the timers; the next scheduled run tries again. A
persistent refusal is a signal to read the JSON reason code and `status`, not
to raise the run frequency or remove the lock.

## Loopback is not the Forgejo server

Inside the container, `localhost` and `127.0.0.1` name the container itself.
The units publish no port and use podman's default network, so
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
deployment, one controller host and one ledger copy is the supported topology.

- Do not copy `state.sqlite3`, or the directory, to run a second controller.
  A copied file is a second ledger with its own reservation budget: each copy
  can admit an instance the other cannot see.
- Do not mount the directory into two containers that may run `apply`. The
  writer lock is a file lock on one host, not a distributed lock, and the
  reservation budget is local.
- `reap` never needs this directory, which is why cleanup keeps working when
  the ledger is unavailable.
- To migrate hosts, stop admission as in rollback step 1, including the
  in-flight run, and confirm no container is still running before moving the
  whole directory instead of copying it. A process that still holds the ledger
  can keep writing to it while it is moved. Start admission only on the new
  host.

## Rollback

The product rollback order is in
[operations.md](operations.md#scheduler-cleanup-and-rollback). With the units
installed it becomes:

1. Stop admission completely, including the run already in progress.
   Disabling the timer stops future triggers only; a run that is executing can
   still reach the provider:

   ```sh
   systemctl disable --now ci-burst-apply.timer
   systemctl stop ci-burst-apply.service
   systemctl is-active ci-burst-apply.service              # never active/activating
   podman ps --filter ancestor=localhost/ci-burst:latest   # must list no container
   ```

   The service may report `inactive` or `failed`; both mean no invocation is
   running. A run terminated while its provider request was in flight may
   already have launched an instance, so do not treat the stop as proof that
   nothing was created.

2. Keep `ci-burst-reap.timer` enabled and running. Stopping cleanup is not
   part of rollback; expiry termination is the path that removes expired
   instances while admission is off.
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

Run the manual `reap --apply` invocation above if the cleanup host itself is
being retired; otherwise leave its timer in place.

## Not included

- Published image, registry push, image build in CI, and image signing.
- A token-file option; the env-file route is inspect-visible by design today.
- A daemon, published ports, a healthcheck, or readiness probing. One run, one
  exit code.
- Backups, ledger replication, multi-host coordination or a distributed lock.
- Forgejo TLS termination or reverse-proxy configuration. The container only
  needs a name it can resolve and a certificate it can verify.
