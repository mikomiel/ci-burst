"""Tests for the pure inputs of an automated warm-image build.

``ci_burst.image`` decides what a build is made of (the basis digest), whether
one may start (the trigger and rate-limit rules) and what the builder is told to
run (the user data). It makes no AWS call, no Forgejo call and no state write,
so these tests need no fake of either service.

The user-data stub is run only against a stand-in script, with a stub
``shutdown`` first on ``PATH``. The shipped ``warm-cache.sh`` is rendered,
parsed and decoded here but never executed: it powers its host off.
"""

import base64
import dataclasses
import gzip
import hashlib
import inspect
import re
import shlex
import shutil
import subprocess
import tempfile
import traceback
import unittest
from pathlib import Path

from ci_burst import image
from ci_burst.config import load
from ci_burst.errors import ConfigRefusal, ImageRefusal, Refusal
from ci_burst.image_state import Control

from support import write_config
from test_deploy_units import admitted

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "runner" / "image" / "warm-cache.sh"
DOCKERFILE = ROOT / "Dockerfile"

# Assembled at runtime, so no secret-shaped literal sits in this file.
SENTINEL = "-".join(("not", "a", "real", "signature", "value"))
SENTINEL_URL = f"https://example-bucket.invalid/example-prefix/archive.tar.gz?token={SENTINEL}"

NOW = 1_800_000_000.0
DAY = 86_400

IMAGE_TOML = """
[image]
enabled = true
profile = "example-image-role"
state_directory = "/var/lib/example-image-state"
[image.source]
token_env = "EXAMPLE_IMAGE_TOKEN"
basis_paths = ["example.lock"]
[image.warm]
commands = ["bash scripts/example-warm.sh", "bash scripts/example-extra.sh --flag"]
gate_command = "bash scripts/example-gate.sh"
lifetime_seconds = 3000
snapshot_seconds = 900
[image.build]
launch_template_name = "example-builder"
launch_template_version = 1
[image.storage]
bucket = "example-staging"
prefix = "example-prefix/"
"""


def load_image(toml=IMAGE_TOML):
    """The ``ImageConfig`` the real loader builds from a synthetic deployment."""
    with tempfile.TemporaryDirectory() as directory:
        path = write_config(directory)
        path.write_text(path.read_text(encoding="utf-8") + toml, encoding="utf-8")
        return load(path).image


def load_image_with_strip(strip):
    """The ``ImageConfig`` of ``IMAGE_TOML`` with ``source.strip_components`` set to ``strip``."""
    return load_image(IMAGE_TOML.replace("[image.source]\n", f"[image.source]\nstrip_components = {strip}\n"))


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


