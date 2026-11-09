"""Thin AWS adapter with a lazy SDK import.

Only the calls the controller needs are wrapped: read an immutable launch
template version, read a fresh Spot quote, launch one instance, discover owned
instances and request termination. The adapter never logs request parameters,
user data or credential material; a provider failure is reported as its error
code only.

``boto3`` is imported on first use, so the population tests and the observation
and planning code paths run on a checkout with no cloud SDK installed.
"""

import time
from dataclasses import dataclass

from .errors import ProviderRefusal
from .money import parse_money

MANAGED_TAG = "ci-burst:managed"
DEPLOYMENT_TAG = "ci-burst:deployment"
OPERATION_TAG = "ci-burst:operation"
EXPIRES_TAG = "ci-burst:expires-at"


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

    def as_dict(self):
        return {
            "name": self.name,
            "id": self.template_id,
            "version": self.version,
            "image_id": self.image_id,
            "instance_type": self.instance_type,
            "shutdown_behavior": self.shutdown_behavior,
            "bootstrap_present": self.has_user_data,
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

    def __init__(self, config):
        self._config = config
        self._client_cache = None

    def _client(self):
        if self._client_cache is not None:
            return self._client_cache
        try:
            import boto3
            import botocore.exceptions  # noqa: F401  (availability check only)
        except ImportError:
            raise ProviderRefusal("AWS_SDK_UNAVAILABLE", "install the boto3 runtime dependency") from None
        session = boto3.Session(profile_name=self._config.aws.profile)
        self._client_cache = session.client("ec2", region_name=self._config.aws.region)
        return self._client_cache

    def _call(self, method, **kwargs):
        import botocore.exceptions as botocore

        client = self._client()
        try:
            return getattr(client, method)(**kwargs)
        except botocore.ClientError as error:
            code = error.response.get("Error", {}).get("Code", "Unknown")
            raise ProviderRefusal("AWS_REFUSED", str(code)) from None
        except botocore.BotoCoreError as error:
            raise ProviderRefusal("AWS_UNCERTAIN", type(error).__name__, uncertain=True) from None

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
        return Template(
            name=name,
            template_id=entry.get("LaunchTemplateId", ""),
            version=int(entry.get("VersionNumber", version)),
            image_id=image_id,
            instance_type=instance_type,
            shutdown_behavior=shutdown,
            has_user_data=has_user_data,
        )

    def spot_quote(self, instance_type, availability_zone):
        start = time.time() - self._config.aws.price_max_age_seconds
        response = self._call(
            "describe_spot_price_history",
            InstanceTypes=[instance_type],
            AvailabilityZone=availability_zone,
            ProductDescriptions=["Linux/UNIX"],
            StartTime=start,
        )
        entries = response.get("SpotPriceHistory") or []
        if not entries:
            return None
        latest = max(entries, key=lambda item: item.get("Timestamp", 0))
        price = latest.get("SpotPrice")
        if not isinstance(price, str):
            return None
        try:
            micros = parse_money(price, "SpotPrice")
        except ValueError:
            return None
        return SpotQuote(
            price_micros=micros,
            timestamp=float(latest.get("Timestamp", 0)),
            price_display=price,
            instance_type=instance_type,
            availability_zone=availability_zone,
        )

    def owned_instances(self, deployment):
        response = self._call(
            "describe_instances",
            Filters=[
                {"Name": "tag:" + MANAGED_TAG, "Values": ["true"]},
                {"Name": "tag:" + DEPLOYMENT_TAG, "Values": [deployment]},
            ],
        )
        return _instances(response)

    def instances(self, instance_ids):
        if not instance_ids:
            return []
        import botocore.exceptions as botocore

        client = self._client()
        try:
            response = client.describe_instances(InstanceIds=list(instance_ids))
        except botocore.ClientError as error:
            if error.response.get("Error", {}).get("Code") in ("InvalidInstanceID.NotFound",):
                return []
            raise ProviderRefusal("AWS_REFUSED", str(error.response.get("Error", {}).get("Code", "Unknown"))) from None
        except botocore.BotoCoreError as error:
            raise ProviderRefusal("AWS_UNCERTAIN", type(error).__name__, uncertain=True) from None
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
                    launch_time=launch_time.timestamp() if launch_time is not None else None,
                )
            )
    return result
