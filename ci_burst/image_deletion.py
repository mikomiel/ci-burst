"""The one rule behind every image and snapshot deletion.

Four paths remove an image or a snapshot, and none has a rule of its own:

- ``failed``: the image and snapshots of a build in state ``failed``, and of a
  build in any other state but active whose row records ``TEMPLATE_DRIFT`` (in
  ``reason_code`` or ``drift_result``): its version is unusable, so the image is
  kept for 24 hours and then removed.
- ``pending_timeout``: the same for a failed build whose image never left
  ``pending``.
- ``orphan``: a tagged image or snapshot that has no build row, once it is more
  than 24 hours old.
- ``retention``: the oldest surplus of usable images. It never takes the total
  below ``retain`` and only takes images older than ``retain_min_age_seconds``.

Every path runs the same three steps. ``protected_set`` resolves the images no
path may remove, and refuses with ``IMAGE_DELETION_UNSAFE`` when it cannot.
``plan_deletions`` needs that result, checks the ownership tags of every image
and snapshot, and says what each path would remove. ``apply_deletions`` carries
a plan out only when told to: it deregisters an image, reads the image back, and
deletes the image's snapshots only once the deregistration is confirmed.

Each step refuses to run on input that did not come from the step before it,
with the same code. Only ``protected_set`` can build a ``ProtectedSet`` (the
constructor refuses every other attempt, an empty set included),
``plan_deletions`` refuses any other value, and ``apply_deletions`` refuses a
plan item that ``plan_deletions`` did not make or that was changed afterwards.
These checks stop a caller that skips a step or hands over a made-up result.
They do not stop code that reaches into this module's private names, or that
rewrites an object's attributes with ``object.__setattr__``.

A usable build (``candidate`` or ``promoted``) is left to retention unless its
row records ``TEMPLATE_DRIFT``. The current candidate is protected by id only
while its row does not record it, so a drifted candidate is cleaned up like any
other drifted build. An active build (``building`` or ``imaging``) is never
selected by any path, so a build that timed out is marked failed first and
cleaned by the next call.

The module logs nothing and holds no state. A plan and a report carry ids, build
ids and stable codes, never a tag value other than those or a provider message.
"""

import copy
import os
from dataclasses import InitVar, dataclass, field

from .aws import BUILD_ID_TAG, DEPLOYMENT_TAG, IMAGE_BUILD_TAG
from .config import load
from .errors import ConfigRefusal, ImageRefusal, ProviderRefusal
from .image_state import ACTIVE_STATES
from .image_template import TEMPLATE_DRIFT

IMAGE_DELETION_UNSAFE = "IMAGE_DELETION_UNSAFE"

FAILED = "failed"
PENDING_TIMEOUT = "pending_timeout"
ORPHAN = "orphan"
RETENTION = "retention"
PATHS = (FAILED, PENDING_TIMEOUT, ORPHAN, RETENTION)

#: The age an orphan, and the image of a drifted build, must exceed before a path
#: may take it. Strictly greater: an item of exactly this age is kept.
ORPHAN_AGE_SECONDS = 86_400
DRIFT_HOLD_SECONDS = 86_400

#: Why a plan item is a skip.
PROTECTED = "protected"
TOO_YOUNG = "too_young"
AGE_UNKNOWN = "age_unknown"
#: Why a snapshot of an image is not deleted with it: it is not listed, or its
#: tags do not prove that it belongs to the build of that image.
SNAPSHOT_NOT_VERIFIED = "snapshot_not_verified"

#: The actions of a plan item, and of a report row.
DEREGISTER = "deregister"
DELETE_SNAPSHOT = "delete_snapshot"
SKIP = "skip"

#: The results of a report row.
PLANNED = "planned"
SKIPPED = "skipped"
DEREGISTERED = "deregistered"
DEREGISTER_FAILED = "deregister_failed"
DEREGISTER_UNCONFIRMED = "deregister_unconfirmed"
SNAPSHOT_DELETED = "snapshot_deleted"
SNAPSHOT_FAILED = "snapshot_failed"
SNAPSHOT_KEPT = "snapshot_kept"

_DEREGISTERED_STATE = "deregistered"

