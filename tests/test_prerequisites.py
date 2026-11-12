"""Read-only prerequisite verification; fake clients only, no network.

Every test supplies a fake EC2, SSM and IAM client. No boto3 import, credential,
network call or real resource is involved. The checks are configuration
comparisons only; they do not prove a worker's runtime behaviour.
"""

import base64
import copy
import io
import json
import sys
import tempfile
import types
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
BOOTSTRAP = ROOT / "deploy" / "runner" / "bootstrap.sh"
ROUTER_ARTEFACT = ROOT / "deploy" / "router" / "userdata.sh"
PROFILE = "example-runner-profile"
ROLE = "example-runner-role"
PROFILE_ARN = "arn:aws:iam::000000000000:instance-profile/workers/example-runner"
SUBNET_ID = "subnet-0123456789abcdef0"
SECOND_SUBNET_ID = "subnet-0123456789abcdef1"
SECOND_IMAGE_ID = "ami-0123456789abcdef1"
SUBNET_CIDR = "192.0.2.0/24"
ROUTER_ENI_ID = "eni-0123456789abcdef0"
ROUTER_INSTANCE_ID = "i-0123456789abcdef0"
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
        # A template that carries the published bytes passes by default; the
        # drift tests below replace this with something else.
        "UserData": base64.b64encode(BOOTSTRAP.read_bytes()).decode("ascii"),
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
        "CidrBlock": SUBNET_CIDR,
        "MapPublicIpOnLaunch": False,
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


def router_user_data(cidrs=(SUBNET_CIDR,), extra=""):
    """Base64 user data shaped like deploy/router/userdata.sh applies its rules."""
    lines = ["#!/bin/bash", "set -euo pipefail"]
    for cidr in cidrs:
        lines.append(f"iptables -t nat -A POSTROUTING -s {cidr} -o eth0 -j MASQUERADE")
        lines.append(f"iptables -A FORWARD -s {cidr} -o eth0 -j ACCEPT")
    lines.append(extra)
    return base64.b64encode(("\n".join(lines) + "\n").encode()).decode("ascii")


def interface_data(**overrides):
    values = {
        "NetworkInterfaceId": ROUTER_ENI_ID,
        "Attachment": {"InstanceId": ROUTER_INSTANCE_ID},
        "SourceDestCheck": False,
    }
    values.update(overrides)
    return values


def instance_data(**overrides):
    values = {"InstanceId": ROUTER_INSTANCE_ID, "State": {"Name": "running"}}
    values.update(overrides)
    return values


