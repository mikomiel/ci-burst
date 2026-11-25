"""The image-build identity of the AWS adapter, against fake clients.

The fakes take explicit keyword arguments instead of ``**kwargs``, so a request
with an unexpected field fails the test. Their parameter names follow the EC2
and S3 APIs from the design and were not checked against an installed SDK,
which this checkout does not have.

``RealSdkProfileTests``, ``RealSdkLoggingTests`` and ``RealSdkCredentialFailureTests``
are the exception: they run the real boto3 and botocore against temporary
configuration files, a loopback credential endpoint and a stub in place of the
HTTP transport, so they make no network call. They are skipped where boto3 is
not installed.

Sentinel values stand in for the presigned URL, a token and builder user data.
They are synthetic and carry no key-like or signature-like shape.
"""

import ast
import base64
import contextlib
import copy
import http.server
import inspect
import io
import itertools
import json
import logging
import os
import re
import shlex
import sys
import tempfile
import threading
import traceback
import types
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from unittest import mock

try:
    import boto3
    import botocore.credentials
    import botocore.exceptions
    import botocore.httpsession
    import botocore.session
    from botocore.awsrequest import AWSResponse
except ImportError:  # the suite also runs on a checkout with no SDK
    boto3 = None

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
from ci_burst.errors import DeadlineRefusal, ProviderRefusal, Refusal, StateRefusal

from support import Clock, write_config
from test_aws import sdk_client_error

DEPLOYMENT = "test-deployment"
REGION = "eu-central-1"
PROFILE = "example-image-profile"
BUCKET = "example-ci-burst-staging"
ACCOUNT = "111122223333"
PREFIX = "warm-archives/"
NOW = 1_800_000_000.0

SENTINEL_URL = "https://example-bucket.example.org/sentinel-presigned-url-marker"
SENTINEL_TOKEN = "sentinel-forge-token-marker"
SENTINEL_USER_DATA = "#!/bin/bash\necho sentinel-user-data-marker"
SENTINELS = (SENTINEL_URL, SENTINEL_TOKEN, SENTINEL_USER_DATA, "sentinel-user-data-marker")
LEAKY_MESSAGE = f"request {SENTINEL_URL} header {SENTINEL_TOKEN} body {SENTINEL_USER_DATA}"

ROLE_ARN = "arn:aws:iam::123456789012:role/example-image-build-role"
INTERMEDIATE_ROLE_ARN = "arn:aws:iam::123456789012:role/example-intermediate-role"
SOURCE_PROFILE = "example-image-source"
LINK_PROFILE = "example-image-link"
#: Synthetic and short on purpose: nothing here has the shape of a real key.
STATIC_KEYS = {"aws_access_key_id": "fake-id", "aws_secret_access_key": "fake-pw"}
REAL_SDK_REASON = "boto3 is not installed"

