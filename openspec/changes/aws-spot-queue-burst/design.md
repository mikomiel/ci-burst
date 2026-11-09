# Design

## Context

See [proposal.md](proposal.md) for motivation and product scope. This is a
new public project, not a fork or a deployed service. There is no controller,
qualified runner image or live AWS pilot at this stage.

The source change defines three behavior contracts:
[queue observation](specs/queue-observation/spec.md),
[instance lifecycle](specs/spot-runner-lifecycle/spec.md) and
[bounded operation](specs/bounded-operation/spec.md).

## Goals / Non-Goals

**Goals:** one repository queue, one extra Spot instance, explicit application,
durable recovery, conservative cost admission and cleanup outside the scaler's
failure boundary. A contributor can test the product without a cloud account
or a private platform.

**Non-goals:** distributed controllers, direct job assignment, managed runner
images, a secret-distribution service, new IAM/network infrastructure or a
claim that this controller can impose an absolute AWS billing cap.

## Decisions

### 1. A small Python CLI, not another service platform

Use Python 3.13+, `argparse`, `tomllib`, standard-library HTTPS and SQLite,
and the official AWS SDK (`boto3`). Configuration is TOML; monetary values
are quoted decimal strings converted to exact integer accounting units.
The AWS SDK provides current authentication, request models and retry support.
SQLite provides durable local transactions without an external database.

The CLI has planning/reconciliation, status and expiry-cleanup operations.
Mutating operations require explicit `--apply`; planning is the default.
A scheduler can invoke a bounded reconciliation repeatedly. There is no HTTP
listener, dashboard, plugin system or second queue.

Alternatives: Go is viable but is not needed for this low-throughput control
loop. A daemon, web service and distributed datastore add deployment and
recovery state without serving the one-instance requirement.

### 2. Qualify a narrow Forgejo interface instead of guessing readiness

Use `GET /api/v1/repos/{owner}/{repo}/actions/runners/jobs` for the configured
repository. The current public OpenAPI document describes a bare array of
`ActionRunJob`; relevant fields include `handle`, `attempt`, `runs_on`,
`status`, `needs`, `repo_id` and `task_id`. `handle` identifies an individual
attempt. There is no need to copy an upstream implementation or support
undocumented alternate envelopes.

The API adapter validates the accepted shape and complete label requirements.
The server-side label filter is an optimization, not proof of a full match.
Unknown required fields, an unexpected response or an incomplete read make
demand unavailable rather than zero.

The schema alone does **not** prove which status values are runnable or what
token permission the endpoint requires. Contract qualification must include
an independently ready job, a dependency-blocked job, a dependency-satisfied
job, an assigned job and a retry. Until that contract is established for a
supported server, live provisioning is disabled. There is no fallback to
workflow age, aggregate queue counts or assumptions about `needs` being empty.
If qualification fails, the implementation reports unsupported behavior;
it does not invent another scheduler.

Use the smallest token scope accepted by the qualified endpoint. A GET request
does not establish that its credential is narrowly privileged. Follow HTTPS
verification and reject cross-origin authenticated redirects. Do not execute
or interpolate job-supplied strings into commands.

### 3. Keep runner bootstrap with the existing image/template owner

The controller launches an operator-prepared template, not arbitrary user data.
The template uses a numeric version and a literal immutable AMI ID. Its worker
contract is:

- repository-scoped ephemeral registration and `forgejo-runner one-job`;
- declared and qualified labels, architecture, CPU, RAM and toolchain;
- secure operator-owned acquisition of registration credentials;
- no controller/provisioning credentials on the worker;
- process-exit shutdown with EC2 terminate-on-shutdown behavior;
- disposable volumes with delete-on-termination behavior;
- bounded boot/job lifetime, including the no-job and failed-bootstrap cases.

Forgejo ephemeral mode limits a runner to one job; it does not expire an idle
runner promptly. Image qualification must test that difference. The controller
does not mark an instance as runner-ready merely because EC2 says `running`.

Alternative: controller-managed registration would require a separate trusted
secret-delivery channel. Inline secrets in user data, SSH command arguments,
images or logs are not an acceptable shortcut. Building such a channel is
outside the initial scaler; the prepared-template contract avoids it.
This prerequisite is explicit, not a claim that the repository already ships
a turnkey image or a bootstrap service.

### 4. Persist intent before AWS, and freeze the request

Use EC2 `RunInstances` with one-time Spot market options, `MinCount=MaxCount=1`,
a fixed template version, fixed placement inputs and a stable `ClientToken`.
There is no On-Demand fallback or automatic Region/AZ substitution.

Before the request, one SQLite transaction stores the deployment and operation
identities. It records the frozen request parameters, their hash and the stable
launch-request identifier. It also records creation and expiry times with the
cost reservation. Required ownership and expiry tags are part of the launch
request, not a later best-effort tag step.

The durable lifecycle is conceptually `intent -> starting -> running ->
terminating -> terminal`; uncertain creation/cleanup remains outstanding.
A lost response can occur after AWS created the instance. Recovery discovers
by client token and ownership, and retries only the same operation. It does
not assume that an immediately empty eventual-consistency read proves that
a launch never occurred. A changed request requires a different, separately
admitted operation after the old one is resolved.

A supported deployment has one host, one local ledger and an OS-backed writer
lock held through scaling operations. A second writer fails before mutation.
A hung owner is stopped by an external supervisor rather than superseded by
a timing lease that could allow a stale process to act. Tags are ownership
metadata, not a distributed lock. Multiple hosts/ledger copies are unsupported.

