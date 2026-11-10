# Deploy ci-burst

This is the end-to-end walkthrough. It sequences the other operator documents
and adds the two pieces they do not cover: the control-plane AWS identities and
the order in which everything is installed. The details stay with their owners:

| Document | Owns |
| --- | --- |
| [runner-setup.md](runner-setup.md) | worker IAM role, registration credential, network placement, launch template, user data |
| [container.md](container.md) | image build, host paths, container mounts, credential files, systemd units |
| [operations.md](operations.md) | configuration contract, queue contract, reservation scope, identities, rollback, status semantics |
| [pilot-checklist.md](pilot-checklist.md) | the preparation record you fill in, which is not authorization |
| [../SECURITY.md](../SECURITY.md) | the threat model and what ci-burst deliberately does not do |

You are building one control plane and one worker:

```text
Forgejo queue  ──read──▶  scaler host (apply timer)  ──launch──▶  AWS Spot worker ──register──▶ Forgejo
     ▲                                                   │
     └──────────────── one independent host (reap timer) ┘   expiry cleanup, no launch permission
```

Read the whole page before starting. Every step is reversible except the AWS
identities' access keys, which you can deactivate at any time.

## 1. Choose four values

| Value | Meaning | Example |
| --- | --- | --- |
| `OWNER/REPOSITORY` | the one Forgejo repository whose queue is observed | `example-owner/example-repo` |
| `LABEL` | the runner label your worker advertises and the job asks for | `cloud-ci` |
| Region, zone, subnet, security group, instance type | the worker placement | `eu-central-1`, `eu-central-1a`, `subnet-...`, `sg-...`, `c7g.xlarge` |
| `MAX_SPOT_PRICE`, `ALLOWANCE` | the two money limits | `0.1200`, `10.00` |

`LABEL` is the routing contract. A job runs on the worker only when its
`runs-on` matches a label the worker advertises. Use a label the persistent
fleet does not advertise, or accept the overlap explicitly: two runners with
the same label compete for the same jobs, and the burst can take work the warm
fleet would have finished faster.

## 2. Prepare the worker

Follow [runner-setup.md](runner-setup.md) sections 1–4. You create:

- an IAM role and instance profile whose only permission is reading one SSM
  parameter;
- a repository-scoped registration token in an SSM SecureString;
- a network placement that reaches Forgejo, the SSM endpoint and your package
  mirrors;
- a pinned launch template with a literal AMI, an instance type,
  `InstanceInitiatedShutdownBehavior = terminate` and bootstrap user data.

Two additions this walkthrough needs:

- the bootstrap registers the label you chose, for example
  `--labels cloud-ci:host`. The part before `:` is the label; `host` is the
  executor. A job with `runs-on: cloud-ci` matches it.
- record the template id, the numeric version, the subnet and the security
  group. Step 5 uses them.

A template that pins its network (`NetworkInterfaces` or a subnet) keeps that
placement and cannot be used with the ordered zone-candidate list in Step 5:
the controller then passes a top-level `SubnetId` and EC2 refuses both. Pin the
network in the template only for the single-zone form. `operations.md` §
"Availability-zone candidates" owns the rule.

## 3. Create the two control-plane identities

The controller never launches as your human identity. Create two IAM users
with programmatic access and no console access:

| User | Policy | Purpose |
| --- | --- | --- |
| `ci-burst-scaler` | [deploy/iam/scaling-policy.json](../deploy/iam/scaling-policy.json) | reads the queue, launches the one template, terminates only owned instances |
| `ci-burst-reaper` | [deploy/iam/cleanup-policy.json](../deploy/iam/cleanup-policy.json) | describes and terminates only owned instances; no launch |

Replace the placeholders in both files before attaching them: account id,
Region, launch-template id, subnet id, security-group id and the worker role
name. The policies are deliberately small:

- `RunInstances` names exactly one template, one subnet and one security group;
- `TerminateInstances` carries an `ec2:ResourceTag/ci-burst:managed = true`
  condition, so an instance the controller did not tag cannot be terminated;
- the reaper has no `RunInstances` at all, and the scaler cannot create users,
  read other parameters or change IAM.

The deployment identifier is a placeholder in every policy as well: both
termination grants require `ec2:ResourceTag/ci-burst:deployment` to equal the
TOML's `deployment` value. Change them together — changing only the TOML keeps
launch working but denies every later termination.

Create one access key per user. The secret is shown once; write it straight
into the credential file on the host that uses it (Step 4) and never into a
repository, a terminal history, a chat or a ticket. Both keys are long-lived:
rotate them on your own schedule and record the rotation with the deployment.

## 4. Prepare the two hosts

