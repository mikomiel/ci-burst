"""Tests for ``image promote`` and the edit behind it (tasks 6.2 to 6.6).

``promote`` is pure local: it reads the image state file and the deployment
file, and it builds no provider client. Every test here runs it through
``ci_burst.cli.main`` with client factories that raise, so a client built
anywhere fails the test. ``status --refresh`` is the one command that reads the
provider, and ``refresh`` below allows its client for that call alone.

The crash tests end the process the way a crash does. ``Crash`` is a
``BaseException`` that nothing in the code under test catches, raised at the
rename or just before the journal row is completed, so the locks and the
files are left as the real process would leave them. Every value is synthetic.
"""

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from ci_burst import image, image_deletion, image_pipeline, image_promote
from ci_burst.aws import BASIS_DIGEST_TAG
from ci_burst.config import load
from ci_burst.errors import ImageRefusal, StateRefusal
from ci_burst.image_state import ImageState

from support import Clock
from test_image_aws import tag_list
from test_image_cli import CliCase
from test_image_pipeline import (
    DAY,
    DEPLOYMENT,
    GATE,
    NO_GATE,
    NOW,
    PIN,
    READY,
    SCRIPT,
    SOURCE_SHA,
    WORKER,
    Crash,
    WorldAws,
    ami,
    owned,
    snapshot_tree,
)

BACKUP_NAME = re.compile(r"^deployment\.toml\.bak\.\d{8}T\d{6}Z$")
ZONE = "subnet-0123456789abcdef"
ZONES_ONE_PINNED = [
    {"zone": "eu-central-1a", "subnet_id": ZONE + "0", "launch_template_version": 4},
    {"zone": "eu-central-1b", "subnet_id": ZONE + "1"},
]
ZONES_FOLLOWING = [
    {"zone": "eu-central-1a", "subnet_id": ZONE + "0"},
    {"zone": "eu-central-1b", "subnet_id": ZONE + "1"},
]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def stamp(epoch):
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y%m%dT%H%M%SZ")


def changed_lines(before, after):
    """The ``(line number, old, new)`` of every line that differs; the line count must not change."""
    old, new = before.split(b"\n"), after.split(b"\n")
    assert len(old) == len(new), "the number of lines changed"
    return [
        (number, first.decode(), second.decode())
        for number, (first, second) in enumerate(zip(old, new, strict=True), start=1)
        if first != second
    ]



@contextlib.contextmanager
def _deadline(seconds):
    """Fail the test instead of hanging it when the body blocks for ``seconds``."""
    import signal

    def expired(signum, frame):
        raise AssertionError(f"blocked for {seconds} s")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


class PromoteCase(CliCase):
    """A deployment directory, a state directory and an account, with every client forbidden."""

    def setUp(self):
        super().setUp()
        self.forbid_clients()

    # -- the commands ----------------------------------------------------------------
    def show(self, *arguments):
        return self.invoke("image", "promote", *arguments)

    def write_pin(self, *arguments):
        return self.invoke("image", "promote", *arguments, "--write")

    def promoted(self, *arguments):
        code, payload = self.write_pin(*arguments)
        self.assertEqual((code, payload["decision"]), (0, "promoted"), payload)
        return payload

    def rolled_back(self):
        code, payload = self.write_pin("--rollback")
        self.assertEqual((code, payload["decision"]), (0, "rolled_back"), payload)
        return payload

    def refresh(self):
        """``image status --refresh`` with the account's clients, for that call alone."""
        self.allow_clients()
        try:
            code, payload = self.invoke("image", "status", "--refresh")
        finally:
            self.forbid_clients()
        self.assertEqual((code, payload["decision"]), (0, "refreshed"), payload)
        return payload

    def refusal(self, arguments, reason, *, write=True):
        """Run a command, expect the refusal ``reason`` with exit 2 and no change of any kind."""
        before = self.fingerprint()
        code, payload = (self.write_pin if write else self.show)(*arguments)
        self.assertEqual((code, payload.get("error", {}).get("reason")), (2, reason), payload)
        self.assertEqual(self.fingerprint(), before)
        return payload["error"]

    # -- the crashes -------------------------------------------------------------------
    @contextlib.contextmanager
    def rename_fails(self, error):
        """The rename of the temporary file over the deployment file raises ``error``; other renames work."""
        real = os.replace

        def replace(source, target, *arguments, **keywords):
            if str(source).endswith(".promote-tmp"):
                raise error
            return real(source, target, *arguments, **keywords)

        with mock.patch("os.replace", replace):
            yield

    @contextlib.contextmanager
    def flush_fails(self, error, *, directory, after=0):
        """``os.fsync`` raises ``error`` once ``after`` flushes of one kind have worked; every other flush works.

        A write flushes the temporary file, then the backup and, after the rename, the directory: ``directory``
        picks the kind of flush that fails, and ``after`` how many of that kind work first.
        """
        real = os.fsync
        worked = 0

        def fsync(descriptor):
            nonlocal worked
            if stat.S_ISDIR(os.fstat(descriptor).st_mode) == directory:
                worked += 1
                if worked > after:
                    raise error
            return real(descriptor)

        with mock.patch("os.fsync", fsync):
            yield

    @contextlib.contextmanager
    def completion_crashes(self):
        """The process dies after the rename, as the journal row is about to be marked complete."""
        with mock.patch.object(ImageState, "journal_complete", side_effect=Crash("after the rename")):
            yield

    def crash_before_rename(self, *arguments):
        with self.rename_fails(Crash("before the rename")), self.assertRaises(Crash):
            self.write_pin(*arguments)

    def crash_after_rename(self, *arguments):
        with self.completion_crashes(), self.assertRaises(Crash):
            self.write_pin(*arguments)

    # -- the files and the rows --------------------------------------------------------
    def fingerprint(self):
        """What a refused write leaves alone: the deployment directory (its lock file apart), the tables, the events."""
        files = {name: data for name, data in snapshot_tree(self.config_dir).items() if not name.endswith(".lock")}
        return files, self.dump(), self.promoted_events()

    def promoted_events(self):
        """The ``image_promoted`` events of the event file, oldest first. The passes write other events there."""
        return self.events("image_promoted")

    def backups(self):
        return sorted(self.config_dir.glob("deployment.toml.bak.*"))

    def temporary(self):
        return self.config_dir / "deployment.toml.promote-tmp"

    def lock_file(self):
        return self.config_dir / "deployment.toml.lock"

    def edit_file(self, old, new):
        """A hand edit of the deployment file: ``old`` becomes ``new``; ``old`` occurs exactly once."""
        data = self.config_path.read_bytes()
        self.assertEqual(data.count(old), 1, old)
        self.config_path.write_bytes(data.replace(old, new))

    def pin_line_number(self):
        for number, line in enumerate(self.config_path.read_text(encoding="utf-8").split("\n"), start=1):
            if line.startswith("launch_template_version"):
                return number
        raise AssertionError("no pin line")

    def huge_pin_in_the_backup(self):
        """The first backup with a pin longer than Python converts to an integer, and its hash recorded to match."""
        backup = self.backups()[0]
        data = backup.read_bytes().replace(b"launch_template_version = 3", b"launch_template_version = 1" + b"0" * 5000)
        backup.write_bytes(data)
        self.corrupt_journal(1, backup_sha256=sha(data))

    def corrupt_journal(self, journal_id, **fields):
        """A change of a journal row that only damage or a hand edit of the state file could make."""
        connection = sqlite3.connect(self.state_dir / "image-state.sqlite")
        try:
            assignments = ", ".join(f"{column} = ?" for column in fields)
            connection.execute(
                f"UPDATE promotion_journal SET {assignments} WHERE id = ?", (*fields.values(), journal_id)
            )
            connection.commit()
        finally:
            connection.close()

    def corrupt_build(self, build_id, **fields):
        """A change of a build row that only damage or a hand edit of the state file could make."""
        connection = sqlite3.connect(self.state_dir / "image-state.sqlite")
        try:
            assignments = ", ".join(f"{column} = ?" for column in fields)
            connection.execute(f"UPDATE image_builds SET {assignments} WHERE id = ?", (*fields.values(), build_id))
            connection.commit()
        finally:
            connection.close()

    def protected(self):
        """The images that no deletion path may take, with the rows that are pending now."""
        pending = self.query(lambda state: state.pending_journal())
        return image_deletion.protected_set(
            WorldAws(self.world),
            self.config_path,
            previous_pin=None,
            pending_journal=pending,
            candidate=None,
            active=None,
        )

    # -- the candidates ----------------------------------------------------------------
    def seed(self, build_id="build-b", version=5, number=0xB1, *, age=600, source_pin=PIN, **changes):
        """A verified candidate that the account also holds, with ``changes`` to its row."""
        self.add_world_candidate(build_id, version, number, age=age, source_pin=source_pin)
        if changes:
            self.change(build_id, **changes)
        return build_id

    def change(self, build_id, **fields):
        self.write(lambda state: state.advance_build(build_id, **fields))

    def good(self):
        """A record that shows everything a promotion needs, verified a minute ago."""
        return {
            "image_state": "available",
            "template_version_exists": 1,
            "drift_result": "clean",
            "gate_configured": 1,
            "gate_passed": 1,
            "verified_at": self.clock() - 60,
            "source_pin": PIN,
        }


# -- 6.2: promote without --write ------------------------------------------------------


