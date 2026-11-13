# bounded-operation Specification

## Purpose
Make capacity changes explicit, bounded and observable, with conservative
admission, independent expiry cleanup and no implicit permission to spend.

## Requirements

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

### Requirement: Observation is the default
Execution without an explicit apply operation MUST remain read-only toward Forgejo, AWS and the controller's durable launch/reservation state. AWS mutation APIs with a provider dry-run flag MUST NOT substitute for this isolation.

#### Scenario: Default or explicit planning mode
- **WHEN** the operator requests a plan without applying it
- **THEN** no instance is created, tagged or terminated
- **AND** no Forgejo job or registration is changed
- **AND** no launch intent or budget reservation is committed

#### Scenario: Provider dry-run would need mutation permission
- **WHEN** the program is in observation mode
- **THEN** it does not call RunInstances or TerminateInstances even with an AWS dry-run parameter

### Requirement: Positive live admission
A live launch MUST require explicit apply, fresh qualified demand, reconciled ownership, writable durable state, a free supported slot, valid lifetime settings, at least one eligible configured placement candidate with a fresh usable price, and a sufficient configured reservation allowance. Missing or contradictory inputs MUST deny the launch.

#### Scenario: Complete live prerequisites
- **WHEN** every live-admission condition holds for the same decision snapshot
- **THEN** the controller can commit one bounded launch intent before requesting the instance

#### Scenario: Unknown or over-limit price
- **WHEN** a candidate's price is missing, stale, in the wrong scope or above its configured cap
- **THEN** that candidate is not eligible
- **AND** zero or an unrelated historical price is not substituted
- **AND** the launch is refused only when no configured candidate is eligible

### Requirement: Plan reports ordered placement candidates
The plan MUST report every configured placement pool in order — its instance type, availability-zone candidate, subnet and launch-template version — with the Spot price used for it, or an explicit null when no price was read, and MUST identify the selected pool. A pool above the configured price ceiling MUST be skipped rather than treated as a fatal error while another pool remains eligible.

#### Scenario: One candidate exceeds the price ceiling
- **WHEN** a configured pool has a fresh quote above the configured maximum price
- **THEN** that pool is reported as skipped with a stable reason
- **AND** the first eligible pool in order is reported as selected

#### Scenario: A later type is eligible while the first is not
- **WHEN** every pool of the first configured type is skipped and a pool of a later type is eligible
- **THEN** the plan reports the selected pool with its type and zone
- **AND** the launch uses that pool without changing the configured order

#### Scenario: No candidate is eligible
- **WHEN** no configured pool has a usable price or placement prerequisite
- **THEN** the plan is refused
- **AND** every pool's skip reason is reported

### Requirement: Conservative persistent reservations
The controller MUST commit the required monetary reservation before requesting a launch and preserve it through restart and uncertain cleanup. Reservation accounting MUST use exact units rather than floating-point currency. Instance termination MUST NOT automatically refund already consumed budget allowance.

#### Scenario: Restart during an outstanding operation
- **WHEN** the process restarts with a nonterminal operation or uncertain creation
- **THEN** the operation's reservation remains counted
- **AND** restart does not reset the admission allowance

#### Scenario: Cleanup is incomplete or budget is exhausted
- **WHEN** cleanup cannot be confirmed or the configured window has no admission allowance left
- **THEN** the controller does not treat that condition as fresh launch capacity
- **AND** it reports the outstanding reservation or budget refusal

### Requirement: Honest cost scope
Plans and operational documentation MUST identify what a reservation estimates and which charges it does not bound. They MUST NOT present an estimate, instance-count limit, lifetime setting or billing alert as a guaranteed cap on the AWS bill.

#### Scenario: Operator inspects a plan
- **WHEN** a plan reports a monetary reservation
- **THEN** it identifies the price scope and configured cost assumptions
- **AND** it names residual charges and cleanup-failure exposure

### Requirement: Expiry is independent of queue demand
Every owned instance MUST have a validated maximum-lifetime deadline. Expiry cleanup MUST remain possible without Forgejo access or a readable scaling ledger. Unattended deployment acceptance MUST require a separately scheduled cleanup path outside the scaling process's failure boundary.

#### Scenario: Scaler stops while an instance runs
- **WHEN** an owned instance passes its valid expiry and the scaler is unavailable
- **THEN** the independently scheduled cleanup path can select and terminate that instance using its exact ownership scope

#### Scenario: Cleanup-only mode is planned
- **WHEN** cleanup-only mode runs without explicit apply
- **THEN** it reports the expired owned resources without terminating them
- **AND** it does not query the Forgejo queue or launch replacement capacity

### Requirement: Supported writer topology is explicit
The initial release MUST support one controller deployment with one durable local ledger and one write-capable scaling process. It MUST refuse a concurrent writer on that ledger. It MUST NOT claim distributed exclusion for different hosts or independent copies of state.

#### Scenario: Second writer uses the same ledger
- **WHEN** another scaling process holds the deployment's write lock
- **THEN** the second process exits without making a cloud mutation

#### Scenario: Operator considers multiple controllers
- **WHEN** controllers would use different ledgers for the same deployment identity
- **THEN** the documented supported topology excludes that configuration
- **AND** resource tags are not described as a distributed lock

### Requirement: Credentials remain in their intended boundary
Credential values MUST come from the operator's configured environment/file or the AWS SDK credential chain. Provisioning credentials MUST NOT reach a worker. Bootstrap credentials MUST remain the operator-owned image/template responsibility, not controller-generated user data or command arguments.

#### Scenario: Private deployment uses machine authentication
- **WHEN** an unattended controller is prepared for a live pilot
- **THEN** it uses an approved machine-identity route
- **AND** copying a human login cache is not a supported deployment step

