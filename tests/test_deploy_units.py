"""Contract tests for the container deployment artifacts.

The image and the systemd units are configuration, not library code, so these
tests read the committed files instead of starting podman. They need no
credential and no network. The properties they guard are the ones a later edit
could silently remove:

* the apply unit is a one-shot, read-only run with a named container and an
  idempotent force-removal command for stop and start-timeout cleanup;
* the cleanup unit is separate, has no Forgejo token and no ledger, and uses
  only the cleanup credential mount, so it stays runnable when admission stops;
* each timer drives its own unit at the documented interval;
* the image has no daemon surface, uses file credentials only, and the build
  context admits only the declared build inputs;
* credential-free CI builds the image from the repository root, keeps the
  workflow's read-only token and publishes nothing; the semantic workflow
  checks live in ``tests/workflow-policy.test.mjs``.

The live lifecycle itself, including stopping an active invocation and keeping
cleanup running, is exercised by ``tests/test_container_lifecycle.py`` and the
operator procedure in ``docs/container.md``.
"""

import configparser
import fnmatch
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
IMAGE = "localhost/ci-burst:latest"


def unit(name):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(DEPLOY / name, encoding="utf-8")
    return parser


def _cleanup_argv(service):
    return shlex.split(service["Service"]["execstoppost"].lstrip("-").strip())


class ApplyUnitTests(unittest.TestCase):
    def setUp(self):
        self.service = unit("ci-burst-apply.service")
        self.exec_start = self.service["Service"]["execstart"]

    def test_is_one_shot(self):
        self.assertEqual(self.service["Service"]["type"], "oneshot")

    def test_names_its_container_and_replaces_a_leftover(self):
        self.assertIn("--name ci-burst-apply", self.exec_start)
        self.assertIn("--replace", self.exec_start)
        self.assertIn("--init", self.exec_start)

    def test_cleanup_force_removes_the_named_container(self):
        cleanup = _cleanup_argv(self.service)
        self.assertEqual(cleanup[-1], "ci-burst-apply")
        for flag in ("rm", "--force", "--ignore", "--time"):
            with self.subTest(flag=flag):
                self.assertIn(flag, cleanup)
        self.assertEqual(self.service["Service"]["killmode"], "process")
        self.assertGreater(int(self.service["Service"]["timeoutstopsec"]), 0)

    def test_runs_read_only_with_a_tmpfs(self):
        self.assertIn("--read-only", self.exec_start)
        self.assertIn("--tmpfs /tmp:", self.exec_start)

    def test_mounts_config_read_only_and_state_writable(self):
        self.assertIn(
            "/etc/ci-burst/deployment.toml:/etc/ci-burst/deployment.toml:ro",
            self.exec_start,
        )
        self.assertIn("/var/lib/ci-burst:/var/lib/ci-burst:rw", self.exec_start)

    def test_uses_the_token_env_file_and_scaling_credentials(self):
        self.assertIn("--env-file /etc/ci-burst/forgejo.env", self.exec_start)
        self.assertIn("/etc/ci-burst/aws-scaling:/etc/ci-burst/aws:ro", self.exec_start)
        self.assertIn(f"{IMAGE} apply --config", self.exec_start)

    def test_runs_on_the_named_network_with_the_host_path_mtu(self):
        # The default bridge is 1500 and stalls large TLS responses where the
        # path is smaller; the unit must name the measured network.
        self.assertIn("--network ci-burst", self.exec_start)

    def test_publishes_no_port_and_has_no_daemon_flags(self):
        self.assertNotIn("--publish", self.exec_start)
        self.assertNotIn("--detach", self.exec_start)
        self.assertNotIn("--health-cmd", self.exec_start)


