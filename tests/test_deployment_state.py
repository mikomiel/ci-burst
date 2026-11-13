"""The ExecStopPost producer writes the deployment-state document."""

from __future__ import annotations

import importlib.util
import inspect
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "deploy" / "ci-burst-state.py"
    spec = importlib.util.spec_from_file_location("ci_burst_state", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state = _load()


def _document(**overrides):
    values = {
        "dependency": "ci-burst",
        "target": "controller",
        "deployed_ref": "a" * 40,
        "source_ref": "a" * 40,
        "source_checked_at": 15,
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

    def test_an_unknown_serving_revision_fails_integrity(self):
        document = _document(deployed_ref="")
        self.assertFalse(document["integrity_ok"])
        self.assertEqual(document["result"], "unknown")

    def test_a_missing_artifact_fails_integrity(self):
        document = _document(artifact_id="")
        self.assertFalse(document["integrity_ok"])

    def test_the_default_target_is_never_a_hostname(self):
        self.assertEqual(state.DEFAULT_TARGET, "controller")
        self.assertNotIn("gethostname", inspect.getsource(state))

    def test_last_success_advances_only_for_current_or_pinned(self):
        self.assertEqual(
            state.choose_last_success(
                success=True, pinned=False, deployed_ref="a", source_ref="a",
                now=20, previous=10), 20)
        self.assertEqual(
            state.choose_last_success(
                success=True, pinned=True, deployed_ref="a", source_ref="b",
                now=20, previous=10), 20)
        self.assertEqual(
            state.choose_last_success(
                success=True, pinned=False, deployed_ref="a", source_ref="b",
                now=20, previous=10), 10)
        self.assertEqual(
            state.choose_last_success(
                success=False, pinned=True, deployed_ref="a", source_ref="b",
                now=20, previous=10), 10)

    def test_a_sha_file_is_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sha"
            path.write_text("a" * 40 + "\n")
            self.assertEqual(state._sha(str(path)), "a" * 40)
            path.write_text("not-a-sha\n")
            self.assertEqual(state._sha(str(path)), "")

    def test_a_missing_observation_marker_is_not_inferred_from_fetch_head(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            (root / ".git" / "FETCH_HEAD").write_text("b" * 40 + "\n")
            self.assertEqual(state.read_observation(directory), ("", 0))
            (root / "source-sha").write_text("a" * 40 + "\n")
            self.assertEqual(state.read_observation(directory)[0], "a" * 40)

    def test_a_failed_git_show_is_visible_as_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            drift = state._drift(directory, "a" * 40, ["ci-burst-apply.service"], directory)
        self.assertEqual(drift, [{
            "surface": "units",
            "detail": "ci-burst-apply.service: source unavailable",
        }])

    def test_a_missing_installed_unit_is_visible_as_drift(self):
        # The source is a real file in this temporary git repository, so the
        # installed-side miss is the only reason for the finding.
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "deploy").mkdir()
            (source / "deploy" / "ci-burst-apply.service").write_text("unit\n")
            subprocess.run(["git", "-C", str(source), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(source), "-c", "user.name=t", "-c", "user.email=t@t",
                 "commit", "-q", "-m", "unit"], check=True)
            revision = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True).stdout.strip()
            drift = state._drift(str(source), revision, ["ci-burst-apply.service"], str(root / "units"))
        self.assertEqual(drift, [{
            "surface": "units",
            "detail": "ci-burst-apply.service is missing",
        }])