#: The loggers the adapter must keep quiet. Written out here, not read from the
#: module, so that dropping one from the module fails a test.
SDK_LOGGER_NAMES = ("botocore", "boto3", "urllib3")
SENTINEL_CLIENT_TOKEN = "sentinel-client-token-marker"
ASSUMED_MARKERS = ("assumed-id-marker", "assumed-pw-marker", "assumed-session-marker")


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
        self.security_groups_store = []
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

    def describe_security_groups(self, *, GroupIds=None, Filters=None, NextToken=None):
        self._enter("describe_security_groups", dict(GroupIds=GroupIds, Filters=Filters, NextToken=NextToken))
        found = list(self.security_groups_store)
        if GroupIds is not None:
            known = {item.get("GroupId") for item in found}
            # Like EC2: one unknown id fails the whole request.
            if any(group_id not in known for group_id in GroupIds):
                raise ProviderError("InvalidGroup.NotFound")
            found = [item for item in found if item.get("GroupId") in GroupIds]
        for entry in Filters or []:
            if entry["Name"] != "group-name":
                raise AssertionError(f"unexpected filter {entry['Name']}")
            found = [item for item in found if item.get("GroupName") in entry["Values"]]
        response = {"SecurityGroups": copy.deepcopy(found)}
        if "describe_security_groups" in self.pages:
            response["NextToken"] = "next-page"
        return response

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

    def create_multipart_upload(self, *, Bucket, Key, ExpectedBucketOwner):
        self._enter("create_multipart_upload", dict(Bucket=Bucket, Key=Key, ExpectedBucketOwner=ExpectedBucketOwner))
        return {"UploadId": "upload-0001"}

    def upload_part(self, *, Bucket, Key, UploadId, PartNumber, Body, ExpectedBucketOwner):
        self._enter(
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
        if self.fail_part == PartNumber:
            raise ProviderError("SlowDown")
        self.uploaded.append(bytes(Body))
        return {"ETag": f"etag-{PartNumber}"}

    def complete_multipart_upload(self, *, Bucket, Key, UploadId, MultipartUpload, ExpectedBucketOwner):
        self._enter(
            "complete_multipart_upload",
            dict(
                Bucket=Bucket,
                Key=Key,
                UploadId=UploadId,
                MultipartUpload=MultipartUpload,
                ExpectedBucketOwner=ExpectedBucketOwner,
            ),
        )
        return {}

    def abort_multipart_upload(self, *, Bucket, Key, UploadId, ExpectedBucketOwner):
        self._enter(
            "abort_multipart_upload",
            dict(Bucket=Bucket, Key=Key, UploadId=UploadId, ExpectedBucketOwner=ExpectedBucketOwner),
        )
        return {}

    def delete_object(self, *, Bucket, Key, ExpectedBucketOwner):
        self._enter("delete_object", dict(Bucket=Bucket, Key=Key, ExpectedBucketOwner=ExpectedBucketOwner))
        return {}

    def list_objects_v2(self, *, Bucket, Prefix, ExpectedBucketOwner, ContinuationToken=None):
        self._enter(
            "list_objects_v2",
            dict(
                Bucket=Bucket,
                Prefix=Prefix,
                ExpectedBucketOwner=ExpectedBucketOwner,
                ContinuationToken=ContinuationToken,
            ),
        )
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


class FailingCredentials:
    """Lazy credentials whose first resolution fails, like an assumed role that the provider refuses."""

    def __init__(self, error):
        self.error = error
        self.resolved = 0

    def get_frozen_credentials(self):
        self.resolved += 1
        raise self.error


class FakeSts:
    def __init__(self, calls, account=ACCOUNT):
        self.calls = calls
        self.account = account
        self.errors = {}

    def get_caller_identity(self):
        self.calls.append(("get_caller_identity", {}))
        if "get_caller_identity" in self.errors:
            raise self.errors["get_caller_identity"]
        return {"Account": self.account, "Arn": "arn:aws:sts::" + str(self.account) + ":assumed-role/example/session"}


class FakeSession:
    def __init__(self, calls, ec2, s3, credentials=None, sts=None):
        self.calls = calls
        self.ec2 = ec2
        self.s3 = s3
        self.sts = sts if sts is not None else FakeSts(calls)
        self.credentials = credentials
        self.services = []

    def client(self, service, region_name=None, config=None):
        self.services.append(service)
        self.calls.append(("session.client", {"service": service, "region": region_name}))
        return {"ec2": self.ec2, "s3": self.s3, "sts": self.sts}[service]

    def get_credentials(self):
        return self.credentials


class FakeSdkSession:
    """A botocore session stand-in: the merged profile configuration and the credentials.

    ``scoped`` is the image profile. It leads to a source profile that holds
    static keys, unless ``profiles`` (every profile by name) says otherwise.
    """

    def __init__(self, steps, scoped=None, credentials=None, config_error=None, credentials_error=None, profiles=None):
        self.steps = steps
        self.profiles = profiles if profiles is not None else {PROFILE: scoped, SOURCE_PROFILE: dict(STATIC_KEYS)}
        self.credentials = credentials
        self.config_error = config_error
        self.credentials_error = credentials_error

    @property
    def full_config(self):
        self.steps.append("full_config")
        if self.config_error is not None:
            raise self.config_error
        return {"profiles": self.profiles}

    def get_credentials(self):
        self.steps.append("get_credentials")
        if self.credentials_error is not None:
            raise self.credentials_error
        return self.credentials


def chained_profiles(length):
    """``length`` profiles: the image profile, roles that each name the next, and a last one that holds static keys.

    Returns the profiles by name and their names in order.
    """
    names = [PROFILE, *(f"{LINK_PROFILE}-{number}" for number in range(1, length))]
    profiles = {
        name: {"role_arn": ROLE_ARN, "source_profile": following, "region": REGION}
        for name, following in itertools.pairwise(names)
    }
    profiles[names[-1]] = dict(STATIC_KEYS)
    return profiles, names


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
            account=ACCOUNT,
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

    def test_clients_use_the_region_and_only_ec2_s3_and_sts_exist(self):
        session = FakeSession(self.calls, self.ec2, self.s3)
        with mock.patch.object(aws_module, "_sdk_config", return_value=object()) as config:
            aws = ImageAws(REGION, PROFILE, session_factory=lambda profile: session)
            aws._client("ec2")
            aws._client("s3")
            aws._client("sts")
            with self.assertRaises(ValueError):
                aws._client("iam")
        self.assertEqual(session.services, ["ec2", "s3", "sts"])
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

    def client_calls(self):
        """Every operation that needs a client of the image session, by name."""
        return {
            "builders": lambda aws: aws.builders(DEPLOYMENT),
            "instances": lambda aws: aws.instances(["i-0123456789abcdef0"]),
            "run_builder": lambda aws: self.builder(aws),
            "image": lambda aws: aws.image("ami-0123456789abcdef0"),
            "snapshot": lambda aws: aws.snapshot("snap-0123456789abcdef0"),
            "deregister_image": lambda aws: aws.deregister_image("ami-0123456789abcdef0"),
            "put_archive": lambda aws: aws.put_archive(BUCKET, PREFIX + "x.tar.gz", [b"data"], max_bytes=100),
            "presign_get": lambda aws: aws.presign_get(BUCKET, PREFIX + "x.tar.gz", 1800),
        }

    def test_a_failure_to_build_a_client_leaves_as_a_provider_refusal_from_every_operation(self):
        class NoClient(FakeSession):
            def client(self, service, region_name=None, config=None):
                raise RuntimeError(LEAKY_MESSAGE)

        def no_session(profile):
            raise RuntimeError(LEAKY_MESSAGE)

        factories = {"client build": lambda profile: NoClient(self.calls, self.ec2, self.s3), "session": no_session}
        with mock.patch.object(aws_module, "_sdk_config", return_value=object()):
            for label, factory in factories.items():
                for name, call in self.client_calls().items():
                    with self.subTest(label=label, call=name):
                        # The expiry is given, so that ``presign_get`` reaches its client.
                        aws = ImageAws(REGION, PROFILE, session_factory=factory, clock=self.clock, expiry=NOW + 3600)
                        with self.assertRaises(ProviderRefusal) as raised:
                            call(aws)
                        refusal = raised.exception
                        self.assertEqual(
                            (refusal.reason, refusal.detail, refusal.uncertain), ("AWS_UNCERTAIN", "RuntimeError", True)
                        )
                        rendered = [str(refusal), repr(refusal), repr(refusal.as_dict())]
                        rendered.append("".join(traceback.format_exception(refusal)))
                        for text, sentinel in itertools.product(rendered, SENTINELS):
                            self.assertNotIn(sentinel, text)
        self.assertEqual(self.names(), [])

    def test_a_refusal_from_the_session_passes_every_operation_unchanged(self):
        refusal = ProviderRefusal("IMAGE_PROFILE_INVALID", f"profile {PROFILE}: it does not exist")

        class RefusingClient(FakeSession):
            def client(self, service, region_name=None, config=None):
                raise refusal

        def refusing_session(profile):
            raise refusal

        with_expiry = {**self.client_calls(), "credential_expiry": lambda aws: aws.credential_expiry()}
        scenarios = {
            "client build": (lambda profile: RefusingClient(self.calls, self.ec2, self.s3), self.client_calls()),
            "session": (refusing_session, with_expiry),
        }
        with mock.patch.object(aws_module, "_sdk_config", return_value=object()):
            for label, (factory, calls) in scenarios.items():
                for name, call in calls.items():
                    # ``credential_expiry`` reads the session only when no expiry is given.
                    expiry = None if name == "credential_expiry" else NOW + 3600
                    with self.subTest(label=label, call=name):
                        aws = ImageAws(REGION, PROFILE, session_factory=factory, clock=self.clock, expiry=expiry)
                        with self.assertRaises(ProviderRefusal) as raised:
                            call(aws)
                        self.assertIs(raised.exception, refusal)

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
        self.assertEqual(set(session.services), {"ec2", "s3", "sts"})

    def test_the_module_creates_no_iam_client(self):
        source = Path(aws_module.__file__).read_text(encoding="utf-8")
        self.assertNotIn('"iam"', source.replace('IMAGE_SERVICES = frozenset({"ec2", "s3", "sts"})', ""))
        self.assertNotIn("'iam'", source)


class ProfileRouteTests(Base):
    """The default session factory takes the profile's assume-role route or refuses.

    Fake ``boto3`` and ``botocore.session`` modules stand in for the SDK, so these
    run where it is absent. ``RealSdkProfileTests`` runs the decisive cases
    against the real one.
    """

    def setUp(self):
        super().setUp()
        self.steps = []

    def session_for(self, sdk_session, constructor_error=None):
        def fake_botocore_session(*, profile):
            self.steps.append(("botocore.session.Session", profile))
            if constructor_error is not None:
                raise constructor_error
            return sdk_session

        def fake_boto3_session(*, botocore_session):
            self.steps.append("boto3.Session")
            return ("boto3 session", botocore_session)

        boto3_module = types.ModuleType("boto3")
        boto3_module.Session = fake_boto3_session
        session_module = types.ModuleType("botocore.session")
        session_module.Session = fake_botocore_session
        botocore_module = types.ModuleType("botocore")
        botocore_module.session = session_module
        modules = {"boto3": boto3_module, "botocore": botocore_module, "botocore.session": session_module}
        with mock.patch.dict(sys.modules, modules):
            return ImageAws(REGION, PROFILE)._session()

    def refusal(self, constructor_error=None, **sdk):
        with self.assertRaises(ProviderRefusal) as raised:
            self.session_for(FakeSdkSession(self.steps, **sdk), constructor_error)
        refusal = raised.exception
        self.assertEqual(refusal.reason, "IMAGE_PROFILE_INVALID")
        self.assertIn(PROFILE, refusal.detail)
        # No session is handed out, so no client can be built on it.
        self.assertNotIn("boto3.Session", self.steps)
        return refusal

    def test_a_role_with_a_source_profile_is_accepted_and_the_session_is_the_checked_one(self):
        scoped = {"role_arn": ROLE_ARN, "source_profile": SOURCE_PROFILE, "region": REGION}
        sdk_session = FakeSdkSession(self.steps, scoped, types.SimpleNamespace(method="assume-role"))
        session = self.session_for(sdk_session)
        self.assertEqual(session, ("boto3 session", sdk_session))
        # The configuration is read before the credentials are resolved.
        self.assertEqual(
            self.steps,
            [("botocore.session.Session", PROFILE), "full_config", "get_credentials", "boto3.Session"],
        )

    def test_a_profile_without_both_a_role_and_a_source_profile_is_refused_before_credentials_resolve(self):
        cases = {
            "no credential route": {"region": REGION},
            "a role without a source": {"role_arn": ROLE_ARN},
            "a source without a role": {"source_profile": SOURCE_PROFILE},
            "an empty role": {"role_arn": "", "source_profile": SOURCE_PROFILE},
            "an empty source": {"role_arn": ROLE_ARN, "source_profile": ""},
            "static keys and no role": dict(STATIC_KEYS),
        }
        for label, scoped in cases.items():
            with self.subTest(label):
                self.steps.clear()
                # The credentials would pass the method check: the configuration alone refuses.
                refusal = self.refusal(scoped=scoped, credentials=types.SimpleNamespace(method="assume-role"))
                self.assertIn("it needs both role_arn and source_profile", refusal.detail)
                self.assertNotIn("get_credentials", self.steps)

    def test_a_profile_that_names_a_credential_source_is_refused_before_credentials_resolve(self):
        for source in ("Ec2InstanceMetadata", "EcsContainer", "Environment"):
            with self.subTest(source):
                self.steps.clear()
                scoped = {"role_arn": ROLE_ARN, "source_profile": SOURCE_PROFILE, "credential_source": source}
                refusal = self.refusal(scoped=scoped, credentials=types.SimpleNamespace(method="assume-role"))
                self.assertIn("credential_source", refusal.detail)
                self.assertNotIn("get_credentials", self.steps)

    def assume_role_credentials(self):
        return types.SimpleNamespace(method="assume-role")

    def test_a_chain_of_roles_that_ends_in_static_keys_is_accepted(self):
        profiles, names = chained_profiles(3)
        for name in names[:-1]:
            profiles[name].update(external_id="example-external-id", role_session_name="example")
            profiles[name]["duration_seconds"] = "900"
        sdk_session = FakeSdkSession(self.steps, profiles=profiles, credentials=self.assume_role_credentials())
        session = self.session_for(sdk_session)
        self.assertEqual(session, ("boto3 session", sdk_session))
        self.assertEqual(
            self.steps,
            [("botocore.session.Session", PROFILE), "full_config", "get_credentials", "boto3.Session"],
        )

    def test_a_chain_may_span_five_profiles_and_not_six(self):
        for length, accepted in ((2, True), (4, True), (5, True), (6, False), (40, False)):
            with self.subTest(length=length):
                self.steps.clear()
                profiles, _ = chained_profiles(length)
                credentials = self.assume_role_credentials()
                if accepted:
                    self.session_for(FakeSdkSession(self.steps, profiles=profiles, credentials=credentials))
                    continue
                refusal = self.refusal(profiles=profiles, credentials=credentials)
                self.assertIn("longer than 5 profiles", refusal.detail)
                self.assertNotIn("get_credentials", self.steps)

    def test_a_chain_that_returns_to_a_profile_is_refused_before_credentials_resolve(self):
        first, second = f"{LINK_PROFILE}-1", f"{LINK_PROFILE}-2"

        def role(source):
            return {"role_arn": ROLE_ARN, "source_profile": source}

        cases = {
            "the image profile names itself": ({PROFILE: role(PROFILE)}, PROFILE),
            "a link names the image profile": ({PROFILE: role(first), first: role(PROFILE)}, PROFILE),
            "two links name each other": ({PROFILE: role(first), first: role(second), second: role(first)}, first),
        }
        for label, (profiles, repeated) in cases.items():
            with self.subTest(label):
                self.steps.clear()
                refusal = self.refusal(profiles=profiles, credentials=self.assume_role_credentials())
                self.assertIn(f"returns to {repeated}", refusal.detail)
                self.assertNotIn("get_credentials", self.steps)

    def test_a_profile_that_the_chain_names_but_the_files_lack_is_refused(self):
        missing = f"{LINK_PROFILE}-missing"
        cases = {
            "a link": (
                {PROFILE: {"role_arn": ROLE_ARN, "source_profile": missing}},
                f"source profile {missing} does not exist",
            ),
            "the image profile": ({SOURCE_PROFILE: dict(STATIC_KEYS)}, "it does not exist"),
        }
        for label, (profiles, wanted) in cases.items():
            with self.subTest(label):
                self.steps.clear()
                refusal = self.refusal(profiles=profiles, credentials=self.assume_role_credentials())
                self.assertIn(wanted, refusal.detail)
                self.assertNotIn("get_credentials", self.steps)

    def test_a_chained_credential_source_is_refused_before_credentials_resolve(self):
        """The case that the method check cannot see.

        botocore reports the assume-role method for this chain, but it asks the
        ambient route for the root credentials as soon as they are requested.
        """
        profiles = {
            PROFILE: {"role_arn": ROLE_ARN, "source_profile": LINK_PROFILE, "region": REGION},
            LINK_PROFILE: {"role_arn": INTERMEDIATE_ROLE_ARN, "credential_source": "EcsContainer"},
        }
        refusal = self.refusal(profiles=profiles, credentials=self.assume_role_credentials())
        self.assertIn(f"source profile {LINK_PROFILE} may not name credential_source", refusal.detail)
        self.assertNotIn("get_credentials", self.steps)

    def test_a_profile_anywhere_in_the_chain_that_names_an_ambient_route_is_refused_without_echoing_its_value(self):
        keys = (
            "credential_source",
            "web_identity_token_file",
            "credential_process",
            "login_session",
            "sso_session",
            "sso_start_url",
            "sso_role_name",
        )
        for position, label in enumerate(("the image profile", "a middle link", "the profile with the keys")):
            for key in keys:
                with self.subTest(position=label, key=key):
                    self.steps.clear()
                    profiles, names = chained_profiles(3)
                    profiles[names[position]][key] = LEAKY_MESSAGE
                    refusal = self.refusal(profiles=profiles, credentials=self.assume_role_credentials())
                    self.assertIn(f"may not name {key}", refusal.detail)
                    if position:
                        self.assertIn(f"source profile {names[position]} ", refusal.detail)
                    for sentinel in SENTINELS:
                        self.assertNotIn(sentinel, refusal.detail)
                    self.assertNotIn("get_credentials", self.steps)

    def test_the_last_profile_of_the_chain_must_hold_both_static_keys(self):
        cases = {
            "no keys": {"region": REGION},
            "an access key only": {"aws_access_key_id": "fake-id"},
            "a secret key only": {"aws_secret_access_key": "fake-pw"},
            "empty keys": {"aws_access_key_id": "", "aws_secret_access_key": ""},
            "no settings": {},
        }
        for label, settings in cases.items():
            with self.subTest(label):
                self.steps.clear()
                profiles, names = chained_profiles(3)
                profiles[names[-1]] = settings
                refusal = self.refusal(profiles=profiles, credentials=self.assume_role_credentials())
                self.assertIn(f"source profile {names[-1]} ends the chain without static keys", refusal.detail)
                self.assertNotIn("get_credentials", self.steps)

    def test_a_link_that_names_a_role_is_followed_as_a_role_even_when_it_holds_keys(self):
        """botocore would stop at the keys; this adapter refuses what it did not check."""
        profiles, names = chained_profiles(3)
        profiles[names[1]].update(STATIC_KEYS)
        profiles[names[-1]] = {"credential_source": "EcsContainer"}
        refusal = self.refusal(profiles=profiles, credentials=self.assume_role_credentials())
        self.assertIn(f"source profile {names[-1]} may not name credential_source", refusal.detail)
        self.steps.clear()
        del profiles[names[1]]["source_profile"]
        refusal = self.refusal(profiles=profiles, credentials=self.assume_role_credentials())
        self.assertIn(f"source profile {names[1]} needs both role_arn and source_profile", refusal.detail)

    def test_credentials_that_do_not_come_from_assume_role_are_refused(self):
        scoped = {"role_arn": ROLE_ARN, "source_profile": SOURCE_PROFILE}
        cases = {
            "none at all": None,
            "the container route": types.SimpleNamespace(method="container-role"),
            "the instance route": types.SimpleNamespace(method="iam-role"),
            "web identity": types.SimpleNamespace(method="assume-role-with-web-identity"),
            "a shared file": types.SimpleNamespace(method="shared-credentials-file"),
            "the environment": types.SimpleNamespace(method="env"),
            "no method at all": object(),
        }
        for label, credentials in cases.items():
            with self.subTest(label):
                self.steps.clear()
                refusal = self.refusal(scoped=scoped, credentials=credentials)
                self.assertIn("assume-role", refusal.detail)

    def test_an_sdk_error_is_refused_by_class_name_and_never_by_its_message(self):
        class ProfileNotFound(Exception):
            pass

        scoped = {"role_arn": ROLE_ARN, "source_profile": SOURCE_PROFILE}
        stages = {
            "the session": dict(scoped=scoped, constructor_error=ProfileNotFound(LEAKY_MESSAGE)),
            "the configuration": dict(scoped=scoped, config_error=ProfileNotFound(LEAKY_MESSAGE)),
            "the credentials": dict(scoped=scoped, credentials_error=ProfileNotFound(LEAKY_MESSAGE)),
        }
        for stage, sdk in stages.items():
            with self.subTest(stage):
                refusal = self.refusal(**sdk)
                self.assertIn("ProfileNotFound", refusal.detail)
                renderings = (
                    str(refusal),
                    repr(refusal),
                    repr(refusal.as_dict()),
                    "".join(traceback.format_exception(refusal)),
                )
                for text in renderings:
                    for sentinel in SENTINELS:
                        self.assertNotIn(sentinel, text)


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

    def test_an_image_read_stops_at_the_deadline_before_any_request(self):
        self.ec2.images_store.append({"ImageId": "ami-0123456789abcdef0", "State": "available", "Tags": []})
        with self.assertRaises(DeadlineRefusal):
            self.image_aws().image("ami-0123456789abcdef0", deadline=NOW)
        self.assertEqual(self.calls, [])

    def test_an_image_read_before_its_deadline_is_made_as_one_without_a_deadline_is(self):
        self.ec2.images_store.append({"ImageId": "ami-0123456789abcdef0", "State": "available", "Tags": []})
        aws = self.image_aws()
        self.assertEqual(aws.image("ami-0123456789abcdef0", deadline=NOW + 1).state, "available")
        self.assertIsNone(aws.image("ami-0000000000000000f", deadline=NOW + 1))
        self.assertEqual(aws.image("ami-0123456789abcdef0").state, "available")
        self.assertEqual(len(self.requests("describe_images")), 3)

    def test_an_unreadable_image_is_still_the_providers_refusal_when_a_deadline_is_given(self):
        self.ec2.errors["describe_images"] = ProviderError("RequestLimitExceeded")
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().image("ami-0123456789abcdef0", deadline=NOW + 1)
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

    def add_group(self, group_id, name, rules=()):
        self.ec2.security_groups_store.append(
            {"GroupId": group_id, "GroupName": name, "IpPermissions": [dict(rule) for rule in rules]}
        )

    def test_security_groups_are_read_by_id_and_by_name_with_their_inbound_rule_counts(self):
        rule = {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22}
        self.add_group("sg-00000000000000001", "example-closed")
        self.add_group("sg-00000000000000002", "example-open", [rule, rule])
        self.add_group("sg-00000000000000003", "example-other")
        aws = self.image_aws()
        found = aws.security_groups(group_ids=["sg-00000000000000002", "sg-00000000000000001"])
        self.assertEqual(
            [(item.group_id, item.name, item.ingress_rules) for item in found],
            [("sg-00000000000000001", "example-closed", 0), ("sg-00000000000000002", "example-open", 2)],
        )
        found = aws.security_groups(group_names=["example-open", "example-missing"])
        self.assertEqual([(item.name, item.ingress_rules) for item in found], [("example-open", 2)])
        self.assertEqual(
            self.requests("describe_security_groups"),
            [
                {"GroupIds": ["sg-00000000000000002", "sg-00000000000000001"], "Filters": None, "NextToken": None},
                {
                    "GroupIds": None,
                    "Filters": [{"Name": "group-name", "Values": ["example-open", "example-missing"]}],
                    "NextToken": None,
                },
            ],
        )

    def test_a_security_group_entry_carries_no_rule_detail(self):
        rule = {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "UserIdGroupPairs": [{"GroupId": "sg-hidden-marker"}]}
        self.add_group("sg-00000000000000001", "example-open", [rule])
        (item,) = self.image_aws().security_groups(group_ids=["sg-00000000000000001"])
        for text in (repr(item), str(item.as_dict())):
            self.assertNotIn("sg-hidden-marker", text)

    def test_reading_no_group_makes_no_call(self):
        self.assertEqual(self.image_aws().security_groups(), [])
        self.assertEqual(self.calls, [])

    def test_a_group_id_the_provider_does_not_know_fails_the_read_and_is_not_an_empty_group(self):
        self.add_group("sg-00000000000000001", "example-closed")
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().security_groups(group_ids=["sg-00000000000000001", "sg-0000000000000000f"])
        self.assertTrue(raised.exception.uncertain)

    def test_an_unreadable_security_group_is_refused_without_the_provider_message(self):
        self.ec2.errors["describe_security_groups"] = ProviderError("UnauthorizedOperation")
        for arguments in ({"group_ids": ["sg-00000000000000001"]}, {"group_names": ["example-closed"]}):
            with self.subTest(arguments=sorted(arguments)):
                with self.assertRaises(ProviderRefusal) as raised:
                    self.image_aws().security_groups(**arguments)
                self.assertTrue(raised.exception.uncertain)
                for sentinel in SENTINELS:
                    self.assertNotIn(sentinel, raised.exception.detail)
                    self.assertNotIn(sentinel, str(raised.exception.as_dict()))

    def test_security_group_reads_stop_at_the_deadline(self):
        self.add_group("sg-00000000000000001", "example-closed")
        for arguments in ({"group_ids": ["sg-00000000000000001"]}, {"group_names": ["example-closed"]}):
            with self.subTest(arguments=sorted(arguments)):
                with self.assertRaises(DeadlineRefusal):
                    self.image_aws().security_groups(deadline=NOW, **arguments)
        self.assertEqual(self.calls, [])

    def test_a_truncated_or_malformed_security_group_answer_is_refused_and_not_read_as_no_ingress(self):
        self.add_group("sg-00000000000000001", "example-closed")
        self.ec2.pages["describe_security_groups"] = True
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().security_groups(group_ids=["sg-00000000000000001"])
        self.assertEqual(raised.exception.reason, "AWS_INVENTORY_INCOMPLETE")
        self.ec2.pages.clear()
        for broken in (
            {"GroupName": "example-closed", "IpPermissions": []},
            {"GroupId": "sg-00000000000000001", "IpPermissions": []},
            {"GroupId": "sg-00000000000000001", "GroupName": "example-closed"},
            {"GroupId": "sg-00000000000000001", "GroupName": "example-closed", "IpPermissions": None},
        ):
            with self.subTest(broken=sorted(broken)):
                self.ec2.security_groups_store[:] = [broken]
                arguments = {"group_ids": [broken["GroupId"]]} if "GroupId" in broken else {"group_names": ["example-closed"]}
                with self.assertRaises(ProviderRefusal) as raised:
                    self.image_aws().security_groups(**arguments)
                self.assertEqual(raised.exception.reason, "AWS_INVENTORY_INCOMPLETE")

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
        self.assertEqual(
            self.requests("delete_object"), [{"Bucket": BUCKET, "Key": self.KEY, "ExpectedBucketOwner": ACCOUNT}]
        )
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
            raise Refusal("IMAGE_SOURCE_FORBIDDEN", LEAKY_MESSAGE)

        with self.assertRaises(Refusal) as raised:
            self.put(stream())
        self.assertEqual(raised.exception.reason, "IMAGE_SOURCE_FORBIDDEN")
        self.assertEqual(len(self.requests("abort_multipart_upload")), 1)
        self.assertEqual(len(self.requests("delete_object")), 1)
        # The reason crosses the boundary; the URL, token and user data the source
        # put in its detail do not.
        refusal = raised.exception
        for text in (str(refusal), repr(refusal), repr(refusal.as_dict()), refusal.detail):
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, text)

    @staticmethod
    def refusing_after_one_chunk(refusal):
        yield b"x" * 15
        raise refusal

    def test_a_refusal_from_the_source_is_rebuilt_with_its_class_its_reason_and_a_fixed_detail(self):
        sources = {
            "plain": Refusal("IMAGE_SOURCE_FORBIDDEN", LEAKY_MESSAGE),
            "uncertain": Refusal("IMAGE_SOURCE_UNREADABLE", LEAKY_MESSAGE, uncertain=True),
            "provider": ProviderRefusal("IMAGE_SOURCE_FORBIDDEN", LEAKY_MESSAGE),
            "uncertain provider": ProviderRefusal("AWS_UNCERTAIN", LEAKY_MESSAGE, uncertain=True),
            "state": StateRefusal("IMAGE_SOURCE_UNREADABLE", LEAKY_MESSAGE),
        }
        for label, original in sources.items():
            with self.subTest(label):
                self.calls.clear()
                with self.assertRaises(Refusal) as raised:
                    self.put(self.refusing_after_one_chunk(original))
                refusal = raised.exception
                self.assertIsNot(refusal, original)
                # A caller that catches the class (a provider refusal, say) still catches it.
                self.assertIs(type(refusal), type(original))
                self.assertEqual(refusal.reason, original.reason)
                self.assertEqual(refusal.uncertain, original.uncertain)
                self.assertEqual(refusal.detail, "archive source refused")
                self.assertEqual(len(self.requests("abort_multipart_upload")), 1)
                self.assertEqual(len(self.requests("delete_object")), 1)
                for text in (str(refusal), repr(refusal), repr(refusal.as_dict()), repr(vars(refusal))):
                    for sentinel in SENTINELS:
                        self.assertNotIn(sentinel, text)

    def test_a_refusal_class_that_cannot_be_rebuilt_from_reason_detail_and_uncertainty_comes_out_plain(self):
        class OneArgument(Refusal):
            def __init__(self, detail):
                super().__init__("IMAGE_SOURCE_ODD", detail)

        class WithRequest(Refusal):
            def __init__(self, reason, detail="", uncertain=False, *, request=None):
                super().__init__(reason, detail, uncertain)
                self.request = request

        class Reordered(Refusal):
            def __init__(self, detail="", uncertain=False, request=None):
                super().__init__("IMAGE_SOURCE_ODD", detail, uncertain)
                self.request = request

        with self.subTest("a constructor with another signature"):
            with self.assertRaises(Refusal) as raised:
                self.put(self.refusing_after_one_chunk(OneArgument(LEAKY_MESSAGE)))
            refusal = raised.exception
            self.assertIs(type(refusal), Refusal)
            fields = (refusal.reason, refusal.detail, refusal.uncertain)
            self.assertEqual(fields, ("IMAGE_SOURCE_ODD", "archive source refused", False))
        with self.subTest("a constructor that takes three arguments in another order"):
            self.calls.clear()
            with self.assertRaises(Refusal) as raised:
                self.put(self.refusing_after_one_chunk(Reordered(LEAKY_MESSAGE, True)))
            refusal = raised.exception
            # Building it from (reason, detail, uncertain) would run, and shuffle the fields.
            self.assertIs(type(refusal), Refusal)
            fields = (refusal.reason, refusal.detail, refusal.uncertain)
            self.assertEqual(fields, ("IMAGE_SOURCE_ODD", "archive source refused", True))
        with self.subTest("an extra attribute is not copied"):
            self.calls.clear()
            original = WithRequest("IMAGE_SOURCE_ODD", LEAKY_MESSAGE, True, request=LEAKY_MESSAGE)
            with self.assertRaises(Refusal) as raised:
                self.put(self.refusing_after_one_chunk(original))
            refusal = raised.exception
            self.assertIs(type(refusal), WithRequest)
            self.assertIsNone(refusal.request)
            fields = (refusal.reason, refusal.detail, refusal.uncertain)
            self.assertEqual(fields, ("IMAGE_SOURCE_ODD", "archive source refused", True))
            for sentinel in SENTINELS:
                self.assertNotIn(sentinel, repr(vars(refusal)))

    def test_a_deadline_refusal_from_the_source_stays_a_deadline_refusal_with_a_fixed_detail(self):
        with self.assertRaises(DeadlineRefusal) as raised:
            self.put(self.refusing_after_one_chunk(DeadlineRefusal(LEAKY_MESSAGE)))
        self.assertEqual(raised.exception.detail, "archive upload")
        self.assertEqual(len(self.requests("abort_multipart_upload")), 1)
        self.assertEqual(len(self.requests("delete_object")), 1)

    def test_unconfirmed_cleanup_is_stated_for_a_refusal_and_for_a_deadline(self):
        def stalled():
            yield b"x" * 5
            self.clock.advance(300)
            yield b"x" * 5

        self.s3.errors["abort_multipart_upload"] = ProviderError("InternalError")
        with self.assertRaises(Refusal) as refused:
            self.put(self.refusing_after_one_chunk(Refusal("IMAGE_SOURCE_FORBIDDEN", LEAKY_MESSAGE)))
        self.assertEqual(refused.exception.detail, "archive source refused; upload cleanup not confirmed")
        with self.assertRaises(DeadlineRefusal) as late:
            self.put(stalled(), deadline=self.clock() + 240)
        self.assertEqual(late.exception.detail, "archive upload; upload cleanup not confirmed")
        # The delete still ran each time although the abort failed.
        self.assertEqual(len(self.requests("delete_object")), 2)

    def slow_part(self, number):
        """Make the upload of one part (counted from 1) take 300 seconds on the test clock."""
        original = self.s3.upload_part

        def upload_part(**arguments):
            if arguments["PartNumber"] == number:
                self.clock.advance(300)
            return original(**arguments)

        self.s3.upload_part = upload_part

    def assert_discarded_and_not_completed(self):
        self.assertNotIn("complete_multipart_upload", self.names())
        self.assertEqual(len(self.requests("abort_multipart_upload")), 1)
        self.assertEqual(len(self.requests("delete_object")), 1)

    def test_a_deadline_that_has_passed_opens_no_upload(self):
        for offset in (-1, 0):
            with self.subTest(offset=offset):
                self.calls.clear()
                with self.assertRaises(DeadlineRefusal):
                    self.put([b"x" * 15], deadline=self.clock() + offset)
                # Not even the create call: there is nothing to abort or delete.
                self.assertEqual(self.names(), [])

    def test_an_only_part_that_crosses_the_deadline_is_not_completed(self):
        self.slow_part(1)
        with self.assertRaises(DeadlineRefusal):
            self.put([b"x" * 5], deadline=self.clock() + 240)
        # The part went up; completing the upload is what is refused.
        self.assertEqual([len(part) for part in self.s3.uploaded], [5])
        self.assert_discarded_and_not_completed()

    def test_a_final_part_that_crosses_the_deadline_is_not_completed(self):
        self.slow_part(2)
        with self.assertRaises(DeadlineRefusal):
            self.put([b"x" * 15], deadline=self.clock() + 240)
        self.assertEqual([len(part) for part in self.s3.uploaded], [10, 5])
        self.assert_discarded_and_not_completed()

    def test_a_stream_that_ends_after_the_deadline_is_not_completed(self):
        def stream():
            yield b"x" * 10  # exactly one part, so nothing is left to send at the end
            self.clock.advance(300)  # the source takes its time to report that it is done

        with self.assertRaises(DeadlineRefusal):
            self.put(stream(), deadline=self.clock() + 240)
        self.assertEqual([len(part) for part in self.s3.uploaded], [10])
        self.assert_discarded_and_not_completed()

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
        self.assertEqual(
            self.requests("delete_object"), [{"Bucket": BUCKET, "Key": self.KEY, "ExpectedBucketOwner": ACCOUNT}]
        )


