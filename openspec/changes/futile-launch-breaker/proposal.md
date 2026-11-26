# Proposal

## Why

A persistent worker-side fault makes every launched worker end before it
registers. Examples are an unreadable registration parameter, a failed
registration, or an image that lacks a command the bootstrap needs. The
bootstrap's exit path shuts the instance down, so the slot frees while the job
still waits, and the next apply pass launches again. The claim grace does not
stop this: it covers a job only while an operation is `starting` or `running`.
The allowance still bounds spend, but every launch in the window can be wasted
(GitHub issue #139).

## What Changes

- `plan` and `apply` gain one refusal, `recent_launches_futile`. It holds when
  the three most recent finished workers in the last 60 minutes all ended
  without registering.
- The evidence is the record that `export` already writes beside the ledger.
  The controller reads it and never writes it. A worker counts as futile only
  when its record shows that the instance ended, shows the bootstrap's exit
  marker, and shows no sign of registration, and when the ledger holds the
  worker's operation for this deployment. A rename therefore starts without the
  hold of the old name.
- The gate sits in `_decide`, after the earlier wait reasons and before the
  launch template is read. It is a refusal like `budget_allowance_exhausted`,
  and the command exits 2 as it does for any refusal.
- A missing, unreadable, stale or malformed record is no evidence. It does not
  block admission, and the decision reports which state it found.
- The worker-observation requirement that the record never influences admission
  is narrowed to allow this one read-only use. It now also binds `export` to
  write the record last, on every successful run, and to drop a record that is
  dated after the clock of its run when it loads the record, so that one date set
  ahead by a clock that was corrected cannot keep every new file `malformed`.
- Every decision that reaches the gate reports `launch_evidence`.
- The operator, deployment and container guides describe the gate, its limits
  and its one blunt lever.

## Impact

- A hold withholds capacity. After a transient fault is fixed, admission stays
  closed until the evidence ages out, up to 60 minutes after the newest futile
  worker was read.
- Under a persistent fault the gate thins the waste and does not end it. At one
  slot it allows about three wasted launches per hour. With more free slots a
  whole batch can launch each time the gate opens. The gate is not a cap on
  wasted launches and not a spend control. The configured allowance stays the
  only spend control. It is an estimate, not an AWS billing cap.
- Evidence lags the end of a worker by about one export interval, so the first
  burst of futile launches is not prevented.
- The export unit stays optional. Where it is absent, stopped or older than 600
  seconds, the gate never holds and admission behaves as it does today.
- No new provider or queue call, credential, mount, IAM permission,
  configuration key, ledger column or record field. The reaper, the queue
  observer, cost tracking and job results are unchanged.
- Activation is a deployment step, not a source step: an operator runs the new
  image and keeps the export unit writing the record where `apply` reads it.
- Non-goals: an operator acknowledgement or reset command, a `status` field for
  the hold, making `export` mandatory, console reads inside `apply`, a signal
  stored in the ledger, a configurable count or window, a per-pool or per-zone
  breaker, and any change to the reaper.

## Differences from the issue

- The issue allows a cool-down or an operator acknowledgement. This change has
  the cool-down only, because the window ages out by itself. An acknowledgement
  is design open question 1.
- The issue asks to report the reason in `plan` and `status`. `plan` reports it,
  because `plan` and `apply` share one decision. `status` builds no admission
  decision, so it has no verdict to report.
- The issue offers a spec change or a ledger signal. This change takes the spec
  change.
- The issue's rule is a missing registration marker. That marker alone is weak,
  as the issue says, so the gate also requires the exit marker and no marker
  that only a registered worker prints.
