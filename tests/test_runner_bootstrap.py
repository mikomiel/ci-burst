"""Contract test for the operator-owned runner bootstrap.

``deploy/runner/bootstrap.sh`` is the launch template's user data: the exact
bytes published as a template version, not a portable example. ``docs
/runner-setup.md`` references it instead of carrying a second copy. These
checks read the committed files, so they need no AWS account, no Forgejo
instance and no runner binary.

They pin the properties a later edit could silently remove: the advertised
label set in the runner's ``name:executor`` form, the one-job lifecycle that
makes the ephemeral worker disposable, the bounded claim window that ends an
idle worker, the two umasks (077 for the registration token, 0022 for the job),
the process status preserved through cleanup, and the exact bytes of the
declared publication - the pending version while one is owed, otherwise the
published one. A change to this file is a new launch-template version and a
configuration change, never an in-place edit.
"""

import hashlib
import os
import re
import select
import shlex
import shutil
import subprocess
import tempfile
import time
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
#: that version was never published. Each later bootstrap change joins it,
#: because only one publication may be outstanding at a time, so the operator
#: publishes one version 5 that carries all of them. The bounded claim window is
#: one of them. Enabling version 5 for any of them enables the claim window too,
#: so qualify it live (runner-setup.md, "The claim window") before a deployment
#: sets version 5. If version 5 is published from earlier bytes first, the next
#: version is 6 and these constants move with it instead.
PENDING_TEMPLATE_VERSION = 5
PENDING_TEMPLATE_SHA256 = (
    "3474b96723841ebb31ef46b600fa67f44b7db941d7676857cca520b305d28769"
)


def _end_one_job_snippet():
    """The shipped signal helper, for an execution test."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.startswith("end_one_job() {")
    )
    end = next(index for index, line in enumerate(lines) if index > start and line == "}")
    return "\n".join(lines[start : end + 1])


def _capture_snippet():
    """The shipped registration-id block, for an execution test."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.startswith("RUNNER_FILE=/var/lib/forgejo-runner/.runner")
    )
    fallback = next(
        index
        for index, line in enumerate(lines)
        if index > start and line.strip() == "RUNNER_UUID=unknown"
    )
    end = next(
        index for index, line in enumerate(lines) if index > fallback and line.strip() == "fi"
    )
    return "\n".join(lines[start : end + 1])


#: The poll invocation as the bootstrap writes it. A comment in the script must
#: not repeat this text, so a line search finds the command and nothing else.
ONE_JOB_INVOCATION = "/usr/local/bin/forgejo-runner one-job -c /etc/forgejo-runner/config.yml"


def _function_snippet(name):
    """A shipped shell function, for an execution test."""
    lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.startswith(f"{name}() {{")
    )
    end = next(index for index, line in enumerate(lines) if index > start and line == "}")
    return "\n".join(lines[start : end + 1])


def _literal_line(name):
    """The one published assignment line of a literal such as ``NO_TASK_STATUS``."""
    found = [
        line
        for line in BOOTSTRAP.read_text(encoding="utf-8").splitlines()
        if line.startswith(f"{name}=")
    ]
    if len(found) != 1:
        raise AssertionError(f"the bootstrap must assign {name} exactly once, found {found!r}")
    return found[0]


