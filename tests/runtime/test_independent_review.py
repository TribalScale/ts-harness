"""harness/independent-review verdicts, checks waits pinned to head SHAs, merge recording."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from ts_harness.workflows import independent_review, merge_readiness

PR = "https://github.com/o/r/pull/7"
LOGINS = {"pat@acme.com": "pat-gh", "sam@acme.com": "sam-gh"}


def review(login: str, state: str = "APPROVED", at: str = "2026-09-01T00:00:00Z") -> dict[str, Any]:
    return {"user": {"login": login}, "state": state, "submitted_at": at}


def prd_gate(email: str, *, step: str = "approve_prd", decision: str = "approved", at: str = "2026-08-01") -> dict[str, Any]:
    return {
        "kind": "gate",
        "decision": decision,
        "actor_id": "slack:U1",
        "actor_email": email,
        "details": {"step_name": step},
        "created_at": at,
    }


class VerdictTests(unittest.TestCase):
    def _evaluate(self, reviews, approver="pat@acme.com", enabled=True):
        return independent_review.evaluate(
            enabled=enabled,
            reviews=reviews,
            prd_approver=approver,
            login_emails=independent_review.logins_to_emails(LOGINS),
        )

    def test_same_person_is_not_enough(self):
        verdict = self._evaluate([review("pat-gh")])
        self.assertEqual(verdict.state, "pending")
        self.assertIn("PRD approver", verdict.description)

    def test_different_person_passes(self):
        verdict = self._evaluate([review("pat-gh"), review("Sam-GH")])
        self.assertEqual((verdict.state, verdict.reviewer), ("success", "sam-gh"))

    def test_unmapped_reviewer_does_not_count(self):
        verdict = self._evaluate([review("stranger")])
        self.assertEqual(verdict.state, "pending")
        self.assertIn("not mapped", verdict.description)

    def test_latest_review_wins(self):
        verdict = self._evaluate(
            [review("sam-gh", "APPROVED", "2026-09-01"), review("sam-gh", "CHANGES_REQUESTED", "2026-09-02")]
        )
        self.assertEqual(verdict.state, "pending")

    def test_setting_off_posts_success(self):
        verdict = self._evaluate([], enabled=False)
        self.assertEqual((verdict.state, verdict.description), ("success", "disabled by tenant setting"))

    def test_prd_approver_is_latest_approved_prd_gate(self):
        decisions = [
            prd_gate("old@acme.com", at="2026-08-01"),
            prd_gate("pat@acme.com", at="2026-08-02"),
            prd_gate("qa@acme.com", step="qa_gate", at="2026-08-03"),
            prd_gate("sam@acme.com", decision="rejected", at="2026-08-04"),
        ]
        self.assertEqual(independent_review.prd_approver_from_decisions(decisions), "pat@acme.com")


class PublishTests(unittest.TestCase):
    def _memory(self, *, pinned_enabled: bool | None = None, approver="pat@acme.com"):
        memory = MagicMock()
        metadata = {}
        if pinned_enabled is not None:
            metadata = {"governance_snapshot": {"independent_review": {"enabled": pinned_enabled}}}
        memory.find_external_refs.return_value = {
            "refs": [{"program_id": "child", "root_program_id": "root", "program_metadata": metadata}]
        }
        memory.list_decisions.return_value = {"decisions": [prd_gate(approver)]}
        return memory

    def _github(self, reviews):
        github = MagicMock()
        github.list_reviews.return_value = reviews
        github.get_pull.return_value = {"head": {"sha": "h1"}}
        return github

    def test_publish_posts_status_on_head(self):
        memory, github = self._memory(), self._github([review("sam-gh")])
        result = independent_review.publish(
            PR, memory=memory, github=github, identity=SimpleNamespace(github_logins=LOGINS)
        )
        self.assertEqual(result["state"], "success")
        memory.list_decisions.assert_called_once_with(program_id="root", kind="gate")
        args, kwargs = github.create_status.call_args
        self.assertEqual(args, ("o", "r", "h1"))
        self.assertEqual((kwargs["state"], kwargs["context"]), ("success", "harness/independent-review"))

    def test_pinned_off_survives_live_setting_on(self):
        memory, github = self._memory(pinned_enabled=False), self._github([review("pat-gh")])
        with patch.object(independent_review, "_live_identity", return_value=SimpleNamespace(github_logins=LOGINS)):
            result = independent_review.publish(PR, head_sha="h9", memory=memory, github=github)
        self.assertEqual((result["state"], result["description"]), ("success", "disabled by tenant setting"))
        github.list_reviews.assert_not_called()

    def test_pinned_on_survives_live_setting_off(self):
        memory, github = self._memory(pinned_enabled=True), self._github([review("pat-gh")])
        result = independent_review.publish(
            PR, head_sha="h9", memory=memory, github=github, identity=SimpleNamespace(github_logins=LOGINS)
        )
        self.assertEqual(result["state"], "pending")

    def test_webhook_ignores_foreign_prs(self):
        memory = MagicMock()
        memory.find_external_refs.return_value = {"refs": []}
        with patch.object(independent_review, "_default_memory", return_value=memory):
            result = independent_review.on_github_event({"pr_url": PR}, tenant_id="acme")
        self.assertEqual(result["status"], "ignored")


class ProtectionWarningTests(unittest.TestCase):
    def test_unprotected_and_missing_context_warn(self):
        github = MagicMock()
        github.required_status_contexts.return_value = None
        self.assertIn("not protected", independent_review.protection_warnings(github, "o", "r", "main", enabled=True)[0])
        github.required_status_contexts.return_value = ["ci"]
        self.assertIn("does not require", independent_review.protection_warnings(github, "o", "r", "main", enabled=True)[0])
        github.required_status_contexts.return_value = ["ci", "harness/independent-review"]
        self.assertEqual(independent_review.protection_warnings(github, "o", "r", "main", enabled=True), [])

    def test_disabled_setting_needs_no_protection(self):
        github = MagicMock()
        self.assertEqual(independent_review.protection_warnings(github, "o", "r", "main", enabled=False), [])
        github.required_status_contexts.assert_not_called()


class ChecksWaitTests(unittest.TestCase):
    def _memory(self, tasks):
        memory = MagicMock()
        memory.get_workflow.return_value = {"workflow": {"id": "wf", "program_id": "p1"}, "tasks": tasks}
        memory.find_external_refs.return_value = {"refs": []}
        return memory

    def _github(self, state="pending"):
        github = MagicMock()
        github.get_pull.side_effect = lambda owner, repo, number: {
            "head": {"sha": f"sha{number}"},
            "base": {"ref": "main", "repo": {"full_name": f"{owner}/{repo}"}},
        }
        github.check_summary.return_value = {"state": state, "checks": {"ci": state}, "failed": []}
        github.required_status_contexts.return_value = None
        return github

    def test_single_pr_wait_pins_head_and_required_checks(self):
        deliver = {"id": "d1", "step_name": "deliver_work_order", "status": "succeeded", "output": {"pr_url": PR}}
        memory, github = self._memory([deliver]), self._github()
        task = {"id": "t1", "workflow_id": "wf", "input": {}}
        prepared = merge_readiness.prepare_checks_wait(
            task,
            {"pr_checks": {"pr_url": "$prior.deliver_work_order.output.pr_url"}},
            memory=memory,
            github=github,
            program_defaults={"merge_readiness": {"required_checks": {"o/r": ["ci"]}}},
        )
        self.assertEqual(prepared["match"], {"head_sha": "sha7", "repo": "o/r"})
        self.assertNotIn("ready", prepared)
        patched = memory.patch_task_input.call_args.args[1]
        self.assertEqual(patched["_wait_match"], {"head_sha": "sha7", "repo": "o/r"})
        self.assertEqual(patched["_wait_options"]["heads"][0]["required_checks"], ["ci"])
        github.check_summary.assert_called_with("o", "r", "sha7", required=["ci"])
        github.create_status.assert_called()
        ref_kwargs = memory.record_program_external_ref.call_args.kwargs
        self.assertIn("not protected", ref_kwargs["metadata"]["merge_readiness_warnings"][0])

    def test_already_green_is_ready_immediately(self):
        deliver = {"id": "d1", "step_name": "deliver_work_order", "status": "succeeded", "output": {"pr_url": PR}}
        prepared = merge_readiness.prepare_checks_wait(
            {"id": "t1", "workflow_id": "wf", "input": {}},
            {"pr_checks": {"pr_url": "$prior.deliver_work_order.output.pr_url"}},
            memory=self._memory([deliver]),
            github=self._github("success"),
            program_defaults={},
        )
        self.assertEqual(prepared["ready"]["checks_state"], "success")
        self.assertEqual(prepared["ready"]["head_sha"], "sha7")

    def test_multi_pr_wait_uses_heads_not_match(self):
        barrier = {
            "id": "c1",
            "step_name": "collect_delivery_results",
            "status": "succeeded",
            "output": {"pr_urls": [PR, "https://github.com/o/r/pull/8"]},
        }
        github = self._github()
        github.check_summary.side_effect = [
            {"state": "success", "checks": {}, "failed": []},
            {"state": "failure", "checks": {"ci": "failure"}, "failed": ["ci"]},
        ]
        prepared = merge_readiness.prepare_checks_wait(
            {"id": "t1", "workflow_id": "wf", "input": {}},
            {"pr_checks": {"pr_urls": "$prior.collect_delivery_results.output.pr_urls"}},
            memory=self._memory([barrier]),
            github=github,
            program_defaults={},
        )
        self.assertEqual(prepared["match"], {})
        self.assertEqual([h["head_sha"] for h in prepared["options"]["heads"]], ["sha7", "sha8"])
        self.assertEqual(prepared["ready"]["checks_state"], "failure")
        self.assertEqual(prepared["ready"]["failed_checks"], ["https://github.com/o/r/pull/8:ci"])

    def test_missing_pr_url_fails_closed(self):
        with self.assertRaises(ValueError):
            merge_readiness.prepare_checks_wait(
                {"id": "t1", "workflow_id": "wf", "input": {}},
                {"pr_checks": {"pr_url": "$prior.deliver_work_order.output.pr_url"}},
                memory=self._memory([]),
                github=self._github(),
                program_defaults={},
            )

    def test_resume_uses_pinned_match(self):
        from ts_harness.workflows.hooks import wait_external

        task = {
            "id": "t1",
            "status": "blocked",
            "error": "awaiting_external:github.pr_checks",
            "expected_event_type": "github.pr_checks",
            "step_name": "await_pr_checks",
            "input": {"_wait_match": {"head_sha": "sha7", "repo": "o/r"}},
        }
        memory = MagicMock()
        memory.get_workflow.return_value = {"workflow": {}, "tasks": [task]}
        stage = SimpleNamespace(
            wait_external={"event_type": "github.pr_checks", "pr_checks": {}}, approval_required=False
        )
        ctx = SimpleNamespace(
            extras={"event_type": "github.pr_checks", "payload": {"head_sha": "other", "repo": "o/r"}},
            memory_client=memory,
            workflow_id="wf",
            skeleton=SimpleNamespace(stage=lambda name: stage),
            actor="a",
        )
        with self.assertRaises(ValueError):
            wait_external.resume(ctx)
        ctx.extras["payload"] = {"head_sha": "sha7", "repo": "o/r"}
        wait_external.resume(ctx)
        memory.resume_external.assert_called_once()


class MergeRecordingTests(unittest.TestCase):
    def test_on_merge_records_merged_by(self):
        memory = MagicMock()
        memory.find_external_refs.return_value = {"refs": [{"program_id": "p1"}]}
        with patch("ts_harness.memory.client.get_memory_client", return_value=memory):
            result = merge_readiness.on_merge(
                {"pr_url": PR, "merged_by": "sam-gh", "merge_sha": "m1"}, tenant_id="acme"
            )
        self.assertEqual(result["merged_by"], "sam-gh")
        kwargs = memory.record_program_external_ref.call_args.kwargs
        self.assertEqual((kwargs["state"], kwargs["metadata"]["merged_by"]), ("merged", "sam-gh"))


class SettingsTests(unittest.TestCase):
    def test_required_checks_validation(self):
        from ts_harness.config.runtime_settings import validate_runtime_settings_payload

        base = {"definition_type": "runtime_settings", "definition_version": "1.0.0"}
        canonical, issues = validate_runtime_settings_payload(
            {**base, "program_defaults": {"merge_readiness": {"required_checks": {"Org/App": ["ci", "ci", "lint"]}}}}
        )
        self.assertEqual(issues, [])
        self.assertEqual(canonical["program_defaults"]["merge_readiness"]["required_checks"], {"org/app": ["ci", "lint"]})
        self.assertEqual(merge_readiness.required_checks_for("Org/App", canonical["program_defaults"]), ["ci", "lint"])
        _, issues = validate_runtime_settings_payload(
            {**base, "program_defaults": {"merge_readiness": {"required_checks": {"not a repo": []}}}}
        )
        self.assertIn("invalid_repo_slug", {issue["code"] for issue in issues})


if __name__ == "__main__":
    unittest.main()
