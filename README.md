# ci-burst

A small Forgejo queue-driven AWS Spot runner controller — **currently in
design, not implemented**. This is an original MIT-licensed public project.

## Intended first version

- Observe runnable jobs for one Forgejo repository and explicit runner labels.
- Add at most one Spot VM from an operator-prepared, pinned launch template.
- Keep Forgejo as the only scheduler and job-result authority.
- Persist launch intent, ownership and conservative cost reservations.
- Default to dry-run; require explicit apply and independent expiry cleanup.

There is no controller CLI, qualified image, release or deployment in this
bootstrap. AWS resources, IAM, networking, registration credentials and runner
images remain operator-owned prerequisites. No cost-saving or absolute
AWS-billing-cap claim has been established.

## Project stage

The initial OpenSpec proposal, design, acceptance scenarios and ordered tasks
will be reviewed in a separate pull request before controller implementation.
This bootstrap provides project guidance and pinned planning validation only.
Node is a planning-tool dependency, not the proposed controller runtime.

## Planning checks

Use Node.js 22.12 or newer and npm:

```sh
npm ci --ignore-scripts
npm run check:spec
```

Public CI is read-only and uses no cloud credentials or billable operations.
It validates planning artifacts and scans public content; it does not prove
a live Forgejo/AWS integration.

## Contributing and security

Read [CONTRIBUTING.md](CONTRIBUTING.md), [AGENTS.md](AGENTS.md) and
[SECURITY.md](SECURITY.md). GitHub is the sole source/PR surface. After the
one-time bootstrap, changes use protected-main pull requests. Controlled
automated merging is not active merely because this policy exists.

License: [MIT](LICENSE). No affiliation with Forgejo or Amazon Web Services
is implied.
