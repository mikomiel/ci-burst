"""The image-build identity of the AWS adapter, against fake clients.

The fakes take explicit keyword arguments instead of ``**kwargs``, so a request
with an unexpected field fails the test. Their parameter names follow the EC2
and S3 APIs from the design and were not checked against an installed SDK,
which this checkout does not have.

Sentinel values stand in for the presigned URL, a token and builder user data.
They are synthetic and carry no key-like or signature-like shape.
"""

import contextlib
import copy
import inspect
import io
import logging
import sys
import tempfile
import traceback
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ci_burst import aws as aws_module
from ci_burst.aws import (
    BASIS_DIGEST_TAG,
    BUILD_ID_TAG,
    DEPLOYMENT_TAG,
    EXPIRES_TAG,
    IMAGE_BUILD_TAG,
    MANAGED_TAG,
    Aws,
    ImageAws,
)
from ci_burst.config import load
from ci_burst.errors import DeadlineRefusal, ProviderRefusal, Refusal

from support import Clock, write_config
from test_aws import sdk_client_error

DEPLOYMENT = "test-deployment"
REGION = "eu-central-1"
PROFILE = "example-image-profile"
BUCKET = "example-ci-burst-staging"
PREFIX = "warm-archives/"
NOW = 1_800_000_000.0

SENTINEL_URL = "https://example-bucket.example.org/sentinel-presigned-url-marker"
SENTINEL_TOKEN = "sentinel-forge-token-marker"
SENTINEL_USER_DATA = "#!/bin/bash\necho sentinel-user-data-marker"
SENTINELS = (SENTINEL_URL, SENTINEL_TOKEN, SENTINEL_USER_DATA, "sentinel-user-data-marker")
LEAKY_MESSAGE = f"request {SENTINEL_URL} header {SENTINEL_TOKEN} body {SENTINEL_USER_DATA}"


class ProviderError(Exception):
    """A provider error shaped like an SDK client error, carrying a sentinel message."""

    def __init__(self, code, message=LEAKY_MESSAGE):
        super().__init__(f"An error occurred ({code}): {message}")
        self.response = {"Error": {"Code": code, "Message": message}}


def tag_list(tags):
    return [{"Key": key, "Value": value} for key, value in tags.items()]


class FakeEc2:
    """An EC2 stand-in that really applies tag and state filters."""

    def __init__(self, calls):
        self.calls = calls
        self.instances_store = []
        self.images_store = []
        self.snapshots_store = []
        self.templates_store = []
        self.versions_store = {}
        self.consoles = {}
        self.errors = {}
        self.pages = {}
        self.next_instance = 1
        self.next_version = 5

    def _enter(self, name, kwargs):
        self.calls.append((name, copy.deepcopy(kwargs)))
        if name in self.errors:
            raise self.errors[name]

    def add_instance(self, instance_id, tags, state="running"):
        self.instances_store.append({"InstanceId": instance_id, "State": {"Name": state}, "Tags": tag_list(tags)})

    @staticmethod
    def _matches(item, filters):
        tags = {tag["Key"]: tag["Value"] for tag in item.get("Tags", [])}
        for entry in filters or []:
            name, values = entry["Name"], entry["Values"]
            if name.startswith("tag:"):
                if tags.get(name[4:]) not in values:
                    return False
            elif name == "instance-state-name":
                if item["State"]["Name"] not in values:
                    return False
            else:
                raise AssertionError(f"unexpected filter {name}")
        return True

    def run_instances(self, *, LaunchTemplate, MinCount, MaxCount, ClientToken, UserData, TagSpecifications):
        self._enter(
            "run_instances",
            dict(
                LaunchTemplate=LaunchTemplate,
                MinCount=MinCount,
                MaxCount=MaxCount,
                ClientToken=ClientToken,
                UserData=UserData,
                TagSpecifications=TagSpecifications,
            ),
        )
        instance_id = f"i-{self.next_instance:017x}"
        self.next_instance += 1
        tags = {}
        for spec in TagSpecifications:
            if spec["ResourceType"] == "instance":
                tags = {tag["Key"]: tag["Value"] for tag in spec["Tags"]}
        self.add_instance(instance_id, tags, state="pending")
        return {"Instances": [{"InstanceId": instance_id}]}

    def describe_instances(self, *, Filters=None, InstanceIds=None, NextToken=None):
        self._enter("describe_instances", dict(Filters=Filters, InstanceIds=InstanceIds, NextToken=NextToken))
        found = [item for item in self.instances_store if self._matches(item, Filters)]
        if InstanceIds is not None:
            found = [item for item in found if item["InstanceId"] in InstanceIds]
        response = {"Reservations": [{"Instances": [item]} for item in found]}
        if "describe_instances" in self.pages:
            response["NextToken"] = "next-page"
        return response

    def get_console_output(self, *, InstanceId, Latest):
        self._enter("get_console_output", dict(InstanceId=InstanceId, Latest=Latest))
        return {"Output": self.consoles.get(InstanceId, "")}

    def terminate_instances(self, *, InstanceIds):
        self._enter("terminate_instances", dict(InstanceIds=InstanceIds))
        return {"TerminatingInstances": [{"InstanceId": item} for item in InstanceIds]}

    def create_image(self, *, InstanceId, Name, NoReboot, TagSpecifications):
        self._enter("create_image", dict(InstanceId=InstanceId, Name=Name, NoReboot=NoReboot, TagSpecifications=TagSpecifications))
        return {"ImageId": "ami-0123456789abcdef0"}

    def describe_images(self, *, Owners=None, Filters=None, ImageIds=None, NextToken=None):
        self._enter("describe_images", dict(Owners=Owners, Filters=Filters, ImageIds=ImageIds, NextToken=NextToken))
        found = [item for item in self.images_store if self._matches_any(item, Filters)]
        if ImageIds is not None:
            found = [item for item in self.images_store if item["ImageId"] in ImageIds]
            if not found:
                raise ProviderError("InvalidAMIID.NotFound")
        response = {"Images": found}
        if "describe_images" in self.pages:
            response["NextToken"] = "next-page"
        return response

    def describe_snapshots(self, *, OwnerIds=None, Filters=None, SnapshotIds=None, NextToken=None):
        self._enter("describe_snapshots", dict(OwnerIds=OwnerIds, Filters=Filters, SnapshotIds=SnapshotIds, NextToken=NextToken))
        found = [item for item in self.snapshots_store if self._matches_any(item, Filters)]
        if SnapshotIds is not None:
            found = [item for item in self.snapshots_store if item["SnapshotId"] in SnapshotIds]
            if not found:
                raise ProviderError("InvalidSnapshot.NotFound")
        return {"Snapshots": found}

    def _matches_any(self, item, filters):
        tags = {tag["Key"]: tag["Value"] for tag in item.get("Tags", [])}
        return all(tags.get(entry["Name"][4:]) in entry["Values"] for entry in filters or [])

    def deregister_image(self, *, ImageId):
        self._enter("deregister_image", dict(ImageId=ImageId))
        return {}

    def delete_snapshot(self, *, SnapshotId):
        self._enter("delete_snapshot", dict(SnapshotId=SnapshotId))
        return {}

    def describe_launch_templates(self, *, LaunchTemplateNames, NextToken=None):
        self._enter("describe_launch_templates", dict(LaunchTemplateNames=LaunchTemplateNames, NextToken=NextToken))
        known = {item["LaunchTemplateName"] for item in self.templates_store}
        # Like EC2: one unknown name fails the whole request.
        if any(name not in known for name in LaunchTemplateNames):
            raise ProviderError("InvalidLaunchTemplateName.NotFoundException")
        found = [item for item in self.templates_store if item["LaunchTemplateName"] in LaunchTemplateNames]
        return {"LaunchTemplates": found}

    def describe_launch_template_versions(self, *, LaunchTemplateName, Versions, NextToken=None):
        self._enter(
            "describe_launch_template_versions",
            dict(LaunchTemplateName=LaunchTemplateName, Versions=Versions, NextToken=NextToken),
        )
        found = [
            item
            for (name, number), item in self.versions_store.items()
            if name == LaunchTemplateName and str(number) in Versions
        ]
        return {"LaunchTemplateVersions": copy.deepcopy(found)}

    def create_launch_template_version(
        self, *, LaunchTemplateName, SourceVersion, LaunchTemplateData, VersionDescription, ClientToken
    ):
        self._enter(
            "create_launch_template_version",
            dict(
                LaunchTemplateName=LaunchTemplateName,
                SourceVersion=SourceVersion,
                LaunchTemplateData=LaunchTemplateData,
                VersionDescription=VersionDescription,
                ClientToken=ClientToken,
            ),
        )
        number = self.next_version
        self.next_version += 1
        return {"LaunchTemplateVersion": {"VersionNumber": number}}


