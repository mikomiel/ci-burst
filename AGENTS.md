# ci-burst agent instructions

ci-burst is a small controller that adds AWS Spot runners while one Forgejo
repository has queued jobs. Licence: GPL-3.0-only. GitHub is the only
writable source and pull-request surface; the target branch is `main`.
Non-goals: another scheduler, a dashboard, a provider plugin framework, a
published image, a dependency on a private platform.

## Checks

```sh
npm ci --ignore-scripts
npm run check:policy
npm run check:spec
```

- Prerequisites: Node.js 22.12 or newer and Python 3.12 or newer.
  `package-lock.json` pins the planning tools.
- Run these commands without asking. The policy tests use synthetic API
  snapshots and transport substitutes. They need no credential and no cloud
  account.
- A change is ready for a pull request when every command passes.
  `CONTRIBUTING.md` "Review and merge" owns the contexts that `main` requires
  on the exact head.
- `python3 scripts/github_pr_automation.py review` and `merge` only observe.
  Do not pass `--apply` in an authoring session. The script refuses it
  outside the default-branch workflow.
- Before a push, run the Gitleaks scan that `CONTRIBUTING.md` "Checks"
  describes, with the version that the CI workflow pins. A CI scan after the
  push cannot prevent disclosure.

## Project shape

- Forgejo is the only scheduler and the only job-result authority. A VM
  launch is not a passed job.
- The scope is one Forgejo repository and one AWS Spot backend.
- The controller observes by default. It launches only on an explicit
  `apply`. The shipped `qualification = "fixtures-only"` default blocks
  `apply` until an operator records live qualification evidence.
- The controller persists launch intent, ownership and cost commitment
  before each cloud request. Unknown queue, price, ownership or state blocks
  new capacity.
- A worker is untrusted after it accepts job code. Do not give a worker the
  controller's cloud-provisioning identity.
- Product configuration is separate from a deployment. Deployment state and
  live qualification records stay outside this repository.

## Working contract

- Use a task branch and one focused pull request. Keep one writer per branch
  and worktree. Preserve unrelated work.
- Do not push to `main` and do not rewrite published history. Branch
  protection enforces both.
- Required CI and the independent review must describe the exact current
  head. A new commit needs fresh CI and a fresh review.
- The merge automation owns ordinary merges. An authoring agent does not
  merge its own pull request and does not enable a standing auto-merge order.
  When the automation cannot merge, stop and report the pull request and the
  tested head to the maintainer.
- `scripts/pr_policy.py` owns the merge policy. Only `README.md`,
  `CHANGELOG.md` and the named OpenSpec documents are ordinary paths. Every
  other path needs a maintainer merge, including these instructions,
  `.github/`, `scripts/`, `tests/`, `ci_burst/`, `docs/`, the licence and the
  dependency files. Do not narrow that boundary to unblock a pull request.
- A source change does not authorize a release tag. A maintainer merges the
  release pull request to authorize the tag and the GitHub Release. Neither
  authorizes a package, an image, a deployment, a cloud resource, a
  credential or an IAM change.

## Content boundary

- Use synthetic fixtures, examples and reserved example domains. Do not
  publish deployment names, private addresses, webhook URLs, credentials,
  tokens or a private runbook.
- `deploy/runner/bootstrap.sh` is the one documented exception. Its literals
  are published user data. `CONTRIBUTING.md` owns that decision. Do not add a
  second exception and do not replace its values with examples.
- Scan the entire intended commit range and its commit messages before a
  push. Do not put a secret in a command argument or a log.
- Do not run credential-bearing or billable tests without a separately
  approved scope, cost bound and cleanup plan.
- The console reader copies no arbitrary console text. Keep worker output
  bounded and treat it as untrusted input.

## Where to read more

- For the queue contract, configuration, identities, cost scope and rollback,
  read `docs/operations.md`.
- For maintainer activation and rollback of the merge automation, and for
  releases, read `CONTRIBUTING.md`.
- For the product security boundaries and the private reporting route, read
  `SECURITY.md`.
- For requirements, read `openspec/specs/`. `openspec/changes/archive/` holds
  the design and acceptance record of the delivered change.
- For a specification, use controlled technical English.

## Reporting

Report each check as PASS, FAIL, NOT RUN or UNAVAILABLE. Keep planned, source
present, reviewed, merged, deployed and runtime verified distinct. Source
completion is not deployment or runtime evidence.
