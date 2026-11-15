# Proposal

## Why

The controller spends worker time on workers that run no job, in two related
ways: nothing bounds how long a worker waits for a job, and nothing stops the
controller from launching a second worker for a job that a first worker is still
booting to claim. An operator pilot observed workers idling 25-30 minutes before
a job and ephemeral workers living a median of under ten minutes.

- An idle worker is billed for its whole lifetime (GitHub issue #136). The
  bootstrap waits for a task with `one-job --wait`, which blocks until Forgejo
  assigns one. A worker that is launched for a job and does not receive it stays
  up until the bootstrap's own one-hour shutdown. The worker contract is one job
  per worker, so the long wait buys no reuse.
- Admission launches another worker for a booting worker's job (GitHub issue
  #137). The controller counts a Forgejo job in the `waiting` state as demand. A
  worker launched for that job stays in demand until it claims the job, which
  takes the time to boot, register and poll. Each apply tick, about every 60
  seconds, in that time can launch another worker for the same job, up to the free
  slots. Forgejo's runner list reports only whether a runner is active or
  offline, so the controller cannot tell which worker is for which job.

## What Changes

- The bootstrap replaces the blocking wait with a bounded claim window. After the
  runner registers, it polls `one-job` without `--wait` every 10 seconds until it
  receives a task or the claim deadline, 300 seconds after registration, passes.
  It starts no poll at or after the deadline. At the deadline it prints one fixed
  `STEP claim-timeout` marker, exits with status 0 and shuts down. A job it
  receives keeps the existing job timeout and instance lifetime. The runner's
  output still reaches the console while a poll runs.
- The bootstrap recognises an empty poll by an explicit, bounded signature: the
  documented exit status, from 2 to 123, together with the documented message as a
  complete line. Every other outcome ends the loop as a finished attempt and is
  never a reason to poll again.
- `export` recognises `STEP claim-timeout` as one more name in its closed step
  vocabulary and publishes it as a `step` event with no console text.
- The controller adds a claim grace. `controller.claim_grace_seconds` (range
  60..1800 proposed, see design open question 1; default 300, equal to the
  worker's claim window) makes each owned operation in the `starting` or
  `running` state, created less than the grace ago, cover one waiting job, so
  admission requests
  `min(max(0, demand - recent launches), free slots)`. The decision reports
  `recent_launches`, and a fully covered demand waits with the reason
  `awaiting_claim`. After the grace, a job that is still waiting can get a new
  worker.
- The tests, the setup guide, the operations guide, the deployment guide and the
  pilot checklist describe the claim window, the marker and the grace.

## Impact

- No pricing, allowance arithmetic, Spot interruption handling, job retry,
  scheduler role, Forgejo contract, AWS API call, On-Demand fallback or placement
  behaviour changes. The queue stays the only launch gate and Forgejo stays the
  only job-result authority. A worker still takes at most one job.
- No new credential, IAM permission, network path or image layer. The marker
  travels on the console that `export` already reads.
- A worker with no job is expected to end soon after its claim window instead of
  at its lifetime. That lowers expected spend. It is not a cap on the AWS bill: a
  reservation still prices the full configured lifetime, settlement still uses the
  provider's time, and a hung poll or a failed shutdown is still bounded only by
  the lifetime and by independent expiry cleanup.
- A job that becomes runnable while a recent launch has already claimed its own job
  can wait up to the grace for its own worker. This is the cost of a time-based
  grace. The operator can lower the grace to shorten that wait.
- The two halves deploy separately. The worker half is launch-template user data,
  so activation is a deployment step: an operator publishes a template version
  before any worker polls. The controller half takes effect when the controller
  release is deployed.
- The worker half has an unverified assumption: that empty polls keep the
  ephemeral registration valid on the pinned runner. The operator must qualify it
  live before the template version is enabled. This change authorizes no
  publication, no live run and no cloud change.
- Non-goals: changing prices or allowance arithmetic, handling Spot interruptions,
  retrying a job, adding a scheduler role, changing the Forgejo contract, adding an
  AWS API call, falling back to On-Demand capacity, and letting a worker take more
  than one job.
