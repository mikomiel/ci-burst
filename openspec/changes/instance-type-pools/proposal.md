# Proposal

## Why

A deployment can only place a worker in the zones its launch template's single
instance type happens to have Spot capacity in. Measured on the pilot over one
day, the ordered zone fallback absorbed 114 definite `InsufficientInstanceCapacity`
rejections and still lost six admissions after every zone refused, while the
same Region offered five other instance types of the same shape with spare
capacity in all three zones. One type in three zones is three pools; the same
three zones with two same-shape types are six.

The zones themselves cannot grow: `eu-central-1` has exactly three availability
zones and all three are already configured. A second Region would add zones, but
it needs its own VPC, subnets, router, image copy, launch template, regional
registration parameter and multi-region state handling — days of work and new
infrastructure — to widen the pools by the same amount that one optional list
widens them.

## What Changes

- Add an optional ordered `aws.instance_types` list to the deployment
  configuration: an array of one to four EC2 instance type names, refused as
  `INSTANCE_TYPES_INVALID` when it is empty, repeated, malformed or too long.
  Absent means the launch template's own `InstanceType`, so existing
  deployments behave exactly as they do now.
- Treat each configured type in each configured zone as one ordered placement
  pool. The order is type-major — every zone of the first type, then every zone
  of the next — and it is the priority.
- Send the selected pool's `InstanceType` explicitly in the frozen
  `RunInstances` request. The template's `InstanceType` stays required and
  becomes the default and the shape reference.
- Rotate a batch across the first eligible type's zones only when a list is
  configured, so a later type is reached by a definite capacity rejection and
  not by batch spread. Without a list the original whole-list rotation stays,
  because then each candidate's type comes from its own template version.
- Report the pool (zone and type) in plan output, launch records and attempt
  records, and name the pool in the fallback detail.
- Extend the operator prerequisite check: every configured type must be offered
  in every configured zone, must match the template type's vCPU and memory, and
  the template image must support its architecture.

## Impact

- A deployment that omits `aws.instance_types` sends the template's type
  explicitly instead of relying on the template default, and is otherwise
  unchanged: same number of pools, same prices, same admission.
- A deployment that configures a list prices every pool on every admission —
  at most twelve Spot price reads for four types and three zones — and may use
  any of them through the existing capacity fallback.
- No change to the allowance, reservation, settlement, ledger schema, IAM
  policy, network, image or launch template. The `RunInstances` policy condition
  is on the launch template, so an `InstanceType` override is already permitted.
- Enabling a type is a configuration decision; it requires the operator check
  and a canary launch per new type, not a product release.
