# Proposal

## Why

Two audit findings share one blind spot: the controller acts on durable launch
intent without binding it to the configured deployment, and its planning
evidence subtracts one reservation regardless of the batch it reports.

R02: `apply` and `plan` compare the Region and account of every outstanding
operation but not the deployment that owns it. Reusing a ledger after renaming
`deployment` therefore resumes a frozen request whose ownership tag names the
old deployment. The retry is accepted under the new name while `status` and
cleanup select a different ownership filter, so the operator cannot tell which
deployment owns the worker.

R06: `budget.remaining_after` always subtracts one `per_launch_reservation`.
A zero-worker plan can report negative headroom, and a multi-worker plan
overstates the allowance left after its own planned batch. The admission count
is already correct, so this is decision evidence rather than an excess launch.

## What Changes

- Read the deployment from the frozen request's `ci-burst:deployment` tag for
  every nonterminal operation. A nonterminal operation whose recorded
  deployment is non-null and differs from the configured one is reported as
  `SCOPE_CHANGED`, with the recorded deployment in the payload, and is not
  resumed, expired or counted as scoped state.
- Refuse `apply` before the resume and expiry paths when that discrepancy
  exists, and refuse `plan` with the same stable reason without an AWS call.
- Treat a row with no readable deployment tag as unbound. The controller never
  writes such a row, so this tolerates a corrupt or foreign row instead of
  inventing a refusal.
- Keep terminal and failed rows as history and settled cost. A drained ledger
  may serve a new deployment; only nonterminal state binds a scope.
- Compute `budget.remaining_after` as `allowance - committed - launch_count *
  per_launch_reservation`, so zero-, one-, multi-worker and budget-capped plans
  report the allowance left after the batch they report.
- Extract the repeated `SCOPE_CHANGED` literal into one module constant.

## Impact

- A deployment-name change with outstanding intent becomes an explicit refusal
  instead of a silent resume; no `RunInstances` call, ledger write, retag or
  token replacement occurs, and the old row remains attributable to its
  deployment.
- The same deployment still resumes the same frozen request and token
  idempotently.
- `remaining_after` changes only its display value; `affordable`, `launch_count`
  and admission are unchanged.
- No ledger schema change, no dependency, no new column, and no change to
  cleanup, the cost model, the AWS adapter or the CLI.
