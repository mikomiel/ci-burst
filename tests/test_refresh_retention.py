"""Behaviour tests for the refresh script's image-tag retention.

Retention deletes local builds, so it is the one irreversible act in
``deploy/ci-burst-refresh.sh``. One sha-<12> tag per merged commit accumulates at
about 150 MB and filled a 7.8 GB host (HB-3340), but it must never remove a name
a rollback, the serving tag or the deployment-state document still resolves, and
it must not run before a promotion has succeeded.

The stub engine here keeps a real tag-to-image table and a dangling-image table,
so these cases assert the surviving tag set rather than the calls alone: a shared
image keeps its other names, a failed promote removes nothing, a refused prune is
retried on the next run, and an unrelated dangling image is never a candidate.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "ci-burst-refresh.sh"

WANT = "6e1451e7080726b62dd0f69bdd4f705b17bfae58"
TREE = "1" * 40
SHORT = WANT[:12]
OLD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
MID = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
NEW = "cccccccccccccccccccccccccccccccccccccccc"
CANDIDATE_ID = "3" * 64

# Builds this host already holds, oldest first: (commit, image id, created).
BUILDS = [
    (OLD, "a" * 64, "2026-11-10 10:00:00.000000000 +0000 UTC"),
    (MID, "b" * 64, "2026-11-11 10:00:00.000000000 +0000 UTC"),
    (NEW, "c" * 64, "2026-11-12 10:00:00.000000000 +0000 UTC"),
]


def retention_block():
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    start = next(
        index for index, line in enumerate(lines) if line.startswith("# --- image tag retention")
    )
    end = next(
        index
        for index, line in enumerate(lines)
        if index > start and line.startswith("# --- end image tag retention")
    )
    return "\n".join(lines[start : end + 1])


class EngineHarness(unittest.TestCase):
    """A stub engine with a tag table, an image table and dangling images."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls"
        self.calls.write_text("")
        self.tags = self.root / "tags"
        self.images = self.root / "images"
        self.dangling = self.root / "dangling"
        self.state = self.root / "state"
        self.state.mkdir()
        self.prune_status = self.root / "prune-status"
        self.prune_status.write_text("0")
        self.prune_log = self.root / "prunes"
        self.write_host(
            tags=[("latest", "d" * 64), ("previous", "a" * 64)]
            + [(f"sha-{build[0][:12]}", build[1]) for build in BUILDS],
            images=[(build[1], build[2]) for build in BUILDS] + [("d" * 64, BUILDS[2][2])],
        )
        # An unrelated product's dangling image. It carries the standard OCI
        # revision label, which is exactly why that label cannot scope a prune.
        self.dangling.write_text("unrelated|org.opencontainers.image.revision=deadbeef\n")
        self.engine = self.bin / "podman"
        self.engine.write_text(self.engine_source())
        self.engine.chmod(0o755)
        (self.state / "deployed-sha").write_text(f"{OLD}\n")

    def engine_source(self):
        return (
            "#!/bin/bash\n"
            f'printf "%s\\n" "$*" >> {self.calls}\n'
            "ref_id() {\n"
            '  case "$1" in\n'
            '    sha256:*) printf "%s" "${1#sha256:}"; return 0 ;;\n'
            "  esac\n"
            f'  awk -F"|" -v t="${{1##*:}}" \'$1==t {{print $2}}\' {self.tags}\n'
            "}\n"
            'case "$1" in\n'
            "  --version) printf 'podman version 5.0.0\\n' ;;\n"
            "  build)\n"
            f'    printf "candidate|%s\\n" {CANDIDATE_ID} >> {self.tags}\n'
            f'    printf "%s|2026-11-14 00:00:00.000000000 +0000 UTC\\n" {CANDIDATE_ID} >> {self.images}\n'
            "    exit 0 ;;\n"
            "  run)\n"
            '    case "$*" in\n'
            '      *"pip list"*) printf "requests==2.0\\n" ;;\n'
            '      *) printf \'{"decision":"wait"}\\n\' ;;\n'
            "    esac\n"
            "    exit 0 ;;\n"
            "  image)\n"
            '    case "$2" in\n'
            "      exists) exit 0 ;;\n"
            "      inspect)\n"
            '        ref=${!#}\n'
            '        id=$(ref_id "$ref")\n'
            '        case "$*" in\n'
            "          *.Config.Labels*)\n"
            f'            printf "%s\\n" {WANT} ;;\n'
            "          *'{{.Id}}'*)\n"
            '            [ -n "$id" ] || exit 125\n'
            '            printf "sha256:%s\\n" "$id" ;;\n'
            "          *'{{.Created}}'*)\n"
            '            [ -n "$id" ] || exit 125\n'
            f'            created=$(awk -F"|" -v i="$id" \'$1==i {{print $2}}\' {self.images})\n'
            '            [ -n "$created" ] || exit 125\n'
            '            printf "%s\\n" "$created" ;;\n'
            "        esac ;;\n"
            "      prune)\n"
            f'        printf "%s\\n" "$*" >> {self.prune_log}\n'
            f'        if [ "$(cat {self.prune_status})" != "0" ]; then exit 1; fi\n'
            '        case "$*" in\n'
            f'          *"label=org.ci-burst.build=refresh"*)\n'
            f'            grep -Fv "org.ci-burst.build=refresh" {self.dangling} > {self.dangling}.owned || true\n'
            f'            mv {self.dangling}.owned {self.dangling}\n'
            "            ;;\n"
            "        esac\n"
            "        exit 0 ;;\n"
            "    esac\n"
            "    exit 0 ;;\n"
            "  tag)\n"
            '    id=$(ref_id "$2")\n'
            f'    printf "%s|%s\\n" "${{3##*:}}" "$id" >> {self.tags}\n'
            "    exit 0 ;;\n"
            "  rmi)\n"
            '    ref=${!#}\n'
            '    named=${ref##*:}\n'
            f'    grep -v "^$named|" {self.tags} > {self.tags}.new || true\n'
            f'    mv {self.tags}.new {self.tags}\n'
            "    exit 0 ;;\n"
            f"  images) cut -d'|' -f1 {self.tags} ;;\n"
            "  untag)\n"
            '    shift\n'
            '    for name in "$@"; do\n'
            f'      if [ -f {self.root / "untag-fail"} ] && '
            f'grep -Fqx "${{name##*:}}" {self.root / "untag-fail"}; then exit 1; fi\n'
            '      case "$name" in\n'
            '        *:*)\n'
            f'          removed=${{name##*:}}\n'
            f'          id=$(awk -F"|" -v t="$removed" \'$1==t {{print $2}}\' {self.tags})\n'
            f'          grep -v "^$removed|" {self.tags} > {self.tags}.new || true\n'
            f'          mv {self.tags}.new {self.tags}\n'
            f'          if [ -n "$id" ] && ! grep -q "|$id$" {self.tags}; then\n'
            f'            printf "%s|org.ci-burst.build=refresh\\n" "$id" >> {self.dangling}\n'
            "          fi\n"
            "          ;;\n"
            "        *)\n"
            f'          # A bare repository name removes every name on that image.\n'
            f'          cat {self.tags} >> {self.root}/untag-bulk\n'
            f'          : > {self.tags}\n'
            "          ;;\n"
            "      esac\n"
            "    done\n"
            "    exit 0 ;;\n"
            "esac\n"
            "exit 0\n"
        )

    def rewrite_engine(self):
        self.engine.write_text(self.engine_source())
        self.engine.chmod(0o755)

    def write_host(self, tags, images):
        self.tags.write_text("".join(f"{tag}|{image}\n" for tag, image in tags))
        self.images.write_text("".join(f"{image}|{created}\n" for image, created in images))

    def host_tags(self):
        return {line.split("|")[0] for line in self.tags.read_text().splitlines() if line}

    def untagged(self):
        return [
            line.split()[1]
            for line in self.calls.read_text().splitlines()
            if line.startswith("untag ")
        ]

    def untag_calls(self):
        return [
            line.split()
            for line in self.calls.read_text().splitlines()
            if line.startswith("untag ")
        ]

    def prunes(self):
        return self.prune_log.read_text().splitlines() if self.prune_log.exists() else []

    def dangling_ids(self):
        return {line.split("|")[0] for line in self.dangling.read_text().splitlines() if line}

    def run_block(self, retained=1):
        runner = self.root / "run.sh"
        (self.root / "retention.sh").write_text(retention_block())
        runner.write_text(
            "#!/bin/bash\n"
            "set -eu\n"
            f"PATH={self.bin}:/usr/local/bin:/usr/bin:/bin\n"
            "IMAGE=localhost/ci-burst\n"
            f"STATE={self.state}\n"
            "REVISION_LABEL=org.opencontainers.image.revision\n"
            "OWNERSHIP_LABEL=org.ci-burst.build\n"
            f"RETAIN_BUILDS={retained}\n"
            'log() { printf "ci-burst-refresh: %s\\n" "$*"; }\n'
            f"source {self.root / 'retention.sh'}\n"
            "reconcile_image_tags\n"
        )
        runner.chmod(0o755)
        return subprocess.run(["bash", str(runner)], capture_output=True, text=True, timeout=60)


