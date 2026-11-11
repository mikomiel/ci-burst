"""Static contract tests for the optional router artefact.

These read the shipped file; they do not execute it. The behaviour that matters
— first boot, rule application, a reboot — is accepted with the canary in
docs/router.md, because a user-data script cannot be run in this repository.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTEFACT = ROOT / "deploy" / "router" / "userdata.sh"
PRIVATE = re.compile(r"\b(?:10\.(?:[0-9]{1,3}\.){2}[0-9]{1,3}|192\.168\.[0-9]{1,3}\.[0-9]{1,3}|172\.(?:1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3})\b")


def without_comments(text):
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


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
        body = without_comments(self.text)
        self.assertNotIn("PrivateKey", body)

    def test_region_is_resolved_before_the_ssm_read(self):
        self.assertIn("placement/region", self.text)
        self.assertLess(self.text.index("placement/region"), self.text.index("aws ssm get-parameter"))
        self.assertIn('--region "${REGION}"', self.text)

    def test_boot_unit_is_enabled_and_runs_the_forwarding_script(self):
        self.assertIn("systemctl enable --now ci-burst-router-forward.service", self.text)
        self.assertIn("RemainAfterExit=yes", self.text)
        self.assertIn("ExecStart=/usr/local/sbin/ci-burst-router-forward", self.text)

    def test_rule_script_checks_the_rule_before_appending_it(self):
        self.assertIn("iptables -C", self.text)
        self.assertIn("worker-cidrs", self.text)

    def test_wireguard_is_started_only_when_it_is_configured(self):
        self.assertIn('if [ -s "${WG_FILE}" ]', self.text)
        self.assertIn("systemctl enable --now wg-quick@wg0.service", self.text)

    def test_configuration_file_is_shredded_by_the_exit_trap(self):
        self.assertIn("shred -u", self.text)
        self.assertIn("chmod 600", self.text)


if __name__ == "__main__":
    unittest.main()
