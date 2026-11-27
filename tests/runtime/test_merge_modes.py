"""Merge-time freshness per repo: detection on any GitHub plan, status carry-over, the harness mode."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ts_harness.integrations.github_client import GitHubError
from ts_harness.workflows import merge_modes
from ts_harness.workflows.ticket_story import merge_mode_note

PR = "https://github.com/org/app/pull/5"


def _github(rules=None, protection=None, rules_error=None, protection_error=None):
    github = MagicMock()
    github.host = "github.com"
    github.request.side_effect = rules_error or (lambda *_a, **_k: rules)
    if protection_error:
        github.branch_protection.side_effect = protection_error
    else:
        github.branch_protection.return_value = protection
    return github


class DetectTests(unittest.TestCase):
    def setUp(self):
        merge_modes._cache.clear()

    def detect(self, github, defaults=None):
        return merge_modes.detect(github, "org", "app", "dev", program_defaults=defaults or {}, now=1000)

    def test_a_merge_queue_ruleset(self):
        out = self.detect(_github(rules=[{"type": "merge_queue"}, {"type": "pull_request"}]))
        self.assertEqual((out["mode"], out["source"], out["enforced"]), ("merge_queue", "rules", True))

    def test_a_strict_status_check_ruleset_requires_up_to_date(self):
        rules = [{"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": True}}]
        self.assertEqual(self.detect(_github(rules=rules))["mode"], "require_up_to_date")

    def test_classic_strict_protection(self):
        out = self.detect(_github(rules=[], protection={"required_status_checks": {"strict": True}}))
        self.assertEqual((out["mode"], out["source"]), ("require_up_to_date", "protection"))

    def test_a_plan_without_rules_or_protection_falls_back_to_the_harness(self):
        github = _github(rules_error=GitHubError(404, "Not Found"), protection_error=GitHubError(403, "Upgrade to GitHub Pro"))
        out = self.detect(github)
        self.assertEqual((out["mode"], out["source"], out["enforced"]), ("harness", "fallback", False))
        self.assertIn("protection: HTTP 403", out["notes"])
        self.assertIn("not blocked", out["description"])

    def test_unprotected_branch_is_the_harness_mode(self):
        self.assertEqual(self.detect(_github(rules=[], protection=None))["mode"], "harness")

    def test_the_result_is_cached_per_repo_and_branch(self):
        github = _github(rules=[{"type": "merge_queue"}])
        self.detect(github)
        self.detect(github)
        self.assertEqual(github.request.call_count, 1)

    def test_an_override_wins_and_reads_nothing(self):
        github = _github(rules=[{"type": "merge_queue"}])
        out = self.detect(github, {"merge_readiness": {"merge_mode": {"org/app": "harness", "default": "auto"}}})
        self.assertEqual((out["mode"], out["source"]), ("harness", "override"))
        github.request.assert_not_called()

    def test_an_unknown_override_detects(self):
        out = self.detect(_github(rules=[{"type": "merge_queue"}]), {"merge_readiness": {"merge_mode": "sometimes"}})
        self.assertEqual(out["mode"], "merge_queue")


class CarryTests(unittest.TestCase):
    FILES = [{"filename": "src/a.ts", "status": "modified", "patch": "@@ -10,2 +10,3 @@\n a\n+b\n", "sha": "x1"}]

    def _github(self, files):
        github = MagicMock()
        github.list_pull_files.return_value = files
        github.combined_status.return_value = {"statuses": [
            {"context": "harness/code-review", "state": "success", "description": "No blocking findings"},
            {"context": "harness/qa", "state": "success", "description": "QA passed"},
            {"context": "ci/build", "state": "success"},
        ]}
        return github

    def test_statuses_follow_a_base_only_update_even_when_hunks_moved(self):
        reviewed = merge_modes.change_identity(self.FILES, PR)
        moved = [{**self.FILES[0], "patch": "@@ -40,2 +40,3 @@\n a\n+b\n", "sha": "x2"}]
        github = self._github(moved)
        copied = merge_modes.carry_statuses(github, PR, before_sha="old", after_sha="new", reviewed_identity=reviewed)
        self.assertEqual(copied, ["harness/code-review", "harness/qa"])
        kwargs = github.create_status.call_args_list[0].kwargs
        self.assertEqual((kwargs["state"], kwargs["context"]), ("success", "harness/code-review"))
        self.assertIn("kept after a base update", kwargs["description"])
        self.assertEqual(github.create_status.call_args_list[0].args[2], "new")

    def test_nothing_is_carried_when_the_pr_itself_changed(self):
        reviewed = merge_modes.change_identity(self.FILES, PR)
        github = self._github([{**self.FILES[0], "patch": "@@ -10,2 +10,3 @@\n a\n+c\n"}])
        self.assertEqual(merge_modes.carry_statuses(github, PR, before_sha="old", after_sha="new", reviewed_identity=reviewed), [])
        github.create_status.assert_not_called()

    def test_only_the_pr_s_own_entries_are_compared(self):
        other = merge_modes.change_identity([{"filename": "api.ts", "status": "added", "patch": "+x"}], "https://github.com/org/api/pull/2")
        reviewed = merge_modes.change_identity(self.FILES, PR) + other
        copied = merge_modes.carry_statuses(self._github(self.FILES), PR, before_sha="o", after_sha="n", reviewed_identity=reviewed)
        self.assertEqual(len(copied), 2)


class UpdateWebhookTests(unittest.TestCase):
    def test_update_branch_on_a_waiting_pr_keeps_its_harness_statuses(self):
        files = CarryTests.FILES
        memory = MagicMock()
        memory.find_external_waits.return_value = {"waits": [{"workflow_id": "wf-1", "task_id": "m1", "match_pr_urls": [PR]}]}
        memory.get_workflow.return_value = {"workflow": {"id": "wf-1"}, "tasks": [
            {"step_name": "review_precheck", "status": "succeeded", "created_at": "1",
             "output": {"change_identity": merge_modes.change_identity(files, PR)}}]}
        github = CarryTests._github(None, files)
        event = {"pr_url": PR, "before_sha": "old", "head_sha": "new"}
        self.assertEqual(merge_modes.carry_on_update(event, tenant_id="t", memory=memory, github=github),
                         ["harness/code-review", "harness/qa"])

    def test_a_pr_not_waiting_to_merge_is_ignored(self):
        memory = MagicMock()
        memory.find_external_waits.return_value = {"waits": []}
        github = MagicMock()
        event = {"pr_url": PR, "before_sha": "old", "head_sha": "new"}
        self.assertEqual(merge_modes.carry_on_update(event, tenant_id="t", memory=memory, github=github), [])
        github.list_pull_files.assert_not_called()


def _pull(head="h1", mergeable=True):
    return {"state": "open", "merged": False, "base": {"ref": "dev"}, "head": {"sha": head}, "mergeable": mergeable}


class HarnessModeTests(unittest.TestCase):
    def setUp(self):
        merge_modes._cache.clear()
        merge_modes._posted.clear()

    def run_poll(self, *, behind, reviews, defaults=None, heads=("h1",)):
        memory = MagicMock()
        memory.find_external_waits.return_value = {"waits": [{"workflow_id": "wf-1", "task_id": "m1", "match_pr_urls": [PR]}]}
        memory.get_workflow.return_value = {"workflow": {"id": "wf-1"}, "tasks": []}
        github = _github(rules=[], protection=None)
        pulls = iter([_pull(h) for h in heads])
        last = {"pull": _pull(heads[0])}

        def get_pull(*_a):
            last["pull"] = next(pulls, last["pull"])
            return last["pull"]

        github.get_pull.side_effect = get_pull
        github.compare.return_value = {"behind_by": behind}
        github.list_reviews.return_value = reviews
        out = merge_modes.keep_approved_current(github=github, memory=memory, program_defaults=defaults or {}, sleep=lambda _s: None)
        return out, github

    def statuses(self, github):
        return [(c.args[2], c.kwargs["state"]) for c in github.create_status.call_args_list if c.kwargs["context"] == "harness/up-to-date"]

    def test_an_up_to_date_pr_gets_a_success_status_once(self):
        out, github = self.run_poll(behind=0, reviews=[])
        self.assertEqual(self.statuses(github), [("h1", "success")])
        self.assertEqual(out["checked"], 1)

    def test_behind_and_not_approved_waits_with_a_pending_status(self):
        _out, github = self.run_poll(behind=2, reviews=[{"user": {"login": "a"}, "state": "COMMENTED"}])
        self.assertEqual(self.statuses(github), [("h1", "pending")])
        github.update_branch.assert_not_called()

    def test_behind_and_approved_is_updated_and_marked(self):
        reviews = [{"user": {"login": "a"}, "state": "APPROVED"}]
        out, github = self.run_poll(behind=2, reviews=reviews, heads=("h1", "h1", "h2"))
        github.update_branch.assert_called_once_with("org", "app", 5, expected_head_sha="h1")
        self.assertEqual(out["updated"], 1)
        self.assertEqual(self.statuses(github), [("h2", "success")])

    def test_changes_requested_after_an_approval_is_not_approved(self):
        reviews = [{"user": {"login": "a"}, "state": "APPROVED"}, {"user": {"login": "b"}, "state": "CHANGES_REQUESTED"}]
        _out, github = self.run_poll(behind=2, reviews=reviews)
        github.update_branch.assert_not_called()

    def test_repos_github_keeps_current_are_left_alone(self):
        out, github = self.run_poll(behind=2, reviews=[{"user": {"login": "a"}, "state": "APPROVED"}],
                                    defaults={"merge_readiness": {"merge_mode": "merge_queue"}})
        self.assertEqual(out["checked"], 0)
        github.update_branch.assert_not_called()
        github.create_status.assert_not_called()


class ConsoleNoteTests(unittest.TestCase):
    def test_the_merge_step_says_how_the_pr_is_kept_current(self):
        mode = merge_modes._result("harness", "fallback", "dev")
        note = merge_mode_note({"prs": [{"pr_url": PR, "status": "behind", "base_ref": "dev", "merge_mode": mode}]})
        self.assertTrue(note.startswith("Behind dev but merges cleanly."))
        self.assertIn("not blocked", note)


if __name__ == "__main__":
    unittest.main()
