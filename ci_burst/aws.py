"""Thin AWS adapter with a lazy SDK import.

Only the calls the controller needs are wrapped: read an immutable launch
template version, read a fresh Spot quote, launch one instance, discover owned
instances and request termination. The adapter never logs request parameters,
user data or credential material; a provider failure is reported as its error
code only.

``boto3`` is imported on first use, so the tests run on a checkout with no cloud
SDK installed. A caller may inject a client, which the adapter-boundary tests
use to exercise pagination and response parsing without any SDK.
"""

import time
from dataclasses import dataclass
from datetime import datetime, timezone

from .errors import ProviderRefusal
from .money import parse_money

MANAGED_TAG = "ci-burst:managed"
DEPLOYMENT_TAG = "ci-burst:deployment"
OPERATION_TAG = "ci-burst:operation"
EXPIRES_TAG = "ci-burst:expires-at"

MAX_INVENTORY_PAGES = 20

#: The controller owns retries: an unresolved launch is resumed with its frozen
#: client token. Botocore's transparent retries would hide that a launch may
#: already have been sent, so the client is built without them.
RETRY_POLICY = {"total_max_attempts": 1, "mode": "standard"}

#: Only these client error codes prove that RunInstances did not create an
#: instance. Everything else -- throttling, service errors, unknown codes --
#: stays uncertain, because rejecting a retry does not prove the first request
#: created nothing.
DEFINITE_LAUNCH_ERRORS = frozenset(
    {
        "InvalidParameterValue",
        "InvalidParameterCombination",
        "InvalidParameter",
        "MissingParameter",
        "UnauthorizedOperation",
        "AuthFailure",
        "InvalidAMIID.NotFound",
        "InvalidAMIID.Malformed",
        "InvalidLaunchTemplateId.NotFound",
        "InvalidLaunchTemplateName.NotFoundException",
        "InsufficientInstanceCapacity",
    }
)


@dataclass(frozen=True)
class Template:
    """Facts from one immutable launch template version."""

    name: str
    template_id: str
    version: int
    image_id: str
    instance_type: str
    shutdown_behavior: str
    has_user_data: bool
    placement_zone: str | None

    def as_dict(self):
        return {
            "name": self.name,
            "id": self.template_id,
            "version": self.version,
            "image_id": self.image_id,
            "instance_type": self.instance_type,
            "shutdown_behavior": self.shutdown_behavior,
            "bootstrap_present": self.has_user_data,
            "placement_zone": self.placement_zone,
        }


@dataclass(frozen=True)
class SpotQuote:
    price_micros: int
    timestamp: float
    price_display: str
    instance_type: str
    availability_zone: str

    def as_dict(self):
        return {
            "price_display": self.price_display,
            "timestamp": self.timestamp,
            "instance_type": self.instance_type,
            "availability_zone": self.availability_zone,
        }


@dataclass(frozen=True)
class Instance:
    instance_id: str
    state: str
    tags: dict
    launch_time: float | None

    def as_dict(self):
        return {
            "instance_id": self.instance_id,
            "state": self.state,
            "operation": self.tags.get(OPERATION_TAG),
            "expires_at": self.tags.get(EXPIRES_TAG),
        }


