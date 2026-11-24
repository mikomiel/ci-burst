"""Tests for the one image deletion rule.

The real ``ImageAws`` runs against a fake EC2 client that applies a
deregistration or a snapshot deletion to its own stores, so a test sees what was
really removed and in which order. The fake can refuse a call, leave an image
registered after a call that succeeded, or leave it visible in state
``deregistered``. Images, snapshots and build rows are built from synthetic ids.

The rule is checked through the three calls a pass makes: ``protected_set``,
``plan_deletions`` and ``apply_deletions``. ``FailClosedTests`` composes them the
way a caller does, so a path that deleted after a failed resolution would show up
as a call on the fake.
"""

import collections
import copy
import dataclasses
import json
import os
import stat
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

from ci_burst import image_deletion
from ci_burst.aws import BUILD_ID_TAG, DEPLOYMENT_TAG, IMAGE_BUILD_TAG, Image, ImageAws, Snapshot
from ci_burst.config import ImageConfig, load
from ci_burst.errors import DeadlineRefusal, ImageRefusal, ProviderRefusal
from ci_burst.image_deletion import ProtectedSet, apply_deletions, plan_deletions, protected_set
from ci_burst.image_state import Build, Journal

from support import Clock, write_config
from test_image import IMAGE_TOML
from test_image_aws import SENTINELS, FakeEc2, ProviderError, tag_list

NOW = 1_800_000_000.0
HOUR = 3600
DAY = 86_400
REGION = "eu-central-1"
PROFILE = "example-image-profile"
DEPLOYMENT = "example-deployment"
OTHER_DEPLOYMENT = "example-other-deployment"
WORKER = "example-ci-burst-worker"
UNSAFE = "IMAGE_DELETION_UNSAFE"


def ami(number):
    return f"ami-{number:017x}"


def snap(number):
    return f"snap-{number:017x}"


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


#: Launch-template versions of the worker template, and the image each names.
PREVIOUS, PIN, ZONE_PIN, JOURNAL_OLD, JOURNAL_NEW = 2, 3, 4, 5, 6
PIN_IMAGE, PREVIOUS_IMAGE, ZONE_IMAGE = ami(0xA1), ami(0xA3), ami(0xA2)
JOURNAL_OLD_IMAGE, JOURNAL_NEW_IMAGE = ami(0xA4), ami(0xA5)
CANDIDATE_IMAGE, ACTIVE_IMAGE = ami(0xA6), ami(0xA7)
VERSION_IMAGES = {
    PREVIOUS: PREVIOUS_IMAGE,
    PIN: PIN_IMAGE,
    ZONE_PIN: ZONE_IMAGE,
    JOURNAL_OLD: JOURNAL_OLD_IMAGE,
    JOURNAL_NEW: JOURNAL_NEW_IMAGE,
}
ZONE_ENTRY = {"zone": "eu-central-1b", "subnet_id": "subnet-0123456789abcdef0", "launch_template_version": ZONE_PIN}

#: Every source of protection, and the image it protects.
SOURCES = {
    "pin": PIN_IMAGE,
    "zone pin": ZONE_IMAGE,
    "previous pin": PREVIOUS_IMAGE,
    "journal old pin": JOURNAL_OLD_IMAGE,
    "journal new pin": JOURNAL_NEW_IMAGE,
    "candidate": CANDIDATE_IMAGE,
    "active build": ACTIVE_IMAGE,
}

Made = collections.namedtuple("Made", "build_id image_id snapshot_id")


class DeletionEc2(FakeEc2):
    """A fake EC2 whose deregistrations and snapshot deletions change its stores."""

    def __init__(self, calls):
        super().__init__(calls)
        #: image id -> the error ``deregister_image`` raises (nothing changes)
        self.refused = {}
        #: image ids whose deregistration succeeds but which stay registered
        self.stuck = set()
        #: image ids that stay visible in state ``deregistered`` afterwards
        self.ghosts = set()
        #: snapshot id -> the error ``delete_snapshot`` raises
        self.undeletable = {}

    def describe_launch_template_versions(self, **arguments):
        response = super().describe_launch_template_versions(**arguments)
        if "describe_launch_template_versions" in self.pages:
            response["NextToken"] = "next-page"  # the listing never ends: the adapter refuses it
        return response

    def deregister_image(self, *, ImageId):
        self._enter("deregister_image", dict(ImageId=ImageId))
        if ImageId in self.refused:
            raise self.refused[ImageId]
        if ImageId in self.stuck:
            return {}
        for item in list(self.images_store):
            if item["ImageId"] == ImageId:
                if ImageId in self.ghosts:
                    item["State"] = "deregistered"
                else:
                    self.images_store.remove(item)
        return {}

    def delete_snapshot(self, *, SnapshotId):
        self._enter("delete_snapshot", dict(SnapshotId=SnapshotId))
        if SnapshotId in self.undeletable:
            raise self.undeletable[SnapshotId]
        self.snapshots_store[:] = [item for item in self.snapshots_store if item["SnapshotId"] != SnapshotId]
        return {}


def owner_tags(build_id="build-lost", deployment=DEPLOYMENT):
    tags = {IMAGE_BUILD_TAG: "true", DEPLOYMENT_TAG: deployment}
    if build_id is not None:
        tags[BUILD_ID_TAG] = build_id
    return tags


#: The two places a build row records ``TEMPLATE_DRIFT``.
DRIFT_ROWS = ({"reason_code": "TEMPLATE_DRIFT"}, {"drift_result": "TEMPLATE_DRIFT"})


def forged_set(image_ids):
    """A ``ProtectedSet`` that skipped its constructor, as ``object.__new__`` or a copy could make one."""
    forged = object.__new__(ProtectedSet)
    object.__setattr__(forged, "image_ids", frozenset(image_ids))
    return forged


class UncheckedSet(ProtectedSet):
    """A subclass whose constructor checks nothing, so its instances get past ``__post_init__``."""

    def __post_init__(self, _proof):
        pass


class DeletionCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.reset()

    def reset(self):
        """A new account, new state rows and a valid deployment file."""
        self.calls = []
        self.ec2 = DeletionEc2(self.calls)
        self.aws = ImageAws(REGION, PROFILE, clients={"ec2": self.ec2}, clock=Clock(NOW), expiry=NOW + 3600)
        self.rows = []
        self.write_deployment()
        for number, image_id in VERSION_IMAGES.items():
            self.set_version(number, image_id)
        self.config = dataclasses.replace(load(self.deployment_file).image, retain=3, retain_min_age_seconds=DAY)

    # -- the account ----------------------------------------------------
    def write_deployment(self, *, zones=None):
        aws = {"launch_template_name": WORKER, "launch_template_version": PIN}
        if zones is not None:
            aws["availability_zones"] = zones
        self.deployment_file = write_config(self.directory, config={"deployment": DEPLOYMENT, "aws": aws})
        self.deployment_file.write_text(self.deployment_file.read_text(encoding="utf-8") + IMAGE_TOML, encoding="utf-8")

    def set_version(self, number, image_id):
        self.ec2.versions_store[(WORKER, number)] = {
            "VersionNumber": number,
            "LaunchTemplateId": "lt-0123456789abcdef0",
            "LaunchTemplateData": {"ImageId": image_id},
        }

    def add_image(self, image_id, build_id="build-lost", *, age=3 * DAY, state="available", snapshots=(), tags=None):
        item = {
            "ImageId": image_id,
            "State": state,
            "Tags": tag_list(owner_tags(build_id) if tags is None else tags),
            "BlockDeviceMappings": [{"Ebs": {"SnapshotId": snapshot_id}} for snapshot_id in snapshots],
        }
        if age is not None:
            item["CreationDate"] = iso(NOW - age)
        self.ec2.images_store.append(item)

    def add_snapshot(self, snapshot_id, build_id="build-lost", *, age=3 * DAY, tags=None):
        item = {
            "SnapshotId": snapshot_id,
            "State": "completed",
            "Tags": tag_list(owner_tags(build_id) if tags is None else tags),
        }
        if age is not None:
            item["StartTime"] = datetime.fromtimestamp(NOW - age, timezone.utc)
        self.ec2.snapshots_store.append(item)

    def make_build(self, build_id, state="failed", **overrides):
        values = dict(
            id=build_id,
            deployment=DEPLOYMENT,
            client_token="token-" + build_id,
            basis_digest="a" * 64,
            source_sha="b" * 40,
            state=state,
            reason_code="",
            created_at=NOW - 5 * DAY,
            updated_at=NOW - 5 * DAY,
            deadline_at=NOW - 5 * DAY + 2700,
        )
        values.update(overrides)
        return Build(**values)

    def build(self, build_id, state="failed", **overrides):
        row = self.make_build(build_id, state, **overrides)
        self.rows.append(row)
        return row

    def made(self, number, state="failed", *, age=3 * DAY, image_state="available", image_id=None, **row):
        """A build row with its image and one snapshot, all tagged for it."""
        build_id, snapshot_id = f"build-{number:04d}", snap(number)
        image_id = image_id or ami(number)
        self.build(build_id, state, **row)
        self.add_snapshot(snapshot_id, build_id, age=age)
        self.add_image(image_id, build_id, age=age, state=image_state, snapshots=(snapshot_id,))
        return Made(build_id, image_id, snapshot_id)

    @staticmethod
    def journal(old_pin=JOURNAL_OLD, new_pin=JOURNAL_NEW, **overrides):
        values = dict(
            id=1,
            kind="promotion",
            status="pending",
            old_pin=old_pin,
            new_pin=new_pin,
            old_file_sha256="1" * 64,
            new_file_sha256="2" * 64,
            candidate_id="build-0001",
            backup_name="deployment.toml.bak.20260101T000000Z",
            backup_sha256="1" * 64,
            contained_pin=old_pin,
            created_at=NOW,
            updated_at=NOW,
        )
        values.update(overrides)
        return Journal(**values)

    # -- the three calls ------------------------------------------------
    def images(self):
        return self.aws.images(DEPLOYMENT)

    def snapshots(self):
        return self.aws.snapshots(DEPLOYMENT)

    def resolve(self, **overrides):
        values = dict(previous_pin=None, pending_journal=(), candidate=None, active=None)
        values.update(overrides)
        return protected_set(self.aws, self.deployment_file, **values)

    def plan(
        self, mode="all", *, protected=None, images=None, snapshots=None, config=None, builds=None, now=NOW, **resolve
    ):
        return plan_deletions(
            self.images() if images is None else images,
            self.snapshots() if snapshots is None else snapshots,
            self.resolve(**resolve) if protected is None else protected,
            deployment=DEPLOYMENT,
            mode=mode,
            now=now,
            config=self.config if config is None else config,
            builds=self.rows if builds is None else builds,
        )

    def run_paths(self, mode="all", *, apply=True, **resolve):
        """Resolve, plan and apply, as a caller does."""
        return apply_deletions(self.aws, self.plan(mode, **resolve), apply=apply)

    # -- what happened --------------------------------------------------
    def names(self):
        return [name for name, _ in self.calls]

    def requests(self, name):
        return [arguments for call, arguments in self.calls if call == name]

    def mutations(self):
        return [name for name in self.names() if name in ("deregister_image", "delete_snapshot")]

    def stored_images(self):
        return {item["ImageId"] for item in self.ec2.images_store}

    def stored_snapshots(self):
        return {item["SnapshotId"] for item in self.ec2.snapshots_store}

    @staticmethod
    def summary(plan):
        """The path, action, image id and reason of each item, for short assertions."""
        return [(item["path"], item["action"], item["image_id"], item["reason"]) for item in plan]

    @staticmethod
    def results(report):
        return [(row["action"], row["result"]) for row in report]

    def protection(self, source):
        """Set the account up so that ``source`` protects its image; the resolve arguments."""
        self.write_deployment(zones=[ZONE_ENTRY] if source == "zone pin" else None)
        if source == "previous pin":
            return {"previous_pin": PREVIOUS}
        if source in ("journal old pin", "journal new pin"):
            return {"pending_journal": [self.journal()]}
        if source == "candidate":
            return {"candidate": self.make_build("build-cand", "candidate", image_id=CANDIDATE_IMAGE)}
        if source == "active build":
            return {"active": self.make_build("build-active", "imaging", image_id=ACTIVE_IMAGE)}
        return {}


