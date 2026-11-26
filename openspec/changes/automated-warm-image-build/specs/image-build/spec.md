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
The command MUST compute the basis digest over the sorted lines of each basis path and its content hash, the hash of the rendered warm commands, gate command and `source.strip_components` value, and the hash of the shipped `warm-cache.sh`. It MUST NOT use the archive hash as the digest. A build is due when the digest differs from the `ci-burst:basis-digest` tag of the image behind the configured pin.

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
The command MUST stream the archive for the resolved sha through a hashing wrapper into a multipart upload under the configured bucket and prefix, never to disk. The size cap `source.max_archive_bytes` MUST abort the upload and delete the object. The row MUST record the bucket and key before the upload, and a stage done again MUST use them. A deletion MUST stay owed until it succeeds; the lifecycle rule is a backstop. The command MUST NOT create or configure the bucket.

#### Scenario: The key is recorded before the upload
- **WHEN** a pass stages the archive of a new build
- **THEN** the row names the bucket and the key before the multipart upload is created
- **AND** the row records `archive_sha256` only once the upload has finished

#### Scenario: A pass stops after the upload and before it records it
- **WHEN** the object is stored, the pass stops before it records the archive hash, and the next pass ends the row without staging it again (a withdrawn gate, a changed input or the build deadline)
- **THEN** the pass deletes the object that the row names and records `archive_deleted_at`
- **AND** no failure is counted for the deletion

#### Scenario: The prefix changes after a pass stopped
- **WHEN** the configured prefix or bucket changes between the pass that stored the object and the pass that ends the row
- **THEN** the object at the recorded bucket and key is deleted, and the listing of the new prefix is not what finds it

#### Scenario: A stage done again keeps the recorded key
- **WHEN** a pass stages again a row that records a bucket and a key, and the configured bucket or prefix differs
- **THEN** the upload, the presigned URL and the later delete all use the recorded bucket and key

#### Scenario: The lifecycle rule is a backstop only
- **WHEN** docs describe how a staged archive is removed
- **THEN** they name the deletion by the pass first and the bucket lifecycle rule a backstop only

#### Scenario: The delete at a terminal state is refused
- **WHEN** a build reaches a terminal state and the provider refuses the delete of its archive
- **THEN** the pass notes `archive:delete_failed` and the row keeps its terminal state
- **AND** the deletion stays owed, with `archive_deleted_at` empty, and the bucket lifecycle rule stays the backstop

#### Scenario: A backlog of owed deletions is bounded
- **WHEN** more archive deletions are owed than one pass may start, or refused deletions take the time that the pass leaves
- **THEN** the pass starts at most 8 deletions after its own work, and none once the pass deadline is less than 40 seconds away (the read timeout of the SDK client plus a margin), reading the clock before each
- **AND** the pass notes `archive:delete_deferred`, and a row whose deletion this pass attempted is not tried again in it, whatever the clock says

#### Scenario: The owed deletions come after the work of the pass
- **WHEN** a finished build owes an archive deletion that times out and the pass has a decision, a stage or a build to run
- **THEN** the pass does that work first, so the row insert and the stage fit the pass, and the owed deletion runs on the time that is left or is deferred

#### Scenario: A pass that ends on a refusal still retries the owed deletions
- **WHEN** a pass ends on a refused provider read, such as the listing of builders of an overdue build
- **THEN** the owed deletions still run in the tail of the pass, at the bucket and key that their rows record, and set `archive_deleted_at`
- **AND** the refusal that ended the pass is raised unchanged, and a failure of the tail replaces neither it nor the result

#### Scenario: A refused deletion takes its turn
- **WHEN** an owed deletion is refused
- **THEN** the row takes the next place in `archive_delete_seq`, one more than the largest place of any row, and records the attempt time (`archive_delete_tried_at`) for diagnosis only
- **AND** the next pass tries the rows never tried first, then the rows by `archive_delete_seq`, then the oldest
- **AND** a deletion that stays refused does not keep another owed row from being deleted, and a clock that steps back changes none of this

#### Scenario: A cancellation starts no deletion
- **WHEN** a pass is cancelled (a `BaseException` that is no `Exception`, such as `KeyboardInterrupt`) while an archive deletion is owed
- **THEN** no owed deletion starts and the cancellation propagates unchanged

#### Scenario: A refused or deferred deletion stays visible
- **WHEN** a pass ends on a refusal, or returns, and an owed deletion was refused or deferred
- **THEN** the pass writes an `image_reconciled` event with the counts `owed_archives_deleted`, `owed_archive_delete_failed` and `owed_archives_deferred`, and no key or bucket name
- **AND** a refusal that ended the pass is raised unchanged

#### Scenario: The archive exceeds the cap
- **WHEN** the streamed bytes pass `source.max_archive_bytes`
- **THEN** the multipart upload is aborted and the key is deleted
- **AND** the build is `failed` and no builder is launched

#### Scenario: The upload fails
- **WHEN** a part upload fails
- **THEN** the upload is aborted and no builder is launched

#### Scenario: The role moves to another account
- **WHEN** a pass runs in an account other than the `archive_owner` of a row, or the row records no owner, and a bucket of the recorded name exists in the new account
- **THEN** no S3 call is made about the archive of that row
- **AND** a stage done again ends the row `failed` with `IMAGE_STAGING_OWNER_CHANGED`, counts no failure and answers `refused`
- **AND** an owed deletion counts as failed, the row stays owed and is never marked deleted

#### Scenario: A staging call is bound to the account that owns the bucket
- **WHEN** a pass makes an S3 call against a staging bucket: the upload, its abort, a delete or a listing
- **THEN** the call names the account of the image identity as the expected bucket owner, read once per pass from `GetCallerIdentity`, and no call goes out without it
- **AND** the row records that account as `archive_owner` with the bucket and the key, and a stage done again and every delete of the archive name the recorded owner
- **AND** an `AccessDenied` answer sends nothing more and gives `IMAGE_STAGING_BUCKET_DENIED`: when neither deadline has passed as it returns, a stage done again ends its row as a refused start with no failure counted and a first stage fails the build; a refused delete counts as failed and stays owed

#### Scenario: The bucket does not exist
- **WHEN** S3 answers `NoSuchBucket` to the upload of an archive, for instance because the bucket that a row recorded was removed
- **THEN** the upload ends with `IMAGE_STAGING_BUCKET_MISSING`
- **AND** when neither deadline has passed as the answer returns, a stage done again ends its row as a refused start with no failure counted, keeps the archive fields of the row and marks it deleted only when the delete of its archive succeeds
- **AND** when neither deadline has passed as the answer returns, a first stage fails the build; an owed deletion at that bucket counts as failed and stays owed

#### Scenario: The build ends
- **WHEN** a build whose row records an archive key reaches `candidate` or `failed`
- **THEN** the pass deletes the object at the recorded bucket and key and records `archive_deleted_at`

### Requirement: The archive key joins the prefix and the build id with one slash
The archive key MUST be the configured prefix, exactly one `/` and `<build-id>.tar.gz`, whether the configured prefix ends with no slash, one slash or several. The listing of stray archives MUST use the same prefix with its one slash, so that it never reaches a sibling prefix.

#### Scenario: A prefix without a trailing slash
- **WHEN** the prefix is `example-prefix`
- **THEN** the archive key is `example-prefix/<build-id>.tar.gz`

