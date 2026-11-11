"""Contract test for the operator-owned runner bootstrap.

``deploy/runner/bootstrap.sh`` is the launch template's user data: the exact
bytes published as a template version, not a portable example. ``docs
/runner-setup.md`` references it instead of carrying a second copy. These
checks read the committed files, so they need no AWS account, no Forgejo
instance and no runner binary.

They pin the properties a later edit could silently remove: the advertised
label set in the runner's ``name:executor`` form, the one-job lifecycle that
makes the ephemeral worker disposable, the two umasks (077 for the
registration token, 0022 for the job), and the exact bytes the deployed
version carries. A change to this file is a new launch-template version and a
configuration change, never an in-place edit.
"""

import hashlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "deploy" / "runner" / "bootstrap.sh"
SETUP_GUIDE = ROOT / "docs" / "runner-setup.md"
DEPLOYMENT_DOC = ROOT / "docs" / "deployment.md"

#: sha256 of the bytes published as ``ci-burst-runner-multiaz`` version 4.
#: Update only together with a published template version and the deployment
#: TOML's ``launch_template_version``.
PUBLISHED_TEMPLATE_SHA256 = (
    "79435dab3f02405a54957aa60cbe8b3d635fc30e93dbf3ca19ca9ab53d23d219"
)


class RunnerBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.script = BOOTSTRAP.read_text(encoding="utf-8")
        self.lines = self.script.splitlines()

    def _label_argument(self):
        match = re.search(r"^\s*--labels\s+(\S+)\s*\\?$", self.script, re.MULTILINE)
        self.assertIsNotNone(match, "the bootstrap must pass --labels to register")
        return match.group(1).split(",")

    def _line_index(self, needle):
        for index, line in enumerate(self.lines):
            if needle in line:
                return index
        self.fail(f"the bootstrap must contain {needle!r}")

    def test_is_byte_identical_to_the_published_template_version(self):
        # The defect this replaces: the repository file was a placeholder
        # example while workers booted hand-patched user data, so a merged
        # bootstrap change reached no worker and nothing reported the gap.
        self.assertEqual(
            hashlib.sha256(BOOTSTRAP.read_bytes()).hexdigest(),
            PUBLISHED_TEMPLATE_SHA256,
            "publish a new launch-template version and update this hash together",
        )

    def test_advertises_the_routing_label_the_pool_label_and_the_arch_label(self):
        labels = self._label_argument()
        self.assertEqual(labels, ["cloud-ci:host", "strong-ci:host", "linux-arm64:host"])
        for label in labels:
            with self.subTest(label=label):
                self.assertRegex(label, r"^[a-z0-9][a-z0-9-]*:host$")

    def test_the_configuration_example_lists_exactly_the_advertised_labels(self):
        # A deployment whose [worker].labels omits an advertised label makes a
        # job requiring that label ineligible; listing one the bootstrap does
        # not advertise makes a job look routable that can never run. The
        # walkthrough example and the bootstrap must agree.
        advertised = self._label_argument()
        walkthrough = DEPLOYMENT_DOC.read_text(encoding="utf-8").splitlines()
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

    def test_registers_one_ephemeral_runner_without_a_prompt(self):
        self.assertEqual(self.script.count("forgejo-runner register --no-interactive"), 1)
        ephemeral = re.findall(r"^\s*--ephemeral$", self.script, re.MULTILINE)
        self.assertEqual(len(ephemeral), 1, ephemeral)

    def test_runs_exactly_one_job_and_waits_for_it(self):
        self.assertEqual(self.script.count("forgejo-runner one-job --wait"), 1)
        self.assertNotIn("forgejo-runner daemon", self.script)
        self.assertIn("timeout --signal=TERM --kill-after=60 3600", self.script)
        self.assertIn("trap on_exit EXIT", self.script)
        self.assertIn("shutdown -h now", self.script)
        self.assertIn("systemd-run --on-active=3600 --unit=runner-lifetime", self.script)

    def test_runs_the_job_with_the_fleets_umask(self):
        # The script needs 077 for the registration token, but the job must see
        # the modes the persistent fleet produces (systemd UMask=0022). Without
        # this, a mode-sensitive test passes on the fleet and fails on cloud
        # capacity, and the failure looks like a diff defect.
        lowered = [line for line in self.lines if line.strip() == "umask 0022"]
        self.assertEqual(len(lowered), 1, lowered)
        self.assertIn("umask 077", self.script)
        position = self._line_index("umask 0022")
        self.assertGreater(position, self._line_index("umask 077"))
        self.assertLess(position, self._line_index("one-job --wait"))
        self.assertGreater(position, self._line_index("forgejo-runner register"))

    def test_reads_the_registration_token_from_ssm_and_removes_it(self):
        self.assertIn("PARAM=/ci-burst/runner/registration-token", self.script)
        self.assertIn("aws ssm get-parameter", self.script)
        self.assertIn("--with-decryption", self.script)
        self.assertIn('chmod 600 "$TOKEN_FILE"', self.script)
        calls = re.findall(r"^\s*cleanup_token$", self.script, re.MULTILINE)
        self.assertEqual(len(calls), 2, calls)
        self.assertIn('shred -u "$TOKEN_FILE"', self.script)

    def test_blocks_the_metadata_service_for_the_job_account(self):
        for address in ("169.254.169.254", "169.254.170.2"):
            with self.subTest(address=address):
                self.assertIn(f"--uid-owner runner -d {address} -j REJECT", self.script)

    def test_installs_and_downloads_nothing(self):
        # The worker boots a baked image. An apt call or a download would make
        # the launch depend on network availability and re-open the placeholder
        # problem this file now closes.
        for forbidden in (
            "apt-get",
            "curl -fsSL",
            "<sha256",
            "example.com",
            "RUNNER_SHA",
            "RUNNER_LABELS",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.script)
        self.assertIn("/usr/local/bin/forgejo-runner", self.script)

    def test_setup_guide_references_the_bootstrap_instead_of_copying_it(self):
        guide = SETUP_GUIDE.read_text(encoding="utf-8")
        self.assertIn("[`deploy/runner/bootstrap.sh`](../deploy/runner/bootstrap.sh)", guide)
        self.assertNotIn("#!/bin/bash", guide)


if __name__ == "__main__":
    unittest.main()
