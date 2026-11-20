"""Thin AWS adapter with a lazy SDK import.

Only the calls the controller needs are wrapped: read an immutable launch
template version, read a fresh Spot quote, launch one instance per admitted
worker, discover owned instances and request termination. The adapter never logs
request parameters, user data or credential material; a provider failure is
reported as its error code only.

``boto3`` is imported on first use, so the tests run on a checkout with no cloud
SDK installed. A caller may inject a client, which the adapter-boundary tests
use to exercise pagination and response parsing without any SDK.

``ImageAws`` is the separate identity of the image-build path. It is built from
one named shared-config profile, never from ``[aws].profile``, and it can reach
only the EC2 and S3 services. The module has no IAM client. The presigned
archive URL, user data and the full template payload are never logged, echoed in
an error or placed in a returned summary. The module emits no log record of its
own; it raises the level of the SDK's loggers, whose DEBUG records would print
those values, and it strips the traceback from the one WARNING the SDK logs when
an assumed role's credentials fail to refresh (see ``_quiet_sdk_loggers``).
"""

import contextlib
import copy
import hashlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .credit import CreditSpecification
from .errors import DeadlineRefusal, ProviderRefusal, Refusal
from .money import parse_money

MANAGED_TAG = "ci-burst:managed"
DEPLOYMENT_TAG = "ci-burst:deployment"
OPERATION_TAG = "ci-burst:operation"
EXPIRES_TAG = "ci-burst:expires-at"