class FakeEc2:
    def __init__(self):
        self.templates = {}
        self.subnets = {}
        self.interfaces = {}
        self.instances = {}
        self.user_data = {}
        self.route_tables = []
        self.security_groups = {}
        self.security_group_names = {}
        self.instance_types = {
            "t4g.small": {"vcpus": 2, "memory": 2048, "architectures": ["arm64"]},
        }
        self.offerings = set()
        self.images = {"ami-0123456789abcdef0": "arm64"}
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

    def describe_network_interfaces(self, **kwargs):
        self.calls.append(("describe_network_interfaces", kwargs))
        self._maybe_raise("describe_network_interfaces")
        interfaces = []
        for interface_id in kwargs["NetworkInterfaceIds"]:
            if interface_id not in self.interfaces:
                raise ClientError("InvalidNetworkInterfaceID.NotFound")
            interfaces.append(copy.deepcopy(self.interfaces[interface_id]))
        return {"NetworkInterfaces": interfaces}

    def describe_instances(self, **kwargs):
        self.calls.append(("describe_instances", kwargs))
        self._maybe_raise("describe_instances")
        instances = []
        for instance_id in kwargs["InstanceIds"]:
            if instance_id not in self.instances:
                raise ClientError("InvalidInstanceID.NotFound")
            instances.append(copy.deepcopy(self.instances[instance_id]))
        return {"Reservations": [{"Instances": instances}]}

    def describe_instance_attribute(self, **kwargs):
        self.calls.append(("describe_instance_attribute", kwargs))
        self._maybe_raise("describe_instance_attribute")
        instance_id = kwargs["InstanceId"]
        if instance_id not in self.instances:
            raise ClientError("InvalidInstanceID.NotFound")
        value = self.user_data.get(instance_id)
        return {"UserData": {"Value": value}} if value else {"UserData": {}}

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


    def describe_instance_types(self, **kwargs):
        self.calls.append(("describe_instance_types", kwargs))
        self._maybe_raise("describe_instance_types")
        entries = []
        for name in kwargs["InstanceTypes"]:
            facts = self.instance_types.get(name)
            if facts is None:
                continue
            entries.append(
                {
                    "InstanceType": name,
                    "VCpuInfo": {"DefaultVCpus": facts["vcpus"]},
                    "MemoryInfo": {"SizeInMiB": facts["memory"]},
                    "ProcessorInfo": {"SupportedArchitectures": list(facts["architectures"])},
                }
            )
        return {"InstanceTypes": entries}

    def describe_instance_type_offerings(self, **kwargs):
        self.calls.append(("describe_instance_type_offerings", kwargs))
        self._maybe_raise("describe_instance_type_offerings")
        return {
            "InstanceTypeOfferings": [
                {"InstanceType": instance_type, "Location": location, "LocationType": "availability-zone"}
                for instance_type, location in sorted(self.offerings)
            ]
        }

    def describe_images(self, **kwargs):
        self.calls.append(("describe_images", kwargs))
        self._maybe_raise("describe_images")
        return {
            "Images": [
                {"ImageId": image_id, "Architecture": self.images[image_id]}
                for image_id in kwargs["ImageIds"]
                if image_id in self.images
            ]
        }


