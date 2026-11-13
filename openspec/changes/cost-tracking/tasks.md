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
- [x] 1.3 Record the incidental rate and the non-compute buffer on admission and
  use them for accrued, remaining and committed cost; fall back to the
  configured values only for a row that predates the record.
- [x] 1.4 Count the 60-second billing minimum once: `billable_seconds` is the
  floored billed duration, and remaining life starts there.
- [x] 1.5 Cover the 0/1/59/60/61-second boundaries at a 60-second and a
  3_600-second lifetime, the terminal minimum, the frozen basis, the
  missing-basis fallback and the reservation floor in `tests/test_cost.py`.

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
  copy failure, size mismatch and the missing-parameter refusal in
  `tests/test_ledger.py`.
- [x] 2.7 Raise `SCHEMA_VERSION` to 3 and add nullable
  `admitted_incidental_per_hour_micros` and `admitted_buffer_micros`; migrate a
  version-1 or version-2 file to version 3 in one transaction after the
  `.pre-v3.` copy.
- [x] 2.8 Backfill non-terminal rows from the configured parameters, keep
  terminal rows and their settled cost untouched, and accept versions 1, 2 and 3
  read-only.
- [x] 2.9 Use each operation's recorded buffer in `window_totals`.
- [x] 2.10 Cover the v1 and v2 upgrades, the single `.pre-v3.` copy, the
  terminal/non-terminal backfill split, the read-only v2 path, the copy failure
  and the restored backup in `tests/test_ledger.py`.

## 3. Controller

- [x] 3.1 Take the writer lock before the writable connect so the upgrade cannot
  race a second writer.
- [x] 3.2 Record the accepted quote's price at admission and on every capacity
  fallback; record the provider launch time on reconcile.
- [x] 3.3 Settle on the terminal transition, attach an identity observed only at
  termination before settling, settle a full lifetime at the accepted price for
  an unobserved instance, and settle zero for a definite pre-launch failure.
- [x] 3.4 Admit against spend plus commitment plus one worst-case worker, with
  an operation's remaining life floored at its accepted price.
- [x] 3.5 Report `spend`, `committed` and `remaining` from `apply`, `plan` and
  `status`; include the incidental rate in `hourly_ceiling`.
- [x] 3.6 Rewrite the cost-scope note and keep the "estimate, not a bill cap"
  language.
- [x] 3.7 Cover price recording, settlement, the unobserved case, one-shot
  settlement, an uncertain launch first observed terminated, admission through
  the age-out settlement, and a ceiling lowered between applies in
  `tests/test_controller.py`; move the batch and status expectations to the new
  accounting.
- [x] 3.8 Release the writer lock when a writable connect fails, covered by a
  controller test that retries through a fresh ledger.
- [x] 3.9 Bind each operation's lifetime and price to the evidence stored at
  admission, so lowered configuration affects only later launches; cover a
  lowered lifetime between applies in `tests/test_controller.py`.
- [x] 3.10 Price accrued and remaining cost from the admitted basis and the
  frozen request ceiling, so a configuration edit cannot reprice an outstanding
  operation; cover the changed-configuration case and the next admission in
  `tests/test_controller.py`.
- [x] 3.11 Settle an unobserved instance at the admitted ceiling rather than a
  raised configured maximum.

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
- [x] 6.5 Regenerate the changelog; release-please owns `CHANGELOG.md` from the
  merged conventional commits (the bespoke renderer was removed in #101).
- [x] 6.6 Document the frozen admitted basis, the single-count rule, the surplus
  CPU credit residual and the settlement bound in `docs/operations.md`; the
  schema-3 `.pre-v3.` backup and restore in `docs/container.md`; and that cost
  settings apply to new admissions in `docs/deployment.md`.

## 7. Verification

- [x] 7.1 `python -m unittest discover -s tests` and `npm run check:policy`
  pass; `npm run check:spec` validates the deltas.
- [ ] 7.2 After the operator merges and the deployment promotes the image: one
  writable run upgrades the ledger, reports the recomputed spend and commitment,
  and creates the backup; read back one settled operation.
- [ ] 7.3 After one completed day, compare tracked spend with the provider's cost
  report and record the difference and its cause.
