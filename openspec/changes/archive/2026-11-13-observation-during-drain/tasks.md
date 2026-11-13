# Tasks

These are product-source tasks. They authorize no deployment, no credential and
no cloud change.

## 1. Export behaviour

- [x] 1.1 Remove the refusal above `controller.max_instances` from
  `Exporter.run` and append the `above_configured_limit` note instead.
- [x] 1.2 Keep candidate selection and the per-run budget unchanged: live
  workers before termination history, at most `MAX_CONSOLE_READS` reads, and
  `console_read_deferred` for the surplus.
- [x] 1.3 Refuse with the stable `console_read_cap_exceeded` code only when more
  than `MAX_CONSOLE_READS` unfinished non-terminal candidates remain, before any
  console read or file write.

## 2. Acceptance coverage

- [x] 2.1 Two live workers with `max_instances` 1 are reported with two console
  reads, the `above_configured_limit` note, and both the events and metrics
  files written.
- [x] 2.2 Seventeen live workers with `max_instances` 1 refuse with
  `console_read_cap_exceeded` and write no file.
- [x] 2.3 Twelve live workers with `max_instances` 12 behave as before.
- [x] 2.4 A run that defers terminal history still reports the deferral.

## 3. Documentation and specification

- [x] 3.1 `docs/operations.md` states that the read budget is independent of the
  admission ceiling, that a supported drain is reported rather than refused, and
  that the only refusal is the read budget.
- [x] 3.2 The `worker-observation` delta modifies `Observation refuses only when
  owned instances exceed the limit` with the drain and read-budget scenarios.
- [x] 3.3 The delta also modifies `Turnover defers surplus candidates rather
  than refusing`, scoping the deferral to candidates the run will not read while
  the unfinished non-terminal candidates fit the read budget, and the design
  records the PR #107 merge prerequisite and that scoping is chosen over
  deletion.

## 4. Validation

- [x] 4.1 `python3 -m unittest discover -s tests -p 'test_*.py'` passes.
- [x] 4.2 `node --test tests/workflow-policy.test.mjs` passes.
- [x] 4.3 `openspec validate --all --strict --no-interactive` passes.
- [x] 4.4 The gitleaks range, tree and message scans pass on the local commit.
