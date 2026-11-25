# Design

## Decision

`Controller._decide` gets one more gate. After the earlier wait reasons and
before the launch template is read, the controller reads the observation record
that `export` writes beside the ledger. When the three most recent finished
workers in the last 3600 seconds all ended without registering, the decision is
`refused` with the reason `recent_launches_futile`. A hold reads no template and
no price and computes no budget.

The gate can only withhold a launch. It cannot add one, raise a count or relax a
prerequisite. The count of three, the window of 3600 seconds and the staleness
bound of 600 seconds are constants, not configuration.

## The loop

The bootstrap stops on its first error, and its exit path shuts the instance
down. A worker that cannot register therefore ends within minutes, and its
operation leaves the `starting` and `running` states. The claim grace covers a
waiting job only while an operation is in those states, so it stops covering the
job. The slot is free, the job still waits, and the next pass launches again.
Nothing in `_decide` looks at how earlier workers ended.

## Terms

| Term | Meaning |
|---|---|
| Record | `observed.json` in the `export` directory beside the ledger, at `<dirname(state.database)>/export/observed.json`. `export` writes it last, on every successful run. |
| Ended | The record's provider state is `terminated` or `stopped`. These are the two states after which `export` reads the console no more, so the record is finished and its `seen` time no longer changes. |
| Registration evidence | The record shows the registration step, a step that the shipped first-boot script prints only after registration, a one-job process outcome or a Spot interruption notice. |
| Registered | Ended, has an operation identifier, shows registration evidence. |
| Futile | Ended, has an operation identifier, shows the exit marker and no registration evidence. |
| No evidence | Any other record. It is never counted. |
| Considered set | The three registered or futile workers inside the window with the newest `seen`, plus any other inside the window whose `seen` equals the third's. |
| Hold | A decision refused with `recent_launches_futile`. It needs at least three considered workers, all futile. |
| Evidence state | One of `missing`, `unreadable`, `stale`, `malformed` or `fresh`. |

## Where the evidence comes from

| Source | Verdict |
|---|---|
| The record, read from the state directory that `apply` and `export` already mount | Taken. No new call, credential, mount or schema. |
| A signal that `export` writes into the ledger | Declined. The ledger has one writer by contract, and `export` opens it read-only. |
| A console read inside `apply` | Declined. It adds provider calls and read time to a pass that a deadline bounds, and repeats what `export` already does. |
| A lifetime rule: a worker that lived less than some minutes | Declined. It cannot tell a futile launch from a short job. |
| Forgejo registration counts | Declined. The fleet snapshot counts registrations and never ties one to a worker. |

## Why the record may now influence admission

The worker-observation capability says the record never influences admission.
That sentence keeps a derived, lossy source away from admission. This change
keeps the protection where it matters and makes one exception. The record can
only withhold a launch, and only when it is fresh, valid and unanimous.
Everything that authorizes a launch still comes from the queue, the ledger, the
provider and the price read.

The delta modifies the existing requirement instead of adding a second rule
beside it, so the two cannot disagree. It also binds `export`: it must write
the record last, on every successful run, because the gate reads the file's
modification time as a heartbeat, and a change to the record's version or to a
field the gate reads must change the reader in the same change.

## Relation to the open `bounded-idle-workers` change

That change declines to read console markers during admission, for two reasons:
the record is derived data that must never influence admission, and the console
is not a trusted channel. This change reads the record at admission, so it
reverses the first reason for one case and keeps the second.

The second reason holds in full. The gate takes only closed-vocabulary facts that
`export` has already validated, never console text, and it treats a forged or
lost marker as a cost to bound (see "Trust and credentials"). The first reason is
sound for any use of the record that could raise or steer a launch. A one-way
brake is not such a use. It only withholds a launch, so a wrong record delays
capacity and never creates it. Where both changes land, the declined alternative
there is superseded for this one gate and for no other use of the record.

That change's worker-observation delta adds the step marker for a claim timeout
and counts a worker that shows it as registered. This gate agrees: a worker with
a later step marker is registered, not futile.

