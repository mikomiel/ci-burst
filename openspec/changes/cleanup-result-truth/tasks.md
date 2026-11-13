# Tasks

These are product-source tasks. They authorize no deployment, no credential and
no cloud change.

## 1. Result and exit status

- [x] 1.1 `Controller.reap` reports `cleanup_degraded` for an applied pass with
  any refused owned resource, all-refused and mixed; a mixed pass still
  terminates only the proven expired targets.
- [x] 1.2 `cleanup_failed` stays the decision for a `ProviderRefusal` from
  `terminate`, `termination_requested` for a clean applied pass (including a
  verified empty inventory), and `planned` for a dry run.
- [x] 1.3 The `confirmation` sentence is emitted only when identifiers were
  actually sent to the provider.
- [x] 1.4 `cleanup_degraded` is a failed decision in the CLI, so the process
  exits `1`; exit `2` stays reserved for a complete refusal with no mutation.

## 2. Bounded evidence

- [x] 2.1 Add a top-level `cleanup` object, built from the same single
  `owned_instances` read, with `inventory_verified`, `attempted_at`,
  `completed_at`, `observed`, `skipped`, `terminated`, `terminating`,
  `expired`, `requested`, `refused`, `denied` and `unresolved`.
- [x] 2.2 Emit the block on a dry run, a clean apply, a degraded apply and a
  provider-refused termination, with `denied` set on the last.
- [x] 2.3 Keep `requested` and `terminated` distinct so a request is never
  reported as a confirmed removal; retain the existing field names.
- [x] 2.4 Document in the docstring and the operator docs that an absent or
  stale heartbeat proves nothing, and that a verified empty read is not a zero
  bill.

## 3. Tests

- [x] 3.1 All-refused apply reports `cleanup_degraded`, an empty
  `termination_requested`, zero `terminate` calls and no `confirmation`.
- [x] 3.2 Mixed inventory reports `cleanup_degraded`, terminates only the
  proven id and retains both the expired and the refused outcome.
- [x] 3.3 A verified empty inventory stays `termination_requested`; malformed
  operation or expiry tags never terminate.
- [x] 3.4 A delayed readback sequence running -> `shutting-down` -> `terminated`
  reports requested, then terminating, then terminated, and never a confirmed
  removal before the terminal read.
- [x] 3.5 A denied termination reports `cleanup_failed` with `denied` set.
- [x] 3.6 The CLI exit-code table includes `cleanup_degraded` -> `1`, with an
  end-to-end real-`Controller` + `FakeAws` `reap --apply` case that exits `1`
  and makes no provider mutation.
- [x] 3.7 `tests/test_deploy_units.py` guards that the reap unit carries no
  `SuccessExitStatus` and no shell masking; the unit file is unchanged.

## 4. Documentation

- [x] 4.1 `docs/operations.md`: exit codes, cleanup ownership and status
  semantics, including the `cleanup` fields and the heartbeat caveat.
- [x] 4.2 `docs/container.md`: exit codes and journal-based monitoring of the
  cleanup host.
- [x] 4.3 `docs/deployment.md`: the canary step lists the success fields and
  treats `cleanup_degraded` as not-success.

## 5. Validation

- [x] 5.1 New tests are red against the unchanged controller and CLI, and green
  after the change.
- [x] 5.2 Full Python suite, `node --test tests/workflow-policy.test.mjs` and
  `openspec validate --all --strict --no-interactive` pass.
- [x] 5.3 The pinned gitleaks scan of the commit range, tree and messages is
  clean.