class PreviewTests(PromoteCase):
    def test_it_prints_the_evidence_a_person_decides_with(self):
        build = self.build_candidate()
        self.clock.advance(120)
        code, payload = self.show()
        self.assertEqual((code, payload["decision"], payload["action"]), (0, "preview", "promote"))
        self.assertEqual(
            payload["evidence"],
            {
                "build_id": build.id,
                "source_sha": SOURCE_SHA[:12],
                "basis_digest": build.basis_digest[:12],
                "script_sha256": image.script_sha256(SCRIPT),
                "gate": {"configured": True, "passed": True},
                "build_duration_seconds": 390.0,
                "verified_at": build.verified_at,
                "verified_age_seconds": 120,
                "image_id": build.image_id,
                "image_state": "available",
                "template_version": 5,
                "template_version_exists": True,
                "drift_result": "clean",
                "source_pin": PIN,
            },
        )
        self.assertEqual(payload["write_check"], {"ok": True})

    def test_it_prints_the_one_line_that_would_change(self):
        self.seed()
        payload = self.show()[1]
        self.assertEqual(
            payload["pin_change"],
            {
                "line": self.pin_line_number(),
                "from": "launch_template_version = 3",
                "to": "launch_template_version = 5",
            },
        )

    def test_it_prints_the_previous_pin_and_says_there_is_no_rollback_before_a_first_promotion(self):
        build = self.build_candidate()
        payload = self.show()[1]
        self.assertEqual(
            payload["previous_pin"],
            {"version": PIN, "image_id": ami(0xA1), "recorded": False, "image_state": None, "verified_at": None},
        )
        self.assertEqual(payload["rollback"]["available"], False)
        self.assertEqual(payload["rollback"]["reason"], "ROLLBACK_NO_PROMOTION")
        self.assertEqual(payload["pending_journal"], [])
        self.assertEqual(payload["evidence"]["build_id"], build.id)

    def test_it_prints_the_previous_pin_and_the_rollback_target_after_a_promotion(self):
        build = self.build_candidate()
        self.refresh()
        self.promoted()
        self.seed("build-c", 6, 0xC1, age=0, source_pin=5)
        payload = self.show()[1]
        self.assertEqual(payload["evidence"]["build_id"], "build-c")
        self.assertEqual(payload["pin_change"]["from"], "launch_template_version = 5")
        previous = payload["previous_pin"]
        self.assertEqual(
            (previous["version"], previous["image_id"], previous["recorded"], previous["image_state"]),
            (5, build.image_id, True, "available"),
        )
        rollback = payload["rollback"]
        self.assertEqual(
            (rollback["available"], rollback["to_pin"], rollback["image_id"], rollback["image_state"]),
            (True, PIN, ami(0xA1), "available"),
        )
        self.assertRegex(rollback["backup"], BACKUP_NAME)

    def test_it_reads_the_two_files_and_nothing_else(self):
        self.seed()
        before = snapshot_tree(self.dir)
        calls = len(self.world.calls)
        for arguments in ((), ("--version", "5"), ("--rollback",)):
            with self.subTest(arguments=arguments):
                self.show(*arguments)  # the rollback refuses for want of a promotion: it writes nothing either
        self.assertEqual(snapshot_tree(self.dir), before)
        self.assertEqual(len(self.world.calls), calls)
        self.assertEqual((self.sessions, self.source_sessions), ([], 0))
        self.assertFalse(self.lock_file().exists())

    def test_a_missing_state_file_is_refused_and_not_created(self):
        self.assertFalse(self.state_dir.exists())
        self.assertEqual(self.show()[1]["error"]["reason"], "PROMOTE_NO_CANDIDATE")
        self.assertEqual(self.show("--rollback")[1]["error"]["reason"], "ROLLBACK_NO_PROMOTION")
        self.assertFalse(self.state_dir.exists())

    def test_the_default_is_the_newest_verified_candidate_and_version_selects_another(self):
        self.seed("build-b", 5, 0xB1, age=900)
        self.seed("build-c", 6, 0xC1, age=300)
        newest = self.show()[1]
        self.assertEqual((newest["evidence"]["build_id"], newest["pin_change"]["to"]), ("build-c", self.pin_to(6)))
        older = self.show("--version", "5")[1]
        self.assertEqual((older["evidence"]["build_id"], older["pin_change"]["to"]), ("build-b", self.pin_to(5)))

    @staticmethod
    def pin_to(version):
        return f"launch_template_version = {version}"

    def test_a_candidate_that_nobody_verified_is_never_the_default(self):
        self.seed("build-b", 5, 0xB1, age=900)
        self.write(
            lambda state: state.insert_orphan(
                build_id="orphan-1",
                deployment=DEPLOYMENT,
                basis_digest="d" * 64,
                source_sha="e" * 40,
                image_id=ami(0xE1),
                created_at=NOW - 10,
                reason_code="IMAGE_ORPHAN_RECORDED",
            )
        )
        self.assertEqual(self.show()[1]["evidence"]["build_id"], "build-b")

    def test_a_version_that_is_not_a_candidate_is_refused_even_without_write(self):
        self.seed()
        self.add_build("failed-one", "failed", age=DAY, template_version=4)
        self.build_pin_record()
        for label, version in (("no record", "99"), ("a failed build", "4"), ("the pin's record", str(PIN))):
            with self.subTest(case=label):
                error = self.refusal(("--version", version), "PROMOTE_VERSION_NOT_CANDIDATE", write=False)
                self.assertIn(f"version {version}", error["detail"])

    def build_pin_record(self):
        self.refresh()
        self.assertEqual(self.row(f"pin-{PIN}")["state"], "promoted")

    def test_the_write_check_says_what_a_write_would_refuse_and_the_exit_is_still_zero(self):
        self.seed()
        self.assertEqual(self.show()[1]["write_check"], {"ok": True})
        self.change("build-b", drift_result="TEMPLATE_DRIFT")
        code, payload = self.show()
        self.assertEqual(code, 0)
        self.assertEqual(payload["write_check"]["ok"], False)
        self.assertEqual(payload["write_check"]["reason"], "PROMOTE_DRIFT_NOT_CLEAN")
        self.assertEqual(payload["evidence"]["drift_result"], "TEMPLATE_DRIFT")

    def test_a_pending_row_is_reported_with_the_resolution_its_file_implies_and_is_left_pending(self):
        self.seed()
        self.crash_before_rename()
        before = self.dump()
        payload = self.show()[1]
        self.assertEqual(
            [(item["journal_id"], item["implied_resolution"]) for item in payload["pending_journal"]], [(1, "aborted")]
        )
        self.assertEqual(payload["write_check"], {"ok": True})
        self.edit_file(b"region = ", b"# hand edit\nregion = ")
        payload = self.show()[1]
        self.assertEqual(payload["pending_journal"][0]["implied_resolution"], "diverged")
        self.assertEqual(payload["write_check"]["reason"], "PROMOTION_JOURNAL_DIVERGED")
        self.assertEqual(self.dump(), before)
        self.assertEqual([row["status"] for row in self.journal_rows()], ["pending"])

    def test_a_rollback_preview_shows_the_line_and_the_target_and_writes_nothing(self):
        self.seed()
        self.refresh()
        self.promoted()
        before = snapshot_tree(self.dir)
        code, payload = self.show("--rollback")
        self.assertEqual((code, payload["decision"], payload["action"]), (0, "preview", "rollback"))
        self.assertEqual(payload["pin_change"]["from"], self.pin_to(5))
        self.assertEqual(payload["pin_change"]["to"], self.pin_to(PIN))
        self.assertEqual(payload["write_check"], {"ok": True})
        self.assertTrue(payload["rollback"]["available"])
        self.assertEqual(snapshot_tree(self.dir), before)

    def test_a_rollback_preview_reports_a_pin_line_that_the_edit_refuses_as_the_write_does(self):
        self.seed()
        self.refresh()
        self.promoted()
        # The strict loader reads 0x5 as 5, so the pin, the backup and the plan all hold: only the edit refuses.
        self.edit_file(b"launch_template_version = 5", b"launch_template_version = 0x5")
        self.assertEqual(load(self.config_path).aws.launch_template_version, 5)
        code, payload = self.show("--rollback")
        self.assertEqual((code, payload["action"], payload["rollback"]["available"]), (0, "rollback", True))
        self.assertIsNone(payload["pin_change"])
        self.assertEqual(payload["write_check"].get("reason"), "PROMOTE_PIN_LINE_AMBIGUOUS")
        error = self.refusal(("--rollback",), "PROMOTE_PIN_LINE_AMBIGUOUS")
        self.assertEqual(payload["write_check"]["reason"], error["reason"])

    def test_a_rollback_preview_reports_a_diverged_pending_row_as_the_write_refuses_it(self):
        self.seed()
        self.refresh()
        self.promoted()
        self.seed("build-c", 6, 0xC1, age=300, source_pin=5)
        self.clock.advance(60)
        self.crash_before_rename("--version", "6")  # row 2 stays pending; the file still holds the pin of row 1
        self.edit_file(b"region = ", b"# hand edit\nregion = ")  # a hash that is neither of row 2
        code, payload = self.show("--rollback")
        self.assertEqual((code, payload["action"], payload["rollback"]["available"]), (0, "rollback", True))
        self.assertEqual([item["implied_resolution"] for item in payload["pending_journal"]], ["diverged"])
        self.assertEqual(payload["write_check"].get("reason"), "PROMOTION_JOURNAL_DIVERGED")
        error = self.refusal(("--rollback",), "PROMOTION_JOURNAL_DIVERGED")
        self.assertEqual(payload["write_check"]["reason"], error["reason"])
        self.assertEqual([row["status"] for row in self.journal_rows()], ["complete", "pending"])

    def test_a_rollback_preview_names_the_refusal_that_the_write_reaches_first(self):
        self.seed()
        self.refresh()
        self.promoted()
        self.seed("build-c", 6, 0xC1, age=300, source_pin=5)
        self.clock.advance(60)
        self.crash_before_rename("--version", "6")
        promotion_backup = self.config_dir / self.journal_rows()[0]["backup_name"]
        # Refusals pile up, and the preview names the one that the write meets first: the directory, then a
        # diverged row, then the plan, then the edit.
        for label, damage, reason in (
            (
                "a diverged row, which the hand edit makes and which also breaks the pin line for the edit",
                lambda: self.edit_file(b"launch_template_version = 5", b"launch_template_version = 0x5"),
                "PROMOTION_JOURNAL_DIVERGED",
            ),
            (
                "a refused plan as well: the diverged row still comes first",
                promotion_backup.unlink,
                "PROMOTION_JOURNAL_DIVERGED",
            ),
            (
                "a foreign file as well: the directory comes first",
                lambda: (self.config_dir / "runner.env").write_text("synthetic\n", encoding="utf-8"),
                "PROMOTE_DIRECTORY_NOT_DEDICATED",
            ),
        ):
            with self.subTest(case=label):
                damage()
                payload = self.show("--rollback")[1]
                self.assertEqual(payload["write_check"].get("reason"), reason)
                self.assertEqual(self.refusal(("--rollback",), reason)["reason"], reason)
        self.assertEqual(payload["rollback"]["reason"], "PIN_RECORD_DIVERGED")

    def test_a_rollback_preview_names_a_refused_plan_before_a_refused_edit(self):
        self.seed()
        self.refresh()
        self.promoted()
        self.edit_file(b"launch_template_version = 5", b"launch_template_version = 0x5")
        (self.config_dir / self.journal_rows()[0]["backup_name"]).unlink()  # the plan refuses, and so does the edit
        payload = self.show("--rollback")[1]
        self.assertEqual(payload["rollback"]["reason"], "PIN_RECORD_DIVERGED")
        self.assertEqual(payload["write_check"].get("reason"), "PIN_RECORD_DIVERGED")
        self.refusal(("--rollback",), "PIN_RECORD_DIVERGED")

    # -- a pending row: the preview plans as the write does after it resolves the row ---------
    #
    # Each test below runs the preview first and then the write, and compares the two. The write resolves the row
    # for real, so what it does is the reference for what the preview must say.

    def show_changing_nothing(self, *arguments):
        """``image promote`` without ``--write``. It leaves every file, table and event as it was and takes no lock."""
        locks = (self.lock_file(), self.state_dir / "image-state.sqlite.lock")
        for lock in locks:
            lock.unlink(missing_ok=True)  # a lock file is made on demand, so one that is absent must stay absent
        tree, fingerprint = snapshot_tree(self.dir), self.fingerprint()
        result = self.show(*arguments)
        self.assertEqual(snapshot_tree(self.dir), tree)
        self.assertEqual(self.fingerprint(), fingerprint)
        self.assertFalse(any(lock.exists() for lock in locks))
        return result

    def crash_a_second_promotion(self, crash):
        """A to B completed, then B to C crashed (``crash`` is a crash helper): the row of B to C is pending."""
        self.seed()
        self.refresh()
        self.promoted()
        self.seed("build-c", 6, 0xC1, age=300, source_pin=5)
        self.clock.advance(60)
        crash("--version", "6")

    def test_a_rollback_preview_plans_the_rollback_of_a_promotion_that_the_write_completes_first(self):
        self.crash_a_second_promotion(self.crash_after_rename)
        self.assertEqual(self.pin_in_file(), 6)
        code, payload = self.show_changing_nothing("--rollback")
        self.assertEqual((code, payload["decision"], payload["action"]), (0, "preview", "rollback"))
        self.assertEqual(
            [(item["journal_id"], item["implied_resolution"]) for item in payload["pending_journal"]], [(2, "complete")]
        )
        self.assertEqual(payload["write_check"], {"ok": True})
        self.assertEqual(
            payload["pin_change"], {"line": self.pin_line_number(), "from": self.pin_to(6), "to": self.pin_to(5)}
        )
        self.assertEqual((payload["rollback"]["available"], payload["rollback"]["to_pin"]), (True, 5))
        code, written = self.write_pin("--rollback")
        self.assertEqual((code, written["decision"], written["to_pin"]), (0, "rolled_back", 5))
        self.assertEqual(written["resolved"], [{"journal_id": 2, "kind": "promotion", "resolution": "complete"}])
        self.assertEqual(written["change"], payload["pin_change"])
        self.assertEqual(self.pin_in_file(), 5)

    def test_a_backup_replaced_by_a_link_or_a_fifo_is_neither_followed_nor_waited_for(self):
        self.crash_a_second_promotion(self.crash_after_rename)
        backup = self.config_dir / self.journal_rows()[-1]["backup_name"]
        elsewhere = self.dir / "elsewhere"
        elsewhere.mkdir()
        original = elsewhere / "kept-backup"
        shutil.copyfile(backup, original)
        cases = {
            "a link to the same bytes": lambda: backup.symlink_to(original),
            "a FIFO with no writer": lambda: os.mkfifo(backup, 0o600),
        }
        for name, replace in cases.items():
            with self.subTest(name):
                backup.unlink()
                replace()
                with _deadline(20):  # a read of a FIFO with no writer would block here
                    code, payload = self.show("--rollback")
                self.assertEqual((code, payload["decision"]), (0, "preview"))
                rollback = payload["rollback"]
                self.assertEqual((rollback["available"], rollback["reason"]), (False, "PIN_RECORD_DIVERGED"))
                self.assertIn("regular file", rollback["detail"])
                self.assertFalse(payload["write_check"]["ok"])

    def test_a_rollback_preview_with_no_earlier_promotion_plans_the_rollback_that_the_write_makes_after_the_row(self):
        self.seed()
        self.refresh()
        self.crash_after_rename()  # the first promotion: the file holds its pin, and its row is pending
        code, payload = self.show_changing_nothing("--rollback")
        self.assertEqual((code, payload["decision"], payload["action"]), (0, "preview", "rollback"))
        self.assertEqual(payload["write_check"], {"ok": True})
        self.assertEqual(payload["pin_change"]["from"], self.pin_to(5))
        self.assertEqual(payload["pin_change"]["to"], self.pin_to(PIN))
        code, written = self.write_pin("--rollback")
        self.assertEqual((code, written["decision"], written["to_pin"]), (0, "rolled_back", PIN))
        self.assertEqual(written["change"], payload["pin_change"])

    def test_a_rollback_preview_with_no_earlier_promotion_names_the_refusal_that_the_write_meets_after_the_row(self):
        self.seed()
        self.crash_after_rename()  # no pass read the previous pin, so the rollback has no recorded target
        code, payload = self.show_changing_nothing("--rollback")
        self.assertEqual((code, payload["action"]), (0, "rollback"))
        self.assertEqual(payload["write_check"].get("reason"), "ROLLBACK_PREVIOUS_NOT_RECORDED")
        self.assertEqual(payload["rollback"]["reason"], "ROLLBACK_PREVIOUS_NOT_RECORDED")
        code, written = self.write_pin("--rollback")
        self.assertEqual((code, written["error"]["reason"]), (2, "ROLLBACK_PREVIOUS_NOT_RECORDED"))
        self.assertEqual(payload["write_check"], {"ok": False, **written["error"]})
        self.assertEqual([row["status"] for row in self.journal_rows()], ["complete"], "the resolution stays")

    def test_a_rollback_preview_plans_as_if_a_row_that_the_file_shows_aborted_were_aborted(self):
        self.crash_a_second_promotion(self.crash_before_rename)
        self.assertEqual(self.pin_in_file(), 5)
        code, payload = self.show_changing_nothing("--rollback")
        self.assertEqual((code, payload["decision"], payload["action"]), (0, "preview", "rollback"))
        self.assertEqual([item["implied_resolution"] for item in payload["pending_journal"]], ["aborted"])
        self.assertEqual(payload["write_check"], {"ok": True})
        self.assertEqual(payload["pin_change"]["from"], self.pin_to(5))
        self.assertEqual(payload["pin_change"]["to"], self.pin_to(PIN))
        code, written = self.write_pin("--rollback")
        self.assertEqual((code, written["decision"], written["to_pin"]), (0, "rolled_back", PIN))
        self.assertEqual(written["resolved"], [{"journal_id": 2, "kind": "promotion", "resolution": "aborted"}])
        self.assertEqual(written["change"], payload["pin_change"])

    def test_a_rollback_preview_with_nothing_to_roll_back_after_the_resolution_refuses_as_the_write_does(self):
        self.seed()
        self.crash_before_rename()  # the only row is aborted, so no promotion ever completed
        code, payload = self.show_changing_nothing("--rollback")
        self.assertEqual((code, payload["error"]["reason"]), (2, "ROLLBACK_NO_PROMOTION"))
        code, written = self.write_pin("--rollback")
        self.assertEqual((code, written["error"]), (2, payload["error"]))

    def test_a_promotion_preview_after_a_crash_past_the_rename_refuses_as_the_write_does(self):
        self.seed()
        self.crash_after_rename()  # the file holds build-b's pin, the row is pending and build-b is still a candidate
        previews = {}
        for arguments in ((), ("--version", "5")):
            code, payload = self.show_changing_nothing(*arguments)
            self.assertEqual(code, 2, payload)
            # The row on disk is still pending: the refused preview says so, with the resolution the hash implies.
            self.assertEqual([item["implied_resolution"] for item in payload["pending_journal"]], ["complete"])
            previews[arguments] = payload["error"]
        self.assertEqual(
            [error["reason"] for error in previews.values()], ["PROMOTE_NO_CANDIDATE", "PROMOTE_VERSION_NOT_CANDIDATE"]
        )
        # The first write resolves the row for real, and the second meets the journal that the previews planned against.
        for arguments, error in previews.items():
            with self.subTest(arguments=arguments):
                code, written = self.write_pin(*arguments)
                self.assertEqual((code, written["error"]), (2, error))
        self.assertEqual([row["status"] for row in self.journal_rows()], ["complete"])

    def test_a_preview_refuses_when_the_file_changes_between_its_read_and_the_copy_of_the_state(self):
        self.seed()
        copy = ImageState.detached_copy

        def copy_then_change_the_file(state):
            copied = copy(state)
            self.config_path.write_bytes(self.config_path.read_bytes() + b"# a promotion renamed the file here\n")
            return copied

        def copy_then_replace_the_file_with_the_same_bytes(state):
            # A promotion and then a rollback: two atomic replacements that end with the bytes the preview read.
            copied = copy(state)
            replacement = self.config_path.with_name("deployment.toml.replaced")
            replacement.write_bytes(self.config_path.read_bytes())
            os.replace(replacement, self.config_path)
            return copied

        def copy_then_replace_the_file_with_a_fifo(state):
            copied = copy(state)
            self.config_path.unlink()
            os.mkfifo(self.config_path, 0o600)
            return copied

        def copy_then_make_the_path_a_loop_of_links(state):
            copied = copy(state)
            self.config_path.unlink()
            loop = self.config_path.with_name("deployment.toml.loop")
            loop.symlink_to(self.config_path.name)
            self.config_path.symlink_to(loop.name)
            return copied

        def copy_then_write_a_file_that_does_not_load(state):
            copied = copy(state)
            self.config_path.write_bytes(self.config_path.read_bytes() + b'"x; an unknown key" = 1\n')
            return copied

        original = self.config_path.read_bytes()
        for change in (
            copy_then_change_the_file,
            copy_then_replace_the_file_with_the_same_bytes,
            copy_then_replace_the_file_with_a_fifo,
            copy_then_make_the_path_a_loop_of_links,
            copy_then_write_a_file_that_does_not_load,
        ):
            with self.subTest(change=change.__name__):
                self.config_path.with_name("deployment.toml.loop").unlink(missing_ok=True)
                self.config_path.unlink(missing_ok=True)
                self.config_path.write_bytes(original)
                with mock.patch.object(ImageState, "detached_copy", change), _deadline(10):
                    code, payload = self.show()
                self.assertEqual((code, payload["error"]["reason"]), (2, "PROMOTE_FILE_CHANGED"), payload)
                self.assertNotIn("unknown key", json.dumps(payload))
        self.config_path.with_name("deployment.toml.loop").unlink(missing_ok=True)
        self.config_path.unlink(missing_ok=True)
        self.config_path.write_bytes(original)
        descriptors = len(os.listdir("/proc/self/fd"))
        code, payload = self.show()  # with nothing changing, the next preview plans
        self.assertEqual((code, payload["decision"]), (0, "preview"), payload)
        self.assertEqual(len(os.listdir("/proc/self/fd")), descriptors, "the held file is closed")

    def test_evidence_that_a_refresh_writes_just_before_the_copy_is_not_in_the_future_of_the_preview(self):
        self.seed()
        copy = ImageState.detached_copy

        def refresh_then_copy(state):
            # ``image status --refresh`` commits fresh verification between the preview's file read and its copy.
            self.clock.advance(30)
            self.change("build-b", verified_at=self.clock.now)
            return copy(state)

        with mock.patch.object(ImageState, "detached_copy", refresh_then_copy):
            code, payload = self.show()
        self.assertEqual((code, payload["decision"]), (0, "preview"), payload)
        self.assertEqual(payload["write_check"], {"ok": True})
        code, written = self.write_pin()
        self.assertEqual((code, written["decision"]), (0, "promoted"), written)

    def test_a_damaged_completed_row_refuses_the_preview_and_the_write_alike_and_changes_nothing(self):
        self.crash_a_second_promotion(self.crash_before_rename)  # A to B completed; B to C is pending, then aborted
        for damage in ({"old_pin": "x; not a pin"}, {"kind": "x; not a kind"}):
            with self.subTest(damage=damage):
                self.check_a_damaged_completed_row(damage)

    def check_a_damaged_completed_row(self, damage):
        connection = sqlite3.connect(self.state_dir / "image-state.sqlite")
        try:
            connection.execute("PRAGMA ignore_check_constraints = ON")  # what a damaged file can hold
            connection.execute("UPDATE promotion_journal SET old_pin = 3, kind = 'promotion' WHERE id = 1")
            for column, value in damage.items():
                connection.execute(f"UPDATE promotion_journal SET {column} = ? WHERE id = 1", (value,))
            connection.commit()
        finally:
            connection.close()
        code, payload = self.show_changing_nothing("--version", "6")
        self.assertEqual((code, payload["error"]["reason"]), (2, "IMAGE_STATE_UNREADABLE"), payload)
        # The valid pending row is still reported, with the resolution that its hash implies.
        self.assertEqual(
            [(item["journal_id"], item["implied_resolution"]) for item in payload["pending_journal"]], [(2, "aborted")]
        )
        fingerprint = self.fingerprint()  # the files (the lock file apart), the rows and the events
        code, written = self.write_pin("--version", "6")
        self.assertEqual((code, written["error"]), (2, payload["error"]))
        self.assertNotIn("not a", json.dumps(written))
        self.assertNotIn("not a", json.dumps(payload))
        self.assertEqual(self.fingerprint(), fingerprint)

    def test_a_backup_whose_pin_cannot_be_converted_does_not_stop_a_promotion_preview(self):
        self.seed("build-b", 5, 0xB1, age=900)
        self.seed("build-c", 6, 0xC1, age=300)
        self.change("build-b", source_pin=4)
        self.crash_after_rename()  # the row of the promotion to 6 is pending; the preview completes it on a copy
        self.huge_pin_in_the_backup()
        code, payload = self.show_changing_nothing()
        self.assertEqual((code, payload["decision"], payload["action"]), (0, "preview", "promote"), payload)
        self.assertEqual((payload["rollback"]["available"], payload["rollback"]["reason"]), (False, "PIN_RECORD_DIVERGED"))

    def test_a_promotion_preview_picks_the_candidate_that_the_write_picks_after_the_resolution(self):
        self.seed("build-b", 5, 0xB1, age=900)
        self.seed("build-c", 6, 0xC1, age=300)
        self.change("build-b", source_pin=4)  # told apart from build-c by the pin that its record names
        self.crash_after_rename()  # the write picks build-c, the newest: the file holds 6 and the row is pending
        self.assertEqual(self.pin_in_file(), 6)
        code, payload = self.show_changing_nothing()
        self.assertEqual((code, payload["decision"], payload["action"]), (0, "preview", "promote"))
        self.assertEqual([item["implied_resolution"] for item in payload["pending_journal"]], ["complete"])
        self.assertEqual(payload["evidence"]["build_id"], "build-b", "build-c is promoted once the row is resolved")
        self.assertEqual(payload["pin_change"]["to"], self.pin_to(5))
        self.assertEqual(payload["write_check"]["reason"], "PIN_MOVED")
        self.assertIn("derived from version 4", payload["write_check"]["detail"])
        code, written = self.write_pin()
        self.assertEqual((code, {"ok": False, **written["error"]}), (2, payload["write_check"]))

    def test_a_preview_over_a_pending_row_takes_no_lock_and_waits_for_none(self):
        self.crash_a_second_promotion(self.crash_after_rename)
        expected = self.show("--rollback")
        self.assertEqual((expected[0], expected[1]["write_check"]), (0, {"ok": True}))
        holder = ImageState(self.state_dir, clock=self.clock)
        holder.connect()
        self.addCleanup(holder.close)
        holder.acquire_writer()
        with image_promote.file_lock(self.config_path.resolve()):
            self.assertEqual(self.show("--rollback"), expected)


