# Tasks

These are product-source tasks. They authorize no deployment, no credential and
no cloud change.

## 1. Cost model

- [x] 1.1 Add `ci_burst/cost.py` with `CostModel`: accrued cost from the accepted
  price and elapsed time, remaining worst case at the configured maximum, buffer,
  full-lifetime and per-launch helpers, exact micro-units and a 60-second floor.
- [x] 1.2 Cover accrual, the billing minimum, the lifetime cap, terminal freezing,
  the price fallback, incidentals and commitment arithmetic in
  `tests/test_cost.py`.

## 2. Ledger

- [x] 2.1 Raise `SCHEMA_VERSION` to 2 and add `price_micros`, `launch_time`,
  `settled_micros`, `settled_at` and the window index; materialise older rows with
  defaults.
- [x] 2.2 Upgrade a version-1 file once under the writer lock: verified byte copy,
  `ALTER TABLE`, terminal backfill at the configured price, one transaction.
- [x] 2.3 Accept version 1 and 2 in the read-only path without writing; refuse an
  unsupported version as before.
- [x] 2.4 Replace reservation-sum accounting with `window_totals`, which returns
  tracked spend and the forward commitment for a window.
- [x] 2.5 Add `set_launch_time` and one-shot `settle` to the ledger.
- [x] 2.6 Cover commitment, carryover, restart, read-only tolerance, migration,
  backup failure and the missing-parameter refusal in `tests/test_ledger.py`.

## 3. Controller

- [x] 3.1 Take the writer lock before the writable connect so the upgrade cannot
  race a second writer.
- [x] 3.2 Record the accepted quote's price at admission and on every capacity
  fallback; record the provider launch time on reconcile.
- [x] 3.3 Settle on the terminal transition, settle a full lifetime for an
  unobserved instance, and settle zero for a definite pre-launch failure.
- [x] 3.4 Admit against spend plus commitment plus one worst-case worker.
- [x] 3.5 Report `spend`, `committed` and `remaining` from `apply`, `plan` and
  `status`; include the incidental rate in `hourly_ceiling`.
- [x] 3.6 Rewrite the cost-scope note and keep the "estimate, not a bill cap"
  language.
- [x] 3.7 Cover price recording, settlement, the unobserved case and one-shot
  settlement in `tests/test_controller.py`; move the batch and status
  expectations to the new accounting.

## 4. Export

- [x] 4.1 Replace `ci_burst_ledger_consumed_micros` with `ci_burst_spend_micros`
  and add `ci_burst_committed_micros`, `ci_burst_allowance_micros` and
  `ci_burst_remaining_micros`.
- [x] 4.2 Report the same totals in the run summary and keep the ledger-omitted
  behaviour.
- [x] 4.3 Cover an empty ledger, a committed ledger and an absent ledger in
  `tests/test_export.py`.

## 5. Configuration

- [x] 5.1 Add optional `[budget].incidental_per_hour` (money string, default
  zero) to the budget table and `BudgetConfig`.
- [x] 5.2 Cover the default, parsing and the refusal of a float in
  `tests/test_config.py`.

## 6. Documentation

- [x] 6.1 Rewrite the budget section of `docs/operations.md` around tracked cost,
  commitment, settlement, reconciliation and the upgrade.
- [x] 6.2 Document `incidental_per_hour` and the meaning of the allowance in
  `docs/deployment.md`.
- [x] 6.3 Document the schema, the verified backup, the observation compatibility
  window and the rollback restore in `docs/container.md`.
- [x] 6.4 Update the cost paragraph in `README.md`.
- [x] 6.5 Regenerate the changelog with `scripts/render_changelog.py --write`.

## 7. Verification

- [x] 7.1 `python -m unittest discover -s tests` and `npm run check:policy`
  pass; `npm run check:spec` validates the deltas.
- [ ] 7.2 After the operator merges and the deployment promotes the image: one
  writable run upgrades the ledger, reports the recomputed spend and commitment,
  and creates the backup; read back one settled operation.
- [ ] 7.3 After one completed day, compare tracked spend with the provider's cost
  report and record the difference and its cause.
