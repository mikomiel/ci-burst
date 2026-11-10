# Operating ci-burst

This document is the operator contract for the product source in this
repository. It does not authorize a deployment, a cloud account change or a
pilot. Read [SECURITY.md](../SECURITY.md) first.

## Scope and authority

ci-burst observes one Forgejo repository queue and decides whether one more
AWS Spot worker should exist. It is a capacity decision, not a scheduler:

- Forgejo owns job assignment, dependencies, retries and CI results.
- AWS owns instance lifecycle and billing.
- ci-burst owns launch intent, ownership metadata and a conservative local
  reservation.

The source ships no release, image, tag, deployment or live-qualified Forgejo
contract. A green validation run proves source behavior only.

## Configuration

Configuration is one TOML file. It carries product input, never a credential;
the Forgejo token is read from the environment variable named by
`forgejo.token_env`, and AWS uses the SDK credential chain.

```toml
deployment = "example-ci-burst"

[forgejo]
base_url = "https://forgejo.example"
repository = "example-owner/example-repository"
token_env = "CI_BURST_FORGEJO_TOKEN"
contract = "forgejo-17.0.1-runners-jobs"
qualification = "fixtures-only"

[worker]
labels = ["ci-burst:host"]

[aws]
region = "eu-central-1"
availability_zone = "eu-central-1a"
launch_template_name = "example-ci-burst-runner"
launch_template_version = 3
max_spot_price = "0.0400"
max_lifetime_seconds = 3600

[budget]
window_seconds = 86400
allowance = "10.00"
non_compute_allowance = "0.50"

[state]
database = "state.sqlite3"
```

Optional keys: `forgejo.timeout_seconds` (10), `forgejo.max_response_bytes`
(2,000,000), `aws.price_max_age_seconds` (900), `aws.profile`,
`aws.availability_zones`, `controller.max_instances` (only `1` is accepted).
Relative state paths resolve against the configuration file directory. Unknown
keys, floating-point money, a template-version alias, a non-`https` Forgejo URL,
a second instance and a malformed zone-candidate list (`ZONE_CANDIDATES_INVALID`)
are refused.

### Availability-zone candidates

`aws.availability_zone` above is the original one-candidate form: the launch
template keeps the network choice. To let a capacity rejection in one zone fall
through to another, replace it with an ordered array of tables:

```toml
[aws]
region = "eu-central-1"
launch_template_name = "example-ci-burst-runner"
launch_template_version = 3
max_spot_price = "0.0400"
max_lifetime_seconds = 3600

[[aws.availability_zones]]
zone = "eu-central-1a"
subnet_id = "subnet-0123456789abcdef0"

[[aws.availability_zones]]
zone = "eu-central-1b"
subnet_id = "subnet-0123456789abcdef1"
launch_template_version = 4
```

The order is the priority. Every candidate requires `zone` and `subnet_id`; an
optional `launch_template_version` overrides the top-level version for that
candidate. A `subnet_id` is `subnet-` followed by exactly 8 or 17 hexadecimal
characters. A candidate list that is empty, repeats a zone, lacks a valid
subnet, carries an unknown key, uses a non-integer template version, names a
zone outside the configured Region, or is supplied together with
`availability_zone` is refused at load time with the stable code
`ZONE_CANDIDATES_INVALID`. Placement is static configuration: ci-burst never
discovers zones and never substitutes a zone that is not configured.

The candidate subnet is sent as the `RunInstances` request's `SubnetId`
parameter, so a template used with a candidate list must not pin network
interfaces or a subnet itself. The single-zone form sends no subnet override and
keeps the network choice in the launch template.

The source runs on Python 3.12 or newer and needs `boto3` at runtime. The
standard-library tests run without the SDK, a token or a cloud account.

## Commands

```sh
python -m ci_burst plan   --config deployment.toml   # observation only
python -m ci_burst apply  --config deployment.toml   # may launch one instance
python -m ci_burst status --config deployment.toml   # evidence, no mutation
python -m ci_burst reap   --config deployment.toml   # expired resources, no launch
python -m ci_burst reap   --config deployment.toml --apply
```

Exit codes: `0` for a completed decision, `2` for a refusal (nothing was
mutated), `1` for a provider or cleanup failure. Every command prints one JSON
document with stable reason codes. `plan` performs no durable write and no
cloud mutation; `apply` is the only launch path; `reap` never needs Forgejo or
the scaling ledger.