class CleanupUnitTests(unittest.TestCase):
    """Cleanup must stay runnable on its own: no token, no ledger, no launch."""

    def setUp(self):
        self.service = unit("ci-burst-reap.service")
        self.exec_start = self.service["Service"]["execstart"]

    def test_is_one_shot(self):
        self.assertEqual(self.service["Service"]["type"], "oneshot")

    def test_does_not_depend_on_the_apply_unit(self):
        for directive in ("wants", "requires", "after", "before"):
            self.assertNotIn("ci-burst-apply", self.service["Unit"].get(directive, ""))

    def test_reaps_with_the_cleanup_identity_only(self):
        self.assertIn("--volume /etc/ci-burst/aws-cleanup:/etc/ci-burst/aws:ro", self.exec_start)
        self.assertNotIn("aws-scaling", self.exec_start)
        self.assertIn(f"{IMAGE} reap --apply --config", self.exec_start)

    def test_needs_no_forgejo_token_and_no_ledger(self):
        self.assertNotIn("--env-file", self.exec_start)
        self.assertNotIn("/var/lib/ci-burst", self.exec_start)

    def test_runs_on_the_named_network_with_the_host_path_mtu(self):
        self.assertIn("--network ci-burst", self.exec_start)

    def test_has_its_own_named_container_and_cleanup_command(self):
        self.assertIn("--name ci-burst-reap", self.exec_start)
        self.assertIn("--init", self.exec_start)
        cleanup = _cleanup_argv(self.service)
        self.assertEqual(cleanup[-1], "ci-burst-reap")
        self.assertIn("--force", cleanup)
        self.assertIn("--ignore", cleanup)

    def test_its_exit_status_is_not_masked(self):
        # The unit's exit status is the only completion signal the scheduler
        # sees: a refused or degraded cleanup must be able to fail the run.
        raw = (DEPLOY / "ci-burst-reap.service").read_text(encoding="utf-8")
        self.assertNotIn("SuccessExitStatus", raw)
        for masking in ("||", "&&", "sh -c", "exit 0"):
            with self.subTest(masking=masking):
                self.assertNotIn(masking, raw)


class ExportUnitTests(unittest.TestCase):
    """The observation unit reads evidence; it must gain no queue credential."""

    def setUp(self):
        self.service = unit("ci-burst-export.service")
        self.exec_start = self.service["Service"]["execstart"]
        self.raw = (DEPLOY / "ci-burst-export.service").read_text(encoding="utf-8")

    def test_is_one_shot_with_a_named_container(self):
        self.assertEqual(self.service["Service"]["type"], "oneshot")
        self.assertIn("--name ci-burst-export", self.exec_start)
        self.assertIn("--replace", self.exec_start)
        self.assertIn("--init", self.exec_start)

    def test_runs_the_export_command_read_only(self):
        self.assertIn("--read-only", self.exec_start)
        self.assertIn("export --config /etc/ci-burst/deployment.toml", self.exec_start)
        self.assertIn("--metrics-file /var/lib/ci-burst/export/ci-burst.prom", self.exec_start)

    def test_mounts_config_and_credentials_read_only_state_writable(self):
        self.assertIn("/etc/ci-burst/deployment.toml:/etc/ci-burst/deployment.toml:ro", self.exec_start)
        self.assertIn("/etc/ci-burst/aws-scaling:/etc/ci-burst/aws:ro", self.exec_start)
        self.assertIn("/var/lib/ci-burst:/var/lib/ci-burst:rw", self.exec_start)

    def test_needs_no_forgejo_token(self):
        self.assertNotIn("--env-file", self.exec_start)
        self.assertNotIn("forgejo.env", self.exec_start)

    def test_publishes_the_textfile_with_an_ordered_rename(self):
        # Two steps: stage next to the collector, then rename inside its own
        # directory, so a scrape never reads a partial file. The exact commands
        # and paths are pinned: a loose substring check would accept a wrong
        # destination.
        self.assertEqual(
            re.findall(r"^ExecStartPost=(.*)$", self.raw, re.M),
            [
                "/usr/bin/install -m 0644 /var/lib/ci-burst/export/ci-burst.prom"
                " /var/lib/prometheus/node-exporter/.ci-burst.prom.new",
                "/usr/bin/mv -f /var/lib/prometheus/node-exporter/.ci-burst.prom.new"
                " /var/lib/prometheus/node-exporter/ci-burst.prom",
            ],
        )
        # The container never mounts the collector directory: it runs as uid
        # 10001 and the directory is root-owned.
        self.assertNotIn("/var/lib/prometheus", self.exec_start)

    def test_cleanup_force_removes_the_named_container(self):
        cleanup = _cleanup_argv(self.service)
        self.assertEqual(cleanup[0], "/usr/bin/podman")
        self.assertIn("rm", cleanup)
        self.assertIn("--force", cleanup)
        self.assertIn("ci-burst-export", cleanup)

    def test_publishes_no_port_and_has_no_daemon_flags(self):
        self.assertNotIn("--publish", self.exec_start)
        self.assertNotIn("--detach", self.exec_start)
        self.assertNotIn("--restart", self.exec_start)


class TimerTests(unittest.TestCase):
    def test_each_timer_drives_its_own_unit_at_the_documented_interval(self):
        for name, service, interval in (
            ("ci-burst-apply.timer", "ci-burst-apply.service", "60s"),
            ("ci-burst-export.timer", "ci-burst-export.service", "60s"),
            ("ci-burst-reap.timer", "ci-burst-reap.service", "5min"),
        ):
            with self.subTest(timer=name):
                timer = unit(name)
                self.assertEqual(timer["Timer"]["unit"], service)
                self.assertEqual(timer["Timer"]["onunitinactivesec"], interval)
                self.assertEqual(timer["Install"]["wantedby"], "timers.target")

    def test_every_unit_has_a_timer(self):
        for service in ("ci-burst-apply", "ci-burst-export", "ci-burst-reap"):
            with self.subTest(service=service):
                self.assertTrue((DEPLOY / f"{service}.service").is_file())
                self.assertTrue((DEPLOY / f"{service}.timer").is_file())