# -- 6.3: what a write refuses ---------------------------------------------------------


class VerificationTests(PromoteCase):
    """One refusal per item of the record. ``--version 5`` selects the candidate whatever its record shows."""

    def setUp(self):
        super().setUp()
        self.seed()

    def refuses(self, reason, variants):
        for label, changes in variants:
            with self.subTest(case=label):
                self.change("build-b", **{**self.good(), **changes})
                self.refusal(("--version", "5"), reason)

    def test_an_image_that_is_not_recorded_available_is_refused(self):
        self.refuses(
            "PROMOTE_IMAGE_NOT_AVAILABLE",
            [
                ("missing", {"image_state": "missing"}),
                ("pending", {"image_state": "pending"}),
                ("never read", {"image_state": None}),
            ],
        )

    def test_a_version_that_is_not_recorded_as_existing_is_refused(self):
        self.refuses(
            "PROMOTE_VERSION_NOT_RECORDED",
            [("absent", {"template_version_exists": 0}), ("never read", {"template_version_exists": None})],
        )

    def test_a_drift_that_is_not_clean_is_refused(self):
        self.refuses(
            "PROMOTE_DRIFT_NOT_CLEAN",
            [("drift", {"drift_result": "TEMPLATE_DRIFT"}), ("never computed", {"drift_result": None})],
        )

    def test_a_gate_that_is_not_configured_and_passed_is_refused(self):
        self.refuses(
            "PROMOTE_GATE_NOT_PASSED",
            [
                ("failed", {"gate_passed": 0}),
                ("unknown", {"gate_passed": None}),
                ("not configured", {"gate_configured": 0, "gate_passed": 0}),
                ("nothing recorded", {"gate_configured": None, "gate_passed": None}),
            ],
        )

    def test_a_build_without_a_gate_is_accepted_only_when_the_file_allows_it_and_the_record_says_so(self):
        self.configure(self.image_toml(gate="allow_no_gate = true"))
        for label, changes in (
            ("a gate that ran and failed", {"gate_configured": 1, "gate_passed": 0}),
            ("a record that says nothing", {"gate_configured": None, "gate_passed": None}),
        ):
            with self.subTest(case=label):
                self.change("build-b", **{**self.good(), **changes})
                self.refusal(("--version", "5"), "PROMOTE_GATE_NOT_PASSED")
        self.change("build-b", **{**self.good(), "gate_configured": 0, "gate_passed": 0})
        payload = self.promoted("--version", "5")
        self.assertEqual(payload["evidence"]["gate"], {"configured": False, "passed": False})

    def test_a_record_that_is_too_old_or_has_no_time_is_stale(self):
        limit = self.config.image.promote_max_age_seconds
        now = self.clock()
        self.refuses(
            "IMAGE_VERIFICATION_STALE",
            [
                ("older than the limit", {"verified_at": now - limit - 1}),
                ("never verified", {"verified_at": None}),
                ("verified in the future", {"verified_at": now + 1}),
            ],
        )

    def test_a_record_exactly_at_the_limit_is_accepted(self):
        self.change("build-b", verified_at=self.clock() - self.config.image.promote_max_age_seconds)
        self.promoted()

    def test_a_pin_that_moved_since_the_record_was_made_is_refused(self):
        self.edit_file(b"launch_template_version = 3", b"launch_template_version = 4")
        error = self.refusal(("--version", "5"), "PIN_MOVED")
        self.assertIn("version 4", error["detail"])
        self.assertIn("version 3", error["detail"])

    def test_a_version_that_no_candidate_owns_is_refused(self):
        self.refusal(("--version", "99"), "PROMOTE_VERSION_NOT_CANDIDATE")

    def test_no_verified_candidate_is_refused(self):
        self.change("build-b", verified_at=None)
        self.refusal((), "PROMOTE_NO_CANDIDATE")

    def test_the_refusals_come_in_the_order_of_the_design(self):
        # Everything is wrong at once; each fix reveals the next refusal.
        everything = {
            "image_state": None,
            "template_version_exists": None,
            "drift_result": None,
            "gate_configured": None,
            "gate_passed": None,
            "verified_at": None,
            "source_pin": 4,
        }
        self.change("build-b", **everything)
        for reason, fix in (
            ("PROMOTE_IMAGE_NOT_AVAILABLE", {"image_state": "available"}),
            ("PROMOTE_VERSION_NOT_RECORDED", {"template_version_exists": 1}),
            ("PROMOTE_DRIFT_NOT_CLEAN", {"drift_result": "clean"}),
            ("PROMOTE_GATE_NOT_PASSED", {"gate_configured": 1, "gate_passed": 1}),
            ("IMAGE_VERIFICATION_STALE", {"verified_at": self.clock() - 60}),
            ("PIN_MOVED", {"source_pin": PIN}),
        ):
            with self.subTest(reason=reason):
                self.refusal(("--version", "5"), reason)
                self.change("build-b", **fix)
        self.promoted("--version", "5")

    def test_a_zone_with_its_own_version_is_refused_before_the_state_file_or_the_lock_is_touched(self):
        self.configure(zones=ZONES_ONE_PINNED)
        shutil.rmtree(self.state_dir)
        before = snapshot_tree(self.dir)
        code, payload = self.write_pin()
        self.assertEqual((code, payload["error"]["reason"]), (2, "PROMOTE_ZONE_PIN"))
        self.assertIn("entries 0 ", payload["error"]["detail"])
        self.assertEqual(snapshot_tree(self.dir), before)
        self.assertFalse(self.state_dir.exists())
        self.assertFalse(self.lock_file().exists())

    def test_the_zone_refusal_covers_a_rollback_as_well(self):
        self.configure(zones=ZONES_ONE_PINNED)
        self.refusal(("--rollback",), "PROMOTE_ZONE_PIN")

    def test_zones_that_follow_the_pin_move_with_it(self):
        self.configure(zones=ZONES_FOLLOWING)
        self.promoted("--version", "5")
        zones = load(self.config_path).aws.zones
        self.assertEqual([zone.launch_template_version for zone in zones], [5, 5])


class LaunchedGateTests(PromoteCase):
    """A write reads the gate evidence of the build that was launched, not the gate that the file holds when it ends.

    The builds here are real: a pass launches the builder under one deployment file, the file changes, and the pass
    that sees the marker ends the build.
    """

    def build_across_an_edit(self, launched, edited):
        """A build launched under the gate setting ``launched``, ended by a pass that reads the file as ``edited``."""
        self.configure(self.image_toml(gate=launched))
        self.world.console_plan = [(300.0, READY)]
        self.start_build()
        self.configure(self.image_toml(gate=edited))
        self.clock.advance(30)
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        return self.only_build()

    def test_a_build_launched_without_a_gate_is_refused_when_the_file_gains_a_gate_before_the_build_ends(self):
        build = self.build_across_an_edit(NO_GATE, GATE)
        self.assertEqual((build.gate_configured, build.gate_passed), (0, 0))
        code, payload = self.show()
        self.assertEqual(code, 0)
        self.assertEqual(payload["evidence"]["gate"], {"configured": False, "passed": False})
        self.assertEqual(payload["write_check"]["reason"], "PROMOTE_GATE_NOT_PASSED")
        error = self.refusal((), "PROMOTE_GATE_NOT_PASSED")
        self.assertEqual(self.pin_in_file(), PIN)
        self.assertEqual(self.backups(), [])
        self.assertEqual(error["detail"], "the record shows gate configured 0 and passed 0")

    def test_a_build_launched_with_a_gate_is_promoted_with_its_evidence_when_the_file_drops_the_gate(self):
        build = self.build_across_an_edit(GATE, NO_GATE)
        self.assertEqual((build.gate_configured, build.gate_passed), (1, 1))
        payload = self.promoted()
        self.assertEqual(payload["evidence"]["gate"], {"configured": True, "passed": True})
        self.assertEqual(self.pin_in_file(), build.template_version)

    def build_with_a_damaged_gate_record(self, value):
        """A build launched without a gate, whose row was damaged to ``value`` before the pass that ends it.

        The file gains a gate command before that pass, so the file is no witness either: a gate that is required
        but was not run is the case that the write must refuse.
        """
        self.configure(self.image_toml(gate=NO_GATE))
        self.world.console_plan = [(300.0, READY)]
        _, launched = self.start_build()
        self.assertEqual(launched.gate_configured, 0)
        self.corrupt_build(launched.id, gate_configured=value)
        self.configure(self.image_toml(gate=GATE))
        self.clock.advance(30)
        result = self.apply()
        self.assertEqual(result["decision"], "candidate", result)
        return self.only_build()

    def test_a_record_with_a_gate_value_other_than_one_is_refused_as_a_build_without_a_gate(self):
        # Only an exact recorded 1 shows that a gate ran. A damaged value of 2 would read as "true" under a cast.
        build = self.build_with_a_damaged_gate_record(2)
        self.assertEqual((build.gate_configured, build.gate_passed), (0, 0))
        error = self.refusal((), "PROMOTE_GATE_NOT_PASSED")
        self.assertEqual(error["detail"], "the record shows gate configured 0 and passed 0")
        self.assertEqual(self.pin_in_file(), PIN)
        self.assertEqual(self.backups(), [])

    def test_a_record_that_shows_the_gate_with_an_exact_one_is_still_promoted(self):
        # The control for the test above: the same flow, with the row left as the launch wrote it, under a file that
        # holds a gate command, promotes.
        self.configure(self.image_toml(gate=GATE))
        self.world.console_plan = [(300.0, READY)]
        _, launched = self.start_build()
        self.assertEqual(launched.gate_configured, 1)
        self.clock.advance(30)
        self.assertEqual(self.apply()["decision"], "candidate")
        build = self.only_build()
        self.assertEqual((build.gate_configured, build.gate_passed), (1, 1))
        self.promoted()
        self.assertEqual(self.pin_in_file(), build.template_version)


# -- 6.4: the edit ---------------------------------------------------------------------


