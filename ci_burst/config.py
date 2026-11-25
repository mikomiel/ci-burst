"""Strict TOML configuration for one bounded deployment.

The configuration is product input, not a deployment record: it names no
private host, credential or account. Every value is validated once, here, so a
typo cannot silently widen scope. Unknown keys and sections are refused rather
than ignored -- an unrecognized setting is far more likely to be a mistake than
an extension point.

Placement is configured as an ordered list of availability-zone candidates. The
original single-zone key remains supported and is normalized to a one-candidate
list, so no consumer chooses between two placement shapes. An optional ordered
instance-type list multiplies those candidates into one pool per type; the same
type-major order is the priority the controller falls back through.
"""

import os
import re
import tomllib
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigRefusal
from .money import parse_money

#: Contracts whose response shapes this source validates. A contract is a
#: declared interface, not proof of live semantics: see ``qualification``.
SUPPORTED_CONTRACTS = {
    "forgejo-17.0.1-runners-jobs": (
        "GET /api/v1/repos/{owner}/{repo}/actions/runners/jobs returns a JSON "
        "array of ActionRunJob records; a record is eligible demand when its "
        "status is waiting and its complete runs_on set is supported by the "
        "configured worker labels; status blocked is dependency-blocked work, "
        "and needs is metadata that never decides eligibility."
    ),
}

QUALIFICATIONS = ("fixtures-only", "live-verified")

_DEPLOYMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
_TEMPLATE_NAME = re.compile(r"^[A-Za-z0-9()\\./_ -]{3,128}$")
#: EC2 subnet identifiers are ``subnet-`` followed by exactly 8 or 17
#: hexadecimal characters. Any other length would reach the provider as a
#: malformed identifier instead of being refused here.
_SUBNET = re.compile(r"^subnet-(?:[0-9a-f]{8}|[0-9a-f]{17})$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")

#: Keys accepted inside one ``[[aws.availability_zones]]`` table.
_ZONE_CANDIDATE_KEYS = ("zone", "subnet_id", "launch_template_version")