class ProtectedSetTests(DeletionCase):
    def test_the_pinned_image_is_protected(self):
        protected = self.resolve()
        self.assertIsInstance(protected, ProtectedSet)
        self.assertEqual(protected.image_ids, frozenset({PIN_IMAGE}))
        self.assertIn(PIN_IMAGE, protected)
        self.assertNotIn(ZONE_IMAGE, protected)

    def test_each_source_protects_its_own_image(self):
        for source, image_id in SOURCES.items():
            with self.subTest(source):
                arguments = self.protection(source)
                self.assertIn(image_id, self.resolve(**arguments))

    def test_a_source_protects_nothing_else_by_itself(self):
        for source in ("zone pin", "previous pin", "candidate", "active build"):
            with self.subTest(source):
                arguments = self.protection(source)
                self.assertEqual(self.resolve(**arguments).image_ids, {PIN_IMAGE, SOURCES[source]})

    def test_both_pins_of_a_pending_journal_row_are_protected(self):
        protected = self.resolve(pending_journal=[self.journal()])
        self.assertEqual(protected.image_ids, {PIN_IMAGE, JOURNAL_OLD_IMAGE, JOURNAL_NEW_IMAGE})

    def test_every_pending_journal_row_counts(self):
        rows = [self.journal(old_pin=JOURNAL_OLD, new_pin=PREVIOUS), self.journal(old_pin=ZONE_PIN, new_pin=JOURNAL_NEW)]
        protected = self.resolve(pending_journal=rows)
        self.assertEqual(
            protected.image_ids, {PIN_IMAGE, JOURNAL_OLD_IMAGE, PREVIOUS_IMAGE, ZONE_IMAGE, JOURNAL_NEW_IMAGE}
        )

    def test_all_sources_together(self):
        self.write_deployment(zones=[ZONE_ENTRY])
        protected = self.resolve(
            previous_pin=PREVIOUS,
            pending_journal=[self.journal()],
            candidate=self.make_build("build-cand", "candidate", image_id=CANDIDATE_IMAGE),
            active=self.make_build("build-active", "imaging", image_id=ACTIVE_IMAGE),
        )
        self.assertEqual(protected.image_ids, set(SOURCES.values()))

    def test_the_versions_are_read_once_from_the_worker_template(self):
        self.write_deployment(zones=[ZONE_ENTRY])
        self.resolve(previous_pin=PREVIOUS, pending_journal=[self.journal()])
        (read,) = self.requests("describe_launch_template_versions")
        self.assertEqual(read["LaunchTemplateName"], WORKER)
        self.assertEqual(read["Versions"], [str(number) for number in sorted(VERSION_IMAGES)])
        self.assertEqual(self.names(), ["describe_launch_template_versions"])

    def test_a_version_that_two_sources_share_is_read_once(self):
        self.write_deployment(zones=[dict(ZONE_ENTRY, launch_template_version=PIN)])
        self.resolve(previous_pin=PIN, pending_journal=[self.journal(old_pin=PIN, new_pin=PIN)])
        (read,) = self.requests("describe_launch_template_versions")
        self.assertEqual(read["Versions"], [str(PIN)])

    def test_a_build_without_an_image_protects_nothing_by_id(self):
        candidate = self.make_build("build-cand", "candidate")
        active = self.make_build("build-active", "building")
        self.assertEqual(self.resolve(candidate=candidate, active=active).image_ids, {PIN_IMAGE})

    def test_a_missing_candidate_or_active_build_is_fine(self):
        self.assertEqual(self.resolve(candidate=None, active=None).image_ids, {PIN_IMAGE})

    def test_a_candidate_whose_row_records_template_drift_protects_nothing_by_itself(self):
        for row in DRIFT_ROWS:
            with self.subTest(row=row):
                drifted = self.make_build("build-cand", "candidate", image_id=CANDIDATE_IMAGE, **row)
                self.assertNotIn(CANDIDATE_IMAGE, self.resolve(candidate=drifted))
                self.assertEqual(self.resolve(candidate=drifted).image_ids, {PIN_IMAGE})

    def test_a_candidate_without_drift_still_protects_its_image(self):
        clean = self.make_build("build-cand", "candidate", image_id=CANDIDATE_IMAGE, drift_result="clean")
        self.assertIn(CANDIDATE_IMAGE, self.resolve(candidate=clean))

    def test_a_drifted_candidate_leaves_an_image_that_another_source_pins_protected(self):
        drifted = self.make_build("build-cand", "candidate", image_id=PREVIOUS_IMAGE, drift_result="TEMPLATE_DRIFT")
        protected = self.resolve(previous_pin=PREVIOUS, candidate=drifted)
        self.assertEqual(protected.image_ids, {PIN_IMAGE, PREVIOUS_IMAGE})

    def test_only_the_candidate_is_released_by_a_drift_mark(self):
        drifted = self.make_build("build-active", "imaging", image_id=ACTIVE_IMAGE, reason_code="TEMPLATE_DRIFT")
        self.assertIn(ACTIVE_IMAGE, self.resolve(active=drifted))

    def test_the_set_is_immutable(self):
        protected = self.resolve()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            protected.image_ids = frozenset()


class ProtectedSetConstructionTests(DeletionCase):
    """Only ``protected_set`` can make a set, so no caller can fake a resolution."""

    def refused(self, build):
        with self.assertRaises(ImageRefusal) as raised:
            build()
        self.assertEqual(raised.exception.reason, UNSAFE)
        return raised.exception

    def test_the_constructor_refuses_a_set_that_protected_set_did_not_resolve(self):
        for name, build in (
            ("an empty set", lambda: ProtectedSet(frozenset())),
            ("a set naming the pinned image", lambda: ProtectedSet(frozenset({PIN_IMAGE}))),
            ("an empty set with a made-up proof", lambda: ProtectedSet(frozenset(), object())),
            ("a set with a made-up proof", lambda: ProtectedSet(frozenset({PIN_IMAGE}), object())),
            ("a set with a proof that is only a name", lambda: ProtectedSet(frozenset({PIN_IMAGE}), "protected_set")),
            ("a plain set", lambda: ProtectedSet({PIN_IMAGE})),
            ("nothing", lambda: ProtectedSet(None)),
        ):
            with self.subTest(name):
                refusal = self.refused(build)
                self.assertNotIn(PIN_IMAGE, refusal.detail)

    def test_the_proof_does_not_make_an_empty_set_valid(self):
        # White box: with the module's own proof, only the rule against an empty set is left to refuse.
        for empty in (frozenset(), set(), (), None):
            with self.subTest(empty=empty):
                self.refused(lambda: ProtectedSet(empty, image_deletion._PROVENANCE))

    def test_a_resolved_set_cannot_be_rebuilt_with_other_images(self):
        resolved = self.resolve(previous_pin=PREVIOUS)
        for name, changes in (
            ("no image", {"image_ids": frozenset()}),
            ("another image", {"image_ids": frozenset({ami(0xFF)})}),
            ("the pinned image dropped", {"image_ids": resolved.image_ids - {PIN_IMAGE}}),
        ):
            with self.subTest(name):
                self.refused(lambda: dataclasses.replace(resolved, **changes))

    def test_a_resolved_set_keeps_its_equality_hash_and_text(self):
        first, second = self.resolve(), self.resolve()
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertEqual(repr(first), f"ProtectedSet(image_ids=frozenset({{{PIN_IMAGE!r}}}))")
        self.assertEqual(self.plan("all", protected=first), [])


class ProtectedSetRefusalTests(DeletionCase):
    def refused(self, **resolve):
        with self.assertRaises(ImageRefusal) as raised:
            self.resolve(**resolve)
        self.assertEqual(raised.exception.reason, UNSAFE)
        return raised.exception

    def test_a_stale_view_of_the_deployment_file_is_unsafe_and_reads_no_version(self):
        # A bind mount of the file itself that a rename elsewhere replaced: no name links the inode any more.
        real = os.fstat
        inode = os.stat(self.deployment_file).st_ino

        def stale(descriptor):
            result = real(descriptor)
            if result.st_ino != inode:
                return result
            values = list(result[:10])
            values[stat.ST_NLINK] = 0
            return os.stat_result(values)

        with mock.patch("os.fstat", stale):
            refusal = self.refused()
        self.assertIn("replaced", refusal.detail)
        self.assertEqual(self.ec2.calls, [])

    def test_a_replacement_during_the_read_is_unsafe_and_reads_no_version(self):
        # The file is replaced by a rename after it was opened and before its bytes are read: the open file is the
        # old inode, which has no name left once the bytes are in. The new file pins another version.
        target = Path(self.deployment_file)
        replacement = target.parent / "replacement.tmp"
        replacement.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        real = os.read
        renamed = []

        def read(descriptor, size):
            if not renamed:
                renamed.append(True)
                os.replace(replacement, target)
            return real(descriptor, size)

        with mock.patch("os.read", read):
            refusal = self.refused()
        self.assertEqual(renamed, [True])
        self.assertIn("replaced", refusal.detail)
        self.assertEqual(self.ec2.calls, [])

    def test_a_replacement_during_the_read_that_a_hard_link_survives_is_unsafe_and_reads_no_version(self):
        # The old file keeps a link, so its link count stays above zero: only the path, which names the new file
        # now, tells the two apart (#202).
        target = Path(self.deployment_file)
        os.link(target, self.directory / "kept.toml")
        replacement = target.parent / "replacement.tmp"
        replacement.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        real = os.read
        renamed = []

        def read(descriptor, size):
            if not renamed:
                renamed.append(True)
                os.replace(replacement, target)
            return real(descriptor, size)

        with mock.patch("os.read", read):
            refusal = self.refused()
        self.assertEqual(renamed, [True])
        self.assertEqual(os.stat(self.directory / "kept.toml").st_nlink, 1)
        self.assertIn("replaced", refusal.detail)
        self.assertEqual(self.ec2.calls, [])

    def test_an_unreadable_version_is_unsafe_and_leaks_nothing(self):
        self.ec2.errors["describe_launch_template_versions"] = ProviderError("RequestLimitExceeded")
        refusal = self.refused()
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, refusal.detail)
            self.assertNotIn(sentinel, str(refusal))
        self.assertIsNone(refusal.__cause__)
        self.assertTrue(refusal.__suppress_context__)

    def test_a_read_that_is_cut_short_is_unsafe(self):
        self.ec2.pages["describe_launch_template_versions"] = True
        with self.assertRaises(ProviderRefusal) as expected:
            self.aws.template_versions(WORKER, [PIN])
        self.assertEqual(expected.exception.reason, "AWS_INVENTORY_INCOMPLETE")
        refusal = self.refused()
        self.assertNotIn("next-page", refusal.detail)

    def test_a_version_the_provider_does_not_return_is_unsafe(self):
        for number in (PIN, PREVIOUS, JOURNAL_OLD):
            with self.subTest(number=number):
                self.reset()
                del self.ec2.versions_store[(WORKER, number)]
                self.refused(previous_pin=PREVIOUS, pending_journal=[self.journal()])

    def test_a_version_returned_twice_is_unsafe(self):
        original = self.ec2.describe_launch_template_versions

        def doubled(**arguments):
            response = original(**arguments)
            response["LaunchTemplateVersions"] += response["LaunchTemplateVersions"]
            return response

        self.ec2.describe_launch_template_versions = doubled
        self.refused()

    def test_a_configuration_that_cannot_be_loaded_is_unsafe_and_calls_nothing(self):
        text = self.deployment_file.read_text(encoding="utf-8")
        for name, write in (
            ("missing", lambda: self.deployment_file.unlink()),
            ("not toml", lambda: self.deployment_file.write_text("this = = is not toml", encoding="utf-8")),
            ("invalid", lambda: self.deployment_file.write_text('deployment = 7\n', encoding="utf-8")),
            ("unknown key", lambda: self.deployment_file.write_text(text + 'surprise = "x"\n', encoding="utf-8")),
        ):
            with self.subTest(name):
                self.reset()
                write()
                self.refused()
                self.assertEqual(self.calls, [])

    def test_a_version_that_names_no_literal_image_is_unsafe(self):
        for name, data in (
            ("an alias", {"ImageId": "resolve:ssm:/example/alias"}),
            ("a bare id", {"ImageId": "0123456789abcdef0"}),
            ("no image", {"InstanceType": "t4g.small"}),
            ("a number", {"ImageId": 7}),
            ("empty", {"ImageId": ""}),
        ):
            with self.subTest(name):
                self.reset()
                self.ec2.versions_store[(WORKER, PIN)]["LaunchTemplateData"] = data
                refusal = self.refused()
                self.assertIn(str(PIN), refusal.detail)

    def test_a_pin_that_is_not_a_version_number_is_unsafe_before_any_call(self):
        for name, arguments in (
            ("zero", {"previous_pin": 0}),
            ("negative", {"previous_pin": -1}),
            ("true", {"previous_pin": True}),
            ("false", {"previous_pin": False}),
            ("text", {"previous_pin": "3"}),
            ("float", {"previous_pin": 3.0}),
            ("journal none", {"pending_journal": [self.journal(old_pin=None)]}),
            ("journal text", {"pending_journal": [self.journal(new_pin="$Latest")]}),
        ):
            with self.subTest(name):
                self.reset()
                self.refused(**arguments)
                self.assertEqual(self.calls, [])

    def test_the_refusal_of_the_deadline_passes_unchanged(self):
        with self.assertRaises(DeadlineRefusal):
            self.resolve(deadline=NOW - 1)
        self.assertEqual(self.mutations(), [])

    def test_a_refusal_names_a_version_at_most(self):
        self.ec2.versions_store[(WORKER, PIN)]["LaunchTemplateData"] = {"ImageId": "resolve:ssm:/example/alias"}
        refusal = self.refused()
        self.assertNotIn("alias", refusal.detail)


