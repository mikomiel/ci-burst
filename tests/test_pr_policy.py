"""Behavioral contracts for the shared review and merge policy; no network."""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pr_policy as policy

BASE = "a" * 40
HEAD = "b" * 40
OTHER = "c" * 40


def review(state="APPROVED", head=HEAD, number=1, login="ecomwizards-gandalf[bot]", user_id=351676210, kind="Bot"):
    return {"id": number, "state": state, "commit_id": head,
            "user": {"id": user_id, "login": login, "type": kind}}


def pull(number=1, head=HEAD):
    return {"number": number, "state": "open", "draft": False,
            "user": {"login": "contributor"},
            "head": {"sha": head},
            "base": {"sha": BASE, "ref": "main", "repo": {"full_name": "mikomiel/ci-burst"}},
            "changed_files": 1, "mergeable": True, "mergeable_state": "clean"}


def release_pull(number=1, paths=None):
    """The generated release-please pull request shape, with its file list."""
    pr = pull(number)
    pr["user"] = {"login": policy.RELEASE_AUTHOR}
    pr["head"].update(ref=policy.RELEASE_BRANCH, repo={"full_name": policy.REPOSITORY})
    pr["release_paths"] = sorted(policy.RELEASE_PATHS) if paths is None else paths
    pr["reviews"] = []
    return pr


def checks():
    return [{"id": n, "name": name, "head_sha": HEAD, "app": {"id": 15368},
             "status": "completed", "conclusion": "success"}
            for n, name in enumerate(("planning", "public-content", "dependency-review"), 1)]


def statuses():
    return [{"id": 1, "context": "review", "state": "success",
             "creator": {"login": "github-actions[bot]", "type": "Bot"}}]


def comparison(path="README.md"):
    return {"base_commit": {"sha": BASE}, "merge_base_commit": {"sha": BASE},
            "status": "ahead", "files": [{"filename": path}]}


class ReviewTests(unittest.TestCase):
    def decide(self, items, **kwargs):
        return policy.review_verdict(items, HEAD, "contributor", **kwargs)[0]

    def test_exact_head_gandalf_approval(self):
        self.assertEqual(self.decide([review()]), "success")

    def test_other_commit_needs_review(self):
        self.assertEqual(self.decide([review(head=OTHER)]), "pending")

    def test_missing_review_needs_review(self):
        self.assertEqual(self.decide([]), "pending")

    def test_login_type_and_numeric_identity_are_all_required(self):
        for change in ({"login": "stranger"}, {"kind": "User"}, {"user_id": 8}):
            with self.subTest(change=change):
                self.assertEqual(self.decide([review(**change)]), "pending")

    def test_author_cannot_supply_approval(self):
        state, _ = policy.review_verdict([review()], HEAD, "ecomwizards-gandalf[bot]")
        self.assertEqual(state, "pending")

    def test_comment_does_not_erase_decisive_approval(self):
        self.assertEqual(self.decide([review(), review("COMMENTED", number=2)]), "success")

    def test_dismissal_revokes_approval(self):
        self.assertEqual(self.decide([review(), review("DISMISSED", number=2)]), "pending")

    def test_change_request_blocks_even_on_older_commit(self):
        self.assertEqual(self.decide([review("CHANGES_REQUESTED", head=OTHER)]), "failure")

    def test_one_reviewer_cannot_erase_anothers_change_request(self):
        human = review("CHANGES_REQUESTED", number=2, login="maintainer", user_id=9, kind="User")
        self.assertEqual(self.decide([human, review(number=3)], maintainers={9}), "failure")

    def test_verified_maintainer_can_approve(self):
        human = review(login="maintainer", user_id=9, kind="User")
        self.assertEqual(self.decide([human], maintainers={9}), "success")
        self.assertEqual(self.decide([human]), "pending")

    def test_no_dependency_author_exemption(self):
        state, _ = policy.review_verdict([], HEAD, "dependabot[bot]")
        self.assertEqual(state, "pending")

    def test_malformed_input_is_not_approval(self):
        for value in (None, {}, [{"state": "APPROVED"}], [review("UNKNOWN")]):
            with self.subTest(value=value):
                with self.assertRaises(policy.Unusable):
                    policy.review_verdict(value, HEAD, "contributor")

    def test_shared_head_uses_worst_pr_verdict(self):
        good, bad = pull(1), pull(2)
        good["reviews"], bad["reviews"] = [review()], [review("CHANGES_REQUESTED")]
        plans = policy.review_plan([good, bad])
        self.assertEqual(plans[HEAD][0], "failure")

    def test_draft_revokes_a_shared_head_success(self):
        good, draft = pull(1), pull(2)
        good["reviews"] = draft["reviews"] = [review()]
        draft["draft"] = True
        self.assertEqual(policy.review_plan([good, draft])[HEAD][0], "pending")


