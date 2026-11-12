# Spec Delta

## MODIFIED Requirements

### Requirement: Observation refuses only when owned instances exceed the limit

The read budget is independent of the admission ceiling. More non-terminal owned
instances than `controller.max_instances` MUST be reported with
`above_configured_limit`, not refused: a supported drain still writes events and
metrics. A deferred run MUST report the deferral and MUST NOT present itself as
complete. The command MUST refuse, writing no file, only when unfinished
non-terminal candidates exceed `MAX_CONSOLE_READS` (16), with
`console_read_cap_exceeded`.

#### Scenario: A supported drain is reported, not refused
- **WHEN** two non-terminal owned workers run with `controller.max_instances` set to 1
- **THEN** the decision is `reported` with two console reads and the `above_configured_limit` note
- **AND** the events file and the metrics file are both written

#### Scenario: Live workers exceed the read budget
- **WHEN** 17 unfinished non-terminal owned workers are candidates in one run
- **THEN** the command refuses with `console_read_cap_exceeded` and exit code 2
- **AND** it writes no event and no metrics file

#### Scenario: Turnover is not a bound violation
- **WHEN** the number of owned instances exceeds the configured limit but every one is reported `terminated`
- **THEN** the command does not refuse

## MODIFIED Requirements

### Requirement: Turnover defers surplus candidates rather than refusing

A finished record MUST NOT be read again, so normal turnover cannot consume the
budget, and live workers MUST be read before termination history. Surplus
candidates the run will not read MUST be deferred and reported, and MUST be read
on a later run. The deferral applies while the unfinished non-terminal candidates
fit within the per-run read budget; above it the command MUST refuse under the
refusal requirement, because live evidence cannot be covered in one run.

#### Scenario: Surplus terminal history is deferred
- **WHEN** the unfinished non-terminal candidates fit within the per-run read budget and terminal owned instances push the candidate count above it
- **THEN** the run reports `console_read_deferred` and does not refuse

#### Scenario: Turnover exceeds the per-run read budget
- **WHEN** the provider still lists terminal owned instances and the run has more
  candidates than the per-run read budget, while its unfinished non-terminal
  candidates fit within that budget
- **THEN** the run reads up to the budget and reports the deferral
- **AND** it publishes the events and metrics for the records it read
- **AND** the deferred records are read on a later run
