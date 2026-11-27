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
import os
import re
import shlex
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from ci_burst import aws as aws_module, image, image_pipeline
from ci_burst.aws import (
    BASIS_DIGEST_TAG,
    BUILD_ID_TAG,
    DEPLOYMENT_TAG,
    EXPIRES_TAG,
    IMAGE_BUILD_TAG,
    READ_TIMEOUT_SECONDS,
    ImageAws,
    SecurityGroup,
)
from ci_burst.config import load
from ci_burst.errors import DeadlineRefusal, ObservationRefusal, ProviderRefusal, Refusal
from ci_burst.forgejo import BranchHead
from ci_burst.image_pipeline import (
    OWED_ARCHIVES_PER_PASS,
    OWED_DELETE_RESERVE_SECONDS,
    PASS_DEADLINE_SECONDS,
    STRAY_SWEEP_SECONDS,
    ImagePipeline,
)
from ci_burst.image_state import ImageState

from support import Clock, write_config
from test_aws import sdk_client_error
from test_image_aws import ACCOUNT, SENTINELS, ProviderError, tag_list

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
        "describe_security_groups",
        "list_objects_v2",
    }
)


def ami(number):
    return f"ami-{number:017x}"


def snap(number):
    return f"snap-{number:017x}"


def sg(number):
    return f"sg-{number:017x}"


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def console(*lines):
    return "\n".join(lines) + "\n"


WAITING = console("[    0.1] kernel boot", "cloud-init: running user data")
READY = console("cloud-init: running user data", f"WARM_SOURCE_SHA256 {ARCHIVE_SHA}", "IMAGE_READY")
FAILED = console(f"WARM_SOURCE_SHA256 {ARCHIVE_SHA}", "  | consumer step output line", "WARM FAIL consumer step 2")
MISMATCH = console("WARM_SOURCE_SHA256 " + "f" * 64, "IMAGE_READY")

BUILD_GROUP = sg(0xB5)
#: One inbound rule, with no address range: the check counts rules and reads nothing else.
INGRESS_RULE = {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22}

SAFE_BUILDER = {
    "ImageId": ami(0xB0),
    "InstanceType": "c6i.large",
    "InstanceInitiatedShutdownBehavior": "terminate",
    "SecurityGroupIds": [BUILD_GROUP],
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
NO_GATE = "allow_no_gate = true"
STATE_DIRECTORY = "/var/lib/example-image-state"


def render_toml(*, image_extra="", source_extra="", gate=GATE, prefix=PREFIX, state_directory=STATE_DIRECTORY):
    return IMAGE_TOML.format(
        state_directory=state_directory,
        image_extra=image_extra,
        source_extra=source_extra,
        gate=gate,
        builder=BUILDER,
        bucket=BUCKET,
        prefix=prefix,
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
    def _name_taken(self, name):
        """Whether an image of the account has this name, listed or not. A deregistered image frees its name."""
        return any(item.get("Name") == name for item in self.w.images.values())

    def _add_image(self, name, specifications):
        specs = {spec["ResourceType"]: self._tags(spec) for spec in specifications}
        image_id, snapshot_id = self.w.new_id("ami"), self.w.new_id("snap")
        self.w.add_image(image_id, tags=specs["image"], state="pending", age=0, snapshots=(snapshot_id,), name=name)
        self.w.add_snapshot(snapshot_id, tags=specs["snapshot"], age=0)
        if self.w.hide_images_for:
            for identifier in (image_id, snapshot_id):
                self.w.hidden_until[identifier] = self.w.clock() + self.w.hide_images_for
        return image_id

    def create_image(self, *, InstanceId, Name, NoReboot, TagSpecifications):
        try:
            self.w.enter(
                "create_image",
                dict(InstanceId=InstanceId, Name=Name, NoReboot=NoReboot, TagSpecifications=TagSpecifications),
            )
        except Exception:
            if self.w.ghost_image and InstanceId in self.w.instances and not self._name_taken(Name):
                self._add_image(Name, TagSpecifications)  # the request was sent and the answer was lost
            raise
        if InstanceId not in self.w.instances:
            raise ProviderError("InvalidInstanceID.NotFound")
        if self._name_taken(Name):
            # EC2 refuses a second image under a name that an image of the account already has.
            self.w.duplicate_refusals += 1
            raise ProviderError("InvalidAMIName.Duplicate")
        return {"ImageId": self._add_image(Name, TagSpecifications)}

    def describe_images(self, *, Owners=None, Filters=None, ImageIds=None, NextToken=None):
        self.w.enter("describe_images", dict(Owners=Owners, Filters=Filters, ImageIds=ImageIds, NextToken=NextToken))
        self.w.settle_images()
        items = [item for item in self.w.images.values() if not self.w.is_hidden(item["ImageId"])]
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
        items = [item for item in self.w.snapshots.values() if not self.w.is_hidden(item["SnapshotId"])]
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

    def describe_security_groups(self, *, GroupIds=None, Filters=None, NextToken=None):
        self.w.enter("describe_security_groups", dict(GroupIds=GroupIds, Filters=Filters, NextToken=NextToken))
        groups = list(self.w.groups.values())
        if GroupIds is not None:
            if any(group_id not in self.w.groups for group_id in GroupIds):
                raise ProviderError("InvalidGroup.NotFound")
            groups = [self.w.groups[group_id] for group_id in GroupIds]
        for entry in Filters or []:
            if entry["Name"] != "group-name":
                raise AssertionError(f"unexpected filter {entry['Name']}")
            groups = [group for group in groups if group["GroupName"] in entry["Values"]]
        return {"SecurityGroups": copy.deepcopy(groups)}

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
        request = (LaunchTemplateName, SourceVersion, LaunchTemplateData, VersionDescription)
        if ClientToken in self.w.version_tokens:
            # EC2 answers a token that comes back with another request with this error, and creates nothing.
            if self.w.version_requests[ClientToken] != request:
                raise ProviderError("IdempotentParameterMismatch")
            return {"LaunchTemplateVersion": {"VersionNumber": self.w.version_tokens[ClientToken]}}
        source = self.w.versions[(LaunchTemplateName, int(SourceVersion))]
        data = copy.deepcopy(source["LaunchTemplateData"])
        data.update(LaunchTemplateData)
        data.update(self.w.version_drift)
        number = self.w.next_version
        self.w.next_version += 1
        self.w.set_version(LaunchTemplateName, number, data, description=VersionDescription)
        self.w.version_tokens[ClientToken] = number
        self.w.version_requests[ClientToken] = copy.deepcopy(request)
        return {"LaunchTemplateVersion": {"VersionNumber": number}}


class WorldS3:
    """The S3 client calls the adapter makes. A call whose owner is not the bucket's owner is refused, as S3 does."""

    def __init__(self, world):
        self.w = world
        self.parts = {}

    def create_multipart_upload(self, *, Bucket, Key, ExpectedBucketOwner):
        self.w.enter("create_multipart_upload", dict(Bucket=Bucket, Key=Key, ExpectedBucketOwner=ExpectedBucketOwner))
        self.w.check_owner(Bucket, ExpectedBucketOwner)
        upload_id = self.w.new_id("upload")
        self.parts[upload_id] = []
        return {"UploadId": upload_id}

    def upload_part(self, *, Bucket, Key, UploadId, PartNumber, Body, ExpectedBucketOwner):
        self.w.enter(
            "upload_part",
            dict(
                Bucket=Bucket,
                Key=Key,
                UploadId=UploadId,
                PartNumber=PartNumber,
                Body=Body,
                ExpectedBucketOwner=ExpectedBucketOwner,
            ),
        )
        self.w.check_owner(Bucket, ExpectedBucketOwner)
        self.parts[UploadId].append(bytes(Body))
        return {"ETag": f"etag-{PartNumber}"}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload, ExpectedBucketOwner):
        self.w.enter(
            "complete_multipart_upload",
            dict(
                Bucket=Bucket,
                Key=Key,
                UploadId=UploadId,
                MultipartUpload=MultipartUpload,
                ExpectedBucketOwner=ExpectedBucketOwner,
            ),
        )
        self.w.check_owner(Bucket, ExpectedBucketOwner)
        self.w.objects[Key] = (b"".join(self.parts.pop(UploadId)), self.w.clock())
        return {}

    def abort_multipart_upload(self, *, Bucket, Key, UploadId, ExpectedBucketOwner):
        self.w.enter(
            "abort_multipart_upload",
            dict(Bucket=Bucket, Key=Key, UploadId=UploadId, ExpectedBucketOwner=ExpectedBucketOwner),
        )
        self.w.check_owner(Bucket, ExpectedBucketOwner)
        self.parts.pop(UploadId, None)
        return {}

    def delete_object(self, *, Bucket, Key, ExpectedBucketOwner):
        self.w.enter("delete_object", dict(Bucket=Bucket, Key=Key, ExpectedBucketOwner=ExpectedBucketOwner))
        self.w.check_owner(Bucket, ExpectedBucketOwner)
        self.w.objects.pop(Key, None)
        return {}

    def list_objects_v2(self, *, Bucket, Prefix, ExpectedBucketOwner, ContinuationToken=None):
        self.w.enter(
            "list_objects_v2",
            dict(
                Bucket=Bucket,
                Prefix=Prefix,
                ExpectedBucketOwner=ExpectedBucketOwner,
                ContinuationToken=ContinuationToken,
            ),
        )
        self.w.check_owner(Bucket, ExpectedBucketOwner)
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
        #: A failed ``CreateImage`` still makes the image when set: the request was sent and the answer was lost.
        self.ghost_image = False
        #: A new image and its snapshot are not listed for this long, but they already hold their name.
        self.hide_images_for = 0.0
        self.duplicate_refusals = 0
        self.templates = {}
        self.versions = {}
        self.version_tokens = {}
        self.version_requests = {}
        self.version_drift = {}
        self.omit_versions_once = set()
        self.groups = {}
        self.next_version = 5
        self.objects = {}
        #: The account that the image identity runs in, and the owner of a bucket that is not listed.
        self.account = ACCOUNT
        #: A bucket that another account owns: bucket name -> account.
        self.bucket_owners = {}
        self.credential_life = float(HOUR)
        self.url_padding = 0
        self.urls = []
        self.ec2 = WorldEc2(self)
        self.s3 = WorldS3(self)
        self.seed()

    def check_owner(self, bucket, expected):
        """S3 answers ``AccessDenied`` to a call that names an owner other than the bucket's."""
        if expected != self.bucket_owners.get(bucket, self.account):
            raise ProviderError("AccessDenied")

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
        self.add_group(BUILD_GROUP, "example-build-group")
        self.add_image(ami(0xA1), tags={BASIS_DIGEST_TAG: OLD_DIGEST}, age=10 * DAY)

    def set_version(self, name, number, data, description=""):
        self.versions[(name, number)] = {
            "VersionNumber": number,
            "LaunchTemplateId": self.templates[name]["LaunchTemplateId"],
            "VersionDescription": description,
            "LaunchTemplateData": data,
        }

    def add_group(self, group_id, name, *, ingress=0):
        """A security group with ``ingress`` inbound rules."""
        self.groups[group_id] = {
            "GroupId": group_id,
            "GroupName": name,
            "IpPermissions": [dict(INGRESS_RULE) for _ in range(ingress)],
        }

    def add_image(self, image_id, *, tags=None, state="available", age=3 * DAY, snapshots=(), name=None):
        item = {
            "ImageId": image_id,
            "State": state,
            "Tags": tag_list(tags or {}),
            "BlockDeviceMappings": [{"Ebs": {"SnapshotId": snapshot_id}} for snapshot_id in snapshots],
            "_created": self.clock() - age,
        }
        if name is not None:
            item["Name"] = name
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

    def is_hidden(self, identifier):
        """Whether an instance, an image or a snapshot exists but is not listed yet."""
        return self.clock() < self.hidden_until.get(identifier, 0.0)

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
            account=world.account,
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

    def terminate(self, instance_ids, *, deadline):
        self._refuse("terminate")
        return super().terminate(instance_ids, deadline=deadline)


class WorldSts:
    """``GetCallerIdentity`` of the world: the answer takes ``seconds`` of the fake clock, as a slow STS does.

    ``error``, when set, is raised once in place of the answer, after the same ``seconds``.
    """

    def __init__(self, world, seconds):
        self.w = world
        self.seconds = seconds
        self.error = None

    def get_caller_identity(self):
        self.w.enter("get_caller_identity", {})
        self.w.clock.advance(self.seconds)
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        return {"Account": self.w.account}


class UncachedAccountAws(ImageAws):
    """The real adapter on the world's clients, handed no account: its first ``account_id`` is a call to ``sts``.

    ``WorldAws`` injects the account, so a pass never waits for it. Here the first use of the account, which can be the
    owner check of an owed delete, pays for the lookup, and the adapter caches the answer as it does in a real pass.
    """

    def __init__(self, world, sts):
        super().__init__(
            REGION,
            PROFILE,
            clients={"ec2": world.ec2, "s3": world.s3, "sts": sts},
            clock=world.clock,
            expiry=world.clock() + world.credential_life,
        )


class SlowClientSession:
    """The session of an adapter that has no S3 client yet: building the first one takes ``seconds`` of the clock.

    ``sts`` is the STS client that it builds, for an adapter that has no account either.
    """

    def __init__(self, world, seconds, sts=None):
        self.w = world
        self.seconds = seconds
        self.sts = sts
        self.built = []

    def client(self, service, region_name=None, config=None):
        self.built.append(service)
        self.w.clock.advance(self.seconds)
        return {"ec2": self.w.ec2, "s3": self.w.s3, "sts": self.sts}[service]


class LazyClientAws(ImageAws):
    """The real adapter on the world's EC2 client and no S3 client: its first S3 call builds one, from ``session``.

    ``WorldAws`` hands the adapter both clients, so a pass never waits to build one. Here the first S3 call, which can
    be an owed delete, pays for the client, as it does in a real pass.
    """

    def __init__(self, world, session):
        super().__init__(
            REGION,
            PROFILE,
            clients={"ec2": world.ec2},
            clock=world.clock,
            expiry=world.clock() + world.credential_life,
            account=world.account,
            session_factory=lambda profile: session,
        )


class SlowLookupAws(WorldAws):
    """The real adapter on the world's clients, whose lookups of the credential's expiry can take time.

    ``WorldAws`` hands the adapter a fixed expiry, so no lookup of it ever waits. A session that assumes a role does
    wait: its credentials are resolved, and refreshed, when they are first read. ``stalls`` maps the number of a lookup
    in the pass (1 is the first) to the seconds of the clock that it takes. The answer is the expiry that the adapter
    began with, so the credential has less life left after a stall. ``began`` holds the clock reading at which each
    stalled lookup began, and ``lookups`` counts the lookups.
    """

    def __init__(self, world, stalls):
        super().__init__(world)
        self.stalls = stalls
        self.lookups = 0
        self.began = []

    def credential_expiry(self, *, deadline):
        self.lookups += 1
        seconds = self.stalls.get(self.lookups, 0)
        if seconds:
            self.began.append(self.world.clock())
            self.world.clock.advance(seconds)
        return super().credential_expiry(deadline=deadline)


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
        self.on_read = None
        self.fail_after_first = None

    def branch(self, name):
        self.calls.append(("branch", name))
        if "branch" in self.errors:
            raise self.errors["branch"]
        return BranchHead(sha=self.sha, protected=self.protected)

    def raw_file(self, sha, path):
        self.calls.append(("raw_file", path))
        if self.on_read is not None:
            self.on_read()
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


class CommitSource(FakeSource):
    """A forge whose files depend on the commit that is read: ``at`` maps a commit to the files it holds.

    A commit that ``at`` does not name holds ``files``. ``reads`` lists every ``(commit, path)`` that was read.
    """

    def __init__(self):
        super().__init__()
        self.at = {}
        self.reads = []

    def raw_file(self, sha, path):
        self.reads.append((sha, path))
        files, self.files = self.files, self.at.get(sha, self.files)
        try:
            return super().raw_file(sha, path)
        finally:
            self.files = files


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


class BuildEndedBeforeTheLock(ImageState):
    """A state file whose active build ends, in another process, after the peek and before the writer lock is taken."""

    def acquire_writer(self):
        if self._lock_fd is None:
            other = ImageState(self.directory, clock=self._clock)  # the other process
            try:
                other.connect()
                active = other.active_build(DEPLOYMENT)
                if active is not None:
                    other.advance_build(active.id, state="failed", reason_code="CONSOLE_BLIND")
            finally:
                other.close()
        return super().acquire_writer()


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
        """A build row of an earlier run, started ``age`` seconds ago.

        A row that names an archive was staged by the identity of the world, unless the test says another owner.
        """
        if fields.get("archive_key") and "archive_owner" not in fields:
            fields["archive_owner"] = ACCOUNT
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
        strip = self.config.image.source.strip_components
        return image.basis_digest(files or self.source.files, warm.commands, warm.gate_command, strip, SCRIPT)

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

    # The gate requirement guards a new launch. An applying pass that finds a build in flight settles it (see the
    # tests of the gate in ``FailureTests`` and ``GateEvidenceTests``), so only an applying pass looks at the state.

    def test_a_missing_gate_is_refused_before_any_client_when_the_state_has_no_build_in_flight(self):
        # The state file exists and holds only an ended build: the look at it is read-only and ends the pass.
        self.add_build("earlier", "failed")
        self.configure(toml=render_toml(gate=""))
        before = snapshot_tree(self.state_dir)
        entry_points = {"pass": lambda: self.pipeline.run_pass(apply=True), "until done": self.pipeline.run_until_done}
        for name, call in entry_points.items():
            with self.subTest(name):
                self.refused(call, "IMAGE_GATE_REQUIRED")
        self.assertEqual(self.sessions, [])
        self.assertEqual(self.source_sessions, 0)
        self.assertEqual(self.world.calls, [])
        self.assertEqual(snapshot_tree(self.state_dir), before)
        # The look took no writer lock and left no connection open.
        self.assertFalse(self.state.available)
        other = ImageState(self.state_dir, clock=self.clock)
        other.connect()
        self.addCleanup(other.close)
        other.acquire_writer()

    def test_a_missing_gate_is_still_refused_by_plan_when_a_build_is_in_flight(self):
        # A build in flight is settled by an applying pass only: plan decides nothing about it.
        self.add_build("stuck", "building", age=600)
        self.configure(toml=render_toml(gate=""))
        before = snapshot_tree(self.state_dir)
        entry_points = {"plan": self.pipeline.plan, "pass without apply": lambda: self.pipeline.run_pass(apply=False)}
        for name, call in entry_points.items():
            with self.subTest(name):
                self.refused(call, "IMAGE_GATE_REQUIRED")
        self.assertEqual((self.sessions, self.source_sessions, self.world.calls), ([], 0, []))
        self.assertEqual(snapshot_tree(self.state_dir), before)

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
        # Every automated launch exports the owner of the bucket too (``WARM_SRC_OWNER``, 12 digits).
        text = image.render_user_data(SCRIPT, image.warm_exports(self.config.image, url, "0" * 64, "0" * 12))
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

    def test_plan_reads_a_state_that_the_caller_opened_for_writing_and_leaves_it_open_and_unwritten(self):
        self.start_build()
        self.state.connect()
        self.addCleanup(self.state.close)
        before = snapshot_tree(self.dir)
        result = self.plan()
        self.assertEqual(snapshot_tree(self.dir), before)
        self.assertTrue(self.state.available)
        self.assertTrue(result["state"]["present"])
        self.assertEqual(result["state"]["active_build"]["state"], "building")
        # It is the caller's connection, still open for writing.
        self.assertTrue(self.state.control_set(blocked=1).blocked)

    def test_plan_reads_a_state_that_the_caller_opened_read_only_and_leaves_it_open(self):
        self.start_build()
        self.assertTrue(self.state.connect_readonly())
        self.addCleanup(self.state.close)
        result = self.plan()
        self.assertTrue(self.state.available)
        self.assertTrue(result["state"]["present"])
        self.assertEqual(result["state"]["active_build"]["state"], "building")
        with self.assertRaises(sqlite3.OperationalError):
            self.state.control_set(blocked=1)

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

    def test_a_changed_strip_components_makes_a_build_due(self):
        # The pin's image was built under strip_components 0, so its tag holds the digest of that layout.
        self.configure(render_toml(source_extra="strip_components = 0"))
        self.pin_current()
        self.assertEqual(self.plan()["decision"], "digest_unchanged")
        self.configure(render_toml(source_extra="strip_components = 1"))
        result = self.plan()
        self.assertEqual((result["decision"], result["reason"]), ("start", None))

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

    def test_a_template_that_names_no_security_group_is_refused(self):
        self.use_template(lambda data: data.pop("SecurityGroupIds"))
        refusal = self.refused(self.pipeline.plan, "IMAGE_BUILD_TEMPLATE_UNSAFE")
        self.assertEqual(refusal.detail, "unsafe properties: security_groups")
        # No group is named, so there is none to read.
        self.assertEqual(self.world.arguments("describe_security_groups"), [])
        for empty in ([], None, [""], "sg-not-a-list"):
            with self.subTest(empty=empty):
                self.use_template(lambda data: data.update(SecurityGroupIds=empty))
                self.refused(self.pipeline.plan, "IMAGE_BUILD_TEMPLATE_UNSAFE", detail_has=("security_groups",))

    def test_a_group_with_an_ingress_rule_is_refused_by_the_property_name_only(self):
        self.world.add_group(BUILD_GROUP, "example-build-group", ingress=1)
        refusal = self.refused(self.pipeline.plan, "IMAGE_BUILD_TEMPLATE_UNSAFE")
        self.assertEqual(refusal.detail, "unsafe properties: security_groups")
        for value in (BUILD_GROUP, "example-build-group", "tcp", "22"):
            self.assertNotIn(value, refusal.detail)

    def test_a_group_without_an_ingress_rule_is_safe_and_is_read_once_by_id(self):
        self.plan()
        self.assertEqual(
            self.world.arguments("describe_security_groups"),
            [{"GroupIds": [BUILD_GROUP], "Filters": None, "NextToken": None}],
        )

    def test_every_group_the_template_names_is_checked_in_each_of_the_three_places(self):
        clean, open_group = sg(0xC1), sg(0xC2)
        self.world.add_group(clean, "example-clean-group")
        self.world.add_group(open_group, "example-open-group", ingress=1)

        def top_level(*groups):
            return lambda data: data.update(SecurityGroupIds=list(groups))

        def by_name(*groups):
            def change(data):
                data.pop("SecurityGroupIds")
                data["SecurityGroups"] = [self.world.groups[group]["GroupName"] for group in groups]

            return change

        def in_interface(*groups):
            def change(data):
                data.pop("SecurityGroupIds")
                data["NetworkInterfaces"] = [{"DeviceIndex": 0, "Groups": list(groups)}]

            return change

        for place, put in (("SecurityGroupIds", top_level), ("SecurityGroups", by_name), ("NetworkInterfaces", in_interface)):
            with self.subTest(place=place, groups="clean"):
                self.use_template(put(clean))
                self.assertEqual(self.plan()["build_template"]["name"], BUILDER)
            with self.subTest(place=place, groups="open"):
                self.use_template(put(open_group))
                self.refused(self.pipeline.plan, "IMAGE_BUILD_TEMPLATE_UNSAFE", detail_has=("security_groups",))
            with self.subTest(place=place, groups="one clean and one open"):
                self.use_template(put(clean, open_group))
                self.refused(self.pipeline.plan, "IMAGE_BUILD_TEMPLATE_UNSAFE", detail_has=("security_groups",))

    def test_groups_named_in_two_places_are_all_checked(self):
        open_group = sg(0xC2)
        self.world.add_group(open_group, "example-open-group", ingress=1)

        def mixed(data):
            data["SecurityGroups"] = ["example-open-group"]

        self.use_template(mixed)
        self.refused(self.pipeline.plan, "IMAGE_BUILD_TEMPLATE_UNSAFE", detail_has=("security_groups",))

    def test_a_network_interface_without_a_group_is_unsafe(self):
        # The interface would take the default group of the VPC, which this check never read.
        for interfaces in (
            [{"DeviceIndex": 0}],
            [{"DeviceIndex": 0, "Groups": []}],
            [{"DeviceIndex": 0, "NetworkInterfaceId": "eni-0123456789abcdef0"}],
            [{"DeviceIndex": 0, "Groups": [BUILD_GROUP]}, {"DeviceIndex": 1}],
            ["not-a-mapping"],
        ):
            with self.subTest(interfaces=interfaces):
                self.use_template(lambda data: data.update(NetworkInterfaces=interfaces))
                self.refused(self.pipeline.plan, "IMAGE_BUILD_TEMPLATE_UNSAFE", detail_has=("security_groups",))

    def test_a_named_group_that_the_account_does_not_list_is_unsafe(self):
        # A name that matches no group gives an empty answer, and an empty answer is not "no ingress".
        self.use_template(lambda data: data.update(SecurityGroupIds=[], SecurityGroups=["example-missing-group"]))
        self.refused(self.pipeline.plan, "IMAGE_BUILD_TEMPLATE_UNSAFE", detail_has=("security_groups",))

    def test_a_group_id_that_the_answer_leaves_out_is_unsafe_even_when_the_provider_raised_no_error(self):
        # EC2 fails the whole request for an id it does not know, so the fake account cannot answer like this. The
        # check still reads the answer and not the absence of an error: a group it was not told about is unknown.
        listed = SecurityGroup(group_id=sg(0xA1), name="example-listed", ingress_rules=0)
        self.assertFalse(image_pipeline._admits_inbound([sg(0xA1)], [], [listed]))
        self.assertTrue(image_pipeline._admits_inbound([sg(0xA1), sg(0xA2)], [], [listed]))
        self.assertTrue(image_pipeline._admits_inbound([sg(0xA2)], [], [listed]))
        self.assertTrue(image_pipeline._admits_inbound([sg(0xA1)], [], [SecurityGroup(sg(0xA1), "example-listed", 1)]))

    def test_a_describe_that_fails_fails_closed_and_names_no_provider_text(self):
        for error in (ProviderError("UnauthorizedOperation"), ProviderError("RequestLimitExceeded")):
            with self.subTest(error=error.response["Error"]["Code"]):
                self.world.fail("describe_security_groups", error)
                self.refused(self.pipeline.plan, "AWS_UNCERTAIN", detail_lacks=SENTINELS)
                self.refused(lambda: self.pipeline.run_pass(apply=True), "AWS_UNCERTAIN", detail_lacks=SENTINELS)
                self.assertEqual(self.builds(), [])
        self.world.errors.clear()
        # An id the provider does not know fails the same way.
        self.use_template(lambda data: data.update(SecurityGroupIds=[sg(0xFF)]))
        self.refused(self.pipeline.plan, "AWS_UNCERTAIN")

    def test_an_unsafe_group_stops_a_pass_before_anything_is_staged(self):
        self.world.add_group(BUILD_GROUP, "example-build-group", ingress=1)
        self.refused(lambda: self.pipeline.run_pass(apply=True), "IMAGE_BUILD_TEMPLATE_UNSAFE")
        self.assertEqual(self.builds(), [])
        names = self.world.names()
        for forbidden in ("create_multipart_upload", "run_instances", "create_image"):
            self.assertNotIn(forbidden, names)
        self.assertFalse(self.world.objects)

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
            [
                (row.id, row.state, row.archive_bucket, row.archive_key, row.archive_sha256, row.builder_id)
                for row in self.builds()
            ]
        )
        _, build = self.start_build()
        # The row names the bucket and the key before the upload, and not yet the hash of an archive or a builder.
        self.assertEqual(seen, [[(build.id, "building", BUCKET, f"{PREFIX}{build.id}.tar.gz", None, None)]])

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

    def tags_without_the_base_image(self, build):
        expected = owned(
            build.id,
            **{
                BASIS_DIGEST_TAG: build.basis_digest,
                image_pipeline.SOURCE_SHA_TAG: SOURCE_SHA,
                image_pipeline.SCRIPT_SHA_TAG: image.script_sha256(SCRIPT),
            },
        )
        return {"image": expected, "snapshot": expected}

    def test_a_base_image_read_that_the_provider_refuses_is_noted_and_the_image_is_created_without_the_tag(self):
        self.world.console_plan = [(300.0, READY)]
        _, build = self.start_build()
        self.clock.advance(30)

        def refuse_the_builder_template(arguments):
            # The pin read and the protected-set reads name the worker template: only the builder's is refused.
            if arguments["LaunchTemplateName"] == BUILDER:
                raise ProviderError("RequestLimitExceeded")

        self.world.hooks["describe_launch_template_versions"] = refuse_the_builder_template
        result = self.apply()
        self.assertIn("base_image:AWS_UNCERTAIN", result["notes"])
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        self.assertEqual(self.image_tags(), self.tags_without_the_base_image(build))
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(self.only_build().state, "candidate")

    def test_a_base_image_read_that_runs_out_of_the_pass_time_is_noted_and_the_image_is_still_created(self):
        self.world.console_plan = [(300.0, READY)]
        _, build = self.start_build()
        self.clock.advance(30)
        # The marker read returns in time and ends the pass deadline: the base image read is the next call.
        self.world.hooks["get_console_output"] = lambda arguments: self.clock.advance(400)
        result = self.apply()
        self.assertIn("base_image:pass_deadline_exceeded", result["notes"])
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        self.assertEqual(self.image_tags(), self.tags_without_the_base_image(build))
        # The image exists and the row says so: the next pass reads it and goes on from there.
        self.assertEqual(result["decision"], "imaging")
        row = self.only_build()
        self.assertEqual(row.state, "imaging")
        self.assertIsNotNone(row.image_id)

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


class StoragePrefixTests(PipelineCase):
    """The archive key is the prefix, exactly one slash and the build id, however the prefix is written."""

    def staged_key(self, prefix):
        self.configure(render_toml(prefix=prefix))
        self.assertEqual(self.apply()["decision"], "building")
        (call,) = self.world.arguments("create_multipart_upload")
        self.assertEqual(call["Bucket"], BUCKET)
        return call["Key"], self.only_build().id

    def test_a_prefix_without_a_trailing_slash_gets_one(self):
        key, build_id = self.staged_key("example-prefix")
        self.assertEqual(key, f"example-prefix/{build_id}.tar.gz")

    def test_a_prefix_with_a_trailing_slash_gets_no_second_one(self):
        key, build_id = self.staged_key("example-prefix/")
        self.assertEqual(key, f"example-prefix/{build_id}.tar.gz")

    def test_a_prefix_with_several_trailing_slashes_ends_with_exactly_one(self):
        key, build_id = self.staged_key("example-prefix//")
        self.assertEqual(key, f"example-prefix/{build_id}.tar.gz")

    def test_a_nested_prefix_keeps_its_inner_slashes(self):
        key, build_id = self.staged_key("example/nested-prefix")
        self.assertEqual(key, f"example/nested-prefix/{build_id}.tar.gz")

    def test_the_archive_that_the_build_names_is_the_one_at_the_key(self):
        self.configure(render_toml(prefix="example-prefix"))
        self.apply()
        build = self.only_build()
        self.assertEqual(build.archive_key, f"example-prefix/{build.id}.tar.gz")
        self.assertEqual(list(self.world.objects), [build.archive_key])

    def test_the_stray_listing_stays_inside_the_folder_of_a_prefix_without_a_trailing_slash(self):
        self.configure(render_toml(prefix="example-prefix"))
        inside, neighbour = "example-prefix/old.tar.gz", "example-prefix-other/old.tar.gz"
        for key in (inside, neighbour):
            self.world.objects[key] = (b"x", NOW - 2 * HOUR)
        self.pin_current()
        result = self.apply()
        self.assertEqual([call["Prefix"] for call in self.world.arguments("list_objects_v2")], ["example-prefix/"])
        self.assertEqual(result["reconcile"]["archives_deleted"], 1)
        self.assertEqual(list(self.world.objects), [neighbour])


