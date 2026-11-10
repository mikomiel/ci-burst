"""Changelog integrity and renderer contracts; no network.

The test reads the committed CHANGELOG.md and the local Git refs only. It
does not prove a release, a deployment or runtime behavior.
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import render_changelog as changelog

TEXT = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
RELEASED = re.compile(r"^## \[(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)\]", re.MULTILINE)


def git(*arguments):
    return subprocess.run(
        ("git", "-C", str(ROOT), *arguments),
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def tags():
    """Tag name to object type; annotated tags report ``tag``."""
    output = git("for-each-ref", "refs/tags", "--format=%(refname:short)%09%(objecttype)")
    found = {}
    for line in output.splitlines():
        name, _, kind = line.partition("\t")
        found[name] = kind
    return found


def shallow():
    try:
        return git("rev-parse", "--is-shallow-repository").strip() == "true"
    except (subprocess.CalledProcessError, OSError):
        return False


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

    def test_every_released_heading_has_a_matching_annotated_tag(self):
        versions = RELEASED.findall(TEXT)
        if not versions:
            return  # No released section yet: nothing to bind to a tag.
        found = tags()
        if not found and shallow():
            self.skipTest("shallow checkout has no tags; fetch tags to verify released headings")
        for version in versions:
            names = (version, f"v{version}")
            annotated = [name for name in names if found.get(name) == "tag"]
            self.assertTrue(
                annotated,
                f"released heading [{version}] needs an annotated tag, one of {names}",
            )


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
