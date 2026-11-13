# Spec Delta

## ADDED Requirements

### Requirement: Replayable contract evidence accompanies manual qualification
The observer's contract qualification MUST be accompanied by a versioned reading of the pinned upstream handler and replayable fixtures for every shape the parser classifies. The reading MUST name the upstream project, tag and source path, state that it is a contract reading rather than a live observation, and require no server, request or credential. Only an operator canary on an isolated deployment establishes live compatibility.

#### Scenario: Contract reading and fixtures agree
- **WHEN** the matrix replays every pinned fixture through the observer
- **THEN** each classification matches the manifest's recorded expectation
- **AND** the manifest names the tag, source path and non-live provenance

#### Scenario: Live compatibility is not inferred from fixtures
- **WHEN** no operator canary has recorded the server and runner versions
- **THEN** the deployment remains `fixtures-only`
- **AND** the matrix result is not recorded as live evidence

#### Scenario: A recorded version changes
- **WHEN** either the recorded server version or runner version changes, or a response shape has no fixture
- **THEN** a new contract reading and a new operator canary are required
- **AND** the previous live qualification is not reused
