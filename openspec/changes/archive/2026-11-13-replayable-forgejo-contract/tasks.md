# Tasks

These are product-source tasks. They authorize no deployment, no credential, no
live Forgejo request and no cloud change.

## 1. Contract reading

- [x] 1.1 Record Forgejo `v17.0.1`, `GetActionRunJobs`,
  `routers/api/v1/shared/runners.go` and the `ActionRunJob` response record in
  `tests/fixtures/forgejo/contract.json`; state that it is a contract reading,
  not a live observation.
- [x] 1.2 Add the absent-`labels`, non-matching-filter, missing/null `needs`,
  empty `runs_on`, terminal-status, mixed waiting/running, retry-new-handle and
  partial-record fixtures.
- [x] 1.3 Record each fixture's request variant, provenance and expected
  demand, counters, notes or refusal.

## 2. Matrix test

- [x] 2.1 Replay every fixture through the real observer and compare the result
  with the manifest.
- [x] 2.2 Assert the manifest covers every fixture exactly once.
- [x] 2.3 Assert the adapter always sends an explicit, non-empty `labels`
  parameter; feed the absent-filter fixture to show that shape is unreachable.

## 3. Documentation

- [x] 3.1 Add the replayable-contract subsection to `docs/operations.md`
  § Qualifying live support: what the matrix proves, what only an isolated
  canary proves, the server and runner versions it must record and the
  requalification rule.
- [x] 3.2 Correct the absent-`labels` claim in the queue contract and in the
  adapter docstring.

## 4. OpenSpec

- [x] 4.1 Add the `queue-observation` requirement and scenarios for replayable
  contract evidence and operator-only live compatibility.

## 5. Verification

- [x] 5.1 `python3 -m unittest discover -s tests -p 'test_*.py'`
- [x] 5.2 `node --test tests/workflow-policy.test.mjs`
- [x] 5.3 `./node_modules/.bin/openspec validate --all --strict --no-interactive`
- [x] 5.4 Gitleaks scan of the commit range.
