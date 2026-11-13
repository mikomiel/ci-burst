# Proposal

## Why

Audit item E06: AWS client calls use botocore's default connection and read
timeouts, `owned_instances` can span 20 pages, and the standalone CLI has no
whole-pass deadline. A slow read can consume the time the pass needs for
admission or cleanup, and a systemd `TimeoutStartSec` kill produces less useful
evidence than an explicit incomplete pass.

## What Changes

- Build the EC2 and STS clients with a bounded connect timeout (10 s) and read
  timeout (30 s) alongside the existing no-retry policy. Retry counts do not
  change.
- Add an optional deadline to `owned_instances`, `instances`, `spot_quote` and
  `console_output`, checked before each paginated page and each per-identifier
  read. A call without a deadline behaves exactly as before.
- Bound one `apply` pass with `controller.pass_deadline_seconds` (30..290,
  default 240). The accepted range stays below the shipped unit's
  `TimeoutStartSec=300`, so the whole range leaves the process headroom to
  report; a deployment that needs a longer pass raises the unit timeout rather
  than a larger setting. A reached deadline returns `decision="refused"`,
  `reasons=["pass_deadline_exceeded"]` and a `phases_deferred` list naming the
  phases that did not complete.
- Check the deadline between phases and before the first mutation. A phase that
  has started, and every frozen intent it committed, runs to its existing
  conclusion so the uncertain reconciliation path is preserved.
- Leave `reap`, `status` and `export` outside the pass deadline; the independent
  reaper keeps its own unit and timer.

## Impact

- A slow provider read now produces a bounded, explicit incomplete pass instead
  of consuming the admission or cleanup budget.
- No expired intent or stale quote is sent after the deadline.
- A frozen intent remains resumable with the same client token when a launch may
  have been accepted before the process stopped.
- No dependency, no scheduler and no provider/IAM/network change.