class ConfigChangedTests(DeletionCase):
    """``check_unchanged``: the pins the set protects must still be the file's when the plan is acted on (#202)."""

    def refused(self, protected):
        with self.assertRaises(ImageRefusal) as raised:
            image_deletion.check_unchanged(protected)
        self.assertEqual(raised.exception.reason, UNSAFE)
        self.assertNotIn(str(self.directory), raised.exception.detail)
        return raised.exception

    def test_a_file_that_is_as_it_was_read_passes(self):
        protected = self.resolve()
        image_deletion.check_unchanged(protected)
        image_deletion.check_unchanged(protected)

    def test_an_edit_after_the_read_is_unsafe(self):
        protected = self.resolve()
        text = self.deployment_file.read_text(encoding="utf-8")
        self.deployment_file.write_text(text + "\n# a hand edit\n", encoding="utf-8")
        self.refused(protected)

    def test_an_edit_that_keeps_the_size_is_unsafe(self):
        protected = self.resolve()
        size = self.deployment_file.stat().st_size
        data = self.deployment_file.read_bytes()
        self.deployment_file.write_bytes(data.replace(b"example-deployment", b"example-deploymenu", 1))
        os.utime(self.deployment_file, ns=(0, 1))
        self.assertEqual(self.deployment_file.stat().st_size, size)
        self.refused(protected)

    def test_a_replacement_with_the_same_bytes_is_unsafe(self):
        protected = self.resolve()
        replacement = self.deployment_file.parent / "replacement.tmp"
        replacement.write_bytes(self.deployment_file.read_bytes())
        os.replace(replacement, self.deployment_file)
        self.refused(protected)

    def test_an_in_place_edit_during_the_read_is_unsafe(self):
        # The same inode keeps its name, so the read cannot tell: the identity is taken before the bytes are read,
        # and the check after the read sees the file that the edit left, not the bytes that were used.
        real = os.read
        edited = []
        text = self.deployment_file.read_text(encoding="utf-8")

        def read(descriptor, size):
            data = real(descriptor, size)
            if not data and not edited:  # the end of the file: every byte that will be used is in
                edited.append(True)
                self.deployment_file.write_text(text + "\n# a hand edit\n", encoding="utf-8")
            return data

        with mock.patch("os.read", read):
            protected = self.resolve()
        self.assertEqual(edited, [True])
        self.refused(protected)

    def test_a_file_that_is_gone_is_unsafe(self):
        protected = self.resolve()
        self.deployment_file.unlink()
        self.refused(protected)

    def test_a_set_that_no_file_was_read_for_is_unsafe(self):
        self.refused(forged_set({ami(1)}))

    def test_an_edit_between_the_plan_and_the_deletions_leaves_every_image_registered(self):
        # The pipeline resolves the set, lists the account, plans, and checks the file again before it acts.
        self.reset()
        self.made(1, "failed")
        protected = self.resolve()
        plan = self.plan("all", protected=protected)
        self.assertEqual([(item["action"], item["image_id"]) for item in plan], [("deregister", ami(1))])
        self.deployment_file.write_text(
            self.deployment_file.read_text(encoding="utf-8") + "\n# a hand edit\n", encoding="utf-8"
        )
        self.refused(protected)
        self.assertEqual(self.mutations(), [])
        self.assertIn(ami(1), self.stored_images())


class FailClosedTests(DeletionCase):
    """An unreadable pin stops every path, and rerunning after the fix proceeds."""

    def world(self):
        """One image for each path, plus a pinned image that no path may take."""
        self.reset()
        self.made(1, "failed")
        self.made(2, "failed", image_state="pending")
        self.add_snapshot(snap(3), "build-lost")
        self.add_image(ami(3), "build-lost", snapshots=(snap(3),))
        for number, age in ((11, 9), (12, 8), (13, 7), (14, 6)):
            self.made(number, "promoted", age=age * DAY)
        return {
            image_deletion.FAILED: ami(1),
            image_deletion.PENDING_TIMEOUT: ami(2),
            image_deletion.ORPHAN: ami(3),
            image_deletion.RETENTION: ami(11),
        }

    def breakers(self):
        """How an input becomes unreadable: what to do, the resolve arguments while broken and once fixed."""

        def versions_unreadable():
            self.ec2.errors["describe_launch_template_versions"] = ProviderError("RequestLimitExceeded")
            return {}

        def versions_fixed():
            self.ec2.errors.clear()
            return {}

        def pin_missing():
            self.saved = self.ec2.versions_store.pop((WORKER, PIN))
            return {}

        def pin_restored():
            self.ec2.versions_store[(WORKER, PIN)] = self.saved
            return {}

        def toml_broken():
            self.deployment_file.write_text("this = = is not toml", encoding="utf-8")
            return {}

        def toml_fixed():
            self.write_deployment()
            return {}

        def pin_aliased():
            self.saved = self.ec2.versions_store[(WORKER, PIN)]["LaunchTemplateData"]
            self.ec2.versions_store[(WORKER, PIN)]["LaunchTemplateData"] = {"ImageId": "resolve:ssm:/example/alias"}
            return {}

        def alias_fixed():
            self.ec2.versions_store[(WORKER, PIN)]["LaunchTemplateData"] = self.saved
            return {}

        def journal_pin_missing():
            return {"pending_journal": [self.journal(old_pin=99, new_pin=100)]}

        def journal_fixed():
            return {"pending_journal": []}

        return {
            "an unreadable version": (versions_unreadable, versions_fixed),
            "a missing pin": (pin_missing, pin_restored),
            "an unloadable TOML file": (toml_broken, toml_fixed),
            "a pin that names an alias": (pin_aliased, alias_fixed),
            "a journal pin that cannot be read": (journal_pin_missing, journal_fixed),
        }

    def test_every_path_deletes_something_when_the_pins_are_healthy(self):
        for path in image_deletion.PATHS:
            with self.subTest(path):
                targets = self.world()
                report = self.run_paths(path)
                self.assertIn(("deregister", "deregistered"), self.results(report))
                self.assertEqual([row["path"] for row in report if row["action"] == "deregister"], [path])
                self.assertEqual(self.requests("deregister_image"), [{"ImageId": targets[path]}])
                self.assertNotIn(targets[path], self.stored_images())

    def test_an_unreadable_pin_deletes_nothing_on_any_path_and_the_rerun_proceeds(self):
        for kind, (break_it, fix_it) in self.breakers().items():
            for mode in (*image_deletion.PATHS, "all"):
                with self.subTest(kind=kind, mode=mode):
                    targets = self.world()
                    images_before, snapshots_before = self.stored_images(), self.stored_snapshots()
                    with self.assertRaises(ImageRefusal) as raised:
                        self.run_paths(mode, **break_it())
                    self.assertEqual(raised.exception.reason, UNSAFE)
                    self.assertEqual(self.mutations(), [])
                    self.assertEqual(self.stored_images(), images_before)
                    self.assertEqual(self.stored_snapshots(), snapshots_before)
                    # The same call, once the input is readable again, proceeds with the same rules.
                    report = self.run_paths(mode, **fix_it())
                    taken = {row["image_id"] for row in report if row["result"] == "deregistered"}
                    wanted = set(targets.values()) if mode == "all" else {targets[mode]}
                    self.assertEqual(taken, wanted)

    def test_a_plan_cannot_be_made_without_a_resolved_set(self):
        self.world()
        for name, protected in (
            ("none", None),
            ("an empty frozenset", frozenset()),
            ("an empty set", set()),
            ("a list of ids", [PIN_IMAGE]),
            ("a mapping", {PIN_IMAGE: True}),
            ("a string", PIN_IMAGE),
            # Past the constructor's own refusal: only plan_deletions can still stop these.
            ("an empty set that skipped the constructor", forged_set(())),
            ("a set naming the pin that skipped the constructor", forged_set({PIN_IMAGE})),
            ("a subclass whose constructor checks nothing", UncheckedSet(frozenset({PIN_IMAGE}))),
        ):
            with self.subTest(name):
                with self.assertRaises(ImageRefusal) as raised:
                    plan_deletions(
                        self.images(),
                        self.snapshots(),
                        protected,
                        deployment=DEPLOYMENT,
                        mode="all",
                        now=NOW,
                        config=self.config,
                        builds=self.rows,
                    )
                self.assertEqual(raised.exception.reason, UNSAFE)
        self.assertEqual(self.mutations(), [])

    def test_a_set_built_by_hand_cannot_reach_a_plan(self):
        """The set is built inside the call, so the constructor's refusal and the plan's both count."""
        self.world()
        resolved = self.resolve()
        for name, build in (
            ("ProtectedSet(frozenset())", lambda: ProtectedSet(frozenset())),
            ("a hand-built set naming the pin", lambda: ProtectedSet(frozenset({PIN_IMAGE}))),
            ("a hand-built set naming other images", lambda: ProtectedSet(frozenset({ami(0xFE), ami(0xFF)}))),
            ("a resolved set rebuilt without images", lambda: dataclasses.replace(resolved, image_ids=frozenset())),
            ("a resolved set rebuilt without the pin", lambda: dataclasses.replace(resolved, image_ids=frozenset({ami(0xFF)}))),
        ):
            with self.subTest(name):
                with self.assertRaises(ImageRefusal) as raised:
                    plan_deletions(
                        self.images(),
                        self.snapshots(),
                        build(),
                        deployment=DEPLOYMENT,
                        mode="all",
                        now=NOW,
                        config=self.config,
                        builds=self.rows,
                    )
                self.assertEqual(raised.exception.reason, UNSAFE)
        self.assertEqual(self.mutations(), [])

    def test_a_pinned_image_is_never_planned_for_deregistration_through_a_made_up_set(self):
        # The harm the guard exists for: an old, tagged image that only its pin protects.
        self.world()
        self.add_image(PIN_IMAGE, "build-lost", age=30 * DAY)
        self.assertEqual(self.summary(self.plan("orphan")), [("orphan", "deregister", ami(3), None), ("orphan", "skip", PIN_IMAGE, "protected")])
        for forged in (forged_set(()), forged_set({ami(0xFF)}), UncheckedSet(frozenset({ami(0xFF)}))):
            with self.subTest(forged=type(forged).__name__, ids=sorted(forged.image_ids)):
                with self.assertRaises(ImageRefusal):
                    self.plan("orphan", protected=forged)
        self.assertEqual(self.mutations(), [])


