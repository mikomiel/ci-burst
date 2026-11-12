"""Exercise orchestration at the GitHub boundary, never against real writes."""

import contextlib
import copy
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import github_pr_automation as driver
from test_pr_policy import (
    BASE, HEAD, OTHER, checks, comparison, pull, release_pull, review, statuses,
)


class FakeGitHub:
    def __init__(self):
        self.prs = [pull()]
        self.published = []
        self.merged = []
        self.pull_reads = 0
        self.review_reads = 0
        self.check_reads = 0
        self.file_reads = 0
        self.files = [{"filename": path, "status": "modified"}
                      for path in sorted(driver.policy.RELEASE_PATHS)]
        self.fail_files = False
        self.change_head = False
        self.withdraw = False
        self.fail_check = False
        self.fail_review_number = None
        self.branch = BASE
        self.path = "README.md"
        self.fail_listing = False

    def open_pulls(self):
        if self.fail_listing:
            raise driver.ApiError("listing_unavailable")
        return copy.deepcopy(self.prs)

    def pull(self, number):
        self.pull_reads += 1
        result = copy.deepcopy(next(p for p in self.prs if p["number"] == number))
        if self.change_head and self.pull_reads > len(self.prs):
            result["head"]["sha"] = OTHER
        return result

    def pull_files(self, number):
        self.file_reads += 1
        if self.fail_files:
            raise driver.ApiError("pull_files_unavailable")
        return copy.deepcopy(self.files)

    def reviews(self, number):
        self.review_reads += 1
        if number == self.fail_review_number:
            raise driver.ApiError("reviews_unavailable")
        if self.withdraw and self.review_reads > len(self.prs):
            return [review("DISMISSED")]
        return [review()]

    def maintainer_ids(self, reviews, author):
        return set()

    def check_runs(self, head):
        self.check_reads += 1
        result = checks()
        if self.fail_check and self.check_reads > 1:
            result[0]["conclusion"] = "failure"
        return result

    def statuses(self, head):
        return statuses()

    def compare(self, base, head):
        return comparison(self.path)

    def branch_head(self):
        return self.branch

    def publish(self, head, state, reason):
        self.published.append((head, state, reason))

    def merge(self, number, head):
        self.merged.append((number, head))


