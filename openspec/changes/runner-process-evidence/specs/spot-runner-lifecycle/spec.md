# Spec Delta

## ADDED Requirements

### Requirement: One-job process status is preserved through cleanup
The bootstrap MUST capture the one-job runner process's exit status before its unconditional cleanup, emit exactly one bounded marker naming that status, and exit with it, so the console exit marker reports the real process status. A completed process (status 0), a failure (any other status) and a timeout (124 or 137) MUST be distinguishable. Token cleanup, registration-file removal, sync and shutdown MUST run on every path, including a failed or timed-out process.

#### Scenario: The one-job process completes
- **WHEN** the runner process exits 0
- **THEN** the bootstrap emits one completed marker with status 0 and exits 0
- **AND** the console exit marker reports the same status

#### Scenario: The one-job process fails
- **WHEN** the runner process exits with a nonzero status that is not a timeout
- **THEN** the bootstrap emits one failed marker with that status and exits with it
- **AND** token cleanup, registration-file removal, sync and shutdown still run

#### Scenario: The one-job process times out
- **WHEN** the lifetime `timeout` ends the runner process with 124 or 137
- **THEN** the bootstrap emits one timeout marker with that status and exits with it
- **AND** token cleanup, registration-file removal, sync and shutdown still run