class BasisDigestTests(unittest.TestCase):
    FILES = {"example.lock": b"locked\n", "docs/example.cfg": b"cfg\n"}
    COMMANDS = ("bash scripts/example-warm.sh", "bash scripts/example-extra.sh --flag")
    GATE = "bash scripts/example-gate.sh"
    STRIP = 1
    SCRIPT = b"#!/bin/bash\necho warm\n"

    #: Derived by hand from the specification's definition, not by this module.
    VECTOR = "9c8b5c0f9c7cf8e71815dd79c633a3d108d9b52aa364e617e9b9fddb16086f2d"

    def digest(self, files=None, commands=None, gate=None, script=None, strip=None):
        return image.basis_digest(
            self.FILES if files is None else files,
            self.COMMANDS if commands is None else commands,
            self.GATE if gate is None else gate,
            self.STRIP if strip is None else strip,
            self.SCRIPT if script is None else script,
        )

    def test_a_fixed_vector(self):
        self.assertEqual(self.digest(), self.VECTOR)

    def test_the_vector_follows_the_documented_definition(self):
        # The same recipe written out again from its parts: sorted path/hash
        # lines, the hash of the rendered commands, gate and strip value, the
        # hash of the script. Zero is a value too: it is hashed as the text "0".
        lines = b"".join(
            path.encode() + b"\0" + sha256_hex(self.FILES[path]).encode() + b"\n"
            for path in sorted(self.FILES, key=lambda path: path.encode())
        )
        for strip in (0, 1):
            with self.subTest(strip=strip):
                rendered = ("\n".join(self.COMMANDS) + "\0" + self.GATE + "\0" + str(strip)).encode()
                expected = sha256_hex(
                    lines + sha256_hex(rendered).encode() + b"\n" + sha256_hex(self.SCRIPT).encode() + b"\n"
                )
                self.assertEqual(self.digest(strip=strip), expected)

    def test_changing_a_basis_file_changes_the_digest(self):
        changed = dict(self.FILES, **{"example.lock": b"locked again\n"})
        self.assertNotEqual(self.digest(files=changed), self.digest())

    def test_renaming_adding_or_dropping_a_basis_file_changes_the_digest(self):
        renamed = {"renamed.lock": self.FILES["example.lock"], "docs/example.cfg": self.FILES["docs/example.cfg"]}
        added = dict(self.FILES, **{"extra.lock": b"x"})
        dropped = {"example.lock": self.FILES["example.lock"]}
        digests = {self.digest(), *(self.digest(files=files) for files in (renamed, added, dropped))}
        self.assertEqual(len(digests), 4)

    def test_changing_a_warm_command_changes_the_digest(self):
        variants = (
            ("bash scripts/example-warm.sh", "bash scripts/example-extra.sh --other"),
            ("bash scripts/example-warm.sh",),
            self.COMMANDS + ("bash scripts/example-third.sh",),
            tuple(reversed(self.COMMANDS)),
        )
        digests = {self.digest()} | {self.digest(commands=commands) for commands in variants}
        self.assertEqual(len(digests), 1 + len(variants))

    def test_changing_or_dropping_the_gate_command_changes_the_digest(self):
        digests = {self.digest(), self.digest(gate="bash scripts/example-other-gate.sh"), self.digest(gate="")}
        self.assertEqual(len(digests), 3)

    def test_changing_the_strip_components_changes_the_digest(self):
        # Only the archive layout differs between the two inputs. The builder receives the value as
        # WARM_SRC_STRIP_COMPONENTS, so the image differs and a build must be due.
        self.assertEqual(self.digest(strip=0), self.digest(strip=0))
        self.assertNotEqual(self.digest(strip=0), self.digest(strip=1))

    def test_changing_the_script_by_one_byte_changes_the_digest(self):
        self.assertNotEqual(self.digest(script=self.SCRIPT + b"#"), self.digest())

    def test_path_order_does_not_change_the_digest(self):
        reversed_files = dict(reversed(list(self.FILES.items())))
        self.assertEqual(list(reversed_files), list(reversed(list(self.FILES))))
        self.assertEqual(self.digest(files=reversed_files), self.digest())

    def test_paths_sort_by_their_utf8_bytes(self):
        # A path that is the start of another sorts first; "." (0x2e) sorts before "/" (0x2f);
        # a non-ASCII path sorts by its UTF-8 bytes, after every ASCII letter.
        files = {"\u00e9": b"4", "a/b": b"1", "z": b"5", "a": b"2", "a.b": b"3"}
        order = ["a", "a.b", "a/b", "z", "\u00e9"]
        by_bytes = b"".join(
            path.encode() + b"\0" + sha256_hex(files[path]).encode() + b"\n" for path in order
        )
        rendered = ("\n".join(self.COMMANDS) + "\0" + self.GATE + "\0" + str(self.STRIP)).encode()
        expected = sha256_hex(
            by_bytes + sha256_hex(rendered).encode() + b"\n" + sha256_hex(self.SCRIPT).encode() + b"\n"
        )
        self.assertEqual(self.digest(files=files), expected)

    def test_the_commands_and_the_gate_cannot_trade_text(self):
        self.assertNotEqual(
            self.digest(commands=("ab",), gate=""),
            self.digest(commands=("a",), gate="b"),
        )
        self.assertNotEqual(
            self.digest(commands=("a b",), gate=""),
            self.digest(commands=("a", "b"), gate=""),
        )

    def test_the_digest_takes_no_archive_hash(self):
        parameters = list(inspect.signature(image.basis_digest).parameters)
        self.assertEqual(parameters, ["files", "warm_commands", "gate_command", "strip_components", "script_bytes"])

    def test_the_digest_hashes_the_values_the_build_exports(self):
        # One encoding serves both: the digest hashes what warm_exports puts in the user data,
        # the strip value as exported included.
        files = {"example.lock": b"locked\n"}
        lines = b"example.lock\0" + sha256_hex(files["example.lock"]).encode() + b"\n"
        for strip in (0, 1):
            with self.subTest(strip=strip):
                config = load_image_with_strip(strip)
                built = image.warm_exports(config, SENTINEL_URL, "ab" * 32)
                rendered = "\0".join(
                    (built["WARM_COMMANDS"], built["WARM_GATE_COMMAND"], built["WARM_SRC_STRIP_COMPONENTS"])
                ).encode()
                expected = sha256_hex(
                    lines + sha256_hex(rendered).encode() + b"\n" + sha256_hex(self.SCRIPT).encode() + b"\n"
                )
                digest = image.basis_digest(
                    files, config.warm.commands, config.warm.gate_command, config.source.strip_components, self.SCRIPT
                )
                self.assertEqual(digest, expected)

    def test_the_digest_from_the_file_hashes_is_the_digest_of_the_files(self):
        hashes = image.file_hashes(self.FILES)
        self.assertEqual(hashes, {path: sha256_hex(content) for path, content in self.FILES.items()})
        digest = image.basis_digest_from_hashes(hashes, self.COMMANDS, self.GATE, self.STRIP, self.SCRIPT)
        self.assertEqual(digest, self.VECTOR)
        self.assertEqual(digest, self.digest())
        # The order of the hashes does not matter: the lines are sorted.
        reversed_hashes = dict(reversed(list(hashes.items())))
        self.assertEqual(
            image.basis_digest_from_hashes(reversed_hashes, self.COMMANDS, self.GATE, self.STRIP, self.SCRIPT), digest
        )

    def test_the_file_hashes_refuse_a_path_with_a_control_character(self):
        for path in ("a\nb", "a\0b"):
            with self.subTest(path=path), self.assertRaises(ConfigRefusal):
                image.file_hashes({path: b"x"})
            with self.subTest(path=path, source="hashes"), self.assertRaises(ConfigRefusal):
                image.basis_digest_from_hashes({path: "e" * 64}, self.COMMANDS, self.GATE, self.STRIP, self.SCRIPT)

    def test_a_path_or_command_with_a_control_character_is_refused(self):
        for label, arguments in (
            ("a path with a line feed", {"files": {"a\nb": b"x"}}),
            ("a path with a NUL", {"files": {"a\0b": b"x"}}),
            ("a command with a line feed", {"commands": ("a\nb",)}),
            ("a command with an escape", {"commands": ("a\x1bb",)}),
            ("a gate with a carriage return", {"gate": "a\rb"}),
        ):
            with self.subTest(label):
                with self.assertRaises(ConfigRefusal) as raised:
                    self.digest(**arguments)
                self.assertEqual(raised.exception.reason, "CONFIG_INVALID")

    def test_commands_must_be_a_non_empty_list_of_strings(self):
        for commands in ("one command as text", (), [], (1,), (None,)):
            with self.subTest(commands=commands):
                with self.assertRaises(ConfigRefusal):
                    image.render_commands(commands, "")

    def test_the_script_hash_is_the_plain_sha256(self):
        self.assertEqual(image.script_sha256(self.SCRIPT), sha256_hex(self.SCRIPT))
        self.assertEqual(image.script_sha256(b""), sha256_hex(b""))


