# Spec Delta

## MODIFIED Requirements

### Requirement: Explicitly pinned launch prerequisites
Live provisioning MUST use an operator-prepared launch template with an explicit numeric version and literal immutable image ID. Region and deployment identity MUST be explicit. Placement MUST come only from the configured ordered placement pools: the ordered availability-zone candidate list, multiplied by the optional ordered instance-type list when one is configured. Every configured type MUST have the template type's exact vCPU count and memory size, and the template image MUST support its architecture. A candidate MAY pin its own numeric template version. The controller MUST NOT create VPCs, subnets, security groups, IAM roles, images or bootstrap-secret infrastructure.

#### Scenario: Approved immutable template
- **WHEN** the configured template version resolves to the approved image and worker contract
- **THEN** that exact configuration is recorded in launch intent
- **AND** the launch uses the configured Region and a configured pool without automatic substitution

#### Scenario: Candidate-specific template version
- **WHEN** a placement candidate pins its own numeric template version
- **THEN** the launch intent for that candidate records that exact version
- **AND** every candidate version used still satisfies the worker template contract

#### Scenario: Mutable or incomplete prerequisites
- **WHEN** the template uses a moving version alias, a dynamically resolved image or missing required worker settings
- **THEN** live provisioning is refused before a launch request

#### Scenario: Malformed instance-type list
- **WHEN** the configured type list is empty, repeats a type, names a malformed type, or exceeds the supported number of types
- **THEN** the configuration is refused with a stable reason
- **AND** no launch plan is built from a partial list

#### Scenario: Configured type has a different shape or architecture
- **WHEN** a configured type differs from the launch template's type in vCPU count or memory size, or the template image does not support its architecture
- **THEN** the operator prerequisite check reports drift before the list is enabled
- **AND** the controller does not silently launch a different resource shape

### Requirement: Ordered capacity fallback within one admission
Placement MUST be an ordered list of configured pools, each combining one configured availability-zone candidate with one configured instance type. The order MUST be type-major: every candidate of the first configured type, then every candidate of the next, and the order MUST be treated as the priority. When the provider definitely rejects a launch for insufficient capacity, and only then, the controller MAY attempt the next configured pool in order within the same admission. It MUST NOT retry a pool, create a second instance, or fall through for any other failure. When an instance-type list is configured, a batch MUST spread its workers across the first eligible type's zones and MUST NOT spread them across types: a later type MUST be reached only by a definite capacity rejection, never by batch spread. When no list is configured, each candidate's type is its own template version's type and the batch MAY rotate across the whole eligible candidate list, preserving the original behaviour. If every pool is rejected the slot MUST stay free with a stable reason.

#### Scenario: First candidate lacks capacity
- **WHEN** the first selected pool is definitely rejected for insufficient instance capacity
- **THEN** the same admission attempts the next configured pool once, in order
- **AND** the worker's operation, reservation and slot are preserved

#### Scenario: The first type is exhausted in every zone
- **WHEN** every zone of the first configured type is definitely rejected for insufficient instance capacity
- **THEN** the same admission attempts the first configured candidate of the next type once, in order
- **AND** the worker's operation, reservation and slot are preserved

#### Scenario: A rotated worker is rejected for capacity
- **WHEN** a worker's rotation starts inside the preferred type and that pool is definitely rejected for insufficient capacity
- **THEN** the same worker tries the remaining zones of the preferred type before any later type
- **AND** the frozen request advances within the same operation and reservation

#### Scenario: Preferred-type evidence expires before a worker starts
- **WHEN** an instance-type list is configured and no pool of the preferred type has fresh price evidence for the next worker
- **THEN** that worker is not started on a later type
- **AND** the batch stops with the stable stale-price reason and the already launched workers stay in place

#### Scenario: No instance-type list is configured
- **WHEN** no instance-type list is configured and two candidates' template versions pin different types
- **THEN** a batch rotates across every eligible candidate as it did before the list existed
- **AND** a capacity rejection still walks the configured candidate order

#### Scenario: A batch larger than one zone count
- **WHEN** the admitted batch is larger than the number of eligible pools of the first configured type
- **THEN** the extra workers start on that type's zones again in rotation
- **AND** no worker starts on a later type without a definite capacity rejection

#### Scenario: Non-capacity or uncertain provider failure
- **WHEN** the launch fails for a reason other than insufficient capacity, or its outcome is uncertain
- **THEN** the controller does not attempt another pool
- **AND** the operation follows the existing failed or uncertain path

#### Scenario: Every candidate lacks capacity
- **WHEN** every configured pool is definitely rejected for insufficient capacity
- **THEN** the operation is failed and the slot is free
- **AND** the queue remains unserved with a stable reason code and no reported launch

#### Scenario: Price evidence expires before the next candidate is sent
- **WHEN** the next configured pool's Spot quote becomes older than the freshness limit while the previous attempt runs
- **THEN** that pool is not frozen or sent
- **AND** a later pool with fresh evidence may still be attempted
- **AND** if none remains, the operation is failed with a free slot and a stable stale-price reason, without a new admission or reservation

### Requirement: Uncertain creation remains owned
A timeout or lost launch response MUST be treated as uncertain creation rather than proof that no instance exists. Recovery MUST discover by the operation's immutable identity and reconcile the result. Changing Region, placement, template or token to bypass uncertainty MUST be forbidden; the only permitted placement change is the configured ordered pool fallback after a definite capacity rejection, which keeps the same operation and reservation.

#### Scenario: AWS created an instance but the response was lost
- **WHEN** a retry follows an uncertain request
- **THEN** it uses the same frozen operation and discovers the existing instance
- **AND** at most that supported operation is resumed

#### Scenario: Discovery is unavailable
- **WHEN** AWS cannot establish whether the uncertain instance exists
- **THEN** the intent and reservation remain outstanding
- **AND** no replacement launch is admitted

### Requirement: Interruption does not authorize fallback or job success
The lifecycle MUST tolerate Spot loss without an interruption notice. It MUST NOT fall back to On-Demand, change the requested resource shape or manufacture a CI result; the only permitted fallback is the configured ordered pool list, and every pool MUST have the template type's vCPU count, memory size and architecture. A later launch MUST require fresh runnable demand and normal admission checks.

#### Scenario: Abrupt Spot loss
- **WHEN** an owned instance disappears while work was running and no notice was received
- **THEN** recovery reconciles the instance's terminal state
- **AND** Forgejo retains the job result and retry decision

#### Scenario: Spot capacity is unavailable
- **WHEN** the approved Spot request cannot obtain capacity in the selected pool
- **THEN** the controller may try the next configured pool within the same admission
- **AND** it does not launch an On-Demand instance, an unconfigured placement or a second instance
- **AND** if every pool fails it reports the failure with a free slot rather than a success
