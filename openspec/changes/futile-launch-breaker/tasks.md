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
  record path, the controller clock reading and the ledger operations of this
  deployment (see 1.9) and returns the evidence state, the considered count and
  the futile count. It imports the record version, the final provider states, the
  operation pattern and the constant from 1.2 from `ci_burst/export.py`.
- [x] 1.4 Open the file once, without blocking and without following a link,
  take the type, the modification time and the size from the open descriptor, and
  decide before it reads any content. Read the file only when it is a regular
  file, is fresh and is at most 16 MiB, and then read the descriptor itself, in
  chunks that the bound still allows and through no buffered stream, until it
  holds 16 MiB plus one byte. A file above 16 MiB, or one that grows past it
  after its size was taken, is `malformed`, and the reader fetches no more than
  16 MiB plus one byte from it. A FIFO with no writer would block a plain open
  while `apply` holds the ledger writer lock. Test the states in the order
  `missing`, `unreadable`, `stale`, `malformed`. Decode the JSON with hooks that
  refuse `NaN`, `Infinity` and `-Infinity`, and a JSON object that names a member
  twice at any level, so a file that holds one is `malformed`. The hooks belong to
  `ci_burst/export.py` and the reader imports them (see 1.8). `export` cannot
  write a repeated member, because it serialises dictionaries with text keys. The
  function never raises, never writes, repairs, locks or deletes, and maps a path
  that is not a regular file, a link and any failure it did not foresee to
  `unreadable`.
- [x] 1.5 Define the fixed values as named constants: 3 workers, a window of 3600
  seconds and a staleness bound of 600 seconds. Offer no configuration key and no
  off switch for them.
- [x] 1.6 Validate the file shape and the fields the gate reads in every record.
  `seen` is a finite number. `state`, `operation`, `exit_code`, `job_process` and
  `spot_interruption` may be null, and `exit_code` is otherwise an integer from 0
  to 255. `steps` is a list of text, and `registered` is true or false. Each key
  of `instances` passes the pattern that `export` applies to an instance
  identifier. `job_process` and `spot_interruption` are null or exactly the value
  that `export` stores: the reader and `export` share one rule for each, so the
  reader accepts no empty list and no other shape that `export` cannot write. A
  record that fails, or whose `seen` is after the clock, makes the whole file
  `malformed`; `export` drops such a record when it loads its own (see 1.12). A
  record whose provider state is null, or text outside the known six, is valid
  and has not ended.
- [x] 1.7 Classify each record as registered, futile or no evidence, order by
  `seen`, keep the window edge inside, and build the considered set with the tie
  rule from the spec. Skip a record that the ledger does not hold for this
  deployment (see 1.9) before the newest three are chosen.
- [x] 1.8 Decode the record in `load_state` in `ci_burst/export.py` with the same
  two hooks. A file that holds a refused constant or a repeated member takes the
  branch of a file that does not decode: the loader drops it whole, notes
  `state_rebuilt` and starts from an empty record, so the emitted history and the
  cumulative counters start again, as when the record is deleted. Add no other
  behaviour here. A loader that kept the last value of a repeat would keep a
  record that the reader calls `malformed`, skip its finished console and rewrite
  the file with a fresh heartbeat. The same reasoning applies to a repaired
  registration value and to a default for an absent registration key (see
  1.10).
- [x] 1.9 Count a worker only when the ledger holds its operation for this
  deployment. The reader takes the operations as a required argument: a mapping
  from each operation identifier that the ledger holds for this deployment to the
  instance identifier that it holds for it, or to null while it holds none. A
  record counts when its operation is in the mapping and the mapped instance is
  null or the instance that the record is filed under. The reader makes no ledger
  call of its own. `export` persists any well-shaped operation tag, and
  reconciliation ignores a terminated instance that no ledger operation names, so
  the tag alone does not show a launch that the ledger holds.
- [x] 1.10 In `load_state`, drop the record of an instance when a stored value
  that bears on registration is present and cannot be kept: a `job_process` or
  `spot_interruption` that `export` does not write, a `steps` list with an element
  that is not text, or a `registered` flag that is not true or false. Also drop
  the record of a worker that has ended when it lacks `registered`, `steps`,
  `job_process` or `spot_interruption`, as an older version wrote it. Use the
  value rules for `job_process` and `spot_interruption` that the reader imports.
  Keep the record of a worker that has not ended and lacks a key: the reader
  counts no such worker, and the run reads its console again while the provider
  lists the instance. Keep a Spot notice that the loader normalises and a
  `steps` list from which it removes an unknown name. Keep the `state`,
  `operation` and `exit_code` repair, which replaces a bad value by null. A worker
  that has ended is not read again, so a record kept with a default
  would be written back under a fresh heartbeat as a futile launch that nobody
  observed. The dropped record is read again while the provider lists the
  instance, within `MAX_CONSOLE_READS`, and leaves no evidence when the provider
  does not.
