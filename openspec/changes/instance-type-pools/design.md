# Design

## Context

See [proposal.md](proposal.md) for motivation. The controller already treats
placement as one ordered list: it prices every configured candidate before any
time-sensitive check, selects the first eligible one, and walks the rest only
after a definite capacity rejection. That contract is exactly what a wider pool
list needs; the change is about identity and ordering, not about a new
scheduler.

```
plans = [price(zone, type) for type in configured types for zone in configured zones]
selected = first eligible plan
on definite capacity rejection: next plan in that order
```

## Goals / Non-Goals

**Goals:** more Spot capacity per Region without new infrastructure; one
environment for the whole fleet (same shape and architecture); the configured
order remains a predictable priority; existing deployments behave unchanged
apart from an explicit, equivalent request field; the operator can verify the
list before enabling it.

**Non-Goals:** multiple Regions, multiple architectures, mixed shapes (for
example a CPU-optimised type with less memory), On-Demand capacity, dynamic or
learned pool ordering, a warm pool, or any change to admission, reservations,
settlement, expiry and cleanup.

## Decisions

### An ordered global type list, not per-zone entries

The zone list already expresses placement. A second list of type names, applied
to every configured candidate, expresses what the operator actually chooses —
"this fleet may run on any of these types" — without repeating subnets per type
and without a second place to keep in step with the network. A per-zone type
sub-list would duplicate the subnet and template facts and would make a
same-shape invariant impossible to state.

### Type-major order, and batch rotation stays inside the first type

The flat order is every zone of the first type, then every zone of the next.
That keeps the first type's zones as the normal landing places and makes later
types genuine fallbacks. Batch admission currently rotates its start across all
eligible candidates; with more than one type that would spread a twelve-worker
burst onto the fallback types, which contradicts "the order is the priority".
The rotation width becomes the first type's contiguous eligible block, and the
rotation keeps the later blocks in order after the preferred one, so a capacity
rejection inside the preferred type still tries that type's remaining zones
before any later type. Without a configured list each candidate's type comes
from its own template version — two candidate types there are not a preference
order — so the whole eligible list keeps rotating exactly as before.

### The template's type remains the shape anchor

The launch template keeps its required `InstanceType`; the controller sends the
selected pool's type explicitly. This gives one code path and a frozen request
that states exactly what was launched, and it keeps the template type as the
comparison point for shape and architecture. The controller does not add a
provider metadata call per admission: the list is static configuration, and a
bad entry would fail launches, so the check belongs in the operator
prerequisite command that already validates the template, subnets and routing.

### Four types, not unlimited

Every admission prices every pool synchronously, and that price read is the
slow, rate-limited part of a tick. Four types across three zones is twelve
reads, which keeps one admission bounded while covering the practical
same-shape options. A larger list is refused at load time rather than silently
becoming slow.

### Skip, do not fall through, for price

A pool above `max_spot_price` is skipped like any other ineligible candidate;
only a definite capacity rejection advances. This is unchanged, and it means a
cheaper or more expensive type can be configured without a second price cap.
`eu-central-1` currently offers the same-shape types at 0.0709–0.1081, all under
the pilot's 0.1200 cap.

## Failure behavior

- A type not offered in a zone, or with a different shape, is found by
  `check_prerequisites.py` before the list is enabled (drift, exit 2).
- At admission, an unavailable price for a pool skips that pool with
  `spot_price_unavailable`; the whole plan refuses only when no pool is
  eligible.
- A non-capacity launch failure still stops the batch and does not fall
  through; a wrong type or architecture therefore surfaces as a failed launch
  with the provider code instead of a silent mixed fleet.
- Uncertain creation, expiry, reservation and fallback token advance are
  untouched; the pool change is another placement value inside the same
  operation.

## Alternatives considered

- **Per-zone type entries.** Rejected: repeats network facts, allows a
  same-shape invariant violation per zone, and multiplies configuration.
- **`RunInstances` Fleet with instance overrides.** Rejected: a fleet is a
  second scheduler and changes the operation identity and token model that the
  durable ledger relies on.
- **A second Region.** Deferred: adds VPC, subnets, router, image copy, template
  and regional parameter ownership for the same number of pools this list
  provides in the existing Region.
- **Raising `controller.max_instances`.** Out of scope: it changes how many
  workers may run, not where they can launch, and was raised to twelve earlier
  the same day.

## Evidence gaps

- The image must boot every configured type. Only a canary launch proves it;
  the prerequisite check proves architecture support, not boot.
- Spot prices and offerings are observed facts, not guarantees; the Type list
  widening is capacity insurance, not a promise of capacity in every pool.
