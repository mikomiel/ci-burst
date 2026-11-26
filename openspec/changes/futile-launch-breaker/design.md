# Design

## Decision

`Controller._decide` gets one more gate. After the earlier wait reasons and
before the launch template is read, the controller reads the observation record
that `export` writes beside the ledger. When the three most recent finished
workers in the last 3600 seconds whose operations the ledger holds for this
deployment all ended without registering, the decision is `refused` with the reason
`recent_launches_futile`. A hold reads no template and no price and computes no
budget.

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
| Record | `observed.json` in the `export` directory beside the ledger, at `<dirname(state.database)>/export/observed.json`. `export` saves it before the textfile and advances its modification time last, on every successful run. |
| Ended | The record's provider state is `terminated` or `stopped`. These are the two states after which `export` reads the console no more, so the record is finished and its `seen` time no longer changes. |
| Registration evidence | The record shows the registration step, a step that the shipped first-boot script prints only after registration, a one-job process outcome or a Spot interruption notice. |
| Recorded | The record has an operation identifier of the shape that this deployment generates, and the ledger holds that operation for this deployment. A row is of this deployment unless its frozen request names another one. When the ledger names an instance for the operation, the record is filed under that instance. |
| Registered | Ended, recorded, shows registration evidence. |
| Futile | Ended, recorded, shows the exit marker and no registration evidence. |
| No evidence | Any other record. It is never counted. |
| Considered set | The three registered or futile workers inside the window with the newest `seen`, plus any other inside the window whose `seen` equals the third's. |
| Hold | A decision refused with `recent_launches_futile`. It needs at least three considered workers, all futile. |
| Evidence state | One of `missing`, `unreadable`, `stale`, `malformed` or `fresh`. |

## Where the evidence comes from

| Source | Verdict |
|---|---|
| The record, read from the state directory that `apply` and `export` already mount | Taken. No new call, credential, mount or schema. |
| The operations of this deployment that the pass reads from its open ledger, to confirm that a worker belongs to a recorded launch | Taken. The controller passes them to the reader as an argument. One more read of the rows on the connection that the pass has open, and no new provider call, credential or schema. |
| The operation tag on its own | Declined. `export` writes the tag of any owned instance that the provider lists, and reconciliation ignores a terminated instance that no ledger operation names. See "Why a worker needs a ledger operation". |
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
beside it, so the two cannot disagree. It also binds `export`: it must save the
record before it replaces the textfile, and advance the record's modification
time last, on every successful run, because the gate reads that time as a
heartbeat, and a change to the record's version or to a field the gate reads
must change the reader in the same change.

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
| An operation that the ledger does not hold, or holds under another instance | No evidence | The controller has no record of that launch, and reconciliation ignores the instance. |
| An operation that the ledger holds for another deployment | No evidence | A rename leaves the old rows as history, and their workers are no evidence for the new name. |

## Why a worker needs a ledger operation

`export` writes the operation tag of every owned instance that the provider lists
with a tag of the right shape. It checks the shape and nothing else. The controller
treats the same instances differently: reconciliation ignores a terminated
instance whose operation the ledger does not hold, so such an instance cannot
occupy a slot or change a decision. A gate that trusted the tag would let three
of those instances hold admission, although the controller never recorded a
launch for them.

The gate therefore counts a worker only when the ledger holds its operation for
this deployment. When the ledger names an instance for that operation, the
record must be filed under that instance. When the ledger names none, the
operation alone is enough. A record that fails the check is skipped before the
newest three are chosen, so it takes no place among them and cannot hide an older
worker that the ledger holds.

A row is of this deployment unless its frozen request names another one. The
controller reads the deployment from the `ci-burst:deployment` tag of the frozen
request, as `_deployment_changed` does for the scope check, and a row with no
readable tag is unbound and stays in, as it does there. The rule is needed because
a rename leaves the old rows in the ledger as history (the drain-and-retain rule
of the lifecycle capability), and `export` keeps the records of their workers for
seven days. Without it, three futile workers of the old name would hold the new
name for up to one window, although a drained ledger is to serve a new
deployment with normal admission. The row already stores the deployment, so the
rule adds no ledger column, record field or provider call.

The controller reads the rows once for the gate. `plan` reads them beside its
state block, and `apply` reads them at the end of reconciliation, in the same
transaction, after reconciliation has settled its rows. `_decide` passes the
operations to the reader as a required argument. The reader stays a function of
the file, the clock and that argument: it makes no call and keeps no state, and a
caller that omits the argument fails at once instead of counting every record.