def _literal_value(name):
    """The unquoted value of a published literal."""
    return shlex.split(_literal_line(name))[0].partition("=")[2]


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

    def test_polls_the_queue_instead_of_blocking_in_the_runner_wait_mode(self):
        # The runner's wait option blocks until a task arrives, so an idle worker
        # would sit there for its whole lifetime. One poll per pass of the claim
        # loop returns at once when no task is queued.
        self.assertEqual(self.script.count(ONE_JOB_INVOCATION), 1)
        self.assertEqual(self.script.count("forgejo-runner one-job"), 1)
        self.assertNotIn("--wait", self.script)
        self.assertNotIn("forgejo-runner daemon", self.script)
        self.assertEqual(self.script.count("run_poll 9>&- 2>&1"), 1)

    def test_keeps_the_job_timeout_and_the_lifetime_literals(self):
        # The claim window ends an idle worker. It does not move the bounds of a
        # job that a poll receives, so both literals stay exactly as they were.
        self.assertEqual(self.script.count("timeout --signal=TERM --kill-after=60 3600"), 1)
        self.assertEqual(
            self.script.count(
                "systemd-run --on-active=3600 --unit=runner-lifetime /sbin/shutdown -h now"
            ),
            1,
        )
        self.assertIn("trap on_exit EXIT", self.script)
        self.assertIn("shutdown -h now", self.script)
        # The job timeout covers the poll itself, not the pause between polls.
        poll = _function_snippet("run_poll")
        self.assertIn("timeout --signal=TERM --kill-after=60 3600", poll)
        self.assertIn(ONE_JOB_INVOCATION, poll)

    def test_the_claim_window_and_the_no_task_signature_are_published_literals(self):
        # Single literals beside the lifetime literal: not configuration, not a
        # tag and not controller input, so no parameter default and no second
        # assignment can move them.
        literals = {
            "CLAIM_WINDOW_SECONDS": "300",
            "POLL_INTERVAL_SECONDS": "10",
            "NO_TASK_STATUS": "2",
            "NO_TASK_MESSAGE": "'Error: no task received'",
        }
        lifetime = self._line_index("systemd-run --on-active=3600")
        remount = self._line_index("mount -o remount")
        for name, value in literals.items():
            with self.subTest(literal=name):
                assignments = [
                    index for index, line in enumerate(self.lines) if line.startswith(f"{name}=")
                ]
                self.assertEqual(len(assignments), 1, assignments)
                self.assertEqual(self.lines[assignments[0]], f"{name}={value}")
                self.assertLess(lifetime, assignments[0])
                self.assertLess(assignments[0], remount)
                self.assertNotIn("${" + name, self.script)
        # The capture bound is a literal too, large enough for an empty poll's whole
        # output and small enough to keep in a shell variable and a tmpfs file.
        self.assertRegex(_literal_line("CAPTURE_LIMIT_BYTES"), r"^CAPTURE_LIMIT_BYTES=[1-9][0-9]*$")
        self.assertGreaterEqual(int(_literal_value("CAPTURE_LIMIT_BYTES")), 4096)
        self.assertLessEqual(int(_literal_value("CAPTURE_LIMIT_BYTES")), 1024 * 1024)

    def test_the_documented_no_task_status_is_from_2_to_123(self):
        # The runner of a claimed job can also end with 0, 1, 124 or a status of
        # 128 or more, so a no-task status in those ranges would let a job end
        # the loop by its status alone. The check is the guard in the script too.
        status = int(_literal_value("NO_TASK_STATUS"))
        self.assertGreaterEqual(status, 2)
        self.assertLessEqual(status, 123)
        guard = _function_snippet("poll_is_empty")
        self.assertIn('[ "$status" -ge 2 ] && [ "$status" -le 123 ]', guard)
        self.assertIn('[ "$status" = "$NO_TASK_STATUS" ]', guard)

    def test_the_claim_window_starts_when_register_returns(self):
        register = self._line_index("forgejo-runner register --no-interactive")
        ephemeral = next(
            index
            for index, line in enumerate(self.lines)
            if index > register and line.strip() == "--ephemeral"
        )
        deadline = self._line_index("CLAIM_DEADLINE=")
        self.assertEqual(self.script.count("CLAIM_DEADLINE="), 1)
        self.assertEqual(deadline, ephemeral + 1)
        self.assertEqual(
            self.lines[deadline], "CLAIM_DEADLINE=$(( $(date +%s) + CLAIM_WINDOW_SECONDS ))"
        )
        self.assertLess(deadline, self._line_index('echo "STEP register"'))

    def test_the_capture_is_a_bounded_private_root_file_outside_the_runner_directory(self):
        creation = [line for line in self.lines if "mktemp /run/runner-capture." in line]
        self.assertEqual(
            creation, ['CAPTURE=$(mktemp /run/runner-capture.XXXXXX) || CAPTURE=""']
        )
        # Created before the first poll starts, by the bootstrap itself (root),
        # never by the job account and never under its directory.
        self.assertLess(
            self._line_index("mktemp /run/runner-capture."), self._line_index("run_poll 9>&- 2>&1")
        )
        self.assertNotIn("/var/lib/forgejo-runner/runner-capture", self.script)
        # A second, bounded copy: tee keeps draining after head reaches the bound,
        # so the runner is neither stopped nor blocked by the capture.
        self.assertIn("| tee -pa /dev/fd/9", self.script)
        self.assertIn('| head -c "$((CAPTURE_LIMIT_BYTES + 1))" > "${CAPTURE:-/dev/null}"', self.script)
        # Removed with the other cleanup, before the shutdown.
        on_exit = _function_snippet("on_exit")
        self.assertLess(on_exit.index("cleanup_token"), on_exit.index("cleanup_capture"))
        self.assertLess(on_exit.index("cleanup_capture"), on_exit.index("shutdown -h now"))
        self.assertLess(self._line_index('CAPTURE=""'), self._line_index("trap on_exit EXIT"))

    def test_the_claim_timeout_marker_is_printed_by_one_path_only(self):
        self.assertEqual(self.script.count("STEP claim-timeout"), 1)
        self.assertEqual(self.script.count('echo "STEP claim-timeout"'), 1)
        self.assertEqual(self.script.count('echo "STEP job"'), 1)
        body = _function_snippet("claim_timeout")
        self.assertIn('echo "STEP claim-timeout"', body)
        # No job attempt happened, so neither the job step nor a one-job marker.
        self.assertNotIn("STEP job", body)
        self.assertNotIn("bootstrap one-job", body)
        # The guarded removal, then a plain exit 0 through the EXIT trap. The
        # function never returns, so the code after the loop cannot be reached.
        self.assertLess(body.index('echo "STEP claim-timeout"'), body.index("remove_registration"))
        self.assertEqual(body.splitlines()[-2].strip(), "exit 0")
        # The job path reaches its marker only after the loop has ended.
        loop_end = max(index for index, line in enumerate(self.lines) if line == "done")
        self.assertLess(loop_end, self._line_index('echo "STEP job"'))

    def test_the_registration_file_is_removed_by_one_guarded_command(self):
        # The runner account owns that directory, so job code can make the removal
        # fail. Both endings share the guard, and neither lets it set the status.
        removal = _function_snippet("remove_registration")
        self.assertIn(
            'rm -f /var/lib/forgejo-runner/.runner || echo "WARNING: runner registration file not removed"',
            removal,
        )
        self.assertEqual(self.script.count("remove_registration\n"), 2)

    def test_preserves_the_one_job_process_status_instead_of_discarding_it(self):
        # `|| true` made the EXIT trap report rc=0 for a failed or timed-out
        # runner process, so the console evidence contradicted the lifecycle.
        tail = self.script[self.script.index("JOB_RC=0") :]
        self.assertNotIn("|| true", tail)
        self.assertNotIn("|| true", self.script[self.script.index(ONE_JOB_INVOCATION) :])
        self.assertIn("JOB_RC=0", self.script)
        self.assertEqual(self.script.count("JOB_RC=0"), 1)
        # The poll's own pipeline slot supplies the status, so `set -e` does not
        # abort before the marker and cleanup below it, and a stage that only
        # copies output cannot replace the runner's result.
        invocation = self._line_index(ONE_JOB_INVOCATION)
        poll = self._line_index("run_poll 9>&- 2>&1")
        capture_status = self._line_index('|| POLL_STATUSES=("${PIPESTATUS[@]}")')
        job_status = self._line_index("JOB_RC=${POLL_STATUSES[0]:-1}")
        self.assertLess(self._line_index("JOB_RC=0"), invocation)
        self.assertLess(invocation, poll)
        self.assertLess(poll, capture_status)
        self.assertLess(capture_status, job_status)
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
        self.assertLess(self._line_index("trap on_exit EXIT"), self._line_index(ONE_JOB_INVOCATION))
        self.assertLess(
            tail.index('rm -f /var/lib/forgejo-runner/.runner'),
            tail.index('case "$JOB_RC" in'),
        )
        self.assertLess(tail.index(ONE_JOB_INVOCATION), tail.index("exit \"$JOB_RC\""))

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
        self.assertLess(position, self._line_index(ONE_JOB_INVOCATION))
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
        self.assertLess(self._line_index("spot_watch &"), self._line_index(ONE_JOB_INVOCATION))

    def test_publishes_one_fixed_marker_with_closed_values_only(self):
        # The controller turns this exact line into the worker-event, so the
        # marker is one string of validated values, never free console text.
        # `unknown` is the shipped fallback for a notice without a documented
        # action and for a registration id that was not captured.
        self.assertEqual(self.script.count("bootstrap spot-interruption"), 1)
        self.assertIn(
            'echo "bootstrap spot-interruption action=$action host=$host runner=$RUNNER_UUID"',
            self.script,
        )
        self.assertIn("terminate|stop|hibernate)", self.script)
        self.assertIn("*) action=unknown ;;", self.script)
        self.assertIn('host=$(hostname)', self.script)

    def test_captures_the_registration_id_before_the_job_account_owns_the_file(self):
        # The registration file also holds the token, and the job account can
        # replace anything in its directory. Reading the id at notice time would
        # let job code block the watcher with a FIFO, so the id is captured while
        # the file is still root-owned, bounded, and shape-checked as a whole
        # value rather than line by line.
        capture = self._line_index("RUNNER_FILE=/var/lib/forgejo-runner/.runner")
        self.assertLess(capture, self._line_index("chown -R runner:runner"))
        self.assertIn("jq -ser", self.script)
        # One JSON document, its raw string checked inside jq before command
        # substitution can strip a trailing newline or fold a second document,
        # and a file over the bound rejected rather than truncated.
        self.assertIn("if length == 1", self.script)
        self.assertIn('.[0].uuid | type) == "string"', self.script)
        self.assertIn('test("[\\n\\r]") | not', self.script)
        self.assertIn("stat -c '%s'", self.script)
        self.assertIn('-le 4096', self.script)
        self.assertIn('[[ ! "$RUNNER_UUID" =~ ', self.script)
        self.assertNotIn("grep -Eq", self.script)
        # The watcher never opens the file: the captured value is all it uses.
        watcher = self.script[self.script.index("spot_watch()") :]
        watcher = watcher[: watcher.index("\n}\n") + 3]
        self.assertNotIn("/var/lib/forgejo-runner/.runner", watcher)
        self.assertNotIn(".uuid", watcher)
        for forbidden in ("cat /var/lib/forgejo-runner/.runner", "TOKEN_FILE", "shred"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, watcher)

    def test_keeps_trying_to_end_the_job_after_the_marker(self):
        # The watcher starts before the job, so a notice can arrive while nothing
        # is running. The marker is printed once, then the signal is retried
        # within the notice window instead of being dropped.
        self.assertIn("for _ in $(seq 1 120); do", self.script)
        self.assertIn("if end_one_job; then", self.script)
        marker = self._line_index('echo "bootstrap spot-interruption')
        retry = self._line_index("for _ in $(seq 1 120); do")
        self.assertLess(marker, retry)
        # end_one_job reports whether it signalled anything, so the retry loop
        # can tell "no runner yet" from "signalled".
        self.assertIn("signalled=1", self.script)
        self.assertIn("signalled=0", self.script)
        self.assertIn('return "$signalled"', self.script)

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


