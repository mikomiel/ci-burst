"""Live lifecycle test for the systemd container cleanup.

This test is opt-in because it starts real containers and transient system
services. Run it on the controller host with::

    CI_BURST_RUN_CONTAINER_TESTS=1 \\
        python3 -m unittest discover -s tests -p 'test_container_lifecycle.py' -v

It requires root, a booted systemd and podman, plus a local image that has
``/bin/sh`` and ``sleep`` (the documented ``localhost/ci-burst:latest`` build
does; override with ``CI_BURST_TEST_IMAGE``). It is skipped in the
credential-free CI job, which has no such host.

The test starts a transient oneshot unit modelled on
``deploy/ci-burst-apply.service``: a long-running container, the unit's own
``ExecStopPost`` command as cleanup, and ``KillMode=process``. It then:

* stops the unit while it is still activating, and requires the named container
  to be gone afterwards (an inactive or failed unit alone proves nothing);
* lets ``TimeoutStartSec`` expire, and requires the same;
* starts another invocation under the same container name, which proves the
  name and the cleanup path are reusable.

The test never reads a Forgejo token, AWS credentials or deployment state. The
artifact contract itself is covered by ``tests/test_deploy_units.py``.
"""

import configparser
import os
import shlex
import shutil
import subprocess
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "ci-burst-apply.service"
IMAGE = os.environ.get("CI_BURST_TEST_IMAGE", "localhost/ci-burst:latest")

CONTAINER = f"ci-burst-lifecycle-{os.getpid()}"
UNIT_PREFIX = f"ci-burst-lifecycle-{os.getpid()}"
LONG_RUNNING = ["-c", "sleep 600"]


def _systemd_available():
    return (
        Path("/run/systemd/system").is_dir()
        and shutil.which("systemctl") is not None
        and shutil.which("systemd-run") is not None
    )


@unittest.skipUnless(
    os.environ.get("CI_BURST_RUN_CONTAINER_TESTS") == "1",
    "set CI_BURST_RUN_CONTAINER_TESTS=1 to run the container lifecycle test",
)
@unittest.skipUnless(os.geteuid() == 0, "needs root for podman and transient system services")
@unittest.skipUnless(_systemd_available(), "needs a booted systemd with systemd-run")
class ContainerLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.podman = shutil.which("podman")
        if cls.podman is None:
            raise unittest.SkipTest("podman is not installed")

        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read(UNIT, encoding="utf-8")
        cleanup = shlex.split(parser["Service"]["execstoppost"].lstrip("-").strip())
        cleanup[0] = cls.podman
        cls.stop_post = [CONTAINER if token == "ci-burst-apply" else token for token in cleanup]
        assert "--force" in cls.stop_post and "--ignore" in cls.stop_post, cls.stop_post

        exists = subprocess.run([cls.podman, "image", "exists", IMAGE], capture_output=True)
        if exists.returncode != 0:
            raise unittest.SkipTest(
                f"local image {IMAGE} is required; build it as docs/container.md describes"
            )

    def _podman(self, *arguments):
        return subprocess.run([self.podman, *arguments], capture_output=True, text=True)

    def _container_exists(self):
        return self._podman("container", "exists", CONTAINER).returncode == 0

    def _unit_state(self, unit):
        return subprocess.run(
            ["systemctl", "is-active", unit], capture_output=True, text=True
        ).stdout.strip()

    def _wait(self, predicate, timeout, description):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.25)
        self.fail(f"timed out waiting for {description}")

    def _cleanup(self, unit):
        subprocess.run(["systemctl", "stop", unit], capture_output=True)
        subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)
        self._podman("rm", "--force", "--ignore", CONTAINER)

    def _start(self, unit, timeout_start):
        self._podman("rm", "--force", "--ignore", CONTAINER)
        subprocess.run(["systemctl", "reset-failed", unit], capture_output=True)
        command = [
            self.podman,
            "run",
            "--rm",
            "--replace",
            "--init",
            "--name",
            CONTAINER,
            "--entrypoint",
            "/bin/sh",
            IMAGE,
            *LONG_RUNNING,
        ]
        started = subprocess.run(
            [
                "systemd-run",
                "--no-block",
                f"--unit={unit}",
                "--service-type=oneshot",
                "--property=KillMode=process",
                f"--property=TimeoutStartSec={timeout_start}",
                "--property=TimeoutStopSec=30",
                "--property=ExecStopPost=" + shlex.join(self.stop_post),
                "--description=ci-burst container lifecycle test",
                *command,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        self.addCleanup(self._cleanup, unit)

    def test_stopping_an_activating_unit_removes_its_container(self):
        unit = f"{UNIT_PREFIX}-stop"
        self._start(unit, 300)
        self._wait(self._container_exists, 60, "the long-running container to start")

        stopped = subprocess.run(["systemctl", "stop", unit], capture_output=True, text=True)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)

        self._wait(lambda: not self._container_exists(), 30, "the container to be removed after stop")
        self.assertIn(self._unit_state(unit), ("inactive", "failed"))

    def test_start_timeout_removes_the_container_and_the_name_is_reusable(self):
        unit = f"{UNIT_PREFIX}-timeout"
        self._start(unit, 15)
        self._wait(self._container_exists, 10, "the long-running container to start")
        self._wait(lambda: self._unit_state(unit) == "failed", 30, "the unit to hit TimeoutStartSec")
        self._wait(lambda: not self._container_exists(), 30, "the container to be removed after the start timeout")

        restart = f"{UNIT_PREFIX}-restart"
        self._start(restart, 300)
        self._wait(self._container_exists, 60, "a second invocation under the same container name")
        subprocess.run(["systemctl", "stop", restart], capture_output=True)
        self._wait(lambda: not self._container_exists(), 30, "the second container to be removed after stop")


if __name__ == "__main__":
    unittest.main()
