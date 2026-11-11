"""Contract tests for the optional router artefact.

The embedded forwarding script is executed here against stub commands, so its
argument vectors are checked rather than described. Nothing in this file proves
a real boot, a real kernel, or live reachability — the canary in
docs/router.md is the only evidence for those.
"""

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTEFACT = ROOT / "deploy" / "router" / "userdata.sh"
PRIVATE = re.compile(
    r"\b(?:10\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}"
    r"|192\.168\.[0-9]{1,3}\.[0-9]{1,3}"
    r"|172\.(?:1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})\b"
)

FORWARD_START = 'cat > "${FORWARD}" <<\'FORWARD\'\n'
FORWARD_END = "\nFORWARD\n"


def without_comments(text):
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def forward_script(text):
    """The forwarding script exactly as the artefact installs it."""
    start = text.index(FORWARD_START) + len(FORWARD_START)
    end = text.index(FORWARD_END, start)
    return text[start:end] + "\n"


def write_stub(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


class RouterArtefactTests(unittest.TestCase):
    def setUp(self):
        self.text = ARTEFACT.read_text()

    def test_the_file_is_published(self):
        self.assertTrue(ARTEFACT.is_file())
        self.assertTrue(self.text.startswith("#!/bin/bash\n"))

    def test_no_private_address_outside_a_comment(self):
        self.assertIsNone(PRIVATE.search(without_comments(self.text)))

    def test_one_parameter_holds_every_deployment_value(self):
        self.assertIn("/ci-burst/router/config", self.text)
        self.assertIn('CONFIG_PARAMETER="${CI_BURST_ROUTER_CONFIG_PARAMETER:-', self.text)
        self.assertNotIn("PrivateKey", without_comments(self.text))

    def test_region_is_resolved_before_the_ssm_read(self):
        self.assertIn("placement/region", self.text)
        self.assertLess(self.text.index("placement/region"), self.text.index("aws ssm get-parameter"))
        self.assertIn('--region "${REGION}"', self.text)

    def test_a_boot_unit_is_enabled_to_run_the_forwarding_script(self):
        self.assertIn("systemctl enable --now ci-burst-router-forward.service", self.text)
        self.assertIn("RemainAfterExit=yes", self.text)
        self.assertIn("ExecStart=/usr/local/sbin/ci-burst-router-forward", self.text)

    def test_the_rule_helper_puts_the_table_before_the_command(self):
        # -C and -A take the chain immediately; a table selector after them is
        # parsed as the chain name. The helper must keep the order fixed.
        self.assertIn('iptables -t "${table}" -C "$@"', self.text)
        self.assertIn('iptables -t "${table}" -A "$@"', self.text)
        self.assertNotIn('ensure -t nat', self.text)

    def test_wireguard_is_started_only_when_it_is_configured(self):
        self.assertIn('if [ -s "${WG_FILE}" ]', self.text)
        self.assertIn("systemctl enable --now wg-quick@wg0.service", self.text)

    def test_configuration_is_written_private_and_shredded(self):
        self.assertIn("chmod 600", self.text)
        self.assertIn("shred -u", self.text)
        self.assertIn("trap ", self.text)


class RouterForwardingTests(unittest.TestCase):
    """Execute the shipped forwarding script with stubbed system commands."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "iptables.log"
        self.log.write_text("")
        self.cidrs = self.root / "worker-cidrs"
        self.cidrs.write_text("192.0.2.0/24\n198.51.100.0/24\n")
        self.script = self.root / "forward.sh"
        self.script.write_text(forward_script(ARTEFACT.read_text()), encoding="utf-8")
        write_stub(
            self.bin,
            "ip",
            'case "$*" in\n'
            '  "route show default") echo "default via 192.0.2.1 dev eth0" ;;\n'
            '  "link show wg0") [ -n "${WG_PRESENT:-}" ] && exit 0; exit 1 ;;\n'
            "esac\n"
            "exit 0\n",
        )
        write_stub(self.bin, "sysctl", "exit 0\n")
        write_stub(
            self.bin,
            "iptables",
            'printf "%s\\n" "$*" >> "$IPTABLES_LOG"\n'
            'case "$*" in\n'
            '  *" -C "*) exit "${IPTABLES_CHECK_EXIT:-1}" ;;\n'
            "esac\n"
            "exit 0\n",
        )

    def run_forward(self, check_exit="1", wireguard=True):
        environment = dict(os.environ)
        environment.pop("WG_PRESENT", None)
        environment.update(
            {
                "PATH": f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
                "IPTABLES_LOG": str(self.log),
                "IPTABLES_CHECK_EXIT": check_exit,
                "CI_BURST_ROUTER_CIDR_FILE": str(self.cidrs),
            }
        )
        if wireguard:
            environment["WG_PRESENT"] = "1"
        return subprocess.run(
            ["bash", str(self.script)], env=environment, capture_output=True, text=True, check=False
        )

    def appended(self):
        return [line for line in self.log.read_text().splitlines() if " -A " in f" {line} "]

    def test_every_cidr_is_masqueraded_on_both_interfaces(self):
        completed = self.run_forward()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        appended = self.appended()
        for cidr in ("192.0.2.0/24", "198.51.100.0/24"):
            self.assertIn(f"-t nat -A POSTROUTING -s {cidr} -o eth0 -j MASQUERADE", appended)
            self.assertIn(f"-t nat -A POSTROUTING -s {cidr} -o wg0 -j MASQUERADE", appended)
            self.assertIn(f"-t filter -A FORWARD -s {cidr} -o eth0 -j ACCEPT", appended)
            self.assertIn(f"-t filter -A FORWARD -s {cidr} -o wg0 -j ACCEPT", appended)

    def test_without_wireguard_only_the_default_interface_is_configured(self):
        completed = self.run_forward(wireguard=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        appended = self.appended()
        for cidr in ("192.0.2.0/24", "198.51.100.0/24"):
            self.assertIn(f"-t nat -A POSTROUTING -s {cidr} -o eth0 -j MASQUERADE", appended)
            self.assertFalse([line for line in appended if "-o wg0" in line])

    def test_existing_rules_are_not_appended_again(self):
        completed = self.run_forward(check_exit="0")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(self.appended(), [])


if __name__ == "__main__":
    unittest.main()
