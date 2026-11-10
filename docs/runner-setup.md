# Set up a Forgejo Actions runner on AWS

This tutorial builds the operator-owned prerequisites ci-burst expects:

- an IAM role and instance profile whose only permission is reading one SSM
  parameter;
- a SecureString parameter holding a repository-scoped Forgejo runner
  registration token;
- a pinned launch template whose bootstrap registers an ephemeral runner, runs
  exactly one job and terminates the instance;
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
| `cloud-ci` | the deployment-specific routing label in `RUNNER_LABELS` and `[worker].labels` |
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
      "Resource": "arn:aws:ssm:eu-central-1:123456789012:parameter/forgejo/runner/registration-token"
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
  --name /forgejo/runner/registration-token \
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

## 4. Launch template

Create a launch template with a pinned, numeric version. ci-burst refuses a
template that is missing any of these:

- a literal `ImageId`, never `resolve:ssm:` or another alias;
- an `InstanceType` (the controller prices exactly that type);
- `InstanceInitiatedShutdownBehavior = terminate`;
- bootstrap `UserData`.

Set these values:

| Setting | Value |
| --- | --- |
| Image | an image matching the instance architecture, with cloud-init |
| Instance type | sized like your existing runners; arm64 images need `t4g`/`c7g`/`m7g`/`r7g` |
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

### Worker bootstrap

User data (no secret in it). The bootstrap is the repository-owned file
[`deploy/runner/bootstrap.sh`](../deploy/runner/bootstrap.sh). It runs as root,
prepares the unprivileged runner account, registers an ephemeral runner, blocks
the metadata service for the job account, runs one job and always terminates
the instance.

Paste the file into the template's user data after replacing the placeholders
its header documents. This guide deliberately does not carry a second copy: the
advertised labels, the runner pin and the lifecycle live in that one file. The
bootstrap is part of the template, so any change to it is a new launch-template
version; an approved version is never edited in place
([operations.md § Worker image and launch template](operations.md#worker-image-and-launch-template)
owns that rule).

`one-job` polls the queue once. When nothing is queued it exits with
`Error: no task received`; that is the expected empty-queue result, not a broken
runner. `one-job -w` (`--wait`) blocks until Forgejo assigns a task. Trigger the
worker only after you observe queue demand, or pass `--wait`; an idle launch
otherwise pays for a boot that does no work.

### Runner pin and the deprecated `register` command

The bootstrap pins `forgejo-runner` **13.2.1** (`RUNNER_VERSION`) and checks the
downloaded binary against `RUNNER_SHA`. The pin is deliberate; do not move it
without the migration this section describes. In that version the `register`
command is deprecated but still works, and prints:

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
   `Name=runner-smoke-test`. Queue the job first: a plain `one-job` exits with
   `Error: no task received` when the queue is empty (see section 4).
2. Read the bootstrap output. It is still available after the instance is gone:

   ```sh
   aws ec2 get-console-output --region eu-central-1 \
     --instance-id i-0123456789abcdef0 --latest --query Output --output text
   ```

   The record can lag several minutes after termination, and
   `tee: /dev/ttyS0: Input/output error` lines are noise from the serial
   console, not evidence that the record is missing. Look for the SSM read,
   `Runner registered successfully.`, one job running, and the power-off.
   A healthy reference run moves through boot, toolchain install (about 25 s),
   registration, one job and power-off; use that order as a sanity check when a
   run stops early.
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
- Keep the worker on a subnet and security group with no inbound access, no
  credentials beyond the one parameter, and no route to unrelated private
  networks.
- Do not put the token in user data, an image layer, a launch-template
  parameter, a tag, a job environment variable or a log.
- Rotate the token by adding a parameter version and revoking the old Forgejo
  token; the role can read every version of that one parameter, so do not add
  other parameters to it.