class EditTests(PromoteCase):
    def test_a_promotion_changes_the_one_line_and_journals_it(self):
        build = self.build_candidate()
        before = self.config_path.read_bytes()
        self.clock.advance(30)
        payload = self.promoted()
        after = self.config_path.read_bytes()
        number = self.pin_line_number()
        change = (number, "launch_template_version = 3", "launch_template_version = 5")
        self.assertEqual(changed_lines(before, after), [change])
        self.assertEqual(payload["change"], {"line": change[0], "from": change[1], "to": change[2]})
        self.assertEqual((payload["resolved"], payload["old_pin"], payload["new_pin"]), ([], PIN, 5))
        (backup,) = self.backups()
        self.assertEqual(backup.name, f"deployment.toml.bak.{stamp(self.clock())}")
        self.assertEqual(backup.read_bytes(), before)
        (row,) = self.journal_rows()
        self.assertEqual(
            {key: row[key] for key in row if key not in ("created_at", "updated_at")},
            {
                "id": payload["journal_id"],
                "kind": "promotion",
                "status": "complete",
                "old_pin": PIN,
                "new_pin": 5,
                "old_file_sha256": sha(before),
                "new_file_sha256": sha(after),
                "candidate_id": build.id,
                "backup_name": backup.name,
                "backup_sha256": sha(before),
                "contained_pin": PIN,
            },
        )
        self.assertEqual(
            (payload["backup"], payload["backup_sha256"], payload["file_sha256_before"], payload["file_sha256_after"]),
            (backup.name, sha(before), sha(before), sha(after)),
        )
        self.assertEqual(self.row(build.id)["state"], "promoted")
        self.assertEqual(payload["evidence"]["build_id"], build.id)
        self.assertFalse(self.temporary().exists())

    def test_the_image_build_table_keeps_its_own_version(self):
        self.seed()
        self.promoted()
        text = self.config_path.read_text(encoding="utf-8")
        self.assertEqual(text.count("launch_template_version = 1"), 1)
        self.assertEqual(load(self.config_path).image.build.launch_template_version, 1)

    def test_an_active_build_does_not_get_in_the_way_of_a_promotion(self):
        self.seed()
        self.add_build("running", "building", age=100)
        self.promoted()
        self.assertEqual(self.row("build-b")["state"], "promoted")
        self.assertEqual(self.row("running")["state"], "building")

    def test_the_effects_come_in_the_order_of_the_design(self):
        self.seed()
        before = self.config_path.read_bytes()
        seen = {}
        real_replace, real_flush = os.replace, image_promote._flush_directory

        def replace(source, target, *arguments, **keywords):
            if str(source).endswith(".promote-tmp"):
                other = ImageState(self.state_dir, clock=self.clock)
                other.connect()
                with self.assertRaises(StateRefusal) as locked:
                    other.acquire_writer()
                other.close()
                with (
                    self.assertRaises(ImageRefusal) as busy,
                    image_promote.file_lock(self.config_path.resolve()),
                ):
                    pass
                seen["rename"] = {
                    "journal": self.table("promotion_journal"),
                    "target": self.config_path.read_bytes(),
                    "temporary": Path(source).read_bytes(),
                    "backups": [path.name for path in self.backups()],
                    "state_lock": locked.exception.reason,
                    "file_lock": busy.exception.reason,
                }
            return real_replace(source, target, *arguments, **keywords)

        def flush(directory):
            seen["flush"] = {
                "target": self.config_path.read_bytes(),
                "statuses": [row["status"] for row in self.table("promotion_journal")],
            }
            return real_flush(directory)

        with mock.patch("os.replace", replace), mock.patch.object(image_promote, "_flush_directory", flush):
            self.promoted()
        after = self.config_path.read_bytes()
        rename = seen["rename"]
        (pending,) = rename["journal"]
        self.assertEqual(pending["status"], "pending", "the row is committed before the rename")
        self.assertEqual((pending["old_file_sha256"], pending["new_file_sha256"]), (sha(before), sha(after)))
        self.assertEqual(rename["target"], before)
        self.assertEqual(rename["temporary"], after)
        self.assertEqual(rename["backups"], [pending["backup_name"]])
        self.assertEqual((rename["state_lock"], rename["file_lock"]), ("IMAGE_STATE_LOCKED", "PROMOTE_BUSY"))
        self.assertEqual(seen["flush"], {"target": after, "statuses": ["pending"]})
        self.assertEqual([row["status"] for row in self.journal_rows()], ["complete"])

    def test_both_locks_are_released_when_a_promotion_ends(self):
        self.seed()
        self.promoted()
        other = ImageState(self.state_dir, clock=self.clock)
        other.connect()
        other.acquire_writer()
        other.close()
        with image_promote.file_lock(self.config_path.resolve()):
            pass

    # -- the crashes ---------------------------------------------------------------------
    def test_a_crash_before_the_rename_leaves_the_old_file_and_the_next_command_aborts_the_row(self):
        self.seed()
        before = self.config_path.read_bytes()
        self.crash_before_rename()
        self.assertEqual(self.config_path.read_bytes(), before)
        (row,) = self.journal_rows()
        self.assertEqual((row["status"], row["old_file_sha256"]), ("pending", sha(before)))
        self.assertTrue(self.temporary().exists())
        (backup,) = self.backups()
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(self.row("build-b")["state"], "candidate")
        self.clock.advance(60)
        payload = self.promoted()
        self.assertEqual(
            payload["resolved"], [{"journal_id": row["id"], "kind": "promotion", "resolution": "aborted"}]
        )
        self.assertEqual([item["status"] for item in self.journal_rows()], ["aborted", "complete"])
        self.assertEqual(len(self.backups()), 2, "the backup of the aborted row is kept")
        self.assertEqual(backup.read_bytes(), before)
        self.assertFalse(self.temporary().exists())
        self.assertEqual(self.pin_in_file(), 5)

    def test_a_crash_after_the_rename_leaves_the_new_file_and_a_command_that_refuses_completes_the_row(self):
        self.seed()
        before = self.config_path.read_bytes()
        self.crash_after_rename()
        after = self.config_path.read_bytes()
        self.assertNotEqual(after, before)
        (row,) = self.journal_rows()
        self.assertEqual((row["status"], row["new_file_sha256"]), ("pending", sha(after)))
        self.assertEqual(self.row("build-b")["state"], "candidate")
        # The file already holds the new pin, so there is nothing left to promote: the command
        # refuses, and the resolution it made first stays.
        code, payload = self.write_pin()
        self.assertEqual((code, payload["error"]["reason"]), (2, "PROMOTE_NO_CANDIDATE"))
        self.assertEqual([item["status"] for item in self.journal_rows()], ["complete"])
        self.assertEqual(self.row("build-b")["state"], "promoted")
        self.assertEqual(self.config_path.read_bytes(), after)

    def test_run_apply_resolves_a_row_that_a_crash_left_behind(self):
        self.seed()
        self.crash_after_rename()
        pinned = self.digest()
        self.world.images[ami(0xB1)]["Tags"] = tag_list({**owned("build-b"), BASIS_DIGEST_TAG: pinned})
        self.allow_clients()
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual(code, 0, payload)
        self.assertEqual(
            payload["journal_resolved"], [{"journal_id": 1, "kind": "promotion", "resolution": "complete"}]
        )
        self.assertEqual(self.row("build-b")["state"], "promoted")
        self.assertEqual([item["status"] for item in self.journal_rows()], ["complete"])

    # -- a hot rollback journal (#194) ----------------------------------------------------
    def leave_a_hot_journal(self):
        """Leave the state file as a process that died inside the commit of ``journal_complete`` leaves it.

        A copy of the file runs that commit's update with a one-page cache, so SQLite writes changed pages into
        the file before the commit, and the file and its rollback journal are copied back mid-transaction. Only a
        writable connection rolls such a journal back. Returns the bytes of both files.
        """
        database = self.state_dir / "image-state.sqlite"
        journal = Path(str(database) + "-journal")
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch) / database.name
            shutil.copyfile(database, work)
            connection = sqlite3.connect(work, isolation_level=None)
            try:
                connection.execute("PRAGMA cache_size=1")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE promotion_journal SET status = 'complete' WHERE status = 'pending'")
                connection.execute("CREATE TABLE spill (x BLOB)")
                for _ in range(64):
                    connection.execute("INSERT INTO spill VALUES (zeroblob(4000))")
                shutil.copyfile(work, database)
                shutil.copyfile(str(work) + "-journal", journal)
            finally:
                connection.execute("ROLLBACK")
                connection.close()
        self.assertGreater(journal.stat().st_size, 0)
        return database.read_bytes(), journal.read_bytes()

    def hot_files(self):
        database = self.state_dir / "image-state.sqlite"
        journal = Path(str(database) + "-journal")
        return database.read_bytes(), (journal.read_bytes() if journal.exists() else None)

    def test_a_hot_journal_makes_status_and_the_previews_refuse_and_name_the_recovery_and_touches_nothing(self):
        self.seed()
        self.crash_after_rename()
        files = self.leave_a_hot_journal()
        before = snapshot_tree(self.dir)
        for arguments in (("status",), ("promote",), ("promote", "--rollback"), ("run",), ("plan",)):
            with self.subTest(command=arguments):
                if arguments[0] in ("run", "plan"):
                    self.allow_clients()
                code, payload = self.invoke("image", *arguments)
                self.forbid_clients()
                self.assertEqual((code, payload["error"]["reason"]), (2, "IMAGE_STATE_RECOVERY_NEEDED"), payload)
                self.assertIn("image run --apply", payload["error"]["detail"])
                self.assertEqual(self.hot_files(), files)
        self.assertEqual(snapshot_tree(self.dir), before)

    def test_promote_write_rolls_a_hot_journal_back_under_its_locks_and_resolves_the_row(self):
        self.seed()
        self.crash_after_rename()
        after = self.config_path.read_bytes()
        self.leave_a_hot_journal()
        code, payload = self.write_pin()
        # The journal rolled back to the pending row; the file holds the new pin, so the row completes and
        # nothing is left to promote.
        self.assertEqual((code, payload["error"]["reason"]), (2, "PROMOTE_NO_CANDIDATE"), payload)
        self.assertFalse(Path(str(self.state_dir / "image-state.sqlite") + "-journal").exists())
        self.assertEqual([item["status"] for item in self.journal_rows()], ["complete"])
        self.assertEqual(self.row("build-b")["state"], "promoted")
        self.assertEqual(self.config_path.read_bytes(), after)
        tables = {row["name"] for row in self.table("sqlite_master")}
        self.assertNotIn("spill", tables)

    def test_run_apply_rolls_a_hot_journal_back_and_resolves_the_row(self):
        self.seed()
        self.crash_after_rename()
        self.leave_a_hot_journal()
        pinned = self.digest()
        self.world.images[ami(0xB1)]["Tags"] = tag_list({**owned("build-b"), BASIS_DIGEST_TAG: pinned})
        self.allow_clients()
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual(code, 0, payload)
        self.assertEqual(
            payload["journal_resolved"], [{"journal_id": 1, "kind": "promotion", "resolution": "complete"}]
        )
        self.assertEqual(self.row("build-b")["state"], "promoted")

    def test_a_hot_journal_stays_while_another_process_holds_the_writer_lock(self):
        self.seed()
        self.crash_after_rename()
        files = self.leave_a_hot_journal()
        lock = os.open(str(self.state_dir / "image-state.sqlite") + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, lock)
        fcntl.flock(lock, fcntl.LOCK_EX)
        code, payload = self.write_pin()
        fcntl.flock(lock, fcntl.LOCK_UN)
        self.assertEqual((code, payload["error"]["reason"]), (2, "IMAGE_STATE_LOCKED"), payload)
        self.assertEqual(self.hot_files(), files)

    def test_a_pending_row_is_judged_from_the_file_as_it_is_under_the_lock(self):
        self.seed()
        self.crash_after_rename()  # the file holds the new pin and the row is pending
        after = self.config_path.read_bytes()
        (backup,) = self.backups()
        self.config_path.write_bytes(backup.read_bytes())  # the first read sees the old file
        real = image_promote.file_lock

        @contextlib.contextmanager
        def late_rename(target):
            with real(target):
                self.config_path.write_bytes(after)  # the rename of the other process lands now
                yield

        with mock.patch.object(image_promote, "file_lock", late_rename):
            code, payload = self.write_pin()
        self.assertEqual((code, payload["error"]["reason"]), (2, "PROMOTE_NO_CANDIDATE"))
        self.assertEqual([row["status"] for row in self.journal_rows()], ["complete"])
        self.assertEqual(self.row("build-b")["state"], "promoted")
        self.assertEqual(self.config_path.read_bytes(), after)

    def test_status_reports_each_crash_state_and_writes_no_row(self):
        self.seed()
        self.crash_before_rename()
        before = self.dump()
        implied = self.invoke("image", "status")[1]["pending_journal"][0]["implied_resolution"]
        self.assertEqual(implied, "aborted")
        # The same row, with the file that the rename would have left.
        row = self.journal_rows()[0]
        self.assertEqual(row["status"], "pending")
        self.config_path.write_bytes(self.temporary().read_bytes())
        implied = self.invoke("image", "status")[1]["pending_journal"][0]["implied_resolution"]
        self.assertEqual(implied, "complete")
        self.edit_file(b"region = ", b"# hand edit\nregion = ")
        implied = self.invoke("image", "status")[1]["pending_journal"][0]["implied_resolution"]
        self.assertEqual(implied, "diverged")
        after = self.dump()
        self.assertEqual(after["promotion_journal"], before["promotion_journal"])
        self.assertEqual(after["image_builds"], before["image_builds"])

    def test_a_hand_edit_while_a_row_is_pending_refuses_every_resolving_command_and_writes_nothing(self):
        self.seed()
        self.crash_before_rename()
        self.edit_file(b"region = ", b"# hand edit\nregion = ")
        edited = self.config_path.read_bytes()
        for arguments in (("--write",), ("--rollback", "--write")):
            with self.subTest(arguments=arguments):
                error = self.refusal(arguments[:-1], "PROMOTION_JOURNAL_DIVERGED")
                self.assertIn("row 1", error["detail"])
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["error"]["reason"]), (2, "PROMOTION_JOURNAL_DIVERGED"))
        self.assertEqual(self.config_path.read_bytes(), edited)
        self.assertEqual([row["status"] for row in self.journal_rows()], ["pending"])
        self.assertEqual((self.sessions, self.source_sessions), ([], 0))

    def test_restoring_the_file_from_its_backup_lets_the_next_command_resolve_the_row(self):
        self.seed()
        self.crash_before_rename()
        original = self.backups()[0].read_bytes()
        self.edit_file(b"region = ", b"# hand edit\nregion = ")
        self.refusal((), "PROMOTION_JOURNAL_DIVERGED")
        self.config_path.write_bytes(original)
        self.clock.advance(60)
        payload = self.promoted()
        self.assertEqual([item["resolution"] for item in payload["resolved"]], ["aborted"])

    def test_both_pins_of_a_pending_row_stay_protected_whatever_the_file_says(self):
        self.seed()
        self.crash_before_rename()
        protected = self.protected()
        self.assertIn(ami(0xA1), protected)
        self.assertIn(ami(0xB1), protected, "only the pending row names the new pin: the file still pins 3")
        self.edit_file(b"region = ", b"# hand edit\nregion = ")
        diverged = self.protected()
        self.assertIn(ami(0xA1), diverged)
        self.assertIn(ami(0xB1), diverged)

    def test_both_pins_stay_protected_after_a_rename_whose_row_is_still_pending(self):
        self.seed()
        self.crash_after_rename()
        self.assertEqual(self.pin_in_file(), 5)
        protected = self.protected()
        self.assertIn(ami(0xB1), protected)
        self.assertIn(ami(0xA1), protected, "only the pending row names the old pin: the file pins 5")

    def test_a_to_b_completes_b_to_c_aborts_and_a_rollback_goes_back_to_a_without_the_aborted_backup(self):
        original = self.config_path.read_bytes()
        self.seed("build-b", 5, 0xB1, age=900)
        self.refresh()
        self.promoted()
        at_b = self.config_path.read_bytes()
        self.seed("build-c", 6, 0xC1, age=300, source_pin=5)
        self.clock.advance(60)
        self.crash_before_rename("--version", "6")
        self.assertEqual(self.config_path.read_bytes(), at_b)
        first, second = self.journal_rows()
        self.assertEqual((first["status"], second["status"]), ("complete", "pending"))
        newer = self.config_dir / second["backup_name"]
        self.assertGreater(second["backup_name"], first["backup_name"], "the aborted row's backup is the newer one")
        self.assertEqual(newer.read_bytes(), at_b)
        self.clock.advance(60)
        payload = self.rolled_back()
        self.assertEqual(
            payload["resolved"], [{"journal_id": second["id"], "kind": "promotion", "resolution": "aborted"}]
        )
        self.assertEqual((payload["old_pin"], payload["new_pin"], payload["to_pin"]), (5, PIN, PIN))
        self.assertEqual(self.config_path.read_bytes(), original)
        rows = self.journal_rows()
        self.assertEqual(
            [(row["kind"], row["status"], row["old_pin"], row["new_pin"]) for row in rows],
            [("promotion", "complete", PIN, 5), ("promotion", "aborted", 5, 6), ("rollback", "complete", 5, PIN)],
        )
        self.assertEqual(newer.read_bytes(), at_b, "the backup of the aborted row is kept as it was")
        self.assertEqual(self.row("build-c")["state"], "candidate")

    # -- the single-key edit ---------------------------------------------------------------
    def test_comments_spacing_and_other_tables_survive_byte_for_byte(self):
        text = self.config_path.read_bytes()
        rich = (
            "# Kept by hand, café. The pin below is moved by image promote.\n".encode()
            + text.replace(
                b"launch_template_version = 3",
                b"launch_template_version   =   3    # the pin, 'single' and \"double\"",
            )
            + b"\n# a last comment without a final newline"
        )
        self.config_path.write_bytes(rich)
        self.seed()
        self.promoted()
        self.assertEqual(self.config_path.read_bytes(), rich.replace(b"=   3    # the pin", b"=   5    # the pin"))

    def test_a_comment_on_the_pin_line_stays_in_the_file_and_never_reaches_the_output(self):
        remark = "SENTINEL-198 see https://private.example.invalid/note"
        text = self.config_path.read_bytes()
        commented = text.replace(b"launch_template_version = 3", f"launch_template_version = 3  # {remark}".encode())
        self.assertNotEqual(commented, text)
        self.config_path.write_bytes(commented)
        self.seed()
        code, preview = self.show()
        self.assertEqual(code, 0, preview)
        line = self.pin_line_number()
        self.assertEqual(
            preview["pin_change"],
            {"line": line, "from": "launch_template_version = 3", "to": "launch_template_version = 5"},
        )
        self.assertNotIn("SENTINEL-198", json.dumps(preview))
        written = self.promoted()
        self.assertEqual(written["change"], preview["pin_change"])
        self.assertNotIn("SENTINEL-198", json.dumps(written))
        self.assertEqual(self.config_path.read_bytes(), commented.replace(b"= 3  # ", b"= 5  # "))

    def test_carriage_returns_survive(self):
        crlf = self.config_path.read_bytes().replace(b"\n", b"\r\n")
        self.config_path.write_bytes(crlf)
        self.seed()
        self.promoted()
        self.assertEqual(
            self.config_path.read_bytes(),
            crlf.replace(b"launch_template_version = 3\r\n", b"launch_template_version = 5\r\n"),
        )

    def test_the_mode_of_the_file_and_of_its_backup_is_preserved(self):
        # Not the default mode of a new file (0o644 under the usual umask), so a lost mode shows.
        os.chmod(self.config_path, 0o600)
        self.seed()
        self.promoted()
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        (backup,) = self.backups()
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_a_symbolic_link_to_the_file_is_followed_and_kept(self):
        link_directory = self.dir / "link"
        link_directory.mkdir()
        link = link_directory / "deployment.toml"
        link.symlink_to(self.config_path)
        self.seed()
        code, payload = self.invoke("image", "promote", "--write", "--config", str(link), config=False)
        self.assertEqual((code, payload["decision"]), (0, "promoted"))
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.pin_in_file(), 5)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual([path.name for path in link_directory.iterdir()], ["deployment.toml"])

    def test_a_link_that_leads_to_another_file_by_the_second_read_is_refused_before_anything_is_written(self):
        # The command checks and locks the file that the link led to at the first read. A link that was
        # moved since then must not make it write into another directory under that lock.
        self.seed()
        link_directory = self.dir / "link"
        link_directory.mkdir()
        link = link_directory / "deployment.toml"
        link.symlink_to(self.config_path)
        elsewhere = self.dir / "elsewhere"
        elsewhere.mkdir()
        moved = elsewhere / "deployment.toml"
        moved.write_bytes(self.config_path.read_bytes())  # the same bytes in a clean directory: only the place differs
        real = image_promote.file_lock

        @contextlib.contextmanager
        def moved_once_locked(target):
            with real(target):
                link.unlink()
                link.symlink_to(moved)  # the lock is held, the second read has not happened yet
                yield

        before = self.fingerprint(), snapshot_tree(elsewhere)
        with mock.patch.object(image_promote, "file_lock", moved_once_locked):
            code, payload = self.invoke("image", "promote", "--write", "--config", str(link), config=False)
        self.assertEqual((code, payload.get("error", {}).get("reason")), (2, "PROMOTE_FILE_CHANGED"), payload)
        for directory in (self.config_dir, elsewhere):
            self.assertNotIn(str(directory), payload["error"]["detail"])
        self.assertEqual((self.fingerprint(), snapshot_tree(elsewhere)), before)
        self.assertEqual([path.name for path in elsewhere.iterdir()], ["deployment.toml"], "nothing was added there")
        self.assertEqual(self.backups(), [])
        self.assertFalse(self.temporary().exists())
        self.assertEqual(self.journal_rows(), [])
        self.assertEqual(self.promoted_events(), [])
        self.assertEqual(self.row("build-b")["state"], "candidate")
        # Nothing stays locked: the same command now works on the file that the link leads to.
        code, payload = self.invoke("image", "promote", "--write", "--config", str(link), config=False)
        self.assertEqual((code, payload["decision"]), (0, "promoted"), payload)
        self.assertEqual(self.pin_in_file(), PIN, "the file of the first read was never the target")
        self.assertEqual(len(list(elsewhere.glob("deployment.toml.bak.*"))), 1)

    def test_a_backup_never_replaces_a_file_of_the_same_name(self):
        self.seed()
        taken = self.config_dir / f"deployment.toml.bak.{stamp(self.clock())}"
        taken.write_bytes(b"an earlier backup\n")
        payload = self.promoted()
        self.assertEqual(taken.read_bytes(), b"an earlier backup\n")
        self.assertEqual(payload["backup"], taken.name + "-1")

    def test_a_pin_line_that_is_not_a_plain_decimal_is_refused_and_nothing_is_written(self):
        self.seed()
        plain = self.config_path.read_bytes()
        for label, line in (
            ("hexadecimal", b"launch_template_version = 0x3"),
            ("octal", b"launch_template_version = 0o3"),
            ("binary", b"launch_template_version = 0b11"),
            ("a quoted key", b'"launch_template_version" = 3'),
        ):
            with self.subTest(case=label):
                # Each of these is the same pin to the loader, so only the edit can refuse it.
                self.config_path.write_bytes(plain.replace(b"launch_template_version = 3", line))
                self.assertEqual(load(self.config_path).aws.launch_template_version, PIN)
                error = self.refusal(("--version", "5"), "PROMOTE_PIN_LINE_AMBIGUOUS")
                self.assertIn("found 0", error["detail"])
                preview = self.show("--version", "5")[1]
                self.assertIsNone(preview["pin_change"])
                self.assertEqual(preview["write_check"]["reason"], "PROMOTE_PIN_LINE_AMBIGUOUS")
        self.config_path.write_bytes(plain)

    def test_a_pin_the_loader_refuses_leaves_the_file_and_the_backups_unchanged(self):
        self.seed("build-big", 10_001, 0xB2)
        self.refusal(("--version", "10001"), "PROMOTE_REPARSE_FAILED")
        self.assertEqual(self.backups(), [])
        self.assertFalse(self.temporary().exists())
        self.assertEqual(self.row("build-big")["state"], "candidate")

    def test_a_file_that_changes_after_it_was_read_is_never_replaced(self):
        self.seed()
        before = self.config_path.read_bytes()
        real = image_promote._create_backup

        def backup_then_edit(snapshot, now):
            path = real(snapshot, now)
            snapshot.target.write_bytes(snapshot.data + b"# edited by hand\n")
            return path

        with mock.patch.object(image_promote, "_create_backup", backup_then_edit):
            code, payload = self.write_pin()
        self.assertEqual((code, payload["error"]["reason"]), (2, "PROMOTE_FILE_CHANGED"))
        self.assertEqual(self.config_path.read_bytes(), before + b"# edited by hand\n")
        self.assertEqual(self.backups(), [])
        self.assertFalse(self.temporary().exists())
        self.assertEqual(self.journal_rows(), [])
        self.assertEqual(self.row("build-b")["state"], "candidate")

    def test_a_failed_rename_aborts_the_row_and_keeps_the_backup(self):
        self.seed()
        before = self.config_path.read_bytes()
        with self.rename_fails(OSError(errno.EXDEV, "cross-device link")):
            code, payload = self.write_pin()
        self.assertEqual((code, payload["error"]["reason"]), (2, "PROMOTE_WRITE_FAILED"))
        self.assertIn("EXDEV", payload["error"]["detail"])
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual([row["status"] for row in self.journal_rows()], ["aborted"])
        (backup,) = self.backups()
        self.assertEqual(backup.read_bytes(), before)
        self.assertFalse(self.temporary().exists())
        self.assertEqual(self.row("build-b")["state"], "candidate")
        self.clock.advance(60)
        self.promoted()

    def test_a_file_system_error_before_the_rename_is_refused_by_its_errno_and_leaves_nothing_behind(self):
        self.seed()
        before = self.fingerprint()
        for step, worked in (("the temporary file", 0), ("the backup", 1)):
            with self.subTest(step=step):
                error = OSError(errno.ENOSPC, "No space left on device", "/synthetic/deployment.toml.promote-tmp")
                with self.flush_fails(error, directory=False, after=worked):
                    code, payload = self.write_pin()
                self.assertEqual((code, payload.get("error", {}).get("reason")), (2, "PROMOTE_WRITE_FAILED"), payload)
                detail = payload["error"]["detail"]
                self.assertIn("ENOSPC", detail)
                for leak in ("No space", "/synthetic", "promote-tmp"):
                    self.assertNotIn(leak, detail, "the errno name only: no message of the error and no path")
                self.assertEqual(self.fingerprint(), before, "the file, the rows and the events are as they were")
                self.assertEqual(self.backups(), [], "no backup that no row names")
                self.assertFalse(self.temporary().exists())
        self.promoted()  # the same command works once the disk does

    def test_an_error_that_is_not_the_file_systems_is_not_mapped_and_the_cleanup_still_runs(self):
        self.seed()
        before = self.fingerprint()
        with self.flush_fails(RuntimeError("a defect"), directory=False, after=1):
            code, payload = self.write_pin()
        self.assertEqual((code, payload["error"]), (1, {"reason": "UNEXPECTED", "detail": "RuntimeError"}))
        self.assertEqual(self.fingerprint(), before)
        self.assertFalse(self.temporary().exists())

    def test_a_file_system_error_after_the_rename_leaves_the_row_pending_and_the_next_command_resolves_it(self):
        self.seed()
        before = self.config_path.read_bytes()
        error = OSError(errno.EIO, "Input/output error", "/synthetic/config")
        with self.flush_fails(error, directory=True):
            code, payload = self.write_pin()
        self.assertEqual((code, payload.get("error", {}).get("reason")), (2, "PROMOTE_WRITE_FAILED"), payload)
        detail = payload["error"]["detail"]
        self.assertIn("EIO", detail)
        self.assertIn("pin may have moved", detail)
        self.assertIn("pending", detail)
        for command in ("promote --write", "promote --rollback --write", "image run --apply"):
            self.assertIn(command, detail)
        for leak in ("Input/output", "/synthetic"):
            self.assertNotIn(leak, detail, "the errno name only: no message of the error and no path")
        after = self.config_path.read_bytes()
        self.assertNotEqual(after, before, "the rename had happened")
        (row,) = self.journal_rows()
        hashes = (row["status"], row["old_file_sha256"], row["new_file_sha256"])
        self.assertEqual(hashes, ("pending", sha(before), sha(after)))
        (backup,) = self.backups()
        self.assertEqual((backup.name, backup.read_bytes()), (row["backup_name"], before), "the row names the backup")
        self.assertFalse(self.temporary().exists())
        self.assertEqual(self.row("build-b")["state"], "candidate")
        self.assertEqual(self.promoted_events(), [], "the promotion is not complete, so no event")
        # The file holds the new pin, so the next command resolves the row from the hash and has nothing left to
        # promote: it refuses, and the resolution it made first stays.
        code, payload = self.write_pin()
        self.assertEqual((code, payload["error"]["reason"]), (2, "PROMOTE_NO_CANDIDATE"))
        self.assertEqual([item["status"] for item in self.journal_rows()], ["complete"])
        self.assertEqual(self.row("build-b")["state"], "promoted")
        self.assertEqual(self.config_path.read_bytes(), after)

    def test_another_promotion_holding_the_file_lock_is_refused_without_waiting(self):
        self.seed()
        with image_promote.file_lock(self.config_path.resolve()):
            self.refusal((), "PROMOTE_BUSY")
        self.promoted()

    def test_a_pass_holding_the_state_lock_is_refused_without_waiting(self):
        self.seed()
        other = ImageState(self.state_dir, clock=self.clock)
        other.connect()
        other.acquire_writer()
        self.addCleanup(other.close)
        self.refusal((), "IMAGE_STATE_LOCKED")
        other.close()
        self.promoted()


