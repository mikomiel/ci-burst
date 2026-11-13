"""Thin AWS adapter with a lazy SDK import.

Only the calls the controller needs are wrapped: read an immutable launch
template version, read a fresh Spot quote, launch one instance per admitted
worker, discover owned instances and request termination. The adapter never logs
request parameters, user data or credential material; a provider failure is
reported as its error code only.

``boto3`` is imported on first use, so the tests run on a checkout with no cloud
SDK installed. A caller may inject a client, which the adapter-boundary tests
use to exercise pagination and response parsing without any SDK.
"""

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from .credit import CreditSpecification
from .errors import DeadlineRefusal, ProviderRefusal
from .money import parse_money

MANAGED_TAG = "ci-burst:managed"
DEPLOYMENT_TAG = "ci-burst:deployment"
OPERATION_TAG = "ci-burst:operation"
EXPIRES_TAG = "ci-burst:expires-at"

MAX_INVENTORY_PAGES = 20

#: One console read is bounded so a single image cannot turn an observation run
#: into an unbounded download. The provider caps the same call at this size.
MAX_CONSOLE_BYTES = 64 * 1024

#: A CPU-credit value is a short enum token. The adapter keeps the template's
#: raw value so an unrecognized mode stays visible as unrecognized, but it does
#: not echo an arbitrary provider string as credit evidence. A present value
#: outside this bound keeps its presence without the payload, never absence.
MAX_CREDIT_SPECIFICATION_CHARS = 32

#: The controller owns retries: an unresolved launch is resumed with its frozen
#: client token. Botocore's transparent retries would hide that a launch may
#: already have been sent, so the client is built without them.
RETRY_POLICY = {"total_max_attempts": 1, "mode": "standard"}

#: Transport bounds for one provider call. They stop a stalled connection or
#: response from consuming the pass budget; retry counts stay unchanged.
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 30

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
        # The account's Spot limit is an account-wide pre-flight refusal: the
        # request never reached a placement, so nothing was created. It is
        # definite, and it is not that candidate's capacity, so it must not
        # advance to the next configured candidate.
        "MaxSpotInstanceCountExceeded",
    }
)

#: Launch error codes that prove the provider rejected the request because the
#: requested placement had no Spot capacity. Every code here is also a definite
#: launch error, so the rejection proves that no instance was created and the
#: same admission may advance to the next configured candidate. No other error
#: is capacity-related: in particular a throttled or unclassified failure may
#: have created an instance and must not be retried in another zone.
CAPACITY_LAUNCH_ERRORS = frozenset({"InsufficientInstanceCapacity"})


