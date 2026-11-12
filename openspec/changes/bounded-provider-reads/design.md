# Design

## Context

See [proposal.md](proposal.md). The controller is one bounded one-shot pass:
reconcile owned state, resume unresolved intent, expire expired workers, observe
demand, admit a batch and launch it. A slow provider read currently has no
budget of its own, so a page, a quote or a console read can consume the time the
later phases need. The deployed unit eventually kills the container at
`TimeoutStartSec=300`, which loses the controller's structured evidence.

## Goals / Non-Goals

**Goals:** bound a single transport call; bound the whole pass below the
systemd unit's start timeout; make an interrupted pass explicit and
deterministic; preserve the frozen intent and token on every mutation path;
keep the independent reaper outside the scaler's budget.

**Non-Goals:** a new scheduler, a background cancellation thread, a provider
plugin framework, changing retry counts, changing `reap`, `status` or `export`,
or claiming that a provider read was completed when it was not.

## Decisions

### The deadline lives in one `apply` pass

`apply` computes `deadline = clock() + controller.pass_deadline_seconds` once
and threads it to `_reconcile`, `_decide` and `_expire`; `Aws` read methods take
it as a keyword-only argument. `plan`, `status`, `reap` and `export` call the
same methods without it, so their behaviour is unchanged. The deadline is an
instant in the controller's clock domain; the fake clock makes the tests
deterministic without sleeps.

### The pass has an ordered phase list

The phases are `inventory`, `reconcile`, `resume`, `expire`, `observe`,
`admission` and `launch`. The deadline is checked before each phase starts and
before every read inside admission. When it is reached, the result names the
suffix that was not completed, starting with the phase that could not complete:
`phases_deferred`. The phase the deadline interrupted is included because it did
not complete. A local `DeadlineRefusal` is the one control-flow signal; it is a
small `Refusal` subclass with the reason `pass_deadline_exceeded`, not a second
error mechanism.

### Transport bounds are not the pass bound

The EC2 and STS clients carry `connect_timeout=10` and `read_timeout=30`
alongside the existing `total_max_attempts=1` policy. These bound one in-flight
call; they do not change retries or the number of calls. The pass deadline
bounds the sequence of calls. A call already in flight is not interrupted; the
next phase check, or the next page or identifier check, refuses after it
returns.

### Mutations never take the deadline

The deadline is checked before `_launch` and before `_expire` selects targets,
but never inside `_send`, `RunInstances`, `TerminateInstances` or the ledger
transaction that records them. The frozen intent and client token are committed
before `_send`. If the process stops after a launch may have been accepted, the
existing uncertain reconciliation path keeps the operation and the next pass
resumes the same request and token. A batch that has started is not abandoned
half-way because its pass budget elapsed.

### The independent reaper stays outside the pass

`reap --apply` runs from its own unit and timer on the cleanup host, without the
scaler's pass deadline. `reap`, `status` and `export` keep their current call
shapes and behaviour. Expiry cleanup therefore remains available when an
`apply` pass defers, refuses or the scaler is stopped.

## Risks / Trade-offs

- A pass that reaches the deadline before `resume` leaves a frozen intent
  unresolved until the next scheduled run; `phases_deferred` says so. That is
  safer than sending a mutation without budget for its reconciliation.
- A provider call that starts just before the deadline can run to its transport
  timeout (30 s). The next check catches the overrun; the pass does not claim
  the read finished within budget.
- The deadline uses the controller's clock. In production that is wall-clock
  time, so a large clock change could shorten or extend one pass; the systemd
  `TimeoutStartSec` remains the outer bound.
- The default 240 s is below the shipped `TimeoutStartSec=300` but above the
  smallest allowed `max_lifetime_seconds=60`. The pass deadline and worker
  lifetime are different bounds; a deliberately short-lived worker is allowed.