def control(**overrides):
    values = {"consecutive_failures": 0, "cooldown_until": None, "blocked": False, "last_check_at": None}
    values.update(overrides)
    return Control(**values)


START = {"start": True, "decision": "start", "reason": None, "clear_control": False}
UNCHANGED = {"start": False, "decision": "digest_unchanged", "reason": None, "clear_control": False}
CLEARING = {**START, "clear_control": True}


def refused(reason, clear=False):
    return {"start": False, "decision": "refused", "reason": reason, "clear_control": clear}


def named(result):
    return result["reason"] or result["decision"]


class BuildDecisionTests(unittest.TestCase):
    """The trigger and rate-limit rules, with the spec's defaults: one build a day, two a day at most."""

    @classmethod
    def setUpClass(cls):
        cls.config = load_image()
        assert cls.config.min_build_interval_seconds == DAY and cls.config.max_builds_per_day == 2

    def decide(self, **overrides):
        facts = {
            "digest": "digest-new",
            "pin_digest": "digest-pinned",
            "now": NOW,
            "last_build_started_at": None,
            "builds_last_day": 0,
            "control": control(),
            "active": False,
            "config": self.config,
        }
        facts.update(overrides)
        return image.build_decision(**facts)

    def test_a_due_build_with_nothing_in_the_way_starts(self):
        self.assertEqual(self.decide(), START)

    def test_a_pin_image_without_a_digest_tag_is_due(self):
        self.assertEqual(self.decide(pin_digest=None), START)

    def test_each_condition_refuses_alone_with_its_reason(self):
        cases = {
            "IMAGE_BUILD_ALREADY_ACTIVE": {"active": True},
            "IMAGE_DAILY_CAP": {"builds_last_day": 2},
            "IMAGE_BLOCKED": {"control": control(blocked=True, consecutive_failures=3)},
            "IMAGE_FAILURE_COOLDOWN": {"control": control(consecutive_failures=1, cooldown_until=NOW + 1)},
            "IMAGE_MIN_INTERVAL": {"last_build_started_at": NOW - DAY + 1},
        }
        for reason, facts in cases.items():
            with self.subTest(reason):
                self.assertEqual(self.decide(**facts), refused(reason))

    def test_an_unchanged_digest_is_an_answer_and_not_a_refusal(self):
        self.assertEqual(self.decide(digest="digest-pinned"), UNCHANGED)

    def test_a_changed_strip_components_is_not_an_unchanged_digest(self):
        # The pin's image was built under strip_components 0 and the configuration now says 1.
        # Nothing else differs, so the digest alone must say that a build is due.
        files = {"example.lock": b"locked\n"}
        script = b"#!/bin/bash\necho warm\n"

        def digest_under(config):
            return image.basis_digest(
                files, config.warm.commands, config.warm.gate_command, config.source.strip_components, script
            )

        pinned, current = load_image_with_strip(0), load_image_with_strip(1)
        pin_digest = digest_under(pinned)
        # The control: the same strip value on both sides is an unchanged digest.
        self.assertEqual(self.decide(config=pinned, digest=digest_under(pinned), pin_digest=pin_digest), UNCHANGED)
        self.assertEqual(self.decide(config=current, digest=digest_under(current), pin_digest=pin_digest), START)

    def test_the_limits_are_exact(self):
        self.assertEqual(self.decide(builds_last_day=1), START)
        self.assertEqual(self.decide(builds_last_day=2), refused("IMAGE_DAILY_CAP"))
        self.assertEqual(self.decide(builds_last_day=3), refused("IMAGE_DAILY_CAP"))
        self.assertEqual(self.decide(control=control(cooldown_until=NOW)), START, "a cooldown that ends now is over")
        self.assertEqual(self.decide(last_build_started_at=NOW - DAY), START)
        self.assertEqual(self.decide(last_build_started_at=NOW - DAY + 1), refused("IMAGE_MIN_INTERVAL"))

    def test_the_limits_come_from_the_configuration(self):
        roomy = dataclasses.replace(self.config, max_builds_per_day=5, min_build_interval_seconds=600)
        self.assertEqual(self.decide(config=roomy, builds_last_day=4, last_build_started_at=NOW - 600), START)
        self.assertEqual(self.decide(config=roomy, builds_last_day=5), refused("IMAGE_DAILY_CAP"))
        self.assertEqual(self.decide(config=roomy, last_build_started_at=NOW - 599), refused("IMAGE_MIN_INTERVAL"))

    def test_the_first_failing_condition_names_the_refusal(self):
        facts = {
            "active": True,
            "builds_last_day": 2,
            "control": control(blocked=True, consecutive_failures=3, cooldown_until=NOW + 60),
            "digest": "digest-pinned",
            "last_build_started_at": NOW - 60,
        }
        # Remove the named condition each time: the next one in line is then named.
        # The order is the spec's: digest, minimum interval, daily cap, cooldown, blocked, builder.
        steps = (
            ("digest_unchanged", {"digest": "digest-new"}),
            ("IMAGE_MIN_INTERVAL", {"last_build_started_at": None}),
            ("IMAGE_DAILY_CAP", {"builds_last_day": 0}),
            ("IMAGE_FAILURE_COOLDOWN", {"control": control(blocked=True, consecutive_failures=3)}),
            ("IMAGE_BLOCKED", {"control": control()}),
            ("IMAGE_BUILD_ALREADY_ACTIVE", {"active": False}),
            ("start", {}),
        )
        for expected, fix in steps:
            with self.subTest(expected):
                self.assertEqual(named(self.decide(**facts)), expected)
                facts.update(fix)

    def test_force_skips_the_digest_comparison_and_the_minimum_interval(self):
        self.assertEqual(self.decide(force=True, digest="digest-pinned"), START)
        self.assertEqual(self.decide(force=True, last_build_started_at=NOW - 60), START)
        self.assertEqual(self.decide(force=True, digest="digest-pinned", last_build_started_at=NOW - 60), START)

    def test_force_never_skips_an_active_build_or_the_daily_cap(self):
        # The spec scenario: two builds in the last day and --force.
        self.assertEqual(self.decide(force=True, builds_last_day=2), refused("IMAGE_DAILY_CAP"))
        self.assertEqual(self.decide(force=True, active=True), refused("IMAGE_BUILD_ALREADY_ACTIVE"))
        self.assertEqual(self.decide(force=True, active=True, builds_last_day=2), refused("IMAGE_DAILY_CAP"))

    def test_force_clears_blocked_and_the_cooldown(self):
        rows = {
            "blocked": control(blocked=True, consecutive_failures=3),
            "cooling down": control(consecutive_failures=1, cooldown_until=NOW + 600),
            "both": control(blocked=True, consecutive_failures=3, cooldown_until=NOW + 600),
        }
        for label, row in rows.items():
            with self.subTest(label):
                self.assertEqual(self.decide(force=True, control=row), CLEARING)

    def test_force_has_nothing_to_clear_on_a_row_that_is_already_clear(self):
        rows = (control(), control(consecutive_failures=1))
        for row in rows:
            with self.subTest(row=row):
                self.assertEqual(self.decide(force=True, control=row), START)

    def test_force_clears_an_expired_cooldown_timestamp_even_when_refused(self):
        row = control(consecutive_failures=1, cooldown_until=NOW - 1)
        self.assertEqual(self.decide(force=True, control=row), CLEARING)
        self.assertEqual(self.decide(force=True, control=row, builds_last_day=2), refused("IMAGE_DAILY_CAP", clear=True))
        self.assertEqual(self.decide(control=row), START, "without --force an expired cooldown is only over")

    def test_a_clearing_applies_even_when_the_start_is_refused(self):
        # The spec: --force and a digest change clear blocked and the cooldown, whatever else refuses the start.
        row = control(blocked=True, consecutive_failures=3, cooldown_until=NOW + 600)
        self.assertEqual(
            self.decide(force=True, builds_last_day=2, control=row), refused("IMAGE_DAILY_CAP", clear=True)
        )
        self.assertEqual(
            self.decide(force=True, active=True, control=row), refused("IMAGE_BUILD_ALREADY_ACTIVE", clear=True)
        )
        self.assertEqual(
            self.decide(control=row, last_failed_digest="digest-failed", active=True),
            refused("IMAGE_BUILD_ALREADY_ACTIVE", clear=True),
        )

    def test_a_refusal_without_a_clearing_asks_for_none(self):
        row = control(blocked=True, consecutive_failures=3, cooldown_until=NOW + 600)
        self.assertEqual(self.decide(builds_last_day=2, control=row), refused("IMAGE_DAILY_CAP"))
        self.assertEqual(self.decide(control=row, last_failed_digest="digest-new"), refused("IMAGE_FAILURE_COOLDOWN"))

    def test_blocked_clears_when_the_digest_differs_from_the_last_failed_build(self):
        row = control(blocked=True, consecutive_failures=3)
        self.assertEqual(self.decide(control=row, last_failed_digest="digest-failed"), CLEARING)

    def test_blocked_stays_when_the_digest_is_the_one_that_failed(self):
        row = control(blocked=True, consecutive_failures=3)
        self.assertEqual(self.decide(control=row, last_failed_digest="digest-new"), refused("IMAGE_BLOCKED"))

    def test_blocked_stays_when_there_is_no_failed_digest_to_compare(self):
        row = control(blocked=True, consecutive_failures=3)
        self.assertEqual(self.decide(control=row, last_failed_digest=None), refused("IMAGE_BLOCKED"))

    def test_a_recovery_resets_the_cooldown_with_blocked(self):
        row = control(blocked=True, consecutive_failures=3, cooldown_until=NOW + 600)
        self.assertEqual(self.decide(control=row, last_failed_digest="digest-failed"), CLEARING)

    def test_a_digest_change_alone_does_not_end_a_cooldown_that_is_not_blocked(self):
        row = control(consecutive_failures=1, cooldown_until=NOW + 600)
        self.assertEqual(
            self.decide(control=row, last_failed_digest="digest-failed"), refused("IMAGE_FAILURE_COOLDOWN")
        )

    def test_a_recovered_build_still_waits_for_the_minimum_interval(self):
        row = control(blocked=True, consecutive_failures=3)
        result = self.decide(control=row, last_failed_digest="digest-failed", last_build_started_at=NOW - 60)
        self.assertEqual(result, refused("IMAGE_MIN_INTERVAL", clear=True))

    def test_a_recovery_is_asked_for_even_when_the_digest_is_unchanged(self):
        # The digest differs from the failed build's but equals the pin's tag: nothing is built, blocked still clears.
        row = control(blocked=True, consecutive_failures=3)
        result = self.decide(control=row, last_failed_digest="digest-failed", digest="digest-pinned")
        self.assertEqual(result, {**UNCHANGED, "clear_control": True})

    def test_the_result_has_exactly_the_documented_fields(self):
        for result in (self.decide(), self.decide(active=True), self.decide(digest="digest-pinned")):
            self.assertEqual(set(result), {"start", "decision", "reason", "clear_control"})
            self.assertIsInstance(result["start"], bool)
            self.assertIsInstance(result["clear_control"], bool)


