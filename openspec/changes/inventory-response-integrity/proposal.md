# Proposal

## Why

`aws._instances` parses every `DescribeInstances` response, and both inventory
entry points (`owned_instances` and `instances`) treat its result as the
complete owned fleet. Two malformed shapes defeat that assumption:

- A response without a `Reservations` list -- including `{}` -- parses to an
  empty inventory. After the retained-visibility window the controller settles a
  confirmed running operation as `terminal` and admits a replacement: a failed
  read becomes proof that nothing is owned.
- An entry without `InstanceId` becomes `Instance(instance_id="")`. The
  controller reads the empty identity as an unresolved intent and re-sends the
  frozen request against it.

## What Changes

- Validate the shared parser. `Reservations` and every `Instances` list must be
  lists, every reservation and instance entry must be a mapping, and every entry
  must carry a non-empty string `InstanceId`.
- Refuse any other shape with the existing `AWS_INVENTORY_INCOMPLETE` reason and
  an uncertain refusal, before reconciliation, settlement or launch.
- Keep `Reservations: []` a valid, complete empty inventory.

## Impact

- Both callers are covered by one parser change; the refusal is visible with a
  stable reason instead of a silent empty fleet.
- If a provider ever omitted `Reservations` for a genuinely empty result, the
  controller refuses rather than launches. That is the safer failure: admission
  waits and the refusal is reported.
- No change to pagination, filters, the controller's retention window, the
  by-identifier `InvalidInstanceID.NotFound` path, the ledger or the CLI.