#### Scenario: A prefix with trailing slashes
- **WHEN** the prefix is `example-prefix//`
- **THEN** the archive key is `example-prefix/<build-id>.tar.gz`

#### Scenario: A sibling prefix
- **WHEN** an object older than one hour sits under `example-prefix-other/` and the prefix is `example-prefix`
- **THEN** reconcile does not list or delete it

### Requirement: Credentials and URLs are not persisted
The presigned archive URL, tokens, rendered user data and raw provider payloads MUST NOT appear in logs, events, the state file, command output or error messages. The command MUST NOT presign when the credential session has less remaining life than `source.presign_seconds` and MUST refuse with `IMAGE_CREDENTIAL_SHORT`, unless a deadline has passed first. The URL MUST exist only in the rendered user data in memory.

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
The builder MUST launch only from the build launch template at its pinned version, with no instance profile, IMDSv2 required with hop limit 1, terminate on instance-initiated shutdown, delete-on-termination storage, and security groups with no inbound rule. `plan` and a `run` pass about to start a build MUST read the template and its security groups, and MUST refuse with `IMAGE_BUILD_TEMPLATE_UNSAFE` when any property differs. A builder MUST be on-demand and MUST NOT chain from the warm image.

#### Scenario: An unsafe template
- **WHEN** the build template carries an instance profile or allows IMDSv1, or a security group that it names has an inbound rule
- **THEN** the command refuses with `IMAGE_BUILD_TEMPLATE_UNSAFE` and launches nothing

#### Scenario: No safe group is named
- **WHEN** the template names no security group, a network interface names none, or a named group name matches no group
- **THEN** the command refuses with `IMAGE_BUILD_TEMPLATE_UNSAFE` and launches nothing
- **AND** the detail names `security_groups` and no group id, name or rule

#### Scenario: Groups are read wherever the template names them
- **WHEN** the template names groups in `SecurityGroupIds`, in `SecurityGroups` or in the `Groups` of a network interface
- **THEN** the precheck reads every one of them and refuses when any has an inbound rule

#### Scenario: The groups cannot be read
- **WHEN** a named group id is unknown, or the image role lacks `ec2:DescribeSecurityGroups`
- **THEN** the read fails and the command refuses with the provider's refusal
- **AND** no build starts

#### Scenario: A pass that only advances a build
- **WHEN** a pass advances or ends an active build and does not stage its archive again
- **THEN** it does not run the precheck and does not read the security groups

#### Scenario: A pass that stages a build again
- **WHEN** a pass stages again a build that has no builder and no staged archive
- **THEN** it runs the precheck again before it uploads anything
- **AND** an unsafe template ends the row as `failed` with `IMAGE_BUILD_TEMPLATE_UNSAFE`, tries to delete its archive, answers `refused` and counts no failure

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
- **WHEN** a pass starts while another pass still holds the state file's writer lock
- **THEN** it refuses with `IMAGE_STATE_LOCKED` and exits 2
- **AND** it makes no provider call and starts no second builder

#### Scenario: A pass finds an active build
- **WHEN** a pass starts after the earlier pass ended and a build is `building` or `imaging`
- **THEN** it reconciles and advances that build instead of deciding on a new one
- **AND** it starts no second builder

#### Scenario: Two active rows
- **WHEN** a test inserts a second active row for the deployment
- **THEN** the insert fails on the unique index

#### Scenario: The state file lacks the running builder
- **WHEN** a tagged builder exists and the state file has no active row
- **THEN** the AWS precheck refuses the launch

### Requirement: A refused start is not a failed build
A refused start MUST NOT count as a failed build. The refusal MUST add no failure, start no cooldown and set no `blocked`, and a clearing that the trigger decision already made MUST stay. A refusal before the build row exists MUST write no row and no archive. A refusal once the row exists, with neither deadline passed, MUST end the row as `failed` with the refusal code and MUST try to delete its archive. Such a row MUST still count toward `max_builds_per_day` and `min_build_interval_seconds`.

#### Scenario: A check refuses before the row exists
- **WHEN** the session has less life than `presign_seconds`, or a builder of the deployment exists, before the build row is written
- **THEN** the pass answers `refused` with `IMAGE_CREDENTIAL_SHORT` or `IMAGE_BUILD_ALREADY_ACTIVE`
- **AND** it writes no build row and no archive and counts no failure

#### Scenario: The session runs short after the pass deadline, before the row exists
- **WHEN** the read of the build template of a new start ends at or after the pass deadline, and the session then has less life than `presign_seconds`
- **THEN** the pass ends with `pass_deadline_exceeded`, not `refused`, and writes no build row and no archive

#### Scenario: The template precheck refuses
- **WHEN** the template precheck refuses a start, and the read that refused it returns before the pass deadline
- **THEN** the pass ends with that refusal, writes no build row and no archive and counts no failure

#### Scenario: A clearing comes before the refusal
- **WHEN** a forced run, or a changed digest, clears `blocked` and the cooldown and the start is then refused
- **THEN** the clearing stays
- **AND** the refusal adds no failure and starts no cooldown

#### Scenario: The session runs short during the upload
- **WHEN** the build row exists and the session has less life than `presign_seconds` once the archive is staged, and neither deadline has passed when the presign refuses
- **THEN** the row ends `failed` with `IMAGE_CREDENTIAL_SHORT`, the pass tries to delete its archive and no builder is launched
- **AND** the pass answers `refused` and counts no failure

#### Scenario: The session runs short and only the pass deadline has passed
- **WHEN** the upload ends at or after the pass deadline and before the build deadline, and the session has less life than `presign_seconds`
- **THEN** the launch presigns nothing and no builder is launched, and the row stays `building` with no builder and no `archive_sha256`
- **AND** the pass counts no failure, keeps the archive, and the next pass stages the build again

#### Scenario: The session runs short and the build deadline has passed
- **WHEN** the upload ends at or after the build deadline, and the session has less life than `presign_seconds`
- **THEN** the launch presigns nothing, no builder is launched and the row ends `failed` with `CONSOLE_BLIND`, not `IMAGE_CREDENTIAL_SHORT`
- **AND** the pass tries to delete the archive that the row names

#### Scenario: The session runs short during the checks of a resumed stage and only the pass deadline has passed
- **WHEN** a pass stages again a build, the read of the build template ends at or after the pass deadline and before the build deadline, and the session then has less life than `presign_seconds`
- **THEN** the pass checks no credential and launches nothing, and the row stays `building` with no builder and no `archive_sha256`, not `failed` with `IMAGE_CREDENTIAL_SHORT`
- **AND** the pass counts no failure, deletes no archive, and the next pass stages the build again

#### Scenario: The session runs short during the checks of a resumed stage and the build deadline has passed
- **WHEN** a pass stages again a build, the read of the build template ends at or after the build deadline, whether or not the pass deadline has passed, and the session then has less life than `presign_seconds`
- **THEN** no builder is launched and the row ends `failed` with `CONSOLE_BLIND`, not `IMAGE_CREDENTIAL_SHORT`
- **AND** the pass counts the failure and tries to delete the archive that the row names

#### Scenario: A builder appears before the launch
- **WHEN** a builder of the deployment exists at the check before the launch call, and the build deadline has not passed when that check starts
- **THEN** the row ends `failed` with `IMAGE_BUILD_ALREADY_ACTIVE`, the pass tries to delete its archive and no builder is launched
- **AND** the pass answers `refused`, counts no failure and leaves that builder to the next pass's reconcile

