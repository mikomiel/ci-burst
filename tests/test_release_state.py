"""Release metadata agreement for the release-please boundary; no network.

release-please owns the version, ``CHANGELOG.md`` and the release tag. It
updates ``pyproject.toml``, ``ci_burst/__init__.py``, the changelog and the
manifest in one release pull request, so those four must agree before and
after that pull request. This test fails when a hand edit makes one of them
describe a different version.

The test does not prove a release, a tag, a deployment or runtime behaviour.
"""

import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "release-please-config.json").read_text(encoding="utf-8"))
MANIFEST = json.loads((ROOT / ".release-please-manifest.json").read_text(encoding="utf-8"))
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
INIT = (ROOT / "ci_burst" / "__init__.py").read_text(encoding="utf-8")

PACKAGE = "ci-burst"
VERSION = r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?)"
RELEASED_HEADING = re.compile(rf"^## \[{VERSION}\]", re.MULTILINE)
PACKAGE_VERSION = re.compile(rf'^__version__ = "{VERSION}"$', re.MULTILINE)


class ReleaseConfigTests(unittest.TestCase):
    def test_one_python_package_is_released_from_the_repository_root(self):
        self.assertEqual(CONFIG["release-type"], "python")
        self.assertIs(CONFIG["include-v-in-tag"], True)
        self.assertIs(CONFIG["include-component-in-tag"], False)
        self.assertIs(CONFIG["bump-minor-pre-major"], True)
        self.assertNotIn(True, (CONFIG.get("skip-changelog"), CONFIG.get("skip-github-release")))
        self.assertEqual(list(CONFIG["packages"]), ["."])
        self.assertEqual(CONFIG["packages"]["."]["package-name"], PACKAGE)
        self.assertEqual(PYPROJECT["project"]["name"], PACKAGE)

    def test_changelog_sections_keep_the_repository_categories(self):
        sections = {
            section["type"]: section
            for section in CONFIG["packages"]["."]["changelog-sections"]
        }
        for commit_type, heading in (
            ("feat", "Added"),
            ("fix", "Fixed"),
            ("perf", "Performance"),
            ("docs", "Documentation"),
            ("ci", "CI"),
        ):
            with self.subTest(commit_type=commit_type):
                self.assertEqual(sections[commit_type]["section"], heading)
                self.assertNotIn("hidden", sections[commit_type])
        for commit_type in ("chore", "test", "refactor", "build"):
            with self.subTest(commit_type=commit_type):
                self.assertIs(sections[commit_type]["hidden"], True)


class VersionAgreementTests(unittest.TestCase):
    def test_manifest_holds_one_release_version(self):
        self.assertEqual(list(MANIFEST), ["."])
        self.assertRegex(MANIFEST["."], rf"^{VERSION}$")

    def test_manifest_project_package_and_latest_section_agree(self):
        version = MANIFEST["."]
        self.assertEqual(PYPROJECT["project"]["version"], version)
        match = PACKAGE_VERSION.search(INIT)
        self.assertIsNotNone(match, "ci_burst/__init__.py has no __version__ assignment")
        self.assertEqual(match["version"], version)
        headings = RELEASED_HEADING.findall(CHANGELOG)
        self.assertTrue(headings, "CHANGELOG.md has no released section")
        self.assertEqual(headings[0], version)


if __name__ == "__main__":
    unittest.main()