class StagingOwnerTests(Base):
    """Every S3 call of the adapter names the account of the image identity as the bucket's owner."""

    KEY = PREFIX + "build-0001.tar.gz"
    OTHER = "999988887777"

    def s3_requests(self):
        return [
            (name, arguments)
            for name, arguments in self.calls
            if name
            in (
                "create_multipart_upload",
                "upload_part",
                "complete_multipart_upload",
                "abort_multipart_upload",
                "delete_object",
                "list_objects_v2",
            )
        ]

    def assert_every_call_names_the_account(self, expected):
        calls = self.s3_requests()
        self.assertEqual({name for name, _ in calls}, set(expected))
        for name, arguments in calls:
            with self.subTest(call=name):
                self.assertEqual(arguments["ExpectedBucketOwner"], ACCOUNT)

    def test_an_upload_names_the_account_on_every_call(self):
        self.image_aws().put_archive(BUCKET, self.KEY, [b"x" * 25], max_bytes=100, part_bytes=10)
        self.assert_every_call_names_the_account(
            ["create_multipart_upload", "upload_part", "complete_multipart_upload"]
        )

    def test_the_cleanup_of_an_upload_names_the_account_on_the_abort_and_on_the_delete(self):
        with self.assertRaises(ProviderRefusal):
            self.image_aws().put_archive(BUCKET, self.KEY, [b"x" * 60, b"y" * 60], max_bytes=100, part_bytes=10)
        self.assert_every_call_names_the_account(
            ["create_multipart_upload", "upload_part", "abort_multipart_upload", "delete_object"]
        )

    def test_a_listing_and_a_delete_name_the_account(self):
        self.s3.pages = [{"Contents": []}]
        aws = self.image_aws()
        aws.list_objects(BUCKET, PREFIX)
        aws.delete_object(BUCKET, self.KEY)
        self.assert_every_call_names_the_account(["list_objects_v2", "delete_object"])

    def test_the_account_is_asked_of_the_identity_once_and_every_call_carries_it(self):
        session = FakeSession(self.calls, self.ec2, self.s3, sts=FakeSts(self.calls, account=ACCOUNT))
        with mock.patch.object(aws_module, "_sdk_config", return_value=object()):
            aws = ImageAws(REGION, PROFILE, session_factory=lambda profile: session, clock=self.clock)
            self.s3.pages = [{"Contents": []}]
            aws.list_objects(BUCKET, PREFIX)
            aws.delete_object(BUCKET, self.KEY)
            aws.put_archive(BUCKET, self.KEY, [b"x" * 5], max_bytes=100, part_bytes=10)
        self.assertEqual(self.names().count("get_caller_identity"), 1)
        self.assertLess(self.names().index("get_caller_identity"), self.names().index("list_objects_v2"))
        self.assert_every_call_names_the_account(
            ["list_objects_v2", "delete_object", "create_multipart_upload", "upload_part", "complete_multipart_upload"]
        )

    def test_an_account_that_is_unknown_sends_no_s3_call_and_no_call_goes_out_without_it(self):
        failing = FakeSts(self.calls)
        failing.errors["get_caller_identity"] = ProviderError("AccessDenied")
        malformed = FakeSts(self.calls, account="not-an-account")
        absent = FakeSts(self.calls, account=None)
        operations = {
            "put_archive": lambda aws: aws.put_archive(BUCKET, self.KEY, [b"x"], max_bytes=100),
            "list_objects": lambda aws: aws.list_objects(BUCKET, PREFIX),
            "delete_object": lambda aws: aws.delete_object(BUCKET, self.KEY),
        }
        for label, sts in (("failing", failing), ("malformed", malformed), ("absent", absent)):
            for name, operation in operations.items():
                with self.subTest(sts=label, operation=name):
                    session = FakeSession(self.calls, self.ec2, self.s3, sts=sts)
                    with mock.patch.object(aws_module, "_sdk_config", return_value=object()):
                        aws = ImageAws(REGION, PROFILE, session_factory=lambda profile, s=session: s, clock=self.clock)
                        with self.assertRaises(ProviderRefusal):
                            operation(aws)
        self.assertEqual(self.s3_requests(), [])

    def test_a_bucket_of_another_account_refuses_an_upload_before_anything_is_sent(self):
        self.s3.errors["create_multipart_upload"] = ProviderError("AccessDenied")
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().put_archive(BUCKET, self.KEY, [b"x" * 25], max_bytes=100, part_bytes=10)
        refusal = raised.exception
        self.assertEqual((refusal.reason, refusal.detail, refusal.uncertain), ("IMAGE_STAGING_BUCKET_DENIED", "AccessDenied", False))
        self.assertNotIn(LEAKY_MESSAGE, str(refusal))
        # One refused call: no part, no completion, no cleanup, and no second try without the owner.
        self.assertEqual([name for name, _ in self.s3_requests()], ["create_multipart_upload"])
        self.assertEqual(self.s3.uploaded, [])

    def test_a_bucket_that_changes_owner_between_parts_is_the_same_refusal_and_is_cleaned_up(self):
        self.s3.errors["upload_part"] = ProviderError("AccessDenied")
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().put_archive(BUCKET, self.KEY, [b"x" * 25], max_bytes=100, part_bytes=10)
        self.assertEqual(raised.exception.reason, "IMAGE_STAGING_BUCKET_DENIED")
        self.assertNotIn("complete_multipart_upload", self.names())
        self.assert_every_call_names_the_account(
            ["create_multipart_upload", "upload_part", "abort_multipart_upload", "delete_object"]
        )

    def test_a_delete_and_a_listing_that_the_owner_check_refuses_are_refusals_with_the_same_reason(self):
        self.s3.errors["delete_object"] = ProviderError("AccessDenied")
        with self.assertRaises(ProviderRefusal) as deleted:
            self.image_aws().delete_object(BUCKET, self.KEY)
        self.assertEqual((deleted.exception.reason, deleted.exception.uncertain), ("IMAGE_STAGING_BUCKET_DENIED", False))
        self.s3.errors["list_objects_v2"] = ProviderError("AccessDenied")
        with self.assertRaises(ProviderRefusal) as listed:
            self.image_aws().list_objects(BUCKET, PREFIX)
        self.assertEqual((listed.exception.reason, listed.exception.uncertain), ("IMAGE_STAGING_BUCKET_DENIED", True))
        # Each was asked once, with the owner.
        self.assertEqual([name for name, _ in self.s3_requests()], ["delete_object", "list_objects_v2"])
        self.assert_every_call_names_the_account(["delete_object", "list_objects_v2"])

    def test_an_owner_that_a_caller_gives_is_named_on_every_call_in_place_of_the_account_of_the_identity(self):
        aws = self.image_aws()
        aws.put_archive(BUCKET, self.KEY, [b"x" * 25], max_bytes=100, part_bytes=10, owner=self.OTHER)
        with self.assertRaises(ProviderRefusal):
            aws.put_archive(BUCKET, self.KEY, [b"x" * 60, b"y" * 60], max_bytes=100, part_bytes=10, owner=self.OTHER)
        aws.delete_object(BUCKET, self.KEY, owner=self.OTHER)
        self.s3.pages = [{"Contents": []}]
        aws.list_objects(BUCKET, PREFIX, owner=self.OTHER)
        calls = self.s3_requests()
        self.assertTrue(calls)
        for name, arguments in calls:
            with self.subTest(call=name):
                self.assertEqual(arguments["ExpectedBucketOwner"], self.OTHER)

    def test_an_owner_that_is_no_account_number_sends_no_s3_call(self):
        aws = self.image_aws()
        for owner in ("", "not-an-account", "12345", 123456789012):
            with self.subTest(owner=owner):
                with self.assertRaises(ProviderRefusal):
                    aws.delete_object(BUCKET, self.KEY, owner=owner)
                with self.assertRaises(ProviderRefusal):
                    aws.put_archive(BUCKET, self.KEY, [b"x"], max_bytes=100, owner=owner)
        self.assertEqual(self.s3_requests(), [])

    def test_another_s3_error_keeps_its_own_reason(self):
        self.s3.errors["delete_object"] = ProviderError("SlowDown")
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws().delete_object(BUCKET, self.KEY)
        self.assertNotEqual(raised.exception.reason, "IMAGE_STAGING_BUCKET_DENIED")


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

    def test_the_credential_check_alone_presigns_nothing(self):
        aws = self.image_aws(expiry=NOW + 1800)
        self.assertIsNone(aws.check_credential_life(1800))
        self.assertEqual(self.requests("generate_presigned_url"), [])
        self.assertEqual(self.names(), [])

    def test_the_credential_check_refuses_a_session_that_is_too_short_with_the_code_of_presign(self):
        with self.assertRaises(ProviderRefusal) as raised:
            self.image_aws(expiry=NOW + 1799).check_credential_life(1800)
        self.assertEqual((raised.exception.reason, raised.exception.uncertain), ("IMAGE_CREDENTIAL_SHORT", False))
        self.assertIn("1799 seconds", raised.exception.detail)
        self.assertEqual(self.requests("generate_presigned_url"), [])

    def test_the_credential_check_refuses_an_unknown_expiry(self):
        session = FakeSession(self.calls, self.ec2, self.s3, credentials=FakeCredentials(None))
        aws = ImageAws(REGION, PROFILE, session_factory=lambda profile: session, clients={"s3": self.s3}, clock=self.clock)
        with self.assertRaises(ProviderRefusal) as raised:
            aws.check_credential_life(300)
        self.assertEqual(raised.exception.reason, "IMAGE_CREDENTIAL_SHORT")

    def test_a_refusal_of_the_session_passes_the_credential_check_unchanged(self):
        refusal = ProviderRefusal("IMAGE_PROFILE_INVALID", f"profile {PROFILE}: it does not exist")

        def refusing_session(profile):
            raise refusal

        with mock.patch.object(aws_module, "_sdk_config", return_value=object()):
            aws = ImageAws(REGION, PROFILE, session_factory=refusing_session, clock=self.clock)
            with self.assertRaises(ProviderRefusal) as raised:
                aws.check_credential_life(1800)
        self.assertIs(raised.exception, refusal)

    def test_a_credential_resolution_that_fails_is_a_provider_refusal_with_a_stable_reason(self):
        _, modules = sdk_client_error("AccessDenied")
        exceptions = modules["botocore.exceptions"]

        class Denied(exceptions.ClientError):
            response = {"Error": {"Code": "AccessDenied", "Message": LEAKY_MESSAGE}}

        class NoCredentialsError(exceptions.BotoCoreError):
            """botocore's error for credentials that are missing."""

        def adapter(error):
            credentials = FailingCredentials(error)
            session = FakeSession(self.calls, self.ec2, self.s3, credentials=credentials)
            aws = ImageAws(
                REGION, PROFILE, session_factory=lambda profile: session, clients={"s3": self.s3}, clock=self.clock
            )
            return aws, credentials

        failures = {
            "denied": (Denied(LEAKY_MESSAGE), "AccessDenied"),
            "missing": (NoCredentialsError(LEAKY_MESSAGE), "NoCredentialsError"),
        }
        with mock.patch.dict(sys.modules, modules):
            for label, (error, detail) in failures.items():
                aws, credentials = adapter(error)
                calls = {
                    "credential_expiry": aws.credential_expiry,
                    "presign_get": lambda aws=aws: aws.presign_get(BUCKET, self.KEY, 1800),
                }
                for name, call in calls.items():
                    with self.subTest(label=label, call=name):
                        with self.assertRaises(ProviderRefusal) as raised:
                            call()
                        refusal = raised.exception
                        self.assertEqual(
                            (refusal.reason, refusal.detail, refusal.uncertain), ("AWS_UNCERTAIN", detail, True)
                        )
                # Both calls resolved the credentials, and neither went on to sign a URL.
                self.assertEqual(credentials.resolved, 2)
        self.assertEqual(self.requests("generate_presigned_url"), [])


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

    def test_a_credential_failure_keeps_the_provider_text_out_of_every_rendering(self):
        _, modules = sdk_client_error("AccessDenied")
        exceptions = modules["botocore.exceptions"]

        class Denied(exceptions.ClientError):
            response = {"Error": {"Code": "AccessDenied", "Message": LEAKY_MESSAGE}}

        class NoCredentialsError(exceptions.BotoCoreError):
            """botocore's error for credentials that are missing."""

        failures = (Denied(LEAKY_MESSAGE), NoCredentialsError(LEAKY_MESSAGE), RuntimeError(LEAKY_MESSAGE))
        for label, patched in (("sdk", modules), ("no sdk", {"botocore": None, "botocore.exceptions": None})):
            for error in failures:
                session = FakeSession(self.calls, self.ec2, self.s3, credentials=FailingCredentials(error))
                aws = ImageAws(
                    REGION,
                    PROFILE,
                    session_factory=lambda profile, s=session: s,
                    clients={"s3": self.s3},
                    clock=self.clock,
                )
                with self.subTest(label=label, error=type(error).__name__):
                    with mock.patch.dict(sys.modules, patched), Capture() as capture:
                        capture.run(lambda aws=aws: aws.presign_get(BUCKET, PREFIX + "x.tar.gz", 1800))
                    # Six surfaces mean a provider refusal came out, not the SDK's own error.
                    self.assertEqual(len(capture.rendered), 6)
                    self.assert_clean(capture.surfaces(), f"{label} {type(error).__name__}")

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

    def test_a_refusal_from_the_source_keeps_its_detail_out_of_every_rendering(self):
        def stream():
            yield b"x" * 15
            raise ProviderRefusal("IMAGE_SOURCE_FORBIDDEN", LEAKY_MESSAGE)

        aws = self.image_aws()
        with Capture() as capture:
            capture.run(lambda: aws.put_archive(BUCKET, PREFIX + "x.tar.gz", stream(), max_bytes=100, part_bytes=10))
        self.assertEqual(len(capture.rendered), 6)
        self.assert_clean(capture.surfaces(), "source refusal")

    def test_the_adapter_does_not_log_at_all(self):
        """No print, no record and no handler: ``logging`` is reached only to set a level.

        The adapter imports ``logging`` to keep the SDK's own loggers at INFO or
        above, so a search for the word cannot tell that from logging. This reads
        the calls instead.
        """

        def called_names(tree):
            return {
                node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
            }

        def emitting(names):
            speaking = {
                "print",
                "debug",
                "info",
                "warning",
                "warn",
                "error",
                "exception",
                "critical",
                "fatal",
                "log",
                "emit",
                "handle",
                "makeRecord",
                "basicConfig",
                "addHandler",
            }
            return {name for name in names if name in speaking or str(name).endswith("Handler")}

        def calls_on_logging(tree):
            return {
                node.func.attr
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logging"
            }

        tree = ast.parse(Path(aws_module.__file__).read_text(encoding="utf-8"))
        self.assertEqual(emitting(called_names(tree)), set())
        # The module names ``logging`` only as ``logging.<name>`` and calls nothing on it but ``getLogger``.
        names_imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "logging"
            for alias in node.names
        ]
        self.assertEqual(names_imported, [])
        self.assertLessEqual(calls_on_logging(tree), {"getLogger"})
        # The walks can find what they forbid.
        probe = ast.parse("logger.debug('x')\nprint('x')\nlogging.StreamHandler()\nhandler.emit(record)")
        self.assertEqual(emitting(called_names(probe)), {"debug", "print", "StreamHandler", "emit"})
        on_logging = ast.parse("logging.warning('x')\nlogging.getLogger('x')")
        self.assertEqual(calls_on_logging(on_logging), {"warning", "getLogger"})

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


