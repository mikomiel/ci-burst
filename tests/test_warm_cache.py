"""Contract tests for the optional warm-cache layer script.

``deploy/runner/image/warm-cache.sh`` runs as root on a builder, fetches a
consumer's source archive and warms the job account's cache. These tests read
the committed script and run it against stub commands, never against a builder,
an account or a network. The harness follows ``tests/test_runner_bootstrap.py``:
the shipped text is rewritten to point at a temporary tree, and only external
commands are stubbed (``runuser``, ``systemd-run``, ``cloud-init``, ``curl``,
``shutdown``, ``sleep``, ``sync``, ``chown``). The control flow, the framing,
the digest check and the clean-up are the shipped code.

Because the script powers a host off and deletes a directory, the harness fails
closed: before every run it checks that no real absolute path survived the
rewrite and that every side-effecting command resolves to a stub.

The cases pin the properties the worker-image specification states: refusals
before any consumer command, the digest check before extraction, no unframed
``IMAGE_READY`` from any consumer output, success that stays up and failure that
powers off, the exact environment of consumer commands, and a clean-up that
keeps only ``cache``.
"""

import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import tomllib
import unittest
from unittest import mock
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "runner" / "image" / "warm-cache.sh"
SHARED_ENV = ROOT / "deploy" / "runner" / "cache-env.sh"
WARM_DOC = ROOT / "docs" / "warm-image.md"

MARKER = "IMAGE_READY"
FRAME = "  | "
# Assembled at runtime, so no secret-shaped literal sits in this file.
SECRET_URL_TOKEN = "-".join(("not", "a", "real", "signature", "value"))
SOURCE_URL = f"https://builder-source.invalid/archive.tar.gz?token={SECRET_URL_TOKEN}"

#: Commands the script runs that act on the host. Each must resolve to a stub.
STUBBED = (
    "shutdown", "systemd-run", "systemctl", "runuser", "cloud-init", "curl", "sleep", "sync",
    "chown", "aws", "tar", "id",
)

#: What bash itself adds to an empty environment.
BASH_INTERNAL = {"PWD", "SHLVL", "_", "OLDPWD"}


def shared_environment():
    """The shared cache environment, name to value."""
    pairs = {}
    for line in SHARED_ENV.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.partition("=")
            pairs[name] = value
    return pairs


def make_archive(directory, files=None):
    """A .tar.gz of ``files`` (name to text); return its path and sha256."""
    files = files or {"lock.txt": "locked\n"}
    source = Path(directory) / "archive-source"
    source.mkdir(exist_ok=True)
    for name, text in files.items():
        (source / name).write_text(text, encoding="utf-8")
    archive = Path(directory) / "archive.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in files:
            tar.add(source / name, arcname=name)
    return archive, sha256(archive.read_bytes()).hexdigest()


def make_wrapped_archive(directory, prefix="example-repo", files=None):
    """A .tar.gz whose members all sit under one top-level directory, as a forge archive does."""
    files = files or {"lock.txt": "locked\n"}
    source = Path(directory) / "wrapped-source"
    source.mkdir(exist_ok=True)
    archive = Path(directory) / "wrapped.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        top = tarfile.TarInfo(prefix)
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        tar.addfile(top)
        for name, content in files.items():
            (source / name).write_text(content, encoding="utf-8")
            tar.add(source / name, arcname=f"{prefix}/{name}")
    return archive, sha256(archive.read_bytes()).hexdigest()


STUB_BODIES = {
    "shutdown": "",
    "systemd-run": "",
    "sleep": "",
    "sync": "",
    "chown": "",
    "aws": "exit 99",
    "cloud-init": """
if [ -n "${STUB_LATE_PROCESS_PID:-}" ]; then
  mkdir -p "$STUB_PROC/$STUB_LATE_PROCESS_PID"
  printf 'Uid:\\t%s\\t%s\\t%s\\t%s\\n' "$STUB_RUNNER_UID" "$STUB_RUNNER_UID" "$STUB_RUNNER_UID" "$STUB_RUNNER_UID" \\
    > "$STUB_PROC/$STUB_LATE_PROCESS_PID/status"
fi
exit "${STUB_CLOUD_INIT_RC:-0}"
""",
    "curl": """
echo "$STUB_CURL_STDOUT"
out=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift 2 ;;
    --config) [ "$2" = - ] && cat > "$STUB_DIR/curl-config"; shift 2 ;;
    *) shift ;;
  esac
done
[ "${STUB_CURL_RC:-0}" = 0 ] || exit "$STUB_CURL_RC"
cp "$STUB_ARCHIVE" "$out"
""",
    # The script no longer uses runuser or a root tar: either call is a defect.
    "runuser": "echo RUNUSER >> \"$STUB_DIR/events\"\nexit 97",
    "tar": "echo ROOT_TAR >> \"$STUB_DIR/events\"\nexit 97",
    "id": 'echo "$STUB_RUNNER_UID"',
    "systemctl": "",
}

