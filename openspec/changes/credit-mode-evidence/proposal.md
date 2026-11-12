# Proposal

## Why

Worker examples use T4g, and admission prices Linux/UNIX Spot compute plus a
configured incidental rate. T3, T3a, T4g and T8i Spot instances launch in
`unlimited` CPU-credit mode by default, and AWS warns that a short, immediately
busy instance can accrue surplus credits that are billed separately. The plan
and the prerequisite check report neither the family's burstability nor the
template's `CreditSpecification`, so an operator cannot see whether the estimate
leaves a charge unbounded.

## What Changes

- Classify the supported instance families explicitly: burstable families with
  their documented AWS default credit mode, known non-burstable families, and
  `unknown` for everything else, never a guess.
- Add an additive `credit` object to every plan candidate: burstable state,
  mode, mode source and an explicitly unquantified surplus-credit exposure. The
  template's value keeps three states: absent (family default), recognized
  (template mode) and present but unrecognized (unknown, never the default).
- Report the launch template's raw `CreditSpecification` and burstability in
  `scripts/check_prerequisites.py`; an unreadable template stays unavailable and
  an unrecognized value is drift.
- Document surplus CPU credits as a residual charge in the operations and
  runner-setup guides, and in the controller's cost-scope note.

## Impact

- Every plan candidate gains one bounded object; pricing, admission, the
  reservation, the ledger and the frozen launch request are unchanged.
- The launch request carries no credit specification: the operator's launch
  template and account default own the mode.
- No instance type, template, IAM, network, credential, dependency or billable
  call is introduced. Choosing a family or a credit mode remains an operator
  decision.