class SdkLoggerGuardTests(unittest.TestCase):
    """The SDK's loggers stay at INFO or above once an image session exists."""

    def setUp(self):
        for logger in (logging.getLogger(), *map(logging.getLogger, SDK_LOGGER_NAMES)):
            self.addCleanup(logger.setLevel, logger.level)

    def levels(self):
        return {name: logging.getLogger(name).level for name in SDK_LOGGER_NAMES}

    def test_an_unset_or_verbose_sdk_logger_is_raised_to_info(self):
        logging.getLogger().setLevel(logging.DEBUG)
        for level in (logging.NOTSET, logging.DEBUG):
            with self.subTest(level=level):
                for name in SDK_LOGGER_NAMES:
                    logging.getLogger(name).setLevel(level)
                ImageAws(REGION, PROFILE)
                self.assertEqual(self.levels(), dict.fromkeys(SDK_LOGGER_NAMES, logging.INFO))

    def test_an_unset_logger_under_a_quieter_root_is_not_made_more_verbose(self):
        logging.getLogger().setLevel(logging.ERROR)
        for name in SDK_LOGGER_NAMES:
            logging.getLogger(name).setLevel(logging.NOTSET)
        ImageAws(REGION, PROFILE)
        self.assertEqual(self.levels(), dict.fromkeys(SDK_LOGGER_NAMES, logging.ERROR))

    def test_a_logger_that_is_already_quieter_is_left_alone(self):
        wanted = {"botocore": logging.ERROR, "boto3": logging.WARNING, "urllib3": logging.INFO}
        for name, level in wanted.items():
            logging.getLogger(name).setLevel(level)
        ImageAws(REGION, PROFILE)
        self.assertEqual(self.levels(), wanted)

    def test_a_root_level_set_after_the_guard_does_not_reopen_the_sdk_or_its_child_loggers(self):
        children = (
            "botocore.endpoint",
            "botocore.auth",
            "botocore.parsers",
            "botocore.credentials",
            "urllib3.connectionpool",
            "boto3.resources.action",
        )
        for name in SDK_LOGGER_NAMES:
            logging.getLogger(name).setLevel(logging.NOTSET)
        logging.getLogger().setLevel(logging.WARNING)
        ImageAws(REGION, PROFILE)
        logging.getLogger().setLevel(logging.DEBUG)
        for child in children:
            with self.subTest(child):
                self.assertFalse(logging.getLogger(child).isEnabledFor(logging.DEBUG))

    def test_the_guard_covers_a_session_or_client_that_is_injected(self):
        constructions = {
            "default": {},
            "clients": {"clients": {"ec2": object(), "s3": object()}},
            "session factory": {"session_factory": lambda profile: object()},
        }
        for label, arguments in constructions.items():
            with self.subTest(label):
                logging.getLogger().setLevel(logging.DEBUG)
                for name in SDK_LOGGER_NAMES:
                    logging.getLogger(name).setLevel(logging.NOTSET)
                ImageAws(REGION, PROFILE, **arguments)
                self.assertEqual(self.levels(), dict.fromkeys(SDK_LOGGER_NAMES, logging.INFO))

    def level(self, name, level):
        """Give the logger ``name`` this level for the rest of the test, then put its old level back."""
        logger = logging.getLogger(name)
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(level)
        return logger

    def test_an_explicit_debug_level_on_an_sdk_descendant_is_raised_to_info(self):
        names = (
            "botocore.endpoint",
            "botocore.parsers",
            "botocore.endpoint.pool",
            "urllib3.connectionpool",
            "boto3.resources.action",
        )
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        # The three top-level loggers are where the guard used to stop.
        for name in SDK_LOGGER_NAMES:
            logging.getLogger(name).setLevel(logging.INFO)
        loggers = [self.level(name, logging.DEBUG) for name in names]
        records = LogRecords()
        root.addHandler(records)
        self.addCleanup(root.removeHandler, records)

        def spoken():
            records.records.clear()
            for logger in loggers:
                logger.debug("debug of %s", logger.name)
                logger.info("info of %s", logger.name)
            return [message for _, _, message in records.records]

        # A level of its own wins over every ancestor, so each descendant still speaks at DEBUG.
        self.assertEqual(len(spoken()), 2 * len(names))
        ImageAws(REGION, PROFILE)
        self.assertEqual(spoken(), [f"info of {name}" for name in names])
        self.assertEqual([logger.level for logger in loggers], [logging.INFO] * len(names))

    def test_a_descendant_that_is_unset_or_already_quieter_is_left_alone(self):
        wanted = {
            "botocore.endpoint": logging.NOTSET,
            "botocore.auth": logging.WARNING,
            "urllib3.connectionpool": logging.ERROR,
            "boto3.resources.action": logging.INFO,
        }
        logging.getLogger().setLevel(logging.DEBUG)
        for name in SDK_LOGGER_NAMES:
            logging.getLogger(name).setLevel(logging.NOTSET)
        for name, level in wanted.items():
            self.level(name, level)
        ImageAws(REGION, PROFILE)
        self.assertEqual({name: logging.getLogger(name).level for name in wanted}, wanted)
        # An unset descendant has no level of its own, so it follows its parent, which the guard raised.
        self.assertEqual(logging.getLogger("botocore.endpoint").getEffectiveLevel(), logging.INFO)

    def test_a_logger_that_only_looks_like_an_sdk_logger_is_left_alone(self):
        names = (
            "botocore2",
            "botocore2.endpoint",
            "boto3x.resources",
            "urllib3_extras",
            "myapp.botocore",
            "myapp.urllib3",
        )
        for name in names:
            self.level(name, logging.DEBUG)
        ImageAws(REGION, PROFILE)
        for name in names:
            with self.subTest(name):
                self.assertEqual(logging.getLogger(name).level, logging.DEBUG)

    def test_a_logger_between_two_sdk_loggers_that_was_never_created_is_skipped(self):
        leaf = self.level("botocore.ci_burst_probe.leaf", logging.DEBUG)
        # Python keeps a placeholder, which has no level, where an ancestor was never created.
        self.assertIsInstance(logging.Logger.manager.loggerDict["botocore.ci_burst_probe"], logging.PlaceHolder)
        ImageAws(REGION, PROFILE)
        self.assertEqual(leaf.level, logging.INFO)
        self.assertIsInstance(logging.Logger.manager.loggerDict["botocore.ci_burst_probe"], logging.PlaceHolder)

    def test_the_credential_logger_gets_one_filter_however_often_an_image_session_is_built(self):
        logger = without_filters(self, "botocore.credentials")
        ImageAws(REGION, PROFILE)
        ImageAws(REGION, PROFILE, session_factory=lambda profile: object())
        self.assertEqual(len(logger.filters), 1)
        self.assertIsInstance(logger.filters[0], logging.Filter)

    def test_a_record_leaves_the_credential_filter_with_its_message_and_without_its_traceback(self):
        logger = without_filters(self, "botocore.credentials")
        ImageAws(REGION, PROFILE)
        try:
            raise ProviderError("AccessDenied")
        except ProviderError:
            exc_info = sys.exc_info()
        record = logging.LogRecord(
            logger.name,
            logging.WARNING,
            __file__,
            1,
            "refresh failed during %s",
            ("mandatory",),
            exc_info,
            sinfo=f"stack {LEAKY_MESSAGE}",
        )
        # A handler that formatted the record first leaves its text on the record.
        record.exc_text = f"Traceback {LEAKY_MESSAGE}"
        # Python 3.12 returns the record from ``filter``, an earlier Python returns True.
        self.assertTrue(logger.filter(record))
        self.assertEqual((record.exc_info, record.exc_text, record.stack_info), (None, None, None))
        self.assertEqual((record.levelno, record.getMessage()), (logging.WARNING, "refresh failed during mandatory"))

    def test_the_warning_for_a_failed_credential_refresh_keeps_its_line_and_loses_the_provider_text(self):
        logger = without_filters(self, "botocore.credentials")
        self.level("botocore.credentials", logging.WARNING)
        logged = LoggedText()
        logger.addHandler(logged)
        self.addCleanup(logger.removeHandler, logged)
        message = "Refreshing temporary credentials failed during %s refresh period."

        def fail_to_refresh():
            """Log what the SDK logs when it cannot refresh, and return the one text a handler wrote."""
            logged.entries.clear()
            try:
                raise ProviderError("AccessDenied")
            except ProviderError:
                logger.warning(message, "mandatory", exc_info=True)
            ((_, _, text),) = logged.entries
            return text

        # Control: as the SDK leaves it, the record carries the traceback and the provider's message.
        control = fail_to_refresh()
        self.assertIn("Traceback", control)
        self.assertIn(SENTINEL_TOKEN, control)
        ImageAws(REGION, PROFILE)
        self.assertEqual(fail_to_refresh(), "WARNING botocore.credentials: " + message % "mandatory")


