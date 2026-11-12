# Design

## Context

See [proposal.md](proposal.md) for motivation. The current accounting is one
column: `operations.reservation_micros`, filled at admission with
`max_spot_price × max_lifetime + non_compute_allowance` and summed per window by
`Ledger.consumed_micros`. It is a launch counter expressed in money. The
controller already receives everything a real estimate needs: the Spot quote of
the accepted request, the provider launch time on every reconcile, and the
provider terminal state.

## Goals / Non-Goals

**Goals:** report tracked cost that an operator can reconcile against the bill;
keep admission bounded by the allowance; upgrade an existing ledger without data
loss; preserve the read-only observation contract; keep the numbers exact
integers.

**Non-Goals:** reading the provider's billing API, allocating cost by tag,
retiring historical rows, per-job accounting, or changing the allowance, ceiling,
lifetime and placement behaviour.

## Decisions

### The accepted quote, not the current price

Cost is `ceil(price × billable_seconds / 3600) + ceil(incidental × billable_seconds / 3600)`.
`price` is the quote that was stored when the accepted request was written, with
the configured maximum as the fallback for a row that predates cost tracking or
lost the write. The current Spot price is not used: it describes now, not the
minutes that were billed. Provider billing data is not used either: it arrives a
day late and needs a permission the scaling identity does not carry.

### Unlived time is committed at the maximum, not at the quote

Admission needs an upper bound, not an estimate. For a non-terminal operation the
commitment is its accrued cost plus the remaining lifetime priced at the higher
of the configured maximum and the operation's accepted price, plus the
non-compute buffer. The accepted-price floor matters because an operator can
lower the ceiling while a worker is alive; without it the commitment would shrink
below the settlement that worker will still incur. For the same reason the
lifetime bound comes from the operation's stored expiry window, not from the
current configuration: lowering `max_lifetime_seconds` applies to the next launch
and cannot shorten a worker that is already running. An admitted worker is therefore priced at
`max_spot_price × max_lifetime + incidental × max_lifetime + buffer`, the earlier
reservation plus the incidental term; the difference is that its commitment
shrinks with every second it has lived, and disappears when it settles. A settled operation keeps only its settled cost.
This preserves the property that total spend cannot exceed the allowance while
admitting many more workers per window.

### The admitted cost basis is frozen per operation

The accepted quote, the stored lifetime and the frozen request already tie most
of an operation's cost to its admission. The two remaining inputs — the
incidental hourly rate and the non-compute buffer — were read from the current
configuration on every estimate, so lowering `incidental_per_hour`,
`non_compute_allowance` or `max_spot_price` silently repriced every outstanding
operation: what it had already accrued and what it still committed. Admission now
records both inputs on the operation, and every estimate uses the recorded value,
falling back to the configured one only for a row that predates the record. The
price ceiling is the highest of the configured maximum, the `MaxPrice` frozen in
the operation's request and its accepted price, so lowering the configured
maximum cannot shrink the ceiling of a request already sent. No cap column is
added: the admitted ceiling already lives in the frozen request, and a malformed
one is parsed tolerantly rather than raising while cost is reported.

A configuration change therefore applies to new admissions only. Terminal rows
keep their stored settled cost and are never repriced. A non-terminal row whose
stored reservation is higher than what its recorded basis reproduces — the
best-effort backfill of the schema-3 upgrade — keeps that reservation as a floor,
so the upgrade cannot release a commitment whose real inputs are unknown.

Rejected alternative: refuse a cost-policy change while operations are
outstanding. It cannot be enforced where the edit is made, because the
configuration is read by the next run rather than validated against the ledger,
and it turns a routine edit into an admission outage: every outstanding worker
would block `apply` until it settled, with no way to distinguish a harmless
change from a dangerous one. It also cannot express a mixed fleet, and it does
not fix the estimate for read-only observation, which never runs an apply.
Freezing the inputs is smaller, needs no operator procedure and keeps the
collision where it belongs: one row, one basis.

### The billing minimum is billed once