#### Scenario: A resumed stage is refused
- **WHEN** a pass stages again a build that has no builder and no staged archive, the check before the upload refuses, and neither deadline has passed when the check returns
- **THEN** the row ends `failed` with the refusal code and the pass tries to delete any archive that the row names
- **AND** no new upload is made and no failure is counted

#### Scenario: A refused row counts toward the limits
- **WHEN** a build row has ended by a refusal
- **THEN** it counts toward `max_builds_per_day` and `min_build_interval_seconds` like any other row

### Requirement: A deadline decides before the outcome of a call that takes time
A deadline MUST decide before the outcome of any call that takes time. In a new start, a stage done again and the launch, the pass MUST read both deadlines again when the read of the template, the lookup of the credential, the upload, the presign, the listing of the builders or the inputs or gate check of a stage done again returns, before it inserts a row, uploads, launches or ends a row. The build deadline MUST decide first. This requirement does not decide a listing that shows a builder.

#### Scenario: A late listing of a new start writes no row
- **WHEN** the listing of the builders in the start checks of a new start shows no builder and returns at or after the pass deadline
- **THEN** the pass ends with `pass_deadline_exceeded`, not `refused`, and writes no build row and no archive
- **AND** no upload starts and no failure is counted

#### Scenario: A late refusal of the listing in a new start
- **WHEN** the provider refuses the listing of the builders in the start checks of a new start, and the refusal returns at or after the pass deadline
- **THEN** the pass ends with `pass_deadline_exceeded`, not with the code of the provider, and writes no build row and no archive
- **AND** no upload starts and no failure is counted

#### Scenario: A late lookup of the credential in a new start
- **WHEN** the lookup of the credential in the start checks of a new start returns at or after the pass deadline, whether it answers or refuses with `IMAGE_CREDENTIAL_SHORT`
- **THEN** the pass ends with `pass_deadline_exceeded`, not `refused`, and writes no build row and no archive

#### Scenario: The checks of a new start return before the pass deadline
- **WHEN** the lookup of the credential and the listing of the builders in the start checks of a new start return before the pass deadline
- **THEN** the checks answer as they find: the build row is written, the start is refused with `IMAGE_CREDENTIAL_SHORT`, or the pass ends with the refusal that the provider gave to the listing

#### Scenario: A late listing before the upload of a resumed stage, the pass deadline only
- **WHEN** a pass stages again a build, and the listing of the builders in the start checks returns at or after the pass deadline and before the build deadline, whether it shows no builder or the provider refuses it
- **THEN** no upload starts and the row stays `building` with no builder and no `archive_sha256`
- **AND** the pass counts no failure, and the next pass stages the build again

#### Scenario: A late listing before the upload of a resumed stage, the build deadline
- **WHEN** a pass stages again a build, and the listing of the builders in the start checks returns at or after the build deadline, whether it shows no builder or the provider refuses it, and whether or not the pass deadline has passed
- **THEN** no upload starts and the row ends `failed` with `CONSOLE_BLIND`, whatever S3 would answer to the upload
- **AND** the pass counts the failure and tries to delete the archive that the row names

#### Scenario: A late lookup of the credential in a resumed stage
- **WHEN** a pass stages again a build, and the lookup of the credential returns, with a session that has less life than `presign_seconds`, at or after a deadline
- **THEN** at or after the build deadline the row ends `failed` with `CONSOLE_BLIND` and the pass counts the failure
- **AND** at or after the pass deadline only, the row stays `building`, the pass counts no failure and deletes no archive
- **AND** no upload starts and no builder is launched

#### Scenario: A late presign
- **WHEN** the lookup of the credential in the presign of the launch returns at or after a deadline, whether it answers or refuses with `IMAGE_CREDENTIAL_SHORT`
- **THEN** no builder is launched and the listing of the builders is not asked for
- **AND** at or after the build deadline the row ends `failed` with `CONSOLE_BLIND`
- **AND** at or after the pass deadline only, the row stays `building` with no `archive_sha256`, and its archive is kept

#### Scenario: A late refusal of the upload
- **WHEN** the upload of a stage done again ends with a refusal that S3 gave for the bucket, at or after the build deadline
- **THEN** the row ends `failed` with `CONSOLE_BLIND`, not with the code of the bucket, and the pass counts the failure

#### Scenario: A late unsafe template in a new start
- **WHEN** the read of the build template of a new start shows an unsafe template at or after the pass deadline
- **THEN** the pass ends with `pass_deadline_exceeded`, not with `IMAGE_BUILD_TEMPLATE_UNSAFE`, and writes no build row and no archive

#### Scenario: A late unsafe template in a resumed stage
- **WHEN** the read of the build template of a stage done again shows an unsafe template at or after the build deadline
- **THEN** the row ends `failed` with `CONSOLE_BLIND`, not with `IMAGE_BUILD_TEMPLATE_UNSAFE`, and the pass counts the failure

#### Scenario: A late refusal of the last listing before the launch
- **WHEN** the provider refuses the last listing of the builders before the launch call, and the refusal returns at or after a deadline
- **THEN** no builder is launched
- **AND** at or after the build deadline the row ends `failed` with `CONSOLE_BLIND`, not with the code of the provider, and the pass counts the failure
- **AND** at or after the pass deadline only, the row stays `building` with no `archive_sha256`, its archive is kept and the pass counts no failure

#### Scenario: The last listing before the launch is refused inside both deadlines
- **WHEN** the provider refuses the last listing of the builders before the launch call, and the refusal returns before both deadlines
- **THEN** no builder is launched and the row ends `failed` with the code of the provider

### Requirement: A stage done again launches only under the inputs of its row
The row MUST record the hash of each basis file when it is inserted. A pass that stages a build again MUST recompute the digest from those hashes, the warm and gate commands, `strip_components` and the script, and MUST read no basis file. With neither deadline passed as the check returns, a different digest or set of `basis_paths` MUST end the row `failed` with `IMAGE_INPUTS_CHANGED` as a refused start, and a row without valid hashes MUST raise `IMAGE_STATE_UNREADABLE`.

#### Scenario: The gate command changes before the stage is done again
- **WHEN** a build row is inserted with a gate command, the pass deadline ends its stage and the configuration then holds no gate command
- **THEN** the next pass ends the row `failed` with `IMAGE_INPUTS_CHANGED` and launches no builder
- **AND** the pass answers `refused` and counts no failure

#### Scenario: The script or a warm command changes
- **WHEN** the warm script or a warm command differs from the one that gave the row's digest when the stage is done again
- **THEN** the row ends `failed` with `IMAGE_INPUTS_CHANGED` and no builder is launched

#### Scenario: The builder lifetime changes while a stage waits
- **WHEN** only `lifetime_seconds` or `snapshot_seconds` changed since the row was inserted
- **THEN** the digest still matches, and a stage that passes its launch checks renders those values from the configuration of the resumed pass

#### Scenario: A staged archive is removed
- **WHEN** the row names an archive and the check finds that the inputs changed
- **THEN** the pass tries to delete that archive and makes no new upload
- **AND** a refused delete is noted `archive:delete_failed` and the deletion is unconfirmed