class RetentionFunctionTests(EngineHarness):
    def test_the_window_is_the_newest_builds_overall_not_extra_builds(self):
        # The deployed build (NEW) is also the newest. With a window of one, it
        # occupies the window and consumes the place: nothing older survives on
        # the window alone, and no named ref may add back an older build.
        (self.state / "deployed-sha").write_text(f"{NEW}\n")
        done = self.run_block(retained=1)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(
            self.host_tags(), {"latest", "previous", f"sha-{NEW[:12]}"}
        )

    def test_a_named_ref_outside_the_window_survives(self):
        (self.state / "deployment-state.json").write_text(
            json.dumps({"schema": "deployment-state.v1", "deployed_ref": OLD})
        )
        done = self.run_block(retained=1)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"sha-{OLD[:12]}", self.host_tags())
        self.assertNotIn(f"sha-{MID[:12]}", self.host_tags())

    def test_a_shared_image_keeps_its_other_names(self):
        # `previous` and the OLD build tag name the same image. Removing the
        # build name must not remove the rollback handle.
        (self.state / "deployed-sha").write_text("e" * 40 + "\n")
        done = self.run_block(retained=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("previous", self.host_tags())
        self.assertNotIn(f"sha-{OLD[:12]}", self.host_tags())
        self.assertNotIn("a" * 64, self.dangling_ids())

    def test_an_uninspectable_tag_is_kept_and_reported(self):
        self.images.write_text(
            "".join(
                f"{build[1]}|{build[2]}\n"
                for build in BUILDS
                if build[1] != BUILDS[1][1]
            )
        )
        done = self.run_block(retained=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"sha-{MID[:12]}", self.host_tags())
        self.assertIn(f"retention_kept: sha-{MID[:12]} cannot be inspected", done.stdout)

    def test_an_unreadable_state_document_removes_nothing(self):
        (self.state / "deployment-state.json").write_text("{ this is not json")
        done = self.run_block(retained=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("retention_failed: cannot read the retained refs", done.stdout)
        self.assertEqual(self.untagged(), [])
        self.assertIn(f"sha-{OLD[:12]}", self.host_tags())

    def test_a_state_document_that_is_a_directory_removes_nothing(self):
        (self.state / "deployment-state.json").mkdir()
        done = self.run_block(retained=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("retention_failed: cannot read the retained refs", done.stdout)
        self.assertEqual(self.untag_calls(), [])
        self.assertEqual(self.prunes(), [])

    def test_a_dangling_state_document_symlink_removes_nothing(self):
        (self.state / "deployment-state.json").symlink_to(self.state / "missing.json")
        done = self.run_block(retained=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("retention_failed: cannot read the retained refs", done.stdout)
        self.assertEqual(self.untag_calls(), [])
        self.assertEqual(self.prunes(), [])

    def test_a_corrupted_deployed_ref_removes_nothing(self):
        (self.state / "deployed-sha").write_text("not-a-commit\n")
        done = self.run_block(retained=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("retention_failed: cannot read the retained refs", done.stdout)
        self.assertEqual(self.untagged(), [])

    def test_nothing_to_remove_is_reported_and_removes_nothing(self):
        self.write_host(tags=[("latest", "d" * 64), ("previous", "a" * 64)], images=[])
        done = self.run_block(retained=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.untagged(), [])
        self.assertIn("removed 0 build tags", done.stdout)

    def test_every_untag_names_one_full_reference(self):
        # podman treats each argument as an image name: a bare repository name
        # removes every name on that image, `latest` and `previous` included.
        (self.state / "deployed-sha").write_text("e" * 40 + "\n")
        done = self.run_block(retained=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.untag_calls()
        self.assertGreaterEqual(len(calls), 1, calls)
        for call in calls:
            with self.subTest(call=call):
                # Exactly one operand, and it is a full repository:tag name.
                self.assertEqual(len(call), 2, call)
                self.assertRegex(call[1], r"^localhost/ci-burst:sha-[0-9a-f]{12}$")
        self.assertFalse((self.root / "untag-bulk").exists())

    def test_an_evicted_image_is_pruned_and_an_unrelated_one_survives(self):
        (self.state / "deployed-sha").write_text("e" * 40 + "\n")
        self.run_block(retained=0)
        # The evicted builds were owned dangling images; the prune removed them.
        self.assertNotIn("a" * 64, self.dangling_ids())
        self.assertNotIn("b" * 64, self.dangling_ids())
        self.assertIn("unrelated", self.dangling_ids())

    def test_a_refused_untag_keeps_the_tag_and_does_not_abort(self):
        # The engine can refuse one name while others succeed. The tag stays,
        # the refusal is named, and the run still completes: the serving image is
        # already correct.
        (self.root / "untag-fail").write_text(f"sha-{MID[:12]}\n")
        done = self.run_block(retained=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"sha-{MID[:12]}", self.host_tags())
        self.assertIn(
            f"retention_failed: cannot remove the tag sha-{MID[:12]}", done.stdout
        )
        self.assertIn("retention: removed", done.stdout)

    def test_the_prune_is_scoped_to_this_products_label(self):
        self.run_block(retained=0)
        prunes = self.prunes()
        self.assertEqual(len(prunes), 1, prunes)
        self.assertIn("label=org.ci-burst.build=refresh", prunes[0])
        self.assertNotIn("org.opencontainers.image.revision", prunes[0])
        # The unrelated product's dangling image is still there.
        self.assertIn("unrelated", self.dangling_ids())


class RefreshOrderTests(unittest.TestCase):
    """Retention is reachable only after a promotion has been proven good."""

    def setUp(self):
        self.lines = SCRIPT.read_text(encoding="utf-8").splitlines()

    def index(self, needle):
        for index, line in enumerate(self.lines):
            if needle in line:
                return index
        self.fail(f"the script must contain {needle!r}")

    def test_the_two_call_sites_follow_their_proof(self):
        calls = [
            index
            for index, line in enumerate(self.lines)
            if line.strip() == "reconcile_image_tags"
        ]
        self.assertEqual(len(calls), 2, calls)
        current, promote = calls
        self.assertGreater(current, self.index('if [ "$serving" = "$want" ]; then'))
        self.assertGreater(current, self.index('log "current: $want"'))
        self.assertGreater(promote, self.index('podman tag "$IMAGE:candidate" "$IMAGE:latest"'))
        self.assertGreater(
            promote, self.index('mv "$STATE/deployed-sha.tmp" "$STATE/deployed-sha"')
        )
        self.assertLess(current, promote)

    def test_no_deletion_exists_outside_the_retention_block(self):
        start = self.index("# --- image tag retention")
        end = self.index("# --- end image tag retention")
        for needle in ("podman untag", "podman image prune"):
            with self.subTest(needle=needle):
                positions = [
                    index
                    for index, line in enumerate(self.lines)
                    if needle in line and line.lstrip().startswith(("podman", "if podman"))
                ]
                self.assertTrue(all(start < position < end for position in positions), positions)

    def test_retention_cannot_clobber_the_callers_work_directory(self):
        # The promote path's EXIT trap removes $work. A function that reuses that
        # name would leak the build context on every promoted run.
        text = SCRIPT.read_text(encoding="utf-8")
        start = text.index("reconcile_image_tags() (")
        end = text.index("# --- end image tag retention")
        body = text[start:end]
        self.assertIn("retention_work=$(mktemp", body)
        for line in body.splitlines():
            self.assertNotEqual(line.strip().split("=")[0], "work")

    def test_a_malformed_window_is_refused_before_any_build(self):
        self.assertIn('CI_BURST_RETAIN_BUILDS:-3', SCRIPT.read_text(encoding="utf-8"))
        self.assertLess(self.index("retained_builds_invalid"), self.index("set -- build"))

    def test_a_bad_window_refuses_without_touching_anything(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(
                os.environ,
                CI_BURST_RETAIN_BUILDS="seven",
                CI_BURST_IMAGE_STATE=directory,
                CI_BURST_SOURCE=directory,
            )
            done = subprocess.run(
                ["sh", str(SCRIPT)], capture_output=True, text=True, timeout=60, env=environment
            )
            self.assertEqual(done.returncode, 2, done.stderr)
            self.assertIn("retained_builds_invalid: seven", done.stdout)


@unittest.skipUnless(shutil.which("sh"), "the refresh script runs under sh")
class RefreshPromoteTests(EngineHarness):
    """The whole script, with the engine and the source stubbed."""

    def setUp(self):
        super().setUp()
        fixture = self.root / "fixture"
        (fixture / "ci_burst").mkdir(parents=True)
        (fixture / "Dockerfile").write_text("FROM debian:13\n")
        (fixture / "ci_burst" / "__init__.py").write_text('__version__ = "0.6.0"\n')
        self.fixture = fixture
        (self.bin / "git").write_text(
            "#!/bin/bash\n"
            '[ "$1" = "-C" ] && shift 2\n'
            'case "$1" in\n'
            "  fetch) exit 0 ;;\n"
            "  rev-parse)\n"
            '    case "$2" in\n'
            f"      FETCH_HEAD) printf '%s\\n' {WANT} ;;\n"
            f'      *"^{{tree}}"*) printf \'%s\\n\' {TREE} ;;\n'
            "      *) exit 1 ;;\n"
            "    esac ;;\n"
            "  archive)\n"
            '    out=""\n'
            '    while [ $# -gt 0 ]; do [ "$1" = "--output" ] && out=$2; shift; done\n'
            '    ( cd "$FIXTURE" && tar -cf "$out" Dockerfile ci_burst ) ;;\n'
            "esac\n"
            "exit 0\n"
        )
        (self.bin / "git").chmod(0o755)

    def run_script(self, promote_fails=False, now_current=False):
        if now_current:
            # A host already serving the channel commit takes the current path;
            # its tag table is whatever the earlier run left behind.
            (self.state / "deployed-sha").write_text(f"{WANT}\n")
        if promote_fails:
            text = self.engine_source().replace(
                '  tag)\n', '  tag)\n    case "$3" in *:latest) exit 1 ;; esac\n'
            )
            self.engine.write_text(text)
            self.engine.chmod(0o755)
        source = self.root / "source"
        source.mkdir(exist_ok=True)
        config = self.root / "deployment.toml"
        config.write_text("")
        environment = dict(
            os.environ,
            FIXTURE=str(self.fixture),
            CI_BURST_SOURCE=str(source),
            CI_BURST_IMAGE_STATE=str(self.state),
            CI_BURST_CONFIG=str(config),
            CI_BURST_AWS_DIR=str(config),
            CI_BURST_TOKEN_ENV="",
            CI_BURST_UNITS="",
            CI_BURST_SMOKE_COMMAND="plan",
            CI_BURST_RETAIN_BUILDS="2",
            PATH=f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
        )
        return subprocess.run(
            ["sh", str(SCRIPT)], capture_output=True, text=True, timeout=120, env=environment
        )

    def test_a_successful_promote_leaves_exactly_the_declared_set(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertIn(f"promoted: {WANT}", done.stdout)
        # latest moves to the candidate, previous captures the old latest, the
        # new build keeps its sha tag, and the newest build holds the one-place
        # window. Every older unnamed build is gone.
        self.assertEqual(
            self.host_tags(), {"latest", "previous", f"sha-{SHORT}", f"sha-{NEW[:12]}"}
        )

    def test_a_failed_promote_removes_nothing(self):
        done = self.run_script(promote_fails=True)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn(f"promote_failed: {WANT}", done.stdout)
        self.assertEqual(self.untagged(), [])
        self.assertEqual(self.prunes(), [])

    def test_a_refused_prune_is_retried_on_an_already_current_run(self):
        self.prune_status.write_text("1")
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        self.assertIn("prune refused", first.stdout)
        # The evicted build (the middle one: `previous` keeps the oldest name)
        # is dangling and still on disk after the refusal.
        self.assertIn("b" * 64, self.dangling_ids())
        self.prune_status.write_text("0")
        second = self.run_script(now_current=True)
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        self.assertIn(f"current: {WANT}", second.stdout)
        # Two attempts: the refused one, and the retry on a run that removed no
        # tag at all. The retry is what actually frees the bytes.
        self.assertEqual(len(self.prunes()), 2)
        self.assertIn("removed 0 build tags", second.stdout)
        self.assertNotIn("b" * 64, self.dangling_ids())
        self.assertIn("unrelated", self.dangling_ids())

    def test_retention_leaves_the_callers_work_variable_and_directory_alone(self):
        # The promote path's EXIT trap removes $work; retention must not touch it.
        work = self.root / "promote-work"
        work.mkdir()
        (work / "marker").write_text("keep")
        (self.root / "retention.sh").write_text(retention_block())
        runner = self.root / "run-work.sh"
        runner.write_text(
            "#!/bin/bash\n"
            "set -eu\n"
            f"PATH={self.bin}:/usr/local/bin:/usr/bin:/bin\n"
            "IMAGE=localhost/ci-burst\n"
            f"STATE={self.state}\n"
            "REVISION_LABEL=org.opencontainers.image.revision\n"
            "OWNERSHIP_LABEL=org.ci-burst.build\n"
            "RETAIN_BUILDS=0\n"
            f'work={work}\n'
            'log() { printf "ci-burst-refresh: %s\\n" "$*"; }\n'
            f"source {self.root / 'retention.sh'}\n"
            "reconcile_image_tags\n"
            'printf "work=%s\\n" "$work"\n'
        )
        runner.chmod(0o755)
        done = subprocess.run(["bash", str(runner)], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(f"work={work}", done.stdout)
        self.assertTrue((work / "marker").exists())
