"""Contract test for the operator-owned runner bootstrap.

``deploy/runner/bootstrap.sh`` is the single source for the launch template's
user data; ``docs/runner-setup.md`` references it instead of carrying a second
copy. These checks read the committed files, so they need no AWS account, no
Forgejo instance and no runner binary.

They pin the properties a later edit could silently remove: the complete
advertised label set in the runner's ``name:executor`` form, and the one-job
lifecycle that makes the ephemeral worker disposable.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "deploy" / "runner" / "bootstrap.sh"
SETUP_GUIDE = ROOT / "docs" / "runner-setup.md"


class RunnerBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.script = BOOTSTRAP.read_text(encoding="utf-8")

    def test_advertises_the_routing_label_and_the_arm64_label(self):
        match = re.search(r'^RUNNER_LABELS="([^"]*)"$', self.script, re.MULTILINE)
        self.assertIsNotNone(match, "the bootstrap must declare RUNNER_LABELS")
        labels = match.group(1).split(",")
        self.assertEqual(labels, ["cloud-ci:host", "linux-arm64:host"])
        for label in labels:
            with self.subTest(label=label):
                self.assertRegex(label, r"^[a-z0-9][a-z0-9-]*:host$")

    def test_the_configuration_example_lists_exactly_the_advertised_labels(self):
        # A deployment whose [worker].labels omits an advertised label makes a
        # job requiring that label ineligible; listing one the bootstrap does not
        # advertise makes a job look routable that can never run. The walkthrough
        # example and the bootstrap must agree.
        advertised = re.search(
            r'^RUNNER_LABELS="([^"]*)"$', self.script, re.MULTILINE
        ).group(1).split(",")
        walkthrough = (ROOT / "docs" / "deployment.md").read_text(encoding="utf-8").splitlines()
        self.assertIn("[worker]", walkthrough, "the walkthrough must show the [worker] table")
        start = walkthrough.index("[worker]")
        documented = None
        for line in walkthrough[start + 1 :]:
            if line.startswith("["):
                break
            match = re.match(r"^labels = \[([^\]]*)\]$", line)
            if match:
                documented = [item.strip().strip('"') for item in match.group(1).split(",")]
                break
        self.assertIsNotNone(documented, "the [worker] table must set labels")
        self.assertEqual(documented, advertised)

    def test_operations_records_the_decision_and_its_matching_semantics(self):
        operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
        self.assertIn("### The routing decision", operations)
        # The record must name both lists that decide routing and the subset
        # rule, because "exclusive" is a property of the label sets in use.
        self.assertIn("`[worker].labels`", operations)
        self.assertIn("subset", operations)
        # And the accepted risk must be stated without claiming that a
        # mis-routed job always fails loudly.
        self.assertIn("may appear to succeed", operations)
        self.assertIn("wrong artifact", operations)
    def test_passes_the_label_set_to_registration_unchanged(self):
        self.assertIn('--labels "${RUNNER_LABELS}"', self.script)
        flag_lines = re.findall(r"^\s*--labels\b.*$", self.script, re.MULTILINE)
        self.assertEqual(len(flag_lines), 1, flag_lines)

    def test_registers_one_ephemeral_runner_without_a_prompt(self):
        self.assertIn("/usr/local/bin/forgejo-runner register --no-interactive", self.script)
        ephemeral = re.findall(r"^\s*--ephemeral$", self.script, re.MULTILINE)
        self.assertEqual(len(ephemeral), 1, ephemeral)

    def test_runs_exactly_one_job_then_terminates(self):
        command = "runuser -u runner -- /usr/local/bin/forgejo-runner one-job"
        self.assertEqual(self.script.count(command), 1)
        self.assertNotIn("forgejo-runner daemon", self.script)
        self.assertIn("timeout --signal=TERM --kill-after=60 3600", self.script)
        self.assertIn("trap on_exit EXIT", self.script)
        self.assertIn("shutdown -h now", self.script)

    def test_setup_guide_references_the_bootstrap_instead_of_copying_it(self):
        guide = SETUP_GUIDE.read_text(encoding="utf-8")
        self.assertIn("[`deploy/runner/bootstrap.sh`](../deploy/runner/bootstrap.sh)", guide)
        self.assertNotIn("#!/bin/bash", guide)


if __name__ == "__main__":
    unittest.main()
