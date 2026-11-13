# Design

## Decision

Retention is a step in `deploy/ci-burst-refresh.sh` that runs only where the
script has already proven the serving image is the promoted one: the promote
path after `latest` has moved and the deployed ref is recorded, and the
already-current path where the serving label equals the channel commit.

## Retained set

| Entry | Why it survives |
|---|---|
| `latest` | The tag the scheduled units resolve. |
| `previous` | The only rollback handle the install documents. |
| `sha-<12>` of `deployed-sha` | This host's record of the promoted commit. |
| `sha-<12>` of every 40-hex ref in `deployment-state.json` | A support path or a newer document may still name it. |
| The most recent `CI_BURST_RETAIN_BUILDS` builds | A recent rollback without a rebuild; default 3. |

Everything else matching `localhost/ci-burst:sha-<12>` is untagged. The set is
computed from the tag shape, not from an age cutoff: age is a guess about intent,
while these five entries are statements about what a path can resolve.

## Why the already-current path also reconciles

A run that finds the channel commit already serving promotes nothing, so it
cannot first prove a promotion. It does prove the serving image, and it is the
only run that will happen until the next merge. Without retention there, a failed
cleanup is never retried until someone merges again. Both call sites sit below a
proof that the serving image is the promoted one, so neither can remove a tag on
the strength of a failed promotion.

## Deletion order and failure behaviour

Untag first, then prune. Untagging alone frees no bytes, because the build
becomes a dangling image that still owns its layers. Each untag names one full
reference (`IMAGE:TAG`), because `podman untag` takes image names: a bare
repository name would remove every name on that image, `latest` and `previous`
included. The prune is filtered by `org.ci-burst.build=refresh`, a label this
build sets itself and no other product uses; the standard
`org.opencontainers.image.revision` label is shared convention and cannot
establish that an image is ours. Images built before the ownership label existed
are left alone.

A failure is reported, never guessed around: an image that cannot be inspected
keeps its tag, and a refused untag or prune logs `retention_failed`. Neither
changes the exit status, because the promotion has already succeeded and a
failing unit would report the deployment as failed when the serving image is
correct.

## Alternatives

- Keep the newest N tags only. Declined: it would remove the `previous` rollback
  handle as soon as a host lagged, and it cannot express the deployed ref.
- Remove by age. Declined: it deletes on a guess, and the failure mode is a
  rollback that needs a network rebuild.
- Prune everything dangling. Declined: the host also runs images this script did
  not build, and the label filter is free.
- A separate retention script. Declined: it needs the same state, the same
  engine calls and the same proof, so it would be a second owner of one decision.

## Rollback

Revert the change; the script then stops removing tags. No retained tag is
restored by the revert, so a host that already ran it keeps the smaller set it
has, and every removed build remains rebuildable from its commit.
