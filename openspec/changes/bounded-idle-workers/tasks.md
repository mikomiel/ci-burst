# Tasks

Sections 1 to 6 are product-source tasks. They authorize no deployment, no
credential, no template publication and no cloud change. Section 7 lists the
operator steps that need their own approval, and no source task stands in for
them.

## 1. Worker claim window

- [x] 1.1 Read the pinned runner's exit status and exact output for a `one-job`
  poll that finds no task, from the runner's own source or release notes. Record
  both in `docs/runner-setup.md`, which today documents only the message
  `Error: no task received`. If neither source states the status, stop and report
  the gap. If the status is outside 2 to 123, stop and report it, because the spec
  allows only 2 to 123 and the runner of a claimed job can end with the other
  statuses. Do not guess a number.
- [x] 1.2 In `deploy/runner/bootstrap.sh`, define the claim window (300 seconds),
  the poll interval (10 seconds), the documented no-task status and the documented
  no-task message as single literals beside the lifetime literal, and start the
  window when `register` returns.
- [x] 1.3 Replace the blocking `one-job --wait` call with a loop that runs
  `one-job` without `--wait` under the existing job timeout and pauses the poll
  interval between polls.
- [x] 1.4 Capture a bounded copy of each poll's output in a file that root creates
  with mode 0600 outside the runner-owned work directory, and remove it with the
  other cleanup. The property to keep is that the job account cannot open the
  capture by its path. The bound limits what the bootstrap reads, so keep draining
  the runner's output after the bound is reached. The runner must never be
  stopped, signalled or blocked by it. Read the capture only after the one-job
  process has exited, and end the loop as a finished attempt if a process that the
  job account owns still exists then. The mechanism is a choice for this task. It
  must meet those rules and the capture rules of the spec, and task 3.2 tests them
  with a stub child process that writes the documented message to every
  descriptor it inherits.
- [x] 1.5 Keep the loop open only when the poll exited with the documented
  no-task status and the capture holds the documented message as a complete line,
  that is the message followed by one line feed with no other byte on the line.
  End the loop on every other outcome and report it through the existing one-job
  marker.
- [x] 1.6 Check the claim deadline after each empty poll and again after each
  pause, so that no poll starts at or after the deadline. Do not stop a running
  poll at the deadline.
- [x] 1.7 At the deadline, print exactly one `STEP claim-timeout` marker. Print
  neither `STEP job` nor a `bootstrap one-job` marker, and do not fall through
  into the code after the poll, which prints both. The EXIT trap (`on_exit`)
  prints the exit marker, runs `cleanup_token`, syncs and shuts down, but it does
  not remove the registration file. On this path, remove the registration file
  with the same guarded `rm -f ... || echo "WARNING: ..."` form that the job path
  uses, so that a failed removal cannot change the exit status. Then leave through
  the EXIT trap with status 0.
- [x] 1.8 Exit 0 after a claim timeout and state it in `docs/runner-setup.md`.
- [x] 1.9 Keep the job timeout and the lifetime shutdown literals unchanged.
- [x] 1.10 Keep sending the output of each poll to the console while it runs. Omit
  only the documented no-task line of an empty poll.

## 2. Reader

- [x] 2.1 Add `claim-timeout` to `STEP_NAMES` in `ci_burst/export.py`. Leave
  `EVENT_SCHEMA`, the event identity form and the metrics unchanged.
- [x] 2.2 In `tests/test_export.py`, copy the pattern of the existing step and
  record-validation tests. Cover: the marker becomes one `step` event, a worker
  that registered and then timed out is counted as registered once, a lookalike
  name such as `claim-timeouts` is still ignored, a persisted record that holds
  the name survives sanitising, and the marker does not finish a record.

## 3. Bootstrap tests

- [x] 3.1 In `tests/test_runner_bootstrap.py`, replace
  `test_runs_exactly_one_job_and_waits_for_it`, which pins `one-job --wait`, with
  tests of the claim loop. Re-point the status-preservation, umask and
  watcher-order tests at the new poll invocation. The metadata-block test never
  names the invocation and stays as it is. Rework `BootstrapTailTests`: its
  `_tail()` starts at the exact line `JOB_RC=0` and its harness stubs only
  `timeout` with one constant status. Keep an anchor that the extraction can use,
  or move it on purpose. Keep exactly one `JOB_RC=0` line, or change that
  assertion on purpose. Give the extracted loop its stubs (`sleep`, the capture
  path and the clock). Add a case for the claim-timeout path beside the existing
  job cases. Keep both existing cases (the status reaches the marker and the exit
  status, and a failed registration removal cannot replace the job result). Do not
  weaken any of these tests.
