# Spec Delta

## ADDED Requirements

### Requirement: Admission stops after repeated futile launches
The controller MUST refuse a launch with the reason `recent_launches_futile` when at least three workers are considered and every considered worker is futile. It MUST evaluate this gate after every earlier admission reason and before it reads the launch template. The count of three and the window of 3600 seconds MUST be fixed values, not configuration. A hold MUST end without an operator action.

#### Scenario: Three futile workers hold admission
- **WHEN** demand is waiting, a slot is free, no recent launch covers the demand, and the three newest classified workers that are not older than 3600 seconds are all futile
- **THEN** the decision is `refused` with the single reason `recent_launches_futile` and a `launch_count` of 0
- **AND** no launch intent, reservation or instance is created
- **AND** no launch template or price is read and no budget is computed

#### Scenario: Fewer than three classified workers
- **WHEN** only two classified workers are not older than 3600 seconds and both are futile
- **THEN** the gate is open
- **AND** admission continues with the template, price and allowance checks as before

#### Scenario: A registered worker is among the newest three
- **WHEN** the three newest classified workers are two futile workers and one registered worker
- **THEN** the gate is open
- **AND** the evidence reports `considered` 3 and `futile` 2

#### Scenario: A registered worker is older than the newest three
- **WHEN** the three newest classified workers are futile and the next older classified worker registered
- **THEN** the gate holds

#### Scenario: A worker exactly at the window edge
- **WHEN** the oldest of three futile workers is exactly 3600 seconds old
- **THEN** that worker is inside the window
- **AND** the gate holds

#### Scenario: A worker leaves the window
- **WHEN** the oldest of three futile workers is more than 3600 seconds old
- **THEN** that worker is outside the window and only two workers are considered
- **AND** the gate is open

#### Scenario: Earlier reasons keep precedence
- **WHEN** the gate would hold and the ledger and the provider disagree about owned resources, no slot is free, no job is waiting, or recent launches already cover every waiting job
- **THEN** the decision reports that earlier reason as it did before
- **AND** it does not report `recent_launches_futile`

#### Scenario: A hold precedes the later reasons
- **WHEN** the gate holds and the launch template is unusable, a price is unavailable or the allowance is exhausted
- **THEN** the only reason is `recent_launches_futile`
- **AND** the decision carries no placement candidates and no budget

#### Scenario: A hold ends when its evidence ages out
- **WHEN** a hold is in force and the oldest of the three considered workers becomes older than 3600 seconds
- **THEN** the next pass is not held and admits within the slot limit and the allowance
- **AND** no operator action was taken

### Requirement: Only ended workers with an operation are classified
The controller MUST classify a worker only from a record that shows the instance has ended, meaning a provider state of `terminated` or `stopped`, and that carries an operation identifier of the shape this deployment generates. The ledger state that the same pass read MUST hold that operation, under the same instance when it names one. Any other record is no evidence and MUST NOT be counted.

#### Scenario: The instance has not ended
- **WHEN** a record shows the provider state `pending`, `running`, `shutting-down` or `stopping`, whatever else it shows
- **THEN** the record is no evidence
- **AND** a registered worker that still runs does not clear a hold

#### Scenario: The record names no operation of this deployment
- **WHEN** a record has no operation identifier, or one that does not match the shape this deployment generates
- **THEN** the record is no evidence

#### Scenario: A provider state outside the known list
- **WHEN** a record's provider state is null, or text that names none of the six provider states
- **THEN** that record has not ended and is no evidence
- **AND** the rest of the file is still usable

#### Scenario: The ledger holds none of the operations
- **WHEN** three ended records show the exit marker and no registration evidence, each carries an operation identifier of the right shape, and the ledger holds none of those operations
- **THEN** the records are no evidence and the gate is open
- **AND** the evidence reports `considered` 0 and `futile` 0

#### Scenario: The ledger holds the operations
- **WHEN** the same three records are read and the ledger holds their operations
- **THEN** the gate holds

#### Scenario: The ledger names another instance
- **WHEN** the ledger holds the operation of a record and names an instance other than the one the record is filed under
- **THEN** that record is no evidence

#### Scenario: The ledger names no instance
- **WHEN** the ledger holds the operation of a record and names no instance for it
- **THEN** the record is evidence under any instance identifier

#### Scenario: A worker that the ledger does not hold takes no place among the newest three
- **WHEN** the three newest ended workers are not in the ledger, and the three next older workers are futile and in the ledger
- **THEN** the older three are the considered workers
- **AND** the gate holds

#### Scenario: Planning and applying count the same workers
- **WHEN** `plan` and `apply` read the same record against the same ledger
- **THEN** both report the same `considered` and `futile` counts

### Requirement: An ended worker is registered or futile by its markers
A classified worker MUST be registered when its record shows registration evidence: the registration step, a step that the shipped first-boot script prints only after registration, a one-job process outcome or a Spot interruption notice. It MUST be futile when its record shows the exit marker (the `bootstrap exiting rc=<n>` status) and no registration evidence.

