# Spec Delta

## ADDED Requirements

### Requirement: One writer per set of export files

A deployment MUST give each set of export files (the record, the event file, the metrics textfile and the collector file the unit publishes it to) one writer: the shipped unit `ci-burst-export.service`, which systemd runs one at a time. A command run outside the unit MUST name its own record, event file and textfile and MUST NOT write the unit's collector file. The command MUST refuse (`EXPORT_PATHS_CONFLICT`) two outputs naming one file, or one naming an entry the ledger or configuration uses.

#### Scenario: A manual export of the unit's files
- **WHEN** an operator wants an export of the unit's set of export files
- **THEN** they run `systemctl start ci-burst-export.service`, and systemd does not start a second instance while one runs

#### Scenario: Outputs that name one file are refused
- **WHEN** two outputs of the command name one file by any spelling, or an output names a link or the file on the way to the ledger database or the configuration file, an entry beside one of those whose name continues it with `.` or `-`, or an entry in the image state directory
- **THEN** the command refuses with `EXPORT_PATHS_CONFLICT` and exits 2 before it writes or creates anything; it checks no other path

## MODIFIED Requirements

### Requirement: Rebuildable dedupe state

The command MUST persist what it has already emitted, MUST prune those
per-instance records after a fixed retention, and MUST treat the record as
derived observation. The record MUST NOT influence admission, launch or
termination, except through the one read-only brake that the bounded-operation
capability defines, which only withholds launches. The command MUST save the
record before it replaces the metrics textfile, so that the textfile carries no
total that the record lacks. It MUST advance the record's modification time as
the last step of every successful run, even when nothing changed, because that
time is that brake's freshness signal, and a run that fails MUST NOT advance it.
That time marks the command's own work, which ends with it. It does not mark
the publication of the metrics textfile into the collector's directory, which the
unit that runs the command does afterwards. A change to the
record's version or to a field that the brake reads MUST ship with the matching
change to the brake's reader. These obligations hold for a set of export files
that has one writer (requirement "One writer per set of export files").

#### Scenario: The same console is read twice
- **WHEN** the command runs twice over the same console content
- **THEN** the second run emits no new event for that content

#### Scenario: A run with nothing new still rewrites the record
- **WHEN** a successful run observes no new marker and no new instance
- **THEN** the record is rewritten and its modification time advances
- **AND** the run emits no new event

#### Scenario: A refused run leaves the record alone
- **WHEN** a run refuses with `console_read_cap_exceeded`
- **THEN** the record is not rewritten
- **AND** its modification time ages, so the brake sees it as `stale` after 600 seconds

#### Scenario: A run that cannot write its textfile keeps the record that it saved
- **WHEN** a run saved the record and then cannot replace the metrics textfile, so the command exits 1
- **THEN** the record stays as the run saved it, under the modification time of the record that the run read, or under the epoch when it read none
- **AND** the modification time ages, so the brake sees it as `stale` after 600 seconds
- **AND** the run published its events before it saved the record, so the next run does not emit them again

#### Scenario: A run that cannot write the record does not replace the textfile
- **WHEN** a run cannot replace the record, so the command exits 1
- **THEN** the command has not replaced the metrics textfile, so no collector reads a total that the record lacks, whether it reads the `--metrics-file` that the command writes itself or the copy that the shipped unit publishes
- **AND** the record keeps its modification time and ages, so the brake sees it as `stale` after 600 seconds
- **AND** the shipped unit does not publish the textfile into the collector's directory, because that step follows only a successful command, so the collector's last-run timestamp does not advance either

#### Scenario: A run that stops after it replaced the textfile cannot lower a counter
- **WHEN** a run replaces the metrics textfile and then cannot advance the record's modification time, or stops before it does
- **THEN** the record already holds the totals of that textfile, under the modification time that it had before the run, so the brake sees it as `stale` after 600 seconds
- **AND** a later run writes totals that are not lower, even when a worker has left the provider's inventory in between

#### Scenario: A run that read its record and cannot write its textfile cannot lower a counter
- **WHEN** a run read the record, saved it, and then cannot replace the metrics textfile, or an interrupt ends the run before it replaced the textfile
- **THEN** the textfile is the previous one, and the record holds the totals of that run under the modification time that the run read, so the brake sees it as `stale` after 600 seconds
- **AND** a later run writes totals that are not lower, so no counter falls

