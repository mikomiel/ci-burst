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
SecureString JSON value. The bootstrap writes the runner configuration from
that value and starts `one-job`; it never calls `register`. The trade-off is
that the operator must replenish credentials for each launch.

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

User data (no secret in it). It runs as root, prepares the unprivileged runner
account, registers, blocks the metadata service for the job account, runs one
job and always terminates the instance:

```sh
#!/bin/bash
set -euo pipefail
umask 077
exec > >(tee -a /dev/ttyS0) 2>&1 || true
export DEBIAN_FRONTEND=noninteractive

# A failed bootstrap must not leave a running instance. Every exit path,
# successful or not, removes the token file and terminates the instance.
TOKEN_FILE=""
cleanup_token() {
  if [ -n "$TOKEN_FILE" ] && [ -e "$TOKEN_FILE" ]; then
    shred -u "$TOKEN_FILE" 2>/dev/null || rm -f "$TOKEN_FILE"
  fi
}
on_exit() {
  rc=$?
  echo "bootstrap exiting rc=$rc"
  cleanup_token
  sync
  shutdown -h now
}
trap on_exit EXIT
# Independent watchdog for a hung step. Do not ignore a failure: without it a
# hung apt or download would never reach the exit trap.
systemd-run --on-active=3600 --unit=runner-lifetime /sbin/shutdown -h now

apt-get update -y
apt-get install -y --no-install-recommends curl ca-certificates awscli iptables

# Pin the runner. Replace version and checksum with the combination you
# verified from the release page.
RUNNER_VERSION=13.2.1
RUNNER_SHA=<sha256-for-your-architecture>
ARCH=arm64
# This pinned version still accepts `register`, and prints:
#   `register` has been deprecated; declare connections in the runner configuration instead
# A future major version may remove it, so the pin is part of the contract:
# change the version only together with a bootstrap that declares the
# connection in the runner configuration.
curl -fsSL --max-time 60 -o /tmp/forgejo-runner \
  "https://code.forgejo.org/forgejo/runner/releases/download/v${RUNNER_VERSION}/forgejo-runner-${RUNNER_VERSION}-linux-${ARCH}"
echo "${RUNNER_SHA}  /tmp/forgejo-runner" | sha256sum -c -
install -m 0755 /tmp/forgejo-runner /usr/local/bin/forgejo-runner
rm -f /tmp/forgejo-runner

# Dedicated unprivileged account: jobs run here, never as root.
id runner >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin runner
install -d -o runner -g runner -m 0700 /var/lib/forgejo-runner
install -d -m 0755 /etc/forgejo-runner
cat > /etc/forgejo-runner/config.yml <<'CFG'
runner:
  file: /var/lib/forgejo-runner/.runner
  capacity: 1
cache:
  enabled: false
CFG
chown root:runner /etc/forgejo-runner/config.yml
chmod 0640 /etc/forgejo-runner/config.yml

# Pin Forgejo's name only if your DNS does not already resolve it.
# echo '192.0.2.10 forgejo.example.com' >> /etc/hosts

PARAM=/forgejo/runner/registration-token
TOKEN_FILE=$(mktemp /run/runner-token.XXXXXX)
chmod 600 "$TOKEN_FILE"

aws ssm get-parameter --region eu-central-1 --name "$PARAM" \
  --with-decryption --query Parameter.Value --output text > "$TOKEN_FILE"

# The stock CLI accepts the registration token only as an argument, and only
# non-interactive mode can set --ephemeral. This is the one place it is read.
# The registration finishes before the runner starts, so no job code exists
# during that window; host root and exec auditing can still observe it.
/usr/local/bin/forgejo-runner register --no-interactive \
  -c /etc/forgejo-runner/config.yml \
  --instance https://forgejo.example.com \
  --token "$(cat "$TOKEN_FILE")" \
  --name "burst-$(hostname)" \
  --labels burst:host \
  --ephemeral
cleanup_token

# The runner account needs its own registration file, not the registration token.
chown -R runner:runner /var/lib/forgejo-runner

# Job code must not be able to use the instance profile to mint credentials.
iptables -A OUTPUT -m owner --uid-owner runner -d 169.254.169.254 -j REJECT
iptables -A OUTPUT -m owner --uid-owner runner -d 169.254.170.2 -j REJECT

# One job, bound in time; shutdown happens in the EXIT trap.
install -d -o runner -g runner -m 0700 /var/lib/forgejo-runner/work
cd /var/lib/forgejo-runner
timeout --signal=TERM --kill-after=60 3600 \
  runuser -u runner -- /usr/local/bin/forgejo-runner one-job -c /etc/forgejo-runner/config.yml || true
```

`one-job` polls the queue once. When nothing is queued it exits with
`Error: no task received`; that is the expected empty-queue result, not a broken
runner. `one-job -w` (`--wait`) blocks until Forgejo assigns a task. Trigger the
worker only after you observe queue demand, or pass `--wait`; an idle launch
otherwise pays for a boot that does no work.

### Labels choose an executor

Set the template's label to match your controller configuration
(`[worker].labels`), for example `ci-burst:host`, or update that configuration
to `burst:host`.

A runner label may carry an executor suffix: the part after the colon names the
executor, and a bare label silently means the default Docker executor.
`--labels strong-ci` therefore registers a Docker-backed runner. On a host
without a Docker socket, registration still succeeds and then every `one-job`
dies immediately with:

```text
Error: daemon Docker Engine socket not found and docker_host config was invalid
```

`--labels strong-ci:host` selects the host executor instead. The bootstrap above
passes `burst:host` for that reason; when you rename the label, keep the `:host`
suffix or provide a working Docker socket in the image.

A worker may advertise more than one label; an image built for a specific
architecture can also carry `linux-arm64:host` or `linux-x86_64:host`, for
example. Every advertised label is a routing claim to qualify against the image,
not a fact derived from the instance type: ci-burst routes on the labels you
declare, so verify that the image and machine really match a label before jobs
depend on it.

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