#### Scenario: A worker ends before it registers
- **WHEN** a record shows the provider state `terminated`, the exit marker and no registration evidence
- **THEN** the worker is futile

#### Scenario: A worker registered and ended
- **WHEN** a record shows the provider state `terminated` and the registration step
- **THEN** the worker is registered
- **AND** this holds whether or not the record shows the exit marker

#### Scenario: The registration line was lost
- **WHEN** a record shows the provider state `terminated`, the exit marker, no registration step and a one-job process outcome
- **THEN** the worker is registered
- **AND** it is not futile

#### Scenario: The console does not reach the end
- **WHEN** a record shows the provider state `terminated`, no exit marker and no registration evidence
- **THEN** the record is no evidence
- **AND** it neither counts toward the gate nor clears a hold

### Requirement: The considered workers are the newest within a fixed window
The controller MUST order classified workers by the record's `seen` time and MUST NOT use another clock. It MUST measure age as the controller clock minus `seen`, and a worker is inside the window when its age is at most 3600 seconds. The considered workers MUST be the three inside the window with the newest `seen`, and every other worker inside the window whose `seen` equals the third's.

#### Scenario: The age is measured from the last read
- **WHEN** a record has a `first_seen` 7200 seconds before the controller clock and a `seen` 100 seconds before it
- **THEN** the age of that worker is 100 seconds

#### Scenario: A tie at the third place
- **WHEN** four classified workers share one `seen`, three are futile and one registered
- **THEN** all four are considered
- **AND** the gate is open

#### Scenario: Every classified worker has aged out
- **WHEN** every classified worker is older than 3600 seconds
- **THEN** no worker is considered
- **AND** the gate is open

### Requirement: The launch evidence is an optional brake
The controller MUST treat a missing, unreadable, stale or malformed record as no evidence, and no evidence MUST NOT deny a launch. The record is not an input to live admission. It MUST NOT replace or relax any prerequisite of positive live admission, and every prerequisite still applies to a launch that the gate lets through. The gate MUST only withhold launches.

#### Scenario: The record is missing
- **WHEN** no file exists at the record path
- **THEN** the evidence state is `missing`
- **AND** the gate is open and admission continues as it would without the gate

#### Scenario: An open gate leaves every prerequisite in force
- **WHEN** the gate is open and the template is unusable, a price is unavailable or the allowance is exhausted
- **THEN** the launch is refused for that reason as it was before
- **AND** otherwise the decision differs from one made without the gate only by `launch_evidence`

#### Scenario: Evidence returns
- **WHEN** `export` next writes a valid record after the state was `stale` or `missing`
- **THEN** the next pass reports `fresh` and evaluates the gate normally

### Requirement: The evidence is read from one file without side effects
The controller MUST read the evidence only from `observed.json` in the `export` directory beside the ledger. It MUST NOT write, repair or delete that file. It MUST NOT make a provider or queue call, and MUST NOT read the ledger again, to read it or to count its workers. It MUST NOT block on the path and MUST read only a regular file that is not a link. The evidence state MUST be exactly one of `missing`, `unreadable`, `stale`, `malformed` or `fresh`, tested in that order.

#### Scenario: The record cannot be read
- **WHEN** the file exists and cannot be opened or read
- **THEN** the evidence state is `unreadable`
- **AND** the gate is open

#### Scenario: The path is not a regular file
- **WHEN** the path names a FIFO, a directory or another file that is not a regular file
- **THEN** the evidence state is `unreadable` and the read does not block, even for a FIFO that has no writer
- **AND** the gate is open, whatever the modification time of the path

#### Scenario: The path is a link
- **WHEN** the path is a link, whatever it points to
- **THEN** the evidence state is `unreadable`
- **AND** the gate is open

#### Scenario: The record is stale
- **WHEN** the file's modification time is more than 600 seconds before the controller clock
- **THEN** the evidence state is `stale`
- **AND** the gate is open, whatever the records say

#### Scenario: The record is exactly at the staleness bound
- **WHEN** the file's modification time is exactly 600 seconds before the controller clock
- **THEN** the evidence state is `fresh`

#### Scenario: The record is dated in the future
- **WHEN** the file's modification time is after the controller clock
- **THEN** the evidence state is `stale`
- **AND** the gate is open

#### Scenario: The file is malformed
- **WHEN** the file's modification time is fresh and the file is not UTF-8 JSON, is not an object, carries a `version` other than the one `export` writes, or has an `instances` value that is not an object
- **THEN** the evidence state is `malformed`
- **AND** the gate is open

#### Scenario: One record is invalid
- **WHEN** the file's modification time is fresh and one record has a field that the gate reads with the wrong type, or a `seen` that is not a finite number or is after the controller clock
- **THEN** the evidence state is `malformed` for the whole file
- **AND** no record from that file is used

#### Scenario: An instance key that `export` rejects
- **WHEN** the file's modification time is fresh and one key of `instances` is not an instance identifier of the shape that `export` accepts
- **THEN** the evidence state is `malformed` for the whole file
- **AND** three futile records filed under such keys do not hold admission

