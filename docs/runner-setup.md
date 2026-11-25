# Set up a Forgejo Actions runner on AWS

This tutorial builds the operator-owned prerequisites ci-burst expects:

- an IAM role and instance profile whose only permission is reading one SSM
  parameter;
- a SecureString parameter holding a repository-scoped Forgejo runner
  registration token;
- a pinned launch template whose bootstrap registers an ephemeral runner, polls
  for a job during a bounded claim window, runs at most one job and terminates
  the instance;
- a worker network placement that can reach both Forgejo and the SSM endpoint.

ci-burst never creates IAM resources, reads SSM or distributes the token. It
launches the template version you approve and observes the queue. Complete this
tutorial before configuring a deployment; the controller refuses an unusable
template.

Placeholders use `example` values. Replace every one with your own.

| Placeholder | Meaning |
| --- | --- |
| `123456789012` | your AWS account id |
| `eu-central-1` / `eu-central-1a` | your Region and availability zone |
| `forgejo.example.com` | your Forgejo hostname |
| `owner/repository` | the repository whose jobs the runner may take |
| `cloud-ci` | the deployment-specific routing label in the bootstrap's `--labels` and `[worker].labels` |
| `vpc-...`, `subnet-...`, `sg-...`, `ami-...` | your network and image ids |

## Threat model

The worker executes untrusted repository code. Treat that code as the
adversary: it runs on the host, under the runner account, and must not be able
to read the reusable registration token or mint new credentials.

Two boundaries make that true here:

1. the registration token is read into a root-only file and passed to
   `forgejo-runner register`, which completes and exits before the runner
   starts or any job is dispatched. On a stock Linux host `/proc/<pid>/cmdline`
   is world-readable, so the protection is that no job code exists during that
   window, not the root UID. Host root and exec auditing can observe it;
2. jobs run as an unprivileged `runner` account whose access to the instance
   metadata service is blocked, so job code cannot use the instance profile to
   read SSM.

Do not run the runner or its jobs as root. If a window visible to host root or
exec auditing is unacceptable, use the pre-registered credential alternative in
section 2.

## 1. Runner IAM role

The worker needs one read: the registration token parameter. It must not be
able to launch, terminate, tag or read anything else.

Policy `forgejo-runner-read-token`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadOneRegistrationToken",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:eu-central-1:123456789012:parameter/ci-burst/runner/registration-token"
    }
  ]
}
```

Add `kms:Decrypt` on your key only if the parameter uses a customer-managed KMS
key. The AWS-managed `aws/ssm` key needs no extra statement.

Role `forgejo-runner`, trusted entity **EC2**, with that policy attached. The
console creates an instance profile of the same name; note it for the launch
template. No other policy, no inline policy, no S3, no EC2 permissions.

If your account uses a separate identity to launch instances (ci-burst, a
scheduler or a human), that identity needs `iam:PassRole` for this role only:

```json
{
  "Sid": "PassOnlyTheRunnerRoleToEc2",
  "Effect": "Allow",
  "Action": "iam:PassRole",
  "Resource": "arn:aws:iam::123456789012:role/forgejo-runner",
  "Condition": { "StringEquals": { "iam:PassedToService": "ec2.amazonaws.com" } }
}
```

## 2. Registration token

A runner authenticates with a repository-scoped credential. Two Forgejo
concepts matter:

- **Runner credential** (repository → Settings → Actions → Runners → Create
  new runner): a UUID and token for one runner. With ephemeral mode it is
  consumed by the first job. Good for a single manual smoke test.
- **Registration token**: reusable within its validity. The worker exchanges it
  for its own ephemeral runner credential, so one stored token serves repeated
  launches.

On the Forgejo host:

```sh
forgejo forgejo-cli actions generate-runner-token --scope owner/repository
```

If Forgejo runs in a container, run that inside it, for example
`podman exec --user <uid>:<gid> <container> forgejo forgejo-cli actions
generate-runner-token --scope owner/repository`. Confirm the subcommand in your
version (`forgejo --help`); older builds expose `forgejo actions
generate-runner-token`. Depending on your version and scope, the token may be
long-lived and not revocable from the UI — record how you will rotate it.

Store it as an SSM SecureString, without a predictable world-readable file:

```sh
umask 077
read -rsp 'registration token: ' REG_TOKEN
token_file=$(mktemp)
printf '%s' "$REG_TOKEN" > "$token_file"
unset REG_TOKEN
aws ssm put-parameter --region eu-central-1 \
  --name /ci-burst/runner/registration-token \
  --type SecureString --value "file://$token_file"
