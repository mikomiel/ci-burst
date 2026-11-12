import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ci_burst.aws import Aws, _definite_launch_error, _instances, _spot_quote, is_capacity_failure
from ci_burst.config import load
from ci_burst.errors import ProviderRefusal

from support import write_config


def history(price, timestamp, zone="eu-central-1a", instance_type="t4g.small", product="Linux/UNIX"):
    return {
        "InstanceType": instance_type,
        "AvailabilityZone": zone,
        "ProductDescription": product,
        "SpotPrice": price,
        "Timestamp": timestamp,
    }


OBSERVED_AT = 1_800_000_000.0


class SpotQuoteTests(unittest.TestCase):
    def test_newest_datetime_quote_is_selected(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        entries = [
            history("0.030000", now - timedelta(minutes=5)),
            history("0.020000", now),
        ]
        quote = _spot_quote(entries, "t4g.small", "eu-central-1a", OBSERVED_AT)
        self.assertEqual(quote.price_micros, 20_000)
        self.assertEqual(quote.timestamp, now.timestamp())
        self.assertEqual(quote.availability_zone, "eu-central-1a")
        # The provider timestamp is the price-change time; the observation is
        # what a freshness bound uses.
        self.assertEqual(quote.observed_at, OBSERVED_AT)
        self.assertNotEqual(quote.timestamp, quote.observed_at)

    def test_quote_outside_the_requested_scope_is_not_used(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        self.assertIsNone(_spot_quote([history("0.020000", now, zone="us-east-1a")], "t4g.small", "eu-central-1a", OBSERVED_AT))
        self.assertIsNone(_spot_quote([history("0.020000", now, instance_type="m5.large")], "t4g.small", "eu-central-1a", OBSERVED_AT))
        self.assertIsNone(_spot_quote([history("0.020000", now, product="Windows")], "t4g.small", "eu-central-1a", OBSERVED_AT))

    def test_missing_timestamp_or_price_is_refused(self):
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        self.assertIsNone(_spot_quote([history(None, now)], "t4g.small", "eu-central-1a", OBSERVED_AT))
        self.assertIsNone(
            _spot_quote(
                [
                    {
                        "SpotPrice": "0.02",
                        "AvailabilityZone": "eu-central-1a",
                        "InstanceType": "t4g.small",
                        "ProductDescription": "Linux/UNIX",
                    }
                ],
                "t4g.small",
                "eu-central-1a",
                OBSERVED_AT,
            )
        )

    def test_incomplete_response_metadata_is_not_accepted_as_scope(self):
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        self.assertIsNone(_spot_quote([{"SpotPrice": "0.02", "Timestamp": now}], "t4g.small", "eu-central-1a", OBSERVED_AT))
        self.assertIsNone(
            _spot_quote(
                [
                    {
                        "SpotPrice": "0.02",
                        "Timestamp": now,
                        "AvailabilityZone": "eu-central-1a",
                        "InstanceType": "t4g.small",
                    }
                ],
                "t4g.small",
                "eu-central-1a",
                OBSERVED_AT,
            )
        )


class ErrorClassificationTests(unittest.TestCase):
    def test_only_known_rejections_are_definite(self):
        for code in ("InvalidParameterValue", "UnauthorizedOperation", "InvalidAMIID.NotFound"):
            self.assertTrue(_definite_launch_error(code), code)
        for code in ("InternalError", "Throttling", "RequestLimitExceeded", "ServiceUnavailable", "Unknown"):
            self.assertFalse(_definite_launch_error(code), code)

    def test_an_account_spot_limit_is_definite_but_not_placement_capacity(self):
        # An exhausted account-wide Spot limit is a pre-flight refusal: nothing
        # was created, so the admission may decide again on the next run. It is
        # not proof that this candidate's zone had no capacity, so it must not
        # advance to another candidate.
        code = "MaxSpotInstanceCountExceeded"
        self.assertTrue(_definite_launch_error(code))
        self.assertFalse(is_capacity_failure(ProviderRefusal("AWS_REFUSED", code)))
        self.assertTrue(is_capacity_failure(ProviderRefusal("AWS_REFUSED", "InsufficientInstanceCapacity")))


class InstanceParsingTests(unittest.TestCase):
    def test_tags_state_and_launch_time_are_parsed(self):
        response = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-1",
                            "State": {"Name": "running"},
                            "LaunchTime": datetime(2026, 11, 9, tzinfo=timezone.utc),
                            "Tags": [{"Key": "ci-burst:managed", "Value": "true"}],
                        }
                    ]
                }
            ]
        }
        parsed = _instances(response)
        self.assertEqual(parsed[0].instance_id, "i-1")
        self.assertEqual(parsed[0].state, "running")
        self.assertEqual(parsed[0].tags["ci-burst:managed"], "true")
        self.assertIsNotNone(parsed[0].launch_time)