#: What only ``protected_set`` hands to ``ProtectedSet``. The constructor checks
#: it when a set is built and ``plan_deletions`` checks the mark it leaves, so a
#: set made any other way is refused.
_PROVENANCE = object()


@dataclass(frozen=True)
class ProtectedSet:
    """The image ids that no deletion path may remove.

    Only ``protected_set`` can build one. The constructor refuses, with
    ``IMAGE_DELETION_UNSAFE``, a set that does not come with the module's own
    proof and an empty set, so ``ProtectedSet(frozenset())`` and a hand-built set
    fail, and so does every ``dataclasses.replace`` copy, with or without other
    images. A resolved set is never empty: it holds at least the image of the
    ``[aws]`` pin. ``plan_deletions`` checks the mark that a resolved set carries
    as well, so a set that skipped the constructor (``object.__new__``, or a
    subclass whose ``__post_init__`` checks nothing) is refused there. So is a
    ``copy.deepcopy`` or a pickle round trip of a resolved set, which loses the
    mark; ``copy.copy`` keeps it.

    The proof is an init-only value that is not stored, and the mark is a stored
    field that takes no part in equality, hash or text.
    """

    image_ids: frozenset
    _proof: InitVar[object] = None
    _resolved: object = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self, proof):
        if proof is not _PROVENANCE or not isinstance(self.image_ids, frozenset) or not self.image_ids:
            raise _unsafe("the protected set was not resolved")
        object.__setattr__(self, "_resolved", _PROVENANCE)

    def __contains__(self, image_id):
        return image_id in self.image_ids


def _is_resolved(protected):
    """Whether ``protected`` is a non-empty set that ``protected_set`` resolved."""
    return isinstance(protected, ProtectedSet) and protected._resolved is _PROVENANCE and bool(protected.image_ids)


