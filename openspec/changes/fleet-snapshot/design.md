# Design

## Context

`apply` already reads the repository runner-jobs queue once per pass and reduces
it to runnable demand. The same response also contains the running attempts, and
Forgejo exposes a second, repo-scoped endpoint that lists runner registrations.
The controller has the owner-scoped token the queue endpoint already requires,
so the registration read adds no credential and no deployment mount.

The capture that informed this change found: the queue entry has no timestamp
and no runner field; a job detail adds only its steps; a runner detail adds its
uuid and ephemeral flag but no current job; registrations are named
`burst-ip-<private-ip-with-dashes>` and all are ephemeral; and one snapshot had
eight running jobs against seven active registrations. That is the evidence
behind both the scope reduction and the non-atomic label.

## Goals / Non-Goals

**Goals:** make idle ephemeral capacity visible; keep the signal durable and
alertable; keep the read bounded and optional; keep the exporter credential-free;
state the uncertainty instead of hiding it.

**Non-Goals:** linking a registration to a job attempt; a per-worker job
verdict; console parsing; changing admission, placement, lifetime or cost; a
new command or listener.

## Decisions

### Counts, not an association

The controller counts registrations and running attempts; it does not claim that
a particular worker ran a particular job. The API cannot support that claim, and
a timing heuristic would be wrong exactly when the fleet is busy and the answer
matters. The idle estimate is therefore `max(0, active - running)`, and a
negative raw difference is reported as a note, not a number.

### Ephemeral registrations are the fleet

The shipped bootstrap registers an ephemeral runner; a persistent runner on the
same repository is not part of this deployment's capacity. The fleet counts use
the ephemeral flag, while the queue counts still include every job whose labels
the worker supports. The result says so, so a persistent runner cannot silently
inflate the idle estimate.

### Best-effort evidence, separate from admission

The registration read and its ledger write are auxiliary. A refused read is
reported as `fleet.available: false` with the refusal, and a refused write is
reported as `persisted: false`; neither changes the decision. The queue read
remains fail-closed and is still the only launch gate. This ordering is
deliberate: a monitoring endpoint that is briefly unavailable must not stop
capacity, and a record that could not be stored must not be invented.

### One row per pass, seven-day retention

`fleet_observations` holds one row per successful pass, pruned on each write
after seven days. The ledger remains small, `status` reads the newest row, and
`export` reads the newest row from the same file it already opens read-only for
cost. No second state file and no second writer exist.

### Staleness is explicit

`export` publishes the observation timestamp whenever a row exists, but the
three count gauges only while the row is at most fifteen minutes old. A stale
row is reported as `fleet_stale` in the result; a missing row or an unreadable
ledger as `fleet_omitted`. An alert on the timestamp is therefore possible
without a silent stale value.

### Shape is qualified, and separate from the queue contract

The registration response has its own contract name
(`forgejo-17.0.1-runners`) and its own refusal codes. The queue contract is not
widened: a registration shape error cannot make demand unavailable, and a queue
shape error cannot make the fleet unavailable. Both are read-only GETs and
neither follows a redirect.

## Alternatives

- **Full worker↔job association.** Rejected: no API link, ambiguous under load,
  new claim state, no control benefit.
- **Console-derived job claim.** Rejected: a worker self-report, and the
  exporter's closed marker vocabulary and credential-free boundary should not be
  widened for a claim that still cannot be verified.
- **No change.** Leaves idle capacity invisible; the existing request was
  specifically to retain at least the cheap part of the value.
- **A new command.** Rejected: `status` already reads the ledger, and a second
  entry point would duplicate its credential and state handling.

## Risks and Trade-offs

- The registration list is repo-scoped; a persistent runner with the same labels
  is excluded from the ephemeral counts but its jobs remain in the queue, so the
  idle estimate can understate. The result documents this.
- A transient 403 or timeout yields `fleet.available: false` for that pass; the
  previous row survives and becomes stale rather than being replaced by zeros.
- A schema-3 file opened read-only has no table; `status` and `export` report no
  snapshot rather than failing. The writable run performs the upgrade.

## Migration and Rollback

The schema-3 to schema-4 upgrade creates one empty table, takes no backfill
parameters, and runs under the writer lock after a size-checked copy to
`state.sqlite3.pre-v4.<epoch>`. A version-1 or version-2 file is upgraded in the
same transaction as before and now lands on schema 4. The previous release
refuses schema 4, so a rollback restores the copy before starting the older
image. No configuration or deployment change is required.

## Evidence Gaps

The idle estimate is not proof that a worker is idle: the two endpoints are read
separately, the queue includes jobs assigned to runners outside this deployment's
ephemeral set, and a registration can be between jobs. The snapshot is a signal
for trends and alerts, not an assignment decision.
