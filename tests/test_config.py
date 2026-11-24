import copy
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from ci_burst.config import WORKER_LIFETIME_SECONDS, ImageConfig, load, loads
from ci_burst.errors import ConfigRefusal

from support import write_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)

    def test_valid_configuration_loads(self):
        config = load(write_config(self.root))
        self.assertEqual(config.deployment, "test-deployment")
        self.assertEqual(config.worker.labels, ("ci-burst:host", "homelab:host"))
        self.assertEqual(config.controller.max_instances, 1)
        self.assertEqual(config.aws.max_spot_price, 40_000)
        self.assertEqual(config.state.database, self.root / "state.sqlite3")

    def test_accepts_a_fleet_of_four(self):
        config = load(write_config(self.root, config={"controller": {"max_instances": 4}}))
        self.assertEqual(config.controller.max_instances, 4)

    def test_accepts_the_ceiling(self):
        config = load(write_config(self.root, config={"controller": {"max_instances": 12}}))
        self.assertEqual(config.controller.max_instances, 12)

    def test_pass_deadline_defaults_to_240_seconds(self):
        self.assertEqual(load(write_config(self.root)).controller.pass_deadline_seconds, 240)

    def test_accepts_the_pass_deadline_range(self):
        for value in (30, 290):
            with self.subTest(value=value):
                config = load(
                    write_config(
                        self.root, config={"controller": {"pass_deadline_seconds": value}}
                    )
                )
                self.assertEqual(config.controller.pass_deadline_seconds, value)

    def test_refuses_a_pass_deadline_outside_the_range(self):
        for value in (29, 291):
            with self.subTest(value=value):
                path = write_config(
                    self.root, config={"controller": {"pass_deadline_seconds": value}}
                )
                with self.assertRaises(ConfigRefusal) as raised:
                    load(path)
                self.assertEqual(raised.exception.reason, "CONFIG_INVALID")
                self.assertEqual(
                    raised.exception.detail,
                    "controller.pass_deadline_seconds: must be between 30 and 290",
                )

    def test_refuses_a_non_integer_pass_deadline(self):
        path = write_config(
            self.root, config={"controller": {"pass_deadline_seconds": 240.0}}
        )
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertIn("controller.pass_deadline_seconds", raised.exception.detail)

    def test_claim_grace_defaults_to_300_seconds(self):
        self.assertEqual(load(write_config(self.root)).controller.claim_grace_seconds, 300)

    def test_claim_grace_default_equals_the_worker_claim_window(self):
        # The grace covers a launch until its worker's claim window ends, so the
        # default must follow the single literal in the shipped bootstrap.
        bootstrap = Path(__file__).resolve().parent.parent / "deploy" / "runner" / "bootstrap.sh"
        windows = re.findall(r"^CLAIM_WINDOW_SECONDS=(\d+)$", bootstrap.read_text(), re.MULTILINE)
        self.assertEqual(len(windows), 1)
        self.assertEqual(
            load(write_config(self.root)).controller.claim_grace_seconds, int(windows[0])
        )

    def test_accepts_the_claim_grace_range(self):
        for value in (60, 300, 1800):
            with self.subTest(value=value):
                config = load(
                    write_config(self.root, config={"controller": {"claim_grace_seconds": value}})
                )
                self.assertEqual(config.controller.claim_grace_seconds, value)

    def test_refuses_a_claim_grace_outside_the_range(self):
        for value in (59, 1801):
            with self.subTest(value=value):
                path = write_config(
                    self.root, config={"controller": {"claim_grace_seconds": value}}
                )
                with self.assertRaises(ConfigRefusal) as raised:
                    load(path)
                self.assertEqual(raised.exception.reason, "CONFIG_INVALID")
                self.assertEqual(
                    raised.exception.detail,
                    "controller.claim_grace_seconds: must be between 60 and 1800",
                )

    def test_refuses_a_non_integer_claim_grace(self):
        # A float, a quoted string and a boolean are all not integers; a boolean
        # is an int subtype in Python but not an integer in the configuration.
        for value in (300.0, "300", True):
            with self.subTest(value=value):
                path = write_config(
                    self.root, config={"controller": {"claim_grace_seconds": value}}
                )
                with self.assertRaises(ConfigRefusal) as raised:
                    load(path)
                self.assertEqual(raised.exception.reason, "CONFIG_INVALID")
                self.assertEqual(
                    raised.exception.detail,
                    "controller.claim_grace_seconds: must be an integer",
                )

    def test_the_controller_table_still_refuses_an_unknown_key(self):
        # The new key joins the allowed list without turning the check off: a
        # near miss of its name is refused and named.
        path = write_config(self.root, config={"controller": {"claim_grace": 300}})
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertEqual(raised.exception.reason, "CONFIG_INVALID")
        self.assertEqual(raised.exception.detail, "controller: unknown key(s) claim_grace")

    def test_refuses_zero_instances(self):
        path = write_config(self.root, config={"controller": {"max_instances": 0}})
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertEqual(raised.exception.reason, "CONFIG_INVALID")
        self.assertEqual(raised.exception.detail, "controller.max_instances: must be between 1 and 12")

    def test_refuses_a_fleet_above_the_ceiling(self):
        path = write_config(self.root, config={"controller": {"max_instances": 13}})
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertEqual(raised.exception.reason, "CONFIG_INVALID")
        self.assertEqual(raised.exception.detail, "controller.max_instances: must be between 1 and 12")

    def test_instance_types_are_optional_and_ordered(self):
        config = load(
            write_config(
                self.root,
                config={"aws": {"instance_types": ["m7g.xlarge", "m8g.xlarge"]}},
            )
        )
        self.assertEqual(config.aws.instance_types, ("m7g.xlarge", "m8g.xlarge"))

    def test_absent_instance_types_keep_the_template_type(self):
        config = load(write_config(self.root))
        self.assertEqual(config.aws.instance_types, ())

    def test_refuses_an_empty_instance_type_list(self):
        path = write_config(self.root, config={"aws": {"instance_types": []}})
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertEqual(raised.exception.reason, "INSTANCE_TYPES_INVALID")
        self.assertEqual(
            raised.exception.detail,
            "aws.instance_types: must be a non-empty array of instance type names",
        )

    def test_refuses_a_duplicate_instance_type(self):
        path = write_config(
            self.root, config={"aws": {"instance_types": ["m7g.xlarge", "m7g.xlarge"]}}
        )
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertEqual(raised.exception.reason, "INSTANCE_TYPES_INVALID")
        self.assertEqual(raised.exception.detail, "aws.instance_types[1]: must be unique")

    def test_accepts_hyphenated_instance_type_names(self):
        config = load(
            write_config(
                self.root,
                config={"aws": {"instance_types": ["m7i.metal-24xl", "u-6tb1.112xlarge"]}},
            )
        )
        self.assertEqual(
            config.aws.instance_types, ("m7i.metal-24xl", "u-6tb1.112xlarge")
        )

    def test_refuses_an_instance_type_with_a_trailing_hyphen(self):
        path = write_config(self.root, config={"aws": {"instance_types": ["m7i.metal-"]}})
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertEqual(raised.exception.reason, "INSTANCE_TYPES_INVALID")
        self.assertIn("must be an instance type name", raised.exception.detail)

    def test_refuses_a_malformed_instance_type(self):
        path = write_config(self.root, config={"aws": {"instance_types": ["M7G.XLARGE"]}})
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertEqual(raised.exception.reason, "INSTANCE_TYPES_INVALID")
        self.assertIn("must be an instance type name", raised.exception.detail)

    def test_refuses_more_than_four_instance_types(self):
        path = write_config(
            self.root,
            config={
                "aws": {
                    "instance_types": [
                        "m6g.xlarge",
                        "m7g.xlarge",
                        "m8g.xlarge",
                        "c7g.xlarge",
                        "r7g.xlarge",
                    ]
                }
            },
        )
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertEqual(raised.exception.reason, "INSTANCE_TYPES_INVALID")
        self.assertEqual(
            raised.exception.detail,
            "aws.instance_types: must list at most 4 instance types",
        )

    def test_relative_state_path_resolves_against_config(self):
        config = load(write_config(self.root, config={"state": {"database": "var/state.sqlite3"}}))
        self.assertTrue(str(config.state.database).endswith("var/state.sqlite3"))

    def test_refuses_unknown_keys(self):
        path = write_config(self.root)
        path.write_text(path.read_text() + "\nunexpected = true\n")
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertIn("unknown key", raised.exception.detail)

    def test_refuses_float_money(self):
        path = write_config(self.root)
        path.write_text(path.read_text().replace('max_spot_price = "0.0400"', "max_spot_price = 0.04"))
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertIn("quoted decimal string", raised.exception.detail)

    def test_refuses_mutable_template_version(self):
        path = write_config(self.root, config={"aws": {"launch_template_version": "$Latest"}})
        with self.assertRaises(ConfigRefusal):
            load(path)

    def test_refuses_plain_text_forgejo_url(self):
        path = write_config(self.root, config={"forgejo": {"base_url": "http://forgejo.example"}})
        with self.assertRaises(ConfigRefusal):
            load(path)

    def test_refuses_unknown_contract(self):
        path = write_config(self.root, config={"forgejo": {"contract": "unknown"}})
        with self.assertRaises(ConfigRefusal):
            load(path)

    def test_refuses_unknown_qualification(self):
        path = write_config(self.root, config={"forgejo": {"qualification": "ready"}})
        with self.assertRaises(ConfigRefusal):
            load(path)

    def test_refuses_availability_zone_outside_region(self):
        path = write_config(self.root, config={"aws": {"availability_zone": "us-east-1a"}})
        with self.assertRaises(ConfigRefusal):
            load(path)

    def test_refuses_duplicate_labels(self):
        path = write_config(self.root, config={"worker": {"labels": ["ci-burst:host", "ci-burst:host"]}})
        with self.assertRaises(ConfigRefusal):
            load(path)

    def test_refuses_missing_required_limit(self):
        path = write_config(self.root)
        text = path.read_text()
        path.write_text(text.replace('allowance = "10.00"\n', ""))
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertIn("budget.allowance", raised.exception.detail)

    def test_incidental_rate_defaults_to_zero(self):
        self.assertEqual(load(write_config(self.root)).budget.incidental_per_hour, 0)

    def test_incidental_rate_is_parsed_in_micros(self):
        path = write_config(self.root, config={"budget": {"incidental_per_hour": "0.005"}})
        self.assertEqual(load(path).budget.incidental_per_hour, 5_000)

    def test_incidental_rate_refuses_a_float(self):
        path = write_config(self.root, config={"budget": {"incidental_per_hour": 0.005}})
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertIn("budget.incidental_per_hour", raised.exception.detail)

    def test_refuses_missing_lifetime(self):
        path = write_config(self.root)
        path.write_text(path.read_text().replace("max_lifetime_seconds = 3600\n", ""))
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertIn("aws.max_lifetime_seconds", raised.exception.detail)

    def test_the_loader_accepts_another_lifetime_and_only_admission_refuses_it(self):
        # reap, status, export and plan must keep working on a file that still holds another value (#138).
        for seconds in (60, 1800, 7200):
            with self.subTest(seconds=seconds):
                config = load(write_config(self.root, config={"aws": {"max_lifetime_seconds": seconds}}))
                self.assertEqual(config.aws.max_lifetime_seconds, seconds)
                with self.assertRaises(ConfigRefusal) as raised:
                    config.require_worker_lifetime()
                self.assertEqual(raised.exception.reason, "WORKER_LIFETIME_MISMATCH")
                self.assertIn(str(WORKER_LIFETIME_SECONDS), raised.exception.detail)
        load(write_config(self.root)).require_worker_lifetime()  # the shipped default is the bootstrap's value

    def test_the_lifetime_matches_every_clock_in_the_worker_bootstrap(self):
        bootstrap = (Path(__file__).resolve().parents[1] / "deploy" / "runner" / "bootstrap.sh").read_text()
        shutdown = re.findall(r"systemd-run --on-active=(\d+) --unit=runner-lifetime ", bootstrap)
        job = re.findall(r"timeout --signal=TERM --kill-after=\d+ (\d+) ", bootstrap)
        self.assertEqual(shutdown, [str(WORKER_LIFETIME_SECONDS)])
        self.assertEqual(job, [str(WORKER_LIFETIME_SECONDS)])

    def test_missing_file(self):
        with self.assertRaises(ConfigRefusal) as raised:
            load(self.root / "absent.toml")
        self.assertEqual(raised.exception.reason, "CONFIG_MISSING")

    def test_invalid_toml(self):
        path = self.root / "broken.toml"
        path.write_text("deployment = ")
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertEqual(raised.exception.reason, "CONFIG_INVALID")

    def test_fixtures_only_qualification_refuses_live(self):
        config = load(write_config(self.root, config={"forgejo": {"qualification": "fixtures-only"}}))
        with self.assertRaises(ConfigRefusal) as raised:
            config.require_live_contract()
        self.assertEqual(raised.exception.reason, "FORGEJO_CONTRACT_UNQUALIFIED")

    def test_live_verified_qualification_permits_live(self):
        config = load(write_config(self.root))
        config.require_live_contract()


