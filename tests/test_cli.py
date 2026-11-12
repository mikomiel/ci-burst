import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from ci_burst.cli import main
from ci_burst.controller import Controller

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

    def test_unknown_command_is_a_usage_error(self):
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main(["unknown"])
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
