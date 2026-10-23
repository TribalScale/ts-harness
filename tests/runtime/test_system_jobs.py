# flake8: noqa
import unittest
from unittest.mock import MagicMock, patch

from ts_harness.gateway import system_jobs


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

        def fetch(owner, repo, number, *, token):
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