class FailureTests(PipelineCase):
    def assert_failed(self, result, reason):
        self.assertEqual((result["decision"], result["reason"]), ("failed", reason), result)
        build = self.only_build()
        self.assertEqual((build.state, build.reason_code), ("failed", reason))
        (event,) = self.events("image_build_failed")
        self.assertEqual(event["reason"], reason)
        return build

    def assert_refused(self, result, reason, *, control=(0, None, False)):
        """The pass refused the start: no build in the answer, no failure counted and no failure event.

        ``control`` is the failure count, the cooldown and the block as they were before the pass.
        """
        self.assertEqual((result["decision"], result["reason"]), ("refused", reason), result)
        self.assertIsNone(result["build"])
        counted = self.control()
        self.assertEqual((counted.consecutive_failures, counted.cooldown_until, counted.blocked), control)
        self.assertEqual(self.events("image_build_failed"), [])
        self.assertEqual(self.events("image_blocked"), [])
        check = self.events("image_check")[-1]
        self.assertEqual((check["decision"], check["reason"], check["build_id"]), ("refused", reason, None))

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

    def failed_with_console_lines(self, *lines):
        """The values of a launched build, and the tail of its failed-build event after a consumer step printed ``lines``.

        Each line is a format string over the presigned ``{url}`` of the launch, its ``{query}`` string, the
        build's archive ``{key}`` and the ``{bucket}``. The first pass launches the builder, and the console is
        set after it.
        """
        _, build = self.start_build()
        url = self.world.urls[-1]
        values = {"url": url, "query": url.split("?", 1)[1], "key": build.archive_key, "bucket": BUCKET}
        printed = [line.format(**values) for line in lines]
        self.world.consoles[build.builder_id] = console(
            f"WARM_SOURCE_SHA256 {ARCHIVE_SHA}", *printed, "WARM FAIL consumer step 2"
        )
        self.assert_failed(self.apply(), "WARM_FAILED")
        (event,) = self.events("image_build_failed")
        return values, event["framed_tail"]

    def events_text(self):
        return (self.state_dir / image_pipeline.EVENTS_FILE).read_text(encoding="utf-8")

    def test_a_framed_line_with_the_presigned_url_shows_it_as_the_archive_url(self):
        values, tail = self.failed_with_console_lines("  | WARM_SRC_URL={url}", "  | fetching {url} now")
        self.assertEqual(tail, "  | WARM_SRC_URL=[archive-url]\n  | fetching [archive-url] now")
        text = self.written_text()
        for secret in (values["url"], values["query"], SIGNATURE, "X-Amz-Signature", "X-Amz-Expires"):
            self.assertNotIn(secret, text)
        self.assertNotIn(values["key"], self.events_text())

    def test_a_framed_line_with_the_archive_url_that_has_no_query_string_shows_it_as_the_archive_url(self):
        values, tail = self.failed_with_console_lines("  | https://{bucket}.s3.example.invalid/{key}", "  | GET s3://{bucket}/{key}")
        self.assertEqual(tail, "  | [archive-url]\n  | GET [archive-url]")
        self.assertNotIn(values["key"], self.events_text())

    def test_a_framed_line_with_only_the_query_string_of_the_url_shows_it_as_the_archive_url(self):
        values, tail = self.failed_with_console_lines(
            "  | query {query}", "  | ?{query}", "  | &X-Amz-Signature=" + SIGNATURE, "  | x-amz-security-token=abc"
        )
        self.assertEqual(
            tail, "  | query [archive-url]\n  | [archive-url]\n  | [archive-url]\n  | [archive-url]"
        )
        self.assertNotIn(SIGNATURE, self.written_text())

    def test_a_framed_line_with_only_the_object_key_shows_it_as_the_archive_url(self):
        values, tail = self.failed_with_console_lines("  | NoSuchKey {key}", "  | key={key}.part")
        self.assertEqual(tail, "  | NoSuchKey [archive-url]\n  | key=[archive-url].part")
        self.assertNotIn(values["key"], self.events_text())

    def test_a_framed_line_with_another_url_shows_no_url_either(self):
        # The presigned URL is not kept after the launch, so the pass cannot tell the archive's from a consumer's,
        # and an event never holds ``://``.
        _, tail = self.failed_with_console_lines(
            "  | git clone https://oauth2:" + TOKEN + "@forge.example.invalid/example.git",
            "  | printed " + CONSUMER_URL,
            "  | step 2 failed: see output",
        )
        self.assertEqual(tail, "  | git clone [archive-url]\n  | printed [archive-url]\n  | step 2 failed: see output")
        text = self.written_text()
        for secret in (TOKEN, CONSUMER_URL, "consumer.example.invalid", "oauth2"):
            self.assertNotIn(secret, text)
        self.assertNotIn("://", self.events_text())

    def test_a_tail_without_a_url_or_a_key_is_written_as_it_was_framed(self):
        _, tail = self.failed_with_console_lines("  | error: step 2 failed (rc=2): see output", "  |   at line 7")
        self.assertEqual(tail, "  | error: step 2 failed (rc=2): see output\n  |   at line 7")

    def test_the_mask_is_the_text_the_builder_script_prints_for_the_same_things(self):
        script = Path(__file__).resolve().parents[1] / "deploy" / "runner" / "image" / "warm-cache.sh"
        self.assertEqual(image_pipeline.ARCHIVE_URL_MASK, "[archive-url]")
        self.assertIn(image_pipeline.ARCHIVE_URL_MASK, script.read_text(encoding="utf-8"))

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

    def expired_build(self, *, before=0):
        """A build whose first pass is over, with the clock ``before`` seconds short of its deadline."""
        _, build = self.start_build()
        self.clock.advance(build.deadline_at - before - self.clock())
        return build

    def test_an_expired_builder_is_ended_before_any_read_that_can_fail(self):
        build = self.expired_build()
        self.world.fail("describe_instances", ProviderError("RequestLimitExceeded"))
        self.refused(self.apply, "AWS_UNCERTAIN")
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        self.assertEqual(self.world.instances[build.builder_id]["State"]["Name"], "terminated")
        # The pass that raised changed no row. The next pass ends the build with the deadline code.
        self.assertEqual(self.only_build().state, "building")
        del self.world.errors["describe_instances"]
        self.assert_failed(self.apply(), "CONSOLE_BLIND")
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="builder"), 0.0)

    def test_an_expired_imaging_builder_is_ended_before_any_read_that_can_fail(self):
        self.world.console_plan = [(30.0, READY)]
        self.world.image_final_state = "pending"
        self.assertEqual(self.apply()["decision"], "imaging")
        build = self.only_build()
        self.clock.advance(build.deadline_at - self.clock())
        self.world.fail("describe_instances", ProviderError("RequestLimitExceeded"))
        self.refused(self.apply, "AWS_UNCERTAIN")
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])

    def test_a_builder_adopted_for_an_expired_build_is_ended_before_the_later_reads(self):
        self.world.ghost_launch = True
        self.world.fail("run_instances", ProviderError("RequestLimitExceeded"), times=1)
        self.assertIn("run_builder:uncertain", self.apply()["notes"])
        (ghost,) = self.world.instances
        self.clock.advance(BUILD_DEADLINE)
        self.world.fail("list_objects_v2", ProviderError("RequestLimitExceeded"))
        self.refused(self.apply, "AWS_UNCERTAIN")
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [ghost]}])
        self.assertEqual(self.only_build().builder_id, ghost)

    def test_a_refused_console_read_before_the_deadline_refuses_the_pass_and_keeps_the_build(self):
        build = self.expired_build(before=120)
        self.world.fail("get_console_output", ProviderError("UnauthorizedOperation"))
        self.refused(self.apply, "AWS_UNCERTAIN")
        later = self.only_build()
        self.assertEqual((later.state, later.builder_id), ("building", build.builder_id))
        self.assertEqual(self.world.arguments("create_image"), [])

    def test_a_refused_console_read_at_the_deadline_ends_the_build_as_console_blind(self):
        build = self.expired_build()
        self.world.fail("get_console_output", ProviderError("UnauthorizedOperation"))
        result = self.apply()
        self.assert_failed(result, "CONSOLE_BLIND")
        self.assertIn("console_read:AWS_UNCERTAIN", result["notes"])
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        self.assertEqual(self.world.arguments("create_image"), [])
        self.assertEqual(self.metric("ci_burst_image_consecutive_failures"), 1.0)
        self.assertEqual(len(self.events("image_check")), 2)

    def test_a_console_read_that_starts_before_the_deadline_and_is_refused_after_it_ends_the_build(self):
        build = self.expired_build(before=10)
        clock = self.clock

        def slow_refusal(adapter, instance_ids, *, deadline=None):
            clock.advance(20)
            raise ProviderRefusal("AWS_UNCERTAIN", "RequestLimitExceeded", uncertain=True)

        with mock.patch.object(WorldAws, "console_output", slow_refusal):
            result = self.apply()
        self.assert_failed(result, "CONSOLE_BLIND")
        self.assertIn("console_read:AWS_UNCERTAIN", result["notes"])
        self.assertEqual(self.world.arguments("create_image"), [])
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])

    def test_a_blind_console_at_the_deadline_ends_the_build_with_one_termination_and_the_blind_code(self):
        build = self.expired_build()
        result = self.apply()
        self.assert_failed(result, "CONSOLE_BLIND")
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        self.assertEqual(self.world.arguments("create_image"), [])
        self.assertEqual(result["reconcile"]["builders_terminated"], 1)
        (event,) = self.events("image_reconciled")
        self.assertEqual((event["builders_terminated"], event["build_id"]), (1, build.id))
        # The builder of the build is not a leak: it reached its own deadline.
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="builder"), 0.0)

    def test_a_readable_console_at_the_deadline_ends_the_build_with_one_termination_and_the_timeout_code(self):
        self.world.console_plan = [(10.0, WAITING)]
        build = self.expired_build()
        result = self.apply()
        self.assert_failed(result, "MARKER_TIMEOUT")
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])

    def test_a_marker_first_seen_after_the_deadline_creates_no_image(self):
        self.world.console_plan = [(2_000.0, READY)]
        build = self.expired_build()
        result = self.apply()
        self.assert_failed(result, "IMAGE_BUILDER_GONE")
        self.assertEqual(self.world.arguments("create_image"), [])
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        self.assertEqual(len(self.events("image_marker_seen")), 1)

    def test_a_builder_that_the_provider_still_lists_after_the_termination_is_not_imaged(self):
        self.world.console_plan = [(2_000.0, READY)]
        self.world.ignore_terminate = True
        build = self.expired_build()
        result = self.apply()
        self.assert_failed(result, "IMAGE_BUILDER_GONE")
        self.assertEqual(self.world.arguments("create_image"), [])
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])

    def test_a_marker_first_read_past_the_deadline_creates_no_image_when_the_early_termination_was_refused(self):
        # Reconcile's termination of the overdue builder is refused once, so the builder is still listed
        # and alive when the marker is read: the deadline, not the builder, ends the build.
        self.world.console_plan = [(2_000.0, READY)]
        build = self.expired_build()
        self.world.fail("terminate_instances", ProviderError("RequestLimitExceeded"), times=1)
        result = self.apply()
        self.assert_failed(result, "MARKER_TIMEOUT")
        self.assertEqual(self.world.arguments("create_image"), [])
        self.assertEqual(len(self.events("image_marker_seen")), 1)
        # The refused attempt was reconcile's. The failure path asked again, and that one ended the builder.
        self.assertEqual(result["reconcile"]["failures"], ["AWS_UNCERTAIN"])
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}] * 2)
        self.assertNotIn("terminate:failed", result["notes"])
        self.assertEqual(self.world.instances[build.builder_id]["State"]["Name"], "terminated")
        self.assertEqual(self.control().consecutive_failures, 1)

    def marker_with_ten_seconds_left(self):
        """A build whose next pass starts ten seconds short of the build deadline and reads a finished marker.

        A test then advances the clock by thirty seconds inside one call of that pass: that passes the build
        deadline and stays inside the pass deadline, so only the build deadline can stop what follows.
        """
        self.world.console_plan = [(2_000.0, READY)]
        return self.expired_build(before=10)

    def assert_no_image_for_a_build_out_of_time(self, result, build):
        """The build failed ``MARKER_TIMEOUT``, made no image, and the failure path ended its live builder."""
        self.assert_failed(result, "MARKER_TIMEOUT")
        self.assertEqual(self.world.arguments("create_image"), [])
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        self.assertEqual(self.world.instances[build.builder_id]["State"]["Name"], "terminated")

    def test_a_deadline_that_comes_during_the_console_read_creates_no_image(self):
        build = self.marker_with_ten_seconds_left()
        self.world.hooks["get_console_output"] = lambda arguments: self.clock.advance(30)
        result = self.apply()
        self.assert_no_image_for_a_build_out_of_time(result, build)
        # The marker is seen when the read ends, so that is the time of its latency.
        (event,) = self.events("image_marker_seen")
        self.assertEqual(event["latency_seconds"], build.deadline_at + 20 - build.created_at)

    def test_a_deadline_that_comes_during_the_base_image_read_creates_no_image(self):
        build = self.marker_with_ten_seconds_left()

        def run_out_of_time(arguments):
            # The pin and the protected set name the worker template: only the builder's is the base image read.
            if arguments["LaunchTemplateName"] == BUILDER:
                self.clock.advance(30)

        self.world.hooks["describe_launch_template_versions"] = run_out_of_time
        result = self.apply()
        self.assert_no_image_for_a_build_out_of_time(result, build)

    def test_a_deadline_that_comes_during_the_lookup_of_the_build_image_creates_no_image(self):
        build = self.marker_with_ten_seconds_left()
        mark = len(self.world.calls)
        spent = []

        def run_out_of_time(arguments):
            # Reconcile lists the images before the console is read: the first listing after it is the lookup.
            if arguments["ImageIds"] is None and "get_console_output" in self.world.names(mark) and not spent:
                spent.append(True)
                self.clock.advance(30)

        self.world.hooks["describe_images"] = run_out_of_time
        result = self.apply()
        self.assertEqual(spent, [True])
        self.assert_no_image_for_a_build_out_of_time(result, build)

    def test_a_deadline_that_comes_after_the_last_clock_read_before_create_image_creates_no_image(self):
        # The pass reads the clock with ten seconds left and is then held up for thirty before ``CreateImage``.
        build = self.marker_with_ten_seconds_left()
        create_image = WorldAws.create_image

        def held_up(aws, *arguments, **keywords):
            self.clock.advance(30)
            return create_image(aws, *arguments, **keywords)

        with mock.patch.object(WorldAws, "create_image", held_up):
            result = self.apply()
        self.assert_no_image_for_a_build_out_of_time(result, build)

    def uncertain_create(self, *, hidden_for=0.0):
        """The first ``CreateImage`` makes the image and the provider's answer is lost. That pass ends there.

        The image and its snapshot are not listed for ``hidden_for`` seconds. The result is the image's id.
        """
        self.world.console_plan = [(30.0, READY)]
        self.world.ghost_image = True
        self.world.hide_images_for = hidden_for
        self.world.fail("create_image", ProviderError("RequestLimitExceeded"), times=1)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("building", None))
        self.assertIn("create_image:AWS_UNCERTAIN", result["notes"])
        build = self.only_build()
        self.assertEqual((build.state, build.image_id), ("building", None))
        self.assertEqual(self.events("image_created"), [])
        (ghost,) = self.world.images.keys() - {ami(0xA1)}
        return ghost

    def test_an_image_first_listed_after_the_deadline_is_adopted_by_reconcile_and_made_once(self):
        ghost = self.uncertain_create(hidden_for=10 * DAY)
        build = self.only_build()
        # The image stays unlisted through every pass before the deadline and is listed at the first one after it.
        for identifier in list(self.world.hidden_until):
            self.world.hidden_until[identifier] = build.deadline_at + 1
        self.clock.advance(build.deadline_at + 5 - self.clock())
        result = self.apply()
        # Reconcile ends the overdue builder first, then adopts the image the earlier call made: no second call.
        self.assertIn({"InstanceIds": [build.builder_id]}, self.world.arguments("terminate_instances"))
        self.assertTrue(result["reconcile"]["image_adopted"])
        self.assertEqual(self.only_build().image_id, ghost)
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(self.events("image_build_failed"), [])

    def test_an_uncertain_create_whose_image_is_listed_by_the_next_pass_is_adopted_and_made_once(self):
        ghost = self.uncertain_create()
        # Reconcile lists the image at the start of the next pass and adopts it by its tags.
        result = self.apply()
        self.assertTrue(result["reconcile"]["image_adopted"])
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(self.only_build().image_id, ghost)
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        self.assertEqual(self.world.duplicate_refusals, 0)
        self.assertEqual(self.control().consecutive_failures, 0)

    def test_an_image_that_is_listed_only_after_reconcile_is_adopted_before_any_second_create(self):
        ghost = self.uncertain_create(hidden_for=100.0)

        def time_passes(arguments):
            # The image is not listed when the next pass reconciles. It is by the time that pass has read the console.
            del self.world.hooks["get_console_output"]
            self.clock.advance(100)

        self.world.hooks["get_console_output"] = time_passes
        result = self.apply()
        self.assertFalse(result["reconcile"]["image_adopted"])
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        self.assertEqual(self.world.duplicate_refusals, 0)
        self.assertEqual(self.only_build().image_id, ghost)
        self.assertEqual(result["decision"], "candidate")
        (event,) = self.events("image_created")
        self.assertEqual((event["build_id"], event["image_id"]), (self.only_build().id, ghost))

    def test_a_duplicate_name_refusal_is_uncertain_counts_no_failure_and_the_image_is_adopted_once_listed(self):
        ghost = self.uncertain_create(hidden_for=100.0)
        # The next pass cannot see the image, so it asks again under the same name. The provider refuses it, and
        # the SDK reports the refusal as a client error with the code of a duplicate name.
        _, modules = sdk_client_error("InvalidAMIName.Duplicate")
        modules["botocore.exceptions"].ClientError = ProviderError
        with mock.patch.dict(sys.modules, modules):
            result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("building", None))
        self.assertIn("create_image:AWS_UNCERTAIN", result["notes"])
        self.assertEqual(self.world.duplicate_refusals, 1)
        build = self.only_build()
        self.assertEqual((build.state, build.image_id), ("building", None))
        self.assertEqual(self.events("image_build_failed"), [])
        self.assertEqual(self.control().consecutive_failures, 0)
        names = [call["Name"] for call in self.world.arguments("create_image")]
        self.assertEqual(names, [f"ci-burst-{DEPLOYMENT}-{build.id}"] * 2)
        # Once the image is listed the next pass adopts it, and no third call is made.
        self.clock.advance(100)
        result = self.apply()
        self.assertTrue(result["reconcile"]["image_adopted"])
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(self.only_build().image_id, ghost)
        self.assertEqual(len(self.world.arguments("create_image")), 2)
        self.assertEqual(self.world.images.keys() - {ami(0xA1)}, {ghost})

    def test_a_lookup_of_the_build_image_that_the_provider_refuses_is_noted_and_the_image_is_still_created(self):
        self.world.console_plan = [(300.0, READY)]
        self.start_build()
        self.clock.advance(30)
        mark = len(self.world.calls)

        def refuse_the_lookup(arguments):
            # Reconcile lists the images before the console is read: the listing after it is the lookup.
            if arguments["ImageIds"] is None and "get_console_output" in self.world.names(mark):
                raise ProviderError("RequestLimitExceeded")

        self.world.hooks["describe_images"] = refuse_the_lookup
        result = self.apply()
        self.assertIn("image_lookup:AWS_UNCERTAIN", result["notes"])
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        self.assertEqual(self.world.duplicate_refusals, 0)
        self.assertIsNotNone(self.only_build().image_id)
        self.assertEqual(self.events("image_build_failed"), [])

    def test_an_image_that_the_lookup_finds_after_the_deadline_passed_is_adopted_and_not_made_again(self):
        # A pass loses the answer of CreateImage with 120 seconds to the deadline. The image is listed after 90.
        self.world.console_plan = [(2_000.0, READY)]
        self.expired_build(before=120)
        self.world.ghost_image = True
        self.world.hide_images_for = 90.0
        self.world.fail("create_image", ProviderError("RequestLimitExceeded"), times=1)
        self.assertIn("create_image:AWS_UNCERTAIN", self.apply()["notes"])
        (ghost,) = self.world.images.keys() - {ami(0xA1)}
        # The next pass reconciles with 50 seconds left, before the image is listed. The deadline passes inside the
        # lookup, which finds the image. Adopting an image is no call of CreateImage, so the build goes on from there.
        self.clock.advance(70)
        spent = []
        mark = len(self.world.calls)

        def run_out_of_time(arguments):
            if arguments["ImageIds"] is None and "get_console_output" in self.world.names(mark) and not spent:
                spent.append(True)
                self.clock.advance(60)

        self.world.hooks["describe_images"] = run_out_of_time
        result = self.apply()
        self.assertEqual(spent, [True])
        self.assertFalse(result["reconcile"]["image_adopted"])
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        self.assertEqual(self.only_build().image_id, ghost)
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(self.events("image_build_failed"), [])

    def test_an_uncertain_create_that_outlives_the_deadline_fails_the_build_and_the_late_image_is_removed(self):
        build = self.marker_with_ten_seconds_left()
        self.world.ghost_image = True
        self.world.hide_images_for = 100.0
        self.world.fail("create_image", ProviderError("RequestLimitExceeded"), times=1)
        self.world.hooks["create_image"] = lambda arguments: self.clock.advance(30)
        result = self.apply()
        failed = self.assert_failed(result, "AWS_UNCERTAIN")
        self.assertIsNone(failed.image_id)
        self.assertEqual(len(self.world.arguments("create_image")), 1)
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        (ghost,) = self.world.images.keys() - {ami(0xA1)}
        self.assert_late_image_removed(ghost, 100)

    def assert_late_image_removed(self, ghost, listed_after):
        """The image of a failed build, listed late, is no orphan: the deletion rule's failed path removes it.

        The build's row exists and carries the image's build id, so reconcile records nothing, and the image is
        taken by the failed-build path before anything starts.
        """
        self.pin_current()
        self.clock.advance(listed_after)
        result = self.apply()
        self.assertEqual(result["reconcile"]["images_recorded"], 0)
        self.assertEqual(result["reconcile"]["failed_images"]["deregistered"], 1)
        self.assertEqual(result["reconcile"]["failed_images"]["snapshots_deleted"], 1)
        self.assertNotIn(ghost, self.world.images)
        self.assertEqual(self.world.snapshots, {})
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="image"), 0.0)

    def test_a_duplicate_name_refusal_at_every_pass_ends_at_the_deadline_and_the_late_image_is_removed(self):
        ghost = self.uncertain_create(hidden_for=10 * DAY)
        result = self.pipeline.run_until_done()
        # Reconcile ended the builder at the deadline, so the marker that the pass then reads fails the build as
        # a marker from a builder that is gone. Until then every pass asked again and was refused.
        self.assert_failed(result, "IMAGE_BUILDER_GONE")
        self.assertIsNone(self.only_build().image_id)
        self.assertGreater(self.world.duplicate_refusals, 10)
        self.assertEqual(len(self.world.arguments("create_image")), self.world.duplicate_refusals + 1)
        self.assertEqual(self.world.images.keys() - {ami(0xA1)}, {ghost})
        self.assert_late_image_removed(ghost, 10 * DAY)

    def test_a_builder_at_its_deadline_is_ended_by_the_first_call_of_the_pass(self):
        build = self.expired_build()
        mark = len(self.world.calls)
        self.apply()
        self.assertEqual(self.world.names(mark)[0], "terminate_instances")
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])

    def test_a_builder_one_second_short_of_its_deadline_is_left_to_the_state_machine(self):
        build = self.expired_build(before=1)
        mark = len(self.world.calls)
        result = self.apply()
        names = self.world.names(mark)
        self.assertEqual(result["reconcile"]["builders_terminated"], 0)
        self.assertLess(names.index("describe_instances"), names.index("terminate_instances"))
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        self.assert_failed(result, "CONSOLE_BLIND")

    def test_an_image_that_became_available_past_the_deadline_still_completes_with_one_termination(self):
        self.world.console_plan = [(30.0, READY)]
        self.world.image_delay = 100_000.0
        self.assertEqual(self.apply()["decision"], "imaging")
        build = self.only_build()
        self.clock.advance(build.deadline_at - self.clock())
        self.world.image_delay = 90.0
        result = self.apply()
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])

    def test_a_refused_early_termination_is_reported_and_the_failure_path_asks_again(self):
        build = self.expired_build()
        self.world.refusals["terminate"] = ProviderRefusal("AWS_UNCERTAIN", "ProviderError", uncertain=True)
        result = self.apply()
        self.assert_failed(result, "CONSOLE_BLIND")
        self.assertEqual(result["reconcile"]["failures"], ["AWS_UNCERTAIN"])
        self.assertEqual(result["reconcile"]["builders_terminated"], 0)
        self.assertIn("terminate:failed", result["notes"])
        self.assertEqual(self.world.instances[build.builder_id]["State"]["Name"], "running")
        # The build is over, so the builder belongs to no build and the next pass ends it.
        del self.world.refusals["terminate"]
        result = self.apply()
        self.assertEqual(result["reconcile"]["builders_terminated"], 1)
        self.assertEqual(self.world.instances[build.builder_id]["State"]["Name"], "terminated")

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
        self.add_build(
            "crashed", "building", age=300, archive_bucket=BUCKET, archive_key=key, archive_sha256=ARCHIVE_SHA
        )
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

    def test_credentials_that_will_not_outlive_the_url_refuse_the_start_before_the_row_and_the_upload(self):
        self.world.credential_life = 600.0
        result = self.apply()
        self.assert_refused(result, "IMAGE_CREDENTIAL_SHORT")
        self.assertEqual(self.builds(), [])
        self.assertEqual(self.world.arguments("create_multipart_upload"), [])
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.urls, [])

    def test_a_refused_start_leaves_the_failures_of_earlier_builds_as_they_were(self):
        self.set_control(consecutive_failures=2, cooldown_until=NOW - 1)
        self.world.credential_life = 600.0
        result = self.apply()
        self.assert_refused(result, "IMAGE_CREDENTIAL_SHORT", control=(2, NOW - 1, False))

    def test_a_session_that_runs_short_during_the_upload_ends_the_row_and_the_archive_without_a_failure(self):
        # Enough life for the check before the upload, too little once the upload is done.
        self.world.credential_life = 1850.0
        self.source.on_chunk = lambda: self.clock.advance(100)
        result = self.apply()
        self.assert_refused(result, "IMAGE_CREDENTIAL_SHORT")
        build = self.only_build()
        self.assertEqual((build.state, build.reason_code), ("failed", "IMAGE_CREDENTIAL_SHORT"))
        self.assertEqual(build.archive_sha256, ARCHIVE_SHA)
        self.assertEqual(self.world.objects, {})
        self.assertIn("delete_object", self.world.names())
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.urls, [])
        self.assertEqual(self.sleeps, [])

    def test_user_data_over_the_limit_fails_the_build_before_the_launch(self):
        self.world.url_padding = 20_000
        result = self.apply()
        self.assert_failed(result, "IMAGE_USERDATA_TOO_LARGE")
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.objects, {})

    def appearing_builder(self, call):
        """Make a builder that no build owns appear when the fake provider first sees ``call``."""
        appeared = []

        def appear(arguments):
            if not appeared:
                appeared.append(self.world.add_instance(foreign_builder_tags()))

        self.world.hooks[call] = appear
        return appeared

    def test_a_builder_that_appears_before_the_row_refuses_the_start_and_is_ended_next_pass(self):
        # The last read before the check of the start: the builder is there after reconcile has looked.
        appeared = self.appearing_builder("describe_security_groups")
        result = self.apply()
        self.assert_refused(result, "IMAGE_BUILD_ALREADY_ACTIVE")
        self.assertEqual(self.builds(), [])
        self.assertEqual(self.world.arguments("create_multipart_upload"), [])
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.arguments("terminate_instances"), [])
        self.assertEqual(self.world.urls, [])
        self.apply()
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": appeared}])

    def test_a_builder_that_appears_before_the_launch_ends_the_row_without_a_failure_and_is_ended_next_pass(self):
        appeared = self.appearing_builder("generate_presigned_url")
        result = self.apply()
        self.assert_refused(result, "IMAGE_BUILD_ALREADY_ACTIVE")
        build = self.only_build()
        self.assertEqual((build.state, build.reason_code), ("failed", "IMAGE_BUILD_ALREADY_ACTIVE"))
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.objects, {})
        self.assertIn("delete_object", self.world.names())
        self.assertEqual(self.world.arguments("terminate_instances"), [])
        self.apply()
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": appeared}])

    def test_an_archive_that_cannot_be_deleted_after_a_refused_start_is_a_note_and_not_a_failure(self):
        self.appearing_builder("generate_presigned_url")
        self.world.fail("delete_object", ProviderError("RequestLimitExceeded"), times=1)
        result = self.apply()
        self.assert_refused(result, "IMAGE_BUILD_ALREADY_ACTIVE")
        self.assertIn("archive:delete_failed", result["notes"])
        self.assertEqual(len(self.world.objects), 1)

    def test_a_start_without_a_gate_is_refused_before_the_row_and_the_upload(self):
        # The pass began because a build was in flight. That build ended before the pass took the lock, so the pass
        # reaches the start of a new build, and a new build needs a gate or the opt-out.
        self.add_build("settled", "building", age=5 * DAY)
        self.configure(render_toml(gate=""), state=BuildEndedBeforeTheLock(self.state_dir, clock=self.clock))
        result = self.apply()
        self.assert_refused(result, "IMAGE_GATE_REQUIRED")
        (ended,) = self.builds()
        self.assertEqual((ended.id, ended.state, ended.reason_code), ("settled", "failed", "CONSOLE_BLIND"))
        self.assertEqual(self.world.arguments("create_multipart_upload"), [])
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.urls, [])

    def test_a_start_without_a_gate_is_refused_for_the_gate_and_not_for_an_unsafe_template(self):
        # The same race, with a build template that would also be refused: the gate requirement is the one reported.
        self.add_build("settled", "building", age=5 * DAY)
        self.configure(render_toml(gate=""), state=BuildEndedBeforeTheLock(self.state_dir, clock=self.clock))
        data = copy.deepcopy(SAFE_BUILDER)
        data["IamInstanceProfile"] = {"Name": "sentinel-profile"}
        self.world.set_version(BUILDER, 1, data)
        result = self.apply()
        self.assert_refused(result, "IMAGE_GATE_REQUIRED")
        self.assertEqual(self.world.arguments("create_multipart_upload"), [])
        self.assertEqual(self.world.arguments("run_instances"), [])

    def assert_unstaged(self, build):
        """A build that waits for the next pass to stage its archive: no builder, no archive hash, no failure."""
        self.assertEqual((build.state, build.builder_id, build.archive_sha256), ("building", None, None))
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.events("image_build_failed"), [])
        control = self.control()
        self.assertEqual((control.consecutive_failures, control.cooldown_until, control.blocked), (0, None, False))

    def archive_keys(self):
        return [call["Key"] for call in self.world.arguments("create_multipart_upload")]

    def test_a_stage_that_outlasts_the_pass_leaves_the_build_for_the_next_pass_to_stage_again(self):
        self.source.on_chunk = lambda: self.clock.advance(400)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("building", None))
        self.assertIn("pass_deadline_exceeded", result["notes"])
        self.assertEqual(result["retention"], {"refused": "pass_deadline_exceeded"})
        first = self.only_build()
        self.assert_unstaged(first)
        self.assertEqual(self.world.objects, {})
        # The next pass stages the same archive under the same key and launches.
        self.source.on_chunk = None
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(self.only_build().id, first.id)
        self.assertEqual(self.archive_keys(), [f"{PREFIX}{first.id}.tar.gz"] * 2)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.events("image_build_failed"), [])
        self.assertEqual(len(self.events("image_build_started")), 1)

    def test_a_pass_deadline_after_the_upload_leaves_the_build_for_the_next_pass_to_stage_again(self):
        # The pass runs out of time between the end of the upload and the launch.
        self.world.hooks["generate_presigned_url"] = lambda arguments: self.clock.advance(400)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("building", None))
        self.assertIn("pass_deadline_exceeded", result["notes"])
        first = self.only_build()
        self.assert_unstaged(first)
        self.assertEqual(first.archive_key, f"{PREFIX}{first.id}.tar.gz")
        self.assertEqual(list(self.world.objects), [first.archive_key])
        # The row is not an uncertain launch, so the next pass stages again and launches.
        del self.world.hooks["generate_presigned_url"]
        result = self.apply()
        self.assertEqual(result["decision"], "building", result)
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assertIsNotNone(build.builder_id)
        self.assertEqual(build.archive_sha256, ARCHIVE_SHA)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(list(self.world.objects), [first.archive_key])
        self.assertEqual(self.events("image_build_failed"), [])

    def a_pass_whose_last_listing_before_the_launch_takes(self, seconds):
        """Run a pass that launches a builder, and make its last read before the launch call take ``seconds``.

        The launch lists the builders once more after it presigned the URL (``_Pass._launch``): that listing starts
        inside the pass, returns empty ``seconds`` later and is the last read before ``RunInstances``. The build deadline
        is far off, so only the pass deadline can stop the launch. Returns the start of the pass and its result.
        """
        started_at = self.clock()
        entered = []

        def take_time(arguments):
            if self.world.arguments("generate_presigned_url") and not entered:
                entered.append(self.clock())
                self.clock.advance(seconds)

        self.world.hooks["describe_instances"] = take_time
        result = self.apply()
        del self.world.hooks["describe_instances"]
        # The premise: the listing started before the pass deadline, so the adapter sent it.
        self.assertEqual(len(entered), 1)
        self.assertLess(entered[0], started_at + PASS_DEADLINE_SECONDS)
        return started_at, result

    def assert_a_last_listing_that_ended_the_pass_launched_nothing(self, started_at, result):
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        # The answer that matters most: no builder was launched after the pass deadline.
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual((result["decision"], result["reason"]), ("building", None), result)
        self.assertIn("pass_deadline_exceeded", result["notes"])
        first = self.only_build()
        # The row waits for the next pass to stage it again: no builder, no archive hash and no failure.
        self.assert_unstaged(first)
        # The build deadline is far off, so it did not stop the launch.
        self.assertLess(self.clock(), first.deadline_at)
        self.assertEqual(first.archive_key, f"{PREFIX}{first.id}.tar.gz")
        self.assertEqual(list(self.world.objects), [first.archive_key])
        # The next pass stages the archive again under the same key and launches.
        result = self.apply()
        self.assertEqual(result["decision"], "building", result)
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assertIsNotNone(build.builder_id)
        self.assertEqual(build.archive_sha256, ARCHIVE_SHA)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.events("image_build_failed"), [])

    def test_a_last_listing_that_ends_at_the_pass_deadline_launches_no_builder(self):
        started_at, result = self.a_pass_whose_last_listing_before_the_launch_takes(PASS_DEADLINE_SECONDS)
        self.assert_a_last_listing_that_ended_the_pass_launched_nothing(started_at, result)

    def test_a_last_listing_that_ends_after_the_pass_deadline_launches_no_builder(self):
        started_at, result = self.a_pass_whose_last_listing_before_the_launch_takes(PASS_DEADLINE_SECONDS + 60)
        self.assert_a_last_listing_that_ended_the_pass_launched_nothing(started_at, result)

    @contextmanager
    def time_after_the_last_listing(self, seconds):
        """Let ``seconds`` pass after the last listing before the launch has been read, before the launch call."""
        real = image_pipeline._Pass._check_no_builder

        def then_wait(stage, build=None):
            real(stage, build)
            if self.world.arguments("generate_presigned_url"):
                self.clock.advance(seconds)

        with mock.patch.object(image_pipeline._Pass, "_check_no_builder", then_wait):
            yield

    def test_a_launch_call_that_would_start_after_the_build_deadline_is_not_made(self):
        first = self.resumed_stage_with_100_seconds_left()
        mark = len(self.world.calls)
        with self.time_after_the_last_listing(150):
            result = self.apply()
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assert_nothing_launched_for_a_build_out_of_time(result, first, mark)

    def test_a_launch_call_that_would_start_after_the_pass_deadline_leaves_the_row_for_the_next_pass(self):
        started_at = self.clock()
        with self.time_after_the_last_listing(PASS_DEADLINE_SECONDS):
            result = self.apply()
        self.assert_a_last_listing_that_ended_the_pass_launched_nothing(started_at, result)

    def test_the_archive_url_and_the_builder_are_bound_to_the_recorded_owner(self):
        self.apply()
        build = self.only_build()
        (presign,) = self.world.arguments("generate_presigned_url")
        self.assertEqual(presign["Params"]["ExpectedBucketOwner"], build.archive_owner)
        (launch,) = self.world.launches
        self.assertIn(f"export WARM_SRC_OWNER={build.archive_owner}\n", launch["UserData"])

    def test_a_last_listing_that_ends_just_inside_the_pass_deadline_launches_the_builder(self):
        # The same pass with a listing that ends inside the deadline, so the refusals above are the deadline's.
        started_at, _ = self.a_pass_whose_last_listing_before_the_launch_takes(PASS_DEADLINE_SECONDS - 1)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        build = self.only_build()
        self.assertIsNotNone(build.builder_id)
        self.assertEqual((build.state, build.archive_sha256), ("building", ARCHIVE_SHA))
        self.assertEqual(self.events("image_build_failed"), [])

    def unstaged_build(self):
        """A build whose first pass ran out of time during the stage. That pass is over."""
        self.source.on_chunk = lambda: self.clock.advance(400)
        self.apply()
        self.source.on_chunk = None
        return self.only_build()

    def test_a_build_that_was_never_staged_fails_at_its_deadline_with_the_blind_code(self):
        build = self.unstaged_build()
        self.clock.advance(build.deadline_at - self.clock())
        result = self.apply()
        self.assert_failed(result, "CONSOLE_BLIND")
        # No second upload, no launch and no builder to end.
        self.assertEqual(len(self.archive_keys()), 1)
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.arguments("terminate_instances"), [])
        self.assertEqual(self.control().consecutive_failures, 1)

    def test_a_staged_archive_is_deleted_when_the_build_fails_at_its_deadline_unlaunched(self):
        self.world.hooks["generate_presigned_url"] = lambda arguments: self.clock.advance(400)
        self.apply()
        build = self.only_build()
        self.assertEqual(list(self.world.objects), [build.archive_key])
        del self.world.hooks["generate_presigned_url"]
        self.clock.advance(BUILD_DEADLINE)
        result = self.apply()
        self.assert_failed(result, "CONSOLE_BLIND")
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.world.arguments("delete_object"), [{"Bucket": BUCKET, "Key": build.archive_key, "ExpectedBucketOwner": ACCOUNT}])
        self.assertEqual(self.world.arguments("run_instances"), [])

    def test_a_build_one_second_short_of_its_deadline_is_staged_again(self):
        build = self.unstaged_build()
        self.clock.advance(build.deadline_at - 1 - self.clock())
        self.apply()
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(len(self.archive_keys()), 2)

    def test_a_resumed_stage_that_is_refused_ends_the_row_without_a_failure(self):
        self.unstaged_build()
        self.world.credential_life = 600.0
        result = self.apply()
        self.assert_refused(result, "IMAGE_CREDENTIAL_SHORT")
        build = self.only_build()
        self.assertEqual((build.state, build.reason_code), ("failed", "IMAGE_CREDENTIAL_SHORT"))
        # The check comes before the second upload: only the first pass opened one.
        self.assertEqual(len(self.archive_keys()), 1)
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.objects, {})

    def test_a_resumed_stage_reads_the_build_template_again_and_an_unsafe_one_ends_the_row_without_a_failure(self):
        first = self.unstaged_build()
        # The template was safe when the build started. A group of it has since gained an inbound rule.
        self.world.add_group(BUILD_GROUP, "example-build-group", ingress=1)
        mark = len(self.world.calls)
        result = self.apply()
        self.assert_refused(result, "IMAGE_BUILD_TEMPLATE_UNSAFE")
        build = self.only_build()
        self.assertEqual((build.id, build.state, build.reason_code), (first.id, "failed", "IMAGE_BUILD_TEMPLATE_UNSAFE"))
        # The check comes before the second upload: only the first pass opened one.
        self.assertNotIn("create_multipart_upload", self.world.names(mark))
        self.assertEqual(len(self.archive_keys()), 1)
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.objects, {})

    def test_a_resumed_stage_that_finds_the_template_unsafe_deletes_the_archive_the_row_names(self):
        self.world.hooks["generate_presigned_url"] = lambda arguments: self.clock.advance(400)
        self.apply()
        first = self.only_build()
        self.assertEqual(list(self.world.objects), [first.archive_key])
        del self.world.hooks["generate_presigned_url"]
        self.world.add_group(BUILD_GROUP, "example-build-group", ingress=1)
        result = self.apply()
        self.assert_refused(result, "IMAGE_BUILD_TEMPLATE_UNSAFE")
        build = self.only_build()
        self.assertEqual((build.id, build.state, build.reason_code), (first.id, "failed", "IMAGE_BUILD_TEMPLATE_UNSAFE"))
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.world.arguments("delete_object"), [{"Bucket": BUCKET, "Key": first.archive_key, "ExpectedBucketOwner": ACCOUNT}])
        self.assertEqual(len(self.archive_keys()), 1)
        self.assertEqual(self.world.arguments("run_instances"), [])

    def test_a_resumed_stage_whose_template_check_runs_out_of_time_leaves_the_build_for_the_next_pass(self):
        first = self.unstaged_build()
        # The pass runs out of time inside the first read of the template check.
        self.world.hooks["describe_launch_templates"] = lambda arguments: self.clock.advance(400)
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("building", None))
        self.assertIn("pass_deadline_exceeded", result["notes"])
        self.assert_unstaged(self.only_build())
        self.assertEqual(self.only_build().id, first.id)
        self.assertNotIn("create_multipart_upload", self.world.names(mark))
        self.assertEqual(len(self.archive_keys()), 1)
        # Nothing is lost: the next pass reads the template again, stages the archive and launches.
        del self.world.hooks["describe_launch_templates"]
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(self.only_build().id, first.id)
        self.assertEqual(len(self.archive_keys()), 2)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.events("image_build_failed"), [])

    def test_a_resumed_stage_whose_template_read_the_provider_refuses_raises_and_leaves_the_row_unstaged(self):
        first = self.unstaged_build()
        self.world.fail("describe_launch_templates", ProviderError("RequestLimitExceeded"), times=1)
        mark = len(self.world.calls)
        self.refused(self.apply, "AWS_UNCERTAIN")
        self.assertNotIn("create_multipart_upload", self.world.names(mark))
        self.assert_unstaged(self.only_build())
        self.assertEqual(self.only_build().id, first.id)
        # The refusal is neither a refused start nor a failure: the next pass reads again and stages.
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(self.only_build().id, first.id)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)

    # -- a stage that is done again under other inputs ---------------------------------
    # A build is inserted under one digest. A pass that stages it again launches only under the inputs that give that
    # digest: the basis files at the row's own commit, the warm settings and the script, read as they are now.

    def assert_inputs_changed(self, result, first):
        """The pass ended the row without a failure and staged nothing: the first pass opened the only upload."""
        self.assert_refused(result, "IMAGE_INPUTS_CHANGED")
        build = self.only_build()
        self.assertEqual((build.id, build.state, build.reason_code), (first.id, "failed", "IMAGE_INPUTS_CHANGED"))
        self.assertEqual(len(self.archive_keys()), 1)
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.arguments("terminate_instances"), [])
        self.assertEqual(self.world.objects, {})

    def test_a_resumed_stage_under_another_script_ends_the_row_without_a_failure_and_launches_nothing(self):
        first = self.unstaged_build()
        self.restart(script=SCRIPT + b"# edited\n")
        mark = len(self.world.calls)
        result = self.apply()
        self.assert_inputs_changed(result, first)
        # The check comes before the second upload: only the first pass opened one.
        self.assertNotIn("create_multipart_upload", self.world.names(mark))

    def test_a_resumed_stage_under_another_gate_command_ends_the_row_without_a_failure_and_launches_nothing(self):
        first = self.unstaged_build()
        self.configure(render_toml(gate=NO_GATE))
        result = self.apply()
        self.assert_inputs_changed(result, first)
        self.assertEqual(self.control().consecutive_failures, 0)

    def test_a_resumed_stage_that_finds_the_inputs_changed_deletes_the_archive_the_row_names(self):
        self.world.hooks["generate_presigned_url"] = lambda arguments: self.clock.advance(400)
        self.apply()
        first = self.only_build()
        self.assertEqual(list(self.world.objects), [first.archive_key])
        del self.world.hooks["generate_presigned_url"]
        self.configure(render_toml(gate=NO_GATE))
        result = self.apply()
        self.assert_inputs_changed(result, first)
        self.assertEqual(self.world.arguments("delete_object"), [{"Bucket": BUCKET, "Key": first.archive_key, "ExpectedBucketOwner": ACCOUNT}])

    def test_a_resumed_stage_does_not_ask_the_forge_for_a_basis_path_so_a_missing_answer_changes_nothing(self):
        # The row recorded the hash of every basis file when it was inserted: a forge that answers a path as missing
        # (a 404 can come from an unavailable route too) is not asked, and the stage goes on.
        first = self.unstaged_build()
        self.source.errors["raw_file"] = ObservationRefusal("IMAGE_BASIS_PATH_MISSING")
        self.world.console_plan = [(30.0, READY)]
        mark = len(self.source.calls)
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(self.only_build().id, first.id)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertNotIn(("raw_file", "example.lock"), self.source.calls[mark:])

    def test_a_basis_path_that_the_file_gained_ends_the_row_at_once_and_asks_the_forge_for_nothing(self):
        first = self.unstaged_build()
        # The file now names a second basis path that the row did not record.
        paths = 'basis_paths = ["example.lock"]'
        self.assertIn(paths, render_toml())
        self.configure(render_toml().replace(paths, 'basis_paths = ["example.lock", "other.lock"]'))
        mark = len(self.source.calls)
        self.assert_inputs_changed(self.apply(), first)
        self.assertEqual(self.source.calls[mark:], [])

    def test_a_resumed_stage_does_not_depend_on_a_forge_read_that_fails(self):
        first = self.unstaged_build()
        self.source.errors["raw_file"] = ObservationRefusal("FORGEJO_UNAVAILABLE")
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(self.only_build().id, first.id)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)

    # The forge client puts no deadline on a read of a basis file, so the pass deadline passes during the reads only
    # because the clock moves while they run.

    def test_a_resumed_stage_is_not_held_by_basis_reads_that_would_outlast_the_pass(self):
        first = self.unstaged_build()
        self.source.on_read = lambda: self.clock.advance(400)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertNotIn("pass_deadline_exceeded", result["notes"])
        self.assertEqual(self.only_build().id, first.id)
        self.assertEqual(len(self.archive_keys()), 2)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.events("image_build_failed"), [])

    def test_a_resumed_stage_that_finds_the_inputs_changed_ends_the_row_although_reads_would_outlast_the_pass(self):
        # No read is made, so the pass has the time to end the row at once.
        first = self.unstaged_build()
        self.configure(render_toml(gate=NO_GATE))
        self.source.on_read = lambda: self.clock.advance(400)
        self.assert_inputs_changed(self.apply(), first)

    def test_a_resumed_stage_under_the_same_inputs_reads_no_file_and_stages_the_commit_of_the_row(self):
        self.source = CommitSource()
        first = self.unstaged_build()
        self.assertEqual(first.source_sha, SOURCE_SHA)
        # The branch moved on and its head holds another lock file. The row recorded the hash of the first one.
        self.source.sha = "cd" * 20
        self.source.at[self.source.sha] = {"example.lock": b"a lock file of a later commit\n"}
        self.source.reads.clear()
        mark = len(self.source.calls)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        build = self.only_build()
        self.assertEqual((build.id, build.source_sha), (first.id, SOURCE_SHA))
        self.assertEqual(self.source.reads, [])
        # Only the archive of the commit of the row is read: neither a basis file nor the branch head.
        self.assertEqual(self.source.calls[mark:], [("archive", SOURCE_SHA)])
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.events("image_build_failed"), [])

    def test_the_pass_after_a_row_that_ended_for_its_inputs_decides_as_usual(self):
        first = self.unstaged_build()
        edited = SCRIPT + b"# edited\n"
        self.restart(script=edited)
        self.assert_inputs_changed(self.apply(), first)
        # The ended row counts toward the limits like any refused row: the next build waits for the interval.
        self.clock.advance(DAY + HOUR)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        ended, started = self.builds()
        self.assertEqual((ended.id, ended.state, ended.reason_code), (first.id, "failed", "IMAGE_INPUTS_CHANGED"))
        self.assertEqual((started.state, started.script_sha256), ("candidate", image.script_sha256(edited)))
        self.assertNotEqual(started.basis_digest, first.basis_digest)
        self.assertEqual(self.control().consecutive_failures, 0)

    # -- a file that loses its gate while a build waits to be staged again -------------------
    # The gate requirement guards a new launch. A build that has a row is settled by the passes that follow, whatever
    # the file says about the gate by then: the pass ends the row when its inputs changed, and launches only under
    # the inputs that the row was inserted with.

    def unstaged_build_with_an_archive(self):
        """A build whose first pass ran out of time after the upload: its row names an archive and has no builder."""
        self.world.hooks["generate_presigned_url"] = lambda arguments: self.clock.advance(400)
        self.apply()
        del self.world.hooks["generate_presigned_url"]
        first = self.only_build()
        self.assertEqual(list(self.world.objects), [first.archive_key])
        return first

    def test_a_resumed_stage_whose_file_lost_the_gate_and_has_no_opt_out_ends_the_row_instead_of_leaving_it(self):
        first = self.unstaged_build_with_an_archive()
        self.assertEqual(first.gate_configured, 1)
        self.configure(render_toml(gate=""))
        result = self.apply()
        self.assert_inputs_changed(result, first)
        self.assertEqual(self.world.arguments("delete_object"), [{"Bucket": BUCKET, "Key": first.archive_key, "ExpectedBucketOwner": ACCOUNT}])
        self.assertEqual(self.control().consecutive_failures, 0)

    def test_the_pass_after_a_row_that_ended_for_a_lost_gate_is_refused_before_any_client(self):
        first = self.unstaged_build_with_an_archive()
        self.configure(render_toml(gate=""))
        self.assert_inputs_changed(self.apply(), first)
        before = (len(self.world.calls), len(self.sessions), self.source_sessions)
        # No build is in flight now, so the pass is a new launch and the gate requirement refuses it as before.
        self.refused(self.apply, "IMAGE_GATE_REQUIRED")
        self.assertEqual((len(self.world.calls), len(self.sessions), self.source_sessions), before)
        build = self.only_build()
        self.assertEqual((build.id, build.state), (first.id, "failed"))

    def test_a_resumed_stage_whose_file_lost_the_opt_out_ends_the_row_and_launches_nothing(self):
        # The digest does not hold the opt-out, so the inputs of the row still give it. Only the gate requirement
        # can refuse the launch of this build: without it the builder would start with no gate and no opt-out.
        self.configure(render_toml(gate=NO_GATE))
        first = self.unstaged_build_with_an_archive()
        self.assertEqual(first.gate_configured, 0)
        self.configure(render_toml(gate=""))
        self.assertEqual(self.digest(), first.basis_digest)
        result = self.apply()
        self.assert_refused(result, "IMAGE_GATE_REQUIRED")
        build = self.only_build()
        self.assertEqual((build.id, build.state, build.reason_code), (first.id, "failed", "IMAGE_GATE_REQUIRED"))
        self.assertEqual(self.world.arguments("delete_object"), [{"Bucket": BUCKET, "Key": first.archive_key, "ExpectedBucketOwner": ACCOUNT}])
        self.assertEqual(self.world.objects, {})
        self.assertEqual(len(self.archive_keys()), 1)
        self.assertEqual(self.world.launches, [])
        self.assertEqual(self.world.arguments("run_instances"), [])

    def lost_opt_out_with(self, ruin):
        """A resumed stage whose file lost the opt-out while ``ruin`` also breaks the build template."""
        self.configure(render_toml(gate=NO_GATE))
        first = self.unstaged_build_with_an_archive()
        self.configure(render_toml(gate=""))
        ruin()
        result = self.apply()
        self.assert_refused(result, "IMAGE_GATE_REQUIRED")
        build = self.only_build()
        self.assertEqual((build.state, build.reason_code), ("failed", "IMAGE_GATE_REQUIRED"))
        self.assertEqual(self.world.arguments("delete_object"), [{"Bucket": BUCKET, "Key": first.archive_key, "ExpectedBucketOwner": ACCOUNT}])
        self.assertEqual(self.world.arguments("run_instances"), [])

    def test_a_lost_opt_out_is_reported_before_an_unsafe_template(self):
        unsafe = copy.deepcopy(SAFE_BUILDER)
        unsafe["IamInstanceProfile"] = {"Name": "sentinel-profile"}
        self.lost_opt_out_with(lambda: self.world.set_version(BUILDER, 1, unsafe))

    def test_a_lost_opt_out_is_reported_before_a_group_read_that_is_refused(self):
        self.lost_opt_out_with(
            lambda: self.world.fail("describe_security_groups", ProviderError("UnauthorizedOperation"))
        )

    def resumed_stage_with_100_seconds_left(self):
        """A build that waits for its stage to be done again, and the clock 100 seconds short of its deadline.

        The pass that follows advances the clock by 150 seconds inside one call: that passes the build deadline and
        stays inside the pass deadline, so only the build deadline can stop the launch.
        """
        first = self.unstaged_build()
        self.clock.advance(first.deadline_at - 100 - self.clock())
        return first

    def assert_nothing_launched_for_a_build_out_of_time(self, result, first, mark):
        """``mark`` is the call log position before the pass that ran out of time: the first pass deleted its own key."""
        build = self.assert_failed(result, "CONSOLE_BLIND")
        self.assertEqual(build.id, first.id)
        # The archive was staged again, then deleted with the row's end.
        self.assertEqual(len(self.archive_keys()), 2)
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.arguments("terminate_instances"), [])
        self.assertEqual(self.world.objects, {})
        deleted = [kwargs for name, kwargs in self.world.calls[mark:] if name == "delete_object"]
        self.assertEqual(deleted, [{"Bucket": BUCKET, "Key": f"{PREFIX}{first.id}.tar.gz", "ExpectedBucketOwner": ACCOUNT}])
        self.assertEqual(self.control().consecutive_failures, 1)

    def test_a_resumed_stage_whose_upload_outlasts_the_build_deadline_launches_nothing_and_deletes_the_archive(self):
        first = self.resumed_stage_with_100_seconds_left()
        self.world.hooks["complete_multipart_upload"] = lambda arguments: self.clock.advance(150)
        mark = len(self.world.calls)
        result = self.apply()
        self.assert_nothing_launched_for_a_build_out_of_time(result, first, mark)

    def test_a_resumed_stage_whose_last_check_before_the_launch_outlasts_the_deadline_launches_nothing(self):
        first = self.resumed_stage_with_100_seconds_left()
        spent = []

        def run_out_of_time(arguments):
            # The launch lists the builders after it presigned the URL: that listing is the last read before the call.
            if self.world.arguments("generate_presigned_url") and not spent:
                spent.append(True)
                self.clock.advance(150)

        self.world.hooks["describe_instances"] = run_out_of_time
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(spent, [True])
        self.assert_nothing_launched_for_a_build_out_of_time(result, first, mark)

    # -- the build deadline decides before the pass deadline, from the end of the upload on ----------------------
    # A stage that outlasts the build deadline ends the build ``CONSOLE_BLIND`` in the pass that sees it, also when it
    # outlasts the pass deadline too. The pass deadline alone leaves the row for the next pass.

    def assert_no_listing_before_the_row_ends(self, mark):
        """No builder is listed between the completion of the upload and the delete that ends the row.

        The pass that began at call ``mark`` lists builders earlier too (reconcile and the start check), so the
        completion splits the two, and ``_fail`` sends the delete as the first call of the row's end.
        """
        names = self.world.names(mark)
        completed = names.index("complete_multipart_upload")
        deleted = names.index("delete_object", completed)
        self.assertNotIn("describe_instances", names[completed:deleted])

    def a_call_that_takes(self, call, seconds):
        """Make the provider call ``call`` take ``seconds`` of the clock. Returns the times it began at."""
        began = []

        def slow(arguments):
            began.append(self.clock())
            self.clock.advance(seconds)

        self.world.hooks[call] = slow
        return began

    def a_completion_that_takes(self, seconds):
        """Make the completion of the multipart upload take ``seconds`` of the clock. Returns the times it began at."""
        return self.a_call_that_takes("complete_multipart_upload", seconds)

    @contextmanager
    def a_last_listing_of_two_pages_that_takes(self, seconds):
        """Make the listing that the launch runs after it presigned the URL return a first page that took ``seconds``.

        The page promises a second one. The adapter reads the clock before it requests a page, so it refuses the second
        page once the pass deadline has passed. The fake provider never paginates, which would leave that refusal,
        inside the launch, unreachable. Yields the times at which a first page was cut this way.
        """
        real = WorldEc2.describe_instances
        cut = []

        def first_page_of_two(ec2, **arguments):
            response = real(ec2, **arguments)
            if ec2.w.arguments("generate_presigned_url") and not cut and arguments.get("NextToken") is None:
                cut.append(ec2.w.clock())
                ec2.w.clock.advance(seconds)
                response = {**response, "NextToken": "second-page"}
            return response

        with mock.patch.object(WorldEc2, "describe_instances", first_page_of_two):
            yield cut

    def requested_second_pages(self):
        return [call for call in self.world.arguments("describe_instances") if call["NextToken"]]

    def test_a_resumed_stage_whose_upload_outlasts_both_deadlines_ends_the_build_blind_and_deletes_the_archive(self):
        # The completion starts before both deadlines and returns after both. The build deadline decides: the row
        # ends in this pass, and the pass deadline does not leave it for the next pass to stage again.
        first = self.resumed_stage_with_100_seconds_left()
        began = self.a_completion_that_takes(PASS_DEADLINE_SECONDS + 60)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        # The premise: the completion began inside both deadlines and ended after both.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], min(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assertGreaterEqual(self.clock(), max(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assert_nothing_launched_for_a_build_out_of_time(result, first, mark)
        self.assert_no_listing_before_the_row_ends(mark)

    def test_a_resumed_stage_whose_upload_outlasts_the_build_deadline_asks_for_no_builder_listing_before_it_ends(self):
        # Inside the pass deadline the adapter would send the listing. The launch reads the build deadline before it
        # asks for one, because a stage that is out of time can only end in a launch that the deadline then refuses.
        first = self.resumed_stage_with_100_seconds_left()
        began = self.a_completion_that_takes(150)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assert_nothing_launched_for_a_build_out_of_time(result, first, mark)
        self.assert_no_listing_before_the_row_ends(mark)

    def test_a_resumed_stage_whose_upload_stream_passes_the_build_deadline_sends_no_part(self):
        # The first chunk of the stream arrives after the build deadline and before the pass deadline: the adapter sends
        # no part and no completion, and the build ends blind.
        first = self.resumed_stage_with_100_seconds_left()
        waits = []

        def first_chunk_is_slow():
            if not waits:
                waits.append(self.clock())
                self.clock.advance(150)

        self.source.on_chunk = first_chunk_is_slow
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(len(waits), 1)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        names = self.world.names(mark)
        self.assertIn("create_multipart_upload", names)
        self.assertNotIn("upload_part", names)
        self.assertNotIn("complete_multipart_upload", names)
        build = self.assert_failed(result, "CONSOLE_BLIND")
        self.assertEqual(build.id, first.id)
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.objects, {})

    def test_a_resumed_stage_whose_upload_outlasts_only_the_pass_deadline_is_staged_again_by_the_next_pass(self):
        # The control of the two tests above: the same completion, with the build deadline far off, leaves the row for
        # the next pass. The pass deadline alone never ends a build.
        first = self.unstaged_build()
        began = self.a_completion_that_takes(PASS_DEADLINE_SECONDS + 60)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual((result["decision"], result["reason"]), ("building", None), result)
        self.assertIn("pass_deadline_exceeded", result["notes"])
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assert_unstaged(build)
        self.assertLess(self.clock(), build.deadline_at)
        # The row still names the archive that the stage stored, and nothing deleted it.
        self.assertEqual(list(self.world.objects), [build.archive_key])
        self.assertNotIn("delete_object", self.world.names(mark))
        # The next pass stages the archive again under the same key and launches.
        del self.world.hooks["complete_multipart_upload"]
        result = self.apply()
        self.assertEqual(result["decision"], "building", result)
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assertIsNotNone(build.builder_id)
        self.assertEqual(build.archive_sha256, ARCHIVE_SHA)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.events("image_build_failed"), [])

    def test_a_last_listing_that_runs_out_of_both_deadlines_ends_the_build_blind(self):
        # The listing starts inside both deadlines. Its first page returns after both, and the adapter refuses the
        # second page for the pass deadline. The build deadline decides all the same.
        first = self.resumed_stage_with_100_seconds_left()
        started_at = self.clock()
        mark = len(self.world.calls)
        with self.a_last_listing_of_two_pages_that_takes(PASS_DEADLINE_SECONDS + 60) as cut:
            result = self.apply()
        self.assertEqual(len(cut), 1)
        self.assertLess(cut[0], min(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assertGreaterEqual(self.clock(), max(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assertEqual(self.requested_second_pages(), [])
        self.assert_nothing_launched_for_a_build_out_of_time(result, first, mark)

    def test_a_last_listing_whose_first_page_ends_after_only_the_build_deadline_requests_no_second_page(self):
        # The first page returns after the build deadline and before the pass deadline: the adapter refuses the second
        # page for the build deadline, and the build ends blind.
        first = self.resumed_stage_with_100_seconds_left()
        started_at = self.clock()
        mark = len(self.world.calls)
        with self.a_last_listing_of_two_pages_that_takes(150) as cut:
            result = self.apply()
        self.assertEqual(len(cut), 1)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual(self.requested_second_pages(), [])
        self.assert_nothing_launched_for_a_build_out_of_time(result, first, mark)

    def test_a_last_listing_that_runs_out_of_only_the_pass_deadline_leaves_the_build_for_the_next_pass(self):
        # The control of the test above: the same listing, with the build deadline far off, is the pass deadline's
        # refusal, and the row waits for the next pass.
        first = self.unstaged_build()
        started_at = self.clock()
        mark = len(self.world.calls)
        with self.a_last_listing_of_two_pages_that_takes(PASS_DEADLINE_SECONDS + 60) as cut:
            result = self.apply()
        self.assertEqual(len(cut), 1)
        self.assertLess(cut[0], started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual(self.requested_second_pages(), [])
        self.assertEqual((result["decision"], result["reason"]), ("building", None), result)
        self.assertIn("pass_deadline_exceeded", result["notes"])
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assert_unstaged(build)
        self.assertLess(self.clock(), build.deadline_at)
        self.assertEqual(list(self.world.objects), [build.archive_key])
        self.assertNotIn("delete_object", self.world.names(mark))
        # The next pass stages the archive again under the same key and launches.
        result = self.apply()
        self.assertEqual(result["decision"], "building", result)
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assertIsNotNone(build.builder_id)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.events("image_build_failed"), [])

    # -- a deadline decides before the credential does -----------------------------------------------------------------
    # The launch presigns the URL, and the presign refuses ``IMAGE_CREDENTIAL_SHORT`` when the session has too little
    # life left. A completion that ends late can leave the session short too. A stage that is out of time must not end
    # as a refused start then: both deadlines are read before the presign, the build deadline first.

    def a_session_that_runs_short_during_a_completion_that_takes(self, seconds):
        """Make the completion of the upload take ``seconds``, on a session that is then too short for the URL.

        The session has 1850 seconds of life when the pass starts: enough for the check before the upload (the URL
        lives 1800), too little for the presign once the completion has taken 100 seconds or more. Returns the times at
        which the completion began.
        """
        self.world.credential_life = 1850.0
        return self.a_completion_that_takes(seconds)

    def test_a_completion_that_ends_after_the_pass_deadline_leaves_the_row_for_the_next_pass_although_the_session_is_short(self):
        # The pass deadline alone cut the stage, so the row waits. The check of the credential, which would end it, never runs.
        began = self.a_session_that_runs_short_during_a_completion_that_takes(PASS_DEADLINE_SECONDS + 60)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        # The premise: the completion began inside the pass deadline and ended after it.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual((result["decision"], result["reason"]), ("building", None), result)
        self.assertIn("pass_deadline_exceeded", result["notes"])
        build = self.only_build()
        self.assert_unstaged(build)
        self.assertLess(self.clock(), build.deadline_at)
        # Nothing was presigned, and the archive that the stage stored is still there for the next pass.
        self.assertEqual(self.world.urls, [])
        self.assertNotIn("generate_presigned_url", self.world.names(mark))
        self.assertEqual(list(self.world.objects), [build.archive_key])
        self.assertNotIn("delete_object", self.world.names(mark))
        # The next pass has a session that is long enough again: it stages the archive again and launches.
        del self.world.hooks["complete_multipart_upload"]
        result = self.apply()
        self.assertEqual(result["decision"], "building", result)
        launched = self.only_build()
        self.assertEqual(launched.id, build.id)
        self.assertIsNotNone(launched.builder_id)
        self.assertEqual(launched.archive_sha256, ARCHIVE_SHA)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.events("image_build_failed"), [])

    def test_a_completion_that_ends_after_both_deadlines_ends_the_build_blind_although_the_session_is_short(self):
        # The build deadline decides before the pass deadline, and both decide before the credential: the row ends
        # ``CONSOLE_BLIND`` and counts. It is not a refused start.
        first = self.resumed_stage_with_100_seconds_left()
        began = self.a_session_that_runs_short_during_a_completion_that_takes(PASS_DEADLINE_SECONDS + 60)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        # The premise: the completion began inside both deadlines and ended after both.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], min(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assertGreaterEqual(self.clock(), max(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assert_nothing_launched_for_a_build_out_of_time(result, first, mark)
        self.assertEqual(self.world.urls, [])
        self.assertNotIn("generate_presigned_url", self.world.names(mark))

    def test_a_completion_that_ends_after_the_build_deadline_ends_the_build_blind_although_the_session_is_short(self):
        # The control of the test above for a pass deadline that has not passed: the build deadline alone decides.
        first = self.resumed_stage_with_100_seconds_left()
        began = self.a_session_that_runs_short_during_a_completion_that_takes(150)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assert_nothing_launched_for_a_build_out_of_time(result, first, mark)
        self.assertEqual(self.world.urls, [])
        self.assertNotIn("generate_presigned_url", self.world.names(mark))

    # -- the same rule where the pass deadline cuts the stage itself -------------------------------------------------
    # The pass can run out of time in the upload stream and in the checks that come before the stage, as well as in
    # the launch. The build deadline decides there too: a build that is out of time ends ``CONSOLE_BLIND`` in the pass
    # that sees it, and the pass deadline alone leaves the row for the next pass.

    def a_source_that_stalls(self, seconds):
        """Make the source take ``seconds`` of the clock before its first chunk. Returns the times it began at.

        The adapter checks the pass deadline before it takes a chunk, so a stall that ends after that deadline cuts the
        upload: the adapter aborts it, deletes the key itself and refuses with ``DeadlineRefusal``.
        """
        began = []

        def stall():
            began.append(self.clock())
            self.clock.advance(seconds)

        self.source.on_chunk = stall
        return began

    def assert_an_upload_cut_by_both_deadlines_ended_the_build_blind(self, result, mark):
        """The upload was cut and the build ended ``CONSOLE_BLIND`` in the same pass. Returns the row.

        The adapter deletes the key of a cut upload itself, so two deletes follow the abort: its own, and the one that
        ends the row (``_fail``), which is the only one that records the deletion. Nothing else is asked after the cut:
        no listing of the builders and no launch.
        """
        build = self.assert_failed(result, "CONSOLE_BLIND")
        names = self.world.names(mark)
        self.assertNotIn("complete_multipart_upload", names)
        self.assertEqual(
            names[names.index("abort_multipart_upload") :], ["abort_multipart_upload", "delete_object", "delete_object"]
        )
        self.assertNotIn("generate_presigned_url", names)
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.arguments("terminate_instances"), [])
        self.assertEqual(self.world.objects, {})
        self.assertIsNotNone(build.archive_deleted_at)
        self.assertEqual(self.query(lambda state: state.archives_owed(DEPLOYMENT)), [])
        self.assertEqual(self.control().consecutive_failures, 1)
        return build

    def test_a_resumed_stage_whose_upload_stream_is_cut_by_both_deadlines_ends_the_build_blind_and_deletes_the_archive(self):
        # The source stalls before its first chunk, and the stall ends after both deadlines. The adapter refuses for the
        # pass deadline. The build deadline decides all the same: the row ends in this pass, and no later pass has to
        # find it out of time.
        first = self.resumed_stage_with_100_seconds_left()
        began = self.a_source_that_stalls(PASS_DEADLINE_SECONDS + 60)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        # The premise: the stall began inside both deadlines and ended after both.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], min(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assertGreaterEqual(self.clock(), max(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        build = self.assert_an_upload_cut_by_both_deadlines_ended_the_build_blind(result, mark)
        self.assertEqual(build.id, first.id)
        # The first pass opened one upload, and this pass opened the one that it cut.
        self.assertEqual(len(self.archive_keys()), 2)

    def test_a_resumed_stage_whose_upload_stream_is_cut_by_only_the_pass_deadline_is_staged_again_by_the_next_pass(self):
        # The control of the test above: the same stall, with the build deadline far off, leaves the row for the next pass.
        first = self.unstaged_build()
        began = self.a_source_that_stalls(PASS_DEADLINE_SECONDS + 60)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual((result["decision"], result["reason"]), ("building", None), result)
        self.assertIn("pass_deadline_exceeded", result["notes"])
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assert_unstaged(build)
        self.assertLess(self.clock(), build.deadline_at)
        # Only the adapter cleaned up after the cut. Nothing ended the row, so nothing recorded a deletion.
        names = self.world.names(mark)
        self.assertEqual(names[names.index("abort_multipart_upload") :], ["abort_multipart_upload", "delete_object"])
        self.assertIsNone(build.archive_deleted_at)
        # The next pass stages the archive again under the same key and launches.
        self.source.on_chunk = None
        result = self.apply()
        self.assertEqual(result["decision"], "building", result)
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assertIsNotNone(build.builder_id)
        self.assertEqual(build.archive_sha256, ARCHIVE_SHA)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.events("image_build_failed"), [])

    def test_a_first_stage_whose_upload_stream_is_cut_by_both_deadlines_ends_the_build_blind_and_deletes_the_archive(self):
        # A new row is staged from the row that was inserted a moment before, which holds no key yet: the delete that
        # ends the build names the key that the stage recorded before it opened the upload. The clock jumps past the
        # build deadline, as it does on a host that was suspended during the pass.
        began = self.a_source_that_stalls(BUILD_DEADLINE + 60)
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(len(began), 1)
        build = self.assert_an_upload_cut_by_both_deadlines_ended_the_build_blind(result, mark)
        deleted = [kwargs for name, kwargs in self.world.calls[mark:] if name == "delete_object"]
        request = {"Bucket": BUCKET, "Key": f"{PREFIX}{build.id}.tar.gz", "ExpectedBucketOwner": ACCOUNT}
        self.assertEqual(deleted, [request, request])
        self.assertEqual(len(self.archive_keys()), 1)

    def assert_ended_blind_before_a_second_stage(self, result, first, mark):
        """A check that runs before the stage ran out of both deadlines: the row ends ``CONSOLE_BLIND`` in the same pass.

        No archive is staged again. The delete that ends the row is the only one, and it names the key that the first
        stage recorded.
        """
        build = self.assert_failed(result, "CONSOLE_BLIND")
        self.assertEqual(build.id, first.id)
        self.assertNotIn("create_multipart_upload", self.world.names(mark))
        self.assertEqual(len(self.archive_keys()), 1)
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.arguments("terminate_instances"), [])
        self.assertEqual(self.world.objects, {})
        deleted = [kwargs for name, kwargs in self.world.calls[mark:] if name == "delete_object"]
        self.assertEqual(deleted, [{"Bucket": BUCKET, "Key": f"{PREFIX}{first.id}.tar.gz", "ExpectedBucketOwner": ACCOUNT}])
        self.assertIsNotNone(build.archive_deleted_at)
        self.assertEqual(self.control().consecutive_failures, 1)

    def test_a_resumed_stage_whose_template_check_outlasts_both_deadlines_ends_the_build_blind_and_deletes_the_archive(self):
        # The first read of the check that runs before the stage ends after both deadlines, and the next read is the
        # one that the adapter refuses for the pass deadline. The control is
        # ``test_a_resumed_stage_whose_template_check_runs_out_of_time_leaves_the_build_for_the_next_pass``.
        first = self.resumed_stage_with_100_seconds_left()
        began = self.a_call_that_takes("describe_launch_templates", PASS_DEADLINE_SECONDS + 60)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], min(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assertGreaterEqual(self.clock(), max(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assert_ended_blind_before_a_second_stage(result, first, mark)

    def test_a_resumed_stage_whose_start_checks_outlast_both_deadlines_end_the_build_blind_and_delete_the_archive(self):
        # The last read of the template check ends after both deadlines, so the listing of the builders that the start
        # checks make next is the one that the adapter refuses for the pass deadline.
        first = self.resumed_stage_with_100_seconds_left()
        began = self.a_call_that_takes("describe_security_groups", PASS_DEADLINE_SECONDS + 60)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], min(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assertGreaterEqual(self.clock(), max(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assert_ended_blind_before_a_second_stage(result, first, mark)
        names = self.world.names(mark)
        self.assertNotIn("describe_instances", names[names.index("describe_security_groups") :])

    def test_a_resumed_stage_whose_start_checks_outlast_only_the_pass_deadline_leave_the_build_for_the_next_pass(self):
        # The control of the test above: the same read, with the build deadline far off, leaves the row for the next pass.
        first = self.unstaged_build()
        began = self.a_call_that_takes("describe_security_groups", PASS_DEADLINE_SECONDS + 60)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual((result["decision"], result["reason"]), ("building", None), result)
        self.assertIn("pass_deadline_exceeded", result["notes"])
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assert_unstaged(build)
        self.assertLess(self.clock(), build.deadline_at)
        names = self.world.names(mark)
        self.assertNotIn("describe_instances", names[names.index("describe_security_groups") :])
        self.assertNotIn("create_multipart_upload", names)
        self.assertNotIn("delete_object", names)
        # The next pass reads again, stages the archive again and launches.
        del self.world.hooks["describe_security_groups"]
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(self.only_build().id, first.id)
        self.assertEqual(len(self.archive_keys()), 2)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.events("image_build_failed"), [])

    def test_a_template_read_that_ends_after_the_build_deadline_starts_no_later_read_of_the_check(self):
        # The first read of the check returns after the build deadline and before the pass deadline. The build is out
        # of time, so the adapter does not call the next read of the check, and the row ends ``CONSOLE_BLIND``.
        first = self.resumed_stage_with_100_seconds_left()
        began = self.a_call_that_takes("describe_launch_templates", 150)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], first.deadline_at)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assert_ended_blind_before_a_second_stage(result, first, mark)
        later = self.world.calls[mark:][self.world.names(mark).index("describe_launch_templates") + 1 :]
        self.assertNotIn("describe_security_groups", [name for name, _ in later])
        versions = [kwargs for name, kwargs in later if name == "describe_launch_template_versions"]
        self.assertNotIn(BUILDER, [kwargs.get("LaunchTemplateName") for kwargs in versions])

    # -- the pin read before a stage done again takes time too ---------------------------------------------------------
    # ``_run`` reads the pin before it advances the active build. When that read ends at or after the pass deadline, the
    # checks of a stage done again decide nothing: the row waits for the next pass, whatever the inputs say.

    def a_pin_image_read_that_ends(self, at):
        """Make the read of the pin's image, the last read of the pin, end at ``at`` seconds after the pass starts.

        Returns the clock readings at which the read began.
        """
        began, started_at = [], self.clock()

        def slow(arguments):
            if arguments.get("ImageIds") == [ami(0xA1)]:
                began.append(self.clock())
                self.clock.advance(started_at + at - self.clock())

        self.world.hooks["describe_images"] = slow
        return began

    def test_a_pin_read_that_ends_at_the_pass_deadline_leaves_a_resumed_stage_with_changed_inputs_for_the_next_pass(self):
        first = self.unstaged_build()
        paths = 'basis_paths = ["example.lock"]'
        self.configure(render_toml().replace(paths, 'basis_paths = ["example.lock", "other.lock"]'))
        started_at = self.clock()
        began = self.a_pin_image_read_that_ends(PASS_DEADLINE_SECONDS)
        mark = len(self.world.calls)
        result = self.apply()
        # The premise: the pin read began inside the pass deadline and ended at it.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual((result["decision"], result["reason"]), ("building", None), result)
        self.assertIn("pass_deadline_exceeded", result["notes"])
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assert_unstaged(build)
        self.assertIsNone(build.archive_deleted_at)
        self.assertNotIn("delete_object", self.world.names(mark))
        # The next pass reads the pin in time and ends the row on the changed inputs.
        del self.world.hooks["describe_images"]
        self.assert_inputs_changed(self.apply(), first)

    def test_a_pin_read_that_ends_inside_the_pass_deadline_still_ends_a_resumed_stage_with_changed_inputs(self):
        # The control of the test above: one second earlier, the changed inputs decide.
        first = self.unstaged_build()
        paths = 'basis_paths = ["example.lock"]'
        self.configure(render_toml().replace(paths, 'basis_paths = ["example.lock", "other.lock"]'))
        started_at = self.clock()
        began = self.a_pin_image_read_that_ends(PASS_DEADLINE_SECONDS - 1)
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assert_inputs_changed(result, first)

    # -- a deadline decides before the credential in the start checks too ---------------------------------------------
    # The checks of a launch run after the template read, which takes time, and the first of them reads the life of the
    # session. A session that aged during that read must not end the row as a refused start once a deadline has passed:
    # the pass deadline alone leaves the row for the next pass, and the build deadline ends it ``CONSOLE_BLIND``. Before
    # a row exists nothing is staged, and the pass deadline ends the pass.

    def a_session_that_runs_short_during_a_call_that_takes(self, call, seconds):
        """Make the provider call ``call`` take ``seconds``, on a session that is then too short for the URL.

        The session has 1850 seconds of life when the pass starts: enough for the URL (it lives 1800) until ``call`` has
        taken 100 seconds or more. Returns the times at which ``call`` began.
        """
        self.world.credential_life = 1850.0
        return self.a_call_that_takes(call, seconds)

    def test_a_template_read_that_ends_after_the_pass_deadline_leaves_a_resumed_stage_for_the_next_pass_although_the_session_is_short(self):
        # The pass deadline alone cut the stage, so the row waits. The check of the credential, which would end it, never runs.
        first = self.unstaged_build()
        began = self.a_session_that_runs_short_during_a_call_that_takes("describe_security_groups", PASS_DEADLINE_SECONDS + 60)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        # The premise: the last read of the template check began inside the pass deadline and ended after it.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual((result["decision"], result["reason"]), ("building", None), result)
        self.assertIn("pass_deadline_exceeded", result["notes"])
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assert_unstaged(build)
        self.assertLess(self.clock(), build.deadline_at)
        # Nothing ended the row: no delete of its archive, no second upload and no listing of the builders after the read.
        names = self.world.names(mark)
        self.assertNotIn("describe_instances", names[names.index("describe_security_groups") :])
        self.assertNotIn("create_multipart_upload", names)
        self.assertNotIn("delete_object", names)
        # The next pass has a session that is long enough again: it stages the archive again and launches.
        del self.world.hooks["describe_security_groups"]
        self.world.credential_life = float(HOUR)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(self.only_build().id, first.id)
        self.assertEqual(len(self.archive_keys()), 2)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.events("image_build_failed"), [])

    def test_a_template_read_that_ends_after_both_deadlines_ends_a_resumed_stage_blind_although_the_session_is_short(self):
        # The build deadline decides before the pass deadline, and both decide before the credential: the row ends
        # ``CONSOLE_BLIND`` and counts. It is not a refused start.
        first = self.resumed_stage_with_100_seconds_left()
        began = self.a_session_that_runs_short_during_a_call_that_takes("describe_security_groups", PASS_DEADLINE_SECONDS + 60)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        # The premise: the read began inside both deadlines and ended after both.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], min(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assertGreaterEqual(self.clock(), max(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assert_ended_blind_before_a_second_stage(result, first, mark)
        names = self.world.names(mark)
        self.assertNotIn("describe_instances", names[names.index("describe_security_groups") :])

    def test_a_template_read_that_ends_after_the_build_deadline_ends_a_resumed_stage_blind_although_the_session_is_short(self):
        # The control of the test above for a pass deadline that has not passed: the build deadline alone decides.
        first = self.resumed_stage_with_100_seconds_left()
        began = self.a_session_that_runs_short_during_a_call_that_takes("describe_security_groups", 150)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], first.deadline_at)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assert_ended_blind_before_a_second_stage(result, first, mark)
        names = self.world.names(mark)
        self.assertNotIn("describe_instances", names[names.index("describe_security_groups") :])

    def test_a_template_read_that_ends_inside_both_deadlines_still_ends_a_resumed_stage_on_a_short_session(self):
        # The control of the three tests above: the same read inside both deadlines leaves the credential to decide.
        first = self.unstaged_build()
        began = self.a_session_that_runs_short_during_a_call_that_takes("describe_security_groups", 100)
        started_at = self.clock()
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertLess(self.clock(), min(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assert_refused(result, "IMAGE_CREDENTIAL_SHORT")
        build = self.only_build()
        self.assertEqual((build.id, build.state, build.reason_code), (first.id, "failed", "IMAGE_CREDENTIAL_SHORT"))
        self.assertEqual(len(self.archive_keys()), 1)
        self.assertEqual(self.world.arguments("run_instances"), [])

    def test_a_template_read_that_ends_after_the_pass_deadline_ends_a_new_start_on_the_deadline_although_the_session_is_short(self):
        # No row exists, so the credential check could only change the answer: the deadline decides before it all the same.
        began = self.a_session_that_runs_short_during_a_call_that_takes("describe_security_groups", PASS_DEADLINE_SECONDS + 60)
        started_at = self.clock()
        self.refused(self.apply, "pass_deadline_exceeded")
        # The premise: the read began inside the pass deadline and ended after it.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        # Nothing was written or started, and the listing of the builders did not follow the read.
        self.assertEqual(self.builds(), [])
        self.assertEqual(self.world.arguments("create_multipart_upload"), [])
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.urls, [])
        names = self.world.names()
        self.assertNotIn("describe_instances", names[names.index("describe_security_groups") :])

    def test_a_template_read_that_ends_inside_the_pass_deadline_still_refuses_a_new_start_on_a_short_session(self):
        # The control of the test above: the same read inside the pass deadline leaves the credential to decide.
        began = self.a_session_that_runs_short_during_a_call_that_takes("describe_security_groups", 100)
        started_at = self.clock()
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assert_refused(result, "IMAGE_CREDENTIAL_SHORT")
        self.assertEqual(self.builds(), [])

    # -- a deadline decides before the outcome of any slow call --------------------------------------------------------
    # One rule runs through the start, the restage and the launch: when a call that can take time returns, both
    # deadlines are read again before the pipeline acts on what the call produced (a row, an upload, a presign, a
    # refusal), the build deadline first. The sections above show it for the upload, for the template read and for the
    # last listing of the launch. These show it for the builder listing of the start checks (its result and its
    # refusal), for the lookup of the credential (in the start checks and in the presign), and for the refusals that an
    # upload and a template read produce. A listing that shows a builder is the one outcome that stays as it was:
    # ``IMAGE_BUILD_ALREADY_ACTIVE`` whenever it returns.

    def a_start_listing_that_takes(self, seconds, mark, *, refused=False):
        """Make the builder listing of the start checks take ``seconds`` of the clock, once.

        Returns the times it began at.

        Reconcile lists the builders first and the launch lists them again: the listing of the start checks is the first
        one after the read of the build template in the pass that began at call ``mark``. With ``refused`` the provider
        throttles it when the time has passed (``AWS_UNCERTAIN``).
        """
        began = []

        def slow(arguments):
            if "describe_security_groups" in self.world.names(mark) and not began:
                began.append(self.clock())
                self.clock.advance(seconds)
                if refused:
                    self.world.fail("describe_instances", ProviderError("RequestLimitExceeded"), times=1)

        self.world.hooks["describe_instances"] = slow
        return began

    def a_launch_listing_that_takes(self, seconds, *, refused):
        """Make the builder listing that the launch runs after it presigned the URL take ``seconds``, once.

        With ``refused`` the provider throttles it when the time has passed (``AWS_UNCERTAIN``). Returns the times it
        began at.
        """
        began = []

        def slow(arguments):
            if self.world.arguments("generate_presigned_url") and not began:
                began.append(self.clock())
                self.clock.advance(seconds)
                if refused:
                    self.world.fail("describe_instances", ProviderError("RequestLimitExceeded"), times=1)

        self.world.hooks["describe_instances"] = slow
        return began

    def credential_lookups_that_take(self, stalls):
        """From now on a pass is handed an adapter whose lookups of the credential's expiry take time.

        The adapter is ``SlowLookupAws``.

        ``stalls`` maps the number of a lookup in the pass to its seconds, and a test can change it for a later pass.
        The session has 1850 seconds of life when the pass starts: enough for the URL (it lives 1800) until a lookup has
        taken 51 seconds or more. The start checks make the first lookup of a pass and the presign makes the second.
        Returns the adapters that the passes were handed.
        """
        self.world.credential_life = 1850.0
        adapters = []

        def factory():
            adapter = SlowLookupAws(self.world, stalls)
            self.sessions.append(adapter)
            adapters.append(adapter)
            return adapter

        self.aws_factory = factory
        self.restart()
        return adapters

    def test_a_start_listing_that_returns_empty_after_the_pass_deadline_inserts_no_row(self):
        # No row exists yet, and the listing is the last call before the insert: the pass ends on its deadline instead.
        began = self.a_start_listing_that_takes(PASS_DEADLINE_SECONDS + 60, 0)
        started_at = self.clock()
        self.refused(self.apply, "pass_deadline_exceeded")
        # The premise: the listing began inside the pass deadline and ended after it.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        # Nothing was written or started: no row, no upload and no builder.
        self.assertEqual(self.builds(), [])
        self.assertEqual(self.world.arguments("create_multipart_upload"), [])
        self.assertEqual(self.world.arguments("run_instances"), [])

    def test_a_start_listing_that_returns_empty_inside_the_pass_deadline_still_inserts_the_row(self):
        # The control of the test above: the same listing, quicker, starts the build.
        began = self.a_start_listing_that_takes(PASS_DEADLINE_SECONDS - 60, 0)
        started_at = self.clock()
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual((result["decision"], result["reason"]), ("building", None), result)
        self.assertEqual(len(self.builds()), 1)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)

    def test_a_start_listing_that_returns_empty_after_the_build_deadline_stages_nothing_and_ends_the_build_blind(self):
        # A resumed stage: the late return of the listing is the last call before the upload. The build deadline has
        # passed, so no upload starts (the pass deadline has not: the adapter would send it).
        first = self.resumed_stage_with_100_seconds_left()
        mark = len(self.world.calls)
        began = self.a_start_listing_that_takes(150, mark)
        started_at = self.clock()
        result = self.apply()
        # The premise: the listing began before the build deadline and ended after it, inside the pass deadline.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], first.deadline_at)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assert_ended_blind_before_a_second_stage(result, first, mark)

    def test_a_start_listing_that_returns_empty_after_the_build_deadline_lets_no_bucket_refusal_win(self):
        # The upload that the late return would have started is refused by S3 (the bucket is another account's now).
        # The build was out of time before the upload began, so the refusal that the upload would have produced is not
        # the answer: the build fails ``CONSOLE_BLIND`` and counts, and no refused start ends the row.
        first = self.resumed_stage_with_100_seconds_left()
        self.world.fail("create_multipart_upload", ProviderError("AccessDenied"))
        mark = len(self.world.calls)
        began = self.a_start_listing_that_takes(150, mark)
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assert_ended_blind_before_a_second_stage(result, first, mark)

    def test_a_start_listing_that_returns_empty_after_both_deadlines_ends_a_resumed_stage_blind(self):
        # The control for the order: both deadlines have passed, and the build deadline decides.
        first = self.resumed_stage_with_100_seconds_left()
        mark = len(self.world.calls)
        began = self.a_start_listing_that_takes(PASS_DEADLINE_SECONDS + 60, mark)
        started_at = self.clock()
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], min(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assertGreaterEqual(self.clock(), max(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assert_ended_blind_before_a_second_stage(result, first, mark)

    def test_a_start_listing_that_returns_empty_after_only_the_pass_deadline_leaves_a_resumed_stage_for_the_next_pass(self):
        # The control of the tests above for a build deadline that is far off: the pass deadline alone leaves the row.
        first = self.unstaged_build()
        mark = len(self.world.calls)
        began = self.a_start_listing_that_takes(PASS_DEADLINE_SECONDS + 60, mark)
        started_at = self.clock()
        result = self.apply()
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual((result["decision"], result["reason"]), ("building", None), result)
        self.assertIn("pass_deadline_exceeded", result["notes"])
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assert_unstaged(build)
        self.assertLess(self.clock(), build.deadline_at)
        names = self.world.names(mark)
        self.assertNotIn("create_multipart_upload", names)
        self.assertNotIn("delete_object", names)

    def test_a_start_listing_that_is_refused_after_the_pass_deadline_ends_a_new_start_on_the_deadline(self):
        # The refusal of a late listing is no more the answer than its result is: no row exists, so the deadline
        # decides.
        began = self.a_start_listing_that_takes(PASS_DEADLINE_SECONDS + 60, 0, refused=True)
        started_at = self.clock()
        self.refused(self.apply, "pass_deadline_exceeded")
        # The premise: the listing began inside the pass deadline, and the provider refused it after the deadline.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual(self.builds(), [])
        self.assertEqual(self.world.arguments("create_multipart_upload"), [])
        self.assertEqual(self.world.arguments("run_instances"), [])

    def test_a_start_listing_that_is_refused_inside_the_pass_deadline_still_raises_the_refusal(self):
        # The control of the test above: the same refusal, quicker, is the answer of the pass.
        began = self.a_start_listing_that_takes(10, 0, refused=True)
        self.refused(self.apply, "AWS_UNCERTAIN")
        self.assertEqual(len(began), 1)
        self.assertEqual(self.builds(), [])

    def test_a_start_listing_that_is_refused_after_the_build_deadline_ends_a_resumed_stage_blind(self):
        # The build deadline decides before the refusal of the provider does: the row ends ``CONSOLE_BLIND`` and counts.
        first = self.resumed_stage_with_100_seconds_left()
        mark = len(self.world.calls)
        began = self.a_start_listing_that_takes(150, mark, refused=True)
        started_at = self.clock()
        result = self.apply()
        # The premise: the listing began before the build deadline, and the provider refused it after it.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], first.deadline_at)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assert_ended_blind_before_a_second_stage(result, first, mark)

    def test_a_start_listing_that_is_refused_after_only_the_pass_deadline_leaves_a_resumed_stage_for_the_next_pass(self):
        # The control of the test above for a build deadline that is far off: the pass deadline alone leaves the row,
        # and the refusal of the provider is not acted on (it would have failed the build and counted).
        first = self.unstaged_build()
        mark = len(self.world.calls)
        began = self.a_start_listing_that_takes(PASS_DEADLINE_SECONDS + 60, mark, refused=True)
        started_at = self.clock()
        result = self.apply()
        # The premise: the listing began inside the pass deadline, and the provider refused it after the deadline.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual((result["decision"], result["reason"]), ("building", None), result)
        self.assertIn("pass_deadline_exceeded", result["notes"])
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assert_unstaged(build)
        self.assertLess(self.clock(), build.deadline_at)
        names = self.world.names(mark)
        self.assertNotIn("create_multipart_upload", names)
        self.assertNotIn("delete_object", names)

    def test_a_launch_listing_that_is_refused_after_the_pass_deadline_leaves_the_row_for_the_next_pass(self):
        # The pass deadline alone cut the stage: the refusal is not acted on, and the row waits with its archive.
        first = self.unstaged_build()
        began = self.a_launch_listing_that_takes(PASS_DEADLINE_SECONDS + 60, refused=True)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        # The premise: the last listing began inside the pass deadline, and the provider refused it after the deadline.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual((result["decision"], result["reason"]), ("building", None), result)
        self.assertIn("pass_deadline_exceeded", result["notes"])
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assert_unstaged(build)
        self.assertLess(self.clock(), build.deadline_at)
        self.assertNotIn("delete_object", self.world.names(mark))
        self.assertEqual(list(self.world.objects), [build.archive_key])
        self.assertIsNone(build.archive_deleted_at)

    def test_a_launch_listing_that_is_refused_after_the_build_deadline_ends_the_build_blind(self):
        first = self.resumed_stage_with_100_seconds_left()
        began = self.a_launch_listing_that_takes(150, refused=True)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        # The premise: the last listing began before the build deadline, and the provider refused it after it.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], first.deadline_at)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assert_nothing_launched_for_a_build_out_of_time(result, first, mark)

    def test_a_launch_listing_that_is_refused_inside_both_deadlines_still_fails_the_build_with_its_code(self):
        # The control of the two tests above: the same refusal, quicker, ends the build with the code of the provider.
        first = self.unstaged_build()
        began = self.a_launch_listing_that_takes(10, refused=True)
        result = self.apply()
        self.assertEqual(len(began), 1)
        build = self.assert_failed(result, "AWS_UNCERTAIN")
        self.assertEqual(build.id, first.id)
        self.assertEqual(self.world.arguments("run_instances"), [])

    def test_a_credential_lookup_that_ends_after_the_pass_deadline_ends_a_new_start_on_the_deadline(self):
        # No row exists, so the refusal that the late lookup produces could only change the answer: the deadline
        # decides.
        adapters = self.credential_lookups_that_take({1: PASS_DEADLINE_SECONDS + 60})
        started_at = self.clock()
        self.refused(self.apply, "pass_deadline_exceeded")
        # The premise: the lookup began inside the pass deadline and ended after it.
        (began,) = adapters[-1].began
        self.assertLess(began, started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        # Nothing was written or started.
        self.assertEqual(self.builds(), [])
        self.assertEqual(self.world.arguments("create_multipart_upload"), [])
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.urls, [])

    def test_a_credential_lookup_that_ends_inside_the_pass_deadline_still_refuses_a_new_start_on_a_short_session(self):
        # The control of the test above: the same lookup, quicker, leaves the credential to decide.
        adapters = self.credential_lookups_that_take({1: 100})
        started_at = self.clock()
        result = self.apply()
        self.assertEqual(len(adapters[-1].began), 1)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assert_refused(result, "IMAGE_CREDENTIAL_SHORT")
        self.assertEqual(self.builds(), [])

    def test_a_credential_lookup_that_ends_after_the_pass_deadline_leaves_a_resumed_stage_for_the_next_pass(self):
        # The pass deadline alone cut the stage, so the row waits. The refusal of the credential, which would end it, is
        # not acted on.
        first = self.unstaged_build()
        stalls = {1: PASS_DEADLINE_SECONDS + 60}
        adapters = self.credential_lookups_that_take(stalls)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        # The premise: the lookup began inside the pass deadline and ended after it.
        (began,) = adapters[-1].began
        self.assertLess(began, started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual((result["decision"], result["reason"]), ("building", None), result)
        self.assertIn("pass_deadline_exceeded", result["notes"])
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assert_unstaged(build)
        self.assertLess(self.clock(), build.deadline_at)
        # Nothing ended the row: no delete of its archive, no second upload and no URL.
        names = self.world.names(mark)
        self.assertNotIn("create_multipart_upload", names)
        self.assertNotIn("delete_object", names)
        self.assertIsNone(build.archive_deleted_at)
        self.assertEqual(self.world.urls, [])
        # The next pass has a long session and a quick lookup: it stages the archive again and launches.
        stalls.clear()
        self.world.credential_life = float(HOUR)
        result = self.apply()
        self.assertEqual(result["decision"], "building", result)
        launched = self.only_build()
        self.assertEqual(launched.id, first.id)
        self.assertIsNotNone(launched.builder_id)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.events("image_build_failed"), [])

    def test_a_credential_lookup_that_ends_after_the_build_deadline_ends_a_resumed_stage_blind(self):
        # The build deadline decides before the credential does: the row ends ``CONSOLE_BLIND`` and counts. It is not a
        # refused start.
        first = self.resumed_stage_with_100_seconds_left()
        adapters = self.credential_lookups_that_take({1: 150})
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        # The premise: the lookup began before the build deadline and ended after it, inside the pass deadline.
        (began,) = adapters[-1].began
        self.assertLess(began, first.deadline_at)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assert_ended_blind_before_a_second_stage(result, first, mark)

    def test_a_credential_lookup_that_ends_after_both_deadlines_ends_a_resumed_stage_blind(self):
        # The build deadline decides before the pass deadline, and both decide before the credential.
        first = self.resumed_stage_with_100_seconds_left()
        adapters = self.credential_lookups_that_take({1: PASS_DEADLINE_SECONDS + 60})
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        (began,) = adapters[-1].began
        self.assertLess(began, min(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assertGreaterEqual(self.clock(), max(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assert_ended_blind_before_a_second_stage(result, first, mark)

    def test_a_credential_lookup_that_ends_inside_both_deadlines_still_ends_a_resumed_stage_on_a_short_session(self):
        # The control of the tests above: the same lookup, quicker, leaves the credential to decide.
        first = self.unstaged_build()
        adapters = self.credential_lookups_that_take({1: 100})
        started_at = self.clock()
        result = self.apply()
        self.assertEqual(len(adapters[-1].began), 1)
        self.assertLess(self.clock(), min(first.deadline_at, started_at + PASS_DEADLINE_SECONDS))
        self.assert_refused(result, "IMAGE_CREDENTIAL_SHORT")
        build = self.only_build()
        self.assertEqual((build.id, build.state, build.reason_code), (first.id, "failed", "IMAGE_CREDENTIAL_SHORT"))
        self.assertEqual(self.world.arguments("run_instances"), [])

    def test_a_presign_whose_credential_lookup_ends_after_the_pass_deadline_leaves_the_row_and_keeps_the_archive(self):
        # A new start: the lookup of the start checks is quick, the upload completes, and the lookup of the presign is
        # the slow one. The pass deadline alone cut the stage, so the row waits for the next pass.
        adapters = self.credential_lookups_that_take({2: PASS_DEADLINE_SECONDS + 60})
        started_at = self.clock()
        result = self.apply()
        # The premise: the second lookup of the pass began inside the pass deadline and ended after it.
        self.assertEqual(adapters[-1].lookups, 2)
        (began,) = adapters[-1].began
        self.assertLess(began, started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assertEqual((result["decision"], result["reason"]), ("building", None), result)
        self.assertIn("pass_deadline_exceeded", result["notes"])
        build = self.only_build()
        self.assert_unstaged(build)
        self.assertLess(self.clock(), build.deadline_at)
        # The archive that the stage stored is still there for the next pass, and no URL was made.
        self.assertEqual(list(self.world.objects), [build.archive_key])
        self.assertNotIn("delete_object", self.world.names())
        self.assertIsNone(build.archive_deleted_at)
        self.assertEqual(self.world.urls, [])

    def test_a_presign_whose_credential_lookup_ends_after_the_build_deadline_ends_the_build_blind(self):
        first = self.resumed_stage_with_100_seconds_left()
        adapters = self.credential_lookups_that_take({2: 150})
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        # The premise: the lookup of the presign began before the build deadline and ended after it.
        self.assertEqual(adapters[-1].lookups, 2)
        (began,) = adapters[-1].began
        self.assertLess(began, first.deadline_at)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assert_nothing_launched_for_a_build_out_of_time(result, first, mark)
        self.assertEqual(self.world.urls, [])

    def test_a_presign_whose_credential_lookup_ends_after_the_build_deadline_signs_no_url_on_a_long_session(self):
        # The session outlives the URL, so the lookup does not refuse: the deadline alone keeps the URL from being made.
        first = self.resumed_stage_with_100_seconds_left()
        adapters = self.credential_lookups_that_take({2: 150})
        self.world.credential_life = float(HOUR)
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(adapters[-1].lookups, 2)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assert_nothing_launched_for_a_build_out_of_time(result, first, mark)
        self.assertNotIn("generate_presigned_url", self.world.names(mark))
        self.assertEqual(self.world.urls, [])

    def test_a_presign_whose_credential_lookup_ends_inside_both_deadlines_still_refuses_the_start(self):
        # The control of the two tests above: the same lookup, quicker, leaves the credential to decide.
        adapters = self.credential_lookups_that_take({2: 100})
        started_at = self.clock()
        result = self.apply()
        self.assertEqual(adapters[-1].lookups, 2)
        self.assertEqual(len(adapters[-1].began), 1)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assert_refused(result, "IMAGE_CREDENTIAL_SHORT")
        build = self.only_build()
        self.assertEqual((build.state, build.reason_code), ("failed", "IMAGE_CREDENTIAL_SHORT"))
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.world.urls, [])

    def test_a_refused_upload_that_returns_after_the_build_deadline_ends_the_build_blind_not_as_a_refused_start(self):
        # S3 refuses the upload of a resumed stage (the bucket is another account's now), and the refusal returns after
        # the build deadline: the build was out of time when the answer came. It fails ``CONSOLE_BLIND`` and counts.
        first = self.resumed_stage_with_100_seconds_left()
        began = self.a_call_that_takes("create_multipart_upload", 150)
        self.world.fail("create_multipart_upload", ProviderError("AccessDenied"))
        started_at = self.clock()
        result = self.apply()
        # The premise: the upload began before the build deadline, and its refusal returned after it.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], first.deadline_at)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        build = self.assert_failed(result, "CONSOLE_BLIND")
        self.assertEqual(build.id, first.id)
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.control().consecutive_failures, 1)

    def test_an_unsafe_template_found_after_the_build_deadline_ends_a_resumed_stage_blind_not_as_a_refused_start(self):
        # The last read of the template check returns after the build deadline, and the template is unsafe: the build
        # was out of time when the answer came. It fails ``CONSOLE_BLIND`` and counts.
        first = self.resumed_stage_with_100_seconds_left()
        self.world.add_group(BUILD_GROUP, "example-build-group", ingress=1)
        began = self.a_call_that_takes("describe_security_groups", 150)
        started_at = self.clock()
        mark = len(self.world.calls)
        result = self.apply()
        # The premise: the read began before the build deadline and ended after it, inside the pass deadline.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], first.deadline_at)
        self.assertGreaterEqual(self.clock(), first.deadline_at)
        self.assertLess(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        self.assert_ended_blind_before_a_second_stage(result, first, mark)

    def test_an_unsafe_template_found_after_the_pass_deadline_ends_a_new_start_on_the_deadline(self):
        # No row exists, so the refusal that the late read produces could only change the answer: the deadline decides.
        # The control is ``test_an_unsafe_group_stops_a_pass_before_anything_is_staged``: the same group, read in time.
        self.world.add_group(BUILD_GROUP, "example-build-group", ingress=1)
        began = self.a_call_that_takes("describe_security_groups", PASS_DEADLINE_SECONDS + 60)
        started_at = self.clock()
        self.refused(self.apply, "pass_deadline_exceeded")
        # The premise: the read began inside the pass deadline and ended after it.
        self.assertEqual(len(began), 1)
        self.assertLess(began[0], started_at + PASS_DEADLINE_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        # Nothing was written or started.
        self.assertEqual(self.builds(), [])
        self.assertEqual(self.world.arguments("create_multipart_upload"), [])
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

    def test_a_retry_after_the_pin_moved_sends_the_request_that_its_token_was_first_used_with(self):
        # The version is created and the pass deadline cuts its read-back. The operator then moves the pin.
        self.world.console_plan = [(30.0, READY)]
        self.world.set_version(WORKER, 4, copy.deepcopy(self.world.versions[(WORKER, PIN)]["LaunchTemplateData"]))

        def run_out_of_time(arguments):
            del self.world.hooks["create_launch_template_version"]
            self.clock.advance(PASS_DEADLINE_SECONDS)

        self.world.hooks["create_launch_template_version"] = run_out_of_time
        result = self.apply()
        self.assertEqual(result["decision"], "imaging")
        self.assertIn("pass_deadline_exceeded", result["notes"])
        pinned = f"launch_template_version = {PIN}\n"
        text = self.config_path.read_text(encoding="utf-8")
        self.assertEqual(text.count(pinned), 1)
        self.config_path.write_text(text.replace(pinned, "launch_template_version = 4\n"), encoding="utf-8")
        self.restart()
        result = self.apply()
        self.assertEqual(result["decision"], "candidate")
        first, retry = self.world.arguments("create_launch_template_version")
        self.assertEqual(retry, first)
        self.assertEqual(retry["SourceVersion"], str(PIN))
        build = self.only_build()
        # The candidate records the pin it was made from: promotion refuses it while the file pins another one.
        self.assertEqual(
            (build.template_version, build.source_template, build.source_region, build.source_pin),
            (5, WORKER, REGION, PIN),
        )
        self.assertEqual(self.world.next_version, 6)

    def test_a_pass_that_runs_out_of_time_while_ending_the_builder_records_no_source_values(self):
        # The image is available and the builder lives, so the pass ends it first, and that call takes the rest of the
        # pass. The version request that follows does not start, so the row must record nothing that it is made under:
        # a pin that moved before the next pass would leave the first real request on the old pin (``PIN_MOVED``).
        self.world.console_plan = [(30.0, READY)]
        self.world.hooks["terminate_instances"] = lambda arguments: self.clock.advance(PASS_DEADLINE_SECONDS)
        started_at = self.clock()
        result = self.apply()
        build = self.only_build()
        # The premise: the builder was ended once, its image was available, and the pass deadline passed by then.
        self.assertEqual(self.world.arguments("terminate_instances"), [{"InstanceIds": [build.builder_id]}])
        self.assertEqual(self.world.images[build.image_id]["State"], "available")
        self.assertGreaterEqual(self.clock(), started_at + PASS_DEADLINE_SECONDS)
        # No version request was made, and the row holds none of the values that one would have carried.
        self.assertEqual(self.world.arguments("create_launch_template_version"), [])
        self.assertEqual((build.source_template, build.source_region, build.source_pin), (None, None, None))
        self.assertEqual((result["decision"], result["reason"]), ("imaging", None))
        self.assertEqual(build.state, "imaging")
        self.assertIn("pass_deadline_exceeded", result["notes"])

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


class ReadDeadlineTests(PipelineCase):
    """A read of the provider that follows a mutation or a wait does not start once the pass deadline is over.

    Each test makes the pass run out of time inside the call just before the read, because that is the only
    place a read can start late: the poll loops stop on their own.
    """

    def by_id_reads(self, image_id, mark=0):
        return [
            kwargs
            for name, kwargs in self.world.calls[mark:]
            if name == "describe_images" and kwargs["ImageIds"] == [image_id]
        ]

    def test_the_image_read_of_an_imaging_build_does_not_start_past_the_pass_deadline(self):
        self.world.console_plan = [(30.0, READY)]
        self.world.image_delay = 100_000.0
        self.assertEqual(self.apply()["decision"], "imaging")
        build = self.only_build()
        mark = len(self.world.calls)

        def run_out_of_time(arguments):
            # The read of the pin's image is the last read of the pass before the build's own.
            if arguments["ImageIds"] == [ami(0xA1)]:
                self.clock.advance(400)

        self.world.hooks["describe_images"] = run_out_of_time
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("imaging", None))
        self.assertIn("pass_deadline_exceeded", result["notes"])
        self.assertEqual(self.by_id_reads(build.image_id, mark), [])
        self.assertEqual(self.only_build().state, "imaging")
        # Nothing is lost: the next pass reads the image and completes the build.
        del self.world.hooks["describe_images"]
        self.world.image_delay = 90.0
        self.assertEqual(self.apply()["decision"], "candidate")
        self.assertEqual(len(self.by_id_reads(build.image_id, mark)), 1)

    def test_the_read_back_of_a_new_version_does_not_start_past_the_pass_deadline(self):
        self.world.console_plan = [(30.0, READY)]
        self.world.hooks["create_launch_template_version"] = lambda arguments: self.clock.advance(400)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("imaging", None))
        self.assertIn("pass_deadline_exceeded", result["notes"])
        # The version exists, and nothing read it back.
        self.assertEqual(self.world.next_version, 6)
        reads = [kwargs for kwargs in self.world.arguments("describe_launch_template_versions") if "5" in kwargs["Versions"]]
        self.assertEqual(reads, [])
        build = self.only_build()
        self.assertEqual((build.state, build.template_version), ("imaging", None))
        self.assertEqual(self.events("image_build_failed"), [])
        # The next pass asks with the same token, gets the same version and gives the verdict.
        del self.world.hooks["create_launch_template_version"]
        self.assertEqual(self.apply()["decision"], "candidate")
        build = self.only_build()
        self.assertEqual(build.template_version, 5)
        attempts = self.world.arguments("create_launch_template_version")
        self.assertEqual(len(attempts), 2)
        self.assertEqual({attempt["ClientToken"] for attempt in attempts}, {f"{build.client_token}-v"})
        self.assertEqual(self.world.next_version, 6)
        self.assertEqual(len([key for key in self.world.versions if key[0] == WORKER]), 2)

    def test_the_read_of_the_pin_image_does_not_start_past_the_pass_deadline(self):
        # ``plan`` reads the pin before any other provider read, so the first read of a template version is its.
        self.world.hooks["describe_launch_template_versions"] = lambda arguments: self.clock.advance(400)
        self.refused(self.plan, "pass_deadline_exceeded")
        self.assertEqual(self.by_id_reads(ami(0xA1)), [])


class BasisReadDeadlineTests(PipelineCase):
    """A read of a basis file does not start once the pass deadline is over.

    The forge client puts no deadline on a read, so the pipeline reads the clock before each one. Each test makes
    the pass run out of time inside a read and counts the files that the forge was asked for: a pass that reads the
    clock only after the last file ends in the same place, and has made every request by then.
    """

    PATHS = ("example.lock", "second.lock", "third.lock")

    def several_basis_paths(self):
        """The deployment file names three basis paths and the forge holds all three."""
        single = 'basis_paths = ["example.lock"]'
        self.assertIn(single, render_toml())
        names = ", ".join(f'"{path}"' for path in self.PATHS)
        self.configure(render_toml().replace(single, f"basis_paths = [{names}]"))
        self.source.files = {path: LOCK_FILE + path.encode() for path in self.PATHS}

    def reads(self, mark=0):
        """The basis files that the forge was asked for since ``mark``, a position in its call log."""
        return [path for kind, path in self.source.calls[mark:] if kind == "raw_file"]

    def unstaged_build(self):
        """A build whose first pass ran out of time during the stage: it has no builder and no staged archive."""
        self.source.on_chunk = lambda: self.clock.advance(400)
        self.apply()
        self.source.on_chunk = None
        return self.only_build()

    def assert_left_for_the_next_pass(self, result, first, world_mark):
        """The pass noted the deadline and left the row as it was: no failure, no second upload, no builder."""
        self.assertEqual((result["decision"], result["reason"]), ("building", None))
        self.assertIn("pass_deadline_exceeded", result["notes"])
        build = self.only_build()
        self.assertEqual(build.id, first.id)
        self.assertEqual((build.state, build.builder_id, build.archive_sha256), ("building", None, None))
        self.assertNotIn("create_multipart_upload", self.world.names(world_mark))
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.events("image_build_failed"), [])
        control = self.control()
        self.assertEqual((control.consecutive_failures, control.cooldown_until, control.blocked), (0, None, False))

    def test_a_decision_does_not_start_a_basis_read_past_the_pass_deadline(self):
        self.several_basis_paths()
        self.source.on_read = lambda: self.clock.advance(400)
        self.refused(self.apply, "pass_deadline_exceeded")
        self.assertEqual(self.reads(), ["example.lock"])
        # Nothing was started: no row, no archive and no builder.
        self.assertEqual(self.builds(), [])
        self.assertEqual(self.world.arguments("create_multipart_upload"), [])
        self.assertEqual(self.world.arguments("run_instances"), [])

    def test_a_plan_does_not_start_a_basis_read_past_the_pass_deadline(self):
        self.several_basis_paths()
        self.source.on_read = lambda: self.clock.advance(400)
        self.refused(self.plan, "pass_deadline_exceeded")
        self.assertEqual(self.reads(), ["example.lock"])
        # The archive probe and the provider come after the reads, and neither was reached.
        self.assertNotIn(("archive", SOURCE_SHA), self.source.calls)
        self.assertEqual((self.sessions, self.world.calls), ([], []))
        self.assertFalse(self.state_dir.exists())

    def test_a_resumed_stage_starts_no_basis_read_at_all(self):
        # The row recorded the hash of every file: the stage that is done again reads none, whatever the clock does.
        self.several_basis_paths()
        first = self.unstaged_build()
        source_mark = len(self.source.calls)
        self.source.on_read = lambda: self.clock.advance(400)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(self.reads(source_mark), [])
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(self.only_build().id, first.id)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)

    def test_a_resumed_stage_does_not_start_its_first_basis_read_when_the_deadline_passed_before_it(self):
        self.several_basis_paths()
        first = self.unstaged_build()
        source_mark, world_mark = len(self.source.calls), len(self.world.calls)

        def run_out_of_time(arguments):
            # The read of the pin's image is the last read of the pass before the stage is done again.
            if arguments.get("ImageIds") == [ami(0xA1)]:
                self.clock.advance(400)

        self.world.hooks["describe_images"] = run_out_of_time
        result = self.apply()
        self.assertEqual(self.reads(source_mark), [])
        self.assert_left_for_the_next_pass(result, first, world_mark)


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

    def test_a_crash_after_the_upload_is_staged_again_under_the_same_key_and_launched(self):
        self.crash_after("archive_sha256")
        build = self.only_build()
        # The row named the bucket and the key before the upload, so the object is the one the row names.
        (key,) = self.world.objects
        self.assertEqual(
            (build.state, build.archive_bucket, build.archive_key, build.archive_sha256, build.builder_id),
            ("building", BUCKET, key, None, None),
        )
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.world.console_plan = [(30.0, READY)]
        result = self.pipeline.run_until_done()
        self.assertEqual(result["decision"], "candidate")
        self.assertEqual(self.only_build().id, build.id)
        self.assertEqual([call["Key"] for call in self.world.arguments("create_multipart_upload")], [key, key])
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.events("image_build_failed"), [])

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


class ArchiveOwedTests(PipelineCase):
    """An archive is deleted from where its row recorded it, and the deletion stays owed until it succeeds."""

    OTHER_PREFIX = "other-prefix/"

    def unstaged_with_an_archive(self):
        """A build whose first pass ran out of time after the upload: its row names an archive and has no builder."""
        self.world.hooks["generate_presigned_url"] = lambda arguments: self.clock.advance(400)
        self.apply()
        del self.world.hooks["generate_presigned_url"]
        first = self.only_build()
        self.assertEqual(list(self.world.objects), [first.archive_key])
        return first

    def crash_after_the_upload(self):
        """A pass whose process dies after the object is stored and before it records the upload, then a new process."""
        self.restart(state=CrashingState(self.state_dir, clock=self.clock, crash_on={"archive_sha256"}))
        with self.assertRaises(Crash):
            self.apply()
        self.restart()
        build = self.only_build()
        self.assertEqual((build.state, build.archive_sha256, build.builder_id), ("building", None, None))
        self.assertEqual(list(self.world.objects), [build.archive_key])
        return build

    def assert_settled_without_a_launch(self, result, build, reason):
        """The pass ended the row without a failure, deleted its object and launched and uploaded nothing more."""
        self.assertEqual((result["decision"], result["reason"]), ("refused", reason), result)
        ended = self.only_build()
        self.assertEqual((ended.id, ended.state, ended.reason_code), (build.id, "failed", reason))
        self.assertIsNotNone(ended.archive_deleted_at)
        control = self.control()
        self.assertEqual((control.consecutive_failures, control.cooldown_until, control.blocked), (0, None, False))
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.world.arguments("delete_object"), [{"Bucket": BUCKET, "Key": build.archive_key, "ExpectedBucketOwner": ACCOUNT}])
        self.assertEqual(len(self.world.arguments("create_multipart_upload")), 1)
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.events("image_build_failed"), [])

    def test_the_bucket_and_the_key_are_in_the_row_before_the_upload_starts(self):
        seen = []
        self.world.hooks["create_multipart_upload"] = lambda arguments: seen.append(
            (arguments, self.query(lambda state: state.active_build(DEPLOYMENT)))
        )
        self.apply()
        ((arguments, row),) = seen
        self.assertEqual((row.archive_bucket, row.archive_key), (arguments["Bucket"], arguments["Key"]))
        self.assertEqual((row.archive_bucket, row.archive_key), (BUCKET, f"{PREFIX}{row.id}.tar.gz"))
        self.assertIsNone(row.archive_sha256)
        self.assertEqual(self.only_build().archive_sha256, ARCHIVE_SHA)

    def test_a_crash_after_the_upload_and_a_withdrawn_gate_delete_the_object_the_row_names(self):
        self.configure(render_toml(gate=NO_GATE))
        build = self.crash_after_the_upload()
        # The opt-out is gone and the digest does not hold it: the row ends for the gate, not for its inputs.
        self.configure(render_toml(gate=""))
        result = self.apply()
        self.assert_settled_without_a_launch(result, build, "IMAGE_GATE_REQUIRED")

    def test_a_crash_after_the_upload_and_a_changed_prefix_delete_the_object_under_the_old_prefix(self):
        self.configure(render_toml(gate=NO_GATE))
        build = self.crash_after_the_upload()
        self.assertTrue(build.archive_key.startswith(PREFIX))
        self.configure(render_toml(gate="", prefix=self.OTHER_PREFIX))
        result = self.apply()
        self.assert_settled_without_a_launch(result, build, "IMAGE_GATE_REQUIRED")
        self.assertEqual(self.world.arguments("list_objects_v2")[-1]["Prefix"], self.OTHER_PREFIX)

    def test_a_stage_done_again_uses_the_recorded_bucket_and_key_whatever_the_configuration_says(self):
        first = self.unstaged_with_an_archive()
        other_bucket = "example-other-staging"
        self.configure(render_toml(prefix=self.OTHER_PREFIX).replace(BUCKET, other_bucket))
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        build = self.only_build()
        self.assertEqual((build.id, build.archive_bucket, build.archive_key), (first.id, BUCKET, first.archive_key))
        self.assertEqual(
            [(call["Bucket"], call["Key"]) for call in self.world.arguments("create_multipart_upload")],
            [(BUCKET, first.archive_key)] * 2,
        )
        self.assertEqual(
            [(call["Params"]["Bucket"], call["Params"]["Key"]) for call in self.world.arguments("generate_presigned_url")],
            [(BUCKET, first.archive_key)] * 2,
        )
        self.assertEqual(self.world.arguments("delete_object"), [{"Bucket": BUCKET, "Key": first.archive_key, "ExpectedBucketOwner": ACCOUNT}])
        self.assertEqual(self.world.objects, {})
        self.assertIsNotNone(build.archive_deleted_at)

    STAGING_CALLS = (
        "create_multipart_upload",
        "upload_part",
        "complete_multipart_upload",
        "abort_multipart_upload",
        "delete_object",
        "list_objects_v2",
    )
    OTHER_ACCOUNT = "999988887777"

    def staging_calls(self, mark=0):
        return [(name, call) for name, call in self.world.calls[mark:] if name in self.STAGING_CALLS]

    def test_every_staging_call_of_a_pass_names_the_account_as_the_bucket_owner(self):
        # A stray object for the sweep, an owed row for the tail, a build that stages, ends and deletes its archive.
        self.world.objects[f"{PREFIX}stray.tar.gz"] = (b"stray", NOW - 2 * HOUR)
        owed = f"{PREFIX}owed-00.tar.gz"
        self.add_build("owed-00", archive_bucket=BUCKET, archive_key=owed)
        self.world.objects[owed] = (b"archive", NOW)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        calls = self.staging_calls()
        self.assertEqual(
            {name for name, _ in calls},
            {"create_multipart_upload", "upload_part", "complete_multipart_upload", "delete_object", "list_objects_v2"},
        )
        deleted = {call["Key"] for name, call in calls if name == "delete_object"}
        self.assertIn(f"{PREFIX}stray.tar.gz", deleted)
        self.assertIn(owed, deleted)
        for name, call in calls:
            with self.subTest(call=name, key=call.get("Key")):
                self.assertEqual(call["ExpectedBucketOwner"], ACCOUNT)

    def test_a_restage_into_a_bucket_that_another_account_now_owns_sends_nothing_and_is_refused(self):
        first = self.unstaged_with_an_archive()
        # The recorded bucket was released by a rotation and reassigned, and the configuration names another one.
        self.world.bucket_owners[BUCKET] = self.OTHER_ACCOUNT
        other_bucket = "example-other-staging"
        self.configure(render_toml(prefix=self.OTHER_PREFIX).replace(BUCKET, other_bucket))
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_STAGING_BUCKET_DENIED"), result)
        # One upload was opened, with the account of the identity as the owner, and the bucket refused it: no part,
        # no completion, no launch and no URL. No call went out without the owner.
        self.assertEqual(
            [
                (call["Bucket"], call["ExpectedBucketOwner"])
                for name, call in self.staging_calls(mark)
                if name == "create_multipart_upload"
            ],
            [(BUCKET, ACCOUNT)],
        )
        names = self.world.names(mark)
        for name in ("upload_part", "complete_multipart_upload", "run_instances", "generate_presigned_url"):
            self.assertNotIn(name, names)
        for name, call in self.staging_calls(mark):
            self.assertEqual(call["ExpectedBucketOwner"], ACCOUNT, name)
        # The row is consistent: ended, no failure counted, and its archive still owed, because the delete is denied too.
        ended = self.only_build()
        self.assertEqual((ended.id, ended.state, ended.reason_code), (first.id, "failed", "IMAGE_STAGING_BUCKET_DENIED"))
        self.assertEqual((ended.archive_bucket, ended.archive_key), (BUCKET, first.archive_key))
        self.assertIsNone(ended.archive_deleted_at)
        self.assertIsNotNone(ended.archive_delete_tried_at)
        self.assertEqual(self.query(lambda state: state.archives_owed(DEPLOYMENT)), [ended])
        control = self.control()
        self.assertEqual((control.consecutive_failures, control.cooldown_until, control.blocked), (0, None, False))
        self.assertIn("archive:delete_failed", result["notes"])
        self.assertEqual(self.events("image_build_failed"), [])
        # The next pass decides from the configuration: a row for the configured bucket, and the old one stays owed.
        self.clock.advance(DAY)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(result["reconcile"]["owed_archive_delete_failed"], 1)
        rows = {row.id: row for row in self.builds()}
        self.assertEqual(len(rows), 2)
        (new,) = [row for row in rows.values() if row.id != first.id]
        self.assertEqual(new.archive_bucket, other_bucket)
        self.assertIsNone(rows[first.id].archive_deleted_at)

    def test_the_row_records_the_owner_with_the_bucket_before_the_upload_and_every_call_names_it(self):
        seen = []
        self.world.hooks["create_multipart_upload"] = lambda arguments: seen.append(
            (arguments, self.query(lambda state: state.active_build(DEPLOYMENT)))
        )
        self.apply()
        ((arguments, row),) = seen
        self.assertEqual((row.archive_bucket, row.archive_owner), (BUCKET, ACCOUNT))
        self.assertEqual(arguments["ExpectedBucketOwner"], row.archive_owner)
        self.assertEqual(self.only_build().archive_owner, ACCOUNT)

    def role_moves_to_another_account(self):
        """The stage of a row ran in the account of the world. The state file stays, and the role is in B now."""
        self.world.account = self.OTHER_ACCOUNT

    def test_a_restage_after_the_role_moved_to_another_account_sends_nothing_and_is_refused(self):
        first = self.unstaged_with_an_archive()
        self.assertEqual(first.archive_owner, ACCOUNT)
        # The recorded bucket name exists in the account of the role now: the world owns it under that account.
        self.role_moves_to_another_account()
        other_bucket = "example-other-staging"
        self.configure(render_toml(prefix=self.OTHER_PREFIX).replace(BUCKET, other_bucket))
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_STAGING_OWNER_CHANGED"), result)
        names = self.world.names(mark)
        for name in (
            "create_multipart_upload",
            "upload_part",
            "complete_multipart_upload",
            "delete_object",
            "run_instances",
            "generate_presigned_url",
        ):
            self.assertNotIn(name, names)
        for name, call in self.staging_calls(mark):
            self.assertNotEqual(call["Bucket"], BUCKET, name)
        ended = self.only_build()
        self.assertEqual((ended.id, ended.state, ended.reason_code), (first.id, "failed", "IMAGE_STAGING_OWNER_CHANGED"))
        # No call was made, so the deletion is owed and was never marked done.
        self.assertIsNone(ended.archive_deleted_at)
        self.assertEqual(ended.archive_owner, ACCOUNT)
        self.assertEqual(self.query(lambda state: state.archives_owed(DEPLOYMENT)), [ended])
        self.assertEqual(self.control().consecutive_failures, 0)
        self.assertIn("archive:delete_failed", result["notes"])
        self.assertIn("archive:owner_changed", result["notes"])
        self.assertNotIn(self.OTHER_ACCOUNT, json.dumps(result))

    def test_an_owed_delete_after_the_role_moved_to_another_account_makes_no_s3_call_and_counts_as_failed(self):
        key = f"{PREFIX}owed-00.tar.gz"
        self.add_build("owed-00", archive_bucket=BUCKET, archive_key=key)
        self.world.objects[key] = (b"archive", NOW)
        self.role_moves_to_another_account()
        other_bucket = "example-other-staging"
        self.configure(render_toml(prefix=self.OTHER_PREFIX).replace(BUCKET, other_bucket))
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 0)
        self.assertEqual(result["reconcile"]["owed_archive_delete_failed"], 1)
        self.assertIn("archive:owner_changed", result["notes"])
        self.assertEqual([call for call in self.world.arguments("delete_object") if call["Key"] == key], [])
        self.assertEqual(self.world.objects[key][0], b"archive")
        row = [row for row in self.builds() if row.id == "owed-00"][0]
        self.assertIsNone(row.archive_deleted_at)
        self.assertEqual(row.archive_delete_seq, 1)
        self.assertEqual(self.query(lambda state: [r.id for r in state.archives_owed(DEPLOYMENT)]), ["owed-00"])

    def test_a_row_that_records_no_owner_is_refused_like_another_owner(self):
        key = f"{PREFIX}owed-00.tar.gz"
        self.add_build("owed-00", archive_bucket=BUCKET, archive_key=key, archive_owner=None)
        self.world.objects[key] = (b"archive", NOW)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["reconcile"]["owed_archive_delete_failed"], 1)
        self.assertIn("archive:owner_changed", result["notes"])
        self.assertEqual([call for call in self.world.arguments("delete_object") if call["Key"] == key], [])
        self.assertEqual(self.world.objects[key][0], b"archive")

    def test_a_restage_of_a_row_that_records_no_owner_sends_nothing_and_is_refused(self):
        first = self.unstaged_with_an_archive()
        self.write(
            lambda state: state._require().execute("UPDATE image_builds SET archive_owner = NULL WHERE id = ?", (first.id,))
        )
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_STAGING_OWNER_CHANGED"), result)
        self.assertNotIn("create_multipart_upload", self.world.names(mark))
        self.assertIsNone(self.only_build().archive_deleted_at)

    def test_an_owed_delete_names_the_owner_that_the_row_recorded(self):
        key = f"{PREFIX}owed-00.tar.gz"
        self.add_build("owed-00", archive_bucket=BUCKET, archive_key=key, archive_owner=ACCOUNT)
        self.world.objects[key] = (b"archive", NOW)
        self.world.console_plan = [(30.0, READY)]
        self.apply()
        (call,) = [call for call in self.world.arguments("delete_object") if call["Key"] == key]
        self.assertEqual(call["ExpectedBucketOwner"], ACCOUNT)
        self.assertEqual(self.world.objects.get(key), None)

    def test_a_new_row_whose_upload_is_denied_is_a_failed_build_that_counts(self):
        self.world.fail("create_multipart_upload", ProviderError("AccessDenied"))
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("failed", "IMAGE_STAGING_BUCKET_DENIED"), result)
        self.assertEqual(self.control().consecutive_failures, 1)
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertNotIn("upload_part", self.world.names())

    def removed_bucket(self, bucket):
        """S3 answers ``NoSuchBucket`` to the upload and to the delete at ``bucket``, as after its removal."""

        def removed(arguments):
            if arguments["Bucket"] == bucket:
                raise ProviderError("NoSuchBucket")

        for method in ("create_multipart_upload", "delete_object"):
            self.world.hooks[method] = removed

    def test_a_restage_into_a_bucket_that_was_removed_is_refused_and_counts_no_failure(self):
        first = self.unstaged_with_an_archive()
        # A rotation removed the recorded bucket, and the configuration names another one.
        self.removed_bucket(BUCKET)
        other_bucket = "example-other-staging"
        self.configure(render_toml(prefix=self.OTHER_PREFIX).replace(BUCKET, other_bucket))
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_STAGING_BUCKET_MISSING"), result)
        # One upload was opened and refused: no part, no completion, no launch and no URL.
        names = self.world.names(mark)
        self.assertEqual(names.count("create_multipart_upload"), 1)
        for name in ("upload_part", "complete_multipart_upload", "run_instances", "generate_presigned_url"):
            self.assertNotIn(name, names)
        # No failure: the cooldown, the count and the block stay as they were, and no failed build is reported.
        control = self.control()
        self.assertEqual((control.consecutive_failures, control.cooldown_until, control.blocked), (0, None, False))
        self.assertEqual(self.events("image_build_failed"), [])
        # The row ended, and it keeps its archive: the delete at the removed bucket is refused too, so the deletion is
        # owed and the row is never marked done.
        ended = self.only_build()
        self.assertEqual((ended.id, ended.state, ended.reason_code), (first.id, "failed", "IMAGE_STAGING_BUCKET_MISSING"))
        self.assertEqual((ended.archive_bucket, ended.archive_key, ended.archive_owner), (BUCKET, first.archive_key, ACCOUNT))
        self.assertIsNone(ended.archive_deleted_at)
        self.assertIsNotNone(ended.archive_delete_tried_at)
        self.assertEqual(self.query(lambda state: state.archives_owed(DEPLOYMENT)), [ended])
        self.assertEqual(
            self.world.arguments("delete_object"),
            [{"Bucket": BUCKET, "Key": first.archive_key, "ExpectedBucketOwner": ACCOUNT}],
        )
        self.assertIn("archive:delete_failed", result["notes"])
        # The next pass decides from the configuration: a row for the configured bucket. The old row stays owed.
        self.clock.advance(DAY)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(result["reconcile"]["owed_archive_delete_failed"], 1)
        rows = {row.id: row for row in self.builds()}
        self.assertEqual(len(rows), 2)
        (new,) = [row for row in rows.values() if row.id != first.id]
        self.assertEqual(new.archive_bucket, other_bucket)
        self.assertIsNone(rows[first.id].archive_deleted_at)

    def test_the_delete_that_ends_a_restage_into_a_removed_bucket_counts_only_when_a_later_pass_retries_it(self):
        first = self.unstaged_with_an_archive()
        self.removed_bucket(BUCKET)
        other_bucket = "example-other-staging"
        self.configure(render_toml(prefix=self.OTHER_PREFIX).replace(BUCKET, other_bucket))
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_STAGING_BUCKET_MISSING"), result)
        # The delete as the row ended was tried and refused: it is noted and the row stays owed. The owed deletions at
        # the end of the pass skip a row that the pass attempted, so the pass adds nothing to the failed count.
        self.assertIn("archive:delete_failed", result["notes"])
        attempts = [call for call in self.world.arguments("delete_object") if call["Key"] == first.archive_key]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(result["reconcile"]["owed_archive_delete_failed"], 0)
        self.assertEqual(self.query(lambda state: [row.id for row in state.archives_owed(DEPLOYMENT)]), [first.id])
        # A later pass retries it with the other owed deletions, and that refusal is counted.
        self.clock.advance(DAY)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["reconcile"]["owed_archive_delete_failed"], 1, result)
        attempts = [call for call in self.world.arguments("delete_object") if call["Key"] == first.archive_key]
        self.assertEqual(len(attempts), 2)

    def test_a_new_row_whose_bucket_does_not_exist_is_a_failed_build_that_counts(self):
        self.world.fail("create_multipart_upload", ProviderError("NoSuchBucket"))
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("failed", "IMAGE_STAGING_BUCKET_MISSING"), result)
        self.assertEqual(self.control().consecutive_failures, 1)
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertNotIn("upload_part", self.world.names())

    def test_an_owed_delete_in_a_bucket_of_another_account_counts_as_failed_and_stays_owed(self):
        released = "example-released-staging"
        key = f"{PREFIX}owed-00.tar.gz"
        self.add_build("owed-00", archive_bucket=released, archive_key=key)
        self.world.objects[key] = (b"archive", NOW)
        self.world.bucket_owners[released] = self.OTHER_ACCOUNT
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 0)
        self.assertEqual(result["reconcile"]["owed_archive_delete_failed"], 1)
        self.assertIn("archive:delete_failed", result["notes"])
        (attempt,) = [call for name, call in self.world.calls if name == "delete_object" and call["Key"] == key]
        self.assertEqual((attempt["Bucket"], attempt["ExpectedBucketOwner"]), (released, ACCOUNT))
        owed = [row.id for row in self.query(lambda state: state.archives_owed(DEPLOYMENT))]
        self.assertEqual(owed, ["owed-00"])
        self.assertEqual(self.world.objects[key][0], b"archive")
        event = self.events("image_reconciled")[-1]
        self.assertEqual((event["owed_archives_deleted"], event["owed_archive_delete_failed"]), (0, 1))
        self.assertNotIn(released, json.dumps(self.events()))

    def test_a_sweep_of_a_bucket_that_another_account_owns_deletes_nothing_and_refuses_the_pass(self):
        self.world.bucket_owners[BUCKET] = self.OTHER_ACCOUNT
        self.world.objects[f"{PREFIX}stray.tar.gz"] = (b"stray", NOW - 2 * HOUR)
        self.refused(self.apply, "IMAGE_STAGING_BUCKET_DENIED")
        self.assertEqual(self.world.arguments("delete_object"), [])
        self.assertEqual([name for name, _ in self.staging_calls()], ["list_objects_v2"])
        self.assertEqual(self.world.arguments("create_multipart_upload"), [])

    def test_a_refused_delete_leaves_the_deletion_owed_for_a_pass_that_has_no_gate_and_nothing_in_flight(self):
        first = self.unstaged_with_an_archive()
        self.world.fail("delete_object", ProviderError("RequestLimitExceeded"), times=1)
        self.clock.advance(BUILD_DEADLINE)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("failed", "CONSOLE_BLIND"), result)
        self.assertIn("archive:delete_failed", result["notes"])
        failed = self.only_build()
        self.assertEqual(failed.id, first.id)
        self.assertIsNone(failed.archive_deleted_at)
        self.assertEqual(list(self.world.objects), [first.archive_key])
        self.assertEqual(self.query(lambda state: state.archives_owed(DEPLOYMENT)), [failed])
        # The file lost its gate and nothing is in flight. The pass still runs, because a deletion is owed.
        self.clock.advance(60)
        self.configure(render_toml(gate=""))
        source_mark, world_mark = len(self.source.calls), len(self.world.calls)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_GATE_REQUIRED"), result)
        self.assertIsNone(result["build"])
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 1)
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.world.arguments("delete_object")[-1], {"Bucket": BUCKET, "Key": first.archive_key, "ExpectedBucketOwner": ACCOUNT})
        self.assertEqual(self.only_build().archive_deleted_at, self.clock())
        self.assertEqual(self.query(lambda state: state.archives_owed(DEPLOYMENT)), [])
        # No launch, no upload, no read of the forge.
        self.assertEqual(self.source.calls[source_mark:], [])
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertNotIn("create_multipart_upload", self.world.names(world_mark))
        control = self.control()
        self.assertEqual(control.consecutive_failures, 1)
        self.assertEqual(self.events("image_check")[-1]["reason"], "IMAGE_GATE_REQUIRED")
        # With nothing owed any more, the same file is refused before any client is built.
        mark = len(self.world.calls)
        self.refused(self.apply, "IMAGE_GATE_REQUIRED")
        self.assertEqual(self.world.calls[mark:], [])

    def test_a_deletion_that_stays_refused_stays_owed_and_is_counted_when_it_succeeds(self):
        first = self.unstaged_with_an_archive()
        self.world.fail("delete_object", ProviderError("RequestLimitExceeded"), times=2)
        self.clock.advance(BUILD_DEADLINE)
        self.apply()
        self.pin_current()
        self.clock.advance(60)
        result = self.apply()
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 0)
        self.assertIn("archive:delete_failed", result["notes"])
        self.assertEqual([row.id for row in self.query(lambda state: state.archives_owed(DEPLOYMENT))], [first.id])
        self.clock.advance(60)
        result = self.apply()
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 1)
        self.assertNotIn("archive:delete_failed", result["notes"])
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.query(lambda state: state.archives_owed(DEPLOYMENT)), [])
        # Once settled, it is not tried again.
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 0)
        self.assertNotIn("delete_object", self.world.names(mark))

    def test_an_owed_deletion_is_tried_after_the_work_of_a_pass_that_ends_on_a_refused_read(self):
        first = self.unstaged_with_an_archive()
        self.world.fail("delete_object", ProviderError("RequestLimitExceeded"), times=1)
        self.clock.advance(BUILD_DEADLINE)
        self.apply()
        self.assertIsNone(self.only_build().archive_deleted_at)
        self.clock.advance(60)
        self.world.fail("describe_instances", ProviderError("RequestLimitExceeded"), times=1)
        self.refused(self.apply, "AWS_UNCERTAIN")
        self.assertEqual(self.world.objects, {})
        self.assertIsNotNone(self.only_build().archive_deleted_at)
        self.assertEqual(self.only_build().id, first.id)

    def test_a_row_that_named_a_key_before_an_upload_that_never_happened_settles_at_the_first_try(self):
        # The pass stopped between the record of the key and the upload. S3 answers a delete of a missing key as done.
        def die(arguments):
            raise Crash("before the upload")

        self.world.hooks["create_multipart_upload"] = die
        with self.assertRaises(Crash):
            self.apply()
        del self.world.hooks["create_multipart_upload"]
        self.restart()
        named = self.only_build()
        self.assertEqual((named.archive_bucket, named.archive_key), (BUCKET, f"{PREFIX}{named.id}.tar.gz"))
        self.assertIsNone(named.archive_sha256)
        self.assertEqual(self.world.objects, {})
        self.clock.advance(BUILD_DEADLINE)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("failed", "CONSOLE_BLIND"), result)
        self.assertEqual(self.query(lambda state: state.archives_owed(DEPLOYMENT)), [])

    def test_the_owed_count_is_part_of_the_reconcile_summary_and_the_event(self):
        self.unstaged_with_an_archive()
        self.world.fail("delete_object", ProviderError("RequestLimitExceeded"), times=1)
        self.clock.advance(BUILD_DEADLINE)
        self.apply()
        self.pin_current()
        self.clock.advance(60)
        result = self.apply()
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 1)
        self.assertEqual(result["reconcile"]["archives_deleted"], 0)
        (event,) = self.events("image_reconciled")
        self.assertEqual((event["owed_archives_deleted"], event["archives_deleted"]), (1, 0))