@unittest.skipUnless(
    shutil.which("bash") and shutil.which("jq"),
    "the shipped image provides bash and jq; the capture is exercised with them",
)
class RegistrationIdCaptureTests(unittest.TestCase):
    """Run the shipped capture block against fixture registration files.

    The source-contract checks above pin the shape; these cases pin the
    behaviour a malformed or hostile registration file must not change.
    """

    UUID = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b"

    def capture(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.json"
            if content is not None:
                path.write_bytes(content)
            snippet = _capture_snippet().replace(
                "/var/lib/forgejo-runner/.runner", str(path)
            )
            script = "set -euo pipefail\n" + snippet + '\nprintf "%s" "$RUNNER_UUID"\n'
            done = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout

    def test_one_valid_document_yields_the_registration_id(self):
        document = '{"uuid":"%s","token":"SECRET-TOKEN"}' % self.UUID
        captured = self.capture(document.encode())
        self.assertEqual(captured, self.UUID)
        self.assertNotIn("SECRET-TOKEN", captured)

    def test_malformed_registration_input_fails_closed(self):
        uuid = self.UUID
        cases = {
            "missing file": None,
            "empty file": b"",
            "uuid is null": b'{"uuid":null}',
            "uuid is not a string": b'{"uuid":42}',
            "trailing newline inside the string": (
                '{"uuid":"%s\\n"}' % uuid
            ).encode(),
            "second document": (
                '{"uuid":"%s"}\n{"uuid":null}' % uuid
            ).encode(),
            "junk trailing line": (
                '{"uuid":"%s"}\njunk' % uuid
            ).encode(),
            "truncated json": b'{"uuid":"0f1e2d3c-4b5a',
            # A valid document followed by padding past the 4096-byte bound and
            # then a second value: rejecting the file, not truncating it, is the
            # point.
            "oversized file": (
                '{"uuid":"%s"}\n' % uuid
            ).encode()
            + b" " * 5000
            + b'\n{"uuid":null}',
        }
        for name, content in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.capture(content), "unknown")

    def test_a_padded_but_bounded_document_still_yields_the_id(self):
        document = '{"uuid":"%s"}' % self.UUID
        captured = self.capture((document + " " * 100).encode())
        self.assertEqual(captured, self.UUID)

    def test_the_size_bound_is_exact(self):
        # A document padded to the bound is accepted; one byte more is rejected
        # rather than truncated.
        document = ('{"uuid":"%s"}' % self.UUID).encode()
        at_bound = document + b" " * (4096 - len(document))
        over_bound = document + b" " * (4097 - len(document))
        self.assertEqual(len(at_bound), 4096)
        self.assertEqual(len(over_bound), 4097)
        self.assertEqual(self.capture(at_bound), self.UUID)
        self.assertEqual(self.capture(over_bound), "unknown")


@unittest.skipUnless(shutil.which("bash"), "the watcher runs under bash")
class EndOneJobTests(unittest.TestCase):
    """Run the signal helper against a stubbed process table.

    A delivered signal is the only thing that may end the retry window; a
    process found but not signalled must keep it open.
    """

    def run_helper(self, kill_status):
        with tempfile.TemporaryDirectory() as directory:
            process = Path(directory) / "proc" / "4242"
            process.mkdir(parents=True)
            (process / "cmdline").write_bytes(
                b"/usr/local/bin/forgejo-runner\0one-job\0"
            )
            snippet = _end_one_job_snippet().replace(
                "/proc/", str(Path(directory) / "proc") + "/"
            )
            script = (
                "set -uo pipefail\n"
                "stat() { printf runner; }\n"
                "grep() { return 0; }\n"
                f"kill() {{ return {kill_status}; }}\n" + snippet + "\nend_one_job\necho rc=$?\n"
            )
            done = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            return done.stdout.strip()

    def test_a_delivered_signal_reports_success(self):
        self.assertEqual(self.run_helper(0), "rc=0")

    def test_a_failed_signal_reports_failure(self):
        # The retry loop depends on this: a process that vanished before the
        # signal must not end the window.
        self.assertEqual(self.run_helper(1), "rc=1")


#: Stand-in for the pinned runner. It counts and records each poll, then runs the
#: scenario the test prepared for that poll. It is a separate process, so it
#: inherits the poll's descriptors exactly as the real runner would.
STUB_RUNNER = """#!/bin/bash
set -u
number=$(( $(cat "$STUB_DIR/count" 2>/dev/null || echo 0) + 1 ))
echo "$number" > "$STUB_DIR/count"
printf '%s\\n' "$*" >> "$STUB_DIR/calls"
if [ -e /proc/self/fd/9 ]; then
  echo open > "$STUB_DIR/fd9"
fi
scenario="$STUB_DIR/poll-$number.sh"
if [ ! -f "$scenario" ]; then
  echo "stub runner: no scenario for poll $number" >&2
  exit 99
fi
. "$scenario"
exit 0
"""

#: Commands the extracted tail runs that a test cannot let loose. The clock and
#: the pause read and advance a virtual clock, so a claim window of minutes runs
#: in milliseconds and every boundary is exact.
COMMON_STUBS = r"""
date() { if [ "${1:-}" = "+%s" ]; then cat "$STUB_DIR/clock"; else command date "$@"; fi; }
sleep() {
  printf '%s\n' "$1" >> "$STUB_DIR/sleeps"
  printf '%s\n' "$(( $(cat "$STUB_DIR/clock") + $1 ))" > "$STUB_DIR/clock"
}
runuser() { shift 3; "$@"; }
"""

#: Drops the options and the duration, then runs the rest: the poll itself, as
#: the stub runner, with the real pipeline around it.
PASS_THROUGH_TIMEOUT = r"""
timeout() {
  printf '%s %s %s\n' "$1" "$2" "$3" >> "$STUB_DIR/timeouts"
  while [[ ${1:-} == --* ]]; do shift; done
  shift
  "$@"
}
"""

#: Stands in for ``on_exit``: it snapshots the capture before the shipped
#: ``cleanup_capture`` removes it, prints the exit marker in the shipped form and
#: logs that the cleanup ran.
TRAP = r"""
rc=$?
if [ -n "$CAPTURE" ] && [ -f "$CAPTURE" ]; then
  cp "$CAPTURE" "$STUB_DIR/capture-snapshot"
  printf '%s %s %s\n' "$(stat -c %a "$CAPTURE")" "$(stat -c %u "$CAPTURE")" "$CAPTURE" > "$STUB_DIR/capture-stat"
fi
cleanup_capture
printf 'bootstrap exiting rc=%s\n' "$rc"
printf cleanup >> "$STUB_DIR/cleanup"
"""

