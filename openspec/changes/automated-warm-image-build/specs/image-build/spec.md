# Spec Delta

## ADDED Requirements

### Requirement: Image build is opt-in and inert by default
ci-burst MUST create, tag or delete a worker image, a snapshot, a builder instance, a launch-template version or a staged archive only when `[image].enabled = true`. When the `[image]` table is absent or `enabled` is false, every `image` command MUST exit 0 with the decision `disabled` and MUST make no AWS, S3 or Forgejo call.

#### Scenario: The feature is absent
- **WHEN** the deployment configuration has no `[image]` table and an operator runs `image run --apply`
- **THEN** the command exits 0 with the decision `disabled`
- **AND** no network service is called

#### Scenario: The feature is disabled
- **WHEN** `[image].enabled` is false and an operator runs `image plan`
- **THEN** the decision is `disabled` and no network service is called

#### Scenario: The operator opts in
- **WHEN** `[image].enabled` is true and the other `[image]` tables are valid
- **THEN** the image commands run their normal checks
- **AND** the scaling behavior of `apply`, `reap` and `export` is unchanged

### Requirement: Image commands default to observation
`image plan`, plain `image status` and `image run` without `--apply` MUST make no provider mutation: no S3 write, no builder, no image, no snapshot, no launch-template version and no deletion. They MUST NOT change the image state file except for the check timestamp. An AWS dry-run flag MUST NOT replace this isolation. No command makes a provider mutation unless `--apply` is given.

#### Scenario: Run without apply
- **WHEN** an operator runs `image run` without `--apply`
- **THEN** the output is the plan
- **AND** no S3 object, instance, image or launch-template version is created

#### Scenario: Plain status
- **WHEN** an operator runs `image status` without `--refresh`
- **THEN** the command makes no provider call that mutates and writes only the check timestamp

#### Scenario: Apply is explicit
- **WHEN** an operator runs `image run --apply` and every precondition holds
- **THEN** the pass MAY stage the archive and launch one builder

### Requirement: Status refresh persists only verification evidence
`image status --refresh` MUST perform describe-only provider reads and MUST persist only these fields: `verified_at`, the image state, whether the template version exists, the drift result and the observed pins. It MUST make no provider mutation and MUST persist no other field. The pass that records the evidence MUST be the only source of `verified_at` for `promote`.

#### Scenario: Refresh persists the listed fields
- **WHEN** an operator runs `image status --refresh`
- **THEN** only the listed fields change in the state file
- **AND** no provider mutation is made

#### Scenario: Stale evidence, refresh, then promote
- **WHEN** `verified_at` is older than `promote_max_age_seconds` and `promote --write` refuses with `IMAGE_VERIFICATION_STALE`
- **AND** the operator runs `image status --refresh` in the build container
- **THEN** the later `promote --write` in the container without AWS credentials succeeds when every other check holds

### Requirement: Image build state is separate from the scaling ledger
Image build state MUST live in its own SQLite file with its own lock and its own schema version, in a state directory that the scaling units do not mount and that the image unit does not share with the scaling ledger. The image code MUST NOT read or write the scaling ledger and MUST NOT change its schema version. A lost or restored image state file MUST NOT permit a second builder or a deletion of an image that a pin references.

#### Scenario: The image code is rolled back
- **WHEN** a controller release without the image code runs `apply` on a host that has an image state file
- **THEN** `apply` opens its ledger at the schema version it expects
- **AND** it does not read the image state file

#### Scenario: The image state file is lost
- **WHEN** the image state file is deleted while a tagged builder is running
- **THEN** the next pass reconciles from AWS tags
- **AND** the tag precheck refuses a second launch with `IMAGE_BUILD_ALREADY_ACTIVE`

#### Scenario: The image state file is restored from an old copy
- **WHEN** an old copy lacks a build that has since started a builder
- **THEN** reconcile terminates the unmatched builder or adopts it by tag
- **AND** no second builder runs

### Requirement: Image builds use a separate assumed-role identity
Image builds MUST use a separate AWS role that the SDK assumes through a shared-config profile that names `role_arn` and `source_profile`, with a session of at most one hour. The `source_profile` chain MUST end in static keys within five profiles and MUST use no other credential route; anything else MUST be refused with `IMAGE_PROFILE_INVALID`. The image path MUST open its own SDK session from `[image].profile` and MUST NOT reuse the scaling session, the scaling credentials or `[aws].profile`.

#### Scenario: A build assumes the role
- **WHEN** a pass makes its first AWS call
- **THEN** the SDK assumes the image role from the profile
- **AND** no helper process is started

#### Scenario: The role cannot be assumed
- **WHEN** the source credentials are missing or the trust policy refuses
- **THEN** the pass exits 1 with a provider refusal on its first call
- **AND** the scaling units are unaffected

#### Scenario: A pass would reuse the scaling profile
- **WHEN** `[image].profile` equals `[aws].profile`
- **THEN** configuration loading refuses with `CONFIG_INVALID`