#: Image-build tags. A builder carries ``IMAGE_BUILD_TAG`` and never
#: ``MANAGED_TAG``: ``owned_instances`` selects on the managed tag, so a builder
#: is invisible to worker discovery, ``controller.max_instances`` and ``reap``.
IMAGE_BUILD_TAG = "ci-burst:image-build"
BUILD_ID_TAG = "ci-burst:build-id"
BASIS_DIGEST_TAG = "ci-burst:basis-digest"

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
    instance_type: str | None = None

    def as_dict(self):
        return {
            "instance_id": self.instance_id,
            "state": self.state,
            "operation": self.tags.get(OPERATION_TAG),
            "expires_at": self.tags.get(EXPIRES_TAG),
            "availability_zone": self.availability_zone,
            "instance_type": self.instance_type,
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


#: Instance states in which a builder still exists or may be started again. A
#: terminated builder is gone and does not block the next one.
BUILDER_STATES = ("pending", "running", "stopping", "stopped")

#: The image identity reaches only these services. There is deliberately no IAM
#: client: the role that this path assumes is created and widened by a person.
IMAGE_SERVICES = frozenset({"ec2", "s3"})

#: Multipart part size. S3 refuses a part below 5 MiB except the last one.
ARCHIVE_PART_BYTES = 8 * 1024 * 1024

#: Resource types that carry the build tags on ``RunInstances``.
BUILDER_TAGGED_RESOURCES = ("instance", "volume", "network-interface")

#: Tag keys that ``create_image`` sets itself. A caller's extra tags may not
#: replace them, and none may be the managed tag.
_IMAGE_RESERVED_TAGS = frozenset({MANAGED_TAG, DEPLOYMENT_TAG, IMAGE_BUILD_TAG, BUILD_ID_TAG, BASIS_DIGEST_TAG})

#: What botocore calls credentials that come from a profile's ``role_arn``
#: (``AssumeRoleProvider.METHOD``). A test with the real SDK pins this value to
#: the installed botocore.
ASSUME_ROLE_METHOD = "assume-role"

#: Profile settings that select a credential provider other than static keys:
#: an ambient endpoint, a web identity token file, a command, a console login
#: session or (every key that starts with ``_SSO_PREFIX``) an SSO session. None
#: may appear in any profile of the image profile's ``source_profile`` chain. A
#: test with the real SDK lists botocore's profile providers, so a new one is noticed.
_AMBIENT_PROFILE_KEYS = frozenset(
    {"credential_source", "web_identity_token_file", "credential_process", "login_session"}
)
_SSO_PREFIX = "sso_"

#: The static keys that must end the ``source_profile`` chain.
_STATIC_KEYS = ("aws_access_key_id", "aws_secret_access_key")

#: The most profiles a chain may span, from the image profile to the one that
#: holds the static keys. botocore has no limit and refuses only a cycle.
MAX_PROFILE_CHAIN = 5

#: The top-level SDK loggers. ``ImageAws`` keeps them, and the loggers below
#: them that exist when it is built, at INFO or above.
SDK_LOGGERS = ("botocore", "boto3", "urllib3")


@dataclass(frozen=True)
class Image:
    image_id: str
    state: str
    tags: dict
    snapshot_ids: tuple
    #: Epoch seconds from ``CreationDate``; ``None`` when the response gave no
    #: time that carries a UTC offset. The deletion rule treats ``None`` as an
    #: age it cannot prove and keeps the image.
    created_at: float | None = None

    def as_dict(self):
        return {"image_id": self.image_id, "state": self.state, "snapshot_ids": list(self.snapshot_ids)}


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    state: str
    tags: dict
    #: Epoch seconds from ``StartTime``, with the same ``None`` rule as ``Image``.
    created_at: float | None = None

    def as_dict(self):
        return {"snapshot_id": self.snapshot_id, "state": self.state}


@dataclass(frozen=True)
class LaunchTemplateInfo:
    name: str
    template_id: str
    default_version: int | None
    latest_version: int | None


@dataclass(frozen=True)
class TemplateVersion:
    """One launch-template version read back in full.

    ``data`` is the whole ``LaunchTemplateData`` for the drift comparison. It can
    hold user data, so it stays out of ``repr``, equality and ``as_dict``.
    """

    name: str
    template_id: str
    version: int
    description: str | None
    data: Mapping = field(repr=False, compare=False)

    def as_dict(self):
        image_id = self.data.get("ImageId")
        return {
            "name": self.name,
            "id": self.template_id,
            "version": self.version,
            "image_id": image_id if isinstance(image_id, str) else None,
        }


@dataclass(frozen=True)
class SecurityGroup:
    """One security group, read to see whether it admits inbound traffic.

    ``ingress_rules`` counts the inbound rules. The rules themselves stay out of
    this object, because a rule can carry an address range.
    """

    group_id: str
    name: str
    ingress_rules: int

    def as_dict(self):
        return {"group_id": self.group_id, "name": self.name, "ingress_rules": self.ingress_rules}


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    last_modified: float | None


@dataclass(frozen=True)
class ArchiveUpload:
    """The staged archive: where it is, how large and the sha256 of its bytes."""

    key: str
    size: int
    sha256: str


class _ArchiveTooLarge(Exception):
    pass


def _boto3_session(profile):
    """A boto3 session whose credentials can only be ``profile``'s assume-role route.

    ``Session(profile_name=...)`` alone is not enough: a profile with no
    credential route falls through to the container and instance-metadata
    providers, and on the controller host those are the scaling identity. The
    first profile is not enough either. botocore resolves the root of a
    ``source_profile`` chain as soon as credentials are requested, so a profile
    further along that names a ``credential_source`` asks the ambient route at
    once, whatever the first profile says, and one that names a web identity
    token file, a credential process, a login session or an SSO session takes
    its credentials from there instead of from keys. So the whole chain is read
    from the merged configuration first (see ``_profile_problem``): the profile
    must name ``role_arn`` and ``source_profile``, every profile it leads to must
    exist and name none of those routes, and the chain must end in static keys.
    The credentials the SDK then resolves must be the assume-role ones.

    The configuration walk reads files only, so a profile that fails it is
    refused before the SDK is asked for credentials and no ambient route is
    contacted. Assumed-role credentials are deferred, so the method check makes
    no STS call either. The detail names the profile and at most the class of an
    SDK error, never the SDK's message or a credential.
    """
    try:
        import boto3
        import botocore.session
    except ImportError:
        raise ProviderRefusal("AWS_SDK_UNAVAILABLE", "install the boto3 runtime dependency") from None
    try:
        sdk_session = botocore.session.Session(profile=profile)
        problem = _profile_problem(sdk_session, profile)
    except Exception as error:  # noqa: BLE001 - reported as a class name only
        problem = f"the SDK could not resolve it ({type(error).__name__})"
    if problem is not None:
        raise ProviderRefusal("IMAGE_PROFILE_INVALID", f"profile {profile}: {problem}")
    return boto3.Session(botocore_session=sdk_session)


def _profile_problem(sdk_session, profile):
    """Why ``profile`` is not an assume-role route that ends in static keys, or ``None`` when it is.

    Follows ``source_profile`` through the SDK's merged configuration (the config
    file and the credentials file), the same mapping botocore resolves from. A
    profile with ``role_arn`` is a role: it needs a ``source_profile``, which is
    the next link. One without ``role_arn`` is the end of the chain and needs
    both static keys. Each link must exist and may not name a key of
    ``_AMBIENT_PROFILE_KEYS`` or one that starts with ``_SSO_PREFIX``, and a
    chain that returns to a profile or spans more than ``MAX_PROFILE_CHAIN``
    profiles is refused.

    This is stricter than botocore in one place: a link past the first that has
    both ``role_arn`` and static keys is resolved by botocore from the keys, and
    is followed here as a role. The detail names a profile and a key, never a value.
    """
    profiles = sdk_session.full_config["profiles"]
    chain, name = [], profile
    while True:
        who = f"source profile {name}" if chain else "it"
        if name in chain:
            return f"its source_profile chain returns to {name}"
        if len(chain) == MAX_PROFILE_CHAIN:
            return f"its source_profile chain is longer than {MAX_PROFILE_CHAIN} profiles"
        settings = profiles.get(name)
        if settings is None:
            return f"{who} does not exist"
        for key in settings:
            if key in _AMBIENT_PROFILE_KEYS or key.startswith(_SSO_PREFIX):
                return f"{who} may not name {key}"
        chain.append(name)
        if settings.get("role_arn"):
            name = settings.get("source_profile")
            if not name:
                return f"{who} needs both role_arn and source_profile"
        elif len(chain) == 1:
            return "it needs both role_arn and source_profile"
        elif all(settings.get(key) for key in _STATIC_KEYS):
            break
        else:
            return f"{who} ends the chain without static keys"
    credentials = sdk_session.get_credentials()
    if getattr(credentials, "method", None) != ASSUME_ROLE_METHOD:
        return "its credentials do not come from the assume-role route"
    return None


class _WithoutTraceback(logging.Filter):
    """Pass a record on with its message, and without its traceback or stack."""

    def filter(self, record):
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def _quiet_sdk_loggers():
    """Keep the SDK's own diagnostics from printing what this adapter keeps private.

    Botocore's DEBUG records print the request parameters (the builder's user
    data, which holds the presigned archive URL, and the client token), the
    response bodies (an assumed role's credentials) and the request signature.
    No client or ``Config`` option switches that off, so the level of the SDK's
    loggers is raised instead. This is a process-wide guard for the image
    command: it holds for any handler or root level that is set later, and it
    suits a command that runs one pass and exits.

    Each top-level logger is set to INFO, or to the level it already has or
    inherits when that is higher. One that is unset or below INFO is raised, a
    quieter one keeps its level, and none becomes more verbose than it was (an
    unset logger under a root set to WARNING stays at WARNING, not INFO).

    A level of its own wins over every ancestor, and Python does not apply an
    ancestor's level again to a logger that already has one. So every logger
    below the top-level three that exists now and was levelled below INFO (for
    example ``botocore.endpoint`` set to DEBUG) is raised to INFO as well. An
    unset one follows its raised parent and a quieter one keeps its level. A
    logger that is levelled to DEBUG after this call is the caller's choice.

    A level cannot hide one record. When an assumed role's credentials fail to
    refresh, the SDK's ``RefreshableCredentials._protected_refresh`` logs a
    WARNING with ``exc_info=True``, and the traceback ends in the provider's
    error message. Raw provider payloads must stay out of every log, and raising
    ``botocore.credentials`` above WARNING would also silence that module's
    other warnings. So a filter on that logger keeps the record, message line
    included, and clears its traceback and stack. It is added once, however
    often an ``ImageAws`` is built.
    """
    for name in SDK_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(max(logging.INFO, logger.getEffectiveLevel()))
    # A copy, because a thread may create a logger while this walks. A placeholder
    # stands where an ancestor was never created and has no level to raise.
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        is_sdk_logger = isinstance(logger, logging.Logger) and name.partition(".")[0] in SDK_LOGGERS
        if is_sdk_logger and logging.NOTSET < logger.level < logging.INFO:
            logger.setLevel(logging.INFO)
    credentials = logging.getLogger("botocore.credentials")
    if not any(isinstance(existing, _WithoutTraceback) for existing in credentials.filters):
        credentials.addFilter(_WithoutTraceback())


def _session_expiry(session):
    """Epoch expiry of the session's credentials, or ``None`` when unknown.

    Assumed-role credentials resolve lazily and carry no expiry until the first
    refresh, so they are resolved first. The resolved secret is discarded here.
    """
    credentials = session.get_credentials()
    if credentials is None:
        return None
    freeze = getattr(credentials, "get_frozen_credentials", None)
    if callable(freeze):
        freeze()
    expiry = getattr(credentials, "_expiry_time", None)
    return _timestamp(expiry)


class ImageAws:
    """The image-build identity. One instance is one fresh SDK session.

    A call site that needs an image session constructs a new ``ImageAws``; it is
    never cached across passes, so a long supervisor re-reads the assumed-role
    profile for every pass. The session comes from ``profile`` alone and never
    from ``[aws].profile``: an empty profile is refused, because the SDK would
    otherwise fall back to its default chain, which may be the scaling identity.
    The default session factory refuses a profile that is not an assume-role
    route, or whose ``source_profile`` chain does not end in static keys, for
    the same reason (see ``_boto3_session``). A failure of the session, of a
    client's construction or of the credentials, an assumed role's included,
    leaves as a ``ProviderRefusal`` that carries an error code or class name and
    none of the SDK's text (see ``_sdk_failures``).

    Primitives here do not decide what may be deleted or terminated. The caller
    verifies the ownership tags first (see the image deletion rule).

    ``session_factory`` (profile name -> session), ``clients`` (service name ->
    client), ``clock`` and ``expiry`` (epoch seconds, or a callable returning
    them) let tests run without the SDK or a network. An injected factory
    replaces the profile check, so it is the caller's to make.
    """

    def __init__(self, region, profile, *, session_factory=None, clients=None, clock=time.time, expiry=None):
        if not isinstance(profile, str) or not profile:
            raise ProviderRefusal("IMAGE_PROFILE_MISSING", "an image session needs a named profile")
        # Before any session or client exists, injected ones included: the SDK
        # logs the request, and the credential response, from the first call.
        _quiet_sdk_loggers()
        self._region = region
        self._profile = profile
        self._session_factory = session_factory or _boto3_session
        self._clients = dict(clients or {})
        self._clock = clock
        self._expiry = expiry
        self._session_cache = None
        self._ec2_reads = None

    # -- plumbing ---------------------------------------------------------
    def _session(self):
        if self._session_cache is None:
            self._session_cache = self._session_factory(self._profile)
        return self._session_cache

    def _client(self, service):
        if service not in IMAGE_SERVICES:
            raise ValueError("the image identity has no client for that service")
        if service not in self._clients:
            overrides = {"signature_version": "s3v4"} if service == "s3" else {}
            config = _sdk_config(
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                read_timeout=READ_TIMEOUT_SECONDS,
                **overrides,
            )
            with _sdk_failures():
                self._clients[service] = self._session().client(service, region_name=self._region, config=config)
        return self._clients[service]

    def _invoke(self, service, method, *, read=False, absent=None, **arguments):
        """Call the provider. ``absent`` is an error code that means "not there"."""
        client = self._client(service)
        try:
            return getattr(client, method)(**arguments)
        except Exception as error:  # noqa: BLE001 - classified below
            if absent is not None and _error_code(error) == absent:
                return None
            raise (_translate_read(error) if read else _translate(error)) from None

    def _check_deadline(self, deadline, detail):
        if deadline is not None and self._clock() >= deadline:
            raise DeadlineRefusal(detail)

    def _pages(self, service, method, request_token, response_token, deadline, detail, absent=None, **arguments):
        """Yield every response page, refusing when the listing would be partial.

        An ``absent`` error code ends the listing as empty.
        """
        token, pages = None, 0
        while True:
            self._check_deadline(deadline, detail)
            page = dict(arguments)
            if token:
                page[request_token] = token
            response = self._invoke(service, method, read=True, absent=absent, **page)
            if response is None:
                return
            if not isinstance(response, Mapping):
                raise _incomplete_inventory(f"{method} returned no response mapping")
            yield response
            token = response.get(response_token)
            pages += 1
            if not token:
                return
            if pages >= MAX_INVENTORY_PAGES:
                raise _incomplete_inventory(f"{method} pagination limit reached")

    def _ec2_pages(self, method, deadline, absent=None, **arguments):
        return self._pages("ec2", method, "NextToken", "NextToken", deadline, method, absent=absent, **arguments)

    def _reads(self):
        """The worker adapter's by-id, console and terminate paths, on this session."""
        if self._ec2_reads is None:
            self._ec2_reads = Aws(None, client=self._client("ec2"), clock=self._clock)
        return self._ec2_reads

    # -- instances --------------------------------------------------------
    def builders(self, deployment, *, deadline=None):
        """Builders of one deployment that still exist (the single-flight precheck)."""
        filters = [
            {"Name": "tag:" + IMAGE_BUILD_TAG, "Values": ["true"]},
            {"Name": "tag:" + DEPLOYMENT_TAG, "Values": [deployment]},
            {"Name": "instance-state-name", "Values": list(BUILDER_STATES)},
        ]
        items = []
        for response in self._ec2_pages("describe_instances", deadline, Filters=filters):
            items.extend(_instances(response))
        return items

    def instances(self, instance_ids, *, deadline=None):
        return self._reads().instances(instance_ids, deadline=deadline)

    def console_output(self, instance_ids, *, deadline=None):
        return self._reads().console_output(instance_ids, deadline=deadline)

    def terminate(self, instance_ids):
        return self._reads().terminate(instance_ids)

    def run_builder(self, *, template_name, template_version, deployment, build_id, expires_at, user_data, client_token):
        """Launch the one builder from the build launch template.

        The request is built from fixed parameters only: a numeric template
        version, on-demand capacity, one instance, no instance profile and the
        build tags. It has no field through which the managed tag or a profile
        could be added.
        """
        _require_version(template_version)
        tags = [
            {"Key": IMAGE_BUILD_TAG, "Value": "true"},
            {"Key": DEPLOYMENT_TAG, "Value": deployment},
            {"Key": BUILD_ID_TAG, "Value": build_id},
            {"Key": EXPIRES_TAG, "Value": str(int(expires_at))},
        ]
        response = self._invoke(
            "ec2",
            "run_instances",
            LaunchTemplate={"LaunchTemplateName": template_name, "Version": str(template_version)},
            MinCount=1,
            MaxCount=1,
            ClientToken=client_token,
            UserData=user_data,
            TagSpecifications=[{"ResourceType": kind, "Tags": list(tags)} for kind in BUILDER_TAGGED_RESOURCES],
        )
        instances = response.get("Instances") or []
        identifiers = [item.get("InstanceId") for item in instances if item.get("InstanceId")]
        if len(identifiers) != 1:
            raise ProviderRefusal("AWS_UNCERTAIN", "launch response did not identify exactly one instance", uncertain=True)
        return identifiers[0]

    # -- images and snapshots ---------------------------------------------
    def create_image(self, instance_id, name, *, deployment, build_id, basis_digest, extra_tags=None):
        """Image a builder without a reboot, tagging the image and its snapshots.

        A reboot would rerun user data after ``cloud-init clean``.
        """
        tags = {
            IMAGE_BUILD_TAG: "true",
            DEPLOYMENT_TAG: deployment,
            BUILD_ID_TAG: build_id,
            BASIS_DIGEST_TAG: basis_digest,
        }
        for key, value in (extra_tags or {}).items():
            if key in _IMAGE_RESERVED_TAGS:
                raise ValueError("an extra image tag may not replace a reserved tag")
            tags[key] = value
        listed = [{"Key": key, "Value": value} for key, value in tags.items()]
        response = self._invoke(
            "ec2",
            "create_image",
            InstanceId=instance_id,
            Name=name,
            NoReboot=True,
            TagSpecifications=[{"ResourceType": kind, "Tags": list(listed)} for kind in ("image", "snapshot")],
        )
        image_id = response.get("ImageId")
        if not isinstance(image_id, str) or not image_id:
            raise ProviderRefusal("AWS_UNCERTAIN", "CreateImage returned no image id", uncertain=True)
        return image_id

    def images(self, deployment, *, deadline=None):
        filters = [
            {"Name": "tag:" + IMAGE_BUILD_TAG, "Values": ["true"]},
            {"Name": "tag:" + DEPLOYMENT_TAG, "Values": [deployment]},
        ]
        items = []
        for response in self._ec2_pages("describe_images", deadline, Owners=["self"], Filters=filters):
            items.extend(_images(response))
        return items

    def image(self, image_id, *, deadline=None):
        """One image, or ``None`` when the provider no longer knows it.

        ``deadline`` is checked before the request, as the listings check it before each page.
        """
        self._check_deadline(deadline, "describe_images")
        client = self._client("ec2")  # before the try, so a refusal it raises is not translated again
        try:
            response = client.describe_images(ImageIds=[image_id], Owners=["self"])
        except Exception as error:  # noqa: BLE001 - classified below
            if _error_code(error) == "InvalidAMIID.NotFound":
                return None
            raise _translate_read(error) from None
        found = _images(response)
        return found[0] if found else None

    def snapshots(self, deployment, *, deadline=None):
        filters = [
            {"Name": "tag:" + IMAGE_BUILD_TAG, "Values": ["true"]},
            {"Name": "tag:" + DEPLOYMENT_TAG, "Values": [deployment]},
        ]
        items = []
        for response in self._ec2_pages("describe_snapshots", deadline, OwnerIds=["self"], Filters=filters):
            items.extend(_snapshots(response))
        return items

    def snapshot(self, snapshot_id):
        """One snapshot, or ``None`` when the provider no longer knows it."""
        client = self._client("ec2")  # before the try, so a refusal it raises is not translated again
        try:
            response = client.describe_snapshots(SnapshotIds=[snapshot_id], OwnerIds=["self"])
        except Exception as error:  # noqa: BLE001 - classified below
            if _error_code(error) == "InvalidSnapshot.NotFound":
                return None
            raise _translate_read(error) from None
        found = _snapshots(response)
        return found[0] if found else None

    def deregister_image(self, image_id):
        self._invoke("ec2", "deregister_image", ImageId=image_id)

    def delete_snapshot(self, snapshot_id):
        self._invoke("ec2", "delete_snapshot", SnapshotId=snapshot_id)

    # -- launch templates -------------------------------------------------
    def launch_templates(self, names, *, deadline=None):
        """Describe templates by name. A name the provider does not know gives no entry.

        EC2 fails a whole request when one name is unknown, so each name is
        described on its own and only its own not-found answer means absent.
        """
        items = []
        for name in names:
            for response in self._ec2_pages(
                "describe_launch_templates",
                deadline,
                absent="InvalidLaunchTemplateName.NotFoundException",
                LaunchTemplateNames=[name],
            ):
                items.extend(_launch_templates(response))
        return items

    def template_versions(self, name, versions, *, deadline=None):
        """Read versions back in full, including the whole ``LaunchTemplateData``."""
        for version in versions:
            _require_version(version)
        items = []
        for response in self._ec2_pages(
            "describe_launch_template_versions",
            deadline,
            LaunchTemplateName=name,
            Versions=[str(version) for version in versions],
        ):
            items.extend(_template_versions(response, name))
        return items

    def template_version(self, name, version):
        found = self.template_versions(name, [version])
        if not found:
            raise ProviderRefusal("TEMPLATE_NOT_FOUND", f"{name} version {version}")
        if len(found) != 1 or found[0].version != version:
            raise _incomplete_inventory(f"DescribeLaunchTemplateVersions did not return exactly version {version}")
        return found[0]

    # -- security groups --------------------------------------------------
    def security_groups(self, *, group_ids=(), group_names=(), deadline=None):
        """Read security groups by id and by name, with their inbound rule counts.

        Ids go in one request and names in another. EC2 fails a request that
        names an unknown id, and that failure is a refusal here, never an empty
        group. A name that matches nothing gives no entry, so the caller compares
        the answer with what it asked for. A name filter matches the group of
        that name in every VPC of the Region.
        """
        items = []
        if group_ids:
            for response in self._ec2_pages("describe_security_groups", deadline, GroupIds=list(group_ids)):
                items.extend(_security_groups(response))
        if group_names:
            filters = [{"Name": "group-name", "Values": list(group_names)}]
            for response in self._ec2_pages("describe_security_groups", deadline, Filters=filters):
                items.extend(_security_groups(response))
        return items

    def create_template_version(self, name, source_version, image_id, description, client_token):
        """Create a non-default version that differs from the pin only in its image.

        ``source_version`` is the numeric pin, never ``$Latest`` or the default.
        The request sets no default and the response is read back by the caller.
        """
        _require_version(source_version)
        if not isinstance(image_id, str) or not image_id.startswith("ami-"):
            raise ValueError("image_id must be a literal image id")
        response = self._invoke(
            "ec2",
            "create_launch_template_version",
            LaunchTemplateName=name,
            SourceVersion=str(source_version),
            LaunchTemplateData={"ImageId": image_id},
            VersionDescription=description,
            ClientToken=client_token,
        )
        entry = response.get("LaunchTemplateVersion") or {}
        number = entry.get("VersionNumber")
        if isinstance(number, bool) or not isinstance(number, int):
            raise ProviderRefusal("AWS_UNCERTAIN", "version creation returned no version number", uncertain=True)
        return number

    # -- source archive storage -------------------------------------------
    def put_archive(self, bucket, key, chunks, *, max_bytes, part_bytes=ARCHIVE_PART_BYTES, deadline=None):
        """Stream ``chunks`` into one multipart upload, hashing the bytes as they go.

        The archive is never written to disk. Overrunning ``max_bytes``, a failed
        part or completion, or a failing source iterator aborts the upload and
        deletes the key. The detail never carries the provider's message or the
        iterator's, which could hold a URL or a token: a refusal that the
        iterator raises leaves here as a new one with the same reason code and a
        fixed detail, of the same class where the class can be rebuilt from its
        reason, detail and uncertainty (see ``_rebuilt_refusal``). ``deadline`` is
        checked before the upload opens, before each chunk and each part, and
        immediately before completion, so a stalled stream or a slow last part
        ends the upload instead of finishing it.
        """
        self._check_deadline(deadline, "archive upload")
        started = self._invoke("s3", "create_multipart_upload", Bucket=bucket, Key=key)
        upload_id = started.get("UploadId")
        if not isinstance(upload_id, str) or not upload_id:
            raise ProviderRefusal("AWS_UNCERTAIN", "multipart upload returned no upload id", uncertain=True)
        try:
            return self._stream_parts(bucket, key, upload_id, chunks, max_bytes, part_bytes, deadline)
        except _ArchiveTooLarge:
            cleaned = self._discard_upload(bucket, key, upload_id)
            raise ProviderRefusal("IMAGE_ARCHIVE_TOO_LARGE", _cleanup_detail(f"over {max_bytes} bytes", cleaned)) from None
        except Refusal as refusal:
            # The source iterator's refusal can carry a URL or a token in its
            # detail, so only its reason code (and whether it was uncertain)
            # crosses this boundary, on a new object. The class survives, so a
            # caller that catches a ``ProviderRefusal`` or a ``DeadlineRefusal``
            # still does; the deadline refusal takes only a detail, so it is
            # rebuilt on its own line.
            cleaned = self._discard_upload(bucket, key, upload_id)
            if isinstance(refusal, DeadlineRefusal):
                raise DeadlineRefusal(_cleanup_detail("archive upload", cleaned)) from None
            raise _rebuilt_refusal(refusal, _cleanup_detail("archive source refused", cleaned)) from None
        except Exception as error:  # noqa: BLE001 - reported as a code only
            cleaned = self._discard_upload(bucket, key, upload_id)
            code = (_error_code(error) or type(error).__name__)[:64]
            raise ProviderRefusal("IMAGE_ARCHIVE_UPLOAD_FAILED", _cleanup_detail(code, cleaned)) from None
        except BaseException:
            self._discard_upload(bucket, key, upload_id)
            raise

    def _stream_parts(self, bucket, key, upload_id, chunks, max_bytes, part_bytes, deadline):
        s3 = self._client("s3")
        digest, parts, buffer, total = hashlib.sha256(), [], bytearray(), 0

        def send(body):
            self._check_deadline(deadline, "archive upload")
            number = len(parts) + 1
            response = s3.upload_part(Bucket=bucket, Key=key, UploadId=upload_id, PartNumber=number, Body=body)
            parts.append({"ETag": response["ETag"], "PartNumber": number})

        for chunk in chunks:
            self._check_deadline(deadline, "archive upload")
            if not isinstance(chunk, (bytes, bytearray)):
                raise TypeError("archive chunk is not bytes")
            total += len(chunk)
            if total > max_bytes:
                raise _ArchiveTooLarge
            digest.update(chunk)
            buffer.extend(chunk)
            while len(buffer) >= part_bytes:
                send(bytes(buffer[:part_bytes]))
                del buffer[:part_bytes]
        if total == 0:
            raise ValueError("archive stream was empty")
        if buffer:
            send(bytes(buffer))
        # The last part can cross the deadline while it uploads, and the per-part
        # check above ran before it: a late upload must not be completed.
        self._check_deadline(deadline, "archive upload")
        s3.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": parts})
        return ArchiveUpload(key=key, size=total, sha256=digest.hexdigest())

    def _discard_upload(self, bucket, key, upload_id):
        """Abort the upload and delete the key. True only when both calls succeeded."""
        s3 = self._client("s3")
        clean = True
        # A completion whose response was lost may have created the object, so
        # the delete runs whether or not the abort found an upload.
        for call, arguments in (
            (s3.abort_multipart_upload, {"Bucket": bucket, "Key": key, "UploadId": upload_id}),
            (s3.delete_object, {"Bucket": bucket, "Key": key}),
        ):
            try:
                call(**arguments)
            except Exception:  # noqa: BLE001 - best effort; reconcile removes a stray object
                clean = False
        return clean

    def list_objects(self, bucket, prefix, *, deadline=None):
        items = []
        for response in self._pages(
            "s3",
            "list_objects_v2",
            "ContinuationToken",
            "NextContinuationToken",
            deadline,
            "ListObjectsV2 page",
            Bucket=bucket,
            Prefix=prefix,
        ):
            for entry in response.get("Contents") or []:
                key = entry.get("Key") if isinstance(entry, Mapping) else None
                size = entry.get("Size") if isinstance(entry, Mapping) else None
                if not isinstance(key, str) or not key or isinstance(size, bool) or not isinstance(size, int):
                    raise _incomplete_inventory("ListObjectsV2 returned an object entry without a key or size")
                items.append(StoredObject(key=key, size=size, last_modified=_timestamp(entry.get("LastModified"))))
        return items

    def delete_object(self, bucket, key):
        self._invoke("s3", "delete_object", Bucket=bucket, Key=key)

    def credential_expiry(self):
        """Epoch expiry of this session's credentials, or ``None`` when unknown.

        An assumed role is assumed here, when its credentials are first read, so
        this is where the provider refuses it (an expired source key, a trust
        policy that denies the caller). That failure leaves as a ``ProviderRefusal``
        with the provider's error code and never its message.
        """
        if self._expiry is not None:
            return self._expiry() if callable(self._expiry) else self._expiry
        with _sdk_failures():
            return _session_expiry(self._session())

    def check_credential_life(self, expires_seconds):
        """Refuse with ``IMAGE_CREDENTIAL_SHORT`` unless the session outlives a URL of ``expires_seconds``.

        The signature dies with the credentials, so a URL that is meant to last
        ``expires_seconds`` needs at least that much credential life. An unknown
        expiry is a refusal, not "long enough". Nothing is presigned, so a caller
        can ask before it spends anything on the build the URL is for.
        """
        expiry = self.credential_expiry()
        if expiry is None:
            raise ProviderRefusal("IMAGE_CREDENTIAL_SHORT", "the credential expiry is unknown")
        remaining = expiry - self._clock()
        if remaining < expires_seconds:
            raise ProviderRefusal(
                "IMAGE_CREDENTIAL_SHORT",
                f"{max(int(remaining), 0)} seconds of credential life remain, {expires_seconds} are needed",
            )

    def presign_get(self, bucket, key, expires_seconds):
        """Presign a GET for one object, only if the session outlives the URL.

        ``check_credential_life`` decides that. The URL is returned and never
        logged, stored or placed in an error.
        """
        self.check_credential_life(expires_seconds)
        client = self._client("s3")
        try:
            return client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=expires_seconds,
            )
        except Exception as error:  # noqa: BLE001 - classified below
            raise _translate(error) from None