shred -u "$token_file"
```

### Rotation

Generate a new token on the Forgejo side, then update the same parameter name
with `aws ssm put-parameter --overwrite` (or a new console version). Updating
the parameter does not invalidate the old Forgejo token; revoke or regenerate
it on the Forgejo side and verify that an old token no longer registers a
runner before you rely on the rotation.

### Alternative: pre-registered runner credentials

If host-root visibility of the registration token is unacceptable, do not store
a registration token on the worker at all. Pre-create ephemeral runner
credentials (one per launch) and store each UUID and token in SSM as a
SecureString JSON value. A bootstrap for this alternative writes the runner
configuration from that value and starts `one-job`; it never calls `register`.
The trade-off is that the operator must replenish credentials for each launch.

## 3. Network placement

The worker must reach:

- `https://forgejo.example.com` (or your Forgejo host and port);
- the SSM endpoint (`ssm.<region>.amazonaws.com:443`) for the token read;
- package mirrors (HTTP `80`, HTTPS `443`, DNS `53`) and anything your jobs
  download.

Two placements work:

**Private subnet with a NAT instance or NAT gateway.** No public address on the
worker. The NAT device forwards internet traffic and, if Forgejo is not
publicly reachable, also forwards to a VPN/mesh that connects the VPC to the
Forgejo network. A NAT instance is cheaper for a small pilot; a NAT gateway is
managed but costs more before data.

**Public subnet with a public IPv4 and no inbound rules.** The worker reaches
the internet directly through the internet gateway. If Forgejo is private, add
a route for its address through the same VPN/tunnel endpoint. The security
group denies all inbound traffic, so the public address is only an egress
identity.

Either way the runner security group needs **no inbound rules** and outbound
traffic for the ports above. If Forgejo's name resolves to a private address,
pin it in `/etc/hosts` on the worker — only when your VPC DNS does not already
resolve it correctly.

If neither placement is available — a private subnet with no NAT device of any
kind — the deployment can build its own router from
[deploy/router/userdata.sh](../deploy/router/userdata.sh). [router.md](router.md)
owns the procedure and the acceptance test: one registered worker per configured
candidate, not one per deployment.

## 4. Launch template

Create a launch template with a pinned, numeric version. ci-burst refuses a
template that is missing any of these:

- a literal `ImageId`, never `resolve:ssm:` or another alias;
- an `InstanceType` — the default pool, and the shape anchor when
  `aws.instance_types` names a list;
- `InstanceInitiatedShutdownBehavior = terminate`;
- bootstrap `UserData`.

Set these values:

