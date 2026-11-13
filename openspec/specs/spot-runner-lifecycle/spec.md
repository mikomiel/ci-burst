# spot-runner-lifecycle Specification

## Purpose
Control the lifecycle of a bounded fleet of owned AWS Spot runner instances
while preserving resource identity, crash recovery and Forgejo's ownership of
job results.

## Requirements

### Requirement: Explicitly pinned launch prerequisites

Live provisioning MUST use an operator-prepared launch template with an explicit numeric
version and literal immutable image ID. Region and deployment identity MUST be explicit. A
candidate MAY pin its own numeric template version. The controller MUST NOT create VPCs,
subnets, security groups, IAM roles, images or bootstrap-secret infrastructure.

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

### Requirement: Ordered capacity fallback within one admission

When the provider definitely rejects a launch for insufficient capacity, and only then,
the controller MAY attempt the next configured pool in order within the same admission. It
MUST NOT retry a pool, create a second instance, or fall through for any other failure. If
every pool is rejected the slot MUST stay free with a stable reason.

#### Scenario: First candidate lacks capacity
- **WHEN** the first selected pool is definitely rejected for insufficient instance capacity
- **THEN** the same admission attempts the next configured pool once, in order
- **AND** the worker's operation, reservation and slot are preserved

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

#### Scenario: A rotated worker is rejected for capacity
- **WHEN** a worker's rotation starts inside the preferred type and that pool is definitely rejected for insufficient capacity
- **THEN** the same worker tries the remaining zones of the preferred type before any later type
- **AND** the frozen request advances within the same operation and reservation

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
A timeout or lost launch response MUST be treated as uncertain creation rather than proof that no instance exists. Recovery MUST discover by the operation's immutable identity and reconcile the result. Changing Region, placement, template or token to bypass uncertainty MUST be forbidden; the only permitted placement change is the configured ordered pool fallback after a definite capacity rejection, which keeps the same operation and reservation.

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

### Requirement: Every placement candidate pins its launch prerequisites

Placement MUST come only from the configured ordered placement pools: the ordered
availability-zone candidate list, multiplied by the optional ordered instance-type list
when one is configured. Every configured type MUST have the template type's exact vCPU
count and memory size, and the template image MUST support its architecture.

#### Scenario: Malformed instance-type list
- **WHEN** the configured type list is empty, repeats a type, names a malformed type, or exceeds the supported number of types
- **THEN** the configuration is refused with a stable reason
- **AND** no launch plan is built from a partial list

#### Scenario: Configured type has a different shape or architecture
- **WHEN** a configured type differs from the launch template's type in vCPU count or memory size, or the template image does not support its architecture
- **THEN** the operator prerequisite check reports drift before the list is enabled
- **AND** the controller does not silently launch a different resource shape

### Requirement: Placement order is type-major

Placement MUST be an ordered list of configured pools, each combining one configured
availability-zone candidate with one configured instance type. The order MUST be
type-major: every candidate of the first configured type, then every candidate of the
next, and the order MUST be treated as the priority.

#### Scenario: The first type is exhausted in every zone
- **WHEN** every zone of the first configured type is definitely rejected for insufficient instance capacity
- **THEN** the same admission attempts the first configured candidate of the next type once, in order
- **AND** the worker's operation, reservation and slot are preserved

### Requirement: Batch spread stays inside the first eligible type

When an instance-type list is configured, a batch MUST spread its workers across the first
eligible type's zones and MUST NOT spread them across types: a later type MUST be reached
only by a definite capacity rejection, never by batch spread. When no list is configured,
each candidate's type is its own template version's type and the batch MAY rotate across
the whole eligible candidate list, preserving the original behaviour.

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

### Requirement: Absence is proven per identifier

The controller MUST establish absence one identifier at a time. When the
provider reports that any identifier in a by-ID set is unknown, the controller
MUST re-read each identifier on its own and MUST record absence only for an
identifier whose own read reports it unknown. A provider read failure MUST
refuse the reconcile without settling an operation, releasing a slot or
releasing a commitment.

#### Scenario: Mixed missing and live batch

- **WHEN** one by-ID batch read is rejected because one identifier is unknown and
  another identifier in the same batch is still running
- **THEN** only the identifiers proven absent are treated as absent
- **AND** the live operation stays nonterminal and keeps its slot

#### Scenario: Live identifier without ownership tags

- **WHEN** a by-ID read resolves a still-live instance whose ownership tags are
  missing
- **THEN** the operation is reconciled to its live state
- **AND** an ownership discrepancy is reported instead of a clean result

#### Scenario: Provider read failure

- **WHEN** reading one identifier fails for a reason other than the provider
  reporting it unknown
- **THEN** the reconcile refuses as uncertain
- **AND** no operation is settled and no slot or commitment is released

#### Scenario: Genuinely aged-out identifier

- **WHEN** a single identifier is proven absent and the retained-visibility
  window has elapsed