class FakeS3:
    def __init__(self, calls):
        self.calls = calls
        self.uploaded = []
        self.errors = {}
        self.fail_part = None
        self.pages = []
        self.presigned = SENTINEL_URL

    def _enter(self, name, kwargs):
        self.calls.append((name, {key: value for key, value in kwargs.items() if key != "Body"}))
        if name in self.errors:
            raise self.errors[name]

    def create_multipart_upload(self, *, Bucket, Key):
        self._enter("create_multipart_upload", dict(Bucket=Bucket, Key=Key))
        return {"UploadId": "upload-0001"}

    def upload_part(self, *, Bucket, Key, UploadId, PartNumber, Body):
        self._enter("upload_part", dict(Bucket=Bucket, Key=Key, UploadId=UploadId, PartNumber=PartNumber, Body=Body))
        if self.fail_part == PartNumber:
            raise ProviderError("SlowDown")
        self.uploaded.append(bytes(Body))
        return {"ETag": f"etag-{PartNumber}"}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload):
        self._enter("complete_multipart_upload", dict(Bucket=Bucket, Key=Key, UploadId=UploadId, MultipartUpload=MultipartUpload))
        return {}

    def abort_multipart_upload(self, *, Bucket, Key, UploadId):
        self._enter("abort_multipart_upload", dict(Bucket=Bucket, Key=Key, UploadId=UploadId))
        return {}

    def delete_object(self, *, Bucket, Key):
        self._enter("delete_object", dict(Bucket=Bucket, Key=Key))
        return {}

    def list_objects_v2(self, *, Bucket, Prefix, ContinuationToken=None):
        self._enter("list_objects_v2", dict(Bucket=Bucket, Prefix=Prefix, ContinuationToken=ContinuationToken))
        index = int(ContinuationToken or 0)
        return self.pages[index]

    def generate_presigned_url(self, ClientMethod, *, Params, ExpiresIn):
        self._enter("generate_presigned_url", dict(ClientMethod=ClientMethod, Params=Params, ExpiresIn=ExpiresIn))
        return self.presigned


class FakeCredentials:
    """Lazy credentials: no expiry until they are resolved, like an assumed role."""

    def __init__(self, expiry):
        self._pending = expiry
        self._expiry_time = None
        self.resolved = 0

    def get_frozen_credentials(self):
        self.resolved += 1
        self._expiry_time = self._pending
        return object()


class FakeSession:
    def __init__(self, calls, ec2, s3, credentials=None):
        self.calls = calls
        self.ec2 = ec2
        self.s3 = s3
        self.credentials = credentials
        self.services = []

    def client(self, service, region_name=None, config=None):
        self.services.append(service)
        self.calls.append(("session.client", {"service": service, "region": region_name}))
        return {"ec2": self.ec2, "s3": self.s3}[service]

    def get_credentials(self):
        return self.credentials