#: Stands in for the unit manager. The lifetime timer is only recorded. A
#: transient unit runs its command in a session of its own and, when the main
#: process ends, kills the whole group, which is what KillMode=control-group does
#: to the unit's control group. The command after the options starts with
#: ``env -i NAME=value ...``: the stub finds the program that env would run.
#:
#: Like the real manager, it expands ``${NAME}`` and ``$NAME`` in the command's
#: arguments from its own environment (empty when unset) and turns ``$$`` into
#: ``$``, before anything runs, unless ``--expand-environment=no`` is among the
#: options. Without the flag a consumer's ``${UV_CACHE_DIR}`` is gone before bash
#: sees it, so the tests below fail when the script drops the flag.
SYSTEMD_RUN_STUB = r"""#!/bin/bash
case "$*" in
  *--on-active=*) echo "systemd-run $*" >> "$STUB_DIR/events"; exit 0 ;;
esac
unit=
options=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --unit=*) unit=${1#--unit=}; shift ;;
    --*) options="$options $1"; shift ;;
    *) break ;;
  esac
done
echo "UNIT $unit" >> "$STUB_DIR/events"
echo "UNIT_OPTIONS$options" >> "$STUB_DIR/events"
expand_argument() {
  local rest=$1 out=
  local pattern='^(([^$]|\$[^{A-Za-z_$])*)\$(\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*)|(\$))(.*)$'
  while [[ $rest =~ $pattern ]]; do
    out+=${BASH_REMATCH[1]}
    if [ -n "${BASH_REMATCH[4]}" ]; then
      out+=${!BASH_REMATCH[4]:-}
    elif [ -n "${BASH_REMATCH[5]}" ]; then
      out+=${!BASH_REMATCH[5]:-}
    else
      out+='$'
    fi
    rest=${BASH_REMATCH[7]}
  done
  expanded=$out$rest
}
case " $options " in
  *" --expand-environment=no "*) ;;
  *)
    arguments=()
    for argument in "$@"; do
      expand_argument "$argument"
      arguments+=("$expanded")
    done
    set -- "${arguments[@]}"
    ;;
esac
[ "$1" = env ] && [ "$2" = -i ] || exit 98
program=
program_arguments=()
for argument in "${@:3}"; do
  if [ -n "$program" ]; then
    program_arguments+=("$argument")
    continue
  fi
  case "$argument" in
    *=*) ;;
    *) program=$argument ;;
  esac
done
case "$program" in
  bash) echo CONSUMER >> "$STUB_DIR/events" ;;
  tar)
    echo "EXTRACT $program ${program_arguments[*]}" >> "$STUB_DIR/events"
    if [ -n "${STUB_TAR_NOISE:-}" ]; then
      printf '%b' "$STUB_TAR_NOISE" >&2
      [ "${STUB_TAR_FAIL:-0}" = 0 ] || exit "$STUB_TAR_FAIL"
    fi
    ;;
  *) exit 99 ;;
esac
exec 3<&0
setsid "$@" <&3 &
pid=$!
wait "$pid"
rc=$?
kill -KILL -- "-$pid" 2>/dev/null || true
exit "$rc"
"""