class ReleaseTests(unittest.TestCase):
    """The one review exemption is the generated release PR, and only its shape."""

    def plan(self, pr):
        return policy.review_plan([pr])[HEAD]

    def test_generated_release_pr_needs_no_independent_review(self):
        self.assertEqual(self.plan(release_pull()), ("success", "automated_release"))
        self.assertEqual(
            self.plan(release_pull(paths=["CHANGELOG.md"])),
            ("success", "automated_release"),
        )

    def test_a_change_request_still_blocks_the_release_pr(self):
        pr = release_pull()
        pr["reviews"] = [review("CHANGES_REQUESTED")]
        self.assertEqual(self.plan(pr), ("failure", "changes_requested"))

    def test_the_recognised_shape_matches_the_generated_release_pull_request(self):
        # Captured from pull request #102, the first generated 0.3.0 release PR.
        # The literals are deliberate: a drift in a policy constant fails here.
        pr = pull()
        pr["user"] = {"login": "mikomiel"}
        pr["head"].update(
            ref="release-please--branches--main--components--ci-burst",
            repo={"full_name": "mikomiel/ci-burst"},
        )
        pr["release_paths"] = [
            ".release-please-manifest.json",
            "CHANGELOG.md",
            "ci_burst/__init__.py",
            "pyproject.toml",
        ]
        pr["reviews"] = []
        self.assertEqual(self.plan(pr), ("success", "automated_release"))

    def test_every_other_shape_keeps_the_review_requirement(self):
        cases = {
            "wrong branch": lambda pr: pr["head"].update(
                ref="release-please--branches--main--components--other"),
            "fork head": lambda pr: pr["head"].update(repo={"full_name": "someone/ci-burst"}),
            "wrong author": lambda pr: pr.update(user={"login": "contributor"}),
            "no file list": lambda pr: pr.pop("release_paths"),
            "empty file list": lambda pr: pr.update(release_paths=[]),
            "unexpected path": lambda pr: pr.update(
                release_paths=["CHANGELOG.md", ".github/workflows/ci.yml"]),
            "draft": lambda pr: pr.update(draft=True),
        }
        for label, change in cases.items():
            with self.subTest(label):
                pr = release_pull()
                change(pr)
                self.assertEqual(self.plan(pr)[0], "pending")

    def test_missing_head_repository_is_not_a_release_candidate(self):
        self.assertFalse(policy.release_candidate(pull()))

    def test_automerge_still_refuses_a_release_pull_request(self):
        pr = release_pull()
        self.assertEqual(
            policy.merge_decision(pr, [], checks(), statuses(), comparison("CHANGELOG.md")),
            (False, "current_head_approval_required"),
        )
        self.assertEqual(
            policy.merge_decision(pr, [review()], checks(), statuses(), comparison("pyproject.toml")),
            (False, "sensitive_paths"),
        )


class CheckTests(unittest.TestCase):
    def test_both_required_checks_and_trusted_review_status(self):
        self.assertTrue(policy.checks_green(checks(), statuses(), HEAD)[0])

    def test_missing_failed_or_pending_required_check_refuses(self):
        missing = checks()[:1]
        failed = checks()
        failed[0]["conclusion"] = "failure"
        pending = checks()
        pending[0].update(status="in_progress", conclusion=None)
        for runs in (missing, failed, pending):
            with self.subTest(runs=runs):
                self.assertFalse(policy.checks_green(runs, statuses(), HEAD)[0])

    def test_skipped_dependency_review_refuses(self):
        runs = checks()
        runs[2].update(conclusion="skipped")
        self.assertFalse(policy.checks_green(runs, statuses(), HEAD)[0])

    def test_required_check_cannot_come_from_another_app_or_head(self):
        for key, value in (("app", {"id": 7}), ("head_sha", OTHER)):
            runs = checks()
            runs[0][key] = value
            self.assertFalse(policy.checks_green(runs, statuses(), HEAD)[0])

    def test_newer_failed_run_wins_over_old_success(self):
        runs = checks()
        failed = copy.deepcopy(runs[0])
        failed.update(id=10, conclusion="failure")
        self.assertFalse(policy.checks_green(runs + [failed], statuses(), HEAD)[0])

    def test_absent_or_revoked_review_context_refuses(self):
        self.assertFalse(policy.checks_green(checks(), [], HEAD)[0])
        revoked = statuses()
        revoked[0]["state"] = "pending"
        self.assertFalse(policy.checks_green(checks(), revoked, HEAD)[0])

    def test_author_status_cannot_impersonate_trusted_gate(self):
        forged = statuses()
        forged[0]["creator"] = {"login": "contributor", "type": "User"}
        self.assertFalse(policy.checks_green(checks(), forged, HEAD)[0])

    def test_latest_status_and_additional_failing_contexts_are_respected(self):
        old, new = statuses()[0], statuses()[0]
        new.update(id=3, state="failure")
        self.assertFalse(policy.checks_green(checks(), [old, new], HEAD)[0])
        extra = dict(old, id=4, context="other", state="pending")
        self.assertFalse(policy.checks_green(checks(), [old, extra], HEAD)[0])


