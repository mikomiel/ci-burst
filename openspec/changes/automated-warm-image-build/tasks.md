# Tasks

Sections 1 to 7 are phase 1 product-source tasks. Section 8 is phase 2 source
work. They authorize no deployment, no IAM change, no credential, no bucket,
template or token creation, no live build and no cloud change. Section 9 lists the
operator steps that need their own approval, and no source task stands in for
them. Land the work in this order, each step as its own pull request with exact-head
CI: the artifacts and the policy file (1), configuration and state (2 and 3),
providers (4), the build pipeline and the commands (5 and 6), documentation (7),
and only then the timer (8).

## 1. Artifacts, scope sentence and policy file

- [ ] 1.1 Land these change artifacts and the `openspec/config.yaml` `context` edit that states the bounded scope. Get maintainer sign-off on the scope change in the pull request, because `openspec/config.yaml` is a sensitive path.
- [x] 1.2 Add `deploy/iam/image-build-policy.json` with placeholders only (`<account-id>`, `<region>`, `<deployment>`, `<build-template-id>`, `<worker-template-id>`, `<build-subnet-id>`, `<build-security-group-id>`, `<base-image-id>`, `<bucket>`, `<prefix>`). Follow the statement list in the design. Include the explicit Deny on an instance profile and the explicit Deny outside the build template. Omit `ec2:ModifyLaunchTemplate`, `ec2:DeleteLaunchTemplateVersions`, `ec2:ModifyImageAttribute`, every `iam:*` action and `ssm:*`. Do not add `iam:PassRole` yet (gate G5).
- [x] 1.3 Add a policy test that copies the pattern of the existing IAM policy tests. Assert: no `iam:*` action, no `ec2:ModifyLaunchTemplate`, no `ec2:DeleteLaunchTemplateVersions`, both explicit Denies are present, every Allow on a mutating action has a tag or resource-scope condition, S3 access names the prefix, and `iam:PassRole` appears only as one statement with the `ec2.amazonaws.com` condition.
- [x] 1.4 Add a test that asserts the controller source constructs no IAM client (scan the package for an `iam` client construction).
- [x] 1.5 Add the source-identity policy and the role trust policy as documented examples (placeholders, `sts:AssumeRole` on the one role only, the external-ID condition, maximum session 1 hour). Ship them in the documentation, not as controller code.

## 2. Configuration

- [x] 2.1 In `ci_burst/config.py`, add the optional strict `[image]`, `[image.source]`, `[image.warm]`, `[image.build]` and `[image.storage]` tables with the keys, ranges and defaults from the design. Reuse the existing allowed-keys and range helpers. Keep the refusal of unknown keys and out-of-range values with `CONFIG_INVALID`. Omit `check_interval_seconds` until section 8.
- [x] 2.2 Refuse an `[image].profile` equal to `[aws].profile`. Refuse a basis path with `..` or a leading `/`, more than 32 basis paths, a warm command or gate command with a control character, and `source.strip_components` other than 0 or 1.
- [x] 2.3 In `tests/test_config.py`, test the absent table, `enabled = false`, each default, both bounds and a value just outside each, a non-integer, an unknown key and each refusal in 2.2. Use synthetic names only.

## 3. Image state

- [x] 3.1 Add `ci_burst/image_state.py`: its own SQLite file `image-state.sqlite` with its own lock file and schema version, in a directory that the scaling code never opens. Create the tables `image_builds`, `pin_log` and `control` from the design.
- [x] 3.2 Create the unique index `one_active` on `image_builds(deployment)` over the states `building` and `imaging`. Do not put it on `id`.
- [x] 3.3 Test: a second active row fails on the index, a `candidate` row does not block a new build, the state file never touches the scaling ledger file or its schema version, a reopened file at a newer schema version refuses cleanly, and a lost file is recreated without error.
- [x] 3.4 Test that the scaling `apply` path opens its ledger unchanged when an image state file sits beside it, and never reads the image file.

## 4. Providers

