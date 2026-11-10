# Proposal

## Why

A Forgejo operator can have runnable jobs waiting for a matching runner while
not wanting to keep another machine running continuously. ci-burst will add
bounded AWS Spot capacity from that demand without creating a second job
queue or a general-purpose multicloud platform.

## What Changes

- Create an original MIT-licensed, self-contained controller with a dry-run
  default and an explicit live-provisioning operation.
- Observe one Forgejo repository-scoped job queue. Qualify the supported API
  response and runnable-job semantics before accepting a live configuration.
- Match configured runner labels, account for pending owned instances and
  provision at most one additional Spot VM in the first version.
- Launch only an operator-prepared, version-pinned template whose image
  registers a one-job ephemeral runner and shuts down after that job.
- Price an ordered list of configured availability-zone candidates, launch in
  the first eligible one, and fall through to the next candidate only when the
  provider definitely rejects the request for insufficient capacity. The
  original single-zone setting remains supported as a one-candidate list.
- Persist launch intent, idempotency data, resource ownership and conservative
  cost reservations in SQLite; reconcile those records with AWS after restart.
- Refuse new launches when queue, ownership, price, budget or durable state
  cannot be established. Enforce lifetime cleanup and provide a cleanup-only
  mode that can run independently of the scaling process.
- Provide secret-safe structured status and deterministic fault tests.

Non-goals: Hetzner support, automatic provider selection, warm pools, snapshot
rebuilds, image construction, runner-secret distribution, automatic job
retries, workflow rewriting, a UI, or changing any deployment's IAM/network.
An operator's real cloud pilot is outside this product-source change.

## Capabilities

### New Capabilities

- `queue-observation`: authenticated repository-scoped observation, freshness,
  runnable eligibility and explicit label matching without modifying jobs.
- `spot-runner-lifecycle`: pinned-template launches, durable intent and
  ownership, one-job worker prerequisites, recovery and termination.
- `bounded-operation`: dry-run isolation, launch admission limits, conservative
  reservations, independent expiry cleanup and secret-safe operational status.

### Modified Capabilities

None. This is a new product with no existing capability specifications.

## Impact

Planned implementation: a Python 3.13+ CLI, standard-library HTTP/SQLite and
the official AWS SDK. Development tooling includes a pinned OpenSpec CLI;
Node is not a proposed controller runtime. Forgejo and EC2 remain the
authorities for jobs and instances respectively. Placement is operator-supplied
ordered configuration; the controller never discovers zones. A launch template,
qualified runner image, secure bootstrap, machine credentials and network
connectivity are operator-owned prerequisites, not resources this change
creates.

Tests and public CI use synthetic data and cloud-boundary substitutes. This
proposal does not establish working software, verified API compatibility,
cloud cost savings, an active deployment or permission to create resources.
