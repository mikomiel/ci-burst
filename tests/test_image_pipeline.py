"""Tests for the orchestration of one automated warm-image build.

``ImagePipeline`` plans, runs and supervises a build, and decides nothing that a
building block owns. These tests therefore run the real blocks (``image``,
``image_console``, ``image_template``, ``image_deletion``, ``ImageState`` and the
``ImageAws`` adapter) against a stateful fake account:

* ``World`` holds EC2 instances, images, snapshots, launch templates and S3
  objects, shares one fake clock with the code under test, and records every
  provider call. The ``ImageAws`` adapter wraps its two fake clients, so the
  adapter's own rules (the deadline, the tag filters, the credential check)
  still apply.
* ``FakeSource`` stands in for the Forgejo reader.
* The ``sleep`` of a test advances the fake clock, so a build that takes
  hours of fake time takes milliseconds.

Every test ends with a scan: no presigned URL, forge token, user data or consumer
URL may appear in the state file, the event file, the metrics file, the telemetry
file, a result or a refusal. All values are synthetic and the sentinels are
assembled at run time, so no secret-shaped literal sits in this file.
"""

import copy
import hashlib
import json
import re
import shlex
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ci_burst import image, image_pipeline
from ci_burst.aws import BASIS_DIGEST_TAG, BUILD_ID_TAG, DEPLOYMENT_TAG, EXPIRES_TAG, IMAGE_BUILD_TAG, ImageAws
from ci_burst.config import load
from ci_burst.errors import ObservationRefusal, ProviderRefusal, Refusal
from ci_burst.forgejo import BranchHead
from ci_burst.image_pipeline import ImagePipeline
from ci_burst.image_state import ImageState

from support import Clock, write_config
from test_image_aws import SENTINELS, ProviderError, tag_list

NOW = 1_800_000_000.0
HOUR = 3600
DAY = 86_400
REGION = "eu-central-1"
PROFILE = "example-image-profile"
DEPLOYMENT = "example-deployment"
WORKER = "example-ci-burst-worker"
BUILDER = "example-ci-burst-builder"
BUCKET = "example-ci-burst-staging"
PREFIX = "warm-archives/"
PIN, PREVIOUS = 3, 2
SOURCE_SHA = "ab" * 20
OLD_DIGEST = "0" * 64
SCRIPT = b"#!/bin/bash\necho synthetic warm script\n"
LOCK_FILE = b"synthetic lock file\n"
ARCHIVE = bytes(range(256)) * 40
ARCHIVE_SHA = hashlib.sha256(ARCHIVE).hexdigest()
BUILD_DEADLINE = 2700

# Assembled at run time, so no secret-shaped literal sits in this file.
SIGNATURE = "-".join(("not", "a", "real", "signature", "value"))
TOKEN = "-".join(("not", "a", "real", "forge", "token"))
CONSUMER_URL = "https://consumer.example.invalid/path?access=" + "-".join(("not", "a", "real", "consumer", "secret"))

READS = frozenset(
    {
        "describe_instances",
        "get_console_output",
        "describe_images",
        "describe_snapshots",
        "describe_launch_templates",
        "describe_launch_template_versions",
        "list_objects_v2",
    }
)


def ami(number):
    return f"ami-{number:017x}"


def snap(number):
    return f"snap-{number:017x}"


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def console(*lines):
    return "\n".join(lines) + "\n"


WAITING = console("[    0.1] kernel boot", "cloud-init: running user data")
READY = console("cloud-init: running user data", f"WARM_SOURCE_SHA256 {ARCHIVE_SHA}", "IMAGE_READY")
FAILED = console(f"WARM_SOURCE_SHA256 {ARCHIVE_SHA}", "  | consumer step output line", "WARM FAIL consumer step 2")
MISMATCH = console("WARM_SOURCE_SHA256 " + "f" * 64, "IMAGE_READY")

SAFE_BUILDER = {
    "ImageId": ami(0xB0),
    "InstanceType": "c6i.large",
    "InstanceInitiatedShutdownBehavior": "terminate",
    "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1, "HttpEndpoint": "enabled"},
    "BlockDeviceMappings": [
        {"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 40, "VolumeType": "gp3", "DeleteOnTermination": True}}
    ],
}

IMAGE_TOML = """
[image]
enabled = true
profile = "example-image-role"
state_directory = "{state_directory}"
{image_extra}
[image.source]
token_env = "EXAMPLE_IMAGE_TOKEN"
basis_paths = ["example.lock"]
{source_extra}
[image.warm]
commands = ["bash scripts/example-warm.sh"]
{gate}
lifetime_seconds = 3000
snapshot_seconds = 900
[image.build]
launch_template_name = "{builder}"
launch_template_version = 1
[image.storage]
bucket = "{bucket}"
prefix = "{prefix}"
"""
GATE = 'gate_command = "bash scripts/example-gate.sh"'
STATE_DIRECTORY = "/var/lib/example-image-state"


def render_toml(*, image_extra="", source_extra="", gate=GATE, state_directory=STATE_DIRECTORY):
    return IMAGE_TOML.format(
        state_directory=state_directory,
        image_extra=image_extra,
        source_extra=source_extra,
        gate=gate,
        builder=BUILDER,
        bucket=BUCKET,
        prefix=PREFIX,
    )


def owned(build_id, deployment=DEPLOYMENT, **extra):
    """The tags that show an image or snapshot was made for one build."""
    return {IMAGE_BUILD_TAG: "true", DEPLOYMENT_TAG: deployment, BUILD_ID_TAG: build_id, **extra}


def foreign_builder_tags(build_id="build-foreign", expires_at=NOW + HOUR):
    return owned(build_id, **{EXPIRES_TAG: str(int(expires_at))})


# -- the fake account ----------------------------------------------------------


class WorldEc2:
    """The EC2 client calls the adapter makes, applied to the world's stores."""

    def __init__(self, world):
        self.w = world

    @staticmethod
    def _tags(item):
        return {tag["Key"]: tag["Value"] for tag in item.get("Tags", [])}

    def _filtered(self, items, filters):
        for entry in filters or []:
            name, values = entry["Name"], entry["Values"]
            if name.startswith("tag:"):
                items = [item for item in items if self._tags(item).get(name[4:]) in values]
            elif name == "instance-state-name":
                items = [item for item in items if item["State"]["Name"] in values]
            else:
                raise AssertionError(f"unexpected filter {name}")
        return items

    @staticmethod
    def _public(item):
        return {key: copy.deepcopy(value) for key, value in item.items() if not key.startswith("_")}

    # -- instances -------------------------------------------------------------
    def describe_instances(self, *, Filters=None, InstanceIds=None, NextToken=None):
        self.w.enter("describe_instances", dict(Filters=Filters, InstanceIds=InstanceIds, NextToken=NextToken))
        visible = {key: item for key, item in self.w.instances.items() if not self.w.is_hidden(key)}
        if InstanceIds is not None:
            if any(identifier not in visible for identifier in InstanceIds):
                raise ProviderError("InvalidInstanceID.NotFound")
            found = [visible[identifier] for identifier in InstanceIds]
        else:
            found = list(visible.values())
        found = self._filtered(found, Filters)
        return {"Reservations": [{"Instances": [self._public(item)]} for item in found]}

    def get_console_output(self, *, InstanceId, Latest):
        self.w.enter("get_console_output", dict(InstanceId=InstanceId, Latest=Latest))
        if InstanceId not in self.w.instances or self.w.is_hidden(InstanceId):
            raise ProviderError("InvalidInstanceID.NotFound")
        return {"Output": self.w.console_text(InstanceId)}

    def terminate_instances(self, *, InstanceIds):
        self.w.enter("terminate_instances", dict(InstanceIds=list(InstanceIds)))
        for identifier in InstanceIds:
            if identifier not in self.w.instances:
                raise ProviderError("InvalidInstanceID.NotFound")
        if not self.w.ignore_terminate:
            for identifier in InstanceIds:
                self.w.instances[identifier]["State"]["Name"] = "terminated"
        return {"TerminatingInstances": [{"InstanceId": identifier} for identifier in InstanceIds]}

    def run_instances(self, *, LaunchTemplate, MinCount, MaxCount, ClientToken, UserData, TagSpecifications):
        arguments = dict(
            LaunchTemplate=LaunchTemplate,
            MinCount=MinCount,
            MaxCount=MaxCount,
            ClientToken=ClientToken,
            UserData=UserData,
            TagSpecifications=TagSpecifications,
        )
        tags = {}
        for spec in TagSpecifications:
            if spec["ResourceType"] == "instance":
                tags = self._tags(spec)
        try:
            self.w.enter("run_instances", arguments)
        except Exception:
            if self.w.ghost_launch:
                self.w.add_instance(tags)  # the request was sent and the answer was lost
            raise
        known = self.w.launch_tokens.get(ClientToken)
        if known is not None:
            if known["UserData"] != UserData:
                raise ProviderError("IdempotentParameterMismatch")
            return {"Instances": [{"InstanceId": known["InstanceId"]}]}
        instance_id = self.w.add_instance(tags)
        record = {"InstanceId": instance_id, "UserData": UserData, "ClientToken": ClientToken, "Tags": tags}
        self.w.launch_tokens[ClientToken] = record
        self.w.launches.append(record)
        return {"Instances": [{"InstanceId": instance_id}]}

    # -- images and snapshots --------------------------------------------------
    def create_image(self, *, InstanceId, Name, NoReboot, TagSpecifications):
        self.w.enter(
            "create_image", dict(InstanceId=InstanceId, Name=Name, NoReboot=NoReboot, TagSpecifications=TagSpecifications)
        )
        if InstanceId not in self.w.instances:
            raise ProviderError("InvalidInstanceID.NotFound")
        specs = {spec["ResourceType"]: self._tags(spec) for spec in TagSpecifications}
        image_id, snapshot_id = self.w.new_id("ami"), self.w.new_id("snap")
        self.w.add_image(image_id, tags=specs["image"], state="pending", age=0, snapshots=(snapshot_id,))
        self.w.add_snapshot(snapshot_id, tags=specs["snapshot"], age=0)
        return {"ImageId": image_id}

    def describe_images(self, *, Owners=None, Filters=None, ImageIds=None, NextToken=None):
        self.w.enter("describe_images", dict(Owners=Owners, Filters=Filters, ImageIds=ImageIds, NextToken=NextToken))
        self.w.settle_images()
        items = list(self.w.images.values())
        if ImageIds is not None:
            items = [item for item in items if item["ImageId"] in ImageIds]
            if not items:
                raise ProviderError("InvalidAMIID.NotFound")
        items = self._filtered(items, Filters)
        return {"Images": [self._public(item) for item in items]}

    def describe_snapshots(self, *, OwnerIds=None, Filters=None, SnapshotIds=None, NextToken=None):
        self.w.enter(
            "describe_snapshots", dict(OwnerIds=OwnerIds, Filters=Filters, SnapshotIds=SnapshotIds, NextToken=NextToken)
        )
        items = list(self.w.snapshots.values())
        if SnapshotIds is not None:
            items = [item for item in items if item["SnapshotId"] in SnapshotIds]
            if not items:
                raise ProviderError("InvalidSnapshot.NotFound")
        items = self._filtered(items, Filters)
        return {"Snapshots": [self._public(item) for item in items]}

    def deregister_image(self, *, ImageId):
        self.w.enter("deregister_image", dict(ImageId=ImageId))
        self.w.images.pop(ImageId, None)
        return {}

    def delete_snapshot(self, *, SnapshotId):
        self.w.enter("delete_snapshot", dict(SnapshotId=SnapshotId))
        self.w.snapshots.pop(SnapshotId, None)
        return {}

    # -- launch templates ------------------------------------------------------
    def describe_launch_templates(self, *, LaunchTemplateNames, NextToken=None):
        self.w.enter("describe_launch_templates", dict(LaunchTemplateNames=LaunchTemplateNames, NextToken=NextToken))
        if any(name not in self.w.templates for name in LaunchTemplateNames):
            raise ProviderError("InvalidLaunchTemplateName.NotFoundException")
        return {"LaunchTemplates": [copy.deepcopy(self.w.templates[name]) for name in LaunchTemplateNames]}

    def describe_launch_template_versions(self, *, LaunchTemplateName, Versions, NextToken=None):
        self.w.enter(
            "describe_launch_template_versions",
            dict(LaunchTemplateName=LaunchTemplateName, Versions=Versions, NextToken=NextToken),
        )
        found = []
        for (name, number), item in sorted(self.w.versions.items()):
            if name != LaunchTemplateName or str(number) not in Versions:
                continue
            if number in self.w.omit_versions_once:
                self.w.omit_versions_once.discard(number)
                continue
            found.append(copy.deepcopy(item))
        return {"LaunchTemplateVersions": found}

    def create_launch_template_version(
        self, *, LaunchTemplateName, SourceVersion, LaunchTemplateData, VersionDescription, ClientToken
    ):
        self.w.enter(
            "create_launch_template_version",
            dict(
                LaunchTemplateName=LaunchTemplateName,
                SourceVersion=SourceVersion,
                LaunchTemplateData=LaunchTemplateData,
                VersionDescription=VersionDescription,
                ClientToken=ClientToken,
            ),
        )
        if ClientToken in self.w.version_tokens:
            return {"LaunchTemplateVersion": {"VersionNumber": self.w.version_tokens[ClientToken]}}
        source = self.w.versions[(LaunchTemplateName, int(SourceVersion))]
        data = copy.deepcopy(source["LaunchTemplateData"])
        data.update(LaunchTemplateData)
        data.update(self.w.version_drift)
        number = self.w.next_version
        self.w.next_version += 1
        self.w.set_version(LaunchTemplateName, number, data, description=VersionDescription)
        self.w.version_tokens[ClientToken] = number
        return {"LaunchTemplateVersion": {"VersionNumber": number}}


class WorldS3:
    """The S3 client calls the adapter makes."""

    def __init__(self, world):
        self.w = world
        self.parts = {}

    def create_multipart_upload(self, *, Bucket, Key):
        self.w.enter("create_multipart_upload", dict(Bucket=Bucket, Key=Key))
        upload_id = self.w.new_id("upload")
        self.parts[upload_id] = []
        return {"UploadId": upload_id}

    def upload_part(self, *, Bucket, Key, UploadId, PartNumber, Body):
        self.w.enter("upload_part", dict(Bucket=Bucket, Key=Key, UploadId=UploadId, PartNumber=PartNumber, Body=Body))
        self.parts[UploadId].append(bytes(Body))
        return {"ETag": f"etag-{PartNumber}"}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload):
        self.w.enter(
            "complete_multipart_upload",
            dict(Bucket=Bucket, Key=Key, UploadId=UploadId, MultipartUpload=MultipartUpload),
        )
        self.w.objects[Key] = (b"".join(self.parts.pop(UploadId)), self.w.clock())
        return {}

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        self.w.enter("abort_multipart_upload", dict(Bucket=Bucket, Key=Key, UploadId=UploadId))
        self.parts.pop(UploadId, None)
        return {}

    def delete_object(self, *, Bucket, Key):
        self.w.enter("delete_object", dict(Bucket=Bucket, Key=Key))
        self.w.objects.pop(Key, None)
        return {}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        self.w.enter("list_objects_v2", dict(Bucket=Bucket, Prefix=Prefix, ContinuationToken=ContinuationToken))
        contents = []
        for key, (body, modified) in sorted(self.w.objects.items()):
            if not key.startswith(Prefix):
                continue
            entry = {"Key": key, "Size": len(body)}
            if modified is not None:
                entry["LastModified"] = datetime.fromtimestamp(modified, timezone.utc)
            contents.append(entry)
        return {"Contents": contents}

    def generate_presigned_url(self, ClientMethod, *, Params, ExpiresIn):
        self.w.enter("generate_presigned_url", dict(ClientMethod=ClientMethod, Params=Params, ExpiresIn=ExpiresIn))
        url = (
            f"https://{Params['Bucket']}.s3.example.invalid/{Params['Key']}"
            f"?X-Amz-Expires={ExpiresIn}&X-Amz-Signature={SIGNATURE}" + "x" * self.w.url_padding
        )
        self.w.urls.append(url)
        return url