class FakeSsm:
    def __init__(self, names=(), pages=None):
        self.parameters = {name: {} for name in names}
        self.pages = list(pages or [])
        self.error = None
        self.calls = []

    def describe_parameters(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.pages:
            page = self.pages.pop(0)
            if isinstance(page, Exception):
                raise page
            return page
        names = set(self.parameters)
        for item in kwargs.get("ParameterFilters") or kwargs.get("Filters") or []:
            if item.get("Key") == "Name" and item.get("Option") == "Equals":
                names &= set(item.get("Values") or [])
        return {"Parameters": [{"Name": name} for name in sorted(names)]}


class FakeIam:
    def __init__(self, profiles=None, arns=None):
        self.profiles = dict(profiles or {})
        self.arns = dict(arns or {})
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
        profile = {"InstanceProfileName": name, "Roles": roles}
        if name in self.arns:
            profile["Arn"] = self.arns[name]
        return {"InstanceProfile": profile}


def botocore_error_modules():
    """A fake botocore so ``Aws._translate`` classifies a ClientError."""
    exceptions = types.ModuleType("botocore.exceptions")

    class ClientError(Exception):
        def __init__(self, code):
            super().__init__(code)
            self.response = {"Error": {"Code": code}}

    class BotoCoreError(Exception):
        pass

    exceptions.ClientError = ClientError
    exceptions.BotoCoreError = BotoCoreError
    botocore = types.ModuleType("botocore")
    botocore.exceptions = exceptions
    return {"botocore": botocore, "botocore.exceptions": exceptions}, ClientError


def deployment_toml(root, *, version=3, zone="eu-central-1a", subnet_id=SUBNET_ID, instance_types=None):
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
    text = "\n".join(lines) + "\n"
    if instance_types:
        rendered = ", ".join(json.dumps(name) for name in instance_types)
        text = text.replace(
            "\n[[aws.availability_zones]]",
            f"\ninstance_types = [{rendered}]\n\n[[aws.availability_zones]]",
            1,
        )
    path.write_text(text, encoding="utf-8")
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

    def use_instance_types(
        self,
        *types,
        offered=None,
        metadata=None,
        architecture="arm64",
        zones=("eu-central-1a",),
    ):
        """Configure the deployment's type list and the provider facts behind it."""
        self.config = prerequisites.load(deployment_toml(self.root, instance_types=list(types)))
        for instance_type in types:
            self.ec2.instance_types.setdefault(
                instance_type, {"vcpus": 2, "memory": 2048, "architectures": ["arm64"]}
            )
        self.ec2.instance_types.update(metadata or {})
        self.ec2.offerings = set(
            offered
            if offered is not None
            else {(instance_type, zone) for instance_type in types for zone in zones}
        )
        self.ec2.images["ami-0123456789abcdef0"] = architecture
        return self.config

    def add_candidate(self, *, zone="eu-central-1b", subnet_id=SECOND_SUBNET_ID, version=4, template_overrides=None):
        """Append one more zone candidate that pins its own template version."""
        path = self.config.path
        block = [
            "",
            "[[aws.availability_zones]]",
            f'zone = "{zone}"',
            f'subnet_id = "{subnet_id}"',
            f"launch_template_version = {version}",
            "",
        ]
        path.write_text(path.read_text(encoding="utf-8") + "\n".join(block) + "\n", encoding="utf-8")
        self.ec2.subnets[subnet_id] = subnet_data(SubnetId=subnet_id, AvailabilityZone=zone)
        self.ec2.route_tables.append(
            route_table_data(
                RouteTableId="rtb-0123456789abcdeff",
                Associations=[{"SubnetId": subnet_id, "Main": False}],
            )
        )
        self.ec2.templates[version] = template_data(**(template_overrides or {}))
        self.config = prerequisites.load(path)
        return self.config

    def test_configured_instance_types_are_reported_as_pools(self):
        self.use_instance_types("t4g.small", "c7g.large")
        result = self.check()
        self.assertEqual(result["decision"], "satisfied")
        self.assertEqual(
            [record["target"] for record in self.records(result, "instance_type")],
            ["t4g.small", "c7g.large"],
        )
        self.assertEqual(
            [record["target"] for record in self.records(result, "pool")],
            ["eu-central-1a/t4g.small", "eu-central-1a/c7g.large"],
        )
        self.assertTrue(all(record["status"] == "pass" for record in self.records(result, "pool")))

    def test_no_instance_type_list_performs_no_pool_reads(self):
        result = self.check()
        self.assertEqual(result["decision"], "satisfied")
        self.assertEqual(self.records(result, "instance_type"), [])
        self.assertEqual(self.records(result, "pool"), [])
        self.assertNotIn(
            "describe_instance_types", [call[0] for call in self.ec2.calls]
        )

    def test_an_instance_type_missing_from_a_zone_is_drift(self):
        self.use_instance_types("t4g.small", "c7g.large", offered={("t4g.small", "eu-central-1a")})
        result = self.check()
        self.assert_drift(result, "instance_type_not_offered")
        record = self.records(result, "pool")[1]
        self.assertEqual(record["target"], "eu-central-1a/c7g.large")
        self.assertEqual(record["status"], "drift")

    def test_a_type_with_a_different_shape_is_drift(self):
        self.use_instance_types(
            "t4g.small",
            "c7g.large",
            metadata={"c7g.large": {"vcpus": 4, "memory": 8192, "architectures": ["arm64"]}},
        )
        result = self.check()
        self.assert_drift(result, "instance_type_shape_mismatch")
        record = [item for item in self.records(result, "instance_type") if item["target"] == "c7g.large"][0]
        self.assertEqual(record["status"], "drift")
        self.assertEqual(record["facts"]["vcpus"], 4)

    def test_an_unsupported_image_architecture_is_drift(self):
        self.use_instance_types("t4g.small", architecture="x86_64")
        result = self.check()
        self.assert_drift(result, "image_architecture_unsupported")
        self.assertEqual(self.records(result, "instance_type")[0]["status"], "drift")

    def test_a_type_must_match_every_configured_template_shape(self):
        self.ec2.instance_types["m7g.2xlarge"] = {
            "vcpus": 8,
            "memory": 32768,
            "architectures": ["arm64"],
        }
        self.use_instance_types("t4g.small")
        self.add_candidate(template_overrides={"InstanceType": "m7g.2xlarge"})
        self.ec2.offerings |= {("t4g.small", "eu-central-1b")}
        result = self.check()
        self.assert_drift(result, "instance_type_shape_mismatch")

    def assert_incomplete_instance_type_facts_are_unavailable(self, facts):
        self.use_instance_types("t4g.small", metadata={"t4g.small": facts})
        result = self.check()
        self.assert_unavailable(result, "instance_types_unavailable")
        self.assertIn(
            "incomplete instance type facts", self.records(result, "pools")[0]["detail"]
        )

    def test_missing_vcpu_facts_are_unavailable(self):
        self.assert_incomplete_instance_type_facts_are_unavailable(
            {"vcpus": None, "memory": 2048, "architectures": ["arm64"]}
        )

    def test_missing_memory_facts_are_unavailable(self):
        self.assert_incomplete_instance_type_facts_are_unavailable(
            {"vcpus": 2, "memory": None, "architectures": ["arm64"]}
        )

    def test_missing_architecture_support_is_unavailable(self):
        self.assert_incomplete_instance_type_facts_are_unavailable(
            {"vcpus": 2, "memory": 2048, "architectures": []}
        )

    def test_a_missing_image_description_is_unavailable(self):
        self.use_instance_types("t4g.small")
        self.ec2.images = {}
        result = self.check()
        self.assert_unavailable(result, "instance_types_unavailable")
        self.assertIn(
            "describe_images did not return", self.records(result, "pools")[0]["detail"]
        )

    def test_a_partial_image_response_is_unavailable(self):
        self.use_instance_types("t4g.small")
        self.add_candidate(template_overrides={"ImageId": SECOND_IMAGE_ID})
        self.ec2.offerings |= {("t4g.small", "eu-central-1b")}
        self.ec2.images = {"ami-0123456789abcdef0": "arm64"}
        result = self.check()
        self.assert_unavailable(result, "instance_types_unavailable")
        self.assertIn(SECOND_IMAGE_ID, self.records(result, "pools")[0]["detail"])

    def test_unreadable_instance_type_metadata_is_unavailable(self):
        self.use_instance_types("t4g.small")
        self.ec2.errors["describe_instance_types"] = ClientError("AccessDenied")
        result = self.check()
        self.assert_unavailable(result, "instance_types_unavailable")
        self.assertEqual(self.records(result, "pools")[0]["detail"], "AccessDenied")

    def use_router(self, *, cidrs=(SUBNET_CIDR,), state="running", user_data=None):
        """Point the candidate's default route at a router instance."""
        self.ec2.route_tables = [
            route_table_data(
                Routes=[
                    {
                        "DestinationCidrBlock": "0.0.0.0/0",
                        "NetworkInterfaceId": ROUTER_ENI_ID,
                        "State": "active",
                    }
                ]
            )
        ]
        self.ec2.interfaces[ROUTER_ENI_ID] = interface_data()
        self.ec2.instances[ROUTER_INSTANCE_ID] = instance_data(State={"Name": state})
        self.ec2.user_data[ROUTER_INSTANCE_ID] = (
            router_user_data(cidrs) if user_data is None else user_data
        )

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

    def test_missing_template_name_error_is_drift(self):
        modules, ClientError = botocore_error_modules()
        with mock.patch.dict(sys.modules, modules):
            self.ec2.errors["describe_launch_template_versions"] = ClientError(
                "InvalidLaunchTemplateName.NotFoundException"
            )
            result = self.check()
        self.assert_drift(result, "template_not_found")

    def test_missing_template_version_error_is_drift_via_adapter_translation(self):
        modules, ClientError = botocore_error_modules()
        with mock.patch.dict(sys.modules, modules):
            # The adapter classifies this code as AWS_UNCERTAIN, not
            # AWS_REFUSED; the not-found detail still proves absence.
            self.ec2.errors["describe_launch_template_versions"] = ClientError(
                "InvalidLaunchTemplateId.VersionNotFound"
            )
            result = self.check()
        self.assert_drift(result, "template_not_found")
        self.assertEqual(self.records(result, "template")[0]["status"], "drift")

    def test_throttled_template_read_is_unavailable_not_drift(self):
        modules, ClientError = botocore_error_modules()
        with mock.patch.dict(sys.modules, modules):
            self.ec2.errors["describe_launch_template_versions"] = ClientError("Throttling")
            result = self.check()
        self.assert_unavailable(result, "template_read_unavailable")
        self.assertNotIn("template_not_found", result["reasons"])

    def test_denied_template_read_is_unavailable_not_drift(self):
        modules, ClientError = botocore_error_modules()
        with mock.patch.dict(sys.modules, modules):
            self.ec2.errors["describe_launch_template_versions"] = ClientError("UnauthorizedOperation")
            result = self.check()
        self.assert_unavailable(result, "template_read_unavailable")
        self.assertNotIn("template_not_found", result["reasons"])

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

    def test_user_data_matching_the_repository_bootstrap_passes(self):
        result = self.check()
        self.assertEqual(self.records(result, "template")[0]["status"], "pass")
        self.assertNotIn("template_bootstrap_drift", result["reasons"])

    def test_user_data_differing_from_the_repository_bootstrap_is_drift(self):
        # The repository file is exactly what an operator publishes as a new
        # version. A pinned version carrying other bytes means a merged change
        # reached no worker, which is the HB-3194 defect.
        stale = base64.b64encode(b"#!/bin/bash\n# another bootstrap\n").decode("ascii")
        self.ec2.templates[3] = template_data(UserData=stale)
        result = self.check()
        self.assert_drift(result, "template_bootstrap_drift")
        self.assertEqual(self.records(result, "template")[0]["status"], "drift")

    def test_user_data_that_is_not_base64_is_drift(self):
        self.ec2.templates[3] = template_data(UserData="not base64 at all")
        self.assert_drift(self.check(), "template_bootstrap_drift")

    def test_user_data_trailing_newlines_do_not_count_as_drift(self):
        # A console paste can add a trailing newline; that is not a behavioural
        # difference and must not fail the check.
        pasted = base64.b64encode(BOOTSTRAP.read_bytes() + b"\n").decode("ascii")
        self.ec2.templates[3] = template_data(UserData=pasted)
        self.assertEqual(self.check()["decision"], "satisfied")

    def test_user_data_without_a_final_newline_passes(self):
        body = BOOTSTRAP.read_bytes().rstrip(b"\n")
        self.ec2.templates[3] = template_data(UserData=base64.b64encode(body).decode("ascii"))
        self.assertEqual(self.check()["decision"], "satisfied")

    def test_user_data_with_extra_trailing_newlines_passes(self):
        body = BOOTSTRAP.read_bytes() + b"\n\n"
        self.ec2.templates[3] = template_data(UserData=base64.b64encode(body).decode("ascii"))
        self.assertEqual(self.check()["decision"], "satisfied")

    def test_user_data_with_a_changed_interior_byte_is_drift(self):
        original = BOOTSTRAP.read_bytes()
        body = original.replace(b"STEP register", b"STEP registeR", 1)
        self.assertNotEqual(body, original, "the fixture byte must exist")
        self.ec2.templates[3] = template_data(UserData=base64.b64encode(body).decode("ascii"))
        self.assert_drift(self.check(), "template_bootstrap_drift")

    def test_unreadable_user_data_source_is_unavailable_not_drift(self):
        with mock.patch.object(prerequisites, "BOOTSTRAP", self.root / "missing-bootstrap.sh"):
            result = self.check()
        self.assert_unavailable(result, "template_bootstrap_source_unreadable")
        record = self.records(result, "template_bootstrap")[0]
        self.assertEqual(record["status"], "unavailable")

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

    def test_path_qualified_profile_arn_uses_the_terminal_name(self):
        self.ec2.templates[3] = template_data(IamInstanceProfile={"Arn": PROFILE_ARN})
        self.iam.profiles = {"example-runner": ROLE}
        self.iam.arns = {"example-runner": PROFILE_ARN}
        result = self.check()
        self.assertEqual(result["decision"], "satisfied")
        self.assertEqual(self.iam.calls[0]["InstanceProfileName"], "example-runner")
        profile = self.records(result, "instance_profile")[0]
        self.assertEqual(profile["facts"]["arn"], PROFILE_ARN)
        self.assertEqual(self.records(result, "template")[0]["facts"]["instance_profile"], "example-runner")

    def test_instance_profile_arn_mismatch_is_drift(self):
        returned = "arn:aws:iam::000000000000:instance-profile/other/example-runner"
        self.ec2.templates[3] = template_data(IamInstanceProfile={"Arn": PROFILE_ARN})
        self.iam.profiles = {"example-runner": ROLE}
        self.iam.arns = {"example-runner": returned}
        self.assert_drift(self.check(), "instance_profile_arn_mismatch")

    def test_missing_registration_parameter_is_drift(self):
        self.ssm.parameters.clear()
        self.assert_drift(self.check(), "registration_parameter_missing")

    def test_parameter_pagination_finds_a_match_after_an_empty_page(self):
        self.ssm.pages = [
            {"Parameters": [], "NextToken": "page-2"},
            {"Parameters": [{"Name": PARAMETER}]},
        ]
        result = self.check()
        self.assertEqual(result["decision"], "satisfied")
        self.assertEqual(self.ssm.calls[1]["NextToken"], "page-2")

    def test_parameter_pagination_failure_is_unavailable_not_missing(self):
        self.ssm.pages = [
            {"Parameters": [], "NextToken": "page-2"},
            ClientError("AccessDeniedException"),
        ]
        result = self.check()
        self.assert_unavailable(result, "registration_parameter_unavailable")
        self.assertNotIn("registration_parameter_missing", result["reasons"])

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

    # -- the optional router ---------------------------------------------
    def test_router_forwarding_the_candidate_cidr_passes(self):
        self.use_router()
        result = self.check()
        self.assertEqual(result["decision"], "satisfied")
        router = self.records(result, "router")[0]
        self.assertEqual(router["status"], "pass")
        self.assertEqual(router["facts"]["instance_id"], ROUTER_INSTANCE_ID)
        self.assertEqual(router["facts"]["instance_state"], "running")
        self.assertIn(SUBNET_CIDR, router["facts"]["forwarded_cidrs"])

    def test_router_missing_the_candidate_cidr_is_drift(self):
        self.use_router(cidrs=("198.51.100.0/24",))
        result = self.check()
        self.assert_drift(result, "router_forwarding_missing_cidr")
        self.assertEqual(self.records(result, "router")[0]["status"], "drift")

    def test_stopped_router_is_drift(self):
        self.use_router(state="stopped")
        self.assert_drift(self.check(), "router_target_not_running")

    def test_router_without_user_data_is_unavailable_not_a_pass(self):
        self.use_router(user_data="")
        self.assert_unavailable(self.check(), "router_user_data_unavailable")

    def test_router_user_data_that_is_not_base64_is_unavailable_not_a_pass(self):
        self.use_router(user_data="not base64!!")
        self.assert_unavailable(self.check(), "router_user_data_unavailable")

    def test_router_mentioning_the_cidr_only_in_a_comment_is_unverified(self):
        self.use_router(user_data=router_user_data(cidrs=(), extra=f"# forwards {SUBNET_CIDR}"))
        self.assert_unavailable(self.check(), "router_forwarding_unverified")

    def test_the_shipped_router_artefact_is_unverified_not_a_pass(self):
        # The bytes this repository publishes, not a synthetic approximation:
        # the artefact reads its CIDRs from SSM, so the check cannot see them.
        self.use_router(user_data=base64.b64encode(ROUTER_ARTEFACT.read_bytes()).decode("ascii"))
        result = self.check()
        self.assert_unavailable(result, "router_forwarding_unverified")
        self.assertEqual(self.records(result, "router")[0]["status"], "unavailable")

    def test_router_reading_its_cidrs_from_ssm_is_unavailable_not_a_pass(self):
        self.use_router(
            user_data=router_user_data(
                cidrs=(),
                extra="aws ssm get-parameter --name /ci-burst/router/config --with-decryption",
            )
        )
        result = self.check()
        self.assert_unavailable(result, "router_forwarding_unverified")
        self.assertEqual(self.records(result, "router")[0]["status"], "unavailable")

    def test_router_fetching_config_with_a_global_option_is_unverified(self):
        # `aws --region X ssm get-parameter` is the same call as
        # `aws ssm get-parameter --region X`; neither declares a CIDR here.
        self.use_router(
            user_data=router_user_data(
                cidrs=(),
                extra='aws --region eu-central-1 ssm get-parameter --name /ci-burst/router/config',
            )
        )
        self.assert_unavailable(self.check(), "router_forwarding_unverified")

    def test_managed_gateway_target_skips_the_router_contract(self):
        result = self.check()
        router = self.records(result, "router")[0]
        self.assertEqual(router["status"], "pass")
        self.assertEqual(router["facts"]["target_kind"], "GatewayId")
        self.assertNotIn("describe_instance_attribute", [name for name, _ in self.ec2.calls])

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

    def test_incomplete_check_takes_precedence_over_drift(self):
        self.ec2.templates[3] = template_data(UserData=None)
        self.ssm.error = ClientError("AccessDeniedException")
        result = self.check()
        self.assertEqual(result["decision"], "unavailable")
        self.assertIn("template_bootstrap_missing", result["reasons"])
        self.assertIn("registration_parameter_unavailable", result["unavailable"])
        self.assertEqual(prerequisites._exit_code(result["decision"]), 3)


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

    def test_drift_with_a_denied_permission_exits_three(self):
        self.ec2.templates[3] = template_data(UserData=None)
        self.ssm.error = ClientError("AccessDeniedException")
        code, document = self.run_main()
        self.assertEqual(code, 3)
        self.assertEqual(document["decision"], "unavailable")
        self.assertIn("template_bootstrap_missing", document["reasons"])
        self.assertIn("registration_parameter_unavailable", document["unavailable"])

    def test_profile_not_found_exits_three_with_sanitized_detail(self):
        boto3_module = types.ModuleType("boto3")

        class ProfileNotFound(Exception):
            pass

        class Session:
            def __init__(self, profile_name=None):
                raise ProfileNotFound("profile example is not configured")

        boto3_module.Session = Session
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"boto3": boto3_module}):
            with redirect_stdout(output):
                code = prerequisites.main(["--config", str(self.config_path)])
        document = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(document["decision"], "unavailable")
        self.assertEqual(document["error"]["reason"], "AWS_CREDENTIALS_UNAVAILABLE")
        self.assertEqual(document["error"]["detail"], "ProfileNotFound")
        self.assertNotIn("profile example", output.getvalue())

    def run_with_session_error(self, error_name):
        boto3_module = types.ModuleType("boto3")
        error_class = type(error_name, (Exception,), {})

        class Session:
            def __init__(self, profile_name=None):
                raise error_class("malformed shared config at /example/secret-path")

        boto3_module.Session = Session
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"boto3": boto3_module}):
            with redirect_stdout(output):
                code = prerequisites.main(["--config", str(self.config_path)])
        return code, json.loads(output.getvalue()), output.getvalue()

    def assert_session_configuration_unavailable(self, error_name):
        code, document, text = self.run_with_session_error(error_name)
        self.assertEqual(code, 3)
        self.assertEqual(document["decision"], "unavailable")
        self.assertEqual(document["error"]["reason"], "AWS_CREDENTIALS_UNAVAILABLE")
        self.assertEqual(document["error"]["detail"], error_name)
        self.assertNotIn("malformed shared config", text)

    def test_config_parse_error_exits_three(self):
        self.assert_session_configuration_unavailable("ConfigParseError")

    def test_invalid_config_error_exits_three(self):
        self.assert_session_configuration_unavailable("InvalidConfigError")

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
    def test_instance_profile_reference_derives_the_terminal_name_from_a_path_arn(self):
        arn = "arn:aws:iam::000000000000:instance-profile/workers/example-role"
        name, reference = prerequisites._instance_profile_reference({"IamInstanceProfile": {"Arn": arn}})
        self.assertEqual(name, "example-role")
        self.assertEqual(reference, arn)

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