#: One valid, enabled image configuration with synthetic names only. Each test
#: overrides or removes single keys so a refusal names exactly one cause.
_IMAGE = {
    "enabled": True,
    "profile": "example-image-role",
    "state_directory": "/var/lib/example-image-state",
    "source": {
        "token_env": "EXAMPLE_IMAGE_TOKEN",
        "basis_paths": ["example.lock"],
    },
    "warm": {"commands": ["bash scripts/example-warm.sh"]},
    "build": {"launch_template_name": "example-builder", "launch_template_version": 1},
    "storage": {"bucket": "example-staging", "prefix": "example-prefix/"},
}


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    # JSON string escapes are valid TOML except for DEL, which JSON leaves raw.
    return json.dumps(value).replace("\x7f", "\\u007f")


def _image_text(image):
    """Render an ``[image]`` table and its sub-tables, in TOML."""
    lines = ["", "[image]"]
    for key, value in image.items():
        if not isinstance(value, dict):
            lines.append(f"{key} = {_toml_value(value)}")
    for name, table in image.items():
        if isinstance(table, dict):
            lines.append(f"[image.{name}]")
            lines.extend(f"{key} = {_toml_value(value)}" for key, value in table.items())
    return "\n".join(lines) + "\n"


class ImageConfigTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)

    def _path(self, image=None, *, aws=None, raw=None):
        path = write_config(self.root, config={"aws": aws} if aws else None)
        if raw is not None:
            path.write_text(path.read_text() + raw)
        elif image is not None:
            path.write_text(path.read_text() + _image_text(image))
        return path

    def _image(self, *, top=None, **tables):
        """A valid image table with ``top`` keys and per-table keys changed.

        A ``None`` value removes the key; a ``None`` table removes the table.
        """
        image = copy.deepcopy(_IMAGE)
        for key, value in (top or {}).items():
            image.pop(key, None) if value is None else image.__setitem__(key, value)
        for name, changes in tables.items():
            if changes is None:
                image.pop(name, None)
                continue
            for key, value in changes.items():
                image[name].pop(key, None) if value is None else image[name].__setitem__(key, value)
        return image

    def _load(self, image, **kwargs):
        return load(self._path(image, **kwargs))

    def assertRefused(self, image, fragment, **kwargs):
        with self.assertRaises(ConfigRefusal) as raised:
            self._load(image, **kwargs)
        self.assertEqual(raised.exception.reason, "CONFIG_INVALID")
        self.assertIn(fragment, raised.exception.detail)

    def test_an_absent_table_is_disabled_and_changes_nothing_else(self):
        plain = load(write_config(self.root))
        self.assertEqual(plain.image, ImageConfig())
        self.assertFalse(plain.image.enabled)
        enabled_off = self._load({"enabled": False})
        self.assertEqual(enabled_off.image, ImageConfig())
        for name in ("deployment", "forgejo", "worker", "controller", "aws", "budget", "state"):
            self.assertEqual(getattr(enabled_off, name), getattr(plain, name))

    def test_enabled_false_is_disabled_without_the_other_tables(self):
        self.assertEqual(self._load({"enabled": False}).image, ImageConfig())
        self.assertEqual(self._load({}).image, ImageConfig())

    def test_enabled_false_keeps_a_complete_table_disabled(self):
        config = self._load(self._image(top={"enabled": False}))
        self.assertEqual(config.image, ImageConfig())

    def test_enabled_false_still_refuses_an_unknown_or_out_of_range_key(self):
        self.assertRefused({"enabled": False, "surprise": 1}, "unknown key(s) surprise")
        self.assertRefused({"enabled": False, "retain": 99}, "image.retain: must be between 2 and 6")
        self.assertRefused(
            {"enabled": False, "source": {"basis_paths": ["../x"]}}, "image.source.basis_paths[0]"
        )

    def test_enabled_must_be_a_boolean(self):
        self.assertRefused({"enabled": 1}, "image.enabled: must be true or false")

    def test_an_enabled_table_needs_its_required_keys(self):
        self.assertRefused(self._image(top={"profile": None}), "image.profile: is required")
        for table, key in (
            ("source", "token_env"),
            ("source", "basis_paths"),
            ("warm", "commands"),
            ("build", "launch_template_name"),
            ("build", "launch_template_version"),
            ("storage", "bucket"),
            ("storage", "prefix"),
        ):
            with self.subTest(table=table, key=key):
                self.assertRefused(self._image(**{table: {key: None}}), f"image.{table}.{key}: is required")
        for table in ("source", "warm", "build", "storage"):
            with self.subTest(table=table):
                self.assertRefused(self._image(**{table: None}), f"image.{table}.")

    def test_defaults(self):
        image = self._load(self._image()).image
        self.assertTrue(image.enabled)
        self.assertEqual(image.profile, "example-image-role")
        self.assertEqual(image.min_build_interval_seconds, 86_400)
        self.assertEqual(image.max_builds_per_day, 2)
        self.assertEqual(image.build_deadline_seconds, 2700)
        self.assertEqual(image.failure_cooldown_seconds, 21_600)
        self.assertEqual(image.max_consecutive_failures, 3)
        self.assertEqual(image.retain, 3)
        self.assertEqual(image.retain_min_age_seconds, 86_400)
        self.assertEqual(image.promote_max_age_seconds, 172_800)
        self.assertEqual(image.source.branch, "main")
        self.assertEqual(image.source.token_env, "EXAMPLE_IMAGE_TOKEN")
        self.assertEqual(image.source.basis_paths, ("example.lock",))
        self.assertEqual(image.source.max_archive_bytes, 268_435_456)
        self.assertEqual(image.source.presign_seconds, 1800)
        self.assertEqual(image.source.strip_components, 1)
        self.assertEqual(image.warm.commands, ("bash scripts/example-warm.sh",))
        self.assertEqual(image.warm.gate_command, "")
        self.assertFalse(image.warm.allow_no_gate)
        self.assertEqual(image.warm.lifetime_seconds, 3600)
        self.assertEqual(image.warm.snapshot_seconds, 1800)
        self.assertEqual(image.build.launch_template_name, "example-builder")
        self.assertEqual(image.build.launch_template_version, 1)
        self.assertEqual(image.storage.bucket, "example-staging")
        self.assertEqual(image.storage.prefix, "example-prefix/")

    def test_explicit_values_are_loaded(self):
        image = self._load(
            self._image(
                top={"retain": 5},
                source={"branch": "release/1", "strip_components": 0, "presign_seconds": 600},
                warm={
                    "gate_command": "bash scripts/example-gate.sh",
                    "allow_no_gate": True,
                    "commands": ["first", "second"],
                },
            )
        ).image
        self.assertEqual(image.retain, 5)
        self.assertEqual(image.source.branch, "release/1")
        self.assertEqual(image.source.strip_components, 0)
        self.assertEqual(image.source.presign_seconds, 600)
        self.assertEqual(image.warm.gate_command, "bash scripts/example-gate.sh")
        self.assertTrue(image.warm.allow_no_gate)
        self.assertEqual(image.warm.commands, ("first", "second"))

    def test_an_empty_gate_command_loads_so_the_command_can_decide(self):
        image = self._load(self._image(warm={"gate_command": "", "allow_no_gate": True})).image
        self.assertEqual(image.warm.gate_command, "")

    def test_bounds(self):
        # key location, minimum, maximum
        bounds = (
            (None, "min_build_interval_seconds", 3600, 604_800),
            (None, "max_builds_per_day", 1, 6),
            (None, "build_deadline_seconds", 900, 14_400),
            (None, "failure_cooldown_seconds", 600, 172_800),
            (None, "max_consecutive_failures", 1, 10),
            (None, "retain", 2, 6),
            (None, "retain_min_age_seconds", 3600, 2_592_000),
            (None, "promote_max_age_seconds", 3600, 2_592_000),
            ("source", "max_archive_bytes", 1_048_576, 2_147_483_648),
            ("source", "presign_seconds", 300, 3600),
            ("source", "strip_components", 0, 1),
            ("warm", "lifetime_seconds", 600, 14_400),
            ("warm", "snapshot_seconds", 60, 3600),
            ("build", "launch_template_version", 1, 10_000),
        )
        for table, key, low, high in bounds:
            where = f"image.{table}.{key}" if table else f"image.{key}"
            for value in (low, high):
                with self.subTest(key=where, accepted=value):
                    image = self._image(**{table: {key: value}}) if table else self._image(top={key: value})
                    parsed = self._load(image).image
                    target = getattr(parsed, table) if table else parsed
                    self.assertEqual(getattr(target, key), value)
            for value in (low - 1, high + 1):
                with self.subTest(key=where, refused=value):
                    image = self._image(**{table: {key: value}}) if table else self._image(top={key: value})
                    self.assertRefused(image, f"{where}: must be between {low} and {high}")

    def test_refuses_a_non_integer(self):
        for value in (1.0, "2", True):
            with self.subTest(value=value):
                self.assertRefused(
                    self._image(top={"max_builds_per_day": value}),
                    "image.max_builds_per_day: must be an integer",
                )
        self.assertRefused(
            self._image(source={"presign_seconds": 900.5}), "image.source.presign_seconds: must be an integer"
        )

    def test_refuses_an_unknown_key_in_every_table(self):
        self.assertRefused(self._image(top={"check_interval_seconds": 21_600}), "unknown key(s) check_interval_seconds")
        for table in ("source", "warm", "build", "storage"):
            with self.subTest(table=table):
                self.assertRefused(self._image(**{table: {"surprise": 1}}), f"image.{table}: unknown key(s) surprise")
        self.assertRefused(self._image(top={"surprise": 1}), "image: unknown key(s) surprise")

    def test_a_sub_table_must_be_a_table(self):
        self.assertRefused(
            {"enabled": True, "profile": "example-image-role", "source": 1},
            "image.source: must be a table",
        )

    def test_refuses_the_scaling_profile(self):
        self.assertRefused(
            self._image(top={"profile": "example-scaling"}),
            "image.profile: must differ from aws.profile",
            aws={"profile": "example-scaling"},
        )
        # Equality is refused even while the feature is disabled.
        self.assertRefused(
            {"enabled": False, "profile": "example-scaling"},
            "image.profile: must differ from aws.profile",
            aws={"profile": "example-scaling"},
        )

    def test_a_different_or_unset_scaling_profile_is_accepted(self):
        config = self._load(self._image(), aws={"profile": "example-scaling"})
        self.assertEqual(config.image.profile, "example-image-role")
        self.assertIsNone(self._load(self._image()).aws.profile)

    def test_refuses_an_unsafe_basis_path(self):
        for path in ("../secrets", "a/../b", "a/..", "..", "/etc/example", "a\x00b", "a\nb"):
            with self.subTest(path=path):
                self.assertRefused(
                    self._image(source={"basis_paths": ["example.lock", path]}),
                    "image.source.basis_paths[1]",
                )

    def test_refuses_an_empty_basis_path_or_list(self):
        self.assertRefused(self._image(source={"basis_paths": [""]}), "image.source.basis_paths[0]: must be a non-empty string")
        self.assertRefused(self._image(source={"basis_paths": []}), "image.source.basis_paths: must be a non-empty array")
        self.assertRefused(self._image(source={"basis_paths": "example.lock"}), "image.source.basis_paths: must be a non-empty array")

    def test_basis_path_count_bound(self):
        paths = [f"example-{index}.lock" for index in range(33)]
        self.assertEqual(len(self._load(self._image(source={"basis_paths": paths[:32]})).image.source.basis_paths), 32)
        self.assertRefused(
            self._image(source={"basis_paths": paths}),
            "image.source.basis_paths: must list at most 32 paths",
        )

    def test_refuses_a_control_character_in_a_warm_command(self):
        for character in ("\n", "\r", "\t", "\x00", "\x1b", "\x7f"):
            with self.subTest(character=repr(character)):
                self.assertRefused(
                    self._image(warm={"commands": ["bash ok.sh", f"bash a{character}b"]}),
                    "image.warm.commands[1]: must not contain a control character",
                )

    def test_refuses_a_control_character_in_the_gate_command(self):
        for character in ("\n", "\x00", "\x7f"):
            with self.subTest(character=repr(character)):
                self.assertRefused(
                    self._image(warm={"gate_command": f"bash a{character}b"}),
                    "image.warm.gate_command: must not contain a control character",
                )

    def test_refuses_empty_commands(self):
        self.assertRefused(self._image(warm={"commands": []}), "image.warm.commands: must be a non-empty array")
        self.assertRefused(self._image(warm={"commands": ["  "]}), "image.warm.commands[0]: must be a non-empty string")
        self.assertRefused(self._image(warm={"gate_command": "  "}), "image.warm.gate_command: must be empty or a command")
        self.assertRefused(self._image(warm={"gate_command": 1}), "image.warm.gate_command: must be a string")

    def test_refuses_a_strip_components_value_other_than_zero_or_one(self):
        for value in (2, -1):
            with self.subTest(value=value):
                self.assertRefused(
                    self._image(source={"strip_components": value}),
                    "image.source.strip_components: must be between 0 and 1",
                )

    def test_refuses_the_token_env_of_apply(self):
        # The base configuration names CI_BURST_TEST_TOKEN for forgejo.token_env.
        self.assertRefused(
            self._image(source={"token_env": "CI_BURST_TEST_TOKEN"}),
            "image.source.token_env: must differ from forgejo.token_env",
        )
        self.assertRefused(
            {"enabled": False, "source": {"token_env": "CI_BURST_TEST_TOKEN"}},
            "image.source.token_env: must differ from forgejo.token_env",
        )
        self.assertEqual(
            self._load(self._image()).image.source.token_env, "EXAMPLE_IMAGE_TOKEN"
        )

    def test_refuses_the_worker_launch_template_as_the_builder(self):
        # The base configuration names ci-burst-runner for aws.launch_template_name.
        # [aws] carries only a name, so the comparison is by name.
        self.assertRefused(
            self._image(build={"launch_template_name": "ci-burst-runner"}),
            "image.build.launch_template_name: must differ from aws.launch_template_name",
        )
        self.assertRefused(
            {"enabled": False, "build": {"launch_template_name": "ci-burst-runner"}},
            "image.build.launch_template_name: must differ from aws.launch_template_name",
        )
        self.assertEqual(
            self._load(self._image()).image.build.launch_template_name, "example-builder"
        )

    def test_refuses_a_malformed_storage_or_source_name(self):
        self.assertRefused(self._image(storage={"prefix": "/example/"}), "image.storage.prefix")
        self.assertRefused(self._image(storage={"prefix": "a/../b"}), "image.storage.prefix")
        self.assertRefused(self._image(storage={"prefix": "a\nb"}), "image.storage.prefix")
        self.assertRefused(self._image(storage={"bucket": "Bad_Bucket"}), "image.storage.bucket")
        self.assertRefused(self._image(source={"token_env": "lower"}), "image.source.token_env")
        self.assertRefused(self._image(source={"branch": "a/../b"}), "image.source.branch")
        self.assertRefused(self._image(build={"launch_template_name": "x"}), "image.build.launch_template_name")

    # -- [image].state_directory ---------------------------------------------
    # The image unit mounts this directory writable and must never see the
    # scaling ledger, so the two are different directories, neither inside the
    # other. The ledger directory here is the temporary root: ``state.database``
    # is the relative ``state.sqlite3`` that ``write_config`` writes.

    def test_an_enabled_table_needs_a_state_directory(self):
        self.assertRefused(self._image(top={"state_directory": None}), "image.state_directory: is required")

    def test_an_absent_or_disabled_table_needs_no_state_directory(self):
        self.assertEqual(load(write_config(self.root)).image, ImageConfig())
        self.assertEqual(self._load({"enabled": False}).image, ImageConfig())
        self.assertIsNone(self._load(self._image(top={"enabled": False})).image.state_directory)

    def test_a_valid_state_directory_loads_as_an_absolute_path(self):
        for written in (
            "/var/lib/example-image-state",
            "/var/lib/example-image-state/",
            "/var/lib//example-image-state",
        ):
            with self.subTest(written=written):
                image = self._load(self._image(top={"state_directory": written})).image
                self.assertEqual(image.state_directory, Path("/var/lib/example-image-state"))

    def test_refuses_an_unsafe_state_directory(self):
        for value, fragment in (
            ("state", "image.state_directory: must be an absolute path"),
            ("./state", "image.state_directory: must be an absolute path"),
            ("/var/lib/../state", 'image.state_directory: must not contain ".."'),
            ("/var/lib/example/..", 'image.state_directory: must not contain ".."'),
            ("", "image.state_directory: must be a non-empty string"),
            (7, "image.state_directory: must be a non-empty string"),
            ("/var/lib/a\nb", "image.state_directory: must not contain a control character"),
            ("/var/lib/a\x00b", "image.state_directory: must not contain a control character"),
        ):
            with self.subTest(value=value):
                self.assertRefused(self._image(top={"state_directory": value}), fragment)

    def test_refuses_the_ledger_directory_and_anything_inside_it(self):
        inside = (str(self.root), str(self.root / "image"), str(self.root / "a" / "b"))
        for value in inside:
            with self.subTest(value=value):
                self.assertRefused(
                    self._image(top={"state_directory": value}),
                    "image.state_directory: must be a different directory from the scaling ledger's directory",
                )

    def test_refuses_a_directory_that_contains_the_ledger_directory(self):
        # Mounting the parent would mount the ledger with it.
        for value in (str(self.root.parent), "/"):
            with self.subTest(value=value):
                self.assertRefused(
                    self._image(top={"state_directory": value}),
                    "image.state_directory: must be a different directory from the scaling ledger's directory",
                )

    def test_the_ledger_comparison_follows_symbolic_links(self):
        with tempfile.TemporaryDirectory() as other:
            link = Path(other) / "alias"
            os.symlink(self.root, link)
            self.assertRefused(
                self._image(top={"state_directory": str(link)}),
                "image.state_directory: must be a different directory from the scaling ledger's directory",
            )
            below = Path(other) / "below"
            os.symlink(self.root, below)
            self.assertRefused(
                self._image(top={"state_directory": str(below / "image")}),
                "image.state_directory: must be a different directory from the scaling ledger's directory",
            )

    def test_the_ledger_directory_is_the_parent_of_an_absolute_database(self):
        with tempfile.TemporaryDirectory() as ledger:
            path = write_config(self.root, config={"state": {"database": str(Path(ledger) / "scaling.sqlite3")}})
            base = path.read_text(encoding="utf-8")
            path.write_text(base + _image_text(self._image(top={"state_directory": ledger})), encoding="utf-8")
            with self.assertRaises(ConfigRefusal) as raised:
                load(path)
            self.assertEqual(raised.exception.reason, "CONFIG_INVALID")
            self.assertIn("image.state_directory: must be a different directory", raised.exception.detail)
            # The configuration directory is not the ledger directory here.
            path.write_text(base + _image_text(self._image(top={"state_directory": str(self.root)})), encoding="utf-8")
            self.assertEqual(load(path).image.state_directory, self.root)

    def test_a_disabled_table_still_refuses_the_ledger_directory(self):
        self.assertRefused(
            {"enabled": False, "state_directory": str(self.root)},
            "image.state_directory: must be a different directory",
        )

    def test_loads_validates_text_as_load_validates_the_file(self):
        path = self._path(self._image())
        text = path.read_text(encoding="utf-8")
        self.assertEqual(loads(text, path), load(path))
        # A relative database resolves against the directory of the path given.
        self.assertEqual(loads(text, path).state.database, self.root / "state.sqlite3")
        for broken in (text + "\nenabled = ", text.replace("[image]", "[image]\nsurprise = 1")):
            with self.subTest(broken=broken[-12:]):
                with self.assertRaises(ConfigRefusal) as raised:
                    loads(broken, path)
                self.assertEqual(raised.exception.reason, "CONFIG_INVALID")