#### Scenario: A marker value that `export` cannot write
- **WHEN** the file's modification time is fresh and a record's `job_process` or `spot_interruption` is neither null nor the value that `export` stores, such as an empty list or a pair whose outcome and status disagree
- **THEN** the evidence state is `malformed` for the whole file
- **AND** the marker is not read as registration evidence

#### Scenario: The export unit writes to another path
- **WHEN** `export` runs with a state file other than the default path
- **THEN** the controller reports `missing` or `stale` for the default path
- **AND** it does not look for a record anywhere else

#### Scenario: The controller leaves the record alone
- **WHEN** `plan` or `apply` runs
- **THEN** the bytes and the modification time of the record are unchanged

#### Scenario: The gate uses the ledger state that its pass read
- **WHEN** `plan` or `apply` decides on a record that holds futile workers
- **THEN** the workers are counted against the ledger state that the same pass already read
- **AND** the gate makes no provider call, no queue call and no ledger read of its own

### Requirement: The evidence read is bounded and decided from the open file
The controller MUST take the type, the modification time and the size of the record from the open file and decide before it reads any content. It MUST read the open file itself, not through a buffered stream, and fetch at most 16 MiB plus one byte from it, and MUST treat a file above 16 MiB as `malformed`. It MUST refuse the JSON constants `NaN`, `Infinity` and `-Infinity`, and a JSON object that names a member twice, when it decodes the file, so a file that holds one is `malformed`.

#### Scenario: The file holds a nonstandard JSON constant
- **WHEN** the file's modification time is fresh and the file holds `NaN`, `Infinity` or `-Infinity`, in a field that the gate reads or in one that it does not
- **THEN** the evidence state is `malformed` for the whole file
- **AND** three futile records in the same file do not hold admission

#### Scenario: A JSON object names a member twice
- **WHEN** the file's modification time is fresh and a JSON object in the file names one member twice, at the top level, in `instances` or in a record, whatever the two values are
- **THEN** the evidence state is `malformed` for the whole file
- **AND** three futile records in the same file do not hold admission, even when the last value of the repeated member is valid

#### Scenario: An instance key repeats
- **WHEN** the file's modification time is fresh and `instances` holds one key twice, a registered record under the first and a futile record under the second
- **THEN** the evidence state is `malformed` for the whole file
- **AND** the futile record, which a decoder that keeps the last value would use alone, does not hold admission

#### Scenario: The file is larger than the size bound
- **WHEN** the file's modification time is fresh and the file is larger than 16 MiB
- **THEN** the evidence state is `malformed`
- **AND** the controller reads none of its content, and the gate is open

#### Scenario: A stale file is not read whatever its size
- **WHEN** the file is larger than 16 MiB and its modification time is more than 600 seconds before the controller clock, or after it
- **THEN** the evidence state is `stale`
- **AND** the controller reads none of its content, and the gate is open

#### Scenario: The file is exactly at the size bound
- **WHEN** the file's modification time is fresh and the file is exactly 16 MiB
- **THEN** the controller reads it and the evidence state is the one its content gives

#### Scenario: The file grows past the size bound while it is read
- **WHEN** the file grows past 16 MiB after the controller took its size
- **THEN** the controller fetches at most 16 MiB plus one byte from the file, however far the file has grown
- **AND** the evidence state is `malformed` and the gate is open

### Requirement: A decision reports the launch evidence it read
Every decision that reaches the gate MUST report `launch_evidence` with the evidence `state`, the `considered` count and the `futile` count, and the counts MUST be 0 unless the state is `fresh`. A decision that ends before the gate MUST NOT carry it. A result that ends before admission computes a decision, such as a deadline deferral, is not required to carry it. `status` builds no admission decision and is not required to report the gate.

#### Scenario: A launch decision reports its evidence
- **WHEN** no record exists and a launch is ready
- **THEN** `launch_evidence` is `{"state": "missing", "considered": 0, "futile": 0}`
- **AND** the launch proceeds as it would without the gate

#### Scenario: A hold reports the counts
- **WHEN** three futile workers hold admission
- **THEN** `launch_evidence` is `{"state": "fresh", "considered": 3, "futile": 3}`

#### Scenario: A decision that ends before the gate carries none
- **WHEN** the decision is `slot_busy`, `no_runnable_demand`, `awaiting_claim` or `owned_state_discrepancy`
- **THEN** it carries no `launch_evidence`

#### Scenario: Planning reports the same hold as apply
- **WHEN** `plan` reads the same unchanged record as an `apply`, the ledger matches the provider inventory and the `apply` has no unresolved intent to retry
- **THEN** the plan reports the same `launch_evidence`, reason and `launch_count` that the `apply` would use
- **AND** no launch intent, reservation or instance is created

#### Scenario: Planning cannot read the record
- **WHEN** `plan` runs where the record is missing or unreadable although an `apply` can read it
- **THEN** the plan reports the state it found in `launch_evidence`
- **AND** the plan plans as if the gate were open
