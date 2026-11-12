# Proposal

## Why

Lowering `controller.max_instances` below the live worker count is explicitly
supported: admission pauses and no worker is evicted. But the worker-console
reader refused (`console_read_cap_exceeded`) whenever non-terminal owned
instances exceeded that limit, so observation blanked exactly during a
legitimate drain. With two live workers and a limit of one the run made zero
console reads and wrote no event and no metrics file, and the last-run timestamp
went stale just as an operator needed to watch the drain finish.

## What Changes

- Remove the admission-ceiling refusal from `Exporter.run`. More non-terminal
  owned instances than `controller.max_instances` are reported with the
  `above_configured_limit` note, the vocabulary the controller already uses for
  the same condition.
- Keep candidate selection and the per-run budget unchanged: records that can
  still carry new evidence, live before termination history, at most 16 console
  reads per run, and the surplus deferred with `console_read_deferred`. A
  deferred run remains partial observation, never complete evidence.
- Refuse only when the read budget itself cannot cover the live evidence: more
  than 16 unfinished non-terminal candidates. The stable reason code stays
  `console_read_cap_exceeded`, and its detail names the read-budget condition.
- Update `docs/operations.md` and the acceptance coverage.

## Impact

- A deployment inside its limit behaves exactly as before.
- A deployment draining above a lowered limit keeps writing events and metrics,
  bounded by the same 16 reads and 64 KiB per console, and still reports the
  over-limit condition.
- No new provider call, file, metric family, credential, IAM or network change.
  A fleet with more than 16 unfinished live workers is still refused, unchanged
  in that it writes nothing and exits 2.