The check fails open. An empty ledger, or a ledger that `plan` cannot find, holds
no operation, so the gate counts no worker and stays open.

## The clock

| Question | Decision | Why |
|---|---|---|
| What orders and ages a record? | `seen`. | It stops changing when the record finishes, because `export` never reads a finished record again. Every loaded record has one, because `export` drops a record without a usable `seen`. `first_seen` can be null, and the ledger's creation time needs a join that a record without an operation cannot make. |
| Is a window edge inside? | Yes. An age of at most 3600 seconds is inside. | One rule for both edges. |
| What does a tie do? | The considered set takes every worker equal to the third. | The verdict never depends on opaque instance identifiers, and a tie that hides a registered worker opens the gate. |
| How is freshness judged? | By the file's modification time, at most 600 seconds old. | Version 2 of the record has no generation time. `export` saves the file before the textfile, under the modification time of the record that it read, or the epoch when it read none, and advances that time as its last step, after the event file (when the run has events to add or the file exists) and the textfile, on every successful run, so the time is a heartbeat of the command's own work, and a refused, failed or stopped run lets it age. The heartbeat does not cover the publication of the textfile into the collector's directory, which the unit does afterwards (see "Failure behaviour"). The reader takes the time from the open descriptor, so a replacement by `export` cannot pair one file's time with another file's content. It reads the controller clock after that, before it reads the content, and judges the age, the date of each record and the window against that one reading: a reading taken before the open would see a heartbeat or a `seen` that `export` wrote in between as dated after the clock, and the hold would open. |
| What is a time after the controller clock? | The file is `stale`. A record with such a `seen` makes the file `malformed`. `export` drops such a record when it loads its own, against the clock of its run. | An age that cannot be established is not evidence. A clock that was set back leaves a finished record dated ahead, and `export` never reads a finished record again, so without the drop every file that it wrote would stay `malformed` until the clock caught up. |

A rebuilt record is stamped with the time of the run that rebuilds it. `export`
selects the owned instances that the provider still lists, which is about the last
hour of ended workers, and reads at most `MAX_CONSOLE_READS` (16, live workers
first) of their consoles in one run. It defers the surplus ended workers to later
runs while the provider still lists them. A rebuild therefore re-dates the evidence that those
reads recover and can renew a hold for up to one window.

