"""Tests for the ``image`` command group of the command line (tasks 6.1 and 6.7).

The commands run through ``ci_burst.cli.main``, the way an operator runs them,
against the pipeline's fake account (``World``, ``FakeSource``) and the real
state file, with the four seams of ``ci_burst.cli`` replaced: the AWS factory,
the forge factory, the clock and the sleep. A test that needs a command to make
no provider call installs factories that raise ``Forbidden``, an exception that
nothing in the code under test may catch, so a violation fails the test instead
of becoming a refusal.

The deployment file lives in a directory of its own, the scaling ledger and the
image state in two others, as the loader and ``promote`` require. Every value is
synthetic. Each test ends with the pipeline's secret scan, which now also reads
every result a command printed.
"""

import contextlib
import copy
import hashlib
import io
import json
import sqlite3
import unittest
from unittest import mock

from ci_burst import cli, image
from ci_burst.errors import ObservationRefusal, ProviderRefusal
from ci_burst.image_state import PIN_RECORD_REASON, ImageState
from ci_burst.image_status import VERSION_NOT_FOUND

from support import write_config
from test_image_aws import ProviderError
from test_image_pipeline import (
    BUCKET,
    DAY,
    DEPLOYMENT,
    FAILED,
    NOW,
    PIN,
    READY,
    SCRIPT,
    WAITING,
    WORKER,
    PipelineCase,
    WorldAws,
    ami,
    owned,
    render_toml,
    snapshot_tree,
)

#: The four columns that ``status --refresh`` may rewrite on a build row.
VERIFICATION = ("image_state", "template_version_exists", "drift_result", "verified_at")
TABLES = ("image_builds", "pin_log", "promotion_journal", "control")
#: The provider calls of a describe-only read of a launch-template version and an image.
DESCRIBES = frozenset({"describe_launch_template_versions", "describe_images"})


class Forbidden(BaseException):
    """A call that the command under test must not make. Nothing catches it."""


class StrictAws(WorldAws):
    """The live adapter's answer for a version that is not there: a refusal that carries the code.

    The fake account answers an absent version with an empty list. The live
    adapter refuses the read with the provider's error code, and ``status
    --refresh`` must treat both as absence.
    """

    def template_versions(self, name, versions, *, deadline=None):
        for version in versions:
            if (name, version) not in self.world.versions:
                raise ProviderRefusal("AWS_UNCERTAIN", VERSION_NOT_FOUND, uncertain=True)
        return super().template_versions(name, versions, deadline=deadline)


class CliCase(PipelineCase):
    """The fake account behind the real command line, with a deployment file in a directory of its own."""

    strict_missing = False

    def setUp(self):
        super().setUp()
        self.make_aws = self.aws_for_command
        self.make_source = self.source_for_command

    def write_deployment(self, toml=None, *, with_image=True, pin=PIN, zones=None):
        """Write the deployment file into a directory of its own.

        The ledger and the image state live in other directories, as the loader requires.
        """
        self.config_dir = self.dir / "config"
        self.config_dir.mkdir(exist_ok=True)
        aws = {"launch_template_name": WORKER, "launch_template_version": pin}
        if zones:
            aws["availability_zones"] = zones
        ledger = str(self.dir / "ledger" / "state.sqlite3")
        self.config_path = write_config(
            self.config_dir, config={"deployment": DEPLOYMENT, "aws": aws, "state": {"database": ledger}}
        )
        if with_image:
            block = self.image_toml() if toml is None else toml
            self.config_path.write_text(self.config_path.read_text(encoding="utf-8") + block, encoding="utf-8")

    def configure(self, toml=None, *, with_image=True, pin=PIN, zones=None, **keywords):
        """Write the deployment file, then build a pipeline on it."""
        self.write_deployment(toml, with_image=with_image, pin=pin, zones=zones)
        self.restart(**keywords)

    def image_toml(self, **keywords):
        return render_toml(state_directory=str(self.state_dir), **keywords)

    # -- the seams of the command line -------------------------------------------
    def aws_for_command(self, config):
        session = (StrictAws if self.strict_missing else WorldAws)(self.world)
        self.sessions.append(session)
        return session

    def source_for_command(self, config):
        return self.source_factory()

    def forbid_clients(self):
        def forbidden(config):
            raise Forbidden("a client was built")

        self.make_aws = self.make_source = forbidden

    def allow_clients(self):
        self.make_aws = self.aws_for_command
        self.make_source = self.source_for_command

    def invoke(self, *arguments, config=True):
        """Run ``ci-burst`` with the seams replaced; return the exit code and the JSON it printed."""
        argv = [*arguments, *(["--config", str(self.config_path)] if config else [])]
        buffer = io.StringIO()
        with contextlib.ExitStack() as stack:
            for name, value in (
                ("make_image_aws", self.make_aws),
                ("make_image_source", self.make_source),
                ("image_clock", self.clock),
                ("image_pause", self.sleep),
            ):
                stack.enter_context(mock.patch.object(cli, name, value))
            stack.enter_context(mock.patch("ci_burst.image.shipped_script", return_value=SCRIPT))
            stack.enter_context(contextlib.redirect_stdout(buffer))
            code = cli.main(argv)
        self.seen.append(buffer.getvalue())
        return code, json.loads(buffer.getvalue())

    # -- the state file, read without touching it --------------------------------------
    def table(self, name):
        database = self.state_dir / "image-state.sqlite"
        connection = sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {name} ORDER BY rowid")]
        finally:
            connection.close()

    def dump(self):
        if not (self.state_dir / "image-state.sqlite").exists():
            return None
        return {name: self.table(name) for name in TABLES}

    def builds(self):
        """The builds of the deployment, without the pin records: those are evidence, not builds."""
        return [row for row in super().builds() if row.reason_code != PIN_RECORD_REASON]

    def row(self, build_id):
        (found,) = [row for row in self.table("image_builds") if row["id"] == build_id]
        return found

    def journal_rows(self):
        return self.table("promotion_journal")

    # -- the files ---------------------------------------------------------------
    def file_sha(self):
        return hashlib.sha256(self.config_path.read_bytes()).hexdigest()

    def pin_in_file(self):
        for line in self.config_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("launch_template_version"):
                return int(line.split("=")[1].split("#")[0])
        raise AssertionError("no pin line")

    # -- fixtures ----------------------------------------------------------------
    def build_candidate(self):
        """A real build through the pipeline: version 5 on a new image, verified now. No command runs."""
        self.world.console_plan = [(100.0, WAITING), (300.0, READY)]
        result = self.pipeline.run_until_done()
        self.assertEqual(result["decision"], "candidate", result)
        self.world.console_plan = []
        return self.only_build()

    def add_pending(self, *, old_sha, new_sha, old_pin=PIN, new_pin=5, candidate_id=None, kind="promotion"):
        def insert(state):
            return state.journal_insert(
                kind=kind,
                old_pin=old_pin,
                new_pin=new_pin,
                old_file_sha256=old_sha,
                new_file_sha256=new_sha,
                backup_name="deployment.toml.bak.20260101T000000Z",
                backup_sha256="2" * 64,
                contained_pin=old_pin,
                candidate_id=candidate_id,
            )

        return self.write(insert)

    def add_world_candidate(self, build_id, version, image_number, *, age, drift=None, source_pin=PIN):
        """A verified candidate row and the version and image it names in the account.

        The version is a copy of ``source_pin`` with the new image, so it is clean unless ``drift``
        changes a field of its data.
        """
        image_id = ami(image_number)
        data = copy.deepcopy(self.world.versions[(WORKER, source_pin)]["LaunchTemplateData"])
        data.update({"ImageId": image_id, **(drift or {})})
        self.world.set_version(WORKER, version, data)
        self.world.add_image(image_id, tags=owned(build_id), age=age)
        self.add_build(
            build_id,
            "candidate",
            age=age,
            image_id=image_id,
            template_version=version,
            gate_configured=1,
            gate_passed=1,
            image_state="available",
            template_version_exists=1,
            drift_result="clean",
            verified_at=NOW - age + 60,
            source_pin=source_pin,
            script_sha256="c" * 64,
        )