class ProtectedImageTests(DeletionCase):
    def test_every_source_stops_every_path_from_taking_an_old_unknown_image(self):
        for source, image_id in SOURCES.items():
            with self.subTest(source):
                self.reset()
                arguments = self.protection(source)
                self.add_image(image_id, "build-lost", age=30 * DAY)
                plan = self.plan("all", **arguments)
                self.assertEqual(self.summary(plan), [("orphan", "skip", image_id, "protected")])
                report = apply_deletions(self.aws, plan, apply=True)
                self.assertEqual(self.results(report), [("skip", "skipped")])
                self.assertEqual(self.mutations(), [])
                self.assertIn(image_id, self.stored_images())

    def test_without_the_source_the_same_image_is_planned(self):
        for source, image_id in SOURCES.items():
            with self.subTest(source):
                self.reset()
                self.add_image(image_id, "build-lost", age=30 * DAY)
                if source == "pin":
                    # The pin is always read, so this control moves the image id the pin names.
                    self.set_version(PIN, ami(0xFF))
                self.assertEqual(self.summary(self.plan("all")), [("orphan", "deregister", image_id, None)])

    def test_every_source_stops_the_cleanup_of_a_failed_builds_image(self):
        for source, image_id in SOURCES.items():
            with self.subTest(source):
                self.reset()
                arguments = self.protection(source)
                made = self.made(1, "failed", image_id=image_id)
                plan = self.plan("all", **arguments)
                self.assertEqual(self.summary(plan), [("failed", "skip", image_id, "protected")])
                (item,) = plan
                self.assertEqual(item["snapshot_ids"], [made.snapshot_id])
                apply_deletions(self.aws, plan, apply=True)
                self.assertEqual(self.mutations(), [])
                self.assertIn(made.snapshot_id, self.stored_snapshots())

    def test_a_failed_builds_image_that_an_operator_pinned_by_hand_is_reported_protected(self):
        made = self.made(1, "failed", image_id=PIN_IMAGE)
        other = self.made(2, "failed")
        report = self.run_paths("failed")
        # Rows come in image id order: the unprotected image sorts before the pinned one.
        self.assertEqual(
            report,
            [
                {"path": "failed", "image_id": other.image_id, "action": "deregister", "result": "deregistered"},
                {
                    "path": "failed",
                    "image_id": other.image_id,
                    "action": "delete_snapshot",
                    "result": "snapshot_deleted",
                    "snapshot_id": other.snapshot_id,
                },
                {"path": "failed", "image_id": PIN_IMAGE, "action": "skip", "result": "skipped", "reason": "protected"},
            ],
        )
        self.assertIn(PIN_IMAGE, self.stored_images())
        self.assertIn(made.snapshot_id, self.stored_snapshots())
        self.assertEqual(self.requests("deregister_image"), [{"ImageId": other.image_id}])

    def test_protection_comes_before_the_age_wait(self):
        self.made(1, "failed", reason_code="TEMPLATE_DRIFT", age=HOUR, image_id=PIN_IMAGE)
        self.assertEqual(self.summary(self.plan("all")), [("failed", "skip", PIN_IMAGE, "protected")])

    def test_a_protected_image_is_never_taken_by_retention(self):
        protected_images = {11: PREVIOUS_IMAGE, 12: PIN_IMAGE}
        for number, age in ((11, 9), (12, 8), (13, 7), (14, 6), (15, 5)):
            self.made(number, "promoted", age=age * DAY, image_id=protected_images.get(number))
        plan = self.plan("retention", previous_pin=PREVIOUS)
        self.assertEqual([item["image_id"] for item in plan], [ami(13), ami(14)])
        self.assertTrue(all(item["action"] == "deregister" for item in plan))

    def test_only_the_protected_image_of_a_pair_is_kept(self):
        self.made(1, "failed", image_id=PREVIOUS_IMAGE)
        self.made(2, "failed", image_id=ami(2))
        plan = self.plan("failed", previous_pin=PREVIOUS)
        self.assertEqual(
            self.summary(plan), [("failed", "deregister", ami(2), None), ("failed", "skip", PREVIOUS_IMAGE, "protected")]
        )


class PromotedPinTests(DeletionCase):
    """The new pin of the newest completed journal row protects its image, whatever file a pass reads.

    A container whose bind mount of the deployment file keeps the old inode reads the file as it was before
    ``promote --write`` replaced it. That file still pins A, so only the journal names B, the image that was just
    promoted from A. These tests use the file of the account (``PIN`` is A) and ``JOURNAL_NEW`` as B.
    """

    def refused(self, **resolve):
        with self.assertRaises(ImageRefusal) as raised:
            self.resolve(**resolve)
        self.assertEqual(raised.exception.reason, UNSAFE)
        return raised.exception

    def usable_images_with_b_the_oldest(self):
        """Four usable images against a retention of three, and B is the oldest, so retention takes B first."""
        self.made(11, "promoted", age=9 * DAY, image_id=JOURNAL_NEW_IMAGE)
        for number, age in ((12, 8), (13, 7), (14, 6)):
            self.made(number, "candidate", age=age * DAY)

    def test_the_image_of_the_promoted_pin_is_protected_when_the_file_still_pins_the_old_one(self):
        protected = self.resolve(previous_pin=PIN, promoted_pin=JOURNAL_NEW)
        self.assertEqual(protected.image_ids, {PIN_IMAGE, JOURNAL_NEW_IMAGE})
        self.assertIn(JOURNAL_NEW_IMAGE, protected)

    def test_a_stale_file_alone_does_not_protect_the_promoted_image(self):
        # The control: without the promoted pin nothing in this set names B.
        self.assertNotIn(JOURNAL_NEW_IMAGE, self.resolve(previous_pin=PIN))

    def test_the_promoted_pin_protects_nothing_else_by_itself(self):
        self.assertEqual(self.resolve(promoted_pin=JOURNAL_NEW).image_ids, {PIN_IMAGE, JOURNAL_NEW_IMAGE})

    def test_no_promoted_pin_is_the_same_as_before(self):
        # ``None`` is the state before any promotion, and it is the default of the argument.
        self.assertEqual(self.resolve(previous_pin=PREVIOUS, promoted_pin=None).image_ids, {PIN_IMAGE, PREVIOUS_IMAGE})
        self.assertEqual(self.resolve(previous_pin=PREVIOUS).image_ids, {PIN_IMAGE, PREVIOUS_IMAGE})

    def test_in_the_normal_case_the_promoted_pin_adds_nothing(self):
        # After a promotion the file pins the new version too, so the journal names an image the file names already.
        protected = self.resolve(previous_pin=PREVIOUS, promoted_pin=PIN)
        self.assertEqual(protected.image_ids, {PIN_IMAGE, PREVIOUS_IMAGE})

    def test_the_promoted_pin_is_read_with_the_other_versions_in_one_call(self):
        self.resolve(previous_pin=PREVIOUS, promoted_pin=JOURNAL_NEW)
        (read,) = self.requests("describe_launch_template_versions")
        self.assertEqual(read["LaunchTemplateName"], WORKER)
        self.assertEqual(read["Versions"], [str(PREVIOUS), str(PIN), str(JOURNAL_NEW)])
        self.assertEqual(self.names(), ["describe_launch_template_versions"])

    def test_a_promoted_pin_that_another_source_names_is_read_once(self):
        self.resolve(previous_pin=PREVIOUS, promoted_pin=PIN)
        (read,) = self.requests("describe_launch_template_versions")
        self.assertEqual(read["Versions"], [str(PREVIOUS), str(PIN)])

    def test_every_path_leaves_the_image_of_the_promoted_pin_alone(self):
        self.add_image(JOURNAL_NEW_IMAGE, "build-lost", age=30 * DAY)
        plan = self.plan("all", previous_pin=PIN, promoted_pin=JOURNAL_NEW)
        self.assertEqual(self.summary(plan), [("orphan", "skip", JOURNAL_NEW_IMAGE, "protected")])
        report = apply_deletions(self.aws, plan, apply=True)
        self.assertEqual(self.results(report), [("skip", "skipped")])
        self.assertEqual(self.mutations(), [])
        self.assertIn(JOURNAL_NEW_IMAGE, self.stored_images())

    def test_without_the_promoted_pin_the_same_image_is_planned(self):
        self.add_image(JOURNAL_NEW_IMAGE, "build-lost", age=30 * DAY)
        plan = self.plan("all", previous_pin=PIN)
        self.assertEqual(self.summary(plan), [("orphan", "deregister", JOURNAL_NEW_IMAGE, None)])

    def test_retention_takes_the_next_oldest_image_and_not_the_promoted_one(self):
        self.usable_images_with_b_the_oldest()
        plan = self.plan("retention", previous_pin=PIN, promoted_pin=JOURNAL_NEW)
        self.assertEqual([item["image_id"] for item in plan], [ami(12)])
        self.run_paths("retention", previous_pin=PIN, promoted_pin=JOURNAL_NEW)
        self.assertEqual(self.requests("deregister_image"), [{"ImageId": ami(12)}])
        self.assertIn(JOURNAL_NEW_IMAGE, self.stored_images())

    def test_without_the_promoted_pin_retention_takes_the_promoted_image_first(self):
        # The control that shows the hazard: only the promoted pin separates the two plans.
        self.usable_images_with_b_the_oldest()
        plan = self.plan("retention", previous_pin=PIN)
        self.assertEqual([item["image_id"] for item in plan], [JOURNAL_NEW_IMAGE])

    def test_a_pin_that_is_not_a_version_number_is_unsafe_before_any_call(self):
        for name, value in (
            ("zero", 0),
            ("negative", -1),
            ("true", True),
            ("false", False),
            ("text", "3"),
            ("float", 3.0),
        ):
            with self.subTest(name):
                self.reset()
                self.refused(promoted_pin=value)
                self.assertEqual(self.calls, [])

    def test_a_promoted_version_that_the_provider_does_not_return_is_unsafe(self):
        del self.ec2.versions_store[(WORKER, JOURNAL_NEW)]
        self.refused(previous_pin=PIN, promoted_pin=JOURNAL_NEW)

    def test_a_promoted_version_that_names_no_literal_image_is_unsafe(self):
        self.ec2.versions_store[(WORKER, JOURNAL_NEW)]["LaunchTemplateData"] = {"ImageId": "resolve:ssm:/example/alias"}
        refusal = self.refused(previous_pin=PIN, promoted_pin=JOURNAL_NEW)
        self.assertIn(str(JOURNAL_NEW), refusal.detail)
        self.assertNotIn("alias", refusal.detail)

    def test_a_promoted_version_that_cannot_be_read_stops_every_path_and_deletes_nothing(self):
        # Only the promoted version is missing: the file's pin and the previous pin read fine.
        self.usable_images_with_b_the_oldest()
        del self.ec2.versions_store[(WORKER, JOURNAL_NEW)]
        for mode in (*image_deletion.PATHS, "all"):
            with self.subTest(mode=mode):
                with self.assertRaises(ImageRefusal) as raised:
                    self.run_paths(mode, previous_pin=PIN, promoted_pin=JOURNAL_NEW)
                self.assertEqual(raised.exception.reason, UNSAFE)
        self.assertEqual(self.mutations(), [])