class FakeClient:
    def __init__(self):
        self.calls = []
        self.responses = []

    def _respond(self, name, kwargs):
        self.calls.append((name, kwargs))
        return self.responses.pop(0)

    def describe_instances(self, **kwargs):
        return self._respond("describe_instances", kwargs)

    def describe_spot_price_history(self, **kwargs):
        return self._respond("describe_spot_price_history", kwargs)

    def describe_launch_template_versions(self, **kwargs):
        return self._respond("describe_launch_template_versions", kwargs)

    def run_instances(self, **kwargs):
        return self._respond("run_instances", kwargs)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.config = load(write_config(self.root))
        self.client = FakeClient()

    def aws(self, clock=None):
        return Aws(self.config, client=self.client, clock=clock or (lambda: 1_800_000_000.0))

    def test_every_inventory_page_is_consumed(self):
        self.client.responses = [
            {"Reservations": [{"Instances": [{"InstanceId": "i-1", "State": {"Name": "running"}}]}], "NextToken": "page-2"},
            {"Reservations": [{"Instances": [{"InstanceId": "i-2", "State": {"Name": "pending"}}]}]},
        ]
        instances = self.aws().owned_instances("test-deployment")
        self.assertEqual([item.instance_id for item in instances], ["i-1", "i-2"])
        self.assertEqual(self.client.calls[1][1]["NextToken"], "page-2")

    def test_pagination_limit_blocks_the_inventory(self):
        self.client.responses = [
            {"Reservations": [], "NextToken": f"page-{index}"} for index in range(20)
        ]
        with self.assertRaises(ProviderRefusal) as raised:
            self.aws().owned_instances("test-deployment")
        self.assertEqual(raised.exception.reason, "AWS_INVENTORY_INCOMPLETE")

    def test_template_carries_placement_and_rejects_an_indirect_image(self):
        self.client.responses = [
            {
                "LaunchTemplateVersions": [
                    {
                        "LaunchTemplateId": "lt-1",
                        "VersionNumber": 3,
                        "LaunchTemplateData": {
                            "ImageId": "resolve:ssm:/ci/burst/image",
                            "InstanceType": "t4g.small",
                            "InstanceInitiatedShutdownBehavior": "terminate",
                            "UserData": "AAAA",
                        },
                    }
                ]
            }
        ]
        with self.assertRaises(ProviderRefusal) as raised:
            self.aws().template("runner", 3)
        self.assertEqual(raised.exception.reason, "TEMPLATE_IMAGE_NOT_LITERAL")

        self.client.responses = [
            {
                "LaunchTemplateVersions": [
                    {
                        "LaunchTemplateId": "lt-1",
                        "VersionNumber": 3,
                        "LaunchTemplateData": {
                            "ImageId": "ami-1",
                            "InstanceType": "t4g.small",
                            "InstanceInitiatedShutdownBehavior": "terminate",
                            "UserData": "AAAA",
                            "Placement": {"AvailabilityZone": "eu-central-1a"},
                        },
                    }
                ]
            }
        ]
        template = self.aws().template("runner", 3)
        self.assertEqual(template.placement_zone, "eu-central-1a")
        self.assertTrue(template.has_user_data)

    def test_template_must_terminate_on_shutdown(self):
        self.client.responses = [
            {
                "LaunchTemplateVersions": [
                    {
                        "LaunchTemplateId": "lt-1",
                        "VersionNumber": 3,
                        "LaunchTemplateData": {
                            "ImageId": "ami-1",
                            "InstanceType": "t4g.small",
                            "InstanceInitiatedShutdownBehavior": "stop",
                            "UserData": "AAAA",
                        },
                    }
                ]
            }
        ]
        with self.assertRaises(ProviderRefusal) as raised:
            self.aws().template("runner", 3)
        self.assertEqual(raised.exception.reason, "TEMPLATE_SHUTDOWN_NOT_TERMINATE")

    def test_launch_must_identify_exactly_one_instance(self):
        self.client.responses = [{"Instances": [{"InstanceId": "i-1"}, {"InstanceId": "i-2"}]}]
        with self.assertRaises(ProviderRefusal) as raised:
            self.aws().run_spot({"MinCount": 1, "MaxCount": 1})
        self.assertTrue(raised.exception.uncertain)

    def test_client_is_built_without_sdk_internal_retries(self):
        import sys
        import types
        from unittest import mock

        recorded = {}

        boto3_module = types.ModuleType("boto3")

        class Session:
            def __init__(self, profile_name=None):
                recorded["profile"] = profile_name

            def client(self, service, region_name=None, config=None):
                recorded.update(service=service, region=region_name, config=config)
                return object()

        boto3_module.Session = Session
        config_module = types.ModuleType("botocore.config")

        class Config:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        config_module.Config = Config
        botocore_module = types.ModuleType("botocore")
        botocore_module.config = config_module
        with mock.patch.dict(
            sys.modules, {"boto3": boto3_module, "botocore": botocore_module, "botocore.config": config_module}
        ):
            Aws(self.config)._client()
        self.assertEqual(recorded["config"].kwargs["retries"]["total_max_attempts"], 1)
        self.assertEqual(recorded["service"], "ec2")
        self.assertEqual(recorded["region"], "eu-central-1")

    def test_unclassified_client_exception_is_uncertain(self):
        class RaisingClient:
            def describe_instances(self, **kwargs):
                raise RuntimeError("ResponseParserError")

            def run_instances(self, **kwargs):
                raise RuntimeError("ResponseParserError")

        aws = Aws(self.config, client=RaisingClient())
        for call in (lambda: aws.instances(["i-1"]), lambda: aws.run_spot({"MinCount": 1, "MaxCount": 1})):
            with self.assertRaises(ProviderRefusal) as raised:
                call()
            self.assertTrue(raised.exception.uncertain)
            self.assertEqual(raised.exception.reason, "AWS_UNCERTAIN")

    def test_aged_out_instance_record_is_an_empty_result(self):
        class NotFound(Exception):
            response = {"Error": {"Code": "InvalidInstanceID.NotFound"}}

        class MissingClient:
            def describe_instances(self, **kwargs):
                raise NotFound()

        self.assertEqual(Aws(self.config, client=MissingClient()).instances(["i-old"]), [])

    def test_a_mixed_not_found_batch_is_resolved_per_identifier(self):
        class NotFound(Exception):
            response = {"Error": {"Code": "InvalidInstanceID.NotFound"}}

        class MixedClient:
            """Real EC2 rejects the whole by-ID call when one id is unknown."""

            def __init__(self):
                self.calls = []

            def describe_instances(self, **kwargs):
                identifiers = kwargs.get("InstanceIds") or []
                self.calls.append(list(identifiers))
                if len(identifiers) > 1:
                    raise NotFound()
                if identifiers == ["i-old"]:
                    raise NotFound()
                return {
                    "Reservations": [
                        {"Instances": [{"InstanceId": "i-live", "State": {"Name": "running"}}]}
                    ]
                }

        client = MixedClient()
        instances = Aws(self.config, client=client).instances(["i-old", "i-live"])
        # The live identifier survives beside the aged-out one, and absence was
        # never inferred from the rejected batch.
        self.assertEqual([item.instance_id for item in instances], ["i-live"])
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(client.calls[0], ["i-old", "i-live"])
        self.assertEqual(client.calls[1:], [["i-old"], ["i-live"]])

    def test_a_per_identifier_read_failure_is_uncertain_not_absence(self):
        class NotFound(Exception):
            response = {"Error": {"Code": "InvalidInstanceID.NotFound"}}

        class FailingMemberClient:
            def describe_instances(self, **kwargs):
                identifiers = kwargs.get("InstanceIds") or []
                if len(identifiers) > 1:
                    raise NotFound()
                if identifiers == ["i-old"]:
                    raise NotFound()
                raise RuntimeError("ResponseParserError")

        with self.assertRaises(ProviderRefusal) as raised:
            Aws(self.config, client=FailingMemberClient()).instances(["i-old", "i-live"])
        # A real failure must never be swallowed into an empty list.
        self.assertEqual(raised.exception.reason, "AWS_UNCERTAIN")
        self.assertTrue(raised.exception.uncertain)

    def test_identity_and_ec2_share_one_session(self):
        import sys
        import types
        from unittest import mock

        sessions = []

        boto3_module = types.ModuleType("boto3")

        class Session:
            def __init__(self, profile_name=None):
                sessions.append(profile_name)

            def client(self, service, region_name=None, config=None):
                if service == "sts":
                    class STS:
                        def get_caller_identity(self):
                            return {"Account": "111122223333"}

                    return STS()
                return object()

        boto3_module.Session = Session
        config_module = types.ModuleType("botocore.config")

        class Config:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        config_module.Config = Config
        botocore_module = types.ModuleType("botocore")
        botocore_module.config = config_module
        with mock.patch.dict(
            sys.modules, {"boto3": boto3_module, "botocore": botocore_module, "botocore.config": config_module}
        ):
            aws = Aws(self.config)
            self.assertEqual(aws.account_id(), "111122223333")
            aws._client()
        self.assertEqual(len(sessions), 1)

    def test_no_read_is_made_for_an_empty_instance_list(self):
        self.assertEqual(self.aws().instances([]), [])
        self.assertEqual(self.client.calls, [])


class MissingSdkTests(unittest.TestCase):
    """A checkout without the runtime SDK must refuse, not raise an import error."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.config = load(write_config(self.root, config={"aws": {"profile": "ci-burst-check"}}))
        self.missing = {"boto3": None, "botocore": None, "botocore.config": None}

    def test_each_live_entry_path_returns_the_documented_refusal(self):
        for call in (Aws(self.config)._client, Aws(self.config).account_id):
            with self.subTest(entry=call.__name__):
                with mock.patch.dict(sys.modules, self.missing):
                    with self.assertRaises(ProviderRefusal) as raised:
                        call()
                self.assertEqual(raised.exception.reason, "AWS_SDK_UNAVAILABLE")
                self.assertFalse(raised.exception.uncertain)
                self.assertIn("boto3", raised.exception.detail)
                # The diagnostic names the installation requirement only.
                self.assertNotIn("ci-burst-check", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
