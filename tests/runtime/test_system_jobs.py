# flake8: noqa
import unittest
from unittest.mock import MagicMock, patch

from ts_harness.gateway import system_jobs
from ts_harness.workflows.pr_rejection import REJECTION_REASONS


class ParseGithubPrUrlTests(unittest.TestCase):
    def test_parse_standard_pull_url(self):
        parsed = system_jobs.parse_github_pr_url(
            "https://github.com/example-org/prd-dev-project/pull/1"
        )
        self.assertEqual(parsed, ("example-org", "prd-dev-project", 1))

    def test_parse_rejects_non_pr_url(self):
        self.assertIsNone(system_jobs.parse_github_pr_url("https://github.com/org/repo"))


class PollGithubMergesTests(unittest.TestCase):
    def test_resumes_when_pr_merged(self):
        memory = MagicMock()
        memory.find_external_waits.return_value = {
            "waits": [
                {
                    "workflow_id": "wf-1",
                    "task_id": "t-wait",
                    "match_pr_url": "https://github.com/org/app/pull/9",
                }
            ]
        }
        engine = MagicMock()
        engine.resume_external.return_value = {"status": "resumed", "task_id": "t-wait"}
        fetch = MagicMock(
            return_value={"merged": True, "merge_commit_sha": "abc123", "state": "closed"}
        )

        with (
            patch.object(system_jobs, "get_memory_client", return_value=memory),
            patch.object(system_jobs, "get_workflow_engine", return_value=engine),
            patch.object(system_jobs, "fetch_pull_request", fetch),
            patch.dict("os.environ", {"GITHUB_TOKEN": "token"}, clear=False),
        ):
            result = system_jobs.poll_github_merges()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["resumed"], 1)
        engine.resume_external.assert_called_once()
        kwargs = engine.resume_external.call_args.kwargs
        self.assertEqual(kwargs["event_type"], "github.merge")
        self.assertEqual(kwargs["payload"]["pr_url"], "https://github.com/org/app/pull/9")
        self.assertEqual(kwargs["payload"]["merge_sha"], "abc123")
        self.assertEqual(kwargs["actor"], "github-merge-poller")

    def test_a_polled_merge_is_recorded_like_the_webhook(self):
        memory = MagicMock()
        memory.find_external_waits.return_value = {
            "waits": [{"workflow_id": "wf-1", "task_id": "t-wait", "match_pr_url": "https://github.com/org/app/pull/9"}]
        }
        engine = MagicMock()
        engine.resume_external.return_value = {"status": "resumed", "task_id": "t-wait"}
        pr = {"merged": True, "merge_commit_sha": "abc123", "state": "closed", "merged_by": {"login": "alice"}, "base": {"ref": "dev"}}
        with (
            patch.object(system_jobs, "get_memory_client", return_value=memory),
            patch.object(system_jobs, "get_workflow_engine", return_value=engine),
            patch.object(system_jobs, "fetch_pull_request", MagicMock(return_value=pr)),
            patch("ts_harness.workflows.merge_readiness.on_merge") as on_merge,
            patch.dict("os.environ", {"GITHUB_TOKEN": "token"}, clear=False),
        ):
            system_jobs.poll_github_merges(force=True)
        event = on_merge.call_args.args[0]
        self.assertEqual(event, {"pr_url": "https://github.com/org/app/pull/9", "base_branch": "dev",
                                 "merged_by": "alice", "merge_sha": "abc123"})

    def test_skips_open_pr(self):
        memory = MagicMock()
        memory.find_external_waits.return_value = {
            "waits": [
                {
                    "workflow_id": "wf-1",
                    "task_id": "t-wait",
                    "match_pr_url": "https://github.com/org/app/pull/9",
                }
            ]
        }
        engine = MagicMock()
        fetch = MagicMock(return_value={"merged": False, "state": "open"})

        with (
            patch.object(system_jobs, "get_memory_client", return_value=memory),
            patch.object(system_jobs, "get_workflow_engine", return_value=engine),
            patch.object(system_jobs, "fetch_pull_request", fetch),
            patch.dict("os.environ", {"GITHUB_TOKEN": "token"}, clear=False),
        ):
            result = system_jobs.poll_github_merges()

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["resumed"], 0)
        engine.resume_external.assert_not_called()

    def _poll_rejection(self, rejection, pr, handled=REJECTION_REASONS):
        memory = MagicMock()
        memory.find_external_waits.return_value = {
            "waits": [{"workflow_id": "wf-1", "task_id": "t-wait", "match_pr_url": "https://github.com/org/app/pull/9"}]
        }
        engine = MagicMock()
        engine.block_case_prefixes.return_value = list(handled)
        with (
            patch.object(system_jobs, "get_memory_client", return_value=memory),
            patch.object(system_jobs, "get_workflow_engine", return_value=engine),
            patch.object(system_jobs, "fetch_pull_request", MagicMock(return_value=pr)),
            patch.object(system_jobs, "detect_rejection", MagicMock(return_value=rejection)) as detect,
            patch.dict("os.environ", {"GITHUB_TOKEN": "token"}, clear=False),
        ):
            result = system_jobs.poll_github_merges()
        return result, engine, detect

    def test_blocks_the_wait_when_a_human_rejects_the_pr(self):
        rejection = {
            "reason": "external_blocked:github.pr_changes_requested by alice",
            "actor": "alice",
            "feedback": "Human reviewer alice requested changes on the pull request:\nRename it.",
        }
        result, engine, _ = self._poll_rejection(rejection, {"merged": False, "state": "open", "head": {"sha": "s"}})

        self.assertEqual((result["rejected"], result["skipped"]), (1, 0))
        engine.resume_external.assert_not_called()
        engine.block_wait.assert_called_once()
        args, kwargs = engine.block_wait.call_args
        self.assertEqual(args, ("wf-1", "t-wait", rejection["reason"]))
        self.assertEqual(kwargs["output"]["review_feedback"], rejection["feedback"])
        self.assertEqual(kwargs["output"]["pr_url"], "https://github.com/org/app/pull/9")
        self.assertEqual(kwargs["actor"], "github-merge-poller")

    def test_definitions_without_rejection_cases_keep_waiting(self):
        rejection = {"reason": "external_blocked:github.pr_closed by bob", "actor": "bob", "feedback": ""}
        result, engine, detect = self._poll_rejection(rejection, {"merged": False, "state": "closed"}, handled=())

        self.assertEqual((result["rejected"], result["skipped"]), (0, 1))
        engine.block_case_prefixes.assert_called_once_with("wf-1", "t-wait")
        detect.assert_not_called()
        engine.block_wait.assert_not_called()

    def test_merged_pr_resumes_without_checking_for_rejection(self):
        result, engine, detect = self._poll_rejection(
            {"reason": "x", "actor": "", "feedback": ""}, {"merged": True, "merge_commit_sha": "abc", "state": "closed"}
        )
        self.assertEqual(result["resumed"], 1)
        detect.assert_not_called()
        engine.block_wait.assert_not_called()

    def test_no_waits_is_noop(self):
        memory = MagicMock()
        memory.find_external_waits.return_value = {"waits": []}
        engine = MagicMock()

        with (
            patch.object(system_jobs, "get_memory_client", return_value=memory),
            patch.object(system_jobs, "get_workflow_engine", return_value=engine),
            patch.dict("os.environ", {"GITHUB_TOKEN": "token"}, clear=False),
        ):
            result = system_jobs.poll_github_merges()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["checked"], 0)
        engine.resume_external.assert_not_called()

    def test_api_error_does_not_abort_other_waits(self):
        memory = MagicMock()
        memory.find_external_waits.return_value = {
            "waits": [
                {
                    "workflow_id": "wf-bad",
                    "task_id": "t-bad",
                    "match_pr_url": "https://github.com/org/app/pull/1",
                },
                {
                    "workflow_id": "wf-good",
                    "task_id": "t-good",
                    "match_pr_url": "https://github.com/org/app/pull/2",
                },
            ]
        }
        engine = MagicMock()
        engine.resume_external.return_value = {"status": "resumed"}

        def fetch(owner, repo, number, *, github):
            if number == 1:
                raise RuntimeError("api down")
            return {"merged": True, "merge_commit_sha": "deadbeef"}

        with (
            patch.object(system_jobs, "get_memory_client", return_value=memory),
            patch.object(system_jobs, "get_workflow_engine", return_value=engine),
            patch.object(system_jobs, "fetch_pull_request", side_effect=fetch),
            patch.dict("os.environ", {"GITHUB_TOKEN": "token"}, clear=False),
        ):
            result = system_jobs.poll_github_merges()

        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["resumed"], 1)
        engine.resume_external.assert_called_once()

    def test_joins_and_release_upstreams_run_when_github_reconcile_is_skipped(self):
        joins = MagicMock(return_value={"checked": 1, "resumed": 1, "errors": 0})
        upstreams = MagicMock(return_value={"checked": 2, "resumed": 0, "errors": 0})
        with (
            patch.object(system_jobs, "reconcile_due", return_value=False),
            patch.object(system_jobs, "poll_wave_joins", joins),
            patch.object(system_jobs, "poll_release_upstreams", upstreams),
        ):
            result = system_jobs.poll_github_merges()

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["joins"]["resumed"], 1)
        self.assertEqual(result["upstreams"]["checked"], 2)

    def test_release_upstream_poller_resumes_tracks_no_longer_pending(self):
        memory = MagicMock()
        memory.find_external_waits.return_value = {
            "waits": [
                {"workflow_id": "track-ready", "options": {"environment": "staging"}},
                {"workflow_id": "track-waiting", "options": {"environment": "staging"}},
                {"workflow_id": "track-unpinned", "options": {}},
            ]
        }
        memory.get_workflow.side_effect = lambda wid: {"workflow": {"id": wid}, "tasks": []}
        engine = MagicMock()
        states = {"track-ready": "ready", "track-waiting": "pending"}
        with (
            patch.object(system_jobs, "get_memory_client", return_value=memory),
            patch.object(system_jobs, "get_workflow_engine", return_value=engine),
            patch(
                "ts_harness.workflows.release_tracks.upstream_status",
                side_effect=lambda _m, workflow, _t, _s: {"state": states[workflow["id"]]},
            ),
        ):
            result = system_jobs.poll_release_upstreams()

        self.assertEqual(result, {"checked": 2, "resumed": 1, "errors": 0})
        self.assertEqual(engine.resume_external.call_args.args, ("track-ready",))

    def test_dispatch_system_job_routes_handler(self):
        poll = MagicMock(return_value={"status": "ok"})
        with patch.dict(system_jobs.SYSTEM_HANDLERS, {"poll_github_merges": poll}):
            result = system_jobs.dispatch_system_job({"kind": "system", "handler": "poll_github_merges"})
        self.assertEqual(result, {"status": "ok"})
        poll.assert_called_once_with()

    def test_dispatch_unknown_handler_returns_error(self):
        result = system_jobs.dispatch_system_job({"kind": "system", "handler": "nope"})
        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()


