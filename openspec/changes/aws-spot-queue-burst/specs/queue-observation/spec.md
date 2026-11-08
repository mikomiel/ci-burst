# Spec Delta

## Purpose

Derive bounded runner demand from the configured Forgejo repository without
creating another queue or confusing dependency waiting with runnable work.

## ADDED Requirements

### Requirement: Qualified repository-scoped queue interface
The observer MUST use an explicitly qualified Forgejo runner-jobs API for one configured repository. Qualification MUST establish the response shape, runnable-job semantics and required token scope. An unsupported or unqualified interface MUST block live provisioning.

#### Scenario: Qualified interface returns runnable work
- **WHEN** the configured interface matches a validated API contract and returns a complete usable response
- **THEN** the observer derives demand only from the jobs in that repository response
- **AND** the result identifies the qualified contract used

#### Scenario: Interface cannot be qualified
- **WHEN** the response shape or runnable-job meaning is unknown
- **THEN** the observer reports an unsupported-contract refusal
- **AND** the controller does not launch an instance

### Requirement: Runnable and label-compatible demand
Demand MUST include only runnable, unassigned job attempts whose complete required label set is supported by the configured worker. Workflow age, pull-request count, blocked dependencies and the existence of a queued run MUST NOT substitute for runnable demand.

#### Scenario: Runnable matching attempt
- **WHEN** an unassigned runnable attempt requires labels supported by the configured worker
- **THEN** the attempt contributes to demand

#### Scenario: Dependency waiting or incompatible labels
- **WHEN** an attempt is dependency-blocked, already assigned or requires an unsupported label
- **THEN** the attempt does not contribute to launch demand
- **AND** a large aggregate queue does not override that result

### Requirement: Complete and fresh observations
The observer MUST distinguish a verified empty queue from an incomplete, stale or unreadable result. Every page required by the qualified API contract MUST be consumed. A failed observation MUST block new launches instead of reusing a previous demand value.

#### Scenario: Verified empty queue
- **WHEN** every required page is read and no eligible attempt remains
- **THEN** the observer reports verified zero demand

#### Scenario: Incomplete or failed read
- **WHEN** pagination, authentication, parsing or a required API request fails
- **THEN** the observer reports unavailable demand rather than an empty queue
- **AND** the controller does not launch from the previous snapshot

### Requirement: Attempt-aware deduplication
The observer MUST deduplicate repeated records for the same attempt using the qualified contract's stable attempt identity. A later retry MUST remain distinguishable from an earlier attempt. The controller MUST NOT promise exclusive job assignment merely because it observed that attempt.

#### Scenario: Repeated record across pages
- **WHEN** the same attempt appears more than once in a complete observation
- **THEN** it contributes at most once to demand

#### Scenario: Retry and competing assignment
- **WHEN** a retry has a different qualified attempt identity or another runner claims an observed job
- **THEN** a later observation uses the current canonical job state
- **AND** the controller does not requeue or reserve the job in Forgejo

### Requirement: Forgejo retains scheduling and result authority
The observer MUST NOT mutate workflows, dependencies, job assignment, retry policy or job results. The controller's launch decision MUST remain a capacity decision, not a second scheduler or a successful CI verdict.

#### Scenario: Demand disappears during worker startup
- **WHEN** another runner consumes the observed work before the new worker becomes ready
- **THEN** Forgejo retains the assignment decision
- **AND** the unused owned instance remains subject to its configured lifetime cleanup

#### Scenario: Worker is interrupted
- **WHEN** a worker disappears during a job
- **THEN** the controller does not mark that job successful or retry it automatically
- **AND** Forgejo and the operator's workflow retry policy retain result authority

### Requirement: Labels are routing claims rather than qualification evidence
The product MUST treat labels as configured routing inputs rather than proof of hardware, architecture or toolchain capability. The deployment MUST qualify every advertised label. Strict workload-class exclusivity MUST NOT be claimed when broader labels also let the worker accept other jobs.

#### Scenario: Configured label lacks worker evidence
- **WHEN** an operator has not qualified an advertised capability against the runner image and machine contract
- **THEN** the live-pilot acceptance gate remains open
- **AND** adding the label alone does not close that gate

#### Scenario: Broader jobs can also match
- **WHEN** the configured worker labels permit jobs outside the demand class to be assigned
- **THEN** the deployment documents that routing behavior
- **AND** exclusive burst routing requires an explicitly qualified label policy