class Aws:
    """Live adapter. Construct once per command."""

    def __init__(self, config, client=None, clock=time.time):
        self._config = config
        self._client_cache = client
        self._clock = clock

    def _client(self):
        if self._client_cache is not None:
            return self._client_cache
        try:
            import boto3
        except ImportError:
            raise ProviderRefusal("AWS_SDK_UNAVAILABLE", "install the boto3 runtime dependency") from None
        from botocore.config import Config

        session = boto3.Session(profile_name=self._config.aws.profile)
        self._client_cache = session.client(
            "ec2",
            region_name=self._config.aws.region,
            config=Config(retries=RETRY_POLICY),
        )
        return self._client_cache

    def _call(self, method, **kwargs):
        client = self._client()
        try:
            return getattr(client, method)(**kwargs)
        except Exception as error:  # noqa: BLE001 - classified below
            raise _translate(error) from None

    # -- reads ----------------------------------------------------------
    def template(self, name, version):
        response = self._call(
            "describe_launch_template_versions",
            LaunchTemplateName=name,
            Versions=[str(version)],
        )
        versions = response.get("LaunchTemplateVersions") or []
        if not versions:
            raise ProviderRefusal("TEMPLATE_NOT_FOUND", f"{name} version {version}")
        entry = versions[0]
        data = entry.get("LaunchTemplateData") or {}
        image_id = data.get("ImageId")
        if not isinstance(image_id, str) or not image_id:
            raise ProviderRefusal("TEMPLATE_IMAGE_NOT_LITERAL", "LaunchTemplateData.ImageId is absent")
        if image_id.startswith("resolve:"):
            raise ProviderRefusal("TEMPLATE_IMAGE_NOT_LITERAL", "resolved image reference")
        instance_type = data.get("InstanceType")
        if not isinstance(instance_type, str) or not instance_type:
            raise ProviderRefusal("TEMPLATE_INSTANCE_TYPE_MISSING", "LaunchTemplateData.InstanceType is absent")
        shutdown = data.get("InstanceInitiatedShutdownBehavior")
        if shutdown != "terminate":
            raise ProviderRefusal(
                "TEMPLATE_SHUTDOWN_NOT_TERMINATE", "InstanceInitiatedShutdownBehavior must be terminate"
            )
        has_user_data = bool(data.get("UserData"))
        placement = data.get("Placement") or {}
        placement_zone = placement.get("AvailabilityZone")
        return Template(
            name=name,
            template_id=entry.get("LaunchTemplateId", ""),
            version=int(entry.get("VersionNumber", version)),
            image_id=image_id,
            instance_type=instance_type,
            shutdown_behavior=shutdown,
            has_user_data=has_user_data,
            placement_zone=placement_zone if isinstance(placement_zone, str) and placement_zone else None,
        )

    def spot_quote(self, instance_type, availability_zone):
        start = datetime.fromtimestamp(self._clock() - self._config.aws.price_max_age_seconds, timezone.utc)
        response = self._call(
            "describe_spot_price_history",
            InstanceTypes=[instance_type],
            AvailabilityZone=availability_zone,
            ProductDescriptions=["Linux/UNIX"],
            StartTime=start,
        )
        return _spot_quote(response.get("SpotPriceHistory") or [], instance_type, availability_zone)

    def owned_instances(self, deployment):
        filters = [
            {"Name": "tag:" + MANAGED_TAG, "Values": ["true"]},
            {"Name": "tag:" + DEPLOYMENT_TAG, "Values": [deployment]},
        ]
        items, token, pages = [], None, 0
        while True:
            arguments = {"Filters": filters}
            if token:
                arguments["NextToken"] = token
            response = self._call("describe_instances", **arguments)
            items.extend(_instances(response))
            token = response.get("NextToken")
            pages += 1
            if not token:
                return items
            if pages >= MAX_INVENTORY_PAGES:
                raise ProviderRefusal("AWS_INVENTORY_INCOMPLETE", "DescribeInstances pagination limit reached")

    def instances(self, instance_ids):
        """Read the named instances; an aged-out record is an empty result.

        `InvalidInstanceID.NotFound` is not proof that the instance never
        existed or that it terminated at a particular time; the controller
        applies its own retained-visibility window before treating absence as
        terminal.
        """
        if not instance_ids:
            return []
        client = self._client()
        try:
            response = client.describe_instances(InstanceIds=list(instance_ids))
        except Exception as error:  # noqa: BLE001 - classified below
            if _error_code(error) == "InvalidInstanceID.NotFound":
                return []
            raise _translate(error) from None
        return _instances(response)

    # -- mutations ------------------------------------------------------
    def run_spot(self, request):
        response = self._call("run_instances", **request)
        instances = response.get("Instances") or []
        identifiers = [item.get("InstanceId") for item in instances if item.get("InstanceId")]
        if len(identifiers) != 1:
            raise ProviderRefusal("AWS_UNCERTAIN", "launch response did not identify exactly one instance", uncertain=True)
        return identifiers

    def terminate(self, instance_ids):
        if not instance_ids:
            return {}
        return self._call("terminate_instances", InstanceIds=list(instance_ids))


def _translate(error):
    """Map SDK failures to refusals without importing the SDK for tests."""
    try:
        import botocore.exceptions as botocore
    except ImportError:
        return ProviderRefusal("AWS_UNCERTAIN", type(error).__name__, uncertain=True)
    if isinstance(error, botocore.ClientError):
        code = str(error.response.get("Error", {}).get("Code", "Unknown"))
        if not _definite_launch_error(code):
            return ProviderRefusal("AWS_UNCERTAIN", code, uncertain=True)
        return ProviderRefusal("AWS_REFUSED", code)
    if isinstance(error, botocore.BotoCoreError):
        return ProviderRefusal("AWS_UNCERTAIN", type(error).__name__, uncertain=True)
    # A response-parser failure or another unclassified exception can occur
    # after the service accepted the request. It is never proof of non-creation.
    return ProviderRefusal("AWS_UNCERTAIN", type(error).__name__, uncertain=True)


def _error_code(error):
    response = getattr(error, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def _definite_launch_error(code):
    return code in DEFINITE_LAUNCH_ERRORS


def _timestamp(value):
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _spot_quote(entries, instance_type, availability_zone):
    """Pick the newest quote in the requested placement scope."""
    # The response must identify its own scope. A missing field is not
    # replaced with the value we asked for, because that would fabricate
    # verification evidence the provider never supplied.
    scoped = [
        entry
        for entry in entries
        if entry.get("AvailabilityZone") == availability_zone
        and entry.get("InstanceType") == instance_type
        and entry.get("ProductDescription") == "Linux/UNIX"
    ]
    if not scoped:
        return None
    newest = max(scoped, key=lambda entry: _timestamp(entry.get("Timestamp")) or 0.0)
    timestamp = _timestamp(newest.get("Timestamp"))
    price = newest.get("SpotPrice")
    if timestamp is None or not isinstance(price, str):
        return None
    try:
        micros = parse_money(price, "SpotPrice")
    except ValueError:
        return None
    return SpotQuote(
        price_micros=micros,
        timestamp=timestamp,
        price_display=price,
        instance_type=instance_type,
        availability_zone=availability_zone,
    )


def _instances(response):
    result = []
    for reservation in response.get("Reservations") or []:
        for item in reservation.get("Instances") or []:
            tags = {tag.get("Key"): tag.get("Value") for tag in item.get("Tags") or []}
            launch_time = item.get("LaunchTime")
            result.append(
                Instance(
                    instance_id=item.get("InstanceId", ""),
                    state=(item.get("State") or {}).get("Name", "unknown"),
                    tags=tags,
                    launch_time=_timestamp(launch_time),
                )
            )
    return result
