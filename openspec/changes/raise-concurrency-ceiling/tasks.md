# Tasks

These are product-source tasks. They authorize no deployment, no credential and
no cloud change.

## 1. Bound

- [x] 1.1 Raise the `controller.max_instances` validation to `1..12` in
  `ci_burst/config.py`, keeping the default at `1`.
- [x] 1.2 Update the two acceptance requirements that restate the range:
  `bounded-operation` §Bounded fleet admission and `spot-runner-lifecycle`
  §Bounded owned-instance fleet.
- [x] 1.3 Update the operator documentation that names the range:
  `docs/container.md`, `docs/operations.md`, `docs/deployment.md` and `README.md`.

## 2. Acceptance coverage

- [x] 2.1 `tests/test_config.py` accepts the ceiling `12` and refuses `13` with
  the range named in the refusal detail.
- [x] 2.2 The existing controller batch tests pass unchanged: a batch of four is
  still valid, still capped by free slots and still capped by the allowance.
- [x] 2.3 Twelve-worker admission is exercised, not just configuration loading:
  a backlog above twelve produces twelve launches, twelve distinct operations
  and client tokens, one reservation each, and the documented `hourly_ceiling`.

## 3. Console reader at the new ceiling

- [x] 3.1 Archive the completed `worker-console-export` change so the
  `worker-observation` capability has merged requirements before this change
  modifies one.
- [x] 3.2 Read a console only while it can carry new evidence: a live worker on
  every run, a terminating one until the read that shows it terminal, and a
  finished record never again.
- [x] 3.3 Defer candidates beyond the per-run read budget with a note instead of
  refusing, and keep the refusal for more non-terminal workers than the
  configured ceiling.
- [x] 3.4 Regression coverage: turnover beyond the budget is deferred not
  refused, a deferred record is read on the next run, a finished record is not
  read again, and more live workers than the ceiling still refuses.
- [x] 3.5 Update `docs/operations.md` for the new bound and the deferral note.

## 4. Validation

- [x] 4.1 `npm run check:policy` and `npm run check:spec` pass on the change.
- [x] 4.2 Publication is gated on the workflow's own secret scan of the exact
  head range. The change touches no credential, address or deployment value, and
  the authoring environment carries no gitleaks binary, so that scan is the CI
  step rather than a local one.
