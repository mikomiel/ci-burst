# Contributing

ci-burst is at the design stage. Discuss changes against the active OpenSpec
proposal before implementing the controller. Keep changes small and testable
without a private platform, cloud account or production credentials.

## Pull requests

Use an isolated branch/worktree with one writer. After bootstrap, do not push
directly to main or rewrite published history. Required CI and independent
review must apply to the exact current head. An authoring agent does not
merge its own PR, enable a standing automerge order or bypass protection.
If approved merge automation is unavailable, stop at the maintainer gate.

Changes to automation/review policy, permissions, agent instructions,
`.github/`, `scripts/`, `tests/`, dependencies or packaging need a human
maintainer decision. Keep that boundary intact; a green check alone is not
permission to change it. No release, tag, package, image or cloud deployment
is authorized by a source PR.

## Review and merge

After maintainer-controlled activation, main requires the exact-head
`planning`, `public-content` and `review` checks. The `review` status comes
from the trusted default-branch publisher, not from a native approval count
that the configured App cannot satisfy. A maintainer can click Merge when
these gates pass; repository automation can merge eligible ordinary PRs.

`scripts/pr_policy.py` is the shared rule owner. It accepts the pinned Gandalf
bot identity or a different human reviewer whose write/maintain/admin access
was verified through GitHub. It requires approval of the current head, rejects
outstanding change requests, and treats dismissal or unreadable evidence as
a refusal. Comments do not erase decisive reviews. There is no dependency-bot
review exemption.

The publisher and merger run only trusted default-branch scripts. Neither
executes PR-head code or consumes PR-produced artifacts with a write token.
The merger checks complete API results, both CI checks from GitHub Actions,
the official `review` status, the immutable base/head file comparison and
fresh refs/reviews/checks before a head-pinned squash merge. It admits at most
one merge per run and verifies the remote result. A timeout is not permission
to repeat a merge blindly.

The sensitive-path list in `scripts/pr_policy.py` is enforced, not advisory:
workflows, scripts, tests, agent instructions, security policy and dependency/
packaging definitions need a human decision and are never auto-merged. Both
sides of a rename count; an incomplete file list cannot clear the guard.

### Status freshness and failure behavior

A commit status is a stored assertion, not an atomic live review query.
Trusted lifecycle events, CI completion and a five-minute sweep refresh the
`review` status. A human click relies on the last published evaluation; a
withdrawal may take until the next successful sweep to appear. Automation
independently reads the reviews again immediately before its merge request.
An unreadable review snapshot revokes known heads and fails the job. If PR
enumeration itself fails, unknown heads cannot be revoked; the job fails
loudly rather than claiming a fresh evaluation.

### Maintainer-controlled activation and rollback

Source tests do not establish an active merge gate. Use this order:

1. Keep existing protection while the policy PR receives green exact-head CI
   and independent review. Land it through an authorised maintainer/bootstrap
   path; an authoring agent does not self-merge or invent a green status.
2. Verify the default-branch publisher runs successfully and posts `review`
   on the exact current PR head. Verify a changed or unapproved head is refused.
3. Configure PR-only protection with zero native required approvals and require
   `planning`, `public-content` and `review` from GitHub Actions (App 15368).
   Keep strict up-to-date checks, administrator enforcement, conversation
   resolution, and the bans on force pushes and branch deletion. A ruleset
   requiring a pull request with zero approvals can retain PR-only enforcement.
4. Read the effective rules back. Check a valid owner-click path and one
   ordinary reviewed automation candidate; sensitive policy PRs remain human.
   Keep GitHub's built-in standing auto-merge orders off: the workflow owns
   its bounded, freshly checked merge request.

If activation fails, retain or restore the native maintainer-review requirement
and disable the automerge workflow. Preserve CI, state truth and published
history. Revert defective source through a reviewed PR. Never lower protection
merely to get the policy PR merged.

Local observation is safe and does not publish statuses or merge:

```sh
python3 scripts/github_pr_automation.py review
python3 scripts/github_pr_automation.py merge
```

`--apply` is refused outside the expected default-branch GitHub workflow
context. Do not use it from an authoring session. No policy operation creates
a release, deployment or cloud resource.

## Checks

Use Node.js 22.12+, Python 3.12+ and the committed planning lockfile:

```sh
npm ci --ignore-scripts
npm run check:policy
npm run check:spec
```

Policy tests use synthetic API snapshots and transport substitutes, not a
GitHub write token. The pinned YAML parser is a development-only dependency
for semantic workflow tests; it was already present in the planning tool's
locked dependency graph.

The CI workflow pins the Gitleaks version and binary checksum. Before any
public push, run that version against the full intended commit range and an
export of its intended files with `.gitleaks.toml`, `--redact` and
`--no-banner`. Also scan the complete intended blob stream through
`gitleaks stdin`, so filename allowlists do not exempt generated lockfiles.
The CI workflow includes that step. Scan commit messages as content as well.
Inspect generated files, fixtures and example values, not just the working-tree
diff. CI after push cannot prevent disclosure. Never upload a scan report
containing a secret; use redacted diagnostics.

Tests and examples must use synthetic data and reserved example domains.
Do not publish real private deployment names, addresses, URLs or credentials.
An ignore file is not a secret scanner.

## Evidence and live tests

Distinguish planned behavior, source present, reviewed, merged, deployed and
runtime verified. Report checks as PASS, FAIL, NOT RUN or UNAVAILABLE.
Structural OpenSpec validation does not validate a real queue or cloud API.

Live/billable tests require a separate explicit scope, least-privilege machine
identity, Region, cost/lifetime/concurrency bounds and cleanup/rollback plan.
Default PR checks must not need those permissions. No human login cache or
provisioning credential belongs on a worker.
