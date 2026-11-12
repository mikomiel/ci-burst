# Design

## Context

See [proposal.md](proposal.md) for motivation. The bootstrap's one-job
invocation ran under `set -euo pipefail` with `|| true` appended, and the EXIT
trap printed the shell's exit status. The trap is also the only cleanup path:
it shreds the registration token, syncs and shuts the instance down. The
exporter already reads a small fixed marker vocabulary from the instance
console and republishes only names and statuses, never console text.

```
JOB_RC=0
timeout ... forgejo-runner one-job --wait ... || JOB_RC=$?
echo "STEP job"
rm -f .runner
case "$JOB_RC" in
  0) echo "bootstrap one-job completed rc=0" ;;
  124|137) echo "bootstrap one-job timeout rc=$JOB_RC" ;;
  *) echo "bootstrap one-job failed rc=$JOB_RC" ;;
esac
exit "$JOB_RC"          # the EXIT trap still runs: token, sync, shutdown
```

## Goals / Non-Goals

**Goals:** the exit marker reports the real one-job process status; a completed,
failed and timed-out process are distinguishable in the event stream; cleanup
and termination still run on every path; historical event lines remain valid.

**Non-Goals:** reporting the Forgejo job result from the worker (Forgejo owns
it); changing registration, the SSM read, the iptables rules, the umasks or the
lifetime watchdog; adding a metric for the process status; decoding console
output in the exporter.

## Decisions

### Capture the status in the script, not in the trap

The exit marker stays a single line printed by one owner, the EXIT trap. The
tail sets `JOB_RC` from the one-job command and exits with it, so the trap's
existing `rc=$?` reports the same status without a second code path. A
`case` over `JOB_RC` keeps the marker vocabulary closed at three names and
reuses the kernel's timeout convention: 124 for `TERM`, 137 for `KILL`.

### A separate `job-process` event, not a reinterpreted `exit`

`exit` already means "the bootstrap shell is finishing", and existing consumers
may treat it that way. Reinterpreting it would break historical lines and
overload one identity with two meanings. A distinct event kind carries the
process outcome and status, keeps the historical `exit` shape valid, and names
exactly what is known — that the runner process ended, not whether the job
passed.

### The event schema is closed and validated like the others

`job-process` joins `EVENT_SCHEMA` with exactly `outcome` and `rc` in addition
to the common fields. Its identity is
`<instance>:job-process:<outcome>:<rc>`, and membership in a precomputed
identity set rejects a forged outcome, a padded zero and a status the bootstrap
cannot pair with the outcome. Persisted dedupe state and retained event lines
are validated against the same set, so a malformed line is dropped rather than
republished.

### Publication is an explicit state, not a weakened pin

The bootstrap is launch-template user data, so its bytes are only active once a
version is published. The test keeps the last published version and digest and
adds a pending pair for the prepared version: the file must equal the pending
digest while one is owed, and the published digest otherwise. Publication —
create the version, bump the deployment TOML, flip the constants — stays an
operator step. While it is owed the test is green against the pending bytes, and
the prerequisite check reports the deployed-template drift.

## Risks / Trade-offs

- A worker whose console buffer scrolls past the marker before a read publishes
  no `job-process` event; the exit marker still reports the status. Evidence is
  only ever added, never un-learned.
- The marker is one more console line on a channel the exporter already bounds;
  it adds no read and no text.
- While a pending digest is owed the contract test passes against the prepared
  bytes and `check-prerequisites` reports template drift. The pin is never
  widened to accept either digest: an unpublished bootstrap change reaching no
  worker was the original defect this contract exists to catch.
