"""Read-only prerequisite verification; fake clients only, no network.

Every test supplies a fake EC2, SSM and IAM client. No boto3 import, credential,
network call or real resource is involved. The checks are configuration
comparisons only; they do not prove a worker's runtime behaviour.
"""

import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import check_prerequisites as prerequisites  # noqa: E402

from support import write_config  # noqa: E402

from ci_burst.errors import ProviderRefusal  # noqa: E402

PARAMETER = prerequisites.DEFAULT_REGISTRATION_PARAMETER
PROFILE = "example-runner-profile"
ROLE = "example-runner-role"
SUBNET_ID = "subnet-0123456789abcdef0"
SG_ID = "sg-0123456789abcdef0"
VPC_ID = "vpc-0123456789abcdef0"
ROUTE_TABLE_ID = "rtb-0123456789abcdef0"


class ClientError(Exception):
    """A provider error carrying the response code the script classifies."""

    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def template_data(**overrides):
    values = {
        "ImageId": "ami-0123456789abcdef0",
        "InstanceType": "t4g.small",
        "InstanceInitiatedShutdownBehavior": "terminate",
        "UserData": "ExampleBootstrap",
        "SecurityGroupIds": [SG_ID],
        "IamInstanceProfile": {"Name": PROFILE},
    }
    values.update(overrides)
    return {key: value for key, value in values.items() if value is not None}


def subnet_data(**overrides):
    values = {
        "SubnetId": SUBNET_ID,
        "AvailabilityZone": "eu-central-1a",
        "VpcId": VPC_ID,
        "State": "available",
    }
    values.update(overrides)
    return values


def route_table_data(**overrides):
    values = {
        "RouteTableId": ROUTE_TABLE_ID,
        "VpcId": VPC_ID,
        "Associations": [{"SubnetId": SUBNET_ID, "Main": False}],
        "Routes": [
            {"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-0123456789abcdef0", "State": "active"}
        ],
    }
    values.update(overrides)
    return values


class FakeEc2:
    def __init__(self):
        self.templates = {}
        self.subnets = {}
        self.route_tables = []
        self.security_groups = {}
        self.security_group_names = {}
        self.error = None
        self.errors = {}
        self.calls = []

    def _maybe_raise(self, method):
        if self.error is not None:
            raise self.error
        if method in self.errors:
            raise self.errors[method]

    def describe_launch_template_versions(self, **kwargs):
        self.calls.append(("describe_launch_template_versions", kwargs))
        self._maybe_raise("describe_launch_template_versions")
        version = int(kwargs["Versions"][0])
        if version not in self.templates:
            return {"LaunchTemplateVersions": []}
        return {
            "LaunchTemplateVersions": [
                {
                    "LaunchTemplateId": "lt-0123456789abcdef0",
                    "VersionNumber": version,
                    "LaunchTemplateData": copy.deepcopy(self.templates[version]),
                }
            ]
        }

    def describe_subnets(self, **kwargs):
        self.calls.append(("describe_subnets", kwargs))
        self._maybe_raise("describe_subnets")
        subnets = []
        for subnet_id in kwargs["SubnetIds"]:
            if subnet_id not in self.subnets:
                raise ClientError("InvalidSubnetID.NotFound")
            subnets.append(copy.deepcopy(self.subnets[subnet_id]))
        return {"Subnets": subnets}

    def describe_route_tables(self, **kwargs):
        self.calls.append(("describe_route_tables", kwargs))
        self._maybe_raise("describe_route_tables")
        filters = {item["Name"]: item["Values"] for item in kwargs.get("Filters", [])}
        tables = copy.deepcopy(self.route_tables)
        if "association.subnet-id" in filters:
            wanted = set(filters["association.subnet-id"])
            return {
                "RouteTables": [
                    table
                    for table in tables
                    if any(
                        association.get("SubnetId") in wanted
                        for association in table.get("Associations") or []
                    )
                ]
            }
        vpcs = set(filters.get("vpc-id") or [])
        return {
            "RouteTables": [
                table
                for table in tables
                if table.get("VpcId") in vpcs
                and any(association.get("Main") for association in table.get("Associations") or [])
            ]
        }

    def describe_security_groups(self, **kwargs):
        self.calls.append(("describe_security_groups", kwargs))
        self._maybe_raise("describe_security_groups")
        if "GroupIds" in kwargs:
            groups = []
            for group_id in kwargs["GroupIds"]:
                if group_id not in self.security_groups:
                    raise ClientError("InvalidGroup.NotFound")
                groups.append(copy.deepcopy(self.security_groups[group_id]))
            return {"SecurityGroups": groups}
        names = set()
        for item in kwargs.get("Filters") or []:
            if item.get("Name") == "group-name":
                names = set(item.get("Values") or [])
        return {
            "SecurityGroups": [
                copy.deepcopy(self.security_group_names[name])
                for name in names
                if name in self.security_group_names
            ]
        }