- [x] 4.1 Extend `ci_burst/forgejo.py` (it exists) with a branch read that returns the head sha and the `protected` flag, a raw-file read for the basis paths and the token test, and a streaming archive read by sha with the size cap. Keep the existing transport, redirect refusal and response-size discipline. Read the token only from the variable named by `source.token_env`.
- [x] 4.2 Test with the existing fake transport: 401 and 403 give `IMAGE_SOURCE_FORBIDDEN`, an unprotected or missing flag gives `IMAGE_BRANCH_UNPROTECTED`, the archive is requested by sha, an archive over the cap aborts the stream, and the token of `apply` is never read.
- [ ] 4.3 Extend `ci_burst/aws.py`: an image session from `[image].profile`; builder `RunInstances` from the build template with the image tags and without the managed tag; console read; `CreateImage` with no reboot and tags; version create and read-back; describes for instances, images, snapshots, templates and versions; deregister; snapshot delete; S3 multipart put, abort, list, delete and presign. Add the tag constants. Do not change `owned_instances`.
- [ ] 4.4 Test with fake clients: builders never carry the managed tag, `owned_instances` never returns a builder, no request carries an instance profile, the multipart upload aborts on overrun and failure, `CreateImage` sets no reboot, the presign refuses when the session has less life than `presign_seconds`, and no IAM client is created.
- [ ] 4.5 Test that no log line, event, state value or error text contains the presigned URL, the token or user data, using sentinel values.

## 5. Build pipeline

- [ ] 5.1 Add `ci_burst/image.py`: the basis digest (sorted `path NUL content-hash LF` lines, then the command hash and the script hash), the trigger conditions with the first failing condition named, `--force` semantics and the daily cap.
- [ ] 5.2 Render builder user data: a short stub with quoted exports and a gzip and base64 payload of `warm-cache.sh`. Refuse with `IMAGE_USERDATA_TOO_LARGE` at 16,384 bytes or more. Reject control characters in exported values.
- [ ] 5.3 Test the rendered size with the largest allowed `WARM_COMMANDS` (the 16,384 byte limit), the refusal path, the quoting of hostile values and that the decoded payload equals the script byte for byte.
- [ ] 5.4 Add `WARM_SRC_STRIP_COMPONENTS` to `deploy/runner/image/warm-cache.sh` (0 or 1, default 0, anything else ends the script before any consumer command and without the marker) and test it in `tests/test_warm_cache.py` by copying the pattern of the existing input tests.
- [ ] 5.5 Ship `warm-cache.sh` in the controller container (a COPY in the Dockerfile) and add a test that the shipped copy equals `deploy/runner/image/warm-cache.sh` byte for byte.
- [ ] 5.6 Implement the console marker parser with the existing framed-line discipline: the digest line equal to the staged archive sha256, then the whole line `IMAGE_READY`. `WARM FAIL` and `warm exiting rc=` fail. Test a forged marker in consumer output, a digest mismatch, a blind console and a timeout (`CONSOLE_BLIND` and `MARKER_TIMEOUT`).
- [ ] 5.7 Implement the state machine (`building`, `imaging`, `candidate`, `failed`, `promoted`), the AWS tag precheck, reconcile-first cleanup, `CreateImage` in the pass that first sees the marker, the pass deadline of 240 seconds with a 30 second console poll, builder termination at image `available`, and the `IMAGE_BUILDER_GONE` path. Implement `image run --apply --until-done`: repeat bounded passes with a fixed 30 second sleep until `candidate`, `failed` or the build deadline, resumable by rerunning the command, and refused without `--apply`. Test the slow-tick case: the marker is first seen in the last pass of the snapshot window and the image is still created. Test that the marker arriving after the first pass completes to `candidate` with no timer, and that an interrupted supervisor resumes the same build.
- [ ] 5.8 Implement version creation from the configured pin with a client token, the read-back, the full `LaunchTemplateData` comparison and `TEMPLATE_DRIFT`. Test a clean result, a drift in a non-image field, a retry with the same token after a crash and that the default version is never set.
- [ ] 5.9 Implement failure bounds: the build deadline, the failure cooldown, `blocked` after `max_consecutive_failures`, the minimum interval, the daily cap and their clearing rules. Test the daily cap with `--force`, `blocked` and its two recoveries, and that `status` names them.
- [ ] 5.10 Implement one deletion rule used by retention, failed-build cleanup, orphan cleanup and the pending-image timeout: resolve the protected set (every TOML-referenced version including zone pins, the recorded previous pin, pending journal pins, the current candidate, an active build), verify the ownership tags, delete nothing when the TOML or a version cannot be read, and delete a snapshot only after a confirmed deregistration. Retention adds the minimum age and `retain`. Test an unreadable pin on each path, a protected image, a failed build's image that is pinned by hand, a foreign-tag image, a failed deregister and a dry run.
- [ ] 5.11 Test reconcile: a leaked builder, a stray archive older than one hour, a failed build's image (cleaned only under the deletion rule), an orphan image recorded as an unverified candidate and never pinned, and a lost or restored state file with a running builder.
- [ ] 5.12 Write the events and the Prometheus textfile into the image state directory, atomically. Test the event fields and that they hold no secret.