## Queue contract

The qualified contract is `forgejo-17.0.1-runners-jobs`:
`GET /api/v1/repos/{owner}/{repo}/actions/runners/jobs`, authenticated with
the operator's token, returning one JSON array of `ActionRunJob` records.

A record is eligible demand only when:

- its `status` is `waiting`;
- its `needs` array is present and empty (dependency-blocked work is not
  demand; absent dependency metadata makes the observation unavailable);
- its complete `runs_on` set is supported by the configured worker labels;
- its `handle` is present, and repeated records for that attempt agree.

Records with `running`, `success`, `failure`, `cancelled` or `skipped` do not
contribute. Any other status, a missing required field, a non-array payload,
invalid JSON, a response above the size bound or an unreadable request makes
demand unavailable rather than zero. The contract defines no pagination, so the
single response is the complete queue for that repository; the adapter does not
use the server-side label filter because its semantics are not qualified.

Attempts are deduplicated by `handle`. A retry is a new handle and remains
distinguishable. ci-burst never assigns, requeues or reserves a job, and it does
not promise exclusivity merely because it observed a record. Untrusted response
values (status, handle) are never echoed in refusal details; an echoed handle
must match a bounded identifier shape or is reported as `<redacted>`.

### Qualifying live support

`qualification = "fixtures-only"` is the shipped default and blocks `apply`.
To qualify a deployment, the operator must, with an owner-scoped read token:

1. confirm the endpoint returns the array shape above for the repository;
2. observe one known waiting job, one dependency-blocked job and one running
   job, and verify the demand classification;
3. record the evidence with the deployment;
4. only then set `qualification = "live-verified"`.

A different Forgejo version or a changed response shape needs a new contract
identifier, not a re-declaration.

## Worker image and launch template

The image and template are operator-owned prerequisites. ci-burst launches a
pinned template; it does not build images, configure networks or distribute
registration credentials. A complete setup guide, with the IAM role, SSM
SecureString parameter, network choices and bootstrap, is
[runner-setup.md](runner-setup.md). The controller refuses a template that is not
usable:

- numeric `launch_template_version`, never an alias;
- literal `ImageId`, never `resolve:ssm:` or another indirection;
- `InstanceType` set;
- `InstanceInitiatedShutdownBehavior = terminate`;
- bootstrap `UserData` present (the controller checks presence only and never
  reads, stores or reports its contents).

The worker contract the template must implement:

- a repository-scoped ephemeral runner that executes at most one job
  (`forgejo-runner one-job` or equivalent);
- process exit after the assigned job, and exit on bootstrap failure or when
  no job arrives, followed by instance shutdown and termination;
- disposable volumes with delete-on-termination;
- no controller, provisioning or AWS credential on the worker;
- registration credentials acquired through an operator-owned secure channel.
  Inline user data, image layers, command arguments and logs are not accepted
  channels, and ci-burst never generates or forwards that secret;
- a maximum lifetime that covers image boot, a delayed job and a failed
  bootstrap. Ephemeral registration is not an expiry mechanism.

A candidate may pin its own numeric template version. Every version a candidate
names must satisfy this contract; a template whose `Placement` pins one zone
makes the other candidates ineligible instead of overriding that pin.

Changing the image or bootstrap means a new template version and a
configuration change; an approved version is not edited in place.

## Labels

Labels are routing claims, not evidence of hardware, architecture or toolchain
capability. A job label matches a worker label when the two are equal or the
worker label's name part before `:` is equal (for example `homelab` matches
`homelab:host`). The whole job label set must match.

A broad label can also accept jobs outside the intended class. Exclusive burst
routing requires a label only the burst worker advertises, and the operator
must qualify that label against the image and machine before relying on it.

## Budget and reservations

Before a launch, ci-burst computes an exact reservation in micro-units:

```
reservation = ceil(max_spot_price * max_lifetime_seconds / 3600)
            + non_compute_allowance
```

A launch is refused unless the selected candidate has a fresh Spot quote for the
configured instance type and its availability zone, is not older than
`price_max_age_seconds`, and is not above `max_spot_price`. Every configured
candidate is priced in order; one whose quote is missing, stale, out of scope or
above the ceiling is skipped and the next candidate is considered, so one
expensive zone does not refuse a launch another configured zone can serve. The
quote response must identify its own availability zone, instance type and
`Linux/UNIX` product; missing metadata is not replaced with the requested scope.
The reservation is computed from `max_spot_price`, never from an observed quote,
and the current window must still have `allowance` left after it. The window is
anchored at a multiple of `window_seconds`. Outstanding operations carry their
reservation into later windows; a completed operation is charged to the window
it started in. Requesting termination does not refund consumed allowance, and a
restart does not reset it.