class Base(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.ec2 = FakeEc2(self.calls)
        self.s3 = FakeS3(self.calls)
        self.clock = Clock(NOW)

    def image_aws(self, *, expiry=None, **overrides):
        values = dict(
            clients={"ec2": self.ec2, "s3": self.s3},
            clock=self.clock,
            expiry=expiry if expiry is not None else NOW + 3600,
        )
        values.update(overrides)
        return ImageAws(REGION, PROFILE, **values)

    def names(self):
        return [name for name, _ in self.calls]

    def requests(self, name):
        return [arguments for call, arguments in self.calls if call == name]

    def builder(self, aws=None, **overrides):
        values = dict(
            template_name="example-ci-burst-image-builder",
            template_version=2,
            deployment=DEPLOYMENT,
            build_id="build-0001",
            expires_at=NOW + 2700,
            user_data="#!/bin/bash\necho stub",
            client_token="token-0001",
        )
        values.update(overrides)
        return (aws or self.image_aws()).run_builder(**values)


class SessionTests(Base):
    def test_an_empty_or_missing_profile_is_refused_before_any_session(self):
        factory = mock.Mock()
        for profile in (None, ""):
            with self.subTest(profile=profile):
                with self.assertRaises(ProviderRefusal) as raised:
                    ImageAws(REGION, profile, session_factory=factory)
                self.assertEqual(raised.exception.reason, "IMAGE_PROFILE_MISSING")
        factory.assert_not_called()

    def test_the_session_is_built_from_the_named_profile_and_not_cached_across_instances(self):
        profiles = []

        def factory(profile):
            profiles.append(profile)
            return FakeSession(self.calls, self.ec2, self.s3)

        with mock.patch.object(aws_module, "_sdk_config", return_value=object()):
            first = ImageAws(REGION, PROFILE, session_factory=factory)
            first._client("ec2")
            first._client("s3")
            second = ImageAws(REGION, PROFILE, session_factory=factory)
            second._client("ec2")
        self.assertEqual(profiles, [PROFILE, PROFILE])

    def test_clients_use_the_region_and_only_ec2_and_s3_exist(self):
        session = FakeSession(self.calls, self.ec2, self.s3)
        with mock.patch.object(aws_module, "_sdk_config", return_value=object()) as config:
            aws = ImageAws(REGION, PROFILE, session_factory=lambda profile: session)
            aws._client("ec2")
            aws._client("s3")
            with self.assertRaises(ValueError):
                aws._client("iam")
            with self.assertRaises(ValueError):
                aws._client("sts")
        self.assertEqual(session.services, ["ec2", "s3"])
        self.assertEqual({arguments["region"] for arguments in self.requests("session.client")}, {REGION})
        # The bucket client signs with SigV4 and both clients are bounded in time.
        self.assertEqual(config.call_args_list[1].kwargs["signature_version"], "s3v4")
        for call in config.call_args_list:
            self.assertEqual(call.kwargs["connect_timeout"], aws_module.CONNECT_TIMEOUT_SECONDS)
            self.assertEqual(call.kwargs["read_timeout"], aws_module.READ_TIMEOUT_SECONDS)

    def test_a_missing_sdk_is_the_documented_refusal(self):
        with mock.patch.dict(sys.modules, {"boto3": None}):
            with self.assertRaises(ProviderRefusal) as raised:
                ImageAws(REGION, PROFILE)._session()
        self.assertEqual(raised.exception.reason, "AWS_SDK_UNAVAILABLE")

    def test_no_iam_client_is_created_by_any_operation(self):
        session = FakeSession(self.calls, self.ec2, self.s3)
        self.ec2.templates_store.append(
            {"LaunchTemplateName": "example-worker", "LaunchTemplateId": "lt-0123456789abcdef0"}
        )
        self.ec2.versions_store[("example-worker", 3)] = {
            "VersionNumber": 3,
            "LaunchTemplateId": "lt-0123456789abcdef0",
            "LaunchTemplateData": {"ImageId": "ami-0123456789abcdef0"},
        }
        self.s3.pages = [{"Contents": []}]
        with mock.patch.object(aws_module, "_sdk_config", return_value=object()):
            aws = ImageAws(
                REGION, PROFILE, session_factory=lambda profile: session, clock=self.clock, expiry=NOW + 3600
            )
            instance_id = aws.run_builder(
                template_name="example-ci-burst-image-builder",
                template_version=2,
                deployment=DEPLOYMENT,
                build_id="build-0001",
                expires_at=NOW + 2700,
                user_data="stub",
                client_token="token-0001",
            )
            aws.builders(DEPLOYMENT)
            aws.instances([instance_id])
            aws.console_output([instance_id])
            aws.create_image(instance_id, "example-warm", deployment=DEPLOYMENT, build_id="build-0001", basis_digest="0" * 64)
            aws.images(DEPLOYMENT)
            aws.snapshots(DEPLOYMENT)
            aws.launch_templates(["example-worker"])
            aws.template_version("example-worker", 3)
            aws.create_template_version("example-worker", 3, "ami-0123456789abcdef0", "build-0001", "token-0002")
            aws.put_archive(BUCKET, PREFIX + "build-0001.tar.gz", [b"data"], max_bytes=100)
            aws.list_objects(BUCKET, PREFIX)
            aws.presign_get(BUCKET, PREFIX + "build-0001.tar.gz", 1800)
            aws.delete_object(BUCKET, PREFIX + "build-0001.tar.gz")
            aws.deregister_image("ami-0123456789abcdef0")
            aws.delete_snapshot("snap-0123456789abcdef0")
            aws.terminate([instance_id])
        self.assertEqual(set(session.services), {"ec2", "s3"})

    def test_the_module_creates_no_iam_client(self):
        source = Path(aws_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"iam"', source.replace('IMAGE_SERVICES = frozenset({"ec2", "s3"})', ""))
        self.assertNotIn("'iam'", source)


class BuilderTests(Base):
    def test_the_builder_request_has_the_image_tags_and_never_the_managed_tag(self):
        self.builder()
        (request,) = self.requests("run_instances")
        self.assertEqual(
            {spec["ResourceType"] for spec in request["TagSpecifications"]}, set(aws_module.BUILDER_TAGGED_RESOURCES)
        )
        for spec in request["TagSpecifications"]:
            tags = {tag["Key"]: tag["Value"] for tag in spec["Tags"]}
            self.assertNotIn(MANAGED_TAG, tags)
            self.assertEqual(
                tags,
                {
                    IMAGE_BUILD_TAG: "true",
                    DEPLOYMENT_TAG: DEPLOYMENT,
                    BUILD_ID_TAG: "build-0001",
                    EXPIRES_TAG: str(int(NOW + 2700)),
                },
            )

    def test_the_builder_is_one_on_demand_instance_from_a_numeric_template_version(self):
        self.builder(template_version=7)
        (request,) = self.requests("run_instances")
        self.assertEqual(request["LaunchTemplate"], {"LaunchTemplateName": "example-ci-burst-image-builder", "Version": "7"})
        self.assertEqual((request["MinCount"], request["MaxCount"]), (1, 1))
        self.assertEqual(request["ClientToken"], "token-0001")
        self.assertEqual(
            set(request),
            {"LaunchTemplate", "MinCount", "MaxCount", "ClientToken", "UserData", "TagSpecifications"},
        )

    def test_a_template_version_must_be_a_positive_number(self):
        for version in ("$Latest", "$Default", "3", 0, -1, True, None, 1.5):
            with self.subTest(version=version):
                with self.assertRaises(ValueError):
                    self.builder(template_version=version)
        self.assertEqual(self.requests("run_instances"), [])

    def test_the_launch_call_has_no_field_for_extra_tags_or_a_profile(self):
        parameters = set(inspect.signature(ImageAws.run_builder).parameters)
        self.assertEqual(
            parameters,
            {"self", "template_name", "template_version", "deployment", "build_id", "expires_at", "user_data", "client_token"},
        )

    def test_a_launch_response_must_identify_exactly_one_instance(self):
        self.ec2.run_instances = lambda **kwargs: {"Instances": [{"InstanceId": "i-1"}, {"InstanceId": "i-2"}]}
        with self.assertRaises(ProviderRefusal) as raised:
            self.builder()
        self.assertTrue(raised.exception.uncertain)

    def test_owned_instances_never_returns_a_builder(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        config = load(write_config(Path(directory.name)))
        self.ec2.add_instance(
            "i-0123456789abcdef0",
            {MANAGED_TAG: "true", DEPLOYMENT_TAG: DEPLOYMENT, "ci-burst:operation": "op"},
        )
        builder_id = self.builder()
        # The control: an instance that does carry the managed tag is returned,
        # so the filter in this fake can fail the assertion below.
        owned = Aws(config, client=self.ec2, clock=self.clock).owned_instances(DEPLOYMENT)
        self.assertEqual([item.instance_id for item in owned], ["i-0123456789abcdef0"])
        self.assertNotIn(builder_id, [item.instance_id for item in owned])
        self.ec2.add_instance(
            "i-0123456789abcdef1", {MANAGED_TAG: "true", DEPLOYMENT_TAG: DEPLOYMENT, "ci-burst:operation": "op"}
        )
        again = Aws(config, client=self.ec2, clock=self.clock).owned_instances(DEPLOYMENT)
        self.assertEqual(len(again), 2)

    def test_the_builder_precheck_finds_live_builders_of_this_deployment_only(self):
        live = self.builder()
        self.ec2.add_instance("i-0123456789abcdef1", {IMAGE_BUILD_TAG: "true", DEPLOYMENT_TAG: DEPLOYMENT}, state="terminated")
        self.ec2.add_instance("i-0123456789abcdef2", {IMAGE_BUILD_TAG: "true", DEPLOYMENT_TAG: "other"}, state="running")
        self.ec2.add_instance("i-0123456789abcdef3", {MANAGED_TAG: "true", DEPLOYMENT_TAG: DEPLOYMENT}, state="running")
        found = self.image_aws().builders(DEPLOYMENT)
        self.assertEqual([item.instance_id for item in found], [live])
        (request,) = self.requests("describe_instances")
        states = [entry for entry in request["Filters"] if entry["Name"] == "instance-state-name"][0]["Values"]
        self.assertEqual(states, ["pending", "running", "stopping", "stopped"])

    def test_builder_listing_refuses_a_truncated_inventory(self):
        self.ec2.pages["describe_instances"] = True
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().builders(DEPLOYMENT)
        self.assertEqual(raised.exception.reason, "AWS_INVENTORY_INCOMPLETE")
        self.assertEqual(len(self.requests("describe_instances")), aws_module.MAX_INVENTORY_PAGES)

    def test_builder_listing_stops_at_the_deadline(self):
        with self.assertRaises(DeadlineRefusal):
            self.image_aws().builders(DEPLOYMENT, deadline=NOW)
        self.assertEqual(self.calls, [])

    def test_instance_reads_console_and_terminate_use_the_image_session(self):
        instance_id = self.builder()
        self.ec2.consoles[instance_id] = "WARM_SOURCE_SHA256 " + "0" * 64 + "\nIMAGE_READY\n"
        aws = self.image_aws()
        self.assertEqual([item.instance_id for item in aws.instances([instance_id])], [instance_id])
        self.assertIn("IMAGE_READY", aws.console_output([instance_id])[instance_id])
        aws.terminate([instance_id])
        self.assertEqual(self.requests("terminate_instances"), [{"InstanceIds": [instance_id]}])


class ImageAndTemplateTests(Base):
    def test_create_image_never_reboots_and_tags_image_and_snapshots(self):
        image_id = self.image_aws().create_image(
            "i-0123456789abcdef0",
            "example-warm-0001",
            deployment=DEPLOYMENT,
            build_id="build-0001",
            basis_digest="a" * 64,
            extra_tags={"ci-burst:source-sha": "b" * 40},
        )
        self.assertEqual(image_id, "ami-0123456789abcdef0")
        (request,) = self.requests("create_image")
        self.assertIs(request["NoReboot"], True)
        self.assertEqual([spec["ResourceType"] for spec in request["TagSpecifications"]], ["image", "snapshot"])
        for spec in request["TagSpecifications"]:
            tags = {tag["Key"]: tag["Value"] for tag in spec["Tags"]}
            self.assertEqual(
                tags,
                {
                    IMAGE_BUILD_TAG: "true",
                    DEPLOYMENT_TAG: DEPLOYMENT,
                    BUILD_ID_TAG: "build-0001",
                    BASIS_DIGEST_TAG: "a" * 64,
                    "ci-burst:source-sha": "b" * 40,
                },
            )

    def test_extra_image_tags_cannot_replace_a_reserved_tag_or_add_the_managed_tag(self):
        for key in (MANAGED_TAG, IMAGE_BUILD_TAG, DEPLOYMENT_TAG, BUILD_ID_TAG, BASIS_DIGEST_TAG):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self.image_aws().create_image(
                        "i-0123456789abcdef0",
                        "example-warm",
                        deployment=DEPLOYMENT,
                        build_id="build-0001",
                        basis_digest="a" * 64,
                        extra_tags={key: "x"},
                    )
        self.assertEqual(self.requests("create_image"), [])

    def test_create_image_without_an_image_id_is_uncertain(self):
        self.ec2.create_image = lambda **kwargs: {}
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().create_image(
                "i-0123456789abcdef0", "example-warm", deployment=DEPLOYMENT, build_id="b", basis_digest="a" * 64
            )
        self.assertTrue(raised.exception.uncertain)

    def test_images_and_snapshots_are_read_by_ownership_tags(self):
        tags = tag_list({IMAGE_BUILD_TAG: "true", DEPLOYMENT_TAG: DEPLOYMENT, BUILD_ID_TAG: "build-0001"})
        self.ec2.images_store.append(
            {
                "ImageId": "ami-0123456789abcdef0",
                "State": "available",
                "Tags": tags,
                "BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-0123456789abcdef0"}}, {"DeviceName": "x"}],
            }
        )
        self.ec2.images_store.append({"ImageId": "ami-foreign", "State": "available", "Tags": []})
        self.ec2.snapshots_store.append({"SnapshotId": "snap-0123456789abcdef0", "State": "completed", "Tags": tags})
        aws = self.image_aws()
        (image,) = aws.images(DEPLOYMENT)
        self.assertEqual(image.snapshot_ids, ("snap-0123456789abcdef0",))
        self.assertEqual(image.tags[BUILD_ID_TAG], "build-0001")
        (snapshot,) = aws.snapshots(DEPLOYMENT)
        self.assertEqual(snapshot.snapshot_id, "snap-0123456789abcdef0")
        self.assertEqual(aws.image("ami-0123456789abcdef0").state, "available")
        self.assertIsNone(aws.image("ami-0000000000000000f"))
        self.assertEqual(aws.snapshot("snap-0123456789abcdef0").state, "completed")
        self.assertIsNone(aws.snapshot("snap-0000000000000000f"))

    def test_a_truncated_or_malformed_listing_is_refused_not_returned_partially(self):
        self.ec2.pages["describe_images"] = True
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().images(DEPLOYMENT)
        self.assertEqual(raised.exception.reason, "AWS_INVENTORY_INCOMPLETE")
        self.ec2.pages.clear()
        self.ec2.images_store.append(
            {"State": "available", "Tags": tag_list({IMAGE_BUILD_TAG: "true", DEPLOYMENT_TAG: DEPLOYMENT})}
        )
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().images(DEPLOYMENT)
        self.assertEqual(raised.exception.reason, "AWS_INVENTORY_INCOMPLETE")

    def test_an_unreadable_image_is_uncertain_and_not_absence(self):
        self.ec2.errors["describe_images"] = ProviderError("RequestLimitExceeded")
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().image("ami-0123456789abcdef0")
        self.assertTrue(raised.exception.uncertain)

    def test_deregister_and_snapshot_delete_name_exactly_one_resource(self):
        aws = self.image_aws()
        aws.deregister_image("ami-0123456789abcdef0")
        aws.delete_snapshot("snap-0123456789abcdef0")
        self.assertEqual(self.requests("deregister_image"), [{"ImageId": "ami-0123456789abcdef0"}])
        self.assertEqual(self.requests("delete_snapshot"), [{"SnapshotId": "snap-0123456789abcdef0"}])

    def test_a_failed_deregister_raises_so_the_snapshot_is_not_deleted_by_the_caller(self):
        self.ec2.errors["deregister_image"] = ProviderError("UnauthorizedOperation")
        with self.assertRaises(ProviderRefusal):
            self.image_aws().deregister_image("ami-0123456789abcdef0")

    def test_a_new_version_derives_from_the_numeric_pin_and_changes_only_the_image(self):
        number = self.image_aws().create_template_version(
            "example-worker", 3, "ami-0123456789abcdef0", "build-0001 aaaaaaaaaaaa", "token-0002"
        )
        self.assertEqual(number, 5)
        (request,) = self.requests("create_launch_template_version")
        self.assertEqual(request["SourceVersion"], "3")
        self.assertEqual(request["LaunchTemplateData"], {"ImageId": "ami-0123456789abcdef0"})
        self.assertEqual(request["ClientToken"], "token-0002")
        self.assertNotIn("modify_launch_template", self.names())

    def test_a_new_version_refuses_a_moving_source_or_a_non_literal_image(self):
        for source in ("$Latest", "$Default", 0, None):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    self.image_aws().create_template_version("example-worker", source, "ami-0123456789abcdef0", "d", "t")
        for image in ("resolve:ssm:/path", "", None):
            with self.subTest(image=image):
                with self.assertRaises(ValueError):
                    self.image_aws().create_template_version("example-worker", 3, image, "d", "t")
        self.assertEqual(self.requests("create_launch_template_version"), [])

    def test_template_version_read_back_returns_the_whole_data_without_exposing_it(self):
        data = {"ImageId": "ami-0123456789abcdef0", "InstanceType": "t4g.small", "UserData": SENTINEL_USER_DATA}
        self.ec2.versions_store[("example-worker", 4)] = {
            "VersionNumber": 4,
            "LaunchTemplateId": "lt-0123456789abcdef0",
            "VersionDescription": "build-0001",
            "LaunchTemplateData": data,
        }
        version = self.image_aws().template_version("example-worker", 4)
        self.assertEqual(version.data, data)
        self.assertIsNot(version.data, data)
        self.assertEqual(version.as_dict()["image_id"], "ami-0123456789abcdef0")
        for text in (repr(version), str(version), repr(version.as_dict())):
            self.assertNotIn("sentinel-user-data-marker", text)
        self.assertEqual(self.requests("describe_launch_template_versions")[0]["Versions"], ["4"])

    def test_a_missing_version_is_refused_and_a_dataless_entry_is_incomplete(self):
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().template_version("example-worker", 9)
        self.assertEqual(raised.exception.reason, "TEMPLATE_NOT_FOUND")
        self.ec2.versions_store[("example-worker", 9)] = {"VersionNumber": 9}
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().template_version("example-worker", 9)
        self.assertEqual(raised.exception.reason, "AWS_INVENTORY_INCOMPLETE")

    def test_launch_templates_are_described_and_an_unknown_name_is_empty(self):
        self.ec2.templates_store.append(
            {
                "LaunchTemplateName": "example-worker",
                "LaunchTemplateId": "lt-0123456789abcdef0",
                "DefaultVersionNumber": 1,
                "LatestVersionNumber": 4,
            }
        )
        aws = self.image_aws()
        (info,) = aws.launch_templates(["example-worker"])
        self.assertEqual((info.default_version, info.latest_version), (1, 4))
        self.assertEqual(aws.launch_templates(["example-missing"]), [])

    def test_a_missing_name_among_several_does_not_hide_the_others(self):
        self.ec2.templates_store.append(
            {
                "LaunchTemplateName": "example-worker",
                "LaunchTemplateId": "lt-0123456789abcdef0",
                "DefaultVersionNumber": 1,
                "LatestVersionNumber": 4,
            }
        )
        found = self.image_aws().launch_templates(["example-missing", "example-worker"])
        self.assertEqual([info.name for info in found], ["example-worker"])
        self.assertEqual(
            [request["LaunchTemplateNames"] for request in self.requests("describe_launch_templates")],
            [["example-missing"], ["example-worker"]],
        )

    def test_a_version_read_that_returns_another_version_is_refused(self):
        self.ec2.versions_store[("example-worker", 4)] = {
            "VersionNumber": 7,
            "LaunchTemplateId": "lt-0123456789abcdef0",
            "LaunchTemplateData": {"ImageId": "ami-0123456789abcdef0"},
        }
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().template_version("example-worker", 4)
        self.assertEqual(raised.exception.reason, "AWS_INVENTORY_INCOMPLETE")


