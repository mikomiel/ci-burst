"""Read-only verification of one deployment's worker prerequisites.

The worker network, security groups, instance profile, registration parameter
and launch template are operator-owned and remain a manual tutorial. This
command compares the deployed facts with the deployment TOML and the template
contract the controller already enforces, so a silent drift is visible before a
launch fails. It also compares the pinned template version's user data with
``deploy/runner/bootstrap.sh``, the file an operator publishes as the next
version, so a repository edit that was never published is drift rather than a
silent no-op.

The command only reads: it never mutates a resource, never reads a parameter
value (not even a ciphertext), and never stores one. The template is read
through the controller's own adapter, so its refusals and the reported image,
instance type and shutdown behaviour are exactly what the controller uses.

Exit codes: 0 satisfied, 2 drift, 3 the check could not run completely. An
incomplete check takes precedence over drift: when both occur, the decision is
``unavailable`` and exits 3, while the drift reasons are still listed. A
missing permission, a credential failure or a session configuration failure is
reported as ``unavailable``; it is not drift and not a pass.
"""

import argparse
import base64
import binascii
import json
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ci_burst.aws import Aws  # noqa: E402
from ci_burst.config import load  # noqa: E402
from ci_burst.errors import ConfigRefusal, ProviderRefusal, Refusal  # noqa: E402

DEFAULT_CONFIG = "deployment.toml"
#: The parameter name the published bootstrap reads. An operator who chose
#: another name passes ``--registration-parameter``; the value is never read
#: either way.
DEFAULT_REGISTRATION_PARAMETER = "/ci-burst/runner/registration-token"

#: The repository's copy of the launch template's user data. The pinned version
#: must carry exactly these bytes: an operator who edits this file publishes a
#: new template version and bumps ``launch_template_version`` to it.
BOOTSTRAP = ROOT / "deploy" / "runner" / "bootstrap.sh"

EXIT_SATISFIED = 0
EXIT_UNEXPECTED = 1
EXIT_DRIFT = 2
EXIT_UNAVAILABLE = 3

_TEMPLATE_DRIFT = {
    "TEMPLATE_NOT_FOUND": "template_not_found",
    "TEMPLATE_IMAGE_NOT_LITERAL": "template_image_not_literal",
    "TEMPLATE_INSTANCE_TYPE_MISSING": "template_instance_type_missing",
    "TEMPLATE_SHUTDOWN_NOT_TERMINATE": "template_shutdown_not_terminate",
}

#: EC2 error codes that definitively mean the template or version does not
#: exist. The adapter may translate one of these to ``AWS_REFUSED`` or
#: ``AWS_UNCERTAIN`` depending on its retry classification, so the detail code
#: alone decides. Denied, throttled and unknown codes stay unavailable.
_TEMPLATE_NOT_FOUND_CODES = frozenset(
    {
        "InvalidLaunchTemplateId.NotFound",
        "InvalidLaunchTemplateId.VersionNotFound",
        "InvalidLaunchTemplateName.NotFoundException",
    }
)

#: Bound on the best-effort ``DescribeParameters`` pagination. Reaching it is
#: an incomplete check, not proof that the parameter is absent.
MAX_PARAMETER_PAGES = 100

#: Expected boto3/botocore credential, session or session-configuration
#: failures. A failure is reported by class name only: an exception message can
#: contain a profile name, a file path or other setup detail that must not
#: reach the output.
_CREDENTIAL_FAILURES = frozenset(
    {
        "ProfileNotFound",
        "NoCredentialsError",
        "PartialCredentialsError",
        "CredentialRetrievalError",
        "SSOTokenLoadError",
        "SSOError",
        "TokenRetrievalError",
        "ConfigParseError",
        "InvalidConfigError",
    }
)

#: Provider error codes that mean the named object does not exist. Every other
#: error -- including AccessDenied, throttling and an unclassified exception --
#: is reported as unavailable, because it does not prove drift.
_MISSING_SUBNET = frozenset({"InvalidSubnetID.NotFound"})
_MISSING_SECURITY_GROUP = frozenset({"InvalidGroup.NotFound", "InvalidGroupId.NotFound"})
_MISSING_INSTANCE_PROFILE = frozenset({"NoSuchEntity", "NoSuchEntityException"})

