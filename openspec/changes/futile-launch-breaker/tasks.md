# Tasks

These are product-source tasks. They authorize no deployment, no credential, no
template publication and no cloud change. Fixtures are synthetic: made-up
instance identifiers and 32-character hexadecimal operation identifiers.

## 1. Reader

- [x] 1.1 Add one helper that returns the default record path,
  `<dirname(state.database)>/export/observed.json`, and use it in `_export` in
  `ci_burst/cli.py` and in the gate, so the path has one owner. `--state-file`
  keeps overriding only the export run.
- [x] 1.2 Add one named constant beside `STEP_NAMES` in `ci_burst/export.py` that
  lists the steps the shipped first-boot script prints only after the register
  step. Do not copy the step names into a second list.
- [x] 1.3 Add `ci_burst/launch_evidence.py` with one function that takes the
  record path and the controller clock reading and returns the evidence state,
  the considered count and the futile count. It imports the record version, the
  final provider states, the operation pattern and the constant from 1.2 from
  `ci_burst/export.py`.
- [x] 1.4 Open the file once, take the modification time from the open
  descriptor, and read the file whole. Test the states in the order `missing`,
  `unreadable`, `stale`, `malformed`. The function never raises, never writes,
  repairs, locks or deletes, and maps any failure it did not foresee to
  `unreadable`.
- [x] 1.5 Define the fixed values as named constants: 3 workers, a window of 3600
  seconds and a staleness bound of 600 seconds. Offer no configuration key and no
  off switch for them.
- [x] 1.6 Validate the file shape and the fields the gate reads in every record.
  `seen` is a finite number. `state`, `operation`, `exit_code`, `job_process` and
  `spot_interruption` may be null, and `exit_code` is otherwise an integer from 0
  to 255. `steps` is a list of text, and `registered` is true or false. A record
  that fails, or whose `seen` is after the clock, makes the whole file
  `malformed`. A record whose provider state is null, or text outside the known
  six, is valid and has not ended.
- [x] 1.7 Classify each record as registered, futile or no evidence, order by
  `seen`, keep the window edge inside, and build the considered set with the tie
  rule from the spec.

## 2. Gate

- [x] 2.1 In `Controller._decide`, call the reader after the `awaiting_claim`
  branch and before `_templates`. Call it only there, so a decision that ends
  earlier reads nothing and carries no `launch_evidence`.
- [x] 2.2 When at least three workers are considered and all are futile, set
  `decision` to `refused`, add the single reason `recent_launches_futile`, keep
  `launch_count` at 0 and return before any template, price or budget work.
- [x] 2.3 Put `launch_evidence` with `state`, `considered` and `futile` on every
  decision that reaches the gate. The counts are 0 unless the state is `fresh`.
- [x] 2.4 Leave `status` unchanged. Leave `plan` read-only: it calls the same
  `_decide`, so it reports the same hold.
- [x] 2.5 Correct the `ci_burst/export.py` module docstring, which says the
  record never influences admission, so that it names the one read-only brake and
  the duty to rewrite the record on every successful run.

## 3. Tests

- [x] 3.1 Reader tests cover each of the five states and their order; the file at
  exactly 600 seconds (fresh) and over it (stale); a file dated in the future; a
  file that is not UTF-8 JSON; a wrong version; `instances` that is not an object;
  one invalid record that makes the whole file `malformed`; a `seen` in the
  future; and a provider state that is null or text outside the known six. The
  staleness cases set the file's modification time explicitly with `os.utime`,
  because the fake clock is not wall time.
- [x] 3.2 Classification tests cover futile; registered by the registration step;
  registered by a later marker with the registration line lost; no exit marker;
  an instance that has not ended, including a registered one that still runs; no
  operation identifier; a window edge at exactly 3600 seconds and past it; a tie
  at the third place; fewer than three workers; and a registered worker older than
  the newest three. A test shows that `first_seen` does not move a record in or
  out of the window.
- [x] 3.3 Add a contract test in `tests/test_export.py` that runs the real
  exporter over fake consoles (a worker that ends before registering, a worker
  that registered, a worker that registered and was killed with no exit marker,
  an empty console, and an instance whose provider state the exporter cannot
  label, run twice so the record carries first the text `unknown` and then null)
  and classifies the file it wrote. It fails when the record version, a field name
  or the write cadence drifts, and it keeps the file `fresh` and the unlabelled
  record unclassified. Add a test that the constant from 1.2 is a subset of
  `STEP_NAMES` and excludes the registration step.
- [x] 3.4 Exporter tests show that a successful run with nothing new still
  rewrites the record and advances its modification time, that a refused run
  leaves it unrewritten, and that a deleted record is rebuilt with the rebuilding
  run's time.
- [x] 3.5 Controller tests add one class beside `ClaimGraceTests` in
  `tests/test_controller.py`. They cover the hold with its single reason and its
  `launch_evidence`; no template read and no price read on a hold, checked
  against the fake provider's calls; no launch intent, reservation or instance;
  each fail-open state launching as before and reporting its state; a hold ending
  when the fake clock passes the window; earlier reasons keeping precedence with
  no `launch_evidence`; the later reasons unchanged when the gate is open; `plan`
  and `apply` agreeing on the same record; `plan` reporting the state it found
  when it cannot read the record; and the record's bytes and modification time
  unchanged after `plan` and `apply`.
- [x] 3.6 Update any existing assertion that compares a whole decision document
  for the new key, and weaken no existing assertion.

## 4. Documentation

- [x] 4.1 In `docs/operations.md`, add a section after "Claim grace" that states
  the rule, the three constants, the evidence states, the fail-open behaviour, the
  length of a hold, the steady state under a persistent fault, the residual cases
  from the design, the two levers and why deleting the record does not resume,
  that `status` does not report the gate, and that `export` creates the record
  owner-only, so a `plan` run as another user reads `unreadable`.
- [x] 4.2 In `docs/operations.md`, add `recent_launches_futile` to the list of
  zero-bound reasons in "Fleet size and fan-out", extend the "recovery path" line
  in "Claim grace" with the gate, correct the sentence in "Worker evidence" that
  says deleting the dedupe record never changes a decision, and note in "Launch
  decision and capacity fallback" that a hold comes before the template read.
- [x] 4.3 In `docs/deployment.md` and `docs/container.md`, replace the wording
  that the observation unit is "independent of admission": it is optional and
  launches nothing, but where it runs, `apply` reads its record as a brake.

## 5. Verification

- [x] 5.1 Run the focused tests:
  `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_export.py'`
  and the same with `'test_controller.py'`.
- [x] 5.2 Run the full suite:
  `PYTHONDONTWRITEBYTECODE=1 timeout 1500 npm run -s check:policy`.
- [x] 5.3 Run `npm run -s check:spec`.

## 6. Operator

- [ ] 6.1 Not a source task. Where the gate is wanted, run the new image and
  confirm that the export unit runs on its timer and writes the record where the
  apply unit reads it, so `launch_evidence` reports `fresh`, not `missing` or
  `unreadable`.
- [ ] 6.2 Not a source task. Observe for 24 hours that `launch_evidence` stays
  `fresh` and the gate stays open on a healthy pool, then record the result.