class OwedArchiveBoundTests(PipelineCase):
    """The owed deletions of one pass are bounded, take turns, and never hide the rest of the pass."""

    def owe(self, count, *, stuck=(), modified=NOW):
        """``count`` finished builds that each owe an archive, oldest first. A key in ``stuck`` is refused for good.

        The objects were last modified at ``modified``.
        """
        keys = []
        for number in range(count):
            key = f"{PREFIX}owed-{number:02d}.tar.gz"
            self.add_build(f"owed-{number:02d}", age=5 * DAY - number * 60, archive_bucket=BUCKET, archive_key=key)
            self.world.objects[key] = (b"archive", modified)
            keys.append(key)
        refused = {keys[number] for number in stuck}

        def refuse(arguments):
            if "/owed-" in arguments["Key"]:
                self.starts.append(self.clock())
            self.clock.advance(self.seconds_per_delete)
            if arguments["Key"] in refused:
                raise ProviderError("RequestLimitExceeded")

        self.seconds_per_delete = 1
        self.starts = []
        self.world.hooks["delete_object"] = refuse
        return keys

    def assert_owed_deletes_follow_the_work(self, mark):
        """Every owed deletion of the pass came after the last call of the pass's own work."""
        names = self.world.names(mark)
        owed = [
            index
            for index, (name, call) in enumerate(self.world.calls[mark:])
            if name == "delete_object" and "/owed-" in call["Key"]
        ]
        self.assertTrue(owed)
        own = [index for index, name in enumerate(names) if name in ("run_instances", "get_console_output")]
        self.assertGreater(min(owed), max(own))

    def test_a_backlog_larger_than_the_bound_starts_no_more_deletions_than_the_bound_after_the_work(self):
        self.owe(12, stuck=range(12))
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(len(self.owed_deletes(0)), OWED_ARCHIVES_PER_PASS)
        self.assertIn("archive:delete_deferred", result["notes"])
        # The pass did its own work first: the build started, was launched and ended.
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assert_owed_deletes_follow_the_work(0)

    def test_a_pass_whose_work_uses_the_pass_leaves_no_time_for_an_owed_deletion(self):
        self.owe(3, stuck=range(3))
        result = self.apply()
        self.assertEqual(result["decision"], "building", result)
        self.assertEqual(self.owed_deletes(0), [])
        self.assertIn("archive:delete_deferred", result["notes"])
        self.assertEqual(len(self.world.arguments("run_instances")), 1)

    def test_slow_refused_deletions_never_start_inside_the_reserve_before_the_pass_deadline(self):
        self.owe(12, stuck=range(12))
        self.world.console_plan = [(30.0, READY)]
        self.seconds_per_delete = 40
        started_at = self.clock()
        result = self.apply()
        limit = started_at + PASS_DEADLINE_SECONDS - OWED_DELETE_RESERVE_SECONDS
        self.assertTrue(self.starts)
        self.assertTrue(all(start < limit for start in self.starts), (self.starts, limit))
        # The bound that stopped them was the reserve, not the cap: the next start would have been inside it.
        self.assertLess(len(self.starts), OWED_ARCHIVES_PER_PASS)
        self.assertGreaterEqual(self.starts[-1] + self.seconds_per_delete, limit)
        self.assertIn("archive:delete_deferred", result["notes"])
        self.assertEqual(result["decision"], "candidate", result)

    def uncached_account(self, seconds):
        """From now on a pass is handed no account: its first ``account_id`` asks STS, and the answer takes ``seconds``.

        Returns the STS client of the world, whose ``seconds`` a test can change for a later pass. In an ordinary pass
        the stray listing of reconcile asks first and the answer is cached before the owed tail runs, so a test that
        wants the owed delete to pay for the lookup must end the pass before that listing, or have no listing at all.
        """
        sts = WorldSts(self.world, seconds)

        def factory():
            session = UncachedAccountAws(self.world, sts)
            self.sessions.append(session)
            return session

        self.aws_factory = factory
        self.restart()
        return sts

    def assert_the_row_is_deferred_and_untouched(self, key):
        """No call was made about the object: the row is owed in its place, never tried and never marked done."""
        self.assertEqual(self.starts, [])
        self.assertEqual(self.world.arguments("delete_object"), [])
        self.assertEqual(self.world.objects[key][0], b"archive")
        row = self.only_build_named("owed-00")
        self.assertIsNone(row.archive_deleted_at)
        self.assertIsNone(row.archive_delete_tried_at)
        self.assertIsNone(row.archive_delete_seq)
        self.assertEqual(self.query(lambda state: [owed.id for owed in state.archives_owed(DEPLOYMENT)]), ["owed-00"])

    def test_an_account_lookup_that_ends_inside_the_reserve_starts_no_owed_delete_in_a_cleanup_pass(self):
        (key,) = self.owe(1)
        # No gate and nothing in flight: the pass reads nothing before the owed delete, so its owner check is the first
        # use of the account. The clock is before the reserve when the pass asks, and the answer ends inside it.
        self.configure(render_toml(gate=""))
        sts = self.uncached_account(PASS_DEADLINE_SECONDS - OWED_DELETE_RESERVE_SECONDS + 5)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_GATE_REQUIRED"), result)
        self.assertEqual(len(self.world.arguments("get_caller_identity")), 1)
        self.assert_the_row_is_deferred_and_untouched(key)
        reconcile = result["reconcile"]
        self.assertEqual(
            (reconcile["owed_archives_deleted"], reconcile["owed_archive_delete_failed"], reconcile["owed_archives_deferred"]),
            (0, 0, 1),
        )
        self.assertIn("archive:delete_deferred", result["notes"])
        self.assertNotIn("archive:delete_failed", result["notes"])
        # The row lost nothing: a later pass, whose lookup is quick, deletes it.
        sts.seconds = 1
        self.clock.advance(60)
        result = self.apply()
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 1, result)
        self.assertNotIn(key, self.world.objects)
        self.assertIsNotNone(self.only_build_named("owed-00").archive_deleted_at)

    def test_an_account_lookup_refused_inside_the_reserve_leaves_the_owed_delete_untried(self):
        # As above, but the lookup's answer that ends inside the reserve is a refusal. No delete started, so the row is
        # deferred and keeps its place, as for a lookup that answers there; it is not counted as a failed delete.
        (key,) = self.owe(1)
        self.configure(render_toml(gate=""))
        sts = self.uncached_account(PASS_DEADLINE_SECONDS - OWED_DELETE_RESERVE_SECONDS + 5)
        sts.error = ProviderError("RequestTimeout")
        started_at = self.clock()
        result = self.apply()
        # The premise: the one lookup ended inside the reserve.
        self.assertGreater(self.clock(), started_at + PASS_DEADLINE_SECONDS - OWED_DELETE_RESERVE_SECONDS)
        self.assertEqual(len(self.world.arguments("get_caller_identity")), 1)
        self.assert_the_row_is_deferred_and_untouched(key)
        reconcile = result["reconcile"]
        self.assertEqual(
            (reconcile["owed_archives_deleted"], reconcile["owed_archive_delete_failed"], reconcile["owed_archives_deferred"]),
            (0, 0, 1),
        )
        self.assertNotIn("archive:delete_failed", result["notes"])

    def test_an_account_lookup_after_a_refused_read_starts_no_owed_delete_inside_the_reserve(self):
        (key,) = self.owe(1)
        sts = self.uncached_account(READ_TIMEOUT_SECONDS)
        # The first read of the pass takes 190 seconds and is refused, so the pass ends before the stray listing and the
        # owed tail is the first use of the account. 10 seconds remain before the reserve, and the lookup takes 30.
        self.world.hooks["describe_instances"] = lambda arguments: self.clock.advance(190)
        self.world.fail("describe_instances", ProviderError("RequestLimitExceeded"), times=1)
        started_at = self.clock()
        self.refused(self.apply, "AWS_UNCERTAIN")
        del self.world.hooks["describe_instances"]
        # The premise: the lookup did end inside the reserve.
        self.assertGreater(self.clock(), started_at + PASS_DEADLINE_SECONDS - OWED_DELETE_RESERVE_SECONDS)
        self.assertEqual(len(self.world.arguments("get_caller_identity")), 1)
        self.assert_the_row_is_deferred_and_untouched(key)
        (event,) = self.events("image_reconciled")
        self.assertEqual(
            (event["owed_archives_deleted"], event["owed_archive_delete_failed"], event["owed_archives_deferred"]),
            (0, 0, 1),
        )
        sts.seconds = 1
        self.pin_current()
        self.clock.advance(60)
        result = self.apply()
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 1, result)
        self.assertNotIn(key, self.world.objects)
        self.assertIsNotNone(self.only_build_named("owed-00").archive_deleted_at)

    def test_a_quick_account_lookup_leaves_the_owed_delete_to_start(self):
        # The same pass as the first test with a lookup that ends before the reserve: the delete starts, so the checks
        # above can tell a deferred delete from one that never ran.
        (key,) = self.owe(1)
        self.configure(render_toml(gate=""))
        self.uncached_account(5)
        started_at = self.clock()
        result = self.apply()
        self.assertEqual(len(self.world.arguments("get_caller_identity")), 1)
        (start,) = self.starts
        self.assertLess(start, started_at + PASS_DEADLINE_SECONDS - OWED_DELETE_RESERVE_SECONDS)
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 1, result)
        self.assertNotIn("archive:delete_deferred", result["notes"])
        self.assertNotIn(key, self.world.objects)

    def lazy_s3_client(self, seconds):
        """From now on a pass is handed an adapter with no S3 client: the first S3 call builds one, in ``seconds``.

        The account is known, so the owner check asks STS for nothing and the client build is the only wait. In a
        pass with no listing of the staging prefix (a cleanup pass) the owed delete is the first S3 call. Returns the
        session, whose ``seconds`` a test can change for a later pass.
        """
        session = SlowClientSession(self.world, seconds)
        # The build of a client needs the SDK's configuration type, which this suite does not install.
        sdk_config = mock.patch.object(aws_module, "_sdk_config", return_value=object())
        sdk_config.start()
        self.addCleanup(sdk_config.stop)

        def factory():
            adapter = LazyClientAws(self.world, session)
            self.sessions.append(adapter)
            return adapter

        self.aws_factory = factory
        self.restart()
        return session

    def test_a_client_build_that_ends_inside_the_reserve_starts_no_owed_delete_in_a_cleanup_pass(self):
        (key,) = self.owe(1)
        # No gate and nothing in flight: the pass reads nothing before the owed delete. The account is known, so the
        # owner check passes with the clock before the reserve, and the build of the S3 client that follows ends inside it.
        self.configure(render_toml(gate=""))
        session = self.lazy_s3_client(PASS_DEADLINE_SECONDS - OWED_DELETE_RESERVE_SECONDS + 5)
        started_at = self.clock()
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_GATE_REQUIRED"), result)
        # The premise: the client was built, and its build ended inside the reserve.
        self.assertEqual(session.built, ["s3"])
        self.assertGreater(self.clock(), started_at + PASS_DEADLINE_SECONDS - OWED_DELETE_RESERVE_SECONDS)
        # No delete was sent, so the row is deferred as one that never started: not tried, not failed, not done.
        self.assert_the_row_is_deferred_and_untouched(key)
        reconcile = result["reconcile"]
        self.assertEqual(
            (reconcile["owed_archives_deleted"], reconcile["owed_archive_delete_failed"], reconcile["owed_archives_deferred"]),
            (0, 0, 1),
        )
        self.assertIn("archive:delete_deferred", result["notes"])
        self.assertNotIn("archive:delete_failed", result["notes"])
        # The row lost nothing: a later pass, whose client builds quickly, deletes it.
        session.seconds = 1
        self.clock.advance(60)
        result = self.apply()
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 1, result)
        self.assertNotIn(key, self.world.objects)
        self.assertIsNotNone(self.only_build_named("owed-00").archive_deleted_at)

    def test_an_sts_client_build_that_ends_inside_the_reserve_asks_for_no_account_in_a_cleanup_pass(self):
        # No gate and nothing in flight, so the owner check of the owed delete is the first use of the account. The
        # build of the STS client ends inside the reserve: no lookup starts, and neither does the delete.
        (key,) = self.owe(1)
        self.configure(render_toml(gate=""))
        session = SlowClientSession(
            self.world, PASS_DEADLINE_SECONDS - OWED_DELETE_RESERVE_SECONDS + 5, sts=WorldSts(self.world, 1)
        )
        sdk_config = mock.patch.object(aws_module, "_sdk_config", return_value=object())
        sdk_config.start()
        self.addCleanup(sdk_config.stop)
        self.aws_factory = lambda: ImageAws(
            REGION,
            PROFILE,
            clients={"ec2": self.world.ec2, "s3": self.world.s3},
            clock=self.world.clock,
            expiry=self.world.clock() + self.world.credential_life,
            session_factory=lambda profile: session,
        )
        self.restart()
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_GATE_REQUIRED"), result)
        self.assertEqual(session.built, ["sts"])
        self.assertEqual(self.world.arguments("get_caller_identity"), [])
        self.assert_the_row_is_deferred_and_untouched(key)
        self.assertEqual(result["reconcile"]["owed_archives_deferred"], 1, result)

    def test_a_quick_client_build_leaves_the_owed_delete_to_start(self):
        # The same pass as the first test with a client build that ends before the reserve: the delete starts, so the
        # checks above can tell a deferred delete from one that never ran.
        (key,) = self.owe(1)
        self.configure(render_toml(gate=""))
        session = self.lazy_s3_client(5)
        started_at = self.clock()
        result = self.apply()
        self.assertEqual(session.built, ["s3"])
        (start,) = self.starts
        self.assertLess(start, started_at + PASS_DEADLINE_SECONDS - OWED_DELETE_RESERVE_SECONDS)
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 1, result)
        self.assertNotIn("archive:delete_deferred", result["notes"])
        self.assertNotIn(key, self.world.objects)
        self.assertIsNotNone(self.only_build_named("owed-00").archive_deleted_at)

    def a_stray_object_and_a_first_read_that_leaves_5_seconds(self, lookup_seconds):
        """A stray archive older than an hour, and a pass whose first read of reconcile leaves 5 seconds of it.

        The adapter is handed no account, so the stray listing that follows is the first use of it: it asks STS, and
        the answer takes ``lookup_seconds``. Returns the stray key, the pass deadline and the clock readings at which
        STS was asked (a hook sees the reading before the answer takes its time).
        """
        stray = f"{PREFIX}stray.tar.gz"
        self.world.objects[stray] = (b"stray", NOW - 2 * HOUR)
        self.uncached_account(lookup_seconds)
        deadline = self.clock() + PASS_DEADLINE_SECONDS
        spent, asked = [], []

        def read_for_most_of_the_pass(arguments):
            if not spent:
                spent.append(True)
                self.clock.advance(PASS_DEADLINE_SECONDS - 5)

        self.world.hooks["describe_instances"] = read_for_most_of_the_pass
        self.world.hooks["get_caller_identity"] = lambda arguments: asked.append(self.clock())
        return stray, deadline, asked

    def test_a_stray_listing_whose_account_lookup_ends_after_the_pass_deadline_is_not_sent_and_sweeps_nothing(self):
        stray, deadline, asked = self.a_stray_object_and_a_first_read_that_leaves_5_seconds(READ_TIMEOUT_SECONDS)
        refusal = self.refused(self.apply, "pass_deadline_exceeded")
        # The refusal is the listing's, not that of a later read of the pass.
        self.assertEqual(refusal.detail, "ListObjectsV2 page")
        # The premise: the lookup started inside the pass and ended at or after its deadline.
        (asked_at,) = asked
        self.assertLess(asked_at, deadline)
        self.assertGreaterEqual(self.clock(), deadline)
        # So the listing was never sent, and no delete followed it: the stray object is still there.
        self.assertEqual(len(self.world.arguments("get_caller_identity")), 1)
        self.assertEqual(self.world.arguments("list_objects_v2"), [])
        self.assertEqual(self.world.arguments("delete_object"), [])
        self.assertEqual(self.world.objects[stray][0], b"stray")
        # Nothing after the listing ran: no image read, no build.
        self.assertNotIn("describe_images", self.world.names())
        self.assertEqual(self.builds(), [])

    def test_a_stray_listing_whose_account_lookup_ends_before_the_pass_deadline_is_sent_but_sweeps_nothing(self):
        # The same pass with a lookup of 4 of the 5 seconds, so the refusal above is the deadline's.
        stray, deadline, asked = self.a_stray_object_and_a_first_read_that_leaves_5_seconds(4)
        result = self.apply()
        (asked_at,) = asked
        self.assertLess(asked_at + 4, deadline)
        self.assertEqual(len(self.world.arguments("list_objects_v2")), 1)
        # The listing was sent, but the pass is past the sweep bound (``STRAY_SWEEP_SECONDS``): no delete starts.
        self.assertEqual(self.world.arguments("delete_object"), [])
        self.assertEqual(self.world.objects[stray][0], b"stray")
        self.assertEqual(result["reconcile"]["archives_deleted"], 0)
        self.assertIn("archive:sweep_deferred", result["notes"])

    # -- the sweep of stray archives has a bound of its own ------------------------------------------------------------
    # The sweep runs in reconcile, before the work of the pass, and its listing is bounded by the pass deadline only.
    # It starts no delete once the pass is ``STRAY_SWEEP_SECONDS`` old, so a late listing or a backlog of slow deletes
    # starts none after that, and the stray deletes end within about that bound and one delete (30 + 40 seconds). The
    # work and the owed deletions at the end run on the time that is left; a pass whose own work uses all of it leaves
    # them none, with or without strays, and one whose work ends between 130 and 200 seconds can still lose its turn.

    def strays(self, count):
        """``count`` stray archives: older than an hour, and named by no row."""
        for number in range(count):
            self.world.objects[f"{PREFIX}stray-{number:02d}.tar.gz"] = (b"stray", NOW - 2 * HOUR)

    def remaining_strays(self):
        return [name for name in self.world.objects if "/stray-" in name]

    def a_listing_that_takes(self, seconds):
        """Make the stray listing take ``seconds`` of the clock. Returns the times it began at."""
        began = []

        def slow(arguments):
            began.append(self.clock())
            self.clock.advance(seconds)

        self.world.hooks["list_objects_v2"] = slow
        return began

    def stray_deletes_that_take(self, seconds):
        """Make each delete of a stray object take ``seconds`` of the clock. Returns the times they began at.

        A hook of ``delete_object`` that is set already (the owed deletions of ``owe``) keeps handling the other keys.
        """
        others = self.world.hooks.get("delete_object")
        began = []

        def slow(arguments):
            if "/stray-" in arguments["Key"]:
                began.append(self.clock())
                self.clock.advance(seconds)
            elif others is not None:
                others(arguments)

        self.world.hooks["delete_object"] = slow
        return began

    def test_a_listing_that_returns_at_the_sweep_bound_starts_no_stray_delete(self):
        self.strays(3)
        listed = self.a_listing_that_takes(STRAY_SWEEP_SECONDS)
        started_at = self.clock()
        result = self.apply()
        # The premise: the listing began inside the bound and returned at it, inside the pass deadline.
        (began,) = listed
        self.assertLess(began, started_at + STRAY_SWEEP_SECONDS)
        self.assertLess(began + STRAY_SWEEP_SECONDS, started_at + PASS_DEADLINE_SECONDS)
        # So no delete started, and the strays are all still there.
        self.assertEqual(self.world.arguments("delete_object"), [])
        self.assertEqual(len(self.remaining_strays()), 3)
        self.assertEqual(result["reconcile"]["archives_deleted"], 0)
        self.assertIn("archive:sweep_deferred", result["notes"])
        # The pass went on with its own work, and a later pass with a quick listing sweeps them.
        self.assertEqual(result["decision"], "building", result)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        del self.world.hooks["list_objects_v2"]
        later = self.apply()
        self.assertEqual(later["reconcile"]["archives_deleted"], 3, later)
        self.assertEqual(self.remaining_strays(), [])
        self.assertNotIn("archive:sweep_deferred", later["notes"])

    def test_a_listing_that_returns_just_inside_the_sweep_bound_sweeps_every_stray(self):
        # The same pass with a listing one second shorter, so the test above can tell a bound from a sweep that never ran.
        self.strays(3)
        listed = self.a_listing_that_takes(STRAY_SWEEP_SECONDS - 1)
        started_at = self.clock()
        result = self.apply()
        (began,) = listed
        self.assertLess(began + STRAY_SWEEP_SECONDS - 1, started_at + STRAY_SWEEP_SECONDS)
        self.assertEqual(len(self.world.arguments("delete_object")), 3)
        self.assertEqual(self.remaining_strays(), [])
        self.assertEqual(result["reconcile"]["archives_deleted"], 3)
        self.assertNotIn("archive:sweep_deferred", result["notes"])

    def test_slow_stray_deletes_start_no_new_delete_at_or_after_the_sweep_bound(self):
        self.strays(5)
        # The first delete ends inside the bound, so a second one starts. It ends after the bound, so no third starts.
        seconds = STRAY_SWEEP_SECONDS * 2 // 3
        began = self.stray_deletes_that_take(seconds)
        started_at = self.clock()
        result = self.apply()
        self.assertTrue(all(start < started_at + STRAY_SWEEP_SECONDS for start in began), began)
        self.assertEqual(len(began), 2, began)
        self.assertGreaterEqual(began[-1] + seconds, started_at + STRAY_SWEEP_SECONDS)
        self.assertEqual(result["reconcile"]["archives_deleted"], 2)
        self.assertEqual(len(self.remaining_strays()), 3)
        self.assertIn("archive:sweep_deferred", result["notes"])
        # The pass still had its time: it started and launched the build.
        self.assertEqual(result["decision"], "building", result)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)

    def test_a_stray_backlog_leaves_the_owed_delete_its_turn(self):
        # Twenty strays whose deletes each take as long as one can, and a finished build that owes its archive. The pass
        # has no work to do, so the sweep is all that can take the time that the owed delete needs.
        (owed,) = self.owe(1)
        self.strays(20)
        self.pin_current()
        began = self.stray_deletes_that_take(OWED_DELETE_RESERVE_SECONDS)
        started_at = self.clock()
        result = self.apply()
        # The sweep stopped at its bound: one delete began inside it and took the reserve, and no other began.
        self.assertEqual(len(began), 1, began)
        self.assertEqual(len(self.remaining_strays()), 19)
        self.assertIn("archive:sweep_deferred", result["notes"])
        # The owed delete started after the sweep, and inside its own bound.
        (start,) = self.starts
        self.assertGreaterEqual(start, began[0] + OWED_DELETE_RESERVE_SECONDS)
        self.assertLess(start, started_at + PASS_DEADLINE_SECONDS - OWED_DELETE_RESERVE_SECONDS)
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 1)
        self.assertNotIn(owed, self.world.objects)
        self.assertIsNotNone(self.only_build_named("owed-00").archive_deleted_at)
        self.assertNotIn("archive:delete_deferred", result["notes"])

    def test_a_stray_backlog_leaves_a_pass_that_ends_a_build_and_its_owed_delete_their_time(self):
        # The same backlog in a pass that has work: it starts a build and follows it to its end, which takes about 120
        # seconds in this world. The sweep (the bound and one delete) and that work still leave the owed delete its turn.
        # A bound of 60 seconds would not: the sweep would take 80 and the owed delete would start at 201.
        (owed,) = self.owe(1)
        self.strays(20)
        began = self.stray_deletes_that_take(OWED_DELETE_RESERVE_SECONDS)
        self.world.console_plan = [(30.0, READY)]
        started_at = self.clock()
        result = self.apply()
        # The sweep stopped after its first delete, and the work ran to the end of the build.
        self.assertEqual(len(began), 1, began)
        self.assertEqual(len(self.remaining_strays()), 19)
        self.assertIn("archive:sweep_deferred", result["notes"])
        self.assertEqual(result["decision"], "candidate", result)
        # The owed delete started after the work, inside its own bound, and settled.
        (start,) = self.starts
        self.assertGreaterEqual(start, began[0] + OWED_DELETE_RESERVE_SECONDS)
        self.assertLess(start, started_at + PASS_DEADLINE_SECONDS - OWED_DELETE_RESERVE_SECONDS)
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 1)
        self.assertNotIn(owed, self.world.objects)
        self.assertNotIn("archive:delete_deferred", result["notes"])

    def test_the_last_stray_delete_ends_before_the_last_start_of_an_owed_delete(self):
        # One delete can use up the reserve, so the last stray delete ends at the bound plus the reserve (70 seconds).
        # An owed delete starts until the pass deadline minus the reserve (200 seconds): the stray deletes leave it
        # that time. This checks the constants only; the listing before the first delete is not part of it.
        self.assertLess(STRAY_SWEEP_SECONDS + OWED_DELETE_RESERVE_SECONDS, PASS_DEADLINE_SECONDS - OWED_DELETE_RESERVE_SECONDS)

    def test_every_stray_delete_is_given_the_sweep_bound_as_its_deadline(self):
        self.strays(2)
        deadlines = []
        delete = ImageAws.delete_object

        def spy(adapter, bucket, stray, **keywords):
            deadlines.append(keywords.get("deadline"))
            return delete(adapter, bucket, stray, **keywords)

        started_at = self.clock()
        with mock.patch.object(ImageAws, "delete_object", spy):
            self.apply()
        self.assertEqual(deadlines, [started_at + STRAY_SWEEP_SECONDS] * 2)

    def test_a_stray_delete_that_the_adapter_refuses_for_the_deadline_ends_the_sweep_without_a_failure(self):
        self.strays(3)
        refused = []

        def refuse(adapter, bucket, stray, **keywords):
            refused.append(stray)
            raise DeadlineRefusal("archive delete")

        with mock.patch.object(ImageAws, "delete_object", refuse):
            result = self.apply()
        # The first delete was refused for the deadline: the sweep stopped, noted it and counted no failure.
        self.assertEqual(len(refused), 1)
        self.assertEqual(result["reconcile"]["failures"], [])
        self.assertEqual(result["reconcile"]["archives_deleted"], 0)
        self.assertIn("archive:sweep_deferred", result["notes"])
        # The pass went on with its own work.
        self.assertEqual(result["decision"], "building", result)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)

    # -- the bound is elapsed time too: a wall clock that is stepped back cannot stretch it -----------------------------
    # The pass deadline and the deadline that the adapter checks before each request are times on the wall clock. A wall
    # clock that is stepped back (a time correction, a host that resumed) would keep a sweep going past its bound, so the
    # pipeline also reads elapsed time on a monotonic clock before each stray delete. The sweep stops at whichever clock
    # reaches the bound first. The tests above run on the real monotonic clock, which a pass that only advances the fake
    # wall clock moves by milliseconds, so there the wall clock alone decides.

    def a_pipeline_with_a_monotonic_clock(self, monotonic):
        """Build the pipeline of this test again, on the same files and with the injected monotonic clock."""
        self.pipeline = ImagePipeline(
            self.config,
            self.config_path,
            aws_factory=self.aws_factory,
            source_factory=self.source_factory,
            state=self.state,
            clock=self.clock,
            monotonic=monotonic,
            sleep=self.sleep,
            script=SCRIPT,
        )

    def a_call_that_takes_while_the_wall_clock_steps_back(self, call, seconds, monotonic, *, only=None):
        """Make the provider call ``call`` take ``seconds`` of elapsed time, and step the wall clock back by as much.

        The wall clock then shows none of the time that the call took. ``only`` picks the calls by their arguments.
        Returns the readings of the wall clock and of the monotonic clock at which each call began.
        """
        began = []

        def slow(arguments):
            if only is None or only(arguments):
                began.append((self.clock(), monotonic()))
                monotonic.advance(seconds)
                self.clock.advance(-seconds)

        self.world.hooks[call] = slow
        return began

    def test_a_wall_clock_that_steps_back_does_not_stretch_the_bound_of_the_stray_sweep(self):
        self.strays(5)
        monotonic = Clock(1000.0)
        self.a_pipeline_with_a_monotonic_clock(monotonic)
        # Each delete takes 12 seconds, and the wall clock steps back by 12 seconds with it: it never shows the bound.
        seconds = STRAY_SWEEP_SECONDS * 2 // 5
        began = self.a_call_that_takes_while_the_wall_clock_steps_back(
            "delete_object", seconds, monotonic, only=lambda arguments: "/stray-" in arguments["Key"]
        )
        started_at, started_on = self.clock(), monotonic()
        result = self.apply()
        # The premise: the wall clock was before the bound whenever a delete began, so only elapsed time could stop the sweep.
        self.assertTrue(all(wall < started_at + STRAY_SWEEP_SECONDS for wall, _ in began), began)
        # Three deletes began inside the bound, at 0, 12 and 24 seconds of elapsed time, and a fourth would have at 36.
        self.assertEqual([elapsed - started_on for _, elapsed in began], [0, seconds, 2 * seconds])
        self.assertEqual(result["reconcile"]["archives_deleted"], 3)
        self.assertEqual(len(self.remaining_strays()), 2)
        self.assertEqual(result["reconcile"]["failures"], [])
        self.assertIn("archive:sweep_deferred", result["notes"])
        # The pass went on with its own work, and a later pass sweeps what the bound left.
        self.assertEqual(result["decision"], "building", result)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        del self.world.hooks["delete_object"]
        later = self.apply()
        self.assertEqual(later["reconcile"]["archives_deleted"], 2, later)
        self.assertEqual(self.remaining_strays(), [])
        self.assertNotIn("archive:sweep_deferred", later["notes"])

    def test_a_listing_that_returns_at_the_bound_by_elapsed_time_starts_no_stray_delete_on_a_wall_clock_that_stepped_back(self):
        self.strays(3)
        monotonic = Clock(1000.0)
        self.a_pipeline_with_a_monotonic_clock(monotonic)
        listed = self.a_call_that_takes_while_the_wall_clock_steps_back("list_objects_v2", STRAY_SWEEP_SECONDS, monotonic)
        result = self.apply()
        # The premise: the listing took the bound in elapsed time, which it alone spent, and the wall clock went back by as much.
        self.assertEqual(len(listed), 1)
        self.assertEqual(monotonic(), 1000.0 + STRAY_SWEEP_SECONDS)
        self.assertEqual(self.world.arguments("delete_object"), [])
        self.assertEqual(len(self.remaining_strays()), 3)
        self.assertEqual(result["reconcile"]["archives_deleted"], 0)
        self.assertIn("archive:sweep_deferred", result["notes"])
        self.assertEqual(result["decision"], "building", result)

    def test_a_listing_that_returns_just_inside_the_bound_by_elapsed_time_sweeps_every_stray_on_a_wall_clock_that_stepped_back(self):
        # The same pass with a listing one second shorter, so the test above can tell a bound from a sweep that never ran.
        self.strays(3)
        monotonic = Clock(1000.0)
        self.a_pipeline_with_a_monotonic_clock(monotonic)
        listed = self.a_call_that_takes_while_the_wall_clock_steps_back(
            "list_objects_v2", STRAY_SWEEP_SECONDS - 1, monotonic
        )
        result = self.apply()
        self.assertEqual(len(listed), 1)
        self.assertEqual(monotonic(), 1000.0 + STRAY_SWEEP_SECONDS - 1)
        self.assertEqual(len(self.world.arguments("delete_object")), 3)
        self.assertEqual(self.remaining_strays(), [])
        self.assertEqual(result["reconcile"]["archives_deleted"], 3)
        self.assertNotIn("archive:sweep_deferred", result["notes"])

    def test_the_wall_clock_still_stops_the_sweep_when_elapsed_time_has_not_reached_the_bound(self):
        # The control of the tests above for the other clock: a wall clock that jumps ahead ends the sweep as before,
        # whatever the monotonic clock says. The adapter refuses the delete for the deadline that it was given.
        self.strays(3)
        monotonic = Clock(1000.0)
        self.a_pipeline_with_a_monotonic_clock(monotonic)
        self.a_listing_that_takes(STRAY_SWEEP_SECONDS)
        result = self.apply()
        self.assertEqual(monotonic(), 1000.0)
        self.assertEqual(self.world.arguments("delete_object"), [])
        self.assertEqual(len(self.remaining_strays()), 3)
        self.assertIn("archive:sweep_deferred", result["notes"])

    # -- both cutoffs are read again right before the request ----------------------------------------------------------
    # The pipeline reads elapsed time before it calls the adapter, and the adapter reads the clocks once more right
    # before it sends the request: time can pass in between (the lookup of the account, the build of the S3 client). The
    # adapter therefore evaluates both cutoffs there, the wall clock against the deadline that it is given and elapsed
    # time on the pipeline's monotonic clock against the bound. The tests above cover the pipeline's own check.

    @contextmanager
    def a_delete_that_finds_time_gone_when_it_is_called(self, monotonic, seconds, *, only_the_first=True):
        """Make ``seconds`` of elapsed time pass between the pipeline's check and the adapter's, for a stray delete.

        The adapter is called after the pipeline's check has passed, so the time passes on entry to the call. The wall
        clock steps back by as much, so it shows none of it. Yields the readings ``((wall, elapsed), (wall, elapsed))``
        before and after each such step.
        """
        real = ImageAws.delete_object
        readings = []

        def late(adapter, bucket, key, **keywords):
            if "/stray-" in key and not (only_the_first and readings):
                before = (self.clock(), monotonic())
                monotonic.advance(seconds)
                self.clock.advance(-seconds)
                readings.append((before, (self.clock(), monotonic())))
            return real(adapter, bucket, key, **keywords)

        with mock.patch.object(ImageAws, "delete_object", late):
            yield readings

    def test_a_stray_delete_is_not_sent_when_elapsed_time_reaches_the_bound_after_the_pipeline_check_on_a_wall_clock_that_stepped_back(self):
        self.strays(3)
        monotonic = Clock(1000.0)
        self.a_pipeline_with_a_monotonic_clock(monotonic)
        started_at, started_on = self.clock(), monotonic()
        with self.a_delete_that_finds_time_gone_when_it_is_called(monotonic, STRAY_SWEEP_SECONDS) as readings:
            result = self.apply()
        # The premise: the adapter was called once, after the pipeline's check had passed (elapsed time was inside the
        # bound), and it found the wall clock behind its deadline and elapsed time at the bound.
        ((before, after),) = readings
        self.assertLess(before[1] - started_on, STRAY_SWEEP_SECONDS)
        self.assertLess(after[0], started_at + STRAY_SWEEP_SECONDS)
        self.assertGreaterEqual(after[1] - started_on, STRAY_SWEEP_SECONDS)
        # So the adapter sent no delete, and the sweep ended without a failure.
        self.assertEqual(self.world.arguments("delete_object"), [])
        self.assertEqual(len(self.remaining_strays()), 3)
        self.assertEqual(result["reconcile"]["archives_deleted"], 0)
        self.assertEqual(result["reconcile"]["failures"], [])
        self.assertIn("archive:sweep_deferred", result["notes"])
        # The pass went on with its own work.
        self.assertEqual(result["decision"], "building", result)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)

    def test_a_stray_delete_is_sent_when_elapsed_time_is_just_inside_the_bound_after_the_pipeline_check(self):
        # The control of the test above: one second less of elapsed time, so the test above can tell a check at the
        # adapter from an adapter that never sends. The sweep then runs to its end.
        self.strays(3)
        monotonic = Clock(1000.0)
        self.a_pipeline_with_a_monotonic_clock(monotonic)
        started_at, started_on = self.clock(), monotonic()
        with self.a_delete_that_finds_time_gone_when_it_is_called(monotonic, STRAY_SWEEP_SECONDS - 1) as readings:
            result = self.apply()
        ((before, after),) = readings
        self.assertLess(after[0], started_at + STRAY_SWEEP_SECONDS)
        self.assertLess(after[1] - started_on, STRAY_SWEEP_SECONDS)
        self.assertEqual(len(self.world.arguments("delete_object")), 3)
        self.assertEqual(self.remaining_strays(), [])
        self.assertEqual(result["reconcile"]["archives_deleted"], 3)
        self.assertNotIn("archive:sweep_deferred", result["notes"])

    def test_a_stray_delete_is_not_sent_after_the_pipeline_check_when_the_wall_clock_alone_reaches_the_deadline(self):
        # The other clock: elapsed time shows none of the time (the monotonic clock is not moved), the wall clock jumps
        # to the deadline, and the adapter refuses for it as before.
        self.strays(3)
        monotonic = Clock(1000.0)
        self.a_pipeline_with_a_monotonic_clock(monotonic)
        started_at, started_on = self.clock(), monotonic()
        real = ImageAws.delete_object
        jumped = []

        def late(adapter, bucket, key, **keywords):
            if not jumped:
                jumped.append((self.clock(), monotonic()))
                self.clock.advance(STRAY_SWEEP_SECONDS)
            return real(adapter, bucket, key, **keywords)

        with mock.patch.object(ImageAws, "delete_object", late):
            result = self.apply()
        # The premise: the wall clock reached its deadline between the checks, and elapsed time had not moved.
        ((wall, elapsed),) = jumped
        self.assertLess(wall, started_at + STRAY_SWEEP_SECONDS)
        self.assertGreaterEqual(self.clock(), started_at + STRAY_SWEEP_SECONDS)
        self.assertEqual(monotonic() - started_on, 0)
        self.assertEqual(self.world.arguments("delete_object"), [])
        self.assertEqual(len(self.remaining_strays()), 3)
        self.assertIn("archive:sweep_deferred", result["notes"])

    def test_an_active_build_advances_in_a_pass_that_meets_a_slow_refused_backlog(self):
        self.owe(12, stuck=range(12))
        first = self.apply()
        self.assertEqual(first["decision"], "building", first)
        # Each refused deletion now takes 25 seconds: the whole backlog would use up the pass deadline.
        self.seconds_per_delete = 25
        self.world.console_plan = [(30.0, READY)]
        mark = len(self.world.calls)
        result = self.apply()
        # The build advanced first, to its end, and the owed deletions took the time that was left.
        self.assertEqual(result["decision"], "candidate", result)
        self.assert_owed_deletes_follow_the_work(mark)
        self.assertLessEqual(len(self.owed_deletes(mark)), OWED_ARCHIVES_PER_PASS)
        self.assertIn("archive:delete_deferred", result["notes"])

    def owed_deletes(self, mark):
        return [call for name, call in self.world.calls[mark:] if name == "delete_object" and "/owed-" in call["Key"]]

    def test_a_clock_that_moves_backward_does_not_skip_a_row_that_a_denied_delete_left_owed(self):
        keys = self.owe(1, stuck=[0])
        self.world.console_plan = [(30.0, READY)]
        first = self.apply()
        self.assertEqual(first["reconcile"]["owed_archive_delete_failed"], 1)
        self.pin_current()
        tried = self.only_build_named("owed-00").archive_delete_tried_at
        self.assertIsNotNone(tried)
        # The next pass starts before the recorded attempt: a pass skips a row by what it attempted, not by that time.
        self.clock.advance(-(self.clock() - tried) - 3600)
        self.assertLess(self.clock(), tried)
        mark = len(self.world.calls)
        second = self.apply()
        self.assertEqual(second["reconcile"]["owed_archive_delete_failed"], 1, second)
        self.assertEqual(len(self.owed_deletes(mark)), 1)
        self.assertIn("archive:delete_failed", second["notes"])
        # The bucket allows it again, the clock is still behind, and the row settles.
        del self.world.hooks["delete_object"]
        self.clock.advance(-3600)
        third = self.apply()
        self.assertEqual(third["reconcile"]["owed_archives_deleted"], 1, third)
        self.assertNotIn(keys[0], self.world.objects)
        self.assertIsNotNone(self.only_build_named("owed-00").archive_deleted_at)

    def test_the_ninth_row_is_tried_within_two_passes_when_the_clock_stepped_back_after_its_own_attempt(self):
        keys = self.owe(9)
        ninth = keys[8]
        deletable = {ninth}

        def refuse(arguments):
            if arguments["Key"] not in deletable:
                raise ProviderError("RequestLimitExceeded")

        self.world.hooks["delete_object"] = refuse
        # The ninth row was refused first, at a clock reading that is far ahead of the readings of the eight others,
        # which were refused later, after the clock had stepped back. By the times, the ninth is behind all eight.
        self.write(lambda state: state.mark_archive_delete_tried("owed-08", NOW + 100_000))
        for number in range(8):
            self.write(lambda state, number=number: state.mark_archive_delete_tried(f"owed-{number:02d}", NOW))
        self.world.console_plan = [(30.0, READY)]
        settled = []
        for attempt in range(2):
            result = self.apply()
            self.pin_current()
            row = self.only_build_named("owed-08")
            settled.append(row.archive_deleted_at is not None)
            if settled[-1]:
                break
        self.assertTrue(any(settled), settled)
        self.assertNotIn(ninth, self.world.objects)
        # The eight rows that stay refused are still owed.
        owed = self.query(lambda state: [row.id for row in state.archives_owed(DEPLOYMENT)])
        self.assertEqual(sorted(row for row in owed if row.startswith("owed-")), [f"owed-{n:02d}" for n in range(8)])

    def test_a_restage_that_needs_most_of_the_pass_is_not_held_by_an_owed_delete_that_times_out(self):
        key = f"{PREFIX}owed-slow.tar.gz"
        self.add_build("owed-slow", archive_bucket=BUCKET, archive_key=key)
        self.world.objects[key] = (b"archive", NOW)
        seconds = {"per_delete": 0}

        def refuse(arguments):
            if arguments["Key"] == key:
                self.clock.advance(seconds["per_delete"])
                raise ProviderError("RequestTimeout")

        self.world.hooks["delete_object"] = refuse
        # The first pass owes the row too, and has no active build when it starts: the delete is refused at once.
        self.world.hooks["generate_presigned_url"] = lambda arguments: self.clock.advance(400)
        self.apply()
        del self.world.hooks["generate_presigned_url"]
        staged_again = [row for row in self.builds() if row.id != "owed-slow"][0]
        seconds["per_delete"] = 30
        # The restage needs 215 seconds of the 240 of the pass: 30 more for a delete would miss its deadline.
        self.world.hooks["create_multipart_upload"] = lambda arguments: self.clock.advance(215)
        self.world.console_plan = [(5.0, READY)]
        mark = len(self.world.calls)
        result = self.apply()
        self.assertEqual(self.only_build_named(staged_again.id).reason_code, "")
        self.assertNotEqual(result["decision"], "failed", result)
        self.assertNotIn("pass_deadline_exceeded", result["notes"])
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.owed_deletes(mark), [])
        self.assertIn("archive:delete_deferred", result["notes"])
        # The build ends in the next pass. The owed row is tried after that work, on the time it leaves.
        del self.world.hooks["create_multipart_upload"]
        self.world.console_plan = [(30.0, READY)]
        mark = len(self.world.calls)
        ended = self.apply()
        self.assertEqual(ended["decision"], "candidate", ended)
        self.assertEqual(len(self.owed_deletes(mark)), 1)
        self.assert_owed_deletes_follow_the_work(mark)
        # The next pass settles it.
        self.clock.advance(60)
        seconds["per_delete"] = 0
        del self.world.hooks["delete_object"]
        self.pin_current()
        later = self.apply()
        self.assertEqual(later["reconcile"]["owed_archives_deleted"], 1)
        self.assertNotIn(key, self.world.objects)

    def only_build_named(self, build_id):
        (row,) = [row for row in self.builds() if row.id == build_id]
        return row

    def test_the_stray_sweep_leaves_an_owed_object_to_the_bounded_retry_and_still_deletes_a_true_stray(self):
        # The objects are older than an hour, which is what the sweep takes for a stray.
        old = NOW - 2 * HOUR
        keys = self.owe(12, stuck=range(6), modified=old)
        stray = f"{PREFIX}no-row-owns-this.tar.gz"
        self.world.objects[stray] = (b"archive", old)
        self.world.console_plan = [(30.0, READY)]
        self.apply()
        # The build's own archive goes at the end of the build: it is neither owed nor a stray.
        attempts = [
            call["Key"]
            for call in self.world.arguments("delete_object")
            if "/owed-" in call["Key"] or call["Key"] == stray
        ]
        # Eight owed deletions, the six refused among them, and the sweep's one true stray: nothing else.
        self.assertEqual(sorted(attempts), sorted([*keys[:OWED_ARCHIVES_PER_PASS], stray]))
        self.assertNotIn(stray, self.world.objects)
        # The four that the bound deferred are still there, and so are the six that were refused.
        self.assertTrue(set(keys[OWED_ARCHIVES_PER_PASS:]) <= set(self.world.objects))
        self.assertTrue(set(keys[:6]) <= set(self.world.objects))
        self.assertEqual(
            [row.id for row in self.query(lambda state: state.archives_owed(DEPLOYMENT))],
            [f"owed-{number:02d}" for number in (8, 9, 10, 11, 0, 1, 2, 3, 4, 5)],
        )

    def test_a_backlog_within_the_bound_defers_nothing(self):
        self.owe(OWED_ARCHIVES_PER_PASS)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], OWED_ARCHIVES_PER_PASS)
        self.assertNotIn("archive:delete_deferred", result["notes"])
        self.assertEqual(self.query(lambda state: state.archives_owed(DEPLOYMENT)), [])

    def test_a_permanently_refused_row_does_not_keep_a_newer_row_from_its_turn_on_a_later_pass(self):
        # More refused rows than one pass can try, then one row that would succeed, the newest of them all.
        stuck = OWED_ARCHIVES_PER_PASS + 1
        keys = self.owe(stuck + 1, stuck=range(stuck))
        self.configure(render_toml(gate=""))
        self.apply()
        self.assertEqual(len(self.world.arguments("delete_object")), OWED_ARCHIVES_PER_PASS)
        self.assertIn(keys[-1], self.world.objects)
        self.clock.advance(60)
        result = self.apply()
        # The pass tries the row never tried and the one that is not refused first, then the refused rows that were
        # tried the longest ago. Rows 6 and 7 wait for their turn behind rows 0 to 5.
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 1)
        self.assertNotIn(keys[-1], self.world.objects)
        newest = [row for row in self.builds() if row.archive_key == keys[-1]]
        self.assertIsNotNone(newest[0].archive_deleted_at)
        self.assertEqual(
            [row.id for row in self.query(lambda state: state.archives_owed(DEPLOYMENT))],
            [f"owed-{number:02d}" for number in (6, 7, 8, 0, 1, 2, 3, 4, 5)],
        )

    def test_a_slow_failing_owed_delete_does_not_starve_the_insert_of_the_next_build(self):
        # The basis read of the decision takes 200 of the 240 seconds of the pass.
        self.owe(1, stuck=[0])
        self.seconds_per_delete = 30
        self.source.on_read = lambda: self.clock.advance(200)
        result = self.apply()
        self.assertEqual(result["decision"], "building", result)
        build = [row for row in self.builds() if not row.id.startswith("owed-")]
        self.assertEqual([row.state for row in build], ["building"])
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        # The owed delete came after the decision and the launch, or was deferred: never before them.
        names = self.world.names()
        if "delete_object" in names:
            self.assertGreater(names.index("delete_object"), names.index("run_instances"))
        self.assertIn("archive:delete_deferred", result["notes"])

    def test_a_pass_that_ends_on_a_refused_read_still_deletes_what_is_owed_and_raises_the_same_refusal(self):
        old_bucket = "example-old-staging"
        key = f"{PREFIX}owed-old.tar.gz"
        self.add_build("owed-old", archive_bucket=old_bucket, archive_key=key)
        self.world.objects[key] = (b"archive", NOW)

        def refuse(arguments):
            raise ProviderError("RequestLimitExceeded")

        # Whatever the first pass tries with the row, the old bucket refuses it.
        self.world.hooks["delete_object"] = refuse
        self.apply()
        del self.world.hooks["delete_object"]
        # The build is active, and the pass that follows finds it overdue and the listing of builders denied.
        self.assertEqual(self.query(lambda state: state.active_build(DEPLOYMENT)).state, "building")
        self.clock.advance(BUILD_DEADLINE)
        self.world.fail("describe_instances", ProviderError("RequestLimitExceeded"), times=1)
        refusal = self.refused(self.apply, "AWS_UNCERTAIN")
        self.assertEqual(refusal.detail, "ProviderError")
        self.assertEqual(self.world.arguments("delete_object")[-1], {"Bucket": old_bucket, "Key": key, "ExpectedBucketOwner": ACCOUNT})
        (owed,) = [row for row in self.builds() if row.id == "owed-old"]
        self.assertIsNotNone(owed.archive_deleted_at)
        self.assertNotIn(key, self.world.objects)

    def test_a_cancellation_of_the_pass_starts_no_owed_deletion_and_propagates(self):
        self.owe(2)

        def cancel(arguments):
            raise KeyboardInterrupt

        self.world.hooks["describe_instances"] = cancel
        with self.assertRaises(KeyboardInterrupt):
            self.apply()
        self.assertEqual(self.world.arguments("delete_object"), [])
        self.assertEqual(len(self.query(lambda state: state.archives_owed(DEPLOYMENT))), 2)

    def test_a_refused_owed_delete_stays_visible_as_an_event_when_the_pass_raises(self):
        old_bucket = "example-old-staging"
        key = f"{PREFIX}owed-old.tar.gz"
        self.add_build("owed-old", archive_bucket=old_bucket, archive_key=key)
        self.world.objects[key] = (b"archive", NOW)
        self.world.hooks["delete_object"] = lambda arguments: (_ for _ in ()).throw(ProviderError("AccessDenied"))
        self.world.fail("describe_instances", ProviderError("RequestLimitExceeded"), times=1)
        refusal = self.refused(self.apply, "AWS_UNCERTAIN")
        self.assertEqual(refusal.detail, "ProviderError")
        (event,) = self.events("image_reconciled")
        self.assertEqual(
            (event["owed_archives_deleted"], event["owed_archive_delete_failed"], event["owed_archives_deferred"]),
            (0, 1, 0),
        )
        text = json.dumps(event)
        self.assertNotIn(old_bucket, text)
        self.assertNotIn(key, text)
        (row,) = self.builds()
        self.assertIsNone(row.archive_deleted_at)
        self.assertIsNotNone(row.archive_delete_tried_at)

    def two_owed_rows_the_first_of_which_the_bucket_denies(self):
        """The first row's delete is denied. The second is deleted, and the record of that raises a SQLite error."""
        denied = f"{PREFIX}owed-a.tar.gz"
        for name, age in (("a", 6 * DAY), ("b", 5 * DAY)):
            key = f"{PREFIX}owed-{name}.tar.gz"
            self.add_build(f"owed-{name}", age=age, archive_bucket="example-old-staging", archive_key=key)
            self.world.objects[key] = (b"archive", NOW)

        def deny(arguments):
            if arguments["Key"] == denied:
                raise ProviderError("AccessDenied")

        self.world.hooks["delete_object"] = deny
        return mock.patch.object(
            ImageState, "mark_archive_deleted", side_effect=sqlite3.OperationalError("disk I/O error")
        )

    def assert_one_event_counts_one_denied_delete(self):
        (event,) = self.events("image_reconciled")
        self.assertEqual(
            (event["owed_archives_deleted"], event["owed_archive_delete_failed"], event["owed_archives_deferred"]),
            (0, 1, 0),
        )
        first = [row for row in self.builds() if row.id == "owed-a"][0]
        self.assertIsNotNone(first.archive_delete_tried_at)
        # The second row's delete went through, but its record did not: it stays owed.
        second = [row for row in self.builds() if row.id == "owed-b"][0]
        self.assertIsNone(second.archive_deleted_at)
        self.assertEqual(
            [row.id for row in self.query(lambda state: state.archives_owed(DEPLOYMENT))], ["owed-b", "owed-a"]
        )

    def assert_the_next_pass_tries_both_rows_again(self, result):
        """The row that was denied is denied again, and the row whose record failed settles."""
        self.assertEqual(result["reconcile"]["owed_archive_delete_failed"], 1, result)
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 1, result)
        rows = {row.id: row for row in self.builds()}
        self.assertIsNone(rows["owed-a"].archive_deleted_at)
        self.assertIsNotNone(rows["owed-b"].archive_deleted_at)

    def test_the_counts_of_the_earlier_attempts_survive_a_later_one_that_raises_in_the_tail(self):
        patched = self.two_owed_rows_the_first_of_which_the_bucket_denies()
        self.world.fail("describe_instances", ProviderError("RequestLimitExceeded"), times=1)
        with patched:
            refusal = self.refused(self.apply, "AWS_UNCERTAIN")
        self.assertEqual(refusal.detail, "ProviderError")
        self.assert_one_event_counts_one_denied_delete()
        self.world.console_plan = [(30.0, READY)]
        self.assert_the_next_pass_tries_both_rows_again(self.apply())

    def test_the_counts_of_the_earlier_attempts_survive_a_later_one_that_raises_in_the_cleanup_pass(self):
        patched = self.two_owed_rows_the_first_of_which_the_bucket_denies()
        self.configure(render_toml(gate=""))
        with patched, self.assertRaises(sqlite3.OperationalError):
            self.apply()
        self.assert_one_event_counts_one_denied_delete()
        self.assert_the_next_pass_tries_both_rows_again(self.apply())

    def test_a_failure_to_write_the_owed_event_replaces_neither_the_refusal_nor_the_result(self):
        self.owe(1, stuck=[0])
        self.world.fail("describe_instances", ProviderError("RequestLimitExceeded"), times=1)
        with mock.patch.object(image_pipeline._Telemetry, "event", side_effect=OSError("disk full")):
            refusal = self.refused(self.apply, "AWS_UNCERTAIN")
        self.assertEqual(refusal.detail, "ProviderError")

    def test_rows_that_the_bound_left_are_counted_in_the_event(self):
        self.owe(OWED_ARCHIVES_PER_PASS + 2)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["reconcile"]["owed_archives_deferred"], 2)
        self.assertEqual(result["reconcile"]["owed_archive_delete_failed"], 0)
        event = self.events("image_reconciled")[-1]
        self.assertEqual(
            (event["owed_archives_deleted"], event["owed_archive_delete_failed"], event["owed_archives_deferred"]),
            (OWED_ARCHIVES_PER_PASS, 0, 2),
        )

    def test_a_failure_of_the_tail_does_not_replace_the_refusal_of_the_pass(self):
        self.owe(1)
        self.world.fail("describe_instances", ProviderError("RequestLimitExceeded"), times=1)
        with mock.patch.object(image_pipeline._Pass, "_delete_owed", side_effect=RuntimeError("tail broke")):
            refusal = self.refused(self.apply, "AWS_UNCERTAIN")
        self.assertEqual(refusal.detail, "ProviderError")

    def test_a_cleanup_pass_does_not_need_the_warm_script(self):
        (key,) = self.owe(1)
        self.configure(render_toml(gate=""), script=None)
        original = image.shipped_script
        absent = self.dir / "absent-warm-cache.sh"
        with mock.patch.object(image, "shipped_script", lambda: original(absent)):
            result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_GATE_REQUIRED"), result)
        self.assertIn("delete_object", self.world.names())
        self.assertNotIn(key, self.world.objects)
        self.assertIsNotNone(self.only_build().archive_deleted_at)

    def test_a_pass_with_no_gate_and_nothing_in_flight_runs_only_the_owed_deletions(self):
        (key,) = self.owe(1)
        self.configure(render_toml(gate=""))
        # Every read that the rest of a pass makes is refused. None of them may change the answer.
        for method in (
            "describe_launch_template_versions",
            "describe_instances",
            "list_objects_v2",
            "describe_images",
        ):
            self.world.fail(method, ProviderError("RequestLimitExceeded"))
        source_mark, world_mark = len(self.source.calls), len(self.world.calls)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_GATE_REQUIRED"), result)
        self.assertEqual(self.world.names(world_mark), ["delete_object"])
        self.assertNotIn(key, self.world.objects)
        self.assertEqual(self.only_build().archive_deleted_at, self.clock())
        self.assertEqual(result["reconcile"]["owed_archives_deleted"], 1)
        self.assertEqual(result["pin"], {"version": PIN, "image_id": None, "basis_digest": None})
        self.assertIsNone(result["retention"])
        self.assertIsNone(result["build"])
        self.assertEqual(self.source.calls[source_mark:], [])
        self.assertEqual(self.events("image_check")[-1]["reason"], "IMAGE_GATE_REQUIRED")


