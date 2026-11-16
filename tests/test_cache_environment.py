"""The job-account cache environment has one definition.

``deploy/runner/cache-env.sh`` owns the values. Three scripts carry a copy,
because each runs as lone user data or as pinned published bytes and cannot read
a repository file: ``bootstrap.sh`` (its bytes are a pinned launch-template
version), ``provision.sh`` (it embeds the file it installs into the image) and
``warm-cache.sh`` (it runs on a builder whose image may predate the file). A warm
step that uses other paths produces a cache no job reads, and nothing fails at
runtime, so these tests fail instead, naming the variable.

The copies are parsed as text, never run. The checks themselves are proven able
to fail by mutating a copy of the input inside the test. The documented examples
are held to the same directory, because the clean-up of the warm layer keeps
only ``cache``.
"""

import posixpath
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "deploy" / "runner" / "cache-env.sh"
BOOTSTRAP = ROOT / "deploy" / "runner" / "bootstrap.sh"
PROVISION = ROOT / "deploy" / "runner" / "image" / "provision.sh"
WARM_SCRIPT = ROOT / "deploy" / "runner" / "image" / "warm-cache.sh"
WARM_DOC = ROOT / "docs" / "warm-image.md"

CACHE_DIRECTORY = "/var/lib/forgejo-runner/cache"

#: One assignment per line: ``NAME=value``, optionally indented, optionally
#: followed by a line continuation, as the env blocks of the scripts write it.
ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(\S+?)\s*\\?\s*$")


def parse_environment(text):
    """Every uncommented assignment in ``text``: a list of ``(name, value)``."""
    found = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        match = ASSIGNMENT.match(line)
        if match:
            found.append((match.group(1), match.group(2)))
    return found


def definition_from(text):
    """The name-to-value mapping of a shared file; a repeated name is refused."""
    pairs = parse_environment(text)
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names):
        raise AssertionError("the shared file defines a name twice")
    return dict(pairs)


def shared_definition():
    return definition_from(SHARED.read_text(encoding="utf-8"))


def copy_problems(label, text, shared):
    """Each way a copy of the cache environment departs from the shared file.

    A line counts as a cache assignment when its name is one the shared file
    defines or its value lies under the cache directory. A name that the shared
    file lacks but a copy sets under the cache directory is a second definition
    in the making, so it is reported too.
    """
    problems = []
    seen = {}
    for name, value in parse_environment(text):
        if name not in shared and not value.startswith(CACHE_DIRECTORY):
            continue
        if name not in shared:
            problems.append(f"{label}: {name} is set to {value} but the shared file does not define it")
            continue
        seen.setdefault(name, []).append(value)
    for name, expected in shared.items():
        if name not in seen:
            problems.append(f"{label}: {name} is missing; the shared file sets {expected}")
            continue
        for value in seen[name]:
            if value != expected:
                problems.append(f"{label}: {name} is {value}; the shared file sets {expected}")
    return problems


def under_cache(value):
    path = posixpath.normpath(value)
    return path == CACHE_DIRECTORY or path.startswith(CACHE_DIRECTORY + "/")