### Requirement: The image path has no IAM authority
The controller code MUST contain no IAM client. No shipped policy MUST contain an `iam:*` wildcard or any IAM action other than the one optional, condition-scoped `iam:PassRole` statement, `ec2:ModifyLaunchTemplate` or `ec2:DeleteLaunchTemplateVersions`. The operator, not ci-burst, MUST create and change all IAM. The documented source identity MUST be allowed `sts:AssumeRole` on the one image role and no other action.

#### Scenario: The code or policy gains an IAM action
- **WHEN** the controller source constructs an IAM client or a shipped policy contains an `iam:*` wildcard, an IAM action other than the one condition-scoped `iam:PassRole` statement, `ec2:ModifyLaunchTemplate` or `ec2:DeleteLaunchTemplateVersions`
- **THEN** a repository test fails and names the file

#### Scenario: The source identity is documented
- **WHEN** an operator reads the documented source-identity policy
- **THEN** it allows `sts:AssumeRole` on one role ARN and nothing else
- **AND** the role's trust policy example names one source principal and an external ID

### Requirement: The policy file is tag-scoped
The shipped image-build policy MUST be tag-scoped, MUST contain an explicit Deny on `ec2:RunInstances` when an instance profile is attached and an explicit Deny on `ec2:RunInstances` outside the build launch template, and MUST scope S3 access to the configured prefix. `iam:PassRole` MUST appear only as one statement for the single worker role with `iam:PassedToService` equal to `ec2.amazonaws.com`, and only after gate G5 shows that it is needed.

#### Scenario: The policy omits a required Deny
- **WHEN** the policy lacks the instance-profile Deny or the build-template Deny
- **THEN** a repository test fails

#### Scenario: A pass-role statement is proposed
- **WHEN** a policy change adds `iam:PassRole`
- **THEN** a repository test fails unless the statement names one role ARN and the `ec2.amazonaws.com` condition

### Requirement: Policy conditions are verified before enabling
Documentation MUST list the verification gates G1 to G5 and MUST require the operator to verify them with the IAM policy simulator or a real dry run before `[image].enabled` is set. Until the operator records the gates, `[image].enabled` MUST stay false.

#### Scenario: A gate is not yet verified
- **WHEN** an operator has not recorded gates G1 to G5
- **THEN** the documentation and the operator checklist state that `[image].enabled` MUST stay false

#### Scenario: A gate fails
- **WHEN** the simulator shows that a Deny or a condition does not hold
- **THEN** the operator corrects the policy and repeats the gate before enabling

### Requirement: Source access uses a dedicated read-only token
The image path MUST read the source with a dedicated read-only Forgejo token from its own env file, named by `source.token_env`. It MUST NOT use the token that `apply` uses and MUST NOT fall back to it. `image plan` MUST read the branch, one raw file and the archive endpoint, and MUST refuse with `IMAGE_SOURCE_FORBIDDEN` on a 401 or 403 before any AWS call. The operator documentation MUST record the scope with which the token was created.

#### Scenario: The token is forbidden
- **WHEN** the forge answers 401 or 403 to the branch, raw or archive read
- **THEN** `plan` and `run` refuse with `IMAGE_SOURCE_FORBIDDEN` and exit 2
- **AND** no AWS call is made

#### Scenario: The token variable is absent
- **WHEN** the variable named by `source.token_env` is unset
- **THEN** the command refuses and does not read the variable that `apply` uses

#### Scenario: The operator replaces the token
- **WHEN** the operator widens or replaces the token and runs `plan` again
- **THEN** the refusal clears without a state change

### Requirement: The source is one revision on the protected branch
The command MUST resolve the head sha of `source.branch` through the forge's branch endpoint and MUST refuse with `IMAGE_BRANCH_UNPROTECTED` unless the response reports the branch as protected. The archive, the basis digest and every tag MUST describe that one sha. The command MUST NOT use a pull-request ref. A basis path that is absent at that sha MUST be refused with `IMAGE_BASIS_PATH_MISSING`.

#### Scenario: A protected branch
- **WHEN** the branch is reported protected
- **THEN** the archive is fetched for the resolved sha, never for the branch name

#### Scenario: An unprotected branch
- **WHEN** the response omits the protected flag or reports it false
- **THEN** the command refuses with `IMAGE_BRANCH_UNPROTECTED` and stages nothing

#### Scenario: A basis path is missing
- **WHEN** a configured basis path is absent at the sha
- **THEN** the command refuses with `IMAGE_BASIS_PATH_MISSING` and exits 2

#### Scenario: The branch moves during a pass
- **WHEN** the branch head changes after the sha was resolved
- **THEN** the pass still builds the resolved sha and records it

### Requirement: The basis digest decides whether a build is due
The command MUST compute the basis digest over the sorted lines of each basis path and its content hash, the hash of the rendered warm and gate commands and the hash of the shipped `warm-cache.sh`. It MUST NOT use the archive hash as the digest. A build is due when the digest differs from the `ci-burst:basis-digest` tag of the image behind the configured pin.