## Why a missing record does not block admission

`AGENTS.md` says: "Unknown queue, price, ownership or state blocks new
capacity." That rule covers inputs that authorize a launch. If one is unknown,
nothing may launch. The record authorizes nothing. It can only withhold.

A block on an unknown record would also make the optional `export` unit
mandatory. A stopped unit would then stop every launch, and an observation fault
would become an outage. That is the larger risk. The configured allowance still
limits what a wasted launch can cost.

So the record is not an input to the existing "Positive live admission"
requirement. The added requirement says so, and says every listed prerequisite
still applies to a launch that the gate lets through. Each decision that reaches
the gate reports the evidence state, so a silent loss of the gate shows in the
decision document.

## Why futile needs the exit marker

A record with no registration marker does not prove that the worker never
registered. A console read keeps only the most recent output, and the runner's
own output goes to that console, so the registration line can scroll out before
the first read. The issue says this. A rule on the missing marker alone would
count that worker as futile.

Two facts narrow the rule. The bootstrap's exit path prints the exit marker at
the end of the script, so a record that shows it was read after the script
reached its end. And the later steps, the one-job outcome and the interruption
notice are printed only after registration, so a record that shows any of them
proves that the worker registered, even when the registration line is gone. A
worker is futile only when the exit marker is present and none of those later
markers is.

The cost is a narrower gate. A worker that ended without an exit marker and
without registration evidence, such as an empty console, a hard kill before
registration or a power-off, is no evidence and never futile. A worker that ended
without an exit marker but shows registration evidence is registered and can
clear a hold. A custom first-boot script that prints no exit marker never trips
the gate.

| The record shows | Class | Why |
|---|---|---|
| Ended, exit marker, no registration evidence | Futile | The script ran to its end and never registered. |
| Ended, registration step | Registered | The worker reached the register step. |
| Ended, a later marker, no registration step | Registered | The registration line scrolled out, and a later marker proves it ran. |
| Ended, no exit marker, no registration evidence | No evidence | The console may not reach the end of the script. |
| Not ended | No evidence | The record can still change. |
| No operation identifier of this deployment | No evidence | It names no launch that this controller made. |

## The clock

| Question | Decision | Why |
|---|---|---|
| What orders and ages a record? | `seen`. | It stops changing when the record finishes, because `export` never reads a finished record again. Every loaded record has one, because `export` drops a record without a usable `seen`. `first_seen` can be null, and the ledger's creation time needs a join that a record without an operation cannot make. |
| Is a window edge inside? | Yes. An age of at most 3600 seconds is inside. | One rule for both edges. |
| What does a tie do? | The considered set takes every worker equal to the third. | The verdict never depends on opaque instance identifiers, and a tie that hides a registered worker opens the gate. |
| How is freshness judged? | By the file's modification time, at most 600 seconds old. | Version 2 of the record has no generation time. `export` writes the file last, after the event file and the textfile, on every successful run, so the time is a heartbeat, and a refused, failed or stopped run lets it age. The reader takes the time from the open descriptor, so a replacement by `export` cannot pair one file's time with another file's content. |
| What is a time after the controller clock? | The file is `stale`. A record with such a `seen` makes the file `malformed`. | An age that cannot be established is not evidence. |

A rebuilt record is stamped with the time of the run that rebuilds it. `export`
reads again every owned instance that the provider still lists, which is about the
last hour of ended workers. A rebuild therefore re-dates that evidence and can
renew a hold for up to one window.

## What the gate does under a persistent fault

At one slot, the first three launches run before the gate has three records, and
a fourth can follow when evidence lags. Then the gate holds until the oldest of
the three newest records is older than 3600 seconds. At that moment the gate opens
and one launch is admitted. That worker fails too, its record replaces the one
that aged out, and the gate holds again. The steady state is about three launches
per hour. The fault is thinned and not stopped.

