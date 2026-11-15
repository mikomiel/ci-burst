# Spec Delta

## MODIFIED Requirements

### Requirement: Bounded fleet admission
The controller MUST admit at most the configured `controller.max_instances`
(1..12, default 1) concurrent owned workers. Each worker MUST have its own launch
intent, reservation, ledger operation and idempotency token, and one request MUST
create exactly one instance. A batch MUST NOT exceed the configured limit, the
remaining allowance or the waiting demand that no recent launch already covers.

#### Scenario: Demand exceeds the free slots
- **WHEN** eligible demand that no recent launch covers is larger than the
  configured limit minus the non-terminal owned operations
- **THEN** the controller launches only the free slots in one batch
- **AND** each launch commits its own operation and reservation
- **AND** the next tick can fill slots that have since become free

#### Scenario: Budget permits fewer launches than free slots
- **WHEN** the remaining allowance affords fewer per-launch reservations than the
  batch would otherwise contain
- **THEN** the batch is capped at the affordable count
- **AND** the decision reports the cap and the remaining launches are not
  committed

#### Scenario: Configured limit is lowered below the live count
- **WHEN** the configured limit is lower than the number of live owned workers
- **THEN** new admission waits and reports the over-limit condition
- **AND** no running worker is terminated to satisfy the limit
- **AND** workers terminate on their own configured lifetime

## ADDED Requirements

### Requirement: Recent launches cover waiting demand for a claim grace
An owned operation in the `starting` or `running` state that was created less than `controller.claim_grace_seconds` ago is a recent launch, and each recent launch MUST cover one waiting job. An operation in any other state MUST NOT cover a job. The controller MUST measure the age from the operation's recorded creation time. An operation that has reached the grace MUST NOT cover a job, so a job that is still waiting MAY get a new worker.

#### Scenario: A booting worker covers its job
- **WHEN** one job is waiting, one owned operation in the `starting` or `running` state was created less than the grace ago, and the slots permit at least one more worker
- **THEN** the decision is `wait` with the reason `awaiting_claim` and a `launch_count` of 0
- **AND** `recent_launches` is 1
- **AND** no launch intent, reservation or instance is created

#### Scenario: A terminating worker covers no job
- **WHEN** one job is waiting, the only owned operation created less than the grace ago is in the `terminating` state, and the slots and the allowance permit at least one more worker
- **THEN** `recent_launches` is 0
- **AND** the controller requests one worker within the slot limit and the allowance

#### Scenario: A recent worker never claims its job
- **WHEN** the worker of a recent launch fails during bootstrap or never registers, and its job is still waiting
- **THEN** the operation stops covering the job when it enters the state `terminating`, reaches a terminal state or reaches the grace, whichever comes first
- **AND** the next admission MAY then launch a replacement within the slot limit and the allowance

#### Scenario: The controller restarts inside the grace
- **WHEN** the process restarts while a recent launch is younger than the grace
- **THEN** the grace is measured from the creation time recorded in the ledger
- **AND** the restart neither extends the grace nor forgets the launch

### Requirement: Admission requests only the demand that no recent launch covers
Admission MUST request at most the lesser of the free slots and the waiting jobs minus the recent launches, never fewer than zero. Every operation that is not terminal or failed still occupies a slot, whether or not it covers a job.

#### Scenario: Demand exceeds the recent launches
- **WHEN** three jobs are waiting, two owned operations in the `starting` or `running` state are recent, and the slots and the allowance permit at least one more worker
- **THEN** the controller requests one worker
- **AND** the decision reports `recent_launches` 2 beside a demand of 3

#### Scenario: Waiting jobs are fewer than the recent launches
- **WHEN** one job is waiting, two owned operations in the `starting` or `running` state are recent, and the slots permit at least one more worker
- **THEN** the decision is `wait` with the reason `awaiting_claim` and a `launch_count` of 0
- **AND** `recent_launches` is 2
- **AND** the demand left uncovered is zero, not negative

#### Scenario: An existing reason keeps precedence
- **WHEN** the ledger and the provider disagree about owned resources, no slot is free, or no job is waiting
- **THEN** the decision reports `owned_state_discrepancy`, `slot_busy` or `no_runnable_demand` as it did before
- **AND** it does not report `awaiting_claim`
- **AND** a `slot_busy` or `no_runnable_demand` decision still reports `recent_launches`

### Requirement: The claim grace is bounded
`controller.claim_grace_seconds` MUST be an integer from 60 to 1800 and MUST default to 300, equal to the claim window of the published bootstrap. A value outside that range MUST be refused as invalid configuration. The grace MUST NOT change the slot limit, the allowance or the price checks.

#### Scenario: The grace is out of range
- **WHEN** `controller.claim_grace_seconds` is below 60, above 1800 or not an integer
- **THEN** the configuration is refused with `CONFIG_INVALID` and a detail that names `controller.claim_grace_seconds`
- **AND** no launch is planned or sent

#### Scenario: The grace is not configured
- **WHEN** the deployment configuration omits `controller.claim_grace_seconds`
- **THEN** the grace is 300 seconds
- **AND** that value equals the claim window of the published bootstrap

### Requirement: A decision reports the recent launches it counted
Every decision that admission computes, whether it launches, waits or refuses for an admission reason, MUST report `recent_launches`, the number of recent launches it counted. A result that ends before admission computes a decision, such as an owned-state discrepancy found by reconciliation, an invalid state, a changed scope, a refused observation or a deadline deferral, is not required to carry `recent_launches`. The grace MUST NOT change the read-only behaviour of `plan`.

#### Scenario: Planning reports the same coverage as apply
- **WHEN** the operator runs `plan` while a recent launch exists and the ledger matches the provider inventory
- **THEN** the plan reports the same `recent_launches`, `launch_count` and reason that an `apply` would use
- **AND** no launch intent, reservation or instance is created