#### Scenario: A lockfile changes
- **WHEN** a basis file changes
- **THEN** the digest differs from the pin's tag and a build is due

#### Scenario: The shipped script changes
- **WHEN** the shipped `warm-cache.sh` changes and no basis file changes
- **THEN** the digest differs, `plan` shows the new script sha and a build is due

#### Scenario: The digest matches
- **WHEN** the digest equals the pin's tag
- **THEN** no build starts and the decision names `digest_unchanged`

### Requirement: Builds are rate limited and refusals are named
A build MUST start only when the digest differs from the pin's tag and the minimum build interval has passed, the daily cap is not reached, the failure cooldown is clear, the control state is not `blocked` and no builder exists. `--force` MUST skip only the digest comparison and the minimum interval, and MUST clear `blocked` and the cooldown. `image plan` MUST show the shipped script sha and name the first condition that prevents a build.

#### Scenario: The daily cap is reached
- **WHEN** two builds have started in the last day and an operator runs `run --apply --force`
- **THEN** the command refuses with `IMAGE_DAILY_CAP` and exits 2

#### Scenario: The minimum interval has not passed
- **WHEN** a build started less than `min_build_interval_seconds` ago and the digest differs
- **THEN** `plan` names the interval as the first blocking condition
- **AND** `run --apply --force` skips the interval but still applies the daily cap

### Requirement: The source archive is staged privately, bounded and deleted
The command MUST stream the archive for the resolved sha through a hashing wrapper into a multipart upload under the configured bucket and prefix, and MUST NOT write the archive to disk. The size cap `source.max_archive_bytes` MUST abort the upload and delete the object. The command MUST delete the archive at the terminal state of the build, and the bucket lifecycle rule MUST be documented as a backstop only. The command MUST NOT create the bucket or change its configuration.

#### Scenario: The archive exceeds the cap
- **WHEN** the streamed bytes pass `source.max_archive_bytes`
- **THEN** the multipart upload is aborted and the key is deleted
- **AND** the build is `failed` and no builder is launched

#### Scenario: The upload fails
- **WHEN** a part upload fails
- **THEN** the upload is aborted and no builder is launched

#### Scenario: The build ends
- **WHEN** a build reaches `candidate` or `failed`
- **THEN** its archive object is deleted

### Requirement: Credentials and URLs are not persisted
The presigned archive URL, tokens, rendered user data and raw provider payloads MUST NOT appear in logs, events, the state file, command output or error messages. The command MUST NOT presign when the credential session has less remaining life than `source.presign_seconds` and MUST refuse with `IMAGE_CREDENTIAL_SHORT`. The URL MUST exist only in the rendered user data in memory.

#### Scenario: A build runs
- **WHEN** a pass presigns, launches and fails
- **THEN** no persisted file, event or output contains the URL or the token

#### Scenario: The session is nearly expired
- **WHEN** the role session has less life left than `presign_seconds`
- **THEN** the pass refuses with `IMAGE_CREDENTIAL_SHORT` and launches nothing

#### Scenario: An error carries a payload
- **WHEN** a provider error contains configuration or user data
- **THEN** the reported reason holds only the stable code and a bounded, sanitized message

### Requirement: Builder user data is compact and bounded
The command MUST render builder user data as a short script stub with shell-quoted exports and a gzip and base64 payload of `warm-cache.sh`, and MUST refuse with `IMAGE_USERDATA_TOO_LARGE` before `RunInstances` when the result is 16,384 bytes or more. Every exported value MUST come from configuration and MUST be refused when it contains a control character. A repository test MUST render the largest allowed `WARM_COMMANDS` and assert the size.

#### Scenario: The largest allowed commands
- **WHEN** the largest allowed commands are rendered
- **THEN** the user data is below 16,384 bytes

#### Scenario: A rendering is too large
- **WHEN** configuration produces user data of 16,384 bytes or more
- **THEN** the command refuses with `IMAGE_USERDATA_TOO_LARGE` and does not call `RunInstances`

#### Scenario: A value holds a control character
- **WHEN** a warm command contains a control character
- **THEN** configuration loading refuses it

### Requirement: The controller container ships the warm script
The controller container MUST include `warm-cache.sh`, and a repository test MUST fail when that copy differs from `deploy/runner/image/warm-cache.sh`.

#### Scenario: The copied script drifts
- **WHEN** the script in the container build differs from the repository script
- **THEN** a repository test fails

#### Scenario: The script is missing
- **WHEN** the container build lacks `warm-cache.sh`
- **THEN** `image plan` refuses and a repository test fails