# -- the real SDK -------------------------------------------------------------
#
# Everything below needs boto3 and is skipped without it. It makes no network
# call: the configuration is a temporary file, the one HTTP route the SDK could
# take is a loopback endpoint that records its visitors, and requests that would
# leave the process are answered by a stub on botocore's ``before-send`` event.

LAUNCH_RESPONSE = (
    b'<RunInstancesResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
    b"<requestId>request-0001</requestId><reservationId>r-0123456789abcdef0</reservationId>"
    b"<instancesSet><item><instanceId>i-0123456789abcdef0</instanceId></item></instancesSet>"
    b"</RunInstancesResponse>"
)
ASSUME_ROLE_SETTINGS = {"role_arn": ROLE_ARN, "source_profile": SOURCE_PROFILE, "region": REGION}
SOURCE_CREDENTIALS = f"[{SOURCE_PROFILE}]\n" + "".join(f"{key} = {value}\n" for key, value in STATIC_KEYS.items())
BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{16,}={0,2}")


def assume_role_response():
    identifier, credential, session_marker = ASSUMED_MARKERS
    return (
        '<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><AssumeRoleResult><Credentials>'
        f"<AccessKeyId>{identifier}</AccessKeyId><SecretAccessKey>{credential}</SecretAccessKey>"
        f"<SessionToken>{session_marker}</SessionToken><Expiration>2999-01-01T00:00:00Z</Expiration></Credentials>"
        "<AssumedRoleUser><AssumedRoleId>role-0001:session-0001</AssumedRoleId>"
        "<Arn>arn:aws:sts::123456789012:assumed-role/example-image-build-role/session-0001</Arn></AssumedRoleUser>"
        "</AssumeRoleResult><ResponseMetadata><RequestId>request-0002</RequestId></ResponseMetadata>"
        "</AssumeRoleResponse>"
    ).encode()