# -- 6.4: the pure parts of the edit ------------------------------------------------------

DEPLOYMENT_TEXT = (
    "# a comment\n"
    "[image.build]\n"
    "launch_template_version = 1\n"
    "\n"
    "[aws]\n"
    'region = "eu-central-1"\n'
    "launch_template_version = 3  # pinned\n"
    "\n"
    "[[aws.availability_zones]]\n"
    "launch_template_version = 9\n"
)


class PlanEditTests(unittest.TestCase):
    def test_only_the_digits_of_the_aws_pin_change(self):
        text, change = image_promote.plan_edit(DEPLOYMENT_TEXT, 7)
        self.assertEqual(text, DEPLOYMENT_TEXT.replace("= 3  # pinned", "= 7  # pinned"))
        self.assertEqual(
            change,
            {"line": 7, "from": "launch_template_version = 3", "to": "launch_template_version = 7"},
        )

    def test_the_other_tables_with_a_key_of_that_name_are_not_touched(self):
        text, _ = image_promote.plan_edit(DEPLOYMENT_TEXT, 7)
        self.assertEqual(text.count("launch_template_version = 1\n"), 1)
        self.assertEqual(text.count("launch_template_version = 9\n"), 1)

    def test_the_new_value_may_have_more_digits(self):
        text, change = image_promote.plan_edit(DEPLOYMENT_TEXT, 10_000)
        self.assertIn("launch_template_version = 10000  # pinned", text)
        self.assertEqual(change["to"], "launch_template_version = 10000")

    def test_carriage_returns_indentation_spacing_and_a_missing_final_newline_are_kept(self):
        original = "[ aws ] # the table\r\n  launch_template_version=3\r\nregion = 'x'"
        text, change = image_promote.plan_edit(original, 4)
        self.assertEqual(text, "[ aws ] # the table\r\n  launch_template_version=4\r\nregion = 'x'")
        self.assertEqual(change["from"], "  launch_template_version=3")

    def test_the_header_may_come_after_other_tables_and_before_a_sub_table(self):
        original = (
            "[worker]\nlabels = []\n[aws]\nlaunch_template_version = 3\n"
            "[aws.other]\nlaunch_template_version = 8\n"
        )
        text, _ = image_promote.plan_edit(original, 5)
        self.assertEqual(text, original.replace("= 3", "= 5"))

    def test_a_value_that_is_not_a_plain_decimal_is_not_found(self):
        for value in ("0x3", "0o3", "1_0", "+3", '"3"', "3.0", "true", "{ a = 1 }"):
            with self.subTest(value=value):
                with self.assertRaises(ImageRefusal) as raised:
                    image_promote.plan_edit(f"[aws]\nlaunch_template_version = {value}\n", 5)
                self.assertEqual(raised.exception.reason, "PROMOTE_PIN_LINE_AMBIGUOUS")
                self.assertIn("found 0", raised.exception.detail)

    def test_a_key_that_is_quoted_dotted_or_in_another_table_is_not_found(self):
        for label, text in (
            ("quoted", '[aws]\n"launch_template_version" = 3\n'),
            ("quoted header", '["aws"]\nlaunch_template_version = 3\n'),
            ("no aws table", "[image.build]\nlaunch_template_version = 3\n"),
            ("only a zone", "[aws]\nregion = 'x'\n[[aws.availability_zones]]\nlaunch_template_version = 3\n"),
            ("an inline table", "aws = { launch_template_version = 3 }\n"),
        ):
            with self.subTest(case=label):
                with self.assertRaises(ImageRefusal) as raised:
                    image_promote.plan_edit(text, 5)
                self.assertEqual(raised.exception.reason, "PROMOTE_PIN_LINE_AMBIGUOUS")

    def test_two_assignments_are_refused(self):
        # The strict loader cannot produce this (TOML refuses a duplicate key), so it is tested here.
        twice = "[aws]\nlaunch_template_version = 3\nlaunch_template_version = 3\n"
        with self.assertRaises(ImageRefusal) as raised:
            image_promote.plan_edit(twice, 5)
        self.assertEqual(raised.exception.reason, "PROMOTE_PIN_LINE_AMBIGUOUS")
        self.assertIn("found 2", raised.exception.detail)