class WarmHarness:
    """Run the shipped script in a temporary tree against stub commands."""

    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.home = self.root / "runner-home"  # stands in for /var/lib/forgejo-runner
        self.vartmp = self.root / "vartmp"  # stands in for /var/tmp
        self.run_dir = self.root / "tmpfs"  # stands in for /run
        self.stub = self.root / "stub"
        self.console = self.root / "console"  # stands in for /dev/ttyS0
        self.proc = self.root / "procfs"  # stands in for /proc
        for path in (self.home, self.vartmp, self.run_dir, self.stub, self.proc):
            path.mkdir()
        # A baseline process table: a root process and one of another account.
        self.add_process(1, 0)
        self.add_process(500, self.RUNNER_UID + 1)
        self.console.write_text("", encoding="utf-8")
        self.events = self.stub / "events"
        self.events.write_text("", encoding="utf-8")
        for name, body in STUB_BODIES.items():
            self.write_stub(name, body)
        unit_manager = self.stub / "systemd-run"
        unit_manager.write_text(SYSTEMD_RUN_STUB, encoding="utf-8")
        unit_manager.chmod(unit_manager.stat().st_mode | stat.S_IXUSR)
        self.archive, self.digest = make_archive(self.root)

    RUNNER_UID = 4242

    def add_process(self, pid, uid):
        (self.proc / str(pid)).mkdir(exist_ok=True)
        (self.proc / str(pid) / "status").write_text(
            f"Name:\tstub\nPid:\t{pid}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n", encoding="utf-8"
        )

    def write_stub(self, name, body):
        path = self.stub / name
        path.write_text(
            f'#!/bin/bash\necho "{name} $*" >> "$STUB_DIR/events"\n{body}\n',
            encoding="utf-8",
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def rendered(self):
        """The shipped script with its paths pointed at the temporary tree."""
        text = SCRIPT.read_text(encoding="utf-8")
        text = (
            text.replace("/var/lib/forgejo-runner", str(self.home))
            .replace("/var/tmp/", f"{self.vartmp}/")
            .replace("/run/", f"{self.run_dir}/")
            .replace("/proc/", f"{self.proc}/")
            .replace("/dev/ttyS0", str(self.console))
        )
        # Fail closed: no real host path may survive, so a missed replacement
        # cannot reach the builder's, or this host's, filesystem.
        stripped = text.replace(str(self.root), "")
        leftovers = re.findall(r"/(?:var|run|tmp|etc|root|home|proc)/\S*|/dev/tty\S*", stripped)
        self.assertEqual(leftovers, [], "an unreplaced host path would reach this host")
        for line in text.splitlines():
            if "/sbin/shutdown" in line:
                self.assertIn("systemd-run", line, "only the timer may name the real shutdown")
        return text

    def start(self, **inputs):
        """Run the script with inputs; return the completed process."""
        defaults = {
            "WARM_SRC_URL": SOURCE_URL,
            "WARM_SRC_SHA256": self.digest,
            "WARM_COMMANDS": "cat lock.txt",
        }
        defaults.update(inputs)
        env = {
            "PATH": f"{self.stub}:/usr/local/bin:/usr/bin:/bin",
            "STUB_DIR": str(self.stub),
            "STUB_ARCHIVE": str(self.archive),
            "STUB_RUNNER_UID": str(self.RUNNER_UID),
            "STUB_PROC": str(self.proc),
            "SECRET_CANARY": "root-credential-canary",
            "LANG": "C.UTF-8",
        }
        for name in STUBBED:
            self.assertEqual(
                shutil.which(name, path=env["PATH"]), str(self.stub / name), f"{name} must be a stub"
            )
        env.update({name: value for name, value in defaults.items() if value is not None})
        env.update(self.extra_env)
        script = self.root / "warm-cache-under-test.sh"
        script.write_text(self.rendered(), encoding="utf-8")
        completed = subprocess.run(
            ["bash", str(script)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            check=False,
            text=True,
        )
        self.output = completed.stdout
        self.lines = completed.stdout.splitlines()
        return completed

    extra_env = {}

    def event_list(self):
        return self.events.read_text(encoding="utf-8").splitlines()

    def unframed_markers(self):
        return [line for line in self.lines if line.startswith(MARKER)]

    def console_lines(self):
        return self.console.read_text(encoding="utf-8").splitlines()


class RefusalTests(WarmHarness, unittest.TestCase):
    def assertRefused(self, completed):
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.unframed_markers(), [])
        events = self.event_list()
        self.assertNotIn("CONSUMER", events)
        self.assertFalse([e for e in events if e.startswith("curl")], "nothing is fetched on a refusal")
        self.assertTrue(
            [e for e in events if e.startswith("systemd-run")],
            "the lifetime timer is armed even when the script refuses",
        )
        self.assertEqual(events[-1], "shutdown -h now")

    def test_a_missing_sha256_refuses_before_any_consumer_command(self):
        self.assertRefused(self.start(WARM_SRC_SHA256=""))
        self.assertIn("WARM_SRC_SHA256 is required", self.output)

    def test_empty_warm_commands_refuse_before_any_consumer_command(self):
        for commands in ("", "\n\n   \n", "# only a comment\n  # and another\n"):
            with self.subTest(commands=commands):
                self.events.write_text("", encoding="utf-8")
                self.assertRefused(self.start(WARM_COMMANDS=commands))
                self.assertIn("WARM_COMMANDS is empty", self.output)

    def test_malformed_inputs_refuse(self):
        cases = {
            "a digest that is not 64 hex digits": {"WARM_SRC_SHA256": "abc123"},
            "a missing URL": {"WARM_SRC_URL": ""},
            "a URL that is not https": {"WARM_SRC_URL": "http://builder-source.invalid/a.tar.gz"},
            "a URL with whitespace": {"WARM_SRC_URL": "https://builder-source.invalid/a b"},
            "a URL with a quote": {"WARM_SRC_URL": 'https://builder-source.invalid/a"b'},
            "a URL with a backslash": {"WARM_SRC_URL": "https://builder-source.invalid/a\\b"},
            "an empty strip setting": {"WARM_SRC_STRIP_COMPONENTS": ""},
            "a lifetime below the bound": {"WARM_LIFETIME_SECONDS": "10"},
            "a lifetime above the bound": {"WARM_LIFETIME_SECONDS": "99999"},
            "a lifetime that is not a number": {"WARM_LIFETIME_SECONDS": "soon"},
            "a snapshot window above the bound": {"WARM_SNAPSHOT_SECONDS": "86400"},
        }
        for label, inputs in cases.items():
            with self.subTest(label):
                self.events.write_text("", encoding="utf-8")
                self.assertRefused(self.start(**inputs))

    def test_a_strip_components_value_other_than_zero_or_one_refuses(self):
        # Only the exact text 0 or 1 passes; an unset value defaults to 0, and an empty one is refused
        # with the malformed inputs above.
        for value in ("2", "-1", "1 ", " 1", "x", "01", "1\n", "0x1", "1.0", "10"):
            with self.subTest(value=value):
                self.events.write_text("", encoding="utf-8")
                self.assertRefused(self.start(WARM_SRC_STRIP_COMPONENTS=value))
                self.assertIn("WARM_SRC_STRIP_COMPONENTS must be 0 or 1", self.output)
                self.assertFalse([e for e in self.event_list() if e.startswith("EXTRACT")])

    def test_an_invalid_lifetime_still_arms_the_default_timer(self):
        self.start(WARM_LIFETIME_SECONDS="soon")
        timers = [e for e in self.event_list() if e.startswith("systemd-run")]
        self.assertEqual(len(timers), 1)
        self.assertIn("--on-active=5400", timers[0])

    def test_a_refusal_never_prints_the_url(self):
        self.start(WARM_SRC_SHA256="")
        self.assertNotIn(SECRET_URL_TOKEN, self.output)


class SourceTests(WarmHarness, unittest.TestCase):
    def test_a_digest_mismatch_stops_before_extraction_and_any_command(self):
        completed = self.start(WARM_SRC_SHA256="0" * 64)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("digest mismatch", self.output)
        self.assertEqual(self.unframed_markers(), [])
        self.assertNotIn("CONSUMER", self.event_list())
        self.assertFalse((self.vartmp / "warm-src").exists(), "nothing was extracted")
        self.assertFalse((self.vartmp / "warm-src.tar.gz").exists(), "the bad archive is removed")
        self.assertEqual(self.event_list()[-1], "shutdown -h now")

    def test_an_uppercase_digest_is_accepted(self):
        completed = self.start(WARM_SRC_SHA256=self.digest.upper())
        self.assertEqual(completed.returncode, 0, self.output)
        self.assertEqual(self.lines[-1], MARKER)

    def test_a_failed_fetch_ends_without_a_marker_and_without_the_url(self):
        self.extra_env = {"STUB_CURL_RC": "22"}
        completed = self.start()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("WARM FAIL fetch rc=22", self.output)
        self.assertEqual(self.unframed_markers(), [])
        self.assertNotIn(SECRET_URL_TOKEN, self.output)
        self.assertNotIn(SECRET_URL_TOKEN, self.console.read_text(encoding="utf-8"))

    def test_the_source_is_extracted_outside_the_runner_home(self):
        self.start(WARM_COMMANDS=f"pwd > {self.root}/cwd.txt\ncat lock.txt")
        self.assertEqual((self.root / "cwd.txt").read_text(encoding="utf-8").strip(), str(self.vartmp / "warm-src"))
        self.assertNotIn(str(self.home), str(self.vartmp / "warm-src"))


class StripComponentsTests(WarmHarness, unittest.TestCase):
    """WARM_SRC_STRIP_COMPONENTS is the only thing that decides how many components extraction drops."""

    def extraction(self):
        return [e for e in self.event_list() if e.startswith("EXTRACT ")]

    def argv(self, strip):
        return f"EXTRACT tar -xzf - -C {self.vartmp / 'warm-src'} --no-same-owner --no-same-permissions {strip}"

    def test_unset_and_zero_keep_every_path_component(self):
        self.archive, self.digest = make_wrapped_archive(self.root)
        kept = self.root / "kept.txt"
        cases = (
            ("unset", {}),
            ("zero", {"WARM_SRC_STRIP_COMPONENTS": "0"}),
        )
        for label, inputs in cases:
            with self.subTest(label):
                self.events.write_text("", encoding="utf-8")
                kept.unlink(missing_ok=True)
                completed = self.start(WARM_COMMANDS=f"cat example-repo/lock.txt > {kept}", **inputs)
                self.assertEqual(completed.returncode, 0, self.output)
                self.assertEqual(kept.read_text(encoding="utf-8"), "locked\n")
                self.assertEqual(self.extraction(), [self.argv("--strip-components=0")])

    def test_one_drops_the_leading_directory_so_commands_run_from_the_source_root(self):
        self.archive, self.digest = make_wrapped_archive(self.root)
        seen = self.root / "seen.txt"
        completed = self.start(
            WARM_SRC_STRIP_COMPONENTS="1",
            WARM_COMMANDS=f"cat lock.txt > {seen}\ntest ! -e example-repo",
        )
        self.assertEqual(completed.returncode, 0, self.output)
        self.assertEqual(seen.read_text(encoding="utf-8"), "locked\n")
        self.assertEqual(self.extraction(), [self.argv("--strip-components=1")])
        self.assertEqual(self.lines[-1], MARKER)

    def test_a_wrapped_archive_stays_wrapped_without_the_input(self):
        # The counterpart of the test above: the flag is what moves the files.
        self.archive, self.digest = make_wrapped_archive(self.root)
        completed = self.start(WARM_COMMANDS="cat lock.txt")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("WARM FAIL warm 1/1 rc=1", self.output)
        self.assertEqual(self.unframed_markers(), [])


class FetchTests(WarmHarness, unittest.TestCase):
    """The presigned URL reaches curl on stdin, never as an argument."""

    def test_the_url_is_never_a_process_argument(self):
        completed = self.start()
        self.assertEqual(completed.returncode, 0, self.output)
        fetches = [e for e in self.event_list() if e.startswith("curl")]
        self.assertEqual(len(fetches), 1)
        self.assertNotIn(SECRET_URL_TOKEN, fetches[0])
        self.assertIn("--config -", fetches[0])
        self.assertTrue(fetches[0].startswith("curl --disable "), "no default curl config may be read")
        config = (self.stub / "curl-config").read_text(encoding="utf-8")
        self.assertEqual(config, f'url = "{SOURCE_URL}"\n')
        self.assertNotIn(SECRET_URL_TOKEN, self.output)

    def test_nothing_curl_prints_reaches_the_console(self):
        self.extra_env = {"STUB_CURL_STDOUT": f"> GET /archive.tar.gz?token={SECRET_URL_TOKEN}"}
        completed = self.start()
        self.assertEqual(completed.returncode, 0, self.output)
        self.assertNotIn(SECRET_URL_TOKEN, self.output)


class FailureTests(WarmHarness, unittest.TestCase):
    def assertFailedBuild(self, completed):
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.unframed_markers(), [])
        self.assertNotIn(MARKER, [line.strip() for line in self.lines])
        events = self.event_list()
        self.assertEqual(events[-3:], ["sleep 300", "sync ", "shutdown -h now"])

    def test_a_failing_warm_command_exits_non_zero_without_a_marker(self):
        completed = self.start(
            WARM_COMMANDS=f"echo first-ran\nexit 3\ntouch {self.root}/third-ran"
        )
        self.assertFailedBuild(completed)
        self.assertFalse((self.root / "third-ran").exists(), "later commands do not run")
        self.assertIn("WARM FAIL warm 2/3 rc=3", self.output)

    def test_a_failing_gate_exits_non_zero_without_a_marker(self):
        completed = self.start(WARM_GATE_COMMAND="echo gate-ran; exit 4")
        self.assertFailedBuild(completed)
        self.assertIn("WARM FAIL gate rc=4", self.output)

    def test_a_failure_prints_a_framed_log_tail_and_nothing_unframed(self):
        completed = self.start(WARM_COMMANDS="echo cause-visible >&2; echo second-line; exit 1")
        self.assertFailedBuild(completed)
        self.assertIn(f"{FRAME}cause-visible", self.lines)
        self.assertIn(f"{FRAME}second-line", self.lines)
        self.assertNotIn("cause-visible", [line for line in self.lines if not line.startswith(FRAME)])

    def test_a_failure_tail_never_shows_the_archive_url_or_its_query(self):
        query = SOURCE_URL.split("?", 1)[1]
        completed = self.start(WARM_COMMANDS=f"echo 'data {SOURCE_URL} end'; echo 'q={query};'; exit 1")
        self.assertFailedBuild(completed)
        self.assertNotIn(SECRET_URL_TOKEN, self.output)
        self.assertIn(f"{FRAME}data [archive-url] end", self.lines)
        self.assertIn(f"{FRAME}q=[archive-url];", self.lines)

    def test_the_tail_is_bounded_to_the_last_lines_of_the_log(self):
        completed = self.start(WARM_COMMANDS="seq 1 500; exit 1")
        self.assertFailedBuild(completed)
        framed = [line for line in self.lines if line.startswith(FRAME)]
        self.assertEqual(len(framed), 60)
        self.assertEqual(framed[-1], f"{FRAME}500")

    def test_a_failed_build_holds_briefly_and_then_powers_off(self):
        self.start(WARM_COMMANDS="exit 1")
        events = self.event_list()
        hold = events.index("sleep 300")
        self.assertEqual(events.index("shutdown -h now"), len(events) - 1)
        self.assertLess(hold, events.index("shutdown -h now"))

    def test_a_failing_command_that_prints_the_marker_leaves_no_unframed_marker(self):
        payloads = {
            "plain": f"echo {MARKER}; exit 1",
            "with a carriage return": f"printf 'x\\r{MARKER}\\n'; exit 1",
            "with a NUL": f"printf 'x\\0{MARKER}\\n'; exit 1",
            "with a form feed": f"printf 'x\\f{MARKER}\\n'; exit 1",
            "with a vertical tab": f"printf 'x\\v{MARKER}\\n'; exit 1",
            "with a file separator": f"printf 'x\\x1c{MARKER}\\n'; exit 1",
            "with a backspace": f"printf 'x\\b{MARKER}\\n'; exit 1",
            "after an escape sequence": f"printf 'x\\033[1G{MARKER}\\n'; exit 1",
            "after a line separator": f"printf 'x\\xe2\\x80\\xa8{MARKER}\\n'; exit 1",
            "after a next-line character": f"printf 'x\\xc2\\x85{MARKER}\\n'; exit 1",
            "to stderr": f"echo {MARKER} >&2; exit 1",
            "after text on the same line": f"printf 'ok {MARKER}'; exit 1",
        }
        for label, command in payloads.items():
            for field in ("WARM_COMMANDS", "WARM_GATE_COMMAND"):
                with self.subTest(label=label, field=field):
                    self.events.write_text("", encoding="utf-8")
                    self.console.write_text("", encoding="utf-8")
                    inputs = {field: command}
                    if field == "WARM_GATE_COMMAND":
                        inputs["WARM_COMMANDS"] = "echo warm"
                    completed = self.start(**inputs)
                    self.assertFailedBuild(completed)
                    for line in self.lines + self.console_lines() + self.output.split("\n"):
                        self.assertFalse(line.startswith(MARKER), repr(line))
                    self.assertTrue([l for l in self.lines if l.startswith(FRAME) and MARKER in l])


