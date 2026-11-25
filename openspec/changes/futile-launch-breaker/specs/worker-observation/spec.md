# Spec Delta

## MODIFIED Requirements

### Requirement: Rebuildable dedupe state

The command MUST persist what it has already emitted, MUST prune those
per-instance records after a fixed retention, and MUST treat the record as
derived observation. The record MUST NOT influence admission, launch or
termination, except through the one read-only brake that the bounded-operation
capability defines, which only withholds launches. The command MUST rewrite the
record at the end of every successful run, even when nothing changed, because the
file's modification time is that brake's freshness signal. A change to the
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

#### Scenario: The record is deleted
- **WHEN** the record is deleted
- **THEN** the controller reports the evidence state `missing` and admits as it would without the brake
- **AND** the next run rebuilds records for the owned instances that the provider still lists, stamped with that run's time
- **AND** the emitted history and the cumulative counters are lost as before

#### Scenario: The brake can read what the command writes
- **WHEN** the command writes a record for a worker that ended after printing the exit marker and no registration evidence
- **THEN** the controller's reader classifies that worker as futile
- **AND** it classifies a worker that registered as registered
