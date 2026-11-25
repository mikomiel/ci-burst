# Spec Delta

## MODIFIED Requirements

### Requirement: Rebuildable dedupe state

The command MUST persist what it has already emitted, MUST prune those
per-instance records after a fixed retention, and MUST treat the record as
derived observation. The record MUST NOT influence admission, launch or
termination, except through the one read-only brake that the bounded-operation
capability defines, which only withholds launches. The command MUST rewrite the
record at the end of every successful run, even when nothing changed, because the
file's modification time is that brake's freshness signal. That time marks the
command's own writes, which end with the record. It does not mark the publication
of the metrics textfile into the collector's directory, which the unit that runs
the command does afterwards. A change to the
record's version or to a field that the brake reads MUST ship with the matching
change to the brake's reader.

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

#### Scenario: A run that cannot write its textfile leaves the record alone
- **WHEN** a run cannot replace the metrics textfile, so the command exits 1
- **THEN** the record is not rewritten, because the command writes the record after the event file and the textfile
- **AND** its modification time ages, so the brake sees it as `stale` after 600 seconds
- **AND** the next run rediscovers the owned instances and reads again the consoles that it selects within its per-run read budget (`MAX_CONSOLE_READS`, 16, live workers first), so a marker that it still finds on one of them is emitted again under the same identity
- **AND** a console that the run defers past that budget (`console_read_deferred`) is not read in that run, so the run emits nothing for it
- **AND** an instance that left the provider's inventory, or a marker that scrolled out of the console, is not emitted again

#### Scenario: A run that cannot write the record does not reach publication
- **WHEN** a run replaces the metrics textfile and cannot replace the record, so the command exits 1
- **THEN** the record is not rewritten and its modification time ages, so the brake sees it as `stale` after 600 seconds
- **AND** the shipped unit does not publish the textfile into the collector's directory, because that step follows only a successful command
- **AND** the collector's last-run timestamp does not advance either

#### Scenario: The unit cannot publish the textfile
- **WHEN** a run completes and writes the record, and the shipped unit then cannot publish the textfile into the collector's directory
- **THEN** the record's modification time has advanced, so the brake sees the record as `fresh`
- **AND** a hold still rests on the record, because the evidence in it was read
- **AND** the collector's copy of the textfile is not replaced, so its last-run timestamp does not advance

#### Scenario: The record is deleted
- **WHEN** the record is deleted
- **THEN** the controller reports the evidence state `missing` and admits as it would without the brake
- **AND** the next run rebuilds records for the owned instances that the provider still lists, stamped with that run's time
- **AND** the emitted history and the cumulative counters are lost as before

#### Scenario: The brake can read what the command writes
- **WHEN** the command writes a record for a worker that ended after printing the exit marker and no registration evidence
- **THEN** the controller's reader classifies that worker as futile
- **AND** it classifies a worker that registered as registered

#### Scenario: The brake accepts every marker the command writes
- **WHEN** the command writes a record with a one-job process outcome or a Spot interruption notice, including a notice whose registration id is null
- **THEN** the controller's reader accepts the file
- **AND** it classifies the worker as registered
