# Spec Delta

## Purpose

Control the lifecycle of one owned AWS Spot runner instance while preserving
resource identity, crash recovery and Forgejo's ownership of job results.

## ADDED Requirements

### Requirement: Explicitly pinned launch prerequisites
Live provisioning MUST use an operator-prepared launch template with an explicit numeric version and literal immutable image ID. Region and deployment identity MUST be explicit. The controller MUST NOT create VPCs, subnets, security groups, IAM roles, images or bootstrap-secret infrastructure.

#### Scenario: Approved immutable template
- **WHEN** the configured template version resolves to the approved image and worker contract
- **THEN** that exact configuration is recorded in launch intent
- **AND** the launch uses the configured Region without automatic substitution

#### Scenario: Mutable or incomplete prerequisites
- **WHEN** the template uses a moving version alias, a dynamically resolved image or missing required worker settings
- **THEN** live provisioning is refused before a launch request

### Requirement: One owned instance in the initial version
The initial version MUST permit at most one nonterminal owned instance or unresolved launch intent per deployment. Pending, starting, busy, terminating and uncertain resources MUST consume that capacity. Unsupported higher concurrency MUST be rejected rather than silently enabled.

#### Scenario: Instance is starting or termination is incomplete
- **WHEN** the deployment has a nonterminal instance or an unresolved launch
- **THEN** further demand does not create another instance

#### Scenario: Unrecorded owned resource exists
- **WHEN** remote discovery finds matching owned resources that cannot be reconciled with durable state
- **THEN** new launches are blocked
- **AND** the discrepancy is reported rather than treated as available capacity

### Requirement: Durable idempotent launch intent
Before requesting a launch, the controller MUST durably record operation identity, frozen launch parameters, an idempotency token, ownership, expiry and the required reservation. Retries MUST reuse the same token and parameters. A failed durable write MUST prevent the cloud request.

#### Scenario: Process stops before sending the request
- **WHEN** launch intent committed but the process stopped before calling AWS
- **THEN** recovery resumes the same operation
- **AND** it does not allocate a replacement operation solely because the process restarted

#### Scenario: Durable state cannot commit
- **WHEN** the state transaction fails or another writer owns the deployment
- **THEN** no launch request is sent

### Requirement: Uncertain creation remains owned
A timeout or lost launch response MUST be treated as uncertain creation rather than proof that no instance exists. Recovery MUST discover by the operation's immutable identity and reconcile the result. Changing Region, placement, template or token to bypass uncertainty MUST be forbidden.

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
The lifecycle MUST tolerate Spot loss without an interruption notice. It MUST NOT fall back to On-Demand, change the requested resource class or manufacture a CI result. A later launch MUST require fresh runnable demand and normal admission checks.

#### Scenario: Abrupt Spot loss
- **WHEN** an owned instance disappears while work was running and no notice was received
- **THEN** recovery reconciles the instance's terminal state
- **AND** Forgejo retains the job result and retry decision

#### Scenario: Spot capacity is unavailable
- **WHEN** the approved Spot request cannot obtain capacity
- **THEN** the controller reports that failure
- **AND** it does not launch an On-Demand or differently configured replacement