`billable_seconds = max(60, elapsed_seconds)` is the billed duration, and the
remaining life starts there: `remaining_seconds = max(0, lifetime_seconds -
billable_seconds)`. Before this, accrued cost used the floor while remaining cost
subtracted only real elapsed time, so the first minute was charged twice and an
immediately affordable two-worker batch reported almost twice its whole
commitment. A terminal short-lived worker still settles at the one-minute
minimum, and the per-component round-up in `hourly_cost_micros` stays
conservative: a whole lifetime costs at most one micro more than its exact
price.

### Settlement, and the one case with no evidence

An operation settles when a reconcile first observes its instance `terminated`,
using the provider launch time and the observation time (within one scheduler
tick). A worker that never appears again is settled at a full lifetime at its accepted
price — or at the `MaxPrice` frozen in its request when no price was recorded, and
at the configured maximum only for a row whose request carries none — after the
provider retention window. The lifetime is never shorter than the evidence
allows, and that bound keeps the charge inside the commitment the operation
carried while unresolved; a raised configuration never settles above it. A
definite pre-launch rejection already records `failed` with a zero reservation
and settles zero. The write itself is one-shot: it only fills a row with no
settled cost, so a later reconcile, retry or configuration edit cannot rewrite
settled history.

### Schema upgrades with a verified copy, and readable predecessors

A version-1 file is upgraded by a writable connect, which the controller calls
while holding the writer lock: copy the file to `state.sqlite3.pre-v2.<epoch>`,
compare sizes, then `ALTER TABLE`, backfill terminal rows from their recorded
timestamps at the configured price, add the window index, and set
`user_version = 2` in one transaction. A failed copy refuses before the first
write. Read-only commands accept version 1 and version 2, so the auto-deploy
smoke and the export timer keep working between promotion and the first apply.

Schema 3 reuses that procedure. A version-1 or version-2 file is copied to
`state.sqlite3.pre-v3.<epoch>`, size-checked, then migrated to version 3 in one
transaction; a version-1 file passes through the version-2 shape in the same
transaction. The upgrade adds two nullable columns,
`admitted_incidental_per_hour_micros` and `admitted_buffer_micros`. Terminal rows
keep `NULL` admitted inputs and their stored settled cost, so settled history is
never rewritten; nonterminal rows record the incidental rate and buffer supplied
by the caller, which is the configuration at upgrade time — a best-effort
backfill, because the inputs their admission actually used are not recoverable
from an earlier file. Read-only commands accept versions 1, 2 and 3. Rollback is
restoring the `.pre-v3.` copy; the previous release refuses version 3, so the
restore is the only supported way back.

Rejected alternatives: storing the cost fields inside `detail` (unparseable
audit text), a side table without a version (the version guard would stop
describing the schema), bumping the version without read tolerance (the deploy
smoke would block the promotion it is testing), and reserving only at the quote
(under-counts a price rise and weakens the allowance bound).

### The bill stays the operator's reference

The tracked number is an estimate: Spot price movement, data transfer, AMI and
snapshot storage, minimum increments and failed cleanup are outside it. The
operator reconciles a completed day against the provider's cost report; the
product does not carry billing permissions, and the metric is not described as a
guaranteed bill cap.

### Explicit series names

`ci_burst_ledger_consumed_micros` is replaced by
`ci_burst_spend_micros`, `ci_burst_committed_micros`,
`ci_burst_allowance_micros` and `ci_burst_remaining_micros`. No in-estate
dashboard or alert consumed the old name; keeping it as an alias would preserve
the ambiguity this change removes.

## Risks

- **Price movement within a run** makes the estimate differ from the bill; the
  commitment at the maximum absorbs the part that matters for admission, and the
  daily reconciliation measures the residual.
- **Migration rollback** loses operations created after the backup. The rollback
  restores the copy first and reports any live instance without a ledger
  operation as a discrepancy, which blocks admission rather than over-admitting.
- **Backfilled admitted inputs are best effort**: a row that predates schema 3
  records the incidental rate and buffer configured at upgrade time, not
  necessarily the ones its admission used. The stored reservation is its floor,
  so the approximation can only over-commit, never release allowance early.
- **Ledger growth**: the table gains one row per launch. The windows query is
  index-supported; retention is left to a later change.
