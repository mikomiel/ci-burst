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
from the trusted default-branch publisher and is distinct from its publisher
job completing successfully. Native approval requirements remain additional
constraints: a posted App approval is not proof that GitHub counted it.
A human maintainer can click Merge only when all effective rules pass;
repository automation can merge eligible ordinary PRs under the same rules.

Automation's merge scope is exactly the ordinary-path allowlist: `README.md`,
`CHANGELOG.md` and the named OpenSpec documents. Changes under `ci_burst/`,
`docs/`, `tests/`, `.github/` and dependency files such as `pyproject.toml`,
`package.json` and `package-lock.json` are merged by a person. That is the
intended scope, not a gap.

`scripts/pr_policy.py` is the shared rule owner. It accepts the pinned Gandalf
bot identity or a different human reviewer whose write/maintain/admin access
was verified through GitHub. The verified-maintainer fallback serves the
manual merge path; unattended merges still require Gandalf itself at the
current head. Outstanding change requests from eligible reviewers block both
paths. Dismissal or unreadable evidence is a refusal. Comments do not erase
decisive reviews. There is no dependency-bot review exemption.

The publisher and merger run only trusted default-branch scripts. Neither
executes PR-head code or consumes PR-produced artifacts with a write token.
The merger checks complete API results, both CI checks from GitHub Actions,
the official `review` status, the immutable base/head file comparison and
fresh refs/reviews/checks before a head-pinned squash merge. It admits at most
one merge per run and verifies the remote result. A timeout is not permission
to repeat a merge blindly.

The ordinary-path allowlist in `scripts/pr_policy.py` is enforced, not
advisory. Unknown paths need a human decision; they are not implicitly safe.
This pre-implementation repository initially admits only root README/changelog
and the named OpenSpec proposal, design, tasks, metadata and capability-spec
files. Arbitrary Markdown is not implicitly ordinary: new documentation areas
and agent-instruction variants still require a human. Workflows, scripts
wherever introduced, tests and dependency/packaging definitions are not
admitted. New ordinary areas require a reviewed policy change and tests. Both
rename endpoints count; an incomplete file list cannot clear the guard.

### Status freshness and failure behavior

A commit status is a stored assertion, not an atomic live review query.
Trusted lifecycle events, CI completion and a five-minute sweep refresh the
`review` status. A human click relies on the last published evaluation; a
withdrawal may take until the next successful sweep to appear. Automation
independently reads the reviews again immediately before its merge request.
An unchanged draft keeps its own pending verdict without invalidating an
unrelated ready PR. An unreadable snapshot revokes known heads and fails the
job. Negative status updates are attempted before new successes, and one
per-head publication failure does not suppress other known revocations. Any
failed revocation fails the run without publishing new successes. If PR
enumeration itself fails, unknown heads cannot be revoked; the job fails
loudly rather than claiming a fresh evaluation.

### Maintainer-controlled activation and rollback

Source tests do not establish an active merge gate. Use this order:

1. Keep existing protection while the policy PR receives green exact-head CI
   and independent review. Land it through an authorised maintainer/bootstrap
   path; an authoring agent does not self-merge or invent a green status.
2. Verify the default-branch publisher runs successfully and posts `review`
   on the exact current PR head. Verify a changed or unapproved head is refused.
3. Require `planning`, `public-content` and the semantic `review` status from
   GitHub Actions (App 15368). Preserve the configured native approval count,
   stale-review dismissal, strict freshness, administrator enforcement,
   conversation resolution and force-push/deletion bans. If the App approval
   does not satisfy the native count, stop for a separate owner policy decision;
   do not lower the count or broaden App permissions to work around it.
4. Read the effective rules back. Check a valid owner-click path and one
   ordinary reviewed automation candidate; sensitive policy PRs remain human.
   The repository's `allow_auto_merge` capability can be enabled without
   arming a merge. An authoring agent must not place a standing `--auto` order;
   this workflow owns its bounded, freshly checked merge request.

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

## Releases

A release exists only after both maintainer actions, in order:

1. an annotated semantic version tag `vMAJOR.MINOR.PATCH` on `main`; and
2. a GitHub Release for that tag whose notes are the matching `CHANGELOG.md`
   section.

Creating the tag is a maintainer decision. A merge, a green check, a changelog
entry or a package build is not a release or a deployment, and none of them
authorizes a tag, a package, an image, a deployment or a cloud change.

Before 1.0, an observable behaviour change is a minor release and a fix is a
patch; no other compatibility is promised.

A release uses two changelog states:

- **Preparation**, before the tag exists: move the Unreleased entries under
  `## [X.Y.Z] - YYYY-MM-DD` and add `<!-- release-pending: vX.Y.Z -->` below
  that heading. The marker is the explicit preparation state; `check:policy`
  accepts the heading without its tag only while that marker is present. Merge
  the preparation pull request.
- **Published**, after the tag: create the annotated `vX.Y.Z` tag on the merge
  commit and the GitHub Release from that section, then remove the marker in a
  follow-up change. `python3 scripts/render_changelog.py --validate-release`
  verifies that every released heading has its annotated `vX.Y.Z` tag and that
  no marker remains.

The tag must be `v`-prefixed and annotated; a lightweight or unprefixed tag is
not a release and the checks reject it. A released heading without either the
preparation marker or its tag is a defect, and so is an annotated release tag
without its released section.

`CHANGELOG.md` records merged source history, never deployment or runtime
evidence. Its Unreleased section is generated from merged `feat:`, `fix:`,
`docs:` and `ci:` subjects after the latest release tag:

```sh
python3 scripts/render_changelog.py --write
```

`--write` refuses while a preparation marker is pending and its tag does not
exist; do not regenerate between cutting the section and tagging it.

Regeneration fails closed rather than guessing the boundary: the renderer
refuses a shallow checkout, a released section that cannot be reconciled with
a local reachable annotated tag (for example a `--no-tags` clone or a tag on
an unrelated branch), a malformed, noncanonical or duplicate release heading,
and a preparation marker whose tag does not exist. Fetch tags or the full
history instead of bypassing the check.

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
