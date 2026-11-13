# Spec Delta

## MODIFIED Requirements

### Requirement: Closed marker vocabulary
The command MUST recognise only a closed vocabulary: a step name from the shipped first-boot script's fixed set; the `bootstrap one-job <outcome> rc=<n>` marker with outcome `completed`, `failed` or `timeout`; the `bootstrap exiting rc=<n>` status, which now reports the one-job process's real status; the `bootstrap spot-interruption action=<action> host=<hostname> runner=<uuid|unknown>` marker, with `action` in a closed set, `host` matching the shipped hostname pattern and `runner` matching the registration identifier's pattern or the shipped `unknown` fallback; and fixed failure kinds. The process marker MUST NOT be read as a Forgejo job verdict, and no console text MAY be published, so truncation MUST NOT be used as redaction.

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

#### Scenario: A Spot interruption marker
- **WHEN** a console contains `bootstrap spot-interruption action=<action> host=<hostname> runner=<uuid|unknown>` with every value one the bootstrap can emit
- **THEN** the instance records one `spot-interruption` event carrying that action, hostname and registration id
- **AND** the line's other text is not published

#### Scenario: An unshipped interruption value
- **WHEN** a console line names an action outside the closed set, a hostname that does not match the shipped pattern, or a registration id that is neither the identifier's shape nor `unknown`
- **THEN** it is ignored and never reaches the event stream

## ADDED Requirements

### Requirement: Interruption evidence identifies the worker
The `spot-interruption` event MUST identify the instance, the worker hostname and the runner registration id when the worker knows it, and MUST carry the fixed `reason` value `spot_interruption` for a consumer to filter on. A value the worker cannot establish MUST be published as the shipped `unknown` fallback rather than inferred.

#### Scenario: A worker ends on the interruption notice
- **WHEN** the bootstrap reads the two-minute interruption notice and ends its one job
- **THEN** the event stream carries one `spot-interruption` event for that instance with its reason, action, hostname and registration id
- **AND** no free console text is published

#### Scenario: The registration id was not captured
- **WHEN** the worker cannot capture its registration id (for example a missing or malformed registration file)
- **THEN** the console marker carries `unknown` and the event carries `null` for the registration id
- **AND** the instance, hostname, action and reason are still published

#### Scenario: The notice cannot be read
- **WHEN** the token, the notice read or the marker fails
- **THEN** no `spot-interruption` event is written for that worker
- **AND** the instance's other markers and the worker's lifetime bounds are unchanged

### Requirement: Interruption evidence is not a job verdict
The `spot-interruption` event MUST NOT state or imply a Forgejo job result, a job verdict or a retry decision, and MUST NOT carry a Forgejo task or job identity, which a worker cannot know. Because every value in the event stream comes from a console that no party is trusted to write, a consumer MUST treat the event as one input among the job's own evidence.

#### Scenario: A consumer reads the marker
- **WHEN** a consumer evaluates a lost job
- **THEN** the marker is one input beside the job's own evidence, never a substitute for it
- **AND** no success, failure or retry field is added to the event
