# Operating ci-burst

This document is the operator contract for the product source in this
repository. It does not authorize a deployment, a cloud account change or a
pilot. Read [SECURITY.md](../SECURITY.md) first.

## Scope and authority

ci-burst observes one Forgejo repository queue and decides how many AWS Spot
workers should exist, up to a configured bound. It is a capacity decision, not a
scheduler:

- Forgejo owns job assignment, dependencies, retries and CI results.
- AWS owns instance lifecycle and billing.
- ci-burst owns launch intent, ownership metadata and a conservative local
  reservation.

The source ships an annotated release tag (`v0.1.0`) but no published image,
deployment or live-qualified Forgejo contract. A green validation run proves
source behavior only.

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
`aws.availability_zones`, `controller.max_instances` (an integer in `1..4`,
default `1`). Relative state paths resolve against the configuration file
directory. Unknown keys, floating-point money, a template-version alias, a
non-`https` Forgejo URL, a `controller.max_instances` outside `1..4` and a
malformed zone-candidate list (`ZONE_CANDIDATES_INVALID`) are refused.

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
python -m ci_burst apply  --config deployment.toml   # may launch the configured fleet
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

- its `status` is `waiting`, whatever its `needs` array says;
- its complete `runs_on` set is supported by the configured worker labels;
- its `handle` is present, and repeated records for that attempt agree.

Records with `running`, `success`, `failure`, `cancelled` or `skipped` do not
contribute, and a `blocked` record is counted but is never eligible demand. Any
status outside those and `waiting`, a missing required field, a non-array
payload other than `null`, invalid JSON, a response above the size bound or an
unreadable request makes demand unavailable rather than zero. The contract
defines no pagination, so one response is the complete queue for that
repository.

`needs` is metadata, not a dependency signal. The server fills it from the
workflow and leaves it populated after the dependency has succeeded, so an
empty-`needs` rule would hide every dependent job forever. A record without the
`needs` key is still refused: absent dependency metadata is an unexpected
shape, and it is not an empty queue.

**Dependency-blocked work is not listed.** This endpoint answers with the jobs a
runner with these labels may take and the ones already assigned; a job whose
dependencies are unfinished is not in that set. Measured on `forgejo
17.0.1+gitea-1.22.0` (`mikolajm/ai-agents` run 25399, 2026-11-10T20:33Z): the
run reported three `blocked` jobs (`route`, `c1`, `c2`, all `homelab`), and the
label-filtered response returned only one `running` record. `status` `blocked`
is therefore accepted and counted defensively — a server that starts returning
it is not treated as an unknown status — but no live record has been observed
and a deployment cannot qualify against one.

The `labels` query is mandatory, not an optimisation: the server answers JSON
`null` when it is absent. The adapter sends the worker's bare label names (the
part before the executor suffix, so `strong-ci` for `strong-ci:host`) and reads
`null` as an empty queue, because the server also answers `null` for a filter
that matches nothing. The filter is a union prefilter, so a job selected by one
shared label is still compared against its complete `runs_on` set above; it never
substitutes for that comparison.

Attempts are deduplicated by `handle`. A retry is a new handle and remains
distinguishable. ci-burst never assigns, requeues or reserves a job, and it does
not promise exclusivity merely because it observed a record. Untrusted response
values (status, handle) are never echoed in refusal details; an echoed handle
must match a bounded identifier shape or is reported as `<redacted>`.

### Qualifying live support

`qualification = "fixtures-only"` is the shipped default and blocks `apply`.
To qualify a deployment, the operator must, with an owner-scoped read token:

1. confirm the endpoint returns the shape above for the repository, including
   the mandatory `labels` filter and `null` for no match;
2. observe one known waiting job and one running job, and verify the demand
   classification. Dependency-blocked work cannot be part of this evidence: the
   endpoint does not list it (see § Queue contract);
3. record the evidence with the deployment;
4. only then set `qualification = "live-verified"`.