class World:
    """One fake account, shared by every session a test builds."""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.errors = {}
        self.hooks = {}
        self.refusals = {}
        self.counter = 0
        self.instances = {}
        self.hidden_until = {}
        self.hide_launches_for = 0.0
        self.console_plan = []
        self.consoles = {}
        self.ignore_terminate = False
        self.ghost_launch = False
        self.launches = []
        self.launch_tokens = {}
        self.images = {}
        self.snapshots = {}
        self.image_delay = 90.0
        self.image_final_state = "available"
        self.templates = {}
        self.versions = {}
        self.version_tokens = {}
        self.version_drift = {}
        self.omit_versions_once = set()
        self.next_version = 5
        self.objects = {}
        self.credential_life = float(HOUR)
        self.url_padding = 0
        self.urls = []
        self.ec2 = WorldEc2(self)
        self.s3 = WorldS3(self)
        self.seed()

    # -- recording and fault injection -----------------------------------------
    def enter(self, method, kwargs):
        logged = {key: value for key, value in kwargs.items() if key != "Body"}
        self.calls.append((method, copy.deepcopy(logged)))
        hook = self.hooks.get(method)
        if hook is not None:
            hook(kwargs)
        entry = self.errors.get(method)
        if entry is not None:
            error, remaining = entry
            if remaining is not None:
                entry[1] -= 1
                if entry[1] <= 0:
                    del self.errors[method]
            raise error

    def fail(self, method, error, times=None):
        """Make a provider call raise ``error``, ``times`` times or for good."""
        self.errors[method] = [error, times]

    def new_id(self, prefix):
        self.counter += 1
        return f"{prefix}-{self.counter:017x}"

    def names(self, mark=0):
        return [name for name, _ in self.calls[mark:]]

    def arguments(self, method):
        return [kwargs for name, kwargs in self.calls if name == method]

    # -- the account -----------------------------------------------------------
    def seed(self):
        self.templates[WORKER] = {
            "LaunchTemplateName": WORKER,
            "LaunchTemplateId": "lt-0123456789abcdef0",
            "DefaultVersionNumber": 1,
            "LatestVersionNumber": PIN,
        }
        self.templates[BUILDER] = {
            "LaunchTemplateName": BUILDER,
            "LaunchTemplateId": "lt-0123456789abcdef1",
            "DefaultVersionNumber": 1,
            "LatestVersionNumber": 1,
        }
        pin_data = {"ImageId": ami(0xA1), "InstanceType": "c6i.large", "UserData": "c3ludGhldGlj"}
        self.set_version(WORKER, PIN, pin_data)
        self.set_version(BUILDER, 1, copy.deepcopy(SAFE_BUILDER))
        self.add_image(ami(0xA1), tags={BASIS_DIGEST_TAG: OLD_DIGEST}, age=10 * DAY)

    def set_version(self, name, number, data, description=""):
        self.versions[(name, number)] = {
            "VersionNumber": number,
            "LaunchTemplateId": self.templates[name]["LaunchTemplateId"],
            "VersionDescription": description,
            "LaunchTemplateData": data,
        }

    def add_image(self, image_id, *, tags=None, state="available", age=3 * DAY, snapshots=()):
        item = {
            "ImageId": image_id,
            "State": state,
            "Tags": tag_list(tags or {}),
            "BlockDeviceMappings": [{"Ebs": {"SnapshotId": snapshot_id}} for snapshot_id in snapshots],
            "_created": self.clock() - age,
        }
        if age is not None:
            item["CreationDate"] = iso(self.clock() - age)
        self.images[image_id] = item

    def add_snapshot(self, snapshot_id, *, tags=None, age=3 * DAY):
        self.snapshots[snapshot_id] = {
            "SnapshotId": snapshot_id,
            "State": "completed",
            "Tags": tag_list(tags or {}),
            "StartTime": datetime.fromtimestamp(self.clock() - age, timezone.utc),
        }

    def settle_images(self):
        """A pending image becomes ``image_final_state`` once ``image_delay`` has passed."""
        for item in self.images.values():
            if item["State"] == "pending" and self.clock() - item["_created"] >= self.image_delay:
                item["State"] = self.image_final_state

    def add_instance(self, tags, state="running"):
        instance_id = self.new_id("i")
        now = self.clock()
        self.instances[instance_id] = {
            "InstanceId": instance_id,
            "State": {"Name": state},
            "Tags": tag_list(tags),
            "LaunchTime": datetime.fromtimestamp(now, timezone.utc),
            "_launched": now,
        }
        if self.hide_launches_for:
            self.hidden_until[instance_id] = now + self.hide_launches_for
        return instance_id

    def is_hidden(self, instance_id):
        return self.clock() < self.hidden_until.get(instance_id, 0.0)

    def set_state(self, instance_id, state):
        self.instances[instance_id]["State"]["Name"] = state

    def console_text(self, instance_id):
        if instance_id in self.consoles:
            return self.consoles[instance_id]
        elapsed = self.clock() - self.instances[instance_id]["_launched"]
        text = ""
        for delay, value in self.console_plan:
            if elapsed >= delay:
                text = value
        return text


class WorldAws(ImageAws):
    """The real adapter on the world's clients, with room to inject a refusal of its own."""

    def __init__(self, world):
        super().__init__(
            REGION,
            PROFILE,
            clients={"ec2": world.ec2, "s3": world.s3},
            clock=world.clock,
            expiry=world.clock() + world.credential_life,
        )
        self.world = world

    def _refuse(self, name):
        error = self.world.refusals.get(name)
        if error is not None:
            raise error

    def run_builder(self, **arguments):
        self._refuse("run_builder")
        return super().run_builder(**arguments)

    def create_image(self, *arguments, **keywords):
        self._refuse("create_image")
        return super().create_image(*arguments, **keywords)

    def terminate(self, instance_ids):
        self._refuse("terminate")
        return super().terminate(instance_ids)


class FakeSource:
    """The Forgejo reader: one protected branch, a few basis files and one archive."""

    def __init__(self):
        self.sha = SOURCE_SHA
        self.protected = True
        self.files = {"example.lock": LOCK_FILE}
        self.chunks = [ARCHIVE[:1000], ARCHIVE[1000:]]
        self.errors = {}
        self.calls = []
        self.on_chunk = None
        self.fail_after_first = None

    def branch(self, name):
        self.calls.append(("branch", name))
        if "branch" in self.errors:
            raise self.errors["branch"]
        return BranchHead(sha=self.sha, protected=self.protected)

    def raw_file(self, sha, path):
        self.calls.append(("raw_file", path))
        if "raw_file" in self.errors:
            raise self.errors["raw_file"]
        if path not in self.files:
            raise ObservationRefusal("IMAGE_BASIS_PATH_MISSING")
        return self.files[path]

    def archive(self, sha, max_bytes, deadline=None):
        self.calls.append(("archive", sha))
        if "archive" in self.errors:
            raise self.errors["archive"]
        for number, chunk in enumerate(self.chunks):
            if self.on_chunk is not None:
                self.on_chunk()
            yield chunk
            if number == 0 and self.fail_after_first is not None:
                raise self.fail_after_first


class Crash(BaseException):
    """A process that dies: nothing in the code under test may catch it."""


class CrashingState(ImageState):
    """A state file whose process dies the first time a named column is written."""

    def __init__(self, *arguments, crash_on=(), **keywords):
        super().__init__(*arguments, **keywords)
        self.crash_on = set(crash_on)

    def advance_build(self, build_id, *, state=None, **fields):
        for name in sorted(self.crash_on):
            if name in fields:
                self.crash_on.discard(name)
                raise Crash(name)
        return super().advance_build(build_id, state=state, **fields)


class Interrupted(BaseException):
    """An operator interrupt."""


def snapshot_tree(directory):
    """Every file below ``directory`` and its bytes."""
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(Path(directory).rglob("*"))
        if path.is_file()
    }


# -- the base case ---------------------------------------------------------------