class RestageReadsNoBasisFileTests(PipelineCase):
    """A stage done again checks the inputs against the hashes that its row recorded, and reads no file of the forge."""

    PATHS = ("example.lock", "second.lock")

    def two_basis_paths(self):
        single = 'basis_paths = ["example.lock"]'
        self.assertIn(single, render_toml())
        names = ", ".join(f'"{path}"' for path in self.PATHS)
        self.configure(render_toml().replace(single, f"basis_paths = [{names}]"))
        self.source.files = {path: LOCK_FILE + path.encode() for path in self.PATHS}

    def unstaged_build(self):
        """A build whose first pass ran out of time during the stage: it has no builder and no staged archive."""
        self.source.on_chunk = lambda: self.clock.advance(400)
        self.apply()
        self.source.on_chunk = None
        return self.only_build()

    def reads(self, mark=0):
        return [path for kind, path in self.source.calls[mark:] if kind == "raw_file"]

    def test_the_row_records_the_hash_of_every_basis_file_it_was_inserted_under(self):
        self.two_basis_paths()
        build = self.unstaged_build()
        self.assertEqual(
            json.loads(build.basis_file_hashes),
            {path: hashlib.sha256(self.source.files[path]).hexdigest() for path in self.PATHS},
        )
        self.assertEqual(list(json.loads(build.basis_file_hashes)), sorted(self.PATHS))

    def test_a_stage_done_again_reads_no_basis_file_and_launches(self):
        self.two_basis_paths()
        first = self.unstaged_build()
        # Any read of the forge would be refused, and the branch head moved on.
        self.source.errors["raw_file"] = ObservationRefusal("FORGEJO_UNAVAILABLE")
        self.source.sha = "cd" * 20
        mark = len(self.source.calls)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(self.only_build().id, first.id)
        self.assertEqual(self.source.calls[mark:], [("archive", SOURCE_SHA)])
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.events("image_build_failed"), [])

    def test_basis_reads_that_would_use_up_the_pass_no_longer_starve_the_stage(self):
        self.two_basis_paths()
        first = self.unstaged_build()
        self.source.on_read = lambda: self.clock.advance(400)
        mark = len(self.source.calls)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(self.only_build().id, first.id)
        self.assertEqual(self.reads(mark), [])
        self.assertNotIn("pass_deadline_exceeded", result["notes"])
        self.assertEqual(len(self.archive_keys()), 2)
        self.assertEqual(len(self.world.arguments("run_instances")), 1)

    def archive_keys(self):
        return [call["Key"] for call in self.world.arguments("create_multipart_upload")]

    def assert_ended_for_its_inputs(self, first):
        mark = len(self.source.calls)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_INPUTS_CHANGED"), result)
        build = self.only_build()
        self.assertEqual((build.id, build.state, build.reason_code), (first.id, "failed", "IMAGE_INPUTS_CHANGED"))
        self.assertEqual(self.source.calls[mark:], [])
        self.assertEqual(len(self.archive_keys()), 1)
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.control().consecutive_failures, 0)

    def test_a_basis_path_that_the_configuration_gained_is_changed_inputs_and_reads_no_file(self):
        first = self.unstaged_build()
        self.two_basis_paths()
        self.assert_ended_for_its_inputs(first)

    def test_a_basis_path_that_the_configuration_lost_is_changed_inputs_and_reads_no_file(self):
        self.two_basis_paths()
        first = self.unstaged_build()
        self.configure(render_toml())
        self.assert_ended_for_its_inputs(first)

    def test_a_changed_gate_command_is_changed_inputs_and_reads_no_file(self):
        first = self.unstaged_build()
        self.configure(render_toml(gate=NO_GATE))
        self.assert_ended_for_its_inputs(first)

    def test_a_changed_script_is_changed_inputs_and_reads_no_file(self):
        first = self.unstaged_build()
        self.restart(script=SCRIPT + b"# edited\n")
        self.assert_ended_for_its_inputs(first)

    def test_a_row_with_no_recorded_hashes_is_damaged_state_and_is_not_read_from_the_forge(self):
        self.add_build("damaged", "building", age=300)
        mark = len(self.source.calls)
        self.refused(self.apply, "IMAGE_STATE_UNREADABLE", detail_has=("damaged",))
        self.assertEqual(self.source.calls[mark:], [])
        self.assertEqual(self.world.arguments("create_multipart_upload"), [])
        self.assertEqual(self.only_build().state, "building")

    def test_malformed_recorded_hashes_are_damaged_state_and_the_row_stays(self):
        first = self.unstaged_build()
        connection = sqlite3.connect(self.state_dir / "image-state.sqlite")
        try:
            connection.execute("UPDATE image_builds SET basis_file_hashes = ? WHERE id = ?", ("[not json", first.id))
            connection.commit()
        finally:
            connection.close()
        mark = len(self.source.calls)
        self.refused(self.apply, "IMAGE_STATE_UNREADABLE", detail_lacks=("not json",))
        self.assertEqual(self.source.calls[mark:], [])
        self.assertEqual(len(self.archive_keys()), 1)
        self.assertEqual(self.world.arguments("run_instances"), [])
        build = self.only_build()
        self.assertEqual((build.id, build.state), (first.id, "building"))

    def test_recorded_hashes_that_are_not_utf8_text_are_damaged_state_and_the_row_stays(self):
        # The column is text, and a file that something else damaged can hold bytes in it that are no UTF-8. SQLite
        # refuses to hand such text to Python, so the pass must refuse the row, and not fail on the read that fetches it.
        first = self.unstaged_build()
        marker = "marker-before-the-bad-byte"
        connection = sqlite3.connect(self.state_dir / "image-state.sqlite")
        try:
            connection.execute(
                "UPDATE image_builds SET basis_file_hashes = CAST(? AS TEXT) WHERE id = ?",
                (marker.encode() + b"\xff", first.id),
            )
            connection.commit()
            # The premise: the value is stored as text, and Python cannot read it.
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("SELECT basis_file_hashes FROM image_builds").fetchall()
        finally:
            connection.close()
        mark = len(self.source.calls)
        self.refused(self.apply, "IMAGE_STATE_UNREADABLE", detail_has=(first.id,), detail_lacks=(marker,))
        self.assertEqual(self.source.calls[mark:], [])
        self.assertEqual(len(self.archive_keys()), 1)
        self.assertEqual(self.world.arguments("run_instances"), [])
        build = self.only_build()
        self.assertEqual((build.id, build.state), (first.id, "building"))

    # -- a recorded map that the row could never have been written with ----------------------------------------
    # The text is valid JSON, and every value in it is a hash. What is wrong is the set of members, and the pass must
    # not take that for a change of the inputs, which ends the row and deletes its archive, or read past it.

    def record_hashes(self, build, stored):
        connection = sqlite3.connect(self.state_dir / "image-state.sqlite")
        try:
            connection.execute("UPDATE image_builds SET basis_file_hashes = ? WHERE id = ?", (stored, build.id))
            connection.commit()
        finally:
            connection.close()

    def assert_damaged_state_and_the_row_is_left_as_it_was(self, first, marker):
        """The pass refuses the row by its id and never by its value, and it ends, deletes and launches nothing."""
        mark = len(self.source.calls)
        world_mark = len(self.world.calls)
        self.refused(self.apply, "IMAGE_STATE_UNREADABLE", detail_has=(first.id,), detail_lacks=(marker,))
        self.assertEqual(self.source.calls[mark:], [])
        self.assertFalse(
            {"delete_object", "create_multipart_upload", "run_instances"} & set(self.world.names(world_mark)),
            self.world.names(world_mark),
        )
        self.assertEqual(len(self.archive_keys()), 1)
        self.assertEqual(self.world.arguments("run_instances"), [])
        build = self.only_build()
        self.assertEqual((build.id, build.state, build.reason_code), (first.id, "building", ""))
        self.assertIsNone(build.archive_deleted_at)
        self.assertEqual(self.control().consecutive_failures, 0)

    def test_a_recorded_path_that_the_configuration_could_never_hold_is_damaged_state_and_the_row_stays(self):
        # A newline in a path, or a parent directory, is something that no basis path of the configuration is. It is
        # not a change of the inputs, which ends the row as ``IMAGE_INPUTS_CHANGED`` and deletes its archive.
        first = self.unstaged_build()
        recorded = json.loads(first.basis_file_hashes)
        (digest,) = recorded.values()
        marker = "marker-path"
        for label, damaged in (
            ("a newline in the one path", {f"example.lock\n{marker}": digest}),
            ("a path with a newline beside the right one", {**recorded, f"{marker}\nsecond.lock": digest}),
            ("a parent directory beside the right path", {**recorded, f"../{marker}.lock": digest}),
        ):
            with self.subTest(label):
                self.record_hashes(first, json.dumps(damaged))
                self.assert_damaged_state_and_the_row_is_left_as_it_was(first, marker)

    def test_a_recorded_path_that_is_no_text_in_utf8_is_damaged_state_and_the_row_stays(self):
        # The configuration is read from a UTF-8 file, and TOML cannot spell a lone surrogate, so the configuration
        # never held one. The rules of a basis path do not name it, so without a check of its own the path would pass
        # as a path that the configuration lost: a change of the inputs, which ends the row and deletes its archive.
        first = self.unstaged_build()
        recorded = json.loads(first.basis_file_hashes)
        (digest,) = recorded.values()
        marker = "marker-path"
        high, low = chr(0xD83D), chr(0xDE00)
        for label, damaged in (
            ("a lone high surrogate in the one path", {f"example.lock{high}{marker}": digest}),
            ("a lone low surrogate beside the right path", {**recorded, f"{low}{marker}.lock": digest}),
            ("a low and a high surrogate that are no pair", {**recorded, f"{marker}-{low}{high}.lock": digest}),
        ):
            with self.subTest(label):
                text = json.dumps(damaged)
                # The premise: the text is ASCII, so that SQLite takes it, and the decoder gives the surrogate back.
                self.assertTrue(text.isascii())
                self.assertEqual(json.loads(text), damaged)
                self.record_hashes(first, text)
                self.assert_damaged_state_and_the_row_is_left_as_it_was(first, marker)

    def test_a_recorded_path_beyond_the_basic_plane_still_launches(self):
        # The control of the test above: a path of a character that one UTF-8 sequence spells and JSON spells as a
        # pair of escapes is text, and the stage that is done again goes on under it.
        single = 'basis_paths = ["example.lock"]'
        self.assertIn(single, render_toml())
        path = "emoji-\U0001f600.lock"
        self.configure(render_toml().replace(single, f'basis_paths = ["{path}"]'))
        self.source.files = {path: LOCK_FILE}
        first = self.unstaged_build()
        self.assertEqual(list(json.loads(first.basis_file_hashes)), [path])
        self.assertIn("\\ud83d\\ude00", first.basis_file_hashes)
        mark = len(self.source.calls)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(self.only_build().id, first.id)
        self.assertEqual(self.source.calls[mark:], [("archive", SOURCE_SHA)])
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.events("image_build_failed"), [])

    def test_a_recorded_map_that_names_a_path_twice_is_damaged_state_and_nothing_is_launched(self):
        # ``json.loads`` keeps the last of two members with one name: with the right hash last, the wrong one before
        # it would pass unseen, the digest would match, and the pass would launch from a row that it cannot read.
        first = self.unstaged_build()
        (digest,) = json.loads(first.basis_file_hashes).values()
        marker = "marker-value"
        for label, stored in (
            ("a first value that is no hash", f'{{"example.lock": "{marker}", "example.lock": "{digest}"}}'),
            ("a first hash that is another hash", '{"example.lock": "' + "e" * 64 + f'", "example.lock": "{digest}"}}'),
        ):
            with self.subTest(label):
                self.record_hashes(first, stored)
                self.assert_damaged_state_and_the_row_is_left_as_it_was(first, marker)

    def test_a_recorded_map_that_is_sound_and_written_another_way_still_launches(self):
        # The control of the two tests above: the same row and pass launch when the map is sound. The text has another
        # order and other spacing, which the decoder reads as the same map.
        self.two_basis_paths()
        first = self.unstaged_build()
        recorded = json.loads(first.basis_file_hashes)
        self.assertEqual(sorted(recorded), sorted(self.PATHS))
        self.record_hashes(first, json.dumps(dict(reversed(list(recorded.items()))), indent=2))
        mark = len(self.source.calls)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        self.assertEqual(self.only_build().id, first.id)
        self.assertEqual(self.source.calls[mark:], [("archive", SOURCE_SHA)])
        self.assertEqual(len(self.world.arguments("run_instances")), 1)
        self.assertEqual(self.events("image_build_failed"), [])