class LatestPinsTests(DeletionCase):
    """Both pins of the newest journal row of any status protect their images.

    A promotion can replace the deployment file and stop before it completes its row. A pass that reads the old file
    resolves that row as aborted, so no pending row and no completed row names the pin that the new file holds. These
    tests use ``JOURNAL_OLD`` and ``JOURNAL_NEW`` as the two pins of that row, and the file of the account (``PIN``)
    as the file that the pass reads.
    """

    LATEST = (JOURNAL_OLD, JOURNAL_NEW)

    def refused(self, **resolve):
        with self.assertRaises(ImageRefusal) as raised:
            self.resolve(**resolve)
        self.assertEqual(raised.exception.reason, UNSAFE)
        return raised.exception

    def usable_images_with_the_new_pin_oldest(self):
        """Four usable images against a retention of three, and the new pin's image is the oldest."""
        self.made(11, "candidate", age=9 * DAY, image_id=JOURNAL_NEW_IMAGE)
        for number, age in ((12, 8), (13, 7), (14, 6)):
            self.made(number, "candidate", age=age * DAY)

    def test_both_pins_of_the_latest_row_protect_their_images(self):
        protected = self.resolve(latest_pins=self.LATEST)
        self.assertEqual(protected.image_ids, {PIN_IMAGE, JOURNAL_OLD_IMAGE, JOURNAL_NEW_IMAGE})
        self.assertIn(JOURNAL_OLD_IMAGE, protected)
        self.assertIn(JOURNAL_NEW_IMAGE, protected)

    def test_without_the_latest_pins_the_same_set_names_neither_image(self):
        # The control: nothing but the pins separates this set from the one above.
        protected = self.resolve()
        self.assertNotIn(JOURNAL_OLD_IMAGE, protected)
        self.assertNotIn(JOURNAL_NEW_IMAGE, protected)

    def test_no_latest_pins_is_the_same_as_before(self):
        # An empty tuple is the state before any promotion, and it is the default of the argument.
        self.assertEqual(self.resolve(previous_pin=PREVIOUS, latest_pins=()).image_ids, {PIN_IMAGE, PREVIOUS_IMAGE})
        self.assertEqual(self.resolve(previous_pin=PREVIOUS).image_ids, {PIN_IMAGE, PREVIOUS_IMAGE})

    def test_the_latest_pins_are_read_with_the_other_versions_in_one_call(self):
        self.resolve(previous_pin=PREVIOUS, latest_pins=self.LATEST)
        (read,) = self.requests("describe_launch_template_versions")
        self.assertEqual(read["LaunchTemplateName"], WORKER)
        self.assertEqual(read["Versions"], [str(PREVIOUS), str(PIN), str(JOURNAL_OLD), str(JOURNAL_NEW)])
        self.assertEqual(self.names(), ["describe_launch_template_versions"])

    def test_every_path_leaves_the_images_of_the_latest_pins_alone(self):
        self.add_image(JOURNAL_OLD_IMAGE, "build-lost", age=30 * DAY)
        self.add_image(JOURNAL_NEW_IMAGE, "build-lost", age=30 * DAY)
        plan = self.plan("all", latest_pins=self.LATEST)
        self.assertEqual(
            self.summary(plan),
            [
                ("orphan", "skip", JOURNAL_OLD_IMAGE, "protected"),
                ("orphan", "skip", JOURNAL_NEW_IMAGE, "protected"),
            ],
        )
        report = apply_deletions(self.aws, plan, apply=True)
        self.assertEqual(self.results(report), [("skip", "skipped"), ("skip", "skipped")])
        self.assertEqual(self.mutations(), [])
        self.assertLessEqual({JOURNAL_OLD_IMAGE, JOURNAL_NEW_IMAGE}, self.stored_images())

    def test_without_the_latest_pins_the_same_images_are_planned(self):
        self.add_image(JOURNAL_OLD_IMAGE, "build-lost", age=30 * DAY)
        self.add_image(JOURNAL_NEW_IMAGE, "build-lost", age=30 * DAY)
        plan = self.plan("all")
        self.assertEqual(
            self.summary(plan),
            [
                ("orphan", "deregister", JOURNAL_OLD_IMAGE, None),
                ("orphan", "deregister", JOURNAL_NEW_IMAGE, None),
            ],
        )

    def test_retention_takes_the_next_oldest_image_and_not_the_new_pin_of_the_latest_row(self):
        self.usable_images_with_the_new_pin_oldest()
        plan = self.plan("retention", latest_pins=self.LATEST)
        self.assertEqual([item["image_id"] for item in plan], [ami(12)])
        self.run_paths("retention", latest_pins=self.LATEST)
        self.assertEqual(self.requests("deregister_image"), [{"ImageId": ami(12)}])
        self.assertIn(JOURNAL_NEW_IMAGE, self.stored_images())

    def test_without_the_latest_pins_retention_takes_the_new_pin_image_first(self):
        # The control that shows the hazard: only the latest pins separate the two plans.
        self.usable_images_with_the_new_pin_oldest()
        plan = self.plan("retention")
        self.assertEqual([item["image_id"] for item in plan], [JOURNAL_NEW_IMAGE])

    def test_a_pin_that_is_not_a_version_number_is_unsafe_before_any_call(self):
        for name, value in (
            ("zero", 0),
            ("negative", -1),
            ("true", True),
            ("false", False),
            ("text", "3"),
            ("float", 3.0),
        ):
            for position, pins in (("old", (value, JOURNAL_NEW)), ("new", (JOURNAL_OLD, value))):
                with self.subTest(name, position=position):
                    self.reset()
                    self.refused(latest_pins=pins)
                    self.assertEqual(self.calls, [])

    def test_a_latest_version_that_the_provider_does_not_return_is_unsafe(self):
        for version in self.LATEST:
            with self.subTest(version=version):
                self.reset()
                del self.ec2.versions_store[(WORKER, version)]
                self.refused(latest_pins=self.LATEST)

    def test_a_latest_version_that_names_no_literal_image_is_unsafe(self):
        self.ec2.versions_store[(WORKER, JOURNAL_NEW)]["LaunchTemplateData"] = {"ImageId": "resolve:ssm:/example/alias"}
        refusal = self.refused(latest_pins=self.LATEST)
        self.assertIn(str(JOURNAL_NEW), refusal.detail)
        self.assertNotIn("alias", refusal.detail)

    def test_a_latest_version_that_cannot_be_read_stops_every_path_and_deletes_nothing(self):
        self.usable_images_with_the_new_pin_oldest()
        del self.ec2.versions_store[(WORKER, JOURNAL_NEW)]
        for mode in (*image_deletion.PATHS, "all"):
            with self.subTest(mode=mode):
                with self.assertRaises(ImageRefusal) as raised:
                    self.run_paths(mode, latest_pins=self.LATEST)
                self.assertEqual(raised.exception.reason, UNSAFE)
        self.assertEqual(self.mutations(), [])


class ActiveBuildTests(DeletionCase):
    def test_no_path_takes_the_image_of_an_active_build(self):
        for state, image_state in (("building", "available"), ("imaging", "available"), ("imaging", "pending")):
            with self.subTest(state=state, image=image_state):
                self.reset()
                self.made(1, state, age=30 * DAY, image_state=image_state)
                self.assertEqual(self.plan("all"), [])

    def test_an_active_build_is_untouched_without_its_image_being_recorded(self):
        self.made(1, "imaging", age=30 * DAY, image_state="pending", image_id=ami(1))
        self.assertIsNone(self.rows[0].image_id)
        self.assertEqual(self.plan("all"), [])

    def test_an_active_builds_snapshots_are_untouched_too(self):
        self.build("build-0001", "imaging")
        self.add_snapshot(snap(1), "build-0001", age=30 * DAY)
        self.assertEqual(self.plan("all"), [])

    def test_an_active_build_that_is_past_its_deadline_is_untouched_until_it_is_failed(self):
        self.made(1, "imaging", age=30 * DAY, image_state="pending", deadline_at=NOW - DAY)
        self.assertEqual(self.plan("all"), [])
        self.rows[:] = [dataclasses.replace(self.rows[0], state="failed", reason_code="IMAGE_PENDING_TIMEOUT")]
        self.assertEqual(self.summary(self.plan("all")), [("pending_timeout", "deregister", ami(1), None)])

    def test_a_drifted_active_build_is_untouched(self):
        self.made(1, "building", age=30 * DAY, reason_code="TEMPLATE_DRIFT")
        self.assertEqual(self.plan("all"), [])

    def test_an_active_build_does_not_count_toward_retention(self):
        for number, age in ((11, 9), (12, 8), (13, 7)):
            self.made(number, "promoted", age=age * DAY)
        self.made(14, "imaging", age=20 * DAY)
        self.assertEqual(self.plan("retention"), [])


class FailedPathTests(DeletionCase):
    def test_a_failed_build_is_taken_with_its_snapshot_and_without_waiting(self):
        made = self.made(1, "failed", age=10 * 60)
        self.assertEqual(
            self.plan("all"),
            [
                {
                    "path": "failed",
                    "image_id": made.image_id,
                    "build_id": made.build_id,
                    "action": "deregister",
                    "reason": None,
                    "snapshot_ids": [made.snapshot_id],
                    "skipped_snapshots": [],
                }
            ],
        )

    def test_every_image_state_but_pending_is_the_failed_path(self):
        for state in ("available", "failed", "error", "transient", "invalid", "disabled", "unknown"):
            with self.subTest(state):
                self.reset()
                self.made(1, "failed", image_state=state)
                self.assertEqual(self.summary(self.plan("all")), [("failed", "deregister", ami(1), None)])

    def test_a_pending_image_of_a_failed_build_is_the_pending_timeout_path(self):
        self.made(1, "failed", image_state="pending", age=10 * 60)
        self.assertEqual(self.summary(self.plan("all")), [("pending_timeout", "deregister", ami(1), None)])
        self.assertEqual(self.plan("failed"), [])
        self.assertEqual(self.summary(self.plan("pending_timeout")), [("pending_timeout", "deregister", ami(1), None)])

    def test_a_pending_image_of_a_failed_build_waits_for_nothing(self):
        self.made(1, "failed", image_state="pending", age=5)
        report = self.run_paths("pending_timeout")
        self.assertEqual(self.results(report), [("deregister", "deregistered"), ("delete_snapshot", "snapshot_deleted")])

    def test_a_usable_build_is_not_a_failed_build(self):
        for state in ("candidate", "promoted"):
            with self.subTest(state):
                self.reset()
                self.made(1, state)
                self.assertEqual(self.plan("failed"), [])
                self.assertEqual(self.plan("pending_timeout"), [])

    def test_a_template_drift_build_is_held_for_24_hours_by_the_age_of_its_image(self):
        # The drift mark is a property of the row: every state but active holds the image 24 hours.
        for row in ({"reason_code": "TEMPLATE_DRIFT"}, {"drift_result": "TEMPLATE_DRIFT"}):
            for state in ("failed", "candidate", "promoted"):
                for age, expected in (
                    (HOUR, ("failed", "skip", ami(1), "too_young")),
                    (DAY - 1, ("failed", "skip", ami(1), "too_young")),
                    (DAY, ("failed", "skip", ami(1), "too_young")),
                    (DAY + 1, ("failed", "deregister", ami(1), None)),
                    (30 * DAY, ("failed", "deregister", ami(1), None)),
                ):
                    with self.subTest(row=row, state=state, age=age):
                        self.reset()
                        self.made(1, state, age=age, **row)
                        self.assertEqual(self.summary(self.plan("all")), [expected])

    def test_a_held_drift_image_is_not_touched_and_is_taken_once_old_enough(self):
        made = self.made(1, "failed", age=23 * HOUR, reason_code="TEMPLATE_DRIFT")
        report = self.run_paths("all")
        self.assertEqual(report, [{"path": "failed", "image_id": made.image_id, "action": "skip", "result": "skipped", "reason": "too_young"}])
        self.assertEqual(self.mutations(), [])
        report = apply_deletions(self.aws, self.plan("all", now=NOW + 2 * HOUR), apply=True)
        self.assertEqual(self.results(report), [("deregister", "deregistered"), ("delete_snapshot", "snapshot_deleted")])
        self.assertEqual(self.stored_images(), set())

    def test_a_drift_image_with_no_creation_time_is_kept(self):
        self.made(1, "failed", reason_code="TEMPLATE_DRIFT")
        self.ec2.images_store[0].pop("CreationDate")
        self.assertEqual(self.summary(self.plan("all")), [("failed", "skip", ami(1), "age_unknown")])

    def test_a_plain_failed_build_does_not_need_a_creation_time(self):
        self.made(1, "failed")
        self.ec2.images_store[0].pop("CreationDate")
        self.assertEqual(self.summary(self.plan("all")), [("failed", "deregister", ami(1), None)])

    def test_a_drift_build_that_is_pending_still_waits(self):
        self.made(1, "failed", image_state="pending", age=HOUR, reason_code="TEMPLATE_DRIFT")
        self.assertEqual(self.summary(self.plan("all")), [("failed", "skip", ami(1), "too_young")])

    def test_a_drift_build_is_not_counted_toward_retention(self):
        for number, age in ((11, 9), (12, 8), (13, 7)):
            self.made(number, "promoted", age=age * DAY)
        self.made(14, "candidate", age=20 * DAY, drift_result="TEMPLATE_DRIFT")
        self.assertEqual(self.plan("retention"), [])
        self.assertEqual(self.summary(self.plan("all")), [("failed", "deregister", ami(14), None)])

    def current_candidate(self, made):
        """The stored row of ``made`` as the state file reports the current candidate: with its image recorded."""
        (row,) = self.rows
        return dataclasses.replace(row, image_id=made.image_id)

    def test_the_current_candidate_that_recorded_drift_is_cleaned_up_after_24_hours(self):
        # The state file reports a drifted build as the current candidate, because its state stays ``candidate``.
        for row in DRIFT_ROWS:
            for age, expected in (
                (HOUR, ("failed", "skip", ami(1), "too_young")),
                (DAY, ("failed", "skip", ami(1), "too_young")),
                (DAY + 1, ("failed", "deregister", ami(1), None)),
                (30 * DAY, ("failed", "deregister", ami(1), None)),
            ):
                with self.subTest(row=row, age=age):
                    self.reset()
                    made = self.made(1, "candidate", age=age, **row)
                    current = self.current_candidate(made)
                    self.assertEqual(current.image_id, made.image_id)
                    self.assertNotIn(made.image_id, self.resolve(candidate=current))
                    self.assertEqual(self.summary(self.plan("all", candidate=current)), [expected])

    def test_the_drifted_current_candidate_is_removed_with_its_snapshot_once_it_is_old_enough(self):
        made = self.made(1, "candidate", age=DAY + 1, drift_result="TEMPLATE_DRIFT")
        current = self.current_candidate(made)
        report = apply_deletions(self.aws, self.plan("all", candidate=current), apply=True)
        self.assertEqual(self.results(report), [("deregister", "deregistered"), ("delete_snapshot", "snapshot_deleted")])
        self.assertEqual((self.stored_images(), self.stored_snapshots()), (set(), set()))

    def test_the_drifted_current_candidate_is_untouched_while_it_is_held(self):
        made = self.made(1, "candidate", age=23 * HOUR, reason_code="TEMPLATE_DRIFT")
        current = self.current_candidate(made)
        report = self.run_paths("all", candidate=current)
        self.assertEqual(report, [{"path": "failed", "image_id": made.image_id, "action": "skip", "result": "skipped", "reason": "too_young"}])
        self.assertEqual(self.mutations(), [])

    def test_the_current_candidate_without_drift_is_never_cleaned_up(self):
        made = self.made(1, "candidate", age=30 * DAY)
        current = self.current_candidate(made)
        self.assertEqual(self.plan("all", candidate=current), [])
        self.assertIn(made.image_id, self.resolve(candidate=current))


