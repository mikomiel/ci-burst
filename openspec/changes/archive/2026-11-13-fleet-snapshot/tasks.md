# Tasks

These are product-source tasks. They authorize no deployment, no credential and
no cloud change.

## 1. Registration contract

- [x] 1.1 Add `ForgejoObserver.runners()` with its own contract name and refusal
  codes, reading the repository registration list with the existing token.
- [x] 1.2 Validate every registration record: name, status, labels and the
  ephemeral flag; count active and offline separately.
- [x] 1.3 Count running attempts in `Observation`, only for attempts whose labels
  the configured worker supports, and keep them out of demand.

## 2. Ledger

- [x] 2.1 Raise `SCHEMA_VERSION` to 4 and add the `fleet_observations` table.
- [x] 2.2 Upgrade a version-1 or version-2 file through the existing migration,
  landing on schema 4 after one size-checked copy.
- [x] 2.3 Upgrade a version-3 file by creating the table after a size-checked
  `.pre-v4.` copy.
- [x] 2.4 Add `record_fleet_observation` with seven-day pruning and
  `latest_fleet_observation`, which reports no snapshot on a version-3 file.

## 3. Controller

- [x] 3.1 Read the registration snapshot once per pass after the admission
  decision — and after the launch when the pass launches — and report it in the
  result.
- [x] 3.2 Report a refused read as `fleet.available: false` without refusing the
  pass, and a refused write as `persisted: false`.
- [x] 3.3 Report the newest snapshot in `status` without a new credential.
- [x] 3.4 Collect the snapshot after the decision and, on launch, after the
  launch; skip it when the remaining pass budget is below the Forgejo timeout.

## 4. Export

- [x] 4.1 Read the newest snapshot with the existing read-only ledger open.
- [x] 4.2 Emit the active, running and idle gauges and the observation
  timestamp; omit the count gauges when the snapshot is older than fifteen
  minutes and say so.
- [x] 4.3 Keep the exporter free of any Forgejo credential.

## 5. Documentation

- [x] 5.1 Describe the snapshot, the non-atomic caveat, the idle estimate and
  the gauges in `docs/operations.md`.
- [x] 5.2 Describe the schema-4 upgrade and the `.pre-v4.` rollback in
  `docs/container.md`.

## 6. Verification

- [x] 6.1 Cover the registration parser, the running-attempt count and the
  refusal codes in `tests/test_forgejo.py`.
- [x] 6.2 Cover the schema-4 upgrade, the read-only version-3 case, recording,
  newest-read and pruning in `tests/test_ledger.py`.
- [x] 6.3 Cover the result, the failure policy and status in
  `tests/test_controller.py`; the gauges, freshness and omission in
  `tests/test_export.py`.
- [x] 6.4 Run the full test suite, `npm run check:policy` and
  `npm run check:spec`.