#### Scenario: A stage done again reads no basis file
- **WHEN** a pass stages again a row whose inputs are unchanged, and the forge would refuse or delay every read of a basis file
- **THEN** the pass reads no basis file, stages the archive of the row's commit and launches the builder

#### Scenario: The digest formula is the one of the decision
- **WHEN** the digest is computed from the recorded file hashes
- **THEN** it equals the digest of the files that the decision hashed, so the tags of the image stay compatible

#### Scenario: The set of basis paths changes
- **WHEN** the configured `basis_paths` gained or lost a path since the row was inserted
- **THEN** the row ends `failed` with `IMAGE_INPUTS_CHANGED` without a forge read, counts no failure and launches no builder
- **AND** when a read before the check, such as the pin read, returned at or after the pass deadline and before the build deadline, the row stays `building` for the next pass instead

#### Scenario: The row records no valid hashes
- **WHEN** a pass stages again a row whose `basis_file_hashes` is empty or malformed, and neither deadline has passed
- **THEN** the pass raises `IMAGE_STATE_UNREADABLE`, reads no file of the forge and leaves the row as it is

#### Scenario: The branch head moves
- **WHEN** the protected branch head moves after the row was inserted and the files at the row's commit still give its digest
- **THEN** the pass stages the archive of the row's commit and launches the builder

#### Scenario: The next pass decides from the current inputs
- **WHEN** a row has ended with `IMAGE_INPUTS_CHANGED`
- **THEN** the next pass decides from the current inputs like after any ended row
- **AND** the ended row counts toward `max_builds_per_day` and `min_build_interval_seconds`

### Requirement: Recorded hashes are valid only as the configuration could have produced them
Valid hashes MUST name each path once, with a lower-case hex sha256, and MUST name only paths that `basis_paths` could hold: non-empty, no control character, no `..`, no leading `/`, text that UTF-8 encodes, and at most as many paths as it takes. A row whose hashes are not valid MUST raise `IMAGE_STATE_UNREADABLE`, MUST stay as it is and MUST NOT end as `IMAGE_INPUTS_CHANGED`. The insert MUST refuse hashes that are not valid. The error MUST name the row and MUST NOT name a path or a value.

#### Scenario: The row records a path that `basis_paths` could never hold
- **WHEN** a pass stages again a row whose `basis_file_hashes` names an empty path, or a path with a control character, a `..` or a leading `/`, or names more paths than `basis_paths` takes
- **THEN** the pass raises `IMAGE_STATE_UNREADABLE` and does not end the row as `IMAGE_INPUTS_CHANGED`
- **AND** the pass reads no file of the forge, deletes no archive, launches no builder and leaves the row as it is
- **AND** the error names the row and never the path

#### Scenario: The row records a path that is not text in UTF-8
- **WHEN** a pass stages again a row whose `basis_file_hashes` names a path with a lone surrogate, which the TOML file of `basis_paths` cannot spell
- **THEN** the pass raises `IMAGE_STATE_UNREADABLE` and does not end the row as `IMAGE_INPUTS_CHANGED`
- **AND** the pass reads no file of the forge, deletes no archive, launches no builder and leaves the row as it is
- **AND** the error names the row and never the path

#### Scenario: The row records one path twice
- **WHEN** a pass stages again a row whose `basis_file_hashes` holds two members with one name, whatever their values
- **THEN** the pass raises `IMAGE_STATE_UNREADABLE`, launches no builder and leaves the row as it is
- **AND** the error names the row and never a value

#### Scenario: The insert refuses hashes that are not valid
- **WHEN** a row is inserted with hashes that name a path that `basis_paths` could never hold, a path that is not text in UTF-8 included
- **THEN** the insert is refused and no row is written

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

#### Scenario: The pass deadline ends the staging
- **WHEN** the pass deadline passes during the checks before the upload of a resumed stage (the template precheck and the start checks), during the upload, or before the launch call, and the build deadline has not passed by then
- **THEN** the row stays `building` with no builder and no `archive_sha256`
- **AND** the next pass stages again under the same archive key and no failure is counted

#### Scenario: An unstaged build reaches its deadline
- **WHEN** the build deadline has passed and the row has no builder and no `archive_sha256`
- **THEN** the build fails with `CONSOLE_BLIND` and no builder is launched
- **AND** the pass tries to delete any archive that the row names

#### Scenario: Until-done without apply
- **WHEN** an operator passes `--until-done` without `--apply`
- **THEN** the command refuses with exit 2 and does nothing

### Requirement: Provider reads are bounded by the pass deadline
Every provider read of a pass, except the read that confirms a deregistration, MUST be bounded by the pass deadline. A read that would start at or after the deadline MUST NOT be made. Where the pass advances or stages a build, it MUST then note `pass_deadline_exceeded` and leave the row as its last write left it. A refused read of the base image or of the build's image, the deadline included, MUST NOT delay `CreateImage`: the pass notes `base_image:<reason>` or `image_lookup:<reason>`.

#### Scenario: The deadline arrives while a build is advanced
- **WHEN** the pass deadline is reached before a read of an advancing build, the image read and the template read-back included
- **THEN** the read is not made and the pass notes `pass_deadline_exceeded`
- **AND** a later pass resumes from the row

#### Scenario: The base image cannot be read in time
- **WHEN** the pass deadline is reached before the read of the base image in the pass that first sees the marker
- **THEN** `CreateImage` is still called, without the base image tag
- **AND** the pass notes `base_image:pass_deadline_exceeded`

#### Scenario: The base image read is refused
- **WHEN** the provider refuses the read of the base image in the pass that first sees the marker
- **THEN** `CreateImage` is still called, without the base image tag
- **AND** the pass notes `base_image:` followed by the reason code of the refusal

#### Scenario: The lookup of the build's image is refused
- **WHEN** the provider refuses the lookup of the build's image in the pass that first sees the marker, or the pass deadline is reached before it
- **THEN** `CreateImage` is still called, as if the lookup had found no image
- **AND** the pass notes `image_lookup:` followed by the reason code of the refusal

#### Scenario: A deletion path meets the deadline
- **WHEN** the pass deadline is reached before a deletion path, or the deletion plan of `plan`, has read what it needs
- **THEN** it deletes nothing and its result is `refused` with `pass_deadline_exceeded`

#### Scenario: A read before the build is reached meets the deadline
- **WHEN** a read of `plan` before its deletion plan, or a reconcile read or the pin read of a `run` pass, would start at or after the deadline
- **THEN** the command refuses with `pass_deadline_exceeded`

### Requirement: A cutoff is read where a provider call starts
A provider call starts where the AWS client calls the SDK operation. The AWS client, or the pass with no other call in between, MUST read each cutoff of the call right before that point and MUST NOT call the operation at or after it. In a stage, each template read, listing page, upload part and presign MUST take the earlier of the two deadlines. Signing and a credential refresh inside the SDK can delay the request after that check, and no cutoff bounds that delay.

#### Scenario: A cutoff passes before the SDK operation is called
- **WHEN** a cutoff passes while the AWS client builds its client, looks up the account, or looks up the credential for a presign that the session outlives
- **THEN** the client does not call the SDK operation and refuses with `pass_deadline_exceeded`, whichever cutoff passed

### Requirement: Basis reads are bounded by the pass deadline
A read of a basis file MUST NOT start at or after the pass deadline: `plan` and the decision of a `run` pass MUST read the clock before each read, and MUST refuse with `pass_deadline_exceeded`. A stage done again reads no basis file. The forge client puts no deadline on a request, so a read that has started MAY end after the deadline.

