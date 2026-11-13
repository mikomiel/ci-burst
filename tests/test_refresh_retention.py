"""Behaviour tests for the refresh script's image-tag retention.

Retention deletes local builds, so it is the one irreversible act in
``deploy/ci-burst-refresh.sh``. One sha-<12> tag per merged commit accumulates at
about 150 MB and filled a 7.8 GB host (HB-3340), but the fix must never remove a
tag a rollback or the deployment-state document still names, and it must not run
before a promotion has succeeded.

The script is executed here against a stubbed engine and a stubbed source, so
these cases pin behaviour rather than source text: what a successful promote
leaves behind, that a failed promote removes nothing, and that an unreadable tag
is kept rather than guessed at.
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

#: A 40-hex commit and its matching sha-<12> tag, as the script derives them.
WANT = "6e1451e7080726b62dd0f69bdd4f705b17bfae58"
TREE = "1" * 40
SHORT = WANT[:12]
OLD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NEWER = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
TAGS = [
    "latest",
    "previous",
    f"sha-{SHORT}",
    f"sha-{OLD[:12]}",
    f"sha-{NEWER[:12]}",
    "sha-cccccccccccc",
    "sha-dddddddddddd",
]
CREATED = {
    f"sha-{OLD[:12]}": "2026-11-10 10:00:00.000000000 +0000 UTC",
    f"sha-{WANT[:12]}": "2026-11-11 10:00:00.000000000 +0000 UTC",
    f"sha-{NEWER[:12]}": "2026-11-12 10:00:00.000000000 +0000 UTC",
    "sha-cccccccccccc": "2026-11-13 10:00:00.000000000 +0000 UTC",
    "sha-dddddddddddd": "2026-11-13 20:00:00.000000000 +0000 UTC",
}


def retention_block():
    """The shipped retention block, for the function-level cases."""
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


class RetentionHarness(unittest.TestCase):
    """Shared fixture: a stubbed engine that records every call it receives."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls"
        self.calls.write_text("")
        self.tags = self.root / "tags"
        self.tags.write_text("\n".join(TAGS) + "\n")
        self.created = self.root / "created"
        self.created.write_text(
            "\n".join(f"{tag}|{stamp}" for tag, stamp in CREATED.items()) + "\n"
        )
        self.state = self.root / "state"
        self.state.mkdir()
        (self.state / "deployed-sha").write_text(f"{WANT}\n")
        (self.bin / "podman").write_text(
            "#!/bin/bash\n"
            'printf "%s\\n" "$*" >> ' + str(self.calls) + "\n"
            'case "$1" in\n'
            "  images) cat " + str(self.tags) + " ;;\n"
            "  image)\n"
            '    case "$2" in\n'
            "      inspect)\n"
            '        tag=${5##*:}\n'
            "        created=$(awk -F'|' -v t=\"$tag\" '$1==t {print $2}' " + str(self.created) + ")\n"
            '        [ -n "$created" ] || exit 125\n'
            '        printf "%s\\n" "$created" ;;\n'
            "      prune) exit 0 ;;\n"
            "    esac ;;\n"
            "  untag) exit 0 ;;\n"
            'esac\n'
            "exit 0\n"
        )
        (self.bin / "podman").chmod(0o755)

    def call_lines(self):
        return [line for line in self.calls.read_text().splitlines() if line]

    def untagged(self):
        return [line.split()[1] for line in self.call_lines() if line.startswith("untag ")]

    def run_block(self, retained=1):
        runner = self.root / "run.sh"
        runner.write_text(
            "#!/bin/bash\n"
            "set -eu\n"
            f"PATH={self.bin}:/usr/local/bin:/usr/bin:/bin\n"
            "IMAGE=localhost/ci-burst\n"
            f"STATE={self.state}\n"
            "REVISION_LABEL=org.opencontainers.image.revision\n"
            f"RETAIN_BUILDS={retained}\n"
            'log() { printf "ci-burst-refresh: %s\\n" "$*"; }\n'
            f"source {self.root / 'retention.sh'}\n"
            "reconcile_image_tags\n"
        )
        (self.root / "retention.sh").write_text(retention_block())
        runner.chmod(0o755)
        return subprocess.run(["bash", str(runner)], capture_output=True, text=True, timeout=60)