class PipelineCase(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.state_dir = self.dir / "state"
        self.clock = Clock(NOW)
        self.world = World(self.clock)
        self.source = FakeSource()
        self.sleeps = []
        self.interrupt_after = None
        self.sessions = []
        self.source_sessions = 0
        self.seen = []
        self.addCleanup(self.assert_no_secrets)
        self.configure()

    # -- wiring ----------------------------------------------------------------
    def configure(self, toml=None, *, with_image=True, **keywords):
        """Write the deployment file, then build a pipeline on it."""
        self.config_path = write_config(
            self.dir,
            config={"deployment": DEPLOYMENT, "aws": {"launch_template_name": WORKER, "launch_template_version": PIN}},
        )
        if with_image:
            text = self.config_path.read_text(encoding="utf-8")
            self.config_path.write_text(text + (render_toml() if toml is None else toml), encoding="utf-8")
        self.restart(**keywords)

    def restart(self, *, state=None, script=SCRIPT):
        """A new process on the same files: a new state object and a new pipeline."""
        self.config = load(self.config_path)
        self.state = state or ImageState(self.state_dir, clock=self.clock)
        self.pipeline = ImagePipeline(
            self.config,
            self.config_path,
            aws_factory=self.aws_factory,
            source_factory=self.source_factory,
            state=self.state,
            clock=self.clock,
            sleep=self.sleep,
            script=script,
        )

    def aws_factory(self):
        session = WorldAws(self.world)
        self.sessions.append(session)
        return session

    def source_factory(self):
        self.source_sessions += 1
        return self.source

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock.advance(seconds)
        if self.interrupt_after is not None and len(self.sleeps) >= self.interrupt_after:
            self.interrupt_after = None
            raise Interrupted()

    # -- running ---------------------------------------------------------------
    def apply(self, **keywords):
        result = self.pipeline.run_pass(apply=True, **keywords)
        self.seen.append(json.dumps(result))
        return result

    def plan(self, **keywords):
        result = self.pipeline.plan(**keywords)
        self.seen.append(json.dumps(result))
        return result

    def refused(self, call, reason, *, detail_has=(), detail_lacks=()):
        """Run ``call``, expect a refusal with ``reason`` and return it."""
        with self.assertRaises(Refusal) as caught:
            call()
        refusal = caught.exception
        self.seen.append(json.dumps(refusal.as_dict()))
        self.assertEqual(refusal.reason, reason)
        for text in detail_has:
            self.assertIn(text, refusal.detail)
        for text in detail_lacks:
            self.assertNotIn(text, refusal.detail)
        return refusal

    # -- the state file --------------------------------------------------------
    def query(self, call):
        state = ImageState(self.state_dir, clock=self.clock)
        state.connect()
        try:
            return call(state)
        finally:
            state.close()

    def write(self, call):
        return self.query(call)

    def builds(self):
        return self.query(lambda state: state.builds(DEPLOYMENT))

    def only_build(self):
        rows = self.builds()
        self.assertEqual(len(rows), 1, rows)
        return rows[0]

    def control(self):
        return self.query(lambda state: state.control_get())

    def add_build(self, build_id, state="failed", *, age=5 * DAY, digest="c" * 64, **fields):
        """A build row of an earlier run, started ``age`` seconds ago."""
        created = NOW - age
        writer = ImageState(self.state_dir, clock=lambda: created)
        writer.connect()
        try:
            writer.insert_build(
                build_id=build_id,
                deployment=DEPLOYMENT,
                client_token="token-" + build_id,
                basis_digest=digest,
                source_sha=SOURCE_SHA,
                deadline_at=created + BUILD_DEADLINE,
            )
            writer.advance_build(build_id, state=None if state == "building" else state, **fields)
        finally:
            writer.close()

    def set_control(self, **fields):
        self.write(lambda state: state.control_set(**fields))

    def add_journal(self, *, old_pin, new_pin, status="complete"):
        def insert(state):
            row = state.journal_insert(
                kind="promotion",
                old_pin=old_pin,
                new_pin=new_pin,
                old_file_sha256="0" * 64,
                new_file_sha256="1" * 64,
                backup_name="deployment.toml.bak",
                backup_sha256="2" * 64,
                contained_pin=old_pin,
            )
            if status == "complete":
                state.journal_complete(row.id)

        self.write(insert)

    # -- the telemetry files ---------------------------------------------------
    def events(self, name=None):
        path = self.state_dir / image_pipeline.EVENTS_FILE
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [row for row in rows if name is None or row["event"] == name]

    def metrics(self):
        path = self.state_dir / image_pipeline.METRICS_FILE
        samples = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            match = re.fullmatch(r"(\w+)\{(.*)\} (\S+)", line)
            self.assertIsNotNone(match, line)
            labels = dict(re.findall(r'(\w+)="((?:[^"\\]|\\.)*)"', match.group(2)))
            samples[(match.group(1), tuple(sorted(labels.items())))] = float(match.group(3))
        return samples

    def metric(self, name, **labels):
        key = (name, tuple(sorted({"deployment": DEPLOYMENT, **labels}.items())))
        return self.metrics().get(key)

    # -- the build -------------------------------------------------------------
    def digest(self, files=None):
        warm = self.config.image.warm
        return image.basis_digest(files or self.source.files, warm.commands, warm.gate_command, SCRIPT)

    def start_build(self, **keywords):
        """Run the first pass. It starts a build and polls a blind console until its time is up."""
        result = self.apply(**keywords)
        self.assertEqual(result["decision"], "building", result)
        return result, self.only_build()

    def pin_current(self):
        """Tag the pin's image with the digest of the current inputs: no build is due."""
        self.world.images[ami(0xA1)]["Tags"] = tag_list({BASIS_DIGEST_TAG: self.digest()})

    # -- the secret scan -------------------------------------------------------
    def secrets(self):
        found = {*SENTINELS, TOKEN, CONSUMER_URL, SIGNATURE, "X-Amz-Signature"}
        found.update(self.world.urls)
        found.update(launch["UserData"] for launch in self.world.launches)
        found.update({"WARM_PAYLOAD_END", "export WARM_SRC_URL", "export WARM_SRC_SHA256"})
        return found

    def written_text(self):
        parts = list(self.seen)
        for name in (image_pipeline.EVENTS_FILE, image_pipeline.METRICS_FILE, image_pipeline.TELEMETRY_FILE):
            path = self.state_dir / name
            if path.exists():
                parts.append(path.read_text(encoding="utf-8"))
        database = self.state_dir / "image-state.sqlite"
        if database.exists():
            connection = sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True)
            try:
                parts.append("\n".join(connection.iterdump()))
            except sqlite3.Error:
                pass
            finally:
                connection.close()
        return "\n".join(parts)

    def assert_no_secrets(self):
        text = self.written_text()
        for secret in self.secrets():
            self.assertNotIn(secret, text)


class SessionTests(PipelineCase):
    def test_a_new_pipeline_touches_nothing(self):
        self.assertEqual(self.sessions, [])
        self.assertEqual(self.source_sessions, 0)
        self.assertEqual(self.world.calls, [])
        self.assertFalse(self.state_dir.exists())


class DisabledTests(PipelineCase):
    def check_disabled(self):
        self.assertEqual(self.pipeline.plan(), {"decision": "disabled"})
        self.assertEqual(self.pipeline.run_pass(apply=False), {"decision": "disabled"})
        self.assertEqual(self.pipeline.run_pass(apply=True), {"decision": "disabled"})
        self.assertEqual(self.pipeline.run_until_done(), {"decision": "disabled"})
        self.assertEqual(self.sessions, [])
        self.assertEqual(self.source_sessions, 0)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.world.calls, [])
        self.assertFalse(self.state_dir.exists())

    def test_without_an_image_table_every_entry_point_is_disabled(self):
        self.configure(with_image=False)
        self.check_disabled()

    def test_with_the_image_table_disabled_every_entry_point_is_disabled(self):
        self.configure(toml="[image]\nenabled = false\n")
        self.check_disabled()


class PrecheckTests(PipelineCase):
    def assert_nothing_touched(self):
        self.assertEqual(self.sessions, [])
        self.assertEqual(self.source_sessions, 0)
        self.assertEqual(self.world.calls, [])
        self.assertFalse(self.state_dir.exists())

    def test_a_missing_gate_is_refused_before_any_network_call(self):
        self.configure(toml=render_toml(gate=""))
        entry_points = {
            "plan": self.pipeline.plan,
            "pass without apply": lambda: self.pipeline.run_pass(apply=False),
            "pass": lambda: self.pipeline.run_pass(apply=True),
            "until done": self.pipeline.run_until_done,
        }
        for name, call in entry_points.items():
            with self.subTest(name):
                self.refused(call, "IMAGE_GATE_REQUIRED")
        self.assert_nothing_touched()

    def test_a_missing_gate_that_is_allowed_is_planned_and_reported(self):
        self.configure(toml=render_toml(gate="allow_no_gate = true"))
        result = self.plan()
        self.assertEqual(result["decision"], "start")
        self.assertEqual(result["gate"], {"configured": False, "allow_no_gate": True})

    def test_a_missing_script_is_refused_before_any_network_call(self):
        original = image.shipped_script
        absent = self.dir / "absent-warm-cache.sh"
        self.restart(script=None)
        with mock.patch.object(image, "shipped_script", lambda: original(absent)):
            self.refused(self.pipeline.plan, "IMAGE_SCRIPT_MISSING")
            self.refused(lambda: self.pipeline.run_pass(apply=True), "IMAGE_SCRIPT_MISSING")
        self.assert_nothing_touched()

    def test_the_shipped_script_is_used_when_none_is_given(self):
        original = image.shipped_script
        present = self.dir / "warm-cache.sh"
        present.write_bytes(SCRIPT)
        self.restart(script=None)
        with mock.patch.object(image, "shipped_script", lambda: original(present)):
            result = self.plan()
        self.assertEqual(result["script_sha256"], image.script_sha256(SCRIPT))


class PlanTests(PipelineCase):
    def assert_no_provider_call_before_the_provider(self):
        self.assertEqual(self.sessions, [])
        self.assertEqual(self.world.calls, [])

    def test_plan_reports_a_start_and_writes_nothing(self):
        before = snapshot_tree(self.dir)
        result = self.plan()
        self.assertEqual(snapshot_tree(self.dir), before)
        self.assertFalse(self.state_dir.exists())
        self.assertEqual(set(self.world.names()) - READS, set())
        self.assertEqual(len(self.sessions), 1)
        self.assertEqual(result["decision"], "start")
        self.assertIsNone(result["reason"])
        self.assertFalse(result["forced"])
        self.assertEqual(
            result["source"], {"branch": "main", "sha": SOURCE_SHA[:12], "protected": True, "basis_files": 1}
        )
        self.assertEqual(result["basis_digest"], self.digest()[:12])
        self.assertEqual(result["pin"], {"version": PIN, "image_id": ami(0xA1), "basis_digest": OLD_DIGEST[:12]})
        self.assertEqual(result["script_sha256"], image.script_sha256(SCRIPT))
        self.assertEqual(result["gate"], {"configured": True, "allow_no_gate": False})
        self.assertEqual(
            result["build_template"], {"name": BUILDER, "version": 1, "image_id": ami(0xB0), "volume_gib": 40}
        )
        self.assertEqual(result["builders"], [])
        self.assertFalse(result["state"]["present"])
        self.assertEqual(result["deletion"], {"items": []})

    def test_the_user_data_is_sized_with_a_url_as_long_as_a_real_one(self):
        result = self.plan()
        url = "https://" + "x" * (image_pipeline.SYNTHETIC_URL_CHARS - len("https://"))
        text = image.render_user_data(SCRIPT, image.warm_exports(self.config.image, url, "0" * 64))
        self.assertGreaterEqual(len(url), 4096)
        self.assertEqual(result["user_data"], {"bytes": len(text.encode("utf-8")), "limit": image.USER_DATA_LIMIT})

    def test_plan_leaves_the_files_of_an_earlier_run_alone(self):
        self.start_build()
        before = snapshot_tree(self.dir)
        mark = len(self.world.calls)
        result = self.plan()
        forced = self.plan(force=True)
        self.assertEqual(snapshot_tree(self.dir), before)
        self.assertEqual(set(self.world.names(mark)) - READS, set())
        # The build that is running also started less than a minimum interval ago,
        # and that condition comes first. A forced plan reaches the active build.
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_MIN_INTERVAL"))
        self.assertEqual((forced["decision"], forced["reason"]), ("refused", "IMAGE_BUILD_ALREADY_ACTIVE"))
        self.assertEqual(result["state"]["active_build"]["state"], "building")
        self.assertEqual(len(result["builders"]), 1)

    def test_a_pass_without_apply_is_the_plan(self):
        self.assertEqual(self.pipeline.run_pass(apply=False), self.plan())

    def test_force_shows_what_a_forced_run_would_decide(self):
        self.pin_current()
        self.assertEqual(self.plan()["decision"], "digest_unchanged")
        forced = self.plan(force=True)
        self.assertEqual(forced["decision"], "start")
        self.assertTrue(forced["forced"])
        self.assertEqual(self.pipeline.run_pass(apply=False, force=True), forced)

    def test_the_first_failing_condition_is_reported(self):
        self.pin_current()
        self.assertEqual((self.plan()["decision"], self.plan()["reason"]), ("digest_unchanged", None))

    def test_a_recent_build_is_a_minimum_interval_refusal(self):
        self.add_build("recent", "failed", age=600)
        result = self.plan()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_MIN_INTERVAL"))
        self.assertEqual(self.plan(force=True)["decision"], "start")

    def test_two_builds_in_a_day_are_a_daily_cap_refusal(self):
        self.configure(toml=render_toml(image_extra="min_build_interval_seconds = 3600"))
        self.add_build("first", "failed", age=3 * HOUR)
        self.add_build("second", "failed", age=2 * HOUR)
        result = self.plan(force=True)
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_DAILY_CAP"))

    def test_a_cooldown_is_a_failure_cooldown_refusal(self):
        self.set_control(consecutive_failures=1, cooldown_until=NOW + HOUR)
        result = self.plan()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_FAILURE_COOLDOWN"))
        self.assertEqual(result["state"]["control"]["cooldown_until"], NOW + HOUR)

    def test_a_blocked_row_is_refused_until_the_digest_changes(self):
        self.add_build("failed", "failed", digest=self.digest())
        self.set_control(consecutive_failures=3, blocked=True)
        result = self.plan()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_BLOCKED"))
        self.assertFalse(result["clear_control"])
        self.source.files = {"example.lock": b"a changed lock file\n"}
        changed = self.plan()
        self.assertEqual(changed["decision"], "start")
        self.assertTrue(changed["clear_control"])
        self.assertTrue(self.control().blocked)  # a plan persists nothing

    def test_a_builder_at_the_provider_is_an_active_build(self):
        self.world.add_instance(foreign_builder_tags())
        result = self.plan()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_BUILD_ALREADY_ACTIVE"))
        self.assertEqual(len(result["builders"]), 1)

    def test_the_forge_is_checked_before_the_provider(self):
        cases = {
            "IMAGE_BRANCH_UNPROTECTED": lambda: setattr(self.source, "protected", False),
            "IMAGE_BASIS_PATH_MISSING": lambda: setattr(self.source, "files", {}),
            "IMAGE_SOURCE_FORBIDDEN": lambda: self.source.errors.update(branch=ObservationRefusal("IMAGE_SOURCE_FORBIDDEN")),
        }
        for reason, arrange in cases.items():
            with self.subTest(reason):
                self.source = FakeSource()
                arrange()
                self.refused(self.pipeline.plan, reason)
                self.assert_no_provider_call_before_the_provider()

    def test_a_forbidden_file_or_archive_is_refused_before_the_provider(self):
        for step in ("raw_file", "archive"):
            with self.subTest(step):
                self.source = FakeSource()
                self.source.errors[step] = ObservationRefusal("IMAGE_SOURCE_FORBIDDEN", "HTTP 403")
                self.refused(self.pipeline.plan, "IMAGE_SOURCE_FORBIDDEN")
                self.assert_no_provider_call_before_the_provider()

    def test_a_script_that_makes_the_user_data_too_large_is_refused(self):
        script = b"".join(hashlib.sha256(str(number).encode()).digest() for number in range(2048))
        self.restart(script=script)
        self.refused(self.pipeline.plan, "IMAGE_USERDATA_TOO_LARGE")
        self.assertEqual(set(self.world.names()) - READS, set())

    def test_plan_shows_the_deletions_it_would_make_and_makes_none(self):
        self.add_build("old", "failed", reason_code="WARM_FAILED")
        self.world.add_image(ami(0x20), tags=owned("old"), age=2 * DAY, snapshots=(snap(0x20),))
        self.world.add_snapshot(snap(0x20), tags=owned("old"), age=2 * DAY)
        result = self.plan()
        rows = result["deletion"]["items"]
        self.assertEqual(
            [(row["action"], row["result"], row["image_id"]) for row in rows],
            [("deregister", "planned", ami(0x20)), ("delete_snapshot", "planned", ami(0x20))],
        )
        self.assertIn(ami(0x20), self.world.images)
        self.assertNotIn("deregister_image", self.world.names())
        self.assertNotIn("delete_snapshot", self.world.names())

    def test_plan_reports_a_deletion_resolution_that_was_refused(self):
        self.add_journal(old_pin=99, new_pin=PIN)  # a launch-template version that does not exist
        result = self.plan()
        self.assertEqual(result["deletion"], {"refused": "IMAGE_DELETION_UNSAFE"})