Observed on `forgejo 17.0.1+gitea-1.22.0`: `runs_on` holds bare label names
(`strong-ci`, `homelab`), an absent `labels` parameter returns `null`, a
non-matching filter also returns `null`, and the filter is a union over the job's
labels. Statuses observed in one run (`mikolajm/ai-agents` run 25336,
2026-11-10): a job waiting for a runner reports `waiting` and still lists the
dependency that already succeeded, while a dependent job reports `blocked`
until it does. A changed response shape or a different Forgejo version needs a
new contract identifier, not a re-declaration; correcting how the adapter reads
an unchanged shape does not.

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

### The routing decision

The worker advertises the heavy pool's label as well as its own: `strong-ci` and
`cloud-ci`, plus the image's architecture label (`linux-arm64`). Cloud capacity
is therefore extra capacity for the pool that actually backs up, not a separate
island — a `runs-on: strong-ci` job can be picked up by the burst worker, which
is the point of bursting. `cloud-ci` remains the explicit route: a workflow that
asks for it is asking for cloud capacity specifically, which is what a manual
dispatch uses to qualify a deployment without adding work to the pool. It is not
a guarantee that the qualification job is the one the worker runs — a shared
label means the worker takes whichever matching job Forgejo assigns next.

Both pools see `strong-ci` demand, and that is intended. It has consequences an
operator must budget for:

- the controller counts `strong-ci` demand, so it launches when the pool is busy
  rather than only when someone dispatches. Each launch reserves the configured
  maximum price × lifetime against the window, so a busy day consumes the
  allowance faster than a dispatch-only deployment would; the window guard is
  what stops it, and it refuses rather than overspending;
- a job the worker takes can fail if the worker or its network does. Forgejo
  records that failure like any other and the job can be re-run; nothing about
  the label makes the job less safe to run, because the worker runs the same
  repository code as the pool;
- the worker's own labels decide what Forgejo may **assign**, while
  `[worker].labels` in the deployment decides what the controller **counts** as
  demand. A supported deployment configures the same set; a mismatch either
  launches for work the worker cannot take, or misses work it could.

Complete-label-set matching is a subset check, so a job whose `runs_on` is a
subset of the worker's labels is eligible — including a job that asks only
`[strong-ci]` or only `[linux-arm64]`. That is consistent with sharing the pool
label. It also means a workflow that asks for a label the worker does not
advertise stays waiting for another runner, and no label match proves anything
about capability.

Accepted risk, stated plainly:

- a job that names only labels the worker advertises, and needs a capability the
  image lacks, may fail at run time **or may appear to succeed while producing
  the wrong artifact** — a host-architecture selection is the obvious example,
  and labels cannot detect it. Qualification of the image for the job class, and
  assertions inside the job, are the mitigations; neither the label match nor the
  controller verifies what a job produces;
- a worker that dies mid-job takes that job down with it. That is a failed run,
  not a wrong result, and it is why the burst label stays available for
  workloads that should only ever run on cloud capacity;
- removing a label is a coordinated change, not one edit. Taking `cloud-ci` out
  of `[worker].labels` alone still lets Forgejo assign a cloud-only job to a
  worker launched for pool congestion, because the worker keeps registering the
  label; taking it out of the bootstrap alone leaves the controller counting
  cloud-only demand and spending reservations on a worker that cannot take those
  jobs. Both sets change together, with the new template version this repository
  requires for any bootstrap change, and this section and the deployment example
  are the owners that record it.

## Budget and reservations

Before a launch, ci-burst computes an exact reservation in micro-units:

```
reservation = ceil(max_spot_price * max_lifetime_seconds / 3600)
            + non_compute_allowance
```

Every worker in a batch reserves this same `per_launch` amount, so a batch of N
launches needs `N × per_launch` of the current window's remaining allowance. The
configured limit caps the batch; the allowance can cap it first.

A launch is refused unless the selected candidate has a fresh Spot quote for the
configured instance type and its availability zone, was observed no longer ago
than `price_max_age_seconds`, and is not above `max_spot_price`. Freshness is the
age of the **observation**: a Spot price records when it last *changed*, and a
price that has been stable for hours is still current, so the provider's own
timestamp is carried as evidence rather than used as the bound. Every configured
candidate is priced in order; one whose quote is missing, stale, out of scope or
above the ceiling is skipped and the next candidate is considered, so one
expensive zone does not refuse a launch another configured zone can serve. The
quote response must identify its own availability zone, instance type and
`Linux/UNIX` product; missing metadata is not replaced with the requested scope.
The reservation is computed from `max_spot_price`, never from an observed quote,
and the current window must still have `allowance` left after it. The window is
anchored at a multiple of `window_seconds`. Outstanding operations carry their
reservation into later windows; a completed operation is charged to the window
it started in. A **definite** provider rejection releases the reservation,
because the provider proved that no instance was created; an uncertain outcome,
or a rejected retry, keeps it until the intent is resolved. Requesting
termination does not refund consumed allowance, and a restart does not reset it.

