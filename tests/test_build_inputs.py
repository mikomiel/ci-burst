"""Contract tests for recording a built artefact's inputs.

The controller image is built on a host from a source commit, so the commit
identifies the source but not the bytes: the base image and the resolved
Python dependencies are inputs of their own. These tests guard the properties
that make a build record useful and keep them from being silently removed:

* the base image is pinned by digest, not by a moving tag;
* the refresh script writes one record per build, atomically, with the source
  SHA, tree SHA, Dockerfile hash, ``FROM`` line, base digest, image ID, engine
  version, resolved Python distributions and a timestamp, before it promotes;
* a source SHA that cannot be resolved aborts before anything is built or
  recorded, and a record that cannot be produced never promotes;
* the worker recipe records its own inputs inside the image before it prints
  ``IMAGE_READY``, and fails the build when it cannot.

The behavioural tests use fake ``git`` and ``podman`` binaries in a temporary
directory, following ``tests/test_deploy_units.py``. They never start podman,
build an image, reach a registry or need a credential.
"""

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
REFRESH = DEPLOY / "ci-burst-refresh.sh"
PROVISION = DEPLOY / "runner" / "image" / "provision.sh"
CONTAINER_DOC = ROOT / "docs" / "container.md"
WORKER_README = DEPLOY / "runner" / "image" / "README.md"

SOURCE_SHA = "0123456789abcdef0123456789abcdef01234567"
TREE_SHA = "89abcdef0123456789abcdef0123456789abcdef"
BASE_DIGEST = "sha256:" + "5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
DOCKERFILE = f"FROM python:3.12-slim@{BASE_DIGEST}\n"

RECORD_KEYS = {
    "schema",
    "source_sha",
    "tree_sha",
    "dockerfile_sha256",
    "from",
    "base_digest",
    "image_id",
    "engine",
    "python_distributions",
    "created_at",
}

DISTRIBUTIONS = ["boto3==1.40.0", "botocore==1.40.0", "ci-burst==0.3.0"]


class BaseImagePinTests(unittest.TestCase):
    def setUp(self):
        self.dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_the_base_image_is_pinned_by_digest(self):
        # A moving tag is the defect: the same source would build against
        # different base bytes after an upstream tag update.
        match = re.search(
            r"(?m)^FROM python:3\.12-slim@(sha256:[0-9a-f]{64})$",
            self.dockerfile,
        )
        self.assertIsNotNone(
            match, "the controller base image must be pinned by manifest digest"
        )

    def test_every_from_line_is_pinned_by_digest(self):
        froms = re.findall(r"(?m)^FROM\s+(\S+)", self.dockerfile)
        self.assertTrue(froms)
        for reference in froms:
            with self.subTest(reference=reference):
                self.assertRegex(reference, r"@sha256:[0-9a-f]{64}$")


class RefreshBuildRecordShapeTests(unittest.TestCase):
    """The committed script text: record location, ordering and fields."""

    def setUp(self):
        self.script = REFRESH.read_text(encoding="utf-8")

    def test_writes_the_record_under_the_state_directory(self):
        self.assertIn('mkdir -p "$STATE/builds"', self.script)
        self.assertIn('record="$STATE/builds/sha-$short.json"', self.script)
        self.assertIn('log "build_inputs: $record"', self.script)

    def test_writes_atomically_through_a_hidden_temp_file(self):
        # A visible partial record would misreport a build that is still
        # running; the rename must stay inside the same directory.
        self.assertIn(
            'record_tmp=$(mktemp "$STATE/builds/.sha-$short.XXXXXX")', self.script
        )
        self.assertIn('mv -f "$record_tmp" "$record"', self.script)
        self.assertLess(
            self.script.index('record_tmp=$(mktemp "$STATE/builds/.sha-$short.XXXXXX")'),
            self.script.index('mv -f "$record_tmp" "$record"'),
        )

    def test_records_the_required_inputs(self):
        for key in sorted(RECORD_KEYS):
            with self.subTest(key=key):
                self.assertIn(f'"{key}"', self.script)
        for marker in (
            'rev-parse "$want^{tree}"',
            'sha256sum "$work/src/Dockerfile"',
            'podman --version',
            "--network=none",
            "-m pip list --format=freeze",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.script)

    def test_writes_the_record_after_the_build_and_before_promotion(self):
        record = self.script.index('log "build_inputs: $record"')
        self.assertLess(self.script.index("podman build"), record)
        self.assertLess(record, self.script.index('"$IMAGE:sha-$short"'))
        self.assertLess(record, self.script.index('"$IMAGE:latest"'))

    def test_a_record_failure_is_a_named_refusal(self):
        self.assertIn('log "build_inputs_failed:', self.script)