class SuccessTests(WarmHarness, unittest.TestCase):
    def test_success_prints_the_marker_once_as_the_last_output_with_the_digest(self):
        completed = self.start(WARM_GATE_COMMAND="cat lock.txt")
        self.assertEqual(completed.returncode, 0, self.output)
        self.assertEqual(self.unframed_markers(), [MARKER])
        self.assertEqual(self.lines[-1], MARKER)
        self.assertEqual(self.lines[-2], f"WARM_SOURCE_SHA256 {self.digest}")
        self.assertEqual(self.console_lines()[-1], MARKER)

    def test_success_stays_up_for_the_snapshot_window_and_never_powers_off(self):
        completed = self.start(WARM_SNAPSHOT_SECONDS="900")
        self.assertEqual(completed.returncode, 0, self.output)
        events = self.event_list()
        self.assertEqual(events[-1], "sleep 900", "the last act is the snapshot window")
        self.assertFalse([e for e in events if e.startswith("shutdown")])
        self.assertNotIn("sleep 300", events, "the failure hold is not used on success")

    def test_the_default_snapshot_window_and_lifetime_apply(self):
        self.start()
        events = self.event_list()
        self.assertEqual(events[-1], "sleep 1800")
        timers = [e for e in events if e.startswith("systemd-run")]
        self.assertEqual(len(timers), 1)
        self.assertIn("--on-active=5400", timers[0])

    def test_the_timer_is_armed_before_the_first_consumer_command(self):
        self.start(WARM_LIFETIME_SECONDS="900", WARM_GATE_COMMAND="cat lock.txt")
        events = self.event_list()
        timer = next(i for i, e in enumerate(events) if e.startswith("systemd-run"))
        self.assertIn("--on-active=900", events[timer])
        self.assertIn("/sbin/shutdown -h now", events[timer])
        self.assertLess(timer, events.index("CONSUMER"))
        self.assertLess(timer, next(i for i, e in enumerate(events) if e.startswith("curl")))

    def test_a_successful_command_that_prints_the_marker_stays_out_of_the_output(self):
        completed = self.start(
            WARM_COMMANDS=f"echo {MARKER}\nprintf '{MARKER}\\r\\n'",
            WARM_GATE_COMMAND=f"echo {MARKER} >&2",
        )
        self.assertEqual(completed.returncode, 0, self.output)
        self.assertEqual(self.unframed_markers(), [MARKER], "the script's own marker is the only one")
        self.assertEqual(self.output.count(MARKER), 1, "consumer text never reaches the output")
        self.assertEqual(self.lines[-1], MARKER)

    def test_consumer_output_goes_to_a_log_not_to_the_output(self):
        self.start(WARM_COMMANDS="echo noisy-consumer-line; echo noisy-error >&2")
        self.assertNotIn("noisy-consumer-line", self.output)
        self.assertNotIn("noisy-error", self.output)
        logs = list(self.run_dir.glob("warm-cache.*"))
        self.assertEqual(len(logs), 1)
        text = logs[0].read_text(encoding="utf-8")
        self.assertIn("noisy-consumer-line", text)
        self.assertIn("noisy-error", text)

    def test_consumer_stdin_is_empty(self):
        completed = self.start(WARM_COMMANDS=f"cat > {self.root}/stdin.txt")
        self.assertEqual(completed.returncode, 0, self.output)
        self.assertEqual((self.root / "stdin.txt").read_text(encoding="utf-8"), "")

    def test_the_url_never_reaches_the_output_on_success(self):
        self.start()
        self.assertNotIn(SECRET_URL_TOKEN, self.output)
        self.assertNotIn(SECRET_URL_TOKEN, self.console.read_text(encoding="utf-8"))

    def test_the_script_calls_no_cloud_api(self):
        self.start(WARM_GATE_COMMAND="cat lock.txt")
        self.assertFalse([e for e in self.event_list() if e.startswith("aws")])
        text = SCRIPT.read_text(encoding="utf-8")
        executable = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
        self.assertFalse([l for l in executable if re.search(r"\baws\b|ec2|create-image", l)])

    def test_each_command_runs_in_order_and_the_gate_runs_last(self):
        order = self.root / "order.txt"
        self.start(
            WARM_COMMANDS=f"echo one >> {order}\n\n# skipped\n  echo two >> {order}",
            WARM_GATE_COMMAND=f"echo gate >> {order}",
        )
        self.assertEqual(order.read_text(encoding="utf-8").split(), ["one", "two", "gate"])


