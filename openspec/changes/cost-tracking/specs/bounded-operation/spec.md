# Spec Delta

## REMOVED Requirements

### Requirement: Conservative persistent reservations
**Reason**: A reservation is a launch count in money, not spend. It is replaced
by tracked cost and a forward commitment, which keeps the allowance bound while
making the window report the money the fleet is expected to be billed.

## ADDED Requirements

### Requirement: Tracked cost from launch evidence

The controller MUST record the accepted request's Spot price and the provider's
launch time for every operation and MUST compute tracked cost from them in exact
integer micro-units. Cost MUST use a minimum billable duration of 60 seconds and
MUST NOT exceed the configured maximum lifetime. A row without a recorded price
MUST be costed at the configured maximum.

#### Scenario: A worker is launched
- **WHEN** a request is accepted
- **THEN** the operation stores the accepted quote's price
- **AND** the first reconcile that observes the instance stores its provider launch time
- **AND** accrued cost grows with the seconds it has lived at that price

#### Scenario: The accepted price was not recorded
- **WHEN** an operation has no recorded price
- **THEN** its cost is computed at the configured maximum price
- **AND** the estimate over-states rather than under-states the charge

### Requirement: Settlement

An operation that reaches a terminal state MUST be settled exactly once and MUST
NOT be charged again afterwards. An instance that aged out unobserved MUST be
settled at a full configured lifetime; a definite pre-launch failure MUST settle
at zero.

#### Scenario: A worker terminates
- **WHEN** a reconcile observes the instance `terminated`
- **THEN** the operation's final cost is settled once
- **AND** a later reconcile does not change the settled cost

#### Scenario: An instance ages out unobserved
- **WHEN** the provider no longer returns an instance after the retention window
- **THEN** the operation is settled at its full configured lifetime
- **AND** it is not treated as a zero-cost launch

#### Scenario: A launch is rejected before creation
- **WHEN** the provider definitely refuses the request
- **THEN** the operation settles at zero
- **AND** no instance cost is charged to the window

### Requirement: Forward cost commitment

Budget admission MUST use the window's tracked spend plus the worst-case
remaining cost of every non-terminal operation plus the configured buffer, and
MUST price one newly admitted worker at the configured maximum price for its
whole lifetime. The unspent part of a non-terminal commitment MUST become
available as the operation ages and MUST be released when it settles. Money
already spent MUST NOT be refunded, and a completed operation MUST be charged to
the window it started in.

#### Scenario: The allowance admits fewer workers than the free slots
- **WHEN** the window's commitment plus one worst-case worker exceeds the allowance
- **THEN** the batch is capped or refused with the budget reason
- **AND** the decision reports spend, commitment and remaining allowance

#### Scenario: A worker settles
- **WHEN** an operation reaches a terminal state
- **THEN** its remaining commitment is released
- **AND** its settled cost stays charged to the window it started in

#### Scenario: Restart with an outstanding operation
- **WHEN** the process restarts with a non-terminal operation
- **THEN** its accrued cost and remaining commitment are unchanged by the restart
- **AND** restart does not reset the admission allowance

### Requirement: Upgrade from the reservation schema

The ledger MUST upgrade a version-1 file in place, exactly once, only after
copying it to a backup whose size matches the original. It MUST refuse without
mutating when that copy fails or the configured cost parameters are unavailable.
Read-only observation MUST accept the previous schema and MUST NOT create,
migrate or replace the file. An unsupported schema version MUST be refused.

#### Scenario: The first writable run after promotion
- **WHEN** a writable command opens a version-1 file with the configured parameters
- **THEN** it copies the file, upgrades the schema and backfills terminal operations
- **AND** the file reports the new version while the backup remains beside it

#### Scenario: The backup cannot be written
- **WHEN** the copy fails or its size differs
- **THEN** the command refuses before the first schema write
- **AND** the file stays at its original version

#### Scenario: Observation runs before the upgrade
- **WHEN** a read-only command opens a version-1 file
- **THEN** it reports operations with cost defaults and conservative estimates
- **AND** it does not write to the file

## MODIFIED Requirements

### Requirement: Honest cost scope
The controller and its documentation MUST identify what tracked cost and the
forward commitment estimate, and which charges they do not bound. They MUST NOT
present an estimate, instance-count limit, lifetime setting or billing alert as a
guaranteed cap on the AWS bill.

#### Scenario: Operator inspects a plan
- **WHEN** a plan reports tracked cost or a commitment
- **THEN** it identifies the price scope and the configured assumptions
- **AND** it names residual charges and cleanup-failure exposure
- **AND** it does not describe the estimate as the provider bill
