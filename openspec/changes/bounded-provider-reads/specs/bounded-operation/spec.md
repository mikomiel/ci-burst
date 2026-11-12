# Spec Delta

## ADDED Requirements

### Requirement: Provider reads and the pass are bounded
Every AWS client MUST carry bounded connect and read timeouts without changing retry counts. `owned_instances`, `instances`, `spot_quote` and `console_output` MUST accept an optional deadline and MUST refuse before the next page or identifier once it is reached. One `apply` MUST bound its pass by `controller.pass_deadline_seconds` (30..840, default 240). A read called without a deadline MUST behave as before.

#### Scenario: Inventory pagination reaches the deadline
- **WHEN** the clock reaches the pass deadline between two `DescribeInstances` pages
- **THEN** the read stops before requesting the next page
- **AND** it raises the local deadline refusal and makes no further call

#### Scenario: A read has no deadline
- **WHEN** a read is called without a deadline
- **THEN** it consumes every page and identifier exactly as before

### Requirement: A slow provider produces a bounded incomplete pass
When the pass deadline is reached, `apply` MUST return `decision="refused"` with `reasons=["pass_deadline_exceeded"]` and a `phases_deferred` list naming the phases that did not complete. It MUST NOT send an expired intent or a quote the deadline interrupted, and it MUST NOT terminate an instance it did not select. A phase that has already started MUST complete.

#### Scenario: A slow quote read exhausts the pass
- **WHEN** a Spot quote read advances the clock past the pass deadline
- **THEN** `apply` refuses with `pass_deadline_exceeded`
- **AND** no `RunInstances` or `TerminateInstances` call is made
- **AND** `phases_deferred` names the phases that did not complete

#### Scenario: The deadline reaches before expiry cleanup
- **WHEN** the pass deadline is reached before `expire` starts
- **THEN** no owned instance is terminated
- **AND** the pass reports `expire` and the later phases as deferred

### Requirement: Mutations do not take the pass deadline
The deadline MUST NOT be passed into `_send`, `RunInstances` or `TerminateInstances`. A frozen intent and its client token MUST be committed before the send, so a cancellation after a possibly accepted launch keeps the existing uncertain reconciliation path and resumes with the same token.

#### Scenario: A launch may have been accepted when the pass is cancelled
- **WHEN** the process stops after `RunInstances` may have been accepted and before a response was recorded
- **THEN** the operation remains non-terminal with its frozen request and token
- **AND** the next pass resumes that request instead of allocating a replacement

### Requirement: The independent reaper is outside the pass deadline
`reap`, `status` and `export` MUST keep their existing behaviour and MUST NOT take `controller.pass_deadline_seconds`. Expiry cleanup MUST remain schedulable on its own host and timer when the scaler's pass is deferred, refuses or is stopped.

#### Scenario: The reaper runs while the scaler pass is deferred
- **WHEN** an `apply` pass ends with `pass_deadline_exceeded`
- **THEN** an independently scheduled `reap --apply` can still select and terminate an expired owned instance
- **AND** the reaper is not bounded by the scaler's pass deadline