### Requirement: The builder is launched only from the safe build template
The builder MUST launch only from the configured build launch template at its pinned version, with no instance profile, IMDSv2 required with hop limit 1, terminate on instance-initiated shutdown, delete-on-termination storage, and a security group with no ingress. `plan` and `run` MUST read the template and MUST refuse with `IMAGE_BUILD_TEMPLATE_UNSAFE` when any property differs. A builder MUST be on-demand and MUST NOT chain from the warm image.

#### Scenario: An unsafe template
- **WHEN** the build template carries an instance profile or allows IMDSv1 or has an ingress rule
- **THEN** the command refuses with `IMAGE_BUILD_TEMPLATE_UNSAFE` and launches nothing

#### Scenario: A profile is attached at launch
- **WHEN** a request would attach an instance profile to a builder
- **THEN** the policy Deny refuses it and the controller does not send it

#### Scenario: A safe template
- **WHEN** the template has every required property
- **THEN** one builder launches with the state row written before the call and a client token from the state file

### Requirement: Builders are not owned workers
Builders MUST carry `ci-burst:image-build=true`, the deployment tag, `ci-burst:build-id` and `ci-burst:expires-at`, and MUST NOT carry `ci-burst:managed=true`. A repository test MUST assert that `owned_instances` never returns a builder, so a builder never counts against `controller.max_instances` and `reap` never selects it.

#### Scenario: A builder runs beside workers
- **WHEN** a builder and workers exist for one deployment
- **THEN** `owned_instances` returns the workers only
- **AND** the builder does not count against `controller.max_instances`

#### Scenario: A builder is mis-tagged
- **WHEN** a launch request for a builder would carry the managed tag
- **THEN** a repository test fails

### Requirement: At most one builder exists
At most one builder MUST exist for a deployment. A unique index keyed on the deployment over the `building` and `imaging` states MUST enforce this in the state file, and before any launch the command MUST find no instance with the image-build tag and the deployment tag in the states pending, running, stopping or stopped. A second launch MUST be refused with `IMAGE_BUILD_ALREADY_ACTIVE`.

#### Scenario: A concurrent run
- **WHEN** a second pass starts while a build is `building`
- **THEN** it refuses with `IMAGE_BUILD_ALREADY_ACTIVE` and exits 2

#### Scenario: Two active rows
- **WHEN** a test inserts a second active row for the deployment
- **THEN** the insert fails on the unique index

#### Scenario: The state file lacks the running builder
- **WHEN** a tagged builder exists and the state file has no active row
- **THEN** the AWS precheck refuses the launch

### Requirement: A run can supervise a build to completion
Each pass MUST be bounded by the pass deadline and MUST be resumable. `image run --apply --until-done` MUST repeat bounded passes with a fixed sleep between them until the build reaches `candidate` or `failed`, or the build deadline passes. Without `--until-done`, `run --apply` MUST run one pass. Rerunning the same command MUST resume an interrupted supervisor from the state file. `--until-done` MUST be refused without `--apply`.

#### Scenario: The marker arrives after the first pass
- **WHEN** the first pass ends at its deadline before the marker and the marker appears later
- **THEN** a later pass of the same `--until-done` run sees the marker, calls `CreateImage` in that pass and completes the build to `candidate` without a timer

#### Scenario: The supervisor is interrupted
- **WHEN** the supervising process is stopped during a build
- **THEN** rerunning `image run --apply --until-done` resumes the same build and starts no second builder

#### Scenario: The build deadline passes
- **WHEN** the build deadline passes with no marker
- **THEN** the supervisor ends the build with `MARKER_TIMEOUT` or `CONSOLE_BLIND`

#### Scenario: Until-done without apply
- **WHEN** an operator passes `--until-done` without `--apply`
- **THEN** the command refuses with exit 2 and does nothing

### Requirement: Success requires the framed marker and the staged digest
A build MUST succeed only when the console shows `WARM_SOURCE_SHA256` with a digest equal to the archive sha256 held in the state file, followed by the whole line `IMAGE_READY`, read through the existing framed-line discipline. A `WARM FAIL` line or a `warm exiting rc=` line MUST fail the build. The command MUST NOT guess success.

#### Scenario: The marker matches
- **WHEN** the console shows the matching digest and then `IMAGE_READY`
- **THEN** the build proceeds to image creation in the same pass

#### Scenario: The digest differs
- **WHEN** the marker digest differs from the staged archive sha256
- **THEN** the build fails with `IMAGE_MARKER_DIGEST_MISMATCH` and no image is created

#### Scenario: Consumer output imitates the marker
- **WHEN** a consumer command prints text that looks like the marker
- **THEN** the unframed-line rule does not count it as success

#### Scenario: A warm command fails
- **WHEN** the console shows `WARM FAIL` or `warm exiting rc=`
- **THEN** the build fails with `WARM_FAILED`, the builder is terminated and no image is created

### Requirement: A missing marker fails closed
When no marker appears by `build_deadline_seconds`, the command MUST terminate the builder, MUST create no image and MUST record `CONSOLE_BLIND` when the console is empty or unreadable, or `MARKER_TIMEOUT` when the console is readable.

