# Spec Delta

## MODIFIED Requirements

### Requirement: Stable metrics textfile

The command MUST write one Prometheus textfile atomically, MUST expose counters
that do not fall when an instance terminates or when a per-instance record
expires, and MUST expose a per-run timestamp that a consumer can use to detect a
stopped exporter. Cumulative counters MUST be persisted separately from the
expiring per-instance records. When the ledger is readable, the textfile MUST
also expose tracked spend, forward commitment, allowance and remaining allowance
as gauges in micro-units of currency.

#### Scenario: A successful run
- **WHEN** the command completes
- **THEN** the textfile names the deployment on every series
- **AND** the run timestamp reflects that run
- **AND** launches and registrations are counters
- **AND** spend, commitment, allowance and remaining allowance are gauges

#### Scenario: A record expires
- **WHEN** a per-instance record passes the retention window
- **THEN** the record is pruned
- **AND** the cumulative counters keep the contribution it made

#### Scenario: The ledger is absent or unreadable
- **WHEN** the ledger cannot be read
- **THEN** the cost gauges are omitted and the run reports that the ledger was omitted
- **AND** the worker counters and the run timestamp are still published

#### Scenario: The write fails
- **WHEN** the textfile cannot be replaced
- **THEN** the previous textfile is unchanged and the command exits 1