_DEFAULT_DESTINATIONS = frozenset({"0.0.0.0/0", "::/0"})
_ROUTE_TARGETS = (
    "GatewayId",
    "NatGatewayId",
    "TransitGatewayId",
    "EgressOnlyInternetGatewayId",
    "InstanceId",
    "NetworkInterfaceId",
    "VpcPeeringConnectionId",
    "CarrierGatewayId",
    "LocalGatewayId",
)


@dataclass(frozen=True)
class Clients:
    """Injected clients. Tests supply fakes; ``main`` builds them from boto3."""

    ec2: object
    ssm: object
    iam: object


class ProviderCallError(Exception):
    """A read failed. ``code`` is the provider error code or exception name."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _provider_code(error):
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if isinstance(code, str) and code:
            return code
    return type(error).__name__


def _call(client, method, **kwargs):
    try:
        return getattr(client, method)(**kwargs)
    except Exception as error:  # noqa: BLE001 - classified by the caller
        raise ProviderCallError(_provider_code(error)) from None


def _record(name, target, *, status="pass", facts=None, reasons=(), detail=None):
    record = {"check": name, "target": target, "status": status}
    if facts is not None:
        record["facts"] = facts
    if reasons:
        record["reasons"] = list(reasons)
    if detail:
        record["detail"] = detail
    return record


def _launch_template_data(response):
    entries = response.get("LaunchTemplateVersions") or []
    if not entries:
        return None
    data = entries[0].get("LaunchTemplateData")
    return data if isinstance(data, dict) else {}


def _without_trailing_newlines(value):
    # A console paste can add or drop trailing newlines. That is not a
    # behavioural difference; every other byte is compared exactly.
    return value.rstrip(b"\n")


def _template_user_data(data):
    """Return the template's decoded user data, or ``None`` when absent/invalid."""
    encoded = data.get("UserData")
    if not isinstance(encoded, str) or not encoded.strip():
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return None


def _repository_bootstrap():
    try:
        return BOOTSTRAP.read_bytes()
    except OSError:
        return None


def _security_group_ids(data):
    values = [value for value in data.get("SecurityGroupIds") or [] if isinstance(value, str) and value]
    # A template that pins an elastic network interface carries its groups
    # inside that interface instead of the top-level list.
    for interface in data.get("NetworkInterfaces") or []:
        if isinstance(interface, dict):
            values.extend(value for value in interface.get("Groups") or [] if isinstance(value, str) and value)
    return list(dict.fromkeys(values))


def _security_group_names(data):
    return [value for value in data.get("SecurityGroups") or [] if isinstance(value, str) and value]


def _instance_profile_reference(data):
    """Return ``(terminal_profile_name, reference_arn)`` from a template.

    A template may reference the profile by ARN. IAM ARNs can carry a path
    (``.../instance-profile/workers/example-runner``) while
    ``GetInstanceProfile`` takes only the terminal name, so the lookup name and
    the ARN reference are returned separately.
    """
    profile = data.get("IamInstanceProfile")
    if not isinstance(profile, dict):
        return None, None
    arn = profile.get("Arn")
    reference = arn if isinstance(arn, str) and arn else None
    name = profile.get("Name")
    if isinstance(name, str) and name:
        return name, reference
    marker = ":instance-profile/"
    if reference and marker in reference:
        path = reference.split(marker, 1)[1]
        return (path.rsplit("/", 1)[-1] or None), reference
    return None, reference


def _network_pinned(data):
    return bool(data.get("NetworkInterfaces"))


def _template_facts(template, data=None):
    facts = {
        "template_id": template.template_id,
        "image_id": template.image_id,
        "instance_type": template.instance_type,
        "shutdown_behavior": template.shutdown_behavior,
        "bootstrap_present": template.has_user_data,
        "placement_zone": template.placement_zone,
        "security_group_ids": [],
        "security_group_names": [],
        "instance_profile": None,
        "instance_profile_arn": None,
    }
    if data is not None:
        facts["security_group_ids"] = _security_group_ids(data)
        facts["security_group_names"] = _security_group_names(data)
        facts["instance_profile"], facts["instance_profile_arn"] = _instance_profile_reference(data)
    return facts