With more free slots, a batch has no evidence before it launches. The whole batch,
limited by the free slots, the demand and the allowance, goes out each time the
gate opens. The gate is not a cap on wasted launches. It lowers their rate. It is
not a spend control either. The configured allowance, which is an estimate and not
an AWS billing cap, remains the only one.

## Recovery

Recovery is automatic and has three triggers: the window passes, a registered
worker joins the three newest, or the record stops being fresh. A hold lasts at
most 3600 seconds after the newest futile worker was read. The first pass that
finds the gate open launches as any other pass does.

No launch happens during a hold, so a fix to the fault produces no new evidence.
After the fix, admission still waits for the window. Two levers exist, and
neither is a clean resume.

| Lever | Effect |
|---|---|
| Delete the record | The next pass sees `missing` and admits. `export` rebuilds records for the instances that the provider still lists on its next run and stamps them with its own time, so the gate can hold again for a full window. |
| Stop the export timer | The record ages past 600 seconds and the gate opens. Worker observation stops too. This is the one blunt lever. |

## Trust and credentials

The gate adds no credential, no IAM permission, no mount and no network path.
`apply` and `export` already mount the same state directory, and the reader opens
one regular file in it read-only, without blocking and without following a link.

The console is untrusted, because a worker is untrusted after it accepts job code.
A worker can print any marker that the closed vocabulary accepts, and it can flood
its console. Both directions of forgery are bounded.

| Forgery | Effect |
|---|---|
| A forged registration marker | The record shows registration, so the gate stays open. That is the behaviour without any gate. |
| A suppressed registration marker: a flood before the first read, then an exit marker | The worker reads as futile. Three in a row pause admission for at most the window. |

The gate is one-way. Forgery can delay capacity. It cannot raise a launch count,
spend or access.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| No file | `missing`. Admission continues as before. |
| The file cannot be opened or read | `unreadable`. |
| The path is not a regular file, such as a FIFO or a directory, or is a link | `unreadable`. The reader opens the path without blocking and checks the type of the open descriptor before it reads, so a FIFO with no writer cannot stall a pass that holds the ledger writer lock. `export` replaces the record by a rename of a regular file, so a link is not its output. |
| The file is older than 600 seconds, or dated in the future | `stale`. |
| The file is not UTF-8 JSON, has another version, or holds a record with a bad field | `malformed`. The whole file is dropped, because a skipped record could leave older futile records among the newest three. |
| An `instances` key that `export` rejects, or a `job_process` or `spot_interruption` value that `export` cannot write | `malformed`. The reader asks the rule that `export` applies when it loads its own record, so a record under a key that is not an instance identifier, or an empty list read as registration evidence, cannot steer the gate. |
| A record's provider state is null, or text outside the known six | That record has not ended. The file stays usable. `export` writes `unknown` for a state it cannot label, turns it into null when it next loads the record, and keeps that null until the record is pruned. |
| The file's owner-only mode denies a reader that runs as another user | `unreadable`. `export` creates the file through a temporary file, which is owner-only. |
| `export` refused its last run, or its unit stopped | The file ages and reads as `stale` after 600 seconds. |
| `export` cannot write its textfile | The record is written after the textfile, so it is not refreshed and reads as `stale` after 600 seconds. Events published before the failure repeat on the next run under the same identities, and the consoles are read again, because no record of the failed run was saved. The repeats count against the event file bound, so a long outage can push older lines out of the file. |
| `export` writes its textfile and cannot write the record | The textfile carries the new run time, so an alert on a stopped exporter stays quiet, while the record is not refreshed and reads as `stale` after 600 seconds. The command exits 1, so the failed unit is the signal, and the hold fails open. |
| `export` writes to another path with `--state-file` | The default path reads as `missing` or `stale`. The reader never searches. |
| A failure the reader did not foresee | `unreadable`. The reader never raises, because a pass must not fail for an optional brake. |
| Two passes read while `export` replaces the file | Each pass sees one whole file. `export` replaces it atomically. |

## Observability