def protected_set(
    aws,
    config_path,
    *,
    previous_pin,
    pending_journal,
    candidate,
    active,
    promoted_pin=None,
    latest_pins=(),
    deadline=None,
):
    """Resolve the images that no deletion path may remove, or refuse.

    The set holds, with a launch-template version read from the worker template
    for each pin:

    - the image of the ``[aws]`` pin and of every ``[[aws.availability_zones]]``
      pin;
    - the image of ``previous_pin`` (an int, or ``None`` when no promotion has
      happened);
    - the image of ``promoted_pin`` (an int, or ``None`` when none has happened):
      the new pin of the newest completed journal row, the pin that row wrote to
      the file. The file names it too, unless the pass reads a stale file (a
      container whose bind mount of the file keeps the old inode after
      ``promote --write`` replaces it): then only the journal names the image
      that was just promoted, and retention could remove it;
    - the images of the ``latest_pins`` (a tuple of pins, empty when no row
      follows the newest complete one): both pins of every journal row newer
      than the newest ``complete`` row, whatever its status. A pass that reads
      the stale file resolves a write that never completed its row as
      ``aborted``, so the row is neither pending nor complete, yet the file on
      disk can already pin its new version, and a later failed write does not
      say which pin is live;
    - the images of ``old_pin`` and ``new_pin`` of every row of
      ``pending_journal``;
    - the image of ``candidate`` and of ``active``, the current candidate and the
      active build (each a build row or ``None``; a row with no image yet
      protects nothing by id, and ``plan_deletions`` never selects an active
      build's image either way). A candidate whose row records
      ``TEMPLATE_DRIFT`` (in ``reason_code`` or ``drift_result``) is the one
      exception: its version is unusable, so it is not protected by being the
      candidate, and the ``failed`` path removes its image once the 24-hour hold
      has passed. Any pin that names the same image still protects it. The
      active build is protected whatever its row records.

    ``config_path`` is the deployment file. Every call loads it again, so a pin
    written after the caller's own load is seen. A pending journal row protects
    both of its pins until a later task resolves the row. Resolving a row from
    the hash of the file is task 6.4 and is out of scope here: this function
    never reads the file's bytes and never decides which of a row's two pins is
    live. Pass every row in status ``pending``: a diverged row stays pending.

    It refuses with ``IMAGE_DELETION_UNSAFE`` when the deployment file is a
    view of a file that a rename replaced (no name links its inode), when the
    configuration cannot be loaded, when a pin is not a version number, when the read does not return
    exactly the pinned versions, when a version cannot be read, or when a version
    names no literal image (an alias that resolves later says which image is in
    use only at launch). Then no path may delete anything: ``plan_deletions``
    cannot be called without the result, and no other way to make one exists
    (see ``ProtectedSet``). A refusal of the deadline passes unchanged. The
    detail names a version number at most, never a provider message.
    """
    try:
        stale = os.stat(config_path).st_nlink == 0
    except OSError:
        stale = False  # the load below refuses a file that cannot be read
    if stale:
        # A bind mount of the file itself that a rename elsewhere replaced: its pins and the journal rows that a
        # pass resolved from it are old, so no set built from them says which image is live.
        raise _unsafe("the deployment file is a view of a file that was replaced")
    try:
        config = load(config_path)
    except ConfigRefusal:
        raise _unsafe("the deployment configuration could not be loaded") from None
    pins = [config.aws.launch_template_version]
    pins.extend(zone.launch_template_version for zone in config.aws.zones)
    if previous_pin is not None:
        pins.append(previous_pin)
    if promoted_pin is not None:
        pins.append(promoted_pin)
    pins.extend(latest_pins)
    for row in pending_journal:
        pins.extend((row.old_pin, row.new_pin))
    # Check each pin before the set below merges equal values: 3.0 and True would
    # otherwise collapse into the integers 3 and 1 and pass.
    if not all(type(pin) is int and pin >= 1 for pin in pins):
        raise _unsafe("a pin is not a launch-template version number")
    wanted = sorted(set(pins))
    try:
        read = aws.template_versions(config.aws.launch_template_name, wanted, deadline=deadline)
    except ProviderRefusal:
        raise _unsafe("a launch-template version could not be read") from None
    if sorted(version.version for version in read) != wanted:
        raise _unsafe("the read did not return exactly the pinned versions")
    image_ids = set()
    for version in read:
        image_id = version.data.get("ImageId")
        if not isinstance(image_id, str) or not image_id.startswith("ami-"):
            raise _unsafe(f"version {version.version} does not name a literal image")
        image_ids.add(image_id)
    if candidate is not None and candidate.image_id is not None and not _drifted(candidate):
        image_ids.add(candidate.image_id)
    if active is not None and active.image_id is not None:
        image_ids.add(active.image_id)
    return ProtectedSet(frozenset(image_ids), _PROVENANCE)