class ArchiveTests(Base):
    KEY = PREFIX + "build-0001.tar.gz"

    def put(self, chunks, **overrides):
        values = dict(max_bytes=100, part_bytes=10)
        values.update(overrides)
        return self.image_aws().put_archive(BUCKET, self.KEY, chunks, **values)

    def test_a_stream_becomes_ordered_parts_and_reports_size_and_digest(self):
        import hashlib

        payload = b"0123456789" * 3 + b"abc"
        result = self.put([payload[:7], payload[7:25], payload[25:]])
        self.assertEqual(result.size, len(payload))
        self.assertEqual(result.sha256, hashlib.sha256(payload).hexdigest())
        self.assertEqual(b"".join(self.s3.uploaded), payload)
        self.assertEqual([len(part) for part in self.s3.uploaded], [10, 10, 10, 3])
        (completion,) = self.requests("complete_multipart_upload")
        self.assertEqual([part["PartNumber"] for part in completion["MultipartUpload"]["Parts"]], [1, 2, 3, 4])
        self.assertNotIn("abort_multipart_upload", self.names())
        self.assertNotIn("delete_object", self.names())

    def test_an_overrun_aborts_the_upload_and_deletes_the_key(self):
        with self.assertRaises(ProviderRefusal) as raised:
            self.put([b"x" * 60, b"y" * 60, b"z" * 60], max_bytes=100)
        self.assertEqual(raised.exception.reason, "IMAGE_ARCHIVE_TOO_LARGE")
        self.assertNotIn("complete_multipart_upload", self.names())
        (abort,) = self.requests("abort_multipart_upload")
        self.assertEqual(abort["UploadId"], "upload-0001")
        self.assertEqual(self.requests("delete_object"), [{"Bucket": BUCKET, "Key": self.KEY}])
        # Nothing past the cap was uploaded.
        self.assertLessEqual(sum(len(part) for part in self.s3.uploaded), 100)

    def test_a_stream_of_exactly_the_cap_is_accepted(self):
        result = self.put([b"x" * 100], max_bytes=100)
        self.assertEqual(result.size, 100)

    def test_a_failed_part_aborts_the_upload_and_deletes_the_key(self):
        self.s3.fail_part = 2
        with self.assertRaises(ProviderRefusal) as raised:
            self.put([b"x" * 35])
        self.assertEqual(raised.exception.reason, "IMAGE_ARCHIVE_UPLOAD_FAILED")
        self.assertNotIn("complete_multipart_upload", self.names())
        self.assertEqual(len(self.requests("abort_multipart_upload")), 1)
        self.assertEqual(len(self.requests("delete_object")), 1)

    def test_a_failed_completion_aborts_and_deletes_a_possibly_created_key(self):
        self.s3.errors["complete_multipart_upload"] = ProviderError("InternalError")
        with self.assertRaises(ProviderRefusal) as raised:
            self.put([b"x" * 15])
        self.assertEqual(raised.exception.reason, "IMAGE_ARCHIVE_UPLOAD_FAILED")
        self.assertEqual(len(self.requests("abort_multipart_upload")), 1)
        self.assertEqual(len(self.requests("delete_object")), 1)

    def test_a_failing_source_stream_aborts_and_deletes(self):
        def stream():
            yield b"x" * 15
            raise ConnectionError(LEAKY_MESSAGE)

        with self.assertRaises(ProviderRefusal) as raised:
            self.put(stream())
        self.assertEqual(raised.exception.reason, "IMAGE_ARCHIVE_UPLOAD_FAILED")
        self.assertEqual(raised.exception.detail, "ConnectionError")
        self.assertEqual(len(self.requests("abort_multipart_upload")), 1)
        self.assertEqual(len(self.requests("delete_object")), 1)

    def test_a_refusal_from_the_source_stream_keeps_its_reason_after_cleanup(self):
        def stream():
            yield b"x" * 15
            raise Refusal("IMAGE_SOURCE_FORBIDDEN")

        with self.assertRaises(Refusal) as raised:
            self.put(stream())
        self.assertEqual(raised.exception.reason, "IMAGE_SOURCE_FORBIDDEN")
        self.assertEqual(len(self.requests("abort_multipart_upload")), 1)
        self.assertEqual(len(self.requests("delete_object")), 1)

    def test_a_stalled_source_stream_stops_at_the_deadline_and_is_cleaned_up(self):
        def stream():
            yield b"x" * 5
            self.clock.advance(300)
            yield b"x" * 5

        with self.assertRaises(DeadlineRefusal):
            self.put(stream(), deadline=self.clock() + 240)
        self.assertNotIn("complete_multipart_upload", self.names())
        self.assertEqual(len(self.requests("abort_multipart_upload")), 1)
        self.assertEqual(len(self.requests("delete_object")), 1)

    def test_a_deadline_that_ends_between_parts_stops_the_next_part(self):
        original = self.s3.upload_part

        def slow_part(**arguments):
            self.clock.advance(300)
            return original(**arguments)

        self.s3.upload_part = slow_part
        with self.assertRaises(DeadlineRefusal):
            self.put([b"x" * 35], deadline=self.clock() + 240)
        self.assertEqual(len(self.s3.uploaded), 1)
        self.assertNotIn("complete_multipart_upload", self.names())
        self.assertEqual(len(self.requests("abort_multipart_upload")), 1)
        self.assertEqual(len(self.requests("delete_object")), 1)

    def test_no_deadline_leaves_the_upload_unbounded_by_the_clock(self):
        def stream():
            yield b"x" * 5
            self.clock.advance(10_000)
            yield b"x" * 5

        self.assertEqual(self.put(stream()).size, 10)

    def test_an_interrupt_aborts_the_upload_and_propagates(self):
        def stream():
            yield b"x" * 15
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.put(stream())
        self.assertEqual(len(self.requests("abort_multipart_upload")), 1)
        self.assertEqual(len(self.requests("delete_object")), 1)

    def test_an_empty_or_non_bytes_stream_is_not_staged(self):
        for chunks in ([], [b""], ["text"]):
            with self.subTest(chunks=chunks):
                self.calls.clear()
                with self.assertRaises(ProviderRefusal) as raised:
                    self.put(chunks)
                self.assertEqual(raised.exception.reason, "IMAGE_ARCHIVE_UPLOAD_FAILED")
                self.assertNotIn("complete_multipart_upload", self.names())
                self.assertEqual(len(self.requests("abort_multipart_upload")), 1)

    def test_unconfirmed_cleanup_is_stated_in_the_detail(self):
        self.s3.fail_part = 1
        self.s3.errors["abort_multipart_upload"] = ProviderError("InternalError")
        with self.assertRaises(ProviderRefusal) as raised:
            self.put([b"x" * 15])
        self.assertIn("cleanup not confirmed", raised.exception.detail)
        # The delete still ran although the abort failed.
        self.assertEqual(len(self.requests("delete_object")), 1)

    def test_no_upload_starts_when_the_provider_refuses_to_open_one(self):
        self.s3.errors["create_multipart_upload"] = ProviderError("AccessDenied")
        with self.assertRaises(ProviderRefusal):
            self.put([b"x"])
        self.assertEqual(self.names(), ["create_multipart_upload"])

    def test_objects_are_listed_across_pages_and_a_truncated_listing_is_refused(self):
        self.s3.pages = [
            {"Contents": [{"Key": PREFIX + "a", "Size": 1, "LastModified": datetime(2026, 11, 1, tzinfo=timezone.utc)}], "NextContinuationToken": "1"},
            {"Contents": [{"Key": PREFIX + "b", "Size": 2}]},
        ]
        found = self.image_aws().list_objects(BUCKET, PREFIX)
        self.assertEqual([item.key for item in found], [PREFIX + "a", PREFIX + "b"])
        self.assertEqual(found[0].last_modified, datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())
        self.assertIsNone(found[1].last_modified)
        self.s3.pages = [{"Contents": [], "NextContinuationToken": "0"}]
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().list_objects(BUCKET, PREFIX)
        self.assertEqual(raised.exception.reason, "AWS_INVENTORY_INCOMPLETE")

    def test_an_object_entry_without_a_key_is_refused(self):
        self.s3.pages = [{"Contents": [{"Size": 1}]}]
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().list_objects(BUCKET, PREFIX)
        self.assertEqual(raised.exception.reason, "AWS_INVENTORY_INCOMPLETE")

    def test_delete_names_one_key(self):
        self.image_aws().delete_object(BUCKET, self.KEY)
        self.assertEqual(self.requests("delete_object"), [{"Bucket": BUCKET, "Key": self.KEY}])


