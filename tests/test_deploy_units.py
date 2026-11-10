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
  context admits only the declared build inputs.

The live lifecycle itself, including stopping an active invocation and keeping
cleanup running, is exercised by ``tests/test_container_lifecycle.py`` and the
operator procedure in ``docs/container.md``.
"""

import configparser
import fnmatch
import re
import shlex
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

    def test_has_its_own_named_container_and_cleanup_command(self):
        self.assertIn("--name ci-burst-reap", self.exec_start)
        self.assertIn("--init", self.exec_start)
        cleanup = _cleanup_argv(self.service)
        self.assertEqual(cleanup[-1], "ci-burst-reap")
        self.assertIn("--force", cleanup)
        self.assertIn("--ignore", cleanup)


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