#: One EC2 instance type name: a family and a size, such as ``m7g.xlarge``.
#: Both parts may carry a hyphen, as ``u-6tb1.112xlarge`` and
#: ``m7i.metal-24xl`` do; a leading, trailing or doubled hyphen is refused.
#: The list is validated here because every configured type multiplies the
#: placement pools an admission prices.
INSTANCE_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*\.[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_INSTANCE_TYPE_LENGTH = 64

#: How many instance types one deployment may list. Every admission prices
#: every configured type in every configured zone, so the bound keeps those
#: synchronous provider reads predictable.
_MAX_INSTANCE_TYPES = 4
#: The worker's own lifetime. ``deploy/runner/bootstrap.sh`` arms its shutdown and bounds its job with this
#: literal; a test compares the two. ``apply`` admits only under it (``Config.require_worker_lifetime``, #138).
WORKER_LIFETIME_SECONDS = 3600

#: How many basis paths one image build may hash. Each path is one raw read
#: from the forge before any build starts, so the bound keeps that predictable.
_MAX_BASIS_PATHS = 32


@dataclass(frozen=True)
class ForgejoConfig:
    base_url: str
    repository: str
    token_env: str
    contract: str
    qualification: str
    timeout_seconds: int
    max_response_bytes: int


@dataclass(frozen=True)
class WorkerConfig:
    labels: tuple


@dataclass(frozen=True)
class ControllerConfig:
    """How many owned workers one deployment may run at once, and for how long.

    The default of one preserves the original single-slot behaviour; the
    supported range is small on purpose, because every slot is a separate
    reservation against the same allowance.

    ``pass_deadline_seconds`` bounds one ``apply`` pass, including its provider
    reads. Its accepted range (30..290) keeps the complete range below the
    shipped unit's ``TimeoutStartSec=300``, so a pass reports an incomplete
    result instead of being killed at the unit's outer bound. A deployment that
    needs a longer pass must raise the unit timeout; a larger value is not
    offered here. ``reap``, ``status`` and ``export`` keep their own behaviour
    and do not take it.

    ``claim_grace_seconds`` is how long a launch that is still booting or
    running covers one waiting job, so that the next pass does not launch
    another worker for the same job. Its default equals the claim window of the
    published bootstrap; there is no off value, because a grace of zero would
    remove the protection against a repeat launch.
    """

    max_instances: int
    pass_deadline_seconds: int
    claim_grace_seconds: int


@dataclass(frozen=True)
class ZoneCandidate:
    """One entry in the ordered placement list.

    ``subnet_id`` is ``None`` only for the legacy single-zone form, where the
    network choice stays in the launch template. A configured candidate list
    always names a subnet, because the fallback needs a placement the template
    does not already pin.
    """

    availability_zone: str
    subnet_id: str | None
    launch_template_version: int


@dataclass(frozen=True)
class AwsConfig:
    """One Region, an ordered zone list, and an ordered instance-type list.

    ``instance_types`` is empty for the original single-type form: the launch
    template's own ``InstanceType`` is then the only pool per candidate. A
    non-empty tuple multiplies every candidate into one pool per type, in
    type-major order, and the template's type is no longer launched on its own.
    """

    region: str
    zones: tuple
    instance_types: tuple
    launch_template_name: str
    launch_template_version: int
    max_spot_price: int
    max_lifetime_seconds: int
    price_max_age_seconds: int
    profile: str | None


@dataclass(frozen=True)
class BudgetConfig:
    window_seconds: int
    allowance: int
    non_compute_allowance: int
    #: Estimated non-compute cost of one instance-hour (public IPv4 and EBS),
    #: added to tracked cost. Zero keeps a deployment that never configured it
    #: tracking compute only.
    incidental_per_hour: int


@dataclass(frozen=True)
class StateConfig:
    database: Path


@dataclass(frozen=True)
class ImageSourceConfig:
    branch: str
    token_env: str
    basis_paths: tuple
    max_archive_bytes: int
    presign_seconds: int
    strip_components: int


@dataclass(frozen=True)
class ImageWarmConfig:
    commands: tuple
    #: Empty means no gate. The build refuses an empty gate unless
    #: ``allow_no_gate`` is set, so the loader accepts both.
    gate_command: str
    allow_no_gate: bool
    lifetime_seconds: int
    snapshot_seconds: int


@dataclass(frozen=True)
class ImageBuildConfig:
    launch_template_name: str
    launch_template_version: int


@dataclass(frozen=True)
class ImageStorageConfig:
    bucket: str
    prefix: str


@dataclass(frozen=True)
class ImageConfig:
    """The optional automated warm-image build; disabled unless opted in.

    An absent ``[image]`` table and ``enabled = false`` both yield the same
    disabled value: ``enabled`` is false and every other field is ``None``, so
    no consumer can read a half-configured image path. A present table is
    validated strictly in either case, so a typo is refused before the
    operator enables it; required keys are enforced only when it is enabled.
    """

    enabled: bool = False
    profile: str | None = None
    min_build_interval_seconds: int | None = None
    max_builds_per_day: int | None = None
    build_deadline_seconds: int | None = None
    failure_cooldown_seconds: int | None = None
    max_consecutive_failures: int | None = None
    retain: int | None = None
    retain_min_age_seconds: int | None = None
    promote_max_age_seconds: int | None = None
    source: ImageSourceConfig | None = None
    warm: ImageWarmConfig | None = None
    build: ImageBuildConfig | None = None
    storage: ImageStorageConfig | None = None
    #: The directory of ``image-state.sqlite`` and its telemetry files. It is a
    #: different directory from the scaling ledger's and not inside it, so the
    #: image unit never mounts the ledger.
    state_directory: Path | None = None


@dataclass(frozen=True)
class Config:
    deployment: str
    path: Path
    forgejo: ForgejoConfig
    worker: WorkerConfig
    controller: ControllerConfig
    aws: AwsConfig
    budget: BudgetConfig
    state: StateConfig
    image: ImageConfig = field(default_factory=ImageConfig)

    def require_worker_lifetime(self):
        """Refuse admission unless ``aws.max_lifetime_seconds`` is the lifetime the worker bootstrap arms.

        The expiry tag and the cost reservation of a new worker come from the configured value, its shutdown and its
        job bound from the bootstrap's literal: one clock, not two (#138). Only admission refuses, so ``reap``,
        ``status``, ``export`` and ``plan`` keep working on a file that still holds another value.
        """
        if self.aws.max_lifetime_seconds != WORKER_LIFETIME_SECONDS:
            raise ConfigRefusal(
                "WORKER_LIFETIME_MISMATCH",
                f"aws.max_lifetime_seconds must be {WORKER_LIFETIME_SECONDS}, the lifetime that the worker bootstrap "
                "arms on the instance; apply admits nothing until it is",
            )

    def require_live_contract(self):
        """Refuse live admission until the operator declares live evidence."""
        if self.forgejo.qualification != "live-verified":
            raise ConfigRefusal(
                "FORGEJO_CONTRACT_UNQUALIFIED",
                "forgejo.qualification is fixtures-only; live provisioning needs "
                "an operator-qualified API contract",
            )


def _fail(where, message):
    raise ConfigRefusal("CONFIG_INVALID", f"{where}: {message}")


def _table(value, where, allowed):
    if not isinstance(value, dict):
        _fail(where, "must be a table")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        _fail(where, f"unknown key(s) {', '.join(unknown)}")
    return value


def _string(table, key, where, *, required=True, pattern=None, pattern_name=""):
    value = table.get(key)
    if value is None:
        if required:
            _fail(f"{where}.{key}", "is required")
        return None
    if not isinstance(value, str) or not value:
        _fail(f"{where}.{key}", "must be a non-empty string")
    if pattern is not None and not pattern.fullmatch(value):
        _fail(f"{where}.{key}", f"must match {pattern_name or pattern.pattern}")
    return value


def _integer(table, key, where, *, minimum, maximum, required=True):
    value = table.get(key)
    if value is None:
        if required:
            _fail(f"{where}.{key}", "is required")
        return None
    if type(value) is not int:
        _fail(f"{where}.{key}", "must be an integer")
    if not minimum <= value <= maximum:
        _fail(f"{where}.{key}", f"must be between {minimum} and {maximum}")
    return value


def _money(table, key, where, *, required=True):
    value = table.get(key)
    if value is None:
        if required:
            _fail(f"{where}.{key}", "is required")
        return None
    try:
        return parse_money(value, f"{where}.{key}")
    except ValueError as error:
        _fail(f"{where}.{key}", str(error).split(": ", 1)[-1])


def _https_url(table, where):
    value = _string(table, "base_url", where)
    if not value.startswith("https://") or value.endswith("/"):
        _fail(f"{where}.base_url", "must be an https URL without a trailing slash")
    return value


def _labels(table, where):
    value = table.get("labels")
    if not isinstance(value, list) or not value:
        _fail(f"{where}.labels", "must be a non-empty array")
    seen = set()
    for index, label in enumerate(value):
        if not isinstance(label, str) or not _LABEL.fullmatch(label):
            _fail(f"{where}.labels[{index}]", "must be a plain runner label such as \"example:host\"")
        if label in seen:
            _fail(f"{where}.labels[{index}]", "must be unique")
        seen.add(label)
    return tuple(value)


def _zone_candidate_refusal(where, message):
    """Refuse a malformed ordered placement list with its own stable reason."""
    raise ConfigRefusal("ZONE_CANDIDATES_INVALID", f"{where}: {message}")


def _zone_candidates(aws_table, region, default_version):
    """Return the ordered launch candidates for either supported input form.

    ``aws.availability_zone`` (the original setting) becomes one candidate with
    no subnet override. ``aws.availability_zones`` is an ordered array of
    tables; each entry needs a zone and a subnet and may pin its own template
    version. The two forms are mutually exclusive because one deployment cannot
    have both an implicit and an explicit order.
    """
    listed = aws_table.get("availability_zones")
    single = aws_table.get("availability_zone")
    if listed is not None and single is not None:
        _zone_candidate_refusal(
            "aws",
            "availability_zone and availability_zones are mutually exclusive; "
            "use the ordered list to configure fallback",
        )
    if listed is None:
        if single is None:
            _fail("aws", "availability_zone or availability_zones is required")
        zone = _string(aws_table, "availability_zone", "aws")
        if not re.fullmatch(re.escape(region) + r"[a-z]", zone):
            _fail("aws.availability_zone", "must be a zone letter suffix of the configured Region")
        return (ZoneCandidate(availability_zone=zone, subnet_id=None, launch_template_version=default_version),)
    if not isinstance(listed, list):
        _zone_candidate_refusal("aws.availability_zones", "must be an array of tables")
    if not listed:
        _zone_candidate_refusal("aws.availability_zones", "must contain at least one candidate")
    seen = set()
    candidates = []
    for index, entry in enumerate(listed):
        where = f"aws.availability_zones[{index}]"
        if not isinstance(entry, dict):
            _zone_candidate_refusal(where, "must be a table")
        unknown = sorted(set(entry) - set(_ZONE_CANDIDATE_KEYS))
        if unknown:
            _zone_candidate_refusal(where, "unknown key(s) " + ", ".join(unknown))
        zone = entry.get("zone")
        if not isinstance(zone, str) or not zone:
            _zone_candidate_refusal(f"{where}.zone", "must be a non-empty string")
        if not re.fullmatch(re.escape(region) + r"[a-z]", zone):
            _zone_candidate_refusal(f"{where}.zone", "must be a zone letter suffix of the configured Region")
        if zone in seen:
            _zone_candidate_refusal(f"{where}.zone", "must be unique across candidates")
        seen.add(zone)
        subnet = entry.get("subnet_id")
        if not isinstance(subnet, str) or not _SUBNET.fullmatch(subnet):
            _zone_candidate_refusal(
                f"{where}.subnet_id",
                'must be a subnet id: "subnet-" followed by 8 or 17 hexadecimal characters',
            )
        version = entry.get("launch_template_version", default_version)
        if type(version) is not int or not 1 <= version <= 10_000:
            _zone_candidate_refusal(
                f"{where}.launch_template_version", "must be an integer between 1 and 10000"
            )
        candidates.append(
            ZoneCandidate(
                availability_zone=zone,
                subnet_id=subnet,
                launch_template_version=version,
            )
        )
    return tuple(candidates)


def _instance_types_refusal(where, message):
    """Refuse a malformed instance-type list with its own stable reason."""
    raise ConfigRefusal("INSTANCE_TYPES_INVALID", f"{where}: {message}")


def _instance_types(aws_table):
    """Return the ordered instance types, or an empty tuple for the template.

    ``aws.instance_types`` is optional and authoritative when present: every
    configured type becomes one pool in every configured zone, and the launch
    template's own ``InstanceType`` is only the shape reference the operator
    check compares against. An absent key keeps the original single-type
    behaviour instead of guessing a list from the provider.
    """
    where = "aws.instance_types"
    listed = aws_table.get("instance_types")
    if listed is None:
        return ()
    if not isinstance(listed, list) or not listed:
        _instance_types_refusal(where, "must be a non-empty array of instance type names")
    if len(listed) > _MAX_INSTANCE_TYPES:
        _instance_types_refusal(
            where, f"must list at most {_MAX_INSTANCE_TYPES} instance types"
        )
    seen = set()
    for index, entry in enumerate(listed):
        if not isinstance(entry, str) or not INSTANCE_TYPE_PATTERN.fullmatch(entry) or len(entry) > MAX_INSTANCE_TYPE_LENGTH:
            _instance_types_refusal(
                f"{where}[{index}]",
                'must be an instance type name such as "m7g.xlarge"',
            )
        if entry in seen:
            _instance_types_refusal(f"{where}[{index}]", "must be unique")
        seen.add(entry)
    return tuple(listed)


def _boolean(table, key, where, *, default):
    value = table.get(key, default)
    if type(value) is not bool:
        _fail(f"{where}.{key}", "must be true or false")
    return value


def _default(value, fallback):
    return fallback if value is None else value


def _no_control(value, where):
    if any(unicodedata.category(character) == "Cc" for character in value):
        _fail(where, "must not contain a control character")


def _image_basis_paths(table, where, *, required):
    value = table.get("basis_paths")
    if value is None:
        if required:
            _fail(f"{where}.basis_paths", "is required")
        return None
    if not isinstance(value, list) or not value:
        _fail(f"{where}.basis_paths", "must be a non-empty array")
    if len(value) > _MAX_BASIS_PATHS:
        _fail(f"{where}.basis_paths", f"must list at most {_MAX_BASIS_PATHS} paths")
    for index, entry in enumerate(value):
        entry_where = f"{where}.basis_paths[{index}]"
        if not isinstance(entry, str) or not entry:
            _fail(entry_where, "must be a non-empty string")
        if entry.startswith("/") or ".." in entry:
            _fail(entry_where, 'must be a path inside the archive: no ".." and no leading "/"')
        _no_control(entry, entry_where)
    return tuple(value)


def _image_commands(table, where, *, required):
    value = table.get("commands")
    if value is None:
        if required:
            _fail(f"{where}.commands", "is required")
        return None
    if not isinstance(value, list) or not value:
        _fail(f"{where}.commands", "must be a non-empty array of commands")
    for index, entry in enumerate(value):
        entry_where = f"{where}.commands[{index}]"
        if not isinstance(entry, str) or not entry.strip():
            _fail(entry_where, "must be a non-empty string")
        _no_control(entry, entry_where)
    return tuple(value)


def _image_gate_command(table, where):
    value = table.get("gate_command", "")
    if not isinstance(value, str):
        _fail(f"{where}.gate_command", "must be a string")
    if value and not value.strip():
        _fail(f"{where}.gate_command", "must be empty or a command")
    _no_control(value, f"{where}.gate_command")
    return value


def _image_source(image_table, forgejo_token_env, *, required):
    where = "image.source"
    table = _table(
        image_table.get("source", {}),
        where,
        ("branch", "token_env", "basis_paths", "max_archive_bytes", "presign_seconds", "strip_components"),
    )
    branch = _string(table, "branch", where, required=False, pattern=_BRANCH, pattern_name="a branch name")
    if branch is not None and ".." in branch:
        _fail(f"{where}.branch", "must not contain \"..\"")
    token_env = _string(table, "token_env", where, required=required, pattern=_ENV_NAME, pattern_name="ENV_NAME")
    if token_env is not None and token_env == forgejo_token_env:
        _fail(
            f"{where}.token_env",
            "must differ from forgejo.token_env: the image path never uses the token of apply",
        )
    basis_paths = _image_basis_paths(table, where, required=required)
    max_archive_bytes = _integer(
        table, "max_archive_bytes", where, minimum=1_048_576, maximum=2_147_483_648, required=False
    )
    presign_seconds = _integer(table, "presign_seconds", where, minimum=300, maximum=3600, required=False)
    strip_components = _integer(table, "strip_components", where, minimum=0, maximum=1, required=False)
    if not required:
        return None
    return ImageSourceConfig(
        branch=_default(branch, "main"),
        token_env=token_env,
        basis_paths=basis_paths,
        max_archive_bytes=_default(max_archive_bytes, 268_435_456),
        presign_seconds=_default(presign_seconds, 1800),
        strip_components=_default(strip_components, 1),
    )


def _image_warm(image_table, *, required):
    where = "image.warm"
    table = _table(
        image_table.get("warm", {}),
        where,
        ("commands", "gate_command", "allow_no_gate", "lifetime_seconds", "snapshot_seconds"),
    )
    commands = _image_commands(table, where, required=required)
    gate_command = _image_gate_command(table, where)
    allow_no_gate = _boolean(table, "allow_no_gate", where, default=False)
    lifetime_seconds = _integer(table, "lifetime_seconds", where, minimum=600, maximum=14_400, required=False)
    snapshot_seconds = _integer(table, "snapshot_seconds", where, minimum=60, maximum=3600, required=False)
    if not required:
        return None
    return ImageWarmConfig(
        commands=commands,
        gate_command=gate_command,
        allow_no_gate=allow_no_gate,
        lifetime_seconds=_default(lifetime_seconds, 3600),
        snapshot_seconds=_default(snapshot_seconds, 1800),
    )


def _image_build(image_table, worker_template, *, required):
    where = "image.build"
    table = _table(
        image_table.get("build", {}), where, ("launch_template_name", "launch_template_version")
    )
    name = _string(
        table, "launch_template_name", where, required=required,
        pattern=_TEMPLATE_NAME, pattern_name="a launch template name",
    )
    if name is not None and name == worker_template:
        _fail(
            f"{where}.launch_template_name",
            "must differ from aws.launch_template_name: the builder never launches from the worker template",
        )
    version = _integer(
        table, "launch_template_version", where, minimum=1, maximum=10_000, required=required
    )
    if not required:
        return None
    return ImageBuildConfig(launch_template_name=name, launch_template_version=version)


def _image_storage(image_table, *, required):
    where = "image.storage"
    table = _table(image_table.get("storage", {}), where, ("bucket", "prefix"))
    bucket = _string(table, "bucket", where, required=required, pattern=_BUCKET, pattern_name="a bucket name")
    prefix = _string(table, "prefix", where, required=required)
    if prefix is not None:
        if prefix.startswith("/") or ".." in prefix:
            _fail(f"{where}.prefix", 'must be a key prefix: no ".." and no leading "/"')
        _no_control(prefix, f"{where}.prefix")
    if not required:
        return None
    return ImageStorageConfig(bucket=bucket, prefix=prefix)


def _image_state_directory(table, where, *, required, ledger_directory):
    """The image state directory: absolute, and apart from the scaling ledger's directory.

    The image unit mounts this directory writable and must never see the ledger,
    so the two directories may not be the same, and neither may lie inside the
    other. The comparison follows symbolic links on both sides. The value that
    is returned is the path as the operator wrote it (``.`` and doubled
    separators collapsed), because that is the path the unit mounts.
    """
    value = table.get("state_directory")
    if value is None:
        if required:
            _fail(f"{where}.state_directory", "is required")
        return None
    if not isinstance(value, str) or not value:
        _fail(f"{where}.state_directory", "must be a non-empty string")
    _no_control(value, f"{where}.state_directory")
    path = Path(value)
    if not path.is_absolute():
        _fail(f"{where}.state_directory", "must be an absolute path")
    if ".." in path.parts:
        _fail(f"{where}.state_directory", 'must not contain ".."')
    directory = Path(os.path.normpath(path))
    resolved, ledger = Path(os.path.realpath(directory)), Path(os.path.realpath(ledger_directory))
    if resolved == ledger or ledger in resolved.parents or resolved in ledger.parents:
        _fail(
            f"{where}.state_directory",
            "must be a different directory from the scaling ledger's directory (the parent of state.database), "
            "neither inside it nor containing it",
        )
    return directory


def _image(raw, aws_profile, forgejo_token_env, worker_template, ledger_directory):
    """Return the validated image configuration, or the disabled value.

    The tables are checked whenever they are present, so an unknown key, an
    out-of-range value or an unsafe path never waits for the operator to enable
    the feature. Required keys and the returned fields exist only when enabled.
    """
    image_table = raw.get("image")
    if image_table is None:
        return ImageConfig()
    where = "image"
    table = _table(
        image_table,
        where,
        (
            "enabled",
            "profile",
            "min_build_interval_seconds",
            "max_builds_per_day",
            "build_deadline_seconds",
            "failure_cooldown_seconds",
            "max_consecutive_failures",
            "retain",
            "retain_min_age_seconds",
            "promote_max_age_seconds",
            "state_directory",
            "source",
            "warm",
            "build",
            "storage",
        ),
    )
    enabled = _boolean(table, "enabled", where, default=False)
    profile = table.get("profile")
    if profile is None:
        if enabled:
            _fail("image.profile", "is required")
    elif not isinstance(profile, str) or not profile:
        _fail("image.profile", "must be a non-empty string")
    elif profile == aws_profile:
        _fail("image.profile", "must differ from aws.profile: the image path never reuses the scaling profile")
    ranges = {
        "min_build_interval_seconds": (3600, 604_800, 86_400),
        "max_builds_per_day": (1, 6, 2),
        "build_deadline_seconds": (900, 14_400, 2700),
        "failure_cooldown_seconds": (600, 172_800, 21_600),
        "max_consecutive_failures": (1, 10, 3),
        "retain": (2, 6, 3),
        "retain_min_age_seconds": (3600, 2_592_000, 86_400),
        "promote_max_age_seconds": (3600, 2_592_000, 172_800),
    }
    values = {
        key: _default(_integer(table, key, where, minimum=low, maximum=high, required=False), default)
        for key, (low, high, default) in ranges.items()
    }
    source = _image_source(table, forgejo_token_env, required=enabled)
    warm = _image_warm(table, required=enabled)
    build = _image_build(table, worker_template, required=enabled)
    storage = _image_storage(table, required=enabled)
    state_directory = _image_state_directory(table, where, required=enabled, ledger_directory=ledger_directory)
    if not enabled:
        return ImageConfig()
    return ImageConfig(
        enabled=True,
        profile=profile,
        source=source,
        warm=warm,
        build=build,
        storage=storage,
        state_directory=state_directory,
        **values,
    )


def load(path):
    """Load and validate a configuration file, or raise ``ConfigRefusal``."""
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigRefusal("CONFIG_MISSING", str(config_path)) from None
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigRefusal("CONFIG_UNREADABLE", f"{config_path}: {error}") from None
    return loads(text, config_path)


def loads(text, path):
    """Validate configuration text as ``load`` validates a file, or raise ``ConfigRefusal``.

    ``path`` is where the text lives: it is kept as ``Config.path`` and a relative
    ``state.database`` resolves against its directory. Nothing is read or written,
    so a caller can check a new text before it replaces the file.
    """
    config_path = Path(path)
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ConfigRefusal("CONFIG_INVALID", f"TOML: {error}") from None

    _table(raw, "config", ("deployment", "controller", "forgejo", "worker", "aws", "budget", "state", "image"))

    deployment = _string(raw, "deployment", "config", pattern=_DEPLOYMENT, pattern_name="a tag-safe name")

    controller_table = _table(
        raw.get("controller", {}),
        "controller",
        ("max_instances", "pass_deadline_seconds", "claim_grace_seconds"),
    )
    max_instances = _integer(
        controller_table, "max_instances", "controller", minimum=1, maximum=12, required=False
    )
    pass_deadline_seconds = _integer(
        controller_table,
        "pass_deadline_seconds",
        "controller",
        minimum=30,
        maximum=290,
        required=False,
    )
    claim_grace_seconds = _integer(
        controller_table,
        "claim_grace_seconds",
        "controller",
        minimum=60,
        maximum=1800,
        required=False,
    )
    controller = ControllerConfig(
        max_instances=1 if max_instances is None else max_instances,
        pass_deadline_seconds=240 if pass_deadline_seconds is None else pass_deadline_seconds,
        claim_grace_seconds=300 if claim_grace_seconds is None else claim_grace_seconds,
    )

    forgejo_table = _table(
        raw.get("forgejo", {}),
        "forgejo",
        ("base_url", "repository", "token_env", "contract", "qualification", "timeout_seconds", "max_response_bytes"),
    )
    contract = _string(forgejo_table, "contract", "forgejo")
    if contract not in SUPPORTED_CONTRACTS:
        _fail("forgejo.contract", "must name a supported contract: " + ", ".join(sorted(SUPPORTED_CONTRACTS)))
    qualification = _string(forgejo_table, "qualification", "forgejo")
    if qualification not in QUALIFICATIONS:
        _fail("forgejo.qualification", "must be one of: " + ", ".join(QUALIFICATIONS))
    repository = _string(forgejo_table, "repository", "forgejo", pattern=_REPOSITORY, pattern_name="owner/name")
    forgejo = ForgejoConfig(
        base_url=_https_url(forgejo_table, "forgejo"),
        repository=repository,
        token_env=_string(forgejo_table, "token_env", "forgejo", pattern=_ENV_NAME, pattern_name="ENV_NAME"),
        contract=contract,
        qualification=qualification,
        timeout_seconds=_integer(forgejo_table, "timeout_seconds", "forgejo", minimum=1, maximum=60, required=False) or 10,
        max_response_bytes=_integer(
            forgejo_table, "max_response_bytes", "forgejo", minimum=1024, maximum=64_000_000, required=False
        )
        or 2_000_000,
    )

    worker_table = _table(raw.get("worker", {}), "worker", ("labels",))
    worker = WorkerConfig(labels=_labels(worker_table, "worker"))

    aws_table = _table(
        raw.get("aws", {}),
        "aws",
        (
            "region",
            "availability_zone",
            "availability_zones",
            "instance_types",
            "launch_template_name",
            "launch_template_version",
            "max_spot_price",
            "max_lifetime_seconds",
            "price_max_age_seconds",
            "profile",
        ),
    )
    region = _string(aws_table, "region", "aws", pattern=_REGION, pattern_name="an AWS Region name")
    launch_template_version = _integer(aws_table, "launch_template_version", "aws", minimum=1, maximum=10_000)
    zones = _zone_candidates(aws_table, region, launch_template_version)
    instance_types = _instance_types(aws_table)
    profile = aws_table.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile):
        _fail("aws.profile", "must be a non-empty string when set")
    aws = AwsConfig(
        region=region,
        zones=zones,
        instance_types=instance_types,
        launch_template_name=_string(
            aws_table, "launch_template_name", "aws", pattern=_TEMPLATE_NAME, pattern_name="a launch template name"
        ),
        launch_template_version=launch_template_version,
        max_spot_price=_money(aws_table, "max_spot_price", "aws"),
        max_lifetime_seconds=_integer(aws_table, "max_lifetime_seconds", "aws", minimum=60, maximum=86_400),
        price_max_age_seconds=_integer(
            aws_table, "price_max_age_seconds", "aws", minimum=60, maximum=86_400, required=False
        )
        or 900,
        profile=profile,
    )

    budget_table = _table(
        raw.get("budget", {}),
        "budget",
        ("window_seconds", "allowance", "non_compute_allowance", "incidental_per_hour"),
    )
    budget = BudgetConfig(
        window_seconds=_integer(budget_table, "window_seconds", "budget", minimum=60, maximum=31_536_000),
        allowance=_money(budget_table, "allowance", "budget"),
        non_compute_allowance=_money(budget_table, "non_compute_allowance", "budget", required=False) or 0,
        incidental_per_hour=_money(budget_table, "incidental_per_hour", "budget", required=False) or 0,
    )

    state_table = _table(raw.get("state", {}), "state", ("database",))
    database = Path(_string(state_table, "database", "state"))
    if not database.is_absolute():
        database = (config_path.parent / database).resolve()
    state = StateConfig(database=database)

    return Config(
        deployment=deployment,
        path=config_path,
        forgejo=forgejo,
        worker=worker,
        controller=controller,
        aws=aws,
        budget=budget,
        state=state,
        image=_image(raw, profile, forgejo.token_env, aws.launch_template_name, database.parent),
    )