class PresignTests(Base):
    KEY = PREFIX + "build-0001.tar.gz"

    def test_presign_signs_a_get_for_the_requested_window(self):
        url = self.image_aws(expiry=NOW + 3600).presign_get(BUCKET, self.KEY, 1800)
        self.assertEqual(url, SENTINEL_URL)
        self.assertEqual(
            self.requests("generate_presigned_url"),
            [{"ClientMethod": "get_object", "Params": {"Bucket": BUCKET, "Key": self.KEY}, "ExpiresIn": 1800}],
        )

    def test_presign_refuses_when_the_session_has_less_life_than_the_window(self):
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws(expiry=NOW + 1799).presign_get(BUCKET, self.KEY, 1800)
        self.assertEqual(raised.exception.reason, "IMAGE_CREDENTIAL_SHORT")
        self.assertFalse(raised.exception.uncertain)
        self.assertEqual(self.requests("generate_presigned_url"), [])

    def test_the_boundary_is_inclusive_and_the_clock_is_the_injected_one(self):
        aws = self.image_aws(expiry=NOW + 1800)
        self.assertEqual(aws.presign_get(BUCKET, self.KEY, 1800), SENTINEL_URL)
        self.clock.advance(1)
        with self.assertRaises(ProviderRefusal) as raised:
            aws.presign_get(BUCKET, self.KEY, 1800)
        self.assertEqual(raised.exception.reason, "IMAGE_CREDENTIAL_SHORT")

    def test_an_expired_session_reports_no_negative_life(self):
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws(expiry=NOW - 10).presign_get(BUCKET, self.KEY, 300)
        self.assertIn("0 seconds", raised.exception.detail)

    def test_an_unknown_expiry_is_a_refusal_not_long_enough(self):
        session = FakeSession(self.calls, self.ec2, self.s3, credentials=FakeCredentials(None))
        aws = ImageAws(REGION, PROFILE, session_factory=lambda profile: session, clients={"s3": self.s3}, clock=self.clock)
        with self.assertRaises(ProviderRefusal) as raised:
            aws.presign_get(BUCKET, self.KEY, 300)
        self.assertEqual(raised.exception.reason, "IMAGE_CREDENTIAL_SHORT")
        session.credentials = None
        aws = ImageAws(REGION, PROFILE, session_factory=lambda profile: session, clients={"s3": self.s3}, clock=self.clock)
        with self.assertRaises(ProviderRefusal):
            aws.presign_get(BUCKET, self.KEY, 300)
        self.assertEqual(self.requests("generate_presigned_url"), [])

    def test_session_credentials_are_resolved_before_their_expiry_is_read(self):
        expiry = datetime.fromtimestamp(NOW + 3600, timezone.utc)
        credentials = FakeCredentials(expiry)
        session = FakeSession(self.calls, self.ec2, self.s3, credentials=credentials)
        aws = ImageAws(REGION, PROFILE, session_factory=lambda profile: session, clients={"s3": self.s3}, clock=self.clock)
        self.assertEqual(aws.presign_get(BUCKET, self.KEY, 1800), SENTINEL_URL)
        self.assertEqual(credentials.resolved, 1)
        short = FakeCredentials(datetime.fromtimestamp(NOW + 100, timezone.utc))
        session = FakeSession(self.calls, self.ec2, self.s3, credentials=short)
        aws = ImageAws(REGION, PROFILE, session_factory=lambda profile: session, clients={"s3": self.s3}, clock=self.clock)
        with self.assertRaises(ProviderRefusal) as raised:
            aws.presign_get(BUCKET, self.KEY, 1800)
        self.assertEqual(raised.exception.reason, "IMAGE_CREDENTIAL_SHORT")

    def test_expiry_may_be_a_callable(self):
        aws = self.image_aws(expiry=lambda: NOW + 3600)
        self.assertEqual(aws.presign_get(BUCKET, self.KEY, 1800), SENTINEL_URL)


