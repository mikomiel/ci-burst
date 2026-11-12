# Design

## Context

See [proposal.md](proposal.md) for motivation. Admission already prices every
candidate before any time-sensitive check and reports the selected pool. The
controller's cost estimate prices the accepted Spot quote, the configured
incidental rate and the forward commitment; it has no input that describes CPU
credits. The launch template is an operator-owned artefact, and the controller
never sends a credit specification.

## Goals / Non-Goals

**Goals:** make a burstable candidate's credit mode visible in plan and
prerequisite evidence; report unknown instead of guessing; name surplus credits
as a residual charge; leave every pricing and launch decision untouched.

**Non-Goals:** changing an instance type, setting `CreditSpecification` in the
launch request or a template, querying the account default credit
specification, pricing surplus credits, or adding a billable benchmark.

## Decisions

### A closed family table, not a name heuristic

The table classifies the families this project's examples and guide name —
`c7g`, `m7g`, `m8g`, `r7g` as non-burstable — and the burstable T families AWS
documents: `t2` (standard), `t3`, `t3a`, `t4g` and `t8i` (unlimited). Every
other family reports `unknown`. Inferring "not burstable" from a type name
would be a guess, and a wrong `false` would hide exactly the exposure this
change exists to reveal.

### Evidence, not a control

The `credit` object is one additive field on an existing candidate record:

```
{"burstable": true|false|null,
 "mode": "standard"|"unlimited"|null,
 "mode_source": "template"|"template-unrecognized"|"aws-default"|"not-applicable"|"unknown",
 "surplus_credits": "unquantified"|"not-applicable"|"unknown"}
```

Pricing, admission, the reservation, the ledger schema and the frozen
`RunInstances` request do not read it. The launch request carries no
`CreditSpecification`, so the template and the account default remain the only
owners of the mode.

### The template's specification outranks the default

The launch template's value has three states, and they stay distinct. Absent
(`None`) means the template sets no specification, so a burstable family reports
its documented default with `mode_source: "aws-default"`. A recognized
`standard` or `unlimited` value reports itself with `mode_source: "template"`.
Any other present value reports `mode: null` with `mode_source:
"template-unrecognized"` and `surplus_credits: "unknown"`: the provider would
refuse that launch, and the family default is not what the template says. The
account-level default can override the family default, which this product cannot
observe without another API and permission; the plan does not query it, and the
mode source says which fact is being reported.

### The prerequisite check reports the same facts

`_template_facts` reports the raw `credit_specification` (or `None` when the
template sets none) and `burstable` for the template type; configured pool types
get `burstable` in the existing `instance_type` record. An unreadable template
is already `unavailable`, so credit evidence cannot turn into a pass. An absent
specification is evidence of the family default, not a missing read. A present
value the product does not recognize is reported raw and as
`credit_specification_invalid` drift, because a launch carrying it would fail
provider validation; it is never read as the default.

## Failure behavior

- Unknown family: `credit` reports `unknown`; the plan still launches on that
  candidate, because this change adds no refusal.
- Non-burstable family: `credit` reports not applicable, unless the template
  pins a recognized mode, which it reports with the template as its source. A
  launch template that still pins a credit specification supplies it to every
  launch, so an operator mixing families should confirm the provider accepts
  that combination; this change does not test it.
- Unrecognized template value: the plan reports no mode and does not fall back
  to the family default; the prerequisite check reports the raw value and
  `credit_specification_invalid` drift.
- Unreadable template: the prerequisite check stays `unavailable` (exit 3), not
  drift and not a pass.
- The estimate still cannot bound surplus credits; docs and the cost-scope note
  say so.

## Alternatives considered

- **Read `DescribeInstanceTypes.BurstablePerformanceSupported` at admission.**
  Rejected: admission performs no metadata call, and a provider read per tick
  would widen the slow path this design keeps bounded. The operator check
  already reads instance-type metadata.
- **Query the account default credit specification.** Rejected: another API and
  permission for a fact the operator owns; the plan names the documented
  default and its source instead.
- **Set `CreditSpecification` in `RunInstances`.** Refused: the launch template
  is operator-owned and the audit explicitly does not authorize changing it.

## Evidence gaps

- The account-level default credit specification is not observed or queried;
  the plan reports the family default, not the account's effective default.
- Surplus-credit cost needs provider billing or runtime burst evidence, so the
  plan reports it as unquantified.
- The family table is curated; a new family stays `unknown` until reviewed.
- Provider acceptance of a template-pinned `CreditSpecification` with a
  non-burstable pool type is not exercised here.
- A representative short-job measurement before changing an operator template,
  and any billable comparison, remain operator work.
