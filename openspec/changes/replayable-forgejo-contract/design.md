# Design

## Context

See [proposal.md](proposal.md). The observer classifies `ActionRunJob` records
from one repository endpoint. The parser already distinguishes runnable,
blocked, assigned, incompatible, duplicate, inconsistent, unknown-status and
shape-error cases, and the fixtures cover most of them, but the declared
contract and those fixtures were not tied together or to a pinned upstream
source. A declaration string cannot show which upstream version the parser was
written against, and a fixture without an expectation cannot fail when the
parser changes.

## Goals / Non-Goals

**Goals:** make the parser's classification replayable offline against a pinned
upstream reading; make the manifest declare what it is and is not; pin the
request variants, including the absent-`labels` hazard; keep the check
stdlib-only and credential-free.

**Non-Goals:** a live Forgejo integration test; an automatic canary; a new
dependency or a YAML/TOML parser; changing `ci_burst/forgejo.py` behaviour; any
AWS, deployment, credential or scheduling change.

## Decisions

### A JSON manifest beside the fixtures

`tests/fixtures/forgejo/contract.json` is JSON because the repository already
parses JSON for these fixtures and in `ci_burst/forgejo.py`, and because the
Python suite must stay standard-library only. YAML would add a parser the
product does not have; TOML is the configuration format, not a fixture format.
The manifest lives with the fixtures so one directory is the complete replay
input.

### The manifest states provenance, not just expectations

Each fixture is marked `upstream-shape` or `defensive-synthetic`. The pinned
handler selects only `waiting` and `running` jobs and always marshals `needs`
and `runs_on` on its response record; terminal statuses, a missing `needs` key,
duplicate handles and malformed records are therefore defensive parser
tolerance, not observed server output. Recording that distinction prevents the
matrix from claiming the endpoint emits every pinned shape.

### Replay the real observer

The matrix feeds each fixture to `ForgejoObserver` through the existing stub
transport and compares `demand`, attempt handles, the counters, notes and
refusal reason with the manifest. It does not re-implement the classifier and it
does not duplicate the expectations as inline assertions: one data file is the
contract, and the test is the replay. A completeness assertion fails when a
fixture exists without a manifest entry or an entry names a missing fixture.

### Assert the explicit labels parameter

An absent `labels` parameter makes the pinned handler skip filtering and admit
every waiting or running job in the repository. The adapter must therefore
always send the parameter, with a non-empty comma-separated value. One test
checks the request URL for several label configurations and one feeds the
absent-filter fixture to show that shape is unreachable.

### Live compatibility stays operator-only

The matrix reads source; it sends no request and holds no credential. Live
compatibility is a repository-scoped ephemeral registration, one-job completion
and idle-worker expiry on a real isolated deployment, and only an operator
canary can record it. The manifest says plainly that it is not a live
observation, and the documentation keeps `qualification = "live-verified"` an
operator decision.

## Risks / Trade-offs

- The reading can age. It is pinned to one tag and source path; a new Forgejo
  version needs a new contract identifier and a new reading, and the
  documentation says so.
- The fixtures are synthetic. They pin the parser against the reading, not
  against a real server's field set; the canary remains the only live proof.
- An upstream change to `GetActionRunJobs` without a version change would not be
  detected by an offline check. Versioned pins and the requalification rule are
  the mitigation.

## Alternatives considered

- **A live integration test against Forgejo.** Rejected for this change: it needs
  a server, a token and a repository, and the audit boundary is offline first.
  The isolated canary remains the operator procedure.
- **A YAML contract manifest.** Rejected: no standard-library YAML parser and no
  new dependency.
- **Inline expectations in the test only.** Rejected: the expectations would not
  be versioned data an operator can read beside the fixtures, and the
  declaration string would remain the only contract record.
