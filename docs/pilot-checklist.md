# Pilot checklist

This checklist is a preparation record, not authorization. Creating cloud
resources, changing IAM or network configuration, publishing an image or
running a pilot needs a separate operator decision with cost, lifetime and
cleanup bounds.

## 1. Image and template

- [ ] Image builds the pinned runner and the job toolchain the queue needs.
- [ ] On a launched worker, the recorded toolchain check passes as the job user
      and fails on any missing command — a loop, not a multi-argument
      `command -v`, which can succeed when only one name resolves:
      `for t in jq promtool uv ruff node /usr/lib/postgresql/*/bin/postgres /usr/lib/postgresql/*/bin/initdb; do command -v "$t" >/dev/null || { echo "MISSING $t"; missing=1; }; done; test -z "${missing:-}"`.
      Include the PostgreSQL **server** executables rather than `psql` alone,
      and check the pinned versions the persistent fleet declares. A missing
      tool is an image defect to fix in the image, never a reason to route the
      job elsewhere.
- [ ] Template version is numeric and points at a literal, immutable image id.
- [ ] `InstanceType`, architecture and memory suit the intended job class.
- [ ] `InstanceInitiatedShutdownBehavior = terminate`; disposable volumes
      delete on termination.
- [ ] Bootstrap registers a repository-scoped ephemeral runner, polls for a task
      in a bounded claim window, runs at most one job, exits after it, and also
      exits on bootstrap failure and when the claim window passes without a job.
- [ ] Maximum lifetime covers image boot, the claim window, one job and a failed
      bootstrap.