class GateTests(unittest.TestCase):
    def test_dry_run_decides_without_publishing(self):
        api = FakeGitHub()
        result = driver.run_review(api)
        self.assertEqual(result[HEAD][0], "success")
        self.assertEqual(api.published, [])

    def test_apply_publishes_exact_head_decision(self):
        api = FakeGitHub()
        driver.run_review(api, apply=True)
        self.assertEqual(api.published, [(HEAD, "success", "approved_at_head")])

    def test_generated_release_pr_is_published_without_review(self):
        api = FakeGitHub()
        api.prs = [release_pull()]
        result = driver.run_review(api, apply=True)
        self.assertEqual(result[HEAD], ("success", "automated_release"))
        self.assertEqual(api.published, [(HEAD, "success", "automated_release")])
        self.assertEqual(api.file_reads, 1)

    def test_ordinary_pr_does_not_read_a_file_list(self):
        api = FakeGitHub()
        driver.run_review(api)
        self.assertEqual(api.file_reads, 0)

    def test_release_shaped_pr_with_an_unexpected_path_keeps_the_review(self):
        api = FakeGitHub()
        api.prs = [release_pull()]
        api.files = [{"filename": "CHANGELOG.md", "status": "modified"},
                     {"filename": ".github/workflows/ci.yml", "status": "modified"}]
        with mock.patch.object(api, "reviews", return_value=[]):
            result = driver.run_review(api, apply=True)
        self.assertEqual(result[HEAD], ("pending", "current_head_approval_required"))
        self.assertEqual(api.published, [(HEAD, "pending", "current_head_approval_required")])

    def test_release_shaped_pr_with_a_deletion_or_rename_keeps_the_review(self):
        api = FakeGitHub()
        api.prs = [release_pull()]
        for status in ("removed", "renamed", "added"):
            with self.subTest(status=status):
                api.published.clear()
                api.files = [{"filename": "pyproject.toml", "status": status}]
                with mock.patch.object(api, "reviews", return_value=[]):
                    result = driver.run_review(api, apply=True)
                self.assertEqual(result[HEAD], ("pending", "current_head_approval_required"))

    def test_unreadable_release_file_list_fails_the_publication_closed(self):
        api = FakeGitHub()
        api.prs = [release_pull()]
        api.fail_files = True
        with self.assertRaises(driver.ApiError):
            driver.run_review(api, apply=True)
        self.assertEqual([item[:2] for item in api.published], [(HEAD, "pending")])

    def test_closed_event_head_is_revoked(self):
        api = FakeGitHub()
        api.prs = []
        result = driver.run_review(api, event_head=HEAD, apply=True)
        self.assertEqual(result[HEAD][0], "pending")
        self.assertEqual(api.published[0][:2], (HEAD, "pending"))

    def test_unchanged_draft_does_not_poison_a_ready_pr_on_another_head(self):
        api = FakeGitHub()
        draft = pull(1, OTHER)
        draft["draft"] = True
        api.prs = [draft, pull(2)]
        result = driver.run_review(api, apply=True)
        self.assertEqual(result[OTHER][0], "pending")
        self.assertEqual(result[HEAD][0], "success")
        self.assertEqual({x[:2] for x in api.published}, {(OTHER, "pending"), (HEAD, "success")})

    def test_publication_failure_does_not_suppress_other_known_revocations(self):
        api = FakeGitHub()
        api.prs = [pull(1), pull(2, OTHER)]
        attempts = []

        def publish(head, state, reason):
            attempts.append((head, state))
            if head == HEAD:
                raise driver.ApiError("one_head_write_failed")
            api.published.append((head, state, reason))

        with mock.patch.object(api, "reviews", return_value=[]), mock.patch.object(api, "publish", side_effect=publish):
            with self.assertRaises(driver.ApiError):
                driver.run_review(api, apply=True)
        self.assertEqual(attempts, [(HEAD, "pending"), (OTHER, "pending")])
        self.assertEqual([x[:2] for x in api.published], [(OTHER, "pending")])

    def test_negative_publications_precede_new_successes(self):
        api = FakeGitHub()
        api.prs = [pull(1), pull(2, OTHER)]
        driver.run_review(api, apply=True)
        self.assertEqual([x[:2] for x in api.published], [(OTHER, "pending"), (HEAD, "success")])

    def test_failed_negative_publication_does_not_publish_new_success(self):
        api = FakeGitHub()
        api.prs = [pull(1), pull(2, OTHER)]
        with mock.patch.object(api, "publish", side_effect=driver.ApiError("write_failed")) as publish:
            with self.assertRaises(driver.ApiError):
                driver.run_review(api, apply=True)
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(publish.call_args.args[:2], (OTHER, "pending"))

    def test_failed_snapshot_revokes_all_known_heads(self):
        api = FakeGitHub()
        api.prs = [pull(1), pull(2, OTHER)]
        api.fail_review_number = 2
        with self.assertRaises(driver.ApiError):
            driver.run_review(api, apply=True)
        self.assertEqual({x[:2] for x in api.published}, {(HEAD, "pending"), (OTHER, "pending")})

    def test_failed_enumeration_can_revoke_event_head_without_guessing_others(self):
        api = FakeGitHub()
        api.fail_listing = True
        with self.assertRaises(driver.ApiError):
            driver.run_review(api, event_head=HEAD, apply=True)
        self.assertEqual([x[:2] for x in api.published], [(HEAD, "pending")])


