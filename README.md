# ci-burst

A small Forgejo queue-driven AWS Spot runner controller. This is an original
public project licensed under GPL-3.0-only.

## What it does

- Observes runnable jobs for one Forgejo repository and explicit runner labels.
- Adds up to a configured number of Spot instances (default one, maximum four)
  from an operator-prepared, pinned launch template while the queue has work and
  the budget allows it.
- Keeps Forgejo as the only scheduler and job-result authority.
- Persists launch intent, ownership and a conservative local reservation
  before every cloud request.
- Defaults to observation; requires an explicit apply to launch, and offers an
  independent expiry-cleanup path that never launches.
- Turns the console of the workers it owns into deduplicated events and a
  metrics textfile, so a launch that never registers is visible as an event
  rather than as a queue that does not move. The reader is bounded, holds no
  queue credential and copies no arbitrary console text.

The product source is implemented, covered by standard-library tests and
released. A release is source history, not a deployment: this repository
publishes no image, and the deployment procedure and the live Forgejo contract
qualification are the operator's records. `qualification = "fixtures-only"` is
the shipped default and blocks `apply` until an operator records that evidence.
AWS resources, IAM, networking, registration credentials and the runner image
remain operator-owned prerequisites. No cost-saving or absolute AWS-billing-cap
claim has been established.

## Try it

Requires Python 3.12 or newer. Live use needs the `boto3` runtime dependency
(`pip install -e .`); tests need neither the SDK nor any credential.

```sh
python -m ci_burst plan   --config deployment.toml
python -m ci_burst apply  --config deployment.toml
python -m ci_burst status --config deployment.toml
python -m ci_burst export --config deployment.toml --metrics-file ci-burst.prom
python -m ci_burst reap   --config deployment.toml [--apply]
```

A minimal configuration is in [docs/operations.md](docs/operations.md), which
also owns the queue contract, the worker image/template contract, labels,
reservation scope, identities, rollback and status semantics. To build the
operator-owned worker prerequisites, including the IAM role, SSM parameter and
bootstrap, follow [docs/runner-setup.md](docs/runner-setup.md). Once those exist, the
[deployment walkthrough](docs/deployment.md) sequences the control-plane
identities, hosts, configuration, scheduling and live qualification. The
[pilot checklist](docs/pilot-checklist.md) is a preparation record, not
authorization. Running the controller from a container beside Forgejo, driven
by systemd timers, is described in [docs/container.md](docs/container.md); the
image is built locally and none is published. A worker subnet with no internet
path of its own needs the optional in-VPC router in [docs/router.md](docs/router.md).

## Project stage

The delivered product change is archived as the [AWS Spot queue-burst
change](openspec/changes/archive/2026-11-11-aws-spot-queue-burst/proposal.md),
with its [design](openspec/changes/archive/2026-11-11-aws-spot-queue-burst/design.md),
[acceptance specifications](openspec/changes/archive/2026-11-11-aws-spot-queue-burst/specs/)
and [task record](openspec/changes/archive/2026-11-11-aws-spot-queue-burst/tasks.md).
Its requirements are promoted to the [capability
specifications](openspec/specs/), and the next behavioural change needs its own
proposal.

A release exists. [CHANGELOG.md](CHANGELOG.md) records merged source history and
[CONTRIBUTING.md](CONTRIBUTING.md#releases) owns the release record and its
boundary: an annotated semver tag on `main` plus a GitHub Release whose notes are
that changelog section.

## Checks

Use Node.js 22.12+, Python 3.12+ and npm:

```sh
npm ci --ignore-scripts
npm run check:policy
npm run check:spec
```

`check:policy` runs the Python source tests and the workflow-policy tests.
Validation CI is read-only and uses no cloud credentials or billable
operations; it does not prove a live Forgejo/AWS integration. Separate
trusted-default-branch workflows can publish the Gandalf-backed `review`
status and merge eligible ordinary PRs after maintainer-controlled activation.
They never execute PR-head code with a write token. See
[review, merge and activation](CONTRIBUTING.md#review-and-merge).

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md) and
[SECURITY.md](SECURITY.md). GitHub is the sole source/PR surface. Changes use
protected-main pull requests; controlled automated merging is not active merely
because this policy exists.

License: [GPL-3.0-only](LICENSE). Copyright (C) 2026 ci-burst contributors.
No affiliation with Forgejo or Amazon Web Services is implied.
