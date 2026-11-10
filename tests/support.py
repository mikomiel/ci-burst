"""Test doubles: a fake AWS adapter, observer and clock.

The fakes implement the same narrow interface as the live adapters, so the
controller's decision logic is exercised without an AWS account, a Forgejo
token or a network. Every fake records calls, which lets tests assert that a
planning path performed no mutation.
"""

import json
from pathlib import Path

from ci_burst.aws import Instance, SpotQuote, Template
from ci_burst.forgejo import Attempt, Observation


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def template(**overrides):
    values = {
        "name": "ci-burst-runner",
        "template_id": "lt-0123456789abcdef0",
        "version": 3,
        "image_id": "ami-0123456789abcdef0",
        "instance_type": "t4g.small",
        "shutdown_behavior": "terminate",
        "has_user_data": True,
        "placement_zone": None,
    }
    values.update(overrides)
    return Template(**values)


def quote(price_micros=20_000, timestamp=None, clock=None, observed_at=None):
    observed = observed_at if observed_at is not None else (clock.now if clock else 1_800_000_000.0)
    return SpotQuote(
        price_micros=price_micros,
        timestamp=timestamp if timestamp is not None else observed,
        price_display=f"{price_micros / 1_000_000:.6f}",
        instance_type="t4g.small",
        availability_zone="eu-central-1a",
        observed_at=observed,
    )


def instance(instance_id="i-0123456789abcdef0", state="running", operation="op", expires_at=None, **tags):
    values = {"ci-burst:managed": "true", "ci-burst:deployment": "test-deployment", "ci-burst:operation": operation}
    if expires_at is not None:
        values["ci-burst:expires-at"] = str(int(expires_at))
    values.update(tags)
    return Instance(instance_id=instance_id, state=state, tags=values, launch_time=0.0)


class FakeAws:
    def __init__(self, *, template_facts=None, spot_quote=None, instances=None, error=None):
        self.template_facts = template_facts if template_facts is not None else template()
        self.spot_quote_value = spot_quote
        self.instances_result = list(instances or [])
        self.error = error
        self.calls = []
        self.run_error = None
        self.run_result = ["i-0123456789abcdef0"]
        self.terminate_error = None
        self.terminated = []
        self.instances_by_id = {}
        self.account = "111122223333"
        self.on_run = None
        self.on_quote = None

    def _maybe_raise(self):
        if self.error is not None:
            raise self.error

    def account_id(self):
        self.calls.append(("account_id",))
        self._maybe_raise()
        return self.account

    def template(self, name, version):
        self.calls.append(("template", name, version))
        self._maybe_raise()
        if isinstance(self.template_facts, Exception):
            raise self.template_facts
        return self.template_facts

    def spot_quote(self, instance_type, availability_zone):
        self.calls.append(("spot_quote", instance_type, availability_zone))
        if self.on_quote is not None:
            self.on_quote()
        self._maybe_raise()
        if isinstance(self.spot_quote_value, Exception):
            raise self.spot_quote_value
        return self.spot_quote_value

    def owned_instances(self, deployment):
        self.calls.append(("owned_instances", deployment))
        self._maybe_raise()
        return list(self.instances_result)

    def instances(self, instance_ids):
        self.calls.append(("instances", list(instance_ids)))
        return [self.instances_by_id[identifier] for identifier in instance_ids if identifier in self.instances_by_id]

    def run_spot(self, request):
        self.calls.append(("run_spot", json.loads(json.dumps(request))))
        if self.on_run is not None:
            self.on_run(request)
        if self.run_error is not None:
            raise self.run_error
        return list(self.run_result)

    def terminate(self, instance_ids):
        self.calls.append(("terminate", list(instance_ids)))
        if self.terminate_error is not None:
            raise self.terminate_error
        self.terminated.extend(instance_ids)
        return {}

    def mutations(self):
        return [call for call in self.calls if call[0] in ("run_spot", "terminate")]


class FakeObserver:
    def __init__(self, observation=None, error=None):
        self.observation = observation if observation is not None else observation_empty()
        self.error = error
        self.calls = 0

    def observe(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.observation


def observation_empty():
    return Observation(contract="forgejo-17.0.1-runners-jobs", attempts=(), seen=0, blocked=0, assigned=0, incompatible=0, duplicates=0)


def observation_demand(count=1):
    attempts = tuple(
        Attempt(handle=f"handle-{index}", job_id=index, attempt=1, status="waiting", labels=("homelab",), needs=())
        for index in range(count)
    )
    return Observation(
        contract="forgejo-17.0.1-runners-jobs",
        attempts=attempts,
        seen=count,
        blocked=0,
        assigned=0,
        incompatible=0,
        duplicates=0,
    )


def write_config(tmp_path, *, config=None):
    """Render the default valid deployment configuration as TOML."""
    values = {
        "deployment": "test-deployment",
        "forgejo": {
            "base_url": "https://forgejo.example",
            "repository": "owner/repo",
            "token_env": "CI_BURST_TEST_TOKEN",
            "contract": "forgejo-17.0.1-runners-jobs",
            "qualification": "live-verified",
        },
        "worker": {"labels": ["ci-burst:host", "homelab:host"]},
        "aws": {
            "region": "eu-central-1",
            "availability_zone": "eu-central-1a",
            "launch_template_name": "ci-burst-runner",
            "launch_template_version": 3,
            "max_spot_price": "0.0400",
            "max_lifetime_seconds": 3600,
        },
        "budget": {"window_seconds": 86400, "allowance": "10.00", "non_compute_allowance": "0.50"},
        "state": {"database": "state.sqlite3"},
    }
    if config:
        for section, overrides in config.items():
            if isinstance(overrides, dict):
                values.setdefault(section, {}).update(overrides)
            else:
                values[section] = overrides
    path = Path(tmp_path) / "deployment.toml"
    path.write_text(_render(values), encoding="utf-8")
    return path


def _render(values):
    lines = []
    for key, value in values.items():
        if isinstance(value, dict):
            lines.append(f"[{key}]")
            for inner, inner_value in value.items():
                lines.append(f"{inner} = {_toml(inner_value)}")
            lines.append("")
        else:
            lines.append(f"{key} = {_toml(value)}")
    return "\n".join(lines)


def _toml(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    return json.dumps(str(value))