| Setting | Value |
| --- | --- |
| Image | an image matching the instance architecture, with cloud-init and the runner preinstalled (see [The worker image](#the-worker-image)) |
| Instance type | sized like your existing runners; arm64 images need `t4g`/`c7g`/`m7g`/`r7g`. A deployment may also list further types of the same shape and architecture in `aws.instance_types` |
| IAM instance profile | `forgejo-runner` |
| Metadata options | IMDSv2 required (`HttpTokens=required`), hop limit 1 |
| Network | the subnet and security group from section 3 |
| Key pair | none |
| Shutdown behaviour | `Terminate` |
| Storage | delete on termination |

Instance sizing is a deployment decision. A worker that runs the same checks as
a 4-core, 6 GiB self-hosted runner should get at least that; a cheap burstable
type is fine only for short or trivial jobs.

ci-burst always requests Spot, whether or not the template pins it, so the
account needs the Spot service-linked role. An identity with
`iam:CreateServiceLinkedRole` can create it once with:

```sh
aws iam create-service-linked-role --aws-service-name spot.amazonaws.com
```

Without it, launches fail with
`AuthFailure.ServiceLinkedRoleCreationNotPermitted`.

### The worker image

When a deployment configures `aws.instance_types`, the image is shared by every
type in that list, so every type must be the same architecture as the image and
must still boot it. The prerequisite check proves architecture support only; a
canary launch per new type proves boot and registration.

The template boots a prebuilt image, not a stock distribution. The image is an
operator-owned prerequisite; this repository carries the build recipe at
[`deploy/runner/image/provision.sh`](../deploy/runner/image/provision.sh) and
the operator steps at
[`deploy/runner/image/README.md`](../deploy/runner/image/README.md), but it
neither builds nor publishes the base image. The build inputs are portable and
listed there: a fresh Debian 13 arm64 base, root on the builder, egress to the
Debian apt repositories, GitHub, `nodejs.org`, `code.forgejo.org` and the
Python package index (`pypi.org` and `files.pythonhosted.org`), including the
redirect targets of those hosts, and one value the recipe takes as input —
`RUFF_VERSION`, the ruff release to install (for example `0.17.4`). The index
is needed because the recipe installs ruff with `uv tool install` on a fresh
builder with no cached wheel. The other tool versions are literal pins in the
recipe. Nothing in that section needs a private repository or an existing
fleet.

The image must provide what the bootstrap below assumes:

- `forgejo-runner` **13.2.1** at `/usr/local/bin/forgejo-runner` — the pin lives
  in the image, not in a download;
- `/etc/forgejo-runner/config.yml`, readable by `runner`, declaring the Forgejo
  instance, `runner.file: /var/lib/forgejo-runner/.runner`, capacity 1 and the
  cache directories;
- the `aws` CLI, which the bootstrap uses to read the registration token, and
  `iptables` for the metadata block;
- the job toolchain the queue needs, not only the runner binary. Runner classes
  differ by architecture, never by installed packages, so a job may be batched
  onto any runner of its architecture. Without one of those tools a job either
  fails or silently skips the checks that needed it, so the image is verified
  with the pilot checklist rather than assumed.
  [operations.md § Worker image and launch template](operations.md#worker-image-and-launch-template)
  owns the rule; the deployment records the set it qualifies;
- systemd, an unprivileged `runner` account with home
  `/var/lib/forgejo-runner`, and cloud-init running the user data as root;
- `/var/lib/forgejo-runner/.runner` as the registration-file location, because
  the bootstrap changes to that directory before starting `one-job`, which
  resolves the file relative to its working directory.

The tools a CI job may rely on are listed in
[What the image contains](../deploy/runner/image/README.md#what-the-image-contains),
which the image README owns. A CI group may rely only on listed tools. To qualify
a heavy job, run it once on a burst worker and add any missing tool to the
recipe.

A deployment that starts from a stock image can bake the toolchain with the
recipe, or put the install into the bootstrap it publishes. The versions and
history recorded in the image directory describe the maintainers' deployment,
not a prerequisite for yours; an independent operator supplies a base image and
`RUFF_VERSION` and records the result with their own deployment. The bootstrap
file here is the recorded deployment's captured template user data, not a
parameterised example; if your deployment needs different values, fork it and
publish your own version (see [Worker bootstrap](#worker-bootstrap)).

An optional warm-cache layer, added on top of this image so jobs do not download
their packages on every worker, is described in [warm-image.md](warm-image.md). It
covers the opt-in automated build (the `image` commands) and the manual runbook, and
it is the one owner of both. The automated build starts from the cold base image
that its own build launch template names, so the image above stays the base.

### Worker bootstrap

User data (no secret in it). The bootstrap is the repository-owned file
[`deploy/runner/bootstrap.sh`](../deploy/runner/bootstrap.sh), and it is the
bytes published as the recorded deployment's template version — not a template
or an example. The byte hash pinned in
[`tests/test_runner_bootstrap.py`](../tests/test_runner_bootstrap.py) describes
that captured artifact only; it does not describe or validate a version you
publish. It runs as root, reads the registration token from the SSM SecureString
parameter, registers one ephemeral runner named `burst-<hostname>`, blocks the
metadata service for the job account, polls for a task during a bounded claim
window ([The claim window](#the-claim-window)), runs at most one job and always
terminates the instance.

The labels, the Forgejo URL and the parameter name are literal in the file. That
is deliberate: a deployment that needs different values edits the file, publishes
a new template version and bumps `launch_template_version`. Editing and
publishing your own version is the independence path: that fork is your own
artefact, with its own validation and versioning, and this repository does not
maintain a second copy of it. Publishing:

```sh
aws ec2 create-launch-template-version --launch-template-name <template> \
  --source-version <current> \
  --launch-template-data "{\"UserData\":\"$(base64 -w0 deploy/runner/bootstrap.sh)\"}"
```

Then set the new number in the deployment TOML. `check-prerequisites` compares
the pinned version's user data with this file and reports drift when they
differ, so a merged edit that was never published does not pass unnoticed. An
approved version is never edited in place
([operations.md § Worker image and launch template](operations.md#worker-image-and-launch-template)
owns that rule).

The bootstrap reports the one-job runner process with exactly one bounded marker
— `bootstrap one-job completed rc=0`, `bootstrap one-job failed rc=<n>` or
`bootstrap one-job timeout rc=<n>` (124 or 137) — and then exits with that
status, so the `bootstrap exiting rc=<n>` line is truthful. These markers
describe the worker process lifecycle and never the Forgejo job result: a runner
can exit 0 after a job that failed, and the job's verdict stays with Forgejo.
A poll that finds no task while the claim window is open is not a job attempt and
prints no marker. [operations.md § Worker evidence](operations.md#worker-evidence)
owns how the exporter reads them.

The bootstrap also watches the Spot interruption notice, as root, because the job
account is denied the metadata service. It polls IMDSv2 every five seconds; on a
notice it prints one marker, `bootstrap spot-interruption action=<action>
host=<hostname> runner=<uuid|unknown>`, and attempts to end the job account's
runner process with `SIGTERM` so it stops before AWS reclaims the instance. The `runner`
value is captured at registration, before the job account owns that directory,
and read with `jq` as one field only: the file's token is never copied or
printed. A value that cannot be captured is published as `unknown` and becomes
`null` in the worker-event. The watcher starts after registration, so a notice
during registration is not observed. A metadata read that fails is not fatal: the worker keeps the
lifetime and job-timeout bounds it had before, and no marker is written. The marker becomes the `spot-interruption` worker-event
([operations.md § The Spot interruption marker](operations.md#the-spot-interruption-marker));
it records that the notice was observed and that a signal delivery was
attempted - not that the worker or the job ended - and it is never a substitute
for a job result.

The bootstrap runs with `umask 077` while it handles the registration token, then
lowers to `umask 0022` before starting the job, because that is what a
systemd-managed runner produces under its default `UMask=0022`. Without the
second umask the job would create `0600`/`0700` files where that runner creates
`0644`/`0755`. That difference is invisible until a job asserts a mode, and then
it fails only on burst capacity — a false red that looks like a defect in the
change under test.

### The claim window

After `register` returns, the bootstrap does not block in the runner while it
waits for a task. It polls the queue for a bounded claim window, so a worker that
no job needs ends itself instead of idling until its maximum lifetime.

- A poll is one run of `one-job` without its wait option (`-w`, `--wait`, which
  blocks until Forgejo assigns a task). It asks Forgejo once and returns at once
  when nothing is queued.
- The claim window is 300 seconds and the pause between polls is 10 seconds. The
  window starts when `register` returns. Both numbers are starting points, not
  measurements. They are single literals beside the lifetime literal in the
  bootstrap (`CLAIM_WINDOW_SECONDS` and `POLL_INTERVAL_SECONDS`), not
  configuration, tags or controller input.
- The bootstrap checks the claim deadline before the first poll, after each empty
  poll and again after each pause, and it starts no poll at or after the deadline.
  A worker can end up to one pause after the deadline, and boot comes before the
  window, so the window is not a maximum idle time of 300 seconds.
- The deadline never interrupts a running poll, because a running poll may
  already hold a job. A job that a poll receives keeps the job timeout and the
  instance lifetime exactly as before, and the lifetime shutdown stays the outer
  bound on every path.
- When the window passes with no task, the worker prints one `STEP claim-timeout`
  marker, removes the registration file and exits with status 0 through the same
  exit trap as every other ending, so the console shows `bootstrap exiting rc=0`.
  It prints neither the `job` step marker nor a `bootstrap one-job` marker,
  because no job attempt happened. A window that ends on schedule is the designed
  outcome, so the status is 0, and 124 and 137 stay the job-timeout statuses. A
  completed job also exits 0: the exporter tells the two endings apart by the
  `claim-timeout` step and the missing one-job marker.

**The empty poll of the pinned runner.** In 13.2.1, a poll that finds no task
exits with status 2 and prints `Error: no task received`, followed by one line
feed, on standard error. These values are literals in the bootstrap
(`NO_TASK_STATUS` and `NO_TASK_MESSAGE`), and a runner version change needs new
values. The runner's source at the tag `v13.2.1` states them:

- [`internal/app/cmd/cmd.go`](https://code.forgejo.org/forgejo/runner/raw/tag/v13.2.1/internal/app/cmd/cmd.go):
  `Execute` calls `os.Exit(2)` when the error is `poll.ErrNoTaskReceived` and
  `os.Exit(1)` for any other error.
- [`internal/app/poll/single.go`](https://code.forgejo.org/forgejo/runner/raw/tag/v13.2.1/internal/app/poll/single.go):
  `ErrNoTaskReceived` is `errors.New("no task received")`, and a poll without the
  wait option returns it when it received no task.
- cobra v1.10.2
  [`command.go`](https://raw.githubusercontent.com/spf13/cobra/v1.10.2/command.go):
  `ExecuteC` prints the returned error to standard error with
  `PrintErrln(cmd.ErrPrefix(), err.Error())`, and the prefix is `Error:`. The
  runner's root command sets `SilenceUsage`, so no usage text follows.
- [`main.go`](https://code.forgejo.org/forgejo/runner/raw/tag/v13.2.1/main.go):
  the runner registers `SIGINT` and `SIGTERM` with `signal.NotifyContext`. No file
  of the runner's own source registers `SIGQUIT`. The runner's dependencies were
  not read.
- Go [`runtime/signal_unix.go`](https://raw.githubusercontent.com/golang/go/go1.26.8/src/runtime/signal_unix.go)
  at the tag go1.26.8, the toolchain that the runner's `go.mod` names: a signal
  that the program has not registered and that is marked `_SigThrow` ends the
  process with `exit(2)`. [`sigtab_linux_generic.go`](https://raw.githubusercontent.com/golang/go/go1.26.8/src/runtime/sigtab_linux_generic.go)
  marks `SIGQUIT` that way.

Status 2 is inside the band from 2 to 123 that the bootstrap requires, because the
runner of a claimed job can also end with 0, 1, 124 or a status of 128 or more.
That source has two limits. A poll whose request for a task failed (a network
error or a timeout) also ends with status 2 and the same message, after an
error-level log line, so it looks like an empty poll. The exporter reads that line
as a `runner_error`, so a claim timeout that follows such a poll can carry that
error kind next to its `claim-timeout` step. And a Go program exits with status 2
after an unrecovered panic or a fatal signal such as `SIGQUIT`. That is Go runtime
behaviour, not runner code. The runner's own source registers no handler for
`SIGQUIT`, so the runtime default applies. A job step runs as the runner's account,
so it can send the runner `SIGQUIT` and make it end with status 2, and a live
check observed exactly that. An invalid or already used registration fails earlier, with status 1. Status 2
alone is therefore not proof, and the bootstrap asks for more.

The bootstrap keeps polling only when all three of these hold:

1. the runner exited with the documented status, which must be from 2 to 123;
2. the poll's captured output contains the documented message as a complete line,
   that is the message followed by one line feed, with no other byte on that line;
3. no process of the job account still exists when the capture is read.

Every other outcome ends the loop as a finished attempt, and the one-job marker
reports it: a completed or failed job, the status without the message or the
message without the status, a signal, a timeout, a capture that is missing, larger
than its bound or unreadable, a process of the job account that is still running,
and anything the bootstrap does not recognise. A doubt ends the worker and never
extends the window, so a runner version that changes its status or message makes
the worker stop after its first poll instead of looping.

The capture is a second, bounded copy of each poll's output. Root creates it with
mode 0600 under `/run`, outside the runner's directory, so the job account cannot
open it by its path. Each poll truncates it, `head` keeps at most
`CAPTURE_LIMIT_BYTES` plus one byte of it, the bootstrap reads it only after the
poll has ended, and the exit trap removes it. The bound limits what the bootstrap
reads, never what the runner writes: `tee` keeps draining the runner's output
after the bound, so the runner is never stopped, signalled or blocked by it, and a
claimed job's output can exceed the bound. The runner's output also keeps reaching
the console while the poll runs, and the bootstrap does not hide the no-task
line. A capture over the bound is not an empty poll.

Three limits to know:

- The status half and the survival check do not stop a hostile job. Job code runs
  as the account that runs the runner. It can end the runner with status 2 (see
  above), and it can end its own processes first. The message half is then the
  only guard. Three routes for job code to put the message into the capture are
  closed. This is read from the runner's source, and a live check on the worker
  image confirmed each route:
  - Root creates the capture pipe with mode 0600, so the job account cannot open it
    through `/proc/<pid>/fd`.
  - A host-executor step gets its own output pipes, not the runner's: `cmd.Stdout`
    and `cmd.Stderr` in `act/container/host_environment.go` are the step's own line
    writer.
  - The runner does not echo step output to its own output unless its log level is
    `debug` or `trace`: `internal/app/run/runner.go` installs a null job logger
    below that level, the image's runner configuration sets no level, and the
    default is `info`.

  A runner upgrade, an executor other than `host` and a log level of `debug` or
  `trace` each need the live check again.

  One more route needs a kernel setting. Job code that controls the runner
  process itself, with `ptrace` or a write to `/proc/<pid>/mem` on a process of
  the same account, can make the runner write the message and end with status 2.
  The worker image's kernel default (`kernel.yama.ptrace_scope` 0) lets a process
  trace any process of its own account, and a live check showed that a job step
  could attach to the runner and open its memory for writing. The bootstrap
  therefore sets the scope to 1 before the first poll, which limits tracing to a
  process's own descendants. The runner is the step's ancestor, so the step can
  no longer attach to it, and the same live check showed the attach and the memory
  write refused. A stricter value that is already set is kept. A worker that
  cannot confirm the limit prints `WARNING: ptrace not limited, polling once` and
  ends after an empty first poll, so a forged empty poll ends that worker instead
  of starting another poll.

  If an empty poll is forged anyway, Forgejo bounds the damage: it gives a used
  ephemeral registration no second task. While it still counts the claimed task
  as running, a poll with that registration gets the no-task result and reads as
  empty, so the worker polls idle until the deadline. Once the task has ended,
  normally or at the server's zombie-task timeout, the poll is refused with
  status 1.
- A process that keeps the runner's output open after the runner exits keeps the
  poll from ending, because the copy waits for the end of the stream. The worker
  then waits for it. The job timeout does not end that wait, because `timeout`
  watches only its direct child and has already returned with the runner's
  status, so only the instance lifetime does.
- The survival check reads the real, effective, saved and filesystem uid of each
  process from `/proc/<pid>/status`. A uid or a process table that it cannot read
  counts as a surviving process, so the loop ends. A process of the `runner`
  account that exists in the baked image at that moment would end every worker
  after its first poll, and the live check below shows it.

**Qualify it live before you enable it.** The live checks named above ran
against an earlier version of these bytes. They do not qualify the exact template
version a deployment will set. Before a
deployment sets a template version that carries this bootstrap, run bounded
launches from that version and confirm:

- an empty poll exits with status 2 and prints the message on standard error, and
  no other line of that poll matches an error rule of the exporter;
- the ephemeral registration stays valid across repeated empty polls, and the
  first claimed task consumes it;
- after a job that ends normally, a poll with the same registration is refused
  and does not read as an empty poll; after a job step ends the runner with
  `SIGQUIT`, no second task reaches that registration;
- an adversarial job step does not make the poll empty, and the worker does not
  poll again after that job. The step writes the documented message to its own
  standard output and standard error, to the capture path, to the runner's own
  descriptors through `/proc` and to a descriptor that a child process inherits,
  and it tries to attach to the runner with `ptrace` and to open its
  `/proc/<pid>/mem` for writing. Then it sends the runner
  `SIGQUIT`, ends and leaves no process of the job account running. Record each
  result and the runner's exit status (expect 2);
- a background process of the job account that is left running after a job step
  ends is still there when the bootstrap classifies the poll, and the worker does
  not poll again after that job;
- a launch with nothing queued ends with `STEP claim-timeout` and then
  `bootstrap exiting rc=0`, and a launch that claims a job ends with the
  `completed` one-job marker.

### Runner pin and the deprecated `register` command

The image pins `forgejo-runner` **13.2.1**; the bootstrap never downloads it. The
pin is deliberate; do not move it without the migration this section describes.
In that version the `register` command is deprecated but still works, and prints:

```text
`register` has been deprecated; declare connections in the runner configuration instead
```

The deprecation applies to the 13.x runner line, including the pinned 13.2.1.
The bootstrap depends on `register --no-interactive ... --ephemeral`: the
non-interactive flag is what lets a fresh worker boot without a person, and
`--ephemeral` is what makes Forgejo delete the runner after one job. A
replacement must keep both properties. Declaring the connection in the runner
configuration (`server.connections`) or passing the connection options to
`one-job` removes `register`, but Forgejo must mint the runner UUID and token
before the worker boots, so the reusable registration-token model in section 2
changes with it. The upstream
[registration guide](https://forgejo.org/docs/latest/admin/actions/registration/)
owns the configuration shape and the ephemeral semantics.

The pin is correct while the pinned runner still provides non-interactive
ephemeral registration. The version that would force the migration is the first
release that no longer ships the `register` subcommand: no released version
removes it as of 13.2.1, and removal would first be possible in a new major
(14.0.0). A migration is a bootstrap change, so it also needs a new
launch-template version and an end-to-end run that proves one job and a
terminated instance before it replaces the pin.

### Labels choose an executor

The bootstrap is the single owner of the advertised label set. It advertises a
deployment-specific routing label, the heavy pool's label when the burst shares
that pool, and the architecture label of the image, each in the runner's
`name:executor` form:

- the part before `:` is the label the controller matches;
- the part after `:` names the executor. A bare label silently means the
  default Docker executor, and on a host without a Docker socket every
  `one-job` dies immediately with:

  ```text
  Error: daemon Docker Engine socket not found and docker_host config was invalid
  ```

  The bootstrap uses the `:host` executor for that reason; when you rename or
  add a label, keep `:host` or provide a working Docker socket in the image.

The controller routes on its own configuration, not on what a worker advertises,
so `[worker].labels` must list the same set including the architecture label, or
the burst is not eligible for jobs that require it.

Every advertised label is a routing claim rather than evidence of capability,
and it must be qualified against the actual image and machine before a job
relies on it. [operations.md § Labels](operations.md#labels) owns that rule and
the matching semantics; this guide does not repeat them.

## 5. Verify

1. Add a trivial workflow or job with a matching `runs-on` label, then launch
   one instance from the template version with a distinct tag such as
   `Name=runner-smoke-test`. Queue the job first to watch a claimed job. Launch
   once more with nothing queued to watch a worker with no job: it polls for its
   claim window, finds nothing and ends by itself (see
   [The claim window](#the-claim-window)). In that launch there is no job to
   watch in steps 3 and 4: the runner stays idle, and the instance terminates
   when the claim window passes.
2. Read the bootstrap output. It is still available after the instance is gone:

   ```sh
   aws ec2 get-console-output --region eu-central-1 \
     --instance-id i-0123456789abcdef0 --latest --query Output --output text
   ```

   The record can lag several minutes after termination, and
   `tee: /dev/ttyS0: Input/output error` lines are noise from the serial
   console, not evidence that the record is missing. Look for the SSM read,
   `Runner registered successfully.`, then one job running, or `STEP claim-timeout`
   and `bootstrap exiting rc=0` when nothing was queued (no `bootstrap one-job`
   marker appears in that case), and the power-off.
   A healthy reference run moves through boot, the SSM token read,
   registration, one job (or the claim timeout) and power-off; use that order as a
   sanity check when a run stops early.
3. In Forgejo, the repository's Actions → Runners page shows the new runner in
   `idle`, then `active` when the job starts.
4. Confirm the job ran on the worker and that the instance terminated
   afterwards.
5. A manually launched instance is not owned by ci-burst. The controller's
   cleanup selects only instances carrying its own `ci-burst:managed` and
   deployment tags, so terminate a manual test yourself.

## 6. Cost and safety

- A worker costs compute plus a public IPv4 (if assigned) for the minutes it
  runs, plus any NAT device you keep running. Spot is cheaper; the controller
  reserves against your configured maximum price.
- A burstable worker (T2, T3, T3a, T4g, T8i) runs in `standard` or `unlimited`
  CPU-credit mode. Standard mode throttles the worker to its baseline once its
  earned credits are spent; unlimited mode keeps bursting and can bill surplus
  CPU credits on top of the Spot price. T3, T3a, T4g and T8i launch in unlimited
  mode by default and T2 in standard mode, unless the template's
  `CreditSpecification` or the account default says otherwise. `plan` reports the
  mode per candidate (or an explicit unknown) and does not query the account
  default: where the template sets none it reports the family default, not the
  account's effective default. A template value that is present but not
  `standard` or `unlimited` is reported as unknown evidence, never as the
  default; a value the adapter cannot represent (empty, over-long or
  non-printable) is still present and stays unknown, never absent.
  `scripts/check_prerequisites.py` reports the value when it is representable
  and `credit_specification_invalid` drift for either case. Choosing the family
  and the credit mode, and any billable benchmark, remain your decisions; a
  cheap burstable type is fine only for short or trivial jobs, and unlimited
  mode is not a bound on cost.
- Keep the worker on a subnet and security group with no inbound access, no
  credentials beyond the one parameter, and no route to unrelated private
  networks.
- Do not put the token in user data, an image layer, a launch-template
  parameter, a tag, a job environment variable or a log.
- Rotate the token by adding a parameter version and revoking the old Forgejo
  token; the role can read every version of that one parameter, so do not add
  other parameters to it.
