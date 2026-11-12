# Spec Delta

## ADDED Requirements

### Requirement: Cleanup outcome and evidence

An applied cleanup pass that refused any owned resource MUST report
`cleanup_degraded` and exit nonzero; `termination_requested` MUST mean none was
refused. `cleanup_failed` stays the provider refusal. Malformed ownership MUST
NEVER authorize termination. The result MUST carry one `cleanup` object from the
same read with verification, clocks and observed, skipped, terminated,
terminating, expired, requested, refused, denied and unresolved counts. A
request MUST NOT count as a confirmed removal.

#### Scenario: Every candidate is refused

- **WHEN** an applied cleanup pass refuses every owned candidate and sends no
  termination request
- **THEN** the decision is `cleanup_degraded`
- **AND** the command exits nonzero
- **AND** `termination_requested` is empty and no confirmation is emitted
- **AND** the provider receives no terminate call

#### Scenario: Proven and refused resources share one inventory

- **WHEN** one owned instance has a valid expired expiry tag and another has a
  malformed tag
- **THEN** only the proven instance is terminated
- **AND** the result reports `cleanup_degraded`
- **AND** both the expired and the refused outcome are retained

#### Scenario: Inventory is verified empty

- **WHEN** the complete owned-instance read returns no records
- **THEN** the decision is `termination_requested` with an empty request list
- **AND** the cleanup block reports `inventory_verified: true` and zero counts
- **AND** the run does not claim a confirmed removal

#### Scenario: Malformed ownership or expiry

- **WHEN** an owned instance has a missing, empty, `pending` or malformed
  ownership or expiry tag
- **THEN** it is reported as refused
- **AND** no termination is requested for it

#### Scenario: Heartbeat is absent or stale

- **WHEN** no cleanup document exists for the interval, or the newest one is old
- **THEN** the cleanup path is unobserved
- **AND** that absence is not evidence of an empty inventory, a zero bill or a
  completed cleanup

#### Scenario: Delayed terminal readback

- **WHEN** a run requests termination and later inventory reads report
  `shutting-down` and then `terminated`
- **THEN** the requesting run reports a request, not a confirmed removal
- **AND** only the read that observes `terminated` reports a terminal count

#### Scenario: No confirmed removal without a provider terminal state

- **WHEN** the provider has not reported a terminal state for a requested or
  refused instance
- **THEN** the cleanup evidence keeps it unresolved
- **AND** a requested or denied termination is not counted as removed