#: What STS answers when the caller may not assume the role, with a message that carries the sentinels.
DENIED_RESPONSE = (
    '<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/"><Error><Type>Sender</Type>'
    f"<Code>AccessDenied</Code><Message>{LEAKY_MESSAGE}</Message></Error>"
    "<RequestId>request-0003</RequestId></ErrorResponse>"
).encode()


def profile_config(name, **settings):
    lines = [f"[profile {name}]", *(f"{key} = {value}" for key, value in settings.items())]
    return "\n".join(lines) + "\n"


def profile_configs(profiles):
    """One configuration file holding every profile of ``profiles`` (name -> settings)."""
    return "\n".join(profile_config(name, **settings) for name, settings in profiles.items())


def markers_in(text, markers):
    """The markers found in ``text``, raw or inside any base64 run of it."""
    decoded = []
    for run in BASE64_RUN.findall(text):
        unpadded = run.rstrip("=")
        with contextlib.suppress(ValueError):  # a run that is not base64 at all
            decoded.append(base64.b64decode(unpadded + "=" * (-len(unpadded) % 4)).decode("utf-8", "replace"))
    return [marker for marker in markers if marker in text or any(marker in part for part in decoded)]


class RawBody(io.BytesIO):
    """The raw half of a botocore ``AWSResponse``, which botocore reads through ``stream()``."""

    def stream(self, *args, **kwargs):
        yield self.read()


class AmbientCredentials:
    """A loopback container-credentials endpoint: the ambient identity that must not be used."""

    def __init__(self):
        self.hits = []
        ambient = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                ambient.hits.append(self.path)
                body = json.dumps(
                    {
                        "AccessKeyId": "ambient-id",
                        "SecretAccessKey": "ambient-pw",
                        "Token": "ambient-tk",
                        "Expiration": "2999-01-01T00:00:00Z",
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                """Keep request lines out of the test output."""

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/ambient"

    def __enter__(self):
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
        return False


class LogRecords(logging.Handler):
    """Every record that reaches the root logger, with its logger name and level."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append((record.name, record.levelno, record.getMessage()))

    def text(self):
        return "\n".join(message for _, _, message in self.records)

    def sdk_debug(self):
        return [
            name for name, level, _ in self.records if level < logging.INFO and name.split(".")[0] in SDK_LOGGER_NAMES
        ]


class LoggedText(logging.Handler):
    """What a handler with a plain ``Formatter`` writes for each record it is given, traceback included.

    ``LogRecords`` keeps only the message, which is why a traceback needs this one.
    """

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        self.entries = []

    def emit(self, record):
        self.entries.append((record.name, record.levelno, self.format(record)))

    def warnings_from(self, name):
        return [text for logger, level, text in self.entries if logger == name and level == logging.WARNING]


def without_filters(case, name):
    """The logger ``name`` with no filter on it until the test ends, when its filters come back.

    Building an ``ImageAws`` leaves a filter on the SDK's credential logger for the life of the
    process, so a test that wants to see the filter added, or the record before it, starts here.
    """
    logger = logging.getLogger(name)
    case.addCleanup(setattr, logger, "filters", list(logger.filters))
    logger.filters.clear()
    return logger


class RealSdkCase(unittest.TestCase):
    """The real boto3 and botocore on temporary files, with nothing else of the environment."""

    def use_files(self, config, credentials="", **environment):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ci-burst-sdk-")))
        (directory / "config").write_text(config, encoding="utf-8")
        (directory / "credentials").write_text(credentials, encoding="utf-8")
        values = {
            "AWS_CONFIG_FILE": str(directory / "config"),
            "AWS_SHARED_CREDENTIALS_FILE": str(directory / "credentials"),
            "AWS_EC2_METADATA_DISABLED": "true",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
            "HOME": str(directory),
        }
        values.update(environment)
        # No other AWS_* or proxy variable of this process survives into the SDK.
        self.enterContext(mock.patch.dict(os.environ, values, clear=True))

    def forbid_transport(self):
        """Record, and refuse, every request that reaches the SDK's HTTP layer."""
        sent = []

        def send(session, request):
            sent.append(request.url)
            raise AssertionError("the SDK reached for the network")

        self.enterContext(mock.patch.object(botocore.httpsession.URLLib3Session, "send", send))
        return sent


@unittest.skipIf(boto3 is None, REAL_SDK_REASON)
class RealSdkProfileTests(RealSdkCase):
    """The default session factory against the real botocore, with no network call.

    Without the check, the route botocore would take is the loopback endpoint
    here, so a refusal is proved by that endpoint never being asked.
    """

    def ambient(self, config, credentials=""):
        endpoint = self.enterContext(AmbientCredentials())
        self.use_files(config, credentials, AWS_CONTAINER_CREDENTIALS_FULL_URI=endpoint.url)
        return endpoint

    def assert_refused(self):
        with self.assertRaises(ProviderRefusal) as raised:
            ImageAws(REGION, PROFILE)._session()
        self.assertEqual(raised.exception.reason, "IMAGE_PROFILE_INVALID")
        self.assertIn(PROFILE, raised.exception.detail)
        return raised.exception

    def test_a_profile_with_no_credential_route_does_not_fall_back_to_the_ambient_identity(self):
        endpoint = self.ambient(profile_config(PROFILE, region=REGION))
        # Control: the session this adapter used to build resolves the ambient identity.
        plain = boto3.Session(profile_name=PROFILE).get_credentials()
        self.assertEqual(plain.method, "container-role")
        self.assertEqual(len(endpoint.hits), 1)
        endpoint.hits.clear()
        sent = self.forbid_transport()
        self.assert_refused()
        self.assertEqual(endpoint.hits, [])
        self.assertEqual(sent, [])

    def test_a_profile_that_takes_its_root_credentials_from_a_credential_source_is_refused_unresolved(self):
        config = profile_config(PROFILE, role_arn=ROLE_ARN, credential_source="EcsContainer", region=REGION)
        endpoint = self.ambient(config)
        # Control: the method alone would pass this profile, and resolving it asks the ambient endpoint at once.
        plain = boto3.Session(profile_name=PROFILE).get_credentials()
        self.assertEqual(plain.method, aws_module.ASSUME_ROLE_METHOD)
        self.assertEqual(len(endpoint.hits), 1)
        endpoint.hits.clear()
        sent = self.forbid_transport()
        self.assert_refused()
        self.assertEqual(endpoint.hits, [])
        self.assertEqual(sent, [])

    def test_a_credential_source_beside_a_source_profile_is_read_from_the_real_configuration(self):
        config = profile_config(PROFILE, **ASSUME_ROLE_SETTINGS, credential_source="EcsContainer")
        self.ambient(config, SOURCE_CREDENTIALS)
        self.assertIn("it may not name credential_source", self.assert_refused().detail)

    def test_a_role_that_assumes_through_a_web_identity_file_is_refused(self):
        config = profile_config(PROFILE, **ASSUME_ROLE_SETTINGS, web_identity_token_file="/nonexistent/token")
        endpoint = self.ambient(config, SOURCE_CREDENTIALS)
        # Control: botocore resolves this profile through web identity, not through the assume-role route.
        plain = boto3.Session(profile_name=PROFILE).get_credentials()
        self.assertEqual(plain.method, "assume-role-with-web-identity")
        sent = self.forbid_transport()
        refusal = self.assert_refused()
        self.assertIn("it may not name web_identity_token_file", refusal.detail)
        self.assertEqual(endpoint.hits, [])
        self.assertEqual(sent, [])

    def test_a_credential_source_further_along_the_chain_is_refused_unresolved(self):
        config = profile_configs(
            {
                PROFILE: {"role_arn": ROLE_ARN, "source_profile": LINK_PROFILE, "region": REGION},
                LINK_PROFILE: {"role_arn": INTERMEDIATE_ROLE_ARN, "credential_source": "EcsContainer"},
            }
        )
        endpoint = self.ambient(config)
        # Control: botocore reports the assume-role method for this chain, so the method alone
        # passes it, and resolving it asks the ambient endpoint at once.
        plain = boto3.Session(profile_name=PROFILE).get_credentials()
        self.assertEqual(plain.method, aws_module.ASSUME_ROLE_METHOD)
        self.assertEqual(len(endpoint.hits), 1)
        endpoint.hits.clear()
        sent = self.forbid_transport()
        refusal = self.assert_refused()
        self.assertIn(f"source profile {LINK_PROFILE} may not name credential_source", refusal.detail)
        self.assertEqual(endpoint.hits, [])
        self.assertEqual(sent, [])

    def test_a_credential_process_further_along_the_chain_is_refused_and_never_run(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="ci-burst-process-")))
        marker = directory / "ran"
        script = directory / "credential_process.py"
        script.write_text(
            "import json, pathlib\n"
            f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
            "print(json.dumps({'Version': 1, 'AccessKeyId': 'fake-id', 'SecretAccessKey': 'fake-pw'}))\n",
            encoding="utf-8",
        )
        config = profile_configs(
            {
                PROFILE: {"role_arn": ROLE_ARN, "source_profile": LINK_PROFILE, "region": REGION},
                LINK_PROFILE: {"credential_process": shlex.join([sys.executable, str(script)])},
            }
        )
        self.use_files(config)
        # Control: resolving the chain as botocore does runs the command.
        boto3.Session(profile_name=PROFILE).get_credentials()
        self.assertTrue(marker.exists())
        marker.unlink()
        sent = self.forbid_transport()
        refusal = self.assert_refused()
        self.assertIn(f"source profile {LINK_PROFILE} may not name credential_process", refusal.detail)
        self.assertFalse(marker.exists())
        self.assertEqual(sent, [])

    def test_a_source_profile_without_keys_is_refused_and_the_ambient_identity_is_not_asked(self):
        config = profile_configs(
            {
                PROFILE: {"role_arn": ROLE_ARN, "source_profile": LINK_PROFILE, "region": REGION},
                LINK_PROFILE: {"region": REGION},
            }
        )
        endpoint = self.ambient(config)
        # Control: botocore itself does not fall back to the container route for a source profile.
        with self.assertRaises(botocore.exceptions.InvalidConfigError):
            boto3.Session(profile_name=PROFILE).get_credentials()
        self.assertEqual(endpoint.hits, [])
        sent = self.forbid_transport()
        refusal = self.assert_refused()
        self.assertIn(f"source profile {LINK_PROFILE} ends the chain without static keys", refusal.detail)
        self.assertEqual(endpoint.hits, [])
        self.assertEqual(sent, [])

    def test_a_chain_of_roles_with_static_keys_in_the_config_file_is_accepted_without_any_network_call(self):
        config = profile_configs(
            {
                PROFILE: {"role_arn": ROLE_ARN, "source_profile": LINK_PROFILE, "region": REGION},
                LINK_PROFILE: {"role_arn": INTERMEDIATE_ROLE_ARN, "source_profile": SOURCE_PROFILE},
                SOURCE_PROFILE: STATIC_KEYS,
            }
        )
        endpoint = self.ambient(config)
        sent = self.forbid_transport()
        aws = ImageAws(REGION, PROFILE)
        session = aws._session()
        self.assertIsInstance(session, boto3.Session)
        self.assertEqual(session.get_credentials().method, aws_module.ASSUME_ROLE_METHOD)
        self.assertEqual(aws._client("ec2").meta.region_name, REGION)
        # Each role in the chain is assumed on first use, not before.
        self.assertEqual(sent, [])
        self.assertEqual(endpoint.hits, [])

    def test_a_role_with_static_source_keys_is_accepted_without_any_network_call(self):
        endpoint = self.ambient(profile_config(PROFILE, **ASSUME_ROLE_SETTINGS), SOURCE_CREDENTIALS)
        sent = self.forbid_transport()
        aws = ImageAws(REGION, PROFILE)
        session = aws._session()
        self.assertIsInstance(session, boto3.Session)
        self.assertEqual(session.profile_name, PROFILE)
        self.assertEqual(session.get_credentials().method, aws_module.ASSUME_ROLE_METHOD)
        # Both clients build on that session; the role is assumed on first use, not before.
        self.assertEqual(aws._client("ec2").meta.region_name, REGION)
        self.assertEqual(aws._client("s3").meta.region_name, REGION)
        self.assertEqual(sent, [])
        self.assertEqual(endpoint.hits, [])

    def test_a_profile_that_is_in_neither_file_is_refused(self):
        self.use_files(profile_config("another-profile", region=REGION))
        self.assertIn("it does not exist", self.assert_refused().detail)

    def test_the_assume_role_method_name_is_the_installed_botocores(self):
        self.assertEqual(botocore.credentials.AssumeRoleProvider.METHOD, aws_module.ASSUME_ROLE_METHOD)

    def test_the_providers_that_can_answer_for_a_profile_in_the_chain_are_covered_by_the_denied_settings(self):
        """A botocore that adds a provider fails here, so that the denied settings are read again.

        Each provider is listed with the setting that selects it. The shared-credentials and config
        providers read the static keys the chain must end in; every other setting is refused.
        """
        selected_by = {
            "AssumeRoleWithWebIdentityProvider": "web_identity_token_file",
            "SSOProvider": "sso_session",
            "SharedCredentialProvider": None,
            "LoginProvider": "login_session",
            "ProcessProvider": "credential_process",
            "ConfigProvider": None,
        }
        self.use_files("")
        builder = botocore.credentials.ProfileProviderBuilder(botocore.session.Session())
        installed = {type(provider).__name__ for provider in builder.providers("example-profile")}
        self.assertLessEqual(installed, set(selected_by))
        for provider, setting in selected_by.items():
            if setting is None:
                continue
            with self.subTest(provider):
                config = profile_configs(
                    {
                        PROFILE: {"role_arn": ROLE_ARN, "source_profile": LINK_PROFILE, "region": REGION},
                        LINK_PROFILE: {**STATIC_KEYS, setting: "example-value"},
                    }
                )
                self.use_files(config)
                self.assertIn(f"may not name {setting}", self.assert_refused().detail)


