# Spec Delta

## ADDED Requirements

### Requirement: A claim timeout is a fixed step marker
The command MUST recognise `STEP claim-timeout` as one more name in the closed step vocabulary and MUST publish it as a `step` event naming that step, with no console text. It MUST NOT read the marker as a job result, a registration failure or the end of the instance, and a record MUST still finish only after observed termination. A step name outside the closed vocabulary MUST remain ignored.

#### Scenario: A worker's claim window ends
- **WHEN** a console contains the `STEP claim-timeout` marker
- **THEN** the instance records one `step` event naming `claim-timeout`
- **AND** no console text is emitted

#### Scenario: A registered worker times out
- **WHEN** a console contains both the registration step marker and the `STEP claim-timeout` marker
- **THEN** the worker is counted as registered once
- **AND** it is not counted as unregistered

#### Scenario: The marker is not termination
- **WHEN** a console contains the `STEP claim-timeout` marker while the provider still reports the instance live
- **THEN** that record is not finished
- **AND** the instance remains a candidate on the next run

#### Scenario: A lookalike step name
- **WHEN** a console line names a step that is not in the closed vocabulary, for example `claim-timeouts`
- **THEN** it is ignored and never reaches the event stream