class GateEvidenceTests(PipelineCase):
    """A candidate records the gate of the build that was launched, never the gate that the file holds when it ends.

    The builder prints its marker only after its gate command passed, so a build that launched with a gate passed it,
    and a build that launched without one has nothing to show. The file can change between the launch and the pass
    that sees the marker: a gate gained or dropped there must not change what the candidate says about the build.
    """

    def unstaged_build(self):
        """A build whose first pass ran out of time during the stage: it has no builder and no staged archive."""
        self.source.on_chunk = lambda: self.clock.advance(400)
        self.apply()
        self.source.on_chunk = None
        return self.only_build()

    def finish(self):
        """The pass that sees the marker ends the build as a candidate."""
        self.clock.advance(30)
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        return self.only_build()

    def launch_then_edit(self, launched, edited):
        """A build launched under the gate setting ``launched``, ended by a pass that reads the file as ``edited``."""
        self.configure(render_toml(gate=launched))
        self.world.console_plan = [(300.0, READY)]
        _, launched_build = self.start_build()
        self.configure(render_toml(gate=edited))
        return launched_build, self.finish()

    def test_a_build_launched_without_a_gate_is_not_credited_with_a_gate_that_the_file_gains(self):
        launched, built = self.launch_then_edit(NO_GATE, GATE)
        self.assertEqual(launched.gate_configured, 0)
        self.assertEqual(built.id, launched.id)
        self.assertEqual((built.gate_configured, built.gate_passed), (0, 0))

    def test_a_build_launched_with_a_gate_keeps_its_evidence_when_the_file_drops_the_gate(self):
        launched, built = self.launch_then_edit(GATE, NO_GATE)
        self.assertEqual(launched.gate_configured, 1)
        self.assertEqual(built.id, launched.id)
        self.assertEqual((built.gate_configured, built.gate_passed), (1, 1))

    def test_a_build_launched_with_a_gate_is_settled_when_the_file_drops_the_gate_and_has_no_opt_out(self):
        # The gate requirement guards a new launch: the pass that finds this build in flight settles it, and the
        # candidate keeps the evidence of the launch that happened. Nothing else is launched.
        launched, built = self.launch_then_edit(GATE, "")
        self.assertEqual(launched.gate_configured, 1)
        self.assertEqual(built.id, launched.id)
        self.assertEqual((built.gate_configured, built.gate_passed), (1, 1))
        self.assertEqual(len(self.builds()), 1)
        self.assertEqual(len(self.world.launches), 1)

    def test_a_build_launched_without_a_gate_under_a_file_that_did_not_change_records_no_gate(self):
        self.configure(render_toml(gate=NO_GATE))
        self.world.console_plan = [(300.0, READY)]
        self.start_build()
        built = self.finish()
        self.assertEqual((built.gate_configured, built.gate_passed), (0, 0))

    def test_a_row_that_records_no_gate_shows_that_none_ran(self):
        self.world.console_plan = [(300.0, READY)]
        _, build = self.start_build()
        self.write(lambda state: state.advance_build(build.id, gate_configured=None))
        self.assertIsNone(self.only_build().gate_configured)
        built = self.finish()
        # The file holds a gate command, but the row does not say that the build ran it.
        self.assertEqual((built.gate_configured, built.gate_passed), (0, 0))

    def finish_after_damaging_the_recorded_gate(self, value):
        """A build launched without a gate, whose row is damaged after the launch, then ended by a pass.

        The damage comes after the launch because the launch writes the column itself.
        """
        self.configure(render_toml(gate=NO_GATE))
        self.world.console_plan = [(300.0, READY)]
        _, build = self.start_build()
        self.assertEqual(build.gate_configured, 0)
        self.write(lambda state: state.advance_build(build.id, gate_configured=value))
        self.assertEqual(self.only_build().gate_configured, value)
        return self.finish()

    def test_a_recorded_gate_of_two_is_not_gate_evidence(self):
        # Only a recorded 1 shows that a gate ran: 2 is not "true".
        built = self.finish_after_damaging_the_recorded_gate(2)
        self.assertEqual((built.gate_configured, built.gate_passed), (0, 0))

    def test_a_recorded_gate_that_is_text_is_not_gate_evidence(self):
        built = self.finish_after_damaging_the_recorded_gate("yes")
        self.assertEqual((built.gate_configured, built.gate_passed), (0, 0))

    def test_a_recorded_gate_of_one_is_still_gate_evidence(self):
        # The control for the two tests above: the same flow with an undamaged row records the gate that ran.
        self.configure(render_toml(gate=GATE))
        self.world.console_plan = [(300.0, READY)]
        _, build = self.start_build()
        self.assertEqual(build.gate_configured, 1)
        built = self.finish()
        self.assertEqual((built.gate_configured, built.gate_passed), (1, 1))

    # -- a build that is staged again ------------------------------------------------
    # A stage that the pass deadline ended is done again by a later pass. That pass launches only under the inputs
    # that its row was inserted with (the gate command is one of them), so the gate it writes is the gate it was
    # inserted with. The write of the gate in the stage stays as a second line of defence.

    def staged_again(self, inserted, restaged):
        """A build inserted under the gate setting ``inserted``, and the pass that stages it under ``restaged``."""
        self.configure(render_toml(gate=inserted))
        inserted_build = self.unstaged_build()
        self.configure(render_toml(gate=restaged))
        self.world.console_plan = [(30.0, READY)]
        return inserted_build, self.apply()

    def assert_not_launched_under_another_gate(self, inserted, result):
        """The pass ended the row, launched no builder and counted no failure."""
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_INPUTS_CHANGED"), result)
        self.assertEqual(self.world.launches, [])
        build = self.only_build()
        self.assertEqual((build.id, build.state, build.reason_code), (inserted.id, "failed", "IMAGE_INPUTS_CHANGED"))
        # The row still says what it was inserted with: no launch happened to write another gate.
        self.assertEqual((build.gate_configured, build.gate_passed), (inserted.gate_configured, None))
        control = self.control()
        self.assertEqual((control.consecutive_failures, control.cooldown_until, control.blocked), (0, None, False))
        self.assertEqual(self.events("image_build_failed"), [])

    def test_a_build_inserted_with_a_gate_is_not_launched_without_it_when_staged_again(self):
        inserted, result = self.staged_again(GATE, NO_GATE)
        self.assertEqual(inserted.gate_configured, 1)
        self.assert_not_launched_under_another_gate(inserted, result)

    def test_a_build_inserted_without_a_gate_is_not_launched_with_one_when_staged_again(self):
        inserted, result = self.staged_again(NO_GATE, GATE)
        self.assertEqual(inserted.gate_configured, 0)
        self.assert_not_launched_under_another_gate(inserted, result)

    def test_a_build_staged_again_under_the_gate_it_was_inserted_with_records_that_gate(self):
        inserted, result = self.staged_again(GATE, GATE)
        self.assertEqual(result["decision"], "candidate", result)
        built = self.only_build()
        (launch,) = self.world.launches
        self.assertEqual(built.id, inserted.id)
        self.assertIn(f"export WARM_GATE_COMMAND={shlex.quote('bash scripts/example-gate.sh')}", launch["UserData"])
        self.assertEqual((built.gate_configured, built.gate_passed), (1, 1))

    def test_a_build_staged_again_without_a_gate_under_a_file_that_still_has_none_records_no_gate(self):
        inserted, result = self.staged_again(NO_GATE, NO_GATE)
        self.assertEqual(result["decision"], "candidate", result)
        built = self.only_build()
        (launch,) = self.world.launches
        self.assertEqual(built.id, inserted.id)
        self.assertIn(f"export WARM_GATE_COMMAND={shlex.quote('')}", launch["UserData"])
        self.assertEqual((built.gate_configured, built.gate_passed), (0, 0))

    def test_the_stage_writes_the_gate_of_the_launch_when_the_row_has_lost_its_value(self):
        # The defence in ``_stage_and_launch``: the row names the gate of the launch that starts the builder, so a row
        # that lost its value still records the gate that ran.
        self.configure(render_toml(gate=GATE))
        inserted = self.unstaged_build()
        self.write(lambda state: state.advance_build(inserted.id, gate_configured=None))
        self.assertIsNone(self.only_build().gate_configured)
        self.world.console_plan = [(30.0, READY)]
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        built = self.only_build()
        self.assertEqual(built.id, inserted.id)
        self.assertEqual((built.gate_configured, built.gate_passed), (1, 1))


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
        self.assertEqual(self.world.arguments("delete_object"), [{"Bucket": BUCKET, "Key": old, "ExpectedBucketOwner": ACCOUNT}])
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

    def test_the_images_of_a_failed_build_go_in_reconcile_and_the_neighbours_stay(self):
        self.pin_current()
        self.add_build("failed-one", "failed", image_id=ami(0xD1))
        self.world.add_image(ami(0xD1), tags=owned("failed-one"), snapshots=(snap(0xD1),))
        self.world.add_snapshot(snap(0xD1), tags=owned("failed-one"))
        self.world.add_image(ami(0xD2), tags=owned("failed-one", deployment="other-deployment"), snapshots=(snap(0xD2),))
        self.world.add_snapshot(snap(0xD2), tags=owned("failed-one", deployment="other-deployment"))
        self.world.add_image(ami(0xD3), tags={"team": "synthetic"}, snapshots=(snap(0xD3),))
        result = self.apply()
        self.assertEqual(result["reconcile"]["failed_images"]["deregistered"], 1)
        self.assertEqual(result["reconcile"]["failed_images"]["snapshots_deleted"], 1)
        # Reconcile took them, so the rule at the end of the pass has nothing left to remove.
        self.assertEqual(result["retention"]["deregistered"], 0)
        self.assertEqual(set(self.world.images), {ami(0xA1), ami(0xD2), ami(0xD3)})
        self.assertEqual(set(self.world.snapshots), {snap(0xD2)})
        (event,) = self.events("image_pruned")
        self.assertEqual((event["deregistered"], event["snapshots_deleted"]), (1, 1))
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="image"), 0.0)

    def test_the_images_of_a_failed_build_are_removed_before_a_new_build_starts(self):
        self.add_build("failed-one", "failed", image_id=ami(0xD1))
        self.world.add_image(ami(0xD1), tags=owned("failed-one"), snapshots=(snap(0xD1),))
        self.world.add_snapshot(snap(0xD1), tags=owned("failed-one"))
        result = self.apply()
        self.assertEqual(result["decision"], "building")
        self.assertEqual(result["reconcile"]["failed_images"]["deregistered"], 1)
        names = self.world.names()
        self.assertLess(names.index("deregister_image"), names.index("create_multipart_upload"))
        self.assertLess(names.index("delete_snapshot"), names.index("run_instances"))

    def test_reconcile_runs_the_failed_build_path_only_and_retention_keeps_the_others(self):
        self.configure(render_toml(image_extra="retain = 2\nretain_min_age_seconds = 3600\n"))
        self.pin_current()
        for number, (name, age) in enumerate((("oldest", 6 * DAY), ("older", 4 * DAY), ("old", 2 * DAY)), start=0xF1):
            self.add_build(name, "candidate", age=age, image_id=ami(number), verified_at=NOW - age + HOUR)
            self.world.add_image(ami(number), tags=owned(name), age=age)
        result = self.apply()
        self.assertEqual(result["reconcile"]["failed_images"]["deregistered"], 0)
        self.assertEqual(result["retention"]["deregistered"], 1)
        self.assertEqual(set(self.world.images), {ami(0xA1), ami(0xF2), ami(0xF3)})

    def test_an_old_image_with_no_row_that_is_not_usable_is_recorded_removed_and_counted_as_leaked(self):
        self.pin_current()
        self.world.add_image(
            ami(0xE1), tags=owned("nobody"), state="failed", age=2 * DAY, snapshots=(snap(0xE1),)
        )
        self.world.add_snapshot(snap(0xE1), tags=owned("nobody"), age=2 * DAY)
        result = self.apply()
        self.assertNotIn(ami(0xE1), self.world.images)
        self.assertEqual(self.world.snapshots, {})
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="image"), 1.0)
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="snapshot"), 1.0)
        self.assertEqual(result["reconcile"]["images_recorded"], 1)
        # It had a row first, so the rule took it as a failed image. It is no build of ours that failed.
        row = self.query(lambda state: state.build("nobody"))
        self.assertEqual((row.state, row.reason_code, row.image_state), ("failed", "IMAGE_ORPHAN_RECORDED", "failed"))
        self.assertIsNone(row.verified_at)
        control = self.control()
        self.assertEqual((control.consecutive_failures, control.cooldown_until, control.blocked), (0, None, False))
        self.assertEqual(self.events("image_build_failed"), [])

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

    def test_an_image_that_is_not_available_and_has_no_row_is_recorded_with_its_state(self):
        self.pin_current()
        self.world.image_final_state = "pending"
        before = self.config_path.read_bytes()
        states = {"orphan-pending": "pending", "orphan-failed": "failed", "orphan-error": "error", "orphan-busy": "transient"}
        for number, (name, state) in enumerate(states.items(), start=0xC2):
            self.world.add_image(ami(number), tags=owned(name), state=state, age=HOUR)
        result = self.apply()
        self.assertEqual(result["reconcile"]["images_recorded"], len(states))
        for number, (name, state) in enumerate(states.items(), start=0xC2):
            row = self.query(lambda current, name=name: current.build(name))
            self.assertEqual((row.state, row.reason_code, row.image_state), ("candidate", "IMAGE_ORPHAN_RECORDED", state))
            self.assertEqual((row.image_id, row.verified_at, row.template_version), (ami(number), None, None))
            self.assertIn(ami(number), self.world.images)
        self.assertIsNone(self.query(lambda state: state.verified_candidate(DEPLOYMENT)))
        self.assertEqual(self.world.arguments("create_launch_template_version"), [])
        self.assertEqual(self.config_path.read_bytes(), before)
        (event,) = self.events("image_reconciled")
        self.assertEqual(event["images_recorded"], len(states))
        # Recorded once, and the hold of a day still keeps all of them.
        self.assertEqual(self.apply()["reconcile"]["images_recorded"], 0)
        self.assertEqual(len(self.builds()), len(states))

    def test_a_recorded_orphan_that_becomes_available_is_noted_and_stays_unverified(self):
        self.pin_current()
        self.world.image_final_state = "pending"
        self.world.add_image(ami(0xC2), tags=owned("orphan-pending"), state="pending", age=HOUR)
        self.assertEqual(self.apply()["reconcile"]["images_recorded"], 1)
        row = self.query(lambda state: state.build("orphan-pending"))
        self.assertEqual((row.state, row.image_state), ("candidate", "pending"))
        self.world.image_final_state = "available"
        self.assertEqual(self.apply()["reconcile"]["images_recorded"], 0)
        row = self.query(lambda state: state.build("orphan-pending"))
        self.assertEqual((row.state, row.image_state, row.verified_at), ("candidate", "available", None))
        self.assertIsNone(self.query(lambda state: state.verified_candidate(DEPLOYMENT)))
        self.assertEqual(len(self.builds()), 1)
        self.assertIn(ami(0xC2), self.world.images)

    def test_a_recorded_orphan_that_turns_failed_is_removed_once_the_orphan_hold_has_passed(self):
        self.pin_current()
        self.world.image_final_state = "pending"
        self.world.add_image(ami(0xC3), tags=owned("orphan-late"), state="pending", age=HOUR, snapshots=(snap(0xC3),))
        self.world.add_snapshot(snap(0xC3), tags=owned("orphan-late"), age=HOUR)
        self.apply()
        self.world.image_final_state = "failed"
        self.clock.advance(2 * HOUR)
        self.apply()
        row = self.query(lambda state: state.build("orphan-late"))
        self.assertEqual((row.state, row.image_state), ("candidate", "failed"))
        self.assertIn(ami(0xC3), self.world.images)
        self.clock.advance(DAY)
        result = self.apply()
        row = self.query(lambda state: state.build("orphan-late"))
        self.assertEqual((row.state, row.reason_code, row.image_state), ("failed", "IMAGE_ORPHAN_RECORDED", "failed"))
        self.assertEqual(result["reconcile"]["failed_images"]["deregistered"], 1)
        self.assertNotIn(ami(0xC3), self.world.images)
        self.assertEqual(self.world.snapshots, {})
        self.assertEqual(self.control().consecutive_failures, 0)

    def test_a_recorded_orphan_that_stays_pending_is_removed_after_the_orphan_hold(self):
        self.pin_current()
        self.world.image_final_state = "pending"
        self.world.add_image(ami(0xC4), tags=owned("orphan-stuck"), state="pending", age=DAY + HOUR)
        result = self.apply()
        self.assertEqual(result["reconcile"]["images_recorded"], 1)
        # A failed build whose image never left pending is the pending path's, which runs at the end of the pass.
        self.assertEqual(result["reconcile"]["failed_images"]["deregistered"], 0)
        self.assertEqual(result["retention"]["deregistered"], 1)
        self.assertNotIn(ami(0xC4), self.world.images)
        row = self.query(lambda state: state.build("orphan-stuck"))
        self.assertEqual((row.state, row.image_state), ("failed", "pending"))
        self.assertEqual(self.metric("ci_burst_image_leaked_resources_total", kind="image"), 1.0)

    def test_a_failed_orphan_row_is_not_a_failed_build_and_does_not_clear_a_block(self):
        self.add_build("earlier", "failed", age=5 * DAY, digest=self.digest())
        self.set_control(consecutive_failures=3, blocked=True)
        tags = owned("nobody", **{BASIS_DIGEST_TAG: "9" * 64})
        self.world.add_image(ami(0xE2), tags=tags, state="failed", age=2 * DAY)
        result = self.apply()
        self.assertEqual((result["decision"], result["reason"]), ("refused", "IMAGE_BLOCKED"))
        self.assertTrue(self.control().blocked)
        self.assertEqual(self.query(lambda state: state.build("nobody")).state, "failed")
        self.assertEqual(self.world.arguments("run_instances"), [])

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

    def test_reconcile_deletes_nothing_when_the_protected_set_cannot_be_resolved_and_the_pass_goes_on(self):
        self.stale_failed_build("failed-one", 0xD1)
        self.add_journal(old_pin=99, new_pin=PIN)
        result = self.apply()
        self.assertEqual(result["reconcile"]["failed_images"], {"refused": "IMAGE_DELETION_UNSAFE"})
        self.assertEqual(result["retention"], {"refused": "IMAGE_DELETION_UNSAFE"})
        self.assertEqual(result["decision"], "building")
        self.assertIn(ami(0xD1), self.world.images)
        self.assertEqual(self.world.arguments("deregister_image"), [])
        self.assertEqual(self.events("image_pruned"), [])

    def test_reconcile_deletes_nothing_when_the_plan_cannot_be_made_and_the_pass_goes_on(self):
        self.stale_failed_build("failed-one", 0xD1)
        self.world.fail("describe_snapshots", ProviderError("RequestLimitExceeded"))
        result = self.apply()
        self.assertEqual(result["reconcile"]["failed_images"], {"refused": "AWS_UNCERTAIN"})
        self.assertEqual(result["retention"], {"refused": "AWS_UNCERTAIN"})
        self.assertEqual(result["decision"], "building")
        self.assertIn(ami(0xD1), self.world.images)
        self.assertEqual(self.world.arguments("deregister_image"), [])

    def test_the_previous_pin_protects_its_image(self):
        self.pin_current()
        self.stale_failed_build("kept", 0xD1)
        self.stale_failed_build("removed", 0xD2)
        self.world.set_version(WORKER, PREVIOUS, {"ImageId": ami(0xD1)})
        self.add_journal(old_pin=PREVIOUS, new_pin=PIN)
        result = self.apply()
        self.assertEqual(result["reconcile"]["failed_images"]["deregistered"], 1)
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
        self.assertEqual(result["reconcile"]["failed_images"]["deregistered"], 1)
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

    # -- a pass that reads a stale file ----------------------------------------------
    # A container whose bind mount of the deployment file keeps the old inode reads the file as it was before
    # ``promote --write`` replaced it. The file of these tests pins version 3 (``PIN``): the pin that a promotion
    # or a rollback replaced. Only the journal names the pin that was written.

    def three_usable_images(self, oldest):
        """Three usable images against ``retain = 2``: retention takes the oldest unless something protects it.

        ``oldest`` is the image number of the first image: it has a ``promoted`` row and the oldest creation time. The
        other two are verified candidates, and the newest of them is protected as the current candidate. No pin names
        any of the three.
        """
        self.configure(render_toml(image_extra="retain = 2\nretain_min_age_seconds = 3600\n"))
        self.pin_current()
        rows = ((oldest, "promoted", 9 * DAY), (0xC1, "candidate", 3 * DAY), (0xD1, "candidate", 2 * DAY))
        for number, state, age in rows:
            name = f"build-{number:x}"
            self.add_build(name, state, age=age, image_id=ami(number), verified_at=NOW - age + HOUR)
            self.world.add_image(ami(number), tags=owned(name), age=age, snapshots=(snap(number),))
            self.world.add_snapshot(snap(number), tags=owned(name), age=age)

    def complete_a_rollback(self, *, old_pin, new_pin):
        def roll_back(state):
            row = state.journal_insert(
                kind="rollback",
                old_pin=old_pin,
                new_pin=new_pin,
                old_file_sha256="0" * 64,
                new_file_sha256="1" * 64,
                backup_name="deployment.toml.bak",
                backup_sha256="2" * 64,
                contained_pin=new_pin,
            )
            state.journal_complete(row.id)

        self.write(roll_back)

    def test_the_image_that_a_promotion_just_wrote_survives_a_pass_that_reads_a_stale_file(self):
        # The file this pass reads pins A (version 3). The journal says that B (version 4) replaced it.
        self.three_usable_images(0xB1)
        self.world.set_version(WORKER, 4, {"ImageId": ami(0xB1)})
        self.add_journal(old_pin=PIN, new_pin=4)
        result = self.apply()
        self.assertEqual(result["retention"]["deregistered"], 1)
        # The oldest unprotected image goes instead of B.
        self.assertEqual(set(self.world.images), {ami(0xA1), ami(0xB1), ami(0xD1)})
        self.assertEqual(self.world.arguments("deregister_image"), [{"ImageId": ami(0xC1)}])

    def test_without_a_journal_row_the_same_pass_takes_the_oldest_image(self):
        # The control: nothing but the journal row separates this pass from the one above.
        self.three_usable_images(0xB1)
        result = self.apply()
        self.assertEqual(result["retention"]["deregistered"], 1)
        self.assertEqual(set(self.world.images), {ami(0xA1), ami(0xC1), ami(0xD1)})

    def a_late_snapshot_inventory(self, call):
        """The ``call``-th snapshot read starts before the pass deadline and returns after it."""
        calls = []

        def late(arguments):
            calls.append(arguments)
            if len(calls) == call:
                self.clock.advance(PASS_DEADLINE_SECONDS)

        self.world.hooks["describe_snapshots"] = late

    def test_a_snapshot_inventory_that_returns_after_the_deadline_deletes_nothing(self):
        # The control above deregisters one image. Here the last read of the retention inputs (the second snapshot
        # read of the pass, after the one of the failed-image path in reconcile) starts before the pass deadline and
        # returns after it, so no plan is made from it and nothing is deleted.
        self.three_usable_images(0xB1)
        self.a_late_snapshot_inventory(2)
        result = self.apply()
        self.assertEqual(result["retention"], {"refused": "pass_deadline_exceeded"})
        self.assertEqual(len(self.world.arguments("describe_snapshots")), 2)
        self.assertEqual(set(self.world.images), {ami(0xA1), ami(0xB1), ami(0xC1), ami(0xD1)})
        self.assertEqual(self.world.arguments("deregister_image"), [])
        self.assertEqual(self.world.arguments("delete_snapshot"), [])

    def test_a_dry_run_whose_snapshot_inventory_returns_after_the_deadline_lists_nothing(self):
        self.three_usable_images(0xB1)
        self.a_late_snapshot_inventory(1)
        self.assertEqual(self.plan()["deletion"], {"refused": "pass_deadline_exceeded"})
        self.assertEqual(len(self.world.arguments("describe_snapshots")), 1)

    def test_the_image_that_a_rollback_just_restored_survives_a_pass_that_reads_a_stale_file(self):
        # The file this pass reads pins version 3. The rollback replaced it with the older version 2.
        self.three_usable_images(0xE1)
        self.world.set_version(WORKER, PREVIOUS, {"ImageId": ami(0xE1)})
        self.complete_a_rollback(old_pin=PIN, new_pin=PREVIOUS)
        result = self.apply()
        self.assertEqual(result["retention"]["deregistered"], 1)
        self.assertEqual(set(self.world.images), {ami(0xA1), ami(0xE1), ami(0xD1)})

    def test_plan_does_not_list_the_promoted_image_for_deletion_when_the_file_is_stale(self):
        self.three_usable_images(0xB1)
        self.world.set_version(WORKER, 4, {"ImageId": ami(0xB1)})
        self.add_journal(old_pin=PIN, new_pin=4)
        rows = self.plan()["deletion"]["items"]
        deregistered = [row["image_id"] for row in rows if row["action"] == "deregister"]
        self.assertEqual(deregistered, [ami(0xC1)])
        self.assertNotIn(ami(0xB1), [row["image_id"] for row in rows])
        self.assertIn(ami(0xB1), self.world.images)

    def test_a_promoted_version_that_the_account_does_not_return_deletes_nothing_and_fails_no_build(self):
        # The pass cannot say which image the last promotion wrote, so the whole rule refuses, as for any pin.
        self.three_usable_images(0xB1)
        self.add_journal(old_pin=PIN, new_pin=4)  # no version 4 exists in the account
        result = self.apply()
        self.assertEqual(result["retention"], {"refused": "IMAGE_DELETION_UNSAFE"})
        self.assertEqual(set(self.world.images), {ami(0xA1), ami(0xB1), ami(0xC1), ami(0xD1)})
        self.assertEqual(self.world.arguments("deregister_image"), [])
        control = self.control()
        self.assertEqual((control.consecutive_failures, control.cooldown_until, control.blocked), (0, None, False))

    # -- a row that was resolved aborted ---------------------------------------------
    # A promotion to B replaced the file and stopped before it completed its row. A pass that reads the old file
    # resolves the row as aborted. The row is then neither pending nor complete, yet the file on disk pins B.

    def abort_the_pending_row(self):
        self.write(lambda state: state.journal_abort(state.pending_journal()[0].id))

    def test_plan_does_not_list_the_image_of_an_aborted_promotion_for_deletion(self):
        self.three_usable_images(0xB1)
        self.world.set_version(WORKER, 4, {"ImageId": ami(0xB1)})
        self.add_journal(old_pin=PIN, new_pin=4, status="pending")
        self.abort_the_pending_row()
        rows = self.plan()["deletion"]["items"]
        deregistered = [row["image_id"] for row in rows if row["action"] == "deregister"]
        self.assertEqual(deregistered, [ami(0xC1)])
        self.assertNotIn(ami(0xB1), [row["image_id"] for row in rows])
        self.assertIn(ami(0xB1), self.world.images)

    def test_a_later_failed_promotion_does_not_expose_the_image_of_an_earlier_aborted_one(self):
        # A promotion from 3 to 4 renamed the file and stopped; a pass reading the old file resolved it aborted. A
        # later promotion from 3 to 5 through the same stale file committed its row, failed, and was resolved aborted
        # too. The file on disk still pins 4, and only the older aborted row names it.
        self.three_usable_images(0xB1)
        self.world.set_version(WORKER, 4, {"ImageId": ami(0xB1)})
        self.world.set_version(WORKER, 5, {"ImageId": ami(0xD1)})
        self.add_journal(old_pin=PIN, new_pin=4, status="pending")
        self.abort_the_pending_row()
        self.add_journal(old_pin=PIN, new_pin=5, status="pending")
        self.abort_the_pending_row()
        rows = self.plan()["deletion"]["items"]
        self.assertEqual([row["image_id"] for row in rows if row["action"] == "deregister"], [ami(0xC1)])
        self.assertNotIn(ami(0xB1), [row["image_id"] for row in rows])

    def test_a_row_that_completes_settles_the_aborted_rows_before_it(self):
        # After a completed row the file pins what that row wrote, so older aborted rows protect nothing more.
        self.three_usable_images(0xB1)
        self.world.set_version(WORKER, 4, {"ImageId": ami(0xB1)})
        self.add_journal(old_pin=PIN, new_pin=4, status="pending")
        self.abort_the_pending_row()
        # A later promotion from version 2 to the pinned version 3 completed. Version 2 names a protected candidate.
        self.world.set_version(WORKER, PREVIOUS, {"ImageId": ami(0xD1)})
        self.add_journal(old_pin=PREVIOUS, new_pin=PIN)
        rows = self.plan()["deletion"]["items"]
        self.assertEqual([row["image_id"] for row in rows if row["action"] == "deregister"], [ami(0xB1)])

    def test_without_a_row_the_plan_lists_the_oldest_image_for_deletion(self):
        # The control: nothing but the aborted row separates this plan from the one above.
        self.three_usable_images(0xB1)
        rows = self.plan()["deletion"]["items"]
        self.assertEqual([row["image_id"] for row in rows if row["action"] == "deregister"], [ami(0xB1)])

    def test_both_pins_of_an_aborted_row_are_protected_and_the_previous_pin_stays_that_of_the_completed_row(self):
        self.pin_current()
        for number in (0xD1, 0xD2, 0xD3, 0xD4):
            self.stale_failed_build(f"failed-{number:x}", number)
        self.world.set_version(WORKER, PREVIOUS, {"ImageId": ami(0xD1)})
        self.world.set_version(WORKER, 4, {"ImageId": ami(0xD2)})
        self.world.set_version(WORKER, 5, {"ImageId": ami(0xD3)})
        # A completed promotion from version 2 to version 3 (the file pins 3), then an aborted one from 4 to 5.
        self.add_journal(old_pin=PREVIOUS, new_pin=PIN)
        self.add_journal(old_pin=4, new_pin=5, status="pending")
        self.abort_the_pending_row()
        result = self.apply()
        self.assertEqual(result["reconcile"]["failed_images"]["deregistered"], 1)
        # Versions 2 (the previous pin of the completed row), 4 and 5 (the pins of the newest row) keep their images.
        self.assertEqual(set(self.world.images), {ami(0xA1), ami(0xD1), ami(0xD2), ami(0xD3)})


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

    def test_a_pass_on_a_state_the_caller_opened_ends_with_no_connection_and_no_lock(self):
        # A writable connection kept without the lock between passes could roll back a journal that another
        # writer left in between, so the pass closes both (#194).
        self.pin_current()
        self.state.acquire_writer()
        self.state.connect()
        self.apply()
        self.assertFalse(self.state.available)
        other = ImageState(self.state_dir, clock=self.clock)
        self.addCleanup(other.close)
        other.acquire_writer()  # refuses IMAGE_STATE_LOCKED if the pass had kept the lock

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
        text = (self.state_dir / image_pipeline.METRICS_FILE).read_text(encoding="utf-8")
        self.assertIn(
            "# HELP ci_burst_image_console_latency_seconds"
            " Seconds from the build start to the latest sighting of the marker on the console.\n",
            text,
        )
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

    def test_the_first_metrics_of_a_new_deployment_count_zero_builds_and_have_no_duration_or_timestamp_series(self):
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

    def test_the_event_whitelist_refuses_a_url_that_has_no_query_string(self):
        # Every character of these is one an id or a code may hold: only the scheme separator gives them away.
        telemetry = image_pipeline._Telemetry(self.state_dir, DEPLOYMENT, self.clock)
        urls = (
            "https://example.invalid/path",
            f"https://{BUCKET}.s3.example.invalid/{PREFIX}build-0001.tar.gz",
            f"s3://{BUCKET}/{PREFIX}build-0001.tar.gz",
            "build-0001 https://example.invalid",
            "a://b",
            "://",
        )
        for value in urls:
            for field in ("decision", "reason", "build_id", "digest", "source_sha"):
                with self.assertRaises(ValueError, msg=f"{field}={value!r}"):
                    telemetry.event("image_check", **{field: value})
            with self.assertRaises(ValueError, msg=repr(value)):
                telemetry.event("image_build_failed", build_id="b-1", reason=value)
        self.assertFalse((self.state_dir / image_pipeline.EVENTS_FILE).exists())

    def test_the_event_whitelist_still_takes_a_value_with_a_colon_or_a_slash_that_is_no_url(self):
        telemetry = image_pipeline._Telemetry(self.state_dir, DEPLOYMENT, self.clock)
        telemetry.event("image_check", decision="start", reason="AWS:UNCERTAIN/x", build_id=f"{PREFIX}b-1")
        (event,) = self.events("image_check")
        self.assertEqual((event["reason"], event["build_id"]), ("AWS:UNCERTAIN/x", f"{PREFIX}b-1"))

    def test_the_failed_build_event_refuses_a_tail_that_still_holds_a_url(self):
        telemetry = image_pipeline._Telemetry(self.state_dir, DEPLOYMENT, self.clock)
        for tail in ("  | see https://example.invalid/path", "  | s3://example-bucket/key", "  | ://"):
            with self.assertRaises(ValueError, msg=tail):
                telemetry.event("image_build_failed", build_id="b-1", reason="WARM_FAILED", framed_tail=tail)
        self.assertFalse((self.state_dir / image_pipeline.EVENTS_FILE).exists())


