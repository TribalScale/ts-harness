"""Shared GitHub client (mocked HTTP) and normalized webhook routing."""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

from ts_harness.gateway import github_webhook
from ts_harness.integrations.github_client import (
    GitHubClient,
    GitHubError,
    classify_github_wait_error,
    parse_github_pr_url,
    parse_repo_slug,
    summarize_checks,
)


class FakeTransport:
    def __init__(self, responses: dict[tuple[str, str], tuple[int, Any]] | None = None):
        self.responses = responses or {}
        self.calls: list[dict[str, Any]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "body": json.loads(body) if body else None}
        )
        path = url.split("api.github.com", 1)[1].split("?", 1)[0]
        status, payload = self.responses.get((method, path), (200, {}))
        raw = json.dumps(payload).encode() if payload is not None else b""
        return status, raw


def client(responses=None) -> tuple[GitHubClient, FakeTransport]:
    transport = FakeTransport(responses)
    return GitHubClient(token="t0k", transport=transport), transport


class ParsingTests(unittest.TestCase):
    def test_pr_url(self):
        self.assertEqual(parse_github_pr_url("https://github.com/o/r/pull/3"), ("o", "r", 3))
        self.assertIsNone(parse_github_pr_url("https://github.com/o/r"))

    def test_repo_slug_forms(self):
        for value in ("o/r", "https://github.com/o/r", "https://github.com/o/r.git", "git@github.com:o/r.git"):
            self.assertEqual(parse_repo_slug(value), ("o", "r"), value)
        self.assertIsNone(parse_repo_slug("not a repo"))