class ConflictingChecksTests(unittest.TestCase):
    """A PR waiting for CI that conflicts with its base is sent back to resolve the conflict."""

    def _wait(self):
        return {"workflow_id": "wf-1", "task_id": "t-checks", "options": {"heads": [
            {"repo": "org/app", "pr_url": "https://github.com/org/app/pull/9", "head_sha": "abc"}]}}

    def _run(self, pull):
        memory = MagicMock()
        memory.find_external_waits.return_value = {"waits": [self._wait()]}
        engine = MagicMock()
        github = MagicMock()
        github.get_pull.return_value = pull
        with patch.object(system_jobs, "get_memory_client", return_value=memory), \
                patch.object(system_jobs, "get_workflow_engine", return_value=engine):
            result = system_jobs.send_back_conflicting_checks(github=github)
        return result, engine, memory

    def test_a_conflicting_pr_blocks_the_wait_with_merge_instructions(self):
        result, engine, memory = self._run({"mergeable": False, "state": "open", "base": {"ref": "dev"}})
        self.assertEqual(result["sent_back"], 1)
        memory.find_external_waits.assert_called_once_with(event_type="github.pr_checks", tenant_id="default")
        args, kwargs = engine.block_wait.call_args
        self.assertEqual(args[:3], ("wf-1", "t-checks", system_jobs.CONFLICT_REASON))
        summary = kwargs["output"]["summary"]
        self.assertIn("merge origin/dev", summary)
        self.assertIn("https://github.com/org/app/pull/9", summary)

    def test_mergeable_unknown_or_merged_prs_are_left_alone(self):
        for pull in ({"mergeable": None, "state": "open"}, {"mergeable": True, "state": "open"},
                     {"mergeable": False, "merged": True, "state": "closed"}):
            result, engine, _ = self._run(pull)
            self.assertEqual(result["sent_back"], 0, pull)
            engine.block_wait.assert_not_called()

    def test_the_feedback_reaches_the_revision(self):
        from ts_harness.workflows.engine import _qa_feedback_text

        text = _qa_feedback_text({}, {"output": {"summary": "merge origin/dev", "conflicting_prs": []}}, system_jobs.CONFLICT_REASON)
        self.assertIn("merge origin/dev", text)


