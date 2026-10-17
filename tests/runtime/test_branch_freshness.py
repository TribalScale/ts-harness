"""ensure_fresh blocks PRs whose base branch moved."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.workflows import system_steps
from ts_harness.workflows.branch_freshness import ensure_fresh
from ts_harness.workflows.system_steps import StepBlocked

PR_A = "https://github.com/acme/app/pull/1"
PR_B = "https://github.com/acme/app/pull/2"


def _memory():
    memory = MagicMock()
    memory.get_workflow.return_value = {
        "workflow": {"id": "wf-1"},
        "tasks": [
            {
                "step_name": "qa_gate",
                "status": "succeeded",
                "output": {"result": {"prs": [{"pr_url": PR_A}, {"pr_url": PR_B}]}},
                "created_at": "2026-01-01",
            }
        ],
    }
    return memory


def _github(pulls, behind):
    github = MagicMock()
    github.get_pull.side_effect = lambda _o, _r, number: pulls[number]
    github.compare.side_effect = lambda _o, _r, _base, head: {"behind_by": behind[head], "ahead_by": 1}
    return github


def _pull(sha, **extra):
    return {"state": "open", "merged": False, "base": {"ref": "main"}, "head": {"sha": sha}, **extra}


TASK = {"id": "fresh-1", "workflow_id": "wf-1"}
CONFIG = {"hook": "ensure_fresh", "pr_urls": "$prior.qa_gate.output.result.prs"}


class EnsureFreshTests(unittest.TestCase):
    def test_all_fresh_succeeds(self):
        github = _github({1: _pull("a"), 2: _pull("b")}, {"a": 0, "b": 0})
        output = ensure_fresh(TASK, CONFIG, memory=_memory(), github=github)
        self.assertTrue(output["fresh"])
        self.assertEqual([pr["status"] for pr in output["prs"]], ["fresh", "fresh"])
        github.compare.assert_any_call("acme", "app", "main", "a")

    def test_behind_pr_blocks_with_stale_list(self):
        github = _github({1: _pull("a"), 2: _pull("b")}, {"a": 0, "b": 3})
        with self.assertRaises(StepBlocked) as ctx:
            ensure_fresh(TASK, CONFIG, memory=_memory(), github=github)
        self.assertIn("stale_base:", str(ctx.exception))
        self.assertIn(f"{PR_B} behind_by=3", str(ctx.exception))
        self.assertEqual([pr["pr_url"] for pr in ctx.exception.output["stale"]], [PR_B])

    def test_merged_and_closed_prs_are_skipped(self):
        github = _github({1: _pull("a", merged=True, state="closed"), 2: _pull("b", state="closed")}, {})
        output = ensure_fresh(TASK, CONFIG, memory=_memory(), github=github)
        self.assertEqual([pr["status"] for pr in output["prs"]], ["skipped", "skipped"])
        github.compare.assert_not_called()

    def test_no_pr_urls_blocks(self):
        memory = MagicMock()
        memory.get_workflow.return_value = {"workflow": {}, "tasks": []}
        with self.assertRaisesRegex(ValueError, "resolved no PR URLs"):
            ensure_fresh(TASK, CONFIG, memory=memory, github=MagicMock())

    def test_registered_system_step_returns_blocked_output(self):
        github = _github({1: _pull("a"), 2: _pull("b")}, {"a": 2, "b": 0})
        with patch("ts_harness.workflows.branch_freshness.GitHubClient.from_env", return_value=github):
            result = system_steps.run_system_step("ensure_fresh", TASK, CONFIG, memory=_memory())
        self.assertEqual(result["status"], "blocked")
        self.assertTrue(result["reason"].startswith("stale_base:"))
        self.assertEqual(result["output"]["stale"][0]["pr_url"], PR_A)


class BaseAdvancedFanOutTests(unittest.TestCase):
    MERGED = "https://github.com/acme/app/pull/9"

    def _waits(self):
        return {
            "waits": [
                {"workflow_id": "wf-merged", "task_id": "m0", "match_pr_urls": [self.MERGED]},
                {"workflow_id": "wf-a", "task_id": "m1", "match_pr_urls": [PR_A]},
                {"workflow_id": "wf-b", "task_id": "m2", "match_pr_url": PR_B},
                {"workflow_id": "wf-other", "task_id": "m3", "match_pr_urls": ["https://github.com/acme/other/pull/1"]},
            ]
        }

    def _run(self, pulls, behind, event_extra=None):
        from ts_harness.workflows.merge_readiness import (
            BASE_ADVANCED_REASON,
            fan_out_base_advanced,
        )

        memory = MagicMock()
        memory.find_external_waits.return_value = self._waits()
        engine = MagicMock()
        blocked = fan_out_base_advanced(
            {"pr_url": self.MERGED, **(event_extra or {"base_branch": "main"})},
            tenant_id="acme",
            memory=memory,
            github=_github(pulls, behind),
            engine=engine,
        )
        return blocked, engine, BASE_ADVANCED_REASON

    def test_blocks_only_same_repo_same_base_siblings(self):
        pulls = {1: _pull("a"), 2: {**_pull("b"), "base": {"ref": "release"}}}
        blocked, engine, reason = self._run(pulls, {"a": 1, "b": 1})
        self.assertEqual([item["workflow_id"] for item in blocked], ["wf-a"])
        args, kwargs = engine.block_wait.call_args
        self.assertEqual(args, ("wf-a", "m1", reason))
        self.assertEqual(kwargs["output"]["stale"][0]["pr_url"], PR_A)
        self.assertEqual(kwargs["output"]["merged_pr_url"], self.MERGED)

    def test_up_to_date_and_merged_siblings_are_left_alone(self):
        pulls = {1: _pull("a"), 2: _pull("b", merged=True, state="closed")}
        blocked, engine, _ = self._run(pulls, {"a": 0})
        self.assertEqual(blocked, [])
        engine.block_wait.assert_not_called()

    def test_base_ref_falls_back_to_merged_pull(self):
        pulls = {1: _pull("a"), 2: _pull("b"), 9: _pull("z")}
        blocked, _engine, _ = self._run(pulls, {"a": 2, "b": 2}, event_extra={})
        self.assertEqual(sorted(item["workflow_id"] for item in blocked), ["wf-a", "wf-b"])

    def test_on_merge_fans_out_even_for_foreign_prs(self):
        from ts_harness.workflows import merge_readiness

        memory = MagicMock()
        memory.find_external_refs.return_value = {"refs": []}
        with (
            patch("ts_harness.memory.client.get_memory_client", return_value=memory),
            patch.object(merge_readiness, "fan_out_base_advanced", return_value=[{"workflow_id": "wf-a"}]) as fan_out,
        ):
            result = merge_readiness.on_merge({"pr_url": self.MERGED}, tenant_id="acme")
        self.assertEqual(result["status"], "ignored")
        self.assertEqual(result["base_advanced"], [{"workflow_id": "wf-a"}])
        fan_out.assert_called_once()
        memory.record_program_external_ref.assert_not_called()


if __name__ == "__main__":
    unittest.main()
