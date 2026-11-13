# Proposal

## Why

`reap --apply` reported `termination_requested` and exited `0` even when every
owned instance had been refused. An instance with a missing expiry tag was
correctly not terminated, yet the result claimed the pass succeeded and
`termination_requested` stayed empty. The scheduled unit observes the exit
status, so one unresolved resource could keep the unit green indefinitely while
nothing on the cleanup host reported it.

The same result exposed no bounded cleanup evidence. The worker exporter has a
last-run timestamp, but it does not describe this independent command: an absent
or stale reap heartbeat was indistinguishable from a verified empty inventory,
and refused, requested and terminating resources were not counted separately.

## What Changes

- An applied pass that refused at least one owned resource reports
  `cleanup_degraded`, in both the all-refused and the mixed case, and the CLI
  exits `1`. A mixed inventory still terminates only the proven expired
  targets. A clean pass, including a verified empty inventory, stays
  `termination_requested`; a dry run stays `planned`; a provider refusal stays
  `cleanup_failed`.
- The `confirmation` sentence is emitted only when identifiers were actually
  sent to the provider.
- The reap result gains a top-level `cleanup` object built from the same single
  owned-instance read: inventory verification, attempt and completion clocks,
  and observed, skipped, terminated, terminating, expired, requested, refused,
  denied and unresolved counts. A requested termination stays distinct from a
  confirmed removal.
- The documentation states that the block is evidence of one inventory read,
  not a bill, and that an absent or stale heartbeat proves nothing.
- No new provider call, mount, timer, unit, metric endpoint or dependency.

## Impact

- An installation whose cleanup keeps meeting a malformed resource now reports a
  failed scheduled run, and the operator can read the reason from the same JSON
  document. Valid targets are still cleaned in the same run.
- The document only gains fields; existing field names are unchanged, and no
  configuration change is required.
- No credential, IAM, network, image or template change.
