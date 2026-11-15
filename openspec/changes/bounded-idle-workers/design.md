# Design

## Decision

Two independent halves share one rule: a worker should claim its job soon or go
away, and the controller should assume that a worker it launched recently will
claim one waiting job.

- The worker polls the queue in a bounded claim window instead of blocking in
  `one-job --wait`. When the window ends with no task, it prints one fixed marker
  and shuts down. A job it receives keeps the existing job timeout and instance
  lifetime.
- Before the controller decides how many workers to request, it counts each
  recent launch as covering one waiting job, for a claim grace.

Neither half needs the other. The worker half is user data that an operator must
publish as a launch-template version. The controller half is controller code and
one optional configuration key.

## Terms

| Term | Meaning |
|---|---|
| Poll | One run of the runner's `one-job` mode without `--wait`. It asks the queue once. |
| Empty poll | A poll that the bootstrap recognises as having found no task, by its exit status, its message and the absence of a process of the job account. |
| Finished attempt | Any poll outcome that is not an empty poll. It ends the claim loop. |
| Complete line | The documented no-task message followed by one line feed, with no other byte on that line. |
| Claim window | 300 seconds, measured from the moment registration returns, in which the worker polls. |
| Claim deadline | The end of the claim window. |
| Claim timeout | The end of the worker when the claim deadline has passed, every poll that returned was empty and no poll is running. |
| Recent launch | An owned operation in the `starting` or `running` state, created less than the claim grace ago. |
| Claim grace | `controller.claim_grace_seconds`. Each recent launch covers one waiting job for this long. |

## The claim window

After `register` returns, the bootstrap records the claim deadline and loops:

1. Run one poll as the job account's runner, under the existing job timeout, with
   a bounded copy of its output captured. The output also reaches the console
   while the poll runs (see "The console copy").
2. Classify the outcome (see "Recognising an empty poll").
3. After an empty poll, if the deadline has passed, print `STEP claim-timeout` and
   leave. Otherwise pause 10 seconds. After the pause, if the deadline has passed,
   print `STEP claim-timeout` and leave. Otherwise poll again. After any other
   outcome, stop polling and report the outcome through the existing one-job
   marker.

The deadline is checked between polls only: after each empty poll and again after
each pause. No poll starts at or after the deadline. A poll that ends empty less
than one interval before the deadline is followed by a pause and a second check,
so the next poll does not start. A running poll may already hold a job, so the
deadline never stops it. A job that a poll receives stays bounded by the job
timeout and the instance lifetime, exactly as before. The independent lifetime
shutdown stays the outer bound on every path, including a poll that hangs without
a task.

The window and the interval are single literals in `bootstrap.sh`. That file is
the published user data, which `CONTRIBUTING.md` already names as the one place
where literals replace configuration. They are not parameters, not tags and not
controller input. The values 300 and 10 are starting points, not measurements
(see the evidence gaps).

Each poll costs the Forgejo server at least one request. The fleet limit, at most
12 workers, bounds how many workers poll at once. A job queued after a worker's
latest poll is claimed within about one interval.

## Recognising an empty poll

The loop continues only on a documented signature. A poll is empty when all of
these hold:

- the `one-job` process exited with the documented no-task exit status,
- its captured output contains the documented no-task message as a complete line,
  and
- no process of the job account still exists when the bootstrap reads the capture.

Every other outcome is a finished attempt and ends the loop. That covers a
completed or failed job, one half of the signature without the other, an
oversized, unreadable or missing capture, a signal, a timeout, a poll that leaves
a process of the job account running and any outcome the bootstrap does not
recognise. The rule fails toward ending. A runner that changes its message or its
status makes the worker stop after one attempt. It never makes the worker poll
without a bound, and the existing one-job marker still reports the real status.

Why both halves. The message is only text, and the job account is the same
account that runs the runner, so code under test may be able to write that text
by some route. A check that trusts such text is forgeable by the code under test,
which is also why the console markers carry no free text. The bootstrap therefore
closes the routes it controls and does not rely on the message alone:

- The capture is created by root, bounded in size and kept out of the
  runner-owned directory, so the job account cannot open it by its path.
- The bootstrap reads the capture only after the one-job process has exited. A
  process of the job account that still exists at that time makes the poll a
  finished attempt, so no job process can write into the capture after the
  bootstrap has read it.
