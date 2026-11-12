# Spec Delta

## ADDED Requirements

### Requirement: Burstable CPU-credit evidence
Plan evidence MUST report, for every candidate, whether its instance family is
burstable and the CPU-credit mode or an explicit unknown. Prerequisite evidence
MUST report the launch template's credit specification and the instance type's
burstability. Neither MUST present surplus CPU credits as bounded by the tracked
cost estimate, and the launch request MUST NOT set a credit specification. The
template's absent, recognized and unrecognized credit-specification states MUST
stay distinct.

#### Scenario: A burstable candidate is planned
- **WHEN** a candidate's family is burstable and the launch template sets no credit specification
- **THEN** the plan reports the family's default credit mode
- **AND** it reports the AWS family default as the mode's source
- **AND** it reports surplus CPU credits as unquantified
- **AND** it does not query the account-level default credit specification

#### Scenario: A non-burstable or unknown family is planned
- **WHEN** a candidate's family is known non-burstable and the template pins no mode
- **THEN** the plan reports the credit mode as not applicable
- **WHEN** the template pins a recognized mode for that family
- **THEN** the plan reports that mode with the template as its source
- **WHEN** the family is not in the supported table
- **THEN** the plan reports the credit mode as unknown rather than guessing

#### Scenario: The launch template sets a credit mode
- **WHEN** the template's `CreditSpecification` names `standard` or `unlimited`
- **THEN** the plan reports that mode with the template as its source
- **AND** the frozen launch request still carries no credit specification

#### Scenario: The launch template sets an unrecognized credit mode
- **WHEN** the template's `CreditSpecification` is present but names no recognized mode
- **THEN** the plan reports the mode as `null` with `template-unrecognized` as its source
- **AND** it does not report the family default as the mode
- **AND** it reports surplus CPU credits as unknown
- **AND** the prerequisite check reports the raw value and `credit_specification_invalid` drift

#### Scenario: The template's credit evidence cannot be read
- **WHEN** the prerequisite check cannot read the launch template
- **THEN** it reports the credit evidence as unavailable
- **AND** it does not report a pass

#### Scenario: The cost estimate is described
- **WHEN** a plan or the operations documentation describes tracked cost
- **THEN** surplus CPU credits are named as a separate residual charge
- **AND** the estimate is not described as a cap on the AWS bill

### Requirement: Present but unrepresentable credit evidence stays unknown

A launch template credit specification that is present but cannot be represented
as bounded evidence MUST stay present. A burstable candidate MUST report it as
unknown credit evidence rather than the family default, and the prerequisite
check MUST report drift rather than a pass.

#### Scenario: The launch template sets an unrepresentable credit mode
- **WHEN** the template's `CreditSpecification` is present but empty, non-printable, non-ASCII or longer than the bounded evidence allows
- **THEN** it stays present: it is unknown credit evidence, never an absent specification
- **AND** a burstable candidate's plan reports the mode as `null` with `template-unrecognized` as its source
- **AND** it does not report the family default as the mode
- **AND** it reports surplus CPU credits as unknown
- **AND** the prerequisite check reports `credit_specification_invalid` drift and does not report a pass