#### Scenario: A record that cannot be read restarts the counters
- **WHEN** a record exists and the run cannot read it: a read fails, even for a moment, or the path holds a link or a FIFO
- **THEN** the command treats it as a record that does not decode: it notes `state_rebuilt`, starts from an empty record and saves a regular file in its place
- **AND** the emitted history and the cumulative counters start again, as when the record is deleted, so the textfile of an earlier run can hold higher totals until a run replaces it
- **AND** the run saves the new record under the epoch, so a run that then cannot write its textfile leaves no fresh heartbeat
- **AND** the record carries neither the records nor the modification time of a link's target

#### Scenario: An event file that cannot be read fails the run
- **WHEN** the event file exists and the run cannot read it, or the path holds a link, a FIFO or a directory, whether or not the run has new events
- **THEN** the command exits 1 before it replaces the event file, the record or the textfile
- **AND** it opens the path without blocking, so a FIFO with no writer cannot stop it
- **AND** only a missing event file is an empty stream

#### Scenario: The unit cannot publish the textfile
- **WHEN** a run completes and writes the record, and the shipped unit then cannot publish the textfile into the collector's directory
- **THEN** the record's modification time has advanced, so the brake sees the record as `fresh`
- **AND** a hold still rests on the record, because the evidence in it was read
- **AND** the collector's copy of the textfile is not replaced, so its last-run timestamp does not advance

#### Scenario: The record is deleted
- **WHEN** the record is deleted
- **THEN** the controller reports the evidence state `missing` and admits as it would without the brake
- **AND** the next run rebuilds records from the consoles that it reads within its per-run read budget (`MAX_CONSOLE_READS`, 16, live workers first) among the owned instances that the provider still lists, stamped with that run's time
- **AND** a console that it defers past that budget (`console_read_deferred`) waits for a later run while the provider still lists the instance
- **AND** the emitted history and the cumulative counters are lost as before

#### Scenario: A record that names a member twice is dropped whole
- **WHEN** the record holds a JSON object that names a member twice, at any level, or one of the constants `NaN`, `Infinity` and `-Infinity`
- **THEN** the command treats the file as one that does not decode: it drops it whole, notes `state_rebuilt` and starts from an empty record
- **AND** the emitted history and the cumulative counters start again, as when the record is deleted
- **AND** the brake's reader refuses the same files, so the command never keeps such a record and rewrites it with a fresh modification time

#### Scenario: A repeated instance key does not turn a registered worker into a hold
- **WHEN** the record names one instance key twice, a registered record first and a futile record last, beside two other futile records, and the command runs over those three finished workers, which fit within its per-run read budget (`MAX_CONSOLE_READS`)
- **THEN** the command drops the file whole and reads the consoles of the three workers, so the worker that registered is recorded as registered
- **AND** the controller's reader finds no hold in the record that the run writes

#### Scenario: A record whose registration value cannot be kept is dropped
- **WHEN** the record of an instance holds a `job_process` or `spot_interruption` of a shape that the command does not write, a `steps` list with an element that is not text, or a `registered` flag that is not true or false
- **THEN** the command drops that record only and notes `state_rebuilt`
- **AND** it does not keep the record with the value replaced by its default, because a worker that has ended is not read again and the run would write the record back under a fresh modification time as a futile launch that nobody observed

#### Scenario: A dropped record is read again while the provider lists the instance
- **WHEN** the record of an ended worker is dropped because its registration evidence cannot be kept, the provider still lists the instance, and the console shows that the worker registered
- **THEN** the run reads that console again within its per-run read budget and records the worker as registered
- **AND** the brake's reader finds no hold, although the record holds two other futile workers

#### Scenario: A dropped record counts when its console shows no registration
- **WHEN** the record of an ended worker is dropped because its registration evidence cannot be kept, and the console that the run reads shows the exit marker and no registration evidence
- **THEN** the run records the worker as futile, on what the console shows now