class FakeSsm:
    def __init__(self, names=()):
        self.parameters = {name: {} for name in names}
        self.error = None
        self.calls = []

    def describe_parameters(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        names = set(self.parameters)
        for item in kwargs.get("ParameterFilters") or kwargs.get("Filters") or []:
            if item.get("Key") == "Name" and item.get("Option") == "Equals":
                names &= set(item.get("Values") or [])
        return {"Parameters": [{"Name": name} for name in sorted(names)]}


class FakeIam:
    def __init__(self, profiles=None):
        self.profiles = dict(profiles or {})
        self.error = None
        self.calls = []

    def get_instance_profile(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        name = kwargs["InstanceProfileName"]
        if name not in self.profiles:
            raise ClientError("NoSuchEntity")
        role = self.profiles[name]
        roles = [] if role is None else [{"RoleName": role}]
        return {"InstanceProfile": {"InstanceProfileName": name, "Roles": roles}}


def deployment_toml(root, *, version=3, zone="eu-central-1a", subnet_id=SUBNET_ID):
    """A deployment with one ordered candidate so the network is configured."""
    lines = [
        'deployment = "test-deployment"',
        "",
        "[forgejo]",
        'base_url = "https://forgejo.example"',
        'repository = "owner/repo"',
        'token_env = "CI_BURST_TEST_TOKEN"',
        'contract = "forgejo-17.0.1-runners-jobs"',
        'qualification = "live-verified"',
        "",
        "[worker]",
        'labels = ["ci-burst:host"]',
        "",
        "[aws]",
        'region = "eu-central-1"',
        'launch_template_name = "ci-burst-runner"',
        f"launch_template_version = {version}",
        'max_spot_price = "0.0400"',
        "max_lifetime_seconds = 3600",
        "",
        "[[aws.availability_zones]]",
        f'zone = "{zone}"',
        f'subnet_id = "{subnet_id}"',
        "",
        "[budget]",
        "window_seconds = 86400",
        'allowance = "10.00"',
        'non_compute_allowance = "0.50"',
        "",
        "[state]",
        'database = "state.sqlite3"',
    ]
    path = Path(root) / "deployment.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class PrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.ec2 = FakeEc2()
        self.ec2.templates[3] = template_data()
        self.ec2.subnets[SUBNET_ID] = subnet_data()
        self.ec2.route_tables = [route_table_data()]
        self.ec2.security_groups[SG_ID] = {
            "GroupId": SG_ID,
            "GroupName": "example-runner",
            "VpcId": VPC_ID,
        }
        self.ssm = FakeSsm({PARAMETER})
        self.iam = FakeIam({PROFILE: ROLE})
        self.config = prerequisites.load(deployment_toml(self.root))

    def check(self, config=None):
        clients = prerequisites.Clients(ec2=self.ec2, ssm=self.ssm, iam=self.iam)
        return prerequisites.check(config or self.config, clients)

    def records(self, result, name):
        return [record for record in result["checks"] if record["check"] == name]

    def assert_drift(self, result, reason):
        self.assertEqual(result["decision"], "drift")
        self.assertIn(reason, result["reasons"])
        self.assertEqual(prerequisites._exit_code(result["decision"]), 2)

    def assert_unavailable(self, result, reason):
        self.assertEqual(result["decision"], "unavailable")
        self.assertIn(reason, result["unavailable"])
        self.assertNotIn(reason, result["reasons"])
        self.assertEqual(prerequisites._exit_code(result["decision"]), 3)

    # -- satisfied -------------------------------------------------------
    def test_satisfied_prerequisites_are_reported(self):
        result = self.check()
        self.assertEqual(result["decision"], "satisfied")
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["unavailable"], [])

        template = self.records(result, "template")[0]
        self.assertEqual(template["status"], "pass")
        self.assertEqual(template["facts"]["image_id"], "ami-0123456789abcdef0")
        self.assertEqual(template["facts"]["instance_type"], "t4g.small")
        self.assertEqual(template["facts"]["shutdown_behavior"], "terminate")
        self.assertTrue(template["facts"]["bootstrap_present"])
        self.assertEqual(template["facts"]["security_group_ids"], [SG_ID])
        self.assertEqual(template["facts"]["instance_profile"], PROFILE)

        self.assertEqual(self.records(result, "security_group")[0]["status"], "pass")
        self.assertEqual(self.records(result, "instance_profile")[0]["facts"]["role"], ROLE)
        placement = self.records(result, "placement")[0]
        self.assertEqual(placement["status"], "pass")
        self.assertEqual(placement["facts"]["subnet_zone"], "eu-central-1a")
        self.assertEqual(placement["facts"]["egress_route"]["destination"], "0.0.0.0/0")
        self.assertEqual(self.records(result, "registration_parameter")[0]["status"], "pass")

    def test_registration_parameter_is_checked_without_reading_its_value(self):
        result = self.check()
        self.assertEqual(self.records(result, "registration_parameter")[0]["status"], "pass")
        self.assertEqual(len(self.ssm.calls), 1)
        self.assertIn("ParameterFilters", self.ssm.calls[0])
        self.assertNotIn("WithDecryption", self.ssm.calls[0])
        self.assertNotIn(PARAMETER + "value", json.dumps(result))

    def test_registration_parameter_path_can_be_overridden(self):
        self.ssm.parameters = {"/example/custom-registration-token": {}}
        clients = prerequisites.Clients(ec2=self.ec2, ssm=self.ssm, iam=self.iam)
        result = prerequisites.check(self.config, clients, "/example/custom-registration-token")
        self.assertEqual(result["decision"], "satisfied")
        self.assertEqual(
            self.records(result, "registration_parameter")[0]["target"],
            "/example/custom-registration-token",
        )

    def test_named_security_group_is_accepted(self):
        self.ec2.templates[3] = template_data(
            SecurityGroupIds=None,
            SecurityGroups=["example-runner"],
        )
        self.ec2.security_group_names["example-runner"] = {"GroupName": "example-runner", "GroupId": SG_ID}
        result = self.check()
        self.assertEqual(result["decision"], "satisfied")
        self.assertEqual(self.records(result, "security_group")[0]["target"], "example-runner")

    def test_main_route_table_is_used_when_the_subnet_has_no_explicit_association(self):
        self.ec2.route_tables = [
            route_table_data(Associations=[{"Main": True}]),
        ]
        result = self.check()
        self.assertEqual(result["decision"], "satisfied")
        self.assertEqual(self.records(result, "placement")[0]["facts"]["route_table_ids"], [ROUTE_TABLE_ID])

    def test_single_zone_configuration_skips_placement_checks(self):
        config = prerequisites.load(
            write_config(
                self.root,
                config={"state": {"database": "state.sqlite3"}},
            )
        )
        result = self.check(config)
        self.assertEqual(result["decision"], "satisfied")
        self.assertEqual(self.records(result, "placement"), [])
        self.assertEqual(self.records(result, "candidate")[0]["facts"]["subnet_id"], None)

    # -- template drift --------------------------------------------------
    def test_missing_template_version_is_drift(self):
        self.ec2.templates.clear()
        self.assert_drift(self.check(), "template_not_found")

    def test_indirect_image_is_drift(self):
        self.ec2.templates[3] = template_data(ImageId="resolve:ssm:/example/image")
        self.assert_drift(self.check(), "template_image_not_literal")

    def test_missing_instance_type_is_drift(self):
        self.ec2.templates[3] = template_data(InstanceType=None)
        self.assert_drift(self.check(), "template_instance_type_missing")

    def test_non_terminating_shutdown_is_drift(self):
        self.ec2.templates[3] = template_data(InstanceInitiatedShutdownBehavior="stop")
        self.assert_drift(self.check(), "template_shutdown_not_terminate")

    def test_missing_bootstrap_is_drift(self):
        self.ec2.templates[3] = template_data(UserData=None)
        self.assert_drift(self.check(), "template_bootstrap_missing")

    def test_template_placement_mismatch_is_drift(self):
        self.ec2.templates[3] = template_data(Placement={"AvailabilityZone": "eu-central-1b"})
        self.assert_drift(self.check(), "template_placement_mismatch")

    def test_network_pinned_template_with_a_candidate_list_is_drift(self):
        self.ec2.templates[3] = template_data(
            NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": SUBNET_ID}]
        )
        self.assert_drift(self.check(), "template_network_pinned")

    # -- security groups, profile and parameter --------------------------
    def test_missing_security_group_is_drift(self):
        self.ec2.security_groups.clear()
        self.assert_drift(self.check(), "security_group_missing")

    def test_unpinned_security_group_is_drift(self):
        self.ec2.templates[3] = template_data(SecurityGroupIds=None)
        self.assert_drift(self.check(), "template_security_group_not_pinned")

    def test_missing_instance_profile_is_drift(self):
        self.iam.profiles.clear()
        self.assert_drift(self.check(), "instance_profile_missing")

    def test_unpinned_instance_profile_is_drift(self):
        self.ec2.templates[3] = template_data(IamInstanceProfile=None)
        self.assert_drift(self.check(), "template_instance_profile_not_pinned")

    def test_instance_profile_without_a_role_is_drift(self):
        self.iam.profiles[PROFILE] = None
        self.assert_drift(self.check(), "instance_profile_role_missing")

    def test_missing_registration_parameter_is_drift(self):
        self.ssm.parameters.clear()
        self.assert_drift(self.check(), "registration_parameter_missing")

    # -- placement drift -------------------------------------------------
    def test_missing_subnet_is_drift(self):
        self.ec2.subnets.clear()
        self.assert_drift(self.check(), "subnet_missing")

    def test_subnet_in_another_zone_is_drift(self):
        self.ec2.subnets[SUBNET_ID] = subnet_data(AvailabilityZone="eu-central-1b")
        self.assert_drift(self.check(), "subnet_zone_mismatch")

    def test_unavailable_subnet_is_drift(self):
        self.ec2.subnets[SUBNET_ID] = subnet_data(State="pending")
        self.assert_drift(self.check(), "subnet_not_available")

    def test_missing_route_table_is_drift(self):
        self.ec2.route_tables = []
        self.assert_drift(self.check(), "route_table_missing")

    def test_missing_egress_route_is_drift(self):
        self.ec2.route_tables = [
            route_table_data(
                Routes=[{"DestinationCidrBlock": "192.0.2.0/24", "GatewayId": "local", "State": "active"}]
            )
        ]
        self.assert_drift(self.check(), "egress_route_missing")

    def test_blackholed_egress_route_is_drift(self):
        self.ec2.route_tables = [
            route_table_data(
                Routes=[
                    {
                        "DestinationCidrBlock": "0.0.0.0/0",
                        "GatewayId": "igw-0123456789abcdef0",
                        "State": "blackhole",
                    }
                ]
            )
        ]
        self.assert_drift(self.check(), "egress_route_blackhole")

    # -- unavailable permissions -----------------------------------------
    def test_missing_iam_permission_is_unavailable_not_a_pass(self):
        self.iam.error = ClientError("AccessDenied")
        result = self.check()
        self.assert_unavailable(result, "instance_profile_unavailable")
        profile = self.records(result, "instance_profile")[0]
        self.assertEqual(profile["status"], "unavailable")
        self.assertEqual(profile["detail"], "AccessDenied")

    def test_missing_ssm_permission_is_unavailable_not_a_pass(self):
        self.ssm.error = ClientError("AccessDeniedException")
        result = self.check()
        self.assert_unavailable(result, "registration_parameter_unavailable")
        self.assertEqual(self.records(result, "registration_parameter")[0]["status"], "unavailable")

    def test_missing_subnet_permission_is_unavailable_not_a_pass(self):
        self.ec2.errors["describe_subnets"] = ClientError("UnauthorizedOperation")
        result = self.check()
        self.assert_unavailable(result, "subnet_read_unavailable")
        self.assertEqual(self.records(result, "placement")[0]["status"], "unavailable")

    def test_missing_template_permission_is_unavailable_not_a_pass(self):
        self.ec2.errors["describe_launch_template_versions"] = ClientError("UnauthorizedOperation")
        result = self.check()
        self.assert_unavailable(result, "template_read_unavailable")
        self.assertEqual(self.records(result, "template")[0]["status"], "unavailable")


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.ec2 = FakeEc2()
        self.ec2.templates[3] = template_data()
        self.ec2.subnets[SUBNET_ID] = subnet_data()
        self.ec2.route_tables = [route_table_data()]
        self.ec2.security_groups[SG_ID] = {"GroupId": SG_ID, "GroupName": "example-runner"}
        self.ssm = FakeSsm({PARAMETER})
        self.iam = FakeIam({PROFILE: ROLE})
        self.config_path = deployment_toml(self.root)
        self.clients = prerequisites.Clients(ec2=self.ec2, ssm=self.ssm, iam=self.iam)

    def run_main(self):
        output = io.StringIO()
        with mock.patch.object(prerequisites, "build_clients", return_value=self.clients):
            with redirect_stdout(output):
                code = prerequisites.main(["--config", str(self.config_path)])
        return code, json.loads(output.getvalue())

    def test_satisfied_exits_zero(self):
        code, document = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(document["decision"], "satisfied")

    def test_drift_exits_two(self):
        self.ec2.templates.clear()
        code, document = self.run_main()
        self.assertEqual(code, 2)
        self.assertEqual(document["decision"], "drift")
        self.assertIn("template_not_found", document["reasons"])

    def test_unavailable_exits_three(self):
        self.iam.error = ClientError("AccessDenied")
        code, document = self.run_main()
        self.assertEqual(code, 3)
        self.assertEqual(document["decision"], "unavailable")
        self.assertIn("instance_profile_unavailable", document["unavailable"])

    def test_missing_configuration_exits_three(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = prerequisites.main(["--config", str(self.root / "missing.toml")])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.getvalue())["error"]["reason"], "CONFIG_MISSING")

    def test_missing_sdk_exits_three(self):
        output = io.StringIO()
        with mock.patch.object(
            prerequisites,
            "build_clients",
            side_effect=ProviderRefusal("AWS_SDK_UNAVAILABLE", "install boto3"),
        ):
            with redirect_stdout(output):
                code = prerequisites.main(["--config", str(self.config_path)])
        self.assertEqual(code, 3)
        self.assertEqual(json.loads(output.getvalue())["error"]["reason"], "AWS_SDK_UNAVAILABLE")


class HelperTests(unittest.TestCase):
    def test_instance_profile_name_can_be_derived_from_an_arn(self):
        data = {"IamInstanceProfile": {"Arn": "arn:aws:iam::000000000000:instance-profile/example-role"}}
        self.assertEqual(prerequisites._instance_profile_name(data), "example-role")

    def test_default_routes_ignore_non_default_and_inactive_destinations(self):
        tables = [
            {
                "Routes": [
                    {"DestinationCidrBlock": "198.51.100.0/24", "GatewayId": "local"},
                    {"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1", "State": "blackhole"},
                    {"DestinationIpv6CidrBlock": "::/0", "EgressOnlyInternetGatewayId": "eigw-1"},
                ]
            }
        ]
        routes = prerequisites._default_routes(tables)
        self.assertEqual([route["destination"] for route in routes], ["0.0.0.0/0", "::/0"])
        self.assertEqual([route["active"] for route in routes], [False, True])


if __name__ == "__main__":
    unittest.main()