class ClientTests(unittest.TestCase):
    def test_auth_headers_and_pull(self):
        gh, transport = client({("GET", "/repos/o/r/pulls/5"): (200, {"number": 5, "merged": True})})
        self.assertTrue(gh.get_pull("o", "r", 5)["merged"])
        headers = transport.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer t0k")
        self.assertEqual(headers["Accept"], "application/vnd.github+json")

    def test_tree_and_file_use_contents_paths(self):
        gh, transport = client(
            {
                ("GET", "/repos/o/r/git/trees/dev"): (200, {"tree": []}),
                ("GET", "/repos/o/r/contents/src/app.py"): (200, {"type": "file"}),
            }
        )
        self.assertEqual(gh.get_tree("o", "r", "dev")["tree"], [])
        self.assertEqual(gh.get_file("o", "r", "src/app.py", ref="dev")["type"], "file")
        self.assertIn("recursive=1", transport.calls[0]["url"])
        self.assertIn("ref=dev", transport.calls[1]["url"])

    def test_error_raises(self):
        gh, _ = client({("GET", "/repos/o/r/pulls/5"): (500, {"message": "boom"})})
        with self.assertRaises(GitHubError) as ctx:
            gh.get_pull("o", "r", 5)
        self.assertEqual(ctx.exception.status, 500)

    def test_from_env_requires_token(self):
        with patch.dict("os.environ", {"GITHUB_TOKEN": ""}):
            with self.assertRaises(GitHubError):
                GitHubClient.from_env()

    def test_create_status_body(self):
        gh, transport = client()
        gh.create_status("o", "r", "abc", state="pending", context="harness/independent-review", description="x" * 200)
        call = transport.calls[0]
        self.assertEqual((call["method"], call["url"].split(".com")[1]), ("POST", "/repos/o/r/statuses/abc"))
        self.assertEqual(call["body"]["context"], "harness/independent-review")
        self.assertEqual(len(call["body"]["description"]), 140)

    def test_branch_protection_missing_is_none(self):
        gh, _ = client({("GET", "/repos/o/r/branches/main/protection"): (404, {"message": "Branch not protected"})})
        self.assertIsNone(gh.required_status_contexts("o", "r", "main"))

    def test_required_contexts_merge_legacy_and_checks(self):
        gh, _ = client(
            {
                ("GET", "/repos/o/r/branches/main/protection"): (
                    200,
                    {"required_status_checks": {"contexts": ["ci"], "checks": [{"context": "harness/independent-review"}]}},
                )
            }
        )
        self.assertEqual(gh.required_status_contexts("o", "r", "main"), ["ci", "harness/independent-review"])

    def test_dispatch_and_deployment(self):
        gh, transport = client({("POST", "/repos/o/r/actions/workflows/deploy.yml/dispatches"): (204, None)})
        gh.dispatch_workflow("o", "r", "deploy.yml", ref="main", inputs={"sha": "abc", "n": 1})
        self.assertEqual(transport.calls[0]["body"], {"ref": "main", "inputs": {"sha": "abc", "n": "1"}})
        gh.create_deployment("o", "r", ref="abc", environment="staging")
        body = transport.calls[1]["body"]
        self.assertEqual((body["ref"], body["environment"], body["required_contexts"]), ("abc", "staging", []))

    def test_close_comment_delete_branch(self):
        gh, transport = client({("DELETE", "/repos/o/r/git/refs/heads/wo/wo-1"): (204, None)})
        gh.close_pull("o", "r", 2)
        gh.comment("o", "r", 2, "cancelled")
        self.assertTrue(gh.delete_branch("o", "r", "wo/wo-1"))
        self.assertEqual(transport.calls[0]["body"], {"state": "closed"})
        self.assertEqual(transport.calls[1]["url"].split(".com")[1], "/repos/o/r/issues/2/comments")

    def test_check_summary_uses_statuses_and_runs(self):
        gh, _ = client(
            {
                ("GET", "/repos/o/r/commits/abc/status"): (200, {"statuses": [{"context": "lint", "state": "success"}]}),
                ("GET", "/repos/o/r/commits/abc/check-runs"): (
                    200,
                    {"check_runs": [{"name": "test", "status": "completed", "conclusion": "success"}]},
                ),
            }
        )
        summary = gh.check_summary("o", "r", "abc")
        self.assertEqual(summary["state"], "success")
        self.assertEqual(summary["checks"], {"lint": "success", "test": "success"})

    def test_check_runs_fall_back_to_actions_jobs_when_token_lacks_checks(self):
        sha = "a" * 40
        gh, transport = client(
            {
                ("GET", f"/repos/o/r/commits/{sha}/check-runs"): (403, {"message": "Resource not accessible by personal access token"}),
                ("GET", "/repos/o/r/actions/runs"): (
                    200,
                    {"workflow_runs": [{"id": 2, "created_at": "2026-10-01T02:00:00Z"}, {"id": 1, "created_at": "2026-10-01T01:00:00Z"}]},
                ),
                ("GET", "/repos/o/r/actions/runs/1/jobs"): (200, {"jobs": [{"name": "test", "status": "completed", "conclusion": "failure"}]}),
                ("GET", "/repos/o/r/actions/runs/2/jobs"): (
                    200,
                    {"jobs": [{"name": "test", "status": "completed", "conclusion": "success"}, {"name": "security", "status": "in_progress", "conclusion": None}]},
                ),
            }
        )
        summary = gh.check_summary("o", "r", sha, required=["test", "security"])
        self.assertEqual(summary["checks"], {"test": "success", "security": "pending"})
        runs_call = next(c for c in transport.calls if c["url"].split("?")[0].endswith("/actions/runs"))
        self.assertIn(f"head_sha={sha}", runs_call["url"])

    def test_actions_fallback_resolves_a_branch_ref_to_its_sha(self):
        sha = "b" * 40
        gh, transport = client(
            {
                ("GET", "/repos/o/r/commits/wo/wo-1/check-runs"): (403, {"message": "Resource not accessible by personal access token"}),
                ("GET", "/repos/o/r/commits/wo/wo-1"): (200, {"sha": sha}),
                ("GET", "/repos/o/r/actions/runs"): (200, {"workflow_runs": []}),
            }
        )
        self.assertEqual(gh.check_runs("o", "r", "wo/wo-1"), [])
        runs_call = next(c for c in transport.calls if c["url"].split("?")[0].endswith("/actions/runs"))
        self.assertIn(f"head_sha={sha}", runs_call["url"])

    def test_check_runs_other_errors_do_not_fall_back(self):
        gh, transport = client({("GET", "/repos/o/r/commits/abc/check-runs"): (500, {"message": "boom"})})
        with self.assertRaises(GitHubError):
            gh.check_runs("o", "r", "abc")
        self.assertEqual(len(transport.calls), 1)


