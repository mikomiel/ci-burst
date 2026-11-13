# Design

## Context

`Controller.reap` is the independent cleanup path. It reads the owned inventory
once with the cleanup identity, selects instances whose `ci-burst:operation` and
`ci-burst:expires-at` tags are valid and expired, and requests termination. It
needs no Forgejo token, no ledger and no launch permission. See
[proposal.md](proposal.md).

## Goals / Non-Goals

**Goals:** make the command result match what the run actually established; give
an external scheduler a nonzero status for an incomplete cleanup; publish
bounded evidence in the existing JSON document; keep the ownership rule that
only a valid, expired tag authorises termination.

**Non-Goals:** a metrics server, a mutable textfile, a new timer, mount or unit
change, a retry loop, a dashboard, a Forgejo or ledger read, bill matching, or a
second provider call to confirm termination.

## Decisions

### The decision names the outcome, not the intention

The apply path has three factual outcomes: nothing was refused, something was
refused, or the provider refused the termination call. They map to
`termination_requested`, `cleanup_degraded` and `cleanup_failed`. A refused
resource is the reason a mixed run is degraded even though it did terminate the
proven targets; the run's job is the whole inventory, not just the easy part.
`termination_requested` therefore means "this pass had nothing it refused and
nothing it requested is confirmed removed", which is also true of a verified
empty or not-yet-expired inventory.

The dry run remains `planned`: it established candidates without claiming a
cleanup outcome.

### Evidence from one read, one block

Every count comes from the same `owned_instances` result that drove the
decision, so the document cannot describe a second, later inventory as if it
were the input to this one:

- `inventory_verified` is true when that read completed; a failed read still
  surfaces as a refusal document from the CLI and has no cleanup block.
- `attempted_at` and `completed_at` are Unix epoch seconds from the cleanup
  host's clock, started before the read and taken after the termination attempt.
- `observed` is the owned-instance count the read returned.
- `skipped` counts records already `shutting-down` or `terminated`, split into
  `terminating` and `terminated`.
- `expired` counts well-formed expired candidates, `refused` counts malformed
  ownership or expiry records.
- `requested` counts identifiers actually sent to `terminate` (zero on a dry
  run, zero when everything was refused). `denied` counts those identifiers when
  the provider refused the call.
- `unresolved` counts refused resources, expired candidates this read did not
  confirm gone, and resources observed still shutting down.

A requested identifier is never counted as removed: `terminated` only counts a
terminal state read back in this same inventory.

### What the block cannot prove

A `cleanup` block with `inventory_verified: true` and zero counts proves one
complete inventory read returned no owned records for this deployment. It is
not a zero AWS bill, and it is not proof that an earlier termination request
reached the provider or reached a terminal state. An absent or stale heartbeat
proves nothing at all: journald rotation, a down cleanup host and a timer that
was never installed all look identical from the outside. The operator must read
the absence as "unobserved", not as "empty".

## Risks / Trade-offs

- A permanently malformed owned instance makes every applied run exit `1` until
  an operator fixes or removes it. That is the intended signal: a green unit
  must not mean a refused resource was ignored.
- The block describes one run. It is not a historical record; continuity comes
  from the journal and from the repeated scheduled runs.
- An uncertain provider outcome is counted as `denied` and `cleanup_failed`
  rather than as a request, which over-reports failure instead of
  under-reporting an incomplete cleanup.

## Migration

The new keys are additive. No configuration, credential, image or deployment
change is required.