def _require_version(version):
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("a launch-template version must be a positive number")


def _cleanup_detail(detail, cleaned):
    return detail if cleaned else detail + "; upload cleanup not confirmed"


def _rebuilt_refusal(refusal, detail):
    """A new refusal with ``refusal``'s reason and uncertainty and the given ``detail``.

    It has the class of ``refusal`` when that class can be built from ``(reason,
    detail, uncertain)`` and then carries exactly those, which is every class of
    ``errors`` but the deadline refusal. Anything else a class carries is not
    copied, because it could hold the text that is being dropped. A class built
    another way, or one that would shuffle the three, comes out as a plain
    ``Refusal``.
    """
    wanted = (refusal.reason, detail, refusal.uncertain)
    try:
        rebuilt = type(refusal)(*wanted)
    except TypeError:
        rebuilt = None
    if rebuilt is None or (rebuilt.reason, rebuilt.detail, rebuilt.uncertain) != wanted:
        rebuilt = Refusal(*wanted)
    return rebuilt


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


@contextlib.contextmanager
def _sdk_failures():
    """Let only a refusal leave a call that builds the session, a client or credentials.

    Credentials of an assumed role resolve on their first use, so the SDK's own
    error for a role it was refused comes out of whichever call needs them first,
    and its message can carry what was asked. A refusal, such as the profile
    check's, passes unchanged. Any other failure leaves as ``_translate`` makes
    it: the error's code or class and nothing of its text.
    """
    try:
        yield
    except Refusal:
        raise
    except Exception as error:  # noqa: BLE001 - classified by _translate
        raise _translate(error) from None


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


