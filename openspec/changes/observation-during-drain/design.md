# Design

## Context

`Exporter.run` reads at most `MAX_CONSOLE_READS` (16) consoles per run over the
records that can still carry new evidence, live workers first. It also refused
whenever the number of non-terminal owned instances exceeded
`controller.max_instances`, treating that as an ownership-tag disagreement.

That second check conflates two different questions. The admission ceiling is a
product bound on how many workers the controller may admit. Observation is a
read-only evidence path over whatever the deployment already owns. The supported
drain — lowering the ceiling below the live count — pauses admission and evicts
nothing, so the owned inventory is legitimately above the ceiling while workers
finish on their own lifetime.

## Goals / Non-Goals

**Goals:** keep evidence flowing during a supported drain; report the over-limit
condition with the controller's existing note vocabulary; keep the read budget,
the 64 KiB clip and the refusal semantics for a fleet that cannot be observed in
one run; keep the diff to one behaviour change.

**Non-Goals:** changing admission, eviction, expiry cleanup, candidate selection,
the console parser, the event schema, the metrics families, or the provider
interface.

## Decisions

### Observation is not admission

Observation MUST NOT refuse a legitimate drain. The admission ceiling and the
read budget are independent bounds: the ceiling decides what may be admitted,
the budget decides how much evidence one run may read. A refused admission must
not suppress cleanup evidence, so the exporter computes the non-terminal owned
count and, when it exceeds `controller.max_instances`, appends the
`above_configured_limit` note — the same word the controller's decision already
carries for a lowered limit. It still reads candidates and writes both
artefacts.

### Only the read budget refuses

Refusal is reserved for the one condition observation cannot satisfy: more
unfinished non-terminal candidates than the whole read budget. Live workers are
read before termination history, so 17 or more unfinished live workers means the
run cannot cover the live evidence at all. The reason code stays
`console_read_cap_exceeded` and the detail states the per-run read-budget
condition, so an operator sees a budget refusal rather than a limit refusal. The
check runs before any console read and before either file is written, so a
refused run stays mutation-free and writes nothing.

### Deferral is partial observation

`console_read_deferred` still marks candidates the run did not read. That note
must not be lost or presented as complete observation: the deferred records are
read on a later run while the provider still knows them, and the documentation
says the result is partial evidence.

### The deferral rule is scoped to the read budget

This change modifies two requirements, not one. `Observation refuses only when
owned instances exceed the limit` carries the drain report and the read-budget
refusal; `Turnover defers surplus candidates rather than refusing` keeps the
deferral of surplus candidates and gains the budget qualifier. The second
requirement is introduced by the prerequisite promotion PR #107, which splits
`Provider-only bounded observation`; until #107 merges it is absent from
`openspec/specs/worker-observation/spec.md`, so this delta's MODIFIED block for
it applies only after that merge. Strict validation reports the absent archive
target as an INFO, not as a failure, and the requirement must not be renamed or
dropped to avoid that notice.

Read alone, the promoted sentence — when the candidates exceed the per-run
budget the command MUST defer the rest and MUST NOT refuse — contradicts the new
refusal for 17 unfinished live candidates. The rule is correct for surplus
terminal history and wrong for live evidence the budget cannot cover, so it is
scoped rather than deleted: deletion would remove a behaviour that still holds
and drop its only scenario, while scoping keeps the deferral and names the read
budget as the boundary between deferring and refusing. `Provider-only bounded
observation` is not touched: after the split it no longer carries the deferral
rule.

## Failure behavior

- An above-limit drain: `decision: reported`, `above_configured_limit` note,
  events and metrics written for the records read.
- More than 16 unfinished non-terminal candidates: `ProviderRefusal` with
  `console_read_cap_exceeded`, exit code 2, no event and no metrics file.
- Surplus terminal history within the read budget: `console_read_deferred` note,
  reported.
- A provider error, an unreadable ledger or a corrupt record keeps its existing
  behavior; this change touches none of those paths.

## Credential boundaries

None. The change adds no provider call, no credential, no network path and no
mutation. The exporter still reads consoles through the existing adapter and
holds no queue credential.

## Alternatives considered

- **Keep the ceiling refusal and raise it with the ceiling.** Rejected: the
  refusal is what blanks observation during a drain, and a supported drain is not
  a disagreement to detect.
- **Refuse only after several consecutive over-limit runs.** Rejected: adds
  state and a new note for a condition that is legitimate by definition.
- **Drop the refusal entirely.** Rejected: more than 16 unfinished live workers
  cannot be covered in one run, and silently deferring live evidence would
  present partial observation as complete.
- **Count terminal records against the read budget.** Rejected: a terminated
  instance keeps its tags for about an hour, so normal turnover would consume the
  budget; finished records are already excluded.

## Evidence gaps

The change is verified by the standard-library test suite at this base. No
runtime deployment is exercised: the tests use the in-repository fakes, and
provider behavior beyond the documented console and instance-state contract is
not re-qualified here.