#### Scenario: The console is blind
- **WHEN** the console is empty at the deadline
- **THEN** the builder is terminated, no image is created and the reason is `CONSOLE_BLIND`

#### Scenario: The console is readable but has no marker
- **WHEN** the console has output and no marker at the deadline
- **THEN** the builder is terminated and the reason is `MARKER_TIMEOUT`

#### Scenario: A failed build recovers
- **WHEN** the basis digest changes or an operator runs `run --apply --force`
- **THEN** a new build MAY start once the cooldown and the daily cap permit

### Requirement: A gate command is required
`image.warm.gate_command` MUST be non-empty unless `allow_no_gate = true`. Otherwise the command MUST refuse with `IMAGE_GATE_REQUIRED`. Documentation MUST state that the offline gate proves cache behavior and does not prove that the image is safe.

#### Scenario: No gate and no opt-out
- **WHEN** the gate command is empty and `allow_no_gate` is false
- **THEN** the command refuses with `IMAGE_GATE_REQUIRED` and exits 2

#### Scenario: The opt-out is recorded
- **WHEN** `allow_no_gate` is true and the gate command is empty
- **THEN** the build proceeds and the candidate record states that no gate ran

### Requirement: The image is created in the pass that sees the marker
The pass that first sees the marker MUST call `CreateImage` before it does any other step, with no reboot, and MUST tag the image and its snapshots with `ci-burst:image-build`, the deployment, the build id, the full basis digest, the source sha, the script sha and the base image. The pass MUST NOT leave the builder running after the image is `available` or after the snapshot window or lifetime timer ends it. Waiting for the image and every later step MAY continue in a later pass.

#### Scenario: A slow tick
- **WHEN** the pass that sees the marker is the last pass inside the snapshot window
- **THEN** `CreateImage` is called in that pass
- **AND** the build does not fail with `IMAGE_BUILDER_GONE`

#### Scenario: The builder is gone
- **WHEN** the builder no longer exists when `CreateImage` would be called
- **THEN** the build fails with `IMAGE_BUILDER_GONE` and the cooldown starts

#### Scenario: The image stays pending
- **WHEN** the image is not `available` by the deadline
- **THEN** the build is `failed`
- **AND** the image and its snapshots are removed only under the image deletion rule

### Requirement: Reconcile runs first and cleans leaks
Every pass MUST reconcile before any transition. Reconcile MUST terminate builder-tagged instances that are not the active build's builder or are past the deadline, MUST delete objects under the prefix that are not the active archive and are older than one hour, and MUST clean the images of failed builds only under the image deletion rule.

#### Scenario: A leaked builder
- **WHEN** a builder is tagged for the deployment and the state file has no matching active build
- **THEN** reconcile terminates it before any other step

#### Scenario: A stray archive
- **WHEN** an object older than one hour sits under the prefix and is not the active archive
- **THEN** reconcile deletes it

### Requirement: Orphans are recorded and the builder has its own backstop
A tagged image that has no state row MUST be recorded as an unverified `candidate` and MUST NOT be pinned. The builder's lifetime timer and terminate-on-shutdown setting MUST remain the leak backstop that does not depend on the controller.

#### Scenario: An orphan image
- **WHEN** a tagged image has no state row
- **THEN** it is recorded as an unverified candidate and is never selected for promotion

#### Scenario: The controller is not running
- **WHEN** no pass runs after a builder starts
- **THEN** the builder's lifetime timer powers it off and terminate-on-shutdown removes it

### Requirement: Failures are bounded
Each build MUST be bounded by `build_deadline_seconds`, the builder's lifetime timer, `min_build_interval_seconds`, `max_builds_per_day`, a failure cooldown, and a `blocked` state that begins after `max_consecutive_failures` consecutive failures. `blocked` MUST clear only with a basis digest change or `image run --apply --force`, and `image status` MUST say so and show how.

#### Scenario: The failure limit is reached
- **WHEN** consecutive failures reach `max_consecutive_failures`
- **THEN** the control state becomes `blocked` and every later `run --apply` refuses until it clears
- **AND** an `image_blocked` event is written

#### Scenario: Recovery from blocked
- **WHEN** the basis digest changes or an operator runs `run --apply --force`
- **THEN** `blocked` clears and the cooldown resets

#### Scenario: Status explains blocked
- **WHEN** an operator runs `image status` while blocked
- **THEN** the output names the two ways to clear it

### Requirement: A launch-template version is created inert and drift-checked
After the image is `available`, the command MUST create a new launch-template version from the configured pin as `SourceVersion` (never `$Latest` or the default), changing only `ImageId`, with a client token from the state file. It MUST then read back both versions and compare their full `LaunchTemplateData`. Any difference other than `ImageId` MUST yield `TEMPLATE_DRIFT`, mark the version unusable and prevent promotion.