@unittest.skipIf(boto3 is None, REAL_SDK_REASON)
class RealSdkLoggingTests(RealSdkCase):
    """The SDK's DEBUG records print requests and credentials, so the adapter silences them.

    Each test first shows the leak with the SDK loggers as botocore leaves them,
    so a clean second half is not a capture that sees nothing. Two descendants
    have a level of their own, as a debugging session leaves them, and a level of
    its own wins over every ancestor: a second control shows that raising only the
    three top-level loggers leaves those two speaking.
    """

    #: SDK loggers below the top three that carry an explicit DEBUG level.
    LEVELLED = ("botocore.endpoint", "botocore.parsers")

    def setUp(self):
        root = logging.getLogger()
        for logger in (root, *map(logging.getLogger, (*SDK_LOGGER_NAMES, *self.LEVELLED))):
            self.addCleanup(logger.setLevel, logger.level)
        for name in SDK_LOGGER_NAMES:
            logging.getLogger(name).setLevel(logging.NOTSET)
        for name in self.LEVELLED:
            logging.getLogger(name).setLevel(logging.DEBUG)
        self.records = LogRecords()
        root.addHandler(self.records)
        self.addCleanup(root.removeHandler, self.records)
        root.setLevel(logging.DEBUG)

    def set_the_top_level_loggers(self, level):
        for name in SDK_LOGGER_NAMES:
            logging.getLogger(name).setLevel(level)

    def assert_only_the_levelled_loggers_speak(self):
        self.assertTrue(self.records.sdk_debug())
        self.assertLessEqual(set(self.records.sdk_debug()), set(self.LEVELLED))

    def stub(self, request, body, calls):
        """Answer a request with ``body``, noting its form fields in ``calls``."""
        sent = request.body.decode() if isinstance(request.body, bytes) else request.body
        calls.append(urllib.parse.parse_qs(sent))
        return AWSResponse(request.url, 200, {}, RawBody(body))

    def test_a_builder_launch_through_the_real_client_logs_no_user_data_or_token(self):
        self.use_files(profile_config("default", region=REGION))
        session = boto3.Session(aws_access_key_id="fake-id", aws_secret_access_key="fake-pw", region_name=REGION)
        requests = []
        session.events.register(
            "before-send.ec2.RunInstances", lambda request, **kwargs: self.stub(request, LAUNCH_RESPONSE, requests)
        )
        markers = (*SENTINELS, SENTINEL_CLIENT_TOKEN)
        user_data_text = base64.b64encode(LEAKY_MESSAGE.encode()).decode()
        template = {"LaunchTemplateName": "example-ci-burst-image-builder", "Version": "2"}
        launch = {
            "LaunchTemplate": template,
            "MinCount": 1,
            "MaxCount": 1,
            "ClientToken": SENTINEL_CLIENT_TOKEN,
            "UserData": LEAKY_MESSAGE,
        }

        # Control: a plain client logs the whole request, encoded user data and token included.
        session.client("ec2", region_name=REGION).run_instances(**launch)
        self.assertIn(user_data_text, self.records.text())
        self.assertCountEqual(markers_in(self.records.text(), markers), markers)

        # Control: with only the top-level loggers raised, the request still reaches the log
        # through the two descendants that have a level of their own, and through nothing else.
        self.set_the_top_level_loggers(logging.INFO)
        self.records.records.clear()
        session.client("ec2", region_name=REGION).run_instances(**launch)
        self.assertCountEqual(markers_in(self.records.text(), markers), markers)
        self.assert_only_the_levelled_loggers_speak()
        # The adapter starts from loggers that are unset, so it has to raise those three itself.
        self.set_the_top_level_loggers(logging.NOTSET)

        self.records.records.clear()
        requests.clear()
        aws = ImageAws(REGION, PROFILE, session_factory=lambda profile: session)
        instance_id = aws.run_builder(
            template_name=template["LaunchTemplateName"],
            template_version=2,
            deployment=DEPLOYMENT,
            build_id="build-0001",
            expires_at=NOW + 2700,
            user_data=LEAKY_MESSAGE,
            client_token=SENTINEL_CLIENT_TOKEN,
        )
        self.assertEqual(instance_id, "i-0123456789abcdef0")
        # The request really travelled through the real client and carried both values.
        (request,) = requests
        self.assertEqual(request["UserData"], [user_data_text])
        self.assertEqual(request["ClientToken"], [SENTINEL_CLIENT_TOKEN])
        self.assertEqual(markers_in(self.records.text(), markers), [])
        self.assertEqual(self.records.sdk_debug(), [])

    def test_assumed_role_credentials_are_not_logged_when_they_are_resolved(self):
        self.use_files(profile_config(PROFILE, **ASSUME_ROLE_SETTINGS), SOURCE_CREDENTIALS)
        requests = []

        def sts(request, **kwargs):
            return self.stub(request, assume_role_response(), requests)

        # Control: a plain session logs the response that holds the assumed credentials.
        plain = botocore.session.Session(profile=PROFILE)
        plain.register("before-send.sts.AssumeRole", sts)
        plain.get_credentials().get_frozen_credentials()
        self.assertCountEqual(markers_in(self.records.text(), ASSUMED_MARKERS), ASSUMED_MARKERS)

        # Control: with only the top-level loggers raised, the response still reaches the log
        # through the descendants that have a level of their own, and through nothing else.
        self.set_the_top_level_loggers(logging.INFO)
        self.records.records.clear()
        again = botocore.session.Session(profile=PROFILE)
        again.register("before-send.sts.AssumeRole", sts)
        again.get_credentials().get_frozen_credentials()
        self.assertCountEqual(markers_in(self.records.text(), ASSUMED_MARKERS), ASSUMED_MARKERS)
        self.assert_only_the_levelled_loggers_speak()
        # The adapter starts from loggers that are unset, so it has to raise those three itself.
        self.set_the_top_level_loggers(logging.NOTSET)

        self.records.records.clear()
        requests.clear()
        aws = ImageAws(REGION, PROFILE)
        aws._session().events.register("before-send.sts.AssumeRole", sts)
        self.assertIsNotNone(aws.credential_expiry())
        # The role really was assumed, through the stub.
        self.assertEqual([call["Action"] for call in requests], [["AssumeRole"]])
        self.assertEqual(markers_in(self.records.text(), ASSUMED_MARKERS), [])
        self.assertEqual(self.records.sdk_debug(), [])


