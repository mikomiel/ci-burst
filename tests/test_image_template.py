"""Tests for the inert launch-template version of a built image.

The real ``ImageAws`` runs against a fake EC2 client, so the request shapes and
the numeric-version rules are the adapter's own. The fake stores each created
version, copies the source version's data into it the way EC2 does, honours
``ClientToken`` and can inject a difference into the new version. It also
records any call that could move or remove a version, so a test can show that
none happens.

A small stub of the adapter pins the plumbing that the fake cannot show: which
arguments the module passes, and that a refusal of the provider leaves it
unchanged.

Sentinels stand in for user data and are synthetic.
"""

import copy
import hashlib
import unittest

from ci_burst.aws import ImageAws, TemplateVersion
from ci_burst.errors import DeadlineRefusal, ProviderRefusal
from ci_burst.image_template import create_candidate_version, differing_fields

from support import Clock
from test_image_aws import FakeEc2, ProviderError

NAME = "example-ci-burst-worker"
REGION = "eu-central-1"
PROFILE = "example-image-profile"
NOW = 1_800_000_000.0
PIN = 3
OLD_IMAGE = "ami-0123456789abcdef0"
NEW_IMAGE = "ami-0123456789abcdef1"
BUILD_ID = "build-0001"
DIGEST = hashlib.sha256(b"example basis").hexdigest()
TOKEN = "example-token-0001"

SENTINEL_USER_DATA = "c2VudGluZWwtdXNlci1kYXRhLW1hcmtlcg=="
SENTINEL_TAG = "sentinel-tag-value-marker"