# -- 6.1: disabled, guards and exit codes --------------------------------------------

FORMS = (
    ("image", "plan"),
    ("image", "run"),
    ("image", "run", "--force"),
    ("image", "run", "--apply"),
    ("image", "run", "--apply", "--until-done"),
    ("image", "run", "--apply", "--until-done", "--force"),
    ("image", "status"),
    ("image", "status", "--refresh"),
    ("image", "promote"),
    ("image", "promote", "--version", "5"),
    ("image", "promote", "--write"),
    ("image", "promote", "--rollback"),
    ("image", "promote", "--rollback", "--write"),
)


class DisabledTests(CliCase):
    def assert_disabled(self):
        self.forbid_clients()
        before = snapshot_tree(self.dir)
        for form in FORMS:
            with self.subTest(form=" ".join(form)):
                code, payload = self.invoke(*form)
                self.assertEqual((code, payload), (0, {"decision": "disabled"}))
        self.assertEqual(snapshot_tree(self.dir), before)
        self.assertFalse(self.state_dir.exists())
        self.assertEqual((self.sessions, self.source_sessions, self.world.calls), ([], 0, []))

    def test_an_absent_image_table_answers_disabled_to_every_form_and_builds_nothing(self):
        self.configure(with_image=False)
        self.assert_disabled()

    def test_a_disabled_image_table_answers_disabled_to_every_form_and_builds_nothing(self):
        self.configure("\n[image]\nenabled = false\n")
        self.assert_disabled()

    def test_an_enabled_table_with_a_typo_is_still_refused_by_the_strict_loader(self):
        self.write_deployment(self.image_toml(image_extra="retian = 3\n"))
        code, payload = self.invoke("image", "plan")
        self.assertEqual((code, payload["error"]["reason"]), (2, "CONFIG_INVALID"))