- [ ] The claim window is qualified live before the template version that
      carries it is set in the deployment, with every check in
      [runner-setup.md § The claim window](runner-setup.md#the-claim-window)
      ("Qualify it live before you enable it"): a bounded launch with no job
      queued ends with `STEP claim-timeout` and then `bootstrap exiting rc=0`, a
      launch that claims a job ends with the `completed` one-job marker, and the
      empty-poll, registration, forged-message and surviving-process checks pass
      on the pinned runner.
- [ ] The worker holds no controller, provisioning or AWS credential.
- [ ] Registration credentials arrive through an operator-owned secure channel —
      not user data, image layers or logs. The pinned runner CLI accepts its
      token only as an argument, so the bootstrap reads it from a `0600` file and
      shreds that file before the job starts.

## 2. Labels

- [ ] Every advertised label is qualified against the image and machine.
- [ ] The routing decision is recorded in `operations.md` § "The routing
      decision", including which labels the worker shares with the persistent
      fleet and the accepted risk of that overlap.
- [ ] A job with the burst label completes on the burst worker, a job with the
      shared pool label can be assigned to it, and a job requiring a label the
      worker does not advertise does not.

## 3. Forgejo contract

- [ ] An owner-scoped read token can call the runner-jobs endpoint.
- [ ] One waiting and one running job classify correctly in a `plan` run
  (dependency-blocked work is not listed by the endpoint)
      correctly in a `plan` run.
- [ ] `qualification = "live-verified"` is set only after that evidence is
      recorded with the deployment.

## 4. Capacity, quota and network

- [ ] The Region and every configured pool (instance type × availability-zone
      candidate) has Spot capacity; the account's Spot quota covers the
      configured `controller.max_instances` workers.
- [ ] The worker reaches the Forgejo endpoint over the approved path; TLS
      verification is on and no redirect is followed.
- [ ] Egress from the worker is limited to what the queue needs; the worker
      cannot reach unrelated private networks.
- [ ] Every configured instance type produced one **registered** worker in a
      canary run, and every configured candidate was exercised at least once; a
      pool that produced none was removed, not merely configured.
- [ ] When the worker subnets route through a router instance, its boot
      configuration forwards every candidate subnet CIDR, and it still does
      after a reboot ([router.md](router.md)).

## 5. Identity, limits and state

- [ ] Scaling and cleanup use separate machine identities with the reviewed
      scopes; neither is a copied human login cache.
- [ ] `max_spot_price`, `max_lifetime_seconds`, `controller.max_instances`,
      `window_seconds`, `allowance` and `non_compute_allowance` are reviewed for
      the pilot, and the allowance covers the admitted batch size.
- [ ] The ledger path is on durable local storage with the writer lock proven.
- [ ] The scheduler runs `apply`; a separate scheduler runs `reap --apply`
      outside the scaler's failure boundary, and that path was tested while the
      scaler was stopped.

## 6. Ownership and cleanup evidence

- [ ] A plan reports the exact template, Region, ordered pools, selected pool,
      tracked spend and forward commitment.
- [ ] A launched worker's readback shows all four ownership tags and the expiry.
- [ ] A refused or denied cleanup stays visible and does not free its slot.
- [ ] The operator knows that an accepted termination request is not confirmed
      removal and that residual charges may remain.

## 7. Rollback

- [ ] Disabling the `apply` schedule stops admission without touching cleanup.
- [ ] Owned resources can be identified from tags alone for cleanup.
- [ ] Reverting the source is a reviewed change, not a history rewrite.

## 8. Acceptance

- [ ] One comparable job completed and its cost was recorded against the
      tracked cost and the operator's expectation.
- [ ] Abrupt instance loss was reconciled; Forgejo, not ci-burst, decided the
      job result.
- [ ] No On-Demand fallback, automatic retry or manufactured success occurred.

## 9. Automated warm image build (optional)

Use this section only if you intend to run the `ci-burst image` commands
([operations.md](operations.md#automated-warm-image-build)). Its items follow
section 9 of the task list of the `automated-warm-image-build` change, in order.
Like the rest of this checklist, ticking an item records preparation, not
authorization: every step that creates cloud resources, changes IAM or network
configuration, or spends money needs its own operator decision with cost,
lifetime and cleanup bounds. Keep `[image].enabled` false until the gates that
come before enabling are recorded: G1 to G5, G9, G10, G12 and G13, and the check
that the tagged `GetConsoleOutput` grant applies to a builder
([deployment.md](deployment.md#image-build-role-and-source-identity-optional)),
which no numbered gate covers. A refused console read leaves a build in flight
instead of ending it ([#203](https://github.com/mikomiel/ci-burst/issues/203)). The other
gates are recorded later, in the items that name them: G6 after enabling and
before the first build, G7 and G11 from the first build, and G8 only once a timer
unit exists. Record the evidence in your own deployment records, not in this
repository.

- [ ] A fresh independent reviewer has read the policy file
      ([deploy/iam/image-build-policy.json](../deploy/iam/image-build-policy.json))
      and the credential boundaries before anything is applied.
- [ ] You applied the IAM yourself: the controller applies none of it. The image
      role has a trust policy with the one source principal, the external-ID
      condition and a maximum session of 1 hour. The source identity has
      `sts:AssumeRole` on that one role only. The policy file is attached
      ([deployment.md](deployment.md#image-build-role-and-source-identity-optional)).
- [ ] The verification gates that come before enabling are run with the IAM
      policy simulator or a real dry run, and each result is recorded: G1
      (instance-profile Deny), G2 (build-template Deny with a user-data
      override), G3 (request tags on the volume and the network interface), G4
      (extra `CreateImage` actions), G5 (pass-role for
      `CreateLaunchTemplateVersion`; add the one scoped statement only if it is
      needed), G9 (SDK profile with `external_id` and
      `duration_seconds`, refresh inside the container, and whether a
      source-identity condition is possible), G10 (effective expiry of a
      presigned URL that assumed-role credentials signed), G12 (a version that
      names a pending image) and G13 (read-back normalization). The EC2 and S3
      API contracts are qualified before `[image]` is enabled.
- [ ] The private bucket prefix has block public access, default encryption and
      a 7-day lifecycle rule on the prefix. The build launch template has the
      cold base image, no instance profile, IMDSv2 required with hop limit 1,
      terminate on shutdown, and a 20 GiB `gp3` volume that deletes on
      termination. The build security group has no ingress and minimal egress.
      The build subnet exists
      ([deployment.md](deployment.md#the-image-tables-optional)).
- [ ] The staging prefix belongs to this deployment alone: no other deployment,
      tool or person writes under it, and no prefix of another deployment is the
      same as yours, inside it or around it. Do not reuse the example prefix.
      Every applying pass deletes each object under the prefix that is more than
      one hour old, except the archive of the active build, and it checks neither
      the key name nor the writer
      ([deployment.md](deployment.md#the-image-tables-optional)).
- [ ] A dedicated read-only Forgejo token exists, ideally for a dedicated
      read-only collaborator account, with its own env file, and its scope is
      recorded ([deployment.md](deployment.md#the-image-token)).
- [ ] The deployment TOML is in its own directory: the build line mounts it
      read-only and `promote --write` writes in it
      ([deployment.md](deployment.md#the-dedicated-toml-directory)).
      `[image]` is added with `enabled = false`, and `image plan` reports
      `disabled` and makes no network call.
- [ ] Only after the gates that come before enabling have passed,
      `[image].enabled = true` is set, as its own approved step. `image plan`
      then confirms: the token reads the branch, every basis file and the first
      chunk of the archive; the branch is reported protected; the rendered
      user-data size; and the build template is safe. You also confirm that those
      earlier gates are recorded. The `protected` flag is the part of G6 that
      `image plan` settles. A passing plan does not show that the token is
      read-only: an overprivileged token passes the same reads, so record the
      scope that Forgejo shows for the token as separate evidence.
- [ ] Before the first build, you inspect the archive yourself to settle the rest
      of G6: you list the archive of the commit that `image plan` reports, confirm
      the leading directory that `strip_components` drops, and confirm that
      `export-ignore` removes no file that the warm commands need. `image plan`
      reads one chunk of the archive and shows neither
      ([deployment.md](deployment.md#the-image-tables-optional)).
- [ ] The first manual build runs as `image run --apply --until-done --force`
      ([container.md](container.md#run-the-image-commands)). You record the
      console latency and the blind-probe cause (G7), the build duration,
      whether terminating at `pending` is safe (G11) and the token scope. You
      confirm that `CreateImage` ran in the pass that saw the marker and that the
      run needed no timer. You leave `[image.warm]` unchanged while the build is
      active ([warm-image.md](warm-image.md#the-candidate-and-what-the-gate-proves)).
- [ ] You reviewed the candidate: `image promote` without `--write` printed its
      evidence and you read it
      ([operations.md](operations.md#candidate-and-promotion)). Before
      `promote --write`, a rollback or a hand edit of the pin, you finished or
      stopped any running `--until-done` container, and afterwards you started a
      fresh one ([container.md](container.md#run-the-image-commands)).
      Before the pin changed, one canary job completed on the new version from a
      test identity or a test deployment
      ([warm-image.md](warm-image.md#canary-and-cutover)): `promote --write`
      replaces the production pin at once, and the next scheduled `apply` can
      launch from it. Only then `promote --write` ran in the isolated container,
      the first production job on the new version was watched, and `--rollback`
      restored the previous pin
      ([operations.md](operations.md#rolling-back-an-image)). The effect on job
      duration is measured, as the `warm-worker-image` change already requires
      (its tasks 6.5 and 6.6).
- [ ] After the first manual build and about three clean promotions, the timer
      unit is qualified (G8) and enabled as a separate approved step, and one
      timer period passes without a leak. A clean promotion is one that you did
      not roll back and whose first jobs completed on the warm cache. Do not
      enable the timer earlier. This repository ships no unit or timer for the
      image build today, so G8 and this item apply only once one exists.
- [ ] When the cold base image or the build launch template changes, you force a
      rebuild with `image run --apply --until-done --force`, because the basis
      digest covers neither, only after any build in flight has settled, and you
      check that the candidate's builder was launched after the change from the
      intended template before you promote it
      ([warm-image.md](warm-image.md#triggers)).
- [ ] You rotate the source key on the schedule that
      [deployment.md](deployment.md#image-build-role-and-source-identity-optional)
      states, and record the date.
- [ ] After the timer period, you decide whether to open a separate
      automatic-promotion change. Automatic promotion is not part of this
      feature.