- The exit status is the half that job code cannot write, provided that the
  runner does not end a claimed job with the documented status. The spec
  therefore allows only a documented status from 2 to 123. The runner process of a
  claimed job can end with 0 after a failed job (the reader already says so), with
  124 or 137 when the job timeout ends it, with a status of 128 or more after a
  signal and with 1 as the usual failure status. If the pinned runner documents an
  empty poll with a status outside that range, the message half would be the only
  guard, so task 1.1 stops and reports instead.

The runner itself writes into the capture through the descriptor the bootstrap
gives it, so closing the path does not close every route. Whether the runner's
child processes inherit that descriptor is unverified, and no source in this
repository says. A job step that could write to it would forge the message half
only, and the status half would still have to match. A forgery then needs the
runner of a claimed job to end with the documented status. The status range and task
7.2 bound that, and task 7.3 tests it with no process of the job account left
running, so the survival rule cannot mask a status that matches. Task 3.2 tests the
survival rule with a matching status and message, and task 7.8 observes a live
surviving process. Open question 2 records the
decision that the check feeds.

The bound limits what the bootstrap reads. It must not limit what the runner
writes. A poll that receives a task is one process for the whole job, so its
output can exceed any small bound. A bound applied on the write side can stop that
process, because a closed pipe signals the writer and a full pipe blocks it. The
implementation keeps draining the output after the bound is reached, and a test
with a stub that writes more than the bound proves that the process is not
stopped. An oversized capture is a finished attempt.

The repository documents the message `Error: no task received` for the pinned
runner. It does not record the exit status. The implementation task reads the
status from the pinned runner's contract and stops and reports if no source states
it, or if it lies outside 2 to 123. This change does not write a number. A runner
version bump reopens the signature, because the tests pin the status and the
message.

## The console copy

`export` classifies the `runner_error` and `forgejo_ping` kinds from console
lines, and today the runner's output reaches the console as it is written. The
capture does not replace that path. The bootstrap keeps sending the output of the
one-job process to the console while the process runs, and the capture is a
second, bounded copy that serves only the classification of the poll. A design
that only captured would show a long job's runner output at the end, or never when
the instance is reclaimed first.

The bootstrap may omit the documented no-task line of an empty poll from the
console. That keeps a claim timeout from reading as a failure if the runner logs
that line at error level. Task 7.5 records the console text of one empty poll. If
any other line of an empty poll matches a failure rule, that task stops and
reports, because the spec requires that a claim timeout writes no such line.

## One marker for a claim timeout

The worker prints `STEP claim-timeout` once, on the claim-timeout path only. It
prints neither `STEP job` nor a `bootstrap one-job` marker, because both describe
a job attempt and none happened. The claim-timeout path therefore cannot fall
through into the code after the poll, which prints both. The reader's step
vocabulary is a closed set in code, so the reader needs the new name added to that
set. The marker then travels as an ordinary `step` event with the same identity
form, the same schema and the same metrics. No new event kind is needed and no
console text is published.

The worker still leaves through the EXIT trap, which prints
`bootstrap exiting rc=<n>`, runs `cleanup_token`, syncs and shuts down. The trap
does not remove the registration file. The job path removes it in the
straight-line code after the poll, so the claim-timeout path removes it with the
same guarded command before it exits (`rm -f ... || echo "WARNING: ..."`). The
guard keeps a failed removal from changing the exit status, and no job marker is
printed for it.

A claim timeout exits 0. A window that ends on schedule is the designed outcome,
so the exit marker must not read as a failure. The reader publishes the exit
marker's status as `rc` and does not turn it into a failure kind, and 124 and 137
are the codes that mark a job timeout, so this path must not use them. A completed
job also exits 0. The reader tells the two apart by the `claim-timeout` step and
by the missing one-job marker.

## The claim grace

`_decide` computes `free = limit - instances` and `requested = min(demand, free)`.
Demand is the number of Forgejo jobs in the `waiting` state. A job stays waiting
until a worker claims it, and a worker launched for that job needs time to boot,
register and poll. Each apply tick, about every 60 seconds, can therefore launch
another worker for the same job, up to the free slots. Forgejo's runner list
reports only whether a runner is active or offline, and a job record names no
runner, so the controller cannot link a worker to a job.

The controller uses time instead. Each recent launch covers one waiting job:

```
recent    = starting or running owned operations,
            created less than claim_grace_seconds ago
requested = min(max(0, demand - recent), free)
```

