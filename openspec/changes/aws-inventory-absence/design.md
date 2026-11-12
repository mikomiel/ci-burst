# Design

## Context

See [proposal.md](proposal.md) for motivation. Reconciliation reads the
nonterminal operations that are missing from the filtered owned inventory by
their identifiers:

```
missing = nonterminal operations absent from the owned inventory
by_id = Aws.instances([operation.instance_id for operation in missing])
absent = missing - by_id
```

EC2's by-ID `DescribeInstances` is a batch contract: one unknown identifier
rejects the request. The adapter's `InvalidInstanceID.NotFound` shortcut then
returned `[]`, which is indistinguishable from "every identifier is absent".
Absence is a per-identifier fact and must be read as one.

## Goals / Non-Goals

**Goals:** a live identifier keeps its ownership evidence, slot and commitment
beside an aged-out one; a provider read failure refuses instead of settling; a
genuinely aged-out single identifier keeps the existing conservative settlement
and visibility policy; a missing SDK produces the documented refusal.

**Non-Goals:** new refusal codes, a schema or controller change, pagination, a
new bound, retries, a changed `_session()` guard, the transport retry policy, or
any other audit finding.

## Decisions

### The batch stays first, then one read per identifier

The batch call is the normal, cheapest path and is unchanged. It is repeated per
identifier only after EC2 rejects the batch with `InvalidInstanceID.NotFound`. A
per-identifier `InvalidInstanceID.NotFound` means absence for that identifier
only; every other per-identifier failure is translated exactly as the batch
failure is today. The adapter translates the provider error with the shared
translator, so a known code keeps its own reason (for example `AWS_REFUSED` for
an authorisation error) and an unclassified one becomes `AWS_UNCERTAIN`; the read
path then forces `uncertain=True`, because a failed read leaves the operation's
state unknown whatever the provider's code was.
The caller's list is bounded by `controller.max_instances`, so the fallback is
bounded by the caller and needs no new constant or pagination.

### No new refusal code

`AWS_UNCERTAIN` already means "the provider outcome is unknown, so the operation
stays owned". A failed read of one identifier leaves the same question open as a
failed batch read, so it reuses that code. Reusing it keeps the documented
refusal table, the CLI exit code and every controller path unchanged; the
rollback comes from the existing `ledger.transaction()` around reconciliation,
not from a new mechanism.

### The SDK guard moves to the type that needs it

`_session()` already refuses `AWS_SDK_UNAVAILABLE` when `boto3` is missing, but
`_client()` and `account_id()` imported `botocore.config.Config` before calling
it and raised `ModuleNotFoundError`. One helper, `_sdk_config()`, performs that
import inside the same refusal and is called by both entry paths. The
injected-client path is untouched, so the offline tests still need no SDK.

## Risks

- An endpoint that rejects every single-identifier read while accepting the batch
  would turn a normal reconcile into a refusal. That failure is reported, not
  silently absorbed; it settles and releases nothing.
- A reconcile over a large missing set performs one read per identifier. The
  bound is the configured ceiling, and the fallback runs only when the batch was
  rejected.
- The refusal table now names a code that appears only when the runtime
  dependency is absent; no successful path changes.
