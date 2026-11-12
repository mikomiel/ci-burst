# Spec Delta

## MODIFIED Requirements

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
