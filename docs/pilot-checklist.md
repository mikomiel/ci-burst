# Pilot checklist

This checklist is a preparation record, not authorization. Creating cloud
resources, changing IAM or network configuration, publishing an image or
running a pilot needs a separate operator decision with cost, lifetime and
cleanup bounds.

## 1. Image and template

- [ ] Image builds the pinned runner and the job toolchain the queue needs.
- [ ] Template version is numeric and points at a literal, immutable image id.
- [ ] `InstanceType`, architecture and memory suit the intended job class.
- [ ] `InstanceInitiatedShutdownBehavior = terminate`; disposable volumes
      delete on termination.
- [ ] Bootstrap registers a repository-scoped ephemeral runner, runs one job,
      exits after it, and also exits on bootstrap failure and on no job.
- [ ] Maximum lifetime covers boot, queue wait and a failed bootstrap.
- [ ] The worker holds no controller, provisioning or AWS credential.
- [ ] Registration credentials arrive through an operator-owned secure channel;
      not user data, image layers, command arguments or logs.

## 2. Labels

- [ ] Every advertised label is qualified against the image and machine.
- [ ] The burst label is not advertised by the persistent fleet, or the
      operator accepts the routing overlap explicitly.
- [ ] A test job with the burst label completes; a job without it does not
      land on the burst worker.

## 3. Forgejo contract

- [ ] An owner-scoped read token can call the runner-jobs endpoint.
- [ ] One waiting, one dependency-blocked and one running job classify
      correctly in a `plan` run.
- [ ] `qualification = "live-verified"` is set only after that evidence is
      recorded with the deployment.

## 4. Capacity, quota and network

- [ ] The Region and every configured availability-zone candidate have the
      chosen instance type and Spot capacity; the account's Spot quota covers
      one instance.
- [ ] The worker reaches the Forgejo endpoint over the approved path; TLS
      verification is on and no redirect is followed.
- [ ] Egress from the worker is limited to what the queue needs; the worker
      cannot reach unrelated private networks.

## 5. Identity, limits and state

- [ ] Scaling and cleanup use separate machine identities with the reviewed
      scopes; neither is a copied human login cache.
- [ ] `max_spot_price`, `max_lifetime_seconds`, `window_seconds`, `allowance`
      and `non_compute_allowance` are reviewed for the pilot.
- [ ] The ledger path is on durable local storage with the writer lock proven.
- [ ] The scheduler runs `apply`; a separate scheduler runs `reap --apply`
      outside the scaler's failure boundary, and that path was tested while the
      scaler was stopped.

## 6. Ownership and cleanup evidence

- [ ] A plan reports the exact template, Region, ordered candidates, selected
      zone and reservation.
- [ ] One owned instance readback shows all four ownership tags and the expiry.
- [ ] A refused or denied cleanup stays visible and does not free the slot.
- [ ] The operator knows that an accepted termination request is not confirmed
      removal and that residual charges may remain.

## 7. Rollback

- [ ] Disabling the `apply` schedule stops admission without touching cleanup.
- [ ] Owned resources can be identified from tags alone for cleanup.
- [ ] Reverting the source is a reviewed change, not a history rewrite.

## 8. Acceptance

- [ ] One comparable job completed and its cost was recorded against the
      reservation and the operator's expectation.
- [ ] Abrupt instance loss was reconciled; Forgejo, not ci-burst, decided the
      job result.
- [ ] No On-Demand fallback, automatic retry or manufactured success occurred.
