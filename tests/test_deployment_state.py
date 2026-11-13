"""The ExecStopPost producer writes the deployment-state document."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "deploy" / "ci-burst-state.py"
    spec = importlib.util.spec_from_file_location("ci_burst_state", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


state = _load()


def _document(**overrides):
    values = {
        "dependency": "ci-burst",
        "target": "controller",
        "deployed_ref": "a" * 40,
        "source_ref": "a" * 40,
        "deployed_version": "0.4.0",
        "artifact_id": "sha256:abc",
        "pinned": False,
        "service_result": "success",
        "last_success": 10,
        "now": 20,
        "drift": [],
    }
    values.update(overrides)
    return state.build_document(**values)


class DeploymentStateTests(unittest.TestCase):
    def test_current_carries_the_supplied_last_success(self):
        document = _document(last_success=20)
        self.assertEqual(document["result"], "current")
        self.assertFalse(document["behind"])
        self.assertEqual(document["last_success"], 20)

    def test_behind_is_unknown_not_current(self):
        document = _document(source_ref="b" * 40)
        self.assertTrue(document["behind"])
        self.assertEqual(document["result"], "unknown")

    def test_a_held_target_stays_current_and_is_pinned(self):
        document = _document(source_ref="b" * 40, pinned=True)
        self.assertTrue(document["pinned"])
        self.assertTrue(document["behind"])
        self.assertEqual(document["result"], "current")

    def test_a_failed_attempt_keeps_last_success(self):
        document = _document(service_result="exit-code", last_success=10)
        self.assertEqual(document["result"], "failed")
        self.assertEqual(document["last_success"], 10)

    def test_a_missing_artifact_fails_integrity(self):
        document = _document(artifact_id="")
        self.assertFalse(document["integrity_ok"])

    def test_a_sha_file_is_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sha"
            path.write_text("a" * 40 + "\n")
            self.assertEqual(state._sha(str(path)), "a" * 40)
            path.write_text("not-a-sha\n")
            self.assertEqual(state._sha(str(path)), "")