class ReparseTests(PromoteCase):
    def snapshot(self):
        return image_promote.read_snapshot(self.config)

    def test_a_text_that_differs_in_the_pin_alone_passes(self):
        snapshot = self.snapshot()
        text, _ = image_promote.plan_edit(snapshot.text, 5)
        self.assertIsNone(image_promote.check_reparse(snapshot, text, 5))

    def test_a_text_that_differs_in_anything_else_is_refused(self):
        snapshot = self.snapshot()
        text, _ = image_promote.plan_edit(snapshot.text, 5)
        for label, other in (
            ("another key of the table", text.replace("max_lifetime_seconds = 3600", "max_lifetime_seconds = 3601")),
            ("another table", text.replace("window_seconds = 86400", "window_seconds = 86401")),
            ("the image table", text.replace("lifetime_seconds = 3000", "lifetime_seconds = 3001")),
            ("a different pin", text.replace("launch_template_version = 5", "launch_template_version = 6")),
        ):
            with self.subTest(case=label):
                self.assertNotEqual(other, text)
                with self.assertRaises(ImageRefusal) as raised:
                    image_promote.check_reparse(snapshot, other, 5)
                self.assertEqual(raised.exception.reason, "PROMOTE_REPARSE_FAILED")

    def test_a_text_that_is_not_toml_or_that_the_loader_refuses_is_refused(self):
        snapshot = self.snapshot()
        text, _ = image_promote.plan_edit(snapshot.text, 5)
        for label, other in (
            ("not toml", text + "= =\n"),
            ("an unknown key", text.replace("[aws]\n", "[aws]\nunknown_key = 1\n")),
        ):
            with self.subTest(case=label):
                with self.assertRaises(ImageRefusal) as raised:
                    image_promote.check_reparse(snapshot, other, 5)
                self.assertEqual(raised.exception.reason, "PROMOTE_REPARSE_FAILED")
                self.assertIn("the strict loader refused the new text: CONFIG_INVALID", raised.exception.detail)

    def test_a_pin_outside_the_range_is_refused(self):
        snapshot = self.snapshot()
        text, _ = image_promote.plan_edit(snapshot.text, 10_001)
        with self.assertRaises(ImageRefusal) as raised:
            image_promote.check_reparse(snapshot, text, 10_001)
        self.assertEqual(raised.exception.reason, "PROMOTE_REPARSE_FAILED")


class StaleViewTests(PromoteCase):
    """A container that mounted the file itself keeps the inode that a rename elsewhere replaced."""

    def setUp(self):
        super().setUp()
        self.seed()

    def test_an_open_file_that_a_rename_replaced_is_refused_as_stale(self):
        target = Path(self.config.path).resolve()
        descriptor = os.open(target, os.O_RDONLY)
        try:
            replacement = target.parent / "replacement.tmp"
            replacement.write_bytes(target.read_bytes())
            os.replace(replacement, target)
            with self.assertRaises(ImageRefusal) as raised:
                image_promote._read_open_file(self.config, target, descriptor)
        finally:
            os.close(descriptor)
        self.assertEqual(raised.exception.reason, "PROMOTE_FILE_STALE")

    def test_a_rename_during_the_read_is_refused_as_stale(self):
        # The descriptor was checked by nothing before the read: a replacement between the open and the read
        # leaves old bytes, and the check after the read sees that the open file lost its name.
        target = Path(self.config.path).resolve()
        replacement = target.parent / "replacement.tmp"
        replacement.write_bytes(target.read_bytes())
        descriptor = os.open(target, os.O_RDONLY)
        real = os.read
        renamed = []

        def read(fd, size):
            if not renamed:
                renamed.append(True)
                os.replace(replacement, target)
            return real(fd, size)

        try:
            with mock.patch("os.read", read), self.assertRaises(ImageRefusal) as raised:
                image_promote._read_open_file(self.config, target, descriptor)
        finally:
            os.close(descriptor)
        self.assertEqual(renamed, [True])
        self.assertEqual(raised.exception.reason, "PROMOTE_FILE_STALE")

    @contextlib.contextmanager
    def replaced_during_the_read(self):
        """The first read of the deployment file renames a copy over it, while a hard link keeps the old file named.

        The old file keeps a link, so a check of the link count alone sees nothing (#202). The yield gives the
        target; the old file is also named ``kept.toml`` outside the deployment directory.
        """
        target = Path(self.config.path).resolve()
        os.link(target, self.dir / "kept.toml")
        replacement = target.parent / "replacement.tmp"
        inode = os.stat(target).st_ino
        real = os.read
        renamed = []

        def read(descriptor, size):
            if not renamed and os.fstat(descriptor).st_ino == inode:
                renamed.append(True)
                replacement.write_bytes(target.read_bytes())
                os.replace(replacement, target)
            return real(descriptor, size)

        with mock.patch("os.read", read):
            yield target
        self.assertEqual(renamed, [True])

    def test_a_replacement_during_the_read_that_a_hard_link_survives_is_refused_as_stale(self):
        with self.replaced_during_the_read() as target:
            with self.assertRaises(ImageRefusal) as raised:
                image_promote.read_snapshot(self.config)
        self.assertEqual(raised.exception.reason, "PROMOTE_FILE_STALE")
        self.assertEqual((self.dir / "kept.toml").read_bytes(), target.read_bytes())

    def test_a_promote_preview_and_write_are_refused_when_a_hard_link_survives_the_replacement(self):
        for write in (False, True):
            with self.subTest(write=write), self.replaced_during_the_read():
                self.refusal((), "PROMOTE_FILE_STALE", write=write)
            (self.dir / "kept.toml").unlink()
        self.assertEqual(self.backups(), [])

    def test_still_named_is_true_for_the_file_the_path_names_and_false_for_any_other(self):
        target = Path(self.config.path).resolve()
        descriptor = os.open(target, os.O_RDONLY)
        try:
            self.assertTrue(image_promote.still_named(descriptor, target))
            kept = self.dir / "kept.toml"
            os.link(target, kept)
            self.assertTrue(image_promote.still_named(descriptor, target))
            replacement = target.parent / "replacement.tmp"
            replacement.write_bytes(target.read_bytes())
            os.replace(replacement, target)
            self.assertEqual(os.fstat(descriptor).st_nlink, 1)
            self.assertFalse(image_promote.still_named(descriptor, target))
            kept.unlink()
            self.assertFalse(image_promote.still_named(descriptor, target))
            target.unlink()
            self.assertFalse(image_promote.still_named(descriptor, target))
        finally:
            os.close(descriptor)

    def test_the_same_file_still_linked_is_read(self):
        # The control: nothing but the rename separates this read from the one above.
        target = Path(self.config.path).resolve()
        descriptor = os.open(target, os.O_RDONLY)
        try:
            snapshot = image_promote._read_open_file(self.config, target, descriptor)
        finally:
            os.close(descriptor)
        self.assertEqual(snapshot.data, target.read_bytes())

    @contextlib.contextmanager
    def stale_view(self):
        """Every ``fstat`` of the deployment file reports no link left, as the stale mount does."""
        real = os.fstat
        inode = os.stat(Path(self.config.path).resolve()).st_ino

        def fstat(descriptor):
            result = real(descriptor)
            if result.st_ino != inode:
                return result
            values = list(result[:10])
            values[stat.ST_NLINK] = 0
            return os.stat_result(values)

        with mock.patch("os.fstat", fstat):
            yield

    def test_promote_write_through_a_stale_view_is_refused_and_nothing_is_written(self):
        with self.stale_view():
            self.refusal((), "PROMOTE_FILE_STALE")
        self.assertEqual(self.backups(), [])

    def test_a_rollback_and_a_preview_through_a_stale_view_are_refused(self):
        with self.stale_view():
            self.refusal(("--rollback",), "PROMOTE_FILE_STALE")
            self.refusal((), "PROMOTE_FILE_STALE", write=False)


class StateOwnerTests(PromoteCase):
    """The state and the events that a command opened belong to the file that it plans from (#199)."""

    def setUp(self):
        super().setUp()
        self.seed()
        self.other_state = self.dir / "other-state"

    def moved_state(self, text):
        return text.replace(str(self.state_dir), str(self.other_state))

    def renamed_deployment(self, text):
        renamed = re.sub(r'(?m)^deployment = "[^"]*"$', 'deployment = "renamed-deployment"', text, count=1)
        self.assertNotEqual(renamed, text)
        return renamed

    @contextlib.contextmanager
    def changed_before_the_snapshot(self, change):
        """The file changes after the command line loaded it and before ``promote`` reads it."""
        real = image_promote.read_snapshot
        calls = []

        def read_snapshot(config, **keywords):
            if not calls:
                self.config_path.write_text(change(self.config_path.read_text(encoding="utf-8")), encoding="utf-8")
            calls.append(True)
            return real(config, **keywords)

        with mock.patch.object(image_promote, "read_snapshot", read_snapshot):
            yield calls

    def untouched(self):
        """The tables, the events and the names in the deployment directory; the test itself edits the file."""
        names = sorted(name for name in snapshot_tree(self.config_dir) if not name.endswith(".lock"))
        return self.dump(), self.promoted_events(), names

    def refused(self, arguments, *, write, change):
        original = self.config_path.read_bytes()
        try:
            before = self.untouched()
            with self.changed_before_the_snapshot(change) as calls:
                code, payload = (self.write_pin if write else self.show)(*arguments)
            self.assertEqual(calls, [True], "refused at the first read")
            self.assertEqual((code, payload.get("error", {}).get("reason")), (2, "PROMOTE_STATE_CHANGED"), payload)
            self.assertEqual(self.untouched(), before)
            for path in (self.state_dir, self.other_state, self.config_dir):
                self.assertNotIn(str(path), json.dumps(payload))
            self.assertFalse(self.other_state.exists(), "the state the file names now is not created")
            self.assertEqual(self.backups(), [])
            self.assertEqual(self.journal_rows(), [])
        finally:
            self.config_path.write_bytes(original)

    def test_a_state_directory_changed_before_the_snapshot_is_refused_by_every_form(self):
        for arguments, write in (((), False), ((), True), (("--rollback",), True), (("--rollback",), False)):
            with self.subTest(arguments=arguments, write=write):
                self.refused(arguments, write=write, change=self.moved_state)

    def test_a_deployment_renamed_before_the_snapshot_is_refused(self):
        for write in (False, True):
            with self.subTest(write=write):
                self.refused((), write=write, change=self.renamed_deployment)

    def refused_under_the_locks(self, change, *arguments):
        """The first read passes; ``change`` runs once the write holds the lock of the file, before it takes the
        writer lock of the state and reads the file again."""
        real = image_promote.file_lock

        @contextlib.contextmanager
        def changed_once_locked(target):
            with real(target):
                change()
                yield

        before = self.untouched()
        with mock.patch.object(image_promote, "file_lock", changed_once_locked):
            code, payload = self.write_pin(*arguments)
        self.assertEqual((code, payload.get("error", {}).get("reason")), (2, "PROMOTE_STATE_CHANGED"), payload)
        self.assertEqual(self.untouched(), before)
        self.assertEqual(self.journal_rows(), [])
        self.assertEqual(self.backups(), [])
        self.assertFalse(self.other_state.exists())
        for path in (self.state_dir, self.other_state, self.config_dir):
            self.assertNotIn(str(path), json.dumps(payload))

    def edit(self, change):
        self.config_path.write_text(change(self.config_path.read_text(encoding="utf-8")), encoding="utf-8")

    def test_a_state_directory_changed_under_the_locks_is_refused_and_nothing_is_written(self):
        for arguments in ((), ("--rollback",)):
            with self.subTest(arguments=arguments):
                original = self.config_path.read_bytes()
                self.refused_under_the_locks(lambda: self.edit(self.moved_state), *arguments)
                self.config_path.write_bytes(original)

    def test_a_deployment_renamed_under_the_locks_is_refused(self):
        self.refused_under_the_locks(lambda: self.edit(self.renamed_deployment))

    def test_under_the_locks_the_state_refusal_comes_before_a_zone_pin(self):
        # The same order as at the first read: the owner of the state first, then the zones.
        def pin_a_zone_and_move_the_state():
            self.write_deployment(zones=ZONES_ONE_PINNED)
            self.edit(self.moved_state)

        self.refused_under_the_locks(pin_a_zone_and_move_the_state)

    def test_a_refusal_under_the_locks_leaves_a_hot_journal_of_the_old_state_as_it_is(self):
        # The writable open rolls a hot journal back, so the file is checked before the state is opened for writing.
        EditTests.leave_a_hot_journal(self)
        before = EditTests.hot_files(self)
        real = image_promote.file_lock

        @contextlib.contextmanager
        def changed_once_locked(target):
            with real(target):
                self.edit(self.moved_state)
                yield

        with mock.patch.object(image_promote, "file_lock", changed_once_locked):
            code, payload = self.write_pin()
        self.assertEqual((code, payload.get("error", {}).get("reason")), (2, "PROMOTE_STATE_CHANGED"), payload)
        self.assertEqual(EditTests.hot_files(self), before, "the database and its journal are byte for byte the same")
        self.assertEqual(self.backups(), [])
        self.assertFalse(self.other_state.exists())

    def test_a_link_to_the_same_directory_named_after_the_start_is_a_change(self):
        # The values are compared as written: a link is not followed, so another name for the same directory is
        # refused too, and a rerun plans from the file as it is.
        alias = self.dir / "state-alias"
        alias.symlink_to(self.state_dir, target_is_directory=True)
        self.refused((), write=False, change=lambda text: text.replace(str(self.state_dir), str(alias)))


class LateFileChangeTests(PromoteCase):
    """The write checks the path and the file again right before it resolves rows and right before the rename."""

    def setUp(self):
        super().setUp()
        self.seed()

    def test_a_config_link_moved_while_the_backup_is_written_is_refused(self):
        link_directory = self.dir / "link"
        link_directory.mkdir()
        link = link_directory / "deployment.toml"
        link.symlink_to(self.config_path)
        elsewhere = self.dir / "elsewhere"
        elsewhere.mkdir()
        moved = elsewhere / "deployment.toml"
        moved.write_bytes(self.config_path.read_bytes())
        real = image_promote._create_backup

        def moved_during_the_backup(*arguments, **keywords):
            backup = real(*arguments, **keywords)
            link.unlink()
            link.symlink_to(moved)
            return backup

        original, dump = self.config_path.read_bytes(), self.dump()
        with mock.patch.object(image_promote, "_create_backup", moved_during_the_backup):
            code, payload = self.invoke("image", "promote", "--write", "--config", str(link), config=False)
        self.assertEqual((code, payload.get("error", {}).get("reason")), (2, "PROMOTE_FILE_CHANGED"), payload)
        self.assertEqual(self.config_path.read_bytes(), original, "the file the link left keeps its pin")
        self.assertEqual(moved.read_bytes(), original)
        self.assertEqual(self.dump(), dump)
        self.assertEqual(self.backups(), [])
        self.assertFalse(self.temporary().exists())

    def test_an_edit_after_the_locked_read_leaves_a_pending_row_pending(self):
        self.crash_before_rename()
        self.assertEqual([row["status"] for row in self.journal_rows()], ["pending"])
        real = ImageState.journal_check

        def edited_after_the_read(state):
            result = real(state)
            self.edit_file(b"region = ", b"# hand edit\nregion = ")
            return result

        with mock.patch.object(ImageState, "journal_check", edited_after_the_read):
            code, payload = self.write_pin()
        self.assertEqual((code, payload.get("error", {}).get("reason")), (2, "PROMOTE_FILE_CHANGED"), payload)
        self.assertEqual([row["status"] for row in self.journal_rows()], ["pending"], "not resolved from a stale hash")


class FreshnessTests(unittest.TestCase):
    LIMIT = 3600

    def test_a_record_up_to_the_limit_is_accepted(self):
        for age in (0, 1, self.LIMIT - 1, self.LIMIT):
            with self.subTest(age=age):
                self.assertIsNone(image_promote.check_fresh(1000.0 - age, 1000.0, self.LIMIT))

    def test_a_record_past_the_limit_has_no_time_or_lies_in_the_future_is_stale(self):
        for label, verified_at in (("past the limit", 1000.0 - self.LIMIT - 1), ("no time", None), ("future", 1001.0)):
            with self.subTest(case=label):
                with self.assertRaises(ImageRefusal) as raised:
                    image_promote.check_fresh(verified_at, 1000.0, self.LIMIT)
                self.assertEqual(raised.exception.reason, "IMAGE_VERIFICATION_STALE")


