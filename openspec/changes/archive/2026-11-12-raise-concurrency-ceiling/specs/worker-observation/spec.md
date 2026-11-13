# Spec Delta

## ADDED Requirements

### Requirement: A record finishes only after observed termination

The read budget MUST be spent only on records that can still carry new evidence: a worker
the provider reports live or terminating is a candidate on every run, and a record is
finished only after a read that observed the provider reporting it `terminated` or
`stopped`. A console exit marker MUST NOT finish a record, because the bootstrap can
finish while the instance and its console carry on.

#### Scenario: Console exit marker is not termination
- **WHEN** a console read shows an exit marker while the provider still reports the instance live
- **THEN** that record is not finished
- **AND** the instance remains a candidate on the next run

### Requirement: Turnover defers surplus candidates rather than refusing

A finished record MUST NOT be read again, so normal turnover cannot consume the budget,
and live workers MUST be read before termination history. When the candidates exceed the
per-run budget the command MUST defer the rest and report the deferral; it MUST NOT
refuse, because a deferred record is a candidate again on the next run while the provider
still knows it.

#### Scenario: Turnover exceeds the per-run read budget
- **WHEN** the provider still lists terminal owned instances and the run has more
  candidates than the per-run read budget
- **THEN** the run reads up to the budget and reports the deferral
- **AND** it publishes the events and metrics for the records it read
- **AND** the deferred records are read on a later run

### Requirement: Observation refuses only when owned instances exceed the limit

The command MUST refuse when the number of non-terminal owned instances exceeds the
configured `controller.max_instances`, counting every instance the provider does not
report as `terminated`: that is the ownership tags disagreeing with the deployment, not
turnover.

#### Scenario: Turnover is not a bound violation
- **WHEN** the number of owned instances exceeds the configured limit but every one is reported `terminated`
- **THEN** the command does not refuse

## MODIFIED Requirements

### Requirement: Provider-only bounded observation

The command MUST read evidence only from the provider console of instances that carry this
deployment's ownership tags. It MUST NOT call the queue interface, hold a queue
credential, or mutate any provider resource. It MUST bound one run at a small fixed number
of console reads and a fixed number of bytes per console.

#### Scenario: Owned instances are observed
- **WHEN** the deployment owns instances and the command runs
- **THEN** each owned instance contributes at most one console read
- **AND** a record already observed in a terminal state contributes none
- **AND** no queue request is made and no resource is created, modified or terminated

#### Scenario: The bound is exceeded
- **WHEN** the number of non-terminal owned instances exceeds the configured
  `controller.max_instances`
- **THEN** the command refuses with a stable reason code and exit code 2
- **AND** it writes no event and no metrics file

#### Scenario: An instance ages out
- **WHEN** the provider no longer knows one of the owned instance identifiers
- **THEN** that instance is skipped without failing the run