class RefreshBuildRecordBehaviourTests(unittest.TestCase):
    """Run the script with fake git and podman; no host state is touched."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ci-burst-build-inputs-"))
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
        # The fetched commit's tree, as far as this script can see it.
        self.context = self.root / "context"
        self.context.mkdir()
        self.dockerfile_bytes = DOCKERFILE.encode("utf-8")
        (self.context / "Dockerfile").write_bytes(self.dockerfile_bytes)
        self._fake_git()
        self._fake_podman()

    def _fake(self, name, body):
        path = self.bin / name
        path.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >>{shlex.quote(str(self.calls))}\n" + body + "\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def _fake_git(self):
        self._fake(
            "git",
            'case "$*" in\n'
            '  *" rev-parse "*)\n'
            '    [ "${CI_BURST_TEST_REV_PARSE_RC:-0}" = "0" ] || exit "${CI_BURST_TEST_REV_PARSE_RC}"\n'
            '    case "$*" in\n'
            '      *"^{tree}"*) printf "%s\\n" "$CI_BURST_TEST_TREE" ;;\n'
            '      *) printf "%s\\n" "$CI_BURST_TEST_WANT" ;;\n'
            "    esac\n"
            "    exit 0 ;;\n"
            '  *" archive "*)\n'
            "    out= ; prev= ;\n"
            '    for a in "$@"; do [ "$prev" = "--output" ] && out=$a; prev=$a; done\n'
            '    tar -cf "$out" -C "$CI_BURST_TEST_CONTEXT" .; exit 0 ;;\n'
            "  *) exit 0 ;;\n"
            "esac",
        )

    def _fake_podman(self):
        self._fake(
            "podman",
            'case "$*" in\n'
            "  --version) printf 'podman version 5.4.2\\n'; exit 0 ;;\n"
            '  "build "*) exit 0 ;;\n'
            '  "run "*" --config "*) printf "%s" "$CI_BURST_TEST_SMOKE"; exit 0 ;;\n'
            '  "run "*"--entrypoint python"*)\n'
            '    printf "%s\\n" "$CI_BURST_TEST_DISTRIBUTIONS"\n'
            '    exit "${CI_BURST_TEST_DIST_RC:-0}" ;;\n'
            '  "image inspect "*"latest"*) printf "sha256:old\\n"; exit 0 ;;\n'
            '  "image inspect "*) printf "sha256:new\\n"; exit 0 ;;\n'
            '  "image exists "*) exit 0 ;;\n'
            '  "tag "*) exit 0 ;;\n'
            '  "rmi "*) exit 0 ;;\n'
            "  *) exit 0 ;;\n"
            "esac",
        )

    def run_script(self, **extra):
        environment = {
            "PATH": f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
            "CI_BURST_SOURCE": str(self.root / "source"),
            "CI_BURST_IMAGE_STATE": str(self.state),
            "CI_BURST_CONFIG": str(self.root / "deployment.toml"),
            "CI_BURST_AWS_DIR": str(self.root / "aws"),
            "CI_BURST_TOKEN_ENV": str(self.root / "forgejo.env"),
            "CI_BURST_UNIT_DIR": str(self.units),
            "CI_BURST_SMOKE_RETRY_SECONDS": "0",
            "CI_BURST_TEST_WANT": SOURCE_SHA,
            "CI_BURST_TEST_TREE": TREE_SHA,
            "CI_BURST_TEST_CONTEXT": str(self.context),
            "CI_BURST_TEST_SMOKE": '{"decision": "wait"}',
            "CI_BURST_TEST_DISTRIBUTIONS": "\n".join(DISTRIBUTIONS),
        }
        environment.update({name: str(value) for name, value in extra.items()})
        return subprocess.run(
            ["sh", str(REFRESH)],
            env=environment,
            capture_output=True,
            text=True,
        )

    def record_path(self):
        return self.state / "builds" / f"sha-{SOURCE_SHA[:12]}.json"

    def test_records_exactly_one_complete_record_per_build(self):
        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        records = sorted(path.name for path in (self.state / "builds").iterdir())
        self.assertEqual(records, [self.record_path().name])
        document = json.loads(self.record_path().read_text(encoding="utf-8"))
        self.assertEqual(set(document), RECORD_KEYS)
        self.assertEqual(document["schema"], "ci-burst.build-inputs.v1")
        self.assertEqual(document["source_sha"], SOURCE_SHA)
        self.assertEqual(document["tree_sha"], TREE_SHA)
        self.assertEqual(
            document["dockerfile_sha256"],
            hashlib.sha256(self.dockerfile_bytes).hexdigest(),
        )
        self.assertEqual(document["from"], f"FROM python:3.12-slim@{BASE_DIGEST}")
        self.assertEqual(document["base_digest"], BASE_DIGEST)
        self.assertEqual(document["image_id"], "sha256:new")
        self.assertEqual(document["engine"], "podman version 5.4.2")
        self.assertEqual(document["python_distributions"], DISTRIBUTIONS)
        self.assertRegex(
            document["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"
        )
        log_lines = [
            line
            for line in completed.stdout.splitlines()
            if line.startswith("ci-burst-refresh: build_inputs: ")
        ]
        self.assertEqual(log_lines, [f"ci-burst-refresh: build_inputs: {self.record_path()}"])

    def test_reads_distributions_without_network_after_build_and_before_promotion(self):
        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        calls = self.calls.read_text(encoding="utf-8").splitlines()
        distribution_calls = [call for call in calls if "--entrypoint python" in call]
        self.assertEqual(len(distribution_calls), 1, distribution_calls)
        self.assertIn("--network=none", distribution_calls[0])
        self.assertIn("pip list --format=freeze", distribution_calls[0])

        def index_of(predicate):
            return next(index for index, call in enumerate(calls) if predicate(call))

        built = index_of(lambda call: call.startswith("build "))
        read = index_of(lambda call: "--entrypoint python" in call)
        promoted = index_of(
            lambda call: call.startswith("tag ")
            and call.endswith(f"localhost/ci-burst:sha-{SOURCE_SHA[:12]}")
        )
        latest = index_of(
            lambda call: call.startswith("tag ")
            and call.endswith("localhost/ci-burst:latest")
        )
        self.assertLess(built, read)
        self.assertLess(read, promoted)
        self.assertLess(promoted, latest)

    def test_an_unresolvable_source_sha_aborts_before_building_or_recording(self):
        completed = self.run_script(CI_BURST_TEST_REV_PARSE_RC="128")
        self.assertEqual(completed.returncode, 2, completed.stderr)
        self.assertIn("resolve_failed", completed.stdout)
        self.assertFalse((self.state / "builds").exists())
        calls = self.calls.read_text(encoding="utf-8")
        self.assertNotIn("build ", calls)
        self.assertFalse((self.state / "deployed-sha").exists())

    def test_a_record_that_cannot_be_completed_never_promotes(self):
        completed = self.run_script(CI_BURST_TEST_DIST_RC="1")
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("build_inputs_failed", completed.stdout)
        self.assertFalse(self.record_path().exists())
        calls = self.calls.read_text(encoding="utf-8")
        self.assertNotIn("localhost/ci-burst:latest", calls)
        self.assertFalse((self.state / "deployed-sha").exists())


class WorkerBuildInputsTests(unittest.TestCase):
    """The worker recipe records its inputs before it announces the image."""

    def setUp(self):
        self.script = PROVISION.read_text(encoding="utf-8")
        self.ready = self.script.index('echo "IMAGE_READY"')
        self.publish = self.script.index('mv -f "$inputs_tmp" "$build_inputs"')

    def test_writes_the_file_inside_the_image_before_IMAGE_READY(self):
        self.assertIn("build_inputs=/etc/ci-burst/build-inputs.txt", self.script)
        self.assertLess(self.publish, self.ready)

    def test_records_versions_checksums_packages_and_the_script_hash(self):
        section = self.script[self.script.index("script_path=$0"): self.ready]
        for marker in (
            'sha256sum "$script_path"',
            "dpkg-query -W",
            "PRETTY_NAME",
            "dpkg --print-architecture",
            "RUFF_VERSION",
            "UV_VERSION",
            "NODE_VERSION",
            "RUNNER_VERSION",
            "PROMTOOL_VERSION",
            "sha256sum",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, section)

    def test_fails_the_build_when_it_cannot_write_the_file(self):
        # `set -e` is the mechanism: the write is a plain command, not a
        # suppressed one, and the temp file is removed by the exit trap.
        self.assertIn("set -euo pipefail", self.script)
        section = self.script[self.script.index("script_path=$0"): self.ready]
        self.assertNotIn("|| true", section)
        self.assertNotIn("if ! ", section)
        self.assertIn('rm -f "$inputs_tmp"', self.script)


class DocumentationTests(unittest.TestCase):
    def test_container_guide_states_the_three_identities(self):
        text = CONTAINER_DOC.read_text(encoding="utf-8")
        for marker in (
            "### Source identity and image identity",
            "release version",
            "deployed source SHA",
            "image ID",
            "build record",
            "builds/sha-",
            "not a relabel",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_worker_readme_names_the_file_and_the_operator_boundary(self):
        text = " ".join(WORKER_README.read_text(encoding="utf-8").split())
        for marker in (
            "/etc/ci-burst/build-inputs.txt",
            "Before `IMAGE_READY`",
            "Qualification remains operator-run",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)


if __name__ == "__main__":
    unittest.main()