class ImageRefusalTests(unittest.TestCase):
    def test_the_refusal_class_carries_its_reason_and_detail(self):
        refusal = ImageRefusal("IMAGE_DAILY_CAP", "two builds in the last day")
        self.assertIsInstance(refusal, Refusal)
        self.assertEqual(refusal.reason, "IMAGE_DAILY_CAP")
        self.assertEqual(refusal.as_dict(), {"reason": "IMAGE_DAILY_CAP", "detail": "two builds in the last day"})


def real_script():
    return SCRIPT.read_bytes()


def exports(**overrides):
    values = {
        "WARM_SRC_URL": SENTINEL_URL,
        "WARM_SRC_SHA256": "ab" * 32,
        "WARM_COMMANDS": "bash scripts/example-warm.sh\nbash scripts/example-extra.sh --flag",
        "WARM_GATE_COMMAND": "bash scripts/example-gate.sh",
        "WARM_LIFETIME_SECONDS": "3000",
        "WARM_SNAPSHOT_SECONDS": "900",
        "WARM_SRC_STRIP_COMPONENTS": "1",
    }
    values.update(overrides)
    return values


PAYLOAD = re.compile(r"<<'WARM_PAYLOAD_END'[^\n]*\n(?P<payload>[^\n]*)\nWARM_PAYLOAD_END\n")