class AppendLineTests(unittest.TestCase):
    """The event file takes a line with one write on an append descriptor, and a write that falls short is an error."""

    LINE = '{"event": "image_check"}\n'

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.path = self.dir / image_pipeline.EVENTS_FILE

    @contextmanager
    def spied(self, *, short=None):
        """Record the flags of every ``os.open`` and the data of every ``os.write``; ``short`` caps what a write takes.

        A second write ends the test with an error instead of looping, so a retry is a failure and never a hang.
        """
        record = {"flags": [], "writes": []}
        real_open, real_write = os.open, os.write

        def spy_open(path, flags, *arguments):
            record["flags"].append(flags)
            return real_open(path, flags, *arguments)

        def spy_write(descriptor, data):
            record["writes"].append(bytes(data))
            if len(record["writes"]) > 1:
                raise AssertionError("the line was written in more than one write")
            return real_write(descriptor, bytes(data) if short is None else bytes(data)[:short])

        with mock.patch.object(os, "open", spy_open), mock.patch.object(os, "write", spy_write):
            yield record

    def test_a_line_is_written_whole_with_one_write_on_an_append_descriptor(self):
        with self.spied() as record:
            image_pipeline._append_line(self.path, self.LINE)
        self.assertEqual(record["writes"], [self.LINE.encode("utf-8")])
        self.assertTrue(record["flags"][0] & os.O_APPEND)
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.LINE)

    def test_the_next_line_goes_behind_the_first(self):
        image_pipeline._append_line(self.path, self.LINE)
        image_pipeline._append_line(self.path, "second line\n")
        self.assertEqual(self.path.read_text(encoding="utf-8"), self.LINE + "second line\n")

    def test_a_line_with_characters_beyond_ascii_is_written_whole_in_bytes(self):
        line = '{"note": "\u017c\u00f3\u0142w"}\n'
        with self.spied() as record:
            image_pipeline._append_line(self.path, line)
        self.assertEqual(record["writes"], [line.encode("utf-8")])
        self.assertEqual(self.path.read_bytes(), line.encode("utf-8"))

    def test_a_write_that_falls_short_is_an_error_and_is_not_retried(self):
        for short in (5, 0):
            with self.subTest(short=short):
                with self.spied(short=short) as record:
                    with self.assertRaises(OSError):
                        image_pipeline._append_line(self.path, self.LINE)
                self.assertEqual(record["writes"], [self.LINE.encode("utf-8")])

    def test_a_short_write_reaches_the_caller_of_the_event_writer(self):
        telemetry = image_pipeline._Telemetry(self.dir, DEPLOYMENT, Clock(NOW))
        with self.spied(short=5):
            with self.assertRaises(OSError):
                telemetry.event("image_check", decision="start")

    def test_the_descriptor_is_closed_after_a_short_write(self):
        closed = []
        real_close = os.close

        def spy_close(descriptor):
            closed.append(descriptor)
            return real_close(descriptor)

        with self.spied(short=5), mock.patch.object(os, "close", spy_close):
            with self.assertRaises(OSError):
                image_pipeline._append_line(self.path, self.LINE)
        self.assertEqual(len(closed), 1)


