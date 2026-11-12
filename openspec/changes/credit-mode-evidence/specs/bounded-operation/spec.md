# Spec Delta

## ADDED Requirements

### Requirement: Burstable CPU-credit evidence
Plan evidence MUST report, for every candidate, whether its instance family is
burstable and the CPU-credit mode or an explicit unknown. Prerequisite evidence
MUST report the launch template's credit specification and the instance type's
burstability. Neither MUST present surplus CPU credits as bounded by the tracked
cost estimate, and the launch request MUST NOT set a credit specification.

#### Scenario: A burstable candidate is planned
- **WHEN** a candidate's family is burstable and the launch template sets no credit specification
- **THEN** the plan reports the family's default credit mode
- **AND** it reports the AWS default as the mode's source
- **AND** it reports surplus CPU credits as unquantified

#### Scenario: A non-burstable or unknown family is planned
- **WHEN** a candidate's family is known non-burstable
- **THEN** the plan reports the credit mode as not applicable
- **WHEN** the family is not in the supported table
- **THEN** the plan reports the credit mode as unknown rather than guessing

#### Scenario: The launch template sets a credit mode
- **WHEN** the template's `CreditSpecification` names `standard` or `unlimited`
- **THEN** the plan reports that mode with the template as its source
- **AND** the frozen launch request still carries no credit specification

#### Scenario: The template's credit evidence cannot be read
- **WHEN** the prerequisite check cannot read the launch template
- **THEN** it reports the credit evidence as unavailable
- **AND** it does not report a pass

#### Scenario: The cost estimate is described
- **WHEN** a plan or the operations documentation describes tracked cost
- **THEN** surplus CPU credits are named as a separate residual charge
- **AND** the estimate is not described as a cap on the AWS bill
