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
- ci-burst owns launch intent, ownership metadata and a conservative forward
  cost commitment.

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
`aws.availability_zones`, `aws.instance_types` (an ordered list of at most four
type names), `controller.max_instances` (an integer in `1..12`, default `1`).
Relative state paths resolve against the configuration file directory. Unknown
keys, floating-point money, a template-version alias, a non-`https` Forgejo URL,
a `controller.max_instances` outside `1..12`, a malformed zone-candidate list
(`ZONE_CANDIDATES_INVALID`) and a malformed instance-type list
(`INSTANCE_TYPES_INVALID`) are refused.

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

### Instance-type pools

Every configured candidate can be multiplied by an ordered instance-type list,
so the Region can offer more Spot capacity than one type has across its zones:

```toml
[aws]
region = "eu-central-1"
launch_template_name = "example-ci-burst-runner"
launch_template_version = 6
instance_types = ["m7g.xlarge", "m8g.xlarge"]
max_spot_price = "0.0400"
max_lifetime_seconds = 3600

[[aws.availability_zones]]
zone = "eu-central-1a"
subnet_id = "subnet-0123456789abcdef0"
```

The order is type-major and it is the priority: every zone of `m7g.xlarge`
first, then every zone of `m8g.xlarge`. A definite capacity rejection walks to
the next pool in that order within the same admission and operation, and a
rotation that starts inside the first type stays there until that type's pools
are exhausted. A worker always starts on the first type: a batch spreads across
its zones only, and if none of them has fresh price evidence the batch stops
with `spot_price_stale` rather than starting a later type. Without a configured
list each candidate keeps its template version's type and the batch rotates
across every eligible candidate as before.

The list is optional. When it is absent, the launch template's own
`InstanceType` is the only pool per candidate, which is the original behaviour.
When it is present it is authoritative, and `RunInstances` carries the selected
pool's `InstanceType` explicitly; the template's type stays required as the
default and as the shape reference.

Every configured type must have the template type's exact vCPU count and memory
size, and the template image must support its architecture. The controller does
not read instance-type metadata: `scripts/check_prerequisites.py` verifies that
each type is offered in each configured zone and that shape and architecture
match, so drift is visible before the list is enabled. A list that is empty,
repeats a type, names a malformed type, or holds more than four types is
refused at load time with `INSTANCE_TYPES_INVALID`. The bound exists because
every admission prices every pool: four types in three zones is twelve Spot
price reads per tick.

The source runs on Python 3.12 or newer and needs `boto3` at runtime. The
standard-library tests run without the SDK, a token or a cloud account.

## Commands

```sh
python -m ci_burst plan   --config deployment.toml   # observation only
python -m ci_burst apply  --config deployment.toml   # may launch the configured fleet
python -m ci_burst status --config deployment.toml   # evidence, no mutation
python -m ci_burst export --config deployment.toml \
                          --metrics-file /var/lib/prometheus/node-exporter/ci-burst.prom
python -m ci_burst reap   --config deployment.toml   # expired resources, no launch
python -m ci_burst reap   --config deployment.toml --apply
```

Exit codes: `0` for a completed decision, `2` for a refusal (nothing was
mutated), `1` for a provider or cleanup failure. Every command prints one JSON
document with stable reason codes. `plan`, `status` and `export` perform no
cloud mutation and no ledger write; `apply` is the only launch path; `reap`
never needs Forgejo or the scaling ledger.

## Worker evidence

The queue cannot tell you about a worker that never arrived. `export` reads the
console of the instances this deployment owns and turns the shipped first-boot
markers into two artefacts:

```sh
python -m ci_burst export --config deployment.toml \
  --metrics-file /var/lib/prometheus/node-exporter/ci-burst.prom \
  [--events-file PATH] [--state-file PATH]
```

The two optional paths default to `<ledger directory>/export/`, as
`worker-events.ndjson` and `observed.json`.

### The event stream

One JSON object per line:

