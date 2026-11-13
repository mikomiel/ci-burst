# Proposal

## Why

`Aws.instances` reads named instances with one EC2 `DescribeInstances` call. EC2
rejects that whole call when any one identifier is unknown, so a mixed batch in
which one identifier has aged out and another instance is still running returns
an empty list for every identifier. Reconciliation then treats every missing
operation as an aged-out record: after the retained-visibility window it settles
the operation as terminal and frees its slot, although the provider still
reports that instance live.

That is not the normal correctly tagged inventory path, but when it occurs a live
resource loses its slot and its commitment without proof of termination, and a
later admission may create replacement capacity. A second defect turns a missing
optional SDK into a generic `UNEXPECTED` error rather than the documented
`AWS_SDK_UNAVAILABLE` refusal, because `_client()` and `account_id()` import
`botocore.config` before the lazy import guard in `_session()`.

## What Changes

- Resolve a rejected by-ID batch per identifier: re-read each identifier on its
  own, treat `InvalidInstanceID.NotFound` as absence for that identifier only,
  and collect the records that exist. Any other per-identifier failure is an
  uncertain refusal, so the reconcile transaction rolls back and no operation
  settles or releases its slot.
- Add one guarded `_sdk_config()` helper that imports `botocore.config.Config`
  under the same `AWS_SDK_UNAVAILABLE` guard, and use it from `_client()` and
  `account_id()`.
- Add the `AWS_SDK_UNAVAILABLE` row to the refusal table in
  `docs/deployment.md`.
- No controller, schema, configuration or dependency change. The caller's list
  is already bounded by `controller.max_instances` (at most twelve), so the
  per-identifier re-read needs no new constant, no pagination and no new call
  site.

## Impact

- A correctly tagged deployment still reads one batch per reconcile.
- A deployment whose batch is rejected pays at most one extra provider read per
  identifier on that reconcile, and only while an identifier is missing from the
  filtered owned inventory.
- A live instance without ownership tags now produces an
  `OWNERSHIP_TAGS_MISSING` discrepancy and keeps its slot instead of being
  settled as terminal.
- A source checkout without the runtime SDK reports a stable refusal and exit
  code 2 instead of an unexpected import error.
