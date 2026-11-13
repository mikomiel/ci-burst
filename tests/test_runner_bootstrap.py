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
#:
#: Version 5 is still owed: #110 prepared it for the one-job process status, and
#: that version was never published. This change joins it, because only one
#: publication may be outstanding at a time, so the operator publishes one
#: version 5 that carries both changes. If version 5 is published from the bytes
#: without this change first, the next version is 6 and these constants move with
#: it instead.
PENDING_TEMPLATE_VERSION = 5
PENDING_TEMPLATE_SHA256 = (
    "bc85ae28abfb0ddf9bbc456e5ee969115978871779b837a3756b1f6d4471928b"
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

    def test_watches_the_spot_notice_as_root_before_the_job_starts(self):
        # The job account is denied the metadata service above, so the watcher
        # runs as root and must be running before the one job it can end.
        self.assertIn("/latest/api/token", self.script)
        self.assertIn("/latest/meta-data/spot/instance-action", self.script)
        self.assertIn("spot_watch &", self.script)
        self.assertIn("SPOT_WATCH_PID=$!", self.script)
        self.assertLess(
            self._line_index("-d 169.254.169.254 -j REJECT"),
            self._line_index("spot_watch &"),
        )
        self.assertLess(self._line_index("spot_watch &"), self._line_index("one-job --wait"))

    def test_publishes_one_fixed_marker_with_closed_values_only(self):
        # The controller turns this exact line into the worker-event, so the
        # marker is one string of validated values, never free console text.
        # `unknown` is the shipped fallback for a notice without a documented
        # action and for a notice that arrives before the runner registered.
        self.assertEqual(self.script.count("bootstrap spot-interruption"), 1)
        self.assertIn(
            'echo "bootstrap spot-interruption action=$action host=$host runner=$runner"',
            self.script,
        )
        self.assertIn("terminate|stop|hibernate)", self.script)
        self.assertIn("*) action=unknown ;;", self.script)
        self.assertIn("runner=unknown", self.script)
        self.assertIn('host=$(hostname)', self.script)

    def test_reads_only_the_registration_id_and_never_the_token(self):
        # The registration file also holds the runner token, so the watcher reads
        # one field with jq and validates its shape; it never prints or copies the
        # file, and a missing file leaves the field unknown.
        body = self.script[self.script.index("spot_watch()") :]
        body = body[: body.index("\n}\n") + 3]
        self.assertIn("jq -r '.uuid // empty' /var/lib/forgejo-runner/.runner", body)
        self.assertIn("grep -Eq", body)
        # One read of the registration file, and never its content: no cat, no
        # copy, no token field.
        self.assertEqual(body.count("/var/lib/forgejo-runner/.runner"), 1)
        for forbidden in ("cat /var/lib/forgejo-runner/.runner", "TOKEN_FILE", "shred"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_ends_only_the_job_accounts_runner_process(self):
        # The watcher signals the one process it can prove belongs to the job
        # account; it never kills by name alone and never touches Forgejo.
        watcher = self.script[self.script.index("end_one_job()") :]
        self.assertIn("stat -c '%U'", watcher)
        self.assertIn("= runner ]", watcher)
        self.assertIn("grep -qa 'forgejo-runner'", watcher)
        self.assertIn('kill -TERM "$pid"', watcher)
        self.assertNotIn("kill -9", self.script)
        self.assertNotIn("forgejo-runner daemon", self.script)

    def test_a_metadata_failure_is_not_fatal_and_the_watcher_is_removed(self):
        # A worker that cannot read the notice keeps the old lifetime and job
        # timeout bounds, and the watcher must not outlive the bootstrap.
        self.assertIn('|| token=""', self.script)
        self.assertIn('|| action=""', self.script)
        self.assertIn("sleep 5", self.script)
        self.assertIn('kill "$SPOT_WATCH_PID"', self.script)
        self.assertLess(
            self._line_index('kill "$SPOT_WATCH_PID"'),
            self._line_index("bootstrap exiting rc="),
        )

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

    def _run(self, status, *, registration_file=True):
        """Run the extracted tail for *status* and return it with the trap log.

        ``registration_file=False`` puts a non-empty directory where the
        registration file lives, which makes the tail's real ``rm -f`` fail:
        the bootstrap hands that directory to the ``runner`` account, so job
        code can make the removal fail. No command is stubbed.
        """
        registration = self.work / ".runner"
        if registration_file:
            registration.write_text("registration", encoding="utf-8")
        else:
            registration.mkdir()
            (registration / "config.yml").write_text("stale", encoding="utf-8")
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
        if registration_file:
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

    def test_a_failed_registration_cleanup_cannot_replace_the_job_result(self):
        # The removal of `.runner` can fail: the bootstrap hands
        # /var/lib/forgejo-runner to the `runner` user, so job code can make
        # the path a directory. Under `set -e` that failure used to become the
        # shell's exit status: no one-job marker, no `exit "$JOB_RC"`, and the
        # trap reported rc=1 — destroying the evidence this tail preserves.
        completed, cleanup = self._run(42, registration_file=False)
        self.assertIn("cannot remove", completed.stderr, "the removal must really fail")
        self.assertIn("STEP job", completed.stdout)
        markers = [
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip().startswith("bootstrap one-job")
        ]
        self.assertEqual(markers, ["bootstrap one-job failed rc=42"])
        self.assertIn(
            "WARNING: runner registration file not removed",
            completed.stdout,
            "the failed cleanup must be reported as plain text, not as a marker",
        )
        self.assertEqual(completed.returncode, 42)
        self.assertEqual(cleanup, "cleanup", "the EXIT trap must still run")
        self.assertTrue((self.work / ".runner").is_dir())


if __name__ == "__main__":
    unittest.main()