def _template_refusal_reason(refusal):
    """Map a template-read refusal to drift, or ``None`` for unavailable."""
    reason = _TEMPLATE_DRIFT.get(refusal.reason)
    if reason is not None:
        return reason
    # The adapter may translate a client error to ``AWS_REFUSED`` (definite
    # launch errors) or ``AWS_UNCERTAIN`` (everything else), depending on its
    # retry classification. The provider detail code decides: only an exact,
    # definitive not-found code proves absence; a denied, throttled or unknown
    # code stays unavailable.
    if refusal.detail in _TEMPLATE_NOT_FOUND_CODES:
        return "template_not_found"
    return None


def _route_target(route):
    for key in _ROUTE_TARGETS:
        value = route.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _default_routes(route_tables):
    """Return every default route, marking a blackholed destination."""
    routes = []
    for table in route_tables:
        for route in table.get("Routes") or []:
            if not isinstance(route, dict):
                continue
            destination = route.get("DestinationCidrBlock") or route.get("DestinationIpv6CidrBlock")
            if destination not in _DEFAULT_DESTINATIONS:
                continue
            routes.append(
                {
                    "destination": destination,
                    "target": _route_target(route),
                    "active": route.get("State", "active") != "blackhole",
                }
            )
    return routes


class Prerequisites:
    """One read-only comparison for one validated deployment configuration."""

    def __init__(self, config, clients, registration_parameter=DEFAULT_REGISTRATION_PARAMETER):
        self._config = config
        self._clients = clients
        self._registration_parameter = registration_parameter
        self._aws = Aws(config, client=clients.ec2)

    def run(self):
        checks = []
        templates = self._templates(checks)
        self._candidates(checks, templates)
        checks.append(self._registration_parameter_check())
        drift = [reason for check in checks if check["status"] == "drift" for reason in check.get("reasons", [])]
        unavailable = [
            reason for check in checks if check["status"] == "unavailable" for reason in check.get("reasons", [])
        ]
        # An incomplete check wins: a denied or unreadable read means the
        # answer is not known, even when an independent drift reason was found.
        # The drift reasons stay in ``reasons`` so they are not lost.
        if unavailable:
            decision = "unavailable"
        elif drift:
            decision = "drift"
        else:
            decision = "satisfied"
        return {
            "command": "check-prerequisites",
            "deployment": self._config.deployment,
            "region": self._config.aws.region,
            "config": str(self._config.path),
            "registration_parameter": self._registration_parameter,
            "decision": decision,
            "reasons": _unique(drift),
            "unavailable": _unique(unavailable),
            "checks": checks,
        }

    # -- launch template -------------------------------------------------
    def _templates(self, checks):
        """Read each distinct configured version once, through the adapter."""
        templates = {}
        expected = _repository_bootstrap()
        if expected is None:
            checks.append(
                _record(
                    "template_bootstrap",
                    "repository",
                    status="unavailable",
                    reasons=["template_bootstrap_source_unreadable"],
                    detail="deploy/runner/bootstrap.sh",
                )
            )
        name = self._config.aws.launch_template_name
        for version in sorted({candidate.launch_template_version for candidate in self._config.aws.zones}):
            target = f"{name}@{version}"
            try:
                template = self._aws.template(name, version)
            except ProviderRefusal as refusal:
                reason = _template_refusal_reason(refusal)
                if reason:
                    checks.append(_record("template", target, status="drift", reasons=[reason], detail=refusal.detail))
                else:
                    checks.append(
                        _record(
                            "template",
                            target,
                            status="unavailable",
                            reasons=["template_read_unavailable"],
                            detail=refusal.detail or refusal.reason,
                        )
                    )
                templates[version] = (None, None)
                continue
            try:
                response = _call(
                    self._clients.ec2,
                    "describe_launch_template_versions",
                    LaunchTemplateName=name,
                    Versions=[str(version)],
                )
            except ProviderCallError as error:
                checks.append(
                    _record(
                        "template",
                        target,
                        status="unavailable",
                        facts=_template_facts(template),
                        reasons=["template_read_unavailable"],
                        detail=error.code,
                    )
                )
                templates[version] = (template, None)
                continue
            data = _launch_template_data(response)
            if data is None:
                checks.append(
                    _record(
                        "template",
                        target,
                        status="unavailable",
                        facts=_template_facts(template),
                        reasons=["template_read_unavailable"],
                        detail="describe_launch_template_versions returned no version",
                    )
                )
                templates[version] = (template, None)
                continue
            facts = _template_facts(template, data)
            reasons = []
            if not facts["security_group_ids"] and not facts["security_group_names"]:
                reasons.append("template_security_group_not_pinned")
            if not facts["instance_profile"]:
                reasons.append("template_instance_profile_not_pinned")
            # A template without user data is reported by the per-candidate
            # check as `template_bootstrap_missing`; only a present-but-different
            # body is drift against the repository file.
            if expected is not None and facts["bootstrap_present"]:
                actual = _template_user_data(data)
                if actual is None or _without_trailing_newlines(actual) != _without_trailing_newlines(expected):
                    reasons.append("template_bootstrap_drift")
            checks.append(
                _record("template", target, status="drift" if reasons else "pass", facts=facts, reasons=reasons)
            )
            checks.extend(self._security_group_checks(data))
            instance_profile = self._instance_profile_check(data)
            if instance_profile is not None:
                checks.append(instance_profile)
            templates[version] = (template, data)
        return templates

    def _security_group_checks(self, data):
        calls = [
            (group_id, "GroupId", {"GroupIds": [group_id]}) for group_id in _security_group_ids(data)
        ]
        calls += [
            (name, "GroupName", {"Filters": [{"Name": "group-name", "Values": [name]}]})
            for name in _security_group_names(data)
        ]
        checks = []
        for target, key, arguments in calls:
            try:
                response = _call(self._clients.ec2, "describe_security_groups", **arguments)
            except ProviderCallError as error:
                if error.code in _MISSING_SECURITY_GROUP:
                    checks.append(_record("security_group", target, status="drift", reasons=["security_group_missing"]))
                else:
                    checks.append(
                        _record(
                            "security_group",
                            target,
                            status="unavailable",
                            reasons=["security_group_read_unavailable"],
                            detail=error.code,
                        )
                    )
                continue
            groups = response.get("SecurityGroups") or []
            if any(group.get(key) == target for group in groups):
                checks.append(_record("security_group", target))
            else:
                checks.append(_record("security_group", target, status="drift", reasons=["security_group_missing"]))
        return checks

    def _instance_profile_check(self, data):
        name, reference_arn = _instance_profile_reference(data)
        if not name:
            return None
        try:
            response = _call(self._clients.iam, "get_instance_profile", InstanceProfileName=name)
        except ProviderCallError as error:
            if error.code in _MISSING_INSTANCE_PROFILE:
                return _record("instance_profile", name, status="drift", reasons=["instance_profile_missing"])
            return _record(
                "instance_profile",
                name,
                status="unavailable",
                reasons=["instance_profile_unavailable"],
                detail=error.code,
            )
        profile = response.get("InstanceProfile")
        facts = {"role": None, "reference_arn": reference_arn, "arn": None}
        if isinstance(profile, dict):
            facts["arn"] = profile.get("Arn") if isinstance(profile.get("Arn"), str) else None
            roles = profile.get("Roles")
            facts["role"] = next(
                (
                    entry.get("RoleName")
                    for entry in roles or []
                    if isinstance(entry, dict) and entry.get("RoleName")
                ),
                None,
            )
        reasons = []
        if reference_arn is not None and facts["arn"] != reference_arn:
            reasons.append("instance_profile_arn_mismatch")
        if not facts["role"]:
            reasons.append("instance_profile_role_missing")
        return _record("instance_profile", name, status="drift" if reasons else "pass", facts=facts, reasons=reasons)

    # -- network placement ------------------------------------------------
    def _candidates(self, checks, templates):
        for candidate in self._config.aws.zones:
            target = self._candidate_target(candidate)
            template, data = templates.get(candidate.launch_template_version, (None, None))
            reasons = []
            if template is not None:
                if not template.has_user_data:
                    reasons.append("template_bootstrap_missing")
                if template.placement_zone is not None and template.placement_zone != candidate.availability_zone:
                    reasons.append("template_placement_mismatch")
                if candidate.subnet_id is not None and data is not None and _network_pinned(data):
                    reasons.append("template_network_pinned")
            checks.append(
                _record(
                    "candidate",
                    target,
                    status="drift" if reasons else "pass",
                    facts={
                        "zone": candidate.availability_zone,
                        "subnet_id": candidate.subnet_id,
                        "launch_template_version": candidate.launch_template_version,
                    },
                    reasons=reasons,
                )
            )
            if candidate.subnet_id is None:
                # The legacy single-zone form keeps the network in the
                # template; there is no configured subnet to verify.
                continue
            subnet_check, placement_facts = self._subnet(candidate)
            if subnet_check is not None:
                checks.append(subnet_check)
                continue
            checks.append(self._route_tables(candidate, placement_facts))

    def _subnet(self, candidate):
        """Return ``(record, None)`` on failure or ``(None, facts)`` on success."""
        target = self._candidate_target(candidate)
        try:
            response = _call(self._clients.ec2, "describe_subnets", SubnetIds=[candidate.subnet_id])
        except ProviderCallError as error:
            if error.code in _MISSING_SUBNET:
                return _record("placement", target, status="drift", reasons=["subnet_missing"]), None
            return (
                _record(
                    "placement",
                    target,
                    status="unavailable",
                    reasons=["subnet_read_unavailable"],
                    detail=error.code,
                ),
                None,
            )
        subnets = [subnet for subnet in response.get("Subnets") or [] if subnet.get("SubnetId") == candidate.subnet_id]
        if not subnets:
            return _record("placement", target, status="drift", reasons=["subnet_missing"]), None
        subnet = subnets[0]
        facts = {
            "zone": candidate.availability_zone,
            "subnet_id": candidate.subnet_id,
            "vpc_id": subnet.get("VpcId"),
            "subnet_zone": subnet.get("AvailabilityZone"),
            "subnet_state": subnet.get("State"),
        }
        reasons = []
        if subnet.get("AvailabilityZone") != candidate.availability_zone:
            reasons.append("subnet_zone_mismatch")
        if subnet.get("State") not in (None, "available"):
            reasons.append("subnet_not_available")
        if reasons:
            return _record("placement", target, status="drift", facts=facts, reasons=reasons), None
        # Carry the already-read facts into the route check's record.
        return None, facts

    def _route_tables(self, candidate, facts):
        target = self._candidate_target(candidate)
        tables = []
        try:
            response = _call(
                self._clients.ec2,
                "describe_route_tables",
                Filters=[{"Name": "association.subnet-id", "Values": [candidate.subnet_id]}],
            )
            tables = response.get("RouteTables") or []
            if not tables and facts["vpc_id"]:
                # A subnet with no explicit association uses its VPC's main
                # route table. Check that one instead of assuming a route.
                response = _call(
                    self._clients.ec2,
                    "describe_route_tables",
                    Filters=[
                        {"Name": "vpc-id", "Values": [facts["vpc_id"]]},
                        {"Name": "association.main", "Values": ["true"]},
                    ],
                )
                tables = response.get("RouteTables") or []
        except ProviderCallError as error:
            return _record(
                "placement",
                target,
                status="unavailable",
                facts=facts,
                reasons=["route_table_read_unavailable"],
                detail=error.code,
            )
        if not tables:
            return _record("placement", target, status="drift", facts=facts, reasons=["route_table_missing"])
        facts["route_table_ids"] = [table.get("RouteTableId") for table in tables if table.get("RouteTableId")]
        routes = _default_routes(tables)
        active = [route for route in routes if route["active"] and route["target"]]
        if active:
            facts["egress_route"] = {"destination": active[0]["destination"], "target": active[0]["target"]}
            return _record("placement", target, facts=facts)
        if routes:
            return _record("placement", target, status="drift", facts=facts, reasons=["egress_route_blackhole"])
        return _record("placement", target, status="drift", facts=facts, reasons=["egress_route_missing"])

    @staticmethod
    def _candidate_target(candidate):
        if candidate.subnet_id is None:
            return candidate.availability_zone
        return f"{candidate.availability_zone}/{candidate.subnet_id}"

    # -- registration secret ----------------------------------------------
    def _registration_parameter_check(self):
        name = self._registration_parameter
        token = None
        for _ in range(MAX_PARAMETER_PAGES):
            arguments = {"ParameterFilters": [{"Key": "Name", "Option": "Equals", "Values": [name]}]}
            if token:
                arguments["NextToken"] = token
            try:
                # ``DescribeParameters`` returns metadata only. The value is
                # never requested, decrypted, compared, printed or stored.
                response = _call(self._clients.ssm, "describe_parameters", **arguments)
            except ProviderCallError as error:
                return _record(
                    "registration_parameter",
                    name,
                    status="unavailable",
                    reasons=["registration_parameter_unavailable"],
                    detail=error.code,
                )
            names = [
                parameter.get("Name")
                for parameter in response.get("Parameters") or []
                if isinstance(parameter, dict)
            ]
            if name in names:
                return _record("registration_parameter", name)
            token = response.get("NextToken")
            if not token:
                return _record(
                    "registration_parameter",
                    name,
                    status="drift",
                    reasons=["registration_parameter_missing"],
                )
        # An unexhausted listing is not proof that the parameter is absent.
        return _record(
            "registration_parameter",
            name,
            status="unavailable",
            reasons=["registration_parameter_unavailable"],
            detail="pagination limit reached",
        )