class BuildTemplateTests(PipelineCase):
    UNSAFE = {
        "IamInstanceProfile": (lambda data: data.update(IamInstanceProfile={"Name": "sentinel-profile"}), ("sentinel-profile",)),
        "InstanceMarketOptions": (lambda data: data.update(InstanceMarketOptions={"MarketType": "spot"}), ("spot",)),
        "MetadataOptions.HttpTokens": (lambda data: data["MetadataOptions"].update(HttpTokens="optional"), ("optional",)),
        "MetadataOptions.HttpPutResponseHopLimit": (
            lambda data: data["MetadataOptions"].update(HttpPutResponseHopLimit=2),
            ("2",),
        ),
        "InstanceInitiatedShutdownBehavior": (
            lambda data: data.update(InstanceInitiatedShutdownBehavior="stop"),
            ("stop",),
        ),
        "ImageId": (lambda data: data.pop("ImageId"), ()),
        "BlockDeviceMappings": (lambda data: data.pop("BlockDeviceMappings"), ()),
        "BlockDeviceMappings.Ebs.VolumeType": (
            lambda data: data["BlockDeviceMappings"][0]["Ebs"].update(VolumeType="gp2"),
            ("gp2",),
        ),
        "BlockDeviceMappings.Ebs.DeleteOnTermination": (
            lambda data: data["BlockDeviceMappings"][0]["Ebs"].update(DeleteOnTermination=False),
            ("False",),
        ),
    }

    def use_template(self, change):
        data = copy.deepcopy(SAFE_BUILDER)
        change(data)
        self.world.set_version(BUILDER, 1, data)

    def test_each_unsafe_property_is_refused_by_name_only(self):
        for name, (change, values) in self.UNSAFE.items():
            with self.subTest(name):
                self.use_template(change)
                refusal = self.refused(self.pipeline.plan, "IMAGE_BUILD_TEMPLATE_UNSAFE", detail_has=(name,))
                for value in values:
                    self.assertNotIn(value, refusal.detail)
                self.assertEqual(refusal.detail, "unsafe properties: " + name)

    def test_an_alias_or_a_boolean_is_not_a_safe_value(self):
        self.use_template(lambda data: data.update(ImageId="resolve:ssm:/example/image"))
        self.refused(self.pipeline.plan, "IMAGE_BUILD_TEMPLATE_UNSAFE", detail_has=("ImageId",))
        self.use_template(lambda data: data["MetadataOptions"].update(HttpPutResponseHopLimit=True))
        self.refused(self.pipeline.plan, "IMAGE_BUILD_TEMPLATE_UNSAFE", detail_has=("HttpPutResponseHopLimit",))

    def test_every_unsafe_property_is_named(self):
        def ruin(data):
            data["IamInstanceProfile"] = {"Name": "sentinel-profile"}
            data["MetadataOptions"]["HttpTokens"] = "optional"

        self.use_template(ruin)
        refusal = self.refused(self.pipeline.plan, "IMAGE_BUILD_TEMPLATE_UNSAFE")
        self.assertIn("IamInstanceProfile", refusal.detail)
        self.assertIn("MetadataOptions.HttpTokens", refusal.detail)

    def test_a_missing_template_or_version_is_refused(self):
        del self.world.versions[(BUILDER, 1)]
        self.refused(self.pipeline.plan, "IMAGE_BUILD_TEMPLATE_UNSAFE")
        del self.world.templates[BUILDER]
        self.refused(self.pipeline.plan, "IMAGE_BUILD_TEMPLATE_UNSAFE")

    def test_a_large_volume_is_reported_and_not_refused(self):
        self.use_template(lambda data: data["BlockDeviceMappings"][0]["Ebs"].update(VolumeSize=400))
        self.assertEqual(self.plan()["build_template"]["volume_gib"], 400)

    def test_an_unsafe_template_stops_a_pass_before_anything_is_staged(self):
        self.use_template(lambda data: data.update(InstanceInitiatedShutdownBehavior="stop"))
        self.refused(lambda: self.pipeline.run_pass(apply=True), "IMAGE_BUILD_TEMPLATE_UNSAFE")
        self.assertEqual(self.builds(), [])
        names = self.world.names()
        for forbidden in ("create_multipart_upload", "run_instances", "create_image"):
            self.assertNotIn(forbidden, names)
        self.assertFalse(self.world.objects)


class BuildFlowTests(PipelineCase):
    def image_tags(self):
        specs = self.world.arguments("create_image")[-1]["TagSpecifications"]
        return {spec["ResourceType"]: {tag["Key"]: tag["Value"] for tag in spec["Tags"]} for spec in specs}

    def test_the_first_pass_starts_a_build_and_polls_until_its_time_is_up(self):
        started = self.clock()
        result, build = self.start_build()
        launches = self.world.arguments("run_instances")
        self.assertEqual(len(launches), 1)
        launch = launches[0]
        self.assertEqual(launch["LaunchTemplate"], {"LaunchTemplateName": BUILDER, "Version": "1"})
        self.assertEqual(launch["ClientToken"], build.client_token)
        tags = {tag["Key"]: tag["Value"] for tag in launch["TagSpecifications"][0]["Tags"]}
        self.assertEqual(
            tags,
            {
                IMAGE_BUILD_TAG: "true",
                DEPLOYMENT_TAG: DEPLOYMENT,
                BUILD_ID_TAG: build.id,
                EXPIRES_TAG: str(int(started + BUILD_DEADLINE)),
            },
        )
        self.assertEqual(build.deadline_at, started + BUILD_DEADLINE)
        self.assertEqual(build.builder_id, self.world.launches[0]["InstanceId"])
        self.assertEqual(build.archive_key, f"{PREFIX}{build.id}.tar.gz")
        self.assertEqual(build.archive_sha256, ARCHIVE_SHA)
        self.assertEqual(self.world.objects[build.archive_key][0], ARCHIVE)
        self.assertEqual(build.basis_digest, self.digest())
        self.assertEqual(build.source_sha, SOURCE_SHA)
        self.assertEqual(build.script_sha256, image.script_sha256(SCRIPT))
        self.assertEqual(build.gate_configured, 1)
        self.assertEqual(result["build"]["id"], build.id)
        # One bounded pass: a read of the console every 30 seconds, and it ends in time.
        self.assertEqual(self.sleeps, [30] * 7)
        self.assertEqual(self.clock() - started, 210)
        self.assertLess(self.clock() - started, image_pipeline.PASS_DEADLINE_SECONDS)
        self.assertEqual(len(self.world.arguments("get_console_output")), 8)

    def test_the_user_data_carries_the_presigned_url_and_the_archive_hash(self):
        _, build = self.start_build()
        user_data = self.world.launches[0]["UserData"]
        (url,) = self.world.urls
        self.assertIn(shlex.quote(url), user_data)
        self.assertIn(f"export WARM_SRC_SHA256={ARCHIVE_SHA}", user_data)
        self.assertEqual(self.world.arguments("generate_presigned_url")[0]["ExpiresIn"], 1800)
        self.assertEqual(self.world.arguments("generate_presigned_url")[0]["Params"]["Key"], build.archive_key)

    def test_the_row_is_written_before_anything_is_staged(self):
        seen = []
        self.world.hooks["create_multipart_upload"] = lambda arguments: seen.append(
            [(row.state, row.archive_key, row.builder_id) for row in self.builds()]
        )
        self.start_build()
        self.assertEqual(seen, [[("building", None, None)]])

    def test_a_slow_staging_shortens_the_polling_and_the_pass_still_ends_in_time(self):
        started = self.clock()
        spent = []
        self.source.on_chunk = lambda: spent or (spent.append(True), self.clock.advance(100))
        self.start_build()
        self.assertEqual(self.sleeps, [30] * 3)
        self.assertEqual(self.clock() - started, 190)

    def test_the_pass_that_sees_the_marker_creates_the_image_before_any_other_step(self):
        self.world.console_plan = [(300.0, READY)]
        _, build = self.start_build()
        self.clock.advance(30)
        mark = len(self.world.calls)
        result = self.apply()
        names = self.world.names(mark)
        first = names.index("create_image")
        self.assertEqual(set(names[:first]) - READS, set())
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        creation = self.world.arguments("create_image")[0]
        self.assertEqual(creation["InstanceId"], build.builder_id)
        self.assertTrue(creation["NoReboot"])
        self.assertEqual(creation["Name"], f"ci-burst-{DEPLOYMENT}-{build.id}")
        row = self.only_build()
        expected = owned(
            build.id,
            **{
                BASIS_DIGEST_TAG: build.basis_digest,
                image_pipeline.SOURCE_SHA_TAG: SOURCE_SHA,
                image_pipeline.SCRIPT_SHA_TAG: image.script_sha256(SCRIPT),
                image_pipeline.BASE_IMAGE_TAG: ami(0xB0),
            },
        )
        self.assertEqual(self.image_tags(), {"image": expected, "snapshot": expected})
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(row.state, "candidate")

    def test_the_candidate_is_created_after_the_builder_ends_and_is_verified(self):
        self.world.console_plan = [(300.0, READY)]
        before = self.config_path.read_bytes()
        self.start_build()
        self.clock.advance(30)
        result = self.apply()
        build = self.only_build()
        names = self.world.names()
        self.assertLess(names.index("terminate_instances"), names.index("create_launch_template_version"))
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        creation = self.world.arguments("create_launch_template_version")
        self.assertEqual(len(creation), 1)
        self.assertEqual(creation[0]["SourceVersion"], str(PIN))
        self.assertEqual(creation[0]["ClientToken"], f"{build.client_token}-v")
        self.assertEqual(creation[0]["LaunchTemplateData"], {"ImageId": build.image_id})
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(build.state, "candidate")
        self.assertEqual(build.template_version, 5)
        self.assertEqual((build.gate_configured, build.gate_passed), (1, 1))
        self.assertEqual(build.image_state, "available")
        self.assertEqual(build.template_version_exists, 1)
        self.assertEqual(build.drift_result, "clean")
        self.assertEqual(build.verified_at, self.clock())
        self.assertEqual(build.source_pin, PIN)
        self.assertEqual(build.script_sha256, image.script_sha256(SCRIPT))
        self.assertEqual(self.world.objects, {})
        self.assertIn("delete_object", self.world.names())
        control = self.control()
        self.assertEqual((control.consecutive_failures, control.cooldown_until, control.blocked), (0, None, False))
        # Nothing was pinned: the version is inert and the file is the one that was written.
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(self.world.templates[WORKER]["DefaultVersionNumber"], 1)
        self.assertEqual(self.world.versions[(WORKER, PIN)]["LaunchTemplateData"]["ImageId"], ami(0xA1))

    def test_a_build_runs_across_passes_with_a_fresh_session_for_each(self):
        self.world.console_plan = [(100.0, WAITING), (300.0, READY)]
        result = self.pipeline.run_until_done()
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(len(self.sessions), 2)
        self.assertEqual(len({id(session) for session in self.sessions}), 2)
        self.assertEqual(set(self.sleeps), {image_pipeline.SUPERVISOR_SLEEP_SECONDS})
        for method in ("run_instances", "create_image", "create_launch_template_version"):
            self.assertEqual(len(self.world.arguments(method)), 1, method)
        self.assertEqual(self.only_build().state, "candidate")

    def test_the_marker_that_shows_in_the_last_poll_of_a_pass_still_creates_the_image(self):
        self.world.console_plan = [(210.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "imaging")
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        self.assertEqual(self.only_build().state, "imaging")
        self.assertEqual(self.apply()["decision"], "candidate")
        self.assertEqual(len(self.world.arguments("create_image")), 1)

    def test_a_marker_after_the_last_poll_is_seen_by_the_next_pass(self):
        self.world.console_plan = [(215.0, READY)]
        self.start_build()
        self.assertEqual(self.world.arguments("create_image"), [])
        result = self.apply()
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        self.assertEqual(result["decision"], "candidate")

    def test_nothing_is_built_while_the_digest_is_unchanged(self):
        self.pin_current()
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("digest_unchanged", None))
        self.assertEqual(self.builds(), [])
        self.assertEqual(self.control().last_check_at, self.clock())
        for forbidden in ("create_multipart_upload", "run_instances"):
            self.assertNotIn(forbidden, self.world.names())
        (event,) = self.events("image_check")
        self.assertEqual(event["decision"], "digest_unchanged")
        self.assertEqual(event["digest"], self.digest()[:12])
        self.assertEqual(event["pin_digest"], self.digest()[:12])
        self.assertEqual(event["pin_version"], PIN)
        self.assertEqual(event["source_sha"], SOURCE_SHA[:12])
        self.assertFalse(event["forced"])

    def test_force_applies_to_the_first_pass_only(self):
        self.pin_current()
        self.assertEqual(self.pipeline.run_until_done()["decision"], "digest_unchanged")
        self.world.console_plan = [(100.0, WAITING), (300.0, READY)]
        result = self.pipeline.run_until_done(force=True)
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual([event["forced"] for event in self.events("image_check")], [False, True, False])

    def test_a_clearing_is_persisted_even_when_the_start_is_refused(self):
        self.add_build("earlier", "failed", age=600, digest="d" * 64)
        self.set_control(consecutive_failures=3, blocked=True, cooldown_until=NOW + HOUR)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_MIN_INTERVAL"))
        control = self.control()
        self.assertFalse(control.blocked)
        self.assertIsNone(control.cooldown_until)
        self.assertEqual(control.consecutive_failures, 3)
        self.assertEqual(len(self.builds()), 1)

    def test_the_pin_is_observed_once_and_again_when_it_changes(self):
        self.pin_current()
        self.apply()
        first = self.query(lambda state: state.pin_last())
        self.assertEqual((first.pin_version, first.image_id), (PIN, ami(0xA1)))
        self.clock.advance(60)
        self.apply()
        self.assertEqual(self.query(lambda state: state.pin_last()), first)
        self.world.add_image(ami(0xA2), tags={BASIS_DIGEST_TAG: self.digest()}, age=DAY)
        self.world.set_version(WORKER, PIN, {"ImageId": ami(0xA2)})
        self.apply()
        second = self.query(lambda state: state.pin_last())
        self.assertEqual((second.pin_version, second.image_id), (PIN, ami(0xA2)))
        self.assertEqual(second.observed_at, self.clock())