def payload_of(text):
    """The base64 line of the here-document, exactly one."""
    matches = PAYLOAD.findall(text)
    assert len(matches) == 1, matches
    return matches[0]


class WarmExportsTests(unittest.TestCase):
    def test_the_names_are_the_inputs_the_script_reads(self):
        read = set(re.findall(r"(?m)^(WARM_[A-Z0-9_]+)=\$\{\1:?-", SCRIPT.read_text(encoding="utf-8")))
        self.assertEqual(len(read), 7)
        self.assertEqual(set(image.warm_exports(load_image(), SENTINEL_URL, "ab" * 32)), read)

    def test_the_values_come_from_the_configuration_and_the_two_build_facts(self):
        values = image.warm_exports(load_image(), SENTINEL_URL, "ab" * 32)
        self.assertEqual(
            values,
            {
                "WARM_SRC_URL": SENTINEL_URL,
                "WARM_SRC_SHA256": "ab" * 32,
                "WARM_COMMANDS": "bash scripts/example-warm.sh\nbash scripts/example-extra.sh --flag",
                "WARM_GATE_COMMAND": "bash scripts/example-gate.sh",
                "WARM_LIFETIME_SECONDS": "3000",
                "WARM_SNAPSHOT_SECONDS": "900",
                "WARM_SRC_STRIP_COMPONENTS": "1",
            },
        )
        self.assertTrue(all(isinstance(value, str) for value in values.values()))

    def test_no_gate_exports_an_empty_gate(self):
        config = load_image(IMAGE_TOML.replace('gate_command = "bash scripts/example-gate.sh"\n', ""))
        self.assertEqual(image.warm_exports(config, SENTINEL_URL, "ab" * 32)["WARM_GATE_COMMAND"], "")

    def test_the_strip_components_setting_reaches_the_export(self):
        config = load_image(IMAGE_TOML.replace("[image.source]\n", "[image.source]\nstrip_components = 0\n"))
        self.assertEqual(image.warm_exports(config, SENTINEL_URL, "ab" * 32)["WARM_SRC_STRIP_COMPONENTS"], "0")


