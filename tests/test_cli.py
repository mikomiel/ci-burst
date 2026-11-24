import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from ci_burst.cli import build_parser, main
from ci_burst.controller import Controller
from ci_burst.errors import ConfigRefusal, ImageRefusal, ProviderRefusal, StateRefusal

from support import FakeAws, instance, write_config


class CliTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)

    def run_cli(self, arguments):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(arguments)
        return code, json.loads(buffer.getvalue())

    def test_plan_without_a_token_refuses_and_exits_two(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            code, payload = self.run_cli(["plan", "--config", str(write_config(self.root))])
        self.assertEqual(code, 2)
        self.assertEqual(payload["decision"], "refused")
        self.assertEqual(payload["refusal"]["reason"], "FORGEJO_TOKEN_MISSING")

    def test_apply_requires_an_operator_qualified_contract(self):
        config = write_config(self.root, config={"forgejo": {"qualification": "fixtures-only"}})
        code, payload = self.run_cli(["apply", "--config", str(config)])
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["reason"], "FORGEJO_CONTRACT_UNQUALIFIED")
        self.assertFalse((self.root / "state.sqlite3").exists())

    def test_apply_refuses_another_worker_lifetime_before_the_ledger_and_reap_still_runs(self):
        config = write_config(self.root, config={"aws": {"max_lifetime_seconds": 1800}})
        built = []

        def controller(configuration):
            built.append(configuration)
            return Controller(configuration, aws=FakeAws())

        with mock.patch("ci_burst.cli.Controller", controller):
            code, payload = self.run_cli(["apply", "--config", str(config)])
            self.assertEqual(code, 2)
            self.assertEqual(payload["error"]["reason"], "WORKER_LIFETIME_MISMATCH")
            self.assertFalse((self.root / "state.sqlite3").exists(), "no ledger was opened")
            code, payload = self.run_cli(["reap", "--config", str(config)])
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["decision"], "planned")

    def test_exit_codes_follow_the_decision(self):
        cases = [
            ({"decision": "planned"}, 0),
            ({"decision": "launched"}, 0),
            ({"decision": "refused"}, 2),
            ({"decision": "launch_failed"}, 1),
            ({"decision": "uncertain"}, 1),
            ({"decision": "cleanup_failed"}, 1),
            ({"decision": "cleanup_degraded"}, 1),
        ]
        for result, expected in cases:
            with self.subTest(decision=result["decision"]):
                klass = type("StubController", (), {"__init__": lambda self, config: None, "reap": lambda self, apply=False: result})
                with mock.patch("ci_burst.cli.Controller", klass):
                    code, payload = self.run_cli(["reap", "--config", str(write_config(self.root))])
                self.assertEqual(code, expected)
                self.assertEqual(payload["decision"], result["decision"])

    def test_reap_apply_all_refused_exits_one_without_a_mutation(self):
        # End to end through the real Controller and the CLI boundary: the
        # scheduled unit observes the exit status, so a refused inventory must
        # not look like a completed cleanup.
        aws = FakeAws(instances=[instance(instance_id="i-bad-expiry", operation="a")])
        config = write_config(self.root)

        def controller(configuration):
            return Controller(configuration, aws=aws)

        with mock.patch("ci_burst.cli.Controller", controller):
            code, payload = self.run_cli(["reap", "--config", str(config), "--apply"])
        self.assertEqual(code, 1)
        self.assertEqual(payload["decision"], "cleanup_degraded")
        self.assertEqual(payload["termination_requested"], [])
        self.assertNotIn("confirmation", payload)
        self.assertEqual(aws.mutations(), [])
        self.assertFalse((self.root / "state.sqlite3").exists())

    def test_apply_without_the_aws_sdk_reports_the_documented_refusal(self):
        missing = {"boto3": None, "botocore": None, "botocore.config": None}
        with mock.patch.dict(sys.modules, missing):
            code, payload = self.run_cli(["apply", "--config", str(write_config(self.root))])
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["reason"], "AWS_SDK_UNAVAILABLE")

    def test_unknown_command_is_a_usage_error(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["unknown"])
        self.assertEqual(raised.exception.code, 2)

    # -- the image group ---------------------------------------------------------------
    def test_the_image_group_has_four_commands_with_the_documented_options(self):
        parse = build_parser().parse_args
        run = parse(["image", "run", "--config", "c.toml", "--apply", "--until-done", "--force"])
        self.assertEqual((run.command, run.image_command), ("image", "run"))
        self.assertEqual((run.apply, run.until_done, run.force), (True, True, True))
        bare = parse(["image", "run", "--config", "c.toml"])
        self.assertEqual((bare.apply, bare.until_done, bare.force), (False, False, False))
        self.assertEqual(parse(["image", "plan", "--config", "c.toml"]).image_command, "plan")
        refresh = parse(["image", "status", "--config", "c.toml", "--refresh"])
        self.assertEqual((refresh.image_command, refresh.refresh), ("status", True))
        self.assertFalse(parse(["image", "status", "--config", "c.toml"]).refresh)
        promote = parse(["image", "promote", "--config", "c.toml", "--version", "5", "--write"])
        self.assertEqual(
            (promote.image_command, promote.version, promote.rollback, promote.write), ("promote", 5, False, True)
        )
        rollback = parse(["image", "promote", "--config", "c.toml", "--rollback"])
        self.assertEqual((rollback.version, rollback.rollback, rollback.write), (None, True, False))

    def test_every_image_command_needs_a_configuration_and_the_group_needs_a_command(self):
        for arguments in (["image"], ["image", "plan"], ["image", "run"], ["image", "status"], ["image", "promote"]):
            with self.subTest(arguments=arguments):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
                    main(arguments)
                self.assertEqual(raised.exception.code, 2)

    def test_the_image_group_maps_a_decision_to_an_exit_code(self):
        config = str(write_config(self.root))
        cases = [
            ({"decision": "disabled"}, 0),
            ({"decision": "planned"}, 0),
            ({"decision": "digest_unchanged"}, 0),
            ({"decision": "building"}, 0),
            ({"decision": "candidate"}, 0),
            ({"decision": "refused"}, 2),
            ({"decision": "failed"}, 1),
        ]
        for result, expected in cases:
            with self.subTest(decision=result["decision"]):
                with mock.patch("ci_burst.cli._image", return_value=result):
                    code, payload = self.run_cli(["image", "run", "--config", config])
                self.assertEqual((code, payload), (expected, result))

    def test_a_refusal_exits_two_and_a_provider_refusal_of_the_image_group_exits_one(self):
        config = str(write_config(self.root))
        cases = [
            (ImageRefusal("IMAGE_EXAMPLE", "example detail"), 2),
            (ConfigRefusal("CONFIG_EXAMPLE"), 2),
            (StateRefusal("STATE_EXAMPLE"), 2),
            (ProviderRefusal("AWS_EXAMPLE", "RequestLimitExceeded", uncertain=True), 1),
        ]
        for refusal, expected in cases:
            with self.subTest(reason=refusal.reason):
                with mock.patch("ci_burst.cli._image", side_effect=refusal):
                    code, payload = self.run_cli(["image", "status", "--config", config])
                self.assertEqual((code, payload), (expected, {"error": refusal.as_dict()}))

    def test_a_provider_refusal_outside_the_image_group_is_still_a_refusal(self):
        def refuse(self):
            raise ProviderRefusal("AWS_EXAMPLE", "RequestLimitExceeded", uncertain=True)

        stub = type("StubController", (), {"__init__": lambda self, config: None, "status": refuse})
        with mock.patch("ci_burst.cli.Controller", stub):
            code, payload = self.run_cli(["status", "--config", str(write_config(self.root))])
        self.assertEqual((code, payload["error"]["reason"]), (2, "AWS_EXAMPLE"))

    def test_an_unexpected_error_in_an_image_command_is_reported_by_its_type_alone(self):
        error = RuntimeError("example-detail-marker")
        with mock.patch("ci_burst.cli._image", side_effect=error):
            code, payload = self.run_cli(["image", "plan", "--config", str(write_config(self.root))])
        self.assertEqual((code, payload), (1, {"error": {"reason": "UNEXPECTED", "detail": "RuntimeError"}}))


if __name__ == "__main__":
    unittest.main()
