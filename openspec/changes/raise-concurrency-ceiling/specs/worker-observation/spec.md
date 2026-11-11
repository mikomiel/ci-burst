# Spec Delta

## MODIFIED Requirements

### Requirement: Provider-only bounded observation

The command MUST read evidence only from the provider console of instances that
carry this deployment's ownership tags. It MUST NOT call the queue interface,
hold a queue credential, or mutate any provider resource. It MUST bound one run
at a small fixed number of console reads and a fixed number of bytes per
console.

The read budget MUST be spent only on records that can still carry new evidence:
a non-terminal worker is a candidate on every run, and a worker observed in a
terminal state is a candidate exactly until the read that shows it terminal.
A finished record MUST NOT be read again, so normal turnover cannot consume the
budget. When the candidates exceed the per-run budget the command MUST defer the
rest and report the deferral; it MUST NOT refuse, because a deferred record is a
candidate again on the next run while the provider still knows it.

The command MUST refuse when the number of non-terminal owned instances exceeds
the configured `controller.max_instances`: that is the ownership tags disagreeing
with the deployment, not turnover.

#### Scenario: Owned instances are observed
- **WHEN** the deployment owns instances and the command runs
- **THEN** each owned instance contributes at most one console read
- **AND** a record already observed in a terminal state contributes none
- **AND** no queue request is made and no resource is created, modified or terminated

#### Scenario: Turnover exceeds the per-run read budget
- **WHEN** the provider still lists terminal owned instances and the run has more
  candidates than the per-run read budget
- **THEN** the run reads up to the budget and reports the deferral
- **AND** it publishes the events and metrics for the records it read
- **AND** the deferred records are read on a later run

#### Scenario: The bound is exceeded
- **WHEN** the number of non-terminal owned instances exceeds the configured
  `controller.max_instances`
- **THEN** the command refuses with a stable reason code and exit code 2
- **AND** it writes no event and no metrics file

#### Scenario: An instance ages out
- **WHEN** the provider no longer knows one of the owned instance identifiers
- **THEN** that instance is skipped without failing the run
