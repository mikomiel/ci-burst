# ci-burst contributor instructions

ci-burst is an original public open-source project. GitHub is the only
authoritative source and PR surface for this repository.

## Current stage

The repository is being bootstrapped with an OpenSpec change. No controller
is implemented yet. Read `openspec/changes/aws-spot-queue-burst/` before
implementation. Planning completion is not implementation or runtime evidence.

## Working contract

- Keep one writer per branch/worktree. Preserve unrelated work.
- Use task branches and focused pull requests after the initial bootstrap.
  Do not push directly to protected main or rewrite published history.
- Required CI and independent review must describe the exact current head.
  A configured merge automation owns ordinary merges; an authoring agent
  does not manually merge or enable a standing automerge order.
- Changes to workflow policy, permissions, agent instructions, scripts, tests,
  dependency definitions or packaging require a maintainer decision. Do not
  narrow that boundary to unblock a pull request.
- A public-source change does not authorize release tags, packages, images,
  deployment, cloud resources, credentials or IAM changes.

## Public-content boundary

Use synthetic fixtures and examples. Do not publish real deployment names,
private addresses, webhook URLs, credentials, tokens or private runbooks.
Scan the entire intended commit range before a public push; a CI scan after
push cannot prevent disclosure. Never put secrets in command arguments or
logs. Do not run credential-bearing or billable tests without a separately
approved scope, cost bound and cleanup plan.

## Project shape

The initial design is one Forgejo repository scope and one AWS Spot backend.
Do not add another scheduler, a dashboard, a provider plugin framework or a
private-platform dependency. Product configuration is separate from any
operator-specific deployment. Use controlled technical English for specs.

## Checks

The planning tooling is pinned in `package-lock.json`. Run:

```sh
npm ci --ignore-scripts
npm run check:policy
npm run check:spec
```

Use Python 3.12+ for the standard-library policy tests. The policy automation
CLI is observation-only in authoring sessions; do not invoke `--apply`.
`CONTRIBUTING.md` owns the maintainer activation and rollback procedure.

The CI workflow owns its secret-scan invocation. Run the same scan before
publication. Read `CONTRIBUTING.md` and `SECURITY.md` for contributor and
security-reporting routes. Report checks as PASS, FAIL, NOT RUN or UNAVAILABLE.