Alternative: stateless polling loses uncertain operations and cost state.
A distributed lock/store is unnecessary for the supported topology.

### 5. Reserve conservatively; do not pretend to control the AWS bill

Live admission needs a fresh compatible Spot quote below a configured hourly
cap, maximum lifetime, a configured reservation window and sufficient allowance.
Quote identity includes Region, placement/product and instance type; missing
or mismatched data refuses a launch. Set the Spot request's maximum price as
well, rather than treating the observed price as a guarantee.

Reserve the conservative maximum compute exposure for the lifetime, plus an
explicit operator-configured allowance for known non-compute charges. Use
exact arithmetic and persist the reservation before requesting AWS. Do not
refund it merely because termination was requested; the initial design can
keep the full reservation consumed for its admission window rather than
introduce uncertain actual-bill reconciliation. Outstanding resources remain
counted when a window changes, including unresolved expiry or cleanup.

EBS, transfer, public addressing, logs, bootstrap services, minimum billing
increments and failed cleanup can make the actual bill differ. The plan must
name its assumptions and residual exposure. A reservation is an admission
control, not a hard billing ceiling. Absolute spend containment would require
separate infrastructure/network controls and an independently proven cleanup
bound; billing alerts do not provide that guarantee.

### 6. Reconcile ownership, and make expiry cleanup independent

At each applied reconciliation, read cloud state before admitting a new launch.
Count every nonterminal owned instance and uncertain operation against the
one-instance limit. A corrupt/lost ledger, untracked matching resource or
unavailable provider blocks new launches; it does not initialize free capacity.

`reap` uses exact deployment/operation ownership and validated expiry tags.
It has its own explicit apply switch, does not need Forgejo, and can run with
separate cleanup-only credentials and no scaling database. Unknown or foreign
ownership is a refusal, never permission to delete. A failed termination stays
visible and retryable. Accepted termination is not remote confirmation that
the resource and its disposable dependencies have disappeared.

Before unattended use, an operator must schedule cleanup outside the scaler's
process/host failure boundary and test it while the scaler is stopped. A CLI
subcommand alone is not evidence that this path is deployed or independent.
Stopping launch admission is immediate rollback; stopping all cleanup is not.

### 7. Use bounded evidence and secret-safe output

Use the AWS SDK's operator-selected credential route; do not copy human login
caches into a service or worker. The controller needs scoped EC2 observe,
launch/tag and termination permissions plus only explicitly approved template
role passing. It does not need IAM administration or resource-construction
permissions. Actual policies belong to the deployment owner and need their
own review before a pilot.

Logs use stable reason codes, operation/resource IDs and bounded state fields.
Do not log authorization headers, credentials, user data, raw API responses or
complete configuration/environment dumps. Tests use synthetic secrets to
verify error paths without publishing real credentials.

## Risks / Trade-offs

- Spot can disappear without notice -> reconcile actual instance state; leave
  CI result/retry decisions with Forgejo and the workflow owner.
- A job can be claimed by another runner during boot -> accept possible idle
  startup and expire the unused instance; no exclusive job reservation claim.
- Broad labels can accept unintended jobs -> qualify the complete advertised
  set and its routing policy before the pilot.
- A local ledger is not highly available -> refuse uncertain state, preserve
  reservations and retain the independently scheduled cleanup path.
- API/permission semantics are not yet runtime-qualified -> block live support
  until the queue contract tests have representative evidence.
- Cleanup can fail or exceed its deadline -> stop new admission, expose the
  unresolved resource and report residual cost instead of a successful cap.

## Delivery and Rollback Plan

1. Bootstrap documentation, read-only public CI and this OpenSpec change.
2. Obtain independent design review; qualify the public API contract and add
   synthetic regression fixtures before controller implementation depends on it.
3. Implement observation first, then durable admission and mocked AWS lifecycle,
   then cleanup and failure recovery in focused reviewed pull requests.
4. Keep public CI credential-free and non-billable. Fault tests cover lost
   responses, restart, partial results, interruption and denied deletion.
5. A separate operator-specific pilot must approve identity, image, labels,
   connectivity, quota, spend/lifetime limits, cleanup and rollback before AWS
   mutation. No release/tag/image publication is part of this source change.

Rollback disables admission first, retains state and cleanup, terminates only
proven owned resources, and confirms their remote terminal state. Restore a
previous source version through a normal revert; do not rewrite public history.
No deployment or runtime verification is claimed by completing these artifacts.

## Interface Evidence

- [Forgejo public OpenAPI](https://codeberg.org/swagger.v1.json), inspected
  2026-11-08: reported version `17.0.0-99-322c239+gitea-1.22.0`, SHA-256
  `020885f071657c8504585fdff5cb4b5f06b9a48476c49e2f7316a7712c887850`.
  This establishes endpoint/field documentation, not live readiness semantics.
- [Forgejo ephemeral-runner security](https://forgejo.org/docs/latest/admin/actions/security/)
  and [registration](https://forgejo.org/docs/latest/admin/actions/registration/).
- [EC2 RunInstances](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_RunInstances.html)
  and [EC2 idempotency](https://docs.aws.amazon.com/ec2/latest/devguide/ec2-api-idempotency.html).
- [Spot interruption preparation](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/prepare-for-interruptions.html).
