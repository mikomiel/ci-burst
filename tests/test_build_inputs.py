"""Contract tests for recording a built artefact's inputs.

The controller image is built on a host from a source commit, so the commit
identifies the source but not the bytes: the base image and the resolved
Python dependencies are inputs of their own. These tests guard the properties
that make a build record useful and keep them from being silently removed:

* the base image is pinned by digest, not by a moving tag;
* the refresh script writes one authoritative record per image, keyed by image
  ID and published by rename, with the Dockerfile hash, ``FROM`` line, base
  digest, image ID and resolved Python distributions, plus a bounded list of
  attestations naming the source and tree SHA and the engine and timestamp of
  each build that resolved those bytes; a source-keyed reference names that
  record and is never the only copy of any inputs;
* one image ID may legitimately carry several attestations, but an image ID
  whose build inputs differ is a conflict that refuses promotion; a failed
  retry cannot replace the serving image's record, and a record that cannot be
  produced never promotes;
* a source SHA that cannot be resolved aborts before anything is built or
  recorded;
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

# Two builds of one source: the serving image A and a retry B that resolves
# different bytes. Image IDs are what podman reports, so the record path is
# derived from them.
IMAGE_A = "sha256:" + "a" * 64
IMAGE_B = "sha256:" + "b" * 64

# A second commit whose only changes are excluded from the build context: the
# tree differs, the image bytes do not.
OTHER_SOURCE_SHA = "fedcba9876543210fedcba9876543210fedcba98"
OTHER_TREE_SHA = "abcdef0123456789abcdef0123456789abcdef01"

RECORD_KEYS = {
    "schema",
    "image_id",
    "dockerfile_sha256",
    "from",
    "base_digest",
    "python_distributions",
    "attestations",
}

ATTESTATION_KEYS = {"source_sha", "tree_sha", "engine", "created_at"}

REFERENCE_KEYS = {"schema", "source_sha", "image_id", "record"}

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

    def test_keys_the_record_by_image_identity_under_the_state_directory(self):
        self.assertIn('records="$STATE/builds/images"', self.script)
        self.assertIn('mkdir -p "$records"', self.script)
        self.assertIn('record="$records/$image_key.json"', self.script)
        self.assertIn('log "build_inputs: $record"', self.script)

    def test_keeps_a_source_keyed_reference_separate_from_the_record(self):
        # The source SHA names a moving build of one source; it must never be
        # the only copy of the inputs, so it points at the image-keyed record.
        self.assertIn('reference="$STATE/builds/sha-$short.json"', self.script)
        self.assertIn('"schema": "ci-burst.build-inputs-ref.v1"', self.script)
        self.assertIn('"record": os.environ["RECORD"]', self.script)
        self.assertIn('log "build_inputs_ref: $reference"', self.script)

    def test_validates_the_image_identity_before_using_it_in_a_path(self):
        # A case pattern, not a line-based matcher: an embedded newline must
        # not be able to smuggle a separator into the record path.
        self.assertIn("sha256:*[!A-Za-z0-9._-]*)", self.script)
        self.assertIn('image_key=${candidate#sha256:}', self.script)
        self.assertIn("build_inputs_failed: unusable image identity", self.script)

    def test_refuses_a_conflicting_record_for_an_existing_image(self):
        self.assertIn('[ -e "$record" ]', self.script)
        self.assertIn('$record.conflict', self.script)
        self.assertIn("build_inputs_failed: image record conflict", self.script)

    def test_appends_an_attestation_for_a_second_source_of_one_image(self):
        # The image-keyed record is authoritative, so a second source that
        # resolves the same bytes appends to it rather than replacing it.
        self.assertIn('"schema": "ci-burst.build-inputs.v2"', self.script)
        self.assertIn('"attestations"', self.script)
        self.assertIn('log "build_inputs_attested: $record source=$want"', self.script)
        self.assertIn("merged[\"attestations\"] = attestations + [new]", self.script)

    def test_caps_the_attestation_list_and_refuses_at_the_cap(self):
        self.assertIn("CAP = 8", self.script)
        self.assertIn("len(attestations) >= CAP", self.script)
        self.assertIn("build_inputs_full:", self.script)

    def test_writes_atomically_through_hidden_temp_files(self):
        # A visible partial record would misreport a build that is still
        # running; each rename must stay inside its own directory.
        self.assertIn(
            'record_tmp=$(mktemp "$records/.$image_key.XXXXXX")', self.script
        )
        self.assertIn(
            "candidate_record=$(mktemp \"$records/.$image_key.candidate.XXXXXX\")",
            self.script,
        )
        self.assertIn(
            'reference_tmp=$(mktemp "$STATE/builds/.sha-$short.XXXXXX")', self.script
        )
        self.assertIn('mv -f "$record_tmp" "$record"', self.script)
        self.assertIn('mv -f "$reference_tmp" "$reference"', self.script)
        self.assertLess(
            self.script.index('record_tmp=$(mktemp "$records/.$image_key.XXXXXX")'),
            self.script.index('mv -f "$record_tmp" "$record"'),
        )

    def test_records_the_required_inputs(self):
        for key in sorted(RECORD_KEYS | ATTESTATION_KEYS):
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
        reference = self.script.index('log "build_inputs_ref: $reference"')
        self.assertLess(self.script.index("podman build"), record)
        self.assertLess(record, reference)
        self.assertLess(reference, self.script.index('"$IMAGE:sha-$short"'))
        self.assertLess(reference, self.script.index('"$IMAGE:latest"'))

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
        # Written by the fake `tag latest` on success, so a test can prove the
        # serving tag did or did not move.
        self.promoted = self.root / "promoted.log"
        self.promoted.touch()
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
        self.check_record = self.bin / "check_record.py"
        self.check_record.write_text(
            "import json\n"
            "import sys\n"
            "from pathlib import Path\n"
            "\n"
            "required = {\n"
            '    "schema", "image_id", "dockerfile_sha256", "from",\n'
            '    "base_digest", "python_distributions", "attestations",\n'
            "}\n"
            "builds, image_id = Path(sys.argv[1]), sys.argv[2]\n"
            "\n"
            "for path in builds.rglob('*.json'):\n"
            "    try:\n"
            "        document = json.loads(path.read_text(encoding='utf-8'))\n"
            "    except (OSError, ValueError):\n"
            "        continue\n"
            "    if isinstance(document, dict) and set(document) == required \\\n"
            "            and document.get('image_id') == image_id \\\n"
            "            and isinstance(document.get('attestations'), list) \\\n"
            "            and document['attestations']:\n"
            "        raise SystemExit(0)\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
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
        check = shlex.quote(str(self.check_record))
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
            '  "image inspect "*) printf "%s\\n" "$CI_BURST_TEST_IMAGE_ID"; exit 0 ;;\n'
            '  "image exists "*) exit 0 ;;\n'
            '  "tag "*)\n'
            # The promotion step itself refuses to tag an image whose complete
            # record is not on disk: ordering is a behaviour, not source text.
            f'    python3 {check} "$CI_BURST_TEST_BUILDS" "$CI_BURST_TEST_IMAGE_ID" || exit 70 ;\n'
            '    case "$*" in\n'
            '      *":latest")\n'
            '        [ "${CI_BURST_TEST_FAIL_LATEST_TAG:-0}" = "0" ] || exit 1\n'
            '        printf "%s\\n" "$CI_BURST_TEST_IMAGE_ID" >>"$CI_BURST_TEST_PROMOTED" ;;\n'
            "    esac\n"
            "    exit 0 ;;\n"
            '  "rmi "*) exit 0 ;;\n'
            "  *) exit 0 ;;\n"
            "esac",
        )

    def run_script(self, image_id=IMAGE_A, **extra):
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
            "CI_BURST_TEST_IMAGE_ID": image_id,
            "CI_BURST_TEST_BUILDS": str(self.state / "builds"),
            "CI_BURST_TEST_PROMOTED": str(self.promoted),
        }
        environment.update({name: str(value) for name, value in extra.items()})
        return subprocess.run(
            ["sh", str(REFRESH)],
            env=environment,
            capture_output=True,
            text=True,
        )

    def image_record_path(self, image_id):
        return self.state / "builds" / "images" / f"{image_id.split(':', 1)[1]}.json"

    def reference_path(self, source_sha=SOURCE_SHA):
        return self.state / "builds" / f"sha-{source_sha[:12]}.json"

    def test_records_exactly_one_complete_record_per_image(self):
        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        records = sorted(path.name for path in (self.state / "builds" / "images").iterdir())
        self.assertEqual(records, [self.image_record_path(IMAGE_A).name])
        document = json.loads(
            self.image_record_path(IMAGE_A).read_text(encoding="utf-8")
        )
        self.assertEqual(set(document), RECORD_KEYS)
        self.assertEqual(document["schema"], "ci-burst.build-inputs.v2")
        self.assertEqual(
            document["dockerfile_sha256"],
            hashlib.sha256(self.dockerfile_bytes).hexdigest(),
        )
        self.assertEqual(document["from"], f"FROM python:3.12-slim@{BASE_DIGEST}")
        self.assertEqual(document["base_digest"], BASE_DIGEST)
        self.assertEqual(document["image_id"], IMAGE_A)
        self.assertEqual(document["python_distributions"], DISTRIBUTIONS)
        self.assertEqual(len(document["attestations"]), 1)
        attestation = document["attestations"][0]
        self.assertEqual(set(attestation), ATTESTATION_KEYS)
        self.assertEqual(attestation["source_sha"], SOURCE_SHA)
        self.assertEqual(attestation["tree_sha"], TREE_SHA)
        self.assertEqual(attestation["engine"], "podman version 5.4.2")
        self.assertRegex(
            attestation["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"
        )

    def test_a_same_image_from_a_different_commit_appends_an_attestation(self):
        # A commit that changes only docs/, tests/ or deploy/ files is excluded
        # by .dockerignore, so a build of it returns the image already on the
        # host under another source. That is not a contradiction: record both
        # sources against the image and promote normally.
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        (self.state / "deployed-sha").unlink()

        second = self.run_script(
            CI_BURST_TEST_WANT=OTHER_SOURCE_SHA, CI_BURST_TEST_TREE=OTHER_TREE_SHA
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn(
            f"build_inputs_attested: {self.image_record_path(IMAGE_A)}",
            second.stdout,
        )

        document = json.loads(
            self.image_record_path(IMAGE_A).read_text(encoding="utf-8")
        )
        self.assertEqual(set(document), RECORD_KEYS)
        self.assertEqual(
            [(item["source_sha"], item["tree_sha"]) for item in document["attestations"]],
            [(SOURCE_SHA, TREE_SHA), (OTHER_SOURCE_SHA, OTHER_TREE_SHA)],
        )
        for attestation in document["attestations"]:
            with self.subTest(source=attestation["source_sha"]):
                self.assertEqual(set(attestation), ATTESTATION_KEYS)
                self.assertEqual(attestation["engine"], "podman version 5.4.2")
                self.assertRegex(
                    attestation["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$"
                )

        # The source-keyed reference names the same image, whose attestation
        # list now carries several sources.
        reference = json.loads(
            self.reference_path(OTHER_SOURCE_SHA).read_text(encoding="utf-8")
        )
        self.assertEqual(reference["source_sha"], OTHER_SOURCE_SHA)
        self.assertEqual(reference["image_id"], IMAGE_A)
        self.assertEqual(
            reference["record"], f"images/{self.image_record_path(IMAGE_A).name}"
        )
        self.assertEqual(
            self.promoted.read_text(encoding="utf-8").splitlines(), [IMAGE_A, IMAGE_A]
        )
        self.assertEqual(
            (self.state / "deployed-sha").read_text(encoding="utf-8").strip(),
            OTHER_SOURCE_SHA,
        )

        # Recording the same source again is idempotent: no second attestation
        # and no rewrite of the record.
        recorded = self.image_record_path(IMAGE_A).read_bytes()
        (self.state / "deployed-sha").unlink()
        again = self.run_script(
            CI_BURST_TEST_WANT=OTHER_SOURCE_SHA, CI_BURST_TEST_TREE=OTHER_TREE_SHA
        )
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertIn("build_inputs_kept", again.stdout)
        self.assertEqual(self.image_record_path(IMAGE_A).read_bytes(), recorded)

    def test_the_source_keyed_file_is_a_reference_not_the_record(self):
        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        reference = json.loads(self.reference_path().read_text(encoding="utf-8"))
        self.assertEqual(set(reference), REFERENCE_KEYS)
        self.assertEqual(reference["schema"], "ci-burst.build-inputs-ref.v1")
        self.assertEqual(reference["source_sha"], SOURCE_SHA)
        self.assertEqual(reference["image_id"], IMAGE_A)
        self.assertEqual(
            reference["record"],
            f"images/{self.image_record_path(IMAGE_A).name}",
        )
        # The reference carries no inputs of its own: the image record does.
        self.assertTrue(self.image_record_path(IMAGE_A).exists())
        log_lines = [
            line
            for line in completed.stdout.splitlines()
            if line.startswith("ci-burst-refresh: build_inputs")
        ]
        self.assertEqual(
            log_lines,
            [
                f"ci-burst-refresh: build_inputs: {self.image_record_path(IMAGE_A)}",
                f"ci-burst-refresh: build_inputs_ref: {self.reference_path()}",
            ],
        )

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
        self.assertFalse(self.image_record_path(IMAGE_A).exists())
        self.assertFalse(self.reference_path().exists())
        calls = self.calls.read_text(encoding="utf-8")
        self.assertNotIn("localhost/ci-burst:latest", calls)
        self.assertFalse((self.state / "deployed-sha").exists())

    def test_a_record_publication_failure_never_promotes(self):
        # The rename is the publication. If it fails, nothing may be tagged
        # and no reference may claim a record that is not there.
        mv = self.bin / "mv"
        mv.write_text(
            "#!/bin/sh\n"
            'for arg in "$@"; do\n'
            '  case "$arg" in *"/builds/images/"*) exit 1 ;; esac\n'
            "done\n"
            'exec /bin/mv "$@"\n',
            encoding="utf-8",
        )
        mv.chmod(0o755)
        completed = self.run_script()
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("build_inputs_failed", completed.stdout)
        self.assertFalse(self.image_record_path(IMAGE_A).exists())
        self.assertFalse(self.reference_path().exists())
        self.assertEqual(self.promoted.read_text(encoding="utf-8"), "")
        tag_calls = [
            call
            for call in self.calls.read_text(encoding="utf-8").splitlines()
            if call.startswith("tag ")
        ]
        self.assertEqual(tag_calls, [])
        self.assertFalse((self.state / "deployed-sha").exists())

    def test_a_failed_same_source_retry_keeps_the_serving_images_record(self):
        # Run A promotes. The run then dies before committed deployed-sha, as
        # an interrupted promotion does; the next scheduled run sees the old
        # SHA and rebuilds the same source as B. B records and then fails to
        # tag. A still serves, so A's inputs must still be recorded, unchanged.
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        serving = self.image_record_path(IMAGE_A)
        recorded = serving.read_bytes()
        self.assertEqual(
            self.promoted.read_text(encoding="utf-8").splitlines(), [IMAGE_A]
        )
        (self.state / "deployed-sha").unlink()

        second = self.run_script(
            image_id=IMAGE_B, CI_BURST_TEST_FAIL_LATEST_TAG="1"
        )
        self.assertNotEqual(second.returncode, 0, second.stdout)
        self.assertIn("promote_failed", second.stdout)
        self.assertIn(
            f"build_inputs: {self.image_record_path(IMAGE_B)}", second.stdout
        )

        self.assertTrue(
            serving.exists(),
            "a failed retry replaced the serving image's only build record",
        )
        self.assertEqual(serving.read_bytes(), recorded)
        document = json.loads(serving.read_text(encoding="utf-8"))
        self.assertEqual(document["image_id"], IMAGE_A)
        self.assertEqual(document["attestations"][0]["source_sha"], SOURCE_SHA)
        # B's build has its own record; A's is not a casualty of it.
        other = json.loads(
            self.image_record_path(IMAGE_B).read_text(encoding="utf-8")
        )
        self.assertEqual(other["image_id"], IMAGE_B)
        # The failure happened before `latest` moved: A still serves.
        self.assertEqual(
            self.promoted.read_text(encoding="utf-8").splitlines(), [IMAGE_A]
        )

    def test_a_same_image_rebuild_keeps_the_first_record(self):
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        serving = self.image_record_path(IMAGE_A)
        recorded = serving.read_bytes()
        (self.state / "deployed-sha").unlink()

        second = self.run_script()
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("build_inputs_kept", second.stdout)
        self.assertEqual(serving.read_bytes(), recorded)
        self.assertEqual(
            self.promoted.read_text(encoding="utf-8").splitlines(),
            [IMAGE_A, IMAGE_A],
        )

    def test_a_conflicting_image_record_refuses_promotion(self):
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        serving = self.image_record_path(IMAGE_A)
        recorded = serving.read_bytes()
        (self.state / "deployed-sha").unlink()

        # Same image ID, different build inputs: the Dockerfile changed without
        # changing the bytes it produces. That is a contradiction, not a second
        # source for the image, so keep the first record, keep the candidate as
        # evidence, and refuse to promote.
        changed_bytes = self.dockerfile_bytes + b"# a changed build input\n"
        (self.context / "Dockerfile").write_bytes(changed_bytes)
        second = self.run_script(
            CI_BURST_TEST_WANT=OTHER_SOURCE_SHA, CI_BURST_TEST_TREE=OTHER_TREE_SHA
        )
        self.assertNotEqual(second.returncode, 0, second.stdout)
        self.assertIn("build_inputs_failed: image record conflict", second.stdout)
        self.assertEqual(serving.read_bytes(), recorded)
        conflict = serving.with_name(serving.name + ".conflict")
        self.assertTrue(conflict.exists())
        candidate = json.loads(conflict.read_text(encoding="utf-8"))
        self.assertEqual(
            candidate["dockerfile_sha256"], hashlib.sha256(changed_bytes).hexdigest()
        )
        self.assertNotEqual(
            candidate["dockerfile_sha256"],
            json.loads(recorded)["dockerfile_sha256"],
        )
        self.assertEqual(len(candidate["attestations"]), 1)
        self.assertEqual(candidate["attestations"][0]["source_sha"], OTHER_SOURCE_SHA)
        self.assertEqual(
            self.promoted.read_text(encoding="utf-8").splitlines(), [IMAGE_A]
        )
        self.assertFalse((self.state / "deployed-sha").exists())

    def test_the_attestation_list_is_capped_and_refuses_at_the_cap(self):
        # Eight sources may be attested for one image. A ninth is refused rather
        # than silently dropping a source that a reference may still name.
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        for index in range(1, 8):
            (self.state / "deployed-sha").unlink()
            completed = self.run_script(
                CI_BURST_TEST_WANT=f"{index:040x}",
                CI_BURST_TEST_TREE=f"{index + 100:040x}",
            )
            self.assertEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )
        document = json.loads(
            self.image_record_path(IMAGE_A).read_text(encoding="utf-8")
        )
        self.assertEqual(len(document["attestations"]), 8)

        (self.state / "deployed-sha").unlink()
        refused = self.run_script(
            CI_BURST_TEST_WANT="f" * 40, CI_BURST_TEST_TREE="e" * 40
        )
        self.assertEqual(refused.returncode, 2, refused.stdout)
        self.assertIn("build_inputs_full", refused.stdout)
        self.assertEqual(
            len(
                json.loads(
                    self.image_record_path(IMAGE_A).read_text(encoding="utf-8")
                )["attestations"]
            ),
            8,
        )
        self.assertEqual(
            self.promoted.read_text(encoding="utf-8").splitlines(), [IMAGE_A] * 8
        )
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

    def test_container_guide_states_both_record_paths_and_the_retention_rule(self):
        text = " ".join(CONTAINER_DOC.read_text(encoding="utf-8").split())
        for marker in (
            "builds/images/<image-id>.json",
            "builds/sha-<12>.json",
            "immutable",
            "reference",
            "retained",
            "pruned manually",
            "conflict",
            "attestation",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, text)

    def test_container_guide_states_the_attestation_append_rule_and_cap(self):
        text = " ".join(CONTAINER_DOC.read_text(encoding="utf-8").split())
        for marker in (
            "attestation list",
            "appends",
            "build_inputs_attested",
            "build_inputs_full",
            "capped at 8 sources",
            "build inputs",
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