class RepinChecksWaitTests(unittest.TestCase):
    """A push to a PR whose CI the harness is waiting for moves the wait to the new commit."""

    def test_the_wait_follows_the_new_head(self):
        from ts_harness.workflows.merge_readiness import repin_checks_waits

        memory = MagicMock()
        pr = "https://github.com/org/app/pull/15"
        memory.find_external_waits.return_value = {"waits": [
            {"task_id": "t-checks", "input": {"_wait_match": {"repo": "org/app", "head_sha": "old"},
                                              "_wait_options": {"heads": [{"pr_url": pr, "head_sha": "old"}], "warnings": []}}},
            {"task_id": "t-other", "input": {"_wait_options": {"heads": [{"pr_url": "https://github.com/org/app/pull/9", "head_sha": "x"}]}}},
        ]}
        moved = repin_checks_waits({"pr_url": pr, "head_sha": "new"}, tenant_id="default", memory=memory)
        self.assertEqual(moved, ["t-checks"])
        task_id, patched = memory.patch_task_input.call_args.args
        self.assertEqual(task_id, "t-checks")
        self.assertEqual(patched["_wait_match"]["head_sha"], "new")
        self.assertEqual(patched["_wait_options"]["heads"][0]["head_sha"], "new")

    def test_same_head_or_unknown_pr_changes_nothing(self):
        from ts_harness.workflows.merge_readiness import repin_checks_waits

        memory = MagicMock()
        memory.find_external_waits.return_value = {"waits": [
            {"task_id": "t", "input": {"_wait_options": {"heads": [{"pr_url": "https://github.com/o/r/pull/1", "head_sha": "abc"}]}}}]}
        self.assertEqual(repin_checks_waits({"pr_url": "https://github.com/o/r/pull/1", "head_sha": "abc"}, tenant_id="default", memory=memory), [])
        self.assertEqual(repin_checks_waits({"pr_url": "", "head_sha": "abc"}, tenant_id="default", memory=memory), [])
        memory.patch_task_input.assert_not_called()