```json
{"id":"i-0123456789abcdef0:step:register","ts":"2026-11-11T16:55:17Z","deployment":"example","instance":"i-0123456789abcdef0","zone":"eu-central-1a","operation":"...","event":"step","step":"register"}
{"id":"i-0123456789abcdef0:exit:0","ts":"...","deployment":"example","instance":"i-0123456789abcdef0","zone":"eu-central-1a","operation":"...","event":"exit","rc":0}
```

`event` is `step` (a step name from the shipped script's fixed set), `exit`
(`bootstrap exiting rc=N`) or `error`, where `kind` is one of `forgejo_ping`,
`ssm_timeout`, `apt_failed`, `register_failed` or `runner_error`. No console
text is published: a failure line contributes its kind only. The console is not
a trusted channel, and truncation is not redaction — read the raw line with the
provider CLI when you need it.

The file is replaced atomically on each run rather than appended to in place, so
a collector must open it by path and must not hold a descriptor across runs; that
rewrite is also what makes rotation and the size bound possible.

Delivery is at least once. `id` is derived from the instance and the marker, so
a repeat after a crash between publishing an event and saving the dedupe record
carries the same identity and a consumer can drop it. The dedupe record keeps
what was already emitted and is pruned after seven days; deleting it loses the
history and the cumulative counters, never a decision.

### The metrics textfile

Every series carries the deployment label. `ci_burst_worker_launches_total`,
`ci_burst_worker_registered_total` and `ci_burst_worker_unregistered_total` are
cumulative counters kept separately from the expiring per-instance records, so
they do not fall when an instance ages out of the provider's record window or
when a record is pruned. `ci_burst_worker_unregistered_total` counts workers
that ended without ever showing the registration marker; evidence observed once
is never un-learned by a later empty console. `ci_burst_spend_micros`, `ci_burst_committed_micros`,
`ci_burst_allowance_micros` and `ci_burst_remaining_micros` come from the
read-only ledger, are gauges in micro-units, and are omitted — with a
`ledger_omitted` note in the JSON result — when it cannot be read.
`ci_burst_export_last_run_timestamp_seconds` is what a consumer uses to detect a
stopped exporter.

### Bounds

| Bound | Value |
| --- | --- |
| console reads per run | 16, over the records that can still carry new evidence |
| non-terminal owned instances | above `controller.max_instances`: reported with `above_configured_limit` |
| unfinished live workers per run | refused above 16 (`console_read_cap_exceeded`) |
| bytes read per console | 64 KiB |
| error kinds per instance | 3 |
| published console text | none |
| event file | rotated at 4 MiB |
| dedupe record | pruned after 7 days |

A terminated worker keeps its ownership tags for about an hour after termination,
so a busy fleet legitimately shows many terminal records at once. A record is
read again only while its console can still change: every run while the provider
reports the instance live or terminating, and finished after the read that
observed it `terminated` or `stopped`. An exit line on the console is not that
signal — the bootstrap can finish while the instance and its console carry on.
Live workers are read first, so the growing consoles are never the ones deferred.

A run whose unfinished non-terminal candidates fit within the budget but whose
candidate list exceeds it defers the surplus terminal history with the note
`console_read_deferred`, and the next run picks it up while it is still visible.
Above that budget the run refuses instead, because live evidence cannot be
covered in one run. A deferred run is partial observation: the note is the
signal, and the run must not be read as complete evidence.

The read budget is independent of the admission ceiling. A deployment running
more non-terminal owned workers than its configured `controller.max_instances`
is a supported drain, not a refusal: the run reports `above_configured_limit` and
still writes both artefacts. `shutting-down`, `stopping` and `stopped` still
occupy a slot, so the count matches the controller's. The only refusal is the
read budget itself: when more than 16 unfinished non-terminal workers are
candidates, live evidence cannot be covered in one run and the command refuses
(`console_read_cap_exceeded`, exit 2, nothing written).

An instance without the ownership tags — a hand-launched canary — is invisible
here on purpose; read its console directly with the provider CLI instead.

Every input is validated before it is republished, because the command rewrites
files it also reads. An ownership tag that is not one this deployment generated
is omitted; a record, an instance key or a historical event line that no longer matches the
schema — or that belongs to another deployment — is dropped (`state_rebuilt` or
`events_pruned` in the result); a record with no usable retention timestamp is
dropped rather than kept forever;
a ledger state outside the ledger's own vocabulary is counted as `invalid`
(`ledger_state_unknown`). None of those is a failure: derived evidence that
cannot be validated is discarded, not published. The
command never calls Forgejo and never mutates a resource, so it keeps working
when the queue or the admission path is the thing that is broken.


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
- `InstanceType` set — the template's type, and the shape anchor for any
  configured `aws.instance_types` list;
- `InstanceInitiatedShutdownBehavior = terminate`;
- bootstrap `UserData` present (the controller checks presence only and never
  reads, stores or reports its contents).

The repository's [`deploy/runner/bootstrap.sh`](../deploy/runner/bootstrap.sh) is
the user data of the version a candidate pins: edit it, publish a new template
version and bump the pin together. `scripts/check_prerequisites.py` compares the
pinned version's user data with that file and reports drift, so an edit that was
merged but never published does not pass unnoticed; the controller itself still
checks presence only.

The worker contract the template must implement:

- a repository-scoped ephemeral runner that executes at most one job
  (`forgejo-runner one-job` or equivalent);
- process exit after the assigned job, and exit on bootstrap failure or when
  no job arrives, followed by instance shutdown and termination;
- disposable volumes with delete-on-termination;
- no controller, provisioning or AWS credential on the worker;
- registration credentials acquired through an operator-owned secure channel.
  Inline user data, image layers and logs are not accepted channels, and
  ci-burst never generates or forwards that secret. The pinned runner CLI
  accepts its token only as an argument, so the published bootstrap reads it
  from a `0600` file and shreds that file before the job starts; the exposure
  this leaves to host root and exec auditing, and its bounds, are in
  [runner-setup.md § Threat model](runner-setup.md#threat-model);
- a maximum lifetime that covers image boot, a delayed job and a failed
  bootstrap. Ephemeral registration is not an expiry mechanism;
- the job toolchain the deployment's queue needs, not only the runner binary.
  Runner classes differ by architecture and by cost tier, never by which
  packages are installed: a job may be batched onto any runner of its
  architecture, so every image carries the same set the persistent fleet
  provides. `jq`, `promtool`, the pinned node/uv/ruff toolchain, the PostgreSQL
  server binaries and whatever parsers a job invokes are the usual members.

  A missing tool does not reliably fail the job. Depending on the workflow it
  either fails, or skips the checks that needed it and exits green — a green
  result that validated less than it claims. So the requirement lives here
  rather than in a label, and a job keeps its own assertions about what it
  needs; the image is verified rather than assumed.

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

Because the toolchain is part of every image, the labels a job names are
expected to say *where* it runs, not *what is installed*: a pool or arch label
must never be the mechanism that keeps a job away from a runner whose image is
missing a package. An image that lacks part of the queue's toolchain is a
runner-baseline defect, fixed by changing the image, and routing around it hides
the defect for as long as the label holds.

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
  cloud-only demand and committing cost to a worker that cannot take those
  jobs. Both sets change together, with the new template version this repository
  requires for any bootstrap change, and this section and the deployment example
  are the owners that record it.

## Cost tracking and the allowance

Tracked cost is an estimate of what the fleet will be billed, in exact
micro-units, not a provider invoice. Each operation records the Spot price of the
request that was accepted and, on the first reconcile that observes the instance,
the provider's launch time. Its cost is computed per second:

```
cost = ceil(price * billable_seconds / 3600)
     + ceil(incidental_per_hour * billable_seconds / 3600)
billable_seconds = clamp(max(60, end - launch_time), 0, max_lifetime_seconds)
```

`price` is the accepted quote. A row without one — created before this ledger
schema, or whose write was lost — uses `max_spot_price`, which over-states but
never under-states. The 60-second floor is the provider's per-instance minimum.
`incidental_per_hour` covers public IPv4 and EBS and defaults to zero: set it to
the deployment's rates, or leave it zero and track compute only.

Admission does not use accrued cost alone. For every non-terminal operation the
controller commits:

```
commitment = accrued cost
           + ceil(remaining_price * remaining_seconds / 3600)
           + ceil(incidental_per_hour * remaining_seconds / 3600)
           + non_compute_allowance
remaining_seconds = max(0, lifetime_seconds - elapsed)
remaining_price = max(max_spot_price, accepted_price or 0)
lifetime_seconds = max(60, ceil(expires_at - created_at))
```

so a newly admitted worker commits `max_spot_price * max_lifetime +
incidental_per_hour * max_lifetime + non_compute_allowance` — the earlier
reservation plus the incidental term, which is zero unless configured — and that
commitment shrinks every second it lives. The accepted price is a floor on `remaining_price`: lowering
`max_spot_price` below a live operation's accepted price does not shrink that
operation's commitment, because it still settles at the accepted price. The
lifetime comes from the operation's own durable expiry window, so lowering
`max_lifetime_seconds` applies to the next launch and never shortens a worker
that is already admitted. A batch is affordable while the window's `spend` plus the forward
commitment plus one new worker's worst case stays inside `allowance`:

```
affordable = (allowance - committed) // per_launch
```

`spend` is the settled cost of completed operations plus the accrued cost of
operations still running; `committed` is `spend` plus the forward commitment.

An operation settles once, when a reconcile observes its instance `terminated`.
An instance that disappears before the provider retention window settles at a
full configured lifetime at the price the launch was accepted at (the configured
maximum when no price was recorded). That price keeps the settlement inside the
commitment the operation carried while unresolved; settling at the maximum would
charge more than admission reserved. A definite pre-launch rejection settles zero, and a
settled or failed operation is never charged again. A restart does not reset the
window.

The unspent part of a commitment is released as the operation ages and fully
released when it settles, so a one-job worker that exits after ten minutes stops
reserving its full lifetime. That is the only release this accounting makes:
money already spent stays counted, and a completed operation is charged to the
window it started in. An accepted launch stays charged even if the instance
never reaches `running`, and an uncertain outcome keeps its commitment until the
intent is resolved.

Tracked cost does not bound EBS, data transfer, public IPv4 beyond the configured
rate, AMI and snapshot storage, log delivery, minimum billing increments, Spot
price changes after launch, or resources left by failed cleanup. It is an
estimate and not a guaranteed cap on the AWS bill, and no status, lifetime or
alert in this project should be described as one.

### Reconciling with the provider bill

The provider's cost report is the billing reference, not this estimate. Compare
a completed day's final `ci_burst_spend_micros` (the last sample before the
window rolls) with the account's EC2 usage for that day. A difference is
expected from data transfer, storage and price movement; a large or persistent
difference means `incidental_per_hour` or `max_spot_price` does not describe the
deployment. The product carries no billing permission, so the comparison is an
operator read, not a controller call.

### Sizing the window

The window is anchored at a multiple of `window_seconds` from the epoch, not a
trailing period, so the end of one window and the start of the next are two
budgets that can be spent back to back. What carries between windows is the
forward commitment of every non-terminal operation: a worker that started before
the boundary and is still running keeps blocking admission in the new window
until it settles, while its spend stays charged to the window it started in.

The allowance admits roughly `allowance // per_launch` workers **at any
instant**, not per window: as workers settle, their unspent commitment returns
and later launches become affordable. A window can therefore settle far more
than `allowance // per_launch` operations while its `spend` stays under
`allowance`. "Per day" is the same statement only when `window_seconds` is 86400.

"Per dispatch" is not a safe unit either. The controller admits a launch whenever
eligible demand remains and a slot is free, so one dispatched workflow with
several eligible jobs can spend several launches as its one-job workers finish.

Worked example: a `0.1200` maximum price, a one-hour lifetime, a `0.0500`
non-compute allowance and a `0.0050` incidental rate commit `0.1750` per new
worker. A `5.00` window admits 28 workers at once (`28 * 0.175 = 4.90`); if each
lives about ten minutes it settles near `0.025` each, so the window can settle
far more than 28 launches while `spend` stays under `5.00`.

## Fleet size and fan-out

Admission is demand-driven. One tick computes a batch size from three bounds:

```
launches = min(waiting jobs, free slots, affordable workers)
```

- **Waiting jobs** is the eligible demand in the qualified queue observation.
- **Free slots** is `controller.max_instances` minus the non-terminal owned
  operations in the ledger. An intent, starting, running, terminating or
  uncertain worker occupies a slot; a terminal or failed one does not.
- **Affordable workers** is `(allowance - committed) // per_launch` for the
  current anchored window, with `committed` and `per_launch` as defined in
  § Cost tracking and the allowance.

A zero bound is not an error. No free slot waits with `slot_busy`, no demand waits
with `no_runnable_demand`, and no affordable worker refuses with
`budget_allowance_exhausted`. When the allowance allows fewer launches than both
demand and free slots, the batch is capped and the decision carries the
`budget_capped_batch` note. `plan` reports the same `demand`, `free_instances`
and `launch_count` that an `apply` would use.

Each worker in a batch has its own launch intent, ledger operation, frozen
request, client token and reservation. A batch is not one admission with several
instances: every worker can be resumed, reconciled, expired and charged on its
own. Every request still sends `MinCount = MaxCount = 1`.

The launch order rotates the eligible pool list cyclically by the worker index,
inside the first eligible type's block, so a batch starts at successive pools of
that type and wraps: four workers over two zones start A, B, A, B. Rotation
spreads workers across the ordered zones; it is not a capacity guarantee, and a
later instance type is reached only by a definite capacity rejection, never by
batch spread. Each worker starts at the first pool in its rotation whose quote is
still fresh, records the earlier pools as `skipped` (`spot_price_stale`), and
walks its own pool order only on a definite capacity rejection.

The worker contract stays one job per worker. An idle worker waits for one job
until its own maximum lifetime, then shuts down and terminates. Its forward
commitment is released only when it settles, so an idle worker keeps its worst
case committed for as long as it lives, and money already spent is never
refunded. Fan-out therefore trades committed allowance for readiness: N workers
can take N jobs at once, and a worker that never gets a job still costs up to its
configured lifetime.

If any worker in the batch ends in anything other than an accepted launch, the
controller stops the batch and leaves the workers already launched in place. It
does not terminate them to make the batch uniform.

The shared admission lifetime is re-read before each worker is committed. If it
elapsed while an earlier request was in flight, the batch stops with
`admission_expired` and no further operation is created: the decision is
`launched` when a worker already launched, otherwise `refused`. A later worker
with no fresh candidate stops the same way with `spot_price_stale`.

Lowering `controller.max_instances` below the number of live owned workers never
terminates anything. New admission waits with `slot_busy` and the decision
carries the note `above_configured_limit` until enough workers finish on their own
lifetime; expiry cleanup remains the only owner of termination. Observation
reports the same condition and keeps reading the draining workers' consoles, so
the drain stays visible while it lasts.

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
waiting demand, free slots and affordable workers (§ Fleet size and
fan-out). Each worker in the batch is its own admission, with its own frozen
request, ledger operation, client token and reservation; the batch stops as soon
as one worker is not accepted. Within one worker, if the provider definitely
rejects the selected pool for insufficient capacity
(`InsufficientInstanceCapacity`), that rejection proves no instance was created,
so that admission advances to the next eligible pool in order — at most once per
pool — and a success ends the walk. Every attempt sends `MinCount = MaxCount = 1`,
so no fallback and no batch can produce a second instance for one worker. Before
each worker and each advance the controller re-reads the clock: the pool's Spot
quote must still be within `price_max_age_seconds` and the admission lifetime
must not have elapsed. A pool whose evidence expired is recorded as `skipped`
(`spot_price_stale`), not frozen or sent, and the next remaining pool is
considered. The result reports a `launches` array with one entry per admitted
worker (`operation`, `instance_id`, `zone`, `instance_type`, `reservation`,
`decision`, `attempts`). Each entry's `zone` and `instance_type` are the pool
that actually accepted that worker, so a capacity fallback is not reported under
the original choice, and its `attempts` carries that worker's own ordered
outcomes (`capacity_rejected`, `accepted`, `rejected`, `uncertain` or
`skipped`). The
top-level `operation`, `instance_id`, `expires_at` and `reservation` are the
first launch's values, so a one-worker deployment reads the shape it did before;
the top-level `attempts` is the ordered outcome list of the first launch, or of
the step that stopped the batch. `selected` remains the batch's first choice, so
the accepted attempt is the placement that actually launched.

No other failure falls through: a non-capacity definite rejection keeps today's
`launch_failed` behaviour, and an uncertain response keeps the operation
outstanding. If every pool is rejected for capacity the operation is
`failed` — its slot is free again — the queue stays unserved, and the result
carries the stable `capacity_exhausted` reason plus the final provider refusal.
If the remaining pools' price evidence expires during the attempt, the
operation is also `failed` with its slot free and the stable `spot_price_stale`
reason, and no new admission or reservation is created. Nothing is reported as
launched.

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
created nothing. Only a definite rejection of the first attempt may release that
worker's slot, and ambiguous provider errors stay uncertain.

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
an operator resolves them. When more than one launch intent is unresolved,
`apply` retries them in creation order and stops at the first intent whose age
exceeds the client-token window, whose lifetime elapsed, or whose retry is not
accepted. Intents after the deciding one are not sent and retain their existing
ledger state for the next apply.

Every attempted intent produces one `resumes` record in creation order. A
record's `expires_at` and `reservation` always come from the durable operation,
whatever the outcome; `instance_id` is `null` until a launch is confirmed, and
`refusal` appears only for an uncertain retry:

| `decision` | `reasons` | `instance_id` | `refusal` | `expires_at` / `reservation` | `attempts` |
| --- | --- | --- | --- | --- | --- |
| `resumed` | `[]` | confirmed instance | absent | from the operation | `[]` |
| `uncertain` | `[]` | `null` | provider refusal | from the operation | `[]` |
| `refused` | `["unresolved_intent_expired"]` | `null` | absent | from the operation | `[]` |
| `refused` | `["unresolved_intent_lifetime_elapsed"]` | `null` | absent | from the operation | `[]` |

Every record also carries `operation`. The top level carries the deciding
intent's `operation`, `decision`, `expires_at` and `reservation`; its
`instance_id` appears only when that intent was confirmed, its `reasons` only for
the two expiry refusals, and its `refusal` only for an uncertain retry. The top
level carries no `attempts` field, and a resume sends one frozen request, so a
record's `attempts` is always empty.

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
makes `plan` refuse instead of computing free slots from zero consumption. The
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
the anchored window's `allowance`, `spend`, `committed`, `remaining` and
`launches`, the number of `busy_instances`, and the `hourly_ceiling` at that
limit (`(max_spot_price + incidental_per_hour) × max_instances`).

## Supported limits

One repository, one Region and one ordered list of placement pools: the
availability-zone candidate list (the single-zone key is the one-candidate form),
optionally multiplied by an ordered instance-type list of at most four same-shape
types, up to `controller.max_instances` (1..12) owned instances, one writer and
one ledger. Ordered fallback is supported only for a definite insufficient
capacity rejection, within one admission and at most once per configured pool. Not
supported: concurrent controllers, multiple ledgers, warm pools, On-Demand
fallback, automatic zone or Region discovery and substitution, provider
selection, job retries, workflow rewrites, image construction and secret
distribution.

Observation has its own bound. Its per-run console-read budget is independent of
`controller.max_instances`: a supported drain above the ceiling is reported, not
refused, and only more than 16 unfinished non-terminal candidates refuse a run.
