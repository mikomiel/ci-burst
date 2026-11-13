# Spec Delta

## ADDED Requirements

### Requirement: Retention runs only on a proven serving image
The refresh MUST remove build tags only after it has proven that the serving
image is the promoted one: after the serving tag has moved and the deployed ref
is recorded, or on a run whose serving label equals the channel commit. No
deletion may occur before that proof, so a failed promotion removes nothing.

#### Scenario: A promotion succeeds
- **WHEN** the serving tag has moved to the candidate and the deployed ref is recorded
- **THEN** retention runs once for that host

#### Scenario: A promotion fails
- **WHEN** the build, the smoke run or the serving-tag move fails
- **THEN** no tag is removed and no image is pruned

### Requirement: The retained set is explicit
Retention MUST keep `latest`, `previous`, the `sha-<12>` tag of the deployed ref recorded on the host, the `sha-<12>` tag of every 40-hex ref the deployment-state document names, and the most recent `CI_BURST_RETAIN_BUILDS` builds by image creation time. It MUST remove only `sha-<12>` tags outside that set, and MUST NOT remove a tag whose image it cannot inspect.

#### Scenario: An old build outside the set
- **WHEN** a build tag is older than the retained window and is not named by either ref source
- **THEN** its tag is removed and its dangling image is pruned

#### Scenario: A ref the state document names
- **WHEN** the deployment-state document names a commit whose tag is older than the window
- **THEN** that tag is kept

#### Scenario: An uninspectable tag
- **WHEN** the engine cannot report the creation time of a build tag
- **THEN** the tag is kept and reported as unverified

### Requirement: A retention failure does not misreport the deployment
A refused untag or prune MUST be logged and MUST NOT change the exit status, because the promotion has already succeeded and the serving image is correct.

#### Scenario: The engine refuses an untag
- **WHEN** an untag or the prune returns a failure
- **THEN** the run logs a retention failure and still completes successfully
