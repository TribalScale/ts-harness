"""Amending a running work order: console, admin API, ``/amend`` PR comments, history and acknowledgement."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timezone
from typing import Any
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.gateway import asgi_app, github_webhook, pr_commands, system_jobs
from ts_harness.workflows import work_order_amendments as amendments
from ts_harness.workflows.expressions import resolve_token
from ts_harness.workflows.qa_history import previous_qa_rounds

PR = "https://github.com/org/web/pull/7"
BODY = "# PROJ-11 Schedule blocks\n\n- Given a plan, When I open /hub/week, Then the page matches D1\n"
NOW = datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)


class FakeMemory:
    """Workflow tasks, runtime_kv and program artifacts, in memory."""

    def __init__(self, *, status: str = "running", tasks: list[dict[str, Any]] | None = None):
        self.workflow = {"id": "wf-1", "tenant_id": "default", "status": status, "workflow_type": "implement_jira_ticket",
                         "program_id": "prog-1", "external_ref": "PROJ-11"}
        self.tasks = tasks if tasks is not None else [
            {"id": "prep", "step_name": "prepare_ticket_delivery", "status": "succeeded", "created_at": "1",
             "input": {"jira_issue_key": "PROJ-11"}, "output": {"work_order_body": BODY, "work_order_id": "PROJ-11"}},
            {"id": "code", "step_name": "deliver_work_order", "status": "succeeded", "created_at": "2",
             "input": {"work_order_body": BODY}, "output": {"pr_url": PR}},
            {"id": "qa", "step_name": "qa_gate", "status": "ready", "created_at": "3",
             "input": {"work_order_body": BODY, "pr_url": PR}},
        ]
        self.kv: dict[tuple[str, str], tuple[Any, int]] = {}
        self.artifacts: dict[str, Any] = {}
        self.output_patches: list[tuple[str, str]] = []

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return {"workflow": dict(self.workflow), "tasks": copy.deepcopy(self.tasks)} if workflow_id == "wf-1" else {}

    def _task(self, task_id: str) -> dict[str, Any]:
        return next(t for t in self.tasks if t["id"] == task_id)

    def patch_task_output(self, task_id: str, output: dict[str, Any], *, actor: str = "", reason: str = "") -> dict[str, Any]:
        self._task(task_id)["output"] = copy.deepcopy(output)
        self.output_patches.append((task_id, actor))
        return {"id": task_id}

    def patch_task_input(self, task_id: str, input_data: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
        self._task(task_id)["input"] = copy.deepcopy(input_data)
        return {"id": task_id}

    def kv_get(self, namespace: str, key: str) -> dict[str, Any]:
        value, version = self.kv.get((namespace, key), (None, 0))
        return {"value": copy.deepcopy(value), "version": version}

    def kv_put(self, namespace: str, key: str, value: Any, *, expected_version: int | None = None) -> int:
        _, version = self.kv.get((namespace, key), (None, 0))
        self.kv[(namespace, key)] = (copy.deepcopy(value), version + 1)
        return version + 1

    def get_program_status(self, program_id: str) -> dict[str, Any]:
        return {"artifacts": dict(self.artifacts)}

    def patch_program_artifacts(self, program_id: str, artifacts: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.artifacts.update(artifacts)
        return {}

    def list_programs(self, **kwargs: Any) -> dict[str, Any]:
        return {"programs": [{"id": "prog-1"}]}

    def get_program(self, program_id: str) -> dict[str, Any]:
        return {"program": {"id": program_id}, "workflows": [dict(self.workflow)]}

    def get_workflow_children(self, workflow_id: str) -> list[dict[str, Any]]:
        return []


def _amend(memory: FakeMemory, text: str = "Row surfaces (#F3F4F6) belong to PROJ-12.", **kwargs: Any) -> dict[str, Any]:
    return amendments.amend_work_order(
        memory, "wf-1", text, person="lee@example.com", actor="lee@example.com", source="console", now=NOW, **kwargs
    )


class AmendWorkOrderTests(unittest.TestCase):
    def test_amendment_lands_where_every_later_step_reads_the_work_order(self):
        memory = FakeMemory()
        result = _amend(memory)
        self.assertEqual(result["status"], "amended")
        body = memory._task("prep")["output"]["work_order_body"]
        self.assertTrue(body.startswith(BODY.rstrip()))
        self.assertIn("## Scope amendment (by lee@example.com, 2026-11-20)", body)
        self.assertIn("Row surfaces (#F3F4F6) belong to PROJ-12.", body)
        # $input.work_order_body resolves from the earliest succeeded output carrying it.
        self.assertEqual(resolve_token("$input.work_order_body", tasks=memory.tasks), body)
        # A step that already holds a copy and has not run yet gets it too; a finished one is left alone.
        self.assertIn("## Scope amendment", memory._task("qa")["input"]["work_order_body"])
        self.assertEqual(memory._task("code")["input"]["work_order_body"], BODY)
        self.assertEqual(result["tasks_patched"], 1)

    def test_history_keeps_the_original_and_every_amendment(self):
        memory = FakeMemory()
        _amend(memory, "First change.")
        _amend(memory, "Second change.")
        history = amendments.amendment_history(memory, "wf-1")
        self.assertEqual(history["original"], BODY)
        self.assertEqual([a["text"] for a in history["amendments"]], ["First change.", "Second change."])
        self.assertEqual(history["amendments"][1]["body_before"].count("## Scope amendment"), 1)
        self.assertEqual(history["current"].count("## Scope amendment"), 2)

    def test_one_amendment_per_comment(self):
        memory = FakeMemory()
        self.assertEqual(_amend(memory, comment_id="c-1")["status"], "amended")
        self.assertEqual(_amend(memory, comment_id="c-1")["status"], "duplicate")
        self.assertEqual(len(memory.output_patches), 1)

    def test_runner_reapplies_a_missing_amendment(self):
        memory = FakeMemory()
        _amend(memory)
        stale = {"work_order_body": BODY, "pr_url": PR}
        updated = amendments.with_recorded_amendments(memory, "wf-1", stale)
        self.assertIn("## Scope amendment", updated["work_order_body"])
        self.assertIs(amendments.with_recorded_amendments(memory, "wf-1", updated), updated)
        self.assertIs(amendments.with_recorded_amendments(FakeMemory(), "wf-1", stale), stale)

    def test_program_artifacts_carrying_the_body_are_amended(self):
        memory = FakeMemory()
        memory.artifacts = {"work_order_body": BODY}
        _amend(memory)
        self.assertIn("## Scope amendment", memory.artifacts["work_order_body"])

    def test_refusals(self):
        with self.assertRaisesRegex(amendments.AmendmentError, "empty"):
            _amend(FakeMemory(), "  ")
        with self.assertRaisesRegex(amendments.AmendmentError, "only a running work order"):
            _amend(FakeMemory(status="completed"))
        with self.assertRaisesRegex(amendments.AmendmentError, "no work order yet"):
            _amend(FakeMemory(tasks=[{"id": "prep", "step_name": "prepare_ticket_delivery", "status": "running"}]))


class AmendCommandTests(unittest.TestCase):
    def _run(self, memory: FakeMemory, *, permission: str = "write", body: str = "/amend Rows belong to PROJ-12.",
             comment_id: str = "c-9") -> tuple[dict[str, Any], list[str]]:
        said: list[str] = []
        engine = mock.MagicMock()
        result = pr_commands.handle_pr_command(
            {"pr_url": PR, "author": "dana", "body": body, "comment_id": comment_id}, memory=memory, engine=engine,
            config=None, reply=lambda url, text: said.append(text),
        )
        return result, said

    def test_amend_at_any_stage_from_a_writer(self):
        memory = FakeMemory()
        with mock.patch.object(pr_commands, "_write_permission", return_value="write"):
            result, said = self._run(memory)
        self.assertEqual(result["status"], "amended")
        self.assertIn("## Scope amendment (by @dana", memory._task("prep")["output"]["work_order_body"])
        self.assertIn("Rows belong to PROJ-12.", memory._task("prep")["output"]["work_order_body"])
        self.assertEqual(len(said), 1)
        self.assertIn("Amended PROJ-11", said[0])
        # The same comment again (the poller after the webhook) does nothing.
        with mock.patch.object(pr_commands, "_write_permission", return_value="write"):
            again, said = self._run(memory)
        self.assertEqual((again["status"], said), ("duplicate", []))

    def test_without_write_access_it_is_refused_on_the_pr(self):
        memory = FakeMemory()
        with mock.patch.object(pr_commands, "_write_permission", return_value="read"):
            result, said = self._run(memory)
        self.assertEqual(result["status"], "denied")
        self.assertIn("needs write access", said[0])
        self.assertNotIn("Scope amendment", memory._task("prep")["output"]["work_order_body"])
        # A refused comment is not answered again by the poller.
        self.assertTrue(amendments.comment_handled(memory, "wf-1", "c-9"))

    def test_a_pr_with_no_running_delivery(self):
        memory = FakeMemory(status="completed")
        result, said = self._run(memory)
        self.assertEqual(result["status"], "no_delivery")
        self.assertIn("no running delivery", said[0])

    def test_the_webhook_carries_amend_and_the_comment_id(self):
        event = github_webhook.parse_github_event({
            "action": "created", "issue": {"number": 7, "html_url": PR, "pull_request": {}},
            "comment": {"id": 4242, "body": "/amend rows belong to PROJ-12", "user": {"login": "dana"}},
            "repository": {"full_name": "org/web"}})
        self.assertEqual((event["kind"], event["comment_id"]), ("github.pr_comment", "4242"))
        self.assertEqual(pr_commands.parse_command(event["body"]), ("amend", "rows belong to PROJ-12"))
        with mock.patch("ts_harness.gateway.pr_commands.handle_pr_command", return_value={"status": "amended"}) as handle, \
                mock.patch.object(github_webhook, "notify_subscribers", return_value=[]), \
                mock.patch.object(github_webhook, "_handle_pr_feedback") as feedback:
            out = github_webhook.handle_github_webhook({
                "action": "created", "issue": {"number": 7, "html_url": PR, "pull_request": {}},
                "comment": {"id": 4242, "body": "/amend x", "user": {"login": "dana"}}, "repository": {"full_name": "org/web"}})
        self.assertEqual(out["status"], "amended")
        handle.assert_called_once()
        feedback.assert_not_called()

    def test_the_poller_applies_missed_comments_once(self):
        memory = FakeMemory()
        github = mock.MagicMock()
        github.list_issue_comments.return_value = [
            {"id": 1, "body": "looks good", "user": {"login": "dana"}},
            {"id": 2, "body": "/amend Toolbar belongs to PROJ-13.", "user": {"login": "dana"}},
        ]
        handled: list[str] = []

        def handle(event: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            handled.append(event["comment_id"])
            with mock.patch.object(pr_commands, "_write_permission", return_value="write"):
                return pr_commands.handle_amend_command(event, memory=memory, engine=mock.MagicMock(), config=None,
                                                        reply=lambda *a: None, **{k: v for k, v in kwargs.items() if k != "memory"})

        first = system_jobs.poll_pr_amendments(memory=memory, github=github, handle=handle)
        second = system_jobs.poll_pr_amendments(memory=memory, github=github, handle=handle)
        self.assertEqual((first["checked"], first["applied"]), (1, 1))
        self.assertEqual(second["applied"], 0)
        self.assertEqual(handled, ["2"])
        self.assertIn("Toolbar belongs to PROJ-13.", memory._task("prep")["output"]["work_order_body"])


class AmendApiTests(unittest.TestCase):
    def setUp(self):
        self.runtime = mock.Mock()
        self.runtime.config.gateway.default_tenant_id = "default"
        self.runtime.config.gateway.cors_origins = ["*"]
        self.auth = mock.Mock(actor_id="admin-1", roles=["admin"], tenant_id="default", email="lee@example.com")
        self.runtime.policy.evaluate_identity.side_effect = lambda identity, required: mock.Mock(
            decision="allow" if set(identity["roles"]) & set(required) else "deny", reason="admin or owner required"
        )
        self.memory = FakeMemory()
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "_auth", return_value=self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
            mock.patch.object(asgi_app, "get_memory_client", return_value=self.memory),
            mock.patch.object(asgi_app, "_require_task_program_level", return_value=None),
            mock.patch.object(amendments, "acknowledge", return_value={"pr": True, "chat": True}),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    def test_amend_and_read_the_history(self):
        response = self.client.post("/admin/workflows/amend", json={"workflow_id": "wf-1", "text": "Rows belong to PROJ-12."})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "amended")
        self.assertEqual(response.json()["acknowledged"], {"pr": True, "chat": True})
        self.assertIn("(by lee@example.com,", self.memory._task("prep")["output"]["work_order_body"])
        history = self.client.get("/admin/workflows/wf-1/amendments").json()
        self.assertEqual(history["original"], BODY)
        self.assertEqual(history["amendments"][0]["source"], "console")

    def test_errors(self):
        self.assertEqual(self.client.post("/admin/workflows/amend", json={"workflow_id": "wf-1"}).status_code, 400)
        self.assertEqual(self.client.post("/admin/workflows/amend", json={"workflow_id": "nope", "text": "x"}).status_code, 404)
        self.memory.workflow["status"] = "completed"
        response = self.client.post("/admin/workflows/amend", json={"workflow_id": "wf-1", "text": "x"})
        self.assertEqual(response.status_code, 409)
        self.memory.workflow["tenant_id"] = "other"
        self.assertEqual(self.client.get("/admin/workflows/wf-1/amendments").status_code, 404)


class PreviousQaRoundsTests(unittest.TestCase):
    def test_earlier_verdicts_reach_the_next_qa_call(self):
        tasks = [
            {"id": "qa-1", "step_name": "qa_gate", "status": "blocked", "created_at": "1", "output": {"status": "failed", "result": {"prs": [
                {"pr_url": PR, "status": "failed", "defects": [{"category": "test_data", "summary": "plan still loading"}],
                 "acceptance_criteria": [{"criterion": "matches D1", "status": "fail"}, {"criterion": "x", "status": "pass"}]}]}}},
            {"id": "qa-2", "step_name": "qa_gate", "status": "ready", "created_at": "2"},
            {"id": "code", "step_name": "deliver_work_order", "status": "succeeded", "created_at": "0", "output": {"pr_url": PR}},
        ]
        rounds = previous_qa_rounds(tasks, "qa-2")
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0]["prs"][0]["failed_criteria"], ["matches D1"])
        self.assertEqual(rounds[0]["prs"][0]["defects"][0]["category"], "test_data")
        self.assertEqual(previous_qa_rounds(tasks[1:], "qa-2"), [])


if __name__ == "__main__":
    unittest.main()
