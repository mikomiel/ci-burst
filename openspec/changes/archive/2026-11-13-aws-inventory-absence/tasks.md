# Tasks

These are product-source tasks. They authorize no deployment, no credential and
no cloud change.

## 1. Per-identifier absence

- [x] 1.1 Keep the single by-ID batch read in `Aws.instances` first.
- [x] 1.2 On `InvalidInstanceID.NotFound`, re-read each identifier on its own,
  treat the same code as absence for that identifier only and collect the
  records that exist.
- [x] 1.3 Translate every other per-identifier failure with `_translate`, so it
  is an uncertain refusal and the reconcile transaction rolls back instead of
  reporting an empty list.
- [x] 1.4 State in the method docstring that absence is established per
  identifier and that a provider read failure refuses.

## 2. Documented SDK refusal

- [x] 2.1 Add `_sdk_config()` that imports `botocore.config.Config` under the
  `AWS_SDK_UNAVAILABLE` refusal.
- [x] 2.2 Use it in `_client()` and `account_id()`; leave `_session()`, the
  retry policy and the injected-client path unchanged.
- [x] 2.3 Add the `AWS_SDK_UNAVAILABLE` row to the refusal table in
  `docs/deployment.md`.

## 3. Acceptance coverage

- [x] 3.1 Adapter: a mixed not-found batch returns only the live record and
  makes one batch call plus one call per identifier.
- [x] 3.2 Adapter: a per-identifier read failure raises an uncertain refusal (the
  translated reason is kept; an unclassified code becomes `AWS_UNCERTAIN`)
  and never an empty list; the existing aged-out single identifier still returns
  an empty result.
- [x] 3.3 Adapter and CLI: a missing `boto3`/`botocore.config` returns
  `AWS_SDK_UNAVAILABLE` from both live entry paths with no credential
  configuration in the detail, and the CLI exits 2 with that reason.
- [x] 3.4 Controller end-to-end with the real adapter: the live operation stays
  nonterminal with its slot occupied and an `OWNERSHIP_TAGS_MISSING` discrepancy,
  while the genuinely aged-out operation keeps the conservative settlement.

## 4. Validation

- [x] 4.1 The full standard-library suite, `node --test
  tests/workflow-policy.test.mjs` and `openspec validate --all --strict
  --no-interactive` pass on the change.
- [x] 4.2 The pinned Gitleaks version scans the commit range, the intended tree
  and the commit messages with `.gitleaks.toml`.
