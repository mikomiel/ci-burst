# Proposal

## Why

The refresh script adds one immutable `localhost/ci-burst:sha-<12>` tag per
merged commit and removes none. At about 150 MB per build, one 7.8 GB state host
reached 100% disk after roughly forty-five merges, so an Ansible converge failed
with `ENOSPC` and the next build had nowhere to write (HB-3340).

The retained set cannot be guessed: the only rollback handle is
`localhost/ci-burst:previous`, a host may be serving an image built before the
build records existed, and the deployment-state document names commits that a
support path may still resolve. Retention is therefore an explicit contract, not
a "keep the newest few tags" heuristic.

## What Changes

- After a run has proven the serving image is the promoted one, the refresh
  script removes build tags outside the retained set: `latest`, `previous`, the
  tag of the deployed commit recorded on the host, the tag of every 40-hex ref
  the deployment-state document names, and the most recent
  `CI_BURST_RETAIN_BUILDS` builds (explicit variable, default 3).
- It prunes the resulting dangling images, scoped by this product's own build
  label, because an untagged image still owns its layers.
- It keeps and reports any tag whose image cannot be inspected, and a refusal
  changes neither the promotion nor the exit status.

## Impact

- No admission, launch, queue, cost or job-result behaviour changes. The
  controller's data, ledger and credentials are untouched.
- Deletion is limited to tags this deployment created, and only outside the
  retained set. A build can always be rebuilt from its commit; the rollback
  handle is in the retained set by name.
- The change bounds disk growth per host. It does not clean a host that is
  already full: that cleanup is an operator action with its own go.
- Non-goals: removing images by age, pruning unrelated dangling images,
  garbage-collecting the image store beyond the dangling set, and any retention
  of the runner AMI or launch-template versions.