class MergerTests(unittest.TestCase):
    def test_dry_run_has_no_merge_side_effect(self):
        api = FakeGitHub()
        result = driver.run_merge(api)
        self.assertTrue(result[0]["eligible"])
        self.assertEqual(api.merged, [])

    def test_apply_merges_one_eligible_exact_head(self):
        api = FakeGitHub()
        result = driver.run_merge(api, apply=True)
        self.assertEqual(api.merged, [(1, HEAD)])
        self.assertTrue(result[0]["merged"])

    def test_sensitive_policy_pr_is_never_automerged(self):
        api = FakeGitHub()
        api.path = ".github/workflows/automerge.yml"
        result = driver.run_merge(api, apply=True)
        self.assertEqual(result[0]["reason"], "sensitive_paths")
        self.assertEqual(api.merged, [])

    def test_moved_refs_withdrawn_review_new_failure_or_advanced_base_abort(self):
        for attribute, value in (("change_head", True), ("withdraw", True), ("fail_check", True), ("branch", OTHER)):
            with self.subTest(attribute=attribute):
                api = FakeGitHub()
                setattr(api, attribute, value)
                result = driver.run_merge(api, apply=True)
                self.assertFalse(result[0]["eligible"])
                self.assertEqual(api.merged, [])

    def test_shared_head_is_left_for_a_person(self):
        api = FakeGitHub()
        api.prs = [pull(1), pull(2)]
        result = driver.run_merge(api, apply=True)
        self.assertTrue(all(x["reason"] == "shared_head" for x in result))
        self.assertEqual(api.merged, [])

    def test_unreadable_reviews_do_not_merge(self):
        api = FakeGitHub()
        api.fail_review_number = 1
        with self.assertRaises(driver.ApiError):
            driver.run_merge(api, apply=True)
        self.assertEqual(api.merged, [])