class EnvironmentTests(WarmHarness, unittest.TestCase):
    def read_env(self, path):
        pairs = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            pairs[name] = value
        return pairs

    def test_commands_receive_exactly_the_shared_cache_environment(self):
        warm_env = self.root / "warm-env.txt"
        gate_env = self.root / "gate-env.txt"
        completed = self.start(
            WARM_COMMANDS=f"env > {warm_env}; umask > {self.root}/umask.txt",
            WARM_GATE_COMMAND=f"env > {gate_env}",
        )
        self.assertEqual(completed.returncode, 0, self.output)
        shared = {
            name: value.replace("/var/lib/forgejo-runner", str(self.home))
            for name, value in shared_environment().items()
        }
        for path in (warm_env, gate_env):
            with self.subTest(path.name):
                seen = self.read_env(path)
                for name in BASH_INTERNAL:
                    seen.pop(name, None)
                expected = dict(shared)
                expected["HOME"] = str(self.home)
                expected["LC_ALL"] = "C.UTF-8"
                expected["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                self.assertEqual(seen, expected)
                self.assertNotIn("SECRET_CANARY", seen)
        self.assertEqual((self.root / "umask.txt").read_text(encoding="utf-8").strip(), "0022")

    def test_the_inherited_environment_and_the_inputs_never_reach_a_command(self):
        seen = self.root / "seen.txt"
        self.start(WARM_COMMANDS=f"env > {seen}")
        text = seen.read_text(encoding="utf-8")
        for secret in ("root-credential-canary", SECRET_URL_TOKEN, self.digest):
            self.assertNotIn(secret, text)
        self.assertFalse(re.search(r"^WARM_", text, re.MULTILINE))

    # systemd-run replaces ${NAME} and $NAME in its arguments with the service
    # manager's own value before env -i runs; the stub does the same unless the
    # script passes --expand-environment=no.
    EXPRESSIONS = (
        'printf "%s\\n" "${UV_CACHE_DIR}" > @cache@\n'
        'printf "%s\\n" $HOME > @home@\n'
        'printf "%s|%s\\n" "$$" "$(echo inner)" > @shell@'
    )

    def run_expressions(self):
        paths = {name: self.root / f"expr-{name}.txt" for name in ("cache", "home", "shell")}
        command = self.EXPRESSIONS
        for name, path in paths.items():
            command = command.replace(f"@{name}@", str(path))
        completed = self.start(WARM_COMMANDS=command)
        return completed, paths

    def test_variable_expressions_in_a_command_reach_bash_unchanged(self):
        completed, paths = self.run_expressions()
        self.assertEqual(completed.returncode, 0, self.output)
        cache = shared_environment()["UV_CACHE_DIR"].replace("/var/lib/forgejo-runner", str(self.home))
        self.assertEqual(paths["cache"].read_text(encoding="utf-8").strip(), cache)
        self.assertEqual(paths["home"].read_text(encoding="utf-8").strip(), str(self.home))
        pid, _, inner = paths["shell"].read_text(encoding="utf-8").strip().partition("|")
        self.assertTrue(pid.isdigit(), "$$ must be bash's process id, not a literal dollar")
        self.assertEqual(inner, "inner")

    def test_without_the_flag_the_service_manager_would_empty_the_variables(self):
        original = WarmHarness.rendered

        def without_flag(harness):
            text = original(harness)
            self.assertIn(" --expand-environment=no", text)
            return text.replace(" --expand-environment=no", "")

        with mock.patch.object(WarmHarness, "rendered", without_flag):
            completed, paths = self.run_expressions()
        self.assertEqual(paths["cache"].read_text(encoding="utf-8").strip(), "")
        self.assertEqual(paths["home"].read_text(encoding="utf-8").strip(), "")
        self.assertEqual(paths["shell"].read_text(encoding="utf-8").strip(), "$|inner")

    def test_the_script_copy_of_the_cache_environment_is_the_shared_one(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for name, value in shared_environment().items():
            self.assertIn(f"  {name}={value}\n", text)


class CleanupTests(WarmHarness, unittest.TestCase):
    def seed_home(self):
        (self.home / ".runner").write_text("registration", encoding="utf-8")
        (self.home / ".bashrc").write_text("stale", encoding="utf-8")
        (self.home / "leftover-dir" / "deep").mkdir(parents=True)
        (self.home / "cache" / "uv" / "pkg").mkdir(parents=True)
        (self.home / "cache" / "uv" / "pkg" / "wheel").write_text("w", encoding="utf-8")

    def test_cleanup_removes_source_registration_and_non_cache_entries_and_keeps_cache(self):
        self.seed_home()
        completed = self.start(
            WARM_COMMANDS=(
                'mkdir -p "$HOME/.local/share" && echo x > "$HOME/.local/share/tool"\n'
                'echo cached > "$NPM_CONFIG_CACHE.marker"; mkdir -p "$NPM_CONFIG_CACHE" && echo n > "$NPM_CONFIG_CACHE/entry"'
            )
        )
        self.assertEqual(completed.returncode, 0, self.output)
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["cache"])
        self.assertTrue((self.home / "cache" / "uv" / "pkg" / "wheel").exists())
        self.assertTrue((self.home / "cache" / "npm" / "entry").exists())
        self.assertFalse((self.vartmp / "warm-src").exists())
        self.assertFalse((self.vartmp / "warm-src.tar.gz").exists())
        self.assertIn("cloud-init clean --logs", self.event_list())

    def test_cleanup_runs_before_the_marker_and_cloud_init_is_cleaned_with_logs(self):
        self.start()
        events = self.event_list()
        self.assertIn("cloud-init clean --logs", events)
        self.assertLess(events.index("cloud-init clean --logs"), events.index("sync "))

    def test_a_failing_cloud_init_clean_prevents_the_marker(self):
        self.extra_env = {"STUB_CLOUD_INIT_RC": "1"}
        completed = self.start()
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.unframed_markers(), [])
        self.assertIn("WARM FAIL cloud-init clean", self.output)
        self.assertEqual(self.event_list()[-1], "shutdown -h now")

    def test_a_failing_sweep_of_the_runner_home_prevents_the_marker(self):
        self.write_stub("find", "exit 1")
        completed = self.start()
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.unframed_markers(), [])
        self.assertEqual(self.event_list()[-1], "shutdown -h now")

    def test_a_cache_that_a_command_replaced_with_a_symlink_prevents_the_marker(self):
        completed = self.start(
            WARM_COMMANDS='rm -rf "$XDG_CACHE_HOME" && ln -s /dev/null "$XDG_CACHE_HOME"'
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.unframed_markers(), [])
        self.assertIn("not a plain directory", self.output)

    def test_a_deleted_cache_prevents_the_marker(self):
        completed = self.start(WARM_COMMANDS='rm -rf "$XDG_CACHE_HOME"')
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.unframed_markers(), [])

    def test_a_failure_removes_the_extracted_source(self):
        self.start(WARM_COMMANDS="exit 1")
        self.assertFalse((self.vartmp / "warm-src").exists())