The reservation estimates compute at the configured maximum price for the
configured lifetime, plus the configured non-compute allowance. It does not
bound EBS, data transfer, public addressing, log delivery, minimum billing
increments, Spot price changes after launch, or resources left by failed
cleanup. It is not a guaranteed cap on the AWS bill, and no status, lifetime or
alert in this project should be described as one.

### Sizing the window

Every launch the controller admits reserves the configured maximum price ×
lifetime plus the non-compute allowance, and that reservation is charged to an
**anchored** window: boundaries are floored from the epoch at a multiple of
`window_seconds`, and outstanding operations carry into later windows. It is not
a trailing period, so the end of one window and the start of the next are two
budgets that can be spent back to back around the boundary.

A batch of N workers reserves N times the per-launch amount in one tick. The
same allowance therefore admits `allowance // per_launch` launches per anchored
window, whether they are admitted one tick at a time or several per tick up to
`controller.max_instances`. That is a bound per window, not per rolling hour or
per day: a full batch just before a boundary and another just after can reserve
up to twice the allowance across the pair of windows. Size the allowance from
launches per configured window; "per day" is the same statement only when
`window_seconds` is 86400.

"Per dispatch" is not a safe unit either. The controller admits a launch whenever
eligible demand remains and a slot is free, so one dispatched workflow with
several eligible jobs can spend several reservations as its one-job workers
finish. Only a workload with an established one-launch-per-dispatch shape can be
sized by dispatches; every other deployment is sized by admitted launches.

Which attempts stop charging is narrow:

- a **proven rejection before creation** releases its reservation;
- an **accepted launch** keeps its reservation even if the instance never
  reaches `running` or dies during startup, because the provider may have created
  and billed something;
- an uncertain outcome keeps it until the intent is resolved.

So a window can be exhausted without a job ever running — accepted launches that
failed to start, or attempts left uncertain. That is the guard being
conservative on purpose rather than a defect.

Worked example from the pilot: a `0.1200` maximum price with a one-hour lifetime
and a `0.50` non-compute allowance reserves `0.62` per launch, so a `5.00` window
admits eight (`8 × 0.62 = 4.96`; a ninth would need `5.58`). One evening of
shared-pool launches plus five capacity rejections from before the release rule
consumed exactly that, and every cycle then refused with
`budget_allowance_exhausted` until the window rolled.

## Fleet size and fan-out

Admission is demand-driven. One tick computes a batch size from three bounds:

```
launches = min(waiting jobs, free slots, affordable reservations)
```

- **Waiting jobs** is the eligible demand in the qualified queue observation.
- **Free slots** is `controller.max_instances` minus the non-terminal owned
  operations in the ledger. An intent, starting, running, terminating or
  uncertain worker occupies a slot; a terminal or failed one does not.
- **Affordable reservations** is `(allowance - consumed) // per_launch` for the
  current anchored window, with `per_launch` as defined in § Budget and
  reservations.

A zero bound is not an error. No free slot waits with `slot_busy`, no demand waits
with `no_runnable_demand`, and no affordable reservation refuses with
`budget_allowance_exhausted`. When the allowance allows fewer launches than both
demand and free slots, the batch is capped and the decision carries the
`budget_capped_batch` note. `plan` reports the same `demand`, `free_instances`
and `launch_count` that an `apply` would use.

Each worker in a batch has its own launch intent, ledger operation, frozen
request, client token and reservation. A batch is not one admission with several
instances: every worker can be resumed, reconciled, expired and charged on its
own. Every request still sends `MinCount = MaxCount = 1`.

