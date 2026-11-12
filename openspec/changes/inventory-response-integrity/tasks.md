# Tasks

These are product-source tasks. They authorize no deployment, no credential and
no cloud change.

## 1. Inventory parser

- [x] 1.1 Refuse a response that is not a mapping, whose `Reservations` is absent
  or not a list, whose reservation or instance entry is not a mapping, whose
  `Instances` is absent or not a list, or whose entry has a missing, empty or
  non-string `InstanceId`, as an uncertain `AWS_INVENTORY_INCOMPLETE`.
- [x] 1.2 Keep `Reservations: []` a valid complete empty inventory.
- [x] 1.3 Leave pagination, filters and the by-identifier absence path unchanged.

## 2. Tests

- [x] 2.1 Adapter tests for every refused shape and both entry points; an empty
  list still parses and a multi-reservation response is unchanged.
- [x] 2.2 End-to-end tests: a malformed read cannot settle a running operation
  or launch a replacement; an entry without an id cannot be adopted or resumed.
- [x] 2.3 End-to-end test: an empty `Reservations` list still settles the
  aged-out operation and admits a replacement.
- [x] 2.4 Red/green evidence: the defect tests fail against the base parser and
  pass with the fix.

## 3. Documentation

- [x] 3.1 `docs/deployment.md`: add `AWS_INVENTORY_INCOMPLETE` to the
  authoritative refusal list and state that a malformed inventory is a refusal,
  not an empty fleet.
