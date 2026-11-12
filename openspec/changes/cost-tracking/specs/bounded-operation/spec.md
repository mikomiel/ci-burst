# Spec Delta

## REMOVED Requirements

### Requirement: Conservative persistent reservations
**Reason**: A reservation is a launch count in money, not spend. It is replaced
by tracked cost and a forward commitment, which keeps the allowance bound while
making the window report the money the fleet is expected to be billed.

## ADDED Requirements

### Requirement: Tracked cost from launch evidence

The controller MUST record the accepted request's Spot price, the admitted
incidental rate and buffer, and the provider's launch time, and MUST compute
tracked cost from them in exact integer micro-units. Cost MUST use a 60-second
minimum billed duration, counted once: the floored duration is the accrued
duration and the start of the remaining commitment. Cost MUST NOT exceed the
operation's stored lifetime.

#### Scenario: A worker is launched
- **WHEN** a request is accepted
- **THEN** the operation stores the accepted quote's price and the admitted incidental rate and buffer
- **AND** the first reconcile that observes the instance stores its provider launch time
- **AND** accrued cost grows with the seconds it has lived at that price

#### Scenario: A short-lived worker is settled
- **WHEN** an operation is billed for less than the one-minute minimum
- **THEN** its whole cost uses the one billed minute
- **AND** the same minute is not also charged as remaining life

#### Scenario: The price or the incidental rate was not recorded
- **WHEN** an operation has no recorded price
- **THEN** its cost uses the higher of the configured maximum and the frozen request `MaxPrice`
- **AND** a row without a recorded incidental rate uses the configured rate

#### Scenario: The accepted price was not recorded
- **WHEN** an operation has no recorded price
- **THEN** its cost is computed at the configured maximum price
- **AND** the estimate over-states rather than under-states the charge

### Requirement: Settlement

An operation that reaches a terminal state MUST be settled exactly once, and a
stored settled cost MUST NOT be rewritten by a later reconcile, retry or
configuration change. An instance that aged out unobserved MUST be settled at a
full configured lifetime at its recorded price, or at the frozen request
`MaxPrice` when none was recorded. A definite pre-launch failure MUST settle at
zero.

#### Scenario: A worker terminates
- **WHEN** a reconcile observes the instance `terminated`
- **THEN** the operation's final cost is settled once
- **AND** a later reconcile does not change the settled cost

#### Scenario: An instance ages out unobserved
- **WHEN** the provider no longer returns an instance after the retention window
- **THEN** the operation is settled at its full configured lifetime
- **AND** the settlement uses the frozen request `MaxPrice` when no price was recorded
- **AND** it is not treated as a zero-cost launch

#### Scenario: A launch is rejected before creation
- **WHEN** the provider definitely refuses the request
- **THEN** the operation settles at zero
- **AND** no instance cost is charged to the window

### Requirement: Forward cost commitment

The admission commitment MUST be tracked spend plus, for every non-terminal
operation, its worst-case remaining cost and its admitted buffer. Remaining
cost MUST be priced at no less than the accepted price and the frozen
`MaxPrice`, at the admitted incidental rate, over its durable lifetime. An
operation the upgrade marked as backfilled MUST keep its stored reservation
as a floor; a normally admitted operation MUST NOT be floored.

#### Scenario: The allowance admits fewer workers than the free slots
- **WHEN** the window's commitment plus one worst-case worker exceeds the allowance
- **THEN** the batch is capped or refused with the budget reason
- **AND** the decision reports spend, commitment and remaining allowance

#### Scenario: The ceiling is lowered while a worker is alive
- **WHEN** the configured maximum price drops below an outstanding operation's accepted price
- **THEN** its remaining commitment stays at the accepted price or the frozen request ceiling
- **AND** a lowered configured incidental rate or buffer does not change it
- **AND** the lowered ceiling cannot make another worker look affordable on the shortfall

#### Scenario: A cost setting is edited while a worker is outstanding
- **WHEN** the configured maximum price, incidental rate, buffer or lifetime changes
- **THEN** an operation already admitted keeps the basis it was admitted under
- **AND** only a later admission is priced with the new settings

#### Scenario: Restart with an outstanding operation
- **WHEN** the process restarts with a non-terminal operation
- **THEN** its accrued cost and remaining commitment are unchanged by the restart
- **AND** restart does not reset the admission allowance

### Requirement: Release of committed cost

Unspent commitment MUST be released as a non-terminal operation ages and MUST be
released when it settles. Money already spent MUST NOT be refunded, and a
completed operation MUST be charged to the window it started in.

#### Scenario: A worker settles
- **WHEN** an operation reaches a terminal state
- **THEN** its remaining commitment is released
- **AND** its settled cost stays charged to the window it started in

### Requirement: Upgrade from the reservation schema

The ledger MUST upgrade a version-1 or version-2 file in place, exactly once,
after a size-matched backup, refusing without mutation when that copy or the
parameters are unavailable. The upgrade MUST add the incidental rate, buffer
and backfill marker, keep settled costs and terminal inputs, and backfill
non-terminal rows while marking them. Read-only observation MUST accept every
earlier schema without writing; rollback MUST restore the backup.

#### Scenario: The first writable run after promotion
- **WHEN** a writable command opens a version-1 or version-2 file with the configured parameters
- **THEN** it copies the file, upgrades the schema and backfills terminal operations
- **AND** it records the configured incidental rate and buffer on non-terminal operations
- **AND** a row whose request records no price settles at the configured maximum
- **AND** the file reports the new version while the backup remains beside it

#### Scenario: The backup cannot be written
- **WHEN** the copy fails or its size differs
- **THEN** the command refuses before the first schema write
- **AND** the file stays at its original version

#### Scenario: Observation runs before the upgrade
- **WHEN** a read-only command opens a version-1 or version-2 file
- **THEN** it reports operations with cost defaults and conservative estimates
- **AND** it does not write to the file

#### Scenario: Rolling back after an upgrade
- **WHEN** the previous release must read the ledger again
- **THEN** the backup copy is restored in place of the upgraded file
- **AND** the restored file reports its original version

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
