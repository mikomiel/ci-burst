"""Contract tests for the container deployment artifacts.

The image and the systemd units are configuration, not library code, so these
tests read the committed files instead of starting podman. They need no
credential and no network. The properties they guard are the ones a later edit
could silently remove:

* the apply unit is a one-shot, read-only run that can be stopped to quiesce an
  in-flight invocation;
* the cleanup unit is separate, has no Forgejo token and no ledger, and uses
  only the cleanup credential mount, so it stays runnable when admission stops;
* each timer drives its own unit at the documented interval;
* the image has no daemon surface, and the build context is default-deny.

The live lifecycle itself, including stopping an active invocation and keeping
cleanup running, is the operator procedure in ``docs/container.md``; it is not
exercised here.
"""

import configparser
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
IMAGE = "localhost/ci-burst:latest"


def unit(name):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(DEPLOY / name, encoding="utf-8")
    return parser


class ApplyUnitTests(unittest.TestCase):
    def setUp(self):
        self.service = unit("ci-burst-apply.service")
        self.exec_start = self.service["Service"]["execstart"]

    def test_is_one_shot_so_a_run_can_be_stopped(self):
        self.assertEqual(self.service["Service"]["type"], "oneshot")
        self.assertNotIn("restart", self.service["Service"])

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


class TimerTests(unittest.TestCase):
    def test_each_timer_drives_its_own_unit_at_the_documented_interval(self):
        for name, service, interval in (
            ("ci-burst-apply.timer", "ci-burst-apply.service", "60s"),
            ("ci-burst-reap.timer", "ci-burst-reap.service", "5min"),
        ):
            with self.subTest(timer=name):
                timer = unit(name)
                self.assertEqual(timer["Timer"]["unit"], service)
                self.assertEqual(timer["Timer"]["onunitinactivesec"], interval)
                self.assertEqual(timer["Install"]["wantedby"], "timers.target")

    def test_both_units_have_a_timer(self):
        for service in ("ci-burst-apply", "ci-burst-reap"):
            with self.subTest(service=service):
                self.assertTrue((DEPLOY / f"{service}.service").is_file())
                self.assertTrue((DEPLOY / f"{service}.timer").is_file())


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_uses_the_python_slim_base_and_installs_the_package(self):
        self.assertIn("FROM python:3.12-slim", self.dockerfile)
        self.assertIn("pip install --no-cache-dir .", self.dockerfile)

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


class BuildContextTests(unittest.TestCase):
    def test_context_is_default_deny_and_admits_the_build_inputs(self):
        lines = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        patterns = [
            line.strip()
            for line in lines
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(patterns[0], "*")
        for required in ("!pyproject.toml", "!README.md", "!ci_burst", "!ci_burst/**"):
            self.assertIn(required, patterns)


if __name__ == "__main__":
    unittest.main()
