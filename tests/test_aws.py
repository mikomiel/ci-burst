import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ci_burst.aws import Aws, _instances, _spot_quote
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


class SpotQuoteTests(unittest.TestCase):
    def test_newest_datetime_quote_is_selected(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        entries = [
            history("0.030000", now - timedelta(minutes=5)),
            history("0.020000", now),
        ]
        quote = _spot_quote(entries, "t4g.small", "eu-central-1a")
        self.assertEqual(quote.price_micros, 20_000)
        self.assertEqual(quote.timestamp, now.timestamp())
        self.assertEqual(quote.availability_zone, "eu-central-1a")

    def test_quote_outside_the_requested_scope_is_not_used(self):
        now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        self.assertIsNone(_spot_quote([history("0.020000", now, zone="us-east-1a")], "t4g.small", "eu-central-1a"))
        self.assertIsNone(_spot_quote([history("0.020000", now, instance_type="m5.large")], "t4g.small", "eu-central-1a"))
        self.assertIsNone(_spot_quote([history("0.020000", now, product="Windows")], "t4g.small", "eu-central-1a"))

    def test_missing_timestamp_or_price_is_refused(self):
        self.assertIsNone(_spot_quote([{"SpotPrice": "0.02"}], "t4g.small", "eu-central-1a"))
        self.assertIsNone(
            _spot_quote(
                [history(None, datetime(2026, 11, 9, tzinfo=timezone.utc))], "t4g.small", "eu-central-1a"
            )
        )


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

    def test_no_read_is_made_for_an_empty_instance_list(self):
        self.assertEqual(self.aws().instances([]), [])
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