class SharedDefinitionTests(unittest.TestCase):
    def setUp(self):
        self.shared = shared_definition()

    def test_the_shared_file_defines_the_six_job_variables(self):
        self.assertEqual(
            set(self.shared),
            {
                "XDG_CACHE_HOME",
                "UV_CACHE_DIR",
                "NPM_CONFIG_CACHE",
                "npm_config_cache",
                "PIP_CACHE_DIR",
                "CI_PROFILE_CACHE_ROOT",
            },
        )

    def test_every_shared_value_lies_under_the_cache_directory(self):
        # The warm layer's clean-up keeps only `cache`, so a value outside it
        # would be deleted from the image.
        for name, value in self.shared.items():
            with self.subTest(name=name):
                self.assertTrue(under_cache(value), f"{name}={value} leaves {CACHE_DIRECTORY}")

    def test_the_shared_file_sets_no_value_through_expansion_or_quotes(self):
        for name, value in self.shared.items():
            with self.subTest(name=name):
                self.assertNotRegex(value, r"[$`'\"]")

    def test_bootstrap_carries_exactly_the_shared_assignments(self):
        self.assertEqual(
            copy_problems("bootstrap.sh", BOOTSTRAP.read_text(encoding="utf-8"), self.shared), []
        )

    def test_provision_carries_exactly_the_shared_assignments(self):
        # The probe once lacked npm_config_cache; the installed copy fixes that.
        self.assertEqual(
            copy_problems("provision.sh", PROVISION.read_text(encoding="utf-8"), self.shared), []
        )

    def test_the_warm_script_carries_exactly_the_shared_assignments(self):
        self.assertEqual(
            copy_problems("warm-cache.sh", WARM_SCRIPT.read_text(encoding="utf-8"), self.shared), []
        )

    def test_provision_installs_a_byte_identical_copy_of_the_shared_file(self):
        text = PROVISION.read_text(encoding="utf-8")
        start = text.index("<<'CACHE_ENV'\n") + len("<<'CACHE_ENV'\n")
        end = text.index("\nCACHE_ENV\n", start) + 1
        self.assertEqual(text[start:end], SHARED.read_text(encoding="utf-8"))
        self.assertIn("/etc/ci-burst/cache-env.sh", text)

    def test_provision_probes_with_the_installed_file_and_no_inline_copy(self):
        text = PROVISION.read_text(encoding="utf-8")
        probe = text[text.index("mapfile -t cache_env"): text.index('echo "PROBE end')]
        self.assertIn('"${cache_env[@]}"', probe)
        inline = [name for name, _ in parse_environment(probe.split("runuser", 1)[1])]
        self.assertEqual(inline, ["HOME", "PATH"], "the probe sets no cache variable inline")
        names = [name for name, _ in parse_environment(text) if name in self.shared]
        self.assertEqual(sorted(names), sorted(self.shared), "one copy, the installed file")

    def test_bootstrap_bytes_are_untouched_by_this_definition(self):
        # The published user data cannot read the shared file, so it must not
        # mention it either; a change there is a new launch-template version.
        self.assertNotIn("cache-env", BOOTSTRAP.read_text(encoding="utf-8"))


class DivergenceDetectionTests(unittest.TestCase):
    """The checks above can fail: each mutation below must be reported by name."""

    def setUp(self):
        self.shared = shared_definition()
        self.bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
        self.provision = PROVISION.read_text(encoding="utf-8")

    def test_a_different_value_names_the_variable(self):
        mutated = self.bootstrap.replace(
            "UV_CACHE_DIR=/var/lib/forgejo-runner/cache/uv",
            "UV_CACHE_DIR=/var/lib/forgejo-runner/cache/uv2",
        )
        self.assertNotEqual(mutated, self.bootstrap)
        problems = copy_problems("bootstrap.sh", mutated, self.shared)
        self.assertEqual(len(problems), 1)
        self.assertIn("UV_CACHE_DIR", problems[0])

    def test_a_dropped_variable_names_the_variable(self):
        lines = [l for l in self.bootstrap.splitlines() if "npm_config_cache=" not in l]
        problems = copy_problems("bootstrap.sh", "\n".join(lines), self.shared)
        self.assertEqual(len(problems), 1)
        self.assertIn("npm_config_cache is missing", problems[0])

    def test_the_old_provision_probe_is_reported(self):
        # The probe as it was before the shared file: no lowercase npm variable.
        old = "\n".join(
            f"     {name}={value} \\"
            for name, value in self.shared.items()
            if name != "npm_config_cache"
        )
        problems = copy_problems("provision.sh", old, self.shared)
        self.assertEqual(problems, [f"provision.sh: npm_config_cache is missing; the shared file sets {self.shared['npm_config_cache']}"])

    def test_a_second_variable_under_the_cache_directory_is_reported(self):
        mutated = self.bootstrap.replace(
            "PIP_CACHE_DIR=/var/lib/forgejo-runner/cache/pip \\",
            "PIP_CACHE_DIR=/var/lib/forgejo-runner/cache/pip \\\n      HF_HOME=/var/lib/forgejo-runner/cache/hf \\",
        )
        self.assertNotEqual(mutated, self.bootstrap)
        problems = copy_problems("bootstrap.sh", mutated, self.shared)
        self.assertEqual(len(problems), 1)
        self.assertIn("HF_HOME", problems[0])

    def test_a_copy_that_moves_a_value_out_of_the_cache_is_reported(self):
        mutated = self.provision.replace(
            "PIP_CACHE_DIR=/var/lib/forgejo-runner/cache/pip",
            "PIP_CACHE_DIR=/var/lib/forgejo-runner/pip",
        )
        self.assertNotEqual(mutated, self.provision)
        problems = copy_problems("provision.sh", mutated, self.shared)
        self.assertTrue(any("PIP_CACHE_DIR" in p for p in problems), problems)

    def test_the_path_check_rejects_values_outside_the_cache_directory(self):
        for value in (
            "/var/lib/forgejo-runner",
            "/var/lib/forgejo-runner/cachex",
            "/var/lib/forgejo-runner/cache/../x",
            "/tmp/cache",
        ):
            with self.subTest(value=value):
                self.assertFalse(under_cache(value))
        self.assertTrue(under_cache(CACHE_DIRECTORY + "/uv"))

    def test_a_shared_name_defined_twice_is_refused(self):
        with self.assertRaises(AssertionError):
            definition_from("A=/var/lib/forgejo-runner/cache/a\nA=/var/lib/forgejo-runner/cache/b\n")