The existing `slot_busy` and `no_runnable_demand` returns stay first, and they
report the count too. When the demand that remains uncovered is zero, the
decision is `wait` with the reason `awaiting_claim`. Every decision that `_decide`
returns reports `recent_launches`, so `plan` and `apply` explain a reduced or
empty batch with a number. A result that ends before `_decide` runs has no count
to report: `apply` returns the owned-state discrepancy refusal before admission,
and `plan` returns an invalid state, a changed scope and a refused observation
before it calls `_decide`. A deadline deferral in `apply` can replace a computed
wait or refusal with a deferral that holds no decision, while a deferral in the
launch phase keeps the merged decision and so carries the count. The spec requires
the count on every decision that `_decide` returns and does not require it on the
other results. `status` builds no decision, so it reports no count. Each
operation's `created_at` is already in its slot listing.

Which operations count. Only an operation in the state `starting` or `running`
covers a job. An operation in the state `terminating` belongs to an instance that
is shutting down or that expiry cleanup is terminating, so its worker will not
claim a job, and counting it would hold back a launch for up to the grace after a
short job. The cost of excluding it is slightly more launches during a burst. A
`terminating` operation still occupies a slot until it is terminal, because `free`
counts every operation that is not terminal or failed.

An operation in the state `intent` or `uncertain` has no recorded instance. In
`apply`, the controller resumes every operation without an instance before it
computes admission, and it computes no admission decision in that pass. When the
provider reports the instance, reconciliation rewrites the operation to
`starting`, `running`, `terminating` or `terminal`. The operation keeps its
`created_at`, so an adopted worker covers a job from the next pass on while it is
inside the grace.

`plan` reads durable state only and does not resume or reconcile, so it can differ
from `apply` in two ways. It can show a launch for a job that `apply` holds back
while an operation has no instance. That difference exists today. It also counts a
stored `starting` or `running` operation that reconciliation would rewrite to
`terminating`. The
earlier rule counted both states, so that second difference is new, and it exists
only until the next `apply` reconciles the row. The plan scenario therefore holds
when the ledger matches the provider.

Example, with a limit of 4:

| Waiting jobs | Non-terminal workers | Recent | Free slots | Requested |
|---|---|---|---|---|
| 1 | 1 | 1 | 3 | 0 (`awaiting_claim`) |
| 3 | 2 | 2 | 2 | 1 |
| 3 | 2 | 0 | 2 | 2 |
| 1 | 2 | 2 | 2 | 0 (`awaiting_claim`) |
| 1 | 1 (`terminating`) | 0 | 3 | 1 |

With the default limit of 1, the free-slot bound already stops a second launch
while one worker is non-terminal. The grace matters only when
`controller.max_instances` is above 1.

The clock is the operation's `created_at`. Every row has it, and `plan` and
`apply` already read it. `launch_time` is recorded only after a reconcile observes
the instance, so it is empty exactly when a launch is youngest. The cost model
already falls back from `launch_time` to `created_at` when the first is empty. No
ledger or schema change is needed.

The default equals the bootstrap's claim window, 300 seconds, so the coverage of a
launch ends at about the time the worker's own claim window ends. The range 60 to
1800 is proposed, not measured (open question 1).

Costs and limits of a time-based grace:

- A recent launch that has already claimed its own job still covers one waiting
  job until the grace ends. A job that becomes runnable in that time can wait up
  to the grace for its own worker. A lower grace shortens that wait and weakens
  the protection against a repeat launch.
- The grace clock starts when the ledger row is created and the claim window
  starts at registration, so the grace ends earlier than the claim window by the
  boot time. A job that a slow-booting first worker was about to claim can get a
  second worker. That worker finds nothing and ends at its own claim deadline. The
  cost is one extra short-lived worker per lost race, bounded by the slot limit and
  the allowance.
- After the grace, a job that is still waiting can get a new worker. That is the
  recovery path for a worker that failed bootstrap or never claimed.

## Credential boundaries

No credential changes. The bootstrap still reads the registration token as root,
removes it right after `register` and before the first poll, and runs the runner
as the job account with the metadata service blocked. The capture holds only
runner output that already reaches the console. The job account cannot open it by
path, and task 7.3 qualifies the descriptors. The controller half reads ledger
rows it already reads, makes no new AWS call and takes no new credential.

## What this does not claim