#### Scenario: The deadline passes between two basis reads of a plan
- **WHEN** the pass deadline passes during the read of one basis file and `plan` has another basis path to read
- **THEN** `plan` refuses with `pass_deadline_exceeded`
- **AND** it reads no further file, probes no archive and builds no provider client

#### Scenario: The deadline passes between two basis reads of a decision
- **WHEN** the pass deadline passes during the read of one basis file in the decision of a `run` pass that has another basis path to read
- **THEN** the command refuses with `pass_deadline_exceeded`
- **AND** it reads no further file and writes no row

#### Scenario: Slow basis reads do not starve a stage done again
- **WHEN** reading the basis files and uploading the archive each fit in one pass but not together
- **THEN** a stage done again makes no basis read, so the upload has the whole pass and the build launches

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
When no marker appears by `build_deadline_seconds`, the command MUST terminate the builder, MUST create no image and MUST record `CONSOLE_BLIND` when the console is empty or unreadable, or `MARKER_TIMEOUT` when the console is readable. The command MUST NOT call `CreateImage` or launch a builder at or after that deadline, and MUST read the clock after its last read and right before each call.

#### Scenario: The console is blind
- **WHEN** the console is empty at the deadline
- **THEN** the builder is terminated, no image is created and the reason is `CONSOLE_BLIND`

#### Scenario: The console is readable but has no marker
- **WHEN** the console has output and no marker at the deadline
- **THEN** the builder is terminated and the reason is `MARKER_TIMEOUT`

#### Scenario: A marker first read after the deadline, the builder ended
- **WHEN** reconcile ended the builder of an overdue build and the pass reads the marker for the first time
- **THEN** no image is created
- **AND** the build fails with `IMAGE_BUILDER_GONE`

#### Scenario: A marker first read after the deadline, the builder alive
- **WHEN** the provider refused reconcile's termination of the builder of an overdue build and the pass reads the marker for the first time
- **THEN** no image is created
- **AND** the build fails with `MARKER_TIMEOUT` and the pass tries again to end the builder

#### Scenario: The deadline comes during the console read
- **WHEN** the build deadline passes during the read of the console that shows the marker, and the builder is alive
- **THEN** no image is created and the build fails with `MARKER_TIMEOUT`
- **AND** the latency of the marker is the time at the end of the read

#### Scenario: The deadline comes during a read before the call
- **WHEN** the build deadline passes during the lookup of the build's image, which lists no image, or during the read of the base image, after the pass has read the marker
- **THEN** no image is created and the build fails with `MARKER_TIMEOUT`
- **AND** the failure path ends the builder

#### Scenario: A resumed stage outlasts the deadline
- **WHEN** the build deadline passes during a read of the template check of a resumed stage, during its upload, or during the calls between the upload and the launch call (the presign and the listing of the builders unless it shows a builder)
- **THEN** no later call of the stage starts, no builder is launched and the build fails with `CONSOLE_BLIND`
- **AND** the pass tries to delete the archive that the row names

#### Scenario: A resumed stage outlasts both deadlines
- **WHEN** a call of a resumed stage ends at or after the build deadline and at or after the pass deadline: a read in the checks before the upload (the template, the credential, the listing of the builders unless it shows a builder), a wait in the stream of the upload, the completion of the upload, the presign, or the last listing of the builders before the launch call unless it shows a builder
- **THEN** no builder is launched and the build fails with `CONSOLE_BLIND` in that pass, and the row is not left for the next pass to stage again
- **AND** the pass tries to delete the archive that the row names

#### Scenario: A resumed stage outlasts only the pass deadline
- **WHEN** a call of a resumed stage ends at or after the pass deadline and before the build deadline: a read in the checks before the upload (the template, the credential, the listing of the builders unless it shows a builder), a wait in the stream of the upload, the completion of the upload, the presign, or the last listing of the builders before the launch call unless it shows a builder
- **THEN** no builder is launched and the row stays `building` with no builder and no `archive_sha256`
- **AND** the next pass stages again and no failure is counted

#### Scenario: A failed build recovers
- **WHEN** the basis digest changes or an operator runs `run --apply --force`
- **THEN** a new build MAY start once the cooldown and the daily cap permit

### Requirement: A gate command is required
`image.warm.gate_command` MUST be non-empty unless `allow_no_gate = true`. Otherwise the command MUST refuse with `IMAGE_GATE_REQUIRED`, except as the next requirement states. The candidate record MUST show the gate setting that the builder was launched with, not the one that the configuration holds when the build ends, and only a recorded 1 MUST count as a gate that ran. Documentation MUST state that the offline gate proves cache behavior and does not prove that the image is safe.

#### Scenario: No gate and no opt-out
- **WHEN** the gate command is empty, `allow_no_gate` is false and no build is in flight
- **THEN** the command refuses with `IMAGE_GATE_REQUIRED` and exits 2
- **AND** no client is built

#### Scenario: The opt-out is recorded
- **WHEN** `allow_no_gate` is true and the gate command is empty
- **THEN** the build proceeds and the candidate record states that no gate ran

#### Scenario: A gate is gained while a build runs
- **WHEN** a build is launched with no gate command and the configuration holds a gate command when the build ends
- **THEN** the candidate record states that no gate ran
- **AND** `promote --write` refuses with `PROMOTE_GATE_NOT_PASSED` while `allow_no_gate` is false

#### Scenario: A gate is dropped while a build runs
- **WHEN** a build is launched with a gate command and the configuration holds no gate command when the build ends
- **THEN** the candidate record states that the gate ran and passed

#### Scenario: The recorded gate setting is not 1
- **WHEN** a build is launched without a gate command, its row then records a gate setting other than the integer 1, for example 2, and the build ends
- **THEN** the candidate record states that no gate ran
- **AND** `promote --write` refuses with `PROMOTE_GATE_NOT_PASSED` while `allow_no_gate` is false

### Requirement: A missing gate refuses a launch and does not strand a build
The gate requirement MUST guard a launch. `plan`, and an applying pass with no build in flight and no archive deletion owed, MUST refuse with `IMAGE_GATE_REQUIRED` before any client is built. An applying pass with either MUST run and settle it, and MUST refuse each launch with `IMAGE_GATE_REQUIRED`, counting no failure. A pass that starts with no build in flight MUST run the bounded owed deletions only, then answer `refused`.

#### Scenario: A build in flight is settled with no gate command and no opt-out
- **WHEN** a build is `building` or `imaging` and the configuration then holds no gate command and no opt-out
- **THEN** an applying pass runs, settles the build and starts no other
- **AND** the candidate record shows the gate setting that the builder was launched with

#### Scenario: A stage that waits for its next pass meets a file with no gate
- **WHEN** a row is inserted with a gate command, the pass deadline ends its stage and the configuration then holds no gate command and no opt-out
- **THEN** the next pass ends the row `failed` with `IMAGE_INPUTS_CHANGED`, tries to delete its archive and starts no new build
- **AND** the pass answers `refused` and counts no failure

#### Scenario: The opt-out is withdrawn while a stage waits
- **WHEN** a row is inserted under `allow_no_gate = true` with no gate command, the pass deadline ends its stage and the configuration then holds no opt-out
- **THEN** the next pass ends the row `failed` with `IMAGE_GATE_REQUIRED`, tries to delete its archive and launches no builder
- **AND** the pass answers `refused` and counts no failure