PUBLISHED_LITERALS = (
    "CLAIM_WINDOW_SECONDS",
    "POLL_INTERVAL_SECONDS",
    "NO_TASK_STATUS",
    "NO_TASK_MESSAGE",
    "CAPTURE_LIMIT_BYTES",
)


class TailHarness:
    """Run the shipped claim loop against stub commands, never the real bootstrap.

    The real file reads SSM, writes iptables rules and shuts the host down. These
    cases extract the tail from ``JOB_RC=0`` onward, point its paths into a
    temporary tree and stub only external commands: ``timeout`` and ``runuser``
    run a stub runner (or return one constant status), ``sleep`` and ``date``
    read a virtual clock, and ``id`` names the job account's uid. The pipeline,
    the capture, the classification and the endings are the shipped code. A
    synthetic EXIT trap stands in for ``on_exit`` and proves the tail's exit still
    reaches it.

    The job account cannot be a real user here, so a surviving job process is a
    fake ``/proc/<pid>/status`` entry that carries the stub uid.
    """

    CLOCK_START = 1_000_000
    JOB_UID = 4242

    def setUp(self):
        super().setUp()
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.work = self.root / "runner"  # stands in for /var/lib/forgejo-runner
        self.run_dir = self.root / "run"  # stands in for /run
        self.proc = self.root / "proc"  # stands in for /proc
        self.stub = self.root / "stub"  # stub state, logs and scenarios
        for directory in (self.work, self.run_dir, self.proc, self.stub):
            directory.mkdir()
        # A baseline process table: a root process and one of another account.
        self.add_process(1, 0)
        self.add_process(500, self.JOB_UID + 1)
        self.capture_dir = self.run_dir

    def add_process(self, pid, uid, effective=None):
        effective = uid if effective is None else effective
        (self.proc / str(pid)).mkdir(exist_ok=True)
        (self.proc / str(pid) / "status").write_text(
            f"Name:\tstub\nPid:\t{pid}\nPPid:\t1\n"
            f"Uid:\t{uid}\t{effective}\t{uid}\t{effective}\n"
            f"Gid:\t{uid}\t{effective}\t{uid}\t{effective}\n",
            encoding="utf-8",
        )

    def tail_text(self):
        lines = BOOTSTRAP.read_text(encoding="utf-8").splitlines()
        start = lines.index("JOB_RC=0")
        tail = "\n".join(lines[start:])
        self.assertIn("/var/lib/forgejo-runner/.runner", tail)
        return (
            tail.replace("/var/lib/forgejo-runner", str(self.work))
            .replace("/usr/local/bin/forgejo-runner", str(self.stub / "forgejo-runner"))
            .replace("/run/runner-capture.", f"{self.capture_dir}/runner-capture.")
            .replace("/proc/", f"{self.proc}/")
        )

    # Scenarios: the bash a stub runner runs for one poll.

    def no_task(self):
        """The shipped no-task status and message."""
        return int(_literal_value("NO_TASK_STATUS")), _literal_value("NO_TASK_MESSAGE")

    @staticmethod
    def advance(seconds):
        return f'echo $(( $(cat "$STUB_DIR/clock") + {seconds} )) > "$STUB_DIR/clock"'

    def empty_poll(self, advance=0):
        status, message = self.no_task()
        return "\n".join(
            (
                self.advance(advance),
                "printf '%s\\n' 'level=info msg=\"runner started\"' >&2",
                f"printf '%s\\n' {shlex.quote(message)} >&2",
                f"exit {status}",
            )
        )

    def job_poll(self, status=0, advance=0):
        return "\n".join(
            (
                self.advance(advance),
                "printf '%s\\n' 'task 1 started' >&2",
                "printf '%s\\n' 'task 1 finished' >&2",
                f"exit {status}",
            )
        )

    # Running the tail.

    def _reset(self):
        for path in self.stub.iterdir():
            if path.is_file():
                path.unlink()

    def start_tail(
        self,
        *,
        polls=(),
        constant=None,
        registration_file=True,
        capture_available=True,
        clock_offset=0,
        env=None,
    ):
        """Launch the extracted tail and return the process.

        ``polls`` are the scenarios, one per poll, run by the stub runner.
        ``constant=(status, output)`` stubs ``timeout`` alone with one status and
        optional output instead. ``clock_offset`` moves the virtual clock from the
        moment the claim window started. ``registration_file=False`` puts a
        non-empty directory where the registration file lives, which makes the
        shipped ``rm -f`` fail; no command is stubbed for that. ``env`` adds
        variables to the environment of the bootstrap shell.
        """
        self._reset()
        registration = self.work / ".runner"
        if registration_file:
            registration.write_text("registration", encoding="utf-8")
        else:
            registration.mkdir()
            (registration / "config.yml").write_text("stale", encoding="utf-8")
        self.capture_dir = self.run_dir if capture_available else self.root / "no-such-directory"
        window = int(_literal_value("CLAIM_WINDOW_SECONDS"))
        (self.stub / "clock").write_text(str(self.CLOCK_START + clock_offset), encoding="utf-8")
        runner = self.stub / "forgejo-runner"
        runner.write_text(STUB_RUNNER, encoding="utf-8")
        runner.chmod(0o755)
        for number, scenario in enumerate(polls, start=1):
            (self.stub / f"poll-{number}.sh").write_text(scenario + "\n", encoding="utf-8")
        if constant is None:
            timeout = PASS_THROUGH_TIMEOUT
        else:
            status, output = constant
            timeout = (
                f"STUB_RC={status}\nSTUB_OUTPUT={shlex.quote(output)}\n"
                "timeout() {\n"
                '  if [ -n "$STUB_OUTPUT" ]; then printf \'%s\\n\' "$STUB_OUTPUT" >&2; fi\n'
                '  return "$STUB_RC"\n'
                "}\n"
            )
        script = self.root / "harness.sh"
        script.write_text(
            "\n".join(
                (
                    "#!/bin/bash",
                    "set -euo pipefail",
                    f"export STUB_DIR={shlex.quote(str(self.stub))} PROC_DIR={shlex.quote(str(self.proc))}"
                    f" CAPTURE_DIR={shlex.quote(str(self.capture_dir))}",
                    *(_literal_line(name) for name in PUBLISHED_LITERALS),
                    f"CLAIM_DEADLINE={self.CLOCK_START + window}",
                    'CAPTURE=""',
                    _function_snippet("cleanup_capture"),
                    COMMON_STUBS,
                    f"id() {{ printf '%s\\n' {self.JOB_UID}; }}",
                    timeout,
                    f"trap {shlex.quote(TRAP)} EXIT",
                    self.tail_text(),
                    "",
                )
            ),
            encoding="utf-8",
        )
        return subprocess.Popen(
            ["/bin/bash", str(script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, **(env or {})},
        )

    def run_tail(self, **options):
        process = self.start_tail(**options)
        stdout, stderr = process.communicate(timeout=60)
        return subprocess.CompletedProcess(
            process.args,
            process.returncode,
            stdout.decode("utf-8", "replace"),
            stderr.decode("utf-8", "replace"),
        )

    # What a run left behind.

    def read_text(self, name):
        path = self.stub / name
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def read_lines(self, name):
        return self.read_text(name).splitlines()

    @staticmethod
    def markers(completed):
        return [
            line.strip()
            for line in completed.stdout.splitlines()
            if line.strip().startswith("bootstrap one-job")
        ]

    def capture_snapshot(self):
        path = self.stub / "capture-snapshot"
        return path.read_bytes() if path.exists() else None

    # Assertions the scenarios share.

    def assert_finished_attempt(self, completed, *, marker, status, polls=1):
        """One job path: the runner's own status is the result, and no claim timeout."""
        interval = _literal_value("POLL_INTERVAL_SECONDS")
        self.assertEqual(len(self.read_lines("calls")), polls)
        self.assertEqual(self.read_lines("sleeps"), [interval] * (polls - 1))
        self.assertEqual(self.markers(completed), [marker])
        self.assertEqual(completed.stdout.count("STEP job"), 1)
        self.assertNotIn("STEP claim-timeout", completed.stdout)
        self.assertEqual(completed.returncode, status)
        self.assertEqual(self.read_text("cleanup"), "cleanup")
        self.assertFalse((self.work / ".runner").exists())

    def assert_claim_timeout(self, completed, *, polls, pauses):
        """The claim window ended: one marker, status 0, no job step, no job marker."""
        interval = _literal_value("POLL_INTERVAL_SECONDS")
        self.assertEqual(len(self.read_lines("calls")), polls)
        self.assertEqual(self.read_lines("sleeps"), [interval] * pauses)
        self.assertEqual(completed.stdout.count("STEP claim-timeout"), 1)
        self.assertNotIn("STEP job", completed.stdout)
        self.assertEqual(self.markers(completed), [])
        self.assertEqual(completed.returncode, 0)
        self.assertIn("bootstrap exiting rc=0", completed.stdout)
        self.assertEqual(self.read_text("cleanup"), "cleanup")
        self.assertFalse((self.work / ".runner").exists())


@unittest.skipUnless(shutil.which("bash"), "the tail runs under bash")
class BootstrapTailTests(TailHarness, unittest.TestCase):
    """Run only the extracted tail, never the real bootstrap.

    Here ``timeout`` is stubbed with one constant status and optional output, so
    each one-job status is produced without starting a runner. The claim loop,
    the capture and the endings are the shipped code.
    """

    def _run(self, status, *, registration_file=True, output="", clock_offset=0):
        """Run the extracted tail for *status* and return it with the trap log.

        ``registration_file=False`` puts a non-empty directory where the
        registration file lives, which makes the tail's real ``rm -f`` fail:
        the bootstrap hands that directory to the ``runner`` account, so job
        code can make the removal fail. No command is stubbed.
        """
        completed = self.run_tail(
            constant=(status, output),
            registration_file=registration_file,
            clock_offset=clock_offset,
        )
        if registration_file:
            self.assertEqual(completed.stderr, "")
        return completed, self.read_text("cleanup")

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

    def test_the_documented_status_without_the_message_is_a_finished_attempt(self):
        status, _ = self.no_task()
        completed, cleanup = self._run(status)
        self.assertEqual(self.markers(completed), [f"bootstrap one-job failed rc={status}"])
        self.assertEqual(completed.returncode, status)
        self.assertNotIn("STEP claim-timeout", completed.stdout)
        self.assertEqual(cleanup, "cleanup")

    def test_a_claim_timeout_ends_the_worker_with_status_0_and_no_job_marker(self):
        status, message = self.no_task()
        window = int(_literal_value("CLAIM_WINDOW_SECONDS"))
        # The clock starts at the deadline, so the window has passed before the first poll.
        completed, cleanup = self._run(status, output=message, clock_offset=window)
        self.assertEqual(completed.stdout.count("STEP claim-timeout"), 1)
        self.assertNotIn("STEP job", completed.stdout)
        self.assertEqual(self.markers(completed), [])
        self.assertEqual(completed.returncode, 0)
        self.assertIn("bootstrap exiting rc=0", completed.stdout)
        self.assertEqual(cleanup, "cleanup")
        self.assertFalse(
            (self.work / ".runner").exists(),
            "the claim-timeout path must remove the registration file",
        )
        self.assertEqual(self.read_lines("sleeps"), [], "no pause once the deadline has passed")

    def test_a_failed_registration_removal_after_a_claim_timeout_keeps_status_0(self):
        status, message = self.no_task()
        window = int(_literal_value("CLAIM_WINDOW_SECONDS"))
        completed, cleanup = self._run(
            status, registration_file=False, output=message, clock_offset=window
        )
        self.assertIn("cannot remove", completed.stderr, "the removal must really fail")
        self.assertIn("WARNING: runner registration file not removed", completed.stdout)
        self.assertEqual(completed.stdout.count("STEP claim-timeout"), 1)
        # The warning is plain text. No job ran, so no one-job marker and no job step.
        self.assertEqual(self.markers(completed), [])
        self.assertNotIn("STEP job", completed.stdout)
        self.assertEqual(completed.returncode, 0)
        self.assertIn("bootstrap exiting rc=0", completed.stdout)
        self.assertEqual(cleanup, "cleanup", "token cleanup, sync and shutdown still run")
        self.assertTrue((self.work / ".runner").is_dir())


@unittest.skipUnless(shutil.which("bash"), "the claim loop runs under bash")
class ClaimLoopTests(TailHarness, unittest.TestCase):
    """Run the shipped claim loop with a stub runner, a virtual clock and no pause.

    Every outcome that is not the documented empty result must end the loop, and
    the loop must never poll at or after the deadline. The stub runner is a real
    child process behind the real pipeline, so the capture, the console copy and
    the status handling are exercised, not simulated.
    """

    def test_each_poll_runs_the_one_job_mode_without_wait_under_the_job_timeout(self):
        completed = self.run_tail(polls=[self.empty_poll(), self.empty_poll(), self.job_poll()])
        self.assertEqual(
            self.read_lines("calls"), ["one-job -c /etc/forgejo-runner/config.yml"] * 3
        )
        self.assertEqual(
            self.read_lines("timeouts"), ["--signal=TERM --kill-after=60 3600"] * 3
        )
        self.assertFalse(
            (self.stub / "fd9").exists(), "the runner must not inherit the console copy descriptor"
        )
        self.assertEqual(completed.returncode, 0)

    def test_empty_polls_then_a_claim_timeout(self):
        window = int(_literal_value("CLAIM_WINDOW_SECONDS"))
        interval = int(_literal_value("POLL_INTERVAL_SECONDS"))
        polls = window // interval
        # Scenarios beyond the expected polls must never be used.
        completed = self.run_tail(polls=[self.empty_poll()] * (polls + 5))
        self.assert_claim_timeout(completed, polls=polls, pauses=polls)

    def test_empty_polls_then_a_job(self):
        completed = self.run_tail(polls=[self.empty_poll(), self.empty_poll(), self.job_poll()])
        self.assert_finished_attempt(
            completed, marker="bootstrap one-job completed rc=0", status=0, polls=3
        )
        self.assertIn("task 1 finished", completed.stdout)

    def test_every_other_outcome_ends_the_loop_as_a_finished_attempt(self):
        status, message = self.no_task()
        limit = int(_literal_value("CAPTURE_LIMIT_BYTES"))
        quoted = shlex.quote(message)
        flood = f"head -c {limit * 2} /dev/zero | tr '\\0' x; printf '\\n'"
        cases = {
            "the status alone": (
                [f"echo 'something else' >&2\nexit {status}"],
                {},
                f"bootstrap one-job failed rc={status}",
                status,
            ),
            "the message alone": (
                [f"printf '%s\\n' {quoted} >&2\nexit 1"],
                {},
                "bootstrap one-job failed rc=1",
                1,
            ),
            "a signal": (["kill -TERM $$"], {}, "bootstrap one-job failed rc=143", 143),
            "an oversized capture": (
                [f"printf '%s\\n' {quoted} >&2\n{flood}\nexit {status}"],
                {},
                f"bootstrap one-job failed rc={status}",
                status,
            ),
            "a missing capture": (
                [self.empty_poll()],
                {"capture_available": False},
                f"bootstrap one-job failed rc={status}",
                status,
            ),
            "a capture that is no longer a regular file": (
                [
                    'for path in "$CAPTURE_DIR"/runner-capture.*; do rm -f "$path"; mkdir "$path"; done\n'
                    f"printf '%s\\n' {quoted} >&2\nexit {status}"
                ],
                {},
                f"bootstrap one-job failed rc={status}",
                status,
            ),
        }
        for name, (polls, options, marker, result) in cases.items():
            with self.subTest(outcome=name):
                # A second scenario that must never run: no poll follows a finished attempt.
                completed = self.run_tail(polls=polls + [self.empty_poll()], **options)
                self.assert_finished_attempt(completed, marker=marker, status=result)

    def test_no_poll_starts_at_or_after_the_deadline(self):
        window = int(_literal_value("CLAIM_WINDOW_SECONDS"))
        interval = int(_literal_value("POLL_INTERVAL_SECONDS"))
        cases = (
            # (seconds after registration when the first poll ends, polls, pauses)
            # Control: time remains for a pause and one more poll.
            (window - 2 * interval, 2, 2),
            # Ends exactly one interval before the deadline.
            (window - interval, 1, 1),
            # Ends less than one interval before it: pause, then the second check ends the window.
            (window - interval + 5, 1, 1),
            (window - 1, 1, 1),
            # Ends at or after the deadline: neither a pause nor another poll.
            (window, 1, 0),
            (window + 1, 1, 0),
            (window + 600, 1, 0),
        )
        for ended, polls, pauses in cases:
            with self.subTest(ended=ended):
                scenarios = [self.empty_poll(advance=ended)] + [self.empty_poll()] * 40
                completed = self.run_tail(polls=scenarios)
                self.assert_claim_timeout(completed, polls=polls, pauses=pauses)

    def test_no_poll_starts_when_the_deadline_has_passed_before_the_first_poll(self):
        # The setup between register and the loop takes unbounded time, so the first
        # poll is held to the same rule as every later one: no poll at or after the
        # deadline. The stub scenarios must never run.
        window = int(_literal_value("CLAIM_WINDOW_SECONDS"))
        for offset in (window, window + 1, window + 600):
            with self.subTest(clock_offset=offset):
                completed = self.run_tail(polls=[self.empty_poll()] * 3, clock_offset=offset)
                self.assert_claim_timeout(completed, polls=0, pauses=0)
        # The control: one second before the deadline the first poll does start, and
        # the pause after that empty poll then ends the window.
        completed = self.run_tail(polls=[self.empty_poll()] * 3, clock_offset=window - 1)
        self.assert_claim_timeout(completed, polls=1, pauses=1)

    def test_the_deadline_does_not_interrupt_a_running_poll(self):
        window = int(_literal_value("CLAIM_WINDOW_SECONDS"))
        poll = "\n".join(
            (
                "printf '%s\\n' 'task started before the deadline' >&2",
                self.advance(window + 600),
                "printf '%s\\n' 'task finished after the deadline' >&2",
                "exit 0",
            )
        )
        completed = self.run_tail(polls=[poll, self.empty_poll()])
        self.assert_finished_attempt(
            completed, marker="bootstrap one-job completed rc=0", status=0
        )
        self.assertIn("task started before the deadline", completed.stdout)
        self.assertIn("task finished after the deadline", completed.stdout)

    def test_a_poll_that_writes_more_than_the_capture_bound_runs_to_its_own_status(self):
        # A closed pipe signals the writer and a full pipe blocks it, so a bound
        # applied on the write side would stop this poll. Several pipe buffers of
        # output must reach the console, and the runner must exit with its own status.
        limit = int(_literal_value("CAPTURE_LIMIT_BYTES"))
        total = limit * 4
        poll = "\n".join(
            (
                f"head -c {total} /dev/zero | tr '\\0' x",
                "printf '\\nEND-OF-OUTPUT\\n'",
                "exit 7",
            )
        )
        completed = self.run_tail(polls=[poll, self.empty_poll()])
        self.assert_finished_attempt(
            completed, marker="bootstrap one-job failed rc=7", status=7
        )
        self.assertIn("\nEND-OF-OUTPUT\n", completed.stdout)
        self.assertGreaterEqual(completed.stdout.count("x"), total)
        # The copy that classifies the poll stops at the bound plus one byte.
        self.assertEqual(len(self.capture_snapshot()), limit + 1)

    def test_a_child_that_writes_the_message_to_every_inherited_pipe_does_not_make_a_poll_empty(self):
        # Job code runs as the same account as the runner, so text that it writes
        # through a descriptor it inherits can land in the capture. Here the runner
        # of the claimed job ends with its own status, so the loop still ends. Job
        # code that can also end the runner with the documented status is the subject
        # of the live check in runner-setup.md, not of this stub.
        status, message = self.no_task()
        poll = "\n".join(
            (
                "(",
                "  for fd in /proc/self/fd/*; do",
                f"    if [ -p \"$fd\" ]; then printf '%s\\n' {shlex.quote(message)} >> \"$fd\"; fi",
                "  done",
                ")",
                "exit 1",
            )
        )
        completed = self.run_tail(polls=[poll, self.empty_poll()])
        self.assert_finished_attempt(
            completed, marker="bootstrap one-job failed rc=1", status=1
        )
        # Not vacuous: the capture really held the message as a complete line.
        self.assertIn("\n" + message + "\n", "\n" + self.capture_snapshot().decode("utf-8"))

    def test_the_message_makes_a_poll_empty_only_as_a_whole_line(self):
        # A complete line is the message and one line feed with no other byte on the
        # line, so a longer line, a carriage return, a missing line feed or a NUL byte
        # in the capture is a finished attempt even with the documented status.
        window = int(_literal_value("CLAIM_WINDOW_SECONDS"))
        status, message = self.no_task()
        quoted = shlex.quote(message)
        not_empty = {
            "a longer line before the message": f"printf 'x %s\\n' {quoted} >&2",
            "a space before the message": f"printf ' %s\\n' {quoted} >&2",
            "a longer line after the message": f"printf '%s x\\n' {quoted} >&2",
            "a carriage return after the message": f"printf '%s\\r\\n' {quoted} >&2",
            "no line feed after the message": f"printf '%s' {quoted} >&2",
            "a NUL byte after the message": f"printf '%s\\n\\0' {quoted} >&2",
        }
        for name, body in not_empty.items():
            with self.subTest(capture=name):
                completed = self.run_tail(polls=[f"{body}\nexit {status}", self.empty_poll()])
                self.assert_finished_attempt(
                    completed, marker=f"bootstrap one-job failed rc={status}", status=status
                )
        # The controls: the same message as a whole line is an empty poll wherever it sits.
        empty = {
            "the first line": f"printf '%s\\n' {quoted} >&2; echo 'level=info msg=\"later\"' >&2",
            "the last line": f"echo 'level=info msg=\"earlier\"' >&2; printf '%s\\n' {quoted} >&2",
            "between other lines": (
                f"echo first >&2; printf '%s\\n' {quoted} >&2; echo last >&2"
            ),
            "twice": f"printf '%s\\n%s\\n' {quoted} {quoted} >&2",
        }
        for name, body in empty.items():
            with self.subTest(capture=name):
                poll = "\n".join((self.advance(window), body, f"exit {status}"))
                completed = self.run_tail(polls=[poll])
                self.assert_claim_timeout(completed, polls=1, pauses=0)

    def test_non_ascii_runner_output_does_not_hide_the_message(self):
        # The size check counts bytes. In a UTF-8 locale a character count would differ
        # from the file size, and a harmless accented letter would end the loop early.
        window = int(_literal_value("CLAIM_WINDOW_SECONDS"))
        status, message = self.no_task()
        poll = "\n".join(
            (
                self.advance(window),
                "printf 'caf\\303\\251 \\342\\200\\246\\n' >&2",
                f"printf '%s\\n' {shlex.quote(message)} >&2",
                f"exit {status}",
            )
        )
        completed = self.run_tail(polls=[poll], env={"LC_ALL": "C.UTF-8"})
        self.assert_claim_timeout(completed, polls=1, pauses=0)

    def test_a_surviving_job_process_ends_the_loop_even_when_status_and_message_match(self):
        status, message = self.no_task()
        survivor = "\n".join(
            (
                'mkdir -p "$PROC_DIR/9001"',
                f"printf 'Name:\\tstub\\nUid:\\t{self.JOB_UID}\\t{self.JOB_UID}\\t{self.JOB_UID}\\t{self.JOB_UID}\\n'"
                ' > "$PROC_DIR/9001/status"',
            )
        )
        matching = "\n".join(
            (f"printf '%s\\n' {shlex.quote(message)} >&2", f"exit {status}")
        )
        completed = self.run_tail(polls=[survivor + "\n" + matching, self.empty_poll()])
        self.assert_finished_attempt(
            completed, marker=f"bootstrap one-job failed rc={status}", status=status
        )
        self.assertIn("\n" + message + "\n", "\n" + self.capture_snapshot().decode("utf-8"))
        # The control: the same status and message without the survivor is an empty poll.
        shutil.rmtree(self.proc / "9001")
        completed = self.run_tail(polls=[matching] + [self.empty_poll()] * 40)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout.count("STEP claim-timeout"), 1)
        self.assertGreater(len(self.read_lines("calls")), 1)

    def test_the_documented_message_with_another_status_is_a_finished_attempt(self):
        # Every ordinary outcome of a claimed job's runner must be a finished
        # attempt even when it prints the message; 3 stands for any other status
        # in the band. This is not a guard against hostile job code, which can end
        # the runner with the no-task status (see docs/runner-setup.md).
        _, message = self.no_task()
        cases = {
            0: "completed rc=0",
            1: "failed rc=1",
            3: "failed rc=3",
            124: "timeout rc=124",
            125: "failed rc=125",
            128: "failed rc=128",
            137: "timeout rc=137",
        }
        for other, marker in cases.items():
            with self.subTest(status=other):
                poll = f"printf '%s\\n' {shlex.quote(message)} >&2\nexit {other}"
                completed = self.run_tail(polls=[poll, self.empty_poll()])
                self.assert_finished_attempt(
                    completed, marker=f"bootstrap one-job {marker}", status=other
                )

    def test_runner_output_reaches_the_console_while_a_poll_runs(self):
        release = self.stub / "release"
        poll = "\n".join(
            (
                "printf '%s\\n' 'first line from the runner' >&2",
                f'for _ in $(seq 1 400); do [ -e "{release}" ] && break; sleep 0.05; done',
                "printf '%s\\n' 'last line from the runner' >&2",
                "exit 0",
            )
        )
        process = self.start_tail(polls=[poll])
        try:
            seen = b""
            deadline = time.monotonic() + 20
            while b"first line from the runner" not in seen and time.monotonic() < deadline:
                ready, _, _ = select.select([process.stdout], [], [], 0.2)
                if ready:
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        break
                    seen += chunk
            self.assertIn(b"first line from the runner", seen)
            # The poll is still running: the stub waits for a file only this test creates.
            self.assertIsNone(process.poll())
            self.assertFalse(release.exists())
        finally:
            release.write_text("go", encoding="utf-8")
            stdout, _ = process.communicate(timeout=60)
        text = (seen + stdout).decode("utf-8", "replace")
        self.assertIn("last line from the runner", text)
        self.assertEqual(process.returncode, 0)

    def test_every_line_but_the_no_task_line_of_an_empty_poll_reaches_the_console(self):
        window = int(_literal_value("CLAIM_WINDOW_SECONDS"))
        status, message = self.no_task()
        runner_lines = ["level=info msg=\"first\"", "level=info msg=\"second\"", "plain text"]
        poll = "\n".join(
            (
                self.advance(window),
                *(f"printf '%s\\n' {shlex.quote(line)} >&2" for line in runner_lines),
                f"printf '%s\\n' {shlex.quote(message)} >&2",
                f"exit {status}",
            )
        )
        completed = self.run_tail(polls=[poll])
        self.assert_claim_timeout(completed, polls=1, pauses=0)
        console = completed.stdout.splitlines()
        for line in runner_lines:
            self.assertIn(line, console)
        # A claimed job's output is never filtered, even text that imitates the message.
        job = "\n".join(
            (f"printf '%s\\n' {shlex.quote(message)} >&2", "echo 'job output' >&2", "exit 0")
        )
        completed = self.run_tail(polls=[job])
        self.assertIn("job output", completed.stdout.splitlines())

    def test_the_capture_is_private_outside_the_runner_directory_and_removed_at_cleanup(self):
        completed = self.run_tail(polls=[self.empty_poll(advance=int(_literal_value("CLAIM_WINDOW_SECONDS")))])
        self.assertEqual(completed.returncode, 0)
        mode, owner, path = self.read_text("capture-stat").split(" ", 2)
        # Mode 0600 and created by the bootstrap itself (root in production): the job
        # account holds no descriptor to it and cannot open it by its path.
        self.assertEqual(mode, "600")
        self.assertEqual(int(owner), os.getuid())
        capture = Path(path)
        self.assertEqual(capture.parent, self.run_dir)
        self.assertNotIn(self.work, capture.parents)
        self.assertFalse(capture.exists(), "the capture must be removed with the other cleanup")
        # Only the pipeline's own head stage writes it, never the runner by path.
        self.assertNotIn(str(capture), self.read_text("calls"))

    def test_the_console_of_an_idle_worker_parses_as_a_claim_timeout(self):
        from ci_burst.export import parse_console

        window = int(_literal_value("CLAIM_WINDOW_SECONDS"))
        completed = self.run_tail(polls=[self.empty_poll(advance=window - 5)])
        self.assertEqual(completed.returncode, 0)
        script = BOOTSTRAP.read_text(encoding="utf-8")
        for step in ('echo "STEP mount-options"', 'echo "STEP register"'):
            self.assertIn(step, script)
        # The harness trap prints the exit marker in the form on_exit ships.
        self.assertIn('echo "bootstrap exiting rc=$rc"', _function_snippet("on_exit"))
        console = "STEP mount-options\nSTEP register\n" + completed.stdout
        facts = parse_console(console)
        self.assertEqual(facts.steps, ("mount-options", "register", "claim-timeout"))
        self.assertEqual(facts.exit_code, 0)
        self.assertEqual(facts.errors, ())
        self.assertIsNone(facts.job_process)


