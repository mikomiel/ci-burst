# Spec Delta

## MODIFIED Requirements

### Requirement: Bounded fleet admission
The controller MUST admit at most the configured `controller.max_instances`
(1..12, default 1) concurrent owned workers. Each worker MUST have its own launch
intent, reservation, ledger operation and idempotency token, and one request MUST
create exactly one instance. A batch MUST NOT exceed the configured limit or the
remaining allowance.

#### Scenario: Demand exceeds the free slots
- **WHEN** eligible demand is larger than the configured limit minus the
  non-terminal owned operations
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