def plan_deletions(images, snapshots, protected, *, deployment, mode, now, config, builds):
    """Say what each selected path would remove. Nothing is changed.

    ``images`` and ``snapshots`` are the adapter's ``Image`` and ``Snapshot``
    listings, ``protected`` the result of ``protected_set``, ``now`` epoch
    seconds, ``config`` the enabled ``ImageConfig`` (read only for
    ``retention``) and ``builds`` every build row of the deployment
    (``ImageState.builds``). ``mode`` is ``"all"``, one path name or a collection
    of path names; anything else is a ``ValueError``. Any ``protected`` that
    ``protected_set`` did not resolve (anything that is not a ``ProtectedSet``,
    an empty set, or one made by hand) is refused with ``IMAGE_DELETION_UNSAFE``
    before anything is read.

    An image or snapshot is the module's to remove only when its tags show the
    image-build marker ``"true"``, this ``deployment`` and a build id. An image in
    state ``deregistered`` counts as gone. A snapshot goes with its image only
    when it is listed and carries the same build id. A snapshot that no listed
    image references follows the ``failed`` and ``orphan`` rules by its own tags
    and age; a snapshot of a usable build is left alone.

    Which path takes an image follows its build row:

    - no row: ``orphan``, after ``ORPHAN_AGE_SECONDS``;
    - state ``building`` or ``imaging``: none, ever;
    - ``reason_code`` or ``drift_result`` of ``TEMPLATE_DRIFT``: ``failed``,
      after ``DRIFT_HOLD_SECONDS``;
    - state ``failed``: ``pending_timeout`` while the image is ``pending``,
      else ``failed``, with no wait;
    - any other state: ``retention`` only.

    A protected image is a skip item with reason ``protected`` on every path that
    would have taken it. An image that must wait is a skip with ``too_young``,
    or ``age_unknown`` when the provider gave no creation time, which keeps the
    image. Age is the provider's creation time of the image (or snapshot), never
    the row's.

    Retention counts the owned, ``available`` images of builds in any state but
    active whose row is usable, plus the protected ones. It removes the oldest
    unprotected images older than ``config.retain_min_age_seconds``, as many as
    the count exceeds ``config.retain``, so the total never falls below
    ``retain``. The images of failed builds and the images with no row are not
    counted: their own paths remove them.

    The plan is a list of items sorted by path (the order of ``PATHS``), then by
    image id (an item with no image first), then by snapshot ids. Each item is a
    dict with ``path``, ``image_id`` (``None`` for a snapshot with no image),
    ``build_id``, ``action`` (``deregister``, ``delete_snapshot`` or ``skip``),
    ``reason`` (the skip reason, else ``None``), ``snapshot_ids`` (the verified
    snapshots the item covers) and ``skipped_snapshots`` (``snapshot_id`` and
    ``reason`` of each snapshot of the image that is not verified). An item is a
    ``dict`` in every use (equality, JSON, text) and also remembers what was
    decided, so ``apply_deletions`` can refuse an item that was built by hand or
    changed afterwards. The list itself can be filtered, copied or emptied, and
    an item survives ``copy.copy`` and ``copy.deepcopy``. ``dict(item)``,
    ``item.copy()`` and a JSON round trip give plain dicts, which
    ``apply_deletions`` refuses.
    """
    if not _is_resolved(protected):
        raise _unsafe("the protected set was not resolved")
    paths = _paths(mode)
    if RETENTION in paths and not _limits(config):
        raise _unsafe("the retention limits are not configured")
    rows = {build.id: build for build in builds if build.deployment == deployment}
    listed = [image for image in images if image.state != _DEREGISTERED_STATE]
    by_snapshot = {snapshot.snapshot_id: snapshot for snapshot in snapshots}
    referenced = {snapshot_id for image in listed for snapshot_id in image.snapshot_ids}
    plan, counted = [], []
    for image in listed:
        build_id = _owner(image, deployment)
        row = rows.get(build_id)
        if build_id is None or (row is not None and row.state in ACTIVE_STATES):
            continue
        target = _target(row, image.state)
        if image.state == "available" and (target is None or image.image_id in protected):
            counted.append(image)
        if target is not None and target[0] in paths:
            verdict = _verdict(image.image_id, image.created_at, target[1], protected, now)
            plan.append(_image_item(target[0], image, build_id, verdict, by_snapshot, deployment))
    for snapshot in snapshots:
        build_id = _owner(snapshot, deployment)
        row = rows.get(build_id)
        if snapshot.snapshot_id in referenced or build_id is None or (row is not None and row.state in ACTIVE_STATES):
            continue
        target = _target(row, None)
        if target is not None and target[0] in paths:
            verdict = _verdict(None, snapshot.created_at, target[1], protected, now)
            plan.append(_item(target[0], None, build_id, verdict, [snapshot.snapshot_id], [], lone=True))
    if RETENTION in paths:
        surplus = max(0, len(counted) - config.retain)
        eligible = [
            image
            for image in counted
            if image.image_id not in protected
            and image.created_at is not None
            and now - image.created_at > config.retain_min_age_seconds
        ]
        eligible.sort(key=lambda image: (image.created_at, image.image_id))
        for image in eligible[:surplus]:
            plan.append(_image_item(RETENTION, image, _owner(image, deployment), None, by_snapshot, deployment))
    plan.sort(key=lambda item: (PATHS.index(item["path"]), item["image_id"] or "", item["snapshot_ids"]))
    return plan