The launch order rotates the eligible candidate list by the worker index
(`eligible[i:] + eligible[:i]`), so three workers over three zones select the
configured order, then the next zone, and so on. Rotation spreads workers across
the ordered zones; it is not a capacity guarantee, and each worker still walks
its own candidate order only on a definite capacity rejection.

The worker contract stays one job per worker. An idle worker waits for one job
until its own maximum lifetime, then shuts down and terminates; the reservation
was charged when it launched and that termination does not refund it. Fan-out
therefore trades reserved allowance for readiness: N workers can take N jobs at
once, and a worker that never gets a job still costs up to its configured
lifetime.

If any worker in the batch ends in anything other than an accepted launch, the
controller stops the batch and leaves the workers already launched in place. It
does not terminate them to make the batch uniform.

Lowering `controller.max_instances` below the number of live owned workers never
terminates anything. New admission waits with `slot_busy` and the decision
carries the note `above_configured_limit` until enough workers finish on their own
lifetime; expiry cleanup remains the only owner of termination.

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

`apply` admits a batch of at most `controller.max_instances` workers, sized by
waiting demand, free slots and affordable reservations (§ Fleet size and
fan-out). Each worker in the batch is its own admission, with its own frozen
request, ledger operation, client token and reservation; the batch stops as soon
as one worker is not accepted. Within one worker, if the provider definitely
rejects the selected candidate for insufficient capacity
(`InsufficientInstanceCapacity`), that rejection proves no instance was created,
so that admission advances to the next eligible candidate in order — at most once
per candidate — and a success ends the walk. Every attempt sends `MinCount =
MaxCount = 1`, so no fallback and no batch can produce a second instance for one
worker. Before each worker and each advance the controller re-reads the clock:
the candidate's Spot quote must still be within `price_max_age_seconds` and the
admission lifetime must not have elapsed. A candidate whose evidence expired is
recorded as `skipped` (`spot_price_stale`), not frozen or sent, and the next
remaining candidate is considered. The result reports a `launches` array with one
entry per admitted worker (`operation`, `instance_id`, `zone`, `reservation`,
`decision`) and the ordered `attempts` of the first launch, or of the launch
that stopped the batch, with each outcome (`capacity_rejected`, `accepted`,
`rejected`, `uncertain` or `skipped`); the top-level `operation`, `instance_id`,
`expires_at` and `reservation` are the first launch's values, so a one-worker
deployment reads the shape it did before. `selected` remains the batch's first
choice, so the accepted attempt is the placement that actually launched.

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
`shutting-down`, `stopping` and `stopped` keep the operation outstanding and its
slot occupied. Expiry cleanup terminates only an instance that is both
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
ownership is reported, never terminated. A **live** owned instance whose
`ci-burst:operation` tag names no ledger operation blocks new launches: it may
be spending money nothing accounts for, and an operator has to look. A
**terminated** instance in that same position — a real operation id the ledger
never recorded — is reported under `slot.ignored_discrepancies` and blocks
nothing: EC2 keeps listing a terminated instance for about an hour, and
refusing launches until then would disable the burst after every legitimate
termination the ledger did not record. An instance whose operation tag is
missing or `pending` is a different case and still blocks, terminated or not,
because nothing says which operation owned it.

## Identities

Use two routes, both machine identities; never copy a human login cache and
never place credentials on a worker.

- **Scaling identity**: read instance, template and Spot-price data; launch up
  to the configured fleet size; create the ownership tags; request termination.
  It needs no IAM administration, image creation, VPC construction or secret
  access.
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
zero residual cost. It also reports the configured `controller.max_instances`,
the anchored window's `allowance`, `consumed` and `launches`, the number of
`busy_instances`, and the `hourly_ceiling` at that limit
(`max_spot_price × max_instances`).

## Supported limits

One repository, one Region and one ordered list of availability-zone candidates
(the single-zone key is the one-candidate form), up to
`controller.max_instances` (1..4) owned instances, one writer and one ledger.
Ordered fallback is supported only for a definite insufficient capacity
rejection, within one admission and at most once per configured candidate. Not
supported: concurrent controllers, multiple ledgers, warm pools, On-Demand
fallback, automatic zone or Region discovery and substitution, provider
selection, job retries, workflow rewrites, image construction and secret
distribution.