- [x] 3.2 Run the loop in a test with stubbed `forgejo-runner`, `timeout`, `sleep`
  and clock. Cover: empty polls then a claim timeout, empty polls then a job,
  each outcome that is not the documented empty result ending the loop (status
  alone, message alone, a signal, an oversized capture, a missing capture), no
  poll after a finished attempt, no poll that starts at or after the deadline
  (after an empty poll that ends less than one poll interval before it, and after
  an empty poll that ends at or after it), no one-job marker for an empty poll, no
  deadline check during a running poll, a stub that writes more than the capture
  bound and still runs to its own exit status, a stub child process that writes
  the documented message to every descriptor it inherits while the stub runner
  exits with a status other than the documented one (the loop ends), and a stub
  child process that outlives the one-job process while the status and the message
  match (the loop ends).
- [x] 3.3 Test that the capture is created by root with mode 0600 outside the
  runner-owned directory and is removed during cleanup. Test that the
  claim-timeout path removes the registration file. Test that a failed removal (a
  non-empty directory at the path) prints no `bootstrap one-job` marker and does
  not change the exit status.
- [x] 3.4 Feed the stubbed console of an idle worker to `parse_console` and assert
  that it yields the `claim-timeout` step, an `exit` event with `rc` 0 and no
  failure kind.
- [x] 3.5 Update the pending publication pair (`PENDING_TEMPLATE_VERSION` and
  `PENDING_TEMPLATE_SHA256`) so the repository bytes equal the declared state.
  Do not flip the published pair. That is the operator's step in section 7. The
  pending version is shared with any other bootstrap change that is not published
  yet, so check what is unpublished before this edit merges, and follow the
  activation note in the design.
- [x] 3.6 Test the status band. Assert that the documented no-task status literal
  in `deploy/runner/bootstrap.sh` is from 2 to 123. Assert that a poll whose
  capture holds the documented message ends the loop as a finished attempt for
  each of the statuses 0, 1, 124, 125, 128 and 137.
- [x] 3.7 Test that runner output reaches the console while a poll runs. Use a stub
  that writes a line and then waits, and assert that the line reaches the console
  before the stub exits. Assert that every line other than the documented no-task
  line of an empty poll reaches the console.

## 4. Controller claim grace

- [ ] 4.1 In `ci_burst/config.py`, add `claim_grace_seconds` to `ControllerConfig`
  and to the controller table's allowed-keys tuple, with the range and default
  from the spec (design open question 1 can change them). Keep the refusal of
  unknown keys.
- [ ] 4.2 In `ci_burst/controller.py` `_decide`, count the recent launches from
  `slot["operations"]`. Count only operations in the state `starting` or
  `running`, and skip every other state before the age test. Use each operation's
  `created_at` and the controller clock. Compute the count before the `slot_busy`
  and `no_runnable_demand` returns, and keep those returns first. Then return
  `wait` with `awaiting_claim` when no demand is left uncovered, and compute
  `requested = min(max(0, demand - recent_launches), free)`. Report
  `recent_launches` in every decision that `_decide` returns. Add no field to a
  result that ends before `_decide` runs.
- [ ] 4.3 In `tests/test_config.py`, test the default, both bounds, a value just
  outside each bound, a non-integer value and the refusal of an unknown key.
- [ ] 4.4 In `tests/test_controller.py`, test each scenario in the delta: a booting
  worker covers its job, demand above the recent launches, demand below them, a
  terminating worker that covers no job, a replacement after the grace, a
  replacement when the recent worker reaches a terminal state first, the
  precedence of the existing reasons (including that `slot_busy` and
  `no_runnable_demand` report `recent_launches`), a restart inside the grace, and
  `plan` reporting what `apply` uses without mutation (with a ledger that matches
  the provider inventory). Set
  `controller.max_instances` above the number of seeded operations in every case
  that expects `awaiting_claim` or a launch, because the default limit of 1 ends in
  `slot_busy` first.
- [ ] 4.5 Audit the existing controller tests. `seed(...)` creates a row at the
  test clock's current time, so a seeded operation in the state `starting` or
  `running` is inside the grace, and a test that runs `apply` twice can be inside
  it as well. Give such a row an age beyond the grace unless the test is about
  coverage, and do not change an expectation without naming the reason in the
  test.
- [ ] 4.6 Add a test that the controller's default grace equals the claim-window
  literal in `deploy/runner/bootstrap.sh`.

## 5. Documentation

- [x] 5.1 `docs/runner-setup.md`: replace the paragraph that says `one-job` polls
  once and that `--wait` blocks. State the claim window and the poll interval, the
  no-task status and message, the `claim-timeout` marker, the exit status of a
  claim timeout and the live-qualification requirement. Update the bootstrap
  summary that says the worker runs exactly one job. In section 5 (Verify), change
  step 1 so that it also describes a launch with no queued job, and change step 2
  so that it names `STEP claim-timeout` as the expected record of that launch.