def _created_at(value):
    """Epoch seconds of an image or snapshot creation time, or ``None``.

    EC2 gives an image's ``CreationDate`` as an ISO 8601 string and a
    snapshot's ``StartTime`` as a datetime. Only a time with a UTC offset is
    understood: a naive time, and a time whose zone cannot say its offset
    (``utcoffset()`` is ``None``, or not a ``timedelta``), would need a guessed
    zone, which makes an item look older or younger than it is, so each gives
    ``None`` like any other value. Nothing is raised: an age that cannot be read
    must not stop a listing, and the rules that need an age keep the item
    instead.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    try:
        if value.utcoffset() is None:
            return None
        return value.timestamp()
    except (OverflowError, OSError, TypeError, ValueError):
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
            instance_type = item.get("InstanceType")
            result.append(
                Instance(
                    instance_id=instance_id,
                    state=(item.get("State") or {}).get("Name", "unknown"),
                    tags=tags,
                    launch_time=_timestamp(launch_time),
                    availability_zone=zone if isinstance(zone, str) and zone else None,
                    instance_type=instance_type if isinstance(instance_type, str) and instance_type else None,
                )
            )
    return result


def _incomplete_inventory(detail):
    return ProviderRefusal("AWS_INVENTORY_INCOMPLETE", detail, uncertain=True)


def _tag_map(item):
    return {tag.get("Key"): tag.get("Value") for tag in item.get("Tags") or [] if isinstance(tag, Mapping)}


def _entries(response, key, what):
    entries = response.get(key) if isinstance(response, Mapping) else None
    if not isinstance(entries, list):
        raise _incomplete_inventory(f"{what} returned no {key} list")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise _incomplete_inventory(f"{what} returned an entry that is not a mapping")
    return entries


def _images(response):
    result = []
    for item in _entries(response, "Images", "DescribeImages"):
        image_id = item.get("ImageId")
        if not isinstance(image_id, str) or not image_id:
            raise _incomplete_inventory("DescribeImages returned an image without an ImageId")
        snapshots = []
        for mapping in item.get("BlockDeviceMappings") or []:
            snapshot_id = ((mapping or {}).get("Ebs") or {}).get("SnapshotId")
            if isinstance(snapshot_id, str) and snapshot_id:
                snapshots.append(snapshot_id)
        result.append(
            Image(
                image_id=image_id,
                state=str(item.get("State") or "unknown"),
                tags=_tag_map(item),
                snapshot_ids=tuple(snapshots),
                created_at=_created_at(item.get("CreationDate")),
            )
        )
    return result


def _snapshots(response):
    result = []
    for item in _entries(response, "Snapshots", "DescribeSnapshots"):
        snapshot_id = item.get("SnapshotId")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise _incomplete_inventory("DescribeSnapshots returned a snapshot without a SnapshotId")
        result.append(
            Snapshot(
                snapshot_id=snapshot_id,
                state=str(item.get("State") or "unknown"),
                tags=_tag_map(item),
                created_at=_created_at(item.get("StartTime")),
            )
        )
    return result


def _launch_templates(response):
    result = []
    for item in _entries(response, "LaunchTemplates", "DescribeLaunchTemplates"):
        name, template_id = item.get("LaunchTemplateName"), item.get("LaunchTemplateId")
        if not isinstance(name, str) or not name or not isinstance(template_id, str) or not template_id:
            raise _incomplete_inventory("DescribeLaunchTemplates returned a template without a name or id")
        result.append(
            LaunchTemplateInfo(
                name=name,
                template_id=template_id,
                default_version=_whole_number(item.get("DefaultVersionNumber")),
                latest_version=_whole_number(item.get("LatestVersionNumber")),
            )
        )
    return result


def _template_versions(response, name):
    result = []
    for item in _entries(response, "LaunchTemplateVersions", "DescribeLaunchTemplateVersions"):
        number = _whole_number(item.get("VersionNumber"))
        data = item.get("LaunchTemplateData")
        if number is None or not isinstance(data, Mapping):
            raise _incomplete_inventory("DescribeLaunchTemplateVersions returned a version without its data")
        description = item.get("VersionDescription")
        result.append(
            TemplateVersion(
                name=name,
                template_id=str(item.get("LaunchTemplateId") or ""),
                version=number,
                description=description if isinstance(description, str) else None,
                data=copy.deepcopy(dict(data)),
            )
        )
    return result


def _security_groups(response):
    result = []
    for item in _entries(response, "SecurityGroups", "DescribeSecurityGroups"):
        group_id, name, rules = item.get("GroupId"), item.get("GroupName"), item.get("IpPermissions")
        # A group with no readable rule list is not a group without ingress.
        if not isinstance(group_id, str) or not group_id or not isinstance(name, str) or not name:
            raise _incomplete_inventory("DescribeSecurityGroups returned a group without an id or a name")
        if not isinstance(rules, list):
            raise _incomplete_inventory("DescribeSecurityGroups returned a group without its inbound rules")
        result.append(SecurityGroup(group_id=group_id, name=name, ingress_rules=len(rules)))
    return result


def _whole_number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None