class OrphanPathTests(DeletionCase):
    def test_a_tagged_image_with_no_row_is_taken_after_24_hours(self):
        for age, expected in (
            (HOUR, ("orphan", "skip", ami(1), "too_young")),
            (DAY, ("orphan", "skip", ami(1), "too_young")),
            (DAY + 1, ("orphan", "deregister", ami(1), None)),
            (None, ("orphan", "skip", ami(1), "age_unknown")),
        ):
            with self.subTest(age=age):
                self.reset()
                self.add_snapshot(snap(1), "build-lost")
                self.add_image(ami(1), "build-lost", age=age, snapshots=(snap(1),))
                self.assertEqual(self.summary(self.plan("all")), [expected])

    def test_an_orphan_takes_its_snapshots_with_it(self):
        self.add_snapshot(snap(1), "build-lost")
        self.add_snapshot(snap(2), "build-lost")
        self.add_image(ami(1), "build-lost", snapshots=(snap(1), snap(2)))
        report = self.run_paths("orphan")
        self.assertEqual(
            self.results(report), [("deregister", "deregistered")] + [("delete_snapshot", "snapshot_deleted")] * 2
        )
        self.assertEqual((self.stored_images(), self.stored_snapshots()), (set(), set()))

    def test_an_orphan_snapshot_with_no_image_is_taken_after_24_hours(self):
        for age, expected in (
            (HOUR, ("orphan", "skip", None, "too_young")),
            (DAY + 1, ("orphan", "delete_snapshot", None, None)),
            (None, ("orphan", "skip", None, "age_unknown")),
        ):
            with self.subTest(age=age):
                self.reset()
                self.add_snapshot(snap(1), "build-lost", age=age)
                plan = self.plan("all")
                self.assertEqual(self.summary(plan), [expected])
                self.assertEqual(plan[0]["snapshot_ids"], [snap(1)])

    def test_an_orphan_snapshot_is_deleted_without_a_deregistration(self):
        self.add_snapshot(snap(1), "build-lost", age=3 * DAY)
        report = self.run_paths("orphan")
        self.assertEqual(self.names().count("deregister_image"), 0)
        self.assertEqual(report, [{"path": "orphan", "image_id": None, "action": "delete_snapshot", "result": "snapshot_deleted", "snapshot_id": snap(1)}])
        self.assertEqual(self.stored_snapshots(), set())

    def test_a_row_of_another_deployment_does_not_shield_an_image(self):
        self.add_image(ami(1), "build-0001", age=3 * DAY)
        self.build("build-0001", "promoted", deployment=OTHER_DEPLOYMENT)
        self.assertEqual(self.summary(self.plan("all")), [("orphan", "deregister", ami(1), None)])

    def test_a_row_of_this_deployment_makes_an_image_not_an_orphan(self):
        self.add_image(ami(1), "build-0001", age=3 * DAY)
        self.build("build-0001", "promoted")
        self.assertEqual(self.plan("orphan"), [])


class SnapshotOnlyTests(DeletionCase):
    def test_the_snapshot_of_a_failed_build_with_no_image_is_taken(self):
        self.build("build-0001", "failed")
        self.add_snapshot(snap(1), "build-0001", age=HOUR)
        self.assertEqual(self.summary(self.plan("all")), [("failed", "delete_snapshot", None, None)])

    def test_the_snapshot_of_a_drifted_build_with_no_image_waits_by_its_own_age(self):
        self.build("build-0001", "failed", reason_code="TEMPLATE_DRIFT")
        self.add_snapshot(snap(1), "build-0001", age=HOUR)
        self.assertEqual(self.summary(self.plan("all")), [("failed", "skip", None, "too_young")])
        self.reset()
        self.build("build-0001", "failed", reason_code="TEMPLATE_DRIFT")
        self.add_snapshot(snap(1), "build-0001", age=DAY + 1)
        self.assertEqual(self.summary(self.plan("all")), [("failed", "delete_snapshot", None, None)])

    def test_the_snapshot_of_a_usable_or_active_build_with_no_image_is_left_alone(self):
        for number, state in enumerate(("candidate", "promoted", "building", "imaging"), start=1):
            self.build(f"build-{number:04d}", state)
            self.add_snapshot(snap(number), f"build-{number:04d}", age=30 * DAY)
        self.assertEqual(self.plan("all"), [])

    def test_a_snapshot_that_a_listed_image_references_is_not_alone(self):
        self.build("build-0001", "failed")
        self.add_snapshot(snap(1), "build-0001")
        foreign = hand_image(ami(9), {"example": "x"}, snapshot_ids=(snap(1),))
        self.assertEqual(self.plan("all", images=[foreign]), [])

    def test_a_snapshot_of_a_protected_image_is_left_with_it(self):
        made = self.made(1, "failed", image_id=PIN_IMAGE)
        plan = self.plan("all")
        self.assertEqual(self.summary(plan), [("failed", "skip", PIN_IMAGE, "protected")])
        self.assertEqual(plan[0]["snapshot_ids"], [made.snapshot_id])

    def test_a_snapshot_of_a_deregistered_image_is_treated_as_alone(self):
        made = self.made(1, "failed", image_state="deregistered")
        plan = self.plan("all")
        self.assertEqual(self.summary(plan), [("failed", "delete_snapshot", None, None)])
        self.assertEqual(plan[0]["snapshot_ids"], [made.snapshot_id])
        report = apply_deletions(self.aws, plan, apply=True)
        self.assertEqual(self.names().count("deregister_image"), 0)
        self.assertEqual(self.results(report), [("delete_snapshot", "snapshot_deleted")])

    def test_a_snapshot_with_no_creation_time_is_taken_when_its_build_failed_without_a_wait(self):
        self.build("build-0001", "failed")
        self.add_snapshot(snap(1), "build-0001", age=None)
        self.assertEqual(self.summary(self.plan("all")), [("failed", "delete_snapshot", None, None)])


def hand_image(image_id, tags, *, state="available", snapshot_ids=(), created_at=NOW - 3 * DAY):
    return Image(image_id=image_id, state=state, tags=tags, snapshot_ids=tuple(snapshot_ids), created_at=created_at)


def hand_snapshot(snapshot_id, tags, created_at=NOW - 3 * DAY):
    return Snapshot(snapshot_id=snapshot_id, state="completed", tags=tags, created_at=created_at)


class OwnershipTests(DeletionCase):
    """The rule checks the tags itself, so a listing that is too wide still deletes nothing foreign."""

    FOREIGN = {
        "another deployment": owner_tags("build-0001", OTHER_DEPLOYMENT),
        "no build id": owner_tags(None),
        "an empty build id": owner_tags(""),
        "the marker false": {**owner_tags("build-0001"), IMAGE_BUILD_TAG: "false"},
        "the marker in another case": {**owner_tags("build-0001"), IMAGE_BUILD_TAG: "True"},
        "no marker": {DEPLOYMENT_TAG: DEPLOYMENT, BUILD_ID_TAG: "build-0001"},
        "no deployment": {IMAGE_BUILD_TAG: "true", BUILD_ID_TAG: "build-0001"},
        "no tags": {},
    }

    def test_a_properly_tagged_image_is_taken_on_every_path(self):
        self.build("build-0001", "failed")
        self.assertEqual(
            self.summary(self.plan("all", images=[hand_image(ami(1), owner_tags("build-0001"))], snapshots=[])),
            [("failed", "deregister", ami(1), None)],
        )
        self.assertEqual(
            self.summary(self.plan("all", images=[hand_image(ami(2), owner_tags("build-lost"))], snapshots=[])),
            [("orphan", "deregister", ami(2), None)],
        )

    def test_an_image_with_a_missing_or_wrong_ownership_tag_is_left_alone_on_every_path(self):
        self.build("build-0001", "failed")
        for name, tags in self.FOREIGN.items():
            for build_id in ("build-0001", "build-lost"):
                with self.subTest(name, build=build_id):
                    named = {**tags, BUILD_ID_TAG: build_id} if tags.get(BUILD_ID_TAG) else tags
                    foreign = hand_image(ami(1), named)
                    self.assertEqual(self.plan("all", images=[foreign], snapshots=[]), [])

    def test_a_snapshot_with_a_missing_or_wrong_ownership_tag_is_left_alone(self):
        self.build("build-0001", "failed")
        for name, tags in self.FOREIGN.items():
            with self.subTest(name):
                self.assertEqual(self.plan("all", images=[], snapshots=[hand_snapshot(snap(1), tags)]), [])

    def test_a_snapshot_is_taken_with_its_image_only_under_the_same_build_id(self):
        self.build("build-0001", "failed")
        self.build("build-0002", "failed")
        image = hand_image(ami(1), owner_tags("build-0001"), snapshot_ids=(snap(1), snap(2), snap(3), snap(4), snap(5)))
        snapshots = [
            hand_snapshot(snap(1), owner_tags("build-0001")),
            hand_snapshot(snap(2), owner_tags("build-0002")),
            hand_snapshot(snap(3), owner_tags("build-0001", OTHER_DEPLOYMENT)),
            hand_snapshot(snap(4), owner_tags(None)),
        ]
        (item,) = self.plan("all", images=[image], snapshots=snapshots)
        self.assertEqual(item["action"], "deregister")
        self.assertEqual(item["snapshot_ids"], [snap(1)])
        self.assertEqual(
            item["skipped_snapshots"],
            [{"snapshot_id": snapshot, "reason": "snapshot_not_verified"} for snapshot in (snap(2), snap(3), snap(4), snap(5))],
        )

    def test_a_mismatched_snapshot_is_kept_when_the_image_is_taken(self):
        self.build("build-0001", "failed")
        self.add_snapshot(snap(1), "build-0001")
        self.add_snapshot(snap(2), "build-other")
        self.add_image(ami(1), "build-0001", snapshots=(snap(1), snap(2)))
        report = self.run_paths("failed")
        self.assertEqual(
            report,
            [
                {"path": "failed", "image_id": ami(1), "action": "deregister", "result": "deregistered"},
                {"path": "failed", "image_id": ami(1), "action": "delete_snapshot", "result": "snapshot_deleted", "snapshot_id": snap(1)},
                {
                    "path": "failed",
                    "image_id": ami(1),
                    "action": "delete_snapshot",
                    "result": "skipped",
                    "snapshot_id": snap(2),
                    "reason": "snapshot_not_verified",
                },
            ],
        )
        self.assertEqual(self.requests("delete_snapshot"), [{"SnapshotId": snap(1)}])
        self.assertEqual(self.stored_snapshots(), {snap(2)})

    def test_a_snapshot_that_is_not_listed_is_never_deleted_with_its_image(self):
        self.build("build-0001", "failed")
        self.add_image(ami(1), "build-0001", snapshots=(snap(7),))
        (item,) = self.plan("all")
        self.assertEqual((item["snapshot_ids"], len(item["skipped_snapshots"])), ([], 1))

    def test_the_same_snapshot_listed_twice_by_an_image_is_planned_once(self):
        self.build("build-0001", "failed")
        image = hand_image(ami(1), owner_tags("build-0001"), snapshot_ids=(snap(1), snap(1)))
        (item,) = self.plan("all", images=[image], snapshots=[hand_snapshot(snap(1), owner_tags("build-0001"))])
        self.assertEqual(item["snapshot_ids"], [snap(1)])

    def test_images_of_another_deployment_in_the_listing_are_never_counted_for_retention(self):
        for number in (1, 2, 3, 4):
            self.build(f"build-{number:04d}", "promoted")
        images = [
            hand_image(ami(number), owner_tags(f"build-{number:04d}", OTHER_DEPLOYMENT), created_at=NOW - 9 * DAY)
            for number in (1, 2, 3, 4)
        ]
        self.assertEqual(self.plan("all", images=images, snapshots=[]), [])


