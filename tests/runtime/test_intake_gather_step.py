"""gather_intake_sources as a barrier system step, and $workflow.intake_sources."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ts_harness.intake import connectors
from ts_harness.intake.blobs import stash_stream
from ts_harness.storage.objects import LocalObjectStore
from ts_harness.workflows import system_steps
from ts_harness.workflows.expressions import resolve_value


class FakeMemory:
    def __init__(self, tasks: list[dict], workflow: dict | None = None) -> None:
        self.tasks = tasks
        self.workflow = workflow or {"workflow_id": "wf1", "tenant_id": "acme", "program_id": "p1"}

    def get_workflow(self, workflow_id: str) -> dict:
        return {"workflow": self.workflow, "tasks": self.tasks}


class GatherStepTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = LocalObjectStore(Path(tmp.name))
        patcher = mock.patch("ts_harness.intake.blobs.intake_store", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pointer = stash_stream(
            tenant_id="acme", name="brief.txt", mime="text/plain", fileobj=io.BytesIO(b"Build a login page"),
            size=18, store=self.store,
        )

    def _tasks(self, intake_sources) -> list[dict]:
        first = {"task_id": "t0", "step_name": "pick_repo", "created_at": "1", "status": "succeeded",
                 "input": {"summary": "login", "intake_sources": intake_sources}, "output": {}}
        return [first]

    def _run(self, tasks, config=None):
        barrier = {"task_id": "t1", "workflow_id": "wf1", "step_name": "gather_sources", "created_at": "2", "input": {}}
        return system_steps.run_system_step("gather_intake_sources", barrier, config or {}, memory=FakeMemory(tasks))

    def test_reads_a_stored_pointer_from_the_first_task_input(self):
        result = self._run(self._tasks([self.pointer]))
        self.assertEqual(result["status"], "succeeded")
        output = result["output"]
        self.assertEqual(output["sources"][0]["text"], "Build a login page")
        self.assertIn("Build a login page", output["sources_text"])
        self.assertNotIn("content_base64", json.dumps(self._tasks([self.pointer])))

    def test_missing_list_is_empty_and_builds_no_clients(self):
        with mock.patch.object(connectors, "build_download") as download, \
                mock.patch.object(connectors, "build_drive") as drive:
            result = self._run(self._tasks(None))
        self.assertEqual(result["output"], {"sources": [], "sources_text": "", "skipped": []})
        download.assert_not_called()
        drive.assert_not_called()

    def test_not_a_list_blocks(self):
        result = self._run(self._tasks({"kind": "stored"}))
        self.assertEqual(result["status"], "blocked")
        self.assertIn("must be a list", result["reason"])

    def test_drive_client_uses_the_configured_mailbox_only_when_needed(self):
        drive_pointer = {"kind": "drive", "file_id": "abcdefghijkl", "url": "https://drive.google.com/file/d/abcdefghijkl"}
        with mock.patch.object(connectors, "build_drive", return_value=None) as build_drive:
            self._run(self._tasks([self.pointer]))
            build_drive.assert_not_called()
            result = self._run(self._tasks([drive_pointer]), {"agent_id": "sales-agent"})
        build_drive.assert_called_once_with(tenant_id="acme", agent_id="sales-agent")
        self.assertEqual(result["output"]["sources"][0]["error"], "Google Drive is not connected")

    def test_image_reading_can_be_turned_off(self):
        image = stash_stream(tenant_id="acme", name="shot.png", mime="image/png", fileobj=io.BytesIO(b"\x89PNG"),
                             size=4, store=self.store)
        with mock.patch.dict("os.environ", {"TS_HARNESS_INTAKE_VISION_MODEL": "off"}):
            result = self._run(self._tasks([image]))
        self.assertEqual(result["output"]["sources"][0]["error"], "image reading is not enabled")

    def test_images_are_read_with_the_stage_agents_model(self):
        image = stash_stream(tenant_id="acme", name="shot.png", mime="image/png", fileobj=io.BytesIO(b"\x89PNG"),
                             size=4, store=self.store)
        provider = mock.Mock(endpoint=mock.Mock(api_mode="openai"))
        provider.chat.return_value = {"choices": [{"message": {"content": "a checkout screen"}}]}
        with mock.patch.dict("os.environ", {"TS_HARNESS_INTAKE_VISION_MODEL": ""}), \
                mock.patch.object(connectors, "agent_model", return_value="openai/gpt-4o") as agent_model, \
                mock.patch("ts_harness.agent.provider.build_provider", return_value=provider) as build:
            result = self._run(self._tasks([image]), {"agent_id": "sales-agent"})
        agent_model.assert_called_once_with("sales-agent")
        self.assertEqual(build.call_args.args[0], "openai/gpt-4o")
        self.assertEqual(result["output"]["sources"][0]["text"], "a checkout screen")

    def test_gather_context_can_include_sources(self):
        repo_context = {"repo": "acme/app", "tree": ["a.py"]}
        barrier = {"task_id": "t1", "workflow_id": "wf1", "created_at": "2", "input": {}}
        memory = FakeMemory(self._tasks([self.pointer]))
        with mock.patch("ts_harness.workflows.bug_context.gather_bug_context", return_value=repo_context):
            plain = system_steps.run_system_step("gather_bug_context", barrier, {}, memory=memory)
            merged = system_steps.run_system_step(
                "gather_bug_context", barrier, {"include_intake_sources": True}, memory=memory
            )
        self.assertEqual(plain["output"], repo_context)
        self.assertEqual(merged["output"]["repo"], "acme/app")
        self.assertEqual(merged["output"]["sources"][0]["text"], "Build a login page")


class WorkflowFieldTests(unittest.TestCase):
    def test_workflow_intake_sources_resolves_from_the_first_task_input(self):
        pointers = [{"kind": "drive", "file_id": "abcdefghijkl"}]
        tasks = [{"task_id": "t0", "created_at": "1", "status": "succeeded", "input": {"intake_sources": pointers}}]
        from ts_harness.workflows.bug_context import _input_extras

        extras = _input_extras({"input": {}}, tasks)
        self.assertEqual(resolve_value("$workflow.intake_sources", tasks=tasks, workflow={}, extras=extras), pointers)


class VisionModelTests(unittest.TestCase):
    def _config(self):
        agent = mock.Mock(id="pm-agent", model="openai/gpt-4o")
        return mock.Mock(agents=[agent])

    def test_override_off_and_default(self):
        with mock.patch.object(connectors, "agent_model", return_value="openai/gpt-4o"):
            with mock.patch.dict("os.environ", {"TS_HARNESS_INTAKE_VISION_MODEL": ""}):
                self.assertEqual(connectors.vision_model("pm-agent"), "openai/gpt-4o")
            with mock.patch.dict("os.environ", {"TS_HARNESS_INTAKE_VISION_MODEL": "anthropic/cheap"}):
                self.assertEqual(connectors.vision_model("pm-agent"), "anthropic/cheap")
            for off in ("off", "OFF", "false", "none"):
                with mock.patch.dict("os.environ", {"TS_HARNESS_INTAKE_VISION_MODEL": off}):
                    self.assertIsNone(connectors.vision_model("pm-agent"))
                    self.assertIsNone(connectors.build_image_describer(agent_id="pm-agent"))

    def test_agent_model_follows_the_admin_overlay(self):
        from ts_harness.config.schema import AgentConfig

        baseline = mock.Mock(agents=[AgentConfig(id="pm-agent", workspace="/w/pm", model="openai/gpt-4o")])
        store = mock.Mock()
        store.get_active.return_value = {"agents": {"pm-agent": {"model": "anthropic/newer", "workspace": "/w/pm"}}}
        with mock.patch.dict("os.environ", {"TS_HARNESS_CONFIG": "/cfg.yaml"}), \
                mock.patch.object(connectors, "_runtime_config", return_value=baseline), \
                mock.patch("ts_harness.workflows.agents_overlay_store.agents_overlay_store", return_value=store):
            self.assertEqual(connectors.agent_model("pm-agent"), "anthropic/newer")
            store.get_active.return_value = None
            self.assertEqual(connectors.agent_model("pm-agent"), "openai/gpt-4o")
            self.assertIsNone(connectors.agent_model("ghost-agent"))

    def test_no_config_means_no_model(self):
        with mock.patch.dict("os.environ", {"TS_HARNESS_CONFIG": ""}):
            self.assertIsNone(connectors.agent_model("pm-agent"))


class ResponsesApiImageTests(unittest.TestCase):
    def test_image_parts_are_converted(self):
        from ts_harness.agent.provider import _responses_input_items

        message = connectors._image_message("openai", "image/png", b"x")
        item = _responses_input_items(message)[0]
        self.assertEqual([part["type"] for part in item["content"]], ["input_text", "input_image"])
        self.assertTrue(item["content"][1]["image_url"].startswith("data:image/png;base64,"))

    def test_plain_text_messages_are_unchanged(self):
        from ts_harness.agent.provider import _responses_input_items

        self.assertEqual(_responses_input_items({"role": "user", "content": "hi"}), [{"role": "user", "content": "hi"}])
        self.assertEqual(_responses_input_items({"role": "user", "content": None}), [{"role": "user", "content": ""}])


class DescriberTests(unittest.TestCase):
    def test_message_shape_per_api(self):
        anthropic = connectors._image_message("anthropic", "image/png", b"x")
        self.assertEqual(anthropic["content"][0]["type"], "image")
        self.assertEqual(anthropic["content"][0]["source"]["media_type"], "image/png")
        openai = connectors._image_message("openai", "image/png", b"x")
        self.assertTrue(openai["content"][1]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_describer_calls_the_configured_model(self):
        provider = mock.Mock(endpoint=mock.Mock(api_mode="anthropic"))
        provider.chat.return_value = {"choices": [{"message": {"content": "a login form"}}]}
        with mock.patch.dict("os.environ", {"TS_HARNESS_INTAKE_VISION_MODEL": "anthropic/vision-model"}), \
                mock.patch("ts_harness.agent.provider.build_provider", return_value=provider) as build:
            describe = connectors.build_image_describer(scope={"tenant": "acme"})
            self.assertEqual(describe("a.png", "image/png", b"x"), "a login form")
        build.assert_called_once_with("anthropic/vision-model", scope={"tenant": "acme"})
        self.assertEqual(provider.chat.call_args.args[0][0]["content"][0]["type"], "image")

    def test_large_images_are_refused_before_calling_the_model(self):
        with mock.patch.dict("os.environ", {"TS_HARNESS_INTAKE_VISION_MODEL": "openai/m"}), \
                mock.patch("ts_harness.agent.provider.build_provider") as build:
            describe = connectors.build_image_describer()
            with self.assertRaises(connectors.SourceUnavailable):
                describe("a.png", "image/png", b"x" * (connectors.MAX_VISION_BYTES + 1))
        build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
