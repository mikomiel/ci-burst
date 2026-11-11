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
  `docs/container.md`, `docs/operations.md` and `docs/deployment.md`.

## 2. Acceptance coverage

- [x] 2.1 `tests/test_config.py` accepts the ceiling `12` and refuses `13` with
  the range named in the refusal detail.
- [x] 2.2 The existing controller batch tests pass unchanged: a batch of four is
  still valid, still capped by free slots and still capped by the allowance.

## 3. Validation

- [x] 3.1 `npm run check:policy` and `npm run check:spec` pass on the change.
- [x] 3.2 Publication is gated on the workflow's own secret scan of the exact
  head range. The change touches no credential, address or deployment value, and
  the authoring environment carries no gitleaks binary, so that scan is the CI
  step rather than a local one.