def apply_deletions(aws, plan, *, apply):
    """Carry out a plan from ``plan_deletions``, or only describe it.

    ``aws`` is an ``ImageAws``. Without ``apply`` no call is made at all and
    every action is reported as ``planned``. With it, an image is deregistered
    and then read back: it is deregistered when the provider no longer knows it
    or reports the state ``deregistered``. Only then are its snapshots deleted. A
    deregistration that is refused, or not confirmed (still registered, or the
    read-back refused), deletes no snapshot and is reported, and the run goes on
    with the next item. A refused snapshot deletion is reported the same way. Only
    a provider refusal is reported: any other error propagates and stops the run.

    Ownership and protection were decided by ``plan_deletions``, and nothing
    here decides them again. Instead, before any call (a dry run included), every
    item is checked: it must be an item ``plan_deletions`` made, unchanged since
    (a plain ``dict``, or one that was edited, is not), with one of the actions
    ``deregister``, ``delete_snapshot`` or ``skip``. If one item fails, or
    ``plan`` is not an iterable of items, the whole plan is refused with
    ``IMAGE_DELETION_UNSAFE``, nothing runs, and the detail names no id. An empty
    plan, a filtered list, a copied list and ``copy.copy`` or ``copy.deepcopy`` of
    the items are fine. A plain ``dict`` copy of an item (``dict(item)``,
    ``item.copy()``, or a JSON round trip of the plan) is refused.

    The report is a flat list of rows, each with ``path``, ``image_id``,
    ``action`` and ``result``, and ``snapshot_id``, ``reason`` (a skip's) or
    ``code`` (the reason code of a provider refusal) where they apply. The
    results are ``planned``, ``skipped``, ``deregistered``,
    ``deregister_failed``, ``deregister_unconfirmed``, ``snapshot_deleted``,
    ``snapshot_failed`` and ``snapshot_kept`` (not tried, because the image's
    deregistration was not confirmed).
    """
    report = []
    for item in _decided_items(plan):
        if item["action"] == SKIP:
            report.append(_row(item, SKIP, SKIPPED, reason=item["reason"]))
            continue
        # What is left is ``deregister`` or ``delete_snapshot`` (a snapshot that no
        # image references, deleted with no deregistration before it).
        confirmed = True
        if item["action"] == DEREGISTER:
            if apply:
                result, code = _deregister(aws, item["image_id"])
                confirmed = result == DEREGISTERED
            else:
                result, code = PLANNED, None
            report.append(_row(item, DEREGISTER, result, code=code))
        for snapshot_id in item["snapshot_ids"]:
            if not apply:
                result, code = PLANNED, None
            elif not confirmed:
                result, code = SNAPSHOT_KEPT, None
            else:
                result, code = _delete_snapshot(aws, snapshot_id)
            report.append(_row(item, DELETE_SNAPSHOT, result, snapshot_id=snapshot_id, code=code))
        for entry in item["skipped_snapshots"]:
            report.append(
                _row(item, DELETE_SNAPSHOT, SKIPPED, snapshot_id=entry["snapshot_id"], reason=entry["reason"])
            )
    return report


def _unsafe(detail):
    return ImageRefusal(IMAGE_DELETION_UNSAFE, detail)


class _PlanItem(dict):
    """A plan item: a plain ``dict`` that also keeps a copy of what was decided.

    ``plan_deletions`` makes these and ``apply_deletions`` runs nothing else. The
    copy is deep, so editing the item, or a list inside it, after the decision
    makes the item differ from it.
    """

    __slots__ = ("_decided",)

    def unchanged(self):
        decided = getattr(self, "_decided", None)
        return decided is not None and dict(self) == decided


#: The actions ``apply_deletions`` knows. A tuple, so an item whose action is an
#: unhashable value is refused like any other unknown one.
_ACTIONS = (DEREGISTER, DELETE_SNAPSHOT, SKIP)


def _decided_items(plan):
    """The items of ``plan``, once every one is shown to be as ``plan_deletions`` made it.

    Every item is checked before any is run: one bad item refuses the whole plan.
    """
    try:
        items = list(plan)
    except TypeError:
        raise _unsafe("the plan was not made by plan_deletions") from None
    for item in items:
        if not isinstance(item, _PlanItem):
            raise _unsafe("the plan was not made by plan_deletions")
        if item.get("action") not in _ACTIONS:
            raise _unsafe("a plan item has an action that is not known")
        if not item.unchanged():
            raise _unsafe("a plan item was changed after plan_deletions made it")
    return items


