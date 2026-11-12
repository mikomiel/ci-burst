"""Contract test for the operator-owned runner bootstrap.

``deploy/runner/bootstrap.sh`` is the launch template's user data: the exact
bytes published as a template version, not a portable example. ``docs
/runner-setup.md`` references it instead of carrying a second copy. These
checks read the committed files, so they need no AWS account, no Forgejo
instance and no runner binary.

They pin the properties a later edit could silently remove: the advertised
label set in the runner's ``name:executor`` form, the one-job lifecycle that
makes the ephemeral worker disposable, the two umasks (077 for the
registration token, 0022 for the job), the process status preserved through
cleanup, and the exact bytes of the declared publication — the pending version
while one is owed, otherwise the published one. A change to this file is a new
launch-template version and a configuration change, never an in-place edit.
"""

import hashlib
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "deploy" / "runner" / "bootstrap.sh"
SETUP_GUIDE = ROOT / "docs" / "runner-setup.md"
DEPLOYMENT_DOC = ROOT / "docs" / "deployment.md"

#: sha256 of the bytes published as ``ci-burst-runner-multiaz`` version 4.
#: Update only together with a published template version and the deployment
#: TOML's ``launch_template_version``.
PUBLISHED_TEMPLATE_VERSION = 4
PUBLISHED_TEMPLATE_SHA256 = (
    "79435dab3f02405a54957aa60cbe8b3d635fc30e93dbf3ca19ca9ab53d23d219"
)