class RenderUserDataTests(unittest.TestCase):
    def test_the_stub_exports_every_value_quoted_in_name_order_before_the_payload(self):
        text = image.render_user_data(real_script(), exports())
        self.assertTrue(text.startswith("#!/bin/bash\n"))
        names = re.findall(r"(?m)^export (WARM_[A-Z0-9_]+)=", text)
        self.assertEqual(names, sorted(exports()))
        self.assertIn("export WARM_GATE_COMMAND='bash scripts/example-gate.sh'\n", text)
        self.assertIn("export WARM_LIFETIME_SECONDS=3000\n", text)
        self.assertLess(text.index("export WARM_SRC_URL="), text.index("WARM_PAYLOAD_END"))
        self.assertTrue(text.endswith('bash "$d/warm-cache.sh"\nexit $?\n'))

    def test_the_payload_decodes_to_the_script_byte_for_byte(self):
        script = real_script()
        payload = payload_of(image.render_user_data(script, exports()))
        self.assertEqual(gzip.decompress(base64.b64decode(payload, validate=True)), script)

    def test_the_stub_decodes_the_payload_with_the_shell_tools_it_runs(self):
        script = real_script()
        payload = payload_of(image.render_user_data(script, exports()))
        decoded = subprocess.run(
            ["bash", "-c", "base64 -d | gunzip"], input=payload.encode(), stdout=subprocess.PIPE, check=True, timeout=60
        )
        self.assertEqual(decoded.stdout, script)

    def test_the_stub_is_valid_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / "stub.sh"
            stub.write_text(image.render_user_data(real_script(), exports()), encoding="utf-8")
            checked = subprocess.run(["bash", "-n", str(stub)], capture_output=True, text=True, timeout=60)
        self.assertEqual((checked.returncode, checked.stderr), (0, ""))

    def test_the_output_depends_on_the_arguments_alone(self):
        script = real_script()
        first = image.render_user_data(script, exports())
        reordered = dict(reversed(list(exports().items())))
        self.assertEqual(image.render_user_data(script, reordered), first)
        self.assertEqual(image.render_user_data(script, exports()), first)
        compressed = base64.b64decode(payload_of(first))
        self.assertEqual(compressed[4:8], b"\0\0\0\0", "the gzip header holds no timestamp")

    def test_the_user_data_is_text(self):
        self.assertIsInstance(image.render_user_data(real_script(), exports()), str)

    # -- the size limit ---------------------------------------------------
    def render_commands_of_size(self, total):
        """User data whose WARM_COMMANDS is the one command ``x...x`` that makes it ``total`` bytes."""
        def render(commands):
            return image.render_user_data(real_script(), exports(WARM_COMMANDS=commands))

        one = len(render("x").encode("utf-8"))
        return render("x" * (1 + total - one))

    def test_the_largest_allowed_commands_render_just_below_the_limit(self):
        text = self.render_commands_of_size(16_383)
        self.assertEqual(len(text.encode("utf-8")), 16_383)

    def test_one_byte_more_is_refused(self):
        with self.assertRaises(ImageRefusal) as raised:
            self.render_commands_of_size(16_384)
        self.assertEqual(image.USER_DATA_LIMIT, 16_384)
        self.assertEqual(raised.exception.reason, "IMAGE_USERDATA_TOO_LARGE")
        self.assertIn("16384", raised.exception.detail)

    def test_the_size_is_counted_in_utf8_bytes_not_characters(self):
        def render(commands):
            return image.render_user_data(real_script(), exports(WARM_COMMANDS=commands))

        # A leading space keeps the value quoted whatever follows, so each é adds two bytes and one character.
        one = render(" é")
        characters, size = len(one), len(one.encode("utf-8"))
        self.assertEqual(size, characters + 1)
        count = 1 + (image.USER_DATA_LIMIT - 1) - characters
        with self.assertRaises(ImageRefusal) as raised:
            render(" " + "é" * count)
        self.assertEqual(raised.exception.reason, "IMAGE_USERDATA_TOO_LARGE")
        self.assertLess(characters + count - 1, image.USER_DATA_LIMIT, "short enough as characters")

    def test_a_realistic_configuration_leaves_room_to_spare(self):
        # A presigned URL carrying a session token is about 2 KB; take 2.4 KB. Eight 100-byte
        # warm commands and a gate are more than the consumer this was designed for needs.
        config = load_image()
        commands = tuple(
            f"bash scripts/example-warm-step-{n}.sh --frozen --quiet --jobs 4 --cache /example/cache"
            for n in range(8)
        )
        warm = dataclasses.replace(
            config.warm, commands=commands, gate_command="bash scripts/example-gate.sh --offline"
        )
        config = dataclasses.replace(config, warm=warm)
        url = "https://example-bucket.invalid/example-prefix/archive.tar.gz?" + "a" * 2400
        text = image.render_user_data(real_script(), image.warm_exports(config, url, "ab" * 32))
        size = len(text.encode("utf-8"))
        self.assertLessEqual(size, image.USER_DATA_LIMIT - 4096, f"{size} bytes leaves too little room")

    # -- quoting and control characters -----------------------------------
    HOSTILE = (
        "plain",
        "it's",
        "'; touch {canary}1; '",
        "$(touch {canary}2)",
        "`touch {canary}3`",
        "a;b && c || d | e > f < g &",
        '"double" \\ back\\slash $HOME ${HOME} $((1+1)) !history *glob? [x] ~ {a,b} #hash',
        "--leading-dash and  two  spaces",
        "  leading and trailing spaces  ",
        "unicode: é ü 日本語",
        "ends with a backslash \\",
        "'''",
        "",
    )

    def stand_in(self, count, out):
        """A script that writes each WARM_V<n> value, NUL-terminated, to ``out``."""
        names = " ".join(str(n) for n in range(count))
        return (
            f": > {out}\nfor n in {names}; do v=WARM_V$n; printf '%s\\0' \"${{!v}}\" >> {out}; done\n"
        ).encode()

    def test_hostile_values_reach_the_script_unchanged_and_run_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            canary = root / "canary"
            values = [value.replace("{canary}", str(canary)) for value in self.HOSTILE]
            out = root / "values"
            text = image.render_user_data(
                self.stand_in(len(values), out), {f"WARM_V{n}": value for n, value in enumerate(values)}
            )
            result = StubHarness.run(self, root, text)
            self.assertEqual(result.returncode, 0, result.stderr)
            seen = out.read_bytes().split(b"\0")[:-1]
            self.assertEqual([value.decode("utf-8") for value in seen], values)
            self.assertEqual(sorted(path.name for path in root.glob("canary*")), [], "no value was run as a command")
            # A redirect inside a value (`> f`) would create a file in the working directory.
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["bin", "stub.sh", "values"])

    def test_a_line_feed_separates_warm_commands_and_reaches_the_script_as_one_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            out = root / "commands"
            script = f"printf '%s' \"$WARM_COMMANDS\" > {out}\n".encode()
            commands = "echo 'one'\n  echo \"two\" # three\n$(not run)"
            result = StubHarness.run(self, root, image.render_user_data(script, {"WARM_COMMANDS": commands}))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(out.read_text(encoding="utf-8"), commands)

    def test_control_characters_are_refused_in_every_value(self):
        # Category Cc: C0 controls, DEL and the C1 controls.
        for character in ("\n", "\r", "\t", "\0", "\x1b", "\x7f", "\x85", "\x9b"):
            for name in ("WARM_SRC_URL", "WARM_GATE_COMMAND", "WARM_V1", "WARM_COMMANDS"):
                if name == "WARM_COMMANDS" and character == "\n":
                    continue
                with self.subTest(character=character, name=name):
                    with self.assertRaises(ConfigRefusal) as raised:
                        image.render_user_data(b"x", {name: f"a{character}b"})
                    self.assertEqual(raised.exception.reason, "CONFIG_INVALID")
                    self.assertIn(name, raised.exception.detail)

    def test_only_warm_commands_may_hold_a_line_feed(self):
        image.render_user_data(b"x", {"WARM_COMMANDS": "a\nb"})
        for name in ("WARM_SRC_URL", "WARM_GATE_COMMAND", "WARM_SRC_SHA256", "WARM_V1"):
            with self.subTest(name):
                with self.assertRaises(ConfigRefusal):
                    image.render_user_data(b"x", {name: "a\nb"})

    def test_each_line_of_warm_commands_is_checked_on_its_own(self):
        for commands in ("good\nbad\x1bline\nalso good", "good\r\nwindows", "good\n\x00"):
            with self.subTest(commands=commands):
                with self.assertRaises(ConfigRefusal):
                    image.render_user_data(b"x", {"WARM_COMMANDS": commands})

    def test_a_name_outside_warm_upper_case_is_refused(self):
        bad = ("PATH", "LD_PRELOAD", "warm_x", "WARM_", "WARM", "WARM_x", "WARM_X Y", "WARM_X; id", "WARM_X=1",
               "WARM_X\n", "WARM_X$(id)", "WARM_É", "", " WARM_X")
        for name in bad:
            with self.subTest(name=name):
                with self.assertRaises(ConfigRefusal) as raised:
                    image.render_user_data(b"x", {name: "value"})
                self.assertEqual(raised.exception.reason, "CONFIG_INVALID")
        for name in ("WARM_X", "WARM_SRC_SHA256", "WARM_A_1", "WARM_0"):
            with self.subTest(name=name):
                image.render_user_data(b"x", {name: "value"})

    def test_a_name_that_is_not_a_string_is_refused_beside_a_valid_one(self):
        for name in (1, None, b"WARM_X"):
            with self.subTest(name=name):
                with self.assertRaises(ConfigRefusal) as raised:
                    image.render_user_data(b"x", {"WARM_X": "value", name: "value"})
                self.assertEqual(raised.exception.reason, "CONFIG_INVALID")

    def test_a_value_that_is_not_text_is_refused(self):
        for value in (3000, None, b"bytes", ["a"]):
            with self.subTest(value=value):
                with self.assertRaises(ConfigRefusal):
                    image.render_user_data(b"x", {"WARM_X": value})