- **THEN** the operation keeps the existing conservative settlement and
  visibility policy
- **AND** its slot is released only by that policy

### Requirement: Frozen intent carries its deployment scope
Every nonterminal operation MUST be bound to the deployment in its frozen request's `ci-burst:deployment` tag. `apply` MUST refuse with `SCOPE_CHANGED` before resuming or expiring an operation whose recorded deployment is non-null and differs from the configured one, with no `RunInstances` call and no change to the request, hash, token or tags. `plan` MUST do the same without an AWS call. A row without a readable tag MUST be unbound. Terminal rows MUST NOT refuse a new deployment.

#### Scenario: Deployment name changes while an intent is outstanding
- **WHEN** a nonterminal operation's frozen request names deployment A and the configuration now names deployment B
- **THEN** `apply` refuses with `SCOPE_CHANGED` and `owned_state_discrepancy`
- **AND** no `RunInstances` request is sent
- **AND** the operation stays in its recorded state with the same request, hash and client token

#### Scenario: The refusal names the mismatch safely
- **WHEN** `apply` or `plan` refuses because of a deployment-scope change
- **THEN** the report names the recorded deployment and the operation
- **AND** it does not print the frozen launch payload, its tags or a secret

#### Scenario: Same deployment resumes idempotently
- **WHEN** the configured deployment equals the deployment in the frozen request
- **THEN** `apply` resumes the same request and client token
- **AND** it records one operation row and sends one retry

#### Scenario: A drained ledger is reused with its history retained
- **WHEN** every row in the ledger is terminal or failed and the configured deployment differs from the rows' recorded deployment
- **THEN** `apply` launches normally under the new deployment
- **AND** the old rows keep their state, request, hash and reservation

### Requirement: Batch headroom is reported per planned launch
`budget.remaining_after` MUST report the allowance left after the planned batch: `allowance - committed - launch_count * per_launch_reservation`. It MUST subtract the reported `launch_count`, including zero, and MUST NOT change `affordable`, `launch_count` or admission. The value is planned headroom, not proof of completed launches.

#### Scenario: Zero workers planned
- **WHEN** admission plans no worker because the allowance cannot fund one
- **THEN** `remaining_after` equals `remaining`
- **AND** no reservation is subtracted

#### Scenario: One worker planned
- **WHEN** admission plans one worker
- **THEN** `remaining_after` subtracts one per-launch reservation

#### Scenario: Several workers planned
- **WHEN** admission plans several workers within the allowance
- **THEN** `remaining_after` subtracts one reservation per planned worker

#### Scenario: The allowance caps the batch
- **WHEN** the allowance admits fewer workers than the demand
- **THEN** `remaining_after` subtracts only the admitted count
- **AND** admission still launches exactly that count

### Requirement: Inventory responses are parsed or refused

An inventory response that cannot be parsed as a complete result MUST be refused as uncertain and MUST NOT be reported as a complete empty inventory. The response MUST contain a `Reservations` list; every reservation MUST be a mapping with an `Instances` list; every instance entry MUST be a mapping carrying a non-empty string `InstanceId`. An empty `Reservations` list MUST remain a valid, complete empty inventory.

#### Scenario: Malformed inventory
- **WHEN** `DescribeInstances` returns a response that is missing a `Reservations` list, contains a malformed reservation or entry, or names an entry without an `InstanceId`
- **THEN** the read is refused with `AWS_INVENTORY_INCOMPLETE` as an uncertain outcome
- **AND** no operation is settled and no replacement launch is admitted

#### Scenario: Genuinely empty inventory
- **WHEN** `DescribeInstances` returns an empty `Reservations` list
- **THEN** the inventory is a complete empty result
- **AND** the controller applies its normal absent-instance retention rules

### Requirement: One-job process status is preserved through cleanup
The bootstrap MUST capture the one-job runner process's exit status before its unconditional cleanup, emit exactly one bounded marker naming that status, and exit with it, so the console exit marker reports the real process status. A completed process (status 0), a failure (any other status) and a timeout (124 or 137) MUST be distinguishable. Token cleanup, registration-file removal, sync and shutdown MUST run on every path, including a failed or timed-out process.

#### Scenario: The one-job process completes
- **WHEN** the runner process exits 0
- **THEN** the bootstrap emits one completed marker with status 0 and exits 0
- **AND** the console exit marker reports the same status

#### Scenario: The one-job process fails
- **WHEN** the runner process exits with a nonzero status that is not a timeout
- **THEN** the bootstrap emits one failed marker with that status and exits with it
- **AND** token cleanup, registration-file removal, sync and shutdown still run

#### Scenario: The one-job process times out
- **WHEN** the lifetime `timeout` ends the runner process with 124 or 137
- **THEN** the bootstrap emits one timeout marker with that status and exits with it
- **AND** token cleanup, registration-file removal, sync and shutdown still run