def is_capacity_failure(refusal):
    """True only for a definite, capacity-related launch rejection."""
    return (
        not refusal.uncertain
        and refusal.reason == "AWS_REFUSED"
        and refusal.detail in CAPACITY_LAUNCH_ERRORS
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
    #: The template's CPU-credit choice. ``present`` keeps a value the adapter
    #: cannot represent from reading as absence; ``credit.evidence`` decides
    #: which bounded values are recognized. The launch request never sets one;
    #: the template owns this choice.
    credit_specification: CreditSpecification = CreditSpecification()

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
    """One observed Spot price.

    ``timestamp`` is the provider's own record time, which for a Spot price is
    the moment the price last **changed**, not the moment it was read.
    ``observed_at`` is when this controller read it, and it is the value a
    freshness bound must use: a price that has been stable for hours is still a
    current price. Both are kept so the evidence shows what the provider said
    and when it was asked.
    """

    price_micros: int
    timestamp: float
    price_display: str
    instance_type: str
    availability_zone: str
    observed_at: float

    def as_dict(self):
        return {
            "price_display": self.price_display,
            "timestamp": self.timestamp,
            "observed_at": self.observed_at,
            "instance_type": self.instance_type,
            "availability_zone": self.availability_zone,
        }


@dataclass(frozen=True)
class Instance:
    instance_id: str
    state: str
    tags: dict
    launch_time: float | None
    availability_zone: str | None = None

    def as_dict(self):
        return {
            "instance_id": self.instance_id,
            "state": self.state,
            "operation": self.tags.get(OPERATION_TAG),
            "expires_at": self.tags.get(EXPIRES_TAG),
            "availability_zone": self.availability_zone,
        }


class Aws:
    """Live adapter. Construct once per command."""

    def __init__(self, config, client=None, clock=time.time, account=None):
        self._config = config
        self._client_cache = client
        self._clock = clock
        self._session_cache = None
        # ``client`` and ``account`` let adapter-boundary tests supply the
        # provider identity without an SDK or network.
        self._account_cache = account

    def _session(self):
        """One resolved credential identity for identity and EC2 calls."""
        if self._session_cache is not None:
            return self._session_cache
        try:
            import boto3
        except ImportError:
            raise ProviderRefusal("AWS_SDK_UNAVAILABLE", "install the boto3 runtime dependency") from None
        self._session_cache = boto3.Session(profile_name=self._config.aws.profile)
        return self._session_cache

    def _client_config(self):
        """The one client configuration: guarded SDK import plus transport bounds."""
        return _sdk_config(
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            read_timeout=READ_TIMEOUT_SECONDS,
        )

    def _client(self):
        if self._client_cache is not None:
            return self._client_cache
        # Resolve the guarded configuration first: a checkout without the SDK
        # must refuse here, before the session is touched.
        config = self._client_config()
        self._client_cache = self._session().client(
            "ec2",
            region_name=self._config.aws.region,
            config=config,
        )
        return self._client_cache

    def _check_deadline(self, deadline, detail):
        if deadline is not None and self._clock() >= deadline:
            raise DeadlineRefusal(detail)

    def _call(self, method, **kwargs):
        client = self._client()
        try:
            return getattr(client, method)(**kwargs)
        except Exception as error:  # noqa: BLE001 - classified below
            raise _translate(error) from None

    def account_id(self):
        """Return the AWS account the configured credential route reaches."""
        if self._account_cache is not None:
            return self._account_cache
        config = self._client_config()
        sts = self._session().client("sts", region_name=self._config.aws.region, config=config)
        try:
            response = sts.get_caller_identity()
        except Exception as error:  # noqa: BLE001 - classified below
            raise _translate(error) from None
        account = response.get("Account")
        if not isinstance(account, str) or not account:
            raise ProviderRefusal("AWS_ACCOUNT_UNKNOWN", "get_caller_identity returned no account")
        self._account_cache = account
        return account

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
            credit_specification=_credit_specification(data),
        )

    def spot_quote(self, instance_type, availability_zone, *, deadline=None):
        self._check_deadline(deadline, "Spot price read")
        start = datetime.fromtimestamp(self._clock() - self._config.aws.price_max_age_seconds, timezone.utc)
        response = self._call(
            "describe_spot_price_history",
            InstanceTypes=[instance_type],
            AvailabilityZone=availability_zone,
            ProductDescriptions=["Linux/UNIX"],
            StartTime=start,
        )
        return _spot_quote(
            response.get("SpotPriceHistory") or [],
            instance_type,
            availability_zone,
            self._clock(),
        )

    def owned_instances(self, deployment, *, deadline=None):
        filters = [
            {"Name": "tag:" + MANAGED_TAG, "Values": ["true"]},
            {"Name": "tag:" + DEPLOYMENT_TAG, "Values": [deployment]},
        ]
        items, token, pages = [], None, 0
        while True:
            self._check_deadline(deadline, "DescribeInstances page")
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

    def instances(self, instance_ids, *, deadline=None):
        """Read the named instances, establishing absence per identifier.

        An EC2 by-ID read is rejected for the whole batch when any one
        identifier is unknown, so a rejected batch proves nothing about its
        other members. Absence is therefore established only for an identifier
        read on its own: a record that exists is returned even beside an
        aged-out one, and any other read failure -- of the batch or of one
        identifier -- refuses with an uncertain refusal, because the state of
        that identifier cannot be established, rather than reporting the set
        as empty.

        `InvalidInstanceID.NotFound` is not proof that the instance never
        existed or that it terminated at a particular time; the controller
        applies its own retained-visibility window before treating absence as
        terminal.
        """
        identifiers = list(instance_ids)
        if not identifiers:
            return []
        self._check_deadline(deadline, "DescribeInstances by-id read")
        client = self._client()
        try:
            response = client.describe_instances(InstanceIds=identifiers)
        except Exception as error:  # noqa: BLE001 - classified below
            if _error_code(error) != "InvalidInstanceID.NotFound":
                raise _translate_read(error) from None
            found = []
            for instance_id in identifiers:
                self._check_deadline(deadline, "DescribeInstances by-id read")
                try:
                    response = client.describe_instances(InstanceIds=[instance_id])
                except Exception as member:  # noqa: BLE001 - classified below
                    if _error_code(member) == "InvalidInstanceID.NotFound":
                        continue
                    raise _translate_read(member) from None
                found.extend(_instances(response))
            return found
        return _instances(response)

    def console_output(self, instance_ids, *, deadline=None):
        """Read each owned instance's console once, bounded.

        The SDK presents ``Output`` as plaintext (the EC2 model declares it a
        string), so there is nothing to decode here. A response that is not a
        string is treated as no evidence rather than as a payload to guess at.

        An instance that aged out of the provider's record window is absent from
        the result rather than an error: absence after termination is the normal
        end of an instance, and the console evidence it produced was already
        read while it existed.
        """
        consoles = {}
        for instance_id in instance_ids:
            self._check_deadline(deadline, "GetConsoleOutput read")
            try:
                response = self._client().get_console_output(InstanceId=instance_id, Latest=True)
            except Exception as error:  # noqa: BLE001 - classified below
                if _error_code(error) == "InvalidInstanceID.NotFound":
                    continue
                raise _translate(error) from None
            output = response.get("Output")
            if not isinstance(output, str) or not output:
                consoles[instance_id] = ""
                continue
            payload = output.encode("utf-8", "replace")[:MAX_CONSOLE_BYTES]
            consoles[instance_id] = payload.decode("utf-8", "replace")
        return consoles

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