class RetentionFunctionTests(RetentionHarness):
    def test_a_promote_leaves_the_declared_set(self):
        done = self.run_block(retained=1)
        self.assertEqual(done.returncode, 0, done.stderr)
        untagged = self.untagged()
        # latest and previous are not builds. With the window at one build, the
        # newest build survives on the window and the deployed ref survives as a
        # named ref; every older build outside both is removed.
        self.assertNotIn("localhost/ci-burst:sha-" + SHORT, untagged)
        self.assertIn("localhost/ci-burst:sha-" + OLD[:12], untagged)
        self.assertIn("localhost/ci-burst:sha-cccccccccccc", untagged)
        self.assertIn("localhost/ci-burst:sha-" + NEWER[:12], untagged)
        self.assertNotIn("localhost/ci-burst:sha-dddddddddddd", untagged)
        prune = [line for line in self.call_lines() if line.startswith("image prune")]
        self.assertEqual(len(prune), 1, prune)
        self.assertIn("label=org.opencontainers.image.revision", prune[0])

    def test_a_ref_the_state_document_names_survives(self):
        (self.state / "deployment-state.json").write_text(
            json.dumps({"schema": "deployment-state.v1", "deployed_ref": OLD})
        )
        done = self.run_block(retained=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        untagged = self.untagged()
        self.assertNotIn("localhost/ci-burst:sha-" + OLD[:12], untagged)
        self.assertIn("localhost/ci-burst:sha-cccccccccccc", untagged)
        self.assertIn("localhost/ci-burst:sha-dddddddddddd", untagged)

    def test_an_uninspectable_tag_is_kept_and_reported(self):
        self.created.write_text(
            "\n".join(
                f"{tag}|{stamp}"
                for tag, stamp in CREATED.items()
                if tag != "sha-cccccccccccc"
            )
            + "\n"
        )
        done = self.run_block(retained=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("localhost/ci-burst:sha-cccccccccccc", self.untagged())
        self.assertIn("retention_kept: sha-cccccccccccc cannot be inspected", done.stdout)

    def test_nothing_to_remove_is_reported_and_removes_nothing(self):
        (self.state / "deployed-sha").write_text("")
        self.tags.write_text("latest\nprevious\n")
        done = self.run_block(retained=0)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.untagged(), [])
        self.assertIn("retention: nothing to remove", done.stdout)

    def test_a_refused_untag_is_reported_and_does_not_abort(self):
        (self.bin / "podman").write_text(
            (self.bin / "podman").read_text().replace(
                "  untag) exit 0 ;;",
                '  untag) [ "${2##*:}" = "sha-cccccccccccc" ] && exit 1; exit 0 ;;',
            )
        )
        done = self.run_block(retained=1)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("retention_failed: cannot untag localhost/ci-burst:sha-cccccccccccc", done.stdout)


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
        # The already-current path: after the serving label is proven equal, and
        # inside that branch.
        self.assertGreater(current, self.index('if [ "$serving" = "$want" ]; then'))
        self.assertGreater(current, self.index('log "current: $want"'))
        # The promote path: after the serving tag moves and the deployed ref is
        # recorded, never before. This is what makes a failed promote remove
        # nothing: the only deletion site is below a completed promotion.
        self.assertGreater(promote, self.index('podman tag "$IMAGE:candidate" "$IMAGE:latest"'))
        self.assertGreater(
            promote, self.index('mv "$STATE/deployed-sha.tmp" "$STATE/deployed-sha"')
        )
        self.assertLess(current, promote)

    def test_no_deletion_exists_outside_the_retention_block(self):
        start = self.index("# --- image tag retention")
        end = self.index("# --- end image tag retention")
        for needle in ("podman untag", "podman image prune"):
            positions = [
                index
                for index, line in enumerate(self.lines)
                if needle in line and line.lstrip().startswith(("podman", "if podman"))
            ]
            with self.subTest(needle=needle):
                self.assertTrue(all(start < position < end for position in positions), positions)

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
class RefreshPromoteTests(RetentionHarness):
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
            '    out="";\n'
            '    while [ $# -gt 0 ]; do [ "$1" = "--output" ] && out=$2; shift; done\n'
            '    ( cd "$FIXTURE" && tar -cf "$out" Dockerfile ci_burst ) ;;\n'
            "esac\n"
            "exit 0\n"
        )
        (self.bin / "git").chmod(0o755)

    def engine(self, promote_fails):
        return (
            "#!/bin/bash\n"
            'printf "%s\\n" "$*" >> ' + str(self.calls) + "\n"
            'case "$1" in\n'
            "  --version) printf 'podman version 5.0.0\\n' ;;\n"
            "  build) exit 0 ;;\n"
            "  run)\n"
            '    case "$*" in\n'
            '      *"pip list"*) printf "requests==2.0\\n"; exit 0 ;;\n'
            '      *) printf \'{"decision":"wait"}\\n\'; exit 0 ;;\n'
            "    esac ;;\n"
            "  image)\n"
            '    case "$2" in\n'
            "      exists) exit 0 ;;\n"
            "      inspect)\n"
            '        case "$*" in\n'
            "          *.Config.Labels*)\n"
            f"            printf '%s\\n' {WANT} ;;\n"
            f"          *) printf 'sha256:%s\\n' {'2' * 64} ;;\n"
            "        esac ;;\n"
            "      prune) exit 0 ;;\n"
            "    esac ;;\n"
            "  tag)\n"
            '    case "$3" in\n'
            '      *:latest) ' + ("exit 1" if promote_fails else "exit 0") + " ;;\n"
            "    esac\n"
            "    exit 0 ;;\n"
            "  rmi) exit 0 ;;\n"
            "  images) cat " + str(self.tags) + " ;;\n"
            "  untag) exit 0 ;;\n"
            "esac\n"
            "exit 0\n"
        )

    def run_script(self, promote_fails=False):
        # A host serving an older commit takes the promote path; the tag it is
        # serving becomes `previous` before the serving tag moves.
        (self.state / "deployed-sha").write_text(f"{OLD}\n")
        (self.bin / "podman").write_text(self.engine(promote_fails))
        (self.bin / "podman").chmod(0o755)
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
            CI_BURST_RETAIN_BUILDS="1",
            PATH=f"{self.bin}:/usr/local/bin:/usr/bin:/bin",
        )
        return subprocess.run(
            ["sh", str(SCRIPT)], capture_output=True, text=True, timeout=120, env=environment
        )

    def test_a_successful_promote_untags_only_outside_the_retained_set(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertIn(f"promoted: {WANT}", done.stdout)
        untagged = self.untagged()
        self.assertIn("localhost/ci-burst:sha-cccccccccccc", untagged)
        self.assertIn("localhost/ci-burst:sha-" + OLD[:12], untagged)
        self.assertNotIn("localhost/ci-burst:sha-" + SHORT, untagged)
        self.assertNotIn("localhost/ci-burst:sha-dddddddddddd", untagged)

    def test_a_failed_promote_removes_nothing(self):
        done = self.run_script(promote_fails=True)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn(f"promote_failed: {WANT}", done.stdout)
        self.assertEqual(self.untagged(), [])
        self.assertEqual(
            [line for line in self.call_lines() if line.startswith("image prune")], []
        )