class ResolutionTests(unittest.TestCase):
    """``resolve_pending`` decides each row from the hash of the file, or refuses and writes nothing."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.clock = Clock(NOW)
        self.state = ImageState(Path(directory.name) / "state", clock=self.clock).connect()
        self.addCleanup(self.state.close)

    def candidate(self, build_id):
        self.state.insert_build(
            build_id=build_id,
            deployment=DEPLOYMENT,
            client_token="token-" + build_id,
            basis_digest="a" * 64,
            source_sha="b" * 40,
            deadline_at=NOW + 100,
        )
        self.state.advance_build(build_id, state="candidate")

    def row(self, old, new, *, candidate_id=None, kind="promotion"):
        return self.state.journal_insert(
            kind=kind,
            old_pin=3,
            new_pin=5,
            old_file_sha256=old,
            new_file_sha256=new,
            backup_name="deployment.toml.bak.20260101T000000Z",
            backup_sha256="2" * 64,
            contained_pin=3,
            candidate_id=candidate_id,
        )

    def statuses(self):
        return [self.state.journal(number).status for number in range(1, 5) if self.state.journal(number)]

    def test_the_hash_names_the_resolution(self):
        row = self.row("o" * 64, "n" * 64)
        self.assertEqual(image_promote.implied_resolution(row, "o" * 64), "aborted")
        self.assertEqual(image_promote.implied_resolution(row, "n" * 64), "complete")
        self.assertEqual(image_promote.implied_resolution(row, "x" * 64), "diverged")

    def test_nothing_pending_resolves_nothing(self):
        self.assertEqual(image_promote.resolve_pending(self.state, "o" * 64), [])

    def test_each_row_is_resolved_from_its_own_hashes_and_only_a_completed_promotion_promotes_its_candidate(self):
        self.candidate("kept")
        self.candidate("done")
        self.row("o" * 64, "x" * 64, candidate_id="kept")
        self.row("y" * 64, "o" * 64, candidate_id="done")
        self.row("z" * 64, "o" * 64, kind="rollback", candidate_id="done")
        resolved = image_promote.resolve_pending(self.state, "o" * 64)
        self.assertEqual(
            resolved,
            [
                {"journal_id": 1, "kind": "promotion", "resolution": "aborted"},
                {"journal_id": 2, "kind": "promotion", "resolution": "complete"},
                {"journal_id": 3, "kind": "rollback", "resolution": "complete"},
            ],
        )
        self.assertEqual(self.statuses(), ["aborted", "complete", "complete"])
        self.assertEqual(self.state.build("kept").state, "candidate")
        self.assertEqual(self.state.build("done").state, "promoted")

    def test_one_diverged_row_refuses_the_whole_resolution_and_writes_nothing(self):
        self.candidate("kept")
        self.row("o" * 64, "x" * 64, candidate_id="kept")
        self.row("y" * 64, "z" * 64, candidate_id="kept")
        with self.assertRaises(StateRefusal) as raised:
            image_promote.resolve_pending(self.state, "o" * 64)
        self.assertEqual(raised.exception.reason, "PROMOTION_JOURNAL_DIVERGED")
        self.assertIn("row 2", raised.exception.detail)
        self.assertEqual(self.statuses(), ["pending", "pending"])
        self.assertEqual(self.state.build("kept").state, "candidate")

    def test_a_candidate_that_is_no_longer_a_candidate_or_never_existed_is_left_alone(self):
        self.candidate("failed-one")
        self.state.advance_build("failed-one", state="failed")
        self.row("y" * 64, "o" * 64, candidate_id="failed-one")
        self.row("y" * 64, "o" * 64, candidate_id="never-recorded")
        self.row("y" * 64, "o" * 64)
        image_promote.resolve_pending(self.state, "o" * 64)
        self.assertEqual(self.state.build("failed-one").state, "failed")
        self.assertEqual(self.statuses(), ["complete", "complete", "complete"])


# -- 6.5: a dedicated directory ------------------------------------------------------------


class DirectoryTests(PromoteCase):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_an_env_file_beside_the_deployment_file_is_refused_and_nothing_is_written(self):
        (self.config_dir / "runner.env").write_text("EXAMPLE_RUNNER_SETTING=1\n", encoding="utf-8")
        error = self.refusal((), "PROMOTE_DIRECTORY_NOT_DEDICATED")
        self.assertIn("runner.env", error["detail"])
        self.assertNotIn("EXAMPLE_RUNNER_SETTING", str(error))
        self.assertFalse(self.lock_file().exists(), "the refusal comes before the lock is taken")
        self.assertEqual(self.backups(), [])

    def test_the_file_its_backups_its_lock_and_its_temporary_file_are_accepted(self):
        for name in (
            "deployment.toml.bak.20260101T000000Z",
            "deployment.toml.bak.20260101T000000Z-1",
            "deployment.toml.lock",
            "deployment.toml.promote-tmp",
        ):
            (self.config_dir / name).write_bytes(b"left by an earlier promotion\n")
        self.promoted()
        self.assertEqual(
            (self.config_dir / "deployment.toml.bak.20260101T000000Z").read_bytes(), b"left by an earlier promotion\n"
        )
        self.assertEqual(len(self.backups()), 3)

    def test_a_name_that_only_looks_like_a_backup_is_refused(self):
        for name in ("deployment.toml.orig", "deployment.toml~", "deployment.toml.bak", "other.toml", ".env"):
            with self.subTest(name=name):
                path = self.config_dir / name
                path.write_text("synthetic\n", encoding="utf-8")
                self.refusal((), "PROMOTE_DIRECTORY_NOT_DEDICATED")
                path.unlink()

    def test_a_subdirectory_or_a_symbolic_link_never_counts_as_a_backup(self):
        (self.config_dir / "deployment.toml.bak.directory").mkdir()
        self.refusal((), "PROMOTE_DIRECTORY_NOT_DEDICATED")
        (self.config_dir / "deployment.toml.bak.directory").rmdir()
        (self.config_dir / "deployment.toml.bak.link").symlink_to(self.config_path)
        self.refusal((), "PROMOTE_DIRECTORY_NOT_DEDICATED")

    def test_the_refusal_names_five_files_and_counts_the_rest(self):
        for number in range(7):
            (self.config_dir / f"extra-{number}.txt").write_text("synthetic\n", encoding="utf-8")
        error = self.refusal((), "PROMOTE_DIRECTORY_NOT_DEDICATED")
        shown = re.findall(r"extra-\d\.txt", error["detail"])
        self.assertEqual(shown, [f"extra-{number}.txt" for number in range(5)])
        self.assertIn("and 2 more", error["detail"])

    def test_the_refusal_comes_before_the_state_file_is_consulted_and_covers_a_rollback(self):
        (self.config_dir / "runner.env").write_text("synthetic\n", encoding="utf-8")
        shutil.rmtree(self.state_dir)
        self.refusal((), "PROMOTE_DIRECTORY_NOT_DEDICATED")
        self.refusal(("--rollback",), "PROMOTE_DIRECTORY_NOT_DEDICATED")
        self.assertFalse(self.state_dir.exists())

    def test_the_preview_reports_it_and_writes_nothing(self):
        (self.config_dir / "runner.env").write_text("synthetic\n", encoding="utf-8")
        before = snapshot_tree(self.dir)
        code, payload = self.show()
        self.assertEqual(code, 0)
        self.assertEqual(payload["write_check"]["reason"], "PROMOTE_DIRECTORY_NOT_DEDICATED")
        self.assertEqual(snapshot_tree(self.dir), before)


# -- 6.6 and 6.7: rollback -------------------------------------------------------------------


class RollbackTests(PromoteCase):
    def promote_a_refreshed_pin(self):
        """Candidate build-b (version 5), the pin read from the account, then promoted."""
        self.original = self.config_path.read_bytes()
        self.seed()
        self.refresh()
        self.promoted()
        self.at_b = self.config_path.read_bytes()
        self.clock.advance(60)

    def test_a_clean_rollback_restores_the_previous_pin_and_journals_itself(self):
        self.promote_a_refreshed_pin()
        payload = self.rolled_back()
        self.assertEqual((payload["resolved"], payload["to_pin"], payload["target"]), ([], PIN, f"pin-{PIN}"))
        self.assertEqual((payload["old_pin"], payload["new_pin"]), (5, PIN))
        self.assertEqual(self.config_path.read_bytes(), self.original)
        promotion, rollback = self.journal_rows()
        self.assertEqual(
            (rollback["kind"], rollback["status"], rollback["old_pin"], rollback["new_pin"]),
            ("rollback", "complete", 5, PIN),
        )
        self.assertEqual((rollback["candidate_id"], rollback["contained_pin"]), (f"pin-{PIN}", 5))
        self.assertEqual(rollback["old_file_sha256"], sha(self.at_b))
        self.assertEqual(rollback["new_file_sha256"], sha(self.original))
        self.assertEqual((self.config_dir / rollback["backup_name"]).read_bytes(), self.at_b)
        self.assertEqual(rollback["backup_sha256"], sha(self.at_b))
        self.assertNotEqual(rollback["backup_name"], promotion["backup_name"])
        self.assertEqual(self.row("build-b")["state"], "promoted", "a rollback changes no build")
        self.assertEqual(len(self.backups()), 2)

    def test_the_rollback_edits_the_current_file_and_keeps_a_later_hand_edit(self):
        self.promote_a_refreshed_pin()
        self.config_path.write_bytes(self.at_b + b"# added by hand after the promotion\n")
        self.rolled_back()
        self.assertEqual(
            self.config_path.read_bytes(),
            self.original + b"# added by hand after the promotion\n",
        )

    def test_a_second_rollback_is_refused(self):
        self.promote_a_refreshed_pin()
        self.rolled_back()
        self.refusal(("--rollback",), "PIN_RECORD_DIVERGED")

    def test_a_promotion_after_a_rollback_can_be_rolled_back_to_the_same_pin(self):
        self.promote_a_refreshed_pin()
        self.rolled_back()
        self.seed("build-c", 6, 0xC1, age=300)
        self.clock.advance(60)
        self.promoted()
        self.assertEqual(self.pin_in_file(), 6)
        self.clock.advance(60)
        self.rolled_back()
        self.assertEqual(self.config_path.read_bytes(), self.original)
        self.assertEqual(
            [(row["kind"], row["old_pin"], row["new_pin"], row["status"]) for row in self.journal_rows()],
            [
                ("promotion", PIN, 5, "complete"),
                ("rollback", 5, PIN, "complete"),
                ("promotion", PIN, 6, "complete"),
                ("rollback", 6, PIN, "complete"),
            ],
        )

    def test_a_pending_row_is_resolved_before_the_rollback(self):
        self.original = self.config_path.read_bytes()
        self.seed()
        self.refresh()
        self.crash_after_rename()
        self.clock.advance(60)
        payload = self.rolled_back()
        self.assertEqual(
            payload["resolved"], [{"journal_id": 1, "kind": "promotion", "resolution": "complete"}]
        )
        self.assertEqual(self.config_path.read_bytes(), self.original)
        self.assertEqual([row["status"] for row in self.journal_rows()], ["complete", "complete"])
        self.assertEqual(self.row("build-b")["state"], "promoted")

    def test_there_is_nothing_to_roll_back_without_a_completed_promotion(self):
        self.seed()
        self.refusal(("--rollback",), "ROLLBACK_NO_PROMOTION")
        self.crash_before_rename()
        self.clock.advance(60)
        code, payload = self.write_pin("--rollback")
        self.assertEqual((code, payload["error"]["reason"]), (2, "ROLLBACK_NO_PROMOTION"))
        self.assertEqual([row["status"] for row in self.journal_rows()], ["aborted"], "the resolution stays")

    def test_a_pin_that_a_person_moved_since_the_promotion_is_refused(self):
        self.promote_a_refreshed_pin()
        self.edit_file(b"launch_template_version = 5", b"launch_template_version = 7")
        error = self.refusal(("--rollback",), "PIN_RECORD_DIVERGED")
        self.assertIn("pin 5", error["detail"])
        self.assertIn("pins 7", error["detail"])

    def test_a_backup_that_is_gone_is_refused(self):
        self.promote_a_refreshed_pin()
        self.backups()[0].unlink()
        self.refusal(("--rollback",), "PIN_RECORD_DIVERGED")

    def test_a_backup_that_changed_is_refused(self):
        self.promote_a_refreshed_pin()
        backup = self.backups()[0]
        backup.write_bytes(backup.read_bytes() + b"# changed\n")
        error = self.refusal(("--rollback",), "PIN_RECORD_DIVERGED")
        self.assertIn("sha256", error["detail"])

    def test_a_backup_that_holds_another_pin_is_refused_even_with_a_matching_hash(self):
        self.promote_a_refreshed_pin()
        backup = self.backups()[0]
        original = backup.read_bytes()
        # 4 is another pin; 3.0 equals the recorded 3 as a number but is not an integer pin.
        for value in (b"4", b"3.0"):
            with self.subTest(value=value):
                other = original.replace(b"launch_template_version = 3", b"launch_template_version = " + value)
                backup.write_bytes(other)
                self.corrupt_journal(1, backup_sha256=sha(other))
                error = self.refusal(("--rollback",), "PIN_RECORD_DIVERGED")
                self.assertIn("does not contain the recorded previous pin", error["detail"])

    def test_a_row_whose_contained_pin_is_not_its_previous_pin_is_refused(self):
        self.promote_a_refreshed_pin()
        self.corrupt_journal(1, contained_pin=4)
        self.refusal(("--rollback",), "PIN_RECORD_DIVERGED")

    def test_a_backup_that_is_not_toml_is_refused(self):
        self.promote_a_refreshed_pin()
        garbage = b"= not toml =\n"
        self.backups()[0].write_bytes(garbage)
        self.corrupt_journal(1, backup_sha256=sha(garbage))
        error = self.refusal(("--rollback",), "PIN_RECORD_DIVERGED")
        self.assertIn("cannot be parsed", error["detail"])

    def test_a_backup_name_that_leaves_the_directory_or_the_file_is_refused(self):
        self.promote_a_refreshed_pin()
        outside = self.dir / "deployment.toml.bak.outside"
        outside.write_bytes(self.backups()[0].read_bytes())
        names = (
            "../deployment.toml.bak.outside",
            "other.toml.bak.20260101T000000Z",
            "deployment.toml",
            "deployment.toml.bak.outside",  # the right prefix, but not a name that a backup is given
            "deployment.toml.bak.20260101T000000Z\x00",  # a NUL would make the open raise ValueError
            "deployment.toml.bak.20260101T000000Z/../x",
            "deployment.toml.bak.20260230T010000Z",  # the shape of a stamp, but February has no 30th
            "deployment.toml.bak.20260101T250000Z",
            "deployment.toml.bak.20260101T000000Z-0",
        )
        for name in names:
            with self.subTest(name=name):
                self.corrupt_journal(1, backup_name=name)
                error = self.refusal(("--rollback",), "PIN_RECORD_DIVERGED")
                self.assertIn("journal row 1", error["detail"])
                self.assertIn("next to the file", error["detail"])
                self.assertNotIn(name, error["detail"])

    def test_a_backup_whose_pin_cannot_be_converted_is_refused(self):
        self.promote_a_refreshed_pin()
        self.huge_pin_in_the_backup()
        error = self.refusal(("--rollback",), "PIN_RECORD_DIVERGED")
        self.assertIn("cannot be parsed for its pin", error["detail"])

    def test_the_backup_of_a_newer_aborted_row_is_never_read(self):
        self.promote_a_refreshed_pin()
        self.seed("build-c", 6, 0xC1, age=300, source_pin=5)
        self.crash_before_rename("--version", "6")
        newest = self.backups()[-1]
        newest.write_bytes(b"= not toml =\n")  # if the rollback read it, it would refuse
        self.clock.advance(60)
        self.rolled_back()
        self.assertEqual(self.config_path.read_bytes(), self.original)

    # -- the previous pin's image ---------------------------------------------------------
    def test_a_previous_image_that_is_gone_is_refused(self):
        self.promote_a_refreshed_pin()
        del self.world.images[ami(0xA1)]
        self.refresh()
        self.assertEqual(self.row(f"pin-{PIN}")["image_state"], "missing")
        error = self.refusal(("--rollback",), "ROLLBACK_PREVIOUS_MISSING")
        self.assertIn("missing", error["detail"])

    def test_a_previous_version_that_is_gone_is_refused(self):
        self.promote_a_refreshed_pin()
        del self.world.versions[(WORKER, PIN)]
        self.refresh()
        self.assertEqual(self.row(f"pin-{PIN}")["template_version_exists"], 0)
        self.refusal(("--rollback",), "ROLLBACK_PREVIOUS_MISSING")

    def test_a_previous_pin_that_nothing_read_is_refused_until_a_refresh_reads_it(self):
        self.original = self.config_path.read_bytes()
        self.seed()
        self.promoted()
        self.assertFalse(
            any(row["id"].startswith("pin-") for row in self.table("image_builds")),
            "a pin that no pass observed has no record at all",
        )
        self.refusal(("--rollback",), "ROLLBACK_PREVIOUS_NOT_RECORDED")
        self.refresh()
        self.clock.advance(60)
        self.rolled_back()
        self.assertEqual(self.config_path.read_bytes(), self.original)

    def test_a_previous_pin_that_was_not_read_for_too_long_is_refused_until_a_refresh(self):
        self.promote_a_refreshed_pin()
        self.clock.advance(self.config.image.promote_max_age_seconds + 1)
        self.refusal(("--rollback",), "IMAGE_VERIFICATION_STALE")
        self.refresh()
        self.rolled_back()

    def test_the_first_promotion_records_the_image_of_a_pin_that_a_pass_observed(self):
        self.build_candidate()
        self.promoted()
        record = self.row(f"pin-{PIN}")
        self.assertEqual(
            (record["state"], record["image_id"], record["template_version"], record["image_state"]),
            ("promoted", ami(0xA1), PIN, None),
        )
        self.assertEqual(self.refresh()["pin_records_created"], [])
        self.assertEqual(self.row(f"pin-{PIN}")["image_state"], "available")
        self.rolled_back()

    def test_the_rollback_refuses_the_same_checks_as_a_promotion_before_it_writes(self):
        self.promote_a_refreshed_pin()
        (self.config_dir / "runner.env").write_text("synthetic\n", encoding="utf-8")
        self.refusal(("--rollback",), "PROMOTE_DIRECTORY_NOT_DEDICATED")
        (self.config_dir / "runner.env").unlink()
        self.rolled_back()


# -- the image_promoted event ----------------------------------------------------------------


class EventTests(PromoteCase):
    """A write that moved the pin appends one ``image_promoted`` event, last, to the file that the passes use."""

    def events_path(self):
        return self.state_dir / image_pipeline.EVENTS_FILE

    def test_a_promotion_writes_one_event(self):
        self.seed()
        self.clock.advance(30)
        payload = self.promoted()
        after = self.config_path.read_bytes()
        self.assertEqual(
            self.events(),
            [
                {
                    "ts": "2027-01-15T08:00:30Z",
                    "deployment": DEPLOYMENT,
                    "event": "image_promoted",
                    "old_pin": PIN,
                    "new_pin": 5,
                    "build_id": "build-b",
                    "image_id": ami(0xB1),
                    "rollback": False,
                    "file_digest": sha(after)[:12],
                }
            ],
        )
        self.assertEqual((payload["notes"], payload["file_sha256_after"]), ([], sha(after)))

    def test_a_rollback_writes_one_event_that_names_no_candidate(self):
        original = self.config_path.read_bytes()
        self.seed()
        self.refresh()
        self.promoted()
        self.clock.advance(60)
        payload = self.rolled_back()
        promotion, rollback = self.events()
        self.assertEqual(
            rollback,
            {
                "ts": "2027-01-15T08:01:00Z",
                "deployment": DEPLOYMENT,
                "event": "image_promoted",
                "old_pin": 5,
                "new_pin": PIN,
                "build_id": None,
                "image_id": ami(0xA1),
                "rollback": True,
                "file_digest": sha(original)[:12],
            },
        )
        self.assertEqual((payload["notes"], promotion["rollback"], promotion["build_id"]), ([], False, "build-b"))

    def test_a_preview_writes_no_event(self):
        self.seed()
        self.refresh()
        code, payload = self.show()
        self.assertEqual((code, payload["decision"], payload["action"]), (0, "preview", "promote"))
        self.assertFalse(self.events_path().exists())
        self.promoted()
        (event,) = self.events()
        code, payload = self.show("--rollback")
        self.assertEqual((code, payload["decision"], payload["action"]), (0, "preview", "rollback"))
        self.assertEqual(self.events(), [event])

    def test_a_refusal_writes_no_event(self):
        self.seed()
        runner = self.config_dir / "runner.env"
        runner.write_text("synthetic\n", encoding="utf-8")
        self.refusal((), "PROMOTE_DIRECTORY_NOT_DEDICATED")  # before a lock is taken
        runner.unlink()
        with image_promote.file_lock(self.config_path.resolve()):
            self.refusal((), "PROMOTE_BUSY")  # at the first lock
        self.refusal(("--rollback",), "ROLLBACK_NO_PROMOTION")  # under both locks, with nothing to roll back
        self.change("build-b", drift_result="TEMPLATE_DRIFT")
        self.refusal(("--version", "5"), "PROMOTE_DRIFT_NOT_CLEAN")  # after the record is judged
        self.assertEqual(self.events(), [])
        self.change("build-b", drift_result="clean")
        self.promoted()
        (event,) = self.events()
        self.refusal((), "PROMOTE_NO_CANDIDATE")  # after a write, with nothing left to promote
        self.assertEqual(self.events(), [event])

    def test_an_attempt_that_does_not_finish_writes_no_event_and_the_attempt_that_does_writes_one(self):
        self.seed()
        with self.rename_fails(OSError(errno.EXDEV, "cross-device link")):
            code, payload = self.write_pin()
        self.assertEqual((code, payload["error"]["reason"]), (2, "PROMOTE_WRITE_FAILED"))
        self.assertEqual(self.events(), [])
        self.clock.advance(60)
        self.crash_before_rename()
        self.assertEqual(self.events(), [])
        self.clock.advance(60)
        self.promoted()  # it resolves the aborted row, and that resolution writes no event of its own
        self.assertEqual([event["event"] for event in self.events()], ["image_promoted"])

    def test_resolving_a_pending_row_writes_no_event(self):
        self.seed()
        self.crash_after_rename()  # the row is not complete yet, so the event is not written yet
        self.assertEqual(self.events(), [])
        code, payload = self.write_pin()  # it completes the row, then refuses: the file holds the pin already
        self.assertEqual((code, payload["error"]["reason"]), (2, "PROMOTE_NO_CANDIDATE"))
        self.assertEqual([row["status"] for row in self.journal_rows()], ["complete"])
        self.assertEqual(self.events(), [])

    def test_run_apply_resolving_a_pending_row_writes_no_event(self):
        self.seed()
        self.crash_after_rename()
        self.world.images[ami(0xB1)]["Tags"] = tag_list({**owned("build-b"), BASIS_DIGEST_TAG: self.digest()})
        self.allow_clients()
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual(code, 0, payload)
        self.assertEqual([item["resolution"] for item in payload["journal_resolved"]], ["complete"])
        self.assertEqual(self.promoted_events(), [])

    def test_the_event_is_written_once_after_the_rename_and_after_the_row_is_complete(self):
        self.seed()
        seen = []
        real = image_pipeline._append_line

        def append(path, line):
            seen.append(
                {
                    "file": Path(path).name,
                    "target": self.config_path.read_bytes(),
                    "statuses": [row["status"] for row in self.table("promotion_journal")],
                    "build": self.row("build-b")["state"],
                    "temporary": self.temporary().exists(),
                }
            )
            return real(path, line)

        with mock.patch.object(image_pipeline, "_append_line", append):
            self.promoted()
        after = self.config_path.read_bytes()
        self.assertEqual(
            seen,
            [
                {
                    "file": image_pipeline.EVENTS_FILE,
                    "target": after,
                    "statuses": ["complete"],
                    "build": "promoted",
                    "temporary": False,
                }
            ],
        )

    def test_the_event_joins_the_events_of_the_passes_in_the_same_file(self):
        build = self.build_candidate()
        passes = self.events()
        self.assertTrue(passes)
        self.promoted()
        *earlier, event = self.events()
        self.assertEqual(earlier, passes)
        self.assertEqual(
            (event["event"], event["build_id"], event["image_id"]), ("image_promoted", build.id, build.image_id)
        )

    def test_the_fields_pass_the_allowlist_and_hold_no_path_no_content_and_no_url(self):
        self.seed()
        self.refresh()
        self.promoted()
        self.clock.advance(60)
        self.rolled_back()
        lines = [
            line for line in self.events_path().read_text(encoding="utf-8").splitlines() if "image_promoted" in line
        ]
        events = self.promoted_events()
        self.assertEqual([event["rollback"] for event in events], [False, True])
        for event, line in zip(events, lines, strict=True):
            with self.subTest(rollback=event["rollback"]):
                fields = {key: value for key, value in event.items() if key not in ("ts", "deployment", "event")}
                self.assertEqual(set(fields), image_pipeline._EVENT_FIELDS["image_promoted"])
                for key, value in fields.items():
                    image_pipeline._check_event_value("image_promoted", key, value)  # raises ValueError
                    if isinstance(value, str):
                        # The allowlist lets "/" through, so a path would pass it: an id, a digest or a code does not.
                        self.assertRegex(value, r"\A[A-Za-z0-9._-]{1,64}\Z")
                self.assertEqual(len(fields["file_digest"]), 12)
                for text in (str(self.dir), self.config_path.name, "launch_template", "/", "://"):
                    self.assertNotIn(text, line)
                self.assertIsNone(re.search(r"[0-9a-f]{64}", line), "only the short digest is written")

    # -- a write of the event that fails: the promotion is done and stays ----------------------
    def test_a_failing_event_write_after_the_rename_leaves_the_promotion_in_place_and_is_noted(self):
        self.seed()
        self.refresh()
        before = self.config_path.read_bytes()
        failure = OSError(errno.ENOSPC, "No space left on device", str(self.events_path()))
        with mock.patch.object(image_pipeline, "_append_line", side_effect=failure) as append:
            code, payload = self.write_pin()
        self.assertEqual(append.call_count, 1)
        self.assertEqual((code, payload["decision"], payload["notes"]), (0, "promoted", ["event_write:ENOSPC"]))
        self.assertNotEqual(self.config_path.read_bytes(), before)
        self.assertEqual((self.pin_in_file(), payload["new_pin"]), (5, 5))
        self.assertEqual([row["status"] for row in self.journal_rows()], ["complete"])
        self.assertEqual(self.row("build-b")["state"], "promoted")
        self.assertEqual(self.events(), [])
        # The promotion is whole: the rollback that follows works, and it writes its event.
        self.clock.advance(60)
        self.rolled_back()
        self.assertEqual([event["rollback"] for event in self.events()], [True])

    def test_a_failing_event_write_without_an_errno_is_noted_by_the_type_of_its_error(self):
        self.seed()
        with mock.patch.object(image_pipeline, "_append_line", side_effect=OSError("synthetic /secret/path")):
            code, payload = self.write_pin()
        self.assertEqual((code, payload["decision"], payload["notes"]), (0, "promoted", ["event_write:OSError"]))
        self.assertEqual(self.pin_in_file(), 5)

    def test_a_value_that_the_allowlist_refuses_is_noted_and_the_promotion_stays(self):
        self.seed("build=b")  # an id with a character that no event value may hold
        code, payload = self.write_pin()
        self.assertEqual((code, payload["decision"], payload["notes"]), (0, "promoted", ["event_write:ValueError"]))
        self.assertEqual((self.pin_in_file(), self.row("build=b")["state"]), (5, "promoted"))
        self.assertEqual([row["status"] for row in self.journal_rows()], ["complete"])
        self.assertEqual(self.events(), [])


# -- the module stays local ------------------------------------------------------------------


class MountPointTests(unittest.TestCase):
    """``is_mount_point`` reads the mount table of the process: a bind mount of one file hides from a device check."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name).resolve()
        self.table = self.dir / "mountinfo"
        patch = mock.patch.object(image_promote, "_MOUNTINFO", self.table)
        patch.start()
        self.addCleanup(patch.stop)

    def listing(self, *mount_points):
        lines = [f"{number} 1 8:1 /source {point} rw,relatime shared:1 - ext4 /dev/example rw" for number, point in enumerate(mount_points, start=30)]
        self.table.write_text("\n".join(["29 1 0:5 / / rw - rootfs rootfs rw", *lines]) + "\n", encoding="utf-8")

    def made(self, name="deployment.toml"):
        path = self.dir / name
        path.write_text("x", encoding="utf-8")
        return path

    def test_each_escape_of_the_mount_table_is_undone(self):
        for escaped, plain in (
            ("\\040", " "),
            ("\\011", "\t"),
            ("\\012", "\n"),
            ("\\134", "\\"),
        ):
            with self.subTest(escaped=escaped):
                self.assertEqual(image_promote._unescape_mount_point(f"/a{escaped}b"), f"/a{plain}b")
        self.assertEqual(image_promote._unescape_mount_point("/a\\040b\\011c\\012d\\134e"), "/a b\tc\nd\\e")
        self.assertEqual(image_promote._unescape_mount_point("/plain/path"), "/plain/path")

    def test_a_backslash_that_spells_an_escape_stays_a_backslash(self):
        # A name that holds a backslash and the digits 040 is written as \134 and 040: it is not a space.
        self.assertEqual(image_promote._unescape_mount_point("/a\\134040b"), "/a\\040b")

    def test_a_listed_path_is_a_mount_point(self):
        path = self.made()
        self.listing(str(path))
        self.assertTrue(image_promote.is_mount_point(path))

    def test_a_path_that_is_not_listed_is_not_a_mount_point(self):
        path = self.made()
        self.listing(str(self.dir / "another.toml"), str(self.dir))
        self.assertFalse(image_promote.is_mount_point(path))

    def test_a_mount_of_the_directory_is_not_a_mount_of_the_file(self):
        path = self.made()
        self.listing(str(self.dir))
        self.assertFalse(image_promote.is_mount_point(path))

    def test_a_listed_path_with_a_space_is_a_mount_point(self):
        path = self.made("deployment file.toml")
        self.listing(str(path).replace(" ", "\\040"))
        self.assertTrue(image_promote.is_mount_point(path))

    def test_a_symbolic_link_to_a_listed_file_is_a_mount_point(self):
        path = self.made()
        link = self.dir / "link.toml"
        link.symlink_to(path)
        self.listing(str(path))
        self.assertTrue(image_promote.is_mount_point(link))

    def test_a_mount_table_with_a_name_that_is_not_utf8_is_read(self):
        path = self.made()
        other = b"/mnt/caf\xe9"
        first = b"31 1 8:1 /a " + other + b" rw - ext4 /dev/example rw\n"
        second = b"32 1 8:1 /b " + os.fsencode(path) + b" rw - ext4 /dev/example rw\n"
        self.table.write_bytes(first + second)
        self.assertTrue(image_promote.is_mount_point(path))
        self.table.write_bytes(first)
        self.assertFalse(image_promote.is_mount_point(path))

    def test_a_short_or_empty_line_of_the_table_is_ignored(self):
        path = self.made()
        self.table.write_text("\n29 1 0:5\n", encoding="utf-8")
        self.assertFalse(image_promote.is_mount_point(path))

    def test_an_unreadable_table_falls_back_to_the_device_comparison(self):
        path = self.made()
        self.assertFalse(self.table.exists())
        for answer in (True, False):
            with self.subTest(answer=answer), mock.patch("os.path.ismount", return_value=answer) as ismount:
                self.assertEqual(image_promote.is_mount_point(path), answer)
            ismount.assert_called_once_with(str(path))

    def test_a_readable_table_decides_without_the_device_comparison(self):
        path = self.made()
        self.listing()
        with mock.patch("os.path.ismount", return_value=True) as ismount:
            self.assertFalse(image_promote.is_mount_point(path))
        ismount.assert_not_called()


class PureLocalTests(unittest.TestCase):
    def test_importing_the_promotion_loads_no_provider_or_forge_module(self):
        root = Path(image_promote.__file__).resolve().parent.parent
        script = (
            "import sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import ci_burst.image_promote\n"
            "names = ('boto3', 'botocore', 'ci_burst.aws', 'ci_burst.forgejo', 'ci_burst.image_pipeline')\n"
            "print(sorted(name for name in sys.modules if name in names or name.split('.')[0] in names[:2]))\n"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(root)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual((completed.returncode, completed.stdout.strip()), (0, "[]"), completed.stderr)


if __name__ == "__main__":
    unittest.main()
