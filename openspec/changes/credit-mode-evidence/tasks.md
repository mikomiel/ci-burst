# Tasks

These are product-source tasks. They authorize no deployment, no credential, no
cloud change and no billable call.

## 1. Family evidence

- [x] 1.1 Add `ci_burst/credit.py` with the closed burstable-family table, the
  documented default mode for each, the known non-burstable families named by
  the project, and `unknown` for anything else.
- [x] 1.2 Return one bounded evidence object: burstable state, mode, mode
  source and surplus-credit state; never report surplus credits as bounded.

## 2. Plan evidence

- [x] 2.1 Parse `LaunchTemplateData.CreditSpecification.CpuCredits` into the
  template facts and let an explicit template mode outrank the family default.
- [x] 2.2 Add the `credit` object to every candidate record reported by
  `Controller._decide`, including the selected candidate.
- [x] 2.3 Prove pricing, the reservation and the frozen launch request are
  unchanged and carry no credit specification.

## 3. Prerequisite evidence

- [x] 3.1 Report `credit_specification` and `burstable` in the template facts.
- [x] 3.2 Report `burstable` for each configured pool type.
- [x] 3.3 Keep an unreadable template `unavailable`; an absent specification is
  reported as absent and follows the family default.

## 4. Documentation and acceptance

- [x] 4.1 `docs/operations.md`: surplus CPU credits are a separate residual the
  estimate does not bound, with the unlimited default for burstable Spot.
- [x] 4.2 `docs/runner-setup.md`: standard mode throttles and unlimited mode can
  bill surplus credits; choosing the family and the mode stays operator work.
- [x] 4.3 `COST_SCOPE_NOTE` names surplus CPU credits among the unbounded
  charges.
- [x] 4.4 Cover the burstable default, the template override, non-burstable and
  unknown families, and the unchanged launch request in
  `tests/test_controller.py`; cover the reported, absent and denied prerequisite
  evidence in `tests/test_prerequisites.py`.

## 5. Operator follow-up (not authorized here)

- [ ] 5.1 Measure representative short CI jobs in standard and unlimited mode
  before changing an operator-owned template; any billable comparison needs a
  separate authorization, cost bound and cleanup plan.