@unittest.skipUnless(shutil.which("bash"), "the survival check runs under bash")
class JobAccountAliveTests(unittest.TestCase):
    """Run the shipped survival check against a stubbed process table.

    The check reads ``Uid:`` from ``/proc/<pid>/status``, not the ownership of the
    ``/proc/<pid>`` directory: a process that cleared its dumpable flag has a
    root-owned directory but keeps its real credentials in ``status``.
    """

    UID = 4242

    def alive(self, processes, *, uid_lookup_fails=False):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory) / "proc"
            for pid, status in processes.items():
                (proc / str(pid)).mkdir(parents=True)
                if status is not None:
                    (proc / str(pid) / "status").write_text(status, encoding="utf-8")
            snippet = _function_snippet("job_account_alive").replace("/proc/", f"{proc}/")
            lookup = "return 1" if uid_lookup_fails else f"printf '%s\\n' {self.UID}"
            script = (
                "set -euo pipefail\n"
                f"id() {{ {lookup}; }}\n"
                + snippet
                + "\nif job_account_alive; then echo alive; else echo none; fi\n"
            )
            done = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, timeout=30
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stderr, "")
            return done.stdout.strip()

    @staticmethod
    def status(real, effective=None, saved=None, filesystem=None):
        effective = real if effective is None else effective
        saved = real if saved is None else saved
        filesystem = effective if filesystem is None else filesystem
        return (
            f"Name:\tstub\nUid:\t{real}\t{effective}\t{saved}\t{filesystem}\n"
            "Gid:\t0\t0\t0\t0\n"
        )

    def test_a_process_of_the_job_account_is_found(self):
        self.assertEqual(self.alive({1: self.status(0), 20: self.status(self.UID)}), "alive")

    def test_any_one_of_the_four_credentials_of_the_job_account_counts(self):
        # The process may hold the uid in only one field, for example after a setuid
        # call, so each of the real, effective, saved and filesystem uids is enough.
        uid = self.UID
        fields = {
            "real": self.status(uid, 0, 0, 0),
            "effective": self.status(0, uid, 0, 0),
            "saved": self.status(0, 0, uid, 0),
            "filesystem": self.status(0, 0, 0, uid),
        }
        for name, status in fields.items():
            with self.subTest(field=name):
                self.assertEqual(self.alive({1: self.status(0), 20: status}), "alive")
        self.assertEqual(self.alive({1: self.status(0), 20: self.status(0, 0, 0, 0)}), "none")

    def test_other_accounts_and_unreadable_entries_do_not_count(self):
        table = {
            1: self.status(0),
            2: self.status(self.UID + 1),
            3: None,  # a process that vanished between the listing and the read
            4: "Name:\tstub\nState:\tS (sleeping)\n",  # no credentials line
        }
        self.assertEqual(self.alive(table), "none")

    def test_a_process_table_that_cannot_be_read_counts_as_alive(self):
        # No readable credentials line anywhere means the check proved nothing, and a
        # doubt ends the claim loop. An entry that vanished or has no credentials line
        # is not a readable table.
        self.assertEqual(self.alive({}), "alive")
        self.assertEqual(self.alive({3: None, 4: "Name:\tstub\n"}), "alive")

    def test_a_job_account_uid_that_cannot_be_looked_up_counts_as_alive(self):
        # Without the uid nothing can be ruled out, so the loop must end.
        self.assertEqual(self.alive({1: self.status(0)}, uid_lookup_fails=True), "alive")


