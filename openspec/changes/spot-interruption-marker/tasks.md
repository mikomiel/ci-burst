# Tasks

These are product-source tasks. They authorize no deployment, no credential, no
template publication and no cloud change.

## 1. Worker marker

- [x] 1.1 In `deploy/runner/bootstrap.sh`, poll the IMDSv2 interruption notice as
  root every five seconds, after the job account is denied the metadata service
  and before the one job starts.
- [x] 1.2 On a notice, print exactly one marker:
  `bootstrap spot-interruption action=<action> host=<hostname> runner=<uuid|unknown>`,
  with `action` restricted to `terminate`, `stop`, `hibernate` or `unknown`, and
  the registration id read as one `jq` field that is shape-checked before use.
- [x] 1.3 Send `SIGTERM` to the `runner`-owned `forgejo-runner` process only, and
  keep a failed read or a failed signal non-fatal.
- [x] 1.4 Kill the watcher in the EXIT trap so it never outlives the bootstrap.

## 2. Reader

- [x] 2.1 Recognise the marker in `parse_console` only when every value is in the
  closed vocabulary, and keep it out of the step vocabulary.
- [x] 2.2 Publish one `spot-interruption` event per instance with `action` and
  `host`, a stable identity and the instance's deployment, zone and operation.
- [x] 2.3 Validate a persisted record, a retained event line and a forged line
  with the same vocabulary, and drop what does not match.

## 3. Evidence and limits

- [x] 3.1 Policy tests cover the closed action set, the hostname pattern, the
  root-only watcher, the job-account-only signal, the non-fatal read and the
  event shape.
- [x] 3.2 `docs/operations.md` states the marker's fields, its join to the job's
  attribution line and the fact that it is not a job verdict.
- [x] 3.3 `docs/runner-setup.md` states what the watcher does, what a failed read
  leaves in place, and that the marker is worker evidence.
- [x] 3.4 `npm run check:policy` and `npm run check:spec` pass on the change.
- [ ] 3.5 Operator: publish launch-template version 5 (which also carries the
  one-job process-status change), then record the runtime evidence of one real
  Spot interruption in the event stream. Not a source task.