- [x] 1.11 In the controller, build the mapping of 1.9 from the operation rows of
  the ledger, and leave out a row whose frozen request names another deployment.
  Use the rule of the scope check (`_deployment_changed`): the deployment is the
  `ci-burst:deployment` tag of the frozen request, and a row with no readable tag
  is unbound and stays in. Add no ledger column and no record field. A rename
  leaves the old rows behind as history (the drain-and-retain rule of the
  lifecycle capability), and their workers are no evidence for the new name.
- [x] 1.12 In `load_state`, take the clock of the run as the keyword argument
  `now`, and drop a record whose `seen` is after it, whether its worker has ended
  or not, with the note `state_rebuilt`. A record dated at the clock stays, as the
  reader keeps it. `Exporter.run` passes its own clock, and a call without `now`
  judges no record against a clock. Do not clamp the date and do not keep the
  record: the reader refuses a whole file for such a record, and a worker that has
  ended is not read again, so the file would stay `malformed` until the clock
  caught up. The record of a listed instance is read again, within
  `MAX_CONSOLE_READS`, and dated by that read. An instance that the provider no
  longer lists leaves no evidence.

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
  the duty to write the record last, on every successful run.
- [x] 2.6 Give `_decide` the ledger operations of this deployment and pass them
  to the reader (see 1.11). `plan` reads them beside its state block, before its
  queue call. `apply` reads them at the end of `_reconcile`, in the same
  transaction. The gate adds no provider call and no schema change, and one more
  read of the operation rows on the ledger connection that the pass has open.

## 3. Tests

- [x] 3.1 Reader tests cover each of the five states and their order; the file at
  exactly 600 seconds (fresh) and over it (stale); a file dated in the future; a
  file that is not UTF-8 JSON; a wrong version; `instances` that is not an object;
  one invalid record that makes the whole file `malformed`; a `seen` in the
  future; and a provider state that is null or text outside the known six. A path
  that is a FIFO with no writer, a directory or a link is `unreadable`, and the
  FIFO read runs under a time bound so that a regression fails the test and does
  not hang the run. A key that `export` rejects and each marker value that
  `export` cannot write make the file `malformed`, including values that are not
  hashable. The staleness cases set the file's modification time explicitly with
  `os.utime`, because the fake clock is not wall time. A file above 16 MiB is
  `malformed` when it is fresh and `stale` when it is not, and neither is read.
  A file at exactly 16 MiB is read in full. A file that grows past the bound after
  its size was taken is `malformed`, and the reader fetches exactly 16 MiB plus
  one byte from it, for a file that was at the bound and for a small one alike.
  The tests count the bytes fetched at the descriptor, from the offset of a
  duplicate of the reader's descriptor, so the count holds whatever API reads
  the file and a buffered stream that fetches a whole block more fails it. A
  `NaN`, `Infinity` or `-Infinity` constant in a field that the gate does not read
  makes the file `malformed`. So does a JSON object that names a member twice, at
  the top level, in `instances`, in a record and in a nested object, whatever the
  values: an invalid `steps` followed by a valid one in a record of three
  otherwise futile records, the same instance key twice (registered, then
  futile), and the same records with no repeat, which hold. Each such file is
  checked to hold the repeat that its test names.
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
  record unclassified. Further contract tests run the exporter over every marker
  shape it writes (a one-job outcome, a Spot notice with a registration id and
  one without) and require a `fresh` file, and ask the exporter's loader and the
  reader about the same keys and marker values, so that the reader is no stricter
  and no looser than the writer, and show that a value which the loader drops
  from `state`, `operation` or `steps` never makes the reader hold where the
  record that `export` rewrites would not. Further tests show that the loader
  drops a record file whole (`state_rebuilt`) when a JSON object in it names a
  member twice, at the top level, in `instances` and in a record, or when it holds
  `NaN`, `Infinity` or `-Infinity`; that the same records without a repeat are
  kept; and that a run over three finished workers, with one instance key repeated
  (registered first, futile last) and two other futile records, writes a record
  that the reader does not hold on. Add a test that the constant from 1.2 is a
  subset of `STEP_NAMES` and excludes the registration step. Further tests show
  that the loader drops a record, and only that record, for each registration
  value that it cannot keep and for each registration key that the record of an
  ended worker lacks (see 1.10), and notes `state_rebuilt`; that the reader calls
  the same file `malformed`; that the loader keeps a record whose Spot notice it
  normalises, whose `steps` list holds an unknown name, whose `state`,
  `operation` or `exit_code` it replaces by null, or whose worker has not ended
  and lacks a key, and that the run fills that key from the console while the
  provider lists the instance; that no
  field that the reader requires can be missing from a record that the loader
  keeps as futile; and that a run reads at most `MAX_CONSOLE_READS` dropped
  records and notes `console_read_deferred` for the rest.
- [x] 3.4 Exporter tests show that a successful run with nothing new still
  rewrites the record and advances its modification time, that a refused run
  leaves it unrewritten, that a run that cannot write its textfile leaves it
  unrewritten and that the next run emits again, under the same identities, the
  markers it still finds, and that a deleted record is rebuilt with the rebuilding
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
  unchanged after `plan` and `apply`. The class's fixture writes the terminal
  ledger row of each operation that a record names, as reconciliation leaves it
  for an ended instance, because a worker counts only when the ledger holds its
  operation.