#### Scenario: Only the image differs
- **WHEN** the new version equals the pin's version except for `ImageId`
- **THEN** the candidate record stores the version, the image id, the archive sha, the script sha, the gate result and the source pin

#### Scenario: Another field differs
- **WHEN** the read-back shows any other difference
- **THEN** the command emits `TEMPLATE_DRIFT`, marks the version unusable and keeps the image for 24 hours

#### Scenario: Version creation fails
- **WHEN** the call fails or the pass ends before the response
- **THEN** the next pass retries with the same client token and does not rebuild

### Requirement: Versions are never moved or removed
The command MUST NOT set the default launch-template version and MUST NOT delete a launch-template version. Documentation MUST state that a hand-edited pin bypasses the drift check.

#### Scenario: A version is created
- **WHEN** the command creates a version
- **THEN** the default version is unchanged

#### Scenario: A pin is edited by hand
- **WHEN** an operator edits the pin to a version that no candidate record owns
- **THEN** the documentation states that no drift check applied to that version

### Requirement: No timer and no credentialed process moves the pin
No timer and no process that holds AWS credentials MUST write the deployment TOML or change `aws.launch_template_version`. The shipped timer unit MUST NOT mount the deployment TOML writable and MUST NOT run `promote`.

#### Scenario: The timer runs
- **WHEN** the image timer runs a pass that produces a candidate
- **THEN** the TOML is unchanged and the candidate waits for a person

#### Scenario: A unit file is inspected
- **WHEN** a repository test reads the shipped image unit
- **THEN** it finds the TOML mounted read-only and no `promote` command

### Requirement: Promotion is pure local
`image promote` MUST be pure local: it MUST make no AWS call, hold no AWS credentials and read only the image state file and the deployment TOML. Without `--write` it MUST print the evidence block, the diff of the one pin line, the previous pin and the rollback target.

#### Scenario: Print only
- **WHEN** an operator runs `promote` without `--write`
- **THEN** the command prints the evidence and the pin line `launch_template_version = N`
- **AND** the TOML is unchanged

#### Scenario: An AWS client is requested
- **WHEN** a test makes the AWS client factory raise
- **THEN** `promote` still completes, with or without `--write`

### Requirement: Promotion writes only a verified candidate
`promote --write` MUST refuse unless the candidate record shows: the image `available`; the template version existing; the full `LaunchTemplateData` differing from its source pin only in `ImageId`; the offline gate configured and passed (or `allow_no_gate` recorded); a `verified_at` within `promote_max_age_seconds`; and a TOML pin equal to the record's source pin.

#### Scenario: An unverified candidate
- **WHEN** the candidate record lacks any verification item
- **THEN** `promote --write` refuses and names the missing item

#### Scenario: A stale verification
- **WHEN** `verified_at` is older than `promote_max_age_seconds`
- **THEN** `promote --write` refuses with `IMAGE_VERIFICATION_STALE` and tells the operator to run `image status --refresh`

#### Scenario: The pin moved
- **WHEN** the TOML pin differs from the record's source pin
- **THEN** `promote --write` refuses with `PIN_MOVED`

#### Scenario: A foreign version
- **WHEN** the operator passes `--version N` for a version that no candidate record owns
- **THEN** `promote --write` refuses

#### Scenario: A zone candidate pins its own version
- **WHEN** an `[[aws.availability_zones]]` entry carries its own `launch_template_version`
- **THEN** `promote --write` refuses and tells the operator to edit by hand

### Requirement: Promotion edits one key atomically
`promote --write` MUST edit only the `launch_template_version` key of the `[aws]` table. It MUST refuse with `PROMOTE_PIN_LINE_AMBIGUOUS` unless exactly one such assignment exists, MUST re-parse the new text with the controller's strict loader and require that the parsed result differs from the original only in that key, and MUST rename atomically.

#### Scenario: A successful write
- **WHEN** the candidate is verified and the file is unchanged since it was read
- **THEN** only the one value changes, and comments and other tables are preserved

#### Scenario: The strict re-parse fails
- **WHEN** the new text fails the strict loader or changes any other key
- **THEN** the file and its backup are unchanged and the command refuses

#### Scenario: The key is ambiguous
- **WHEN** the `[aws]` table has no assignment or more than one
- **THEN** the command refuses with `PROMOTE_PIN_LINE_AMBIGUOUS`

### Requirement: Promotion is journaled before the rename
Before the rename, `promote --write` MUST commit a pending-promotion journal row to the image state file with the previous pin, the new pin, the previous and new file sha256, the candidate id, the backup name, the backup's sha256 and the pin that the backup contains. After the rename it MUST mark the row complete. `image status` MUST report a pending row and what the file hash implies.

#### Scenario: The order of a write
- **WHEN** `promote --write` succeeds
- **THEN** the pending row was committed before the rename and is marked complete after it

#### Scenario: Status shows a pending row
- **WHEN** an operator runs `image status` while a row is pending
- **THEN** the output names the pending row and the resolution that the file hash implies
- **AND** the state file is not changed except for the check timestamp

