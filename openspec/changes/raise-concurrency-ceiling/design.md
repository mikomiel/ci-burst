# Design

## Context

See [proposal.md](proposal.md) for motivation. The controller already launches a
batch sized by observed demand:

```
launch_count = min(eligible waiting jobs, limit - non-terminal owned, affordable)
```

Every term is evidence the controller reads each tick: the Forgejo job queue, the
durable ledger reconciled against the provider, and the current window's
remaining allowance. The configured limit appears only in the second term. This
change moves that one number and keeps the other two owners untouched.

## Goals / Non-Goals

**Goals:** a deployment can raise peak concurrency to a still-bounded number
without patching the product; every consumer of the bound keeps working at the new
ceiling; the refusal above it is explicit; the default is unchanged.

**Non-Goals:** dynamic or queue-driven ceilings, multiple concurrent controllers,
warm pools, On-Demand capacity, per-worker capacity greater than one job, or any
change to reservations, placement, expiry and cleanup.

## Decisions

### A static product bound, not a governor

A ceiling that a controller raised and lowered itself would be a second owner of
capacity policy: the launch count is already demand-driven, so an adaptive
ceiling adds no launches that demand would not have produced. It would not have
to write configuration — a governor could hold its state in the durable ledger
instead — but it would still be a policy owner that answers a question admission
already answers, and one that an operator cannot predict from the configuration.
Admission stays single-owned and observable, and an operator who wants a
different peak edits one integer.

### Twelve, not unlimited

The bound must stay inside what one controller host qualifies and what one
deployment can pay for. At the top of the range the controller issues at most one
price read per enabled candidate and one ledger write per admitted worker in one
tick, and `status.hourly_ceiling` reports `max_spot_price × max_instances`, so the
highest supportable ceiling is still a number an operator can see and price.
Twelve is high enough for a pool that shares its label with a persistent fleet
and low enough that the per-tick work and the unallocated spend remain small.
Larger fleets stay outside the product's single-controller scope
(`docs/operations.md` §Supported limits).

### The console reader's bound follows the ceiling

The worker-console reader bounds one run at a small number of console reads. That
bound was sized for a four-worker fleet and counted *records the provider still
lists*, which includes terminated workers: a terminated instance keeps its
ownership tags for about an hour. Turnover therefore looks like an oversized
inventory — twelve slots replacing workers every few minutes exceed the per-run
cap within the hour — and the reader refused (`console_read_cap_exceeded`),
writing no event and no metrics file, exactly while the larger fleet most needed
to be observable.

Two changes keep the bound meaningful without weakening it:

- a record is read only while it can carry new evidence: a worker the provider
  reports live or terminating on every run, and finished only after a read that
  observed it `terminated` or `stopped`. A console exit marker is not that
  signal, because the bootstrap can finish while the instance and its console
  carry on. A finished record is never read again, so turnover costs nothing,
  and live workers are read before termination history;
- candidates beyond the per-run budget are deferred with a note, not refused. A
  deferred record is a candidate on the next run while the provider still knows
  it, so no evidence is lost and the provider work per run stays fixed.

The refusal remains, re-pointed at the drift it was meant to catch: more
non-terminal owned instances than the configured `controller.max_instances`
means the ownership tags disagree with the deployment. Non-terminal is the
controller's definition, not the console reader's: `shutting-down`, `stopping`
and `stopped` still occupy a slot, and only `terminated` frees one.

### Rejected: several deployments as one fleet

Splitting the same queue across two deployments would double the bound without
touching it, but each deployment would observe the same waiting jobs and admit a
worker for them, so the same demand would reserve two instances; the deployments
would also keep independent allowances with no shared exclusion. Forgejo still
assigns each job once, so this is duplicated provisioning rather than duplicated
work, and it defeats the reservation accounting. The supported topology is one
deployment, one controller host, one ledger and one writer, and this change does
not weaken it.

### Rejected: more jobs per worker

One job per instance is what makes a worker's shutdown and the reservation
accounting exact. Multiplexing jobs on one instance would amortise the boot, but
it would also make a worker's termination ambiguous and let one job's footprint
affect another's result. That is a different product change and needs its own
measurement of job interference.

## Risks

- A deployment that raises the bound and leaves its allowance unchanged gains
  launches only up to what the allowance can afford: `min(demand, free slots,
  affordable)` takes the smallest term, so an allowance that was the binding
  constraint stays the ceiling on throughput. An allowance that already afforded
  the old peak now affords the new one. `docs/operations.md` §Sizing the window
  owns the arithmetic.
- A higher ceiling multiplies `hourly_ceiling`. The value is reported, not
  enforced; the window allowance remains the spend guard.

## Migration

None. No state, key or schema change: the bound is read from configuration at
each tick, so a deployment picks up a new value on its next run. Lowering the
bound below the live count still never evicts a worker.