The reservation estimates compute at the configured maximum price for the
configured lifetime, plus the configured non-compute allowance. It does not
bound EBS, data transfer, public addressing, log delivery, minimum billing
increments, Spot price changes after launch, or resources left by failed
cleanup. It is not a guaranteed cap on the AWS bill, and no status, lifetime or
alert in this project should be described as one.

## Launch decision and capacity fallback

`plan` reports every configured candidate in order, each with the zone, subnet,
template version, eligibility and the Spot quote used for it when one was read,
plus the `selected` candidate. A candidate is `skipped` with a stable reason
(`spot_price_unavailable`, `spot_price_stale`, `spot_price_wrong_scope`,
`spot_price_above_cap`, `template_placement_mismatch` or
`template_bootstrap_missing`); when none is eligible the decision is `refused`
with those reasons and `no_eligible_candidate`. A provider error while reading a
price refuses the whole plan (`spot_price_unavailable`) instead of silently
dropping a configured candidate; only an answered quote that is absent from the
requested scope marks that candidate skipped. A configured template version that
cannot be read or is unusable refuses the plan with `template_unusable` and
`template_error` rather than dropping that candidate.

`apply` still launches at most one instance: one admission, one reservation, one
ledger operation and one slot. If the provider definitely rejects the selected
candidate for insufficient capacity (`InsufficientInstanceCapacity`), that
rejection proves no instance was created, so the same admission advances to the
next eligible candidate in order — at most once per candidate — and a success
ends the walk. Every attempt sends `MinCount = MaxCount = 1`, so no fallback can
produce a second instance. Before each advance the controller re-reads the clock:
the candidate's Spot quote must still be within `price_max_age_seconds` and the
admission lifetime must not have elapsed. A candidate whose evidence expired is
recorded as `skipped` (`spot_price_stale`), not frozen or sent, and the next
remaining candidate is considered. The result reports the ordered `attempts`
with each outcome (`capacity_rejected`, `accepted`, `rejected`, `uncertain` or
`skipped`); `selected` remains the admission's first choice, so the accepted
attempt is the placement that actually launched.

No other failure falls through: a non-capacity definite rejection keeps today's
`launch_failed` behaviour, and an uncertain response keeps the operation
outstanding. If every candidate is rejected for capacity the operation is
`failed` — the slot is free — the queue stays unserved, and the result carries
the stable `capacity_exhausted` reason plus the final provider refusal. If the
remaining candidates' price evidence expires during the attempt, the operation
is also `failed` with a free slot and the stable `spot_price_stale` reason, and
no new admission or reservation is created. Nothing is reported as launched.

## Durable state and ownership

One SQLite ledger records operation identity, frozen request, request hash,
client token, expiry, reservation, the instance id and the verified AWS scope
(Region and account). A process-wide file lock
admits exactly one writer; a second writer refuses before any cloud mutation.
The supported topology is one deployment, one controller host and one ledger
copy. Tags are ownership metadata, not a distributed lock.

The lifecycle is `intent -> starting -> running -> terminating -> terminal`,
with `uncertain` for a lost launch response and `failed` for a definite first
refusal. Intent is committed before the launch request. A lost response is
resolved by discovering instances tagged with the operation id, never by
allocating a replacement operation. While an intent has no confirmed instance,
the next apply resumes that **same** frozen request and client token; EC2
idempotency makes the retry safe. A rejected **retry** keeps the operation
uncertain, because rejecting the retry does not prove the original request
created nothing. Only a definite rejection of the first attempt may release the
slot, and ambiguous provider errors stay uncertain.

The frozen intent names the candidate currently being attempted. A definite
capacity rejection is the one case that may replace it: the same operation
advances to the next ordered candidate, replacing the frozen request, its hash
and its own client token while keeping the operation id, admission window and
reservation. A changed placement needs its own idempotency token because one
token cannot describe two requests. A resumed intent is retried exactly as
frozen and never falls through, because a rejected retry cannot prove the
earlier request created nothing.

Two bounds refuse rather than launch: an unresolved intent older than the
24-hour client-token validity, and an intent whose configured lifetime has
already elapsed. Both keep the unresolved ownership and block admission until
an operator resolves them.