### Requirement: A pending promotion is resolved from the file hash
The next `promote`, `promote --rollback` or `image run --apply` MUST resolve a pending row from the file hash: the old hash marks it aborted, the new hash completes it, and any other hash refuses with `PROMOTION_JOURNAL_DIVERGED`. While a row is pending or diverged, both of its pins MUST be protected from deletion.

#### Scenario: A crash before the rename
- **WHEN** the process stops after the journal commit and before the rename
- **THEN** the file still has the old hash and the next resolving command marks the row aborted
- **AND** the pin is unchanged

#### Scenario: A crash after the rename
- **WHEN** the process stops after the rename and before the completion commit
- **THEN** the file has the new hash and the next resolving command completes the row
- **AND** a later rollback finds the recorded previous pin

#### Scenario: The file is edited by hand in between
- **WHEN** the file has neither the old nor the new hash while a row is pending
- **THEN** the command refuses with `PROMOTION_JOURNAL_DIVERGED` and writes nothing
- **AND** retention protects both pins until an operator resolves the divergence

### Requirement: Promotion keeps a backup and refuses a changed file
Before the rename, `promote --write` MUST write a timestamped backup and MUST refuse with `PROMOTE_FILE_CHANGED` when the file differs from the bytes it read.

#### Scenario: A backup exists
- **WHEN** a write succeeds
- **THEN** a backup `<file>.bak.<UTC timestamp>` exists and holds the previous bytes

#### Scenario: The file changed since it was read
- **WHEN** another editor changes the file after the read
- **THEN** the command refuses with `PROMOTE_FILE_CHANGED` and writes nothing

### Requirement: Promotion runs in an isolated operator container
`promote` MUST run only as an operator-invoked one-off. The shipped documentation MUST give a container line that has no network, no AWS credential directory and no forge token. `promote --write` MUST refuse with `PROMOTE_DIRECTORY_NOT_DEDICATED` when the directory of the TOML holds anything other than the TOML, its `.bak.*` files and a lock file. A test MUST assert that `promote` constructs no AWS client.

#### Scenario: The directory holds credentials
- **WHEN** the mounted directory contains a credential directory or an env file
- **THEN** `promote --write` refuses with `PROMOTE_DIRECTORY_NOT_DEDICATED` and writes nothing

#### Scenario: A dedicated directory
- **WHEN** the directory holds only the TOML and its backups
- **THEN** `promote --write` MAY proceed

#### Scenario: Print only needs no directory write
- **WHEN** an operator runs `promote` without `--write` against a read-only mount
- **THEN** the command succeeds

### Requirement: Rollback restores the recorded previous pin
`promote --rollback --write` MUST write the recorded previous pin with the same journaled procedure as a promotion, after it resolves a pending journal row. The previous pin's image MUST be protected from deletion, so a rollback needs no rebuild.

#### Scenario: A bad promotion is rolled back
- **WHEN** the record and the backup named by the newest completed row agree on the previous pin
- **THEN** the pin returns to that number and a completed rollback row is recorded

### Requirement: Rollback verifies the records first
`promote --rollback --write` MUST use the backup named by the newest completed promotion row, MUST ignore the backups of aborted rows, and MUST refuse with `PIN_RECORD_DIVERGED` unless that row's new pin equals the TOML's pin and the named backup exists with its recorded sha256 and the recorded previous pin. A backup of an aborted row MUST be kept and MUST NOT be used for rollback. It MUST refuse unless the previous version and its image are recorded as existing with a fresh `verified_at`.

#### Scenario: An aborted attempt sits between
- **WHEN** a promotion from A to B completed, a later promotion from B to C wrote a newer backup and was marked aborted, and the operator runs `promote --rollback --write`
- **THEN** the rollback uses the backup of the A to B row and restores A
- **AND** the aborted row's backup is kept and unused

#### Scenario: The records diverge
- **WHEN** the TOML pin, the named backup or its recorded sha256 and the completed row disagree
- **THEN** the command refuses with `PIN_RECORD_DIVERGED` and writes nothing

#### Scenario: The previous image is missing
- **WHEN** the previous pin's image is not recorded as existing
- **THEN** the command refuses and does not write

### Requirement: Every image deletion resolves the protected set
Every path that deregisters an image or deletes a snapshot (retention, failed-build cleanup, orphan cleanup and the pending-image timeout) MUST first resolve the protected set: the images of every launch-template version that the TOML references, including per-zone pins, the recorded previous pin, the pins of a pending promotion journal row, the current candidate unless its row records `TEMPLATE_DRIFT`, and an active build. It MUST NOT deregister a protected image.

#### Scenario: A protected image
- **WHEN** an image is the pin's, the previous pin's, a journal pin's, or the candidate's while its row does not record `TEMPLATE_DRIFT`
- **THEN** every deletion path skips it