UNFRAMED_URL_CONSOLE = console(
    f"WARM_SOURCE_SHA256 {ARCHIVE_SHA}",
    f"consumer printed {CONSUMER_URL}",
    "  | consumer step output line",
    "WARM FAIL consumer step 2",
)


class FailureTests(PipelineCase):
    def assert_failed(self, result, reason):
        self.assertEqual((result["decision"], result["reason"]), ("failed", reason), result)
        build = self.only_build()
        self.assertEqual((build.state, build.reason_code), ("failed", reason))
        (event,) = self.events("image_build_failed")
        self.assertEqual(event["reason"], reason)
        return build

    def test_a_consumer_failure_ends_the_build_and_shows_only_its_framed_tail(self):
        self.world.console_plan = [(60.0, UNFRAMED_URL_CONSOLE)]
        result = self.apply()
        build = self.assert_failed(result, "WARM_FAILED")
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        self.assertEqual(self.world.arguments("create_image"), [])
        self.assertEqual(self.world.objects, {})
        (event,) = self.events("image_build_failed")
        self.assertEqual(event["framed_tail"], "  | consumer step output line")
        self.assertEqual(event["duration_seconds"], 60.0)
        self.assertNotIn(CONSUMER_URL, json.dumps(event))
        control = self.control()
        self.assertEqual(control.consecutive_failures, 1)
        self.assertEqual(control.cooldown_until, NOW + 60 + 21_600)
        self.assertFalse(control.blocked)

    def test_a_marker_for_another_archive_ends_the_build_and_creates_no_image(self):
        self.world.console_plan = [(30.0, MISMATCH)]
        result = self.apply()
        build = self.assert_failed(result, "IMAGE_MARKER_DIGEST_MISMATCH")
        self.assertEqual(self.world.arguments("create_image"), [])
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])

    def test_a_console_that_stays_blind_fails_at_the_build_deadline(self):
        result = self.pipeline.run_until_done()
        build = self.assert_failed(result, "CONSOLE_BLIND")
        self.assertEqual(self.clock(), NOW + BUILD_DEADLINE)
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        self.assertEqual(len(self.sessions), 12)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)

    def test_a_console_that_never_shows_the_marker_fails_at_the_build_deadline(self):
        self.world.console_plan = [(10.0, WAITING)]
        result = self.pipeline.run_until_done()
        self.assert_failed(result, "MARKER_TIMEOUT")
        self.assertEqual(self.world.arguments("create_image"), [])

    def test_the_deadline_ends_the_build_in_the_pass_that_reaches_it(self):
        self.start_build()
        self.clock.advance(BUILD_DEADLINE)
        result = self.apply()
        self.assert_failed(result, "CONSOLE_BLIND")
        self.assertEqual(self.sleeps, [30] * 7)

    def test_a_builder_that_vanished_before_the_marker_ends_the_build(self):
        _, build = self.start_build()
        self.world.set_state(build.builder_id, "terminated")
        result = self.apply()
        self.assert_failed(result, "IMAGE_BUILDER_GONE")
        self.assertEqual(self.world.arguments("terminate_instances"), [])
        self.assertEqual(self.world.arguments("create_image"), [])

    def test_a_marker_from_a_builder_that_is_gone_creates_no_image(self):
        self.world.console_plan = [(215.0, READY)]
        _, build = self.start_build()
        self.world.set_state(build.builder_id, "terminated")
        self.clock.advance(10)
        result = self.apply()
        self.assert_failed(result, "IMAGE_BUILDER_GONE")
        self.assertEqual(self.world.arguments("create_image"), [])
        self.assertEqual(len(self.events("image_marker_seen")), 1)

    def test_a_builder_the_provider_does_not_list_yet_is_not_gone(self):
        self.world.hide_launches_for = 45.0
        result, _ = self.start_build()
        self.assertEqual(result["decision"], "building")
        self.assertEqual(self.world.arguments("terminate_instances"), [])

    def test_a_builder_that_stays_unlisted_past_the_grace_is_gone_and_ended_later(self):
        self.world.hide_launches_for = 90.0
        result = self.apply()
        build = self.assert_failed(result, "IMAGE_BUILDER_GONE")
        self.assertEqual(self.clock(), NOW + image_pipeline.BUILDER_VISIBILITY_GRACE_SECONDS)
        self.assertEqual(self.world.arguments("terminate_instances"), [])
        # The builder shows up after all. No row owns it, so the next pass ends it.
        self.clock.advance(60)
        result = self.apply()
        self.assertEqual(result["reconcile"]["builders_terminated"], 1)
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [self.world.launches[0]["InstanceId"]]}])
        self.assertEqual(self.only_build().id, build.id)

    def test_a_row_that_never_got_a_builder_fails_and_nothing_starts_in_the_same_pass(self):
        key = f"{PREFIX}crashed.tar.gz"
        self.world.objects[key] = (ARCHIVE, NOW - 300)
        self.add_build("crashed", "building", age=300, archive_key=key, archive_sha256=ARCHIVE_SHA)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("failed", "IMAGE_BUILDER_GONE"))
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.arguments("create_multipart_upload"), [])
        self.assertNotIn(key, self.world.objects)
        self.assertEqual(len(self.builds()), 1)

    def test_an_uncertain_launch_is_adopted_by_the_next_pass(self):
        self.world.ghost_launch = True
        self.world.fail("run_instances", ProviderError("RequestLimitExceeded"), times=1)
        result = self.apply()
        self.assertEqual(result["decision"], "building")
        self.assertIn("run_builder:uncertain", result["notes"])
        self.assertIsNone(self.only_build().builder_id)
        self.assertEqual(self.sleeps, [])
        self.assertEqual(self.events("image_build_started"), [])
        (ghost,) = self.world.instances
        result = self.apply()
        self.assertTrue(result["reconcile"]["builder_adopted"])
        self.assertEqual(self.only_build().builder_id, ghost)
        self.assertEqual(self.only_build().state, "building")
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        (event,) = self.events("image_reconciled")
        self.assertTrue(event["builder_adopted"])
        self.assertEqual(event["build_id"], self.only_build().id)

    def test_an_uncertain_launch_that_never_happened_fails_the_build_and_is_not_repeated(self):
        self.world.fail("run_instances", ProviderError("RequestLimitExceeded"), times=1)
        self.assertEqual(self.apply()["decision"], "building")
        result = self.apply()
        self.assert_failed(result, "IMAGE_BUILDER_GONE")
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.world.objects, {})

    def test_a_definite_refusal_of_the_launch_fails_the_build_at_once(self):
        self.world.refusals["run_builder"] = ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity")
        result = self.apply()
        self.assert_failed(result, "AWS_REFUSED")
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.sleeps, [])

    def test_an_archive_over_the_limit_fails_the_build_and_leaves_no_object(self):
        self.configure(render_toml(source_extra="max_archive_bytes = 1048576"))
        self.source.chunks = [b"a" * 700_000, b"b" * 700_000]
        result = self.apply()
        self.assert_failed(result, "IMAGE_ARCHIVE_TOO_LARGE")
        self.assertEqual(self.world.objects, {})
        self.assertEqual(len(self.world.arguments("abort_multipart_upload")), 1)
        self.assertEqual(self.world.arguments("run_instances"), [])

    def test_credentials_that_will_not_outlive_the_url_fail_the_build_and_drop_the_archive(self):
        self.world.credential_life = 600.0
        result = self.apply()
        build = self.assert_failed(result, "IMAGE_CREDENTIAL_SHORT")
        self.assertEqual(build.archive_sha256, ARCHIVE_SHA)
        self.assertEqual(self.world.objects, {})
        self.assertIn("delete_object", self.world.names())
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.urls, [])

    def test_user_data_over_the_limit_fails_the_build_before_the_launch(self):
        self.world.url_padding = 20_000
        result = self.apply()
        self.assert_failed(result, "IMAGE_USERDATA_TOO_LARGE")
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.objects, {})

    def test_a_builder_that_appears_before_the_launch_fails_the_build_and_is_ended_next_pass(self):
        appeared = []

        def appear(arguments):
            if not appeared:
                appeared.append(self.world.add_instance(foreign_builder_tags()))

        self.world.hooks["generate_presigned_url"] = appear
        result = self.apply()
        self.assert_failed(result, "IMAGE_BUILD_ALREADY_ACTIVE")
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.world.arguments("terminate_instances"), [])
        self.apply()
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": appeared}])

    def test_a_stage_that_outlasts_the_pass_fails_the_build_with_the_deadline_code(self):
        self.source.on_chunk = lambda: self.clock.advance(400)
        result = self.apply()
        self.assert_failed(result, "pass_deadline_exceeded")
        self.assertEqual(result["retention"], {"refused": "pass_deadline_exceeded"})
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.world.arguments("run_instances"), [])

    def test_a_forge_that_fails_during_the_stage_fails_the_build_by_its_code_alone(self):
        self.source.fail_after_first = ObservationRefusal("FORGEJO_UNAVAILABLE", "token=" + TOKEN)
        result = self.apply()
        self.assert_failed(result, "FORGEJO_UNAVAILABLE")
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.world.arguments("run_instances"), [])

    def test_a_drifted_version_fails_the_build_and_the_image_is_kept_for_a_day(self):
        self.world.console_plan = [(30.0, READY)]
        self.world.version_drift = {"InstanceType": "c6i.xlarge"}
        result = self.apply()
        build = self.assert_failed(result, "TEMPLATE_DRIFT")
        self.assertEqual(build.template_version, 5)
        self.assertEqual(build.drift_result, "TEMPLATE_DRIFT")
        self.assertIsNotNone(build.image_id)
        (event,) = self.events("image_build_failed")
        self.assertEqual(event["drift_fields"], "InstanceType")
        names = self.world.names()
        self.assertLess(names.index("terminate_instances"), names.index("create_launch_template_version"))
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        self.assertIn(build.image_id, self.world.images)
        self.assertEqual(self.world.objects, {})
        # Nothing was verified, and no pass pinned or promoted the version.
        self.assertIsNone(build.verified_at)
        self.assertEqual(self.world.templates[WORKER]["DefaultVersionNumber"], 1)
        # Past the hold the same rule that keeps the image removes it with its snapshot.
        self.pin_current()
        self.clock.advance(DAY + HOUR)
        self.apply()
        self.assertNotIn(build.image_id, self.world.images)
        self.assertEqual(self.world.snapshots, {})
        (pruned,) = self.events("image_pruned")
        self.assertEqual((pruned["deregistered"], pruned["snapshots_deleted"]), (1, 1))

    def test_an_image_that_fails_ends_the_build_and_the_failed_image_is_removed(self):
        self.world.console_plan = [(30.0, READY)]
        self.world.image_final_state = "failed"
        result = self.apply()
        build = self.assert_failed(result, "IMAGE_NOT_AVAILABLE")
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        self.assertEqual(self.world.arguments("create_launch_template_version"), [])
        self.assertNotIn(build.image_id, self.world.images)
        self.assertEqual(self.world.snapshots, {})
        self.assertEqual(self.world.objects, {})

    def test_an_image_that_stays_pending_fails_the_build_at_the_deadline(self):
        self.world.console_plan = [(30.0, READY)]
        self.world.image_final_state = "pending"
        result = self.pipeline.run_until_done()
        build = self.assert_failed(result, "IMAGE_NOT_AVAILABLE")
        self.assertGreaterEqual(self.clock(), NOW + BUILD_DEADLINE)
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        self.assertEqual(self.world.arguments("create_launch_template_version"), [])

    def test_a_version_that_could_not_be_created_is_asked_for_again_with_the_same_token(self):
        self.world.console_plan = [(30.0, READY)]
        self.world.fail("create_launch_template_version", ProviderError("RequestLimitExceeded"), times=1)
        result = self.apply()
        self.assertEqual(result["decision"], "imaging")
        self.assertIn("create_version:AWS_UNCERTAIN", result["notes"])
        build = self.only_build()
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        result = self.apply()
        self.assertEqual(result["decision"], "candidate")
        attempts = self.world.arguments("create_launch_template_version")
        self.assertEqual(len(attempts), 2)
        self.assertEqual({attempt["ClientToken"] for attempt in attempts}, {f"{build.client_token}-v"})
        self.assertEqual(self.world.next_version, 6)
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])

    def test_a_version_that_exists_but_could_not_be_read_back_is_not_created_twice(self):
        self.world.console_plan = [(30.0, READY)]
        self.world.omit_versions_once = {5}
        result = self.apply()
        self.assertEqual(result["decision"], "imaging")
        self.assertIn("create_version:AWS_INVENTORY_INCOMPLETE", result["notes"])
        result = self.apply()
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(self.only_build().template_version, 5)
        self.assertEqual(self.world.next_version, 6)
        self.assertEqual(len([key for key in self.world.versions if key[0] == WORKER]), 2)

    def test_a_read_back_that_raises_is_not_a_verdict_and_the_next_pass_asks_again_with_the_same_token(self):
        # The real adapter raises for a version it cannot read; it does not return a short list.
        self.world.console_plan = [(30.0, READY)]

        def break_the_read_back(arguments):
            del self.world.hooks["create_launch_template_version"]
            self.world.fail("describe_launch_template_versions", ProviderError("RequestLimitExceeded"), times=1)

        self.world.hooks["create_launch_template_version"] = break_the_read_back
        result = self.apply()
        self.assertEqual(result["decision"], "imaging")
        self.assertIn("create_version:AWS_UNCERTAIN", result["notes"])
        self.assertEqual(self.world.next_version, 6)
        build = self.only_build()
        self.assertEqual((build.state, build.template_version), ("imaging", None))
        result = self.apply()
        self.assertEqual(result["decision"], "candidate")
        build = self.only_build()
        self.assertEqual(build.template_version, 5)
        attempts = self.world.arguments("create_launch_template_version")
        self.assertEqual(len(attempts), 2)
        self.assertEqual({attempt["ClientToken"] for attempt in attempts}, {f"{build.client_token}-v"})
        self.assertEqual(self.world.next_version, 6)
        self.assertEqual(len([key for key in self.world.versions if key[0] == WORKER]), 2)

    def test_a_version_that_cannot_be_created_before_the_deadline_fails_the_build(self):
        self.world.console_plan = [(30.0, READY)]
        self.world.fail("create_launch_template_version", ProviderError("RequestLimitExceeded"))
        self.assertEqual(self.apply()["decision"], "imaging")
        self.clock.advance(BUILD_DEADLINE)
        result = self.apply()
        build = self.assert_failed(result, "AWS_UNCERTAIN")
        self.assertIsNone(build.verified_at)
        self.assertEqual(self.world.next_version, 5)

    def test_a_refused_termination_does_not_hide_the_failure_and_the_next_pass_ends_the_builder(self):
        self.world.console_plan = [(30.0, FAILED)]
        self.world.refusals["terminate"] = ProviderRefusal("AWS_UNCERTAIN", "ProviderError", uncertain=True)
        result = self.apply()
        build = self.assert_failed(result, "WARM_FAILED")
        self.assertIn("terminate:failed", result["notes"])
        del self.world.refusals["terminate"]
        result = self.apply()
        self.assertEqual(result["reconcile"]["builders_terminated"], 1)
        self.assertEqual(self.world.instances[build.builder_id]["State"]["Name"], "terminated")


