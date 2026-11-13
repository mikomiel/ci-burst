# Spec Delta

## MODIFIED Requirements

### Requirement: Closed marker vocabulary
The command MUST recognise only a closed vocabulary: a step name from the shipped first-boot script's fixed set; the `bootstrap one-job <outcome> rc=<n>` marker with outcome `completed`, `failed` or `timeout`; the `bootstrap exiting rc=<n>` status, which now reports the one-job process's real status; and fixed failure kinds. The process marker MUST NOT be read as a Forgejo job verdict, and no console text MAY be published, so truncation MUST NOT be used as redaction.

#### Scenario: A worker registers
- **WHEN** a console contains the registration step marker
- **THEN** the instance records a registration event naming that step
- **AND** no console text is emitted

#### Scenario: A worker's one-job process ends
- **WHEN** a console contains one `bootstrap one-job completed|failed|timeout rc=<n>` marker
- **THEN** the instance records one `job-process` event naming that outcome and status
- **AND** no job verdict or console text is published

#### Scenario: An unshipped step name or unrelated text
- **WHEN** a console line names a step outside the fixed set, or matches no marker
- **THEN** it is ignored and never reaches the event stream

#### Scenario: An unshipped process outcome
- **WHEN** a console line names a one-job outcome outside `completed`, `failed` and `timeout`, or pairs an outcome with a status the bootstrap cannot emit
- **THEN** it is ignored and never reaches the event stream