Each host needs rootful podman with `catatonit`, systemd, and the image built
from this repository. The scaler also needs the state directory and the Forgejo
token; the reaper needs neither. Use two hosts: a hung apply timer must not stop
expiry cleanup.

On both hosts:

```sh
git clone https://github.com/<owner>/ci-burst.git /opt/ci-burst-src
cd /opt/ci-burst-src
podman build --tag localhost/ci-burst:latest .
```

Then follow [container.md](container.md) § "Prepare the host paths". On the
scaler:

```sh
install -d -o root -g root -m 0755 /etc/ci-burst
install -o root -g root -m 0644 deployment.toml /etc/ci-burst/deployment.toml
install -d -o 10001 -g 10001 -m 0700 /var/lib/ci-burst
install -d -o root -g 10001 -m 0750 /etc/ci-burst/aws-scaling
install -o root -g 10001 -m 0640 scaling-credentials /etc/ci-burst/aws-scaling/credentials
install -o root -g root -m 0600 forgejo.env /etc/ci-burst/forgejo.env
```

On the reaper host use the same commands without the state directory, the token
file and the scaling directory, and install the reaper key as
`/etc/ci-burst/aws-cleanup/credentials`.

The credential files are standard AWS shared-credentials files:

```ini
[default]
aws_access_key_id = <from the access key>
aws_secret_access_key = <shown once>
```

The container reads them from the mounted directory. Do not copy the scaler
file to the reaper host: cleanup would inherit launch permission, and a broken
scaler identity would take cleanup with it.

### The Forgejo read token

The scaler reads the repository's runner-job list. Create an access token for
an account that can read the repository, scoped to `read:repository` (Forgejo
calls it `repo` in the UI), and put it in `/etc/ci-burst/forgejo.env`:

```sh
CI_BURST_FORGEJO_TOKEN=<token>
```

The env-file is visible in `podman inspect`; treat inspect access on the scaler
as token access, keep the file mode `0600`, and rotate the token if that access
changes hands. See [container.md](container.md) § "Forgejo token".

## 5. Write the deployment TOML

`/etc/ci-burst/deployment.toml` on both hosts, identical content:

```toml
deployment = "example-ci-burst"

[forgejo]
base_url = "https://forgejo.example.com"
repository = "example-owner/example-repo"
token_env = "CI_BURST_FORGEJO_TOKEN"
contract = "forgejo-17.0.1-runners-jobs"
qualification = "fixtures-only"

[worker]
labels = ["cloud-ci:host"]

[aws]
region = "eu-central-1"
launch_template_name = "example-ci-burst-runner"
launch_template_version = 3
max_spot_price = "0.1200"
max_lifetime_seconds = 3600
profile = "default"

[[aws.availability_zones]]
zone = "eu-central-1a"
subnet_id = "subnet-0123456789abcdef0"

[budget]
window_seconds = 86400
allowance = "10.00"
non_compute_allowance = "0.50"

[state]
database = "/var/lib/ci-burst/state.sqlite3"
```

`worker.labels` lists what the worker advertises, with the executor suffix; the
adapter sends the bare names (`cloud-ci`) as the queue filter. `[state]
database` must be absolute and inside `/var/lib/ci-burst`. Keep
`qualification = "fixtures-only"` until Step 7 is complete: it blocks `apply`.

Start with the one candidate the scaling policy authorizes. Add a
`[[aws.availability_zones]]` table per further candidate, in priority order,
and add that candidate's subnet to the policy's `RunInstances` resources in the
same change: the policy — not the TOML — is what decides which placements are
allowed, so a candidate the policy omits is a denied launch rather than a
fallback. A zone without a subnet cannot be used at all.

## 6. Install the units

From the checkout, on the scaler:

```sh
install -o root -g root -m 0644 deploy/ci-burst-apply.service /etc/systemd/system/
install -o root -g root -m 0644 deploy/ci-burst-apply.timer   /etc/systemd/system/
systemctl daemon-reload
```

Install the apply unit and timer, but do not enable or start the timer yet. An
enabled timer also starts at boot (`OnBootSec=30s`), so enabling it before the
supervised burst would let a reboot begin unsupervised admission as soon as
Step 7 sets `live-verified`. Step 8 enables it only after that burst.

On the reaper host:

```sh
install -o root -g root -m 0644 deploy/ci-burst-reap.service /etc/systemd/system/
install -o root -g root -m 0644 deploy/ci-burst-reap.timer   /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ci-burst-reap.timer
```

Check the timers with `systemctl list-timers 'ci-burst-*'`. Do not start
`apply` yet: the next two steps qualify it first.

Run one command by hand before trusting the timer. The shape is in
[container.md](container.md) § "Run one command manually":

