# Design

Status: proposed. Everything below is planned. Nothing is merged, deployed or
runtime-verified. Examples use synthetic names only (`example-org/example-repo`,
`example-ci-burst-staging`, `<account-id>`, `<image-id>`).

## Decision

ci-burst builds the warm worker image itself, in two phases, and never moves the
production pin on its own.

- Phase 1: the operator runs `ci-burst image plan|run|status|promote` by hand.
  `run --apply --until-done` supervises repeated bounded passes until the build
  reaches `candidate` or `failed`, with no timer. `promote --write` is the only
  code path that edits the deployment TOML, and only the operator starts it.
- Phase 2: a systemd timer runs `image run --apply` on a short period. A basis
  digest, not a clock, decides whether a build starts. The timer never promotes.
  It is enabled only after the first manual build succeeded and about three clean
  promotions.
- Automatic promotion is a non-goal and a separate later change.

The design adds no daemon, no webhook listener and no inbound port. It reuses the
one-shot CLI shape, the existing strict TOML loader, the framed console-line
discipline and the apply unit's container pattern.

## Scope change

`openspec/config.yaml` and the `spot-runner-lifecycle` capability say that the
controller creates no runner images. This change replaces that clause with a
bounded permission, and the `config.yaml` context sentence says exactly this.

When `[image].enabled = true`, ci-burst MAY:

- create, tag and delete worker images and their snapshots;
- launch and terminate tagged builder instances, from one named build launch template;
- create new, non-default versions of the worker launch template;
- write and delete objects under one operator-provided private bucket prefix.

ci-burst MUST NOT:

- create or widen IAM, networking, security groups, buckets, key pairs, credentials or launch templates;
- move the production pin (`aws.launch_template_version`) from a timer, or from any process that holds AWS credentials;
- launch an instance outside the build launch template, or attach an instance profile to a builder.

The permission to delete versions in the context sentence is a ceiling, not a
feature. This change never deletes a launch-template version, and its IAM policy
omits that action (see Non-goals in the proposal).

Maintainer sign-off is required in the pull request, because it reverses a stated
product boundary.

## Relationship to open changes

- `warm-worker-image` supplies the script (`warm-cache.sh`), the ready marker and the manual runbook. The script is already in the source tree, but that change is still open, so `openspec/specs/worker-image` does not exist yet. This change therefore adds only new requirements there, and changes none of that change's requirements.
- That change says `Rollout stays the operator's existing flow` and that it does not alter controller behavior or IAM. Those statements are true of that change and of the manual path. The manual runbook stays the fallback when `[image].enabled = false`. When `warm-worker-image` is archived, a follow-up MUST reconcile its rollout requirement with this change. Open question 1.
- `bounded-idle-workers` and `image-tag-retention` touch other requirements. The deltas here modify `Explicitly pinned launch prerequisites` and `Narrow resource ownership` (`spot-runner-lifecycle`) and `Credentials remain in their intended boundary` (`bounded-operation`). Neither open change modifies those three.
- `image-tag-retention` is about container-image tags on the controller host. It is unrelated to AMI retention here.

## Terms

| Term | Meaning |
|---|---|
| Build | One run from a source sha to a candidate image. One row in `image_builds`. |
| Builder | The single EC2 instance, launched from the build launch template, that runs `warm-cache.sh`. |
| Build template | The operator-owned launch template for builders (cold base image, no instance profile, IMDSv2 required). |
| Worker template | The launch template that workers use. Its pinned version is the production pin. |
| Pin | `aws.launch_template_version` in the deployment TOML. The only value that decides which image workers use. |
| Candidate | A built image with a created, drift-checked, inert worker-template version, waiting for a person. |
| Basis digest | A digest over the lockfiles, the warm and gate commands and the shipped script (see Triggers). |
| Marker | The framed console line pair `WARM_SOURCE_SHA256 <digest>` then `IMAGE_READY`. |
| Pass | One invocation of `image run`. It is bounded by a pass deadline. |

## Triggers and the basis digest

1. Manual (phase 1): `image run --apply --until-done [--force]`. A bare `image run --apply` is one pass.
2. Digest-driven (phase 2): the timer runs `image run --apply` about every five minutes. The command makes the source check at most every `check_interval_seconds`. A stale image is not an incident, because a stale image costs time, never correctness (see `warm-worker-image`). There is no urgency trigger.

The command resolves the head sha of `source.branch` and computes:

```
basis_digest = sha256( sorted lines "path NUL sha256(file content) LF"
                       for each basis path at that sha
                     + sha256(rendered warm commands and gate command)
                     + sha256(shipped warm-cache.sh) )
```

The digest does not cover the cold base image or the build launch template. Changing either does not make a rebuild due. The operator forces one with `image run --apply --until-done --force`, and the runbook says so. The digest covers only what the warm layer reads.

The archive hash is not the digest, because archive bytes differ between fetches.
It is used only for transport integrity. The image behind the configured pin
carries its `ci-burst:basis-digest` tag. A build starts when all of these hold:

- the digest differs from that tag (or the pin's image has no such tag);
- `min_build_interval_seconds` (default 24 hours) has passed since the last build started;
- `max_builds_per_day` (default 2) is not reached;
- the failure cooldown is clear and the control row is not `blocked`;
- no builder exists (single-flight).

`--force` skips the digest comparison and the minimum interval only. It never
skips single-flight, the daily cap or the deadline. It clears `blocked` and the
failure cooldown, and nothing else. `image plan` states whether a build would
start and names the first failing condition.

If `[image]` is absent or `enabled = false`, `image run` exits 0 with the
decision `disabled` and calls no network service.

## Identity and credentials

Two identities exist, and neither can act as the other.

| Identity | Holder | Can do |
|---|---|---|
| Scaling identity | `apply`, `reap`, `export` units | Unchanged. Launch, tag and terminate owned workers. |
| Image role | `image` unit and the operator's one-off build container | Build actions on tagged resources and one bucket prefix (see the policy sketch below). |
| Source identity | Same | `sts:AssumeRole` on the image role. Nothing else. |

No helper process is needed. The image container has a shared-config profile:

```
[profile image-build]
role_arn          = arn:aws:iam::<account-id>:role/<image-build-role>
source_profile    = image-build-source
external_id       = <external-id>
duration_seconds  = 3600
role_session_name = ci-burst-image
```

`image-build-source` lives in the credentials file of the same mounted directory.
The SDK assumes the role and refreshes the session by itself. `[image].profile`
names the profile, and the image code opens its own SDK session. It never reuses
the scaling session. Configuration loading refuses an `[image].profile` that equals
`[aws].profile`.

The operator applies the IAM that makes this safe:

- The source identity's policy allows `sts:AssumeRole` on that one role ARN and no other action.
- The role's trust policy names that one source principal and requires the external ID. A source-identity condition is stronger, but whether a profile alone can supply one is not established (gate G9). Do not assert it.
- The role's maximum session duration is 1 hour (the IAM minimum is 1 hour).
- The role has the build permissions and no `iam:*` wildcard and no IAM action except the one optional, condition-scoped `iam:PassRole` statement (gate G5). It has no `ec2:ModifyLaunchTemplate`, no `ec2:DeleteLaunchTemplateVersions`, no `ec2:ModifyImageAttribute` and no `ssm:*`.

The controller code has no IAM client, and no shipped policy contains an `iam:*`
wildcard or any IAM action other than the one optional `iam:PassRole` statement. The repository adds `deploy/iam/image-build-policy.json` (placeholders
only) and a test for these properties.

The long-lived part is now the source key only. The operator rotates it on a
schedule that the runbook states (at least every 90 days) and removes the unit's
mount when image builds are not wanted. A leaked source key can only ask for
short sessions of a role whose trust policy also demands the external ID, which is
a second secret in the same mounted directory. That is a weaker control than an
out-of-band source and the design says so. Nothing automated can change IAM.

### Who can do what

| Action | Timer (image identity) | Operator one-off build container | Operator promote container |
|---|---|---|---|
| Stage archive, launch builder, create image, create template version, prune, clean leaks | yes (phase 2) | yes | no |
| Read the image state file | yes | yes | yes |
| Edit the deployment TOML pin | no | no | yes, with `--write` |
| Apply IAM, create bucket, templates, token | no | no | no |

Nobody automated can change IAM or the pin.

### Policy sketch (placeholders)

The policy file is added by the implementation. It is tag-scoped:

- Describe actions on `*`, and `ec2:GetConsoleOutput` only on instances tagged `ci-burst:image-build=true`.
- `ec2:RunInstances` only with the build template, the base image, the build subnet and the build security group, and creating only instances, volumes and network interfaces whose request tags are `ci-burst:image-build=true`, the deployment tag and the allowed tag keys.
- An explicit Deny on `ec2:RunInstances` when `ec2:InstanceProfile` is not null, and an explicit Deny on instances not launched from the build template.
- `ec2:CreateTags` only on creation (`RunInstances`, `CreateImage`) with allowed keys.
- `ec2:CreateImage` from tagged builders. `ec2:TerminateInstances` on instances with both tags.
- `ec2:CreateLaunchTemplateVersion` on the one worker template.
- `ec2:DeregisterImage` and `ec2:DeleteSnapshot` on images and snapshots tagged for this deployment.
- S3 list on the bucket restricted to the prefix, and put, get and delete on the prefix.
- `iam:PassRole` only if gate G5 shows `CreateLaunchTemplateVersion` needs it, then one statement for the one worker role with `iam:PassedToService = ec2.amazonaws.com`. The operator applies it.

Every statement is a gate item. IAM cannot restrict the content of a
launch-template version. A leaked session could create an arbitrary inert
version. It runs only if a person pins it, and the post-create comparison and the
manual pin are the controls.

## Source access

- A dedicated read-only Forgejo token, ideally of a dedicated account that is a read-only collaborator on the one repository. It lives in its own env file (default name `forgejo-image.env`, named by `source.token_env`). It is never the token that `apply` uses, and the code has no fallback to it.
- `image plan` reads the branch, one raw file and the archive endpoint. A 401 or 403 refuses with `IMAGE_SOURCE_FORBIDDEN` before any AWS call. The operator learns this before enabling.
- The operator runbook records the scope that the token was created with. The minimum scope is established by `image plan` (gate G6). Do not invent the scope name in the product.
- The branch must be protected. The branch response from the forge reports a `protected` flag. The run refuses with `IMAGE_BRANCH_UNPROTECTED` when the flag is absent or false, and has no override. Evidence gap G6: whether the read-only token can see the flag. "Protected" is the forge's statement. ci-burst does not know that review was required. The sha is also never a pull-request ref.

## Source archive and user data

1. Resolve the sha with the branch endpoint. Everything below uses that one sha.
2. Stream the archive endpoint for that sha through a hashing wrapper into a multipart upload at `s3://<bucket>/<prefix>/<build-id>.tar.gz`, with the bucket's default encryption. The prefix and the build id are joined by exactly one `/`, whether the configured prefix ends with no slash, one or several. The listing of stray archives in reconcile uses the same slash-ended prefix, so it never reaches a sibling such as `<prefix>-other/`. `source.max_archive_bytes` aborts the upload and deletes the object on overrun. The archive is never written to disk.
3. Archive layout: a forge archive has one top-level directory. `warm-cache.sh` gains one optional input `WARM_SRC_STRIP_COMPONENTS` (0 or 1, default 0, anything else refused before any consumer command), which is passed to `tar`. Gate G6 confirms the prefix and whether `export-ignore` removes files that the warm commands need.
4. Presign a GET URL locally. The URL appears only in the rendered user data in memory. It is never logged, never stored in the state file and never in an event.
5. The presigned URL dies with the role session, whichever comes first. With a 1-hour session and the default `presign_seconds = 1800` the design relies on a fresh session per pass: each pass is a new process that assumes the role anew and presigns at once before the launch. The `--until-done` supervisor creates a fresh SDK session (it re-reads the assumed-role profile) for every pass, so no pass runs on an expiring session. The code MUST NOT presign when the session has less than `presign_seconds` left, and MUST refuse with `IMAGE_CREDENTIAL_SHORT`. The check, like the single-flight check, runs before the build row is written and again once the archive is staged, because either can change in between: the session ages during an upload and another builder can appear. A refusal before the row writes no row and no archive. A refusal after it ends the row as `failed` with the refusal code, deletes the archive and counts no failure (a refused start is not a failed build: see Failure behavior). Gate G10 measures the real expiry of a URL signed with assumed-role credentials.
6. User data size: `warm-cache.sh` is about 15.6 KB, and its gzip and base64 payload about 8.2 KB, against the 16,384 byte limit for EC2 user data (before base64). The rendered user data is a short `#!/bin/bash` stub with quoted `WARM_*` exports and a gzip and base64 payload of the script, which the stub decodes and runs. A test renders the largest allowed `WARM_COMMANDS` and asserts the size is below 16,384 bytes. A larger rendering is refused before `RunInstances` with `IMAGE_USERDATA_TOO_LARGE`. This is decided now, not at runtime.
7. The controller container copies `warm-cache.sh`, because the image today holds only the Python package. A test asserts that the copy in the build context equals `deploy/runner/image/warm-cache.sh`, byte for byte.
8. Every exported value comes from configuration, never from the archive. Values are shell-quoted and refused when they contain control characters.
9. The archive object is deleted at the terminal state of the build. The bucket's 7-day lifecycle rule is a backstop only. The image role can read the prefix, so a leaked session could read an archive while it exists. That is accepted and bounded by deletion at terminal state, 1-hour sessions and source-key rotation.

## The builder

- `RunInstances` from the build launch template at a pinned version, count 1, `ClientToken` taken from the state file. The state row is written before the call. On-demand only.
- The build launch template is operator-owned: a cold base image (a build never chains from the warm image), no instance profile, IMDSv2 required with hop limit 1, instance-initiated shutdown set to terminate, 20 GiB gp3 storage that deletes on termination, and a build security group with no ingress and minimal egress. `plan` and a `run` pass that is about to start a build read the template and the security groups it names (`SecurityGroupIds`, `SecurityGroups` and the `Groups` of each network interface, through `DescribeSecurityGroups`), and refuse with `IMAGE_BUILD_TEMPLATE_UNSAFE` when any property differs. The detail names the failing properties and never a value. For the groups the property is `security_groups`. It fails when no group is named, when a network interface names none (a launch would give it the default group of the VPC, which the check never reads), when a named group name matches no group (a name matches the group of that name in every VPC of the Region, and each match counts), and when any matched group has an inbound rule. A group id that does not exist, or a role without `ec2:DescribeSecurityGroups`, makes the describe fail: the pass then refuses with the provider's refusal and starts nothing. A pass that only advances or ends a build does not run the check and does not read the groups. A pass that stages a build again, after the pass deadline ended its first stage, runs the check again before it uploads anything, because the rules of the groups can change between the two passes (see Supervising a manual build).
- Builders carry `ci-burst:image-build=true`, the deployment tag, `ci-burst:build-id` and `ci-burst:expires-at`. They never carry `ci-burst:managed=true`. `Aws.owned_instances` selects on that tag, so a managed builder would count against `controller.max_instances` and be reaped by `reap`. A test asserts that `owned_instances` never returns a builder.
- The builder runs consumer code at the protected-branch sha, with the archive available to it for the whole window. That is accepted: the consumer already holds the same source. The builder has no credential of its own. The presigned URL in user data is the only secret on it. Before any fetch or consumer command, `warm-cache.sh` adds firewall rules that reject the IPv4 and IPv6 metadata addresses for the `runner` account and checks each rule, so consumer commands cannot read the user data; a rule that is not in place ends the build. Firewall rules, unlike the DHCP-managed metadata route, hold for the whole window. A failure tail masks the URL and its query.
- The script arms its own lifetime timer first. `terminate` on shutdown plus that timer is the leak backstop that does not depend on the controller image being current.

## Observing success

- `warm-cache.sh` prints `WARM_SOURCE_SHA256 <digest>` and then `IMAGE_READY` only after the warm commands, the offline gate and the clean-up passed. The existing framed-line discipline keeps consumer output from forging them.
- Success needs the marker digest to equal the archive sha256 held in the state file, then a whole line `IMAGE_READY`. `WARM FAIL` or `warm exiting rc=` means failure.
- `gate_command` MUST be non-empty unless `allow_no_gate = true`, otherwise `IMAGE_GATE_REQUIRED`. Without a gate the marker would not prove cache behavior.
- No marker by `build_deadline_seconds` terminates the builder and fails the build, with `CONSOLE_BLIND` (console empty or unreadable) or `MARKER_TIMEOUT` (readable, no marker). Success is never guessed.
- What the gate proves: the warm layer is sufficient with downloads forbidden. It proves cache behavior. It does not prove that the image is safe, because the builder ran consumer code at a branch head. Promotion stays with a person for that reason.

## CreateImage in the same pass

When a pass first sees the marker it calls `CreateImage` in that pass, before it does any step that is not a read. The reads are the lookup of the build's image and the read of the base image. `NoReboot` is true: a reboot would rerun user data after `cloud-init clean`, and the manual path used no-reboot. The builder-side snapshot window (`WARM_SNAPSHOT_SECONDS`, default 1800 in the script) is longer than the timer period, so a slow tick leaves a good build with an image. The pass polls the console every 30 seconds inside a pass deadline of 240 seconds. Waiting for the image to become `available` and every later step continue in later passes.

The call is never made at or after the build deadline. The reads before it take time, so the pass reads the clock again right before the call, and a build that is out of time at that point fails `MARKER_TIMEOUT` and has its live builder ended.

### A CreateImage call with an unknown outcome

A call can make the image and lose the provider's answer. Its outcome is then unknown, and three rules keep that from costing a second image or a failed build.

- The name is the build's: `ci-burst-<deployment>-<build id>`, the same on every call. EC2 refuses a second image under a name that is taken, with `InvalidAMIName.Duplicate`. The provider layer maps every code that is not a definite launch error to an unknown outcome (`AWS_UNCERTAIN`), so this refusal is an unknown outcome and is never a definite one. The pass never changes the name to get past it: an image under another name is the duplicate that the name prevents.
- Before it calls, the pass lists the images that carry the build's id. An image that is listed is adopted: the row records its id and moves to `imaging`, the `image_created` event is written and the pass makes no call. Adopting is no call, so it holds even when the build deadline passed during the lookup: the image exists, and the build goes on. Reconcile adopts such an image too, at the start of the next pass, by the same tags, even when that pass runs after the build deadline: it ends the overdue builder first, and the image came from a call made before the deadline. It records the id and the state and writes no `image_created` event. A lookup that the provider refuses, the pass deadline included, does not stop the call: the pass notes `image_lookup:<reason>` and goes on as if it had found no image. That is safe because of the name.
- Before the build deadline, an unknown outcome leaves the row `building`, notes `create_image:<reason>` and counts no failure. The next pass adopts the image when it is listed, and else asks again under the same name, which the provider refuses while the image exists. At or after the build deadline the build fails: with the code of the refusal when the call itself outlived the deadline, and with `IMAGE_BUILDER_GONE` when reconcile ended the builder first and listed no image of the build. An image that is listed after that has a row and carries the failed build's id, so it is no orphan. The deletion rule removes it and its snapshots with no age wait: the `failed` path takes an image that has settled, and the `pending_timeout` path takes one that is still `pending`. The leak count stays at zero.

### Supervising a manual build

A pass is bounded to 240 seconds, and a build takes 10 to 15 minutes, so one pass cannot finish a build. In phase 1 the operator runs `image run --apply --until-done`. The supervisor repeats bounded passes with a fixed 30-second sleep between them until the build is `candidate` or `failed`, or the build deadline passes. Each pass reconciles first and resumes from the state file, so killing the supervisor loses nothing: rerunning the same command resumes the same build, and the single-flight rules refuse a second builder. Without `--until-done`, `run --apply` is one pass, which is what the phase 2 timer runs. The supervisor is a loop in the CLI, not a daemon, and does not outlive the command.

Every provider read of a pass takes the pass deadline, the image read and the template read-back included, and a read that would start at or after the deadline is not made. What follows depends on where it happened. An advancing or staging pass notes `pass_deadline_exceeded` and leaves the row as its last write left it. A deletion path, and the deletion dry run of `plan`, delete nothing and report `pass_deadline_exceeded` as their result. The other reads of `plan`, reconcile, the pin read and the template check of a start end with the refusal. One read escapes the rule: the read that confirms a deregistration takes no pass deadline. It is one call for each image, and the SDK's connect and read timeouts bound it. A confirmation that the deadline refused would leave the deregistration unconfirmed and keep the snapshots. The lookup of the build's image and the read of the base image before `CreateImage` take the pass deadline. A refusal of either, the deadline included, never delays `CreateImage`. A refused base image read notes `base_image:<reason>` (`base_image:pass_deadline_exceeded` for the deadline) and the image is created without the base image tag. A refused lookup notes `image_lookup:<reason>` and the call is made as if the lookup had found no image.

Staging has the same bound. A pass deadline in the upload, or after it and before the launch call, leaves the row `building` with no builder and no `archive_sha256`, and counts no failure. The next pass stages again under the same key, which replaces whatever the first stage left. Before it uploads anything, that pass runs the template check of a start again, because the rules of the build security groups can change between the two passes. An unsafe template ends the row `failed` with `IMAGE_BUILD_TEMPLATE_UNSAFE`, deletes the archive that the row names and answers `refused` with no failure counted, as any refusal of a start does once the row exists. A pass deadline in the check leaves the row as it was and notes `pass_deadline_exceeded`. A read that the provider refuses ends the pass with that refusal and leaves the row for the next pass. Past the build deadline a build that is still unstaged fails with `CONSOLE_BLIND`, counts as a failure, and the archive that its row names is deleted. The pass checks that deadline when the stage begins, and again right before the launch call, because the upload and the reads in between take time. A stage that crosses the deadline in between launches no builder: the build fails with `CONSOLE_BLIND` and the archive is deleted. The check sits in the launch that every stage uses, so it covers the first stage of a build too. The first stage would reach it only if the one read before the launch call lasted until the build deadline, at least 900 seconds after the build started. The pass deadline stops no read that has begun, but the SDK's connect and read timeouts bound that read to well under 900 seconds. So in practice only a resumed stage reaches the check.

The builder is terminated when the image is `available`, or when the snapshot window or the lifetime timer ends it. Whether the controller may terminate it earlier, while the image is still `pending`, is gate G11, and until the gate is met the controller waits for `available`. The build deadline is the one limit that does not wait. As the first call of a pass, reconcile ends the builder of an active build that is past its deadline, an `imaging` build whose image is still `pending` included. That mirrors the builder's own lifetime timer, so it is not a decision about G11, and the builder is not counted as a leak. The build then ends `IMAGE_NOT_AVAILABLE`, or `candidate` when the image is `available` when the pass reads it. A marker that a pass reads for the first time at or after the build deadline never leads to `CreateImage`. The pass takes that time from the clock after its console read, because the reads take time, and the latency of the marker is that time too. The build fails with `IMAGE_BUILDER_GONE` when reconcile ended the builder, and with `MARKER_TIMEOUT` when the builder is still alive, for example because reconcile's termination was refused or because the deadline came during the pass: the failure path then asks to end it.

## State

The build state lives in `<state dir>/image-state.sqlite`, with its own lock and its own schema version. The state directory is a different directory from the scaling ledger's. The image unit does not mount the scaling directory. A controller rollback or a defect in the image code therefore cannot make `apply` refuse its ledger. If the file is lost, the builds are rebuilt from AWS tags during reconcile (see below), and a lost row never allows a second builder.

Tables:

- `image_builds`: id, deployment, client_token, basis_digest, source_sha, archive_key, archive_sha256, builder_id, image_id, template_version, state, reason_code, created_at, updated_at, deadline_at, and the verification record (gate seen, drift result, `verified_at`, `source_pin`).
- `pin_log`: one row per observed pin: pin version, image id, observed_at.
- `promotion_journal`: one row per promotion or rollback: previous pin, new pin, previous and new file sha256, candidate id, kind (`promotion` or `rollback`), backup file name, the sha256 of that backup, the pin that the backup contains, status (`pending`, `complete` or `aborted`) and timestamps.
- `control`: one row with the consecutive failure count (a refused start is not a failure and never moves it), the failure cooldown end, the `blocked` flag and the last check time.

States: `building`, `imaging`, `candidate`, `failed`, `promoted`. No more.

Single-flight has two layers:

1. A unique index keyed on the deployment over the active states, for example `CREATE UNIQUE INDEX one_active ON image_builds(deployment) WHERE state IN ('building','imaging')`. The index is not on `id`, which is already unique and enforces nothing here. A test inserts two active rows and expects the second to fail.
2. Before a launch, `DescribeInstances` for the image-build tag and the deployment tag in the states pending, running, stopping or stopped MUST return nothing. A lost or restored state file cannot produce two builders.

Reconcile runs first in every pass, before any transition:

- as the first call of the pass, end the builder of an active build that is past its build deadline, an `imaging` build whose image is still `pending` included. This mirrors the builder's own lifetime timer, is not counted as a leak, and the state machine then ends the build. A refused termination is reported and the pass goes on, and the state machine asks again when it ends the build;
- terminate every other builder-tagged instance, that is, every one that is not the active build's builder;
- delete objects under the prefix that are not the active archive and are older than 1 hour;
- clean the images and snapshots of failed builds before any start, but only under the image deletion rule (see Retention). This is the rule's `failed` path and its result is `reconcile.failed_images`. The end of the pass runs every path of the rule and reports it as `retention`, so the image of a failed build that is still `pending` goes at the end of the pass by the pending-image path;
- resolve a pending promotion journal row from the file hash (see `image promote`);
- record a tagged image with no state row as a `candidate` that is not verified, in whatever state the image is, and never pin it. An orphan that is not `available` and is older than the 24-hour hold is marked `failed`, so that the rule removes it as the image of a failed build. Its row keeps `IMAGE_ORPHAN_RECORDED`: it never starts a cooldown, changes the failure count or clears `blocked`, and it counts toward `max_builds_per_day` and `min_build_interval_seconds` by the creation time of its image.

## Launch-template version and promotion

### Version creation (inert)

After the image is `available`, the pass calls `CreateLaunchTemplateVersion` with `SourceVersion` equal to the configured pin (never `$Latest`, never the default), `LaunchTemplateData` of `{ImageId: <new image>}`, a description holding the build id and a short digest, and a `ClientToken` from the state file. It then reads back both versions and compares the full `LaunchTemplateData`. Any difference other than `ImageId` yields `TEMPLATE_DRIFT`: the version is recorded as unusable, an event is emitted, and nothing is promoted. IAM cannot limit version content, so this comparison is the control. A hand-edited pin bypasses it by design.

The version is inert. The scaling identity launches only the pinned version number, and the image role has no `ec2:ModifyLaunchTemplate`, so the default version cannot move.

The verified record holds the version, the image id, the archive sha, the script sha, the gate result, the pin that the version was derived from, and `verified_at`.

### `image promote`

`promote` runs as an operator-invoked one-off. It needs no AWS credentials and makes no AWS call: its verification comes from the image state record. `image run --apply` and `image status --refresh` (a describe-only pass with the image role) stamp `verified_at` and the pin observation. The promote container does not mount the role's credential directory, the forge token file or any scaling credential.

Without `--write`, `promote` prints the evidence block (build id, source sha, basis digest, script sha, gate configured and passed, build duration, verified_at), the diff of the one pin line, the previous pin and the rollback target. It exits 0.

With `--write` it refuses unless the candidate is verified:

- the image is recorded `available`;
- the template version is recorded as existing;
- the full `LaunchTemplateData` differs from the pin that the version was derived from only in `ImageId`;
- the offline gate was configured and passed (or `allow_no_gate` was set and the record says so);
- `verified_at` is within `promote_max_age_seconds` (default 172800);
- the pin in the TOML now equals the record's `source_pin`, so the diff basis is still true;
- no `[[aws.availability_zones]]` entry carries its own `launch_template_version` (see open question 3).

It then edits only the `launch_template_version` key of the `[aws]` table:

1. Read the file bytes and keep their sha256.
2. Find exactly one assignment of that key in the `[aws]` table. Zero or more than one refuses with `PROMOTE_PIN_LINE_AMBIGUOUS`.
3. Build the new text with that one value replaced. Re-parse it with the controller's own strict loader. The parsed result MUST equal the original parse except that one key. A failed parse leaves the file unchanged.
4. Write a temporary file in the same directory with the same mode, flush it and copy the original to `<file>.bak.<UTC timestamp>`.
5. Re-read the target and compare its sha256 with step 1. A difference refuses with `PROMOTE_FILE_CHANGED`. A narrow race between this check and the rename remains. The file has one human editor, so it is accepted.
6. Commit a `pending` row to `promotion_journal` (previous pin, new pin, previous and new file sha256, candidate id, backup name, backup sha256 and the pin the backup contains). The row is durable before the rename.
7. Rename atomically and flush the directory.
8. Mark the row `complete` and commit.

The order is journal, rename, complete, so a crash never leaves a new pin without a record. The next `promote`, `promote --rollback` or `image run --apply` resolves any `pending` row from the sha256 of the file:

| File hash | Resolution |
|---|---|
| the recorded old hash | the rename did not happen: mark the row `aborted` |
| the recorded new hash | the rename happened: mark the row `complete` |
| any other hash | refuse with `PROMOTION_JOURNAL_DIVERGED`, write nothing, and leave the row `pending` |

`image status` reports a pending row and the resolution that the hash implies, but writes nothing (plain status writes only the check timestamp). While a row is `pending`, both of its pins are in the protected set, so no deletion path can remove either image. A diverged row stays until an operator resolves it by hand, for example by restoring the file from the backup, and then reruns a resolving command.

`--rollback` writes the recorded previous pin with the same journaled procedure, after it resolves a pending row. It uses the backup that the newest completed `promotion` row names, never the globally newest `.bak` file. It refuses with `PIN_RECORD_DIVERGED` unless that row's new pin equals the TOML's pin, the named backup exists with the recorded sha256, and it contains the recorded previous pin. It also refuses unless the previous version and its image are recorded as existing at a fresh `verified_at`. Retention protects the previous pin's image, so a rollback needs no rebuild. The TOML owns the current pin and the journal owns the previous pin. Rollback verifies both against the named backup, so they cannot diverge silently.

The backup of an `aborted` row is kept and never used for rollback. Only the operator removes it, and `promote` never deletes a backup. Example: A to B completes (its backup holds A). B to C writes a newer backup holding B, commits `pending` and crashes before the rename. Recovery marks B to C `aborted`. A rollback still uses the A to B row, validates its backup and restores A. A second rollback after a completed rollback refuses with `PIN_RECORD_DIVERGED`, because the newest completed promotion's new pin no longer equals the TOML's pin.

Promotion needs no restart: the apply unit reads the TOML on each pass, and a worker keeps its image until it expires. The shipped apply unit mounts the file read-only, once per run.

### The promote container's mounts

An atomic rename needs write access to the directory of the TOML. Today the TOML sits in a directory that also holds the credential directories and the token env files. Mounting that directory writable into the promote container would defeat "no AWS credentials". The rule:

- `promote --write` refuses with `PROMOTE_DIRECTORY_NOT_DEDICATED` when its target directory holds anything other than the TOML, its `.bak.*` files and a lock file.
- The operator therefore keeps the deployment TOML in a dedicated directory (documented in the deployment guide) before using `--write`. Without it, `promote` without `--write` still works and the operator edits the pin by hand.
- The container runs with `--network none`, read-only root, the dedicated TOML directory writable and the image state directory writable. Whether the shipped apply, export and reap units should move to the dedicated directory in this change is open question 2.

### Canary

The first real jobs under the new pin are the canary. The operator watches them and rolls back by one command. This change builds no synthetic canary job. Whether worker events can name the template version is open question 4.

### Automatic promotion

Out of scope. A later change would need explicit gates (the marker, a clean canary window read from real evidence, no launch-failure spike, a recorded rollback target and a cap per week). Default stays manual.

## Retention

One deletion rule covers every path that deregisters an image or deletes a snapshot: retention, failed-build cleanup in reconcile, orphan cleanup and the pending-image timeout. No path has its own rule.

- Resolve the protected set first: the images of every launch-template version that the TOML references (the `[aws]` pin and every `[[aws.availability_zones]]` pin), the recorded previous pin, the pins of any pending or diverged promotion journal row, the current candidate (unless its row records `TEMPLATE_DRIFT`) and an active build. With the defaults that is at most three images (two when the candidate has drifted, since its image is then removed after the 24-hour hold), and `retain` (default 3, range 2..6) never goes below that. A failed build's image that an operator has pinned by hand is protected like any other.
- Never deregister a protected image.
- Verify the ownership tags (image-build, deployment and the build id) before deleting. An image or snapshot without them is left alone.
- Fail closed: if the TOML or any launch-template version cannot be read, delete nothing.
- Deregister first. Delete a snapshot only after the deregistration is confirmed, and only if it carries the same build id.
- Retention adds one more condition: only images older than `retain_min_age_seconds` are eligible, down to `retain` images.
- Older launch-template versions that reference deregistered images become unlaunchable. They are never deleted here. The previous pin stays valid because it is protected.
- Retention runs inside `image run`. It only reports what it would remove unless `--apply` is given.
- Orphans (tagged images or snapshots with no state row, older than 24 hours) are handled after that grace period. A pass records a tagged image that has no row before the rule runs (see Reconcile), so an orphan image that is not `available` reaches the rule as the image of a failed build.

## Configuration

Optional `[image]` tables. Unknown keys and out-of-range values are refused by the existing strict loader pattern.

```toml
[image]
enabled = false
profile = "image-build"
min_build_interval_seconds = 86400    # 3600..604800
max_builds_per_day = 2                # 1..6
build_deadline_seconds = 2700         # 900..14400
failure_cooldown_seconds = 21600      # 600..172800
max_consecutive_failures = 3          # 1..10
retain = 3                            # 2..6
retain_min_age_seconds = 86400        # 3600..2592000
promote_max_age_seconds = 172800      # 3600..2592000
# check_interval_seconds = 21600      # 900..86400, phase 2 only

[image.source]
branch = "main"
token_env = "CI_BURST_FORGEJO_IMAGE_TOKEN"
basis_paths = ["uv.lock", "package-lock.json"]   # 1..32; no "..", no leading "/"
max_archive_bytes = 268435456
presign_seconds = 1800                # 300..3600
strip_components = 1                  # 0 or 1

[image.warm]
commands = ["bash scripts/warm.sh"]
gate_command = "bash scripts/warm-gate.sh"
allow_no_gate = false
lifetime_seconds = 3600
snapshot_seconds = 1800

[image.build]
launch_template_name = "example-ci-burst-image-builder"
launch_template_version = 1

[image.storage]
bucket = "example-ci-burst-staging"
prefix = "warm-archives"              # no "..", no leading "/"; one "/" joins it to the build id
```

The `[aws]` and `[forgejo]` tables are unchanged. Region, deployment identity and the worker template come from the existing keys.

## Command line

JSON on stdout with stable reason codes and no secrets. Exit codes follow the existing contract: 0 completed decision, 2 refusal, 1 provider failure.

- `image plan --config C`: read-only. Resolves the sha, computes the digest, tests source access, reads the build template, shows the script sha and says whether a build would start and why not.
- `image run --config C [--apply] [--until-done] [--force]`: without `--apply` it is `plan`. With `--apply` it runs one bounded pass. With `--until-done` (which needs `--apply`) it supervises repeated passes to `candidate`, `failed` or the build deadline.
- `image status --config C [--refresh]`: the active or last build, the candidate, the pin, the rollback target, the retention set, the counters, a pending promotion journal row and the `blocked` flag with how to clear it. Plain `status` makes no provider call that mutates and writes only the check timestamp. `--refresh` makes describe-only provider reads with the image role and persists only `verified_at`, the image state, whether the template version exists, the drift result and the observed pins. Neither form mutates a provider resource.
- `image promote --config C [--version N | --rollback] [--write]`: as above.

The `blocked` state clears only with a basis digest change or `image run --apply --force`. `status` prints that.

## Observability

The image unit writes an append-only `image-events.ndjson` and a Prometheus textfile into the image state directory, written atomically. Events: `image_check`, `image_build_started`, `image_marker_seen`, `image_created`, `image_candidate_ready`, `image_build_failed` (reason code and a framed, byte-sanitized console tail), `image_reconciled`, `image_pruned`, `image_blocked` and `image_promoted` (written by promote). Metrics: build state, last build timestamp by outcome, build duration, console latency, consecutive failures, candidate waiting, stale, and leaked resources by kind.

Events carry ids, 12-character digests, counts and reason codes. The presigned URL, tokens, user data and raw provider payloads never appear. No event value holds `://`. The console tail of `image_build_failed` shows every URL, every presigned query string and the archive key as `[archive-url]`, the text that `warm-cache.sh` prints for the archive URL (the script masks only that URL and its query, the pass masks every URL it finds). An event line is written with one `write` on an `O_APPEND` descriptor, so it lands whole behind any other writer's line. A short write is raised as an error and is never continued or retried, because a second write could land behind another writer's line and tear both. Example alert rules go in the documentation and are not shipped: failures at 2 or more or `blocked`; a candidate waiting over 48 hours; leaked resources increasing; stale over 7 days (information). A failed unit also reaches the host's existing unit-failed alerts. How the host's collector reads the textfile is operator-specific.

## Credential boundaries

- The image unit mounts the image role's profile directory, the image token env file, the config read-only and its own state directory. It does not mount the scaling credentials, the scaling state directory or the token that `apply` uses.
- The apply, reap and export units gain no new mount. They never hold image credentials.
- The promote container holds no AWS credential, no forge token and no network.
- Residual risk: a leaked image-role session can call `DeregisterImage` or `DeleteSnapshot` on any image tagged for this deployment, including the pinned one, and so bypass the application's deletion rule. A Deny for pinned images is not possible, because the pin moves and an IAM condition cannot follow it. The mitigations are 1-hour sessions, the external ID, a source identity that exists only on the controller host and that can only assume the role, source-key rotation and CloudTrail records of the calls. Recovery is to rebuild from the protected branch or to roll the pin back to an image that still exists.
- Builders hold no credential. The presigned URL is bearer access to one archive for at most `presign_seconds`.
- Logs, events and the state file hold no URL, token, user data or raw provider payload.

## What this does not claim

- The warm gate proves cache behavior, not integrity.
- A cost figure here is an estimate of one small instance for about 10 to 15 minutes, 20 GiB of gp3 and one snapshot per retained image. It is not measured and not an AWS billing cap. The daily cap and the retention bound the number of builds and images.
- `protected` is the forge's flag, not proof of review.
- The offline gate and the marker are builder evidence, not a canary.
- Nothing in this change is runtime-verified.

## Failure behavior

| Failure | Behavior |
|---|---|
| Forge returns 401 or 403 | `IMAGE_SOURCE_FORBIDDEN`, exit 2, no AWS call |
| Branch not protected | `IMAGE_BRANCH_UNPROTECTED`, exit 2 |
| Basis path missing at the sha | `IMAGE_BASIS_PATH_MISSING`, exit 2 |
| Archive over the size cap, or the upload fails | Abort the multipart upload, delete the key, build `failed`, no builder |
| Session has too little life for the presigned URL | `IMAGE_CREDENTIAL_SHORT`, answered `refused` (exit 2), no launch, no failure counted. Before the row exists no row and no archive is written. After the archive is staged the row ends `failed` with the code and the archive is deleted |
| Role cannot be assumed | Provider refusal on the first call, exit 1, scaling unaffected |
| User data too large | `IMAGE_USERDATA_TOO_LARGE`, before `RunInstances` |
| Build template unsafe | `IMAGE_BUILD_TEMPLATE_UNSAFE`, exit 2, before the row is written: no row, no archive, no failure. When a pass resumes the stage of a build that already has a row, the row ends `failed` with the code, its archive is deleted, the pass answers `refused` (exit 2) and no failure is counted. The detail names the property (`security_groups` for the groups) and no value |
| Security groups cannot be described (unknown id, or no `ec2:DescribeSecurityGroups`) | The provider's refusal before the row is written, no build starts |
| Start refused after the trigger decision cleared `blocked` or the cooldown (a forced run, a changed digest) | The clearing stays. The refusal adds no failure and starts no cooldown |
| No gate command and no `allow_no_gate` | `IMAGE_GATE_REQUIRED`, exit 2 |
| Another builder exists | `IMAGE_BUILD_ALREADY_ACTIVE`, answered `refused` (exit 2), no failure counted. A builder that appears after the archive is staged ends the row `failed` with the code and deletes the archive, and the next pass's reconcile ends that builder |
| Daily cap reached | `IMAGE_DAILY_CAP`, exit 2 |
| `RunInstances` fails | Build `failed`, counts toward cooldown and `blocked` |
| Warm command or gate fails | `WARM_FAILED`, builder terminated, no image |
| Console blind, or no marker | `CONSOLE_BLIND` or `MARKER_TIMEOUT`, builder terminated, no image |
| Marker digest differs from the staged archive | `IMAGE_MARKER_DIGEST_MISMATCH`, no image |
| Builder gone before `CreateImage` | `IMAGE_BUILDER_GONE`, cooldown. A marker that a pass reads for the first time after reconcile ended an overdue builder is this case |
| Marker first read at or after the build deadline, builder still alive | `MARKER_TIMEOUT`, no image, and the failure path asks to end the builder. A refused termination in reconcile is one way to get here. A deadline that passes during the console read, during a lookup of the build's image that lists no image, or during the read of the base image is another |
| Image fails or stays pending | Poll to the build deadline. Reconcile ends an overdue builder first, and the build ends `IMAGE_NOT_AVAILABLE` (or `candidate` when the image is `available` at the read). The image is cleaned up only under the deletion rule |
| Pass deadline reached | An advancing or staging pass notes `pass_deadline_exceeded` and leaves the row as it was. A deletion path, and the deletion dry run of `plan`, delete nothing and report it as their result. The other reads of `plan`, reconcile, the pin read and the template check of a start end with the refusal |
| Base image read refused, the pass deadline included | `CreateImage` is still called, without the base image tag, and the pass notes `base_image:<reason>` |
| Lookup of the build's image refused, the pass deadline included | `CreateImage` is still called, as if the lookup had found no image, and the pass notes `image_lookup:<reason>` |
| `CreateImage` ends with an unknown outcome before the build deadline, a duplicate name included | The row stays `building`, the pass notes `create_image:<reason>` and no failure is counted. The next pass adopts the image by its build id tag once it is listed, and else asks again under the same name |
| `CreateImage` ends with an unknown outcome at or after the build deadline | The build fails: with the refusal's code when the call outlived the deadline, with `IMAGE_BUILDER_GONE` when reconcile ended the builder first. An image that is listed later is removed as the image of a failed build and is no orphan |
| Pass deadline reached during staging | The row stays `building` with no builder and no `archive_sha256`, no failure. The same holds for a pass deadline in the template check that comes before a resumed stage. The next pass stages again under the same key |
| Build still unstaged at the build deadline, or the deadline passes during the stage | `CONSOLE_BLIND`, no builder is launched, the archive that the row names is deleted, counts as a failure. The clock is read again right before the launch call |
| New version differs beyond `ImageId` | `TEMPLATE_DRIFT`, version unusable, image kept 24 hours |
| Version creation fails | Retry with the same client token, no rebuild |
| Crash after a state write | Idempotent retry. Reconcile terminates an unmatched builder. |
| State file lost or restored | Reconcile from tags. The tag precheck blocks a second builder. |
| Image credentials absent or expired | Provider refusal. Scaling unaffected. |
| Image credentials leaked | Bounded to tagged resources and one prefix. Cannot touch IAM, networking, the pin or other instances. |
| Promote: candidate unverified or stale | Refusal with the missing item. File unchanged. |
| Promote: file changed since read | `PROMOTE_FILE_CHANGED`. File unchanged. |
| Promote: strict re-parse fails | Refusal. File and backup unchanged. |
| Promote: directory not dedicated | `PROMOTE_DIRECTORY_NOT_DEDICATED`. File unchanged. |
| Promote: crash before the rename | The journal row is `pending` with the old file hash. The next resolving command marks it `aborted`. |
| Promote: crash after the rename | The journal row is `pending` and the file has the new hash. The next resolving command marks it `complete`. |
| Promote: file edited by hand while a row is pending | `PROMOTION_JOURNAL_DIVERGED`. Nothing is written, both pins stay protected. |
| Deletion path cannot read the TOML or a version | Delete nothing, report the refusal. |
| Failed build's image is pinned by hand | Skipped as protected. |
| Supervisor interrupted | Rerun the same command to resume. |
| Promoted image bad | `promote --rollback`. No rebuild. |
| Image pass fails repeatedly | `blocked` after `max_consecutive_failures`. A refusal is not a failure and is not counted. Clears with a digest change or `--force`. |

## Alternatives considered

- A workflow in the consumer's CI: rejected. AWS credentials move into consumer CI, the command cannot see the pin, so prune could delete the pinned image, and there is no leak backstop or metrics.
- A cloud automation document: rejected. A second privileged role, a pass-role path, the presigned URL as an execution input, version skew with the auto-updating controller and managed tagging that would distort scaling accounting.
- A managed image-building service: rejected. It needs an instance profile and pass-role and creates account-wide objects.
- Long-lived keys used directly: rejected by the operator (decision 2). Assumed-role sessions limit a leaked key's reach to a role whose trust policy also demands the external ID.
- A helper process that assumes the role: rejected. The SDK profile does the same without a process.
- A separate pin file instead of editing the TOML: not adopted, because the operator decided that promotion edits the one key of the existing file.
- Automatic promotion now: deferred. No canary reader exists and a person has approved every pin.
- A webhook trigger: rejected. It needs an inbound listener.
- Sharing the scaling ledger: rejected. A schema bump after a rollback could halt scaling.
- Reusing the `apply` token for source access: rejected. It has write scope.

## Evidence gaps (verification gates)

The operator applies the policy and runs the IAM policy simulator, or a real dry run, before relying on any statement. Each gate is a live-qualification task in `tasks.md` section 9.

| Gate | Question |
|---|---|
| G1 | Does the `ec2:InstanceProfile` Null Deny block `RunInstances` with a profile? |
| G2 | Does the `ec2:LaunchTemplate` StringNotEquals Deny hold when a caller supplies a user-data override? |
| G3 | Do the request-tag conditions work on volume and network-interface resources? |
| G4 | Which extra actions does `CreateImage` need with tag conditions? |
| G5 | Does `CreateLaunchTemplateVersion` need `iam:PassRole` when the source version carries an instance profile? |
| G6 | What is the archive prefix? Does `export-ignore` drop files that the warm commands need? What scope does the dedicated token need, and can it read the `protected` flag? |
| G7 | What is the console blind-probe root cause and the real console latency? |
| G8 | Does the refresh script handle a new timer unit? (phase 2) |
| G9 | Does the SDK profile support `external_id` and `duration_seconds` and refresh inside the container? Can a source-identity condition be met from a profile alone? |
| G10 | What is the effective expiry of a URL presigned with assumed-role credentials? |
| G11 | Is terminating the builder safe while the image is `pending`? |
| G12 | Does `CreateLaunchTemplateVersion` accept an image that is not yet `available`? (The design waits for `available`.) |
| G13 | Does the read-back comparison see the same normalization for both versions? |

Other gaps: the three manual builds are the only runtime evidence, and the console
latency and build duration are unmeasured by the product.

## Open questions

1. How to reconcile `warm-worker-image`'s requirement `Rollout stays the operator's existing flow` once that change is archived. Default: the manual path stays documented as the fallback, and the follow-up modifies that requirement.
2. Whether this change moves the shipped apply, export and reap units to a dedicated TOML directory. Default: it does not. Documentation states the layout, and `promote --write` refuses until the operator adopts it.
3. How `promote` treats a per-zone `launch_template_version`. Default: `--write` refuses, and the operator edits by hand. Retention protects zone-pinned images in any case.
4. Whether worker events can record the template version that a worker used, so that canary reading is possible later. Default: not in this change.

## Risks

- Reversing a stated product boundary: mitigated by opt-in, a separate role, tag scope and no creation of IAM or network objects.
- A second long-lived credential on the host (the source key): accepted with rotation and a role that demands the external ID.
- IAM cannot restrict version content: bounded by the post-create comparison and the manual pin.
- A refresh-delivered `warm-cache.sh` change alters build behavior: its sha is in the digest, in the image tags and in `image plan`.
- A presigned URL sits in builder user data for up to `presign_seconds`: accepted.
- Churn from frequent lockfile changes: bounded by the minimum interval and the daily cap.
- `promote --write` writes a file that no other code writes: bounded by the single-key edit, the strict re-parse, the backup and the dedicated directory.

## Rollback

- Disable: set `[image].enabled = false` and remove the unit's credential mount. Image commands then make no call.
- Pin: `image promote --rollback`, or edit the previous number by hand.
- Source: revert the change. The scaling ledger needs no migration because this change never touched it. Delete `image-state.sqlite` if wanted. Reconcile will find tagged leftovers by tag.
