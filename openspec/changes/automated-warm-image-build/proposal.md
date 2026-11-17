# Proposal

## Why

The warm worker image is built by hand. An operator makes a source archive,
uploads it, launches a builder, watches the console for the ready marker, calls
the image-creation API, publishes a launch-template version, edits the pin in the
deployment configuration and removes the builder, the archive and old images. One
build takes 10 to 15 minutes of operator attention, and the manual path has run
three times. It is the only runtime evidence this change has.

Manual builds happen late. A lockfile changes, nobody rebuilds, and every worker
pays the transfer again. Manual builds also leak: a builder, an archive object or
an old image survives when a step is skipped. The operator decided on 2026-11-17
to automate the build inside ci-burst, with promotion kept under operator control.

The `warm-worker-image` change deliberately stops at a ready marker: the script
creates no image, and ci-burst creates no image. Today `openspec/config.yaml` and
the `spot-runner-lifecycle` capability state that the controller creates no
runner images. This change reverses that one clause, and only that clause.

## What Changes

- Add a `ci-burst image` command group: `plan`, `run`, `status` and `promote`. `plan` and `run` without `--apply` are read-only. `run --apply` stages a source archive, launches one builder, observes the ready marker, creates the image and creates an inert launch-template version. One pass is bounded, so `run --apply --until-done` supervises repeated passes without a timer until the build reaches `candidate` or `failed`.
- Bound the scope change in `openspec/config.yaml`, exactly: when the operator opts in with `[image].enabled = true`, ci-burst MAY create, tag and delete worker images, their snapshots, tagged builder instances and non-default launch-template versions, and stage and delete a source archive under an operator-provided private bucket prefix. It MUST NOT create or widen IAM, networking, buckets, launch templates or credentials. A timer MUST NEVER move the production pin. The change needs maintainer sign-off for this reason.
- Use a separate AWS identity for image builds: a role that the SDK assumes with short sessions through a shared-config profile (`role_arn` with `source_profile`). The source identity can call `sts:AssumeRole` on that one role only. The scaling identity is never used for builds. The operator applies all IAM. The controller code has no IAM client.
- Use a dedicated read-only Forgejo token, ideally of a dedicated account, in its own env file. The token that `apply` uses is never reused. `image plan` tests the token before anything is enabled, and the operator runbook records its scope.
- Keep image build state in its own SQLite file, with its own lock and schema version. The scaling ledger and its schema are not touched.
- Add `ci-burst image promote`. Without `--write` it prints the verified evidence and the diff of the one pin line. With `--write` it edits only the `launch_template_version` key of the `[aws]` table, atomically, with a strict re-parse before the rename, a timestamped backup and a durable journal row committed before the rename, and it refuses when the file changed since it was read or the candidate is not verified. `--rollback` restores the recorded previous pin. Promotion is an operator-invoked one-off in a container that has no AWS credentials. No timer promotes.
- Phase 1 is the manual commands (`run --apply --until-done`). Phase 2 adds a systemd timer and a lockfile and basis-digest trigger. It is enabled only after the first manual build succeeded and about three clean promotions.
- Add bounded retention (keep 3 images), reconcile-first cleanup of leaked builders and archives, a rate limit (one build per 24 hours, at most two per day), a failure cooldown and a `blocked` state.
- Ship the warm script to the controller container, render builder user data in a compact gzip and base64 form with a size test, and add an optional archive-prefix input to `warm-cache.sh`.

## Impact

- No admission, placement, pricing, allowance, Spot handling, job retry or Forgejo-result behaviour changes. The scaling identity, its credentials, its ledger and the apply, reap and export units are unchanged. When `[image]` is absent or disabled, every image command exits without a network call.
- New: `ci_burst/image.py` and `ci_burst/image_state.py`. Extended: `ci_burst/forgejo.py` (branch, archive stream), `ci_burst/aws.py` (builder, image, template version, S3), `ci_burst/config.py` (strict `[image]` tables), `ci_burst/cli.py`, the container build (the warm script must be inside the image), `deploy/runner/image/warm-cache.sh` (one optional input), `deploy/iam/image-build-policy.json` (new, placeholders only) and the documentation. Phase 2 adds `deploy/ci-burst-image.service` and `deploy/ci-burst-image.timer`.
- One more credential set exists on the controller host: the source identity of the image role, mounted only into the image unit. It can only assume the build role. The build role can act on tagged resources and one bucket prefix. It cannot touch IAM, networking, other instances or the production pin.
- The builder runs consumer code at a source revision on the protected branch. The offline gate proves cache behavior, not image safety. That is why a person promotes.
- Cost is bounded by duration, the daily cap and retention. It is not an AWS billing cap, and no cost figure here is measured.
- The change authorizes no IAM application, no bucket, template or token creation, no live build and no deployment. Those are the operator's steps in `tasks.md` section 9.
- Documentation result: `source_docs` with the change; operational knowledge only after the live qualification.
- Approvals needed: maintainer sign-off on the scope change (a `openspec/config.yaml` edit is a sensitive path); the operator applies IAM and creates the bucket, the build launch template, the build security group, the token and the env file.
- Non-goals: automatic promotion (a later, separate change), creating or widening IAM, networking, buckets, launch templates or credentials, a synthetic canary job, rebuilding the cold base image, chaining a build from the warm image, deleting launch-template versions, cross-region copy or image sharing, Spot builders, parallel builders, a billing cap, an inbound webhook, and consumer-specific logic in the product.