class RefreshTests(unittest.TestCase):
    """Merged main is deployed by a poll-and-build; it must never publish or discard."""

    def setUp(self):
        self.service = unit("ci-burst-refresh.service")
        self.timer = unit("ci-burst-refresh.timer")
        self.script = (DEPLOY / "ci-burst-refresh.sh").read_text(encoding="utf-8")

    def test_is_a_one_shot_that_runs_the_installed_script(self):
        self.assertEqual(self.service["Service"]["type"], "oneshot")
        self.assertEqual(
            self.service["Service"]["execstart"], "/usr/local/sbin/ci-burst-refresh"
        )
        self.assertGreaterEqual(int(self.service["Service"]["timeoutstartsec"]), 900)

    def test_writes_deployment_state_after_every_run(self):
        self.assertEqual(
            self.service["Service"]["execstoppost"], "/usr/local/sbin/ci-burst-state"
        )
        self.assertIn('if [ -f "$STATE/hold" ]', self.script)
        self.assertIn('log "hold:', self.script)

    def test_timer_runs_at_boot_and_after_each_finished_run(self):
        self.assertEqual(self.timer["Timer"]["unit"], "ci-burst-refresh.service")
        self.assertEqual(self.timer["Timer"]["onbootsec"], "2min")
        self.assertEqual(self.timer["Timer"]["onunitinactivesec"], "5min")
        self.assertEqual(self.timer["Install"]["wantedby"], "timers.target")

    def test_compares_origin_main_before_building_anything(self):
        self.assertIn("fetch --quiet --depth=1 origin main", self.script)
        self.assertLess(
            self.script.index("deployed-sha"), self.script.index("podman build")
        )

    def test_builds_an_archive_of_the_exact_commit(self):
        # The build context is a tar archive of the fetched commit, so an edited
        # or untracked file in the clone can never enter the image.
        self.assertIn("archive --format=tar", self.script)
        self.assertIn('tar -xf "$work/tree.tar" -C "$work/src"', self.script)
        for avoid in ("reset --hard", "clean -", "push --force", 'git -C "$SOURCE" checkout'):
            with self.subTest(avoid=avoid):
                self.assertNotIn(avoid, self.script)

    def test_builds_with_host_networking_but_smokes_on_the_unit_network(self):
        # A container bridge MTU above the host path MTU stalls large downloads;
        # the build needs the network, the smoke must mirror the scheduled units.
        self.assertIn("podman build --quiet --network=host --tag", self.script)
        self.assertIn("SMOKE_NETWORK=${CI_BURST_SMOKE_NETWORK:-ci-burst}", self.script)
        smoke = self.script[self.script.index("set -- run --rm") :]
        smoke = smoke[: smoke.index("done\n")]
        self.assertNotIn("--network=host", smoke)
        self.assertIn('--network "$SMOKE_NETWORK"', smoke)

    def test_smoke_tests_the_candidate_before_promoting_it(self):
        smoke = self.script.index('"$SMOKE_COMMAND" --config')
        self.assertLess(self.script.index("podman build"), smoke)
        self.assertLess(smoke, self.script.index('"$IMAGE:latest"'))
        # A completed decision is required, not "any document": a config refusal
        # is exit 2 and must fail the smoke.
        self.assertIn('if [ "$attempt" -ge 2 ]', self.script)
        self.assertIn('document.get("decision") not in accepted', self.script)
        self.assertIn("--name ci-burst-refresh-smoke", self.script)

    def test_retries_the_smoke_once_and_only_once(self):
        self.assertIn('if [ "$attempt" -ge 2 ]', self.script)
        self.assertIn("smoke_retry", self.script)
        self.assertIn("CI_BURST_SMOKE_RETRY_SECONDS", self.script)

    def test_promotes_latest_and_both_rollback_handles(self):
        self.assertIn('"$IMAGE:sha-$short"', self.script)
        self.assertIn('"$IMAGE:previous"', self.script)
        self.assertIn("deployed-sha", self.script)

    def test_the_smoke_command_is_the_hosts_role_and_never_guessed(self):
        self.assertIn("SMOKE_COMMAND=${CI_BURST_SMOKE_COMMAND:-plan}", self.script)
        self.assertIn('plan) ACCEPTED_DECISIONS="wait would_launch"', self.script)
        self.assertIn('reap) ACCEPTED_DECISIONS="planned"', self.script)
        # An unknown command must refuse: guessing an accepted set for it is how
        # a role mismatch would promote an image that cannot do its job.
        self.assertIn("smoke_command_invalid", self.script)
        self.assertIn("did not print an accepted $SMOKE_COMMAND decision", self.script)

    def test_a_cleanup_host_needs_no_forgejo_token(self):
        # The reaper is deliberately outside the Forgejo credential boundary, so
        # the smoke must drop --env-file rather than require a file that host must
        # not have.
        self.assertIn('[ -n "$TOKEN_ENV" ] && [ -f "$TOKEN_ENV" ]', self.script)
        self.assertIn('--env-file "$TOKEN_ENV"', self.script)
        # No colon in the default: an explicitly empty value stays empty instead
        # of falling back to the controller's token file on a host that opted out.
        self.assertIn("TOKEN_ENV=${CI_BURST_TOKEN_ENV-/etc/ci-burst/forgejo.env}", self.script)
        self.assertNotIn("TOKEN_ENV=${CI_BURST_TOKEN_ENV:-", self.script)

    def test_a_cleanup_only_host_has_a_versioned_drop_in(self):
        # A systemd drop-in may repeat Environment=, so the text is read as-is;
        # an INI parser would keep only the last one.
        drop_in = (DEPLOY / "ci-burst-refresh-cleanup.conf").read_text(encoding="utf-8")
        for assignment in (
            "Environment=CI_BURST_SMOKE_COMMAND=reap",
            "Environment=CI_BURST_AWS_DIR=/etc/ci-burst/aws-cleanup",
            "Environment=CI_BURST_SMOKE_NETWORK=host",
            "Environment=CI_BURST_TOKEN_ENV=",
            # Quoted, because systemd splits an unquoted Environment= on spaces
            # and would silently compare only the first unit name.
            'Environment="CI_BURST_UNITS=ci-burst-reap.service ci-burst-reap.timer"',
        ):
            with self.subTest(assignment=assignment):
                self.assertIn(assignment, drop_in)
        self.assertIn("[Unit]", drop_in)
        self.assertNotIn("Description=", drop_in.split("[Service]", 1)[1])
        container = (ROOT / "docs" / "container.md").read_text(encoding="utf-8")
        self.assertIn("ci-burst-refresh-cleanup.conf", container)
        self.assertIn("CI_BURST_UNITS", container)
        # The smoke network must match the installed reap unit; the docs must say
        # so, because the shipped unit uses the named network.
        self.assertIn("The smoke network must match the installed reap unit", container)

    def test_a_host_without_podman_egress_has_a_versioned_reap_override(self):
        override = (DEPLOY / "ci-burst-reap-host-network.conf").read_text(encoding="utf-8")
        self.assertIn("ExecStart=", override)
        self.assertIn("--network=host", override)
        self.assertIn("reap --apply", override)
        container = (ROOT / "docs" / "container.md").read_text(encoding="utf-8")
        self.assertIn("ci-burst-reap-host-network.conf", container)

    def test_compares_the_scaler_units_and_schedules_nothing(self):
        units_line = next(
            line for line in self.script.splitlines() if line.startswith("UNITS=")
        )
        self.assertNotIn("reap", units_line)
        self.assertIn("CI_BURST_UNITS", units_line)
        self.assertIn("cmp -s", self.script)
        self.assertIn("units_drift", self.script)
        # The refresh loop reports drift; it never installs, reloads or restarts.
        self.assertNotIn("daemon-reload", self.script)
        self.assertNotIn("systemctl ", self.script)

    def test_holds_a_lock_so_two_runs_never_build_together(self):
        self.assertIn("flock -n 9", self.script)

    def test_carries_no_credential_of_its_own(self):
        for marker in ("aws_secret", "AWS_SECRET", "aws_access_key_id", "password"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.script)


