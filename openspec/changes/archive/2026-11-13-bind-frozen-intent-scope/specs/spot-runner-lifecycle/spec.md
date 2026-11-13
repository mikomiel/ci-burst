# Spec Delta

## ADDED Requirements

### Requirement: Frozen intent carries its deployment scope
Every nonterminal operation MUST be bound to the deployment in its frozen request's `ci-burst:deployment` tag. `apply` MUST refuse with `SCOPE_CHANGED` before resuming or expiring an operation whose recorded deployment is non-null and differs from the configured one, with no `RunInstances` call and no change to the request, hash, token or tags. `plan` MUST do the same without an AWS call. A row without a readable tag MUST be unbound. Terminal rows MUST NOT refuse a new deployment.

#### Scenario: Deployment name changes while an intent is outstanding
- **WHEN** a nonterminal operation's frozen request names deployment A and the configuration now names deployment B
- **THEN** `apply` refuses with `SCOPE_CHANGED` and `owned_state_discrepancy`
- **AND** no `RunInstances` request is sent
- **AND** the operation stays in its recorded state with the same request, hash and client token

#### Scenario: The refusal names the mismatch safely
- **WHEN** `apply` or `plan` refuses because of a deployment-scope change
- **THEN** the report names the recorded deployment and the operation
- **AND** it does not print the frozen launch payload, its tags or a secret

#### Scenario: Same deployment resumes idempotently
- **WHEN** the configured deployment equals the deployment in the frozen request
- **THEN** `apply` resumes the same request and client token
- **AND** it records one operation row and sends one retry

#### Scenario: A drained ledger is reused with its history retained
- **WHEN** every row in the ledger is terminal or failed and the configured deployment differs from the rows' recorded deployment
- **THEN** `apply` launches normally under the new deployment
- **AND** the old rows keep their state, request, hash and reservation

### Requirement: Batch headroom is reported per planned launch
`budget.remaining_after` MUST report the allowance left after the planned batch: `allowance - committed - launch_count * per_launch_reservation`. It MUST subtract the reported `launch_count`, including zero, and MUST NOT change `affordable`, `launch_count` or admission. The value is planned headroom, not proof of completed launches.

#### Scenario: Zero workers planned
- **WHEN** admission plans no worker because the allowance cannot fund one
- **THEN** `remaining_after` equals `remaining`
- **AND** no reservation is subtracted

#### Scenario: One worker planned
- **WHEN** admission plans one worker
- **THEN** `remaining_after` subtracts one per-launch reservation

#### Scenario: Several workers planned
- **WHEN** admission plans several workers within the allowance
- **THEN** `remaining_after` subtracts one reservation per planned worker

#### Scenario: The allowance caps the batch
- **WHEN** the allowance admits fewer workers than the demand
- **THEN** `remaining_after` subtracts only the admitted count
- **AND** admission still launches exactly that count