class ControlTests(PipelineCase):
    """What a pass writes into the control row, and what the row then refuses."""

    FAST = "min_build_interval_seconds = 3600\nmax_builds_per_day = 6\nfailure_cooldown_seconds = 600\n"

    def failing_build(self):
        self.world.console_plan = [(30.0, FAILED)]
        result = self.apply()
        self.assertEqual(result["decision"], "failed", result)
        return result

    def test_each_failure_counts_and_blocks_at_the_limit_until_the_digest_changes(self):
        self.configure(render_toml(image_extra=self.FAST))
        self.failing_build()
        control = self.control()
        self.assertEqual((control.consecutive_failures, control.blocked), (1, False))
        self.assertEqual(control.cooldown_until, self.clock() + 600)
        self.clock.advance(2 * HOUR)
        self.failing_build()
        self.assertEqual(self.control().consecutive_failures, 2)
        self.assertEqual(self.events("image_blocked"), [])
        self.clock.advance(2 * HOUR)
        self.failing_build()
        control = self.control()
        self.assertEqual((control.consecutive_failures, control.blocked), (3, True))
        (event,) = self.events("image_blocked")
        self.assertEqual(event["consecutive_failures"], 3)
        self.assertEqual(self.metric("ci_burst_image_blocked"), 1.0)
        self.assertEqual(self.metric("ci_burst_image_consecutive_failures"), 3.0)
        # The same inputs stay refused, and a refusal starts nothing and changes nothing.
        self.clock.advance(DAY)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_BLOCKED"))
        self.assertEqual(len(self.builds()), 3)
        self.assertEqual(self.control().consecutive_failures, 3)
        # New inputs clear it, and a recovered build has one try: its failure blocks again.
        self.source.files["example.lock"] = b"another synthetic lock file\n"
        self.failing_build()
        self.assertEqual(len(self.builds()), 4)
        self.assertEqual(self.control().consecutive_failures, 4)
        self.assertTrue(self.control().blocked)
        self.assertEqual(len(self.events("image_blocked")), 2)

    def test_a_cooldown_refuses_a_new_build_until_it_ends(self):
        self.configure(render_toml(image_extra="min_build_interval_seconds = 3600\n"))
        self.failing_build()
        self.clock.advance(2 * HOUR)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_FAILURE_COOLDOWN"))
        self.assertEqual(len(self.builds()), 1)
        self.clock.advance(5 * HOUR)
        self.assertEqual(self.apply()["decision"], "failed")
        self.assertEqual(len(self.builds()), 2)

    def test_force_clears_a_block_and_starts_a_build_that_the_next_pass_resumes(self):
        self.configure(render_toml(image_extra="max_consecutive_failures = 1\nmin_build_interval_seconds = 3600\n"))
        self.failing_build()
        self.assertTrue(self.control().blocked)
        self.assertEqual(len(self.events("image_blocked")), 1)
        self.clock.advance(7 * HOUR)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_BLOCKED"))
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.world.console_plan = [(100.0, WAITING), (300.0, READY)]
        result = self.pipeline.run_until_done(force=True)
        self.assertEqual(result["decision"], "candidate")
        control = self.control()
        self.assertEqual((control.consecutive_failures, control.blocked, control.cooldown_until), (0, False, None))
        self.assertEqual(len(self.world.arguments("run_instances")), 2)

    def test_a_success_ends_the_count_and_the_cooldown(self):
        self.configure(render_toml(image_extra="min_build_interval_seconds = 3600\n"))
        self.failing_build()
        self.assertEqual(self.control().consecutive_failures, 1)
        self.clock.advance(7 * HOUR)
        self.world.console_plan = [(30.0, READY)]
        result = self.pipeline.run_until_done()
        self.assertEqual(result["decision"], "candidate")
        control = self.control()
        self.assertEqual((control.consecutive_failures, control.cooldown_until, control.blocked), (0, None, False))
        self.assertEqual(self.metric("ci_burst_image_consecutive_failures"), 0.0)

    def test_a_pass_that_finishes_writes_the_check_time_and_one_that_raises_does_not(self):
        self.pin_current()
        self.apply()
        self.assertEqual(self.control().last_check_at, self.clock())
        self.clock.advance(100)
        self.world.fail("describe_launch_template_versions", ProviderError("RequestLimitExceeded"))
        with self.assertRaises(ProviderRefusal):
            self.apply()
        self.assertEqual(self.control().last_check_at, self.clock() - 100)
        self.assertEqual(self.metric("ci_burst_image_last_run_timestamp_seconds"), self.clock())


class ResumeTests(PipelineCase):
    """A build outlives a pass, a supervisor and a process."""

    def crash_after(self, column):
        """Run a pass in a process that dies the first time ``column`` is written, then start a new one."""
        self.restart(state=CrashingState(self.state_dir, clock=self.clock, crash_on={column}))
        with self.assertRaises(Crash):
            self.apply()
        self.restart()

    def test_an_interrupted_supervisor_frees_the_lock_and_a_rerun_resumes_the_same_build(self):
        self.world.console_plan = [(100.0, WAITING), (300.0, READY)]
        self.interrupt_after = 3
        with self.assertRaises(Interrupted):
            self.pipeline.run_until_done()
        build = self.only_build()
        self.assertEqual(build.state, "building")
        self.assertIsNone(self.control().last_check_at)
        self.assertTrue((self.state_dir / image_pipeline.METRICS_FILE).exists())
        other = ImageState(self.state_dir, clock=self.clock)
        other.connect()
        try:
            other.acquire_writer()
            other.release_writer()
        finally:
            other.close()
        result = self.pipeline.run_until_done()
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(result["build"]["id"], build.id)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(len(self.world.arguments("create_image")), 1)

    def test_a_pass_that_holds_the_lock_makes_another_refuse(self):
        holder = ImageState(self.state_dir, clock=self.clock)
        holder.connect()
        try:
            holder.acquire_writer()
            self.refused(self.apply, "IMAGE_STATE_LOCKED")
        finally:
            holder.release_writer()
            holder.close()
        self.assertEqual(self.world.calls, [])
        self.assertEqual(self.apply()["decision"], "building")

    def test_a_crash_after_the_launch_is_adopted_and_the_launch_is_not_repeated(self):
        self.crash_after("builder_id")
        self.assertIsNone(self.only_build().builder_id)
        self.assertEqual(len(self.world.launches), 1)
        self.world.console_plan = [(100.0, WAITING), (300.0, READY)]
        result = self.pipeline.run_until_done()
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(self.only_build().builder_id, self.world.launches[0]["InstanceId"])
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(len(self.events("image_reconciled")), 1)

    def test_a_crash_after_the_image_was_created_is_adopted_and_no_second_image_is_made(self):
        self.world.console_plan = [(30.0, READY)]
        self.crash_after("image_id")
        build = self.only_build()
        self.assertEqual((build.state, build.image_id), ("building", None))
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        result = self.pipeline.run_until_done()
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        (event,) = self.events("image_reconciled")
        self.assertTrue(event["image_adopted"])
        (image_id,) = self.world.images.keys() - {ami(0xA1)}
        self.assertEqual(self.only_build().image_id, image_id)

    def test_a_crash_after_the_version_was_created_does_not_create_a_second(self):
        self.world.console_plan = [(30.0, READY)]
        self.crash_after("template_version")
        self.assertEqual(self.only_build().state, "imaging")
        self.assertEqual(self.world.next_version, 6)
        result = self.apply()
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(self.only_build().template_version, 5)
        self.assertEqual(self.world.next_version, 6)
        self.assertEqual(len(self.world.arguments("create_launch_template_version")), 2)

    def test_a_crash_in_a_failure_leaves_the_build_active_and_the_next_pass_ends_it(self):
        self.world.console_plan = [(30.0, FAILED)]
        self.crash_after("reason_code")
        self.assertEqual(self.only_build().state, "building")
        self.assertEqual(self.control().consecutive_failures, 0)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("failed", "WARM_FAILED"))
        self.assertEqual(self.control().consecutive_failures, 1)
        self.assertEqual(len(self.events("image_build_failed")), 1)