class MergeTests(unittest.TestCase):
    def decide(self, pr=None, reviews=None, runs=None, states=None, compare=None):
        return policy.merge_decision(
            pull() if pr is None else pr,
            [review()] if reviews is None else reviews,
            checks() if runs is None else runs,
            statuses() if states is None else states,
            comparison() if compare is None else compare,
        )

    def test_ordinary_reviewed_docs_are_eligible(self):
        self.assertEqual(self.decide(), (True, "eligible"))

    def test_does_not_depend_on_native_graphql_review_decision(self):
        pr = pull()
        pr["reviewDecision"] = ""
        self.assertEqual(self.decide(pr=pr), (True, "eligible"))

    def test_closed_draft_or_unknown_mergeability_refuses(self):
        for change in ({"state": "closed"}, {"draft": True}, {"mergeable": None}, {"mergeable_state": "blocked"}):
            pr = pull()
            pr.update(change)
            self.assertFalse(self.decide(pr=pr)[0])

    def test_foreign_repository_or_base_branch_refuses(self):
        for key, value in (("ref", "other"), ("repo", {"full_name": "example/project"})):
            pr = pull()
            pr["base"][key] = value
            self.assertFalse(self.decide(pr=pr)[0])

    def test_no_review_or_missing_check_cannot_merge(self):
        self.assertFalse(self.decide(reviews=[])[0])
        self.assertFalse(self.decide(runs=[])[0])

    def test_sensitive_paths_always_need_a_person(self):
        paths = (".github/workflows/ci.yml", ".github/workflows/release-please.yml",
                 "scripts/pr_policy.py", "tests/test_pr_policy.py",
                 "AGENTS.md", "component/AGENTS.md", "CONTRIBUTING.md", "SECURITY.md",
                 "package.json", "package-lock.json", "pyproject.toml", "uv.lock",
                 "release-please-config.json", ".release-please-manifest.json",
                 "tools/package.json", "component/pyproject.toml", "component/Dockerfile",
                 "Dockerfile", ".gitleaks.toml", ".gitignore", "openspec/config.yaml")
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.decide(compare=comparison(path))[1], "sensitive_paths")

    def test_a_release_pull_request_stays_human_despite_its_ordinary_changelog(self):
        pr = pull()
        pr["changed_files"] = 2
        compare = comparison("CHANGELOG.md")
        compare["files"].append({"filename": ".release-please-manifest.json"})
        self.assertEqual(self.decide(pr=pr, compare=compare), (False, "sensitive_paths"))

    def test_unknown_dependency_packaging_and_script_paths_need_a_person(self):
        paths = (".npmrc", "requirements.in", "requirements-prod.txt", "Pipfile", "MANIFEST.in",
                 "Dockerfile.worker", "tools/check.sh", "helper.py", "docs/check.sh", "docs/.npmrc",
                 "docs/AGENTS.md", "docs/AGENTS.override.md", "docs/design.md", "new-area/script.py",
                 "openspec/changes/queue-burst/AGENTS.override.md",
                 "openspec/changes/queue-burst/specs/queue/AGENTS.override.md",
                 "openspec/changes/queue-burst/unknown.md")
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.decide(compare=comparison(path))[1], "sensitive_paths")

    def test_only_explicit_ordinary_documentation_paths_are_autoeligible_initially(self):
        paths = ("README.md", "CHANGELOG.md", "openspec/changes/queue-burst/proposal.md",
                 "openspec/changes/queue-burst/design.md", "openspec/changes/queue-burst/tasks.md",
                 "openspec/changes/queue-burst/.openspec.yaml",
                 "openspec/changes/queue-burst/specs/queue/spec.md")
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(self.decide(compare=comparison(path)), (True, "eligible"))

    def test_manual_maintainer_fallback_does_not_replace_gandalf_for_automerge(self):
        human = review(login="maintainer", user_id=9, kind="User")
        state, _ = policy.review_verdict([human], HEAD, "contributor", maintainers={9})
        self.assertEqual(state, "success")
        allowed, reason = policy.merge_decision(pull(), [human], checks(), statuses(), comparison(), maintainers={9})
        self.assertFalse(allowed)
        self.assertEqual(reason, "gandalf_required_for_automerge")

    def test_both_sides_of_rename_are_guarded(self):
        c = comparison("README.md")
        c["files"][0]["previous_filename"] = ".github/workflows/ci.yml"
        self.assertEqual(self.decide(compare=c)[1], "sensitive_paths")

    def test_incomplete_comparison_and_stale_base_refuse(self):
        pr = pull()
        pr["changed_files"] = 2
        self.assertFalse(self.decide(pr=pr)[0])
        for key in ("base_commit", "merge_base_commit"):
            c = comparison()
            c[key]["sha"] = OTHER
            self.assertFalse(self.decide(compare=c)[0])

    def test_ref_change_or_close_aborts_final_recheck(self):
        before = pull()
        self.assertTrue(policy.same_refs(before, pull()))
        draft = pull()
        draft["draft"] = True
        self.assertTrue(policy.same_refs(draft, copy.deepcopy(draft)))
        self.assertFalse(policy.same_refs(before, draft))
        for modify in (lambda p: p["head"].update(sha=OTHER),
                       lambda p: p["base"].update(sha=OTHER),
                       lambda p: p.update(state="closed")):
            after = pull()
            modify(after)
            self.assertFalse(policy.same_refs(before, after))


if __name__ == "__main__":
    unittest.main()
