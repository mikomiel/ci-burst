# Spec Delta

## ADDED Requirements

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