class ReconcileTests(PipelineCase):
    """What the first step of every pass finds and removes."""

    def test_a_builder_that_no_build_owns_is_ended_before_anything_starts(self):
        leaked = self.world.add_instance(foreign_builder_tags())
        other_deployment = self.world.add_instance(owned("other-build", deployment="other-deployment"))
        result = self.apply()
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [leaked]}])
        self.assertEqual(self.world.instances[other_deployment]["State"]["Name"], "running")
        self.assertEqual(result["reconcile"]["builders_terminated"], 1)
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_BUILD_ALREADY_ACTIVE"))
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.builds(), [])
        (event,) = self.events("image_reconciled")
        self.assertEqual(event["builders_terminated"], 1)
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="builder"), 1.0)
        # The next pass finds nothing to end and starts the build.
        self.assertEqual(self.apply()["decision"], "building")
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="builder"), 1.0)

    def test_a_builder_past_its_expiry_that_no_build_owns_is_ended_too(self):
        stale = self.world.add_instance(foreign_builder_tags(expires_at=NOW - HOUR))
        self.pin_current()
        self.apply()
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [stale]}])

    def test_the_builder_of_the_active_build_is_left_alone(self):
        _, build = self.start_build()
        result = self.apply()
        self.assertEqual(result["reconcile"]["builders_terminated"], 0)
        self.assertEqual(self.world.arguments("terminate_instances"), [])
        self.assertEqual(result["build"]["builder_id"], build.builder_id)

    def test_stray_archives_go_when_older_than_an_hour_and_the_active_one_stays(self):
        old, active = f"{PREFIX}old.tar.gz", f"{PREFIX}active.tar.gz"
        objects = {
            old: NOW - 2 * HOUR,
            f"{PREFIX}recent.tar.gz": NOW - 1800,
            f"{PREFIX}edge.tar.gz": NOW - 3600,
            f"{PREFIX}unknown.tar.gz": None,
            active: NOW - 2 * HOUR,
        }
        for key, modified in objects.items():
            self.world.objects[key] = (b"x", modified)
        builder = self.world.add_instance(foreign_builder_tags("active"))
        self.add_build("active", "building", age=600, archive_key=active, archive_sha256=ARCHIVE_SHA, builder_id=builder)
        result = self.apply()
        self.assertEqual(result["decision"], "building")
        self.assertEqual(set(self.world.objects), set(objects) - {old})
        self.assertEqual(self.world.arguments("delete_object"), [{"Bucket": BUCKET, "Key": old}])
        self.assertEqual(result["reconcile"]["archives_deleted"], 1)
        (event,) = self.events("image_reconciled")
        self.assertEqual(event["archives_deleted"], 1)
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="archive"), 1.0)

    def test_an_archive_that_cannot_be_deleted_is_reported_and_does_not_stop_the_pass(self):
        key = f"{PREFIX}old.tar.gz"
        self.world.objects[key] = (b"x", NOW - 2 * HOUR)
        self.world.fail("delete_object", ProviderError("RequestLimitExceeded"), times=1)
        self.pin_current()
        result = self.apply()
        self.assertEqual(result["reconcile"]["failures"], ["AWS_UNCERTAIN"])
        self.assertEqual(result["reconcile"]["archives_deleted"], 0)
        self.assertIn(key, self.world.objects)
        self.assertEqual(self.apply()["reconcile"]["archives_deleted"], 1)
        self.assertNotIn(key, self.world.objects)

    def test_the_images_of_a_failed_build_go_and_the_neighbours_stay(self):
        self.pin_current()
        self.add_build("failed-one", "failed", image_id=ami(0xD1))
        self.world.add_image(ami(0xD1), tags=owned("failed-one"), snapshots=(snap(0xD1),))
        self.world.add_snapshot(snap(0xD1), tags=owned("failed-one"))
        self.world.add_image(ami(0xD2), tags=owned("failed-one", deployment="other-deployment"), snapshots=(snap(0xD2),))
        self.world.add_snapshot(snap(0xD2), tags=owned("failed-one", deployment="other-deployment"))
        self.world.add_image(ami(0xD3), tags={"team": "synthetic"}, snapshots=(snap(0xD3),))
        result = self.apply()
        self.assertEqual(result["retention"]["deregistered"], 1)
        self.assertEqual(set(self.world.images), {ami(0xA1), ami(0xD2), ami(0xD3)})
        self.assertEqual(set(self.world.snapshots), {snap(0xD2)})
        (event,) = self.events("image_pruned")
        self.assertEqual((event["deregistered"], event["snapshots_deleted"]), (1, 1))
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="image"), 0.0)

    def test_an_old_image_with_no_row_that_is_not_usable_is_removed_and_counted_as_leaked(self):
        self.pin_current()
        self.world.add_image(
            ami(0xE1), tags=owned("nobody"), state="failed", age=2 * DAY, snapshots=(snap(0xE1),)
        )
        self.world.add_snapshot(snap(0xE1), tags=owned("nobody"), age=2 * DAY)
        self.apply()
        self.assertNotIn(ami(0xE1), self.world.images)
        self.assertEqual(self.world.snapshots, {})
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="image"), 1.0)
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="snapshot"), 1.0)
        self.assertEqual(self.builds(), [])

    def test_an_available_image_with_no_row_is_recorded_as_an_unverified_candidate_and_never_pinned(self):
        self.pin_current()
        before = self.config_path.read_bytes()
        tags = owned("orphan-build", **{BASIS_DIGEST_TAG: "e" * 64, image_pipeline.SOURCE_SHA_TAG: SOURCE_SHA})
        self.world.add_image(ami(0xC1), tags=tags, age=2 * HOUR, snapshots=(snap(0xC1),))
        self.world.add_snapshot(snap(0xC1), tags=owned("orphan-build"), age=2 * HOUR)
        result = self.apply()
        self.assertEqual(result["reconcile"]["images_recorded"], 1)
        row = self.query(lambda state: state.build("orphan-build"))
        self.assertEqual((row.state, row.reason_code, row.image_id), ("candidate", "IMAGE_ORPHAN_RECORDED", ami(0xC1)))
        self.assertEqual((row.basis_digest, row.source_sha), ("e" * 64, SOURCE_SHA))
        self.assertEqual(row.created_at, NOW - 2 * HOUR)
        self.assertIsNone(row.verified_at)
        self.assertIsNone(self.query(lambda state: state.verified_candidate(DEPLOYMENT)))
        self.assertEqual(self.world.arguments("create_launch_template_version"), [])
        self.assertEqual(self.config_path.read_bytes(), before)
        (event,) = self.events("image_reconciled")
        self.assertEqual(event["images_recorded"], 1)
        # It is recorded once, and the rule keeps a usable image.
        self.assertEqual(self.apply()["reconcile"]["images_recorded"], 0)
        self.assertEqual(len(self.builds()), 1)
        self.assertIn(ami(0xC1), self.world.images)
        self.assertEqual(self.metric("ci_burst_image_candidate_waiting_seconds"), 0.0)

    def test_an_image_that_is_not_available_yet_and_has_no_row_is_not_recorded(self):
        self.pin_current()
        self.world.image_final_state = "pending"
        self.world.add_image(ami(0xC2), tags=owned("orphan-pending"), state="pending", age=HOUR)
        self.apply()
        self.assertEqual(self.builds(), [])
        self.assertIn(ami(0xC2), self.world.images)

    def test_a_lost_state_file_ends_the_builder_and_cannot_start_a_second_build(self):
        self.start_build()
        (self.state_dir / "image-state.sqlite").unlink()
        self.restart()
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_BUILD_ALREADY_ACTIVE"))
        self.assertEqual(result["reconcile"]["builders_terminated"], 1)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.builds(), [])
        # The archive of the forgotten build is a stray object once it is an hour old.
        self.clock.advance(2 * HOUR)
        result = self.apply()
        self.assertEqual(result["reconcile"]["archives_deleted"], 1)
        self.assertEqual(result["decision"], "building")

    def test_a_restored_older_state_file_with_a_running_builder_picks_the_build_up_from_the_tags(self):
        self.world.console_plan = [(215.0, READY)]
        self.world.image_delay = 100_000.0
        _, build = self.start_build()
        saved = (self.state_dir / "image-state.sqlite").read_bytes()
        result = self.apply()
        self.assertEqual(result["decision"], "imaging")
        image_id = self.only_build().image_id
        self.assertIsNotNone(image_id)
        # The file goes back to the moment the builder was recorded. The builder still runs
        # and the image exists, but the file knows neither the image nor the new state.
        (self.state_dir / "image-state.sqlite").write_bytes(saved)
        self.restart()
        self.assertEqual((self.only_build().state, self.only_build().image_id), ("building", None))
        result = self.apply()
        self.assertTrue(result["reconcile"]["image_adopted"])
        self.assertEqual(result["reconcile"]["builders_terminated"], 0)
        restored = self.only_build()
        self.assertEqual((restored.id, restored.state, restored.image_id), (build.id, "imaging", image_id))
        self.assertEqual(self.world.instances[build.builder_id]["State"]["Name"], "running")
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        # Once the image is available the build ends as a candidate, with the one version.
        self.world.image_delay = 90.0
        self.assertEqual(self.apply()["decision"], "candidate")
        self.assertEqual(self.world.next_version, 6)


class RetentionTests(PipelineCase):
    """The deletion rule is called with the protected set a pass can resolve."""

    def stale_failed_build(self, build_id, image_number):
        self.add_build(build_id, "failed", image_id=ami(image_number))
        self.world.add_image(ami(image_number), tags=owned(build_id), snapshots=(snap(image_number),))
        self.world.add_snapshot(snap(image_number), tags=owned(build_id))

    def test_a_refusal_to_resolve_the_protected_set_deletes_nothing_and_fails_no_build(self):
        self.pin_current()
        self.stale_failed_build("failed-one", 0xD1)
        self.add_journal(old_pin=99, new_pin=PIN)
        result = self.apply()
        self.assertEqual(result["retention"], {"refused": "IMAGE_DELETION_UNSAFE"})
        self.assertEqual(result["decision"], "digest_unchanged")
        self.assertIn(ami(0xD1), self.world.images)
        self.assertEqual(self.world.arguments("deregister_image"), [])
        self.assertEqual(self.events("image_pruned"), [])
        control = self.control()
        self.assertEqual((control.consecutive_failures, control.cooldown_until, control.blocked), (0, None, False))

    def test_the_previous_pin_protects_its_image(self):
        self.pin_current()
        self.stale_failed_build("kept", 0xD1)
        self.stale_failed_build("removed", 0xD2)
        self.world.set_version(WORKER, PREVIOUS, {"ImageId": ami(0xD1)})
        self.add_journal(old_pin=PREVIOUS, new_pin=PIN)
        result = self.apply()
        self.assertEqual(result["retention"]["deregistered"], 1)
        self.assertIn(ami(0xD1), self.world.images)
        self.assertNotIn(ami(0xD2), self.world.images)

    def test_a_pending_promotion_protects_both_of_its_pins(self):
        self.pin_current()
        self.stale_failed_build("old", 0xD1)
        self.stale_failed_build("new", 0xD2)
        self.stale_failed_build("other", 0xD3)
        self.world.set_version(WORKER, PREVIOUS, {"ImageId": ami(0xD1)})
        self.world.set_version(WORKER, 4, {"ImageId": ami(0xD2)})
        self.add_journal(old_pin=PREVIOUS, new_pin=4, status="pending")
        result = self.apply()
        self.assertEqual(result["retention"]["deregistered"], 1)
        self.assertEqual(set(self.world.images), {ami(0xA1), ami(0xD1), ami(0xD2)})

    def test_the_newest_verified_candidate_is_protected_and_an_orphan_is_not_the_current_candidate(self):
        self.configure(render_toml(image_extra="retain = 2\nretain_min_age_seconds = 3600\n"))
        self.pin_current()
        rows = (("verified", 5 * DAY, True), ("orphan-old", 3 * DAY, False), ("orphan-new", 2 * DAY, False))
        for number, (name, age, verified) in enumerate(rows, start=0xF1):
            fields = {"verified_at": NOW - age + HOUR} if verified else {}
            self.add_build(name, "candidate", age=age, image_id=ami(number), **fields)
            self.world.add_image(ami(number), tags=owned(name), age=age, snapshots=(snap(number),))
            self.world.add_snapshot(snap(number), tags=owned(name), age=age)
        result = self.apply()
        self.assertEqual(result["retention"]["deregistered"], 1)
        self.assertEqual(set(self.world.images), {ami(0xA1), ami(0xF1), ami(0xF3)})

    def test_the_image_of_the_build_in_flight_is_never_removed(self):
        _, build = self.start_build()
        self.clock.advance(HOUR)
        self.world.add_image(ami(0xAB), tags=owned(build.id), state="pending", age=2 * DAY)
        self.apply()
        self.assertIn(ami(0xAB), self.world.images)

    def test_the_rule_runs_after_the_build_so_a_new_candidate_counts_toward_retention(self):
        self.configure(render_toml(image_extra="retain = 2\nretain_min_age_seconds = 3600\n"))
        for number, name in ((0xF1, "older"), (0xF2, "old")):
            age = (number - 0xF0) * DAY * 2
            self.add_build(name, "candidate", age=age, image_id=ami(number), verified_at=NOW - age + HOUR)
            self.world.add_image(ami(number), tags=owned(name), age=age)
        self.world.console_plan = [(30.0, READY)]
        result = self.pipeline.run_until_done()
        self.assertEqual(result["decision"], "candidate")
        # Three usable images against a retention of two: the oldest unprotected one goes.
        self.assertIn(result["build"]["image_id"], self.world.images)
        self.assertEqual(len([i for i in self.world.images if i != ami(0xA1)]), 2)


POLICY = Path(__file__).resolve().parents[1] / "deploy" / "iam" / "image-build-policy.json"
EVENT_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