def _unique(values):
    return list(dict.fromkeys(values))


def build_parser():
    parser = argparse.ArgumentParser(
        prog="check-prerequisites",
        description="Read-only verification of one ci-burst deployment's worker prerequisites.",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG,
        help=f"path to the deployment TOML file (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--registration-parameter",
        default=DEFAULT_REGISTRATION_PARAMETER,
        help="SSM parameter name to check for existence (default: the tutorial's path)",
    )
    return parser


def _is_credential_failure(error):
    """True for an expected credential/session setup failure, by class name."""
    return any(candidate.__name__ in _CREDENTIAL_FAILURES for candidate in type(error).__mro__)


def build_clients(config):
    """Build one credential route for EC2, SSM and IAM.

    The EC2 client comes from the controller's own factory and the other
    clients share its resolved session, so the check reads the same account and
    profile the controller would. Expected session and credential failures are
    re-raised as ``AWS_CREDENTIALS_UNAVAILABLE`` with the exception class name
    only: their messages can carry a profile name or a file path.
    """
    try:
        aws = Aws(config)
        # Resolve the session first: the adapter's lazy SDK import reports a
        # missing boto3 as ``AWS_SDK_UNAVAILABLE`` instead of a bare ImportError.
        session = aws._session()
        ec2 = aws._client()
        return Clients(
            ec2=ec2,
            ssm=session.client("ssm", region_name=config.aws.region),
            iam=session.client("iam", region_name=config.aws.region),
        )
    except Exception as error:
        if _is_credential_failure(error):
            raise ProviderRefusal("AWS_CREDENTIALS_UNAVAILABLE", type(error).__name__) from None
        raise