```sh
podman run --rm --init --name ci-burst-manual --read-only \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
  --volume /etc/ci-burst/deployment.toml:/etc/ci-burst/deployment.toml:ro \
  --volume /var/lib/ci-burst:/var/lib/ci-burst:rw \
  --volume /etc/ci-burst/aws-scaling:/etc/ci-burst/aws:ro \
  --env-file /etc/ci-burst/forgejo.env \
  localhost/ci-burst:latest plan --config /etc/ci-burst/deployment.toml
```

`plan` performs no cloud mutation and no durable write. It must print one JSON
document with a decision, the template, the zone candidates and the price
evidence. A refusal names its reason code; the troubleshooting table in Step 9
maps the common ones.

## 7. Qualify live support

`apply` is refused until the queue contract is verified against the running
Forgejo. With the same read token ([operations.md](operations.md) § "Qualifying
live support"):

1. confirm the endpoint returns the documented shape for your repository,
   including the mandatory `labels` filter and `null` for no match;
2. observe one known waiting job, one dependency-blocked job and one running
   job, and check that `plan` classifies them as demand, blocked and assigned;
3. record that evidence beside the deployment — timestamps, job names and the
   `plan` output — not in this repository;
4. only then set `qualification = "live-verified"` and reinstall the TOML on
   both hosts.

A different Forgejo version or a changed response shape needs a new contract
identifier, not a re-declaration.

## 8. First burst

Keep the `apply` timer stopped and run one decision by hand with the same
command as Step 6, replacing `plan` with `apply`. Watch three things:

- the JSON decision reports `launched` and one operation;
- the instance appears with all four ownership tags
  (`ci-burst:managed`, `ci-burst:deployment`, `ci-burst:operation`,
  `ci-burst:expires-at`) and terminates after its one job;
- Forgejo, not ci-burst, reports the job result.

Only after that burst is understood, turn admission on:

```sh
systemctl enable --now ci-burst-apply.timer
journalctl -u ci-burst-apply.service -f     # one JSON line per run
```

Keep the `reap` timer running on its own host for the whole pilot. Its output
is the only expiry evidence.

## 9. Rollback and troubleshooting

Stop admission without touching cleanup. Disabling the timer stops future
triggers; it does not stop a run that is already executing, so stop the unit and
remove its container too:

```sh
systemctl disable --now ci-burst-apply.timer
systemctl stop ci-burst-apply.service
podman rm --force --ignore ci-burst-apply
podman rm --force --ignore ci-burst-manual
podman ps --all --filter name=ci-burst          # must print no container
```

The manual invocation from Step 8 runs under the fixed name `ci-burst-manual`
and would otherwise keep running after the rollback.

The unit's `ExecStopPost` removes the named container, and the second command
is the verification, not a duplicate. A launch request already sent to AWS is
not recalled by stopping the unit: run `status` to read the operation, and let
the reaper (or the expiry) reconcile it before you re-enable admission.

The ledger keeps the operation; the reaper still terminates expired instances.
To stop cleanup as well, disable the reaper timer and terminate owned instances
by hand from the tags. Reverting the source is a reviewed change, not a history
rewrite.

| Refusal | Meaning |
| --- | --- |
| `CONFIG_MISSING`, `CONFIG_INVALID`, `CONFIG_UNREADABLE` | the TOML path or content is wrong |
| `TEMPLATE_NOT_FOUND` | the template name or numeric version does not exist in the Region |
| `TEMPLATE_IMAGE_NOT_LITERAL`, `TEMPLATE_INSTANCE_TYPE_MISSING`, `TEMPLATE_SHUTDOWN_NOT_TERMINATE` | the template version does not satisfy the worker contract |
| `ZONE_CANDIDATES_INVALID` | a zone candidate is malformed, repeated or empty |
| `FORGEJO_TOKEN_MISSING` | the env-file is not mounted or lacks the variable |
| `FORGEJO_CONTRACT_UNQUALIFIED` | `qualification` is still `fixtures-only`; `apply` is refused |
| `FORGEJO_UNEXPECTED_SHAPE` | the endpoint returned something other than an array or `null` |
| `AWS_REFUSED` | the identity lacks a permission, or a condition (tag, template, subnet) does not match |
| `AWS_UNCERTAIN` | the provider outcome is unknown; the recorded client token resumes the same operation |
| `UNRECORDED_OWNED_INSTANCE`, `OWNERSHIP_TAGS_MISSING`, `OPERATION_TAG_INVALID` | an owned instance needs reconciliation before another launch |
| `spot_price_above_cap`, `spot_price_stale` | the price evidence is above `max_spot_price` or older than `price_max_age_seconds` |

`status` prints the last decision and the operation's state without mutating
anything. A refused cleanup stays visible and does not free the slot; a
termination request is not evidence that the instance is gone.
