"""Strict TOML configuration for one bounded deployment.

The configuration is product input, not a deployment record: it names no
private host, credential or account. Every value is validated once, here, so a
typo cannot silently widen scope. Unknown keys and sections are refused rather
than ignored -- an unrecognized setting is far more likely to be a mistake than
an extension point.

Placement is configured as an ordered list of availability-zone candidates. The
original single-zone key remains supported and is normalized to a one-candidate
list, so no consumer chooses between two placement shapes.
"""

import re
import tomllib
from dataclasses import dataclass
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

#: Keys accepted inside one ``[[aws.availability_zones]]`` table.
_ZONE_CANDIDATE_KEYS = ("zone", "subnet_id", "launch_template_version")


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
    region: str
    zones: tuple
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


@dataclass(frozen=True)
class StateConfig:
    database: Path


@dataclass(frozen=True)
class Config:
    deployment: str
    path: Path
    forgejo: ForgejoConfig
    worker: WorkerConfig
    aws: AwsConfig
    budget: BudgetConfig
    state: StateConfig

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


def load(path):
    """Load and validate a configuration file, or raise ``ConfigRefusal``."""
    config_path = Path(path)
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigRefusal("CONFIG_MISSING", str(config_path)) from None
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigRefusal("CONFIG_UNREADABLE", f"{config_path}: {error}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigRefusal("CONFIG_INVALID", f"TOML: {error}") from None

    _table(raw, "config", ("deployment", "controller", "forgejo", "worker", "aws", "budget", "state"))

    deployment = _string(raw, "deployment", "config", pattern=_DEPLOYMENT, pattern_name="a tag-safe name")

    controller = _table(raw.get("controller", {}), "controller", ("max_instances",))
    max_instances = _integer(controller, "max_instances", "controller", minimum=1, maximum=1, required=False)
    if max_instances not in (None, 1):
        _fail("controller.max_instances", "only one owned instance is supported")

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
    profile = aws_table.get("profile")
    if profile is not None and (not isinstance(profile, str) or not profile):
        _fail("aws.profile", "must be a non-empty string when set")
    aws = AwsConfig(
        region=region,
        zones=zones,
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

    budget_table = _table(raw.get("budget", {}), "budget", ("window_seconds", "allowance", "non_compute_allowance"))
    budget = BudgetConfig(
        window_seconds=_integer(budget_table, "window_seconds", "budget", minimum=60, maximum=31_536_000),
        allowance=_money(budget_table, "allowance", "budget"),
        non_compute_allowance=_money(budget_table, "non_compute_allowance", "budget", required=False) or 0,
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
        aws=aws,
        budget=budget,
        state=state,
    )