## 6. Commands and promotion

- [ ] 6.1 In `ci_burst/cli.py`, add `image plan`, `image run [--apply] [--until-done] [--force]`, `image status [--refresh]` and `image promote [--version N | --rollback] [--write]` with JSON on stdout, stable reason codes and the exit codes 0, 1 and 2. Test the `disabled` decision with no network call and that `plan` and `run` without `--apply` write nothing. Implement the split: plain `status` writes only the check timestamp and makes no mutating provider call; `status --refresh` makes describe-only reads and persists only `verified_at`, the image state, template version existence, the drift result and the observed pins. Test both, and the sequence stale record, `status --refresh`, then a successful `promote --write` with no AWS client.
- [ ] 6.2 Implement `promote` without `--write`: the evidence block, the diff of the one pin line, the previous pin and the rollback target. It reads only the state file and the TOML and builds no AWS client (test with a client factory that raises).
- [ ] 6.3 Implement the verification checks for `--write` (image available, version exists, drift-checked, gate passed, `verified_at` within `promote_max_age_seconds`, pin equals the record's source pin, no zone-level version pin). Test one refusal per check, including the foreign version and the stale record.
- [ ] 6.4 Implement the single-key edit: exactly one assignment in `[aws]`, strict re-parse of the new text through the controller loader with a result that differs only in that key, a timestamped `.bak`, a hash check against the bytes read, the journal row committed before the rename, an atomic rename, a directory flush and the completion commit, and journal resolution from the file hash (`PROMOTION_JOURNAL_DIVERGED`) in `promote`, `promote --rollback` and `image run --apply`, with `image status` reporting a pending row without writing. Test: a successful write, failure injection (a crash before the rename, a crash after the rename and before the completion commit, and a hand edit while a row is pending), that both pins stay protected while a row is pending or diverged, a successful write, the sequence successful promotion, then an aborted next promotion (newer backup, crash before the rename), then a rollback that succeeds to the first pin and ignores the aborted row's kept backup, a re-parse failure that leaves the file and backup unchanged, a file changed after the read (`PROMOTE_FILE_CHANGED`), an ambiguous key, comments and other tables preserved byte for byte, and the file mode preserved.
- [ ] 6.5 Implement `PROMOTE_DIRECTORY_NOT_DEDICATED` and test a directory with an env file and a directory with only the TOML and its backups.
- [ ] 6.6 Implement `--rollback` (journaled, after it resolves a pending row) with `PIN_RECORD_DIVERGED` when the TOML pin, the backup named by the newest completed promotion row (never the newest `.bak`, validated by its recorded sha256 and pin) and that row disagree, and the check that the previous version and image are recorded as existing and fresh. Test a clean rollback, each divergence and a missing previous image.
- [ ] 6.7 Test that the image state for the previous pin is recorded at the first promotion (a pin observed by `run` or `status --refresh` before any candidate exists).

## 7. Documentation and verification

- [ ] 7.1 `docs/warm-image.md`: add an automated section (commands, triggers, the candidate and promotion flow, rollback) and keep the manual runbook as the fallback. State that the offline gate proves cache behavior and not image safety. State that the basis digest excludes the cold base image and the build launch template, so after either changes the operator forces a rebuild with `image run --apply --until-done --force`.
- [ ] 7.2 `docs/deployment.md`: add the `[image]` tables with ranges and defaults, the dedicated TOML directory layout that `promote --write` needs, the image role profile and the source identity (placeholders), the dedicated token and the rotation note for the source key.
- [ ] 7.3 `docs/operations.md`: document the commands, the events and metrics, the `blocked` state and its recovery, example alert rules (not shipped), the leaked-builder backstop and the rollback.
- [ ] 7.4 `docs/runner-setup.md`, `docs/container.md` and the README: link to the one owner of each topic and keep them consistent with each other. Add the one-off container lines for a build and for promotion. The promotion line has no network, no credential mount and a dedicated TOML directory.
- [ ] 7.5 `docs/pilot-checklist.md`: add unticked items for section 9. The checklist records preparation, not authorization.
- [ ] 7.6 Do not edit `CHANGELOG.md`. release-please owns it.
- [ ] 7.7 Run `npm run check:spec` and `npm run check:policy` and the repository's unit test command on the change. Run the Gitleaks scan described in `CONTRIBUTING.md` before any push. The change adds no host name, address, account value, bucket name or deployment name.

## 8. Phase 2: timer and trigger

- [ ] 8.1 Add `check_interval_seconds` (900..86400) to the strict `[image]` table and test its bounds.
- [ ] 8.2 Add `deploy/ci-burst-image.service` and `deploy/ci-burst-image.timer`, copied from the apply unit: one-shot, `--read-only`, `--init`, a named container and forced removal in `ExecStopPost`. Mount the configuration read-only, the image state directory writable and the image role's profile directory read-only. Mount no scaling credential and no scaling state. Use a small tmpfs for `/tmp`. Run `image run --apply` about every five minutes.
- [ ] 8.3 Add a unit test that asserts the three mounts, the absence of a scaling mount, the absence of `promote`, `--read-only` and the forced removal.
- [ ] 8.4 Make the refresh script handle the new unit (gate G8) and add the unit names to its list and its tests.
- [ ] 8.5 Add image checks to `scripts/check_prerequisites.py` (profile present, bucket prefix reachable read-only, build template safe, token accepted).
- [ ] 8.6 Document the enabling conditions: one manual build succeeded and about three clean promotions. A clean promotion is one that the operator did not roll back and whose first jobs completed on the warm cache.
- [ ] 8.7 Document that automatic promotion is out of scope and what a later change would need.

## 9. Operator live qualification (not source tasks)

These steps need operator approval, a bounded scope and a cleanup plan. Listing
them authorizes nothing. Record the evidence in the operator's own deployment
records, not in this repository.

- [ ] 9.1 Review the policy file and the credential boundaries with a fresh independent reviewer before anything is applied.
- [ ] 9.2 Apply the IAM yourself: the image role with its trust policy (the one source principal, the external-ID condition, maximum session 1 hour), the source identity with `sts:AssumeRole` on that one role only, and the policy file. The controller applies none of it.
- [ ] 9.3 Run the verification gates with the IAM policy simulator or a real dry run, and record the result: G1 (instance-profile Deny), G2 (build-template Deny with a user-data override), G3 (request tags on volume and network interface), G4 (extra `CreateImage` actions), G5 (pass-role for `CreateLaunchTemplateVersion`; add the one scoped statement only if needed), G9 (SDK profile with `external_id` and `duration_seconds`, refresh inside the container, and whether a source-identity condition is possible), G10 (effective expiry of a presigned URL signed with assumed-role credentials), G12 (a version that names a pending image) and G13 (read-back normalization). Qualify the EC2 and S3 API contracts before enabling `[image]`.
- [ ] 9.4 Create the private bucket prefix (block public access, default encryption, a 7-day lifecycle rule on the prefix), the build launch template (cold base image, no instance profile, IMDSv2 required with hop limit 1, terminate on shutdown, 20 GiB gp3 that deletes on termination), the build security group (no ingress, minimal egress) and the build subnet.
- [ ] 9.5 Create the dedicated read-only Forgejo token (ideally of a dedicated read-only collaborator account) and its own env file, and record the scope.
- [ ] 9.6 Move the deployment TOML into a dedicated directory if `promote --write` is wanted. Add `[image]` with `enabled = false` and run `image plan`. Confirm that it reports `disabled` and makes no network call.
- [ ] 9.7 After the IAM gates in 9.3 have passed, explicitly set `[image].enabled = true` as an approved step. Run `image plan` and confirm: the token reads the branch, one raw file and the archive; the branch is reported protected; the archive prefix and the `export-ignore` effect (G6); the rendered user-data size; the build template is safe; and the policy gates are recorded.
- [ ] 9.8 Run the first manual build: `image run --apply --until-done --force`. Record the console latency and the blind-probe cause (G7), the build duration, whether terminating at `pending` is safe (G11) and the token scope. Confirm that `CreateImage` ran in the pass that saw the marker and that the run needed no timer.
- [ ] 9.9 Review the candidate. Run `image promote` without `--write` and read the evidence. Run `promote --write` in the isolated container, run one canary job, and confirm `--rollback` restores the previous pin. Measure the effect on job duration, as `warm-worker-image` tasks 6.5 and 6.6 already require.
- [ ] 9.10 After the first manual build and about three clean promotions, qualify the timer unit (G8), enable it as a separate approved phase 2 step and observe one timer period without a leak. Do not enable the timer earlier.
- [ ] 9.11 When the cold base image or the build launch template changes, force a rebuild with `image run --apply --until-done --force`, because the basis digest does not cover either.
- [ ] 9.12 Rotate the source key on the schedule that the runbook states, and record the date.
- [ ] 9.13 After the timer period, decide whether to open a separate automatic-promotion change.