def _paths(mode):
    if mode == "all":
        return set(PATHS)
    try:
        names = {mode} if isinstance(mode, str) else set(mode)
    except TypeError:
        names = set()
    if not names or not names <= set(PATHS):
        raise ValueError("mode must be 'all', a deletion path or a collection of them: " + ", ".join(PATHS))
    return names


def _limits(config):
    retain, age = getattr(config, "retain", None), getattr(config, "retain_min_age_seconds", None)
    return type(retain) is int and type(age) is int and retain >= 1 and age >= 0


def _owner(item, deployment):
    """The build id that an image's or snapshot's tags prove it was made for, or ``None``."""
    build_id = item.tags.get(BUILD_ID_TAG)
    owned = item.tags.get(IMAGE_BUILD_TAG) == "true" and item.tags.get(DEPLOYMENT_TAG) == deployment
    return build_id if owned and isinstance(build_id, str) and build_id else None


def _drifted(row):
    """Whether the build's row records ``TEMPLATE_DRIFT``, in ``reason_code`` or ``drift_result``."""
    return TEMPLATE_DRIFT in (row.reason_code, row.drift_result)


def _target(row, image_state):
    """The path that takes an item of this build and the age it must exceed first.

    ``None`` means no path takes it but retention (a usable build). The wait is
    ``None`` when the item need not wait.
    """
    if row is None:
        return ORPHAN, ORPHAN_AGE_SECONDS
    if _drifted(row):
        return FAILED, DRIFT_HOLD_SECONDS
    if row.state == "failed":
        return (PENDING_TIMEOUT if image_state == "pending" else FAILED), None
    return None


def _verdict(image_id, created_at, wait, protected, now):
    """Why a selected item is a skip, or ``None`` when its path may take it."""
    if image_id is not None and image_id in protected:
        return PROTECTED
    if wait is None:
        return None
    if created_at is None:
        return AGE_UNKNOWN
    return None if now - created_at > wait else TOO_YOUNG


def _image_item(path, image, build_id, verdict, by_snapshot, deployment):
    verified, unverified = [], []
    for snapshot_id in dict.fromkeys(image.snapshot_ids):
        snapshot = by_snapshot.get(snapshot_id)
        if snapshot is not None and _owner(snapshot, deployment) == build_id:
            verified.append(snapshot_id)
        else:
            unverified.append({"snapshot_id": snapshot_id, "reason": SNAPSHOT_NOT_VERIFIED})
    return _item(path, image.image_id, build_id, verdict, verified, unverified)


def _item(path, image_id, build_id, verdict, snapshot_ids, skipped_snapshots, *, lone=False):
    if verdict is not None:
        action = SKIP
    else:
        action = DELETE_SNAPSHOT if lone else DEREGISTER
    item = _PlanItem(
        path=path,
        image_id=image_id,
        build_id=build_id,
        action=action,
        reason=verdict,
        snapshot_ids=snapshot_ids,
        skipped_snapshots=skipped_snapshots,
    )
    item._decided = copy.deepcopy(dict(item))
    return item


def _deregister(aws, image_id):
    """Deregister an image and read it back: the result and the refusal's code."""
    try:
        aws.deregister_image(image_id)
    except ProviderRefusal as refusal:
        return DEREGISTER_FAILED, refusal.reason
    try:
        found = aws.image(image_id)
    except ProviderRefusal as refusal:
        return DEREGISTER_UNCONFIRMED, refusal.reason
    if found is None or found.state == _DEREGISTERED_STATE:
        return DEREGISTERED, None
    return DEREGISTER_UNCONFIRMED, None


def _delete_snapshot(aws, snapshot_id):
    try:
        aws.delete_snapshot(snapshot_id)
    except ProviderRefusal as refusal:
        return SNAPSHOT_FAILED, refusal.reason
    return SNAPSHOT_DELETED, None


def _row(item, action, result, *, snapshot_id=None, reason=None, code=None):
    row = {"path": item["path"], "image_id": item["image_id"], "action": action, "result": result}
    if snapshot_id is not None:
        row["snapshot_id"] = snapshot_id
    if reason is not None:
        row["reason"] = reason
    if code:
        row["code"] = code
    return row