def pin_data():
    """A worker template's data, with values that must never appear in a result."""
    return {
        "ImageId": OLD_IMAGE,
        "InstanceType": "t4g.small",
        "UserData": SENTINEL_USER_DATA,
        "BlockDeviceMappings": [{"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 20, "VolumeType": "gp3"}}],
        "IamInstanceProfile": {"Name": "example-worker-profile"},
        "MetadataOptions": {"HttpTokens": "required", "HttpPutResponseHopLimit": 1},
        "SecurityGroupIds": ["sg-0123456789abcdef0", "sg-0123456789abcdef1"],
        "TagSpecifications": [{"ResourceType": "instance", "Tags": [{"Key": "example", "Value": SENTINEL_TAG}]}],
    }


class TemplateEc2(FakeEc2):
    """A fake EC2 that keeps created launch-template versions."""

    #: The calls that would move or remove a version. The adapter has none of them.
    FORBIDDEN = ("modify_launch_template", "delete_launch_template_versions", "create_launch_template")

    def __init__(self, calls):
        super().__init__(calls)
        self.tokens = {}
        self.change = None
        self.hidden = set()
        self.repeated = set()
        self.dataless = set()
        self.extra = {}

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
        remembered = self.tokens.get((LaunchTemplateName, ClientToken))
        if remembered is not None:
            return {"LaunchTemplateVersion": {"VersionNumber": remembered}}
        source = self.versions_store[(LaunchTemplateName, int(SourceVersion))]
        data = copy.deepcopy(source["LaunchTemplateData"])
        data.update(copy.deepcopy(LaunchTemplateData))
        if self.change is not None:
            self.change(data)
        number = self.next_version
        self.next_version += 1
        self.versions_store[(LaunchTemplateName, number)] = {
            "VersionNumber": number,
            "LaunchTemplateId": source["LaunchTemplateId"],
            "VersionDescription": VersionDescription,
            "LaunchTemplateData": data,
        }
        self.tokens[(LaunchTemplateName, ClientToken)] = number
        return {"LaunchTemplateVersion": {"VersionNumber": number}}

    def describe_launch_template_versions(self, *, LaunchTemplateName, Versions, NextToken=None):
        response = super().describe_launch_template_versions(
            LaunchTemplateName=LaunchTemplateName, Versions=Versions, NextToken=NextToken
        )
        entries = [item for item in response["LaunchTemplateVersions"] if item["VersionNumber"] not in self.hidden]
        for item in entries:
            if item["VersionNumber"] in self.dataless:
                del item["LaunchTemplateData"]
        entries += [copy.deepcopy(item) for item in entries if item["VersionNumber"] in self.repeated]
        entries += [copy.deepcopy(item) for item in self.extra.values()]
        return {"LaunchTemplateVersions": entries}

    def modify_launch_template(self, **kwargs):
        self._enter("modify_launch_template", kwargs)
        return {}

    def delete_launch_template_versions(self, **kwargs):
        self._enter("delete_launch_template_versions", kwargs)
        return {}

    def create_launch_template(self, **kwargs):
        self._enter("create_launch_template", kwargs)
        return {}


class TemplateCase(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.ec2 = TemplateEc2(self.calls)
        self.ec2.versions_store[(NAME, PIN)] = {
            "VersionNumber": PIN,
            "LaunchTemplateId": "lt-0123456789abcdef0",
            "VersionDescription": "the pin",
            "LaunchTemplateData": pin_data(),
        }
        self.aws = ImageAws(REGION, PROFILE, clients={"ec2": self.ec2}, clock=Clock(NOW), expiry=NOW + 3600)

    def create(self, **overrides):
        values = dict(
            template_name=NAME,
            source_pin=PIN,
            image_id=NEW_IMAGE,
            build_id=BUILD_ID,
            basis_digest=DIGEST,
            client_token=TOKEN,
        )
        values.update(overrides)
        return create_candidate_version(self.aws, **values)

    def names(self):
        return [name for name, _ in self.calls]

    def requests(self, name):
        return [arguments for call, arguments in self.calls if call == name]

    def stored(self, number):
        return self.ec2.versions_store[(NAME, number)]["LaunchTemplateData"]


class CleanVersionTests(TemplateCase):
    def test_a_clean_version_is_created_and_reported(self):
        self.assertEqual(self.create(), {"version": 5, "drift": "clean"})
        self.assertEqual(self.stored(5)["ImageId"], NEW_IMAGE)

    def test_the_new_version_derives_from_the_numeric_pin_and_names_only_the_image(self):
        self.create()
        (request,) = self.requests("create_launch_template_version")
        self.assertEqual(request["LaunchTemplateName"], NAME)
        self.assertEqual(request["SourceVersion"], str(PIN))
        self.assertEqual(request["LaunchTemplateData"], {"ImageId": NEW_IMAGE})
        self.assertEqual(request["ClientToken"], TOKEN)

    def test_the_description_names_the_build_and_the_first_12_digest_characters_only(self):
        self.create()
        (request,) = self.requests("create_launch_template_version")
        description = request["VersionDescription"]
        self.assertIn(BUILD_ID, description)
        self.assertIn(DIGEST[:12], description)
        self.assertNotIn(DIGEST[:13], description)
        self.assertLessEqual(len(description), 255)

    def test_both_versions_are_read_back_after_the_creation(self):
        self.create()
        self.assertEqual(self.names(), ["create_launch_template_version", "describe_launch_template_versions"])
        (read,) = self.requests("describe_launch_template_versions")
        self.assertEqual(read["LaunchTemplateName"], NAME)
        self.assertEqual(read["Versions"], [str(PIN), "5"])

    def test_no_call_moves_a_default_version_or_removes_anything(self):
        self.create()
        self.create(client_token="example-token-0002")
        self.assertEqual(set(self.names()), {"create_launch_template_version", "describe_launch_template_versions"})
        for name in TemplateEc2.FORBIDDEN:
            self.assertNotIn(name, self.names())

    def test_the_pin_itself_is_left_as_it_was(self):
        self.create()
        self.assertEqual(self.stored(PIN), pin_data())

    def test_a_clean_result_has_no_fields_entry(self):
        self.assertEqual(set(self.create()), {"version", "drift"})


class DriftTests(TemplateCase):
    def test_a_difference_in_a_field_other_than_the_image_is_drift(self):
        self.ec2.change = lambda data: data.update(InstanceType="t4g.large")
        result = self.create()
        self.assertEqual(result, {"version": 5, "drift": "TEMPLATE_DRIFT", "fields": ["InstanceType"]})

    def test_the_drift_names_every_differing_field_in_order(self):
        def change(data):
            data["UserData"] = "other"
            data["InstanceType"] = "t4g.large"
            data["MetadataOptions"]["HttpTokens"] = "optional"  # nested: the top-level name is reported
            del data["IamInstanceProfile"]  # dropped in the new version
            data["KeyName"] = "example-key"  # added in the new version

        self.ec2.change = change
        result = self.create()
        self.assertEqual(result["drift"], "TEMPLATE_DRIFT")
        self.assertEqual(
            result["fields"],
            ["IamInstanceProfile", "InstanceType", "KeyName", "MetadataOptions", "UserData"],
        )

    def test_a_list_in_another_order_is_drift(self):
        self.ec2.change = lambda data: data["SecurityGroupIds"].reverse()
        self.assertEqual(self.create()["fields"], ["SecurityGroupIds"])

    def test_the_result_never_holds_a_value(self):
        def change(data):
            data["UserData"] = "different-user-data-marker"
            data["InstanceType"] = "t4g.large"

        self.ec2.change = change
        result = self.create()
        rendered = repr(result)
        for value in (
            SENTINEL_USER_DATA,
            "different-user-data-marker",
            "t4g.small",
            "t4g.large",
            SENTINEL_TAG,
            "example-worker-profile",
        ):
            self.assertNotIn(value, rendered)
        self.assertEqual(set(result), {"version", "drift", "fields"})
        self.assertTrue(all(isinstance(name, str) for name in result["fields"]))

    def test_a_new_version_that_carries_another_image_is_drift_in_the_image(self):
        self.ec2.change = lambda data: data.update(ImageId=OLD_IMAGE)
        result = self.create()
        self.assertEqual(result, {"version": 5, "drift": "TEMPLATE_DRIFT", "fields": ["ImageId"]})

    def test_a_new_version_without_an_image_is_drift_in_the_image(self):
        self.ec2.change = lambda data: data.pop("ImageId")
        self.assertEqual(self.create()["fields"], ["ImageId"])

    def test_the_image_is_listed_with_the_other_fields_when_both_are_wrong(self):
        def change(data):
            data["ImageId"] = OLD_IMAGE
            data["InstanceType"] = "t4g.large"

        self.ec2.change = change
        self.assertEqual(self.create()["fields"], ["ImageId", "InstanceType"])

    def test_a_source_without_an_image_field_still_gets_the_new_image(self):
        del self.ec2.versions_store[(NAME, PIN)]["LaunchTemplateData"]["ImageId"]
        self.assertEqual(self.create(), {"version": 5, "drift": "clean"})


class RetryTests(TemplateCase):
    def test_a_retry_with_the_same_token_after_a_crash_finds_the_same_version(self):
        # The version is created, then the read-back fails: the caller never learns the number.
        self.ec2.errors["describe_launch_template_versions"] = ProviderError("RequestLimitExceeded")
        with self.assertRaises(ProviderRefusal) as raised:
            self.create()
        self.assertTrue(raised.exception.uncertain)
        self.assertIn((NAME, 5), self.ec2.versions_store)
        del self.ec2.errors["describe_launch_template_versions"]
        self.assertEqual(self.create(), {"version": 5, "drift": "clean"})
        created = sorted(number for (name, number) in self.ec2.versions_store if name == NAME)
        self.assertEqual(created, [PIN, 5], "the retry must not create a second version")
        first, second = self.requests("create_launch_template_version")
        self.assertEqual(first, second)

    def test_a_retry_after_a_drift_verdict_gives_the_same_verdict_and_version(self):
        self.ec2.change = lambda data: data.update(InstanceType="t4g.large")
        first = self.create()
        second = self.create()
        self.assertEqual(first, second)
        self.assertEqual(first["version"], 5)
        self.assertEqual(len(self.requests("create_launch_template_version")), 2)
        self.assertNotIn((NAME, 6), self.ec2.versions_store)

    def test_another_token_creates_another_version(self):
        # The fake honours tokens, so the retry tests above prove something.
        self.assertEqual(self.create()["version"], 5)
        self.assertEqual(self.create(client_token="example-token-0002")["version"], 6)


class DeadlineTests(TemplateCase):
    """The read-back is a read of the provider, so the pass deadline bounds it. The creation is one call."""

    def test_a_read_back_that_would_start_past_the_deadline_is_refused_and_not_made(self):
        with self.assertRaises(DeadlineRefusal):
            self.create(deadline=NOW)
        self.assertEqual(self.names(), ["create_launch_template_version"])
        self.assertIn((NAME, 5), self.ec2.versions_store, "the version exists, and the retry finds it by its token")

    def test_a_retry_after_the_deadline_refusal_gets_the_same_version_and_the_verdict(self):
        with self.assertRaises(DeadlineRefusal):
            self.create(deadline=NOW)
        self.assertEqual(self.create(deadline=NOW + 240), {"version": 5, "drift": "clean"})
        created = sorted(number for (name, number) in self.ec2.versions_store if name == NAME)
        self.assertEqual(created, [PIN, 5], "the retry must not create a second version")
        first, second = self.requests("create_launch_template_version")
        self.assertEqual(first, second)

    def test_a_deadline_that_has_not_passed_changes_nothing(self):
        self.assertEqual(self.create(deadline=NOW + 240), {"version": 5, "drift": "clean"})
        self.assertEqual(self.names(), ["create_launch_template_version", "describe_launch_template_versions"])

    def test_a_drift_is_still_found_when_a_deadline_is_given(self):
        self.ec2.change = lambda data: data.update(InstanceType="t4g.large")
        self.assertEqual(
            self.create(deadline=NOW + 240), {"version": 5, "drift": "TEMPLATE_DRIFT", "fields": ["InstanceType"]}
        )


class IncompleteReadBackTests(TemplateCase):
    def refused(self):
        with self.assertRaises(ProviderRefusal) as raised:
            self.create()
        self.assertEqual(raised.exception.reason, "AWS_INVENTORY_INCOMPLETE")
        self.assertTrue(raised.exception.uncertain)
        return raised.exception

    def test_a_missing_new_version_is_refused_without_a_verdict(self):
        self.ec2.hidden = {5}
        self.refused()

    def test_a_missing_pin_is_refused_without_a_verdict(self):
        self.ec2.hidden = {PIN}
        self.refused()

    def test_an_empty_read_back_is_refused(self):
        self.ec2.hidden = {PIN, 5}
        self.refused()

    def test_a_repeated_entry_is_refused(self):
        self.ec2.repeated = {5}
        self.refused()

    def test_a_version_that_was_not_asked_for_is_refused(self):
        self.ec2.extra = {9: {"VersionNumber": 9, "LaunchTemplateId": "lt-0123456789abcdef0", "LaunchTemplateData": {}}}
        self.refused()

    def test_an_entry_without_its_data_is_the_adapters_own_refusal(self):
        self.ec2.dataless = {5}
        self.refused()

    def test_the_refusal_holds_no_template_data(self):
        self.ec2.hidden = {5}
        refusal = self.refused()
        for text in (str(refusal), repr(refusal), repr(refusal.as_dict())):
            for value in (SENTINEL_USER_DATA, SENTINEL_TAG, "t4g.small"):
                self.assertNotIn(value, text)

    def test_a_retry_after_an_incomplete_read_back_gives_the_verdict(self):
        self.ec2.hidden = {5}
        self.refused()
        self.ec2.hidden = set()
        self.assertEqual(self.create(), {"version": 5, "drift": "clean"})


class RefusedRequestTests(TemplateCase):
    def test_a_refused_creation_reads_nothing_back(self):
        self.ec2.errors["create_launch_template_version"] = ProviderError("UnauthorizedOperation")
        with self.assertRaises(ProviderRefusal):
            self.create()
        self.assertNotIn("describe_launch_template_versions", self.names())

    def test_an_unreadable_read_back_is_an_uncertain_refusal_and_no_verdict(self):
        self.ec2.errors["describe_launch_template_versions"] = ProviderError("AccessDenied")
        with self.assertRaises(ProviderRefusal) as raised:
            self.create()
        self.assertTrue(raised.exception.uncertain)

    def test_a_creation_that_answers_with_the_pin_is_refused_before_any_read_back(self):
        # Through the real adapter: it returns the number it was given, so the module has to refuse it.
        def answer_with_the_pin(**request):
            self.ec2._enter("create_launch_template_version", request)
            return {"LaunchTemplateVersion": {"VersionNumber": PIN}}

        self.ec2.create_launch_template_version = answer_with_the_pin
        for source_image in (OLD_IMAGE, NEW_IMAGE):
            with self.subTest(source_image=source_image):
                self.ec2.versions_store[(NAME, PIN)]["LaunchTemplateData"]["ImageId"] = source_image
                del self.calls[:]
                with self.assertRaises(ProviderRefusal) as raised:
                    self.create()
                self.assertEqual(raised.exception.reason, "AWS_UNCERTAIN")
                self.assertTrue(raised.exception.uncertain)
                self.assertEqual(self.names(), ["create_launch_template_version"])

    def test_a_moving_or_malformed_source_is_refused_before_any_request(self):
        for source in ("$Latest", "$Default", "3", 0, -1, None, True, 3.0):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    self.create(source_pin=source)
        self.assertEqual(self.calls, [])

    def test_an_image_that_is_not_a_literal_id_is_refused_before_any_request(self):
        for image in ("resolve:ssm:/example/path", "", None, "i-0123456789abcdef0"):
            with self.subTest(image=image):
                with self.assertRaises(ValueError):
                    self.create(image_id=image)
        self.assertEqual(self.calls, [])


class StubAws:
    """The two adapter calls the module makes, recording what it passes."""

    def __init__(self, *, created=7, read=(), create_error=None, read_error=None):
        self.calls = []
        self.deadlines = []
        self.created = created
        self.read = list(read)
        self.create_error = create_error
        self.read_error = read_error

    def create_template_version(self, name, source_version, image_id, description, client_token):
        self.calls.append(("create", name, source_version, image_id, description, client_token))
        if self.create_error is not None:
            raise self.create_error
        return self.created

    def template_versions(self, name, versions, *, deadline=None):
        self.calls.append(("read", name, list(versions)))
        self.deadlines.append(deadline)
        if self.read_error is not None:
            raise self.read_error
        return self.read


def version(number, **data):
    return TemplateVersion(name=NAME, template_id="lt-0123456789abcdef0", version=number, description=None, data=data)


class PlumbingTests(unittest.TestCase):
    def call(self, stub, **overrides):
        values = dict(
            template_name=NAME,
            source_pin=PIN,
            image_id=NEW_IMAGE,
            build_id=BUILD_ID,
            basis_digest=DIGEST,
            client_token=TOKEN,
        )
        values.update(overrides)
        return create_candidate_version(stub, **values)

    def test_the_adapter_gets_the_pin_the_image_the_token_and_a_short_description(self):
        stub = StubAws(read=[version(PIN, ImageId=OLD_IMAGE, K="v"), version(7, ImageId=NEW_IMAGE, K="v")])
        self.assertEqual(self.call(stub), {"version": 7, "drift": "clean"})
        (create, read) = stub.calls
        self.assertEqual(create[:4], ("create", NAME, PIN, NEW_IMAGE))
        self.assertEqual(create[5], TOKEN)
        self.assertIn(BUILD_ID, create[4])
        self.assertIn(DIGEST[:12], create[4])
        self.assertNotIn(DIGEST[:13], create[4])
        self.assertEqual(read, ("read", NAME, [PIN, 7]))

    def test_the_deadline_goes_to_the_read_back_and_none_goes_when_there_is_none(self):
        versions = [version(PIN, ImageId=OLD_IMAGE, K="v"), version(7, ImageId=NEW_IMAGE, K="v")]
        stub = StubAws(read=versions)
        self.assertEqual(self.call(stub, deadline=NOW + 240), {"version": 7, "drift": "clean"})
        self.assertEqual(stub.deadlines, [NOW + 240])
        stub = StubAws(read=versions)
        self.assertEqual(self.call(stub), {"version": 7, "drift": "clean"})
        self.assertEqual(stub.deadlines, [None])
        # The creation is one call: the deadline is not an argument of it.
        self.assertEqual(stub.calls[0][:4], ("create", NAME, PIN, NEW_IMAGE))

    def test_the_read_back_order_does_not_matter(self):
        stub = StubAws(read=[version(7, ImageId=NEW_IMAGE, K="v"), version(PIN, ImageId=OLD_IMAGE, K="v")])
        self.assertEqual(self.call(stub)["drift"], "clean")

    def test_a_refusal_of_the_creation_leaves_unchanged_and_nothing_is_read(self):
        refusal = ProviderRefusal("AWS_REFUSED", "example detail")
        stub = StubAws(create_error=refusal)
        with self.assertRaises(ProviderRefusal) as raised:
            self.call(stub)
        self.assertIs(raised.exception, refusal)
        self.assertEqual([call[0] for call in stub.calls], ["create"])

    def test_a_refusal_of_the_read_back_leaves_unchanged(self):
        refusal = ProviderRefusal("AWS_UNCERTAIN", "example detail", uncertain=True)
        stub = StubAws(read_error=refusal)
        with self.assertRaises(ProviderRefusal) as raised:
            self.call(stub)
        self.assertIs(raised.exception, refusal)

    def test_a_new_version_that_is_the_pin_itself_is_refused_before_any_read_back(self):
        # A provider answer that names the pin as the new version has not created anything. Read back, the
        # pin alone would be compared with itself, so the image the pin already names would decide the
        # verdict: another image is reported as drift and the built image itself as clean.
        for name, source_image in (("a source with another image", OLD_IMAGE), ("a source with the built image", NEW_IMAGE)):
            with self.subTest(name):
                stub = StubAws(created=PIN, read=[version(PIN, ImageId=source_image, K="v")])
                with self.assertRaises(ProviderRefusal) as raised:
                    self.call(stub)
                self.assertEqual(raised.exception.reason, "AWS_UNCERTAIN")
                self.assertTrue(raised.exception.uncertain)
                self.assertEqual([call[0] for call in stub.calls], ["create"])

    def test_the_refusal_of_a_new_version_that_is_the_pin_names_no_template_data(self):
        stub = StubAws(created=PIN, read=[version(PIN, ImageId=NEW_IMAGE, UserData=SENTINEL_USER_DATA)])
        with self.assertRaises(ProviderRefusal) as raised:
            self.call(stub)
        for text in (str(raised.exception), repr(raised.exception), repr(raised.exception.as_dict())):
            for value in (SENTINEL_USER_DATA, NEW_IMAGE, OLD_IMAGE):
                self.assertNotIn(value, text)

    def test_a_new_version_that_is_not_the_pin_is_read_back_as_before(self):
        # The control: any other number goes on to the read-back, and the pin may already carry the built image.
        stub = StubAws(created=7, read=[version(PIN, ImageId=NEW_IMAGE, K="v"), version(7, ImageId=NEW_IMAGE, K="v")])
        self.assertEqual(self.call(stub), {"version": 7, "drift": "clean"})
        self.assertEqual([call[0] for call in stub.calls], ["create", "read"])

    def test_a_field_that_exists_in_one_version_only_is_a_difference_even_when_it_is_empty(self):
        stub = StubAws(read=[version(PIN, ImageId=OLD_IMAGE), version(7, ImageId=NEW_IMAGE, Empty=None)])
        self.assertEqual(self.call(stub)["fields"], ["Empty"])
        stub = StubAws(read=[version(PIN, ImageId=OLD_IMAGE, Empty=None), version(7, ImageId=NEW_IMAGE)])
        self.assertEqual(self.call(stub)["fields"], ["Empty"])

    def test_equal_nested_data_is_not_a_difference(self):
        nested = {"A": [1, {"B": [2, 3]}], "C": {"D": None}}
        stub = StubAws(read=[version(PIN, ImageId=OLD_IMAGE, **copy.deepcopy(nested)), version(7, ImageId=NEW_IMAGE, **nested)])
        self.assertEqual(self.call(stub), {"version": 7, "drift": "clean"})


class DifferingFieldsTests(unittest.TestCase):
    """The one comparison that the creation of a candidate and ``status --refresh`` share."""

    def pair(self, **changes):
        """The pin's data and a candidate's: the same, except for the new image and ``changes``."""
        source, created = pin_data(), pin_data()
        created["ImageId"] = NEW_IMAGE
        created.update(changes)
        return source, created

    def test_data_that_differs_in_the_image_alone_is_clean(self):
        source, created = self.pair()
        self.assertEqual(differing_fields(source, created, NEW_IMAGE), [])

    def test_the_image_must_be_exactly_the_expected_one(self):
        source, created = self.pair()
        for label, expected in (("another image", OLD_IMAGE), ("no image expected", None)):
            with self.subTest(case=label):
                self.assertEqual(differing_fields(source, created, expected), ["ImageId"])
        del created["ImageId"]
        self.assertEqual(differing_fields(source, created, NEW_IMAGE), ["ImageId"])

    def test_the_image_of_the_source_is_never_compared(self):
        source, created = self.pair()
        source["ImageId"] = "ami-0aaaaaaaaaaaaaaaa"
        self.assertEqual(differing_fields(source, created, NEW_IMAGE), [])

    def test_every_other_difference_is_named_once_sorted_and_without_a_value(self):
        source, created = self.pair(InstanceType="t4g.large", KeyName="example-key")
        created["MetadataOptions"]["HttpTokens"] = "optional"  # nested: the top-level name is reported
        del created["IamInstanceProfile"]
        fields = differing_fields(source, created, NEW_IMAGE)
        self.assertEqual(fields, ["IamInstanceProfile", "InstanceType", "KeyName", "MetadataOptions"])
        self.assertTrue(all(isinstance(name, str) for name in fields))

    def test_a_key_in_one_mapping_only_is_a_difference_even_when_its_value_is_none(self):
        source, created = self.pair(Empty=None)
        self.assertEqual(differing_fields(source, created, NEW_IMAGE), ["Empty"])
        self.assertEqual(differing_fields(created, source, OLD_IMAGE), ["Empty"])

    def test_the_mappings_are_left_as_they_were(self):
        source, created = self.pair(InstanceType="t4g.large")
        before = copy.deepcopy((source, created))
        differing_fields(source, created, NEW_IMAGE)
        self.assertEqual((source, created), before)


if __name__ == "__main__":
    unittest.main()
