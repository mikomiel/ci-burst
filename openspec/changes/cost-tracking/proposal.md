# Proposal

## Why

The budget window reports `consumed` as the sum of per-launch reservations. A
reservation prices one hour at the configured maximum Spot price plus the
non-compute buffer, and nothing releases it when the worker exits minutes later,
so the number counts launches rather than spend. On 2026-11-12 a deployment
reported `24.65` consumed for exactly 145 launches at `0.17` each, while the
billing account showed `3.77` for the month. The configured allowance therefore
stops admission at a launch count that has no fixed relationship to the money
the operator meant to cap.

## What Changes

- Track cost per operation from the Spot price of the accepted request and the
  provider's launch time, with the configured incidental hourly rate and the
  provider's 60-second billing minimum.
- Settle each operation once when it terminates; settle a full lifetime when the
  provider record aged out unobserved; settle zero when no instance was created.
- Freeze the cost inputs an operation was admitted under — the incidental hourly
  rate, the non-compute buffer and the request's `MaxPrice` ceiling — so a later
  configuration edit reprices new admissions only, and count the provider's
  60-second billing minimum once across accrued and remaining cost.
- Replace reservation-sum admission with a forward commitment: tracked spend plus
  the worst-case remaining life of every non-terminal operation plus the buffer.
  An admitted worker is still priced at the configured maximum for its lifetime,
  so the allowance remains a bound on what the fleet can spend.
- Upgrade the ledger to schema 2 and then to schema 3 in place, once, after a
  verified byte copy; keep the previous schemas readable by observation during
  the promotion window.
- Rename the exported allowance series and add spend, commitment and remaining
  gauges, because the old name described the reservation sum.
- Document the estimate's scope, the upgrade, the rollback and the operator-side
  reconciliation against the provider bill.

## Impact

- The allowance becomes a cap on tracked spend. No configuration change is
  required; the existing allowance keeps its meaning, now in the intended unit.
- `status`/`plan`/`apply` report `spend`, `committed` and `remaining` instead of
  `consumed`; a consumer that reads the old key must migrate.
- Upgrading a running deployment creates a ledger backup before the first write
  and needs one writable run; observation keeps working until then.
- Rolling the image back after the upgrade requires restoring that backup,
  because the previous schema version is refused by the running code.
- No credential, IAM, network, image or template change.
