# Tasks

These are product-source tasks. They authorize no deployment, no credential and
no cloud change.

## 1. Bounded provider client and reads

- [x] 1.1 Add `CONNECT_TIMEOUT_SECONDS = 10` and `READ_TIMEOUT_SECONDS = 30`
  and build both the EC2 and STS clients with `connect_timeout` and
  `read_timeout` beside the existing `RETRY_POLICY`; retry counts are unchanged.
- [x] 1.2 Add `DeadlineRefusal` with the stable reason `pass_deadline_exceeded`.
- [x] 1.3 Add a keyword-only `deadline=None` to `owned_instances`, `instances`,
  `spot_quote` and `console_output`; check it before each paginated page and
  each per-identifier read, and leave a `None` deadline unchanged.

## 2. Bounded apply pass

- [x] 2.1 Add `controller.pass_deadline_seconds` (integer 30..840, default 240)
  to the controller table and its key allowlist.
- [x] 2.2 Compute one deadline in `apply` and thread it through `_reconcile`,
  `_decide` and `_expire` and their provider reads.
- [x] 2.3 On a reached deadline return `decision="refused"`,
  `reasons=["pass_deadline_exceeded"]` and `phases_deferred`; do not start a
  later phase or send an expired intent or stale quote.
- [x] 2.4 Never pass the deadline into `_send`, `_launch`, `run_spot` or
  `terminate`; a mutation already started keeps its frozen intent and token, and
  the existing uncertain reconciliation path is unchanged.
- [x] 2.5 Leave `plan`, `reap`, `status` and `export` without the pass deadline.

## 3. Tests

- [x] 3.1 `tests/test_aws.py`: client config carries both timeouts; a paginated
  inventory refuses before the next page; a read without a deadline consumes
  every page; console reads refuse before the next identifier.
- [x] 3.2 `tests/support.py`: `FakeAws` accepts the deadline keyword and exposes
  optional read hooks with the existing defaults.
- [x] 3.3 `tests/test_controller.py`: a slow quote read and a deadline between
  candidate quotes produce the bounded incomplete pass with no mutation; a
  deadline before expiry terminates nothing; a deadline after a possibly
  accepted launch keeps the frozen operation for resume with the same token; a
  fast pass is unchanged.
- [x] 3.4 `tests/test_config.py`: the default, both range boundaries and the
  refusals outside the range and for a non-integer value.

## 4. Documentation and OpenSpec

- [x] 4.1 `docs/operations.md`: the pass bound in § Bounds and the deadline versus
  `TimeoutStartSec` versus worker lifetime relation in § Scheduler, cleanup and
  rollback.
- [x] 4.2 `docs/deployment.md`: the new TOML key and its default.
- [x] 4.3 `docs/container.md`: the systemd relation and the independent reaper.
- [x] 4.4 This change's `bounded-operation` delta spec.