#### Scenario: Logs or errors contain sensitive provider data
- **WHEN** a response includes an authorization value, token, secret configuration or user data
- **THEN** status and logs omit or redact that value
- **AND** raw response/configuration/environment dumps are not emitted

### Requirement: Status describes evidence rather than aspiration
The program MUST report stable decision/refusal reasons, operation identity and unresolved resources. It MUST distinguish an EC2 running state from a ready runner and from successful job completion. An unresolved resource or unreadable provider MUST NOT produce a clean-completion claim.

#### Scenario: Instance runs but worker readiness is unproven
- **WHEN** AWS reports the instance running without qualified runner-readiness evidence
- **THEN** status reports the known instance state and the readiness gap separately

#### Scenario: Resources cannot be confirmed gone
- **WHEN** cleanup or provider readback is unavailable
- **THEN** status identifies the unresolved operation and failure
- **AND** it does not report successful cleanup, successful CI or zero residual cost

### Requirement: Provider reads and the pass are bounded
Every AWS client MUST carry bounded connect and read timeouts without changing retry counts. `owned_instances`, `instances`, `spot_quote` and `console_output` MUST accept an optional deadline and MUST refuse before the next page or identifier once it is reached. One `apply` MUST bound its pass by `controller.pass_deadline_seconds` (30..290, default 240), an accepted range below the shipped unit's `TimeoutStartSec=300`. A read called without a deadline MUST behave as before.

#### Scenario: Inventory pagination reaches the deadline
- **WHEN** the clock reaches the pass deadline between two `DescribeInstances` pages
- **THEN** the read stops before requesting the next page
- **AND** it raises the local deadline refusal and makes no further call

#### Scenario: A read has no deadline
- **WHEN** a read is called without a deadline
- **THEN** it consumes every page and identifier exactly as before

#### Scenario: A deployment needs a longer pass
- **WHEN** a deployment needs a pass longer than `290` seconds
- **THEN** it raises the unit's `TimeoutStartSec` instead of setting a larger `pass_deadline_seconds`
- **AND** a larger value is refused as invalid configuration

### Requirement: A slow provider produces a bounded incomplete pass
When the pass deadline is reached, `apply` MUST return `decision="refused"` with `reasons=["pass_deadline_exceeded"]` and a `phases_deferred` list naming the phases that did not complete, including when the read that crossed it returned no usable quote. It MUST NOT send an expired intent or a quote the deadline interrupted, and it MUST NOT terminate an instance it did not select. A phase that has already started MUST complete.

#### Scenario: A slow quote read exhausts the pass
- **WHEN** a Spot quote read advances the clock past the pass deadline
- **THEN** `apply` refuses with `pass_deadline_exceeded`
- **AND** no `RunInstances` or `TerminateInstances` call is made
- **AND** `phases_deferred` names the phases that did not complete

#### Scenario: A final admission read exhausts the pass
- **WHEN** the last admission read returns after the pass deadline with no quote, an above-cap quote or a provider refusal
- **THEN** `apply` reports `pass_deadline_exceeded` and defers `admission` and `launch` instead of returning the ordinary refusal
- **AND** no `RunInstances` or `TerminateInstances` call is made

#### Scenario: A refusal is decided before the deadline
- **WHEN** the last admission read returns before the pass deadline with no usable quote
- **THEN** `apply` returns the ordinary refusal
- **AND** the result has no `pass_deadline_exceeded` reason and no `phases_deferred`

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

### Requirement: Cleanup outcome and evidence

An applied cleanup pass that refused any owned resource MUST report
`cleanup_degraded` and exit nonzero; `termination_requested` MUST mean none was
refused. `cleanup_failed` stays the provider refusal. Malformed ownership MUST
NEVER authorize termination. The result MUST carry one `cleanup` object from the
same read with verification, clocks and observed, skipped, terminated,
terminating, expired, requested, refused, denied and unresolved counts. A
request MUST NOT count as a confirmed removal.

#### Scenario: Every candidate is refused

- **WHEN** an applied cleanup pass refuses every owned candidate and sends no
  termination request
- **THEN** the decision is `cleanup_degraded`
- **AND** the command exits nonzero
- **AND** `termination_requested` is empty and no confirmation is emitted
- **AND** the provider receives no terminate call

#### Scenario: Proven and refused resources share one inventory

- **WHEN** one owned instance has a valid expired expiry tag and another has a
  malformed tag
- **THEN** only the proven instance is terminated
- **AND** the result reports `cleanup_degraded`
- **AND** both the expired and the refused outcome are retained

#### Scenario: Inventory is verified empty

- **WHEN** the complete owned-instance read returns no records
- **THEN** the decision is `termination_requested` with an empty request list
- **AND** the cleanup block reports `inventory_verified: true` and zero counts
- **AND** the run does not claim a confirmed removal

#### Scenario: Malformed ownership or expiry

- **WHEN** an owned instance has a missing, empty, `pending` or malformed
  ownership or expiry tag
- **THEN** it is reported as refused
- **AND** no termination is requested for it

#### Scenario: Heartbeat is absent or stale

- **WHEN** no cleanup document exists for the interval, or the newest one is old
- **THEN** the cleanup path is unobserved
- **AND** that absence is not evidence of an empty inventory, a zero bill or a
  completed cleanup

#### Scenario: Delayed terminal readback

- **WHEN** a run requests termination and later inventory reads report
  `shutting-down` and then `terminated`
- **THEN** the requesting run reports a request, not a confirmed removal
- **AND** only the read that observes `terminated` reports a terminal count

#### Scenario: No confirmed removal without a provider terminal state

- **WHEN** the provider has not reported a terminal state for a requested or
  refused instance
- **THEN** the cleanup evidence keeps it unresolved
- **AND** a requested or denied termination is not counted as removed