class RetentionTests(DeletionCase):
    def usable(self, *days, **row):
        """Usable builds with one image each, ``days`` old, oldest first. Their numbers are 11, 12, ..."""
        return [self.made(11 + index, "promoted", age=age * DAY, **row) for index, age in enumerate(days)]

    def test_fewer_images_than_retain_are_all_kept(self):
        for count in (1, 2, 3):
            with self.subTest(count=count):
                self.reset()
                self.usable(*range(30, 30 - count, -1))
                self.assertEqual(self.plan("retention"), [])

    def test_the_oldest_surplus_image_is_taken_with_its_snapshot(self):
        made = self.usable(9, 8, 7, 6)
        plan = self.plan("retention")
        self.assertEqual(self.summary(plan), [("retention", "deregister", made[0].image_id, None)])
        self.assertEqual(plan[0]["snapshot_ids"], [made[0].snapshot_id])

    def test_the_surplus_is_the_count_over_retain(self):
        made = self.usable(9, 8, 7, 6, 5, 4)
        self.assertEqual([item["image_id"] for item in self.plan("retention")], [made[0].image_id, made[1].image_id, made[2].image_id])

    def test_the_total_never_falls_below_retain(self):
        self.usable(9, 8, 7, 6, 5, 4)
        self.run_paths("retention")
        self.assertEqual(len(self.stored_images()), 3)
        self.assertEqual(self.run_paths("retention"), [])
        self.assertEqual(len(self.stored_images()), 3)

    def test_retention_follows_the_configured_retain(self):
        made = self.usable(9, 8, 7)
        smaller = dataclasses.replace(self.config, retain=2)
        self.assertEqual([item["image_id"] for item in self.plan("retention", config=smaller)], [made[0].image_id])
        larger = dataclasses.replace(self.config, retain=6)
        self.assertEqual(self.plan("retention", config=larger), [])

    def test_protected_images_are_counted_in_the_kept_set(self):
        made = [
            self.made(11, "promoted", age=9 * DAY, image_id=PREVIOUS_IMAGE),
            self.made(12, "promoted", age=8 * DAY, image_id=PIN_IMAGE),
            self.made(13, "promoted", age=7 * DAY),
            self.made(14, "promoted", age=6 * DAY),
        ]
        plan = self.plan("retention", previous_pin=PREVIOUS)
        self.assertEqual([item["image_id"] for item in plan], [made[2].image_id])
        self.run_paths("retention", previous_pin=PREVIOUS)
        self.assertEqual(self.stored_images(), {PREVIOUS_IMAGE, PIN_IMAGE, made[3].image_id})

    def test_more_protected_images_than_retain_leave_no_unprotected_old_one(self):
        self.write_deployment(zones=[ZONE_ENTRY])
        self.made(11, "promoted", age=9 * DAY, image_id=PREVIOUS_IMAGE)
        self.made(12, "promoted", age=8 * DAY, image_id=PIN_IMAGE)
        self.made(13, "promoted", age=7 * DAY, image_id=ZONE_IMAGE)
        old = [self.made(14, "promoted", age=6 * DAY), self.made(15, "promoted", age=5 * DAY)]
        plan = self.plan("retention", previous_pin=PREVIOUS)
        self.assertEqual([item["image_id"] for item in plan], [made.image_id for made in old])
        self.run_paths("retention", previous_pin=PREVIOUS)
        self.assertEqual(self.stored_images(), {PREVIOUS_IMAGE, PIN_IMAGE, ZONE_IMAGE})

    def test_a_young_image_is_kept(self):
        self.made(11, "promoted", age=20 * HOUR)
        self.made(12, "promoted", age=10 * HOUR)
        self.made(13, "promoted", age=5 * HOUR)
        self.made(14, "promoted", age=2 * HOUR)
        self.assertEqual(self.plan("retention"), [])
        self.assertEqual(self.run_paths("retention"), [])
        self.assertEqual(len(self.stored_images()), 4)

    def test_only_an_old_image_is_taken_while_young_ones_stay(self):
        old = self.made(11, "promoted", age=30 * HOUR)
        self.made(12, "promoted", age=20 * HOUR)
        self.made(13, "promoted", age=10 * HOUR)
        self.made(14, "promoted", age=5 * HOUR)
        self.assertEqual([item["image_id"] for item in self.plan("retention")], [old.image_id])

    def test_the_minimum_age_is_strict(self):
        self.made(11, "promoted", age=DAY)
        self.made(12, "promoted", age=DAY - 1)
        self.made(13, "promoted", age=HOUR)
        self.made(14, "promoted", age=60)
        self.assertEqual(self.plan("retention"), [])
        self.reset()
        old = self.made(11, "promoted", age=DAY + 1)
        self.made(12, "promoted", age=DAY - 1)
        self.made(13, "promoted", age=HOUR)
        self.made(14, "promoted", age=60)
        self.assertEqual([item["image_id"] for item in self.plan("retention")], [old.image_id])

    def test_the_minimum_age_follows_the_configuration(self):
        self.usable(9, 8, 7, 6)
        longer = dataclasses.replace(self.config, retain_min_age_seconds=10 * DAY)
        self.assertEqual(self.plan("retention", config=longer), [])
        shorter = dataclasses.replace(self.config, retain_min_age_seconds=6 * DAY + HOUR)
        self.assertEqual(len(self.plan("retention", config=shorter)), 1)

    def test_an_image_with_no_creation_time_is_kept_but_counted(self):
        unknown = self.made(11, "promoted")
        self.ec2.images_store[0].pop("CreationDate")
        others = [self.made(12, "promoted", age=9 * DAY), self.made(13, "promoted", age=8 * DAY), self.made(14, "promoted", age=7 * DAY)]
        plan = self.plan("retention")
        self.assertEqual([item["image_id"] for item in plan], [others[0].image_id])
        self.run_paths("retention")
        self.assertIn(unknown.image_id, self.stored_images())
        self.assertEqual(len(self.stored_images()), 3)

    def test_the_oldest_surplus_images_are_taken_whatever_the_listing_order(self):
        made = self.usable(7, 9, 6, 8, 5)
        self.assertEqual(
            [item["image_id"] for item in self.plan("retention")], [made[1].image_id, made[3].image_id]
        )

    def test_the_failed_images_of_failed_builds_are_not_counted(self):
        self.usable(9, 8, 7)
        failed = self.made(14, "failed", age=20 * DAY)
        self.assertEqual(self.plan("retention"), [])
        self.assertEqual(self.summary(self.plan("all")), [("failed", "deregister", failed.image_id, None)])

    def test_an_image_with_no_row_is_not_counted(self):
        self.usable(9, 8, 7)
        self.add_image(ami(1), "build-lost", age=20 * DAY)
        self.assertEqual(self.plan("retention"), [])
        self.assertEqual(self.summary(self.plan("all")), [("orphan", "deregister", ami(1), None)])

    def test_only_available_images_are_counted_or_taken(self):
        self.usable(9, 8, 7)
        self.made(14, "promoted", age=20 * DAY, image_state="failed")
        self.made(15, "candidate", age=20 * DAY, image_state="pending")
        self.assertEqual(self.plan("retention"), [])

    def test_candidates_are_counted_and_the_current_candidate_is_protected(self):
        current = self.make_build("build-0011", "candidate", image_id=ami(11))
        self.made(11, "candidate", age=9 * DAY)
        self.made(12, "promoted", age=8 * DAY)
        self.made(13, "promoted", age=7 * DAY)
        self.made(14, "promoted", age=6 * DAY)
        plan = self.plan("retention", candidate=current)
        self.assertEqual([item["image_id"] for item in plan], [ami(12)])

    def test_retention_needs_its_limits_and_only_retention_does(self):
        self.usable(9, 8, 7, 6)
        for name, config in (
            ("disabled", ImageConfig()),
            ("zero retain", dataclasses.replace(self.config, retain=0)),
            ("a bool retain", dataclasses.replace(self.config, retain=True)),
            ("a negative minimum age", dataclasses.replace(self.config, retain_min_age_seconds=-1)),
            ("no minimum age", dataclasses.replace(self.config, retain_min_age_seconds=None)),
        ):
            with self.subTest(name):
                with self.assertRaises(ImageRefusal) as raised:
                    self.plan("retention", config=config)
                self.assertEqual(raised.exception.reason, UNSAFE)
                self.assertEqual(self.plan("failed", config=config), [])
        with self.assertRaises(ImageRefusal):
            self.plan("all", config=ImageConfig())


class ModeTests(DeletionCase):
    def world(self):
        self.made(1, "failed")
        self.made(2, "failed", image_state="pending")
        self.add_image(ami(3), "build-lost")
        for number, age in ((11, 9), (12, 8), (13, 7), (14, 6)):
            self.made(number, "promoted", age=age * DAY)

    def test_all_covers_every_path_in_order(self):
        self.world()
        plan = self.plan("all")
        self.assertEqual(
            [(item["path"], item["image_id"]) for item in plan],
            [("failed", ami(1)), ("pending_timeout", ami(2)), ("orphan", ami(3)), ("retention", ami(11))],
        )

    def test_one_path_covers_only_that_path(self):
        self.world()
        for path, image_id in (("failed", ami(1)), ("pending_timeout", ami(2)), ("orphan", ami(3)), ("retention", ami(11))):
            with self.subTest(path):
                self.assertEqual([(item["path"], item["image_id"]) for item in self.plan(path)], [(path, image_id)])

    def test_a_collection_of_paths_covers_those_paths(self):
        self.world()
        for mode in (("failed", "retention"), ["retention", "failed"], {"failed", "retention"}, frozenset({"failed", "retention"})):
            with self.subTest(mode=mode):
                self.assertEqual([item["path"] for item in self.plan(mode)], ["failed", "retention"])

    def test_a_mode_that_is_not_a_path_is_refused_before_anything_is_planned(self):
        self.world()
        for mode in ("", "everything", "ALL", ("failed", "all"), ["nothing"], [], (), None, 7):
            with self.subTest(mode=mode):
                with self.assertRaises(ValueError):
                    self.plan(mode)
        self.assertEqual(self.mutations(), [])

    def test_a_path_does_not_take_what_another_path_owns(self):
        self.world()
        taken = [item["image_id"] for path in image_deletion.PATHS for item in self.plan(path)]
        self.assertEqual(len(taken), len(set(taken)))
        self.assertEqual(sorted(taken), sorted(item["image_id"] for item in self.plan("all")))


