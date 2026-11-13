# Proposal

## Why

The E04 audit recommendation asked for a recorded association from a launched
spot worker to the Forgejo job attempt it ran. The live contract does not carry
one: an `ActionRunJob` has no runner field, a runner registration has no job
field, and neither the job detail nor the setup-step log names the runner. A
capture of the live queue showed eight running jobs against seven active
ephemeral registrations in the same snapshot, so the two endpoints are not even
atomic. The full association would be a claim that is often ambiguous and would
add the most complex state in the product for an observability-only result;
that work is declined.

What is missing today is cheaper and honest: neither the controller result nor
the metrics show how many ephemeral registrations are active against how many
running attempts, so an idle worker — paid for, registered, with no work — is
invisible until the console marker or the bill.

## What Changes

- Read `GET /api/v1/repos/{owner}/{repo}/actions/runners` once per `apply` pass,
  best-effort, with the existing owner-scoped token, and validate its shape as
  its own qualified contract.
- Report `running_attempts`, `active_registrations`, `offline_registrations`
  and an idle estimate in the `apply` result, with the non-atomic caveat stated
  where the numbers appear.
- Persist one snapshot per successful pass in the ledger (schema 4,
  `fleet_observations`, seven-day retention) and print the newest in `status`.
- Emit active-registration, running-attempt and idle gauges plus an observation
  timestamp from `export`; a stale snapshot publishes only its timestamp.
- Document the snapshot, its bounds, the schema upgrade and the rollback.

## Impact

- No admission, placement, lifetime, cost, retry or job-result behaviour
  changes; the queue remains the only launch gate.
- No new credential: the runner-jobs endpoint is already owner-scoped and the
  registration endpoint carries the same requirement.
- One extra HTTP GET per `apply` pass, bounded by the existing timeout and
  response limit. A failure is evidence-only and is reported, never used to
  refuse admission.
- The ledger upgrades to schema 4 on the first writable run after a verified
  copy; the previous release refuses schema 4, so a rollback restores the copy.
- `export` keeps working without a Forgejo credential and without a new
  deployment mount.
- Non-goals: associating a worker with a job attempt; deriving a job verdict
  per worker; copying console text; a dashboard, new command, host agent or
  automatic rerun.