- This is not a cap on the AWS bill. A reservation still prices a worker for its
  full configured lifetime, and settlement still uses the provider's time. The
  claim window shortens how long a worker with no job is expected to run. A poll
  that hangs, a failed shutdown or a provider fault is still bounded only by the
  instance lifetime and by independent expiry cleanup.
- The claim deadline does not state a maximum idle time of 300 seconds. Boot time
  comes before the window, and the deadline is checked between polls.
- The grace is not proof that a recent worker is healthy or will claim a job. It is
  an estimate that stops a repeat launch for a short time.
- The worker markers are evidence of what the worker printed. They are not a Forgejo
  job result.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| A poll exits with an unrecognised status or message | Finished attempt. The existing one-job marker, cleanup and shutdown. No further poll. |
| The capture cannot be created, read or bounded | Finished attempt. Same path. |
| A process of the job account outlives the one-job process | Finished attempt, even when the status and the message match. The bootstrap reads the capture only after the one-job process has exited. Same path. |
| No task arrives before the deadline | `STEP claim-timeout`, the guarded registration-file removal, exit status 0, cleanup and shutdown. A job that is still waiting stays waiting, and the controller can launch a replacement after the grace. |
| A poll hangs without a task | The claim deadline does not interrupt it. The job timeout and the instance lifetime end it, as they end a hung wait today. |
| A Spot interruption notice arrives during the claim window | The watcher's existing retry signals the next `runner`-owned `forgejo-runner` process it finds, and a signalled poll is a finished attempt. A poll that starts after the notice can still receive a task before the signal lands, and the signal then ends that job. This change adds no stop condition for it, because handling Spot interruptions is a non-goal. |
| Empty polls invalidate the registration (unverified) | The next poll fails with something other than the empty result, so the loop ends as a failed attempt and the worker shuts down. The controller can then launch again, bounded by the slot limit and the allowance. This is why section 7 of the tasks qualifies the behaviour before the template is enabled. |
| Job output imitates the empty result | Expected to be rejected: the job account cannot open the capture by path, the bootstrap reads the capture only after the one-job process has exited, a surviving job process ends the loop, and the status must match as well. A descriptor that the runner passes on to its children could still be a route (open question 2). The setup guide says ephemeral mode makes Forgejo delete the runner after one job, so a later poll is expected to be refused. Task 7.3 and task 7.2 qualify both. |
| A claimed job writes more output than the capture bound | The bound limits what the bootstrap reads. The process is not stopped, signalled or blocked, and the oversized capture makes the poll a finished attempt. |
| The runner's no-task status or message changes | The signature stops matching. The worker ends after its first poll instead of looping. |
| A recent worker fails bootstrap or never registers | It covers its job until it enters the state `terminating`, reaches a terminal state or the grace ends. Admission can then launch a replacement. |
| `claim_grace_seconds` is outside 60 to 1800 or not an integer | `CONFIG_INVALID` before any launch is planned. |
| The controller restarts inside the grace | The ledger's `created_at` ages the grace. Nothing resets. |

## Alternatives

- Keep `--wait` and rely on the lifetime. Declined: that is the defect. An idle
  worker is billed for its lifetime.
- Wrap `--wait` in a shorter timeout, or shorten the instance lifetime. Declined:
  a timeout cannot tell an idle wait from a running job, so it also kills a long
  job that was claimed late. The lifetime is the bound for the job as well.
- Reuse a worker for several jobs. Declined: `AGENTS.md` says a worker is
  untrusted after it accepts job code, so a second job must not run on it.
- Link workers to jobs by runner name. Declined: the runner list shows only active
  or offline, with no busy flag, and a job names no runner. The controller still
  could not tell a claimed worker from an idle one.
- Read the worker's console markers during admission. Declined: the worker
  observation record is derived data that must never influence admission, and the
  console is not a trusted channel.
- Capture each poll's output without sending it to the console, and print the
  capture after a finished attempt. Declined: that hides an empty poll but delays
  evidence and can lose it when the instance is reclaimed first.

## Rollback

The halves roll back separately.

- Worker half: revert the bootstrap change and publish the next launch-template
  version. Workers on the earlier version keep using `--wait` and never print the
  new marker. An approved template version is never edited in place. The fastest
  path is the deployment configuration: set the previous approved numeric
  `aws.launch_template_version`, and any pool override, back. Published versions
  are immutable, so the next launch uses `--wait` again and no publication is
  needed. Until the bootstrap revert merges, `scripts/check_prerequisites.py`
  reports `template_bootstrap_drift` for that pin. Do not clear it by publishing
  the new bytes. Workers that already run the new bootstrap end by their own claim
  window or lifetime.
