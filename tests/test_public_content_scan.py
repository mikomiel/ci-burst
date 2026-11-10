"""The public-content scan config: the private-dns-name rule must not flag the dotenv filename.

The CI `public-content` job runs pinned Gitleaks over the whole fetched history with
`.gitleaks.toml`, so a false positive on any pushed branch fails every check. The standard
dotenv filename `.env.local` trips the rule: its match there is the bare word `env.local`.
The allowlist carve-out suppresses exactly that secret, and only it.

This test is structural and hermetic: it replays the rule's regex plus its allowlist over
synthetic samples the way Gitleaks applies them, without needing the scanner binary.
"""

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))


def rule(rule_id):
    for entry in CONFIG["rules"]:
        if entry["id"] == rule_id:
            return entry
    raise AssertionError(f"rule {rule_id} is missing from .gitleaks.toml")


def findings(sample: str, rule_id: str):
    """Replay the rule regex and its secret-targeted allowlists like Gitleaks does."""
    entry = rule(rule_id)
    pattern = re.compile(entry["regex"])
    allowed = [re.compile(a) for a in entry.get("allowlists", []) for a in
               (a["regexes"] if a.get("regexTarget", "secret") == "secret" else [])]
    return [m.group(0) for m in pattern.finditer(sample)
            if not any(a.fullmatch(m.group(0)) for a in allowed)]


class PrivateDnsNameTests(unittest.TestCase):
    def test_dotenv_local_filename_is_allowlisted(self):
        # The false positive that failed CI on an unrelated branch: a forbidden-path
        # tuple entry `".env.local"` matches as the bare word `env.local`.
        self.assertEqual(findings('".env.local",', "private-dns-name"), [])
        self.assertEqual(findings("config/env.local", "private-dns-name"), [])

    def test_real_internal_names_still_flag(self):
        # Synthetic samples; each carries gitleaks:allow so this fixture file does
        # not trip the repo's own public-content scan (they are not real hosts).
        samples = ("host db.internal;", "foo.lan", '".env.lan"', "db.local")  # gitleaks:allow
        for sample in samples:
            self.assertTrue(findings(sample, "private-dns-name"),
                            f"{sample!r} must still be flagged")

    def test_carveout_is_exactly_the_bare_dotenv_word(self):
        entry = rule("private-dns-name")
        allowlists = entry.get("allowlists", [])
        self.assertEqual(len(allowlists), 1)
        self.assertEqual(allowlists[0].get("regexTarget"), "secret")
        secret_regexes = [re.compile(r) for r in allowlists[0]["regexes"]]
        self.assertTrue(any(r.fullmatch("env.local") for r in secret_regexes))
        self.assertFalse(any(r.fullmatch("db.local") for r in secret_regexes))  # gitleaks:allow
        self.assertFalse(any(r.fullmatch("env.local.evil.example") for r in secret_regexes))  # gitleaks:allow

    def test_private_ipv4_rule_is_untouched(self):
        entry = rule("private-ipv4")
        self.assertNotIn("allowlists", entry)
        self.assertEqual(findings("10.1.2.3", "private-ipv4"), ["10.1.2.3"])  # gitleaks:allow


if __name__ == "__main__":
    unittest.main()
