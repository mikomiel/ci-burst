# worker-observation Specification

## Purpose
Define bounded, secret-safe evidence about what the workers this deployment
owns reported on their own instance consoles, expressed as deduplicated events
and one Prometheus textfile, so a launch that never reaches the queue is
visible as an event rather than as a stalled queue.

## Requirements

### Requirement: Provider-only bounded observation

The command MUST read evidence only from the provider console of instances that carry this
deployment's ownership tags. It MUST NOT call the queue interface, hold a queue
credential, or mutate any provider resource. It MUST bound one run at a small fixed number
of console reads and a fixed number of bytes per console.

#### Scenario: Owned instances are observed
- **WHEN** the deployment owns instances and the command runs
- **THEN** each owned instance contributes at most one console read
- **AND** a record already observed in a terminal state contributes none
- **AND** no queue request is made and no resource is created, modified or terminated

#### Scenario: The bound is exceeded
- **WHEN** more unfinished non-terminal owned instances than `MAX_CONSOLE_READS` (16) are candidates in one run
- **THEN** the command refuses with `console_read_cap_exceeded` and exit code 2
- **AND** it writes no event and no metrics file

#### Scenario: An instance ages out
- **WHEN** the provider no longer knows one of the owned instance identifiers
- **THEN** that instance is skipped without failing the run

### Requirement: Closed marker vocabulary
The command MUST recognise only a closed vocabulary: a step name from the shipped first-boot script's fixed set; the `bootstrap one-job <outcome> rc=<n>` marker with outcome `completed`, `failed` or `timeout`; the `bootstrap exiting rc=<n>` status, which now reports the one-job process's real status; and fixed failure kinds. The process marker MUST NOT be read as a Forgejo job verdict, and no console text MAY be published, so truncation MUST NOT be used as redaction.

#### Scenario: A worker registers
- **WHEN** a console contains the registration step marker
- **THEN** the instance records a registration event naming that step
- **AND** no console text is emitted

#### Scenario: A worker's one-job process ends
- **WHEN** a console contains one `bootstrap one-job completed|failed|timeout rc=<n>` marker
- **THEN** the instance records one `job-process` event naming that outcome and status
- **AND** no job verdict or console text is published

#### Scenario: An unshipped step name or unrelated text
- **WHEN** a console line names a step outside the fixed set, or matches no marker
- **THEN** it is ignored and never reaches the event stream

#### Scenario: An unshipped process outcome
- **WHEN** a console line names a one-job outcome outside `completed`, `failed` and `timeout`, or pairs an outcome with a status the bootstrap cannot emit
- **THEN** it is ignored and never reaches the event stream

### Requirement: Failure classification publishes a kind only

A failure line MUST be classified into one allowlisted kind, and the kind is
the whole contribution: the line, its fields and any truncated copy MUST NOT be
published.

#### Scenario: A worker fails before registering
- **WHEN** a console contains a failure line that matches one allowlisted kind
- **THEN** the instance records one failure event naming that kind
- **AND** the matched line is not published

### Requirement: At-least-once event delivery with a stable identity

Each observation MUST carry an identity derived from the instance and the
marker. A crash between publishing an event and persisting the dedupe record
MAY repeat that observation on the next run; it MUST NOT publish a different
identity for it.

#### Scenario: The record is lost between publication and persistence
- **WHEN** an event was published and the persisted record does not contain it
- **THEN** the next run may publish the same observation again
- **AND** both publications carry the same identity

#### Scenario: The record is lost or corrupt
- **WHEN** the persisted observation record cannot be read or is structurally invalid
- **THEN** the command replaces it, notes the rebuild in its result, and continues

### Requirement: Rebuildable dedupe state

The command MUST persist what it has already emitted, MUST prune those
per-instance records after a fixed retention, and MUST treat the record as
derived observation that never influences admission, launch or termination.

#### Scenario: The same console is read twice
- **WHEN** the command runs twice over the same console content
- **THEN** the second run emits no new event for that content

### Requirement: Stable metrics textfile

The command MUST write one Prometheus textfile atomically, MUST expose counters
that do not fall when an instance terminates or when a per-instance record
expires, and MUST expose a per-run timestamp that a consumer can use to detect a
stopped exporter. Cumulative counters MUST be persisted separately from the
expiring per-instance records.

#### Scenario: A successful run
- **WHEN** the command completes
- **THEN** the textfile names the deployment on every series
- **AND** the run timestamp reflects that run
- **AND** launches and registrations are counters

#### Scenario: A record expires
- **WHEN** a per-instance record passes the retention window
- **THEN** the record is pruned
- **AND** the cumulative counters keep the contribution it made

