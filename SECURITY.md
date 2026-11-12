# Security policy

## Project status

The controller source is implemented and has tagged releases. The project does
not support a production deployment: the shipped default is `fixtures-only`, and
live qualification is the operator's recorded evidence. A green CI run, a merge
or a tag is not proof of a live deployment.

## Reporting a vulnerability

Use GitHub private vulnerability reporting for this repository when available.
If no private reporting route is available, open a minimal issue requesting
a private contact; do not include exploit details, credentials, private
deployment information or customer data in that issue. A private channel must
be agreed before sensitive material is shared.

Provide the affected commit/version, expected boundary, observed behavior
and a synthetic reproduction. Never send cloud keys, runner registration
tokens, session caches or production data. No response-time or bounty
commitment is made.

## Product security boundaries

- Forgejo owns scheduling and CI results; a VM launch is not a passed job.
- A worker is untrusted after accepting job code. It must not receive the
  controller's cloud-provisioning identity.
- Images, networking and secure runner registration are operator-owned
  prerequisites, not resources provided by this bootstrap.
- Unknown queue, price, ownership or state blocks new capacity. Cleanup
  failures must remain visible and cannot imply that charges stopped.
- No cloud mutation, IAM change, public endpoint, release or paid resource
  is part of credential-free public CI.