class RefusalSecrecyTests(unittest.TestCase):
    """No refusal holds the presigned URL or any export value."""

    def assertNoSentinel(self, error):
        pieces = [str(error), repr(error), error.detail, str(error.as_dict()), "".join(traceback.format_exception(error))]
        for piece in pieces:
            self.assertNotIn(SENTINEL, piece)
            self.assertNotIn("example-bucket", piece)

    def test_a_rejected_export_never_names_the_url(self):
        bad_exports = (
            exports(WARM_GATE_COMMAND="a\x1bb"),
            exports(WARM_COMMANDS="a\nb\rc"),
            exports(**{"WARM_X\n": "v"}),
            exports(WARM_LIFETIME_SECONDS=3000),
        )
        for values in bad_exports:
            with self.subTest(values=sorted(values)):
                with self.assertRaises(ConfigRefusal) as raised:
                    image.render_user_data(b"x", values)
                self.assertNoSentinel(raised.exception)

    def test_a_control_character_in_the_url_itself_does_not_echo_it(self):
        with self.assertRaises(ConfigRefusal) as raised:
            image.render_user_data(b"x", exports(WARM_SRC_URL=SENTINEL_URL + "\n"))
        self.assertNoSentinel(raised.exception)
        self.assertIn("WARM_SRC_URL", raised.exception.detail)

    def test_the_size_refusal_never_names_the_url_or_a_command(self):
        values = exports(WARM_COMMANDS="y" * image.USER_DATA_LIMIT)
        with self.assertRaises(ImageRefusal) as raised:
            image.render_user_data(real_script(), values)
        self.assertEqual(raised.exception.reason, "IMAGE_USERDATA_TOO_LARGE")
        self.assertNoSentinel(raised.exception)
        self.assertNotIn("yyyy", str(raised.exception.as_dict()))

    def test_a_missing_script_names_only_its_path(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.sh"
            with self.assertRaises(ImageRefusal) as raised:
                image.shipped_script(missing)
        self.assertEqual(raised.exception.reason, "IMAGE_SCRIPT_MISSING")
        self.assertIn(str(missing), raised.exception.detail)

    def test_a_malformed_command_list_does_not_echo_a_command(self):
        with self.assertRaises(ConfigRefusal) as raised:
            image.render_commands((SENTINEL, f"a\x1b{SENTINEL}"), "")
        self.assertNoSentinel(raised.exception)

    def test_a_bad_export_value_type_does_not_echo_it(self):
        with self.assertRaises(ConfigRefusal) as raised:
            image.render_user_data(b"x", {"WARM_SRC_URL": SENTINEL.encode()})
        self.assertNoSentinel(raised.exception)


class StubHarness:
    """Run a rendered stub in a temporary tree, with a stub ``shutdown`` and a stand-in script."""

    @staticmethod
    def run(test, root, text, *, directory="warm-stub."):
        """Run ``text`` with its private directory rewritten under ``root``; return the process."""
        bin_dir = root / "bin"
        bin_dir.mkdir(exist_ok=True)
        shutdown = bin_dir / "shutdown"
        shutdown.write_text(f'#!/bin/bash\necho "shutdown $*" >> "{root}/calls"\n', encoding="utf-8")
        shutdown.chmod(0o755)
        test.assertEqual(text.count("/run/warm-stub."), 1)
        rewritten = text.replace("/run/warm-stub.", f"{root}/{directory}")
        # Fail closed: no real host path may survive the rewrite.
        test.assertNotIn("/run/", rewritten)
        path = root / "stub.sh"
        path.write_text(rewritten, encoding="utf-8")
        env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "LANG": "C.UTF-8"}
        test.assertEqual(shutil.which("shutdown", path=env["PATH"]), str(shutdown), "shutdown must be a stub")
        # The working directory is the temporary tree, so a redirect that escapes quoting lands there
        # and a test can see it, instead of landing in the repository checkout.
        return subprocess.run(
            ["bash", str(path)],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    @staticmethod
    def calls(root):
        path = root / "calls"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


class StubBehaviourTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_the_exit_status_of_the_script_is_the_exit_status_of_the_stub(self):
        for code in (0, 2, 77):
            with self.subTest(code=code):
                text = image.render_user_data(f"exit {code}\n".encode(), {"WARM_X": "1"})
                self.assertEqual(StubHarness.run(self, self.root, text).returncode, code)

    def test_the_script_sees_the_exports_and_the_private_directory_is_removed_afterwards(self):
        out = self.root / "seen"
        script = f"printf '%s|%s' \"$WARM_A\" \"$0\" > {out}\n".encode()
        text = image.render_user_data(script, {"WARM_A": "value one"})
        result = StubHarness.run(self, self.root, text)
        self.assertEqual(result.returncode, 0, result.stderr)
        value, _, path = out.read_text(encoding="utf-8").partition("|")
        self.assertEqual(value, "value one")
        self.assertTrue(path.startswith(f"{self.root}/warm-stub."), path)
        self.assertTrue(path.endswith("/warm-cache.sh"))
        self.assertEqual(list(self.root.glob("warm-stub.*")), [], "the private directory is removed")
        self.assertEqual(StubHarness.calls(self.root), [], "a script that ran is not powered off by the stub")

    def test_the_private_directory_is_not_readable_by_others(self):
        out = self.root / "mode"
        script = f"stat -c %a \"$(dirname \"$0\")\" > {out}\n".encode()
        StubHarness.run(self, self.root, image.render_user_data(script, {}))
        self.assertEqual(out.read_text(encoding="utf-8").strip(), "700")

    def test_a_payload_that_does_not_decode_powers_off_and_runs_nothing(self):
        out = self.root / "ran"
        text = image.render_user_data(f"echo ran > {out}\n".encode(), {"WARM_X": "1"})
        valid = payload_of(text)
        broken = {
            "not base64": "!!!not-base64!!!",
            "base64 of something that is not gzip": base64.b64encode(b"hello").decode(),
            "a truncated gzip stream": valid[: len(valid) // 2 // 4 * 4],
        }
        for label, payload in broken.items():
            with self.subTest(label):
                (self.root / "calls").unlink(missing_ok=True)
                result = StubHarness.run(self, self.root, text.replace(valid, payload))
                self.assertEqual(result.returncode, 1)
                self.assertIn("warm stub: the payload does not decode", result.stderr)
                self.assertEqual(StubHarness.calls(self.root), ["shutdown -h now"])
                self.assertFalse(out.exists(), "a script that did not decode never runs")
                self.assertEqual(list(self.root.glob("warm-stub.*")), [])

    def test_no_private_directory_powers_off_and_runs_nothing(self):
        out = self.root / "ran"
        text = image.render_user_data(f"echo ran > {out}\n".encode(), {})
        result = StubHarness.run(self, self.root, text, directory="missing/warm-stub.")
        self.assertEqual(result.returncode, 1)
        self.assertIn("warm stub: no private directory", result.stderr)
        self.assertEqual(StubHarness.calls(self.root), ["shutdown -h now"])
        self.assertFalse(out.exists())


class ShippedScriptTests(unittest.TestCase):
    def test_it_returns_the_bytes_of_the_script_it_is_given(self):
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "warm-cache.sh"
            copy.write_bytes(real_script())
            self.assertEqual(image.shipped_script(copy), real_script())
            self.assertEqual(image.shipped_script(str(copy)), real_script())

    def test_the_loader_returns_the_repository_script_byte_for_byte(self):
        self.assertEqual(image.shipped_script(SCRIPT), SCRIPT.read_bytes())

    def test_the_default_is_the_path_the_image_ships(self):
        parameter = inspect.signature(image.shipped_script).parameters["path"]
        self.assertEqual(parameter.default, image.DEFAULT_SCRIPT_PATH)

    def test_an_absent_or_unreadable_script_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            for label, path in (("absent", Path(directory) / "missing.sh"), ("a directory", Path(directory))):
                with self.subTest(label):
                    with self.assertRaises(ImageRefusal) as raised:
                        image.shipped_script(path)
                    self.assertEqual(raised.exception.reason, "IMAGE_SCRIPT_MISSING")


class ContainerCopyTests(unittest.TestCase):
    """The image carries the repository's script at the path the loader reads."""

    SOURCE = "deploy/runner/image/warm-cache.sh"

    def copy_instructions(self):
        found = []
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
            if not line.startswith("COPY "):
                continue
            words = shlex.split(line)
            if any(word.endswith("warm-cache.sh") for word in words):
                found.append(words[1:])
        return found

    def test_the_image_copies_the_repository_script_to_the_loader_default(self):
        found = self.copy_instructions()
        self.assertEqual(len(found), 1, found)
        paths = [word for word in found[0] if not word.startswith("--")]
        self.assertEqual(paths, [self.SOURCE, str(image.DEFAULT_SCRIPT_PATH)])
        self.assertTrue((ROOT / self.SOURCE).is_file())

    def test_the_copy_is_owned_by_root_and_read_only(self):
        flags = sorted(word for word in self.copy_instructions()[0] if word.startswith("--"))
        self.assertEqual(flags, ["--chmod=0644", "--chown=0:0"])

    def test_the_script_enters_the_build_context_and_its_neighbours_do_not(self):
        self.assertTrue(admitted(self.SOURCE))
        for path in (
            "deploy/runner/image/provision.sh",
            "deploy/runner/image/README.md",
            "deploy/runner/bootstrap.sh",
            "deploy/runner/cache-env.sh",
            "deploy/ci-burst-refresh.sh",
            "deploy/iam/policy.json",
            "deploy/router/example.conf",
        ):
            with self.subTest(path=path):
                self.assertFalse(admitted(path), f"{path} must not enter the build context")


if __name__ == "__main__":
    unittest.main()
