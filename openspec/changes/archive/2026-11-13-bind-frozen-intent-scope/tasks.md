# Tasks

These are product-source tasks. They authorize no deployment, no credential and
no cloud change.

## 1. Frozen intent scope

- [x] 1.1 Add `_frozen_deployment(operation)` beside `_sha256`; read the
  `ci-burst:deployment` tag from the frozen request and return `None` for an
  absent, malformed or unreadable row.
- [x] 1.2 Add one `SCOPE_CHANGED` module constant and use it for every scope
  refusal.
- [x] 1.3 Extend the `apply` reconcile scope check: a nonterminal operation
  whose recorded deployment differs from the configured one is a
  `SCOPE_CHANGED` discrepancy carrying `deployment`, and is not resumed,
  expired or scoped.
- [x] 1.4 Mirror the Region and deployment checks in `plan` without an AWS call.

## 2. Batch headroom

- [x] 2.1 Subtract `launch_count * per_launch_reservation` in
  `remaining_after`; leave `affordable`, `launch_count` and admission unchanged.

## 3. Tests

- [x] 3.1 Rename refusal: no `RunInstances` call, no ledger write, unchanged
  request, hash and token; the payload names the recorded deployment without
  printing the frozen payload or a secret.
- [x] 3.2 `plan` refuses `SCOPE_CHANGED` and mutates nothing.
- [x] 3.3 Same-deployment resume stays idempotent: one retry, same token, one
  row.
- [x] 3.4 A terminal-only ledger serves a new deployment and retains its old
  rows.
- [x] 3.5 Zero-, one-, multi-worker and budget-capped `remaining_after`, plus an
  apply-level assertion that admission still launches the planned count and
  stays inside the allowance.

## 4. Documentation

- [x] 4.1 `docs/operations.md`: the deployment scope, the drain-and-retain rule
  and `remaining_after` as planned-batch headroom.
- [x] 4.2 `docs/deployment.md`: the deployment-rename procedure.
