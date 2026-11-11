# Spec Delta

## Purpose

Control the lifecycle of a bounded fleet of owned AWS Spot runner instances
while preserving resource identity, crash recovery and Forgejo's ownership of
job results.

## ADDED Requirements

### Requirement: Explicitly pinned launch prerequisites
Live provisioning MUST use an operator-prepared launch template with an explicit numeric version and literal immutable image ID. Region and deployment identity MUST be explicit. Placement MUST come only from the configured ordered availability-zone candidate list, and a candidate MAY pin its own numeric template version. The controller MUST NOT create VPCs, subnets, security groups, IAM roles, images or bootstrap-secret infrastructure.

#### Scenario: Approved immutable template
- **WHEN** the configured template version resolves to the approved image and worker contract
- **THEN** that exact configuration is recorded in launch intent
- **AND** the launch uses the configured Region and a configured candidate without automatic substitution

#### Scenario: Candidate-specific template version
- **WHEN** a placement candidate pins its own numeric template version
- **THEN** the launch intent for that candidate records that exact version
- **AND** every candidate version used still satisfies the worker template contract

#### Scenario: Mutable or incomplete prerequisites
- **WHEN** the template uses a moving version alias, a dynamically resolved image or missing required worker settings
- **THEN** live provisioning is refused before a launch request

### Requirement: Bounded owned-instance fleet
The deployment MUST permit at most the configured `controller.max_instances`
(1..4, default 1) nonterminal owned instances or unresolved launch intents.
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

### Requirement: Ordered capacity fallback within one admission
Placement MUST be an ordered list of configured availability-zone candidates. When the provider definitely rejects a launch for insufficient capacity, and only then, the controller MAY attempt the next configured candidate in order within the same admission. It MUST NOT retry a candidate, create a second instance, or fall through for any other failure. If every candidate is rejected the slot MUST stay free with a stable reason.

#### Scenario: First candidate lacks capacity
- **WHEN** the first selected candidate is definitely rejected for insufficient instance capacity
- **THEN** the same admission attempts the next configured candidate once, in order
- **AND** the worker's operation, reservation and slot are preserved

#### Scenario: Non-capacity or uncertain provider failure
- **WHEN** the launch fails for a reason other than insufficient capacity, or its outcome is uncertain
- **THEN** the controller does not attempt another candidate
- **AND** the operation follows the existing failed or uncertain path

#### Scenario: Every candidate lacks capacity
- **WHEN** every configured candidate is definitely rejected for insufficient capacity
- **THEN** the operation is failed and the slot is free
- **AND** the queue remains unserved with a stable reason code and no reported launch

#### Scenario: Price evidence expires before the next candidate is sent
- **WHEN** the next configured candidate's Spot quote becomes older than the freshness limit while the previous attempt runs
- **THEN** that candidate is not frozen or sent
- **AND** a later candidate with fresh evidence may still be attempted
- **AND** if none remains, the operation is failed with a free slot and a stable stale-price reason, without a new admission or reservation

### Requirement: Durable idempotent launch intent
Before requesting a launch, the controller MUST durably record operation identity, frozen launch parameters, an idempotency token, ownership, expiry and the required reservation. Retries MUST reuse the same token and parameters. A definite capacity rejection MAY advance to the next ordered candidate as specified for fallback, replacing the frozen request and token together. A failed durable write MUST prevent the cloud request.

#### Scenario: Process stops before sending the request
- **WHEN** launch intent committed but the process stopped before calling AWS
- **THEN** recovery resumes the same operation
- **AND** it does not allocate a replacement operation solely because the process restarted

#### Scenario: Durable state cannot commit
- **WHEN** the state transaction fails or another writer owns the deployment
- **THEN** no launch request is sent

### Requirement: Uncertain creation remains owned
A timeout or lost launch response MUST be treated as uncertain creation rather than proof that no instance exists. Recovery MUST discover by the operation's immutable identity and reconcile the result. Changing Region, placement, template or token to bypass uncertainty MUST be forbidden; the only permitted placement change is the configured ordered fallback after a definite capacity rejection, which keeps the same operation and reservation.

#### Scenario: AWS created an instance but the response was lost
- **WHEN** a retry follows an uncertain request
- **THEN** it uses the same frozen operation and discovers the existing instance
- **AND** at most that supported operation is resumed

#### Scenario: Discovery is unavailable
- **WHEN** AWS cannot establish whether the uncertain instance exists
- **THEN** the intent and reservation remain outstanding
- **AND** no replacement launch is admitted

### Requirement: Narrow resource ownership
Every launch MUST atomically tag its deployment, operation and expiry. Discovery and cleanup MUST select only that ownership scope. Resources with missing, contradictory or foreign ownership MUST NOT be terminated by the controller.

#### Scenario: Foreign instance is visible
- **WHEN** an instance does not match the configured deployment and operation ownership
- **THEN** cleanup does not terminate that instance

#### Scenario: Tags are malformed
- **WHEN** expiry or ownership metadata cannot be validated
- **THEN** cleanup reports a refusal for that resource
- **AND** it does not guess ownership or expiry

### Requirement: Operator-owned ephemeral bootstrap
The approved worker image/template MUST register a repository-scoped ephemeral Forgejo runner, execute at most one job using the supported one-job mode, and request instance termination when that process exits. It MUST handle failure before job assignment as well as completed work. ci-burst MUST NOT distribute registration secrets or provisioning credentials to workers.

#### Scenario: One job completes
- **WHEN** the ephemeral runner finishes its assigned job
- **THEN** the worker exits and requests instance termination
- **AND** the controller does not treat the runner's registration as reusable

#### Scenario: No job arrives or bootstrap fails
- **WHEN** a worker never receives work or its bootstrap process fails
- **THEN** its maximum lifetime and independent expiry cleanup still apply
- **AND** ephemeral registration alone is not considered an expiry mechanism

### Requirement: Termination requires remote confirmation
A successful termination request MUST NOT be treated as confirmed resource removal. Capacity and outstanding ownership MUST remain occupied until remote lifecycle evidence establishes termination. Template-defined disposable volumes MUST have delete-on-termination behavior; independent persistent resources are outside this launch contract.

#### Scenario: Termination was requested but the instance is still present
- **WHEN** AWS accepts termination and later reports a nonterminal state
- **THEN** the controller keeps the resource outstanding and retries reconciliation
- **AND** it does not admit a replacement into that occupied slot

#### Scenario: Cleanup is denied
- **WHEN** AWS refuses cleanup or dependent-resource disappearance is not established
- **THEN** the deployment reports an unresolved cleanup failure
- **AND** it does not report that no charged resources remain

### Requirement: Interruption does not authorize fallback or job success
The lifecycle MUST tolerate Spot loss without an interruption notice. It MUST NOT fall back to On-Demand, change the requested resource class or manufacture a CI result; the only permitted fallback is the configured ordered availability-zone list. A later launch MUST require fresh runnable demand and normal admission checks.

#### Scenario: Abrupt Spot loss
- **WHEN** an owned instance disappears while work was running and no notice was received
- **THEN** recovery reconciles the instance's terminal state
- **AND** Forgejo retains the job result and retry decision

#### Scenario: Spot capacity is unavailable
- **WHEN** the approved Spot request cannot obtain capacity in the selected candidate
- **THEN** the controller may try the next configured candidate within the same admission
- **AND** it does not launch an On-Demand instance, an unconfigured placement or a second instance
- **AND** if every candidate fails it reports the failure with a free slot rather than a success
