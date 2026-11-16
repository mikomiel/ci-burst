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

### Requirement: The builder is time-bounded
The script MUST arm a lifetime timer that powers the builder off before it runs any consumer command, and it MUST power the builder off when it exits on any path.

#### Scenario: The builder outlives its work
- **WHEN** a step hangs past the builder lifetime
- **THEN** the timer powers the builder off

#### Scenario: The script fails
- **WHEN** any step fails
- **THEN** the exit path powers the builder off
- **AND** no ready marker has been printed

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

### Requirement: Warm images are traceable and rolled out through the pinned template
Documentation MUST state that the image is tagged with the source digest, that a lockfile change is the reason to rebuild, and that a warm cache reduces downloads without changing a job's result. Rollout MUST remain the existing flow: a new launch-template version, a canary, then the operator pins the version. This change MUST NOT alter controller behaviour, IAM or the router.

#### Scenario: A lockfile changes
- **WHEN** a lockfile that the warm commands read changes
- **THEN** the documentation directs the operator to rebuild the layer
- **AND** a job on the older warm image still resolves its own lockfile and produces the same result, only more slowly

#### Scenario: A warm image is rolled out
- **WHEN** an operator has created a warm image
- **THEN** the controller does not use it until the operator publishes a launch-template version, runs a canary and pins that version

### Requirement: Instance-type selection guidance
Documentation MUST state that the newest generation of the same shape belongs first in `aws.instance_types` and an older type after it as capacity fallback, and that the choice is verified with per-type job duration and cost per job. The documented examples MUST list the newest generation first.

#### Scenario: An operator orders the types
- **WHEN** an operator reads the deployment guide
- **THEN** the guide states the ordering rule and the measurement that checks it
- **AND** its example lists the newest generation first
