# Tasks

These are product-source tasks, not authorization for a real cloud pilot.
Complete the design and API-contract gates before implementation depends on
them. Keep tests and documentation with the behavior they describe.

## 1. Design and Interface Qualification

- [ ] 1.1 Obtain independent review of this proposal, design and failure specifications; resolve findings in a reviewed change before controller implementation.
- [ ] 1.2 Qualify the repository runner-jobs API and its required permission against an explicitly identified Forgejo contract; deliver synthetic fixtures proving ready, dependency-blocked, dependency-satisfied, assigned, empty and retried cases, or record live support as blocked.
- [ ] 1.3 Document the operator-owned immutable image/template contract, one-job shutdown, credential boundaries and label qualification; verify every prerequisite maps to a lifecycle acceptance scenario.

## 2. Observation and Planning

- [ ] 2.1 Add the minimal Python CLI/package and scoped dependencies with maintainer approval; verify a clean public checkout can install and run tests without private services or cloud credentials.
- [ ] 2.2 Implement TOML configuration validation and observation-by-default; test missing limits, mutable templates, invalid money values and unsupported concurrency, and document the exact CLI/configuration shape.
- [ ] 2.3 Implement the qualified Forgejo read adapter with bounded HTTPS handling; replay the contract fixtures, authentication failures, invalid JSON, incomplete reads and cross-origin redirect refusals without a live token.
- [ ] 2.4 Implement complete-label matching and attempt deduplication; test blocked versus ready work, repeated handles, retries and demand disappearing while a worker boots.
- [ ] 2.5 Produce a structured plan with refusal reasons and capability gaps; test that planning performs no cloud/Forgejo mutation and commits no launch intent or reservation, and publish a synthetic example.

## 3. Durable Admission

- [ ] 3.1 Add the local SQLite ledger and single-writer process lock; test transaction failure, concurrent writers, corrupt/missing state and restart without sending a cloud request, and document the supported topology.
- [ ] 3.2 Add exact reservation accounting, quote freshness/scope validation, configured price/lifetime/window limits and active-resource carryover; test unknown or excessive prices, exhausted allowance, window rollover and restart without budget reset.
- [ ] 3.3 Persist frozen launch intent and stable client tokens before provider calls; fault-test process exit before and after transaction commit and verify the next run resumes the same operation.
- [ ] 3.4 Document reservation assumptions and residual non-compute/cleanup costs; verify plans do not claim an absolute billing cap or refund consumed allowance after a termination request.

## 4. Spot Lifecycle

- [ ] 4.1 Implement the one-time Spot launch using the exact approved template/version, one-instance count, configured maximum price and atomic ownership/expiry tags; verify the mocked SDK request and refusal paths without AWS access.
- [ ] 4.2 Reconcile provider state and count starting, running, terminating and uncertain operations against the single slot; test that untracked owned resources and unavailable reads block replacement launches.
- [ ] 4.3 Recover lost/partial creation responses with the frozen client token and parameters; test that retry, restart and changed-placement attempts cannot create a separately admitted duplicate.
- [ ] 4.4 Implement expiry and termination reconciliation; test that accepted deletion is not terminal evidence, denial retains ownership/reservation, and foreign or malformed resources are untouched.
- [ ] 4.5 Preserve job/result authority on idle startup, failed bootstrap and abrupt Spot interruption; verify no On-Demand fallback, automatic job retry or manufactured successful result, and document worker-versus-instance status.

## 5. Independent Cleanup and Operational Safety

- [ ] 5.1 Add planning/apply forms of cleanup-only execution using exact ownership and expiry; test operation without Forgejo or the scaling database and verify that it never launches resources.
- [ ] 5.2 Add secret-safe reason-coded logs and status; test redaction of synthetic tokens, authorization headers, user data and provider errors without dumping raw configuration or environment.
- [ ] 5.3 Document separate least-privilege scaling/cleanup identities, scheduler failure boundaries and stop-admission-first rollback; verify the documentation requires independent cleanup qualification before unattended use.
- [ ] 5.4 Document the operator's pilot checklist for image/labels, API contract, quota/capacity, network, temporary machine identity, cost/lifetime limits and owned-resource readback; verify it does not authorize those actions or use private deployment values.

## 6. Source Acceptance

- [ ] 6.1 Run an end-to-end synthetic queue-to-terminal lifecycle and fault matrix covering launch timeout, restart, interruption, state failure and denied cleanup; verify every capability scenario has a test or an explicit external qualification gate.
- [ ] 6.2 Verify a clean public checkout runs the documented checks and contains no private service dependency; keep CI credential-free, read-only and non-billable.
- [ ] 6.3 Obtain green required CI and independent review for the final exact head, resolve findings and verify the authorized merge before declaring source completion; do not claim release, deployment or runtime verification.

## Workflow follow-up

- Sync/archive the change only after product-source acceptance and review; keep unresolved runtime qualifications explicit rather than checking them off as source tests.
- Release/tag/image publication requires a separate maintainer decision.
- A real pilot requires a separate approved deployment plan and machine identity. Before launch, verify image/label capability, Region quota/capacity, allowed cost/lifetime/concurrency, connectivity and independent orphan expiry.
- During an approved pilot, test scaler failure, abrupt instance loss and denied cleanup; confirm owned resources terminate and compare total cost per completed comparable job. These are runtime acceptance gates, not public CI steps.