class GuardTests(CliCase):
    def test_until_done_without_apply_is_refused_before_anything_is_called(self):
        self.forbid_clients()
        before = snapshot_tree(self.dir)
        for form in (
            ("image", "run", "--until-done"),
            ("image", "run", "--until-done", "--force"),
        ):
            with self.subTest(form=" ".join(form)):
                code, payload = self.invoke(*form)
                self.assertEqual(code, 2)
                self.assertEqual(payload["error"]["reason"], cli.IMAGE_UNTIL_DONE_NEEDS_APPLY)
        self.assertEqual(snapshot_tree(self.dir), before)
        self.assertEqual((self.sessions, self.source_sessions, self.world.calls), ([], 0, []))

    def test_the_refusal_comes_before_the_configuration_is_read(self):
        with mock.patch("ci_burst.cli.load", side_effect=Forbidden("the configuration was read")):
            code, payload = self.invoke("image", "run", "--until-done", "--config", "/nonexistent", config=False)
        self.assertEqual((code, payload["error"]["reason"]), (2, cli.IMAGE_UNTIL_DONE_NEEDS_APPLY))

    def test_a_missing_configuration_is_a_refusal_with_exit_two(self):
        code, payload = self.invoke("image", "plan", "--config", str(self.dir / "absent.toml"), config=False)
        self.assertEqual((code, payload["error"]["reason"]), (2, "CONFIG_MISSING"))

    def test_a_version_and_a_rollback_exclude_each_other(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["image", "promote", "--version", "5", "--rollback", "--config", str(self.config_path)])
        self.assertEqual(raised.exception.code, 2)

    def test_a_version_must_be_a_number(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            cli.main(["image", "promote", "--version", "five", "--config", str(self.config_path)])
        self.assertEqual(raised.exception.code, 2)


class FactoryTests(CliCase):
    """The two functions that construct a client are the only place the image path does."""

    def test_the_aws_session_uses_the_image_role_and_the_region(self):
        with mock.patch.object(cli, "ImageAws") as adapter:
            session = cli.make_image_aws(self.config)
        adapter.assert_called_once_with(self.config.aws.region, "example-image-role")
        self.assertIs(session, adapter.return_value)

    def test_the_forge_reader_uses_its_own_token_variable_and_never_the_scaling_one(self):
        with mock.patch.object(cli, "ForgejoSource") as reader:
            source = cli.make_image_source(self.config)
        reader.assert_called_once_with(
            self.config.forgejo.base_url,
            self.config.forgejo.repository,
            "EXAMPLE_IMAGE_TOKEN",
            timeout_seconds=self.config.forgejo.timeout_seconds,
            max_response_bytes=self.config.forgejo.max_response_bytes,
        )
        self.assertNotEqual("EXAMPLE_IMAGE_TOKEN", self.config.forgejo.token_env)
        self.assertIs(source, reader.return_value)

    def test_only_the_command_line_constructs_an_image_client(self):
        import ast
        from pathlib import Path

        package = Path(cli.__file__).parent
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", None)
                if name in ("ImageAws", "ForgejoSource"):
                    self.assertEqual(path.name, "cli.py", f"{path.name} line {node.lineno} constructs {name}")


class PlanTests(CliCase):
    def test_plan_says_a_build_would_start_and_writes_nothing(self):
        before = snapshot_tree(self.dir)
        code, payload = self.invoke("image", "plan")
        self.assertEqual(code, 0)
        self.assertEqual((payload["decision"], payload["reason"]), ("start", None))
        self.assertEqual(payload["pin"]["version"], PIN)
        self.assertFalse(payload["state"]["present"])
        self.assertEqual(snapshot_tree(self.dir), before)
        self.assertFalse(self.state_dir.exists())

    def test_run_without_apply_is_plan_and_writes_nothing(self):
        before = snapshot_tree(self.dir)
        plan_code, plan = self.invoke("image", "plan")
        run_code, run = self.invoke("image", "run")
        self.assertEqual((plan_code, run_code), (0, 0))
        self.assertEqual(plan, run)
        self.assertEqual(snapshot_tree(self.dir), before)
        mutating = {"run_instances", "create_image", "terminate_instances", "create_multipart_upload"}
        self.assertFalse(mutating & set(self.world.names()))

    def test_plan_and_run_leave_an_existing_state_file_byte_for_byte(self):
        self.add_build("earlier", "failed", age=2 * DAY)
        self.set_control(consecutive_failures=2)
        before = snapshot_tree(self.dir)
        for form in (("image", "plan"), ("image", "run"), ("image", "run", "--force")):
            with self.subTest(form=" ".join(form)):
                self.assertEqual(self.invoke(*form)[0], 0)
        self.assertEqual(snapshot_tree(self.dir), before)

    def test_force_without_apply_shows_what_a_forced_run_would_decide_and_writes_nothing(self):
        self.pin_current()
        before = snapshot_tree(self.dir)
        plain = self.invoke("image", "run")[1]
        forced = self.invoke("image", "run", "--force")[1]
        self.assertEqual((plain["decision"], plain["forced"]), ("digest_unchanged", False))
        self.assertEqual((forced["decision"], forced["forced"]), ("start", True))
        self.assertEqual(snapshot_tree(self.dir), before)

    def test_an_unchanged_digest_is_a_completed_decision_with_exit_zero(self):
        self.pin_current()
        code, payload = self.invoke("image", "plan")
        self.assertEqual((code, payload["decision"]), (0, "digest_unchanged"))

    def test_a_refused_trigger_exits_two(self):
        self.add_build("earlier", "failed", age=600, digest="d" * 64)
        code, payload = self.invoke("image", "plan")
        self.assertEqual((code, payload["decision"], payload["reason"]), (2, "refused", "IMAGE_MIN_INTERVAL"))

    def test_a_configuration_refusal_exits_two_before_any_provider_call(self):
        self.configure(self.image_toml(gate=""))
        code, payload = self.invoke("image", "plan")
        self.assertEqual((code, payload["error"]["reason"]), (2, "IMAGE_GATE_REQUIRED"))
        self.assertEqual((self.sessions, self.world.calls), ([], []))

    def test_a_provider_refusal_that_escapes_exits_one(self):
        self.world.fail("describe_launch_template_versions", ProviderError("RequestLimitExceeded"))
        code, payload = self.invoke("image", "plan")
        self.assertEqual((code, payload["error"]["reason"]), (1, "AWS_UNCERTAIN"))

    def test_a_forge_refusal_exits_two(self):
        self.source.errors["branch"] = ObservationRefusal("IMAGE_SOURCE_FORBIDDEN")
        code, payload = self.invoke("image", "plan")
        self.assertEqual((code, payload["error"]["reason"]), (2, "IMAGE_SOURCE_FORBIDDEN"))


class RunApplyTests(CliCase):
    def test_until_done_builds_a_candidate_and_stamps_the_live_pin_on_the_last_session(self):
        self.world.console_plan = [(100.0, WAITING), (300.0, READY)]
        code, payload = self.invoke("image", "run", "--apply", "--until-done")
        self.assertEqual((code, payload["decision"]), (0, "candidate"))
        self.assertEqual(payload["journal_resolved"], [])
        self.assertEqual(
            payload["pin_evidence"], {"recorded": True, "versions": [PIN], "pin_records_created": [PIN]}
        )
        self.assertEqual(len(self.sessions), 2, "the stamp reuses the session of the last pass")
        record = self.row(f"pin-{PIN}")
        self.assertEqual((record["state"], record["reason_code"]), ("promoted", PIN_RECORD_REASON))
        self.assertEqual((record["image_state"], record["template_version_exists"]), ("available", 1))
        self.assertEqual((record["image_id"], record["verified_at"]), (ami(0xA1), self.clock()))

    def test_a_gate_less_run_that_only_deletes_an_owed_archive_makes_one_provider_call_and_stamps_nothing(self):
        key = "warm-archives/owed-cli.tar.gz"
        self.add_build("owed-cli", "failed", archive_bucket=BUCKET, archive_key=key)
        self.world.objects[key] = (b"archive", NOW)
        self.configure(self.image_toml(gate=""))
        world_mark, source_mark = len(self.world.calls), len(self.source.calls)
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["decision"], payload["reason"]), (2, "refused", "IMAGE_GATE_REQUIRED"))
        self.assertEqual(payload["reconcile"]["owed_archives_deleted"], 1)
        self.assertEqual(self.world.arguments("delete_object")[-1], {"Bucket": BUCKET, "Key": key})
        self.assertEqual(self.world.names(world_mark), ["delete_object"])
        self.assertEqual(self.source.calls[source_mark:], [])
        self.assertEqual(payload["pin_evidence"], {"recorded": False, "reason": "IMAGE_CLEANUP_ONLY"})
        self.assertFalse(any(row["id"].startswith("pin-") for row in self.table("image_builds")))

    def test_one_pass_returns_the_build_in_flight_with_exit_zero(self):
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["decision"]), (0, "building"))
        self.assertEqual(self.sleeps, [30] * 7)
        self.assertEqual(len(self.sessions), 1)

    def test_the_stamp_is_describe_only(self):
        self.pin_current()
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["decision"]), (0, "digest_unchanged"))
        self.assertTrue(payload["pin_evidence"]["recorded"])
        from test_image_pipeline import READS

        self.assertLessEqual(set(self.world.names()), READS)

    def test_a_failed_build_exits_one_and_still_reports_the_pin_evidence(self):
        self.world.console_plan = [(30.0, FAILED)]
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["decision"], payload["reason"]), (1, "failed", "WARM_FAILED"))
        self.assertTrue(payload["pin_evidence"]["recorded"])

    def test_a_refused_trigger_exits_two_and_still_reports_the_pin_evidence(self):
        self.add_build("earlier", "failed", age=600, digest="d" * 64)
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["decision"], payload["reason"]), (2, "refused", "IMAGE_MIN_INTERVAL"))
        self.assertTrue(payload["pin_evidence"]["recorded"])

    def test_a_provider_refusal_exits_one_and_stamps_nothing(self):
        self.world.fail("describe_launch_template_versions", ProviderError("RequestLimitExceeded"))
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["error"]["reason"]), (1, "AWS_UNCERTAIN"))
        self.assertFalse(any(row["id"].startswith("pin-") for row in self.table("image_builds")))

    def test_a_stamp_that_fails_does_not_replace_the_result_of_the_pass(self):
        self.pin_current()
        with mock.patch("ci_burst.image_status.refresh", side_effect=ProviderRefusal("AWS_UNCERTAIN", "Throttled")):
            code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["decision"]), (0, "digest_unchanged"))
        self.assertEqual(
            payload["pin_evidence"], {"recorded": False, "reason": "AWS_UNCERTAIN", "detail": "Throttled"}
        )

    def test_a_configuration_refusal_exits_two_and_creates_no_state_file(self):
        self.configure(self.image_toml(gate=""))
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["error"]["reason"]), (2, "IMAGE_GATE_REQUIRED"))
        self.assertFalse(self.state_dir.exists())
        self.assertEqual(self.sessions, [])

    # The gate requirement guards a new launch. The command opens the state for writing before the pass starts, so
    # the pass looks for a build in flight through that connection.

    def test_a_configuration_refusal_with_no_build_in_flight_builds_no_client_and_writes_nothing(self):
        self.add_build("earlier", "failed", age=3 * DAY)
        self.configure(self.image_toml(gate=""))
        self.forbid_clients()
        before = self.dump()
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["error"]["reason"]), (2, "IMAGE_GATE_REQUIRED"))
        self.assertEqual(self.dump(), before)
        self.assertEqual((self.sessions, self.source_sessions), ([], 0))

    def test_a_pass_settles_a_build_whose_file_lost_the_gate_and_has_no_opt_out(self):
        # The pass ran out of time after the upload, so the row names an archive and has no builder. The file then
        # loses its gate, and the command still ends the row and deletes the archive instead of refusing to run.
        self.world.hooks["generate_presigned_url"] = lambda arguments: self.clock.advance(400)
        self.assertEqual(self.pipeline.run_pass(apply=True)["decision"], "building")
        del self.world.hooks["generate_presigned_url"]
        (first,) = self.builds()
        self.assertEqual(list(self.world.objects), [first.archive_key])
        self.configure(self.image_toml(gate=""))
        code, payload = self.invoke("image", "run", "--apply")
        self.assertNotIn("error", payload)
        self.assertEqual((code, payload["decision"], payload["reason"]), (2, "refused", "IMAGE_INPUTS_CHANGED"))
        self.assertEqual(self.row(first.id)["state"], "failed")
        self.assertEqual(self.row(first.id)["reason_code"], "IMAGE_INPUTS_CHANGED")
        self.assertEqual(len(self.builds()), 1)
        self.assertEqual(self.world.objects, {})
        self.assertEqual(self.world.arguments("run_instances"), [])
        self.assertEqual(self.control().consecutive_failures, 0)
        # The next command has no build in flight, so it is a new launch and the requirement refuses it.
        self.forbid_clients()
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["error"]["reason"]), (2, "IMAGE_GATE_REQUIRED"))

    def test_a_pass_that_is_locked_out_exits_two(self):
        other = ImageState(self.state_dir, clock=self.clock)
        other.connect()
        other.acquire_writer()
        self.addCleanup(other.close)
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["error"]["reason"]), (2, "IMAGE_STATE_LOCKED"))


