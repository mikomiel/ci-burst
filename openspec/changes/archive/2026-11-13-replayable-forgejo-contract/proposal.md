# Proposal

## Why

E02 (#92): the source declares one Forgejo contract
(`forgejo-17.0.1-runners-jobs`) and carries a handful of response fixtures, but
the queue interface is still qualified by a manual deployment step. A
declaration string is not versioned evidence, and generic API documentation does
not establish what the runner-jobs handler returns. The shapes the parser must
classify are therefore partly unpinned, so an operator cannot tell a contract
regression from a server change.

## What Changes

- Add a versioned contract reading, `tests/fixtures/forgejo/contract.json`, of
  Forgejo `v17.0.1` `GetActionRunJobs` in
  `routers/api/v1/shared/runners.go`: the request variants, the response record,
  one expected classification or refusal per synthetic fixture, and whether the
  pinned handler can emit that shape.
- Add fixtures for an absent `labels` parameter, an explicit non-matching
  filter, absent and null `needs`, an empty `runs_on` list, terminal statuses, a
  mixed waiting/running list, a retry with a new handle and a partial record.
- Add a matrix test that replays every fixture through the real observer and
  compares the result with the manifest, and asserts the adapter always sends
  an explicit, non-empty `labels` query parameter.
- Document what the matrix proves, what only an isolated live canary can prove,
  the versions that canary must record and the requalification rule.
- Correct the absent-`labels` claim: the pinned handler applies no filter then
  and admits every waiting or running job in the repository, so the response
  can be a list rather than `null`.

## Impact

- The standard-library suite becomes a versioned contract check: a mismatch
  between the pinned reading and the parser fails offline, with no server,
  token, credential or billable request.
- No behavioural change to `ci_burst/`, no configuration change, no new
  dependency, and no change to scheduling, assignment or result authority.
- `qualification = "fixtures-only"` remains the default. Live compatibility
  remains an operator canary on an isolated deployment, and this change claims
  no live qualification.
