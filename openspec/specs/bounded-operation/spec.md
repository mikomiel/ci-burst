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
The plan MUST report every configured availability-zone candidate in order with the Spot price used for it, or an explicit null when no price was read, and MUST identify the selected candidate. A candidate above the configured price ceiling MUST be skipped rather than treated as a fatal error while another candidate remains eligible.

#### Scenario: One candidate exceeds the price ceiling
- **WHEN** a configured candidate has a fresh quote above the configured maximum price
- **THEN** that candidate is reported as skipped with a stable reason
- **AND** the first eligible candidate in order is reported as selected

#### Scenario: No candidate is eligible
- **WHEN** no configured candidate has a usable price or placement prerequisite
- **THEN** the plan is refused
- **AND** every candidate's skip reason is reported

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
