# Proposal

## Why

`controller.max_instances` is the only concurrency bound in the product, and its
supported range stops at four. A deployment whose worker advertises a shared
pool label can therefore have runnable jobs waiting while the controller admits
nothing more: the allowance caps spend per window, but the ceiling caps
throughput, and the ceiling is reached before the allowance is.

The bound is a product decision, not a deployment fact. Raising it in a
deployment today would be refused by configuration validation, so a deployment
that needs more concurrent workers has no supported configuration.

## What Changes

- Raise the supported `controller.max_instances` range from `1..4` to `1..12` in
  configuration validation, the two acceptance specs that restate it, and the
  operator documentation that names it.
- Add acceptance coverage for the new ceiling and for a value above it.
- Nothing else changes: admission stays `min(eligible demand, free slots,
  affordable reservations)`, and the reservation, ledger, placement, expiry and
  cleanup paths do not read the bound beyond the free-slot count.

## Impact

- A deployment that keeps `4` (or omits the key) behaves exactly as it does now.
- A deployment that raises the bound spends more in a window only when real
  runnable demand exists at that moment; the allowance still caps each batch and
  each window.
- `status.hourly_ceiling` already reports `max_spot_price × max_instances`, so an
  operator sees the higher ceiling's cost implication before enabling it.
- No credential, IAM, network, image or template change; no release artefact is
  required for a deployment to run the new bound once it builds from `main`.
