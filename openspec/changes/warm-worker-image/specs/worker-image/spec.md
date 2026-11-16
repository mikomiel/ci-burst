# Spec Delta

## ADDED Requirements

### Requirement: Optional warm-cache layer
ci-burst MUST provide an optional script that adds a warm-cache layer to the worker image. The layer MUST be generic: the consumer MUST supply the source archive URL, its sha256, the warm commands and optionally an offline gate command, and no consumer-specific command MUST live in ci-burst. The script MUST NOT create an image and MUST NOT hold or require a cloud credential. An operator who does not run it MUST keep the unchanged cold base image.

#### Scenario: A consumer supplies its own commands
- **WHEN** an operator runs the script with an archive URL, its sha256 and warm commands
- **THEN** the script runs those commands and no command from ci-burst's own source
- **AND** the script makes no cloud API call

#### Scenario: A required input is missing
- **WHEN** the sha256 is absent or the warm commands are empty
- **THEN** the script refuses before it runs any consumer command
- **AND** it prints no ready marker

#### Scenario: The layer is not used
- **WHEN** the operator never runs the script
- **THEN** the base image and its cold start are unchanged

### Requirement: Source archive is verified
The script MUST fetch the source archive and compare its bytes with the supplied sha256 before it extracts or runs anything from it. A mismatch MUST stop the script without a ready marker. The script MUST extract the archive into a directory outside the job account's home.

#### Scenario: The archive matches its digest
- **WHEN** the fetched bytes match the supplied sha256
- **THEN** the script extracts them and continues

#### Scenario: The archive does not match its digest
- **WHEN** the fetched bytes differ from the supplied sha256
- **THEN** the script exits non-zero before any warm command runs
- **AND** it prints no ready marker

### Requirement: Warm commands run as the job account with the shared cache environment
The script MUST run the warm commands, and the offline gate when one is supplied, as the job account with an empty inherited environment, the job's umask, and the shared cache environment. The cache variables MUST resolve under the job account's `cache` directory.

#### Scenario: Commands run unprivileged and clean
- **WHEN** the script runs a warm command
- **THEN** the command runs as the job account, not as root
- **AND** it receives only the named variables, including every variable of the shared cache environment

#### Scenario: A job reads what the warm step wrote
- **WHEN** a job later runs on a worker launched from the warm image
- **THEN** it runs with the same cache variables as the warm commands
- **AND** it finds the warm content at the paths those variables name

### Requirement: Failure produces no image
The script MUST print its ready marker only after the warm commands, the offline gate when supplied, and the clean-up have all succeeded. A failure at any step MUST end the script with a non-zero status and without the ready marker. A failed build MUST NOT be offered for image creation.

#### Scenario: A warm command fails
- **WHEN** a warm command exits non-zero
- **THEN** the script exits non-zero without the ready marker

#### Scenario: The offline gate fails
- **WHEN** the offline gate command exits non-zero
- **THEN** the script exits non-zero without the ready marker

#### Scenario: Everything passes
- **WHEN** every step succeeds
- **THEN** the script prints the ready marker once, as its last output
- **AND** it prints the source digest beside the marker

### Requirement: The builder is time-bounded and survives to image creation
The script MUST arm a lifetime timer that powers the builder off before it runs any consumer command. After a successful build it MUST print the ready marker and keep the builder running for a bounded snapshot window, still bounded by the lifetime timer. After a failed build it MUST hold briefly for the console and then power the builder off. Documentation MUST require terminate-on-shutdown and delete-on-termination storage for the builder.

#### Scenario: The build succeeds
- **WHEN** every step has passed and the ready marker is printed
- **THEN** the builder stays running for the snapshot window and is not powered off by the script's own exit path
- **AND** the lifetime timer still powers it off if the operator never creates the image

#### Scenario: The build fails
- **WHEN** any step fails
- **THEN** the script prints no ready marker, holds briefly for the console and then powers the builder off

#### Scenario: The builder outlives its work
- **WHEN** a step hangs past the builder lifetime
- **THEN** the timer powers the builder off

### Requirement: Only the script prints the ready marker
The script MUST send the stdout and stderr of every warm command and of the offline gate to a log file and MUST NOT write them to its own output. On failure the script MAY print a tail of that log, and MUST prefix every printed line with a fixed frame so that no consumer line can begin with the ready marker. Only the script itself MUST print the ready marker, and only on the success path.

#### Scenario: A failing command prints the marker
- **WHEN** a warm command or the offline gate prints the ready marker text and exits non-zero
- **THEN** the script exits non-zero
- **AND** no line of its output begins with the ready marker

#### Scenario: A passing command prints the marker
- **WHEN** a successful warm command prints the ready marker text
- **THEN** that text does not appear unframed in the script's output
- **AND** the script's own marker is the only unframed one

#### Scenario: A failure shows its cause
- **WHEN** a warm command fails
- **THEN** the script prints a framed tail of the command's log so the operator can read the cause

### Requirement: The image carries no consumer source or registration
Before the ready marker the script MUST remove the extracted source, the runner registration file and every entry in the job account's home other than `cache`, and MUST clean cloud-init state including its logs. A clean-up failure MUST prevent the ready marker.