class SummarizeChecksTests(unittest.TestCase):
    def test_failure_wins(self):
        summary = summarize_checks(
            {"statuses": [{"context": "a", "state": "pending"}]},
            [{"name": "b", "status": "completed", "conclusion": "failure"}],
        )
        self.assertEqual((summary["state"], summary["failed"]), ("failure", ["b"]))

    def test_required_missing_is_pending(self):
        summary = summarize_checks({"statuses": []}, [{"name": "b", "status": "completed", "conclusion": "success"}], required=["ci"])
        self.assertEqual((summary["state"], summary["pending"]), ("pending", ["ci"]))

    def test_no_checks_is_pending(self):
        self.assertEqual(summarize_checks({}, [])["state"], "pending")

    def test_in_progress_run_is_pending(self):
        self.assertEqual(summarize_checks({}, [{"name": "b", "status": "in_progress"}])["state"], "pending")

    def test_later_harness_gates_do_not_hold_checks_without_required_names(self):
        combined = {
            "statuses": [
                {"context": "harness/independent-review", "state": "pending"},
                {"context": "harness/code-review", "state": "failure"},
                {"context": "harness/security-scan", "state": "success"},
            ]
        }
        summary = summarize_checks(combined, [])
        self.assertEqual(summary["state"], "success")
        self.assertEqual(summary["checks"], {"harness/security-scan": "success"})

    def test_required_harness_gate_still_counts(self):
        combined = {"statuses": [{"context": "harness/independent-review", "state": "pending"}]}
        summary = summarize_checks(combined, [], required=["harness/independent-review"])
        self.assertEqual(summary["state"], "pending")


REPO = {"full_name": "o/r"}


class ParseEventTests(unittest.TestCase):
    def test_merge_captures_merged_by(self):
        event = github_webhook.parse_github_event(
            {
                "action": "closed",
                "repository": REPO,
                "pull_request": {
                    "merged": True,
                    "html_url": "https://github.com/o/r/pull/9",
                    "merge_commit_sha": "m1",
                    "merged_by": {"login": "alice"},
                },
            },
            "pull_request",
        )
        self.assertEqual((event["kind"], event["merged_by"], event["repo"]), ("github.merge", "alice", "o/r"))

    def test_pr_opened(self):
        event = github_webhook.parse_github_event(
            {
                "action": "synchronize",
                "repository": REPO,
                "pull_request": {"html_url": "u", "number": 1, "head": {"sha": "h1"}, "user": {"login": "claw-bot"}},
            },
            "pull_request",
        )
        self.assertEqual((event["kind"], event["head_sha"], event["author"]), ("github.pr_opened", "h1", "claw-bot"))

    def test_review(self):
        event = github_webhook.parse_github_event(
            {
                "action": "submitted",
                "repository": REPO,
                "pull_request": {"html_url": "u", "number": 1, "head": {"sha": "h1"}},
                "review": {"user": {"login": "bob"}, "state": "APPROVED", "commit_id": "h1"},
            }
        )
        self.assertEqual((event["kind"], event["reviewer"], event["review_state"]), ("github.pr_review", "bob", "approved"))

    def test_check_suite_and_status(self):
        suite = github_webhook.parse_github_event(
            {
                "action": "completed",
                "repository": REPO,
                "check_suite": {"head_sha": "h1", "conclusion": "success", "pull_requests": [{"number": 4}]},
            },
            "check_suite",
        )
        self.assertEqual((suite["kind"], suite["head_sha"]), ("github.pr_checks", "h1"))
        self.assertEqual(suite["pr_urls"], ["https://github.com/o/r/pull/4"])
        self.assertIsNone(
            github_webhook.parse_github_event({"action": "requested", "check_suite": {}}, "check_suite")
        )
        status = github_webhook.parse_github_event(
            {"sha": "h1", "state": "failure", "context": "ci", "repository": REPO}, "status"
        )
        self.assertEqual((status["kind"], status["conclusion"]), ("github.pr_checks", "failure"))

    def test_deployment_status(self):
        event = github_webhook.parse_github_event(
            {
                "repository": REPO,
                "deployment": {"id": 7, "sha": "m1", "environment": "staging"},
                "deployment_status": {"state": "success", "environment": "staging", "target_url": "https://ci/1"},
            }
        )
        self.assertEqual(
            (event["kind"], event["sha"], event["environment"], event["state"]),
            ("github.deployment_status", "m1", "staging", "success"),
        )

    def test_unhandled(self):
        self.assertIsNone(github_webhook.parse_github_event({"zen": "hi"}, "ping"))


