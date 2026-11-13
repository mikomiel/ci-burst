# Spec Delta

## ADDED Requirements

### Requirement: Retention runs only on a proven serving image
The refresh MUST remove build tags only after it has proven that the serving
image is the promoted one: after the serving tag moved and the deployed ref is
recorded, or on a run whose serving label equals the channel commit. No deletion
may occur before that proof, so a failed promotion removes nothing.

#### Scenario: A promotion succeeds
- **WHEN** the serving tag has moved to the candidate and the deployed ref is recorded
- **THEN** retention runs once for that host

#### Scenario: A promotion fails
- **WHEN** the build, the smoke run or the serving-tag move fails
- **THEN** no tag is removed and no image is pruned

### Requirement: The retained set is explicit
Retention MUST keep `latest`, `previous`, the `sha-<12>` tag of the deployed ref recorded on the host, the `sha-<12>` tag of every 40-hex ref the deployment-state document names, and the newest `CI_BURST_RETAIN_BUILDS` builds overall by image creation time. It MUST remove only `sha-<12>` tags outside that set, naming one full `IMAGE:TAG` reference per removal, and MUST leave every other name on that image resolvable.

#### Scenario: An old build outside the set
- **WHEN** a build tag is older than the retained window and is not named by either ref source
- **THEN** that name is removed and nothing else is

#### Scenario: A ref the state document names
- **WHEN** the deployment-state document names a commit whose tag is older than the window
- **THEN** that tag is kept

#### Scenario: A build shares its image with a rollback tag
- **WHEN** an evicted build tag names the same image as `previous`
- **THEN** only the build name is removed and `previous` still resolves

#### Scenario: An uninspectable tag
- **WHEN** the engine cannot report the creation time of a build tag
- **THEN** the tag is kept and reported as unverified

### Requirement: The retained set fails closed
A retained ref that exists but cannot be read MUST stop deletion for that run and be reported, rather than being treated as absent.

#### Scenario: An unparsable state document
- **WHEN** the deployment-state document exists and does not parse
- **THEN** the run removes nothing and logs a retention failure

#### Scenario: A corrupted deployed ref
- **WHEN** the recorded deployed ref is not a 40-hex commit
- **THEN** the run removes nothing and logs a retention failure

### Requirement: Pruning is owned and retryable
The prune MUST filter on this deployment's own build label and MUST run on every pass that reaches retention, so a refused prune is retried even when that pass removed no tag.

#### Scenario: A refused prune
- **WHEN** the prune returns a failure
- **THEN** the run logs it and the next qualifying pass attempts the prune again

#### Scenario: An unrelated dangling image
- **WHEN** another product's dangling image carries the standard OCI revision label
- **THEN** it is not a prune candidate

### Requirement: A retention failure does not misreport the deployment
A refused untag or prune MUST be logged and MUST NOT change the exit status, because the promotion has already succeeded and the serving image is correct.

#### Scenario: The engine refuses an untag
- **WHEN** an untag or the prune returns a failure
- **THEN** the run logs a retention failure and still completes successfully