#### Scenario: Clean-up completes
- **WHEN** the script reaches the ready marker
- **THEN** no extracted source, no runner registration file and no entry in the job account's home other than `cache` remain

#### Scenario: Clean-up fails
- **WHEN** a clean-up step fails
- **THEN** the script exits non-zero without the ready marker

### Requirement: The cache environment has one definition
The job-account cache environment MUST be defined once in a shared file. The warm layer and the image build MUST use that definition. The published bootstrap's cache assignments MUST equal it in every name and value, and a repository test MUST fail when any copy differs or lacks a name.

#### Scenario: A copy diverges
- **WHEN** `bootstrap.sh` or `provision.sh` sets a cache variable to a value that differs from the shared file
- **THEN** the repository test fails and names the variable

#### Scenario: A variable is dropped
- **WHEN** a copy omits a variable that the shared file defines
- **THEN** the repository test fails and names the variable

#### Scenario: A path leaves the cache directory
- **WHEN** a shared value resolves outside the job account's `cache` directory
- **THEN** the repository test fails, because the clean-up would delete it

### Requirement: Warm images are traceable and documented honestly
Documentation MUST state that the image is tagged with the source digest and that a lockfile change is the reason to rebuild. It MUST state that a stale content-addressed package or download cache (uv, npm, pip, browser downloads) costs only time, because the job resolves its own lockfile against it.

#### Scenario: A lockfile changes
- **WHEN** a lockfile that the warm commands read changes
- **THEN** the documentation directs the operator to rebuild the layer
- **AND** a job on the older warm image still resolves its own lockfile through its package cache, only more slowly

### Requirement: Cache validity beyond package caches is the consumer's
Documentation MUST NOT extend the stale-cache claim to any other cache a consumer places under `cache/`, such as a validation or result cache. The consumer MUST key such a cache on its source, lockfiles, toolchain and environment, and ci-burst does not guarantee its validity.

#### Scenario: A consumer warms a result cache
- **WHEN** a consumer's warm commands fill a cache that is not content-addressed
- **THEN** the documentation states that the consumer owns that cache's validity
- **AND** it makes no claim that a stale entry is harmless

### Requirement: Rollout stays the operator's existing flow
Rollout of a warm image MUST remain the operator flow that `deploy/runner/image/README.md` describes: create the image, create a new launch-template version, move the deployment configuration's `launch_template_version` to it, and run the toolchain check on a launched worker. A canary before that move MUST be documented as an operator obligation, and the controller MUST NOT be described as enforcing it. This change MUST NOT alter controller behaviour, IAM or the router.

#### Scenario: A warm image is rolled out
- **WHEN** an operator has created a warm image
- **THEN** the controller does not use it until the operator publishes a launch-template version and moves the configured version to it
- **AND** the documentation tells the operator to run a canary before that move

#### Scenario: No canary evidence exists
- **WHEN** an operator moves the configured version without a canary
- **THEN** the controller launches from it anyway, because it does not check for canary evidence

### Requirement: Instance-type selection guidance
Documentation MUST state that the newest generation of the same shape belongs first in `aws.instance_types` and an older type after it as capacity fallback, and that the choice is verified with per-type job duration and cost per job. The documented examples MUST list the newest generation first.

#### Scenario: An operator orders the types
- **WHEN** an operator reads the deployment guide
- **THEN** the guide states the ordering rule and the measurement that checks it
- **AND** the guide reconciles the rule with its earlier advice to keep the template's type first
- **AND** its example lists the newest generation first

### Requirement: End-to-end runbook
One owner document MUST give an end-to-end runbook from source archive and sha256 to a pinned launch-template version, covering hosting the archive at a credential-free URL, launching the builder, watching the console marker, creating the tagged image, publishing the version and the canary. It MUST give copy-paste AWS CLI commands with comments and placeholders only, and MUST state that a presigned URL is short-lived and never logged or committed.

#### Scenario: An operator follows the runbook
- **WHEN** an operator follows the runbook with their own values
- **THEN** every step has a command or an instruction and every identifier is a placeholder
- **AND** the runbook names the console marker as the signal to create the image

#### Scenario: The archive URL is sensitive
- **WHEN** the runbook hosts the archive behind a presigned URL
- **THEN** it states that the URL expires soon and is never logged or committed

### Requirement: Generic warm-command examples
Documentation MUST give generic warm-command examples, each with an offline gate counterpart, for uv or Python projects, npm, pip, Playwright browsers and a monorepo check runner that has a prime mode and an offline mode. Every cache path in an example MUST lie under the shared cache directory. The Playwright example MUST state that the job environment is the bootstrap's, so a consumer sets `PLAYWRIGHT_BROWSERS_PATH` in its own workflow. Examples MUST NOT name a consumer.

#### Scenario: Example paths stay in the shared cache
- **WHEN** a documented example sets a cache variable or path
- **THEN** the value lies under the shared cache directory
- **AND** a repository test fails when it does not

#### Scenario: A browser cache is warmed
- **WHEN** the Playwright example installs browsers into a cache path
- **THEN** the documentation states that the job's workflow must set the same browsers path, because the bootstrap does not