class RouteEventTests(unittest.TestCase):
    def _run(self, body, event_name, waits, subscribers=None, **kwargs):
        engine = MagicMock()
        engine.resume_external.return_value = {"status": "resumed"}
        memory = MagicMock()
        memory.find_external_waits.return_value = {"waits": waits}
        registry = {kind: [] for kind in github_webhook.EVENT_SUBSCRIBERS}
        registry.update(subscribers or {})
        with (
            patch.dict(github_webhook.EVENT_SUBSCRIBERS, registry),
            patch.object(github_webhook, "get_workflow_engine", return_value=engine),
            patch.object(github_webhook, "get_memory_client", return_value=memory),
        ):
            result = github_webhook.handle_github_webhook(body, event_name=event_name, **kwargs)
        return result, engine, memory

    def test_merge_payload_carries_merged_by(self):
        body = {
            "action": "closed",
            "pull_request": {
                "merged": True,
                "html_url": "https://github.com/o/r/pull/9",
                "merge_commit_sha": "m1",
                "merged_by": {"login": "alice"},
            },
        }
        result, engine, _ = self._run(body, "pull_request", [{"workflow_id": "wf-1"}])
        self.assertEqual(result["merged_by"], "alice")
        self.assertEqual(engine.resume_external.call_args.kwargs["payload"]["merged_by"], "alice")

    def test_checks_resume_only_matching_sha_when_terminal(self):
        body = {"action": "completed", "repository": REPO, "check_suite": {"head_sha": "h1", "conclusion": "success"}}
        waits = [
            {"workflow_id": "wf-a", "match": {"head_sha": "h1"}},
            {"workflow_id": "wf-b", "match": {"head_sha": "other"}},
        ]
        resolver = MagicMock(
            return_value={
                "state": "success",
                "prs": [
                    {"pr_url": "u", "head_sha": "h1", "repo": "o/r", "state": "success", "checks": {"ci": "success"}, "failed": []}
                ],
            }
        )
        result, engine, memory = self._run(body, "check_suite", waits, check_resolver=resolver)
        self.assertEqual(memory.find_external_waits.call_args.kwargs["event_type"], "github.pr_checks")
        self.assertEqual(result["status"], "resumed")
        engine.resume_external.assert_called_once()
        call = engine.resume_external.call_args
        self.assertEqual(call.args[0], "wf-a")
        self.assertEqual(call.kwargs["payload"]["checks_state"], "success")
        self.assertEqual(call.kwargs["payload"]["checks"], {"ci": "success"})

    def test_multi_pr_wait_matches_any_pinned_head(self):
        body = {"action": "completed", "repository": REPO, "check_suite": {"head_sha": "h2"}}
        waits = [{"workflow_id": "wf-multi", "match": {}, "options": {"heads": [{"head_sha": "h1"}, {"head_sha": "h2"}]}}]
        resolver = MagicMock(return_value={"state": "failure", "prs": []})
        result, engine, _ = self._run(body, "check_suite", waits, check_resolver=resolver)
        self.assertEqual(result["status"], "resumed")
        self.assertEqual(engine.resume_external.call_args.kwargs["payload"]["checks_state"], "failure")

    def test_checks_still_pending_do_not_resume(self):
        body = {"action": "completed", "repository": REPO, "check_suite": {"head_sha": "h1"}}
        resolver = MagicMock(return_value={"state": "pending", "prs": []})
        result, engine, _ = self._run(
            body, "check_suite", [{"workflow_id": "wf-a", "match": {"head_sha": "h1"}}], check_resolver=resolver
        )
        self.assertEqual((result["status"], result["pending"]), ("pending", ["wf-a"]))
        engine.resume_external.assert_not_called()

    def test_wait_without_match_is_never_resumed(self):
        body = {"action": "completed", "repository": REPO, "check_suite": {"head_sha": "h1"}}
        result, engine, _ = self._run(
            body, "check_suite", [{"workflow_id": "wf-a", "match": {}}], check_resolver=MagicMock()
        )
        self.assertEqual(result["status"], "no_match")
        engine.resume_external.assert_not_called()

    def test_deployment_matches_sha_and_environment(self):
        body = {
            "repository": REPO,
            "deployment": {"id": 7, "sha": "m1", "environment": "staging"},
            "deployment_status": {"state": "success", "environment": "staging"},
        }
        waits = [
            {"workflow_id": "wf-prod", "match": {"sha": "m1", "environment": "production"}},
            {"workflow_id": "wf-stg", "match": {"sha": "m1", "environment": "staging"}},
        ]
        result, engine, _ = self._run(body, "deployment_status", waits)
        self.assertEqual([r["workflow_id"] for r in result["resumed"]], ["wf-stg"])
        self.assertEqual(engine.resume_external.call_args.kwargs["payload"]["deployment_id"], 7)

    def test_in_progress_deployment_waits(self):
        body = {
            "repository": REPO,
            "deployment": {"sha": "m1", "environment": "staging"},
            "deployment_status": {"state": "in_progress", "environment": "staging"},
        }
        result, engine, _ = self._run(
            body, "deployment_status", [{"workflow_id": "wf", "match": {"sha": "m1", "environment": "staging"}}]
        )
        self.assertEqual(result["status"], "pending")
        engine.resume_external.assert_not_called()

    def test_review_goes_to_subscribers(self):
        seen: list[dict[str, Any]] = []

        def subscriber(event, *, tenant_id):
            seen.append(event)
            return {"posted": True}

        body = {
            "action": "submitted",
            "repository": REPO,
            "pull_request": {"html_url": "u", "number": 1, "head": {"sha": "h1"}},
            "review": {"user": {"login": "bob"}, "state": "approved"},
        }
        with patch.object(github_webhook, "_resolve_subscriber", return_value=subscriber):
            result, engine, _ = self._run(
                body, "pull_request_review", [], subscribers={"github.pr_review": ["mod:fn"]}
            )
        self.assertEqual(result["status"], "notified")
        self.assertEqual(seen[0]["reviewer"], "bob")
        self.assertEqual(result["subscribers"][0]["result"], {"posted": True})
        engine.resume_external.assert_not_called()