@unittest.skipUnless(shutil.which("bash"), "the deadline check runs under bash")
class ClaimDeadlineTests(unittest.TestCase):
    """Run the shipped deadline check against a stubbed clock.

    The deadline has passed when the time is at or after it, and a clock that
    cannot be read counts as passed, so a doubt ends the claim loop.
    """

    DEADLINE = 1_000_300

    def passed(self, clock):
        script = (
            "set -euo pipefail\n"
            f"CLAIM_DEADLINE={self.DEADLINE}\n"
            f"date() {{ {clock}; }}\n"
            + _function_snippet("claim_deadline_passed")
            + "\nif claim_deadline_passed; then echo passed; else echo open; fi\n"
        )
        done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_the_deadline_has_passed_at_and_after_it_and_not_before(self):
        for now, expected in (
            (self.DEADLINE - 300, "open"),
            (self.DEADLINE - 1, "open"),
            (self.DEADLINE, "passed"),
            (self.DEADLINE + 1, "passed"),
            (self.DEADLINE + 3600, "passed"),
        ):
            with self.subTest(now=now):
                self.assertEqual(self.passed(f"printf '%s\\n' {now}"), expected)

    def test_a_clock_that_cannot_be_read_counts_as_passed(self):
        self.assertEqual(self.passed("return 1"), "passed")
        self.assertEqual(self.passed("printf 'garbled\\n'"), "passed")
        self.assertEqual(self.passed("printf '\\n'"), "passed")