class RunApplyJournalTests(CliCase):
    """``image run --apply`` resolves a pending promotion from the file hash before it does anything else."""

    def pending(self, *, old_sha=None, new_sha=None, candidate_id=None):
        return self.add_pending(
            old_sha=old_sha or "0" * 64, new_sha=new_sha or "1" * 64, new_pin=5, candidate_id=candidate_id
        )

    def test_a_row_whose_rename_did_not_happen_is_aborted(self):
        self.pin_current()
        row = self.pending(old_sha=self.file_sha())
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(
            payload["journal_resolved"], [{"journal_id": row.id, "kind": "promotion", "resolution": "aborted"}]
        )
        self.assertEqual(self.journal_rows()[0]["status"], "aborted")

    def test_a_row_whose_rename_happened_is_completed_and_its_candidate_promoted(self):
        self.pin_current()
        self.add_world_candidate("build-b", 5, 0xB1, age=600)
        row = self.pending(new_sha=self.file_sha(), candidate_id="build-b")
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(
            payload["journal_resolved"], [{"journal_id": row.id, "kind": "promotion", "resolution": "complete"}]
        )
        self.assertEqual(self.journal_rows()[0]["status"], "complete")
        self.assertEqual(self.row("build-b")["state"], "promoted")

    def test_a_file_that_matches_neither_hash_refuses_the_whole_run_and_writes_nothing(self):
        self.pin_current()
        self.pending()
        before = self.dump()
        tree = {name: data for name, data in snapshot_tree(self.config_dir).items()}
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["error"]["reason"]), (2, "PROMOTION_JOURNAL_DIVERGED"))
        self.assertEqual(self.dump(), before)
        self.assertEqual(self.journal_rows()[0]["status"], "pending")
        self.assertEqual(snapshot_tree(self.config_dir), tree)
        self.assertEqual((self.sessions, self.source_sessions), ([], 0), "no client is built before the refusal")

    def test_a_row_whose_status_is_outside_the_schema_refuses_the_whole_run_and_deletes_nothing(self):
        # A pending row whose status a damaged file no longer says: a run that skipped it would neither resolve it
        # nor protect the pin it may roll back to from retention.
        self.pin_current()
        self.pending(old_sha=self.file_sha())
        connection = sqlite3.connect(self.state_dir / "image-state.sqlite")
        try:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("UPDATE promotion_journal SET status = 'pendinG'")
            connection.commit()
        finally:
            connection.close()
        before, calls = self.dump(), len(self.world.calls)
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["error"]["reason"]), (2, "IMAGE_STATE_UNREADABLE"))
        self.assertNotIn("pendinG", json.dumps(payload))
        self.assertEqual(self.dump(), before)
        self.assertEqual(len(self.world.calls), calls, "no image is deregistered")
        self.assertEqual((self.sessions, self.source_sessions), ([], 0), "no client is built before the refusal")

    def test_a_completed_row_that_was_changed_refuses_the_run_before_any_client(self):
        # Retention and cleanup read the newest completed row late in the pass, after reconcile may have deleted a
        # stray archive: the run checks every row first.
        self.pin_current()
        self.pending(new_sha=self.file_sha())
        connection = sqlite3.connect(self.state_dir / "image-state.sqlite")
        try:
            connection.execute("UPDATE promotion_journal SET status = 'complete', old_pin = 'x; not a pin'")
            connection.commit()
        finally:
            connection.close()
        before, calls = self.dump(), len(self.world.calls)
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["error"]["reason"]), (2, "IMAGE_STATE_UNREADABLE"))
        self.assertNotIn("not a pin", json.dumps(payload))
        self.assertEqual(self.dump(), before)
        self.assertEqual(len(self.world.calls), calls, "nothing in the account is read or changed")
        self.assertEqual((self.sessions, self.source_sessions), ([], 0), "no client is built before the refusal")

    def test_an_until_done_run_resolves_the_row_once_before_its_first_pass(self):
        self.pin_current()
        self.pending(old_sha=self.file_sha())
        code, payload = self.invoke("image", "run", "--apply", "--until-done")
        self.assertEqual((code, payload["decision"]), (0, "digest_unchanged"))
        self.assertEqual([item["resolution"] for item in payload["journal_resolved"]], ["aborted"])

    def test_a_run_without_a_pending_row_resolves_nothing(self):
        self.pin_current()
        self.add_build("earlier", "failed", age=3 * DAY)
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["journal_resolved"]), (0, []))

    # -- the image of a promotion that this run resolves as aborted -------------------
    # A promotion to version 5 replaced the deployment file and stopped before it completed its row. A container
    # whose bind mount keeps the old inode runs this command: it reads the old file, so the row is resolved as
    # aborted. After that, no pending row and no completed row names version 5, and the file this run reads does not.

    def three_usable_images_with_version_5_the_oldest(self):
        """Three usable images against ``retain = 2``: retention takes the oldest unless something protects it.

        The oldest is the image of version 5. The newest of the other two is protected as the current candidate.
        """
        self.configure(self.image_toml(image_extra="retain = 2\nretain_min_age_seconds = 3600\n"))
        self.pin_current()
        self.add_world_candidate("build-b", 5, 0xB1, age=9 * DAY)
        self.add_world_candidate("build-c", 6, 0xC1, age=3 * DAY)
        self.add_world_candidate("build-d", 7, 0xD1, age=2 * DAY)

    def test_the_image_of_a_promotion_that_this_run_resolves_as_aborted_survives_retention_in_the_same_pass(self):
        self.three_usable_images_with_version_5_the_oldest()
        row = self.pending(old_sha=self.file_sha(), candidate_id="build-b")
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(
            payload["journal_resolved"], [{"journal_id": row.id, "kind": "promotion", "resolution": "aborted"}]
        )
        self.assertEqual(self.journal_rows()[0]["status"], "aborted")
        self.assertEqual(self.pin_in_file(), PIN)
        self.assertEqual(payload["retention"]["deregistered"], 1)
        # The oldest image that nothing protects goes instead of version 5's.
        self.assertEqual(self.world.arguments("deregister_image"), [{"ImageId": ami(0xC1)}])
        self.assertEqual(set(self.world.images), {ami(0xA1), ami(0xB1), ami(0xD1)})

    def test_without_the_row_the_same_run_takes_the_oldest_image(self):
        # The control: nothing but the journal row separates this run from the one above.
        self.three_usable_images_with_version_5_the_oldest()
        code, payload = self.invoke("image", "run", "--apply")
        self.assertEqual((code, payload["journal_resolved"]), (0, []))
        self.assertEqual(payload["retention"]["deregistered"], 1)
        self.assertEqual(self.world.arguments("deregister_image"), [{"ImageId": ami(0xB1)}])
        self.assertEqual(set(self.world.images), {ami(0xA1), ami(0xC1), ami(0xD1)})


