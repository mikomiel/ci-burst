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
without patching the product; the refusal above the new ceiling is explicit; the
default and every existing behaviour are unchanged.

**Non-Goals:** dynamic or queue-driven ceilings, multiple concurrent controllers,
warm pools, On-Demand capacity, per-worker capacity greater than one job, or any
change to reservations, placement, expiry and cleanup.

## Decisions

### A static product bound, not a governor

A ceiling that a controller raised and lowered itself would be a second owner of
capacity policy: the launch count is already demand-driven, so an adaptive
ceiling adds no launches that demand would not have produced, and it would need a
config-writing path or a new durable policy input. Admission stays single-owned
and observable, and an operator who wants a different peak edits one integer.

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

### Rejected: several deployments as one fleet

Splitting the same queue across two deployments would double the bound without
touching it, but each deployment would observe the same waiting jobs and race to
expire its own intent, so the same job could be provisioned twice. The supported
topology is one deployment, one controller host, one ledger and one writer, and
this change does not weaken it.

### Rejected: more jobs per worker

One job per instance is what makes a worker's shutdown and the reservation
accounting exact. Multiplexing jobs on one instance would amortise the boot, but
it would also make a worker's termination ambiguous and let one job's footprint
affect another's result. That is a different product change and needs its own
measurement of job interference.

## Risks

- A deployment that raises the bound and leaves its allowance unchanged sees no
  extra launches: the allowance caps the batch first. That is the intended
  interaction, and `docs/operations.md` §Sizing the window owns the arithmetic.
- A higher ceiling multiplies `hourly_ceiling`. The value is reported, not
  enforced; the window allowance remains the spend guard.

## Migration

None. No state, key or schema change: the bound is read from configuration at
each tick, so a deployment picks up a new value on its next run. Lowering the
bound below the live count still never evicts a worker.