#### Scenario: The write fails
- **WHEN** the textfile cannot be replaced
- **THEN** the previous textfile is unchanged and the command exits 1

### Requirement: Every input is validated before it is republished

The command republishes four inputs: the instance list and its ownership tags,
the persisted observation record, the existing event file and the ledger. Each
MUST be accepted only when it matches its documented schema, and an invalid
value MUST be dropped or bucketed rather than copied into an output.

#### Scenario: A foreign ownership tag
- **WHEN** an owned instance carries an operation tag this deployment did not generate
- **THEN** the tag is omitted from events and from the rewritten record

#### Scenario: A malformed persisted identity or metadata value
- **WHEN** the record holds an identity, instance key, zone, state or exit code outside the closed vocabulary
- **THEN** that value is dropped, the record is rebuilt, and the run continues

#### Scenario: A historical event line no longer matches the schema
- **WHEN** the existing event file holds a line that does not validate against its own kind's schema, or one that belongs to another deployment
- **THEN** the line is dropped when the file is rewritten and the result notes the drop

#### Scenario: A record cannot be aged
- **WHEN** a persisted record has no usable retention timestamp
- **THEN** the record is dropped and the result notes the rebuild

#### Scenario: A ledger row carries an unknown state
- **WHEN** a ledger operation state is outside the ledger's lifecycle vocabulary
- **THEN** the row is counted under a fixed `invalid` state and the result notes it

### Requirement: Degraded sources do not fail the run

Evidence about workers MUST not depend on the ledger. When the ledger is absent
or unreadable the budget series MUST be omitted and the omission reported, and
the console evidence MUST still be produced.

#### Scenario: The ledger is unavailable
- **WHEN** the ledger cannot be opened read-only
- **THEN** the run still writes events and the remaining metrics
- **AND** the result records that ledger series were omitted

### Requirement: A record finishes only after observed termination

The read budget MUST be spent only on records that can still carry new evidence: a worker
the provider reports live or terminating is a candidate on every run, and a record is
finished only after a read that observed the provider reporting it `terminated` or
`stopped`. A console exit marker MUST NOT finish a record, because the bootstrap can
finish while the instance and its console carry on.

#### Scenario: Console exit marker is not termination
- **WHEN** a console read shows an exit marker while the provider still reports the instance live
- **THEN** that record is not finished
- **AND** the instance remains a candidate on the next run

### Requirement: Turnover defers surplus candidates rather than refusing

A finished record MUST NOT be read again, so normal turnover cannot consume the
budget, and live workers MUST be read before termination history. Surplus
candidates the run will not read MUST be deferred and reported, and MUST be read
on a later run. The deferral applies while the unfinished non-terminal candidates
fit within the per-run read budget; above it the command MUST refuse under the
refusal requirement, because live evidence cannot be covered in one run.

#### Scenario: Surplus terminal history is deferred
- **WHEN** the unfinished non-terminal candidates fit within the per-run read budget and terminal owned instances push the candidate count above it
- **THEN** the run reports `console_read_deferred` and does not refuse

#### Scenario: Turnover exceeds the per-run read budget
- **WHEN** the provider still lists terminal owned instances and the run has more
  candidates than the per-run read budget, while its unfinished non-terminal
  candidates fit within that budget
- **THEN** the run reads up to the budget and reports the deferral
- **AND** it publishes the events and metrics for the records it read
- **AND** the deferred records are read on a later run

### Requirement: Observation refuses only when owned instances exceed the limit

The read budget is independent of the admission ceiling. More non-terminal owned
instances than `controller.max_instances` MUST be reported with
`above_configured_limit`, not refused: a supported drain still writes events and
metrics. A deferred run MUST report the deferral and MUST NOT present itself as
complete. The command MUST refuse, writing no file, only when unfinished
non-terminal candidates exceed `MAX_CONSOLE_READS` (16), with
`console_read_cap_exceeded`.

#### Scenario: A supported drain is reported, not refused
- **WHEN** two non-terminal owned workers run with `controller.max_instances` set to 1
- **THEN** the decision is `reported` with two console reads and the `above_configured_limit` note
- **AND** the events file and the metrics file are both written

#### Scenario: Live workers exceed the read budget
- **WHEN** 17 unfinished non-terminal owned workers are candidates in one run
- **THEN** the command refuses with `console_read_cap_exceeded` and exit code 2
- **AND** it writes no event and no metrics file

#### Scenario: Turnover is not a bound violation
- **WHEN** the number of owned instances exceeds the configured limit but every one is reported `terminated`
- **THEN** the command does not refuse