A record dated after the clock of the run is dropped when the run loads it. The
test is the reader's own, `seen` after `now`, so a record dated at the clock
stays. The run does not clamp the date to its own clock: that would give an
observation that no console read made a fresh time, so it could take a place among
the newest three and hide older evidence. It does not keep the record either,
because the reader refuses the whole file for it. While the provider lists the
instance, the run reads its console again and dates the new record by that read.
An instance that the provider no longer lists leaves no evidence.

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
| Delete the record | The next pass sees `missing` and admits. On its next run `export` rebuilds records from the consoles that it reads within its per-run budget (`MAX_CONSOLE_READS`, 16, live workers first) among the instances that the provider still lists, and stamps them with its own time. A surplus of ended workers waits for later runs while the provider still lists them. The gate can hold again for a full window. |
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
| The file is older than 600 seconds, or dated in the future | `stale`. The reader reads none of the content, whatever the size. |
| The file is larger than 16 MiB | `malformed`, tested after the age, so a stale file is `stale` whatever its size. The reader takes the size from the open descriptor and reads no content. A file that grows past the bound after the size was taken is `malformed` too. The reader reads the descriptor itself, in chunks that the bound still allows and through no buffered stream, and stops at 16 MiB plus one byte, so it fetches no more than that from the file. A buffered read of that last byte would fetch a whole block more. `malformed` fits because the file is not a record that `export` writes within the bound, and the order of the states keeps the size test after the age test. `unreadable` would say that the file cannot be read, and it can. |
| The file holds `NaN`, `Infinity` or `-Infinity` | `malformed`. The decoder refuses the three constants, because they are not JSON and `export` never writes them. No field rule decides it, so a constant in a field that the gate does not read drops the file too. |
| A JSON object names a member twice, at any level | `malformed`, whatever the two values are. A decoder keeps only the last value of a repeated member, so a record with an invalid `steps` followed by a valid one would pass every field rule, and an instance key that repeats would keep only its last record, which can hide a registered worker. The decoder refuses the repeat, as it refuses the three constants, so a repeat in a field that the gate does not read drops the file too. `export` cannot write one: it serialises dictionaries whose keys are all text, and a dictionary holds a key once. |
| `export` loads a record that holds `NaN`, `Infinity` or `-Infinity`, or that names a JSON member twice | The loader decodes with the two refusals that the reader uses. They are `export`'s own and the reader imports them, so the two cannot disagree on these two refusals. The loader takes the branch for a file that does not decode: it drops the file whole, notes `state_rebuilt` and starts from an empty record. The emitted history and the cumulative counters start again, as when the record is deleted, and the run reads again the consoles that it selects within its per-run read budget (`MAX_CONSOLE_READS`, 16, live workers first). It cannot keep the last value of a repeat: that would keep a futile record in place of a registered one, skip its finished console and rewrite the file with a fresh heartbeat, so the next pass would hold on evidence that nobody read. |
| The file is not UTF-8 JSON, has another version, or holds a record with a bad field (a `seen` after the controller clock is one) | `malformed`. The whole file is dropped, because a skipped record could leave older futile records among the newest three. |
| An `instances` key that `export` rejects, or a `job_process` or `spot_interruption` value that `export` cannot write | `malformed`. The reader asks the rule that `export` applies when it loads its own record, so a record under a key that is not an instance identifier, or an empty list read as registration evidence, cannot steer the gate. |
| `export` loads a record with a registration value that it cannot keep (a `job_process` or `spot_interruption` of a shape that it does not write, a `steps` list with an element that is not text, or a `registered` flag that is not true or false), or the record of an ended worker that lacks `registered`, `steps`, `job_process` or `spot_interruption` | The loader drops that record only and notes `state_rebuilt`. It does not keep the record with the value replaced by its default, or with a default for the key. An ended worker is not read again, so the default shows no registration, and the next run would write the record back under a fresh heartbeat as a futile launch that nobody observed. While the provider still lists the instance, the run reads its console again within its per-run read budget, so the record shows what the console shows now. An instance that the provider no longer lists leaves no record and is no evidence. The cost is the cost of any dropped record: the cumulative counters count the worker again, and the run emits its markers again under their stable identities. A value that the loader keeps with the evidence it carries, such as a Spot notice that it normalises or a `steps` list from which it removes an unknown name, does not drop the record. The record of a worker that has not ended may lack a key. While the provider lists the instance, the run reads its console again within its per-run read budget, and the console fills the key. Until then the hold counts no such worker. A record file that an older version wrote lacks the keys that the version did not write yet, so the first run that loads it drops the records of the workers that have ended. |
| `export` loads a record dated after the clock of its run | The loader drops that record only, whether its worker has ended or not, and notes `state_rebuilt`. It neither clamps the date to the clock nor keeps the record. The reader refuses a whole file for such a record, and an ended worker is not read again, so a kept record would leave every new file `malformed` until the clock caught up. While the provider lists the instance, the run reads its console again within its per-run read budget, and the record is dated by that read. An instance that the provider no longer lists leaves no evidence. The cost is the cost of any dropped record, and a worker that still runs loses what earlier reads of its console found. A clock set back by more than the age of the newest records leaves every record of that span dated ahead, so one run drops them all, the records of live workers with them. |
| A record's operation is not among the ledger's operations of this deployment, or the ledger names another instance for it | The record is no evidence and takes no place among the newest three. An operation that no row holds and an operation whose row names another deployment are treated alike. An empty ledger, or none for `plan`, leaves no evidence, so the gate stays open. |
| A record's provider state is null, or text outside the known six | That record has not ended. The file stays usable. `export` writes `unknown` for a state it cannot label, turns it into null when it next loads the record, and keeps that null until the record is pruned. It replaces a bad `operation` or `exit_code` by null in the same way and keeps the record, because the reader reads null there as no evidence for that record. A registration value is the exception: see the row for a value that `export` cannot keep. |
| The file's owner-only mode denies a reader that runs as another user | `unreadable`. `export` creates the file through a temporary file, which is owner-only. |
| `export` refused its last run, or its unit stopped | The file ages and reads as `stale` after 600 seconds. |
| `export` cannot write its textfile | The run published its events and saved the record before the textfile, and it leaves the record as it saved it, under the modification time of the record that it read, or under the epoch when it read none. The record is not refreshed and reads as `stale` after 600 seconds, and until then the brake can read the content of a run that did not finish. That evidence was read, as in the case of a unit that cannot publish. When the run read its record, the next run starts from the totals of the record, so no counter falls, and the markers that the record holds are not emitted again. |
| `export` cannot read its record: a read fails, even for a moment, or the path holds a link or a FIFO | `export` reads the path once per run, without blocking and without following a link, through the opener that the reader uses. It treats such an entry as a record that does not decode: it notes `state_rebuilt`, starts from an empty record and saves a regular file in its place. A directory at the path is unreadable too, and the run then cannot save the record (see that row). The emitted history and the cumulative counters start again, as when the record is deleted, so the textfile of an earlier run can hold higher totals until a run replaces it. The run saves the new record under the epoch, so a run that then fails leaves no fresh heartbeat, and it never carries the records or the modification time of a link's target. |
| `export` cannot read an event file that exists, or finds a link, a FIFO or a directory at its path | The run fails before it replaces the event file, the record or the textfile, and exits 1, whether or not it has new events. It opens the path on every run as it opens the record, without blocking and without following a link, so a FIFO with no writer cannot stop it while it holds its locks. Only a missing file is an empty stream: a run that took an unreadable file for one would replace it with its own lines and lose the others. The record is not refreshed and reads as `stale` after 600 seconds. |
| Two `export` runs write one set of export files | Unsupported. A set of export files (the record, the event file, the metrics textfile and the collector file that the unit publishes it to) has one writer: the shipped unit `ci-burst-export.service`. A manual export of that set is `systemctl start ci-burst-export.service`; systemd runs one instance of the unit at a time, its publication step included. A `ci-burst export` command run outside the unit uses its own `--state-file`, `--events-file` and `--metrics-file` and does not write the unit's collector file. Any other sharing is unsupported, and the guarantees of this design do not hold for it. `export` creates no directory but `export` in an existing ledger directory, and refuses two outputs that name one file and an output on an entry that the lookup of the ledger, the configuration or the image state directory reads (`docs/operations.md`, "One writer"); other paths are the operator's. The directory locks (`_directory_locks`) are a guard that makes an accidental second run wait, and they attach no guarantee; a user who can read one of the directories can hold its lock and delay a run until the unit's start timeout stops it. |
| `export` cannot save the record | The run stops before it replaces the textfile, so the collector's copy and its last-run timestamp stay as they were, whether the collector reads the copy that the shipped unit publishes or the `--metrics-file` that `export` writes itself, and no textfile carries a total that the record lacks. The record keeps its modification time and reads as `stale` after 600 seconds, and the hold fails open. The command exits 1, so the failed unit is the signal. The shipped unit publishes the textfile into the collector's directory only after a successful command, so it does not publish here either. |
| `export` replaces the textfile and cannot advance the record's modification time, or is stopped before it does | The record holds the totals of that textfile, under the modification time that it had, so it reads as `stale` after 600 seconds and the hold fails open. The command exits 1 when it can. The next run starts from those totals, so no counter falls, even when a worker has left the provider's inventory in between. |
| The shipped unit cannot publish the textfile after a successful run | `export` has already saved the record and advanced its modification time, so it stays `fresh`, and a hold still rests on it, because the evidence in it was read. The collector's copy of the textfile is not replaced, so its last-run timestamp does not advance, and the failed unit is the signal. |
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
decision. For a pass that reaches new admission, `plan` reports the same hold as
`apply`, because both call `_decide`; `apply` first retries an unresolved intent,
which `plan` does not.

