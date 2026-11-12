# Spec Delta

## ADDED Requirements

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