#### Scenario: A failed build's image is pinned by hand
- **WHEN** an operator has pinned a version that names the image of a failed build
- **THEN** failed-build cleanup skips that image and its snapshots
- **AND** the build row records that its image is protected

#### Scenario: A made-up protected set or plan
- **WHEN** a caller passes a protected set that the resolver did not build, an empty set, or a plan item that the planner did not make or that was changed afterwards
- **THEN** the deletion is refused with `IMAGE_DELETION_UNSAFE` before any call

#### Scenario: A drifted candidate
- **WHEN** the current candidate's row records `TEMPLATE_DRIFT` and no pin names its image
- **THEN** it is not protected as the candidate, and failed-build cleanup removes its image after the 24-hour hold

#### Scenario: A pending journal pins an image
- **WHEN** a promotion journal row is pending and names a version whose image would otherwise be removed
- **THEN** no deletion path removes it

### Requirement: Every image deletion is verified and fails closed
Every deletion path MUST verify the ownership tags (image-build, deployment and build id) before it deletes, MUST delete nothing when the TOML or a launch-template version cannot be read, and MUST delete a snapshot only after a confirmed deregistration of its image. A deletion path MUST only report what it would remove unless `--apply` is given.

#### Scenario: The pin read fails
- **WHEN** the TOML or a launch-template version cannot be read during failed-build cleanup, orphan cleanup or retention
- **THEN** that path deletes nothing and reports the refusal

#### Scenario: Deregister fails
- **WHEN** the deregister call for an image fails or is not confirmed
- **THEN** its snapshots are not deleted

#### Scenario: Ownership tags do not match
- **WHEN** an image or snapshot lacks the image-build tag, the deployment tag or the build id of the build
- **THEN** no deletion path removes it

#### Scenario: A deletion path is rerun after the cause is fixed
- **WHEN** the operator fixes the unreadable input and runs `image run --apply` again
- **THEN** the path proceeds with the same rules

#### Scenario: Deletion without apply
- **WHEN** an operator runs `image run` without `--apply`
- **THEN** the output lists what each path would remove and nothing is deleted

### Requirement: Retention keeps a minimum set of images
Retention MUST keep at least `retain` images for the deployment (default 3, range 2 to 6) and MUST consider only images older than `retain_min_age_seconds`. It runs inside `image run`.

#### Scenario: Fewer images than the minimum
- **WHEN** the deployment has `retain` images or fewer
- **THEN** retention removes none

#### Scenario: A young image
- **WHEN** an unprotected image is younger than `retain_min_age_seconds`
- **THEN** retention keeps it

### Requirement: Image operations are observable without secrets
The image path MUST write an append-only event file and an atomically written Prometheus textfile for build state, outcome timestamps, duration, console latency, consecutive failures, candidate waiting, staleness and leaked resources by kind. Events MUST carry only ids, 12-character digests, counts and stable reason codes. A failed-build event MAY carry a framed, byte-sanitized console tail.

#### Scenario: A build fails
- **WHEN** a build fails with a reason code
- **THEN** an `image_build_failed` event carries the code and the bounded tail

#### Scenario: A build succeeds
- **WHEN** a candidate becomes ready
- **THEN** an `image_candidate_ready` event and the metrics reflect it

#### Scenario: A leak is found
- **WHEN** reconcile removes a leaked resource
- **THEN** the leaked-resource metric for that kind increases

### Requirement: Image configuration is strict
The `[image]` tables MUST refuse unknown keys and out-of-range values with `CONFIG_INVALID` before any call. A phase 2 key MUST be accepted only when the code that uses it is present. Examples and tests MUST use synthetic names only.

#### Scenario: An unknown key
- **WHEN** `[image]` contains an unknown key
- **THEN** loading refuses with `CONFIG_INVALID`

#### Scenario: A value is out of range
- **WHEN** `max_builds_per_day` is 0 or 7
- **THEN** loading refuses with `CONFIG_INVALID`

#### Scenario: A basis path escapes the archive
- **WHEN** a basis path contains `..` or starts with `/`
- **THEN** loading refuses with `CONFIG_INVALID`

### Requirement: The timer is enabled only after manual qualification
The shipped `ci-burst-image` timer unit MUST be a one-shot container that mounts the configuration read-only, mounts the image state directory writable and the image role's profile directory read-only, and mounts no scaling credential and no scaling state. Documentation MUST state that the timer is enabled only after one manual build succeeded and about three clean promotions, and that the operator qualifies the unit with a refresh-script check.

#### Scenario: The unit is inspected
- **WHEN** a repository test reads the unit
- **THEN** it finds the three mounts above, no scaling credential mount and `--read-only`

#### Scenario: The timer is not yet qualified
- **WHEN** no manual build has succeeded
- **THEN** the documentation tells the operator not to enable the timer

#### Scenario: The timer fires during an active build
- **WHEN** a build is active
- **THEN** the pass advances it or refuses with `IMAGE_BUILD_ALREADY_ACTIVE` and starts no second builder
