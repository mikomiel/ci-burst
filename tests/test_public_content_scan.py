"""The public-content scan config: the private-dns-name rule must not flag the dotenv filename.

The CI `public-content` job runs pinned Gitleaks over the whole fetched history with
`.gitleaks.toml`, so a false positive on any pushed branch fails every check. The standard
dotenv filename trips the rule. The rule consumes the preceding context into the match,
so only the exact dot-prefixed dotenv filename match is exempt; prose and path-style
mentions of the bare word are exempt too, while wildcard and SRV-style DNS names, URL and
assignment uses, and all other internal names are still flagged.

This test is structural and hermetic: it replays the rule's regex plus its allowlists over
synthetic samples the way Gitleaks applies them -- allowlist regexes are searched against
the match, not full-matched -- without needing the scanner binary. Patterns are compiled
with re.ASCII because Gitleaks' Go engine treats only ASCII as word characters. Every
sample is assembled at runtime from parts, so this fixture file contains no literal that
the rule itself would flag.
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


DOTENV = dns("", "env", "local")   # the dotenv filename (dot-prefixed)
WORD = dns("env", "local")         # the same bare word


def rule(rule_id):
    for entry in CONFIG["rules"]:
        if entry["id"] == rule_id:
            return entry
    raise AssertionError(f"rule {rule_id} is missing from .gitleaks.toml")


def findings(sample: str, rule_id: str):
    """Replay the rule regex and its match/secret allowlists like Gitleaks does.

    Gitleaks' Go engine treats \\w and \\b as ASCII-only, so the patterns are
    compiled with re.ASCII to keep the oracle faithful (a Unicode letter before
    a hostname is a non-word prefix character, exactly as in Go).
    """
    entry = rule(rule_id)
    pattern = re.compile(entry["regex"], re.ASCII)
    secret_group = entry.get("secretGroup")
    results = []
    for match in pattern.finditer(sample):
        secret = match.group(secret_group) if secret_group else match.group(0)
        suppressed = False
        for allowlist in entry.get("allowlists", []):
            target = match.group(0) if allowlist.get("regexTarget") == "match" else secret
            if any(re.search(regex, target, re.ASCII) for regex in allowlist.get("regexes", [])):
                suppressed = True
        if not suppressed:
            results.append(match.group(0))
    return results


class PrivateDnsNameTests(unittest.TestCase):
    def test_dotenv_filename_is_exempt(self):
        # Only the exact dot-prefixed filename match is exempt.
        self.assertEqual(findings(f'"{DOTENV}",', "private-dns-name"), [])
        self.assertEqual(findings(DOTENV + "\n", "private-dns-name"), [])

    def test_wildcard_and_srv_dns_names_still_flag(self):
        # A leading dot must not hide wildcard or service-discovery names.
        samples = (
            f'hosts: ["*.{dns("example", "internal")}"]',
            dns("_ldap", "_tcp", "example", "internal"),
        )
        for sample in samples:
            self.assertTrue(findings(sample, "private-dns-name"),
                            f"{sample!r} must still be flagged")

    def test_prose_and_path_mentions_of_the_bare_word_are_exempt(self):
        samples = (
            f"the bare word `{WORD}` in prose",
            f'quoted "{WORD}" in prose',
            f"space {WORD} in prose",
            f"path {dns('config')}/{WORD} sample",
        )
        for sample in samples:
            self.assertEqual(findings(sample, "private-dns-name"), [],
                             f"{sample!r} is prose/a path and must be exempt")

    def test_url_and_assignment_uses_still_flag(self):
        self.assertEqual(findings(f"SERVICE_URL=http://{WORD}:8080", "private-dns-name"),
                         ["/" + WORD])
        self.assertEqual(findings(f"HOST={WORD}", "private-dns-name"), ["=" + WORD])

    def test_other_internal_names_still_flag(self):
        samples = (
            f"host {dns('db', 'internal')};",
            dns("foo", "lan"),
            dns("db", "local"),
            dns("x", "env", "local"),
            dns("foo", "env", "local"),
        )
        for sample in samples:
            found = findings(sample, "private-dns-name")
            self.assertTrue(found and sample.split()[-1].rstrip('"]').rstrip(";") in found[-1],
                            f"{sample!r} must still be flagged")

    def test_unicode_letter_prefix_still_flags(self):
        # Go's regex engine treats only ASCII as word characters, so a leading
        # Unicode letter is a non-word prefix and the hostname still matches.
        sample = "é" + WORD
        self.assertEqual(findings(sample, "private-dns-name"), [sample])

    def test_allowlists_are_scoped_and_end_anchored(self):
        match_allowlists = [a for a in rule("private-dns-name").get("allowlists", [])
                            if a.get("regexTarget") == "match"]
        self.assertEqual(len(match_allowlists), 2)
        filename_regexes = match_allowlists[0]["regexes"]
        self.assertEqual(filename_regexes, [r"^\.env\.local$"])
        mention_regexes = match_allowlists[1]["regexes"]
        self.assertTrue(all(r.endswith("$") for r in mention_regexes))
        # Prose and path prefixes are exempt; URL (`/`) and assignment (`=`) are not.
        self.assertTrue(any(re.search(r, "`" + WORD, re.ASCII) for r in mention_regexes))
        self.assertTrue(any(re.search(r, dns("config") + "/" + WORD, re.ASCII)
                            for r in mention_regexes))
        self.assertFalse(any(re.search(r, "/" + WORD, re.ASCII) for r in mention_regexes))
        self.assertFalse(any(re.search(r, "=" + WORD, re.ASCII) for r in mention_regexes))

    def test_no_commit_scoped_exemptions(self):
        # The rule must stay fully armed for every commit; exemptions are by
        # match shape only, never by commit SHA.
        for entry in CONFIG["rules"]:
            for allowlist in entry.get("allowlists", []):
                self.assertNotIn("commits", allowlist)

    def test_private_ipv4_rule_is_untouched(self):
        entry = rule("private-ipv4")
        self.assertNotIn("allowlists", entry)
        sample = ".".join(("10", "1", "2", "3"))
        self.assertEqual(findings(sample, "private-ipv4"), [sample])


if __name__ == "__main__":
    unittest.main()