- [x] 3.6 Update any existing assertion that compares a whole decision document
  for the new key, and weaken no existing assertion. The two assertions that the
  ledger holds no operation after a hold now assert that it holds exactly the
  rows that the fixture wrote, so a hold still creates no operation.
- [x] 3.7 Ledger tests. Reader tests show that the ledger argument is required;
  that workers whose operations the ledger holds hold, and that an empty ledger
  leaves no evidence; that a worker whose operation the ledger does not hold is
  no evidence; that the instance that the ledger names must be the one in the
  record, and that a ledger which names none accepts the operation alone; that an
  unrecorded worker takes no place among the newest three, in both directions; and
  that the evidence state does not depend on the ledger. Controller tests show
  that three futile workers whose operations the ledger does not hold do not hold
  admission, that the same workers hold once the ledger names their operations,
  that `plan` and `apply` agree, and that the ledger must name the instance that
  the record is filed under.
- [x] 3.8 Exporter-to-controller tests. Write the record of three ended workers
  (one registered, two futile) with a `job_process` that `export` cannot write on
  the registered one. Show that the reader calls the file `malformed`, that an
  export run reads the console of that worker again and records it as registered,
  and that the next `apply` is not held. Show that a worker whose instance the
  provider no longer lists leaves no evidence, that a dropped record whose console
  shows no registration counts, and, as the control, that three futile workers
  that nothing damaged still hold after an export run.
- [x] 3.9 Rename tests in `tests/test_controller.py`. Keep the old rows and the
  old record through a change of the deployment name. `plan` and `apply` do not
  hold under the new name, `apply` launches, and the old rows stay in the ledger.
  As the control, the same ledger and record still hold under the old name. A
  further test fixes the rule on the row: the gate counts rows whose frozen
  request names this deployment, leaves out a row that names another, counts every
  row that names none, and counts none when every row names another deployment.
- [x] 3.10 Clock tests. In `tests/test_export.py`, `load_state` drops a record
  dated after the clock, whether its worker has ended or not, keeps a record dated
  at the clock, and judges no record without a clock. After one run, a finished
  record dated ahead no longer makes the file `malformed`, the three futile
  workers beside it hold, and the run gave the dropped record no new time. A
  dropped record of a listed instance is read again and dated by that read. In
  `tests/test_controller.py`, the same two cases run from the exporter to the
  next `apply`.

## 4. Documentation

- [x] 4.1 In `docs/operations.md`, add a section after "Claim grace" that states
  the rule, the three constants, the evidence states, the fail-open behaviour, the
  length of a hold, the steady state under a persistent fault, the residual cases
  from the design, the two levers and why deleting the record does not resume,
  that `status` does not report the gate, that `export` creates the record
  owner-only, so a `plan` run as another user reads `unreadable`, that `export`
  writes the record last so a failed run lets it age, and that a worker with
  registration evidence and no exit marker is counted as registered.
- [x] 4.2 In `docs/operations.md`, add `recent_launches_futile` to the list of
  zero-bound reasons in "Fleet size and fan-out", extend the "recovery path" line
  in "Claim grace" with the gate, correct the sentence in "Worker evidence" that
  says deleting the dedupe record never changes a decision, add that `export`
  saves the dedupe record after the textfile, that the repeats of a run that
  failed count against the event file bound, that a run which fails at the record
  exits 1 before the unit publishes the textfile, so the collector's last-run
  time does not advance, that the counters keep their guarantee in the textfiles
  that the shipped unit publishes and not in direct mode, where a later run can
  lower a counter and a consumer treats a fall as a reset, and that a unit which
  fails at publication keeps the record fresh, and note in "Launch decision and
  capacity fallback" that a hold comes before the template read.
- [x] 4.3 In `docs/deployment.md` and `docs/container.md`, replace the wording
  that the observation unit is "independent of admission": it is optional and
  launches nothing, but where it runs, `apply` reads its record as a brake.
- [x] 4.4 In `docs/operations.md`, state in "Worker evidence" that `export` drops
  the record of an instance when a stored registration value cannot be kept, why,
  what the run does next, what it costs and which values it keeps. State in the
  hold section that a worker counts only when the ledger holds its operation for
  this deployment, how the pass reads the rows (see 2.6), and that a row that
  names another deployment is left out, and add the two residual cases that follow
  from these rules.
- [x] 4.5 In `docs/operations.md`, state in "Worker evidence" that `export` drops
  a record dated after the clock of its run, why (the reader refuses the whole
  file for it, and a worker that has ended is not read again), what the run does
  next and what it costs. State in the hold section that a rename does not carry
  the hold over and that the rule compares the deployment of the row and not its
  region or account. Correct the residual cases: a rename no longer mixes the
  records, and a dropped record includes one dropped for its date.

## 5. Verification

- [x] 5.1 Run the focused tests:
  `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_launch_evidence.py'`
  and the same with `'test_export.py'` and `'test_controller.py'`.
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