The EC2 client is built without the SDK's transparent retries, so one recorded
provider call is one HTTP attempt; the controller's own resume is the only
retry of a launch. Admission, quote freshness and the lifetime are evaluated
against the clock **after** the synchronous Spot-price read, so a slow provider
call cannot commit an already-expired intent or extend stale price evidence.

Termination is confirmed only by the provider reporting `terminated` (or by the
instance record disappearing after the provider's visibility window).
`shutting-down`, `stopping` and `stopped` keep the operation outstanding and the
single slot occupied. Expiry cleanup terminates only an instance that is both
present in the current owned inventory and carries a valid, expired expiry tag;
a ledger id alone is not permission to terminate.

If a confirmed instance disappears from a by-id read, the operation stays
outstanding until EC2's retained-visibility window has elapsed; a recent
`InvalidInstanceID.NotFound` is not treated as immediate proof of termination.

The scope is part of ownership. `apply` resolves the account with
`sts:GetCallerIdentity` over the **same SDK session** that reads and launches
EC2, so identity and inventory cannot resolve different credentials; it refuses
with `SCOPE_CHANGED` while any outstanding operation belongs to another Region
or account; an inventory from a different
scope can never establish that such an operation terminated. `plan` performs
the same Region check without an AWS call. Resolve or clean up the outstanding
operation in its original scope before changing the credential route.
An unclassified provider or response-parsing exception is uncertain, never a
definite failure, because it can occur after the service accepted a request.

The owned inventory is read across every `DescribeInstances` page. An inventory
that cannot be completed, an instance whose operation is not in the ledger, a
terminal operation that still owns a live resource, and two live instances for
one operation all block new admission. Lingering `terminated` records are not
ownership claims and do not block.

Observation opens the ledger read-only. It never creates, initializes or
migrates the file; an existing but uninitialized, unsupported or corrupt ledger
makes `plan` refuse instead of computing a free slot from zero consumption. The
ledger uses a rollback journal so a read-only observation creates no
`-wal`/`-shm` side files. The state path is converted to an escaped file URI
before it is opened read-only, so a filename containing URI metacharacters
cannot select another file.

Every launch carries `ci-burst:managed`, `ci-burst:deployment`,
`ci-burst:operation` and `ci-burst:expires-at` tags. Discovery and cleanup
select only that scope; an instance with missing, foreign or contradictory
ownership is reported, never terminated. An owned instance that cannot be
reconciled with the ledger blocks new launches.

## Identities

Use two routes, both machine identities; never copy a human login cache and
never place credentials on a worker.

- **Scaling identity**: read instance, template and Spot-price data; launch
  one instance; create the ownership tags; request termination. It needs no
  IAM administration, image creation, VPC construction or secret access.
- **Cleanup identity**: describe and terminate owned instances only. It must
  work without Forgejo, without the scaling database and without launch
  permission.

Scope resource permissions to the deployment's tags where the provider
supports it, and review the policy before a pilot. The controller never
creates IAM resources, networks, images or bootstrap-secret infrastructure.

## Scheduler, cleanup and rollback

Run `apply` from an external scheduler. Schedule `reap --apply` separately,
outside the scaler's process and host failure boundary, and test it while the
scaler is stopped. A `reap` command in the repository is not evidence that this
path is deployed.

Rollback order:

1. stop admission (disable the `apply` schedule) — immediate;
2. keep expiry cleanup running;
3. inspect `status` and the ledger;
4. terminate only proven owned resources and confirm their terminal state;
5. revert source through a reviewed change; do not rewrite published history.

Stopping cleanup is not part of rollback.

## Status semantics

`status` reports what is known and what is not. An EC2 `running` instance is not
a registered runner, a ready runner or a successful job. When the provider is
unavailable or cleanup cannot be confirmed, status reports the unresolved
operation and failure; it never reports clean completion, a successful job or
zero residual cost.

## Supported limits

One repository, one Region and one ordered list of availability-zone candidates
(the single-zone key is the one-candidate form), one owned instance, one writer
and one ledger. Ordered fallback is supported only for a definite insufficient
capacity rejection, within one admission and at most once per configured
candidate. Not supported: concurrent controllers, multiple ledgers, warm pools,
On-Demand fallback, automatic zone or Region discovery and substitution,
provider selection, job retries, workflow rewrites, image construction and
secret distribution.