FENCE = re.compile(r"^```[A-Za-z0-9_-]*\n(.*?)^```", re.DOTALL | re.MULTILINE)
EXAMPLE_ASSIGNMENT = re.compile(r"(?<![\w$])([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S+)")
PATH_OPTION = re.compile(r"(?:^|\s)(?:-d|--dest|--find-links|--cache-dir)[ =](\"[^\"]*\"|'[^']*'|\S+)")
FILESYSTEM_PATH = re.compile(r"/var/lib/forgejo-runner\S*")


def fenced_blocks(text):
    return FENCE.findall(text)


def cache_reference(value):
    """True when ``value`` is a cache path or an expansion of a cache variable."""
    value = value.strip("\"'")
    if value.startswith("$") or value.startswith("${"):
        name = re.match(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", value)
        return bool(name) and name.group(1) in set(shared_definition()) | {"PLAYWRIGHT_BROWSERS_PATH"}
    return under_cache(value)


def documented_path_problems(text):
    """Cache variables and paths in fenced examples that leave the shared cache."""
    watched = set(shared_definition()) | {"PLAYWRIGHT_BROWSERS_PATH"}
    problems = []
    for block in fenced_blocks(text):
        for line in block.splitlines():
            if line.lstrip().startswith("#"):
                continue
            for name, value in EXAMPLE_ASSIGNMENT.findall(line):
                if name in watched and not cache_reference(value):
                    problems.append(f"{name}={value} is outside {CACHE_DIRECTORY}")
            for value in PATH_OPTION.findall(line):
                if not cache_reference(value):
                    problems.append(f"option path {value} is outside {CACHE_DIRECTORY}")
            for path in FILESYSTEM_PATH.findall(line):
                if not under_cache(path.rstrip("\"'")):
                    problems.append(f"{path} is outside {CACHE_DIRECTORY}")
    return problems


class DocumentedExampleTests(unittest.TestCase):
    def setUp(self):
        self.text = WARM_DOC.read_text(encoding="utf-8")

    def test_every_documented_cache_path_stays_under_the_shared_cache_directory(self):
        self.assertEqual(documented_path_problems(self.text), [])

    def test_the_examples_cover_each_ecosystem_with_its_offline_gate(self):
        blocks = "\n".join(fenced_blocks(self.text))
        for marker in (
            "uv sync --frozen",
            "UV_OFFLINE=1",
            "npm ci",
            "npm ci --offline",
            "pip download",
            "--no-index",
            "PLAYWRIGHT_BROWSERS_PATH",
            "--prime",
            "--offline",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, blocks)

    def test_the_checker_catches_an_example_that_leaves_the_cache(self):
        cases = {
            "a cache variable": self.text.replace(
                'PLAYWRIGHT_BROWSERS_PATH="$XDG_CACHE_HOME/ms-playwright"',
                "PLAYWRIGHT_BROWSERS_PATH=/opt/browsers",
                1,
            ),
            "a pip destination": self.text.replace(
                '-d "$PIP_CACHE_DIR/downloads"', "-d /var/tmp/downloads", 1
            ),
            "a runner-home path outside the cache": self.text.replace(
                "```sh\n", "```sh\nls /var/lib/forgejo-runner/.local\n", 1
            ),
        }
        for label, mutated in cases.items():
            with self.subTest(label):
                self.assertNotEqual(mutated, self.text)
                self.assertTrue(documented_path_problems(mutated))

    def test_the_checker_accepts_a_path_under_the_cache_and_ignores_prose(self):
        sample = (
            "Prose mentions /var/lib/forgejo-runner/.runner outside a block.\n"
            "```sh\n"
            "UV_CACHE_DIR=/var/lib/forgejo-runner/cache/uv\n"
            'pip download -r requirements.txt -d "$PIP_CACHE_DIR/downloads"\n'
            "```\n"
        )
        self.assertEqual(documented_path_problems(sample), [])


if __name__ == "__main__":
    unittest.main()