class ContextTests(unittest.TestCase):
    def test_apply_is_refused_outside_trusted_default_branch_workflow(self):
        contexts = ({}, {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "pull_request"},
                    {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch"})
        for env in contexts:
            with self.subTest(env=env), mock.patch.dict(os.environ, env, clear=True):
                with mock.patch.object(driver, "GitHub") as constructor, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(driver.main(["merge", "--apply"]), 2)
                    constructor.assert_not_called()

    def test_verified_main_workflow_can_apply_but_other_refs_cannot(self):
        env = {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "schedule",
               "GITHUB_REPOSITORY": "mikomiel/ci-burst", "GITHUB_REF": "refs/heads/main",
               "GITHUB_WORKFLOW_REF": "mikomiel/ci-burst/.github/workflows/review-gate.yml@refs/heads/main"}
        api = FakeGitHub()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(driver, "GitHub", return_value=api):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(driver.main(["review", "--apply"]), 0)
        self.assertEqual(api.published, [(HEAD, "success", "approved_at_head")])
        for change in ({"GITHUB_REF": "refs/heads/feature"}, {"GITHUB_REPOSITORY": "example/project"},
                       {"GITHUB_WORKFLOW_REF": "mikomiel/ci-burst/.github/workflows/review-gate.yml@refs/heads/feature"}):
            with mock.patch.dict(os.environ, dict(env, **change), clear=True):
                with mock.patch.object(driver, "GitHub") as constructor, contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(driver.main(["review", "--apply"]), 2)
                    constructor.assert_not_called()

    def test_local_observation_is_allowed_but_read_only(self):
        api = FakeGitHub()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(driver, "GitHub", return_value=api):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(driver.main(["review"]), 0)
        self.assertEqual(api.published, [])
        self.assertEqual(api.merged, [])

    def test_transport_rejects_write_when_not_apply(self):
        api = driver.GitHub(apply=False)
        with mock.patch.object(driver.subprocess, "run") as run:
            with self.assertRaises(driver.ApiError):
                api.request("statuses/" + HEAD, "POST", {"state": "success"})
            run.assert_not_called()


class TransportTests(unittest.TestCase):
    def test_metadata_transport_is_pinned_and_does_not_echo_provider_errors(self):
        api = driver.GitHub()
        response = mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")
        with mock.patch.object(driver.subprocess, "run", return_value=response) as run:
            self.assertEqual(api.request("pulls/1"), {"ok": True})
            args = run.call_args.args[0]
            self.assertIn("github.com", args)
            self.assertIn("repos/mikomiel/ci-burst/pulls/1", args)
            self.assertEqual(run.call_args.kwargs["timeout"], 30)
        response.returncode, response.stderr = 1, "sensitive-provider-debug"
        with mock.patch.object(driver.subprocess, "run", return_value=response):
            with self.assertRaises(driver.ApiError) as error:
                api.request("pulls/1")
        self.assertNotIn("sensitive-provider-debug", str(error.exception))

    def test_read_pagination_is_complete_or_refused(self):
        api = driver.GitHub()
        first = [{"id": n} for n in range(100)]
        with mock.patch.object(api, "request", side_effect=[first, [{"id": 100}]]) as request:
            self.assertEqual(len(api.pages("pulls?state=open")), 101)
            self.assertIn("page=2", request.call_args.args[0])
        with mock.patch.object(driver, "MAX_PAGES", 2), mock.patch.object(api, "request", return_value=first) as request:
            with self.assertRaises(driver.ApiError):
                api.pages("pulls")
            self.assertEqual(request.call_count, 2)
        malformed = {"total_count": 2, "check_runs": [{"id": 1}]}
        with mock.patch.object(api, "request", return_value=malformed):
            with self.assertRaises(driver.ApiError):
                api.pages("checks", "check_runs")
        with mock.patch.object(api, "request", return_value=["not an object"]):
            with self.assertRaises(driver.ApiError):
                api.pages("pulls")

    def test_review_status_creator_is_read_from_the_status_list(self):
        # The combined status document omits each status creator, so the
        # trusted-status check must read the status list instead.
        api = driver.GitHub()
        with mock.patch.object(api, "request", return_value=statuses()) as request:
            self.assertEqual(api.statuses(HEAD), statuses())
        self.assertEqual(request.call_args.args[0], f"commits/{HEAD}/statuses?per_page=100&page=1")

    def test_association_alone_does_not_grant_maintainer_authority(self):
        api = driver.GitHub()
        human = dict(review(login="maintainer", user_id=9, kind="User"), author_association="COLLABORATOR")
        for permission, expected in (("read", set()), ("write", {9}), ("admin", {9})):
            with mock.patch.object(api, "request", return_value={"permission": permission, "user": {"id": 9}}):
                self.assertEqual(api.maintainer_ids([human], "contributor"), expected)
        with mock.patch.object(api, "request", return_value={"permission": "write", "user": {"id": 10}}):
            with self.assertRaises(driver.ApiError):
                api.maintainer_ids([human], "contributor")

    def test_unchanged_official_status_is_not_republished(self):
        api = driver.GitHub(apply=True)
        previous = statuses()[0]
        previous["description"] = "approved_at_head at bbbbbbbbbbbb"
        with mock.patch.object(api, "statuses", return_value=[previous]), mock.patch.object(api, "request") as request:
            api.publish(HEAD, "success", "approved_at_head")
            request.assert_not_called()
        previous["creator"] = {"login": "contributor", "type": "User"}
        with mock.patch.object(api, "statuses", return_value=[previous]), mock.patch.object(api, "request") as request:
            api.publish(HEAD, "success", "approved_at_head")
            self.assertEqual(request.call_args.args[:2], ("statuses/" + HEAD, "POST"))

    def test_merge_request_is_head_pinned_and_remote_postcondition_is_checked(self):
        api = driver.GitHub(apply=True)
        closed = pull()
        closed.update(state="closed", merged=True, merge_commit_sha=OTHER)
        result = {"merged": True, "sha": OTHER}
        with mock.patch.object(api, "request", side_effect=[result, closed]) as request:
            api.merge(1, HEAD)
            self.assertEqual(request.call_args_list[0].args,
                             ("pulls/1/merge", "PUT", {"sha": HEAD, "merge_method": "squash"}))
            self.assertEqual(request.call_count, 2)
        closed["head"]["sha"] = BASE
        with mock.patch.object(api, "request", side_effect=[result, closed]) as request:
            with self.assertRaises(driver.ApiError):
                api.merge(1, HEAD)
            self.assertEqual(request.call_count, 2)

    def test_uncertain_or_refused_merge_is_never_retried(self):
        api = driver.GitHub(apply=True)
        for response in ({"merged": False}, driver.ApiError("uncertain_write")):
            with mock.patch.object(api, "request", side_effect=[response]) as request:
                with self.assertRaises(driver.ApiError):
                    api.merge(1, HEAD)
                self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