class ArchiveMaskTests(unittest.TestCase):
    """The mask of the tail, on its own: what it leaves behind has no URL separator, whatever the input."""

    KEY = f"{PREFIX}build-0001.tar.gz"

    def mask(self, tail, key=KEY):
        return image_pipeline._mask_archive(tail, key)

    def test_the_result_never_holds_a_url_separator(self):
        awkward = (
            "://",
            "  | ://x",
            "  | (https://example.invalid/a)",
            "  | a=b://c",
            "  | x:///y",
            "  | https://",
            "  | HTTPS://example.invalid/UPPER?Q=1",
            "  | one https://a.invalid two s3://b/c three ftp://d",
            "  | git+ssh://git@example.invalid/x.git",
            "  | " + "a" * 4000 + "://x",
            "  | " + "https://example.invalid/" + "p" * 4000,
        )
        for tail in awkward:
            masked = self.mask(tail)
            self.assertNotIn("://", masked, tail[:80])
            self.assertEqual(self.mask(masked), masked, "masking again changes nothing")

    def test_a_url_goes_from_its_scheme_to_the_next_space_and_the_rest_of_the_line_stays(self):
        self.assertEqual(
            self.mask("  | WARM_SRC_URL=https://example.invalid/a?b=c&d=e after"), "  | WARM_SRC_URL=[archive-url] after"
        )
        self.assertEqual(self.mask("  | fetch s3://example-bucket/key done"), "  | fetch [archive-url] done")
        self.assertEqual(self.mask("  | a https://x.invalid b https://y.invalid c"), "  | a [archive-url] b [archive-url] c")

    def test_the_lines_stay_lines(self):
        masked = self.mask("  | one https://x.invalid\n  | two\n  | three ?X-Amz-Signature=abc\n  | four")
        self.assertEqual(masked, "  | one [archive-url]\n  | two\n  | three [archive-url]\n  | four")

    def test_the_presign_parameters_of_a_query_string_that_stands_alone_are_masked(self):
        for text in ("?X-Amz-Signature=abc", "&X-Amz-Date=20260101T000000Z", "x-amz-security-token=abc", "X-AMZ-Expires=1800"):
            self.assertEqual(self.mask("  | " + text + " end"), "  | [archive-url] end", text)
        # Other parameters are not the archive's.
        self.assertEqual(self.mask("  | retries=3 mode=fast"), "  | retries=3 mode=fast")

    def test_the_object_key_is_masked_wherever_it_stands(self):
        key = self.KEY
        self.assertEqual(self.mask(f"  | NoSuchKey {key}"), "  | NoSuchKey [archive-url]")
        self.assertEqual(self.mask(f"  | {key} {key}"), "  | [archive-url] [archive-url]")
        self.assertEqual(self.mask("  | NoSuchKey other/key.tar.gz"), "  | NoSuchKey other/key.tar.gz")

    def test_a_build_with_no_key_masks_no_key(self):
        for key in (None, ""):
            self.assertEqual(self.mask("  | a b", key), "  | a b")
            self.assertEqual(self.mask("", key), "")


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