The reason appears in the decision document, and every decision that reaches the
gate carries `launch_evidence`. `docs/operations.md` lists the reason where it
lists the others. No metric series is added: `apply` publishes none, and `export`
labels its series by deployment and state only. Each held pass exits 2, as every
refusal does, so the unit records a non-zero exit while the gate holds.

`status` is a non-goal. It builds no admission decision, so it has no verdict to
report, and evaluating the gate there would add a second code path for one
decision. `plan` reports the same hold as `apply`, because both call `_decide`.

## Alternatives

- Make the count or the window configurable. Declined: it adds two keys, each
  with a range and a document, for values that no evidence yet tunes. A constant
  can become configuration later without breaking anyone.
- Let a registered worker that still runs clear a hold. Declined: a running record
  can still change, and mixing it in breaks the rule that evidence comes from
  finished records only. The cost is a false hold; see the evidence gaps.
- Skip an invalid record and use the rest. Declined: dropping a recent record can
  leave older futile records as the newest three.
- Fail closed on a missing record. Declined for the reasons above.
- Order records by `first_seen`. Declined: it can be null, and it is not the
  clock that the exporter's own retention uses.
- A cool-down that `apply` persists. Declined: it adds durable state for what the
  window already derives from existing evidence.
- Write the record before the textfile, as `export` first did. Declined: a run
  that fails at its textfile would still refresh the heartbeat and keep a hold
  fresh while the unit fails. The order taken costs a repeat: while the textfile
  cannot be written, each run reads the same consoles again and repeats its
  events under the same identities, which at-least-once delivery allows. The
  repeats count against the event file bound, so a long outage can push older
  lines out of the file, and a failure of the last write alone leaves a textfile
  that is newer than the record.

## Rollback

Revert the change and run the previous image. The gate leaves `_decide`, so the
next pass admits as it did before. The record's schema, the ledger and the worker
bytes are unchanged, so nothing needs migrating or restoring. `export` keeps
writing the same record, and nothing then reads it for admission. While the new
image runs, the blunt lever above is the only way to open the gate early.

## Evidence gaps

- No live evidence yet. Source and tests prove the shape of the gate, not a real
  hold. The operator task records one 24-hour observation on a healthy pool.
- A worker that registered, flooded its console, and then ended through the exit
  path before printing any later marker reads as futile. Three such workers as the
  newest three, with none registered, hold admission for at most one window.
  Spend is unaffected.
- A registered worker that still runs does not clear a hold. The bound is the same
  window.
- Records carry no deployment name. They live in a state directory that belongs to
  one deployment, and the controller already refuses a changed deployment scope. A
  directory shared by two deployments, or kept across a rename, would mix their
  records.
- Evidence lags the end of a worker by about one export interval. `export` also
  reads at most 16 consoles per run, so heavy turnover defers some records to later
  runs.
- The reader and `export` agree only through a contract test. A change to the
  record's version or fields disables the gate silently unless that test fails.
- Only the shipped first-boot script's markers are known. Another script prints
  none of them, and its workers are no evidence.
- The reader sets no size bound, because `export` sets none when it loads the same
  file. A fixed bound would be a judgment that can fail silently. One worst-case
  record serializes to about one KiB and the retention is seven days, so a bound of
  8 MiB would be passed at about 48 launches per hour, and a busy healthy fleet
  would then read as `malformed` for good.

## Open questions

1. Early resume after a fix. A hold ends only when the window passes. An operator
   acknowledgement would need durable state that an operator can write, a command,
   and a decision about who may clear a hold. It is not proposed. Decide after a
   real hold shows how often a fix lands inside the window.
2. Stronger registration evidence. The console is the one per-worker signal that
   `apply` can use without a new call, and it is lossy and untrusted. A signal from
   Forgejo that ties a registration to a worker would be stronger, but Forgejo
   reports no link between a worker and a job, and the fleet requirement forbids
   reading counts as an association. Revisit if the flood residue above shows up in
   practice.
