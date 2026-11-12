# Tasks

These are product-source tasks. They authorize no deployment, no credential and
no cloud change.

## 1. Configuration

- [x] 1.1 Add the optional ordered `aws.instance_types` list to
  `ci_burst/config.py`, defaulting to an empty tuple that means "the launch
  template's own `InstanceType`".
- [x] 1.2 Refuse an empty, repeated, malformed or over-long list with the stable
  code `INSTANCE_TYPES_INVALID`, and bound the list at four types.

## 2. Admission

- [x] 2.1 Expand every configured candidate over the configured types in
  type-major order, pricing each pool before any time-sensitive check.
- [x] 2.2 Send the selected pool's `InstanceType` in the frozen `RunInstances`
  request and compare the quote's type against the pool's type.
- [x] 2.3 Keep batch rotation inside the first eligible type's zone block and
  require every starting pool to be in that block, so a later type is reached
  only by a definite capacity rejection, never by rotation or expiring
  evidence.
- [x] 2.4 Report the pool (zone and type) in the plan, launch and attempt
  records, and name the pool in the fallback detail.

## 3. Operator prerequisite check

- [x] 3.1 When a type list is configured, read the configured types' vCPU,
  memory and architecture facts and the zones' offerings once per run.
- [x] 3.2 Report `instance_type_not_offered`, `instance_type_shape_mismatch`,
  `image_architecture_unsupported` as drift and `instance_types_unavailable` as
  unavailable, keeping the `0/2/3` exit codes.

## 4. Acceptance coverage

- [x] 4.1 Configuration: the list is optional and ordered; an empty, repeated,
  malformed or five-entry list is refused with `INSTANCE_TYPES_INVALID`.
- [x] 4.2 Admission: pools expand type-major with one price each; a pool above
  the cap is skipped while a later type stays eligible; a capacity rejection
  advances across types within one operation and reservation; a batch stays on
  the first type and still alternates zones; a rotated worker's capacity
  rejection and expiring evidence both stay inside the preferred type; a fully
  stale preferred type stops the batch instead of starting a later type;
  without a configured list, candidates whose template versions pin different
  types keep the original whole-list rotation; the frozen request names the
  selected type.
- [x] 4.3 Prerequisites: a clean type list passes; a type missing from a zone,
  a different shape against any configured template, and an unsupported image
  architecture are drift; a type whose shape matches one template but not
  another is drift; an unreadable metadata read, incomplete vCPU or memory
  facts, and an absent or partial image description are unavailable with exit
  3, not drift.

## 5. Documentation

- [x] 5.1 `docs/operations.md`: the configuration key, the ordering rule, the
  worked pool example, the four-type bound and the refusal code.
- [x] 5.2 `docs/deployment.md`: the template type as the shape anchor and
  default, the unchanged IAM condition, and the prerequisite check's new checks
  and read permissions.
- [x] 5.3 `docs/runner-setup.md` and `docs/pilot-checklist.md`: one canary
  launch per configured type and the same architecture requirement.