#### Scenario: An owed archive deletion is settled with no gate command and no opt-out
- **WHEN** no build is in flight, a finished build owes the deletion of its archive and the configuration holds no gate command and no opt-out
- **THEN** an applying pass runs, deletes the archive and starts no build
- **AND** the pass answers `refused` with `IMAGE_GATE_REQUIRED`, reads no file of the forge and counts no failure

#### Scenario: A cleanup pass does not depend on other provider reads
- **WHEN** no build is in flight at the start of a pass, the configuration holds no gate command and no opt-out, a finished build owes an archive deletion and the provider refuses the pin read and the listings
- **THEN** the pass deletes the archive, records `archive_deleted_at` and answers `refused` with `IMAGE_GATE_REQUIRED`
- **AND** it reads no pin, builders, stray archives, images, retention or forge file, and the pin fields that it did not read and `retention` are empty in the result
- **AND** the command line stamps no pin evidence after it

#### Scenario: A new build is refused under a withdrawn gate
- **WHEN** a pass runs because a build was in flight, that build ended before the pass took the lock, and the configuration holds no gate command and no opt-out
- **THEN** the pass answers `refused` with `IMAGE_GATE_REQUIRED`, writes no build row and makes no upload

#### Scenario: The pass after the settled build is refused
- **WHEN** the build has ended and the configuration still holds no gate command and no opt-out
- **THEN** the next pass refuses with `IMAGE_GATE_REQUIRED` before any client is built

#### Scenario: A plan is refused while a build is in flight
- **WHEN** `plan` runs with no gate command and no opt-out while a build is in flight
- **THEN** it refuses with `IMAGE_GATE_REQUIRED`

#### Scenario: The build ends before the pass takes the lock
- **WHEN** an applying pass found a build in flight, the build ended before the pass took the state lock, and the pass then reaches the start of a new build
- **THEN** the start is refused with `IMAGE_GATE_REQUIRED` before the row and the upload, and the pass answers `refused`

### Requirement: The image is created in the pass that sees the marker
The pass that first sees the marker MUST call `CreateImage` before any step that is not a read, with no reboot, and MUST tag the image and its snapshots with `ci-burst:image-build`, the deployment, the build id, the basis digest, the source sha, the script sha and, when read, the base image. The pass MUST NOT leave the builder running after the image is `available` or after the snapshot window or lifetime timer ends it. Waiting for the image and every later step MAY continue in a later pass.

#### Scenario: A slow tick
- **WHEN** the pass that sees the marker is the last pass inside the snapshot window
- **THEN** `CreateImage` is called in that pass
- **AND** the build does not fail with `IMAGE_BUILDER_GONE`

#### Scenario: The builder is gone
- **WHEN** the builder no longer exists when `CreateImage` would be called
- **THEN** the build fails with `IMAGE_BUILDER_GONE` and the cooldown starts

#### Scenario: The image stays pending
- **WHEN** the image is not `available` by the deadline
- **THEN** the build is `failed` with `IMAGE_NOT_AVAILABLE`
- **AND** the image and its snapshots are removed only under the image deletion rule

### Requirement: An uncertain CreateImage never makes a second image
A `CreateImage` call with an unknown outcome MUST NOT fail the build before the build deadline or lead to a second image. Before every call the command MUST adopt an image that carries the build id instead of calling. Every call for a build MUST use one image name, and the provider's duplicate-name refusal MUST count as an unknown outcome. If the first pass after the deadline lists no image of the build, the build MUST fail.

#### Scenario: The image is listed by the next pass
- **WHEN** a call ends with an unknown outcome, and the image exists and is listed when the next pass reconciles
- **THEN** reconcile adopts the image and the build moves to `imaging`
- **AND** `CreateImage` has been called once

#### Scenario: The image is listed after reconcile
- **WHEN** the image is not listed when reconcile runs, and is listed when the pass looks for it before its call
- **THEN** the pass records the image id, moves the build to `imaging` and writes the `image_created` event
- **AND** the pass does not call `CreateImage` again

#### Scenario: The lookup lists the image after the deadline passed
- **WHEN** the build deadline passes during the lookup of the build's image, and the lookup lists the image
- **THEN** the pass adopts the image, because adopting is no call, and the build moves to `imaging`
- **AND** `CreateImage` is not called

#### Scenario: The image is not listed yet
- **WHEN** the image exists and is not listed, and a pass calls `CreateImage` under the same name
- **THEN** the provider refuses the call as a duplicate name, and the refusal is an unknown outcome
- **AND** the build stays `building`, the pass notes `create_image:` followed by the reason code and no failure is counted
- **AND** a later pass adopts the image once it is listed, with no further call

#### Scenario: The unknown outcome lasts to the deadline
- **WHEN** every call ends with an unknown outcome until the build deadline passes, and the first pass after the deadline lists no image of the build
- **THEN** reconcile ends the builder and the build fails with `IMAGE_BUILDER_GONE`, with no image recorded
- **AND** an image that is listed later carries the failed build's id, so it is no orphan, and the image deletion rule removes it with its snapshots

#### Scenario: The image is first listed after the deadline
- **WHEN** a call before the deadline ended with an unknown outcome, and the image is first listed when the first pass after the deadline reconciles
- **THEN** reconcile ends the overdue builder and adopts the image, because adopting is no call
- **AND** the build goes on from `imaging` and `CreateImage` has been called once

#### Scenario: A call outlives the deadline
- **WHEN** a call ends with an unknown outcome at or after the build deadline
- **THEN** the build fails with the code of the refusal and the failure path ends the builder

### Requirement: Reconcile runs first and cleans leaks
Every pass MUST reconcile before any transition; its first call MUST end the builder of an active build past its build deadline, as a non-leak. Reconcile MUST then terminate every other builder-tagged instance, delete objects under the prefix that are not the active archive and are older than one hour, within the bound of the next requirement, and, before any start, clean failed builds' images only under the image deletion rule. Owed archive deletions MUST run last, on the time the pass leaves.

#### Scenario: A leaked builder
- **WHEN** a builder is tagged for the deployment and the state file has no matching active build
- **THEN** reconcile terminates it before any other step

#### Scenario: A stray archive
- **WHEN** an object older than one hour sits under the prefix and is not the active archive, and the sweep has not reached its bound (see the next requirement)
- **THEN** reconcile deletes it

#### Scenario: The sweep leaves an owed archive to the bounded retry
- **WHEN** an object older than one hour sits under the prefix and a finished build owes its deletion, whether the pass retried it, it was refused or the bound deferred it
- **THEN** the sweep does not delete it, and only the owed retry does, within its bound
- **AND** a stray object that no row names is still deleted by the sweep

#### Scenario: An overdue builder
- **WHEN** the build deadline has passed and the active build has a builder
- **THEN** the first provider call of the pass terminates that builder
- **AND** the leaked-resource metric for `builder` does not increase

#### Scenario: An overdue imaging builder
- **WHEN** the build is `imaging`, its image is still `pending` and the build deadline has passed
- **THEN** reconcile terminates the builder, as the builder's own lifetime timer would
- **AND** the build ends `failed` with `IMAGE_NOT_AVAILABLE`, or `candidate` when the image is `available` when the pass reads it