def make_device_archive(directory):
    """An archive whose only member is a character device node."""
    archive = Path(directory) / "device.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        member = tarfile.TarInfo("device-node")
        member.type = tarfile.CHRTYPE
        member.devmajor, member.devminor = 1, 3
        tar.addfile(member)
    return archive, sha256(archive.read_bytes()).hexdigest()


class ExtractionTests(WarmHarness, unittest.TestCase):
    """The archive is untrusted data: only the job account unpacks it."""

    def test_the_archive_is_extracted_by_the_job_account_in_a_unit_never_by_root(self):
        completed = self.start()
        self.assertEqual(completed.returncode, 0, self.output)
        events = self.event_list()
        self.assertNotIn("ROOT_TAR", events, "root never runs tar")
        self.assertNotIn("RUNUSER", events)
        extract = [e for e in events if e.startswith("EXTRACT ")]
        self.assertEqual(
            extract,
            [f"EXTRACT tar -xzf - -C {self.vartmp / 'warm-src'} --no-same-owner --no-same-permissions --strip-components=0"],
        )
        # The unit that ran it is the first one, and it ran before any warm command.
        self.assertLess(events.index("UNIT warm-job-1"), events.index("CONSUMER"))
        self.assertEqual(events[events.index("UNIT warm-job-1") + 2].split()[0], "EXTRACT")

    def test_no_executable_line_of_the_script_runs_tar_or_a_recursive_chown_as_root(self):
        text = SCRIPT.read_text(encoding="utf-8")
        executable = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
        self.assertEqual([l.strip() for l in executable if re.search(r"(?<![\w./=-])tar\s", l)],
                         ["run_in_unit extract \"$ARCHIVE\" tar -xzf - -C \"$SRC_DIR\" --no-same-owner --no-same-permissions --strip-components=\"$WARM_SRC_STRIP_COMPONENTS\""])
        self.assertFalse([l for l in executable if re.search(r"\bchown\s+-\w*R", l)])
        self.assertFalse([l for l in executable if re.search(r"\brunuser\b", l)])

    def test_a_device_node_in_the_archive_creates_no_device_and_no_marker(self):
        if os.geteuid() == 0:
            self.skipTest("root could make the node; the structural tests above cover this case")
        self.archive, self.digest = make_device_archive(self.root)
        completed = self.start()
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.unframed_markers(), [])
        node = self.vartmp / "warm-src" / "device-node"
        self.assertFalse(node.exists() or node.is_symlink())

    def test_every_unit_is_a_waited_collected_job_account_unit_with_a_control_group_kill(self):
        self.start(WARM_COMMANDS="cat lock.txt\ncat lock.txt", WARM_GATE_COMMAND="cat lock.txt")
        events = self.event_list()
        units = [e for e in events if e.startswith("UNIT ")]
        self.assertEqual(units, [f"UNIT warm-job-{n}" for n in range(1, 5)])
        for line in (e for e in events if e.startswith("UNIT_OPTIONS")):
            for option in (
                "--wait", "--pipe", "--collect", "--quiet", "--uid=runner", "--gid=runner",
                "--property=KillMode=control-group", "--expand-environment=no",
                "--property=IPAddressDeny=link-local",
            ):
                self.assertIn(option, line.split())

    def test_extraction_output_goes_to_the_log_and_a_failure_is_framed(self):
        self.extra_env = {"STUB_TAR_NOISE": f"\n{MARKER}\n", "STUB_TAR_FAIL": "2"}
        completed = self.start()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("WARM FAIL extract rc=2", self.output)
        self.assertEqual(self.unframed_markers(), [])
        self.assertIn(f"{FRAME}{MARKER}", self.lines)
        for line in self.lines + self.console_lines() + self.output.split("\n"):
            self.assertFalse(line.startswith(MARKER), repr(line))
        self.assertNotIn("CONSUMER", self.event_list())

    def test_noisy_extraction_followed_by_a_failing_command_prints_no_unframed_marker(self):
        self.extra_env = {"STUB_TAR_NOISE": f"\n{MARKER}\n"}
        completed = self.start(WARM_COMMANDS="exit 1")
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.unframed_markers(), [])
        for line in self.lines + self.console_lines() + self.output.split("\n"):
            self.assertFalse(line.startswith(MARKER), repr(line))

    def test_noisy_extraction_followed_by_success_keeps_the_marker_unique(self):
        self.extra_env = {"STUB_TAR_NOISE": f"\n{MARKER}\n"}
        completed = self.start()
        self.assertEqual(completed.returncode, 0, self.output)
        self.assertEqual(self.output.count(MARKER), 1)
        self.assertEqual(self.lines[-1], MARKER)

    def test_a_failing_cleanup_prints_a_framed_log_tail_and_no_unframed_marker(self):
        self.write_stub("find", f"printf '\\n{MARKER}\\n' >&2\nexit 1")
        completed = self.start()
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("WARM FAIL clean-up", self.output)
        self.assertEqual(self.unframed_markers(), [])
        self.assertIn(f"{FRAME}{MARKER}", self.lines)