class ApplyTests(DeletionCase):
    def test_a_dry_run_makes_no_call_at_all(self):
        self.made(1, "failed")
        self.made(2, "failed", image_state="pending")
        self.add_image(ami(3), "build-lost")
        self.add_snapshot(snap(4), "build-lost")
        plan = self.plan("all")
        calls_before = list(self.calls)
        images_before, snapshots_before = self.stored_images(), self.stored_snapshots()
        report = apply_deletions(self.aws, plan, apply=False)
        self.assertEqual(self.calls, calls_before)
        self.assertEqual((self.stored_images(), self.stored_snapshots()), (images_before, snapshots_before))
        self.assertEqual({row["result"] for row in report}, {"planned"})
        # Sorted by path, then image id (the item with no image first), so the
        # orphan snapshot with no image comes before the orphan image.
        self.assertEqual(
            [(row["action"], row["image_id"], row.get("snapshot_id")) for row in report],
            [
                ("deregister", ami(1), None),
                ("delete_snapshot", ami(1), snap(1)),
                ("deregister", ami(2), None),
                ("delete_snapshot", ami(2), snap(2)),
                ("delete_snapshot", None, snap(4)),
                ("deregister", ami(3), None),
            ],
        )

    def test_a_dry_run_reports_skips_as_skipped(self):
        self.made(1, "failed", image_id=PIN_IMAGE)
        report = self.run_paths("all", apply=False)
        self.assertEqual(report, [{"path": "failed", "image_id": PIN_IMAGE, "action": "skip", "result": "skipped", "reason": "protected"}])

    def test_an_empty_plan_is_an_empty_report(self):
        self.assertEqual(apply_deletions(self.aws, [], apply=True), [])
        self.assertEqual(apply_deletions(self.aws, [], apply=False), [])
        self.assertEqual(self.calls, [])

    def test_an_image_is_deregistered_and_read_back_before_its_snapshot_is_deleted(self):
        made = self.made(1, "failed")
        plan = self.plan("all")
        del self.calls[:]
        report = apply_deletions(self.aws, plan, apply=True)
        self.assertEqual(self.names(), ["deregister_image", "describe_images", "delete_snapshot"])
        self.assertEqual(self.requests("describe_images")[0]["ImageIds"], [made.image_id])
        self.assertEqual(self.results(report), [("deregister", "deregistered"), ("delete_snapshot", "snapshot_deleted")])
        self.assertEqual((self.stored_images(), self.stored_snapshots()), (set(), set()))

    def test_every_image_is_confirmed_before_its_own_snapshots_and_not_the_next_images(self):
        self.made(1, "failed")
        self.made(2, "failed")
        plan = self.plan("all")
        del self.calls[:]
        apply_deletions(self.aws, plan, apply=True)
        self.assertEqual(
            self.names(),
            ["deregister_image", "describe_images", "delete_snapshot"] * 2,
        )

    def test_an_image_that_stays_visible_as_deregistered_is_confirmed(self):
        made = self.made(1, "failed")
        self.ec2.ghosts.add(made.image_id)
        plan = self.plan("all")
        report = apply_deletions(self.aws, plan, apply=True)
        self.assertEqual(self.results(report), [("deregister", "deregistered"), ("delete_snapshot", "snapshot_deleted")])
        self.assertEqual(self.stored_snapshots(), set())

    def test_a_refused_deregistration_keeps_the_snapshots_and_the_run_continues(self):
        bad = self.made(1, "failed")
        good = self.made(2, "failed")
        self.ec2.refused[bad.image_id] = ProviderError("UnauthorizedOperation")
        with self.assertRaises(ProviderRefusal) as expected:
            self.aws.deregister_image(bad.image_id)
        del self.calls[:]
        report = self.run_paths("all")
        self.assertEqual(
            report,
            [
                {"path": "failed", "image_id": bad.image_id, "action": "deregister", "result": "deregister_failed", "code": expected.exception.reason},
                {"path": "failed", "image_id": bad.image_id, "action": "delete_snapshot", "result": "snapshot_kept", "snapshot_id": bad.snapshot_id},
                {"path": "failed", "image_id": good.image_id, "action": "deregister", "result": "deregistered"},
                {"path": "failed", "image_id": good.image_id, "action": "delete_snapshot", "result": "snapshot_deleted", "snapshot_id": good.snapshot_id},
            ],
        )
        self.assertEqual(self.requests("delete_snapshot"), [{"SnapshotId": good.snapshot_id}])
        self.assertEqual((self.stored_images(), self.stored_snapshots()), ({bad.image_id}, {bad.snapshot_id}))

    def test_a_deregistration_that_is_not_confirmed_keeps_the_snapshots(self):
        made = self.made(1, "failed")
        self.ec2.stuck.add(made.image_id)
        report = self.run_paths("all")
        self.assertEqual(
            self.results(report), [("deregister", "deregister_unconfirmed"), ("delete_snapshot", "snapshot_kept")]
        )
        self.assertNotIn("code", report[0])
        self.assertEqual(self.mutations(), ["deregister_image"])
        self.assertEqual((self.stored_images(), self.stored_snapshots()), ({made.image_id}, {made.snapshot_id}))

    def test_a_read_back_that_is_refused_is_not_a_confirmation(self):
        made = self.made(1, "failed")
        plan = self.plan("all")
        self.ec2.errors["describe_images"] = ProviderError("RequestLimitExceeded")
        with self.assertRaises(ProviderRefusal) as expected:
            self.aws.image(made.image_id)
        report = apply_deletions(self.aws, plan, apply=True)
        self.assertEqual(
            self.results(report), [("deregister", "deregister_unconfirmed"), ("delete_snapshot", "snapshot_kept")]
        )
        self.assertEqual(report[0]["code"], expected.exception.reason)
        self.assertEqual(self.mutations(), ["deregister_image"])

    def test_a_refused_snapshot_deletion_is_reported_and_the_others_go_on(self):
        self.add_snapshot(snap(1), "build-0001")
        self.add_snapshot(snap(2), "build-0001")
        self.add_snapshot(snap(3), "build-0001")
        self.add_image(ami(1), "build-0001", snapshots=(snap(1), snap(2), snap(3)))
        self.build("build-0001", "failed")
        self.ec2.undeletable[snap(2)] = ProviderError("InvalidSnapshot.InUse")
        report = self.run_paths("all")
        self.assertEqual(
            self.results(report),
            [("deregister", "deregistered"), ("delete_snapshot", "snapshot_deleted"), ("delete_snapshot", "snapshot_failed"), ("delete_snapshot", "snapshot_deleted")],
        )
        self.assertIn("code", report[2])
        self.assertEqual(self.stored_snapshots(), {snap(2)})

    def test_a_lone_snapshot_that_cannot_be_deleted_is_reported(self):
        self.add_snapshot(snap(1), "build-lost")
        self.ec2.undeletable[snap(1)] = ProviderError("InvalidSnapshot.InUse")
        report = self.run_paths("orphan")
        self.assertEqual(self.results(report), [("delete_snapshot", "snapshot_failed")])
        self.assertEqual(report[0]["snapshot_id"], snap(1))

    def test_a_protected_image_gets_no_call(self):
        self.made(1, "failed", image_id=PIN_IMAGE)
        self.run_paths("all")
        self.assertEqual(self.mutations(), [])
        # The listings and the version read only: no read-back of an image.
        self.assertEqual(
            sorted(self.names()), ["describe_images", "describe_launch_template_versions", "describe_snapshots"]
        )

    def test_every_row_names_its_path_image_action_and_result(self):
        self.made(1, "failed")
        self.made(2, "failed", image_id=PIN_IMAGE)
        self.add_snapshot(snap(3), "build-lost")
        for row in self.run_paths("all"):
            self.assertLessEqual({"path", "image_id", "action", "result"}, set(row))
            self.assertIn(row["path"], image_deletion.PATHS)
            self.assertIn(row["action"], ("deregister", "delete_snapshot", "skip"))

    def test_an_error_that_is_not_a_provider_refusal_is_not_swallowed(self):
        self.made(1, "failed")
        plan = self.plan("all")

        class Broken:
            def deregister_image(self, image_id):
                raise RuntimeError("example")

        with self.assertRaises(RuntimeError):
            apply_deletions(Broken(), plan, apply=True)

    def test_plan_and_report_carry_ids_and_codes_only(self):
        sentinel = SENTINELS[0]
        self.add_snapshot(snap(1), "build-0001", tags={**owner_tags("build-0001"), "example:note": sentinel})
        self.add_image(
            ami(1),
            "build-0001",
            snapshots=(snap(1),),
            tags={**owner_tags("build-0001"), "example:note": sentinel},
        )
        self.build("build-0001", "failed", reason_code="WARM_FAILED")
        self.ec2.refused[ami(1)] = ProviderError("UnauthorizedOperation")
        plan = self.plan("all")
        report = apply_deletions(self.aws, plan, apply=True)
        for text in (json.dumps(plan), json.dumps(report), repr(plan), repr(report)):
            for marker in SENTINELS:
                self.assertNotIn(marker, text)
            self.assertNotIn("WARM_FAILED", text)


class PlanProvenanceTests(DeletionCase):
    """``apply_deletions`` runs only what ``plan_deletions`` decided, as decided, and checks that before any call."""

    def world(self):
        """A new account: two failed builds to remove and an old tagged image that only its pin keeps.

        Returns the plan: the two failed images (indexes 0 and 1), then the skip item for the pinned image (index 2).
        """
        self.reset()
        self.made(1, "failed")
        self.made(2, "failed")
        self.add_image(PIN_IMAGE, "build-lost", age=30 * DAY)
        plan = self.plan("all")
        self.assertEqual(
            self.summary(plan),
            [
                ("failed", "deregister", ami(1), None),
                ("failed", "deregister", ami(2), None),
                ("orphan", "skip", PIN_IMAGE, "protected"),
            ],
        )
        del self.calls[:]
        return plan

    def refused(self, plan, *, apply):
        with self.assertRaises(ImageRefusal) as raised:
            apply_deletions(self.aws, plan, apply=apply)
        self.assertEqual(raised.exception.reason, UNSAFE)
        self.assertNotIn("ami-", raised.exception.detail)
        # Nothing ran, not even a read, and nothing was removed.
        self.assertEqual(self.calls, [])
        self.assertLessEqual({PIN_IMAGE, ami(1), ami(2), snap(1), snap(2)}, self.stored_images() | self.stored_snapshots())

    def test_a_plan_that_plan_deletions_did_not_make_is_refused_before_any_call(self):
        decided = self.world()
        unprotected = dict(decided[2], action="deregister", reason=None)
        for name, plan in (
            ("a hand-built item for the pinned image", [unprotected]),
            ("plain copies of decided items", [dict(item) for item in decided]),
            ("a decided item, then a hand-built one", [decided[0], decided[1], unprotected]),
            ("a hand-built item, then decided ones", [unprotected, decided[0], decided[1]]),
            ("image ids", [ami(1)]),
            ("a string", "deregister"),
            ("one item in place of the plan", decided[0]),
            ("nothing", None),
        ):
            for apply in (True, False):
                with self.subTest(name, apply=apply):
                    self.refused(plan, apply=apply)

    def test_an_item_edited_after_planning_is_refused_before_any_call(self):
        for name, edit in (
            ("a protected skip turned into a deregistration", lambda plan: plan[2].update(action="deregister", reason=None)),
            ("a decided item pointed at the pinned image", lambda plan: plan[0].update(image_id=PIN_IMAGE)),
            ("a snapshot added to an item", lambda plan: plan[0]["snapshot_ids"].append(snap(99))),
            ("a key removed", lambda plan: plan[0].pop("snapshot_ids")),
            ("a key added", lambda plan: plan[0].update(note="x")),
            ("a path changed", lambda plan: plan[0].update(path="retention")),
            ("a skipped snapshot listed", lambda plan: plan[2]["skipped_snapshots"].append({"snapshot_id": snap(99), "reason": "x"})),
        ):
            for apply in (True, False):
                with self.subTest(name, apply=apply):
                    plan = self.world()
                    edit(plan)
                    self.refused(plan, apply=apply)

    def test_an_item_with_an_action_that_is_not_known_is_refused_and_nothing_runs(self):
        for action in ("purge", "", None, "DEREGISTER", "delete_snapshots", 7, ["deregister"]):
            for apply in (True, False):
                with self.subTest(action=action, apply=apply):
                    plan = self.world()
                    plan[1]["action"] = action  # not the first item: the item before it must not run either
                    self.refused(plan, apply=apply)
        plan = self.world()
        del plan[1]["action"]
        self.refused(plan, apply=True)

    def test_an_unknown_action_does_not_fall_through_to_a_snapshot_deletion(self):
        plan = self.world()
        plan[0]["action"] = "purge"
        with self.assertRaises(ImageRefusal):
            apply_deletions(self.aws, plan, apply=True)
        self.assertEqual(self.requests("delete_snapshot"), [])
        self.assertEqual(self.stored_snapshots(), {snap(1), snap(2)})

    def test_a_plan_of_decided_items_is_accepted_whole_filtered_empty_or_copied(self):
        decided = self.world()
        only_second = [item for item in decided if item["image_id"] == ami(2)]
        report = apply_deletions(self.aws, only_second, apply=True)
        self.assertEqual(self.results(report), [("deregister", "deregistered"), ("delete_snapshot", "snapshot_deleted")])
        self.assertEqual(self.requests("deregister_image"), [{"ImageId": ami(2)}])
        for plan in ([], (), iter(())):
            self.assertEqual(apply_deletions(self.aws, plan, apply=True), [])
        for name, plan in (("a copy", copy.copy(decided)), ("a deep copy", copy.deepcopy(decided)), ("a tuple", tuple(decided))):
            with self.subTest(name):
                self.assertEqual({row["result"] for row in apply_deletions(self.aws, plan, apply=False)}, {"planned", "skipped"})

    def test_a_plan_is_still_a_list_of_dicts_that_serializes_unchanged(self):
        plan = self.world()
        self.assertIs(type(plan), list)
        self.assertTrue(all(isinstance(item, dict) for item in plan))
        self.assertEqual(plan, [dict(item) for item in plan])
        self.assertEqual(json.loads(json.dumps(plan)), [dict(item) for item in plan])
        self.assertEqual(repr(plan), repr([dict(item) for item in plan]))
        self.assertEqual(set(plan[0]), {"path", "image_id", "build_id", "action", "reason", "snapshot_ids", "skipped_snapshots"})


class CodeTests(unittest.TestCase):
    """The codes and names that a caller, an event or an operator reads are fixed strings."""

    def test_the_codes_are_stable_strings(self):
        self.assertEqual(image_deletion.IMAGE_DELETION_UNSAFE, "IMAGE_DELETION_UNSAFE")
        self.assertEqual(image_deletion.PATHS, ("failed", "pending_timeout", "orphan", "retention"))
        self.assertEqual(
            (image_deletion.PROTECTED, image_deletion.TOO_YOUNG, image_deletion.AGE_UNKNOWN),
            ("protected", "too_young", "age_unknown"),
        )
        self.assertEqual(image_deletion.SNAPSHOT_NOT_VERIFIED, "snapshot_not_verified")
        self.assertEqual(
            (image_deletion.DEREGISTER, image_deletion.DELETE_SNAPSHOT, image_deletion.SKIP),
            ("deregister", "delete_snapshot", "skip"),
        )
        self.assertEqual(
            (
                image_deletion.PLANNED,
                image_deletion.SKIPPED,
                image_deletion.DEREGISTERED,
                image_deletion.DEREGISTER_FAILED,
                image_deletion.DEREGISTER_UNCONFIRMED,
                image_deletion.SNAPSHOT_DELETED,
                image_deletion.SNAPSHOT_FAILED,
                image_deletion.SNAPSHOT_KEPT,
            ),
            (
                "planned",
                "skipped",
                "deregistered",
                "deregister_failed",
                "deregister_unconfirmed",
                "snapshot_deleted",
                "snapshot_failed",
                "snapshot_kept",
            ),
        )

    def test_the_waits_are_24_hours(self):
        self.assertEqual((image_deletion.ORPHAN_AGE_SECONDS, image_deletion.DRIFT_HOLD_SECONDS), (DAY, DAY))


if __name__ == "__main__":
    unittest.main()
