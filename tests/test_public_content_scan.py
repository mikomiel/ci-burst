"""The public-content scan config: the private-dns-name rule must not flag the dotenv filename.

The CI `public-content` job runs pinned Gitleaks over the whole fetched history with
`.gitleaks.toml`, so a false positive on any pushed branch fails every check. The standard
dotenv filename trips the rule: its match is the dot-prefixed filename. The rule's
allowlist exempts exactly that dot-prefixed match, while the same word used as a real
hostname (in a URL, a HOST assignment, or bare) is still flagged. Commit messages are
scanned as anonymous text, so a second allowlist exempts the prose line that quotes the
filename in backticks.

This test is structural and hermetic: it replays the rule's regex plus its allowlists over
synthetic samples the way Gitleaks applies them -- allowlist regexes are searched against
the configured target, not full-matched, `\\b` is ASCII as in Go's regexp, and commit
allowlists are identity-based so they are not part of the text replay. Every sample is
assembled at runtime from parts, so this fixture file contains no literal that the rule
itself would flag.
"""

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))


def dns(*parts):
    """Assemble a dotted name at runtime so no flagged literal sits in this file."""
    return ".".join(parts)


DOTENV = dns("", "env", "local")   # the dotenv filename: a dot-prefixed match
HOSTNAME = dns("env", "local")     # the same word as a bare hostname


def rule(rule_id):
    for entry in CONFIG["rules"]:
        if entry["id"] == rule_id:
            return entry
    raise AssertionError(f"rule {rule_id} is missing from .gitleaks.toml")


def line_at(sample, start):
    begin = sample.rfind("\n", 0, start) + 1
    end = sample.find("\n", start)
    return sample[begin:end if end != -1 else len(sample)]


def findings(sample: str, rule_id: str):
    """Replay the rule regex and its allowlists like Gitleaks does."""
    entry = rule(rule_id)
    # Go's regexp treats only ASCII word characters for `\b`; Python's default
    # is Unicode-aware, so the explicit flag is what makes this oracle faithful.
    pattern = re.compile(entry["regex"], re.ASCII)
    secret_group = entry.get("secretGroup")
    results = []
    for match in pattern.finditer(sample):
        secret = match.group(secret_group) if secret_group else match.group(0)
        suppressed = False
        for allowlist in entry.get("allowlists", []):
            if allowlist.get("commits"):
                # Identity-scoped: a text replay has no commit to compare.
                continue
            target = allowlist.get("regexTarget", "secret")
            if target == "match":
                text = match.group(0)
            elif target == "line":
                text = line_at(sample, match.start())
            else:
                text = secret
            if any(re.search(regex, text, re.ASCII) for regex in allowlist.get("regexes", [])):
                suppressed = True
        if not suppressed:
            results.append(match.group(0))
    return results


class PrivateDnsNameTests(unittest.TestCase):
    def test_dotenv_filename_is_exempt(self):
        # The false positive that failed CI on an unrelated branch: a
        # forbidden-path tuple entry with the dotenv filename (dot-prefixed).
        self.assertEqual(findings(f'"{DOTENV}",', "private-dns-name"), [])
        self.assertEqual(findings(DOTENV + "\n", "private-dns-name"), [])

    def test_same_word_as_hostname_still_flags(self):
        samples = (
            f"SERVICE_URL=http://{HOSTNAME}:8080",
            f"HOST={HOSTNAME}",
            f"bare {HOSTNAME} here",
        )
        for sample in samples:
            self.assertEqual(findings(sample, "private-dns-name"), [HOSTNAME],
                             f"{sample!r} must still be flagged")

    def test_prose_line_quoting_the_filename_is_exempt(self):
        # Commit messages are scanned as anonymous text; the prose that
        # describes this false positive quotes the filename in backticks.
        prose = f"the rule matches the bare word `{HOSTNAME}` inside the dotenv filename"
        self.assertEqual(findings(prose, "private-dns-name"), [])
        # The same word on a line without backticks is still hostname-shaped
        # text and is still flagged.
        self.assertEqual(findings(f"the rule matches {HOSTNAME} inside the line", "private-dns-name"),
                         [HOSTNAME])

    def test_ascii_word_boundary_matches_go(self):
        # Go's regexp uses ASCII word characters for `\b`, so a non-ASCII
        # letter before the name is still a boundary; Python's Unicode default
        # would hide the finding, which is why the replay passes re.ASCII.
        self.assertEqual(findings("\u00e9" + HOSTNAME, "private-dns-name"), [HOSTNAME])

    def test_other_internal_names_still_flag(self):
        samples = (
            f"host {dns('db', 'internal')};",
            dns("foo", "lan"),
            f'"{dns("", "env", "lan")}"',
            dns("db", "local"),
            dns("", "db", "internal"),
        )
        for sample in samples:
            self.assertTrue(findings(sample, "private-dns-name"),
                            f"{sample!r} must still be flagged")

    def test_allowlist_is_anchored_to_the_exact_filename(self):
        match_allowlists = [a for a in rule("private-dns-name").get("allowlists", [])
                            if a.get("regexTarget") == "match"]
        self.assertEqual(len(match_allowlists), 1)
        regexes = match_allowlists[0]["regexes"]
        for regex in regexes:
            self.assertTrue(regex.startswith("^") and regex.endswith("$"),
                            f"allowlist regex {regex!r} must be anchored so it cannot broaden")
        # The anchored pattern exempts the dot-prefixed filename match and nothing else.
        self.assertTrue(all(re.search(r, DOTENV) for r in regexes))
        self.assertFalse(any(re.search(r, HOSTNAME) for r in regexes))

    def test_superseded_fix_commit_allowlist_is_one_exact_sha(self):
        # The orphaned first attempt of this fix is exempted by commit SHA only,
        # so the rule stays fully armed for every other commit. The SHA is
        # pinned: this entry was audited as false positives of this rule in that
        # commit's test comments, and a different commit would need its own audit.
        entry = rule("private-dns-name")
        commit_allowlists = [a for a in entry.get("allowlists", []) if a.get("commits")]
        self.assertEqual(len(commit_allowlists), 1)
        self.assertEqual(commit_allowlists[0]["commits"],
                         ["9858491a18f662fb27f551fe81f355b25f38d220"])

    def test_private_ipv4_rule_is_untouched(self):
        entry = rule("private-ipv4")
        self.assertNotIn("allowlists", entry)
        sample = ".".join(("10", "1", "2", "3"))
        self.assertEqual(findings(sample, "private-ipv4"), [sample])


if __name__ == "__main__":
    unittest.main()