#### Scenario: The early termination is refused
- **WHEN** the termination of an overdue builder is refused
- **THEN** reconcile reports the refusal and the pass goes on
- **AND** the state machine asks for the termination again when it ends the build

#### Scenario: A failed build's image
- **WHEN** a build has failed and its image is not `pending`, not protected and not inside the 24-hour hold of a `TEMPLATE_DRIFT`
- **THEN** reconcile removes the image under the image deletion rule before any build starts
- **AND** the pass result reports the counts as `reconcile.failed_images`

#### Scenario: The end of the pass
- **WHEN** the image of a failed build is still `pending` after reconcile and is not protected
- **THEN** the deletion rule removes it at the end of the pass under its pending-image path
- **AND** the pass result reports that run as `retention`

### Requirement: The sweep of stray archives has a bound of its own
The sweep of stray archives runs in reconcile, before the work of the pass. A stray delete MUST NOT start once the pass is 30 seconds old by the wall clock or by elapsed time on a monotonic clock, and the AWS client MUST read both where the delete starts. The sweep then stops, notes `archive:sweep_deferred` and leaves the rest to a later pass. The bound is on the start of the deletes, not on their end. Only the pass deadline bounds the listing.

#### Scenario: A late stray listing starts no delete
- **WHEN** the listing of the prefix, or a read before it, returns when the pass is 30 seconds old or older and before the pass deadline
- **THEN** no stray delete starts and the stray objects remain
- **AND** the pass notes `archive:sweep_deferred`, goes on with its own work, and a later pass sweeps them

#### Scenario: A listing that the pass deadline stops
- **WHEN** the pass deadline has passed before the listing of the prefix requests a page
- **THEN** the pass ends with `pass_deadline_exceeded`, no stray delete starts and nothing after the listing runs

#### Scenario: Slow stray deletes stop at the bound
- **WHEN** stray deletes take long and the pass becomes 30 seconds old before the sweep has ended
- **THEN** the delete that is running ends as the provider answers and no further stray delete starts
- **AND** the pass notes `archive:sweep_deferred` and goes on with its own work

#### Scenario: A wall clock that is stepped back does not stretch the bound
- **WHEN** the system clock is stepped back during the sweep, so that it shows the pass as younger than 30 seconds, and 30 seconds of elapsed time have passed since the pass began
- **THEN** no further stray delete starts and the pass notes `archive:sweep_deferred`
- **AND** the strays that remain are swept by a later pass

#### Scenario: The AWS client reads both cutoffs where a delete starts
- **WHEN** a stray delete passes the check of the pass, and before it starts the wall clock reaches the cutoff, or elapsed time on the monotonic clock reaches 30 seconds while the wall clock was stepped back and shows the pass as younger
- **THEN** the AWS client does not call the delete, the sweep stops with `archive:sweep_deferred` and the stray object remains for a later pass
- **AND** with neither cutoff reached, the AWS client calls the delete

#### Scenario: A stray backlog ends at the bound and the owed deletion is attempted
- **WHEN** many strays older than one hour sit under the prefix, each of their deletes takes long, a finished build owes its archive deletion, and the work of the pass, with or without a build to follow, ends before the last start of an owed deletion
- **THEN** the sweep stops at its bound, the work runs, and the pass attempts the owed deletion at its end
- **AND** no stray delete starts after the bound

### Requirement: Orphans are recorded and the builder has its own backstop
A tagged image that has no state row MUST be recorded as an unverified `candidate`, in whatever state the image is, and MUST NOT be pinned. The builder's lifetime timer and terminate-on-shutdown setting MUST remain the leak backstop that does not depend on the controller.

#### Scenario: An orphan image
- **WHEN** a tagged image has no state row, whether it is `available`, `pending` or `failed`
- **THEN** it is recorded as an unverified candidate and is never selected for promotion

#### Scenario: The controller is not running
- **WHEN** no pass runs after a builder starts
- **THEN** the builder's lifetime timer powers it off and terminate-on-shutdown removes it

### Requirement: A recorded orphan is not a failed build of ours
A recorded orphan image that is not `available` and is older than the 24-hour hold MUST be marked `failed`, and the image deletion rule MUST then remove it as the image of a failed build. Its row MUST keep `IMAGE_ORPHAN_RECORDED`, so it MUST NOT start a cooldown, change the failure count or clear `blocked`. It MUST still count toward `max_builds_per_day` and `min_build_interval_seconds`.

#### Scenario: An old orphan that is not available
- **WHEN** a recorded orphan image is `pending` or `failed` and is older than 24 hours
- **THEN** its row is marked `failed` and keeps `IMAGE_ORPHAN_RECORDED`
- **AND** the image deletion rule removes the image and its snapshots in the same pass

#### Scenario: A young orphan
- **WHEN** a recorded orphan image is not `available` and is 24 hours old or younger, or its age is unknown
- **THEN** its row stays a `candidate` and only the state of the image is noted

#### Scenario: An available orphan
- **WHEN** a recorded orphan image is `available`
- **THEN** its row stays an unverified `candidate` and is never pinned

#### Scenario: An orphan does not move the failure bounds
- **WHEN** a recorded orphan is marked `failed`
- **THEN** the failure count, the cooldown and `blocked` do not change
- **AND** its basis digest does not clear `blocked`

#### Scenario: An orphan counts toward the limits
- **WHEN** an orphan row exists
- **THEN** it counts toward `max_builds_per_day` and `min_build_interval_seconds` by the creation time of its image

### Requirement: Failures are bounded
Each build MUST be bounded by `build_deadline_seconds`, the builder's lifetime timer, `min_build_interval_seconds`, `max_builds_per_day`, a failure cooldown, and a `blocked` state that begins after `max_consecutive_failures` consecutive failures. A refusal MUST NOT count as a failure. `blocked` MUST clear only with a basis digest change or `image run --apply --force`, and `image status` MUST say so and show how.

#### Scenario: The failure limit is reached
- **WHEN** consecutive failed builds reach `max_consecutive_failures`
- **THEN** the control state becomes `blocked` and every later `run --apply` refuses until it clears
- **AND** an `image_blocked` event is written

#### Scenario: Refusals are not failures
- **WHEN** a start is refused, before or after the build row exists
- **THEN** the refusal adds no failure, starts no cooldown and sets no `blocked`
- **AND** a clearing that the trigger decision already made stays
- **AND** no `image_build_failed` or `image_blocked` event is written

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
- **THEN** the candidate record stores the version, the image id, the archive sha, the script sha, the gate result of the launched build and the source pin

#### Scenario: Another field differs
- **WHEN** the read-back shows any other difference
- **THEN** the command emits `TEMPLATE_DRIFT`, marks the version unusable and keeps the image for 24 hours

#### Scenario: Version creation fails
- **WHEN** the call fails or the pass ends before the response, before the build deadline
- **THEN** the next pass retries with the same client token and does not rebuild
- **AND** a failure observed at or after the build deadline fails the build with the refusal's code

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
`image promote` MUST be pure local: it MUST make no AWS call, build no AWS client, hold no AWS credentials and read only local files (the image state file, the deployment TOML and the backup that a journal row names). Without `--write` it MUST print the evidence block, the diff of the one pin line, the previous pin and the rollback target.

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
The next `promote --write`, `promote --rollback --write` or `image run --apply` MUST resolve a pending row from the file hash: the old hash marks it aborted, the new hash completes it, and any other hash refuses with `PROMOTION_JOURNAL_DIVERGED`. A `promote` without `--write` MUST report the resolution that the hash implies, plan against the rows as that resolution leaves them, and write nothing. While a row is pending or diverged, both of its pins MUST be protected from deletion.

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