class RefreshBehaviourTests(unittest.TestCase):
    """Run the script with fake `git` and `podman`, and no host state.

    The static assertions above pin the shape; these pin the behaviour a
    promotion must have or refuse.
    """

    want = "0123456789abcdef0123456789abcdef01234567"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ci-burst-refresh-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls.log"
        self.calls.touch()
        self.state = self.root / "state"
        self.units = self.root / "units"
        self.units.mkdir()
        for name in ("source", "aws"):
            (self.root / name).mkdir()
        (self.root / "deployment.toml").write_text("", encoding="utf-8")
        (self.root / "forgejo.env").write_text("", encoding="utf-8")
        self._fake(
            "git",
            'case "$*" in\n'
            '  *" rev-parse "*) printf "%s\\n" "$CI_BURST_TEST_WANT"; exit 0 ;;\n'
            '  *" archive "*) out= ; prev= ;\n'
            '    for a in "$@"; do [ "$prev" = "--output" ] && out=$a; prev=$a; done\n'
            '    tar -cf "$out" -C "$CI_BURST_TEST_REPO" Dockerfile deploy; exit 0 ;;\n'
            "  *) exit 0 ;;\n"
            "esac",
        )
        self._fake(
            "podman",
            'case "$*" in\n'
            '  "run "*" --config "*) printf "%s" "$CI_BURST_TEST_SMOKE"\n'
            '    if [ -n "${CI_BURST_TEST_SMOKE_SEQUENCE:-}" ]; then\n'
            '      n=$(( $(cat "$CI_BURST_TEST_SMOKE_SEQUENCE.count" 2>/dev/null || echo 0) + 1 ))\n'
            '      printf "%s" "$n" > "$CI_BURST_TEST_SMOKE_SEQUENCE.count"\n'
            '      exit "$(sed -n "${n}p" "$CI_BURST_TEST_SMOKE_SEQUENCE")"\n'
            '    fi\n'
            '    exit "$CI_BURST_TEST_SMOKE_RC" ;;\n'
            '  "build "*) ctx= ; for a in "$@"; do ctx=$a; done\n'
            '    { find "$ctx" -type f | sort; echo marker:; cat "$ctx/ci_burst/marker.py" 2>/dev/null; } '
            '> "$CI_BURST_TEST_CONTEXT"; exit 0 ;;\n'
            '  "image exists "*) exit "${CI_BURST_TEST_EXISTS_RC:-0}" ;;\n'
            '  "image inspect "*"latest"*) [ "${CI_BURST_TEST_INSPECT_RC:-0}" = "0" ] '
            '|| exit "${CI_BURST_TEST_INSPECT_RC}"\n'
            '    printf "%s\\n" sha256:old; exit 0 ;;\n'
            '  "image inspect "*) printf "%s\\n" sha256:new; exit 0 ;;\n'
            '  "tag "*) case "$*" in *"previous"*) exit "${CI_BURST_TEST_PREVIOUS_RC:-0}" ;; '
            "*) exit 0 ;; esac ;;\n"
            "  *) exit 0 ;;\n"
            "esac",
        )

    def _fake(self, name, body):
        path = self.bin / name
        path.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >>{shlex.quote(str(self.calls))}\n" + body + "\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def run_script(self, smoke='{"decision": "wait"}', smoke_rc="0", **extra):
        environment = {
            "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
            "CI_BURST_SOURCE": str(self.root / "source"),
            "CI_BURST_IMAGE_STATE": str(self.state),
            "CI_BURST_CONFIG": str(self.root / "deployment.toml"),
            "CI_BURST_AWS_DIR": str(self.root / "aws"),
            "CI_BURST_TOKEN_ENV": str(self.root / "forgejo.env"),
            "CI_BURST_TEST_WANT": self.want,
            "CI_BURST_TEST_SMOKE": smoke,
            "CI_BURST_TEST_SMOKE_RC": smoke_rc,
            "CI_BURST_TEST_CONTEXT": str(self.root / "context.txt"),
            "CI_BURST_TEST_REPO": str(ROOT),
            "CI_BURST_SMOKE_RETRY_SECONDS": "0",
            "CI_BURST_UNIT_DIR": str(self.units),
        }
        environment.update({name: str(value) for name, value in extra.items()})
        return subprocess.run(
            ["sh", str(DEPLOY / "ci-burst-refresh.sh")],
            env=environment,
            capture_output=True,
            text=True,
        )

    def test_a_refusal_document_never_promotes(self):
        completed = self.run_script(
            smoke='{"error": {"reason": "CONFIG_INVALID"}}', smoke_rc="2"
        )
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("latest", self.calls.read_text(encoding="utf-8"))
        self.assertFalse((self.state / "deployed-sha").exists())

    def test_a_document_without_a_decision_never_promotes(self):
        completed = self.run_script(smoke='{"other": true}')
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse((self.state / "deployed-sha").exists())

    def test_a_refusal_decision_at_zero_exit_never_promotes(self):
        # The gate may not depend on the CLI's exit code alone: a future CLI
        # regression could exit 0 with a refusal document.
        completed = self.run_script(
            smoke='{"decision": "refused", "rejection": {"reason": "CONFIG_INVALID"}}'
        )
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        calls = self.calls.read_text(encoding="utf-8")
        self.assertNotIn("latest", calls)
        # Exit zero is not retried: the document itself is the evidence.
        self.assertEqual(calls.count("plan --config"), 1)
        self.assertFalse((self.state / "deployed-sha").exists())

    def test_a_non_object_document_never_promotes(self):
        completed = self.run_script(smoke='["decision"]')
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse((self.state / "deployed-sha").exists())

    def test_a_cleanup_host_smoke_runs_reap_without_apply_and_promotes(self):
        completed = self.run_script(
            smoke='{"decision": "planned"}',
            CI_BURST_SMOKE_COMMAND="reap",
            CI_BURST_TOKEN_ENV="",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn("reap --config", calls)
        self.assertNotIn("plan --config", calls)
        self.assertNotIn("--apply", calls)
        # The reaper host must not need a Forgejo token file.
        self.assertNotIn("--env-file", calls)
        self.assertTrue((self.state / "deployed-sha").exists())

    def test_a_cleanup_host_refuses_a_plan_decision(self):
        # The per-role accepted set is what stops a wrong-command smoke from
        # promoting: `wait` is a plan decision and cannot prove a reaper image.
        completed = self.run_script(
            smoke='{"decision": "wait"}',
            CI_BURST_SMOKE_COMMAND="reap",
            CI_BURST_TOKEN_ENV="",
        )
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        self.assertFalse((self.state / "deployed-sha").exists())

    def test_an_unknown_smoke_command_refuses_before_building(self):
        completed = self.run_script(CI_BURST_SMOKE_COMMAND="status")
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("build", self.calls.read_text(encoding="utf-8"))
        self.assertFalse((self.state / "deployed-sha").exists())

    def test_an_inspection_failure_never_moves_latest(self):
        # "Cannot inspect the serving image" is not "no serving image": without a
        # confirmed handle the promotion must not happen.
        completed = self.run_script(CI_BURST_TEST_INSPECT_RC="1")
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn(
            "tag localhost/ci-burst:candidate localhost/ci-burst:latest",
            self.calls.read_text(encoding="utf-8"),
        )
        self.assertFalse((self.state / "deployed-sha").exists())

    def test_an_existence_check_error_never_moves_latest(self):
        # `podman image exists` uses 1 for absent and 125 for a storage error;
        # only 1 may skip the rollback handle.
        completed = self.run_script(CI_BURST_TEST_EXISTS_RC="125")
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn(
            "tag localhost/ci-burst:candidate localhost/ci-burst:latest",
            self.calls.read_text(encoding="utf-8"),
        )
        self.assertFalse((self.state / "deployed-sha").exists())

    def test_a_confirmed_absent_serving_image_still_promotes(self):
        completed = self.run_script(CI_BURST_TEST_EXISTS_RC="1")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.calls.read_text(encoding="utf-8")
        self.assertNotIn("localhost/ci-burst:previous", calls)
        self.assertIn("tag localhost/ci-burst:candidate localhost/ci-burst:latest", calls)
        self.assertEqual(
            (self.state / "deployed-sha").read_text(encoding="utf-8").strip(), self.want
        )

    def test_a_missing_previous_handle_never_moves_latest(self):
        completed = self.run_script(CI_BURST_TEST_PREVIOUS_RC="1")
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn("tag sha256:old localhost/ci-burst:previous", calls)
        self.assertNotIn("tag localhost/ci-burst:candidate localhost/ci-burst:latest", calls)

    def test_builds_only_committed_files_at_the_fetched_commit(self):
        # Real git, fake podman: the build context is the fetched commit's
        # archive, so a dirty or untracked file in the clone cannot be deployed.
        repository = self.root / "source"
        (repository / "ci_burst").mkdir()
        marker = repository / "ci_burst" / "marker.py"
        marker.write_text("committed\n", encoding="utf-8")
        (repository / "Dockerfile").write_text(
            (ROOT / "Dockerfile").read_text(encoding="utf-8"), encoding="utf-8"
        )
        self._git(repository, "init", "--quiet", "--initial-branch=main")
        self._git(repository, "add", ".")
        self._git(
            repository,
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "seed",
        )
        self._git(repository, "remote", "add", "origin", str(repository))
        marker.write_text("dirty\n", encoding="utf-8")
        (repository / "ci_burst" / "untracked.py").write_text("untracked\n", encoding="utf-8")
        (self.bin / "git").unlink()

        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        context = (self.root / "context.txt").read_text(encoding="utf-8")
        self.assertIn("ci_burst/marker.py", context)
        self.assertIn("committed", context)
        self.assertNotIn("untracked.py", context)
        self.assertNotIn("dirty", context)

    @staticmethod
    def _git(repository, *args):
        subprocess.run(
            ["git", *args], cwd=repository, check=True, capture_output=True, text=True
        )

    def test_a_promotion_records_the_commit_and_both_rollback_handles(self):
        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            (self.state / "deployed-sha").read_text(encoding="utf-8").strip(), self.want
        )
        self.assertIn(
            f"tag localhost/ci-burst:candidate localhost/ci-burst:sha-{self.want[:12]}",
            calls,
        )
        self.assertLess(
            calls.index("tag sha256:old localhost/ci-burst:previous"),
            calls.index("tag localhost/ci-burst:candidate localhost/ci-burst:latest"),
        )

    def test_a_transient_smoke_failure_is_retried_once(self):
        sequence = self.root / "smoke-sequence"
        sequence.write_text("1\n0\n", encoding="utf-8")
        completed = self.run_script(CI_BURST_TEST_SMOKE_SEQUENCE=str(sequence))
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("smoke_retry", completed.stdout)
        self.assertEqual(
            (self.state / "deployed-sha").read_text(encoding="utf-8").strip(), self.want
        )

    def test_a_repeated_smoke_failure_keeps_the_previous_image(self):
        sequence = self.root / "smoke-sequence"
        sequence.write_text("1\n1\n", encoding="utf-8")
        completed = self.run_script(CI_BURST_TEST_SMOKE_SEQUENCE=str(sequence))
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            self.calls.read_text(encoding="utf-8").count("plan --config"), 2
        )
        self.assertFalse((self.state / "deployed-sha").exists())

    def _installed_unit(self, unit, content=None):
        source = ROOT / "deploy" / unit
        target = self.units / unit
        target.write_text(
            content if content is not None else source.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    def test_reports_drift_between_the_repository_and_the_installed_units(self):
        self._installed_unit("ci-burst-apply.service", "older unit\n")
        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("units_drift:", completed.stdout)
        self.assertIn("ci-burst-apply.service", completed.stdout)
        self.assertIn("units_not_installed:", completed.stdout)

    def test_reports_units_current_when_they_match(self):
        for unit in ("ci-burst-apply.service", "ci-burst-apply.timer",
                     "ci-burst-refresh.service", "ci-burst-refresh.timer"):
            self._installed_unit(unit)
        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("units_current", completed.stdout)
        self.assertNotIn("units_drift", completed.stdout)

    def test_never_writes_a_unit(self):
        self._installed_unit("ci-burst-apply.service", "older unit\n")
        before = {path.name: path.read_text(encoding="utf-8") for path in self.units.iterdir()}
        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        after = {path.name: path.read_text(encoding="utf-8") for path in self.units.iterdir()}
        self.assertEqual(before, after)
        self.assertNotIn("ci-burst-reap.service", after)
        # A drift report is a report: nothing is reloaded or restarted.
        self.assertNotIn("daemon-reload", self.calls.read_text(encoding="utf-8"))

    def test_an_unchanged_commit_builds_nothing(self):
        self.state.mkdir(parents=True)
        (self.state / "deployed-sha").write_text(self.want + "\n", encoding="utf-8")
        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("build", self.calls.read_text(encoding="utf-8"))


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_the_guides_create_the_network_and_use_it(self):
        container = (ROOT / "docs/container.md").read_text(encoding="utf-8")
        walkthrough = (ROOT / "docs/deployment.md").read_text(encoding="utf-8")
        create = "podman network create --opt mtu=1300 ci-burst"
        self.assertIn("### The container network", container)
        self.assertIn(create, container)
        # Every install block in the end-to-end walkthrough creates it too, on
        # the scaler and on the reaper host.
        self.assertEqual(walkthrough.count(create), 2)
        # A manual run or build that omits the network reproduces the stall this
        # change fixes, so both guides' commands name it.
        for name, guide in (("container.md", container), ("deployment.md", walkthrough)):
            with self.subTest(guide=name):
                self.assertIn("podman run --rm --init --network ci-burst --name", guide)
                self.assertIn("podman build --network=host --tag localhost/ci-burst:latest", guide)

    def test_uses_the_python_slim_base_and_installs_the_package(self):
        self.assertIn("FROM python:3.12-slim", self.dockerfile)
        self.assertIn("pip install --no-cache-dir .", self.dockerfile)

    def test_carries_the_license_into_the_image(self):
        self.assertRegex(self.dockerfile, r"(?m)^COPY .*\bLICENSE\b")

    def test_runs_as_a_non_root_one_shot_command(self):
        self.assertIn("USER 10001:10001", self.dockerfile)
        self.assertIn('ENTRYPOINT ["ci-burst"]', self.dockerfile)

    def test_has_no_daemon_surface(self):
        for directive in ("EXPOSE", "HEALTHCHECK", "CMD"):
            with self.subTest(directive=directive):
                self.assertIsNone(
                    re.search(rf"(?m)^{directive}\b", self.dockerfile),
                    f"{directive} is not part of the one-shot image",
                )

    def test_uses_mounted_file_credentials_without_metadata_fallback(self):
        self.assertIn("AWS_EC2_METADATA_DISABLED=true", self.dockerfile)
        self.assertIn("AWS_SHARED_CREDENTIALS_FILE=/etc/ci-burst/aws/credentials", self.dockerfile)
        self.assertIn("AWS_CONFIG_FILE=/etc/ci-burst/aws/config", self.dockerfile)


class ContainerImageCITests(unittest.TestCase):
    """The committed job text: its own timeout and the exact build command.

    Workflow semantics — credential references, permissions, registry use and
    the effective working directory — are pinned in
    ``tests/workflow-policy.test.mjs`` with the pinned ``yaml`` parser. This
    test keeps only the narrow text shape the Python suite can state without a
    second workflow reader.
    """

    TEXT = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    def job_body(self):
        match = re.search(r"(?ms)^  container-image:\n(.*?)(?=^  \S|\Z)", self.TEXT)
        self.assertIsNotNone(match, "container-image must be a job in ci.yml")
        return match.group(1)

    def test_the_job_exists_with_its_own_name_and_timeout(self):
        body = self.job_body()
        self.assertRegex(body, r"(?m)^    name: .*image.*builds?$")
        timeout = re.search(r"(?m)^    timeout-minutes: (\d+)$", body)
        self.assertIsNotNone(timeout, "the job needs its own timeout")
        self.assertLessEqual(int(timeout.group(1)), 10)

    def test_it_runs_the_exact_build_command(self):
        commands = re.findall(r"(?m)^        run: (.+)$", self.job_body())
        self.assertEqual(commands, ["docker build --tag ci-burst:ci ."])

    def test_the_workflow_grants_only_the_read_token(self):
        grants = re.search(r"(?m)^permissions:\n((?:  [^\n]*\n)+)", self.TEXT)
        self.assertIsNotNone(grants, "the workflow needs a top-level permissions block")
        self.assertEqual(grants.group(1).strip(), "contents: read")
        for forbidden in ("packages:", "id-token:", "contents: write"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.TEXT)


def _dockerignore_rules():
    rules = []
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        rules.append((line[1:] if negated else line, negated))
    return rules


def _matches(pattern, path):
    """Match one .dockerignore pattern against a context-relative path.

    Docker uses an ordered, last-match-wins list. A pattern without a slash
    matches the base name at any depth; a pattern with slashes is anchored to
    the context root, where ``**`` spans directories and ``*`` does not.
    """
    if "/" not in pattern:
        return fnmatch.fnmatchcase(path.rsplit("/", 1)[-1], pattern)
    return _match_parts(pattern.split("/"), path.split("/"))


def _match_parts(parts, path_parts):
    if not parts:
        return not path_parts
    if parts[0] == "**":
        rest = parts[1:]
        return any(_match_parts(rest, path_parts[index:]) for index in range(len(path_parts) + 1))
    if not path_parts or not fnmatch.fnmatchcase(path_parts[0], parts[0]):
        return False
    return _match_parts(parts[1:], path_parts[1:])


def _excluded(path):
    excluded = False
    for pattern, negated in _dockerignore_rules():
        if _matches(pattern, path):
            excluded = not negated
    return excluded


def admitted(path):
    """Return whether Docker would put this path into the build context."""
    parts = path.strip("/").split("/")
    for index in range(1, len(parts)):
        if _excluded("/".join(parts[:index])):
            return False
    return not _excluded(path)


class BuildContextTests(unittest.TestCase):
    ALLOWED = (
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "ci_burst/__init__.py",
        "ci_burst/sub/mod.py",
    )

    FORBIDDEN = (
        ".env",
        ".env.local",
        ".git/config",
        ".dockerignore",
        "Dockerfile",
        "aws/credentials",
        "state.sqlite3",
        "state.sqlite3-journal",
        "state.sqlite3.lock",
        "tests/test_cli.py",
        "docs/container.md",
        "deploy/ci-burst-apply.service",
        ".github/workflows/ci.yml",
        "build/lib/ci_burst/cli.py",
        "node_modules/package/index.js",
        "ci_burst/cli.pyc",
        "ci_burst/__pycache__/cli.cpython-312.pyc",
    )

    def test_admits_only_the_build_inputs(self):
        for path in self.ALLOWED:
            with self.subTest(path=path):
                self.assertTrue(admitted(path), f"{path} must enter the build context")

    def test_rejects_synthetic_forbidden_paths(self):
        for path in self.FORBIDDEN:
            with self.subTest(path=path):
                self.assertFalse(admitted(path), f"{path} must not enter the build context")


if __name__ == "__main__":
    unittest.main()