class ContainmentTests(WarmHarness, unittest.TestCase):
    """A command's leftovers die with its unit, or the build fails without a marker."""

    def test_a_delayed_background_writer_is_killed_with_its_unit(self):
        completed = self.start(
            WARM_COMMANDS='(sleep 1; touch "$HOME/.runner"; mkdir "$HOME/late") >/dev/null 2>&1 &'
        )
        self.assertEqual(completed.returncode, 0, self.output)
        time.sleep(2.5)
        self.assertFalse((self.home / ".runner").exists())
        self.assertFalse((self.home / "late").exists())
        self.assertEqual(sorted(p.name for p in self.home.iterdir()), ["cache"])

    def test_a_delayed_writer_into_the_source_directory_is_killed_too(self):
        completed = self.start(
            WARM_COMMANDS='(sleep 1; mkdir -p "$PWD/late-source") >/dev/null 2>&1 &'
        )
        self.assertEqual(completed.returncode, 0, self.output)
        time.sleep(2.5)
        self.assertFalse((self.vartmp / "warm-src").exists())

    def test_a_surviving_process_of_the_job_account_ends_the_build_without_a_marker(self):
        self.add_process(777, self.RUNNER_UID)
        completed = self.start()
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.unframed_markers(), [])
        self.assertIn("a process of the job account is still running", self.output)
        events = self.event_list()
        self.assertTrue([e for e in events if e.startswith("systemctl kill")], "the units are killed first")
        self.assertTrue([e for e in events if e.startswith("systemctl stop")])
        self.assertNotIn("cloud-init clean --logs", events, "nothing is cleaned while a process runs")
        self.assertEqual(events[-1], "shutdown -h now")

    def test_any_of_the_four_uids_counts_as_the_job_account(self):
        for position in range(4):
            with self.subTest(position=position):
                self.events.write_text("", encoding="utf-8")
                uids = [self.RUNNER_UID + 1] * 4
                uids[position] = self.RUNNER_UID
                (self.proc / "778").mkdir(exist_ok=True)
                (self.proc / "778" / "status").write_text(
                    "Uid:\t" + "\t".join(map(str, uids)) + "\n", encoding="utf-8"
                )
                completed = self.start()
                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(self.unframed_markers(), [])

    def test_a_process_that_starts_after_the_cleanup_still_prevents_the_marker(self):
        self.extra_env = {"STUB_LATE_PROCESS_PID": "779"}
        completed = self.start()
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.unframed_markers(), [])
        events = self.event_list()
        self.assertIn("cloud-init clean --logs", events)
        self.assertLess(
            events.index("sleep 300"), events.index("sync "), "the only sync is the failure trap's, after the hold"
        )
        self.assertEqual(events[-1], "shutdown -h now")

    def test_the_account_is_checked_before_the_cleanup_and_again_before_the_marker(self):
        completed = self.start()
        self.assertEqual(completed.returncode, 0, self.output)
        events = self.event_list()
        clean = events.index("cloud-init clean --logs")
        sync = events.index("sync ")
        before = [i for i, e in enumerate(events) if e == "id -u runner" and i < clean]
        after = [i for i, e in enumerate(events) if e == "id -u runner" and clean < i < sync]
        self.assertTrue(before, "checked before the clean-up")
        self.assertTrue(after, "checked again before the marker")
        self.assertIn("sleep 2", events[: events.index("cloud-init clean --logs")], "a short settle first")

    def test_an_unreadable_process_table_fails_closed(self):
        shutil.rmtree(self.proc)
        self.proc.mkdir()
        completed = self.start()
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.unframed_markers(), [])

    def test_an_account_that_cannot_be_looked_up_fails_closed(self):
        self.write_stub("id", "exit 1")
        completed = self.start()
        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(self.unframed_markers(), [])