#### Scenario: A plain promote meets a pending row
- **WHEN** an operator runs `promote` without `--write` while a row is pending
- **THEN** the preview names the row and the resolution that the file hash implies
- **AND** the candidate and the rollback target are those that `promote --write` finds after it resolves the row
- **AND** the row, the state file and the TOML are unchanged

#### Scenario: A rollback preview meets a pending row
- **WHEN** a promotion from B to C stopped after the rename, so that its row is pending and the file holds C, and an operator runs `promote --rollback` without `--write`
- **THEN** the preview plans the rollback to B that `promote --rollback --write` makes after it completes the row
- **AND** the row, the state file and the TOML are unchanged

### Requirement: Promotion keeps a backup and refuses a changed file
Before the rename, `promote --write` MUST write a timestamped backup and MUST refuse with `PROMOTE_FILE_CHANGED` when the file differs from the bytes it read. Every command that reads the file to promote, roll back or resolve a journal row MUST refuse with `PROMOTE_FILE_STALE` when no name links the open file any more.

#### Scenario: A backup exists
- **WHEN** a write succeeds
- **THEN** a backup `<file>.bak.<UTC timestamp>` exists and holds the previous bytes

#### Scenario: The file changed since it was read
- **WHEN** another editor changes the file after the read
- **THEN** the command refuses with `PROMOTE_FILE_CHANGED` and writes nothing

#### Scenario: A stale view of the file
- **WHEN** a container mounted the file itself, a write elsewhere replaced the file by a rename before or during the read, and the open file has no link left (`st_nlink == 0`) once its bytes are read
- **THEN** a command in that container that resolves the journal, promotes or rolls back refuses with `PROMOTE_FILE_STALE` and writes nothing

#### Scenario: The file names another state than the command opened
- **WHEN** `[image].state_directory` (compared as written, without following a link) or `deployment` changes between the start of `promote` (with or without `--write`, `--rollback` included) and a read of the file it plans or writes from, the read under the locks of `--write` included
- **THEN** the command refuses with `PROMOTE_STATE_CHANGED` and names no path
- **AND** it changes no row, event, deployment file, state file or rollback journal of either state, and creates nothing in the state the file names now; only its own lock files may be created

#### Scenario: A concurrent change during a command is outside the operator contract
- **WHEN** an operator edits a non-pin setting of the deployment file other than `[image].enabled = false`, replaces or re-links the file or its `--config` path, swaps a bind mount, or moves, restores or replaces the image state directory while `image run`, `image promote` or `image status --refresh` runs
- **THEN** the change is outside the operator contract, and the product makes no promise about it beyond the refusals that this specification already requires (this requirement names `PROMOTE_FILE_CHANGED`, `PROMOTE_FILE_STALE` and `PROMOTE_STATE_CHANGED`)
- **AND** setting `[image].enabled = false` is the one allowed edit: it does not stop a running command, so the operator waits until no `image run --apply` runs

### Requirement: Promotion runs in an isolated operator container
`promote` MUST run only as an operator-invoked one-off. The shipped documentation MUST give a container line that has no network, no AWS credential directory and no forge token. `promote --write` MUST refuse with `PROMOTE_DIRECTORY_NOT_DEDICATED` when the directory of the TOML holds anything other than the TOML, its `.bak.*` files, its lock file and its promote temporary file. A test MUST assert that `promote` constructs no AWS client.

#### Scenario: The directory holds credentials
- **WHEN** the mounted directory contains a credential directory or an env file
- **THEN** `promote --write` refuses with `PROMOTE_DIRECTORY_NOT_DEDICATED` and writes nothing

#### Scenario: A dedicated directory
- **WHEN** the directory holds only the TOML, its backups, its lock file and its promote temporary file
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
Every path that deregisters an image or deletes a snapshot (retention and every cleanup path) MUST first resolve the protected set: the images of every launch-template version that the TOML references, including per-zone pins; both pins of the newest completed journal row and of every row newer than it; the pins of a pending row; the current candidate unless its row records `TEMPLATE_DRIFT`; and an active build. It MUST NOT deregister a protected image.

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

#### Scenario: A stale file after a promotion
- **WHEN** a promotion from version A to version B is complete, the file that a pass reads still pins version A, and the image of version B is old enough for retention
- **THEN** no deletion path removes the image of version B

#### Scenario: A stale file after a rollback
- **WHEN** a rollback from version B to version A is complete, the file that a pass reads still pins version B, and the image of version A is old enough for retention
- **THEN** no deletion path removes the image of version A

#### Scenario: A promotion that a pass resolved as aborted
- **WHEN** a promotion to version B replaced the file and stopped before its row completed, a pass that read the old file resolved the row `aborted`, and the image of version B is old enough for retention
- **THEN** no deletion path (retention, failed-build cleanup, orphan cleanup or the pending-image timeout) removes the image of either pin of that row

#### Scenario: A later promotion fails after a promotion that a pass resolved as aborted
- **WHEN** after that aborted row a promotion to version C commits its row and fails, and a pass resolves it `aborted` too
- **THEN** no deletion path removes the image of either pin of either row until a newer row completes

#### Scenario: A stale view of the file at a deletion
- **WHEN** the deployment file that a pass reads is a view of a file that a rename replaced before or during the read, and the open file has no link left (`st_nlink == 0`) once its bytes are read
- **THEN** the deletion is refused with `IMAGE_DELETION_UNSAFE` and no path deletes anything

#### Scenario: A pin of an unsettled row is malformed
- **WHEN** a pin of a journal row newer than the newest completed one is not a positive version number
- **THEN** reading the journal refuses with `IMAGE_STATE_UNREADABLE` before the deletion rule runs, and no path deletes anything

#### Scenario: A pin of an unsettled row cannot be resolved
- **WHEN** a pin of a journal row newer than the newest completed one is a valid version number whose version cannot be read or names no literal image
- **THEN** the deletion is refused with `IMAGE_DELETION_UNSAFE` and no path deletes anything

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

### Requirement: Event lines are whole and hold no URL
An event value MUST NOT hold `://`. The console tail of a failed-build event MUST show every URL, every presigned query string and the archive key as `[archive-url]`, the text that `warm-cache.sh` prints for the archive URL. The command MUST write an event line with one write on an append-only descriptor, MUST treat a short write as an error and MUST NOT retry it.

#### Scenario: The tail holds URLs
- **WHEN** the console tail of a failed build holds a URL, a presigned query string or the archive key
- **THEN** the event shows each of them as `[archive-url]`
- **AND** no value of the event holds `://`

#### Scenario: A value would hold a URL separator
- **WHEN** an event field holds a value with `://`
- **THEN** the event is refused and no line is written

#### Scenario: A write falls short
- **WHEN** the write of an event line stores fewer bytes than the line
- **THEN** the write is an error and is neither continued nor retried

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
- **THEN** the pass advances it, or refuses with `IMAGE_STATE_LOCKED` while an earlier pass still runs, and starts no second builder
