# Tasks

These are product-source tasks. They authorize no deployment, no image build and
no cleanup on any host.

## 1. Retention step

- [x] 1.1 Add `CI_BURST_RETAIN_BUILDS` (default 3) and refuse a value outside
  0-99 before anything is built.
- [x] 1.2 Compute the retained tag set from this host's deployed ref and every
  40-hex ref in the deployment-state document, and never interpret another
  value.
- [x] 1.3 Untag `sha-<12>` builds outside the set plus the newest N, one full
  `IMAGE:TAG` name at a time, and prune dangling images filtered by this
  deployment's own `org.ci-burst.build=refresh` label, which the build sets.
- [x] 1.4 Keep and report a tag whose image cannot be inspected, and make a
  refusal change neither the promotion nor the exit status.

## 2. Placement

- [x] 2.1 Run retention after the serving tag has moved and the deployed ref is
  recorded, and on the already-current path.
- [x] 2.2 Keep every other deletion site unchanged; the candidate tag removal
  that already existed is not part of retention.

## 3. Evidence

- [x] 3.1 Behaviour tests against a stubbed engine: the declared set only, a
  state-document ref survives, an uninspectable tag is kept, nothing to remove,
  and a refused untag is reported without aborting.
- [x] 3.2 Whole-script tests: a successful promote leaves exactly the declared
  set, and a failed promote removes nothing.
- [x] 3.3 `docs/container.md` states the retained set, the placement and the
  log lines.
- [x] 3.4 `npm run check:policy` and `npm run check:spec` pass.
- [ ] 3.5 Operator: clean the already-full host under HB-3340 part (1), then
  install the refreshed script on both state hosts. Not a source task.
