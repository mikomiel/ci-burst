# Proposal

## Why

A launch is not a registration. Today the controller can report a healthy
decision and a worker can still never reach the queue: the instance boots, the
first-boot script fails, and nothing the controller holds records why. The only
evidence lives in the instance console, which is a provider read the product
never performs, so an operator learns about the failure from a CI queue that
does not move.

That gap is an observation gap, not a scheduling gap. The controller already
knows which instances it owns; what it lacks is a bounded, secret-safe reading
of what those instances reported about themselves.

## What Changes

- Add an `export` command. It reads the console output of the instances this
  deployment owns, extracts a small fixed vocabulary of progress and failure
  markers, and writes two things: an append-only event stream of new
  observations, and one Prometheus textfile.
- Bound the read: a per-run cap on instances, a byte cap per console, a closed
  set of step names, a small allowlist of failure kinds, and no console text at
  all — a failure line contributes its kind, never a copy of the line.
- Deduplicate across runs with one small state file, so an event is emitted
  once per instance rather than once per minute.
- Emit counters that survive instance termination, so "launched" and
  "registered" are comparable over a window instead of only for live instances.
- Ship the unit and timer that run it, and the one additional provider
  permission it needs.

Non-goals: a dashboard, a metrics backend, a log pipeline, alerting rules, a
second scheduler, retries, job output capture, and any dependency on the
Forgejo API. The command writes files whose names and schema are documented
here; what consumes them is a deployment decision, not product code.

## Capabilities

### New Capabilities

- `worker-observation`: bounded, secret-safe evidence about what owned workers
  reported on their own consoles, expressed as deduplicated events and a
  metrics textfile.

### Modified Capabilities

None. `queue-observation` remains the only reader of the queue, and
`spot-runner-lifecycle` remains the only authority on launch and termination.

## Impact

- `ci_burst/export.py` (new), `ci_burst/aws.py` (one console read),
  `ci_burst/cli.py` (one subcommand).
- `deploy/ci-burst-export.service`, `deploy/ci-burst-export.timer`,
  `deploy/iam/scaling-policy.json`.
- `tests/test_export.py` (new) plus the AWS, CLI and deploy-unit suites.
- `docs/operations.md`, `docs/container.md`, `docs/deployment.md`,
  `docs/router.md`, `README.md`.

The command performs no mutation, holds no Forgejo credential, and introduces no
new runtime dependency: `boto3` is already the one provider dependency.
