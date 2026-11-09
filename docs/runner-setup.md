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

A runner authenticates with a repository-scoped credential. Two routes exist,
and they behave differently:

- **Interactive runner credential** (repository → Settings → Actions → Runners
  → Create new runner): Forgejo shows a UUID and token for *one* runner. With
  ephemeral mode, that credential is consumed by the first job. It is suitable
  for a manual smoke test, not for repeated unattended launches.
- **Repository registration token** (server-side CLI or API): a reuseable token
  that every worker exchanges for its own ephemeral credential. This is the
  route an unattended deployment needs.

On the Forgejo host, create a repository-scoped registration token:

```sh
forgejo forgejo-cli actions generate-runner-token --scope owner/repository
```

If the Forgejo binary runs in a container, run that command inside it, for
example `podman exec --user <uid>:<gid> <container> forgejo forgejo-cli actions
generate-runner-token --scope owner/repository`. Confirm the subcommand exists
in your version (`forgejo --help`) before relying on it; older builds expose
`forgejo actions generate-runner-token`.

Store the value as an SSM SecureString:

```sh
read -rsp 'registration token: ' REG_TOKEN
printf '%s' "$REG_TOKEN" > /tmp/reg-token
aws ssm put-parameter --region eu-central-1 \
  --name /forgejo/runner/registration-token \
  --type SecureString --value file:///tmp/reg-token
shred -u /tmp/reg-token
```

Rotation: generate a new token and update the same parameter name, which
creates a new version. Verify the old token stops registering runners before
you depend on the rotation.

## 3. Network placement

The worker must reach:

- `https://forgejo.example.com` (or your Forgejo host and port);
- the SSM endpoint (`ssm.<region>.amazonaws.com:443`) for the token read;
- package mirrors and anything your jobs download.

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

Whichever you choose, the runner security group needs **no inbound rules** and
outbound `443` at minimum. It must allow all destinations your jobs need, not
just Forgejo.

If Forgejo's name resolves to a private address, pin it in `/etc/hosts` on the
worker; the worker usually has no LAN DNS.

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
| Network | the subnet and security group from step 3 |
| Key pair | none |
| Shutdown behaviour | `Terminate` |
| Storage | delete on termination |

Instance sizing is a deployment decision. A worker that runs the same checks as
a 4-core, 6 GiB self-hosted runner should get at least that; a cheap burstable
type is fine only for short or trivial jobs.

User data (no secret in it):

```sh
#!/bin/bash
set -euo pipefail
exec > >(tee -a /dev/ttyS0) 2>&1 || true
umask 077
export DEBIAN_FRONTEND=noninteractive

apt-get update -y
apt-get install -y --no-install-recommends curl ca-certificates awscli

# Pin the runner. Replace version and checksum with the combination you
# verified from the release page; never install an unverified download.
RUNNER_VERSION=13.2.1
RUNNER_SHA=<sha256-of-this-release-for-your-architecture>
ARCH=arm64
curl -fsSL -o /tmp/forgejo-runner \
  "https://code.forgejo.org/forgejo/runner/releases/download/v${RUNNER_VERSION}/forgejo-runner-${RUNNER_VERSION}-linux-${ARCH}"
echo "${RUNNER_SHA}  /tmp/forgejo-runner" | sha256sum -c -
install -m 0755 /tmp/forgejo-runner /usr/local/bin/forgejo-runner
rm -f /tmp/forgejo-runner

# Pin the Forgejo name if it resolves to a private address.
grep -q 'forgejo.example.com' /etc/hosts || \
  echo '192.0.2.10 forgejo.example.com' >> /etc/hosts

PARAM=/forgejo/runner/registration-token
TOKEN_FILE=$(mktemp /run/runner-token.XXXXXX)
cleanup() { rm -f "$TOKEN_FILE"; }
trap cleanup EXIT INT TERM

aws ssm get-parameter --region eu-central-1 --name "$PARAM" \
  --with-decryption --query Parameter.Value --output text > "$TOKEN_FILE"

/usr/local/bin/forgejo-runner register --no-interactive \
  --instance https://forgejo.example.com \
  --token "$(cat "$TOKEN_FILE")" \
  --name "burst-$(hostname)" \
  --labels burst:host \
  --ephemeral
cleanup
trap - EXIT INT TERM

# Ephemeral runners may run exactly one job. The timeout also bounds the
# "no job ever arrived" case.
timeout --signal=TERM 3600 /usr/local/bin/forgejo-runner one-job || true

shutdown -h now
```

The registration token is passed in the runner's argv for the duration of
`register`; the CLI has no stdin form. The process runs as root and the token
file is removed immediately. No job code runs before registration finishes.

## 5. Verify

1. Launch one instance from the template version. Keep the tag distinct, for
   example `Name=runner-smoke-test`.
2. Read the console output (the bootstrap tees to the serial console). Look for
   a successful SSM read, the runner registration, and `one-job` waiting.
3. In Forgejo, the repository's Actions → Runners page shows the new runner in
   `idle` (or `active` if a matching job is waiting).
4. Add a trivial workflow or job with `runs-on: burst` and confirm it runs on
   the worker and the instance terminates after the job.
5. If no job arrives, the runner waits; terminate the instance manually or let
   your expiry cleanup handle it.

## 6. Cost and safety

- A worker costs compute plus a public IPv4 (if one is assigned) for the
  minutes it runs, plus any NAT device you keep running. Spot is cheaper;
  the controller reserves against your configured maximum price.
- The worker executes untrusted code after it accepts a job. Keep it on a
  subnet and security group with no inbound access, no credentials beyond the
  one parameter, and no route to unrelated private networks.
- Do not put the token in user data, an image layer, a launch-template
  parameter, a tag, a job environment variable or a log.
- Rotate the token by adding a parameter version; the role can read every
  version of that one parameter, so do not add other parameters to it.
