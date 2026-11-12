# Design

## Context

See [proposal.md](proposal.md). Two findings share one area of decision
evidence: what durable state an admission is allowed to act on, and what the
planning output says remains after its own planned batch.

The ledger already stores the complete `RunInstances` request, frozen before the
provider call, and every request produced by `_request` carries the
`ci-burst:deployment` ownership tag. The deployment is therefore already durable
evidence; it only needed to be read and compared. Adding a ledger column would
duplicate that record and require a schema migration.

## Goals / Non-Goals

**Goals:** refuse a deployment-name change before any resume or expiry; keep the
frozen request, token and tags untouched on refusal; keep the same-deployment
retry idempotent; allow an intentionally drained ledger to serve a new
deployment without discarding history; report batch headroom that matches the
reported batch.

**Non-Goals:** a ledger schema change, retagging or rewriting historical rows,
automatic cleanup of another scope, making `status` or `reap` refuse (they remain
read-only reports), or changing admission, the cost model, the AWS adapter, the
CLI or the reaper.

## Decisions

### Read the deployment from the frozen request, not a new column

The frozen request is the immutable record of ownership. A tag read is
available on every `_request` output and on ordinary rows created by an earlier
release, so no migration, backfill or second source of truth is needed. A row
whose request has no readable deployment tag cannot have come from `_request`;
it is treated as unbound and does not refuse. That is the residual risk: a
corrupt or foreign row cannot be attributed and is ignored by this rule rather
than blocking every future deployment.

### Refuse before the resume and expiry paths

The scope check belongs in `_reconcile`, which runs before `_resume` and
`_expire`. A nonterminal foreign operation is reported as a `SCOPE_CHANGED`
discrepancy and excluded from the scoped set, so `apply` returns the existing
`owned_state_discrepancy` refusal before any resume, expiry or admission. The
payload carries `operation`, `reason`, `region`, `account` and, for this
finding, the recorded `deployment`; it never carries the frozen request or its
token. `plan` repeats the Region and deployment checks from the ledger without
an AWS call, so a read-only decision cannot disagree with `apply`.

### Only nonterminal state binds a deployment

Terminal and failed rows are history and settled cost. Refusing a new deployment
because a retired row carries the old name would force an operator to delete
audit evidence. The rule is therefore drain-and-retain: once every row is
terminal or failed, a new deployment may use the same ledger and append new
rows; the old rows keep their state, request, hash and reservation.

### Report the headroom of the planned batch

`remaining_after` is display evidence for the decision that has already been
made. `affordable` and `launch_count` are admission inputs and stay unchanged;
only the subtraction uses the admitted count. The arithmetic is exact integer
micros, so a zero-worker plan reports the unspent allowance instead of a
negative number, and a capped batch subtracts only what it admitted.

## Risks / Trade-offs

- A corrupt row without a deployment tag is unbound and can be resumed under
  the current name. It cannot be produced through `_request`, so this is a
  tolerated corruption rather than a supported path; the operation's Region and
  account still apply.
- The refusal does not clean up another deployment's resources. The operator
  must drain or resolve them in the original scope; `docs/deployment.md` records
  the rename procedure.
- `remaining_after` remains planned evidence. It is not a measure of completed
  launches or provider bills, and its documentation says so.

## Alternatives considered

- **Store the deployment in a new ledger column.** Rejected: the frozen request
  already carries it immutably, and a second copy could disagree with the
  request a resume actually sends.
- **Rewrite or retag the frozen request on rename.** Rejected: the request is
  the idempotency record. Rewriting it would change the operation's identity and
  is exactly the silent ownership transfer the finding reports.
- **Delete terminal rows when the name changes.** Rejected: it discards the
  settled-cost history the allowance window and audit depend on.