# -- 6.1: status ---------------------------------------------------------------------


class StatusTests(CliCase):
    def test_status_without_a_state_file_reports_the_file_and_creates_nothing(self):
        self.forbid_clients()
        before = snapshot_tree(self.dir)
        code, payload = self.invoke("image", "status")
        self.assertEqual((code, payload["decision"]), (0, "status"))
        self.assertEqual(snapshot_tree(self.dir), before)
        self.assertFalse(self.state_dir.exists())
        self.assertEqual(payload["state"], {"present": False})
        self.assertEqual((payload["active_build"], payload["last_build"], payload["candidate"]), (None, None, None))
        self.assertEqual(payload["pin"], {"version": PIN, "zone_pins": []})
        self.assertEqual(payload["rollback"]["available"], False)
        self.assertEqual(payload["rollback"]["reason"], "ROLLBACK_NO_PROMOTION")
        self.assertEqual(payload["pending_journal"], [])
        self.assertEqual(payload["blocked"], {"blocked": False})
        self.assertFalse(payload["check_recorded"])
        self.assertEqual(
            payload["retention"]["protected"], [{"role": "pin", "version": PIN, "image_id": None}]
        )
        self.assertEqual(
            payload["counters"],
            {
                "consecutive_failures": 0,
                "cooldown_until": None,
                "builds_last_day": 0,
                "max_builds_per_day": self.config.image.max_builds_per_day,
                "last_check_at": None,
            },
        )

    def test_status_makes_no_provider_call_builds_no_client_and_writes_only_the_check_time(self):
        self.build_candidate()
        self.set_control(consecutive_failures=2, cooldown_until=NOW + 99)
        self.forbid_clients()
        calls = len(self.world.calls)
        before = self.dump()
        self.clock.advance(120)
        code, payload = self.invoke("image", "status")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.world.calls), calls)
        self.assertTrue(payload["check_recorded"])
        after = self.dump()
        self.assertEqual(
            {name: rows for name, rows in after.items() if name != "control"},
            {name: rows for name, rows in before.items() if name != "control"},
        )
        self.assertEqual(
            {**before["control"][0], "last_check_at": self.clock()}, after["control"][0]
        )

    def test_status_reports_the_builds_the_candidate_and_the_counters(self):
        build = self.build_candidate()
        self.add_build("older-failure", "failed", age=5 * DAY, reason_code="WARM_FAILED")
        self.set_control(consecutive_failures=1, cooldown_until=NOW + 5000)
        self.clock.advance(100)
        code, payload = self.invoke("image", "status")
        self.assertEqual(code, 0)
        self.assertIsNone(payload["active_build"])
        self.assertEqual(payload["last_build"]["id"], build.id)
        candidate = payload["candidate"]
        self.assertEqual(candidate["build_id"], build.id)
        self.assertEqual(candidate["template_version"], 5)
        self.assertEqual(candidate["gate"], {"configured": True, "passed": True})
        self.assertEqual(candidate["verified_age_seconds"], 100)
        self.assertEqual(candidate["drift_result"], "clean")
        self.assertEqual(payload["counters"]["builds_last_day"], 1)
        self.assertEqual(payload["counters"]["consecutive_failures"], 1)
        self.assertEqual(payload["counters"]["cooldown_until"], NOW + 5000)

    def test_a_build_in_flight_is_reported_as_active(self):
        self.start_build()
        code, payload = self.invoke("image", "status")
        self.assertEqual(code, 0)
        self.assertEqual(payload["active_build"]["state"], "building")
        self.assertEqual(payload["last_build"]["state"], "building")

    def test_the_check_time_reported_is_the_one_before_this_status(self):
        self.build_candidate()
        recorded = self.control().last_check_at
        first = self.invoke("image", "status")[1]
        first_time = self.clock()
        self.clock.advance(60)
        second = self.invoke("image", "status")[1]
        self.assertEqual(first["counters"]["last_check_at"], recorded)
        self.assertEqual(second["counters"]["last_check_at"], first_time)
        self.assertEqual(self.control().last_check_at, self.clock())

    def test_a_blocked_control_row_says_how_to_clear_it(self):
        self.build_candidate()
        self.set_control(blocked=True)
        payload = self.invoke("image", "status")[1]
        self.assertEqual(payload["blocked"], image.blocked_explanation())
        self.assertTrue(payload["blocked"]["blocked"])
        self.assertIn("image run --apply --force", payload["blocked"]["clear_by"])

    def test_the_retention_set_comes_from_the_state_and_the_file(self):
        self.configure(
            zones=[
                {"zone": "eu-central-1a", "subnet_id": "subnet-0123456789abcdef0", "launch_template_version": 4},
                {"zone": "eu-central-1b", "subnet_id": "subnet-0123456789abcdef1"},
            ]
        )
        build = self.build_candidate()
        self.add_journal(old_pin=2, new_pin=PIN)
        self.add_pending(old_sha="0" * 64, new_sha="1" * 64, old_pin=PIN, new_pin=5)
        self.forbid_clients()
        payload = self.invoke("image", "status")[1]
        protected = payload["retention"]["protected"]
        roles = [(item["role"], item["version"]) for item in protected]
        self.assertEqual(
            roles,
            [
                ("pin", PIN),
                ("zone_pin", 4),
                ("previous_pin", 2),
                ("pending_journal", PIN),
                ("pending_journal", 5),
                ("candidate", 5),
            ],
        )
        candidate = next(item for item in protected if item["role"] == "candidate")
        self.assertEqual((candidate["image_id"], candidate["build_id"]), (build.image_id, build.id))
        self.assertEqual(payload["pin"]["zone_pins"], [{"zone_index": 0, "launch_template_version": 4}])
        self.assertEqual(payload["retention"]["retain"], self.config.image.retain)

    def test_a_status_that_cannot_record_the_check_still_reports(self):
        self.build_candidate()
        other = ImageState(self.state_dir, clock=self.clock)
        other.connect()
        other.acquire_writer()
        self.addCleanup(other.close)
        before = self.dump()
        code, payload = self.invoke("image", "status")
        self.assertEqual((code, payload["decision"], payload["check_recorded"]), (0, "status", False))
        self.assertEqual(self.dump(), before)

    def test_a_database_error_on_the_check_write_reports_check_recorded_false_and_exits_zero(self):
        self.build_candidate()
        before = self.dump()
        error = sqlite3.OperationalError("attempt to write a readonly database")
        with mock.patch.object(ImageState, "record_check_time", side_effect=error):
            code, payload = self.invoke("image", "status")
        self.assertEqual((code, payload["decision"], payload["check_recorded"]), (0, "status", False))
        self.assertNotIn("error", payload)
        self.assertEqual(self.dump(), before)

    def test_an_interruption_during_the_check_write_releases_the_writer_lock(self):
        self.build_candidate()
        with mock.patch.object(ImageState, "record_check_time", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.invoke("image", "status")
        other = ImageState(self.state_dir, clock=self.clock)
        self.addCleanup(other.close)
        other.acquire_writer()  # refuses IMAGE_STATE_LOCKED if the status had kept the lock

    def test_a_failure_to_release_the_state_after_the_check_write_still_reports_the_write(self):
        self.build_candidate()
        release = ImageState.release_writer
        failures = []

        def failing_release(state):
            held = state._lock_fd is not None
            release(state)
            if held and not failures:
                failures.append(True)
                raise OSError("synthetic release failure")

        self.clock.advance(60)
        with mock.patch.object(ImageState, "release_writer", failing_release):
            code, payload = self.invoke("image", "status")
        self.assertEqual(failures, [True])
        self.assertEqual((code, payload["decision"], payload["check_recorded"]), (0, "status", True))
        self.assertEqual(self.dump()["control"][0]["last_check_at"], self.clock())

    def test_a_pending_row_is_reported_with_the_resolution_its_hash_implies_and_is_not_resolved(self):
        self.pin_current()
        self.add_pending(old_sha=self.file_sha(), new_sha="1" * 64)
        self.add_pending(old_sha="0" * 64, new_sha=self.file_sha())
        self.add_pending(old_sha="0" * 64, new_sha="1" * 64)
        before = self.dump()
        code, payload = self.invoke("image", "status")
        self.assertEqual(code, 0)
        self.assertEqual(
            [(item["journal_id"], item["implied_resolution"]) for item in payload["pending_journal"]],
            [(1, "aborted"), (2, "complete"), (3, "diverged")],
        )
        self.assertEqual(self.table("promotion_journal"), before["promotion_journal"])
        self.assertTrue(all(row["status"] == "pending" for row in self.journal_rows()))
        self.assertEqual(self.table("image_builds"), before["image_builds"])

    def test_the_rollback_target_is_reported_after_a_promotion(self):
        build = self.build_candidate()
        self.assertEqual(self.invoke("image", "status", "--refresh")[0], 0)
        self.forbid_clients()
        self.assertEqual(self.invoke("image", "promote", "--write")[0], 0)
        payload = self.invoke("image", "status")[1]
        self.assertEqual(payload["pin"]["version"], 5)
        self.assertIsNone(payload["candidate"], "a promoted build is no longer a candidate")
        rollback = payload["rollback"]
        self.assertTrue(rollback["available"])
        self.assertEqual((rollback["to_pin"], rollback["image_id"]), (PIN, ami(0xA1)))
        self.assertRegex(rollback["backup"], r"^deployment\.toml\.bak\.\d{8}T\d{6}Z$")
        self.assertEqual(
            payload["retention"]["protected"][0], {"role": "pin", "version": 5, "image_id": build.image_id}
        )


# -- 6.1: status --refresh -----------------------------------------------------------


class RefreshTests(CliCase):
    def test_refresh_reads_describe_only_and_persists_only_the_evidence(self):
        build = self.build_candidate()
        before = self.dump()
        mark = len(self.world.calls)
        self.clock.advance(3 * 3600)
        code, payload = self.invoke("image", "status", "--refresh")
        self.assertEqual((code, payload["decision"]), (0, "refreshed"))
        self.assertTrue(set(self.world.names(mark)) <= DESCRIBES, self.world.names(mark))
        after = self.dump()
        old, new = before["image_builds"][0], after["image_builds"][0]
        self.assertEqual(new["id"], build.id)
        stable = {key: value for key, value in old.items() if key not in VERIFICATION}
        self.assertEqual({key: value for key, value in new.items() if key not in VERIFICATION}, stable)
        self.assertEqual(new["verified_at"], self.clock())
        self.assertEqual(
            (new["image_state"], new["template_version_exists"], new["drift_result"]), ("available", 1, "clean")
        )
        self.assertEqual([row["id"] for row in after["image_builds"]], [build.id, f"pin-{PIN}"])
        for name in ("pin_log", "promotion_journal", "control"):
            self.assertEqual(after[name], before[name], name)

    def test_refresh_names_what_it_refreshed(self):
        build = self.build_candidate()
        payload = self.invoke("image", "status", "--refresh")[1]
        self.assertEqual(
            [(item["build_id"], item["template_version"]) for item in payload["refreshed"]],
            [(f"pin-{PIN}", PIN), (build.id, 5)],
        )
        self.assertEqual(payload["pin_records_created"], [PIN])
        self.assertFalse(payload["pin_observed"])
        self.assertEqual(payload["missing_versions"], [])
        self.assertEqual(set(payload["refreshed"][0]), {"build_id", "template_version", *VERIFICATION})

    def test_refresh_observes_a_pin_that_a_hand_edit_moved(self):
        self.build_candidate()
        self.world.add_image(ami(0xA2), tags={}, age=DAY)
        self.world.set_version(WORKER, PIN, {"ImageId": ami(0xA2)})
        payload = self.invoke("image", "status", "--refresh")[1]
        self.assertTrue(payload["pin_observed"])
        observed = self.table("pin_log")[-1]
        self.assertEqual((observed["pin_version"], observed["image_id"]), (PIN, ami(0xA2)))

    def test_refresh_with_no_state_file_creates_it_and_records_the_pin(self):
        self.assertFalse(self.state_dir.exists())
        code, payload = self.invoke("image", "status", "--refresh")
        self.assertEqual(code, 0)
        self.assertEqual(payload["pin_records_created"], [PIN])
        self.assertTrue(payload["pin_observed"])
        self.assertEqual([(row["pin_version"], row["image_id"]) for row in self.table("pin_log")], [(PIN, ami(0xA1))])
        record = self.row(f"pin-{PIN}")
        self.assertEqual((record["image_state"], record["template_version_exists"]), ("available", 1))
        self.assertIsNone(record["drift_result"])

    def test_refresh_with_no_state_file_records_a_pin_that_the_account_does_not_list(self):
        self.assertFalse(self.state_dir.exists())
        del self.world.versions[(WORKER, PIN)]  # no image id to read, and no state file to look for an observed one in
        code, payload = self.invoke("image", "status", "--refresh")
        self.assertEqual((code, payload["pin_records_created"], payload["missing_versions"]), (0, [PIN], [PIN]))
        self.assertFalse(payload["pin_observed"])
        record = self.row(f"pin-{PIN}")
        found = (record["image_id"], record["image_state"], record["template_version_exists"])
        self.assertEqual(found, (None, None, 0))
        self.assertEqual(self.table("pin_log"), [])

    def test_refresh_records_a_missing_image(self):
        build = self.build_candidate()
        del self.world.images[build.image_id]
        self.invoke("image", "status", "--refresh")
        self.assertEqual(self.row(build.id)["image_state"], "missing")

    def assert_a_missing_version_is_recorded_as_absent(self):
        build = self.build_candidate()
        del self.world.versions[(WORKER, 5)]
        code, payload = self.invoke("image", "status", "--refresh")
        self.assertEqual((code, payload["missing_versions"]), (0, [5]))
        row = self.row(build.id)
        self.assertEqual((row["template_version_exists"], row["drift_result"]), (0, None))
        self.assertEqual(self.row(f"pin-{PIN}")["template_version_exists"], 1)

    def test_refresh_records_a_version_that_the_account_does_not_list(self):
        self.assert_a_missing_version_is_recorded_as_absent()

    def test_refresh_records_a_version_that_the_live_adapter_refuses_with_the_not_found_code(self):
        self.strict_missing = True
        self.assert_a_missing_version_is_recorded_as_absent()

    def test_a_refusal_with_another_code_is_not_taken_for_a_missing_version(self):
        self.strict_missing = True
        self.build_candidate()
        before = self.dump()

        def throttled(session, name, versions, *, deadline=None):
            raise ProviderRefusal("AWS_UNCERTAIN", "RequestLimitExceeded", uncertain=True)

        with mock.patch.object(StrictAws, "template_versions", throttled):
            code, payload = self.invoke("image", "status", "--refresh")
        self.assertEqual((code, payload["error"]["reason"]), (1, "AWS_UNCERTAIN"))
        self.assertEqual(self.dump(), before)

    def test_refresh_records_a_drifted_version(self):
        build = self.build_candidate()
        self.world.versions[(WORKER, 5)]["LaunchTemplateData"]["InstanceType"] = "m6i.large"
        self.invoke("image", "status", "--refresh")
        self.assertEqual(self.row(build.id)["drift_result"], "TEMPLATE_DRIFT")

    def test_a_refused_read_exits_one_and_persists_nothing(self):
        self.build_candidate()
        before = self.dump()
        for method in ("describe_images", "describe_launch_template_versions"):
            with self.subTest(method=method):
                self.world.fail(method, ProviderError("RequestLimitExceeded"))
                code, payload = self.invoke("image", "status", "--refresh")
                self.assertEqual((code, payload["error"]["reason"]), (1, "AWS_UNCERTAIN"))
                self.assertEqual(self.dump(), before)
                del self.world.errors[method]

    def test_a_refused_read_with_no_state_file_records_no_row(self):
        self.assertFalse(self.state_dir.exists())
        for method in ("describe_launch_template_versions", "describe_images"):
            with self.subTest(method=method):
                self.world.fail(method, ProviderError("RequestLimitExceeded"))
                try:
                    code, payload = self.invoke("image", "status", "--refresh")
                finally:
                    del self.world.errors[method]
                self.assertEqual((code, payload["error"]["reason"]), (1, "AWS_UNCERTAIN"))
                self.assertEqual(self.table("image_builds"), [])
        self.assertEqual(self.invoke("image", "status", "--refresh")[0], 0)  # the same command works once the reads do
        self.assertEqual([row["id"] for row in self.table("image_builds")], [f"pin-{PIN}"])

    def test_only_the_newest_retain_candidates_are_refreshed(self):
        self.configure(self.image_toml(image_extra="retain = 2\n"))
        aged = (("c-old", 4 * 3600), ("c-mid", 3 * 3600), ("c-new", 2 * 3600))
        for number, (name, age) in enumerate(aged, start=0xC1):
            self.add_world_candidate(name, number - 0xC1 + 6, number, age=age)
        payload = self.invoke("image", "status", "--refresh")[1]
        self.assertEqual([item["build_id"] for item in payload["refreshed"]], ["pin-3", "c-new", "c-mid"])
        self.assertEqual(self.row("c-old")["verified_at"], NOW - 4 * 3600 + 60)

    def test_a_refresh_with_a_stamp_for_the_rollback_target_reads_both_pins(self):
        self.build_candidate()
        self.invoke("image", "status", "--refresh")
        self.forbid_clients()
        self.assertEqual(self.invoke("image", "promote", "--write")[0], 0)
        self.allow_clients()
        self.clock.advance(3600)
        payload = self.invoke("image", "status", "--refresh")[1]
        self.assertEqual([item["template_version"] for item in payload["refreshed"]], [5, PIN])

    def test_the_refresh_changes_no_resource_of_the_account(self):
        self.build_candidate()
        mark = len(self.world.calls)
        self.invoke("image", "status", "--refresh")
        self.assertEqual(set(self.world.names(mark)) - DESCRIBES, set())
        self.assertEqual(self.world.templates[WORKER]["DefaultVersionNumber"], 1)

    def test_stale_record_then_refresh_then_a_successful_write_with_no_aws_client(self):
        self.build_candidate()
        built = len(self.sessions)
        before = self.config_path.read_bytes()
        self.clock.advance(3 * DAY)
        self.forbid_clients()
        code, payload = self.invoke("image", "promote", "--write")
        self.assertEqual((code, payload["error"]["reason"]), (2, "IMAGE_VERIFICATION_STALE"))
        self.assertEqual(self.config_path.read_bytes(), before)
        self.allow_clients()
        self.assertEqual(self.invoke("image", "status", "--refresh")[0], 0)
        self.forbid_clients()
        code, payload = self.invoke("image", "promote", "--write")
        self.assertEqual((code, payload["decision"]), (0, "promoted"))
        self.assertEqual(self.pin_in_file(), 5)
        self.assertEqual(len(self.sessions), built + 1, "only the refresh built a client")


# -- 6.7: the previous pin's image state is recorded ---------------------------------------


class PreviousPinTests(CliCase):
    def promote_and_roll_back(self):
        self.forbid_clients()
        code, payload = self.invoke("image", "promote", "--write")
        self.assertEqual((code, payload["decision"]), (0, "promoted"), payload)
        self.assertEqual(self.pin_in_file(), 5)
        self.clock.advance(60)
        code, payload = self.invoke("image", "promote", "--rollback", "--write")
        self.assertEqual((code, payload["decision"]), (0, "rolled_back"), payload)
        self.assertEqual(self.pin_in_file(), PIN)
        return payload

    def test_a_pin_observed_by_run_before_any_candidate_exists_is_a_usable_rollback_target(self):
        self.world.console_plan = [(100.0, WAITING), (300.0, READY)]
        self.assertEqual(self.invoke("image", "run", "--apply", "--until-done")[1]["decision"], "candidate")
        payload = self.promote_and_roll_back()
        self.assertEqual(payload["to_pin"], PIN)
        self.assertEqual(payload["target"], f"pin-{PIN}")

    def test_a_pin_observed_by_refresh_before_any_candidate_exists_is_a_usable_rollback_target(self):
        self.assertEqual(self.invoke("image", "status", "--refresh")[1]["pin_records_created"], [PIN])
        self.assertEqual(self.table("image_builds")[0]["id"], f"pin-{PIN}")
        self.build_candidate()
        self.promote_and_roll_back()

    def test_the_first_promotion_records_a_pin_that_no_command_stamped_but_a_rollback_needs_a_refresh(self):
        self.build_candidate()
        self.assertFalse(any(row["id"].startswith("pin-") for row in self.table("image_builds")))
        self.forbid_clients()
        self.assertEqual(self.invoke("image", "promote", "--write")[0], 0)
        record = self.row(f"pin-{PIN}")
        self.assertEqual((record["state"], record["image_id"]), ("promoted", ami(0xA1)))
        self.assertIsNone(record["image_state"])
        code, payload = self.invoke("image", "promote", "--rollback", "--write")
        self.assertEqual((code, payload["error"]["reason"]), (2, "ROLLBACK_PREVIOUS_NOT_RECORDED"))
        self.allow_clients()
        self.assertEqual(self.invoke("image", "status", "--refresh")[0], 0)
        self.forbid_clients()
        self.assertEqual(self.invoke("image", "promote", "--rollback", "--write")[1]["decision"], "rolled_back")

    def test_a_pin_record_changes_neither_what_the_pipeline_decides_nor_what_it_would_delete(self):
        self.world.add_image(ami(0xE1), tags=owned("orphan-one"), age=3 * DAY)
        self.query(lambda state: None)  # an empty state file, so both plans see a file
        before = self.invoke("image", "plan")[1]
        self.assertEqual(
            [(item["path"], item["image_id"], item["action"]) for item in before["deletion"]["items"]],
            [("orphan", ami(0xE1), "deregister")],
        )
        self.invoke("image", "status", "--refresh")
        self.assertEqual(self.row(f"pin-{PIN}")["reason_code"], PIN_RECORD_REASON)
        self.assertEqual(self.invoke("image", "plan")[1], before)
        self.pin_current()
        self.assertEqual(self.invoke("image", "run", "--apply")[1]["decision"], "digest_unchanged")
        self.assertIn(ami(0xA1), self.world.images, "the image behind the pin record is never taken")


if __name__ == "__main__":
    unittest.main()
