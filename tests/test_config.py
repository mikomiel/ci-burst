import tempfile
import unittest
from pathlib import Path

from ci_burst.config import load
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
        for value in (30, 840):
            with self.subTest(value=value):
                config = load(
                    write_config(
                        self.root, config={"controller": {"pass_deadline_seconds": value}}
                    )
                )
                self.assertEqual(config.controller.pass_deadline_seconds, value)

    def test_refuses_a_pass_deadline_outside_the_range(self):
        for value in (29, 841):
            with self.subTest(value=value):
                path = write_config(
                    self.root, config={"controller": {"pass_deadline_seconds": value}}
                )
                with self.assertRaises(ConfigRefusal) as raised:
                    load(path)
                self.assertEqual(raised.exception.reason, "CONFIG_INVALID")
                self.assertEqual(
                    raised.exception.detail,
                    "controller.pass_deadline_seconds: must be between 30 and 840",
                )

    def test_refuses_a_non_integer_pass_deadline(self):
        path = write_config(
            self.root, config={"controller": {"pass_deadline_seconds": 240.0}}
        )
        with self.assertRaises(ConfigRefusal) as raised:
            load(path)
        self.assertIn("controller.pass_deadline_seconds", raised.exception.detail)

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