- Reader half: revert the added step name. The reader then ignores
  `STEP claim-timeout`, and the next run drops retained `claim-timeout` event
  lines from the stream, as the closed vocabulary already does for a name it does
  not ship. That is accepted: the lines are evidence for a consumer, not state this
  command needs.
- Controller half: remove `controller.claim_grace_seconds` from the deployment
  configuration first, because an earlier release refuses the unknown key. Then
  revert and deploy. Admission returns to its earlier behaviour.

The order does not matter for correctness. Ship the reader name with or before the
template version so that the first claim timeout is visible. Activation of the
worker half is a deployment step, not a source step: the bootstrap bytes are the
launch-template user data, so an operator publishes a template version before any
worker polls. The controller half takes effect when the controller release is
deployed.

The next unpublished template version is shared with every other bootstrap change
that has not been published yet. The bootstrap tests declare one pending version
and the digest of the repository bytes, and an operator publishes whatever those
bytes are. If this change's bootstrap edit merges before the operator publishes
that pending version, then enabling that version for another change enables the
claim window too, before it is qualified. Either publish the pending version first
and let this change take the next one, or qualify the claim window before anyone
enables that version. The tasks name this in 3.5 and 7.1.

## Evidence gaps

- Whether empty polls keep the ephemeral registration valid on the pinned runner,
  13.2.1, is unverified. If an empty poll invalidated the registration, the worker
  would end after its second poll and the controller could launch again. The
  operator must qualify this live before the template version is enabled.
- The no-task exit status is not recorded in the repository. Whether the pinned
  runner prints the no-task message to standard output or standard error, and
  whether it prints other lines for an empty poll that the reader's failure rules
  could classify, is also unverified. Whether that status can equal a status that
  the runner of a claimed job also ends with is unverified too. The spec allows
  only 2 to 123 for that reason.
- Whether the runner's child processes inherit its standard output or standard
  error is unverified. If they do, a job step can write the documented message
  into the capture through that descriptor. The status half of the signature and
  the survival rule still apply. Task 7.3 tests the route, and open question 2
  records the decision that the result feeds.
- The values 300 seconds and 10 seconds are unmeasured. The repository holds no
  boot-to-claim timing.
- Hypothesis, not a claim: a known Forgejo behaviour may leave a long-polling
  runner idle after it loses a pick race for a job. The runner's `FetchTask`
  request carries the last tasks version it has seen, and the server returns no
  task until that version changes. If so, a runner that waits with `--wait` and
  loses a race could sit idle while a job waits, and part of the idle time seen in
  the pilot would come from that and not from an empty queue. A fresh `one-job`
  process without `--wait` starts without a remembered version, so the bounded
  poll loop may also reduce this idle mode. This is unverified for the pinned
  runner. The live qualification should look for it and record the result, and
  nothing in this change depends on it being true.
- No live run has exercised either half. The source and the policy tests prove the
  shape, not the behaviour. Runtime verification needs one claim timeout and one
  claimed job in the event stream, and one held launch in a `plan` or `apply`
  result.

## Open questions

1. The range, the default and an off value for `claim_grace_seconds`. This draft
   proposes 60 to 1800 with a default of 300. Recommendation: no off value. The
   other controller keys offer none, the lowest value, 60, is the lever for a
   latency complaint, and the rollback is a revert. The alternative is to accept 0
   as off, which gives an immediate configuration rollback but lets an operator
   remove the protection against a repeat launch. A second question is whether the
   default should exceed the worker's window by a boot margin. This change sets the
   default equal to the window and keeps it there.
2. Whether a descriptor that the runner passes on to its children can reach the
   capture. The spec closes the capture's path, requires the capture to be read
   only after the one-job process has exited, and ends the loop when a process of
   the job account survives. It does not claim that an inherited descriptor is
   closed, because the runner's behaviour is unverified. Recommendation: keep the
   signature as specified and let task 7.3 decide. Activation requires evidence
   from task 7.3 that the runner of a claimed job cannot end with the documented
   status; a status range alone is not that evidence. If the check shows that it
   can, treat the empty-poll signature as forgeable and reopen this change before
   the template version is enabled, whatever the inherited-descriptor result. The cost is a design change to the signature, not a
   rollback, because no version is enabled before the check.
