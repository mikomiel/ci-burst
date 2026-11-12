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