class Capture:
    """Every surface a secret could reach: logs, streams and error renderings."""

    def __init__(self):
        self.records = []
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.rendered = []

    def __enter__(self):
        capture = self

        class Handler(logging.Handler):
            def emit(self, record):
                capture.records.append(record.getMessage())
                if record.exc_info:
                    capture.records.append("".join(traceback.format_exception(*record.exc_info)))

        self._handler = Handler(level=logging.DEBUG)
        self._root = logging.getLogger()
        self._level = self._root.level
        self._root.addHandler(self._handler)
        self._root.setLevel(logging.DEBUG)
        self._streams = contextlib.ExitStack()
        self._streams.enter_context(contextlib.redirect_stdout(self.out))
        self._streams.enter_context(contextlib.redirect_stderr(self.err))
        return self

    def __exit__(self, *exc):
        self._streams.close()
        self._root.removeHandler(self._handler)
        self._root.setLevel(self._level)
        return False

    def run(self, call):
        try:
            return call()
        except Refusal as refusal:
            self.rendered.extend(
                [
                    str(refusal),
                    repr(refusal),
                    refusal.reason,
                    refusal.detail,
                    repr(refusal.as_dict()),
                    "".join(traceback.format_exception(refusal)),
                ]
            )
        except Exception as error:  # noqa: BLE001 - the rendering is what is measured
            self.rendered.extend([str(error), repr(error), "".join(traceback.format_exception(error))])
        return None

    def surfaces(self):
        return [*self.records, self.out.getvalue(), self.err.getvalue(), *self.rendered]


