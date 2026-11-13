# Proposal

## Why

On 2026-11-13 AWS reclaimed the Spot worker running a main-push job. The worker
had no way to say it was interrupted: the job account is denied the metadata
service, nothing on the worker read the two-minute interruption notice, and no
worker-event named the interruption. Forgejo failed the job about fourteen
minutes later with no evidence, and `main` read red although the code was fine
(HB-3329).

The worker's own console is the one channel the product already reads for a
worker that never reached the network, and it is the only channel that needs no
new credential, no new collector and no metadata reachability for the job
account. What it cannot express today is an interruption.

## What Changes

- The bootstrap watches the Spot interruption notice as root, polling IMDSv2
  every five seconds, and on a notice prints one fixed marker carrying the
  notice's action and the worker hostname, then ends the job account's runner
  process so the job stops before the instance is reclaimed.
- `export` recognises that marker as one more closed-vocabulary event,
  `spot-interruption`, with three validated values and no free console text.
- The event stream, the worker-setup guide and the policy tests describe the
  marker, its fields and its limits.

## Impact

- No admission, placement, lifetime, cost, retry or job-result behaviour
  changes. The queue remains the only launch gate and Forgejo remains the only
  job-result authority.
- No new credential, IAM permission, collector, image layer or network path: the
  marker travels on the console the exporter already reads.
- A worker that cannot read the notice behaves exactly as it did before; the
  instance lifetime and the job timeout remain its bounds.
- The marker is evidence that the notice was observed and the job was
  signalled, never a job verdict and not proof that the worker ended; the console
  is not a trusted channel, so a marker alone does not prove an infrastructure
  loss.
- Activation is a deployment step, not a source step: the bootstrap bytes are the
  launch-template user data, so an operator publishes a template version before
  any worker watches the notice.
- Non-goals: changing Forgejo's failure timing, retrying a job, deriving a job
  result from the marker, a worker-side log shipper, and a new credential on the
  worker.
