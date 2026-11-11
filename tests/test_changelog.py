"""Changelog integrity, release states and renderer contracts; no network.

The test reads the committed CHANGELOG.md and local Git refs only. It does not
prove a release, a deployment or runtime behavior.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import render_changelog as changelog

TEXT = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

PREPARED = (
    "# Changelog\n\n## [Unreleased]\n\n"
    "## [0.1.0] - 2026-11-09\n\n<!-- release-pending: v0.1.0 -->\n\n"
    "### Added\n\n- Add the controller.\n"
)
PUBLISHED = PREPARED.replace("<!-- release-pending: v0.1.0 -->\n\n", "")


def git(root, *arguments):
    return subprocess.run(
        ("git", "-C", str(root), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


class ChangelogFileTests(unittest.TestCase):
    def test_root_changelog_is_marked_generated_with_provenance(self):
        self.assertTrue(TEXT.startswith("# Changelog\n"))
        self.assertIn(changelog.GENERATED_MARKER, TEXT)
        self.assertIn(changelog.PROVENANCE, TEXT)
        lowered = TEXT.lower()
        for phrase in ("merged source history", "not deployment", "runtime", "neither a release nor a deployment"):
            self.assertIn(phrase, lowered)

    def test_exactly_one_unreleased_section_and_it_comes_first(self):
        headings = [line for line in TEXT.splitlines() if line.startswith("## [")]
        self.assertEqual(headings.count(changelog.UNRELEASED), 1)
        self.assertEqual(headings[0], changelog.UNRELEASED)
        self.assertEqual(changelog.unreleased_section(TEXT).count(changelog.UNRELEASED), 1)

    def test_released_headings_are_prepared_or_tagged(self):
        errors = changelog.binding_errors(
            TEXT, changelog.applicable_tags(), changelog.invalid_release_refs()
        )
        self.assertEqual(
            errors,
            [],
            "invalid release state; in a shallow checkout run `git fetch --tags` first",
        )


class RepositoryStateValidationTests(unittest.TestCase):
    """The CI assertion must use the same evidence as the CLI.

    ``applicable_tags()`` filters invalid release-shaped refs out on purpose, so
    the repository-state check has to pass ``invalid_release_refs()`` as well or
    CI accepts a state the CLI rejects.
    """

    def test_the_ci_assertion_rejects_an_invalid_release_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            git(root, "init", "-q", "-b", "main")
            git(root, "config", "user.email", "changelog@example.com")
            git(root, "config", "user.name", "Changelog Test")
            (root / "file.txt").write_text("controller\n", encoding="utf-8")
            git(root, "add", "file.txt")
            git(root, "commit", "-q", "-m", "feat: add the controller")
            prepared = (
                "# Changelog\n\n## [Unreleased]\n\n"
                "## [0.1.0] - 2026-11-09\n\n"
                "<!-- release-pending: v0.1.0 -->\n\n### Added\n\n- Add the controller.\n"
            )
            (root / "CHANGELOG.md").write_text(prepared, encoding="utf-8")
            git(root, "tag", "v0.1.0")

            # Default arguments are bound at import, so pass the root explicitly.
            errors = changelog.binding_errors(
                prepared,
                changelog.applicable_tags(root),
                changelog.invalid_release_refs(root),
            )
            self.assertTrue(any("v0.1.0" in error for error in errors), errors)


class ReleaseStateTests(unittest.TestCase):
    def test_preparation_state_passes_the_pre_merge_check_without_a_tag(self):
        self.assertEqual(changelog.binding_errors(PREPARED, {}), [])

    def test_unmarked_released_heading_without_a_tag_fails(self):
        self.assertTrue(changelog.binding_errors(PUBLISHED, {}))

    def test_published_heading_accepts_a_prefixed_annotated_tag(self):
        self.assertEqual(changelog.binding_errors(PUBLISHED, {"v0.1.0": True}), [])

    def test_published_heading_needs_an_applicable_tag(self):
        self.assertTrue(changelog.binding_errors(PUBLISHED, {}))

    def test_preparation_marker_must_match_its_heading(self):
        text = PREPARED.replace("release-pending: v0.1.0", "release-pending: v0.2.0")
        self.assertTrue(changelog.binding_errors(text, {}))

    def test_more_than_one_preparation_marker_fails(self):
        text = PREPARED.replace(
            "## [Unreleased]",
            "## [Unreleased]\n\n## [0.2.0] - 2026-11-10\n\n<!-- release-pending: v0.2.0 -->",
        )
        errors = changelog.binding_errors(text, {})
        self.assertTrue(any("more than one release" in error for error in errors))

    def test_validation_rejects_a_remaining_preparation_marker(self):
        self.assertTrue(changelog.release_validation_errors(PREPARED, {"v0.1.0": True}))

    def test_validation_accepts_a_published_bound_heading(self):
        self.assertEqual(changelog.release_validation_errors(PUBLISHED, {"v0.1.0": True}), [])

    def test_write_is_blocked_while_the_preparation_tag_does_not_exist(self):
        self.assertIsNotNone(changelog.write_blocked(PREPARED, None))
        self.assertIsNone(changelog.write_blocked(PREPARED, "v0.1.0"))
        self.assertIsNone(changelog.write_blocked("# Changelog\n\n## [Unreleased]\n", None))

    def test_duplicate_release_headings_are_refused(self):
        text = PREPARED + "\n## [0.1.0] - 2026-11-10\n\n### Fixed\n\n- Again.\n"
        errors = changelog.binding_errors(text, {})
        self.assertTrue(any("duplicate" in error for error in errors))

    def test_malformed_release_heading_is_refused(self):
        headings = (
            "## [0.1] - 2026-11-09",
            "## [01.2.3] - 2026-11-09",
            "## [0.1.0]",
            "## [0.1.0]garbage",
        )
        for heading in headings:
            with self.subTest(heading=heading):
                text = f"# Changelog\n\n## [Unreleased]\n\n{heading}\n\n- One.\n"
                errors = changelog.binding_errors(text, {})
                self.assertTrue(any("noncanonical" in error for error in errors), errors)

    def test_noncanonical_heading_cannot_bypass_validation_or_write_guard(self):
        for heading in ("##  [0.1.0] - 2026-11-09", "##\t[0.1.0] - 2026-11-09", " ## [0.1.0] - 2026-11-09"):
            with self.subTest(heading=heading):
                text = (
                    "# Changelog\n\n## [Unreleased]\n\n"
                    f"{heading}\n\n<!-- release-pending: v0.1.0 -->\n\n- One.\n"
                )
                self.assertTrue(changelog.binding_errors(text, {}))
                self.assertTrue(changelog.release_validation_errors(text, {}))
                self.assertIsNotNone(changelog.write_blocked(text, None))
                self.assertEqual(
                    changelog.unreleased_section(text).strip(),
                    changelog.UNRELEASED,
                )


    def test_marker_outside_a_released_section_is_refused(self):
        for text in (
            "# Changelog\n\n## [Unreleased]\n\n<!-- release-pending: v0.1.0 -->\n\n- One.\n",
            "# Changelog\n\n<!-- release-pending: v0.1.0 -->\n\n## [Unreleased]\n",
        ):
            with self.subTest(text=text):
                self.assertTrue(changelog.binding_errors(text, {}))
                self.assertTrue(changelog.release_validation_errors(text, {}))


class ReleaseBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "changelog@example.com")
        self.git("config", "user.name", "Changelog Test")

    def git(self, *arguments):
        return git(self.root, *arguments)

    def commit(self, subject, filename="file.txt"):
        (self.root / filename).write_text(subject + "\n", encoding="utf-8")
        self.git("add", filename)
        self.git("commit", "-q", "-m", subject)

    def add_renderer(self):
        scripts = self.root / "scripts"
        scripts.mkdir(exist_ok=True)
        shutil.copyfile(
            ROOT / "scripts" / "render_changelog.py",
            scripts / "render_changelog.py",
        )

    def clone(self, name, *options):
        target = Path(self.temporary.name) / name
        subprocess.run(
            ("git", "clone", "--quiet", *options, f"file://{self.root}", str(target)),
            check=True,
            capture_output=True,
            text=True,
        )
        return target

    def run_write(self, root):
        return subprocess.run(
            (sys.executable, "scripts/render_changelog.py", "--write"),
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

    def run_check(self, root):
        return subprocess.run(
            (sys.executable, "scripts/render_changelog.py", "--check"),
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_no_release_tag_means_no_boundary(self):
        self.commit("feat: add the controller (#4)")
        self.assertIsNone(changelog.release_tag(self.root))

    def test_boundary_problem_accepts_a_reconciled_published_section(self):
        self.commit("feat: add the controller (#4)")
        released = (
            "# Changelog\n\n## [Unreleased]\n\n"
            "## [0.1.0] - 2026-11-09\n\n### Added\n\n- Add the controller.\n"
        )
        (self.root / "CHANGELOG.md").write_text(released, encoding="utf-8")
        self.git("add", "CHANGELOG.md")
        self.git("commit", "-q", "-m", "docs: publish the 0.1.0 release")
        self.git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        self.assertIsNone(changelog.boundary_problem(released, self.root))

    def test_boundary_problem_refuses_a_released_section_without_its_tag(self):
        self.commit("feat: add the controller")
        released = "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-11-09\n\n- One.\n"
        self.assertIn("reachable from HEAD", changelog.boundary_problem(released, self.root))

    def test_boundary_problem_refuses_a_tag_without_its_released_section(self):
        self.commit("feat: add the controller")
        self.git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        self.assertIn(
            "no released section",
            changelog.boundary_problem("# Changelog\n\n## [Unreleased]\n", self.root),
        )

    def test_write_refuses_a_no_tags_clone_with_a_published_section(self):
        released = (
            "# Changelog\n\n## [Unreleased]\n\n"
            "## [0.1.0] - 2026-11-09\n\n### Added\n\n- Add the controller.\n"
        )
        (self.root / "CHANGELOG.md").write_text(released, encoding="utf-8")
        self.add_renderer()
        self.git("add", "CHANGELOG.md", "scripts/render_changelog.py")
        self.git("commit", "-q", "-m", "docs: publish the 0.1.0 release")
        self.git("tag", "-a", "v0.1.0", "-m", "v0.1.0")

        clone = self.clone("no-tags", "--no-tags")
        self.assertEqual(changelog.tag_types(clone), {})
        self.assertEqual(changelog.applicable_tags(clone), {})
        completed = self.run_write(clone)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("reachable from HEAD", completed.stderr)
        self.assertEqual((clone / "CHANGELOG.md").read_text(encoding="utf-8"), released)

    def test_write_refuses_a_shallow_clone(self):
        self.commit("feat: add the controller")
        text = "# Changelog\n\n## [Unreleased]\n"
        (self.root / "CHANGELOG.md").write_text(text, encoding="utf-8")
        self.add_renderer()
        self.git("add", "CHANGELOG.md", "scripts/render_changelog.py")
        self.git("commit", "-q", "-m", "docs: add the changelog")

        clone = self.clone("shallow", "--depth", "1")
        self.assertTrue(changelog.is_shallow(clone))
        completed = self.run_write(clone)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("shallow", completed.stderr)
        self.assertEqual((clone / "CHANGELOG.md").read_text(encoding="utf-8"), text)

    def test_check_reports_a_stale_section_and_names_write(self):
        released = (
            "# Changelog\n\n## [Unreleased]\n\n"
            "## [0.1.0] - 2026-11-09\n\n### Added\n\n- Add the controller.\n"
        )
        self.commit("feat: add the controller (#4)")
        (self.root / "CHANGELOG.md").write_text(released, encoding="utf-8")
        self.add_renderer()
        self.git("add", "CHANGELOG.md", "scripts/render_changelog.py")
        self.git("commit", "-q", "-m", "chore(release): prepare the 0.1.0 section")
        self.git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        self.commit("fix: correct the quote timeout (#9)", "fix.txt")

        completed = self.run_check(self.root)
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("- Correct the quote timeout", completed.stdout)
        self.assertNotIn("controller", completed.stdout)
        self.assertIn("stale", completed.stderr)
        self.assertIn("python3 scripts/render_changelog.py --write", completed.stderr)
        self.assertEqual(
            (self.root / "CHANGELOG.md").read_text(encoding="utf-8"), released
        )

    def test_check_accepts_an_up_to_date_section(self):
        section = (
            "## [Unreleased]\n\n### Fixed\n\n"
            "- Correct the quote timeout ([#9](https://github.com/mikomiel/ci-burst/pull/9)).\n"
        )
        text = (
            "# Changelog\n\n"
            + section
            + "\n## [0.1.0] - 2026-11-09\n\n### Added\n\n- Add the controller.\n"
        )
        self.commit("feat: add the controller (#4)")
        (self.root / "CHANGELOG.md").write_text(text, encoding="utf-8")
        self.add_renderer()
        self.git("add", "CHANGELOG.md", "scripts/render_changelog.py")
        self.git("commit", "-q", "-m", "chore(release): prepare the 0.1.0 section")
        self.git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        self.commit("fix: correct the quote timeout (#9)", "fix.txt")

        rendered = changelog.render_unreleased(
            changelog.merged_subjects(self.root, since="v0.1.0")
        )
        self.assertEqual(rendered, section, "fixture must match the renderer")
        completed = self.run_check(self.root)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout, "changelog is up to date\n")
        self.assertEqual(completed.stderr, "")
        self.assertEqual((self.root / "CHANGELOG.md").read_text(encoding="utf-8"), text)

    def test_check_refuses_a_shallow_clone(self):
        self.commit("feat: add the controller")
        text = "# Changelog\n\n## [Unreleased]\n"
        (self.root / "CHANGELOG.md").write_text(text, encoding="utf-8")
        self.add_renderer()
        self.git("add", "CHANGELOG.md", "scripts/render_changelog.py")
        self.git("commit", "-q", "-m", "chore: add the changelog")

        clone = self.clone("shallow-check", "--depth", "1")
        self.assertTrue(changelog.is_shallow(clone))
        completed = self.run_check(clone)
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("shallow", completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual((clone / "CHANGELOG.md").read_text(encoding="utf-8"), text)

    def test_check_keeps_the_preparation_marker_refusal(self):
        prepared = (
            "# Changelog\n\n## [Unreleased]\n\n"
            "## [0.1.0] - 2026-11-09\n\n<!-- release-pending: v0.1.0 -->\n\n"
            "### Added\n\n- Add the controller.\n"
        )
        self.commit("feat: add the controller")
        (self.root / "CHANGELOG.md").write_text(prepared, encoding="utf-8")
        self.add_renderer()
        self.git("add", "CHANGELOG.md", "scripts/render_changelog.py")
        self.git("commit", "-q", "-m", "chore(release): prepare the 0.1.0 section")

        completed = self.run_check(self.root)
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("release preparation for v0.1.0 is pending", completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(
            (self.root / "CHANGELOG.md").read_text(encoding="utf-8"), prepared
        )

    def test_release_boundary_excludes_released_commits_from_unreleased(self):
        self.commit("feat: add the controller (#4)")
        released = (
            "# Changelog\n\n## [Unreleased]\n\n"
            "## [0.1.0] - 2026-11-09\n\n<!-- release-pending: v0.1.0 -->\n\n"
            "### Added\n\n- Add the controller ([#4](https://github.com/mikomiel/ci-burst/pull/4)).\n"
        )
        (self.root / "CHANGELOG.md").write_text(released, encoding="utf-8")
        self.git("add", "CHANGELOG.md")
        self.git("commit", "-q", "-m", "docs: prepare the 0.1.0 release")
        self.git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        self.commit("fix: correct the quote timeout (#9)", "fix.txt")

        boundary = changelog.release_tag(self.root)
        self.assertEqual(boundary, "v0.1.0")
        section = changelog.render_unreleased(changelog.merged_subjects(self.root, since=boundary))
        self.assertIn("- Correct the quote timeout", section)
        self.assertNotIn("controller", section)

        updated = changelog.replace_unreleased(released, section)
        self.assertIn("## [0.1.0] - 2026-11-09", updated)
        self.assertIn("Add the controller", updated.split("## [0.1.0]")[1])
        self.assertNotIn("Add the controller", changelog.unreleased_section(updated))
        self.assertIn("- Correct the quote timeout", changelog.unreleased_section(updated))

    def test_lightweight_release_tag_is_refused(self):
        self.commit("feat: add the controller")
        self.git("tag", "v0.1.0")
        with self.assertRaises(ValueError):
            changelog.release_tag(self.root)

    def test_unprefixed_release_tag_is_refused(self):
        self.commit("feat: add the controller")
        self.git("tag", "-a", "0.1.0", "-m", "0.1.0")
        with self.assertRaises(ValueError):
            changelog.release_tag(self.root)

    def test_leading_zero_release_tag_is_refused(self):
        self.commit("feat: add the controller")
        self.git("tag", "-a", "v01.2.3", "-m", "v01.2.3")
        with self.assertRaises(ValueError):
            changelog.release_tag(self.root)
        self.assertEqual(changelog.applicable_tags(self.root), {})

    def test_applicable_tags_require_annotated_reachable_prefixed_tags(self):
        self.commit("feat: add the controller")
        self.assertEqual(changelog.applicable_tags(self.root), {})
        self.git("tag", "v0.1.0")
        self.assertEqual(changelog.applicable_tags(self.root), {})
        self.git("tag", "-d", "v0.1.0")
        self.git("tag", "-a", "0.1.0", "-m", "0.1.0")
        self.assertEqual(changelog.applicable_tags(self.root), {})
        self.git("tag", "-d", "0.1.0")
        self.git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        self.assertEqual(changelog.applicable_tags(self.root), {"v0.1.0": False})

    def test_invalid_release_tags_are_rejected_not_ignored(self):
        text = "# Changelog\n\n## [Unreleased]\n\n"
        self.commit("feat: add the controller")
        self.add_renderer()
        cases = {
            "v0.1.0": ("tag", "v0.1.0"),
            "0.1.0": ("tag", "-a", "0.1.0", "-m", "0.1.0"),
            "v01.2.3": ("tag", "-a", "v01.2.3", "-m", "v01.2.3"),
        }
        for name, command in cases.items():
            with self.subTest(tag=name):
                self.git(*command)
                try:
                    invalid = changelog.invalid_release_refs(self.root)
                    tags = changelog.applicable_tags(self.root)
                    self.assertIn(name, invalid)
                    self.assertEqual({}, tags)
                    for errors in (
                        changelog.binding_errors(text, tags, invalid),
                        changelog.release_validation_errors(text, tags, invalid),
                    ):
                        self.assertTrue(
                            any(name in error for error in errors), errors
                        )
                    self.assertIn(name, changelog.boundary_problem(text, self.root))

                    completed = subprocess.run(
                        (sys.executable, "scripts/render_changelog.py", "--validate-release"),
                        cwd=self.root,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(1, completed.returncode, completed.stdout)
                    self.assertIn(name, completed.stderr)
                finally:
                    self.git("tag", "-d", name)

    def test_release_checks_reject_a_tag_on_an_unrelated_branch(self):
        published = (
            "# Changelog\n\n## [Unreleased]\n\n"
            "## [0.1.0] - 2026-11-09\n\n### Added\n\n- Add the controller.\n"
        )
        self.commit("feat: add the controller")
        self.git("checkout", "-q", "-b", "side")
        self.commit("feat: side work", "side.txt")
        self.git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        self.git("checkout", "-q", "main")
        (self.root / "CHANGELOG.md").write_text(published, encoding="utf-8")
        self.add_renderer()

        tags = changelog.applicable_tags(self.root)
        self.assertEqual(tags, {})
        self.assertTrue(changelog.binding_errors(published, tags))
        self.assertTrue(changelog.release_validation_errors(published, tags))
        self.assertIn("reachable from HEAD", changelog.boundary_problem(published, self.root))

        completed = subprocess.run(
            (sys.executable, "scripts/render_changelog.py", "--validate-release"),
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("reachable from HEAD", completed.stderr)

    def test_noncanonical_heading_cannot_bypass_the_checks(self):
        text = (
            "# Changelog\n\n## [Unreleased]\n\n"
            "##  [0.1.0] - 2026-11-09\n\n<!-- release-pending: v0.1.0 -->\n\n- One.\n"
        )
        (self.root / "CHANGELOG.md").write_text(text, encoding="utf-8")
        self.add_renderer()
        for arguments in (("--validate-release",), ("--write",)):
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    (sys.executable, "scripts/render_changelog.py", *arguments),
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                self.assertIn("noncanonical release heading", completed.stderr)
        self.assertEqual((self.root / "CHANGELOG.md").read_text(encoding="utf-8"), text)

    def test_release_checks_reject_a_reachable_tag_without_a_section(self):
        self.commit("feat: add the controller")
        self.git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        text = "# Changelog\n\n## [Unreleased]\n"
        (self.root / "CHANGELOG.md").write_text(text, encoding="utf-8")
        self.add_renderer()

        tags = changelog.applicable_tags(self.root)
        self.assertEqual(tags, {"v0.1.0": False})
        self.assertTrue(changelog.binding_errors(text, tags))
        self.assertTrue(changelog.release_validation_errors(text, tags))
        self.assertIn("no released section", changelog.boundary_problem(text, self.root))

        for arguments in (("--validate-release",), ("--write",)):
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    (sys.executable, "scripts/render_changelog.py", *arguments),
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                self.assertIn("no released section", completed.stderr)
        self.assertEqual((self.root / "CHANGELOG.md").read_text(encoding="utf-8"), text)

    def test_release_checks_reject_a_tag_before_the_release_preparation(self):
        self.commit("feat: add the controller")
        self.git("tag", "-a", "v0.1.0", "-m", "v0.1.0")
        self.commit("feat: add the quote check (#9)", "feature.txt")
        published = (
            "# Changelog\n\n## [Unreleased]\n\n"
            "## [0.1.0] - 2026-11-09\n\n### Added\n\n- Add the quote check.\n"
        )
        (self.root / "CHANGELOG.md").write_text(published, encoding="utf-8")
        self.git("add", "CHANGELOG.md")
        self.git("commit", "-q", "-m", "docs: publish the 0.1.0 section")
        self.add_renderer()

        tags = changelog.applicable_tags(self.root)
        self.assertEqual(tags, {"v0.1.0": False})
        for errors in (
            changelog.binding_errors(published, tags),
            changelog.release_validation_errors(published, tags),
        ):
            self.assertTrue(any("does not contain" in error for error in errors), errors)
        self.assertIn("does not contain", changelog.boundary_problem(published, self.root))

        # The preparation marker waives only the tag's absence: an existing tag
        # that predates the section must still fail closed.
        with_marker = published.replace(
            "## [0.1.0] - 2026-11-09",
            "## [0.1.0] - 2026-11-09\n\n<!-- release-pending: v0.1.0 -->",
        )
        for errors in (
            changelog.binding_errors(with_marker, tags),
            changelog.release_validation_errors(with_marker, tags),
        ):
            self.assertTrue(any("does not contain" in error for error in errors), errors)
        self.assertIn("does not contain", changelog.boundary_problem(with_marker, self.root))

        for arguments in (("--validate-release",), ("--write",)):
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    (sys.executable, "scripts/render_changelog.py", *arguments),
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
                self.assertIn("does not contain", completed.stderr)
        self.assertEqual((self.root / "CHANGELOG.md").read_text(encoding="utf-8"), published)


class RenderTests(unittest.TestCase):
    def test_categories_and_pull_request_links(self):
        section = changelog.render_unreleased(
            [
                "ci: gate merges (#2)",
                "fix(ci): read statuses (#3)",
                "docs: explain setup (#5)",
                "feat!: add the controller (#4)",
                "chore: bootstrap the workspace",
                "Merge pull request #9 from example/branch",
            ]
        )
        self.assertTrue(section.startswith("## [Unreleased]\n"))
        self.assertIn(
            "### Added\n\n- Add the controller ([#4](https://github.com/mikomiel/ci-burst/pull/4)).\n",
            section,
        )
        self.assertIn(
            "### Fixed\n\n- Read statuses ([#3](https://github.com/mikomiel/ci-burst/pull/3)).\n",
            section,
        )
        self.assertIn(
            "### Documentation\n\n- Explain setup ([#5](https://github.com/mikomiel/ci-burst/pull/5)).\n",
            section,
        )
        self.assertIn(
            "### CI\n\n- Gate merges ([#2](https://github.com/mikomiel/ci-burst/pull/2)).\n",
            section,
        )
        for absent in ("bootstrap", "Merge pull request"):
            self.assertNotIn(absent, section)

    def test_category_order_is_stable(self):
        section = changelog.render_unreleased(
            ["ci: one", "docs: two", "fix: three", "feat: four"]
        )
        positions = [section.index(f"### {name}") for name in ("Added", "Fixed", "Documentation", "CI")]
        self.assertEqual(positions, sorted(positions))

    def test_entries_keep_subject_order_without_a_pull_request(self):
        section = changelog.render_unreleased(["docs: first", "docs: second"])
        self.assertLess(section.index("- First."), section.index("- Second."))
        self.assertNotIn("github.com", section)

    def test_replacement_keeps_released_sections(self):
        existing = (
            "# Changelog\n\n## [Unreleased]\n\n- Old entry.\n\n"
            "## [0.1.0] - 2026-01-01\n\n### Added\n\n- Released entry.\n"
        )
        updated = changelog.replace_unreleased(
            existing, "## [Unreleased]\n\n### Fixed\n\n- New entry.\n"
        )
        self.assertIn("- New entry.", updated)
        self.assertNotIn("- Old entry.", updated)
        self.assertIn("## [0.1.0] - 2026-01-01", updated)
        self.assertIn("- Released entry.", updated)
        self.assertEqual(
            changelog.unreleased_section(updated),
            "## [Unreleased]\n\n### Fixed\n\n- New entry.\n",
        )


if __name__ == "__main__":
    unittest.main()
