# Tasks

These are product-source tasks. They authorize no deployment, no credential and
no cloud change.

## 1. Worker bootstrap

- [x] 1.1 Capture the one-job process status in `JOB_RC` instead of discarding
  it with `|| true`, and keep `STEP job` and the registration-file removal.
- [x] 1.2 Emit exactly one bounded marker — `bootstrap one-job completed rc=0`,
  `bootstrap one-job failed rc=<n>` or `bootstrap one-job timeout rc=<n>` — and
  exit with the captured status.
- [x] 1.3 Leave the EXIT trap, token shredding, sync and shutdown unconditional
  on every path.

## 2. Console evidence

- [x] 2.1 Parse the one-job marker before the exit marker into a `job_process`
  fact on `ConsoleFacts`.
- [x] 2.2 Add the `job-process` event kind, its `<instance>:job-process:<outcome>:<rc>`
  identity and its closed vocabulary to the event schema and persisted record.
- [x] 2.3 Keep historical `exit` event lines valid and publish no console text,
  job verdict or success metric.

## 3. Publication state

- [x] 3.1 Express the pending and published launch-template versions and
  digests as explicit constants, and require the file to equal the pending
  digest while a publication is owed.
- [x] 3.2 Keep static assertions for the captured status, the three marker
  literals, the trap/cleanup/shutdown and the absence of `|| true` after the
  one-job invocation.
- [x] 3.3 Run the extracted tail in a synthetic shell with a stub returning
  0/42/124 and prove the marker, the process exit status and the cleanup.

## 4. Documentation and acceptance

- [x] 4.1 State in `docs/runner-setup.md` and `docs/operations.md` that the
  bootstrap markers describe the worker process and never the job result.
- [x] 4.2 Record the operator publication step for the next launch-template
  version in `docs/deployment.md`.
- [x] 4.3 Cover the three markers, an unshipped outcome, the event shape and a
  pre-change history line in `tests/test_export.py`.
