# Design

## Context

See [proposal.md](proposal.md). `owned_instances` and `instances` both delegate
response parsing to `ci_burst.aws._instances`, which currently substitutes an
empty list for a missing or malformed `Reservations` value and an empty string
for a missing `InstanceId`. The controller therefore cannot distinguish a
verified empty fleet from an unreadable one, and it treats `""` as an
unresolved intent that may be resumed.

## Goals / Non-Goals

**Goals:** refuse an unparseable inventory as uncertain; make the refusal visible
with the existing stable reason; cover both inventory entry points at the shared
parser; keep a genuinely empty response valid; prove the two end-to-end defects
are closed.

**Non-Goals:** changing pagination, discovery filters, the controller's retention
window, the by-identifier absence logic, the ledger schema, the CLI, or the
controller's decision paths.

## Decisions

### Validate in the shared parser

Both entry points already call `_instances`, so the validation belongs there.
Duplicating it in the callers would leave a future third caller unprotected and
allow the two paths to disagree about what a complete inventory is.

### A missing key is uncertain, not empty

The AWS API documents `Reservations` as part of a successful response, but a
missing key proves nothing about the fleet. An absent or non-list value, a
non-mapping reservation, an absent or non-list `Instances`, a non-mapping entry,
and a missing, empty or non-string `InstanceId` all raise
`ProviderRefusal("AWS_INVENTORY_INCOMPLETE", ..., uncertain=True)`. The reason
already exists for pagination exhaustion, so the refusal list stays stable and
an operator sees one code for an incomplete read.

### An empty list remains complete

`Reservations: []` is the provider's own way to say the filters matched nothing.
It stays a verified empty inventory, so the existing retention and settlement
rules still settle an aged-out operation and may admit a replacement. Only a
response that cannot be parsed is refused.

## Risks / Trade-offs

- If a provider ever omitted `Reservations` for a genuinely empty result, the
  controller would refuse instead of launching. Admission waits, the refusal is
  reported, and the next successful read proceeds: the failure is loud and
  fail-closed rather than silently over-provisioning.
- A malformed response blocks all owned-inventory decisions until the provider
  answers correctly. That is intentional; the alternative is acting on an
  inventory that was never established.
- The refusal detail names the malformed shape only; it carries no instance,
  request or tag payload.

## Alternatives considered

- **Treat only a missing `Reservations` as empty and validate everything else.**
  Rejected: it preserves the exact defect that releases a running operation.
- **Validate in each caller.** Rejected: duplicated rules, and a later caller
  can silently restore the old behavior.
- **Use a new refusal code.** Rejected: `AWS_INVENTORY_INCOMPLETE` already means
  an inventory that cannot be established, and a second code would fragment the
  documented refusal list.
- **Skip an entry without an id and keep the rest.** Rejected: a partial
  inventory is the same unsafe premise as an empty one, and the missing identity
  is exactly what cannot be reconciled.