def _sdk_config(**overrides):
    """Build the client configuration, refusing when the runtime SDK is absent.

    The lazy import guard belongs at every entry point that needs an SDK type,
    not only at the session: a checkout without ``boto3`` must report the
    documented refusal instead of an import traceback.
    """
    try:
        from botocore.config import Config
    except ImportError:
        raise ProviderRefusal("AWS_SDK_UNAVAILABLE", "install the boto3 runtime dependency") from None
    return Config(retries=RETRY_POLICY, **overrides)


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


def _translate_read(error):
    """Map a read failure to an uncertain refusal.

    A read that failed cannot establish the state of the identifier it asked
    about, whatever the provider's error code would mean for a launch. The
    translated reason code and detail are preserved so the failure stays
    diagnosable.
    """
    refusal = _translate(error)
    return ProviderRefusal(refusal.reason, refusal.detail, uncertain=True)


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


def _credit_specification(data):
    """Return the template's CPU-credit choice, preserving presence.

    A ``CreditSpecification`` the response sets is the template's choice even
    when its ``CpuCredits`` cannot be represented -- empty, non-printable,
    non-ASCII or over-long. Such a value is kept as present with a ``None``
    value: it is unknown credit evidence, and returning absence would report the
    family default as if the template had chosen it. ``credit.evidence`` owns
    recognition; the adapter only bounds the echoed payload.
    """
    specification = data.get("CreditSpecification")
    if specification is None:
        return CreditSpecification()
    value = specification.get("CpuCredits") if isinstance(specification, dict) else None
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or not value.isprintable()
        or len(value) > MAX_CREDIT_SPECIFICATION_CHARS
    ):
        return CreditSpecification(present=True)
    return CreditSpecification(present=True, value=value)


def _spot_quote(entries, instance_type, availability_zone, observed_at):
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
        observed_at=observed_at,
    )


def _instances(response):
    """Parse one DescribeInstances response, refusing an incomplete record.

    An empty ``Reservations`` list is a complete empty inventory. Anything else
    that cannot be read as reservations of identified instances is not: a
    missing ``Reservations`` key is not proof that nothing is owned, and an
    entry without an identity cannot be reconciled, resumed or terminated. The
    adapter therefore refuses such a response as uncertain instead of
    substituting an empty record for it.
    """
    if not isinstance(response, Mapping):
        raise _incomplete_inventory("DescribeInstances returned no response mapping")
    reservations = response.get("Reservations")
    if not isinstance(reservations, list):
        raise _incomplete_inventory("DescribeInstances returned no Reservations list")
    result = []
    for reservation in reservations:
        if not isinstance(reservation, Mapping):
            raise _incomplete_inventory("DescribeInstances returned a reservation that is not a mapping")
        entries = reservation.get("Instances")
        if not isinstance(entries, list):
            raise _incomplete_inventory("DescribeInstances returned no Instances list")
        for item in entries:
            if not isinstance(item, Mapping):
                raise _incomplete_inventory("DescribeInstances returned an instance entry that is not a mapping")
            instance_id = item.get("InstanceId")
            if not isinstance(instance_id, str) or not instance_id:
                raise _incomplete_inventory("DescribeInstances returned an instance entry without an InstanceId")
            tags = {tag.get("Key"): tag.get("Value") for tag in item.get("Tags") or []}
            launch_time = item.get("LaunchTime")
            placement = item.get("Placement") or {}
            zone = placement.get("AvailabilityZone")
            result.append(
                Instance(
                    instance_id=instance_id,
                    state=(item.get("State") or {}).get("Name", "unknown"),
                    tags=tags,
                    launch_time=_timestamp(launch_time),
                    availability_zone=zone if isinstance(zone, str) and zone else None,
                )
            )
    return result


def _incomplete_inventory(detail):
    return ProviderRefusal("AWS_INVENTORY_INCOMPLETE", detail, uncertain=True)
