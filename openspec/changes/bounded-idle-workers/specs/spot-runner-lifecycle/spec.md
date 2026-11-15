# Spec Delta

## MODIFIED Requirements

### Requirement: Operator-owned ephemeral bootstrap
The approved worker image/template MUST register a repository-scoped ephemeral Forgejo runner, execute at most one job using the supported one-job mode, and request instance termination when that job attempt ends or the claim window passes without a job. It MUST handle failure before job assignment as well as completed work. ci-burst MUST NOT distribute registration secrets or provisioning credentials to workers.

#### Scenario: One job completes
- **WHEN** the ephemeral runner finishes its assigned job
- **THEN** the worker exits and requests instance termination
- **AND** the controller does not treat the runner's registration as reusable

#### Scenario: No job arrives or bootstrap fails
- **WHEN** a worker never receives work or its bootstrap process fails
- **THEN** its maximum lifetime and independent expiry cleanup still apply
- **AND** ephemeral registration alone is not considered an expiry mechanism

#### Scenario: The claim window passes without a job
- **WHEN** every poll that has returned is empty, the claim deadline has passed and no poll is running
- **THEN** the worker runs no job and requests instance termination
- **AND** its maximum lifetime and independent expiry cleanup remain the outer bound

### Requirement: One-job process status is preserved through cleanup
The bootstrap MUST capture the exit status of the one-job runner process that ends the job attempt before its unconditional cleanup, emit exactly one bounded marker naming that status, and exit with it, so the console exit marker reports the real process status. A poll that finds no task and leaves the claim window open is not a job attempt and MUST NOT emit that marker. A completed process (status 0), a failure (any other status) and a timeout (124 or 137) MUST be distinguishable. Token cleanup, registration-file removal, sync and shutdown MUST run on every path, including a failed or timed-out process and a claim timeout.

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

#### Scenario: An empty poll is not a job attempt
- **WHEN** a poll is recognised as empty and the claim window is still open
- **THEN** the bootstrap emits no `bootstrap one-job` marker for that poll
- **AND** the status of that poll is not reported as the worker's exit status

#### Scenario: The claim window passes without a task
- **WHEN** the claim window passes without a task
- **THEN** token cleanup, registration-file removal, sync and shutdown still run
- **AND** no `bootstrap one-job` marker is emitted, because no job attempt happened

#### Scenario: The registration file cannot be removed after a claim timeout
- **WHEN** the claim window passes without a task and the removal of the registration file fails
- **THEN** the bootstrap prints a warning and no `bootstrap one-job` marker
- **AND** the bootstrap exits with status 0
- **AND** token cleanup, sync and shutdown still run

## ADDED Requirements

### Requirement: A worker polls for a task within a bounded claim window
After the runner registers, the bootstrap MUST NOT block while it waits for a task. It MUST poll by running the one-job mode without its wait option, pausing a fixed poll interval of 10 seconds between polls. The window and the interval MUST be single literals in the published bootstrap, not configuration or controller input.

#### Scenario: A job arrives within the window
- **WHEN** a poll receives a task before the claim deadline
- **THEN** the runner executes that one job under the existing job timeout
- **AND** the bootstrap starts no further poll
- **AND** the end of the job ends the worker as before

### Requirement: No poll starts once the claim deadline has passed
The claim deadline is the moment 300 seconds after registration returns. The claim deadline has passed when the current time is at or after that moment. The bootstrap MUST check the claim deadline after each empty poll and again after each pause. The bootstrap MUST NOT start a poll when the claim deadline has passed. When the claim deadline has passed at either check, the claim window has passed without a task and the bootstrap MUST end the worker.

#### Scenario: Every poll is empty until the deadline
- **WHEN** every poll is empty
- **THEN** the bootstrap pauses the poll interval between polls
- **AND** at the first check that finds the claim deadline passed, it starts no further poll and ends the worker

#### Scenario: A poll ends empty less than one interval before the deadline
- **WHEN** a poll ends empty 295 seconds after registration returned
- **THEN** the bootstrap pauses the poll interval
- **AND** at the check after the pause the claim deadline has passed, so the bootstrap starts no poll and ends the worker

#### Scenario: A poll ends empty at or after the deadline
- **WHEN** a poll ends empty 300 seconds or more after registration returned
- **THEN** the bootstrap neither pauses nor starts a further poll
- **AND** it ends the worker

### Requirement: A received job is not bounded by the claim window
The bootstrap MUST check the claim deadline only between polls, because a running poll may hold a job. A job that a poll receives MUST stay bounded by the existing job timeout and the instance lifetime. The claim deadline and the capture bound MUST NOT stop, signal or block the one-job process. The capture bound limits what the bootstrap reads, not what the runner writes.

#### Scenario: The deadline passes during a poll
- **WHEN** the claim deadline passes while a poll is running
- **THEN** the deadline does not interrupt that poll
- **AND** if the poll was empty, no further poll starts and the bootstrap ends the worker
- **AND** if the poll received a task, the job timeout and the instance lifetime bound the job
- **AND** if the poll has not returned, the worker waits for it, and the job timeout and the instance lifetime end it

#### Scenario: A job outlives the window
- **WHEN** a task was received before the claim deadline and the job is still running after it
- **THEN** the job is neither stopped nor shortened by the claim deadline

#### Scenario: A claimed job writes more output than the capture bound
- **WHEN** the one-job process of a claimed job writes more output than the capture bound
- **THEN** the bound does not stop, signal or block that process
- **AND** the poll is a finished attempt, because an oversized capture is not an empty poll