class NoSecretTests(Base):
    """4.5: sentinel URL, token and user data never reach a log line, error or summary."""

    def assert_clean(self, surfaces, label):
        for text in surfaces:
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, text, f"{label}: a sentinel reached {text[:80]!r}")

    def launch(self, aws):
        return aws.run_builder(
            template_name="example-ci-burst-image-builder",
            template_version=2,
            deployment=DEPLOYMENT,
            build_id="build-0001",
            expires_at=NOW + 2700,
            user_data=SENTINEL_USER_DATA,
            client_token="token-0001",
        )

    def test_a_provider_error_keeps_only_its_code_in_every_rendering(self):
        _, modules = sdk_client_error("InvalidParameterValue")
        ClientError = modules["botocore.exceptions"].ClientError

        class Leaky(ClientError):
            response = {"Error": {"Code": "InvalidParameterValue", "Message": LEAKY_MESSAGE}}

        for label, patched in (("sdk", modules), ("no sdk", {"botocore": None, "botocore.exceptions": None})):
            for method in ("run_instances", "create_image", "describe_instances", "deregister_image"):
                self.ec2.errors = {method: Leaky(LEAKY_MESSAGE)}
                aws = self.image_aws()
                calls = {
                    "run_instances": lambda: self.launch(aws),
                    "create_image": lambda: aws.create_image(
                        "i-0123456789abcdef0", "n", deployment=DEPLOYMENT, build_id="b", basis_digest="a" * 64
                    ),
                    "describe_instances": lambda: aws.builders(DEPLOYMENT),
                    "deregister_image": lambda: aws.deregister_image("ami-0123456789abcdef0"),
                }
                with self.subTest(label=label, method=method):
                    with mock.patch.dict(sys.modules, patched), Capture() as capture:
                        capture.run(calls[method])
                    self.assertTrue(capture.rendered)
                    self.assert_clean(capture.surfaces(), f"{label} {method}")

    def test_upload_failures_keep_stream_and_provider_text_out_of_every_rendering(self):
        def leaking_stream():
            yield b"x" * 15
            raise RuntimeError(LEAKY_MESSAGE)

        def overrun():
            yield b"x" * 200

        scenarios = {
            "stream": (lambda: leaking_stream(), {}),
            "part": (lambda: [b"x" * 35], {"fail_part": 2}),
            "complete": (lambda: [b"x" * 15], {"errors": {"complete_multipart_upload": ProviderError("InternalError")}}),
            "abort": (lambda: [b"x" * 35], {"fail_part": 1, "errors": {"abort_multipart_upload": ProviderError("InternalError")}}),
            "overrun": (overrun, {}),
        }
        for name, (stream, settings) in scenarios.items():
            with self.subTest(name=name):
                fresh = FakeS3(self.calls)
                for attribute, value in settings.items():
                    setattr(fresh, attribute, value)
                aws = ImageAws(REGION, PROFILE, clients={"ec2": self.ec2, "s3": fresh}, clock=self.clock, expiry=NOW + 3600)
                with Capture() as capture:
                    capture.run(lambda: aws.put_archive(BUCKET, PREFIX + "x.tar.gz", stream(), max_bytes=100, part_bytes=10))
                self.assertTrue(capture.rendered)
                self.assert_clean(capture.surfaces(), name)

    def test_presign_errors_and_refusals_do_not_carry_the_url(self):
        self.s3.errors["generate_presigned_url"] = ProviderError("InternalError")
        with Capture() as capture:
            capture.run(lambda: self.image_aws().presign_get(BUCKET, PREFIX + "x.tar.gz", 1800))
            capture.run(lambda: self.image_aws(expiry=NOW + 10).presign_get(BUCKET, PREFIX + "x.tar.gz", 1800))
        self.assertEqual(len(capture.rendered), 12)
        self.assert_clean(capture.surfaces(), "presign")

    def test_a_whole_successful_pass_logs_and_prints_nothing_secret_and_summaries_omit_it(self):
        self.ec2.versions_store[("example-worker", 3)] = {
            "VersionNumber": 3,
            "LaunchTemplateId": "lt-0123456789abcdef0",
            "LaunchTemplateData": {"ImageId": "ami-0123456789abcdef0", "UserData": SENTINEL_USER_DATA},
        }
        aws = self.image_aws()
        summaries = []
        with Capture() as capture:
            instance_id = self.launch(aws)
            archive = aws.put_archive(BUCKET, PREFIX + "x.tar.gz", [b"data"], max_bytes=100)
            url = aws.presign_get(BUCKET, PREFIX + "x.tar.gz", 1800)
            version = aws.template_version("example-worker", 3)
            summaries.extend(
                [
                    repr(instance_id),
                    repr(archive),
                    repr(version),
                    repr(version.as_dict()),
                    repr([item.as_dict() for item in aws.builders(DEPLOYMENT)]),
                    repr([item for item in aws.builders(DEPLOYMENT)]),
                ]
            )
        # The presigned URL is the return value of presign_get and nothing else.
        self.assertEqual(url, SENTINEL_URL)
        self.assert_clean([*capture.surfaces(), *summaries], "successful pass")

    def test_the_adapter_does_not_log_at_all(self):
        source = Path(aws_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import logging", source)
        self.assertNotIn("print(", source)

    def test_the_launch_request_is_the_only_place_user_data_travels(self):
        self.launch(self.image_aws())
        carriers = [name for name, arguments in self.calls if "sentinel-user-data-marker" in repr(arguments)]
        self.assertEqual(carriers, ["run_instances"])

    def test_no_request_carries_an_instance_profile(self):
        aws = self.image_aws()
        instance_id = self.launch(aws)
        aws.builders(DEPLOYMENT)
        aws.create_image(instance_id, "n", deployment=DEPLOYMENT, build_id="b", basis_digest="a" * 64)
        aws.create_template_version("example-worker", 3, "ami-0123456789abcdef0", "d", "t")
        aws.put_archive(BUCKET, PREFIX + "x.tar.gz", [b"data"], max_bytes=100)
        aws.presign_get(BUCKET, PREFIX + "x.tar.gz", 1800)

        def keys(value):
            if isinstance(value, dict):
                for key, inner in value.items():
                    yield key
                    yield from keys(inner)
            elif isinstance(value, (list, tuple)):
                for inner in value:
                    yield from keys(inner)

        every_key = [key for _, arguments in self.calls for key in keys(arguments)]
        self.assertTrue(every_key)
        for key in every_key:
            self.assertNotIn("InstanceProfile", str(key))
            self.assertNotIn("IamInstanceProfile", str(key))
        self.assertNotIn("InstanceMarketOptions", every_key)


if __name__ == "__main__":
    unittest.main()