#### Scenario: A dropped record of an unlisted instance is no evidence
- **WHEN** the record of an ended worker is dropped because its registration evidence cannot be kept, and the provider no longer lists the instance
- **THEN** the run writes no record for it
- **AND** the brake's reader has no evidence about that worker

#### Scenario: A dropped record that the run does not read is no evidence
- **WHEN** more dropped records of listed instances remain than the per-run read budget allows
- **THEN** the run reads at most `MAX_CONSOLE_READS` consoles and notes `console_read_deferred`
- **AND** a record that waits for a later run is absent from the file that this run writes

#### Scenario: A record dated after the clock of the run is dropped
- **WHEN** the command loads a record whose `seen` is after the clock of its run, whether the worker has ended or not
- **THEN** it drops that record only and notes `state_rebuilt`
- **AND** a record whose `seen` equals the clock stays, as the brake's reader keeps it

#### Scenario: A finished record dated ahead cannot keep the file malformed
- **WHEN** a clock that was set back leaves the record of a finished worker dated after the clock of the run, beside the records of three futile workers, so that the brake's reader calls the file `malformed`
- **THEN** after one successful run the brake's reader calls the file `fresh` and finds the hold that the three futile workers give
- **AND** the run gave the dropped record no new time

#### Scenario: A record dated ahead is read again while the provider lists the instance
- **WHEN** the record of a listed instance is dropped because it is dated after the clock of the run
- **THEN** the run reads that console again within its per-run read budget and dates the new record by that read
- **AND** the record shows what the console shows now, and not what the dropped record showed

#### Scenario: A record dated ahead of an unlisted instance is no evidence
- **WHEN** the record of an instance is dropped because it is dated after the clock of the run, and the provider no longer lists the instance
- **THEN** the run writes no record for it
- **AND** the brake's reader has no evidence about that worker

#### Scenario: A dropped record dated ahead costs only its own history
- **WHEN** the command drops a record because it is dated after the clock of its run
- **THEN** the cumulative counters count that worker again, and the run emits its markers again under their stable identities
- **AND** a worker that still runs loses what earlier reads of its console found, because its record shows what the console shows now
- **AND** a clock set back by more than the age of the newest records drops every record dated ahead in the same run, the records of live workers included
- **AND** the command keeps every other record

#### Scenario: A dropped record costs only its own history
- **WHEN** the command drops a record because its registration evidence cannot be kept
- **THEN** the cumulative counters count that worker again, and the run emits its markers again under their stable identities
- **AND** the command keeps every other record

#### Scenario: A value that the command keeps with its evidence does not drop the record
- **WHEN** a record holds a Spot notice whose registration identifier the command normalises, or a `steps` list with a name outside the vocabulary
- **THEN** the command keeps the record with the evidence that it carries

#### Scenario: A record of an ended worker that lacks a registration key is dropped
- **WHEN** the record of a worker that has ended lacks `job_process`, `spot_interruption`, `steps` or `registered`, as a record that an older version wrote does
- **THEN** the command drops that record only and notes `state_rebuilt`
- **AND** it does not keep the record with a default for the key, because a worker that has ended is not read again and the run would write the record back under a fresh modification time as a futile launch that nobody observed

#### Scenario: A record of a worker that has not ended may lack a registration key
- **WHEN** the record of a worker that has not ended lacks one of those keys
- **THEN** the command keeps the record, and the brake's reader does not count it, because the worker has not ended
- **AND** while the provider lists the instance, the run reads its console again within its per-run read budget, and the console fills the key

#### Scenario: A bad provider state, operation or exit code leaves the record in place
- **WHEN** a record holds a `state`, `operation` or `exit_code` that the command cannot keep
- **THEN** the command keeps the record with that value replaced by null
- **AND** the brake's reader reads the null as no evidence for that record

#### Scenario: The brake can read what the command writes
- **WHEN** the command writes a record for a worker that ended after printing the exit marker and no registration evidence
- **THEN** the controller's reader classifies that worker as futile
- **AND** it classifies a worker that registered as registered

#### Scenario: The brake accepts every marker the command writes
- **WHEN** the command writes a record with a one-job process outcome or a Spot interruption notice, including a notice whose registration id is null
- **THEN** the controller's reader accepts the file
- **AND** it classifies the worker as registered