@unittest.skipIf(boto3 is None, REAL_SDK_REASON)
class RealSdkCredentialFailureTests(RealSdkCase):
    """A role that the provider will not let the adapter assume leaves as a sanitized refusal.

    The role is assumed on the first use of the credentials, not when the session is built,
    so the provider's answer, with its message, comes out of whichever call needs them first.
    botocore also logs a failed refresh at WARNING, with the provider's message in the
    traceback. The adapter keeps that line and drops the traceback, which the second test measures.
    """

    def setUp(self):
        self.use_files(profile_config(PROFILE, **ASSUME_ROLE_SETTINGS), SOURCE_CREDENTIALS)
        self.sent = self.forbid_transport()
        self.asked = []
        root = logging.getLogger()
        for logger in (root, *map(logging.getLogger, SDK_LOGGER_NAMES)):
            self.addCleanup(logger.setLevel, logger.level)
        # A handler on the root keeps botocore's warning off the last-resort stream.
        records = LogRecords()
        root.addHandler(records)
        self.addCleanup(root.removeHandler, records)
        # Earlier tests built an ImageAws, which left its filter on this logger for good.
        without_filters(self, "botocore.credentials")

    def deny(self, request, **kwargs):
        """Answer an AssumeRole request the way STS answers a caller that may not assume the role."""
        sent = request.body.decode() if isinstance(request.body, bytes) else request.body
        self.asked.append(urllib.parse.parse_qs(sent)["Action"])
        return AWSResponse(request.url, 403, {}, RawBody(DENIED_RESPONSE))

    def test_a_denied_role_leaves_as_a_sanitized_refusal_from_every_call_that_needs_the_credentials(self):
        # Control: the SDK's own error carries the provider's message.
        plain = botocore.session.Session(profile=PROFILE)
        plain.register("before-send.sts.AssumeRole", self.deny)
        with self.assertRaises(botocore.exceptions.ClientError) as raised:
            plain.get_credentials().get_frozen_credentials()
        self.assertIn(SENTINEL_TOKEN, str(raised.exception))
        self.assertEqual(self.asked, [["AssumeRole"]])

        self.asked.clear()
        aws = ImageAws(REGION, PROFILE)
        aws._session().events.register("before-send.sts.AssumeRole", self.deny)
        key = PREFIX + "x.tar.gz"
        calls = {
            "credential_expiry": aws.credential_expiry,
            "presign_get": lambda: aws.presign_get(BUCKET, key, 1800),
            "builders": lambda: aws.builders(DEPLOYMENT),
            "image": lambda: aws.image("ami-0123456789abcdef0"),
            "put_archive": lambda: aws.put_archive(BUCKET, key, [b"data"], max_bytes=100),
        }
        for name, call in calls.items():
            with self.subTest(call=name):
                with self.assertRaises(ProviderRefusal) as raised:
                    call()
                refusal = raised.exception
                self.assertEqual(
                    (refusal.reason, refusal.detail, refusal.uncertain), ("AWS_UNCERTAIN", "AccessDenied", True)
                )
                rendered = [str(refusal), repr(refusal), repr(refusal.as_dict()), repr(vars(refusal))]
                rendered.append("".join(traceback.format_exception(refusal)))
                for text, sentinel in itertools.product(rendered, SENTINELS):
                    self.assertNotIn(sentinel, text)
        # Each call went and asked, so none was stopped short of the credentials or answered from a cache.
        self.assertEqual(self.asked, [["AssumeRole"]] * len(calls))
        self.assertEqual(self.sent, [])

    def test_the_warning_botocore_logs_for_the_failed_refresh_keeps_its_line_and_loses_the_provider_text(self):
        # The SDK's loggers as they stand when nobody has set them, so that both halves emit the warning.
        root = logging.getLogger()
        root.setLevel(logging.WARNING)
        for name in SDK_LOGGER_NAMES:
            logging.getLogger(name).setLevel(logging.NOTSET)
        logged = LoggedText()
        root.addHandler(logged)
        self.addCleanup(root.removeHandler, logged)

        # Control: a plain session leaves the provider's message in the traceback of the SDK's own warning.
        plain = botocore.session.Session(profile=PROFILE)
        plain.register("before-send.sts.AssumeRole", self.deny)
        with self.assertRaises(botocore.exceptions.ClientError):
            plain.get_credentials().get_frozen_credentials()
        controls = logged.warnings_from("botocore.credentials")
        self.assertTrue(controls)
        for text in controls:
            self.assertIn("Traceback", text)
            self.assertIn(SENTINEL_TOKEN, text)

        logged.entries.clear()
        aws = ImageAws(REGION, PROFILE)
        aws._session().events.register("before-send.sts.AssumeRole", self.deny)
        # The credential read and a request signed with the credentials are two routes into the refresh.
        for call in (aws.credential_expiry, lambda: aws.builders(DEPLOYMENT)):
            with self.assertRaises(ProviderRefusal):
                call()
        warnings = logged.warnings_from("botocore.credentials")
        # The warning is still logged, and what is logged is its message line alone.
        self.assertTrue(warnings)
        self.assertLessEqual(set(warnings), {text.splitlines()[0] for text in controls})
        for text, sentinel in itertools.product(warnings, SENTINELS):
            self.assertNotIn(sentinel, text)


class NoOffsetZone(tzinfo):
    """A zone that cannot say its offset: ``datetime.utcoffset()`` is ``None``.

    The datetime has a ``tzinfo``, yet it is not aware, and ``timestamp()``
    raises ``TypeError`` for it.
    """

    def utcoffset(self, moment):
        return None

    def dst(self, moment):
        return None

    def tzname(self, moment):
        return None


class MalformedOffsetZone(NoOffsetZone):
    """A zone whose offset is not a ``timedelta``: ``utcoffset()`` raises ``TypeError``."""

    def utcoffset(self, moment):
        return 3600


class CreationTimeTests(Base):
    """An image's ``CreationDate`` and a snapshot's ``StartTime`` become ``created_at``.

    The deletion rule's age checks (retention, orphans and the drift hold)
    read them. The adapter must give a time only when it can prove one.
    """

    TAGS = tag_list({IMAGE_BUILD_TAG: "true", DEPLOYMENT_TAG: DEPLOYMENT, BUILD_ID_TAG: "build-0001"})
    MOMENT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)

    def image_created_at(self, **item):
        self.ec2.images_store.append(
            {"ImageId": "ami-0123456789abcdef0", "State": "available", "Tags": self.TAGS, **item}
        )
        (image,) = self.image_aws().images(DEPLOYMENT)
        return image.created_at

    def snapshot_created_at(self, **item):
        self.ec2.snapshots_store.append(
            {"SnapshotId": "snap-0123456789abcdef0", "State": "completed", "Tags": self.TAGS, **item}
        )
        (snapshot,) = self.image_aws().snapshots(DEPLOYMENT)
        return snapshot.created_at

    def test_an_iso_creation_date_of_an_image_is_read_as_epoch_seconds(self):
        for text in ("2026-01-02T03:04:05.000Z", "2026-01-02T03:04:05Z", "2026-01-02T03:04:05+00:00"):
            with self.subTest(text=text):
                self.ec2.images_store.clear()
                self.assertEqual(self.image_created_at(CreationDate=text), self.MOMENT.timestamp())

    def test_an_offset_other_than_utc_is_converted(self):
        self.assertEqual(self.image_created_at(CreationDate="2026-01-02T05:04:05+02:00"), self.MOMENT.timestamp())

    def test_the_start_time_of_a_snapshot_is_read_as_epoch_seconds(self):
        self.assertEqual(self.snapshot_created_at(StartTime=self.MOMENT), self.MOMENT.timestamp())

    def test_a_snapshot_start_time_may_also_be_an_iso_string(self):
        self.assertEqual(self.snapshot_created_at(StartTime="2026-01-02T03:04:05.000Z"), self.MOMENT.timestamp())

    def test_a_missing_time_is_none(self):
        self.assertIsNone(self.image_created_at())
        self.assertIsNone(self.snapshot_created_at())

    def test_a_time_that_is_not_proven_is_none(self):
        naive = datetime(2026, 1, 2, 3, 4, 5)
        for value in (
            "",
            "yesterday",
            "2026-01-02",
            "2026-01-02T03:04:05",
            "2026-13-45T03:04:05Z",
            naive,
            naive.isoformat(),
            1_767_323_045,
            1_767_323_045.0,
            True,
            [],
            {"Z": 1},
        ):
            with self.subTest(value=repr(value)):
                self.ec2.images_store.clear()
                self.ec2.snapshots_store.clear()
                self.assertIsNone(self.image_created_at(CreationDate=value))
                self.assertIsNone(self.snapshot_created_at(StartTime=value))

    def test_a_zone_without_a_usable_offset_is_not_a_proven_time(self):
        for zone in (NoOffsetZone(), MalformedOffsetZone()):
            moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=zone)
            self.assertIsNotNone(moment.tzinfo)
            with self.subTest(zone=type(zone).__name__):
                self.ec2.images_store.clear()
                self.ec2.snapshots_store.clear()
                self.assertIsNone(self.image_created_at(CreationDate=moment))
                self.assertIsNone(self.snapshot_created_at(StartTime=moment))

    def test_a_zone_without_a_usable_offset_does_not_refuse_the_listing(self):
        for zone in (NoOffsetZone(), MalformedOffsetZone()):
            with self.subTest(zone=type(zone).__name__):
                self.ec2.snapshots_store.clear()
                for snapshot_id, start_time in (
                    ("snap-0123456789abcdef0", datetime(2026, 1, 2, 3, 4, 5, tzinfo=zone)),
                    ("snap-0123456789abcdef1", self.MOMENT),
                ):
                    self.ec2.snapshots_store.append(
                        {"SnapshotId": snapshot_id, "State": "completed", "Tags": self.TAGS, "StartTime": start_time}
                    )
                snapshots = self.image_aws().snapshots(DEPLOYMENT)
                self.assertEqual([snapshot.created_at for snapshot in snapshots], [None, self.MOMENT.timestamp()])

    def test_a_time_that_cannot_be_read_does_not_refuse_the_listing(self):
        self.ec2.images_store.append(
            {"ImageId": "ami-0123456789abcdef0", "State": "available", "Tags": self.TAGS, "CreationDate": "garbage"}
        )
        self.ec2.images_store.append(
            {
                "ImageId": "ami-0123456789abcdef1",
                "State": "available",
                "Tags": self.TAGS,
                "CreationDate": "2026-01-02T03:04:05.000Z",
            }
        )
        images = self.image_aws().images(DEPLOYMENT)
        self.assertEqual([image.created_at for image in images], [None, self.MOMENT.timestamp()])

    def test_a_single_read_carries_the_time_too(self):
        self.ec2.images_store.append(
            {
                "ImageId": "ami-0123456789abcdef0",
                "State": "available",
                "Tags": self.TAGS,
                "CreationDate": "2026-01-02T03:04:05.000Z",
            }
        )
        self.ec2.snapshots_store.append(
            {"SnapshotId": "snap-0123456789abcdef0", "State": "completed", "Tags": self.TAGS, "StartTime": self.MOMENT}
        )
        aws = self.image_aws()
        self.assertEqual(aws.image("ami-0123456789abcdef0").created_at, self.MOMENT.timestamp())
        self.assertEqual(aws.snapshot("snap-0123456789abcdef0").created_at, self.MOMENT.timestamp())

    def test_the_time_is_not_part_of_the_status_output(self):
        self.ec2.images_store.append(
            {
                "ImageId": "ami-0123456789abcdef0",
                "State": "available",
                "Tags": self.TAGS,
                "CreationDate": "2026-01-02T03:04:05.000Z",
            }
        )
        (image,) = self.image_aws().images(DEPLOYMENT)
        self.assertEqual(
            image.as_dict(), {"image_id": "ami-0123456789abcdef0", "state": "available", "snapshot_ids": []}
        )


if __name__ == "__main__":
    unittest.main()