@unittest.skipUnless(shutil.which("bash"), "the cleanup runs under bash")
class CleanupCaptureTests(unittest.TestCase):
    def run_cleanup(self, capture_path):
        script = (
            "set -euo pipefail\n"
            f"CAPTURE={shlex.quote(capture_path)}\n"
            + _function_snippet("cleanup_capture")
            + "\ncleanup_capture\necho done\n"
        )
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)

    def test_the_capture_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            capture = Path(directory) / "runner-capture.abc"
            capture.write_text("Error: no task received\n", encoding="utf-8")
            done = self.run_cleanup(str(capture))
            self.assertEqual((done.returncode, done.stdout.strip()), (0, "done"), done.stderr)
            self.assertFalse(capture.exists())

    def test_an_unset_capture_removes_nothing_and_cannot_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            bystander = Path(directory) / "bystander"
            bystander.write_text("kept", encoding="utf-8")
            done = self.run_cleanup("")
            self.assertEqual((done.returncode, done.stdout.strip()), (0, "done"), done.stderr)
            self.assertTrue(bystander.exists())

    def test_a_capture_that_cannot_be_removed_cannot_stop_the_trap(self):
        # A failing command inside the EXIT trap would skip the sync and the shutdown.
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "runner-capture.abc"
            blocked.mkdir()
            (blocked / "inner").write_text("x", encoding="utf-8")
            done = self.run_cleanup(str(blocked))
            self.assertEqual((done.returncode, done.stdout.strip()), (0, "done"), done.stderr)


if __name__ == "__main__":
    unittest.main()