def check(config, clients, registration_parameter=DEFAULT_REGISTRATION_PARAMETER):
    return Prerequisites(config, clients, registration_parameter).run()


def _exit_code(decision):
    return {"satisfied": EXIT_SATISFIED, "drift": EXIT_DRIFT, "unavailable": EXIT_UNAVAILABLE}.get(
        decision, EXIT_UNEXPECTED
    )


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    try:
        config = load(arguments.config)
    except ConfigRefusal as refusal:
        print(
            json.dumps(
                {"command": "check-prerequisites", "decision": "unavailable", "error": refusal.as_dict()},
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_UNAVAILABLE
    try:
        clients = build_clients(config)
        result = check(config, clients, arguments.registration_parameter)
    except Refusal as refusal:
        print(
            json.dumps(
                {"command": "check-prerequisites", "decision": "unavailable", "error": refusal.as_dict()},
                indent=2,
                sort_keys=True,
            )
        )
        return EXIT_UNAVAILABLE
    except Exception as error:  # noqa: BLE001 - the boundary reports a type name only
        print(json.dumps({"error": {"reason": "UNEXPECTED", "detail": type(error).__name__}}, indent=2))
        return EXIT_UNEXPECTED
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return _exit_code(result["decision"])


if __name__ == "__main__":
    sys.exit(main())
