# Spec Delta

## Purpose

Record a bounded, honest snapshot of ephemeral worker registrations against
running attempts without claiming a worker-to-job association.

## ADDED Requirements

### Requirement: Bounded registration snapshot
The controller MUST read the repository registration list at most once per apply pass, using the same owner-scoped credential as the queue read. The response MUST be validated against its own qualified contract before any count is published. A refused read MUST be reported as unavailable fleet evidence and MUST NOT refuse the pass or change the admission decision.

#### Scenario: Snapshot read succeeds
- **WHEN** the registration list matches the qualified contract
- **THEN** the result reports the ephemeral registration counts, the running attempt count and the idle estimate
- **AND** the result identifies the registration contract used

#### Scenario: Snapshot read fails
- **WHEN** authentication, transport, parsing or the response shape fails
- **THEN** the result reports an unavailable snapshot with the refusal
- **AND** the launch decision is unchanged

### Requirement: Counts are not an association
The snapshot MUST NOT state or imply that a registration belongs to a job attempt, and MUST NOT present the idle estimate as proof that a worker is idle. Running attempts MUST be counted from the queue response only, and only when their complete label set is supported by the configured worker.

#### Scenario: More attempts than registrations
- **WHEN** the queue reports more running attempts than the registration list reports active ephemeral runners
- **THEN** the result records the non-atomic difference as a note
- **AND** the idle estimate is not reported as negative

#### Scenario: Persistent runner shares the labels
- **WHEN** a non-ephemeral runner is registered with labels the queue also uses
- **THEN** it is excluded from the ephemeral registration counts
- **AND** the result does not attribute its jobs to this deployment

### Requirement: Latest snapshot is durable
The controller MUST persist one snapshot per successful pass in the ledger, keyed by observation time, and MUST prune rows older than seven days. Reading a ledger schema that predates the fleet table MUST report no snapshot rather than fail.

#### Scenario: Status reads the newest snapshot
- **WHEN** the ledger holds one or more snapshots
- **THEN** the status output reports the newest one
- **AND** the persisted counts are the ones that pass reported

#### Scenario: Older schema is read-only
- **WHEN** a version-3 ledger is opened read-only
- **THEN** the newest snapshot is absent
- **AND** the status output still reports the rest of the evidence

### Requirement: Metrics publish freshness
The exporter MUST publish the snapshot timestamp and the count gauges from the ledger without a Forgejo credential. It MUST NOT publish the count gauges for a snapshot older than fifteen minutes, and MUST report that staleness in its result.

#### Scenario: Fresh snapshot
- **WHEN** the newest snapshot is within fifteen minutes
- **THEN** the exporter publishes the active, running and idle gauges and the observation timestamp
- **AND** the result does not report the snapshot as omitted or stale

#### Scenario: Stale or absent snapshot
- **WHEN** the newest snapshot is older than fifteen minutes, absent, or the ledger cannot be read
- **THEN** only the timestamp is published when a row exists
- **AND** the result reports the snapshot as stale or omitted

### Requirement: Upgrade preserves operations
A writable run MUST upgrade a version-1, version-2 or version-3 ledger to schema 4 after a size-checked copy, MUST create the fleet table without changing existing operations, and MUST refuse before the first schema write when the copy cannot be verified.

#### Scenario: Upgrade from an earlier schema
- **WHEN** a writable run opens a version-1, -2 or -3 ledger
- **THEN** it copies the file, creates the fleet table and records schema 4
- **AND** every existing operation keeps its state and stored cost

#### Scenario: Copy fails
- **WHEN** the backup copy cannot be written or its size differs
- **THEN** the run refuses before any schema write
- **AND** the file remains readable by the release that wrote it
