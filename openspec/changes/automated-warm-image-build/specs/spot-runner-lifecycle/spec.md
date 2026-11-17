# Spec Delta

## MODIFIED Requirements

### Requirement: Explicitly pinned launch prerequisites

Live provisioning MUST use an operator-prepared launch template with an explicit numeric
version and literal immutable image ID. Region and deployment identity MUST be explicit. A
candidate MAY pin its own numeric template version. The controller MUST NOT create or widen
VPCs, subnets, security groups, IAM, buckets, launch templates, credentials or
bootstrap-secret infrastructure. When the operator has
set `[image].enabled = true`, the controller MAY create, tag and delete worker images, their
snapshots, tagged builder instances and non-default launch-template versions, and MAY stage and
delete a source archive under an operator-provided private bucket prefix, under the
`image-build` capability. A timer MUST NOT move the production launch-template pin.

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

#### Scenario: Image creation without opt-in
- **WHEN** `[image].enabled` is absent or false
- **THEN** the controller creates no image, snapshot, builder instance, launch-template version or staged archive

#### Scenario: A created version does not become the pin
- **WHEN** an image build creates a new launch-template version
- **THEN** the controller launches workers only from the configured numeric version
- **AND** the new version is used only after a person changes the pin

### Requirement: Narrow resource ownership
Every worker launch MUST atomically tag its deployment, operation and expiry. Discovery and cleanup MUST select only that ownership scope. Resources with missing, contradictory or foreign ownership MUST NOT be terminated by the controller. A builder instance that the image build launches MUST carry its own image-build tags, MUST NOT carry the managed tag and MUST NOT be selected by worker discovery or worker cleanup.

#### Scenario: Foreign instance is visible
- **WHEN** an instance does not match the configured deployment and operation ownership
- **THEN** cleanup does not terminate that instance

#### Scenario: Tags are malformed
- **WHEN** expiry or ownership metadata cannot be validated
- **THEN** cleanup reports a refusal for that resource
- **AND** it does not guess ownership or expiry

#### Scenario: A builder is visible to worker discovery
- **WHEN** a builder exists for the deployment
- **THEN** worker discovery and `reap` do not select it
- **AND** only the image reconcile step terminates it
