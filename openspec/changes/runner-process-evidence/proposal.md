# Proposal

## Why

The worker bootstrap discarded the one-job runner process's status with
`|| true`, so its `bootstrap exiting rc=<n>` marker always reported 0 even when
the runner failed or the lifetime timeout killed it. The instance console is the
only evidence channel for a worker that never reached the network, so a
misleading exit marker hides the very failure it exists to show: an operator
cannot tell a completed job process from a crashed or timed-out one, and a
later reader of the event stream is told a worker exited cleanly when it did
not.

## What Changes

- The bootstrap captures the one-job process status instead of discarding it,
  emits exactly one bounded marker — `bootstrap one-job completed rc=0`,
  `bootstrap one-job failed rc=<n>` or `bootstrap one-job timeout rc=<n>` — and
  exits with that status. The cleanup and shutdown trap stays unconditional on
  every path.
- The exporter recognises that marker as a closed vocabulary member and
  publishes one `job-process` event carrying the outcome and status, with a
  stable identity and no console text, job verdict or success metric.
  Historical `exit` event lines still validate.
- The bootstrap contract test expresses an explicit publication state: a
  pending digest while the next launch-template version is owed, and the
  published digest when it is not.
- Worker documentation states that these markers describe the worker process
  lifecycle and never the Forgejo job result, and the deployment walkthrough
  records the operator publication step for the next launch-template version.

## Impact

- The next launch-template version changes the bootstrap user data; until it is
  published the pinned version keeps the old behaviour. The contract test records
  the prepared bytes as pending and the prerequisite check reports the drift, so
  the gap is visible rather than silent.
- One additional event per worker that reaches the one-job phase, and one
  additional closed marker in the console vocabulary. The metric set and the
  job result are unchanged: the event says nothing about whether the Forgejo
  job passed or failed.
- No change to registration, the SSM read, the iptables rules, the umasks, the
  lifetime watchdog, admission or cleanup.