class WaitMatchPersistenceTests(unittest.TestCase):
    def test_wait_external_persists_resolved_match(self):
        from types import SimpleNamespace

        from ts_harness.workflows.hooks import wait_external

        memory = MagicMock()
        ctx = SimpleNamespace(
            task={"id": "t1", "input": {"a": 1}},
            stage=SimpleNamespace(wait_external={"event_type": "github.pr_checks", "match": {"head_sha": "h1"}}),
            memory_client=memory,
            workflow_id="wf",
            actor="runner",
        )
        with patch.object(wait_external, "load_workflow_payload", return_value=({}, [])):
            out = wait_external.handle(ctx)
        self.assertEqual(out["match"], {"head_sha": "h1"})
        patched = memory.patch_task_input.call_args.args[1]
        self.assertEqual(patched, {"a": 1, "_wait_match": {"head_sha": "h1"}})


class PollerUsesClientTests(unittest.TestCase):
    def test_fetch_pull_request_goes_through_client(self):
        from ts_harness.gateway import system_jobs

        with patch.object(system_jobs.GitHubClient, "get_pull", return_value={"merged": False}) as get_pull:
            self.assertEqual(system_jobs.fetch_pull_request("o", "r", 1, token="t"), {"merged": False})
        get_pull.assert_called_once_with("o", "r", 1)

    def test_poller_forwards_merged_by(self):
        from ts_harness.gateway import system_jobs

        memory = MagicMock()
        memory.find_external_waits.return_value = {
            "waits": [{"workflow_id": "wf", "match_pr_url": "https://github.com/o/r/pull/1"}]
        }
        engine = MagicMock()
        with (
            patch.object(system_jobs, "get_memory_client", return_value=memory),
            patch.object(system_jobs, "get_workflow_engine", return_value=engine),
            patch.object(
                system_jobs,
                "fetch_pull_request",
                return_value={"merged": True, "merge_commit_sha": "m", "merged_by": {"login": "alice"}},
            ),
            patch.dict("os.environ", {"GITHUB_TOKEN": "t"}),
        ):
            system_jobs.poll_github_merges()
        self.assertEqual(engine.resume_external.call_args.kwargs["payload"]["merged_by"], "alice")


class GitHubWaitErrorTests(unittest.TestCase):
    def test_forbidden_is_not_retried_and_names_the_permission(self):
        retryable, message = classify_github_wait_error(GitHubError(403, "Resource not accessible by integration"))
        self.assertFalse(retryable)
        self.assertIn("actions read", message)
        self.assertIn("administration read", message)

    def test_server_error_is_retried(self):
        retryable, _message = classify_github_wait_error(GitHubError(502, "bad gateway"))
        self.assertTrue(retryable)

    def test_missing_pr_url_is_not_retried(self):
        retryable, message = classify_github_wait_error(ValueError("await_pr_checks resolved no PR URLs"))
        self.assertFalse(retryable)
        self.assertIn("no PR URLs", message)


if __name__ == "__main__":
    unittest.main()