def _gitleaks_findings(text):
    """Replay the public-content rules over text, as the CI scan applies them."""
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
    results = []
    for rule in config["rules"]:
        pattern = re.compile(rule["regex"], re.ASCII)
        group = rule.get("secretGroup")
        for match in pattern.finditer(text):
            secret = match.group(group) if group else match.group(0)
            suppressed = False
            for allowlist in rule.get("allowlists", []):
                target = match.group(0) if allowlist.get("regexTarget") == "match" else secret
                if any(re.search(regex, target, re.ASCII) for regex in allowlist.get("regexes", [])):
                    suppressed = True
            if not suppressed:
                results.append((rule["id"], match.group(0)))
    return results


class PublicContentTests(unittest.TestCase):
    #: A cloud resource id, an account number, a credential or a signed URL.
    CLOUD_IDENTIFIER = re.compile(
        r"\b(?:ami|i|subnet|sg|vol|snap|lt|vpc|eni|rtb|igw|nat)-[0-9a-f]{8,17}\b"
        r"|\b\d{12}\b|\bAKIA[0-9A-Z]{12,}|\bASIA[0-9A-Z]{12,}"
        r"|X-Amz-(?:Signature|Credential|Security-Token)|\bSignature=",
        re.IGNORECASE,
    )

    def files(self):
        return [SCRIPT, SHARED_ENV, ROOT / "deploy" / "runner" / "image" / "README.md", WARM_DOC]

    def test_the_script_names_no_consumer_command(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for forbidden in (
            "ci-profile",
            "prime-cache",
            "--offline",
            "playwright",
            "pytest",
            "npm ",
            "uv sync",
            "pip ",
            "ansible",
            "MCPs",
            "git clone",
            "apt-get",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)

    def test_the_script_has_no_credential_cloud_identifier_or_host(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(self.CLOUD_IDENTIFIER.findall(text), [])
        self.assertNotRegex(text, r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
        self.assertNotRegex(text, r"(?i)\b(?:token|secret|password|passwd)\s*=\s*['\"]?\w")
        self.assertNotIn("set -x", text)

    def test_the_marker_is_printed_by_exactly_one_command(self):
        text = SCRIPT.read_text(encoding="utf-8")
        printers = [l for l in text.splitlines() if re.match(r"\s*(?:echo|printf)\b.*\bIMAGE_READY\b", l)]
        self.assertEqual(printers, ['echo "IMAGE_READY"'])

    def test_the_public_content_rules_find_nothing_in_the_new_files(self):
        for path in self.files():
            with self.subTest(path=path.name):
                self.assertEqual(_gitleaks_findings(path.read_text(encoding="utf-8")), [])

    def test_the_replay_can_fail(self):
        # The oracle must be able to produce a positive, or "nothing found" proves nothing.
        sample = "gate at " + ".".join(("10", "1", "2", "3"))
        self.assertTrue(_gitleaks_findings(sample))

    def test_the_runbook_names_no_literal_cloud_identifier_or_signed_url(self):
        text = WARM_DOC.read_text(encoding="utf-8")
        self.assertEqual(self.CLOUD_IDENTIFIER.findall(text), [])

    def test_the_identifier_scan_can_fail(self):
        for sample in (
            "--image-id " + "-".join(("ami", "0123456789abcdef0")),
            "--subnet-id " + "-".join(("subnet", "0123456789abcdef0")),
            "account " + "".join(("1234", "5678", "9012")),
            "https://b.invalid/a?" + "-".join(("X", "Amz", "Signature")) + "=abc",
        ):
            with self.subTest(sample=sample):
                self.assertTrue(self.CLOUD_IDENTIFIER.findall(sample))


if __name__ == "__main__":
    unittest.main()