- [ ] 5.2 `docs/operations.md`: document the new controller key in the
  configuration section, the `claim-timeout` step in the event stream section, and
  the new bound in the "Fleet size and fan-out" section (formula, the
  `awaiting_claim` reason, `recent_launches`, the grace and its limits). Rewrite
  the paragraph that says an idle worker waits for one job until its maximum
  lifetime. Keep its statement that a reservation prices the full configured
  lifetime, and describe the claim window as the expected end of an idle worker,
  not as a bound. In "Worker image and launch template", replace "a maximum
  lifetime that covers image boot, a delayed job and a failed bootstrap" with a
  lifetime that covers boot, the claim window, one job and a failed bootstrap, and
  name the claim window in the line that says the worker exits "when no job
  arrives". In the "Qualifying live support" section, in the paragraph that begins
  "Only an isolated operator canary", replace "an idle worker expires without
  taking a job" with an idle worker that ends at its claim deadline without taking
  a job.
- [ ] 5.3 `docs/deployment.md`: add the optional `claim_grace_seconds` key to the
  controller block with its range and default.
- [x] 5.4 `docs/pilot-checklist.md`: reword the bootstrap item that says the worker
  exits on no job so it names the claim window. Reword the item "Maximum lifetime
  covers boot, queue wait and a failed bootstrap" so that it names the claim window
  and the job, not queue wait. Add an unticked item for the qualification in
  section 7. The checklist records preparation. It is not an authorization.
- [ ] 5.5 Do not edit `CHANGELOG.md`. release-please owns it.

## 6. Verification

- [x] 6.1 `npm run check:policy` and `npm run check:spec` pass on the change.
- [ ] 6.2 Run the Gitleaks scan described in `CONTRIBUTING.md` on the change
  before any push. The change adds no host name, address, account value or
  deployment name.

## 7. Operator live qualification (not source tasks)

These steps need operator approval, a bounded scope and a cleanup plan. Listing
them authorizes nothing. Record the evidence in the operator's own deployment
records, not in this repository.

- [ ] 7.1 Publish the next launch-template version that carries the new user
  data. Do not set it in the deployment configuration yet. The setup guide owns
  the procedure. That version also carries every other unpublished bootstrap
  change, and enabling it for one of them enables all of them, so decide the order
  first (see the design's activation note).
- [ ] 7.2 Qualify the pinned runner's `one-job` contract with one bounded launch
  from that version, as the setup guide's verify section describes, before the
  version is enabled. Confirm the no-task exit status and the message. Confirm
  that the ephemeral registration stays valid across repeated empty polls and is
  consumed by the first claimed task. Confirm that a poll after a claimed task is
  refused and does not read as an empty poll.
- [ ] 7.3 Run one adversarial check with no process of the job account left
  running when the step ends. From a job step, as the job account, try to write
  the documented no-task message to the capture path, to the runner's own
  descriptors (including through `/proc`) and to a descriptor that a child process
  inherits. In the same job, try to make the runner end with the documented no-task
  status: end the step with that status, and end the runner's child processes with
  it. Record the runner's exit status for each attempt, and record whether it can
  equal the documented no-task status. Confirm that the worker does not poll again
  after that job. Record whether a child of the runner inherits its standard output
  or error. Both records decide design open question 2: the status and the
  inherited-descriptor results must show that a claimed runner cannot end with the
  documented status, or the signature is redesigned before activation.
- [ ] 7.4 Observe one claim timeout (launch with no job queued) and one claimed
  job in the event stream. For the claim timeout, expect the registration step,
  the `claim-timeout` step, the exit marker with `rc=0` and then provider
  termination, with no failure event. For the claimed job, expect the `completed`
  one-job marker.
- [ ] 7.5 Record the console text of one empty poll, and the boot-to-claim time of
  a few workers, so the operator can review the 300 second and 10 second
  literals. Look for the idle mode named in the design's evidence gaps. If any line
  other than the documented no-task line matches one of the exporter's failure
  rules, stop and report, because the spec requires that a claim timeout writes no
  such line.
- [ ] 7.6 Only after 7.2 to 7.4 and 7.8 hold, and 7.3 has recorded that a
  claimed runner cannot end with the documented no-task status, set the new version in the deployment
  configuration and run the prerequisite check.
- [ ] 7.7 With the controller half deployed, observe that one waiting job does not
  launch a second worker while the first worker is still booting, and that a
  replacement is launched after the grace when the first worker never claims.
- [ ] 7.8 Run the surviving-process case separately from 7.3. From a job step,
  leave a background process of the job account running after the step ends, and
  write nothing to the capture or the runner's descriptors. Record that the
  background process still exists when the bootstrap classifies the poll, and
  confirm that the worker does not poll again after that job. This live check does
  not prove the survival rule on its own, because an ordinary job status and no
  forged message already fail the other signature checks; task 3.2 tests the rule
  with a matching status and message.