#: The publication this change prepares but does not perform. While a pending
#: publication exists the repository bytes must equal it, so the pin is not
#: weakened. Publication is the operator step: publish the next launch-template
#: version, update the deployment TOML's ``launch_template_version``, then flip
#: these constants in a follow-up change (published becomes the pending pair and
#: both PENDING values become ``None``).
PENDING_TEMPLATE_VERSION = 5
PENDING_TEMPLATE_SHA256 = (
    "735fcfe896d52c31e89f53ab059ab301d7ef6b4096fec3e6c24b22dda3ab52fe"
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

    def _expected_publication(self):
        """Return ``(version, sha256)`` for the state the file must be in.

        Exactly one publication is authoritative: the pending pair while a
        pending publication exists, and the published pair otherwise. Half a
        state, a pending version that is not the next one, or two equal digests
        are contradictory and fail rather than silently widen acceptance.
        """
        pending_version = PENDING_TEMPLATE_VERSION
        pending_sha256 = PENDING_TEMPLATE_SHA256
        if (pending_version is None) != (pending_sha256 is None):
            self.fail(
                "incomplete pending publication state: set PENDING_TEMPLATE_VERSION "
                "and PENDING_TEMPLATE_SHA256 together, or neither"
            )
        if pending_version is not None:
            if not isinstance(PUBLISHED_TEMPLATE_VERSION, int) or PUBLISHED_TEMPLATE_SHA256 is None:
                self.fail("a pending publication requires the published version it follows")
            if pending_version != PUBLISHED_TEMPLATE_VERSION + 1:
                self.fail(
                    "the pending publication must be the next version after "
                    f"{PUBLISHED_TEMPLATE_VERSION}, not {pending_version}"
                )
            if pending_sha256 == PUBLISHED_TEMPLATE_SHA256:
                self.fail(
                    "contradictory publication state: the pending digest equals the "
                    "published digest, so no publication is owed"
                )
            return pending_version, pending_sha256
        if not isinstance(PUBLISHED_TEMPLATE_VERSION, int) or PUBLISHED_TEMPLATE_SHA256 is None:
            self.fail(
                "no publication state: set PUBLISHED_TEMPLATE_VERSION and "
                "PUBLISHED_TEMPLATE_SHA256, or a pending pair"
            )
        return PUBLISHED_TEMPLATE_VERSION, PUBLISHED_TEMPLATE_SHA256

    def test_bootstrap_bytes_match_the_declared_publication_state(self):
        # The defect this replaces: the repository file was a placeholder
        # example while workers booted hand-patched user data, so a merged
        # bootstrap change reached no worker and nothing reported the gap.
        version, digest = self._expected_publication()
        self.assertEqual(
            hashlib.sha256(BOOTSTRAP.read_bytes()).hexdigest(),
            digest,
            f"the committed bootstrap must be the bytes of launch-template version {version}. "
            "Publish the next launch-template version, update the deployment TOML's "
            "launch_template_version, then flip PUBLISHED_TEMPLATE_VERSION/SHA256 and "
            "clear the PENDING constants in a follow-up change",
        )

    def test_publication_state_refuses_absent_and_contradictory_states(self):
        states = {
            "half a pending pair": {"PENDING_TEMPLATE_VERSION": None},
            "pending version is not next": {"PENDING_TEMPLATE_VERSION": 6},
            "pending digest equals published": {"PENDING_TEMPLATE_SHA256": PUBLISHED_TEMPLATE_SHA256},
            "no publication state": {
                "PENDING_TEMPLATE_VERSION": None,
                "PENDING_TEMPLATE_SHA256": None,
                "PUBLISHED_TEMPLATE_SHA256": None,
            },
        }
        for name, values in states.items():
            with self.subTest(state=name):
                with mock.patch.multiple(__name__, **values):
                    with self.assertRaises(AssertionError):
                        self._expected_publication()

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

    def test_preserves_the_one_job_process_status_instead_of_discarding_it(self):
        # `|| true` made the EXIT trap report rc=0 for a failed or timed-out
        # runner process, so the console evidence contradicted the lifecycle.
        tail = self.script[self.script.index("JOB_RC=0") :]
        self.assertNotIn("|| true", tail)
        self.assertNotIn("|| true", self.script[self.script.index("one-job --wait") :])
        self.assertIn("JOB_RC=0", self.script)
        self.assertIn("|| JOB_RC=$?", self.script)
        self.assertEqual(self.script.count("JOB_RC=0"), 1)
        # The one-job invocation is protected by the capture, so `set -e` does
        # not abort before the marker and cleanup below it.
        invocation = self._line_index("one-job --wait")
        self.assertLess(self._line_index("JOB_RC=0"), invocation)
        self.assertEqual(self._line_index("|| JOB_RC=$?"), invocation)
        for marker in (
            '"bootstrap one-job completed rc=0"',
            '"bootstrap one-job failed rc=$JOB_RC"',
            '"bootstrap one-job timeout rc=$JOB_RC"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.script)
        self.assertIn('case "$JOB_RC" in', self.script)
        # Cleanup and the shutdown trap stay unconditional on every path.
        for needle in (
            "trap on_exit EXIT",
            "cleanup_token",
            "sync",
            "shutdown -h now",
            'rm -f /var/lib/forgejo-runner/.runner',
            'exit "$JOB_RC"',
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.script)
        self.assertEqual(self.lines[-1], 'exit "$JOB_RC"')
        self.assertLess(self._line_index("trap on_exit EXIT"), self._line_index("one-job --wait"))
        self.assertLess(
            tail.index('rm -f /var/lib/forgejo-runner/.runner'),
            tail.index('case "$JOB_RC" in'),
        )
        self.assertLess(tail.index("one-job --wait"), tail.index("exit \"$JOB_RC\""))

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


class BootstrapTailTests(unittest.TestCase):
    """Run only the extracted tail, never the real bootstrap.

    The real file reads SSM, writes iptables rules and shuts the host down.
    These cases extract the tail from ``JOB_RC=0`` onward, point the runner
    directory at a temporary path and stub ``timeout``, so each one-job status
    is produced without starting a runner. A synthetic EXIT trap stands in for
    the real cleanup and proves the tail's exit still reaches it.
    """

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.work = self.root / "runner"
        self.work.mkdir()

    def _tail(self):
        lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
        start = lines.index("JOB_RC=0")
        tail = "\n".join(lines[start:])
        self.assertIn("/var/lib/forgejo-runner/.runner", tail)
        return tail.replace("/var/lib/forgejo-runner", str(self.work))

    def _run(self, status):
        (self.work / ".runner").write_text("registration", encoding="utf-8")
        cleanup = self.root / f"cleanup-{status}.log"
        script = self.root / f"tail-{status}.sh"
        script.write_text(
            "\n".join(
                (
                    "#!/bin/bash",
                    "set -euo pipefail",
                    f"STUB_RC={status}",
                    'timeout() { return "$STUB_RC"; }',
                    f"trap {shlex.quote('printf cleanup >> ' + shlex.quote(str(cleanup)))} EXIT",
                    self._tail(),
                    "",
                )
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(["/bin/bash", str(script)], capture_output=True, text=True, check=False)
        self.assertEqual(completed.stderr, "")
        return completed, cleanup.read_text(encoding="utf-8") if cleanup.exists() else ""

    def test_each_process_status_reaches_the_marker_and_the_exit_status(self):
        cases = {
            0: "bootstrap one-job completed rc=0",
            42: "bootstrap one-job failed rc=42",
            124: "bootstrap one-job timeout rc=124",
            137: "bootstrap one-job timeout rc=137",
        }
        for status, marker in cases.items():
            with self.subTest(status=status):
                completed, cleanup = self._run(status)
                markers = [
                    line.strip()
                    for line in completed.stdout.splitlines()
                    if line.strip().startswith("bootstrap one-job")
                ]
                self.assertEqual(markers, [marker])
                self.assertIn("STEP job", completed.stdout)
                self.assertEqual(completed.returncode, status)
                self.assertEqual(cleanup, "cleanup")
                self.assertFalse(
                    (self.work / ".runner").exists(),
                    "the registration file must be removed before the marker",
                )


if __name__ == "__main__":
    unittest.main()
