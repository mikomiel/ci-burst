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

Release Please publishes source with lightweight semantic-version tags and
immutable GitHub Releases. Image builds, deployment, and live Forgejo
qualification each have their own approval and evidence. A green validation
run establishes source behavior.

The scaling path never builds an image. An optional image path, off by default,
builds a candidate warm image on request; see
[Automated warm-image build](#automated-warm-image-build). It is a separate
identity and a separate command group, and it never moves the pin that `apply`
launches from.

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
type names), `controller.max_instances` (an integer in `1..12`, default `1`),
`controller.pass_deadline_seconds` (an integer in `30..290`, default `240`),
`controller.claim_grace_seconds` (an integer in `60..1800`, default `300`).
Relative state paths resolve against the configuration file directory. Unknown
keys, floating-point money, a template-version alias, a non-`https` Forgejo URL,
a `controller.max_instances` outside `1..12`, a
`controller.pass_deadline_seconds` outside `30..290`, a
`controller.claim_grace_seconds` outside `60..1800` or not an integer, a
malformed zone-candidate list (`ZONE_CANDIDATES_INVALID`) and a malformed
instance-type list (`INSTANCE_TYPES_INVALID`) are refused.

The optional `[image]` tables configure the automated warm-image build and are
absent by default. [deployment.md](deployment.md#the-image-tables-optional) owns
their keys, ranges and defaults.

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
instance_types = ["m8g.xlarge", "m7g.xlarge"]
max_spot_price = "0.0400"
max_lifetime_seconds = 3600

[[aws.availability_zones]]
zone = "eu-central-1a"
subnet_id = "subnet-0123456789abcdef0"
```

The order is type-major and it is the priority: every zone of `m8g.xlarge`
first, then every zone of `m7g.xlarge`. The example lists the newer generation
first and the older one as the capacity fallback;
[deployment.md](deployment.md#5-write-the-deployment-toml) gives the rule and the
measurement that checks it. A definite capacity rejection walks to
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
mutated), `1` for a provider or cleanup failure. A cleanup pass that refused at
least one owned resource is `cleanup_degraded`, a cleanup failure, and exits
`1`. Every command prints one JSON document with stable reason codes. `plan`,
`status` and `export` perform no cloud mutation and no ledger write; `apply` is
the only worker launch path; `reap` never needs Forgejo or the scaling ledger.
The `image` group has its own commands and the same three exit codes; see
[Image commands](#image-commands). `image run --apply` launches one build
instance, never a worker.

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

**Sharing contract.** Runs that share any export file (the record, the event
file, the metrics textfile or the collector file it is published to) share all
of them, the same deployment name, the same publication mode (the shipped
unit's staged copy, or direct `--metrics-file` into the collector, never both
for one collector file), and the same user; the parent directories stay the
same directories while a run is active. Runs that share no file are
independent. Any other combination is unsupported, and the guarantees below do
not hold for it.

Runs take turns: a run holds an exclusive lock on the directory of each of its
three files, from before it reads the clock, the record or the event file until
its last write, so a manual run waits for the timer's run and the other way
round. The lock is on the directories and leaves no file behind. A user who can
read a directory can hold its lock and delay a run; the shipped unit's
`TimeoutStartSec` then stops it, the record is not refreshed, and the hold fails
open once the record is stale. A run reads the event file on every run, without
blocking and without following a link. Only a missing event file is an empty
stream; a run that cannot read an existing one, or finds a link, a FIFO or a
directory there, exits 1 before it replaces any file.

### The event stream

One JSON object per line:

```json
{"id":"i-0123456789abcdef0:step:register","ts":"2026-11-11T16:55:17Z","deployment":"example","instance":"i-0123456789abcdef0","zone":"eu-central-1a","operation":"...","event":"step","step":"register"}
{"id":"i-0123456789abcdef0:exit:0","ts":"...","deployment":"example","instance":"i-0123456789abcdef0","zone":"eu-central-1a","operation":"...","event":"exit","rc":0}
{"id":"i-0123456789abcdef0:job-process:failed:42","ts":"...","deployment":"example","instance":"i-0123456789abcdef0","zone":"eu-central-1a","operation":"...","event":"job-process","outcome":"failed","rc":42}
{"id":"i-0123456789abcdef0:spot-interruption:terminate:ip-10-0-0-1:0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b","ts":"...","deployment":"example","instance":"i-0123456789abcdef0","zone":"eu-central-1a","operation":"...","event":"spot-interruption","reason":"spot_interruption","action":"terminate","host":"ip-10-0-0-1","runner":"0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"}
```

Every event also carries `instance_type` (for example `"m8g.xlarge"`) when the provider reports the worker's instance type; the key is absent otherwise, and a retained line written before the key existed stays valid.

`event` is `step` (a step name from the shipped script's fixed set), `exit`
(`bootstrap exiting rc=N`), `job-process` (`bootstrap one-job <outcome> rc=N`,
where `outcome` is one of `completed`, `failed` or `timeout`), `spot-interruption`
or `error`, where `kind` is one of `forgejo_ping`, `ssm_timeout`, `apt_failed`,
`register_failed` or `runner_error`. The `job-process` event and the `exit` marker
describe the worker process lifecycle and never the Forgejo job result: a runner
can exit 0 after a job that failed, so no success metric or job verdict is derived
from them and the job result stays with Forgejo. No console text is published: a
failure line contributes its kind only. The console is not a trusted channel, and
truncation is not redaction — read the raw line with the provider CLI when you
need it.

The shipped step names are `mount-options`, `register`, `claim-timeout` and `job`.
`claim-timeout` is the expected end of a worker that took no job. The bootstrap
prints it once, when its claim window passes without a task (see
[runner-setup.md § The claim window](runner-setup.md#the-claim-window)). It is
published as one `step` event with the same identity form as every other step
(`<instance>:step:claim-timeout`), and it carries no console text. No `job` step
and no `job-process` event follow it, because no job attempt happened, and the
`exit` event for that worker carries `rc=0`. The marker is not an `error` kind,
not a registration failure and not a job result. A worker that printed
`register` and then `claim-timeout` counts as registered once and never as
unregistered. The marker does not finish the record: the instance stays a
candidate until the provider reports it `terminated` or `stopped`. A runner can
also exit 0 after a job that failed, so tell an idle worker from a finished one
by this step and the missing `job-process` event, never by `rc=0` alone.

### The Spot interruption marker

The worker's bootstrap reads the two-minute Spot interruption notice and, on the
notice, prints one fixed marker to its console and then tries to end its one
job within a bounded window; the `spot-interruption` event above is that
marker. It carries four validated values:
`reason`, the constant `spot_interruption` that a consumer filters on; `action`,
the notice's own value (`terminate`, `stop`, `hibernate`, or `unknown` when the
notice body has no documented action); `host`, the worker's hostname; and
`runner`, the registration id captured at registration (`null` when the worker
could not capture it). Nothing else from the line is copied.

`host` is the value the job's own `forgejo-ci-attribution` line records as
`host=`, so a consumer that has that line can join the marker to a run and a job
without a provider call. `runner` is the registration id, so the same join can be
made from a runner registration when attribution is missing; it is not a job
identity, because a Forgejo job record carries no runner field. The marker is
evidence that the notice was observed and that a signal delivery was attempted;
it is not proof that the worker or the job ended, and it is never a job
result. Like
every line in this file it comes from the console, which no party is trusted to
write, so treat a marker as one input among the job's, not as proof on its own.

The notice is read by the bootstrap as root because the job account is denied the
metadata service, and the watcher starts after registration, so a notice during
registration is not observed. A worker that cannot read the notice behaves as
before: the instance lifetime and the job timeout still bound it, and no marker
is written. The watcher prints the marker once and then keeps trying to signal
the runner for up to two minutes, because it starts before the job does.

The file is replaced atomically on each run rather than appended to in place, so
a collector must open it by path and must not hold a descriptor across runs; that
rewrite is also what makes rotation and the size bound possible.

Delivery is at least once. `id` is derived from the instance and the marker, so
a repeat after a crash between publishing an event and saving the dedupe record
carries the same identity and a consumer can drop it. `export` publishes the
events, then saves the dedupe record before it replaces the textfile. A run that
cannot write the textfile keeps the record that it saved, so the next run does
not emit those events again. The dedupe record keeps what was
already emitted and is pruned after seven days; deleting it loses the history and
the cumulative counters. It can change a decision in one way only: `apply` and
`plan` read the record as a brake after repeated futile launches (§ Futile launch
hold).

### The metrics textfile

Every series carries the deployment label. `ci_burst_worker_launches_total`,
`ci_burst_worker_registered_total` and `ci_burst_worker_unregistered_total` are
cumulative counters kept separately from the expiring per-instance records, so
they do not fall when an instance ages out of the provider's record window or
when a record is pruned. `export` saves the record, which holds the totals,
before it replaces the textfile, so a textfile never carries a total that the
record lacks, and that holds for a collector that reads `--metrics-file` itself
as well as for the shipped unit, for runs that keep the sharing contract (§ Worker
evidence). Only a lost, damaged or unreadable dedupe
record (deleted, or replaced by `export` because it cannot read or load it)
restarts a counter. `export` reads the record once per run, without blocking and
without following a link, and treats a read that fails, even for a moment, a
link or a FIFO as a damaged record, which it replaces.
After such a reset the textfile of an earlier run can hold higher totals until a
run replaces it.
`ci_burst_worker_unregistered_total` counts workers
that ended without ever showing the registration marker; evidence observed once
is never un-learned by a later empty console. `ci_burst_spend_micros`, `ci_burst_committed_micros`,
`ci_burst_allowance_micros` and `ci_burst_remaining_micros` come from the
read-only ledger, are gauges in micro-units, and are omitted — with a
`ledger_omitted` note in the JSON result — when it cannot be read.
`ci_burst_export_last_run_timestamp_seconds` is what a consumer uses to detect a
stopped exporter, and a consumer sees a new value only after the textfile reaches
the collector. `export` saves the dedupe record, then replaces the textfile at
`--metrics-file`, then advances the record's modification time, which is the
heartbeat that `apply` reads (§ Futile launch hold). A run that cannot save the
record exits 1 before it replaces the textfile. A run that cannot replace the
textfile keeps the record that it saved, under the modification time of the
record that it read, or the epoch when it read none, and exits 1. Either way the
record is not refreshed, so it ages, and the failed unit shows it. In the shipped unit,
`--metrics-file` is in the state directory and `ExecStartPost` publishes the
textfile into the collector's directory. systemd runs `ExecStartPost` only after
`export` exits 0, so a failed run publishes nothing: the collector's timestamp
does not advance. A unit that fails at the publication has already advanced the
record's modification time, and the collector's timestamp does not advance
either. Where `--metrics-file` names the collector's own file (direct mode, as in
the commands above), the collector reads each textfile as soon as `export`
replaces it, and a textfile that it has read never carries a total that a later
run lowers: the record already holds it. A run that read its record and then
stops after it replaced the textfile, or cannot replace it, leaves the record
level with the textfile or ahead of it, under the modification time that it read,
and a later run writes totals that are not lower, even when an instance has left
the provider's inventory in between. A run that could not read its record
restarts the counters, as for a deleted record (see above), and saves the new
record under the epoch.

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

The table above bounds one export run. The controller has a separate bound:
`controller.pass_deadline_seconds` (30..290, default 240) caps one `apply`
pass, including its provider reads. A pass that reaches it returns `refused`
with `pass_deadline_exceeded` and a `phases_deferred` list, with headroom below
the unit's `TimeoutStartSec`; a read already in flight is allowed to return and
the next check reports the overrun. `reap`, `status` and `export` do not take
it. § Scheduler, cleanup and rollback owns the full relation.

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
dropped rather than kept forever, and so is a record dated after the clock of the
run (see the paragraph on dates below);
a ledger state outside the ledger's own vocabulary is counted as `invalid`
(`ledger_state_unknown`). None of those is a failure: derived evidence that
cannot be validated is discarded, not published.

The same rule covers the decoding of the record. `export` refuses a record file
that holds `NaN`, `Infinity` or `-Infinity`, or in which a JSON object names a
member twice, at any level. It writes neither. It drops such a file whole and
notes `state_rebuilt`, so the emitted history and the cumulative counters start
again, as when the record is deleted, and the run reads again the consoles that
it selects within its read budget. The launch hold reads the same file with the
same two refusals (§ Futile launch hold), so `export` carries no record over from
a file that the hold would call `malformed` for either reason. A loader that kept
the last value of a repeat could keep a futile record in place of a registered
one, skip its finished console and rewrite the file with a fresh heartbeat.

The same rule covers a stored value that bears on registration. The hold decides
`registered` or `futile` from the `registered` flag, the `steps` list,
`job_process` and `spot_interruption`. When one of them is present and `export`
cannot keep it, `export` drops the whole record of that instance and notes
`state_rebuilt`. Examples are a `job_process` whose outcome and status disagree,
a `spot_interruption` of a shape that `export` does not write, a `steps` list
with an element that is not text, and a `registered` flag that is not true or
false. `export` does not keep the record with the value replaced by its default.
A worker that has ended is not read again, so the kept record would show no
registration, and the next run would write it back under a fresh heartbeat as a
futile launch that nobody observed.

A key that is absent is the same damage. The hold refuses a record file in
which a record lacks one of those four keys. A record file that an older
version wrote lacks the keys that the version did not write yet. When the
record of a worker that has ended lacks one of them, `export` drops that
record and notes `state_rebuilt`, for the reason above. The first run that
loads such a file drops the records of the workers that have ended, and reads
again, within its read budget, the consoles of those that the provider still
lists. The record of a
worker that has not ended may lack a key. While the provider lists the
instance, the run reads its console again within its budget, and the console
fills the key. Until then the hold counts no such worker.

The same rule covers a date. The hold refuses a record file in which one record
is dated after the controller clock. A clock that was set back leaves such a
record, and a worker that has ended is not read again, so the record would stay.
Every file that `export` wrote from then on would be `malformed` for the hold
until the clock reached that date. `export` therefore compares each record with
the clock of its own run when it loads the record. It drops a record dated after
that clock, whether its worker has ended or not, and notes `state_rebuilt`. A
record dated at the clock stays. `export` never gives a record a new date: only a
read of the console dates a record, and the date is the time of that read. A
clock set back by more than the age of the newest records leaves every record of
that span dated ahead, so one run drops them all, the records of live workers
with them.

A dropped record has two outcomes. While the provider still lists the instance,
the run reads its console again within the read budget, and the record shows what
the console shows now. When the provider no longer lists the instance, the run
writes no record for it, so it is no evidence. The cost is the cost of any
dropped record: the cumulative counters count that worker again, and the run
emits its markers again under their stable identities. A worker that still runs
also loses what earlier reads of its console found, because the record shows
only the console of the new read.

`export` keeps a record when it can keep the evidence in it. A Spot notice whose
registration identifier it normalises, and a `steps` list from which it removes a
name outside the vocabulary, keep the evidence that they carry. A bad `state`,
`operation` or `exit_code` becomes null, and the record stays. The hold reads that
value as no evidence for the record, so the repaired record cannot count as a
futile launch.

The command never calls Forgejo and never mutates a resource, so it keeps working
when the queue or the admission path is the thing that is broken.

### Fleet snapshot

`apply` also records one bounded registration snapshot per pass. It reads
`GET /api/v1/repos/{owner}/{repo}/actions/runners` with the same owner-scoped
token as the queue and reports `running_attempts` (running jobs whose labels the
worker supports, from the same queue response), `active_registrations` and
`offline_registrations` (ephemeral registrations only), and
`idle_registrations = max(0, active - running)`.

The two endpoints are read separately and are not atomic: the snapshot can show
more running attempts than active registrations, and the result then carries a
note rather than a negative estimate. The registrations are the repository's,
not this deployment's: a persistent runner on the same repository is excluded
from the ephemeral counts, while the queue counts still include every job with
the supported labels. Nothing here links a registration to a job attempt or
claims an assignment; the job result remains Forgejo's.

The controller writes the counts to the ledger (`fleet_observations`, added in schema 4,
one row per successful pass, pruned after seven days) and keeps the previous
pass when the read fails. It reads the snapshot only after the admission
decision is fixed, and after the launch when the pass launches; it skips the
read when the remaining pass budget is below the configured Forgejo timeout.
Either case reports `fleet.available: false` and never changes the decision. `status` prints the
newest row. `export` emits `ci_burst_fleet_active_registrations`,
`ci_burst_fleet_running_attempts` and `ci_burst_fleet_idle_registrations` as
gauges, plus `ci_burst_fleet_observation_timestamp_seconds`; a snapshot older
than fifteen minutes is not published as current (`fleet_stale` in the result)
and only its timestamp remains, so an alert can fire on staleness itself.


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

The `labels` query is mandatory, not an optimisation: the server applies no
label filter when it is absent and admits every waiting or running job in the
repository. The adapter sends the worker's bare label names (the part before the
executor suffix, so `strong-ci` for `strong-ci:host`) and reads `null` as an
empty queue, because the server marshals an empty selection — a filter that
matches nothing, or no waiting or running job at all — as `null`. The filter is
a union prefilter, so a job selected by one shared label is still compared
against its complete `runs_on` set above; it never substitutes for that
comparison.

Attempts are deduplicated by `handle`. A retry is a new handle and remains
distinguishable. ci-burst never assigns, requeues or reserves a job, and it does
not promise exclusivity merely because it observed a record. Untrusted response
values (status, handle) are never echoed in refusal details; an echoed handle
must match a bounded identifier shape or is reported as `<redacted>`.

### Qualifying live support

`qualification = "fixtures-only"` is the shipped default and blocks `apply`.
To qualify a deployment, the operator must, with an owner-scoped read token:

1. confirm the endpoint returns the shape above for the repository when the
   adapter's explicit `labels` filter is sent — this qualification does not use
   an unfiltered request;
2. observe one known waiting job and one running job, and verify the demand
   classification. Dependency-blocked work cannot be part of this evidence: the
   endpoint does not list it (see § Queue contract);
3. record the evidence with the deployment;
4. only then set `qualification = "live-verified"`.

Observed on `forgejo 17.0.1+gitea-1.22.0`: `runs_on` holds bare label names
(`strong-ci`, `homelab`), a non-matching filter returns `null`, and the filter
is a union over the job's labels. Statuses observed in one run
(`mikolajm/ai-agents` run 25336, 2026-11-10): a job waiting for a runner reports
`waiting` and still lists the dependency that already succeeded, while a
dependent job reports `blocked` until it does. A changed response shape or a
different Forgejo version needs a new contract identifier, not a
re-declaration; correcting how the adapter reads an unchanged shape does not.

#### Replayable contract reading

`tests/fixtures/forgejo/contract.json` is the versioned contract reading. It
names the pinned upstream project and tag (`forgejo` `v17.0.1`), the handler
(`GetActionRunJobs` in `routers/api/v1/shared/runners.go`), the response record,
the request variant behind every fixture in `tests/fixtures/forgejo/`, and the
demand, counters, notes or refusal the parser must produce for it. Each fixture
is marked as a shape the pinned handler can emit or as a defensive shape that
only exercises the parser. The manifest states that it is a reading of the
source, not a live observation: no server, request, token or credential produced
it. The reading shows that the handler selects only `waiting` and `running`
jobs, and that an absent `labels` parameter applies no filter at all, so the
response can list every waiting and running job in the repository rather than
`null`.

The manifest also records the route's authorization: the repository-scoped route
is registered with `reqOwner`, which admits the repository owner or a site
administrator and otherwise answers `403 user should be the owner of the repo`;
an account that can merely read the repository is refused.

`tests/test_forgejo.py` replays every fixture through the real observer and
compares the result with the manifest. The matrix therefore proves the parser's
classification against the pinned contract — including shapes the endpoint does
not emit — and that the adapter always sends an explicit, non-empty `labels`
parameter. It proves nothing about a running server.

Only an isolated operator canary can establish live compatibility: a
repository-scoped ephemeral runner registers, takes exactly one job to
completion and exits, and an idle worker ends at its claim deadline without
taking a job. The canary must record the exact Forgejo server version and the
exact runner version it ran. This change ran no server and no runner, and the
matrix does not set `qualification = "live-verified"`.

Requalification is required when either recorded version changes, or when a
server returns a response shape no fixture pins. A changed shape or a different
Forgejo version needs a new contract identifier and a new manifest reading;
passing the matrix does not re-qualify a server the operator has not run.

## Worker image and launch template

The image and template are operator-owned prerequisites. The scaling path
launches a pinned template; it does not build images, configure networks or
distribute registration credentials. You build the warm image by hand
([warm-image.md](warm-image.md#manual-runbook)) or, when the optional `[image]`
tables are enabled, with the [`image` commands](#image-commands) of the
[automated build](#automated-warm-image-build). The automated build ends in an
inert candidate template version; only `image promote --write` moves the pin. A
complete setup guide, with the IAM role, SSM SecureString parameter, network
choices and bootstrap, is [runner-setup.md](runner-setup.md). The controller
refuses a template that is not usable:

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
  no job arrives within the bounded claim window
  ([runner-setup.md § The claim window](runner-setup.md#the-claim-window)),
  followed by instance shutdown and termination;
- disposable volumes with delete-on-termination;
- no controller, provisioning or AWS credential on the worker;
- registration credentials acquired through an operator-owned secure channel.
  Inline user data, image layers and logs are not accepted channels, and
  ci-burst never generates or forwards that secret. The pinned runner CLI
  accepts its token only as an argument, so the published bootstrap reads it
  from a `0600` file and shreds that file before the job starts; the exposure
  this leaves to host root and exec auditing, and its bounds, are in
  [runner-setup.md § Threat model](runner-setup.md#threat-model);
- a maximum lifetime that covers image boot, the claim window, one job and a
  failed bootstrap. Ephemeral registration is not an expiry mechanism;
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

## Automated warm-image build

The `image` command group builds a candidate warm image from the head of one
protected branch and records it as an inert launch-template version. It is
opt-in. With `[image]` absent or `enabled = false`, every `image` command prints
`{"decision": "disabled"}` before it opens a state file, builds a client or
reads the network. The scaling path never calls it, and nothing in it moves the
pin that `apply` launches from: a person does that with `image promote --write`.

Each topic has one owner:

| Topic | Owner |
| --- | --- |
| the `[image]` tables, the image role profile, the image token and the dedicated TOML directory | [deployment.md](deployment.md) |
| the build workflow, the gate and the manual runbook | [warm-image.md](warm-image.md) |
| the one-off container line of each command | [container.md](container.md#run-the-image-commands) |
| the commands, exit codes, the `blocked` state and the reason codes of each step | this section |
| the permissions of the image role | [deploy/iam/image-build-policy.json](../deploy/iam/image-build-policy.json) |
| the preparation record before the first real build, which is not authorization | [pilot-checklist.md](pilot-checklist.md#9-automated-warm-image-build-optional) |

Run the commands on the scaler host. The reaper host holds no image credential
and takes no part. Nothing in this repository schedules an image build: you start
one, or you schedule it yourself.

### Image commands

```sh
python -m ci_burst image plan    --config deployment.toml   # read-only: would a build start?
python -m ci_burst image run     --config deployment.toml   # the same read-only decision
python -m ci_burst image run     --config deployment.toml --apply [--until-done] [--force]
python -m ci_burst image status  --config deployment.toml [--refresh]
python -m ci_burst image promote --config deployment.toml [--version N | --rollback] [--write]
```

| Command | Reads | Writes |
| --- | --- | --- |
| `image plan`, `image run` | the forge with the image token, the provider with the image role (describe calls only), the state file read-only | nothing; it never creates the state file |
| `image run --apply` | the same | the state file, the event file and the metrics file; at the provider one builder, its archive, one image, one launch-template version and the deletions of the deletion rule |
| `image status` | local files only | the check time, in a state file that exists |
| `image status --refresh` | local files, and describe calls with the image role | the verification evidence in the state file |
| `image promote` | local files only | nothing |
| `image promote --write` | local files only | one key of the TOML, its backup, its lock file, the state file and one event |

`image plan` says whether a build would start and, if not, which condition
stopped it. It also prints the 12-character source sha and basis digest, the image
behind the pin, the result of the build-template checks, the size of the user data
against its limit and what the deletion rule would remove. `image run` without
`--apply` prints the same decision and applies `--force` to it, so it shows what a
forced run would decide.

`image run --apply` runs one pass under the writer lock of the state file
(`IMAGE_STATE_LOCKED` while another pass holds it). The pass has a deadline of 240
seconds, which parts of the pass check: the provider reads, the archive stream and
its upload, and the polling of the builder, which stops 30 seconds before the
deadline so that retention can still run. It is not a wall-clock limit. The read of
the branch and the reads of the basis files (one request each, up to 32 files) are
not interrupted by it: a request that has begun runs to its end, but no further
basis read starts once the deadline has passed. The describe calls that record the
pin evidence after the pass get no deadline. For the forge reads, `forgejo.timeout_seconds` bounds a silent server on
each request, not a slow one. Each provider call keeps the client's own timeouts
(10 seconds to connect, 30 seconds to read, one attempt). A pass can therefore
outlast 240 seconds: if you need a hard bound, set a timeout in the scheduler that
runs the pass. A build lasts longer than a pass, so every pass resumes the build
the state file names. A pass reconciles first, then starts a build when one is due
or advances the active one (`building`, then `imaging`, then `candidate` or
`failed`), then runs retention. `--until-done` repeats passes, 30 seconds apart, until the build is `candidate` or
`failed` or a pass has nothing in flight (a refusal, an unchanged digest). It needs
`--apply` and refuses without it (`IMAGE_UNTIL_DONE_NEEDS_APPLY`, before the
configuration is read). A pass that raises ends the loop. The exit status does not
tell whether a build is still in flight: a pass can commit a result and then raise,
so read `image status` before the next run. `--force` counts for the first pass only.

An applying run first resolves the pending promotion row that a crashed
`promote --write` left, from the hash of the TOML, and after its pass it records
what the provider says about the live pin and the rollback target. The result
carries both as `journal_resolved` and `pin_evidence`.

`image status` prints `decision: status` with the active and last build, the
candidate and its evidence, the pin and any zone pins, the rollback target and why
it is or is not available, a local report of protected images built from the
state file and the TOML, the counters
(`consecutive_failures`, `builds_last_day`, `cooldown_until`, `last_check_at`), the
`blocked` explanation and any pending promotion row. The protected report is not
the deletion rule: it omits the new pin of the newest completed journal row and
the pins of settled rows since it, which retention protects, and it can list a
candidate that a refresh recorded as `TEMPLATE_DRIFT`, which retention does not
protect for being the candidate (a pin that names its image still protects it). The `deletion` part of `image plan` gives the current dry-run deletion
decision. Plain `status` makes no provider call. It records the check time
best-effort and reports `check_recorded: false` when no state file exists, a pass
holds the writer lock, or the write fails with an OS or database error.
`counters.last_check_at` shows the time from before this call. `status --refresh` makes describe calls for the live pin, the
rollback target and the newest verified candidates, and records whether each
version and image still exists and whether the candidate version differs from its
source in any field but `ImageId`. A promotion decides from that record, so run
the refresh before you promote. It prints `decision: refreshed` and lists the
versions it recorded and any that no longer exist.

Exit codes follow the other commands:

| Exit | Meaning |
| --- | --- |
| `0` | a completed decision: `disabled`, `digest_unchanged`, `start` (a plan), `building`, `imaging`, `candidate`, `status`, `refreshed`, `preview`, `promoted`, `rolled_back` |
| `2` | a refusal: a raised refusal, printed as `{"error": ...}`, or a result with `decision: refused`. It names what the run declined; it does not say that the run changed nothing (see below). An option error, such as `--version` together with `--rollback`, also exits `2` and prints usage on standard error |
| `1` | a build that ended `failed`, a provider failure (a provider refusal that escapes an image command), or `UNEXPECTED` |

Exit `0` from `image run --apply` does not mean a build finished: `building` and
`imaging` also exit `0`. Read `decision`.

A refusal prevents what its reason code names: the start of a build, a promotion,
a write. It does not undo what the run did before it declined. An applying pass
reconciles first, so before it refuses a start (`IMAGE_MIN_INTERVAL`,
`IMAGE_DAILY_CAP` or another answer of [When a build starts](#when-a-build-starts))
it can have ended a leftover builder, deleted a stray archive, run retention and
written the state file, the event file and the metrics textfile. When the pass
returns its refusal as a result, the run also records the pin evidence; the
`reconcile`, `retention` and `pin_evidence` of the result say what it did. A refusal
that a pass raises can follow the same reconcile. On the promotion path,
`PROMOTE_WRITE_FAILED` can follow the rename of the TOML
([Promote reason codes](#promote-reason-codes)).

### When a build starts

A pass decides in this order, and the first check that fails answers. Clearing
comes first: `--force` clears `blocked` and any recorded failure cooldown, and a
basis digest that differs from the last failed build's clears `blocked` and the
cooldown with it.

| Order | Check | Answer when it stops the build | `--force` |
| --- | --- | --- | --- |
| 1 | the basis digest equals the digest tag of the image behind the pin | `digest_unchanged` (an answer, exit `0`) | skipped |
| 2 | the newest build row of any state, a refused one included, started less than `min_build_interval_seconds` ago | `refused`, `IMAGE_MIN_INTERVAL` | skipped |
| 3 | `max_builds_per_day` build rows of any state started in the last 24 hours | `refused`, `IMAGE_DAILY_CAP` | not skipped |
| 4 | a failure cooldown is running | `refused`, `IMAGE_FAILURE_COOLDOWN` | cleared first |
| 5 | the state is `blocked` | `refused`, `IMAGE_BLOCKED` | cleared first |
| 6 | a builder exists that reconcile ended in this pass; in `image plan` and a run without `--apply`, also an active build or builder (an applying pass with an active build resumes it and answers its state) | `refused`, `IMAGE_BUILD_ALREADY_ACTIVE` | not skipped |

Two refusals belong to the start itself and count no failure:
`IMAGE_CREDENTIAL_SHORT` (the role session would not outlive the archive URL, or its expiry is unknown, see
[deployment.md](deployment.md#image-build-role-and-source-identity-optional)) and
`IMAGE_BUILD_ALREADY_ACTIVE`. A pass checks both before it writes the build row
and again just before the launch. A wrong input is raised, not answered, and
nothing is launched: the gate (`IMAGE_GATE_REQUIRED`), the forge on the branch or
basis reads (`IMAGE_SOURCE_FORBIDDEN`, `IMAGE_BRANCH_UNPROTECTED`, `IMAGE_BASIS_PATH_MISSING`)
or the build launch template (`IMAGE_BUILD_TEMPLATE_UNSAFE`). The gate is checked
before the template, so an unsafe template never hides a missing gate. A stage done
again is the exception for its input rechecks: a changed digest, a missing gate or
opt-out, and an unsafe template verdict end its row and are answered `refused`
(below), while a refused basis-file read is raised and leaves the row. Once those
rechecks pass, the stage runs the same staging and launch checks as a first stage:
a start refusal such as `IMAGE_CREDENTIAL_SHORT` ends the row `refused` without a
failure, and a definite refusal of the archive request ends it as a failed build,
counted, with exit `1`. A pass that
finds a build in flight runs and settles it even without a gate; a launch that
pass would then make is refused with `IMAGE_GATE_REQUIRED`, answered `refused`,
and counts no failure.

A pass deadline can end a pass after the build row is written and before the
builder is launched. The next pass stages that build again. Past the build deadline
it ends the build `failed` with `CONSOLE_BLIND` instead, a counted failure; before
it, the pass first checks its inputs: the basis files at the row's own commit, the warm and gate commands,
`strip_components` and the script must still give the row's digest. A different
digest ends the row `failed` with `IMAGE_INPUTS_CHANGED`. The gate command is in
the digest, so removing a configured gate ends the row this way too. A file that
lost the opt-out of a row inserted under `allow_no_gate` keeps the digest and ends
the row with `IMAGE_GATE_REQUIRED`; an unsafe build template then ends it with
`IMAGE_BUILD_TEMPLATE_UNSAFE`. Each is answered `refused`, counts no failure, writes
no `image_build_failed` event and tries to delete the row's archive. A refused read of a basis file, a `404` included, ends
the pass and leaves the row for the next pass: the commit cannot have lost the
file, so a `404` there can be an unavailable route. A pass past the build deadline
that reaches its deadline check ends the row instead. `lifetime_seconds` and `snapshot_seconds` are not in the digest: a
stage that launches renders them from the file as it reads it then.

A build that has no verdict by `build_deadline_seconds` ends `failed` in a pass that
reaches the deadline check. The deadline is not a hard bound: a pass that raises
first, in reconcile or on a console read refused before the deadline, leaves the
build in flight, and an `imaging` build whose image a later read finds `available` can still
become a candidate. A failed
build sets a cooldown of `failure_cooldown_seconds`. After
`max_consecutive_failures` failed builds in a row the state is `blocked`. A
refusal is not a failure and does not count. Only a build that reaches `candidate`
resets the count.

#### The blocked state

`blocked` means the image path stopped itself after repeated failures. It is not
an outage: `apply` and the pin are untouched. While it lasts, the metric
`ci_burst_image_blocked` is `1`, and a pass that reaches check 5 of the table above
answers `refused` with `IMAGE_BLOCKED`. The checks before it answer first: an
unchanged digest answers `digest_unchanged`, and the minimum interval, the daily cap
and a running failure cooldown answer `IMAGE_MIN_INTERVAL`, `IMAGE_DAILY_CAP` and
`IMAGE_FAILURE_COOLDOWN`. A blocked state therefore does not always show as
`IMAGE_BLOCKED` in a pass: read `ci_burst_image_blocked` or `image status`. The
event `image_blocked` is written once, by the failure that set it.

1. Find the cause. The `image_build_failed` events name the reason, and a
   `WARM_FAILED` event carries a framed tail of the builder's output.
2. Fix the cause: the warm commands, the gate, the build template, the forge
   token, the image role.
3. Clear it in one of two ways. Change the repository so that the basis digest
   differs from the last failed build's, then run `image run --apply
   --until-done`; or run `image run --apply --until-done --force` without a
   change. `image plan` and `image run` without `--apply` show the clearing
   (`clear_control`) but persist nothing.

Clearing does not reset the failure count. The first failure after a recovery
blocks the state again, so a recovered build has one attempt.

### Candidate and promotion

A build that works ends as a candidate: an image tagged for this deployment, and a
new version of the worker launch template (`aws.launch_template_name`) that copies
the version the TOML pins now and changes `ImageId` only. The pass reads both
versions back and compares the whole `LaunchTemplateData`. A difference in any
field but `ImageId` ends the build `failed` with `TEMPLATE_DRIFT`; the event names
the fields and never a value. The new version is inert. It is not the default
version, and no worker launches from it until a person promotes it. The builder
starts from the cold base image that the build template names and never from the
warm image, so a warm image is never built on a warm image.

A build reaches `candidate` only after the builder printed its ready marker, and
the builder prints it only after every warm command and the gate command, when one
is configured, exited `0`. The script runs the gate on the same network as the warm
commands and tests only its exit status. A pass shows that the cache is enough only
if the gate command itself fails on a download;
[warm-image.md](warm-image.md#the-candidate-and-what-the-gate-proves) owns what the
gate does and does not prove. The build records at launch whether a gate was
configured, and only that record counts: a gate added or removed after the launch
does not change the candidate. A change to the warm or gate commands while a build
waits for its stage ends that build as `IMAGE_INPUTS_CHANGED` instead of launching
it under other inputs. A candidate
does not prove that the image is safe, that the packages in it are trustworthy, or
that your jobs pass on it. Read the evidence, run a canary
([warm-image.md](warm-image.md#canary-and-cutover)) and decide.

1. `image status --refresh` records the evidence that a promotion needs: the image
   is `available`, the version exists, the drift is `clean`, and the record is
   fresh. A promotion refuses a record older than `promote_max_age_seconds`
   (`IMAGE_VERIFICATION_STALE`).
2. `image promote --config deployment.toml` prints `decision: preview`: the
   candidate evidence, the pin assignment that would change (`pin_change`: the line
   number and the assignment before and after, without a trailing comment), the pin it
   would replace, any pending promotion row and a `write_check`. The `write_check`
   runs the checks of `--write` in order and reports `ok` or the first refusal.
   Without a state file the command refuses (`PROMOTE_NO_CANDIDATE`). Add
   `--version N` to choose the candidate that owns template version `N`.
3. `image promote --config deployment.toml --version N --write` edits the pin. Use
   the same `--version N` for the preview and the write, and canary the image of
   that version ([warm-image.md](warm-image.md#canary-and-cutover)): without it, the
   write selects the newest verified candidate again, which a pass that finished in
   between can have changed. From the final preview through the write, pause any
   scheduler of image runs and let no `image run --apply` of any kind run, so that
   no new candidate appears and retention does not prune the chosen image. Finish
   or stop a running `image run --apply --until-done` container first, and start a
   fresh one afterwards ([container.md](container.md#run-the-image-commands)): a running
   build container loaded the pin when it started, and its later passes would mix
   that pin with the new file. The same holds for a rollback and for a hand edit of
   the pin. The command takes the lock file of the TOML and the writer lock of the
   state file, verifies the candidate against the record, replaces the one
   `launch_template_version` assignment in `[aws]`, and requires the strict loader
   to accept the new text with that key as the only difference. It then writes a
   temporary file and a backup named `<toml>.bak.<UTC stamp>`, commits the journal
   row, replaces the file atomically and marks the row complete. One
   `image_promoted` event follows. The command keeps the file mode of the TOML.
   Changing its owner is best-effort. A `[[aws.availability_zones]]` entry with its
   own `launch_template_version` is refused (`PROMOTE_ZONE_PIN`).
4. The next `apply` loads the new pin and uses it for new admissions. An `apply`
   that is already running keeps the pin it loaded. A launch intent that was recorded before the edit is retried with its original request, so it can still launch on the old image. Watch the first workers in the
   worker evidence above before you treat the image as live.

`promote` is local. It makes no AWS or forge call, builds no client and holds no
credential, so its container has no network and no credential mount
([container.md](container.md#run-the-image-commands)). A pin that you edit by hand
bypasses all of these checks, including the drift check, and takes no lock: pause
any scheduler of image runs and wait until no `image run --apply` is running
before you make one ([container.md](container.md#run-the-image-commands)). The next
`image status --refresh` records the pin so that it can serve as a rollback
target, but it does not verify what you wrote.

A crashed `promote --write` leaves a pending journal row. The next
`promote --write`, `promote --rollback --write` or `image run --apply` resolves it
from the hash of the TOML: the old hash means the rename did not happen
(`aborted`), the new hash means it did (`complete`). A hash that matches neither
refuses with `PROMOTION_JOURNAL_DIVERGED` and changes nothing.

These commands read the TOML through a mount of its directory. A container that
mounted the file itself keeps the inode it mounted, and a promotion elsewhere
replaces the file by a rename: that container then reads the old file. Each read
checks, after the bytes are in, that the open file still has a name, and refuses
with `PROMOTE_FILE_STALE` when it has none, so no journal row is resolved and no
pin is written from a stale view. A replaced file that another hard link still
names is not detected: do not keep hard links to the deployment TOML.

#### Promote reason codes

A refusal names the item and the recorded value, never a secret.
`PROMOTE_DIRECTORY_NOT_DEDICATED` also lists up to five of the foreign entry names
in the directory, so that you can move them. With `--write` every refusal is
raised and exits `2`. Without `--write`, the checks that
depend on the record are planned against the snapshot the preview read and
reported in `write_check`. That is not a promise that the write will succeed, and
a preview can still be refused: when it has nothing to show
(`PROMOTE_NO_CANDIDATE`, `PROMOTE_VERSION_NOT_CANDIDATE`, `ROLLBACK_NO_PROMOTION`),
when the TOML changes while it reads (`PROMOTE_FILE_CHANGED`, `PROMOTE_STATE_CHANGED`), or before the
plan when the file is a stale view or the journal cannot be read.

| Code | Meaning | What to do |
| --- | --- | --- |
| `PROMOTE_NO_CANDIDATE` | no verified candidate is recorded, or no state file exists | run a build, then `image status` |
| `PROMOTE_VERSION_NOT_CANDIDATE` | `--version` names a version that no candidate row owns, such as a pin set by hand | choose a version from `image status`, or edit the pin by hand |
| `PROMOTE_IMAGE_NOT_AVAILABLE` | the record shows the image as not `available` or as `missing` | `image status --refresh`; rebuild if the image is gone |
| `PROMOTE_VERSION_NOT_RECORDED` | the record does not show the launch-template version as existing | `image status --refresh`; rebuild if the version is gone |
| `PROMOTE_DRIFT_NOT_CLEAN` | the record shows a drift result other than `clean` | do not promote this candidate; find what differs, then rebuild |
| `PROMOTE_GATE_NOT_PASSED` | the build did not pass a configured gate, or it ran with no gate and `allow_no_gate` is `false` | configure a gate and rebuild, or set `allow_no_gate` on purpose |
| `IMAGE_VERIFICATION_STALE` | the record has no verification time, lies in the future, or is older than `promote_max_age_seconds` | `image status --refresh`, then promote again |
| `PIN_MOVED` | the TOML pins a version other than the one this candidate was copied from | run a new build; its candidate is copied from the current pin |
| `PROMOTE_ZONE_PIN` | a zone entry carries its own `launch_template_version` | edit the file by hand and keep every pin consistent |
| `PROMOTE_PIN_LINE_AMBIGUOUS` | the edit did not find exactly one plain `launch_template_version = <digits>` line inside `[aws]` | edit the file by hand |
| `PROMOTE_REPARSE_FAILED` | the strict loader refused the new text, or it differs from the old in more than the pin | edit the file by hand; nothing was written |
| `PROMOTE_FILE_CHANGED` | the file changed after it was read, or the path leads to another file than the one checked and locked | run the command again; nothing was written |
| `PROMOTE_STATE_CHANGED` | the file names another `[image].state_directory` or `deployment` than the one the command opened at its start; the values are compared as written, and a link on the path is not followed | run the command again; nothing was written. Change `[image].state_directory`, or what a link on its path names, only while no image command runs |
| `PROMOTE_DIRECTORY_NOT_DEDICATED` | the directory holds more than the TOML, its `.bak.*` backups, its lock file and its promote temporary file, or it cannot be listed | give the TOML a dedicated directory ([deployment.md](deployment.md#the-dedicated-toml-directory)) |
| `PROMOTE_BUSY` | another promotion holds the lock file; it is never waited for | wait, then run the command again |
| `PROMOTE_WRITE_FAILED` | a local file step failed; the detail names the errno and no path | fix the directory (space, mode, owner) and run the command again |
| `PROMOTION_JOURNAL_DIVERGED` | the TOML matches neither hash of a pending journal row | stop scaling `apply` and every image run, and leave the TOML and the state file as they are. There is no dedicated resolve-only command, and a pass from a restored backup can act on the wrong state or images ([#205](https://github.com/mikomiel/ci-burst/issues/205)). Do not resolve it with `image run --apply`; keep the image path disabled until #205 gives a local resolve-only path |
| `PROMOTE_FILE_STALE` | the open TOML has no name left: the container mounted the file itself and a rename elsewhere replaced it | mount the directory of the file, not the file, and run again |
| `PIN_RECORD_DIVERGED` | the pin or the backup no longer matches what the newest completed promotion recorded | set the pin by hand to the intended version, then `image status --refresh` |
| `ROLLBACK_NO_PROMOTION` | no completed promotion is recorded, or no state file exists | set the pin by hand |
| `ROLLBACK_PREVIOUS_NOT_RECORDED` | the previous version and its image are not recorded as read | `image status --refresh`, then roll back |
| `ROLLBACK_PREVIOUS_MISSING` | the record shows that the previous version or its image no longer exists | choose another version by hand, or rebuild |

A `PROMOTE_WRITE_FAILED` that reports a failed directory flush after the rename is
the one case where the pin may have moved: the row stays `pending` and the next
resolving command decides from the file hash.

### Rolling back an image

The pin is one line of the TOML, so a rollback is a promotion in the other
direction. Finish or stop a running build container first, and start a fresh one
afterwards, as for a promotion.

```sh
python -m ci_burst image status  --config deployment.toml --refresh
python -m ci_burst image promote --config deployment.toml --rollback          # preview
python -m ci_burst image promote --config deployment.toml --rollback --write
```

`--rollback` restores the pin that the newest completed promotion replaced. It
uses the backup that row names, and no newer one. It refuses unless the file still
pins the version that promotion wrote, the backup still has its recorded hash and
contains the recorded previous pin, and the previous version and its image were
read as existing and `available` within `promote_max_age_seconds`. It writes the
previous pin with the same single-key edit, so other changes that you made to the
TOML since then stay. The `rollback` object of `image status` reports either the target and its
evidence or the reason a rollback is not available now. It does not check every write precondition, such
as a zone pin (`PROMOTE_ZONE_PIN`); read `write_check` in the output of
`image promote --rollback` without `--write` for those. While a crashed
`promote --write` has left a pending row, the two can disagree:
`image status --refresh` takes the target from completed rows only, and the
preview simulates the resolution of the pending row. Once the row is resolved,
run the refresh and the preview again before you roll back.

If no rollback is available, set the pin by hand to a version whose image exists.
The rest of rollback is the same as for the scaling path (see
[Scheduler, cleanup and rollback](#scheduler-cleanup-and-rollback)): the pin is
read by `apply`, so the next pass admits new workers on the restored image, except
in a zone whose entry carries its own `launch_template_version`: check every zone
pin. A launch intent that was recorded before the edit is retried with its original
request, so it can still launch on the old image. When the old image must not run
again, contain it as an incident instead: stop `apply` admission, keep cleanup
running, and reconcile the existing workers and intents
([Scheduler, cleanup and rollback](#scheduler-cleanup-and-rollback)). To stop the
image path itself, set `enabled = false`: every image command then answers
`disabled`, and the scaling path is unaffected. The edit disables fresh
invocations only: a running `image run --apply --until-done` container keeps the
configuration it loaded at its start, so stop that container too, and wait until no
`image run --apply` of any kind is running: a single pass that loaded the enabled
file can still be staging and launch after the edit. A builder that is running then
is no longer ended by a pass. Its own timer exists only if its user data ran, so
confirm that each builder that carries both `ci-burst:image-build = true` and
`ci-burst:deployment = <your deployment>` is terminated (next section) instead of
assuming that it stops itself.

### Image events and metrics

Applying passes append events to `image-events.ndjson` in the image state
directory. The file is `0600` and append-only; `promote --write` appends its
`image_promoted` event to the same file. One JSON object per line carries `ts`
(UTC), `deployment`, `event` and the fields below. A value is an id, a 12-character
digest, a count or a code, and never a URL. Only `image_build_failed` carries
console text, as `framed_tail`: the framed, sanitized and bounded end of the
builder's output. In that sanitized tail, text that contains `://`, a presigned
query string and a literal match of the recorded archive key are replaced by
`[archive-url]`. Nothing else is redacted, so a warm or gate command must not print a secret.

| Event | Written when | Fields |
| --- | --- | --- |
| `image_check` | when an applying pass finishes with a decision, a `refused` or `failed` one included; a pass that raises before it records an outcome writes none, and a write that raises after this event leaves it in place. `digest` and `source_sha` are absent when the build was already in flight at the start of the pass | `decision`, `reason`, `forced`, `build_id`, `digest`, `pin_digest`, `pin_version`, `source_sha` |
| `image_build_started` | the launch call returned a builder id (a builder that reconcile adopts after an uncertain launch writes none) | `build_id`, `builder_id`, `digest`, `source_sha`, `forced`, `deadline_at` |
| `image_marker_seen` | a pass reads the ready marker on the builder's console (again on later passes while the image creation is unresolved) | `build_id`, `builder_id`, `latency_seconds` |
| `image_created` | `CreateImage` returned an id, or the pass found the image by its tags while creating it (an image that reconcile adopts writes `image_reconciled` instead) | `build_id`, `builder_id`, `image_id` |
| `image_candidate_ready` | the candidate version was created and compared `clean` | `build_id`, `image_id`, `template_version`, `digest`, `duration_seconds` |
| `image_build_failed` | a build failed and the failure was counted. A refused start or restage that ends its row `failed` (such as `IMAGE_INPUTS_CHANGED`) writes none and shows in `image_check` with `decision: refused`; a recorded orphan that is ended `failed` writes none either | `build_id`, `reason`, `image_id`, `digest`, `duration_seconds`, `drift_fields`, `framed_tail` |
| `image_reconciled` | reconcile ended a builder, deleted an archive, recorded an image or adopted a leftover | `builders_terminated`, `archives_deleted`, `images_recorded`, `builder_adopted`, `image_adopted`, `build_id` |
| `image_pruned` | the deletion rule deregistered an image, deleted a snapshot or failed to | `deregistered`, `snapshots_deleted`, `failed`, `skipped` |
| `image_blocked` | the failure that set `blocked` | `consecutive_failures`, `build_id` |
| `image_promoted` | `promote --write` completed a promotion or a rollback | `old_pin`, `new_pin`, `build_id`, `image_id`, `rollback`, `file_digest` |

The event of a promotion is a notice. The journal row is the record, so a failed
event write is a `notes` entry (`event_write:<code>`) in the result and does not
undo the promotion.

Every applying pass also rewrites `image-metrics.prom`, a Prometheus textfile in
the same directory, and the telemetry counters beside it. A pass that started
tries to write them even when it ended `refused` or raised. If that write fails, the
old textfile stays in place. A run that stops before the pass (a wrong input, a
writer lock held by another pass) writes no event and no textfile, and no other
command rewrites the file. Every series carries the `deployment` label.

| Series | Type | Meaning |
| --- | --- | --- |
| `ci_burst_image_builds{state}` | gauge | rows of the state file by state: `building`, `imaging`, `candidate`, `failed`, `promoted`. Every row counts: a recorded orphan (`candidate` or `failed`), a pin record (`promoted`) and a row that a refusal ended (`failed`, not a counted failure) included |
| `ci_burst_image_last_build_timestamp_seconds{outcome}` | gauge | Unix time of the last build that ended `candidate`, or `failed` with a counted failure; a series exists only for an outcome that has occurred |
| `ci_burst_image_build_duration_seconds` | gauge | seconds from start to end of the last build that ended `candidate` or with a counted failure; absent until one has |
| `ci_burst_image_console_latency_seconds` | gauge | seconds from the build start to the latest sighting of the ready marker; absent until one was seen |
| `ci_burst_image_consecutive_failures` | gauge | failed builds since the last `candidate` |
| `ci_burst_image_blocked` | gauge | `1` while builds are blocked |
| `ci_burst_image_candidate_waiting_seconds` | gauge | seconds since the newest verified candidate was last verified; `0` when none waits. `image status --refresh` restarts it, and the series shows that at the next applying pass |
| `ci_burst_image_stale_seconds` | gauge | age of the image behind the configured pin, from the latest pin read that a pass saved; a pass that fails can publish an older read, even one of a previous pin |
| `ci_burst_image_leaked_resources_total{kind}` | counter | leaked resources that a pass removed: a `builder` or an `archive` that reconcile removed, and some `image` and `snapshot` removals by the deletion rule of resources that no build made, such as a tagged image with no row. It does not count every such removal; each `image_pruned` event, when it is written, summarizes one deletion sweep, and a pass can run more than one |
| `ci_burst_image_last_run_timestamp_seconds` | gauge | Unix time of the last successful textfile write, by a pass that returned a decision or one that raised |

Three things look alike and are not the same:

- A pass attempt is a pass that started. It tries to rewrite the textfile, and a
  successful write sets `ci_burst_image_last_run_timestamp_seconds`, whether the
  pass returned a result or raised.
- A completed decision is a pass that returned a result, a `refused` or `failed`
  one included. It writes the `image_check` event just before it returns, and
  then the check time that `image status` reports as `last_check_at`. A failure
  in between can leave the event with no check time and no result; a later
  failure, such as a failed textfile write, undoes neither. A plain `image status`
  also records that time, best-effort (see `check_recorded` in
  [Image commands](#image-commands)), so it is no proof that a pass ran. A pass
  that raises before its answer records no decision: it prints `{"error": ...}`
  and exits nonzero.
- The blocked state is the `blocked` flag of the control row, shown by
  `ci_burst_image_blocked`. It is not the answer of a pass; see
  [The blocked state](#the-blocked-state).

The events file, the textfile and the counters are written inside the container
with mode `0600`, so a collector cannot read them in place. Copy the textfile into
the collector's directory as the export unit does for the worker metrics
(`deploy/ci-burst-export.service`, `install -m 0644` into a temporary name, then
`mv`). The repository ships no unit that does this for the image textfile.

#### Example alert rules

These rules are examples. This repository ships no alert rules and no Prometheus
configuration. The thresholds are starting points: set them from the cadence at
which you run passes and from how often your lockfiles change. Set the `for` of
`CiBurstImageBuildNotAdvancing` above your `build_deadline_seconds` (at most 14400)
plus the time between two of your passes. `CiBurstImageMetricsStale` applies only
if you schedule `image run --apply` yourself.

```yaml
groups:
  - name: ci-burst-image-example
    rules:
      - alert: CiBurstImageBlocked
        expr: ci_burst_image_blocked == 1
        for: 5m
        annotations:
          summary: "Automated image builds stopped themselves after repeated failures"
      - alert: CiBurstImageBuildsFailing
        expr: ci_burst_image_consecutive_failures >= 2
        for: 5m
        annotations:
          summary: "Two image builds in a row failed"
      - alert: CiBurstImageStale
        expr: ci_burst_image_stale_seconds > 7 * 24 * 3600
        for: 1h
        labels:
          severity: information
        annotations:
          summary: "The image behind the pin is more than 7 days old"
      - alert: CiBurstImageCandidateWaiting
        expr: ci_burst_image_candidate_waiting_seconds > 48 * 3600
        for: 1h
        annotations:
          summary: "The newest candidate was last verified more than 48 hours ago"
      - alert: CiBurstImageLeakedResources
        expr: increase(ci_burst_image_leaked_resources_total[1d]) > 0
        annotations:
          summary: "A pass removed a leaked builder, archive, image or snapshot"
      - alert: CiBurstImageMetricsStale
        expr: time() - ci_burst_image_last_run_timestamp_seconds > 2 * 24 * 3600
        for: 1h
        annotations:
          summary: "No image pass has written its metrics for two days"
      - alert: CiBurstImageBuildNotAdvancing
        expr: sum by (deployment) (ci_burst_image_builds{state=~"building|imaging"}) > 0
        for: 6h
        annotations:
          summary: "A build has stayed active for 6 hours: passes are failing or not running"
```

`CiBurstImageMetricsStale` watches the last successful textfile write. It fires
when passes stop, and also when passes run but cannot write the textfile, as long
as the old sample stays exposed. `CiBurstImageMetricsStale` does not fire while
the series has never existed, nor when the exported textfile or the collector is
gone, because the expression is then empty: alert on the absence of the series
too. The sum in
`CiBurstImageBuildNotAdvancing` keeps one timer across the move from `building`
to `imaging`. A pass that raises can still write the
textfile, so repeated provider failures do not trip it, and the failure rules can
stay quiet as well. `CiBurstImageBuildNotAdvancing` covers a build in flight: a
pass that raises before it advances the build cannot end it at its deadline, so the
build stays `building` or `imaging`. Without a build in flight, a pass that raises
has no failure series of its own. Its signals are its nonzero exit status and,
for a failure before the decision is recorded, the missing `image_check` event.
Alert on the scheduled run's absence or nonzero exit in your scheduler, as
[container.md](container.md#scheduled-operation-with-systemd) does for the cleanup
run.

When `ci_burst_image_leaked_resources_total` rises, read the `image_reconciled`
and `image_pruned` events for what was removed. The termination of an overdue
builder of the active build is not counted
([Leaked builders and the backstop](#leaked-builders-and-the-backstop)).

### Leaked builders and the backstop

A builder is an on-demand instance of the build launch template, tagged
`ci-burst:image-build`. It never carries `ci-burst:managed`, so `reap`,
`controller.max_instances` and worker discovery do not see it. Two things on the
instance itself end a leaked builder without any controller: the warm script arms
its own lifetime timer first (`image.warm.lifetime_seconds`), and the template
terminates the instance on shutdown. Both hold when the controller host is down or
the image path is disabled, but only if the user data ran: a builder whose script
never started has no timer, and terminate-on-shutdown does not start a shutdown.
To find such a builder, filter on both `ci-burst:image-build = true` and `ci-burst:deployment = <your deployment>`, then terminate the exact instance id.

When passes do run, every applying pass reconciles before it does anything else.
It terminates every tagged builder that is not the active build's, and the active
build's own once its deadline has passed. It deletes any object under the archive
prefix that is not the active archive and is older than one hour; it checks neither
the key name nor the writer of the object, so the prefix must belong to this
deployment alone
([deployment.md](deployment.md#the-image-tables-optional)). It adopts a builder or
an image that a crash left behind, and records a tagged image that has no row as an
unverified candidate that is never pinned. Such a row has the reason
`IMAGE_ORPHAN_RECORDED`, no verification time and no template version:
`image status` can show it as `last_build`, but never as the `candidate`, and
`promote` cannot select it. Removals of leaked resources add to
`ci_burst_image_leaked_resources_total`; the termination of an overdue builder of an
active build does not, so do not use the counter to account for those. The bucket lifecycle rule on the staging
prefix is a backstop only: when a build ends, ci-burst makes one delete call for
the archive key that its row records. A refused call is noted
`archive:delete_failed` and leaves the deletion unconfirmed; the row keeps its
end state. An upload that finished before its key was recorded leaves no key, and
the reconcile sweep or the lifecycle rule removes that object.

A builder that outlives its lifetime is a defect. To remove it, filter on both `ci-burst:image-build = true` and `ci-burst:deployment = <your deployment>`, then terminate the exact instance id: the
image-build tag alone can match the builder of another deployment in the same
account and Region.

#### Retention

The image of a failed build is removed by the deletion rule, except that the image
of a build whose row records `TEMPLATE_DRIFT` is kept for 24 hours first.
Retention prunes only while the images it counts number more than `retain`, so
`retain` bounds how far it prunes; it does not promise that many usable images
exist. It removes only images older than
`retain_min_age_seconds`, and never removes a protected image: the image behind
the pin, a zone pin, the previous pin, the new pin of the newest completed
promotion or rollback, both pins of every journal row since that one (a failed
promotion that a pass resolved from a stale file still names the live pin), the
pins of a pending promotion, the newest verified candidate (unless a refresh recorded it as `TEMPLATE_DRIFT`: then only a pin that names its image protects it) or the active build. `retain` is not a
cap: the protected set can hold more images. When it
cannot resolve that set it deletes nothing (`IMAGE_DELETION_UNSAFE`), and a
deployment file that is a stale view of a replaced file is one such case. A
journal row whose pin is not a version number is refused when the journal is
read (`IMAGE_STATE_UNREADABLE`), before retention runs. Retention reads the pin from the TOML on every pass, so a
build container must not run across a promotion
([container.md](container.md#run-the-image-commands)).

#### Hand-built worker images

No ci-burst command prunes the worker images made by hand, by the recipe in
`deploy/runner/image/` or by the
[manual warm-image runbook](warm-image.md#manual-runbook), or their
launch-template versions. Their snapshot storage is billed and untracked, so
tracked cost does not bound it (see
[Cost tracking and the allowance](#cost-tracking-and-the-allowance)). The
automated retention (`retain`, `retain_min_age_seconds`) covers only images that
the pipeline built, which carry the `ci-burst:image-build` tag. Deleting a
hand-built image, its snapshots or a template version is an operator decision;
this guide gives no rule for it yet
([#142](https://github.com/mikomiel/ci-burst/issues/142)).

### Image reason codes

Where the code appears depends on how the command ended:

- A result with `decision: failed` or `decision: refused` carries the code at
  top-level `reason`. `failed` exits `1`, `refused` exits `2`.
- A raised refusal prints `{"error": {"reason": ...}}`, with a `detail` when it
  has one, and no `decision`. It exits `1` when it is a provider refusal from an `image` command,
  and `2` otherwise.
- Any other exception prints `{"error": {"reason": "UNEXPECTED", ...}}` and
  exits `1`.

A build that ends `failed` carries one of the reasons below, in its
`image_build_failed` event and in `reason`; the failure counts, sets the cooldown
and moves the count toward `blocked`. A refusal that ends the stage of the archive
or the launch of the builder ends the build in this way, whatever its code. The
exceptions are a pass deadline, which leaves the row for the next pass; a launch
whose outcome is uncertain, which leaves the row `building` with the note
`run_builder:uncertain` (the next pass adopts the builder, or ends the build
`IMAGE_BUILDER_GONE` when it finds none); and the two start refusals of
[When a build starts](#when-a-build-starts). A refusal of the image creation
ends the build when it is definite or the build deadline has passed. A refusal of
the version creation ends it only once the build deadline has passed; before that,
the next pass asks again. `image plan` is a preflight that stages nothing, so a
passing plan does not rule these out.

| Reason of a `failed` build | Meaning |
| --- | --- |
| `WARM_FAILED` | the builder printed a failure marker: the script failed, for example in the archive fetch, a warm command or the gate. Read the cause in the framed tail of the event or the console |
| `IMAGE_MARKER_DIGEST_MISMATCH` | the builder reports another archive digest than the staged one |
| `MARKER_TIMEOUT` | no ready marker by the build deadline, or the marker was first read at or after it while the builder was still running |
| `CONSOLE_BLIND` | the console returned no text by the deadline, the provider refused the console read in a pass at or after the deadline (`notes` holds `console_read:<reason>`), or the launch fell at or after it. A console read refused before the deadline makes the pass raise, and the next pass reads again |
| `IMAGE_BUILDER_GONE` | a pass did not see the builder as active, or no builder was recorded, while the build had no recorded image. It is not proof that the builder ended. A later applying pass asks the provider to terminate a tagged builder that it lists and that is not the active build's, and does not check the result: verify the final state yourself. Find the instance by the failed row's `builder_id` or its `ci-burst:build-id` tag, not by the deployment tags alone, which also match a newer builder ([Leaked builders and the backstop](#leaked-builders-and-the-backstop)). An image may still exist; the deletion rule handles it as the image of a failed build |
| `IMAGE_NOT_AVAILABLE` | the image failed, or was not `available` by the deadline |
| `TEMPLATE_DRIFT` | the candidate version differs from the pin in a field other than `ImageId`, or does not carry the build's image; `drift_fields` names them |
| `FORGEJO_ARCHIVE_TOO_LARGE` | the forge stream of the archive passed `max_archive_bytes` while it was staged. A passing `image plan` does not rule it out, because the plan reads only the first chunk |
| `IMAGE_ARCHIVE_TOO_LARGE`, `IMAGE_ARCHIVE_UPLOAD_FAILED` | the upload counted more than `max_archive_bytes`, or the upload failed |
| `IMAGE_USERDATA_TOO_LARGE` | the user data, rendered with the real presigned URL, is 16384 bytes or more. `image plan` sizes it with a synthetic URL, so a plan can pass where the run fails |
| `IMAGE_SOURCE_FORBIDDEN` | the forge answered `401` or `403` to the archive request |
| a forge or provider code | any other refusal that ended the build as described above, such as `FORGEJO_UNAVAILABLE` or `AWS_REFUSED` |

Cleanup after an oversize or failed upload is best-effort: once it has an upload
id, ci-burst tries to abort it and delete the object, and either call can fail without
changing the reason code. Do not assume that nothing is left. The reconcile sweep
deletes stray objects under the prefix only once they are older than one hour, and
an incomplete multipart upload is not an object: give the staging prefix a
lifecycle rule that aborts incomplete multipart uploads
([deployment.md](deployment.md#the-image-tables-optional)).

The codes below are refusals, not failed builds. A refusal counts nothing toward
the cooldown or the block. Most are raised before anything is staged; the trigger
rules and the two start refusals are in [When a build starts](#when-a-build-starts),
and the codes of `promote` in [Promote reason codes](#promote-reason-codes).

| Code | Meaning |
| --- | --- |
| `IMAGE_UNTIL_DONE_NEEDS_APPLY` | `--until-done` without `--apply` |
| `IMAGE_GATE_REQUIRED` | `[image.warm].gate_command` is empty and `allow_no_gate` is `false`. Raised before any client is built by `image plan` and a run without `--apply`, and by an applying pass when no build is in flight; a pass that settles a build in flight answers it `refused` for the launch it would make, and a stage done again of a row inserted under `allow_no_gate` ends its row `failed` with it (a row inserted with a gate command ends as `IMAGE_INPUTS_CHANGED`) |
| `IMAGE_INPUTS_CHANGED` | a stage done again found that the basis files at the row's commit, the warm or gate commands, `strip_components` or the script no longer give the row's digest. Answered `refused`; the row ends `failed`, the delete of its archive is attempted when it recorded one, and no failure is counted |
| `IMAGE_SOURCE_FORBIDDEN` | the forge answered `401` or `403` to the image token while the branch or a basis file was read, or, in `image plan`, the first chunk of the archive. The same answer to the archive request of a staging run ends the build `failed` (first table) |
| `IMAGE_BRANCH_UNPROTECTED` | the configured branch is not protected on the forge |
| `IMAGE_BASIS_PATH_MISSING` | the forge answered `404` for a `basis_paths` file, or the path escapes the repository, at the commit read (the branch head; the row's own commit on a stage done again) |
| `IMAGE_BUILD_TEMPLATE_UNSAFE` | the build launch template or its version is missing or fails a safety check; the detail names the properties, never a value. On a stage done again the row ends `failed`, uncounted, and the pass answers `refused`. A read of the template that the provider refuses, such as a version that EC2 reports as not found, is the provider's own refusal instead: it is raised (exit `1`), and on a stage done again it leaves the row for the next pass |
| `IMAGE_USERDATA_TOO_LARGE` | in `image plan`: the user data, rendered with a synthetic URL, is 16384 bytes or more. In an applying run it ends the build `failed` instead (first table) |
| `IMAGE_SCRIPT_MISSING` | the container image does not hold the shipped warm script |
| `pass_deadline_exceeded` | the 240-second budget of a pass ran out. It is raised in `image plan` and before a decision. Elsewhere it is a `notes` entry, alone (the row waits for the next pass) or after the name of a lookup that the pass then goes without (such as `image_lookup:` or `base_image:`), or the `refused` code of the `retention` or `deletion` part |
| `IMAGE_STATE_LOCKED` | another pass, refresh or `promote --write` holds the writer lock of the state file; an applying run also meets it when it resolves a pending journal row |
| `IMAGE_STATE_VERSION_UNSUPPORTED`, `IMAGE_STATE_UNREADABLE` | the state file has a schema this version does not know, or cannot be opened as the state file; keep the file and do not edit it |
| `IMAGE_STATE_RECOVERY_NEEDED` | a process stopped inside a commit and left a rollback journal (`image-state.sqlite-journal`) that a read-only open cannot roll back; `status`, `plan`, the previews and `run` without `--apply` refuse and leave it. A writing command rolls it back: `image run --apply`, `image promote --write` (with or without `--rollback`) or `image status --refresh` takes the writer lock before its writable open, which rolls the journal back, and then does what it always does (a run resolves a pending row and runs its pass; a promotion also writes the pin). Another holder of the lock gives `IMAGE_STATE_LOCKED` with the journal left as it is. Do not delete the journal file: it holds the pages that make the file consistent |
| `IMAGE_PROFILE_INVALID` | the image role profile is missing or breaks the profile rules of [deployment.md](deployment.md#image-build-role-and-source-identity-optional); a provider failure, exit `1` |
| `IMAGE_DELETION_UNSAFE` | the deletion rule could not resolve the images it must protect, or read the deployment file through a stale view, so it deleted nothing. It appears inside the `retention` part of a pass result (`deletion` in a plan) and does not fail the command; that part can carry another refusal code, such as a provider code, in the same way |
| a forge or provider code | raised from a read before a build is staged: a `FORGEJO_*` code exits `2`, an `AWS_*` code exits `1` |

Two reasons mark rows that no build made: `IMAGE_ORPHAN_RECORDED` is a tagged
image that reconcile found without a row, and `IMAGE_PIN_RECORDED` is a pin that
no build owns, recorded by `image status --refresh`, by `promote` or by the pin
record after an applying run (`pin_evidence`), so that a rollback can verify it. Neither is a failure.

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
request that was accepted, the incidental hourly rate and the non-compute buffer
it was admitted under, the `MaxPrice` frozen in its request and, on the first
reconcile that observes the instance, the provider's launch time. Its cost is
computed per second:

```
cost = ceil(price * billable_seconds / 3600)
     + ceil(incidental * billable_seconds / 3600)
billable_seconds = clamp(max(60, end - launch_time), 0, lifetime_seconds)
```

`price` is the accepted quote. A row without one — created before this ledger
schema, or whose write was lost — uses the higher of `max_spot_price` and the
`MaxPrice` frozen in its request, which over-states but never under-states.
`incidental` is the rate recorded on the operation at admission, falling back to
the configured `incidental_per_hour` for a row that predates schema 3; it covers
public IPv4 and EBS and defaults to zero. The 60-second floor is the provider's
per-instance minimum, and it is counted once: it starts `billable_seconds`, so
the same interval is not priced again as remaining life. A 60-second worker
therefore commits one billed minute, not two.

Admission does not use accrued cost alone. For every non-terminal operation the
controller commits:

```
commitment = accrued cost
           + ceil(remaining_price * remaining_seconds / 3600)
           + ceil(incidental * remaining_seconds / 3600)
           + buffer
remaining_seconds = max(0, lifetime_seconds - billable_seconds)
remaining_price = max(max_spot_price, admitted MaxPrice, accepted_price or 0)
lifetime_seconds = max(60, ceil(expires_at - created_at))
```

so a newly admitted worker commits `max_spot_price * max_lifetime +
incidental_per_hour * max_lifetime + non_compute_allowance` — the earlier
reservation plus the incidental term, which is zero unless configured — and that
commitment shrinks every second it lives.

`incidental`, `buffer` and `remaining_price` are the inputs the operation was
admitted under, not the current configuration. Admission records the configured
incidental rate and buffer on the operation, and the ceiling is the highest of
the configured maximum, the `MaxPrice` frozen in the operation's request and its
accepted price. Editing `max_spot_price`, `incidental_per_hour`,
`non_compute_allowance` or `max_lifetime_seconds` cannot lower the ceiling of a
request already sent, erase cost already accrued, or release a commitment an
outstanding worker still carries. A raised `max_spot_price` is the one edit that
can raise an outstanding operation's forward commitment, because unlived time is
priced at the higher ceiling; that is conservative and can only hold allowance
back. A lowered ceiling cannot reprice an operation already admitted, and no cost
edit can release one. The lifetime comes from the operation's own durable expiry
window for the same reason.

The schema-3 upgrade marks every non-terminal row whose admitted inputs it
backfilled with `admitted_inputs_backfilled = 1`; a terminal row keeps `0` and
its stored settled cost. That stored mark makes the row's reservation the
commitment floor. So does a non-terminal row whose frozen request does not
decode: without its `MaxPrice`, a lowered `max_spot_price` and the accepted price
would price the remaining life below the ceiling that admission committed. A
terminal row needs no ceiling for its remaining life and keeps no such floor.
The floor is not inferred from a whole-life estimate, because
such an estimate includes the current configured ceiling: raising
`max_spot_price` could then reach the reservation, switch the floor off and
release allowance admission had committed. A row admitted normally carries a
zero mark, no floor, and keeps only the commitment its own recorded basis
reproduces. Terminal rows are never repriced.

A batch is affordable while the window's `spend` plus the forward commitment
plus one new worker's worst case stays inside `allowance`:

```
affordable = (allowance - committed) // per_launch
```

`spend` is the settled cost of completed operations plus the accrued cost of
operations still running; `committed` is `spend` plus the forward commitment.

The launch decision reports `budget.remaining` as the allowance minus
`committed`, and `budget.remaining_after` as the allowance left after the
reported batch is admitted:

```
remaining_after = allowance - committed - launch_count * per_launch
```

A zero-worker decision therefore reports `remaining_after` equal to
`remaining`, and a budget-capped batch subtracts only the workers it admitted.
The field is planned headroom for the decision, not evidence that those launches
completed or that the provider billed that amount.

An operation settles once, when a reconcile observes its instance `terminated`.
An instance that disappears before the provider retention window settles at a
full configured lifetime at the price the launch was accepted at, or at the
`MaxPrice` frozen in its request when no price was recorded (the configured
maximum only for a row whose request carries none). That bound keeps the
settlement inside the commitment the operation carried while unresolved; a raised
configuration never settles above it. A definite pre-launch rejection settles
zero, and a settled or failed operation is never charged again: `settle` writes
only a row that has no settled cost yet, so a later reconcile or configuration
edit cannot rewrite it. A restart does not reset the window.

The unspent part of a commitment is released as the operation ages and fully
released when it settles, so a one-job worker that exits after ten minutes stops
reserving its full lifetime. That is the only release this accounting makes:
money already spent stays counted, and a completed operation is charged to the
window it started in. An accepted launch stays charged even if the instance
never reaches `running`, and an uncertain outcome keeps its commitment until the
intent is resolved.

Tracked cost does not bound EBS, data transfer, public IPv4 beyond the configured
rate, AMI and snapshot storage, log delivery, billing increments longer than the
60-second minimum, Spot price changes after launch, or resources left by failed
cleanup. Tracked cost is an estimate and not a guaranteed cap on the AWS bill, and
no status, lifetime or alert in this project should be described as one.

Burstable CPU credits are a separate residual. A burstable instance in
`unlimited` CPU-credit mode can keep bursting after its earned credits are spent
and accrue surplus CPU credits, which the provider bills on top of the instance
price; a short, immediately busy worker is exactly the shape that incurs them.
T3, T3a, T4g and T8i launch in `unlimited` mode by default and T2 in `standard`
mode, unless the launch template's `CreditSpecification` or the account-level
default says otherwise. Every plan candidate therefore carries a `credit`
object: whether the family is burstable, the observed or default mode, whether
that mode came from the template or the AWS family default, and that surplus
credits are unquantified. The plan does not query the account-level default
credit specification: where the template sets none it reports the family
default, not the account's effective default. A family outside the product's
explicit table reports `unknown` rather than a guessed mode, and a non-burstable
family reports the question as not applicable. A template value that is present
but not `standard` or `unlimited` is reported as unknown evidence, never as the
default mode: the plan reports no mode with a `template-unrecognized` source,
and a launch carrying it would fail provider validation. A value the adapter
cannot represent -- empty, over-long or non-printable -- is still present and
takes that same unknown path; it is never reported as an absent specification.
`scripts/check_prerequisites.py` reports the template's
`CreditSpecification` value when it is representable, and whether the template
and each configured pool type is burstable; an unrecognized or unrepresentable
value is `credit_specification_invalid` drift, while an absent specification
stays absent and is not drift. The
estimate does not bound surplus credits in either mode: choosing the instance
family, setting the credit mode and any billable comparison are operator
decisions, and the product only reports the exposure.

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
launches = min(max(0, waiting jobs - recent launches), free slots, affordable workers)
```

- **Waiting jobs** is the eligible demand in the qualified queue observation.
- **Recent launches** is the number of owned operations in the state `starting`
  or `running` that were created less than `controller.claim_grace_seconds` ago.
  Each one covers one waiting job, so only the demand it does not cover is
  requested (§ Claim grace below).
- **Free slots** is `controller.max_instances` minus the non-terminal owned
  operations in the ledger. An intent, starting, running, terminating or
  uncertain worker occupies a slot; a terminal or failed one does not.
- **Affordable workers** is `(allowance - committed) // per_launch` for the
  current anchored window, with `committed` and `per_launch` as defined in
  § Cost tracking and the allowance.

A zero bound is not an error. No free slot waits with `slot_busy`, no demand waits
with `no_runnable_demand`, demand that recent launches already cover waits with
`awaiting_claim`, repeated futile launches refuse with `recent_launches_futile`
(§ Futile launch hold), and no affordable worker refuses with
`budget_allowance_exhausted`. When the allowance allows fewer launches than both
the uncovered demand and the free slots, the batch is capped and the decision
carries the `budget_capped_batch` note. `plan` reports the same `demand`,
`recent_launches`, `free_instances` and `launch_count` that an `apply` would use,
when the ledger matches the provider inventory.

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

The worker contract stays one job per worker. An idle worker polls for a task
during its claim window, then prints `STEP claim-timeout`, shuts down and
terminates. The claim window is the expected end of an idle worker, not a bound:
a poll that hangs is ended only by the job timeout and the instance lifetime, and
a failed shutdown or a provider fault only by the instance lifetime and by
expiry cleanup. A worker that boots from an earlier template version has no
claim window and waits for one job until the job timeout or its maximum
lifetime. A reservation prices the full configured lifetime, and its forward
commitment is released only when the worker settles, so an idle worker keeps its
worst case committed for as long as it lives, and money already spent is never
refunded. Fan-out therefore trades committed allowance for readiness: N workers
can take N jobs at once, and a worker that never gets a job is expected to end
soon after its claim window but can still cost up to its configured lifetime.

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

### Claim grace

A job stays `waiting` until a worker claims it, and a worker needs time to boot,
register and poll. Without a bound, every tick would launch another worker for the
same job, up to the free slots. Forgejo reports no link between a worker and a
job, so the controller uses time: each recent launch covers one waiting job for
`controller.claim_grace_seconds` (default `300`, the claim window of the worker
bootstrap; range `60..1800`; there is no off value).

- Only an owned operation in the state `starting` or `running` covers a job. A
  `terminating` operation is shutting down and will not claim one, but it still
  occupies a slot until it is terminal; an `intent`, `uncertain`, `terminal` or
  `failed` operation covers nothing.
- The age is the controller clock minus the `created_at` in the ledger, and the
  launch covers a job only while that age is less than the grace. A restart
  neither forgets a launch nor extends its grace.
- When no demand is left uncovered the decision is `wait` with `awaiting_claim`
  and a `launch_count` of `0`, and no launch intent, reservation or instance is
  created. `slot_busy` and `no_runnable_demand` keep precedence and still report
  `recent_launches`.
- Every decision that admission computes reports `recent_launches`, the number it
  counted. A result that ends before admission computes a decision (an owned-state
  discrepancy, an invalid state, a changed scope or a refused observation), and a
  deadline deferral that holds no decision, need not carry one.
- After the grace, a job that is still waiting can get a new worker. That is the
  recovery path for a worker that failed its bootstrap or never registered; the
  replacement stays inside the slot limit and the allowance. When the three most
  recent finished workers all ended that way, the futile launch hold stops the
  replacement until their evidence ages out (§ Futile launch hold).

The grace does not change the slot limit, the allowance or the price checks, and it
matters only when `controller.max_instances` is above `1`: one slot already stops a
second launch while a worker is outstanding. It is an estimate, not proof that a
worker is healthy. A launch that has already taken its own job still covers one
waiting job until the grace ends, so a job that becomes runnable in that time can
wait up to the grace for its own worker. The grace starts when the ledger row is
created and the worker's claim window starts at registration, so the grace ends
earlier by the boot time, and a job that a slow-booting worker was about to claim
can get a second worker, which ends at its own claim deadline. That costs one
extra short-lived worker per lost race, bounded by the slot limit and the
allowance.

`plan` reads durable state only and does not resume or reconcile. It can show a
launch for a job that `apply` holds back while an operation has no instance, and
it counts a stored `starting` or `running` operation that reconciliation would
turn into `terminating`, until the next `apply` reconciles the row.

### Futile launch hold

A persistent worker fault, such as a registration parameter the worker cannot
read or an image that lacks a command the bootstrap needs, makes every worker end
before it registers. The slot frees, the job still waits, and the claim grace no
longer covers it, so each pass would launch again. The hold thins that loop.

`apply` and `plan` read the record that `export` writes, at
`<ledger directory>/export/observed.json`. They never write it. The read makes no
provider or queue call, and the reader makes no ledger call: it needs only the
file and the operations of this deployment that the pass reads from its open
ledger (see below). They open the file without blocking
and read it only when it is a regular file that is not a link, so a FIFO at the
path cannot stall a pass.
They take the type, the age and the size from the open file and decide before
they read a byte: a file that is not regular is `unreadable`, a file past the
freshness bound is `stale` whatever its size, and a file above 16 MiB is
`malformed`. They then read the open file itself, with no buffered stream in
between, and stop at 16 MiB plus one byte. A file that grows past 16 MiB after
they took its size is `malformed`, and they fetch no more than 16 MiB plus one
byte from it.
`--state-file` moves only the export run: the controller reads the default path
and does not look anywhere else.

A worker is classified only when its record shows that the instance ended
(`terminated` or `stopped`), it carries an operation identifier of the shape that
this deployment generates, and the ledger holds that operation for this
deployment (see below). It is:

- registered when the record shows the registration step, a later step
  (`job` or `claim-timeout`), a one-job process outcome or a Spot interruption
  notice, whether or not the record shows the exit marker;
- futile when the record shows the exit marker (`bootstrap exiting rc=<n>`) and
  none of that registration evidence.

Any other record is no evidence and is never counted: an ended worker with no
exit marker and no registration evidence (an empty console, a hard kill before
registration, a power-off), a worker that has not ended, a record with no
operation identifier of this deployment, and a record whose operation the ledger
does not hold for this deployment. A registered worker that ended without an exit
marker, for example after a hard kill, is counted and can clear a hold. A worker
that still runs is never counted, even when it registered.

A worker counts only when the ledger holds its operation for this deployment. The
pass reads the operation rows from the ledger that it has open. `plan` reads them
beside its state block. `apply` reads them at the end of reconciliation, inside
the transaction that has settled its rows and read the slot. The hold adds one
read of the open connection, no provider call and no schema change. A row counts
when its frozen request does not name another deployment. The deployment is the
one that the scope check reads, the `ci-burst:deployment` tag of the frozen
request. A row with no readable tag is unbound, as in that check, and it counts.
`export` writes the operation tag of any owned instance that the provider lists,
and reconciliation ignores a terminated instance that no ledger operation names.
The hold ignores it as well. When the ledger names an
instance for the operation, the record must be filed under that instance. When
the ledger names none, the operation alone is enough. A record that fails this
check is not counted, and it takes no place among the newest three. A ledger that
is empty, or absent for `plan`, leaves no evidence, and the hold stays open.

A rename does not carry the hold over. The procedure to change the deployment
name leaves the old rows in the ledger as history (§ Durable state and
ownership; [Change the deployment name](deployment.md#change-the-deployment-name)),
and `export` keeps the records of their workers until its seven-day retention
drops them. The hold leaves those rows out, so those workers are no evidence for
the new name, and `plan` and `apply` admit under it as under any new deployment.
The hold compares the deployment of the row and nothing else of its scope: it
does not compare the region or the account of the row.

The rule uses three fixed values. They are not configuration and there is no off
switch.

| Value | Meaning |
| --- | --- |
| 3 | workers that must be considered, all futile, for a hold |
| 3600 seconds | the window: a worker is inside while the controller clock minus its record's `seen` is at most this |
| 600 seconds | the record is evidence while its modification time is at most this old |

The considered workers are the three classified workers inside the window with
the newest `seen`, plus any other that ties the third. When at least three are
considered and every one is futile, the decision is `refused` with the single
reason `recent_launches_futile` and a `launch_count` of `0`. No template, price
or budget is read for that decision, and no intent, reservation or instance is
created. The command exits 2, as it does for any refusal. `slot_busy`,
`no_runnable_demand`, `awaiting_claim` and an owned-state discrepancy keep
precedence.

Every decision that reaches the hold reports `launch_evidence` with the evidence
`state` and the `considered` and `futile` counts. The counts are `0` unless the
state is `fresh`. The states, in the order they are tested:

| State | Meaning |
| --- | --- |
| `missing` | no file at the path |
| `unreadable` | the file cannot be opened or read, is not a regular file (a FIFO, a directory) or is a link, or the reader failed in a way it did not foresee |
| `stale` | the modification time is more than 600 seconds old, or after the controller clock. The reader reads the clock after it has taken the file's status from the open descriptor, so a heartbeat that `export` refreshes while the pass reads is not after the clock; the age, the record dates and the window all use that one reading |
| `malformed` | larger than 16 MiB, not UTF-8 JSON (a `NaN`, `Infinity` or `-Infinity` constant counts as not JSON), a JSON object that names a member twice at any level (a decoder keeps only the last value, and `export` never writes one; `export` refuses a constant and a repeat in the same way when it loads its own record, § Worker evidence), another record version, or one record with a bad field the hold reads (a `seen` after the controller clock is one), an instance key that `export` rejects, or a registration marker (`job_process`, `spot_interruption`) of a shape that `export` does not write. The hold drops the whole file. When `export` loads its own record, it drops only the record that holds such a value, the record of an ended worker that lacks a registration key, and a record dated after the clock of its run (§ Worker evidence) |
| `fresh` | the hold is evaluated |

Only `fresh` evidence can hold. Every other state admits as if the hold did not
exist, and every other admission check still applies to a launch the hold lets
through. `export` stays optional: where it is absent, stopped or failing, the
record turns `missing` or `stale` and admission behaves as before. The record's
modification time is the heartbeat of the command's own work. `export` saves the
record before it replaces the textfile, under the modification time that the
record had, and advances that time as its last step, after the event file (when
the run has events to add or the file exists) and the textfile, so only a run
that completed those writes refreshes it. A refused run, or a run that fails
before it gets there (the textfile cannot be written, for example), lets it age:
a run that cannot replace the textfile keeps the record that it saved under the
modification time of the record that it read, or the epoch when it read none.
Until that time is 600 seconds old, a hold can rest on the evidence that the
failed run read. The heartbeat does not cover publication. The shipped unit publishes the textfile
into the collector's directory afterwards, from `ExecStartPost`, and a failure
there leaves the record fresh: a hold still rests on the record, because the
evidence in it was read, while the collector's copy of the textfile is not
replaced. A run that fails earlier does not reach that step, so the collector's
last-run timestamp does not advance either (§ The metrics textfile). `export`
creates the record owner-only, so a `plan` run as another user than the export
unit reads `unreadable` and plans as if the hold were open.

A hold ends on its own when a registered worker joins the newest three, when the
oldest of the three leaves the window, or when the record stops being fresh. It
lasts at most 3600 seconds after the newest futile worker was read. A hold
launches nothing, so a fix to the fault produces no new evidence, and admission
still waits for the window after the fix. Evidence lags the end of a worker by
about one export interval, so the first burst of futile launches is not
prevented.

Under a persistent fault the hold thins the waste and does not end it. At one
slot, about three workers launch per hour: each time the oldest record ages out,
one launch is admitted, fails, and renews the hold. With more free slots, a whole
batch can launch each time the hold opens. The hold is not a cap on wasted
launches and not a spend control. The configured allowance remains the only
spend control.

Known residual cases:

- A worker that registered, flooded its console so the registration line
  scrolled out, and then ended through the exit path before any later marker
  reads as futile. Three such workers hold admission for at most one window.
- A registered worker that still runs does not clear a hold.
- A record that `export` dropped, for a damaged registration value or for a date
  after the clock of its run, is rebuilt from its console alone while the
  provider lists the instance. The first case above applies to it: a
  registration line that has scrolled out of the console reads as futile. A
  worker that still runs also loses what earlier reads of its console found, and
  a clock set back by more than the age of the newest records drops the records
  of all live workers at once.
- A ledger operation that names no instance counts every record that carries its
  tag. The controller launches one instance for each operation, so two such
  records need a tag that the controller did not write.
- Records carry no deployment name, so the hold takes the deployment from the
  ledger row of the operation, so a rename does not mix their evidence. Two
  deployments do not share export files (§ Worker evidence, sharing contract). A row with no readable
  deployment tag is unbound and counts for every name; the controller never
  writes such a row. The hold does not compare the region or the account of a
  row, so a worker that ended under another region or credential route of the
  same deployment name still counts until it leaves the window.
- `export` reads at most 16 consoles per run, so heavy turnover defers some
  records to later runs.
- Only the shipped first-boot script prints the markers. Workers of another
  script are no evidence, and the hold never trips for them.
- `export` bounds the record by its seven-day retention and not by size. The
  reader accepts at most 16 MiB, which holds about 13,000 to 23,000 instances.
  A fleet that launches more than about 77 to 138 workers per hour for seven
  days passes that bound, and then every decision reports `malformed` and the
  hold stays off.

There is no acknowledgement command, and `status` does not report the hold,
because it builds no admission decision. For a pass that reaches new admission,
`plan` reports the same hold as `apply`. A pass with an unresolved launch intent
differs: `apply` retries that intent before it reaches the gate, so a hold does
not stop the retry, while `plan` does not resume and, with a free slot, can
report the hold (§ Claim grace). Two levers exist, and neither is a clean resume:

| Lever | Effect |
| --- | --- |
| Delete the record | The next pass reads `missing` and admits. The next export run rebuilds records from the consoles that it reads within its per-run budget (`MAX_CONSOLE_READS`, 16, live workers first) among the instances the provider still lists, about the last hour of ended workers, and stamps them with its own time. A surplus of ended workers waits for later runs while the provider still lists them. The hold can return for a full window. |
| Stop the export timer | The record ages past 600 seconds and the hold opens. Worker observation stops too. This is the one blunt lever. |

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

A futile launch hold (§ Futile launch hold) is decided before the first template
read. A held decision therefore carries no candidates, no `selected` candidate
and no budget.

`apply` admits a batch of at most `controller.max_instances` workers, sized by
the waiting demand that no recent launch covers, free slots and affordable
workers (§ Fleet size and fan-out). Each worker in the batch is its own admission, with its own frozen
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
client token, expiry, reservation, the instance id, the verified AWS scope
(Region and account) and the deployment carried by the frozen request's
`ci-burst:deployment` tag. A process-wide file lock
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

Four bounds refuse rather than launch: an unresolved intent older than the
24-hour client-token validity, an intent whose configured lifetime has already
elapsed, an intent whose recorded admitted lifetime differs from the
configured `aws.max_lifetime_seconds`, or that records none because the row
predates ledger schema 5 (#138), and an intent written more than 900 seconds
ago (#222). Each intent stores the lifetime it was admitted under, so the
mismatch refusal does not infer it from `expires_at` and `created_at`. All four keep the unresolved
ownership and block admission while they remain unresolved: a later reconciliation that finds the instance settles the row, and otherwise an operator must. When more than one launch intent is unresolved,
`apply` retries them in creation order and stops at the first intent whose age
exceeds the client-token window or the 900-second bound, whose lifetime elapsed or differs, or whose
retry is not accepted. Intents after the deciding one are not sent and retain their existing
ledger state for the next apply.

The fourth bound exists because the expiry tag and the worker's own lifetime
start at different times. The tag is frozen when the intent is admitted, and
the same client token must carry the same request, so a resume cannot
recompute it. The worker arms its lifetime when it boots. A late send
therefore leaves a tag that ends before the worker's own deadline, and
`reap --apply` could terminate the worker while a job still runs (#222). The
bound caps the intent's age: the time from the write of its row (`created_at`)
to the moment that `apply` checks it. The bound leaves room for a resume
in a later pass after an uncertain send (one timer interval plus a full pass
deadline, under six minutes). An intent exactly 900 seconds old is still
sent; only a greater age is refused, with `unresolved_intent_lifetime_short`.

The bound is not a hard cap on the lifetime that a late tag takes from the
worker. `apply` computes the expiry once, when it admits a batch of launches,
and every row of the batch freezes that value, but each row is written later,
after the requests of the workers before it. The tag therefore ends before the
worker's own deadline by the sum of three times: the intent's age at the resume
check (at most 900 seconds), the time from the batch's expiry basis to the
row's write, and the time from that check until the worker boots, which
includes the send itself. Only the first of them is bounded by this check, so
the loss can exceed 900 seconds.

An intent with no confirmed instance that is older than 900 seconds stops
admission while it remains unresolved. A controller outage longer than fifteen
minutes can leave an intent that old, but no outage is needed: an uncertain send
leaves the intent unresolved, and while the controller keeps running, each later
pass that ends uncertain again leaves it unresolved, so its age keeps growing
until it passes 900 seconds. Reconciliation runs before the resume on every pass,
so a later `apply` that finds the intent's instance in the provider settles the
row and admits again on demand. There is no command that resolves one otherwise.
`apply` refuses every pass on its own; keep cleanup running, do not edit or
delete the row, and escalate if provider evidence does not resolve it: an empty
owned inventory does not prove that its request was never sent
([container.md § Rollback](container.md#rollback), step 4, covers an unresolved
intent).

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
| `refused` | `["unresolved_intent_lifetime_mismatch"]` | `null` | absent | from the operation | `[]` |
| `refused` | `["unresolved_intent_lifetime_short"]` | `null` | absent | from the operation | `[]` |

Every record also carries `operation`. The top level carries the deciding
intent's `operation`, `decision`, `expires_at` and `reservation`; its
`instance_id` appears only when that intent was confirmed, its `reasons` only for
the four refusals above, and its `refusal` only for an uncertain retry. The top
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
with `SCOPE_CHANGED` while any outstanding operation belongs to another Region,
another account or another deployment; an inventory from a different scope can
never establish that such an operation terminated. The deployment is read from
the frozen request's `ci-burst:deployment` tag, so changing `deployment` in the
configuration while any operation is nonterminal is an explicit refusal, not a
resume: the frozen request, its hash, its token and its tags stay untouched, the
operation keeps its recorded state, and the discrepancy payload names the
recorded `deployment` without printing the frozen payload or a secret. A row
with no readable deployment tag is unbound, because the controller never writes
one. `plan` performs the same Region and deployment checks without an AWS call.
Resolve or clean up the outstanding operation in its original scope before
changing the credential route or the deployment name.

Terminal and failed rows are history and settled cost, not a scope claim. A
ledger may serve a new deployment once every row is terminal or failed
(drain-and-retain): the new deployment's operations append their own rows while
the old rows keep their state, request, hash and reservation. Do not delete a
ledger or its rows to change the name.
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

Use separate machine identities; never copy a human login cache and never place
credentials on a worker. The scaling and cleanup identities serve the two
production paths. The optional image path adds an image-build identity and a
forge token of its own.

- **Scaling identity**: read instance, template and Spot-price data; launch up
  to the configured fleet size; create the ownership tags; request termination.
  It needs no IAM administration, image creation, VPC construction or secret
  access.
- **Cleanup identity**: describe and terminate owned instances only. It must
  work without Forgejo, without the scaling database and without launch
  permission.
- **Image-build identity** (only when `[image]` is enabled): a role that a
  source identity on the scaler host assumes for one-hour sessions through an SDK
  profile. It describes instances, images, snapshots, launch templates, their
  versions and security groups, and reads the console of builders. It launches
  only the build launch template, with no instance profile. It creates, tags and
  terminates builders and images tagged for this deployment, adds versions to the
  worker launch template and changes nothing else on it, deregisters tagged
  images and deletes tagged snapshots, and reads and writes only the archive
  prefix of the staging bucket. It cannot launch a worker or set a default
  template version. Its permissions are in
  [`deploy/iam/image-build-policy.json`](../deploy/iam/image-build-policy.json).
  You apply them; the controller applies none of them.
- **Image forge token**: a dedicated read-only token, read from its own
  environment variable and used only by the image path. The token of `apply`
  (`forgejo.token_env`) is never used for it
  ([deployment.md](deployment.md#the-image-token)).
- Promotion holds no identity. `image promote` is local and has no credential.

Scope resource permissions to the deployment's tags where the provider
supports it, and review the policy before a pilot. The scaling and cleanup
identities never create IAM resources, networks, images or bootstrap-secret
infrastructure. The image-build identity creates images, snapshots, an archive
object and template versions, and no IAM resource or network.

## Scheduler, cleanup and rollback

Run `apply` from an external scheduler. Schedule `reap --apply` separately,
outside the scaler's process and host failure boundary, and test it while the
scaler is stopped. A `reap` command in the repository is not evidence that this
path is deployed.

Three different times bound one admission pass:

- `controller.pass_deadline_seconds` (30..290, default 240) is the
  controller's own budget for one `apply` pass. It is a cooperative deadline
  checked between phases and before each provider read; the AWS client adds a
  connect (10 s) and read (30 s) transport timeout, which bounds a transport
  step and not a whole response. When the pass budget is reached, the
  remaining phases are deferred and the pass reports `refused` with
  `pass_deadline_exceeded` and `phases_deferred`; it does not send an expired
  intent or a stale quote, and its mutation phases are not interrupted once
  started. A call already in flight can return after the budget; the next
  check reports the overrun.
- `TimeoutStartSec=300` on the shipped apply unit is the outer systemd bound.
  It is longer than the whole accepted pass-deadline range (`30..290`), not
  only the default, so even the longest configured budget leaves headroom for
  the controller's JSON result before systemd kills the unit. With the shipped
  defaults the order is `240 < 300 < max_lifetime_seconds (3600)`. A
  deployment that needs a longer pass must raise this unit timeout;
  `pass_deadline_seconds` is not offered above `290`.
- `aws.max_lifetime_seconds` is the worker instance's own lifetime, not the
  pass duration. `apply` admits only when it is `3600`, the value that the
  worker bootstrap arms for its shutdown and its job bound, so the expiry tag
  and the cost reservation of a new worker use the bootstrap's duration (#138).
  Another value makes the controller refuse `apply` with
  `WORKER_LIFETIME_MISMATCH` before it takes the writer lock or opens the
  ledger; `reap`, `status`, `export` and `plan` keep working, so set
  the value to `3600` and admission resumes. An unresolved intent whose
  recorded admitted lifetime is another value, or that records none because it
  predates ledger schema 5, is not sent again: `apply` refuses it with
  `unresolved_intent_lifetime_mismatch` and keeps admission blocked until an
  operator resolves it, like the other unresolved-intent refusals below. The
  duration is not the start time: an unresolved intent written more than 900
  seconds before `apply` resumes it is refused the same way, with
  `unresolved_intent_lifetime_short` (#222).

`reap`, `status` and `export` keep their existing behaviour and do not take the
pass deadline. `reap --apply` runs from its own unit and timer on the cleanup
host, so expiry cleanup survives an `apply` pass that defers or a scaler that
is stopped.

Rollback order:

1. stop admission (disable the `apply` schedule) — immediate;
2. keep expiry cleanup running;
3. inspect `status` and the ledger;
4. terminate only proven owned resources and confirm their terminal state;
5. revert source through a reviewed change; do not rewrite published history.

Stopping cleanup is not part of rollback. Rolling back the image pin is a
separate, local step: see [Rolling back an image](#rolling-back-an-image).

## Status semantics

`status` reports what is known and what is not. An EC2 `running` instance is not
a registered runner, a ready runner or a successful job. When the provider is
unavailable or cleanup cannot be confirmed, status reports the unresolved
operation and failure; it never reports clean completion, a successful job or
zero residual cost. It also reports the configured `controller.max_instances`,
the anchored window's `allowance`, `spend`, `committed`, `remaining` and
`launches`, the number of `busy_instances`, and the `hourly_ceiling` at that
limit (`(max_spot_price + incidental_per_hour) × max_instances`).

## Cleanup result

`reap --apply` is the independent expiry path. It reads the owned inventory once
with the cleanup identity and terminates only an instance that is both present
in that inventory and carries a valid, non-empty, non-`pending`
`ci-burst:operation` tag and a valid, expired `ci-burst:expires-at` tag. It needs
no Forgejo token, no ledger and no launch permission, so it keeps working while
admission or the scaler host is down.

The decision names the outcome of that run:

| decision | exit | meaning |
|---|---|---|
| `planned` | 0 | dry run; nothing was requested |
| `termination_requested` | 0 | applied pass with no refusal; a request is not confirmed removal |
| `cleanup_degraded` | 1 | applied pass that refused at least one owned resource |
| `cleanup_failed` | 1 | the provider refused the termination call |
| `refused` / error document | 2 | the inventory could not be read; nothing was mutated |

A refused resource does not stop the proven expired targets from being
terminated in the same run: the run is degraded, not aborted. Malformed
ownership or expiry never authorizes termination.

Every `reap` document carries a top-level `cleanup` object built from that same
single inventory read. No additional provider call, export, ledger or Forgejo
read produces it:

| field | meaning |
|---|---|
| `inventory_verified` | `true` when the owned-instance read completed |
| `attempted_at`, `completed_at` | Unix epoch seconds on the cleanup host, before the read and after the termination attempt |
| `observed` | owned instances the read returned |
| `skipped` | records already `shutting-down` or `terminated` |
| `terminating` / `terminated` | the split of `skipped` by provider state |
| `expired` | well-formed expired candidates in this read |
| `requested` | identifiers actually sent to `terminate` (zero on a dry run) |
| `refused` | malformed ownership or expiry records |
| `denied` | requests whose termination was refused or could not be confirmed; every `ProviderRefusal`, including an uncertain transport outcome, counts here |
| `unresolved` | refused, expired and still-terminating records: not confirmed gone |

`requested` is not `terminated`. The `confirmation` sentence is emitted only
when identifiers were sent, and a later read is the only evidence of removal.
`cleanup_failed` and `unresolved` remain the conservative signals: `denied`
alone does not distinguish a provider rejection from an unconfirmed outcome.

A `cleanup` block with `inventory_verified: true` and zero counts proves only
that one complete inventory read returned no owned records for this deployment.
It is not a zero AWS bill, and it is not proof that an earlier request reached
the provider or reached a terminal state. An **absent or stale** document proves
nothing at all: journald rotation, a down cleanup host and a timer that was never
installed produce the same absence. Read that absence as unobserved, never as an
empty inventory.

## Supported limits

One repository, one Region and one ordered list of placement pools: the
availability-zone candidate list (the single-zone key is the one-candidate form),
optionally multiplied by an ordered instance-type list of at most four same-shape
types, up to `controller.max_instances` (1..12) owned instances, one writer and
one ledger. Ordered fallback is supported only for a definite insufficient
capacity rejection, within one admission and at most once per configured pool. Not
supported: concurrent controllers, multiple ledgers, warm pools, On-Demand
fallback for workers, automatic zone or Region discovery and substitution,
provider selection, job retries, workflow rewrites and secret distribution.
Image construction is supported only through the optional `image` commands: one
repository, one branch, one build at a time, and a candidate that a person
promotes. Automatic promotion is not supported, and this repository ships no
timer or unit for an image build.

Observation has its own bound. Its per-run console-read budget is independent of
`controller.max_instances`: a supported drain above the ceiling is reported, not
refused, and only more than 16 unfinished non-terminal candidates refuse a run.
