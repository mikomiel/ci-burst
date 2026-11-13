# Spec Delta

## MODIFIED Requirements

### Requirement: Bounded owned-instance fleet
The deployment MUST permit at most the configured `controller.max_instances`
(1..12, default 1) nonterminal owned instances or unresolved launch intents.
Pending, starting, running, terminating and uncertain workers MUST each consume
one slot, and a value outside the range MUST be rejected. Each worker MUST have
its own admission, reservation and ledger operation, and the controller MUST NOT
terminate a running worker to enforce the limit.

#### Scenario: Instance is starting or termination is incomplete
- **WHEN** the deployment has filled every configured slot with nonterminal
  instances or unresolved launches
- **THEN** further demand does not create another instance

#### Scenario: Configured limit is lowered below the live count
- **WHEN** the configured limit is lower than the number of live owned workers
- **THEN** new admission waits and reports the over-limit condition
- **AND** no running worker is terminated to enforce the limit

#### Scenario: Capacity rejection falls through to the next candidate
- **WHEN** a definite capacity rejection advances the same admission to the next configured candidate
- **THEN** no additional instance, reservation or ledger operation is created
- **AND** the fallback uses `MinCount` and `MaxCount` of one

#### Scenario: Unrecorded owned resource exists
- **WHEN** remote discovery finds matching owned resources that cannot be reconciled with durable state
- **THEN** new launches are blocked
- **AND** the discrepancy is reported rather than treated as available capacity
