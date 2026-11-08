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

## Checks

Use Node.js 22.12+ and the committed planning lockfile:

```sh
npm ci --ignore-scripts
npm run check:spec
```

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