class TelemetryTests(PipelineCase):
    def happy_path(self):
        self.world.console_plan = [(100.0, WAITING), (300.0, READY)]
        return self.pipeline.run_until_done()

    def test_a_build_writes_one_event_per_step_and_each_event_names_only_ids_digests_counts_and_codes(self):
        result = self.happy_path()
        build = self.only_build()
        events = self.events()
        self.assertEqual(
            [event["event"] for event in events],
            [
                "image_build_started",
                "image_check",
                "image_marker_seen",
                "image_created",
                "image_candidate_ready",
                "image_check",
            ],
        )
        for event in events:
            self.assertRegex(event["ts"], EVENT_TIME)
            self.assertEqual(event["deployment"], DEPLOYMENT)
        started, first_check, seen, created, ready, last_check = events
        self.assertEqual(
            {key: value for key, value in started.items() if key not in ("ts", "deployment", "event")},
            {
                "build_id": build.id,
                "builder_id": build.builder_id,
                "digest": build.basis_digest[:12],
                "source_sha": SOURCE_SHA[:12],
                "forced": False,
                "deadline_at": NOW + BUILD_DEADLINE,
            },
        )
        self.assertEqual(started["ts"], "2027-01-15T08:00:00Z")
        self.assertEqual((first_check["decision"], first_check["reason"]), ("building", None))
        self.assertEqual((seen["build_id"], seen["builder_id"], seen["latency_seconds"]), (build.id, build.builder_id, 300.0))
        self.assertEqual((created["image_id"], created["builder_id"]), (build.image_id, build.builder_id))
        self.assertEqual(
            (ready["image_id"], ready["template_version"], ready["digest"], ready["duration_seconds"]),
            (build.image_id, 5, build.basis_digest[:12], 390.0),
        )
        self.assertEqual((last_check["decision"], last_check["build_id"]), ("candidate", build.id))
        self.assertEqual((last_check["pin_version"], last_check["pin_digest"]), (PIN, OLD_DIGEST[:12]))
        # Only the pass that decided read the forge, so only its check names the inputs.
        self.assertEqual((first_check["digest"], first_check["source_sha"]), (build.basis_digest[:12], SOURCE_SHA[:12]))
        self.assertNotIn("digest", last_check)
        self.assertNotIn("source_sha", last_check)
        self.assertEqual(result["decision"], "candidate")

    def test_a_pass_that_finds_nothing_to_do_writes_one_check_event(self):
        self.pin_current()
        self.apply()
        (event,) = self.events()
        self.assertEqual((event["event"], event["decision"], event["reason"]), ("image_check", "digest_unchanged", None))

    def test_the_event_file_only_grows_and_holds_one_json_object_per_line(self):
        self.pin_current()
        self.apply()
        path = self.state_dir / image_pipeline.EVENTS_FILE
        first = path.read_bytes()
        self.clock.advance(60)
        self.apply()
        second = path.read_bytes()
        self.assertTrue(second.startswith(first))
        self.assertGreater(len(second), len(first))
        self.assertTrue(second.endswith(b"\n"))
        for line in second.decode("utf-8").splitlines():
            self.assertIsInstance(json.loads(line), dict)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_the_metrics_describe_a_candidate(self):
        self.happy_path()
        end = float(self.clock())
        self.assertEqual(self.metric("ci_burst_image_builds", state="candidate"), 1.0)
        for name in ("building", "imaging", "failed", "promoted"):
            self.assertEqual(self.metric("ci_burst_image_builds", state=name), 0.0, name)
        self.assertEqual(self.metric("ci_burst_image_last_build_timestamp_seconds", outcome="candidate"), end)
        self.assertIsNone(self.metric("ci_burst_image_last_build_timestamp_seconds", outcome="failed"))
        self.assertEqual(self.metric("ci_burst_image_build_duration_seconds"), 390.0)
        self.assertEqual(self.metric("ci_burst_image_console_latency_seconds"), 300.0)
        self.assertEqual(self.metric("ci_burst_image_consecutive_failures"), 0.0)
        self.assertEqual(self.metric("ci_burst_image_blocked"), 0.0)
        self.assertEqual(self.metric("ci_burst_image_candidate_waiting_seconds"), 0.0)
        self.assertEqual(self.metric("ci_burst_image_stale_seconds"), 10 * DAY + 390.0)
        for kind in image_pipeline.LEAK_KINDS:
            self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind=kind), 0.0, kind)
        self.assertEqual(self.metric("ci_burst_image_last_run_timestamp_seconds"), end)
        # A candidate that waits ages in the metric, and the file follows each pass.
        self.clock.advance(1000)
        self.pin_current()
        self.apply()
        self.assertEqual(self.metric("ci_burst_image_candidate_waiting_seconds"), 1000.0)
        self.assertEqual(self.metric("ci_burst_image_last_run_timestamp_seconds"), end + 1000)

    def test_the_metrics_describe_a_failure(self):
        self.world.console_plan = [(30.0, FAILED)]
        self.apply()
        self.assertEqual(self.metric("ci_burst_image_builds", state="failed"), 1.0)
        self.assertEqual(self.metric("ci_burst_image_builds", state="candidate"), 0.0)
        self.assertEqual(self.metric("ci_burst_image_last_build_timestamp_seconds", outcome="failed"), NOW + 30)
        self.assertIsNone(self.metric("ci_burst_image_last_build_timestamp_seconds", outcome="candidate"))
        self.assertEqual(self.metric("ci_burst_image_build_duration_seconds"), 30.0)
        self.assertEqual(self.metric("ci_burst_image_consecutive_failures"), 1.0)
        self.assertIsNone(self.metric("ci_burst_image_console_latency_seconds"))

    def test_the_first_metrics_of_a_new_deployment_have_no_build_series(self):
        self.pin_current()
        self.apply()
        self.assertEqual(self.metric("ci_burst_image_builds", state="building"), 0.0)
        self.assertIsNone(self.metric("ci_burst_image_build_duration_seconds"))
        self.assertIsNone(self.metric("ci_burst_image_last_build_timestamp_seconds", outcome="candidate"))
        self.assertEqual(self.metric("ci_burst_image_candidate_waiting_seconds"), 0.0)
        self.assertEqual(self.metric("ci_burst_image_stale_seconds"), 10 * DAY)

    def test_every_series_names_the_deployment_and_the_files_leave_no_temporary_file(self):
        self.pin_current()
        self.apply()
        text = (self.state_dir / image_pipeline.METRICS_FILE).read_text(encoding="utf-8")
        for line in text.splitlines():
            if line and not line.startswith("#"):
                self.assertIn(f'deployment="{DEPLOYMENT}"', line)
        names = sorted(path.name for path in self.state_dir.iterdir())
        self.assertEqual(
            names,
            sorted(
                [
                    "image-state.sqlite",
                    "image-state.sqlite.lock",
                    image_pipeline.EVENTS_FILE,
                    image_pipeline.METRICS_FILE,
                    image_pipeline.TELEMETRY_FILE,
                ]
            ),
        )

    def test_the_counters_survive_between_passes_and_a_damaged_counter_file_stops_nothing(self):
        self.world.add_instance(foreign_builder_tags())
        self.pin_current()
        self.apply()
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="builder"), 1.0)
        saved = json.loads((self.state_dir / image_pipeline.TELEMETRY_FILE).read_text(encoding="utf-8"))
        self.assertEqual(saved["leaked"]["builder"], 1)
        self.apply()
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="builder"), 1.0)
        (self.state_dir / image_pipeline.TELEMETRY_FILE).write_text("{not json", encoding="utf-8")
        self.apply()
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="builder"), 0.0)
        json.loads((self.state_dir / image_pipeline.TELEMETRY_FILE).read_text(encoding="utf-8"))

    def test_the_event_whitelist_refuses_a_value_that_is_not_an_id_a_digest_a_count_or_a_code(self):
        telemetry = image_pipeline._Telemetry(self.state_dir, DEPLOYMENT, self.clock)
        for value in (CONSUMER_URL, "https://example.invalid/?a=b", "a=b", "two\nlines", "x" * 201, {"a": 1}, ["a"]):
            with self.assertRaises(ValueError, msg=repr(value)):
                telemetry.event("image_check", decision=value)
        with self.assertRaises(ValueError):
            telemetry.event("image_check", framed_tail="  | plain text")
        with self.assertRaises(ValueError):
            telemetry.event("image_check", user_data="plain")
        self.assertFalse((self.state_dir / image_pipeline.EVENTS_FILE).exists())
        telemetry.event("image_check", decision="start", reason=None, forced=True, pin_version=3, build_id="b-1")
        self.assertEqual(len(self.events("image_check")), 1)

    def test_only_the_failed_build_event_may_carry_free_text(self):
        telemetry = image_pipeline._Telemetry(self.state_dir, DEPLOYMENT, self.clock)
        tail = "  | step 2 failed: see output"
        telemetry.event("image_build_failed", build_id="b-1", reason="WARM_FAILED", framed_tail=tail)
        (event,) = self.events("image_build_failed")
        self.assertEqual(event["framed_tail"], tail)
        with self.assertRaises(ValueError):
            telemetry.event("image_blocked", build_id="b-1", framed_tail=tail)


class SecretTests(PipelineCase):
    """A secret that a run handles reaches no file, no result and no refusal.

    Every test of this module ends with the same scan. These tests make sure the
    secrets are present in the run, so that a scan that finds nothing means something.
    """

    def test_the_presigned_url_and_the_user_data_exist_only_in_the_launch(self):
        self.world.console_plan = [(30.0, READY)]
        self.apply()
        (url,) = self.world.urls
        user_data = self.world.launches[0]["UserData"]
        self.assertIn(url, user_data)
        text = self.written_text()
        self.assertNotIn(url, text)
        self.assertNotIn(SIGNATURE, text)
        self.assertNotIn(user_data[:200], text)
        self.assertNotIn("WARM_SRC_URL", text)

    def test_provider_error_text_reaches_no_file_and_no_result(self):
        self.world.console_plan = [(30.0, READY)]
        self.world.fail("run_instances", ProviderError("RequestLimitExceeded"), times=1)
        self.world.fail("create_image", ProviderError("RequestLimitExceeded"), times=1)
        self.assertEqual(self.apply()["decision"], "building")
        self.world.ghost_launch = True
        self.world.fail("terminate_instances", ProviderError("RequestLimitExceeded"), times=1)
        self.apply()
        self.world.fail("describe_instances", ProviderError("RequestLimitExceeded"))
        self.refused(self.apply, "AWS_UNCERTAIN", detail_lacks=SENTINELS)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, self.written_text())

    def test_a_consumer_secret_on_an_unframed_line_reaches_nothing_even_when_the_build_succeeds(self):
        text = console(f"consumer printed {CONSUMER_URL}", f"WARM_SOURCE_SHA256 {ARCHIVE_SHA}", "IMAGE_READY")
        self.world.console_plan = [(30.0, text)]
        result = self.pipeline.run_until_done()
        self.assertEqual(result["decision"], "candidate")
        self.assertNotIn(CONSUMER_URL, self.written_text())

    def test_the_forge_token_stays_out_of_a_build_that_the_forge_failed(self):
        self.source.fail_after_first = ObservationRefusal("FORGEJO_UNAVAILABLE", "token=" + TOKEN)
        result = self.apply()
        self.assertEqual(result["reason"], "FORGEJO_UNAVAILABLE")
        self.assertNotIn(TOKEN, self.written_text())


class PolicyPinTests(PipelineCase):
    """The tags a pass sets are the tags the shipped policy allows."""

    def policy(self):
        return json.loads(POLICY.read_text(encoding="utf-8"))

    def allowed_keys(self, *sids):
        found = []
        for statement in self.policy()["Statement"]:
            if statement["Sid"] in sids:
                found.append(set(statement["Condition"]["ForAllValues:StringEquals"]["aws:TagKeys"]))
        self.assertEqual(len(found), len(sids), sids)
        return found

    def sent(self, method):
        specs = self.world.arguments(method)[0]["TagSpecifications"]
        return {tag["Key"] for spec in specs for tag in spec["Tags"]}

    def test_the_tags_of_the_image_and_its_snapshots_are_allowed_to_be_created(self):
        self.world.console_plan = [(30.0, READY)]
        self.apply()
        sent = self.sent("create_image")
        self.assertEqual(
            sent,
            {
                IMAGE_BUILD_TAG,
                DEPLOYMENT_TAG,
                BUILD_ID_TAG,
                BASIS_DIGEST_TAG,
                image_pipeline.SOURCE_SHA_TAG,
                image_pipeline.SCRIPT_SHA_TAG,
                image_pipeline.BASE_IMAGE_TAG,
            },
        )
        for allowed in self.allowed_keys("CreateOnlyTaggedImagesAndSnapshots", "TagOnlyOnCreation"):
            self.assertLessEqual(sent, allowed)

    def test_the_tags_of_the_builder_are_allowed_to_be_created(self):
        self.apply()
        sent = self.sent("run_instances")
        self.assertEqual(sent, {IMAGE_BUILD_TAG, DEPLOYMENT_TAG, BUILD_ID_TAG, EXPIRES_TAG})
        for allowed in self.allowed_keys("CreateOnlyTaggedBuilderResources", "TagOnlyOnCreation"):
            self.assertLessEqual(sent, allowed)


if __name__ == "__main__":
    unittest.main()