class ConflictingMergeWaitTests(unittest.TestCase):
    """A PR waiting for a person to merge it is updated only once it conflicts with its base."""

    def _run(self, pull, behind=3, defaults=None):
        memory = MagicMock()
        memory.find_external_waits.return_value = {"waits": [
            {"workflow_id": "wf-1", "task_id": "t-merge", "match_pr_urls": ["https://github.com/org/app/pull/9"]}]}
        engine = MagicMock()
        github = MagicMock()
        github.get_pull.return_value = {"state": "open", "base": {"ref": "dev"}, "head": {"sha": "abc"}, **pull}
        github.compare.return_value = {"behind_by": behind}
        with patch.object(system_jobs, "get_memory_client", return_value=memory), \
                patch.object(system_jobs, "get_workflow_engine", return_value=engine):
            result = system_jobs.send_back_conflicting_merges(github=github, program_defaults=defaults or {})
        return result, engine, github

    def test_a_conflicting_pr_starts_the_refresh(self):
        from ts_harness.workflows.merge_readiness import BASE_ADVANCED_REASON

        result, engine, _ = self._run({"mergeable": False})
        self.assertEqual(result["sent_back"], 1)
        args, kwargs = engine.block_wait.call_args
        self.assertEqual(args[:3], ("wf-1", "t-merge", BASE_ADVANCED_REASON))
        self.assertEqual(kwargs["output"]["stale"][0]["status"], "conflict")

    def test_behind_but_clean_or_unknown_is_left_alone_without_a_compare(self):
        for pull in ({"mergeable": True}, {"mergeable": None}):
            result, engine, github = self._run(pull)
            self.assertEqual(result["sent_back"], 0, pull)
            engine.block_wait.assert_not_called()
            github.compare.assert_not_called()

    def test_refresh_always_updates_a_behind_pr(self):
        result, _engine, _ = self._run({"mergeable": True}, defaults={"merge_readiness": {"refresh": "always"}})
        self.assertEqual(result["sent_back"], 1)


class CiWaitConflictRouteTests(unittest.TestCase):
    """A conflict found while waiting for CI takes the narrow conflict fix (refresh), not a full revision."""

    def _on_block(self):
        import json
        from pathlib import Path

        fragment = json.loads((Path(__file__).resolve().parents[2] / "config/fragments/verified_merge.json").read_text())
        return next(stage for stage in fragment["stages"] if stage["name"] == "await_pr_checks")["on_block"]

    def test_a_base_conflict_is_a_refresh_with_its_own_budget(self):
        from ts_harness.workflows.hooks.revise_on_reject import select_block_case

        config = select_block_case(dict(self._on_block()), f"{system_jobs.CONFLICT_REASON}: the PR conflicts")
        self.assertEqual(config["revise"]["budget"], "refresh")
        self.assertEqual(config["revise"]["static_input"], {"refresh": True})
        self.assertEqual(config["re_gate"]["step_name"], "await_pr_checks")

    def test_failing_checks_are_still_a_full_revision(self):
        from ts_harness.workflows.hooks.revise_on_reject import select_block_case

        config = select_block_case(dict(self._on_block()), "external_blocked:github.pr_checks failure")
        self.assertEqual(config["revise"]["budget"], "checks")
        self.assertNotIn("static_input", config["revise"])