### Requirement: An empty poll is recognised by its status and message
The bootstrap MUST treat a poll as empty, and keep the claim window open, only when the one-job process exited with the documented no-task exit status and its captured output contains the documented no-task message as a complete line. A complete line is the documented message followed by one line feed, with no other byte on that line. Those values are the pinned runner version's, recorded in the worker setup guide.

#### Scenario: The documented empty result
- **WHEN** a poll exits with the documented no-task status, its capture holds the documented message as a complete line, and no process that the job account owns still exists
- **THEN** the bootstrap treats the poll as empty
- **AND** it starts the next poll only if the claim deadline has not passed after the empty poll and has not passed after the pause

### Requirement: The documented no-task status is from 2 to 123
The documented no-task status MUST be from 2 to 123, because the runner process of a claimed job can also end with 0, 1, 124 or a status of 128 or more. The bootstrap MUST NOT treat a poll as empty when its exit status is outside that range.

#### Scenario: A status outside the range
- **WHEN** a poll's capture holds the documented message and its exit status is outside 2 to 123
- **THEN** the poll is a finished attempt
- **AND** the bootstrap starts no further poll

### Requirement: The capture is closed to the job account and read after the poll ends
The capture MUST be bounded to a fixed number of bytes. The job account MUST NOT be able to open the capture by its path. The bootstrap MUST read the capture only after the one-job process has exited. The bootstrap MUST treat the poll as a finished attempt when a process that the job account owns still exists at that time.

#### Scenario: The job account cannot open the capture
- **WHEN** a process that the job account owns opens the capture by its path
- **THEN** the open is refused

#### Scenario: Job output imitates the empty result
- **WHEN** a claimed job writes the documented message by a route that the job account can use, including a path and a stream, and the one-job process ends with a status other than the documented no-task status
- **THEN** the bootstrap does not read that text as an empty poll
- **AND** the claim loop ends after that attempt

#### Scenario: A job process outlives the poll
- **WHEN** the one-job process exits with the documented no-task status, its capture holds the documented message, and a process that the job account owns still exists
- **THEN** the poll is a finished attempt
- **AND** the bootstrap starts no further poll

### Requirement: Runner output reaches the console while a poll runs
The capture MUST NOT replace the console. The bootstrap MUST keep sending the output of the one-job process to the console while the process runs. The bootstrap MAY omit the documented no-task line of an empty poll from the console.

#### Scenario: Runner output reaches the console while a poll runs
- **WHEN** a poll receives a task and the one-job process writes output
- **THEN** that output reaches the console while the process runs
- **AND** it does not wait for the poll to end

### Requirement: Every other poll outcome ends the claim loop
Every poll outcome that is not an empty poll MUST end the claim loop as a finished attempt. That includes a completed or failed job, the documented message with another status, the documented status with another message, a capture that is oversized, unreadable or missing, a signal, a timeout, a poll that leaves a process of the job account running and any unrecognised outcome. An unrecognised outcome MUST NOT be a reason to poll again.

#### Scenario: The status or the message alone
- **WHEN** a poll exits with the documented no-task status but another message, or prints the documented message with another status
- **THEN** the poll is a finished attempt
- **AND** the bootstrap starts no further poll

#### Scenario: A request for a task fails
- **WHEN** the runner's request for a task fails, for example by a network error or a timeout, and the pinned runner therefore ends with the documented no-task status and message after an error-level log line
- **THEN** the poll matches the empty-poll signature and the bootstrap treats it as empty
- **AND** the worker still ends at the first deadline check after a poll or a pause that ends past the claim deadline
- **AND** the error-level line may reach the console, so the claim timeout can carry that runner error

#### Scenario: A poll fails for another reason
- **WHEN** a poll fails with an unrecognised status, for example because the server does not accept the registration
- **THEN** the bootstrap ends the claim loop and emits one failed marker with that status
- **AND** token cleanup, registration-file removal, sync and shutdown still run
- **AND** the bootstrap does not poll again

#### Scenario: The capture cannot be trusted
- **WHEN** the capture cannot be created, is larger than its bound or cannot be read
- **THEN** the poll is a finished attempt
- **AND** the bootstrap starts no further poll

### Requirement: A claim timeout is reported by one fixed marker
When the claim window passes without a task, the bootstrap MUST print exactly one `STEP claim-timeout` marker. It MUST NOT print the `job` step marker or a `bootstrap one-job` marker for that ending, because no job attempt happened, and it MUST NOT add other text to the marker line. It MUST leave through the existing exit path with status 0, so the console exit marker reports `bootstrap exiting rc=0`. It MUST NOT print the marker on any other path.

#### Scenario: The claim window passes
- **WHEN** the claim window passes without a task
- **THEN** the bootstrap prints one `STEP claim-timeout` marker
- **AND** it prints neither the `job` step marker nor a `bootstrap one-job` marker
- **AND** the bootstrap exits with status 0, so the console exit marker reports `bootstrap exiting rc=0`

#### Scenario: A job attempt ends the worker
- **WHEN** a poll ends as a finished attempt, whether the job completed, failed or timed out
- **THEN** the bootstrap prints no `STEP claim-timeout` marker

#### Scenario: A claim timeout does not read as a failure
- **WHEN** the claim window passes after only empty polls in which the runner logged no error-level line
- **THEN** the claim loop writes no console line that the exporter's failure rules classify as a failure kind