## Alternatives

- Make the count or the window configurable. Declined: it adds two keys, each
  with a range and a document, for values that no evidence yet tunes. A constant
  can become configuration later without breaking anyone.
- Let a registered worker that still runs clear a hold. Declined: a running record
  can still change, and mixing it in breaks the rule that evidence comes from
  finished records only. The cost is a false hold; see the evidence gaps.
- Skip an invalid record and use the rest. Declined: dropping a recent record can
  leave older futile records as the newest three.
- Keep a record whose registration value `export` cannot keep, with the value
  replaced by its default, or keep the record of an ended worker that lacks a
  registration key, with a default for the key. Declined: an ended worker is
  not read again, so the default shows no registration and the next run writes
  the record back under a fresh heartbeat as a futile launch that nobody
  observed. `export` drops the record instead (see "Failure behaviour").
- Keep that record and set its provider state to null, so that it reads as not
  ended. Declined: it adds a second repair beside the drop that `load_state`
  already has, for the same effect on the gate. A dropped record of a listed
  instance is read again, and a dropped record of an unlisted instance leaves no
  evidence.
- Count a worker by its operation tag alone. Declined: the tag proves a shape and
  not a launch that the ledger holds (see "Why a worker needs a ledger
  operation").
- Read the ledger inside `_decide` to check the operations. Declined: both
  callers already read the ledger where they read the slot, `apply` in the
  transaction of its reconciliation and `plan` beside its state block. `_decide`
  takes the result as an argument, so the decision rests on the rows of that
  moment, and `_decide` gains no ledger dependency. The cost is one more read of
  the rows on the open connection.
- Store the deployment in each record and filter on it. Declined: a new field
  changes the version of the record and the contract between `export` and the
  reader, for a fact that the ledger row already holds.
- Make the reader skip a record that is dated after its clock. Declined: the
  reader refuses the whole file for any bad record, because a skipped record can
  leave older futile records among the newest three. The fault is in the writer,
  which carried such a record forward, so `export` changes.
- Clamp the date of such a record to the clock of the run, or keep it as it is.
  Declined: a clamp gives an observation a time that no console read made, and
  keeping the record leaves the reader refusing the whole file. `export` drops
  the record, and a console read dates its replacement (see "The clock").
- Fail closed on a missing record. Declined for the reasons above.
- Order records by `first_seen`. Declined: it can be null, and it is not the
  clock that the exporter's own retention uses.
- A cool-down that `apply` persists. Declined: it adds durable state for what the
  window already derives from existing evidence.
- Write the record before the textfile under a fresh modification time, as
  `export` first did. Declined: a run that fails at its own textfile write would
  still refresh the heartbeat and keep a hold fresh while the command fails. The
  order taken saves the record before the textfile, under the modification time
  of the record that the run read, or the epoch when it read none, leaves it
  there when the textfile cannot be replaced, and advances the time last.
- Write the textfile first and the record last, as an earlier revision of this
  change did. Declined: a run that cannot save the record has already replaced
  the textfile with totals that no record holds, and a later run lowers them
  when a worker has left the provider's inventory in between, which breaks the
  contract that counters do not fall. The shipped unit hid that by publishing
  only after a successful command. A collector that reads `--metrics-file`
  itself did not have that protection.
- Save the record and replace the textfile as one step, or stage both files and
  rename them in turn. Declined: two files cannot change together, and whichever
  rename comes second can fail or be cut off. The order taken puts that cut on
  the side that a counter survives: the record ahead of the textfile.
- Keep the totals in a file of their own. Declined: it adds a second durable
  file with its own schema, loader and damage rules, for a duty that the order
  of two writes meets.
- Put the saved record back when the textfile cannot be replaced, the same bytes
  under the same times, as an earlier revision of this change did. Declined: the
  run published its events before it saved the record, so the markers that a
  put-back would emit again buy nothing. A put-back also read the record a
  second time and followed a link, so it could give a regular file the records
  and the modification time of a link's target.
- Keep the totals of a record that could not be read for a moment. Declined: a run
  that cannot read the record does not know its totals. The read failure is a
  counter reset, as for a deleted or damaged record.

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
- A record that `export` dropped, for registration evidence that it cannot keep
  or for a date after the clock of its run, is rebuilt from its console alone
  while the provider lists the instance. A registration line that has scrolled
  out of the console reads as futile, as in the flood case above. A worker that
  still runs also loses what earlier reads of its console found. A clock set back
  by more than the age of the newest records drops the records of all live
  workers at once, and the cumulative counters count each of them again.
- A record whose `state` or `operation` the loader replaced by null is not
  classified, so it takes no place among the newest three, whatever its worker
  did. When it is the one worker that registered, three older futile workers
  can hold admission for what remains of their window. That hold rests on three
  failures that `export` observed.
- A ledger operation that names no instance counts every record that carries its
  tag. The controller launches one instance for each operation, so two such
  records need a tag that the controller did not write.
- Records carry no deployment name. The gate takes the deployment from the ledger
  row of the operation, so a rename does not mix their evidence. A row with no readable deployment tag is
  unbound and counts for every name; the controller never writes such a row. The
  gate does not compare the region or the account of the row, so a worker of the
  same deployment name that ended under another region or credential route still
  counts until it leaves the window.
- Evidence lags the end of a worker by about one export interval. `export` also
  reads at most 16 consoles per run, so heavy turnover defers some records to later
  runs.
- The reader and `export` agree only through a contract test. A change to the
  record's version or fields disables the gate silently unless that test fails.
- Only the shipped first-boot script's markers are known. Another script prints
  none of them, and its workers are no evidence.
- `export` bounds the record by its seven-day retention and not by size, so the
  reader sets its own bound of 16 MiB and reads a larger file as `malformed`
  without reading it. One instance serializes to about 0.7 KiB for a typical
  record and about 1.3 KiB for one that holds every closed-vocabulary value at its
  longest, so the bound holds about 13,000 to 23,000 instances. A fleet that
  launches more than about 77 to 138 workers per hour for the whole retention
  passes it, and then every decision reports `malformed` and the hold stays off.
  That is the fail-open direction, and `launch_evidence` shows the state on every
  decision, so the loss of the gate is visible and not silent.

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
