"""Agent-built programs: builder sessions, tools, bundle validation, and Activate all."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.agent.loop import EnterpriseAgent
from ts_harness.agent.provider import FakeProvider
from ts_harness.agent.sessions import SessionStore, get_session_store, reset_session_store
from ts_harness.agent.transcript import TranscriptStore, reset_transcript_store
from ts_harness.config.schema import load_runtime_config
from ts_harness.gateway import asgi_app
from ts_harness.gateway import program_builder_bff as builder
from ts_harness.gateway.auth import AuthContext, AuthError
from ts_harness.skills.authoring import approve_draft
from ts_harness.skills.catalog import SkillCatalog
from ts_harness.skills.roots import catalog_roots, managed_root
from ts_harness.tools.policy import ToolPolicyContext
from ts_harness.tools.registry import build_default_registry
from ts_harness.workflows import routing_definition_store as routing_store_module
from ts_harness.workflows.program_registry import (
    get_program_definition_registry,
    reset_program_definition_registry_for_tests,
)
from ts_harness.workflows.routing_definition_store import routing_definition_store

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_CONFIG = REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"

WORKFLOW = {
    "workflow_type": "team_digest",
    "definition_version": "1.0.0",
    "display_name": "Team digest",
    "stages": [
        {
            "name": "draft",
            "kind": "task",
            "agent_id": "pm-agent",
            "present_output": True,
            "tool_allow": ["read_file"],
        },
        {
            "name": "approve",
            "kind": "task",
            "agent_id": "orchestrator",
            "depends_on": ["draft"],
            "approval_required": True,
            "present_prompt": "approval",
        },
        {
            "name": "send",
            "kind": "task",
            "agent_id": "comms-agent",
            "depends_on": ["approve"],
            "tool_allow": ["read_file", "channel_send"],
        },
    ],
}

PROGRAM = {
    "program_type": "team_digest",
    "definition_version": "1.0.0",
    "display_name": "Team digest",
    "startable_nodes": ["digest"],
    "nodes": [
        {
            "id": "digest",
            "default_workflow_type": "team_digest",
            "workflow_variants": [],
            "required_artifacts": [],
            "produces_artifacts": ["digest"],
            "input_bindings": [{"kind": "program_summary", "target": "summary"}],
            "completion_adapter": "extract_declared_outputs",
            "terminal": True,
        }
    ],
    "edges": [],
}

INTENT = {
    "id": "team_digest",
    "signals": ["send the team digest", "start a team digest"],
    "program_type": "team_digest",
    "entry_phase": "digest",
}

SKILL_MD = """---
id: digest-writer
version: 1.0.0
category: digest
description: Writes the weekly team digest
---
# Digest writer

Summarize the week.
"""


def _bundle_args(**overrides):
    args = {
        "workflow": copy.deepcopy(WORKFLOW),
        "program": copy.deepcopy(PROGRAM),
        "routing_intent": copy.deepcopy(INTENT),
    }
    args.update(overrides)
    return args


def _admin(actor_id: str = "alice") -> AuthContext:
    return AuthContext(
        actor_id=actor_id,
        tenant_id="default",
        email=f"{actor_id}@example.com",
        roles=["admin"],
        auth_provider="oidc",
    )


class BuilderTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        (root / "skills").mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                "TS_HARNESS_WORKFLOW_DEFINITION_STORE": str(root / "workflows"),
                "TS_HARNESS_PROGRAM_DEFINITION_STORE": str(root / "programs"),
                "TS_HARNESS_ROUTING_DEFINITION_STORE": str(root / "routing"),
                "SKILLS_MANAGED_DIR": str(root / "skills"),
            },
        )
        self.env.start()
        self.routing_store = mock.patch.object(routing_store_module, "_STORE", None)
        self.routing_store.start()
        engine = mock.Mock()
        engine.reload_definitions.return_value = True
        self.engine = mock.patch(
            "ts_harness.workflows.engine.get_workflow_engine", return_value=engine
        )
        self.engine.start()
        reset_program_definition_registry_for_tests()
        reset_session_store(SessionStore(path=None))
        reset_transcript_store(TranscriptStore(path=None))

        config = load_runtime_config(TEST_CONFIG)
        catalog = SkillCatalog(roots=catalog_roots())
        catalog.load()
        locks: dict[str, threading.RLock] = {}
        self.runtime = SimpleNamespace(
            config=config,
            skill_catalog=catalog,
            audit=mock.Mock(),
            agent=mock.Mock(),
            _session_lock=lambda sid: locks.setdefault(sid, threading.RLock()),
        )
        self.tool_runtime = mock.patch(
            "ts_harness.tools.program_builder_tools._runtime", return_value=self.runtime
        )
        self.tool_runtime.start()
        self.alice = _admin("alice")

    def tearDown(self):
        self.tool_runtime.stop()
        self.engine.stop()
        self.routing_store.stop()
        self.env.stop()
        reset_program_definition_registry_for_tests()
        reset_session_store(SessionStore(path=None))
        reset_transcript_store(TranscriptStore(path=None))
        self._tmp.cleanup()

    def new_session(self, auth: AuthContext | None = None):
        auth = auth or self.alice
        session = builder._new_builder_session(self.runtime, auth)
        session.metadata = {**session.metadata, builder.ACTOR_KEY: builder.auth_snapshot(auth)}
        get_session_store().save(session)
        return session

    def reload(self, session):
        return get_session_store().get(session.session_id)

    def ctx(self, session, source: str = builder.BUILDER_CHANNEL) -> ToolPolicyContext:
        return ToolPolicyContext(
            tenant_id="default",
            agent_id="builder-agent",
            session_id=session.session_id,
            source=source,
        )

    def propose(self, session, **overrides):
        return self.call("propose_program_bundle", _bundle_args(**overrides), session)

    def call(self, tool: str, args, session, source: str = builder.BUILDER_CHANNEL):
        return build_default_registry().dispatch(tool, args, context=self.ctx(session, source))

    def activate(self, session, auth: AuthContext | None = None, revisions=None):
        auth = auth or self.alice
        if revisions is None:
            state = builder.builder_session_state(self.runtime, auth, session.session_id)
            revisions = {kind: state[kind]["revision"] for kind in builder.PARTS if state[kind]["revision"]}
        return builder.activate_builder_bundle(
            self.runtime, auth, session.session_id, {"expected_revisions": revisions}
        )


class ProposeBundleTests(BuilderTestCase):
    def test_propose_creates_three_drafts_as_the_person(self):
        session = self.new_session()
        result = self.propose(session)

        for kind in builder.PARTS:
            self.assertEqual(result[kind]["status"], "created", result)
            self.assertTrue(result[kind]["valid"], result[kind]["issues"])
        self.assertTrue(result["ready_to_activate"], result["problems"])

        bundle = builder.read_bundle(self.reload(session))
        for kind in builder.PARTS:
            draft = builder._load_draft(kind, bundle[kind]["draft_id"])
            self.assertEqual(draft["created_by"], "alice")
        routing = builder._load_draft("routing", bundle["routing"]["draft_id"])
        self.assertEqual([i["id"] for i in routing["definition"]["intents"]], ["team_digest"])

    def test_second_propose_updates_the_same_drafts(self):
        session = self.new_session()
        first = self.propose(session)
        workflow = copy.deepcopy(WORKFLOW)
        workflow["display_name"] = "Weekly team digest"
        second = self.propose(self.reload(session), workflow=workflow)

        self.assertEqual(second["workflow"]["status"], "updated")
        self.assertEqual(second["workflow"]["draft_id"], first["workflow"]["draft_id"])
        self.assertNotEqual(second["workflow"]["revision"], first["workflow"]["revision"])
        self.assertEqual(second["program"]["draft_id"], first["program"]["draft_id"])
        self.assertEqual(second["routing"]["draft_id"], first["routing"]["draft_id"])

    def test_person_edit_returns_revision_conflict_and_keeps_their_edit(self):
        from ts_harness.gateway.workflow_definitions_bff import (
            update_workflow_definition_draft_admin,
        )

        session = self.new_session()
        first = self.propose(session)
        edited = copy.deepcopy(WORKFLOW)
        edited["display_name"] = "Edited by alice"
        update_workflow_definition_draft_admin(
            self.runtime,
            self.alice,
            first["workflow"]["draft_id"],
            {"definition": edited, "expected_revision": first["workflow"]["revision"]},
        )

        conflict = self.propose(self.reload(session))
        self.assertEqual(conflict["workflow"]["status"], "revision_conflict")
        self.assertEqual(conflict["workflow"]["current_definition"]["display_name"], "Edited by alice")
        draft = builder._load_draft("workflow", first["workflow"]["draft_id"])
        self.assertEqual(draft["definition"]["display_name"], "Edited by alice")

        merged = copy.deepcopy(edited)
        merged["display_name"] = "Edited by alice, merged"
        retry = self.propose(self.reload(session), workflow=merged)
        self.assertEqual(retry["workflow"]["status"], "updated")

    def test_discarded_draft_is_recreated(self):
        from ts_harness.gateway.workflow_definitions_bff import workflow_definition_store

        session = self.new_session()
        first = self.propose(session)
        workflow_definition_store().delete_draft(first["workflow"]["draft_id"])

        state = builder.builder_session_state(self.runtime, self.alice, session.session_id)
        self.assertEqual(state["workflow"]["status"], "missing")
        self.assertFalse(state["ready"])

        again = self.propose(self.reload(session))
        self.assertEqual(again["workflow"]["status"], "created")
        self.assertNotEqual(again["workflow"]["draft_id"], first["workflow"]["draft_id"])

    def test_existing_types_are_refused(self):
        session = self.new_session()
        workflow = copy.deepcopy(WORKFLOW)
        workflow["workflow_type"] = "prd_request"
        result = self.propose(session, workflow=workflow)
        self.assertEqual(result["error"], "type_exists")
        self.assertIn("prd_request", result["message"])
        self.assertNotIn("workflow", builder.read_bundle(self.reload(session)))

    def test_routing_is_blocked_by_another_routing_draft(self):
        from ts_harness.gateway.routing_definitions_bff import (
            create_routing_definition_draft_admin,
        )

        create_routing_definition_draft_admin(
            self.runtime,
            _admin("bob"),
            {
                "definition": {
                    "definition_type": "channel_routing",
                    "definition_version": "0.0.9",
                    "intents": [],
                }
            },
        )
        session = self.new_session()
        result = self.propose(session)
        self.assertEqual(result["workflow"]["status"], "created")
        self.assertEqual(result["program"]["status"], "created")
        self.assertEqual(result["routing"]["status"], "routing_blocked")
        self.assertEqual(len(routing_definition_store().list_drafts()), 1)

    def test_program_validates_against_pending_workflow(self):
        session = self.new_session()
        result = self.propose(session)
        codes = {issue["code"] for issue in result["program"]["issues"]}
        self.assertNotIn("unknown_workflow_type", codes)
        self.assertTrue(result["program"]["valid"], result["program"]["issues"])

    def test_ungated_publish_stage_is_an_error_in_the_builder(self):
        session = self.new_session()
        workflow = copy.deepcopy(WORKFLOW)
        workflow["stages"][1].pop("approval_required")
        result = self.propose(session, workflow=workflow)

        issues = [i for i in result["workflow"]["issues"] if i["code"] == "ungated_publish_stage"]
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]["severity"], "error")
        self.assertFalse(result["ready_to_activate"])

    def test_builder_catalog_lists_publish_tools_and_drafts(self):
        session = self.new_session()
        self.propose(session)
        catalog = self.call("builder_catalog", {}, self.reload(session))
        self.assertIn("channel_send", catalog["publish_tools"])
        self.assertNotIn("google_workspace_gmail_send", catalog["publish_tools"])
        self.assertIn("comms-agent", catalog["agent_ids"])
        self.assertIn("prd_request", catalog["existing_workflow_types"])
        self.assertEqual(
            catalog["bundle_drafts"]["workflow"]["definition"]["workflow_type"], "team_digest"
        )


class ToolGuardTests(BuilderTestCase):
    def test_tools_refuse_other_sources(self):
        session = self.new_session()
        result = self.call("propose_program_bundle", _bundle_args(), session, source="slack")
        self.assertIn("only on the program builder page", result["error"])

    def test_tools_refuse_session_without_person(self):
        session = builder._new_builder_session(self.runtime, self.alice)
        result = self.propose(session)
        self.assertIn("no person attached", result["error"])

    def test_tools_refuse_person_without_edit_config(self):
        viewer = AuthContext(actor_id="vic", tenant_id="default", roles=["viewer"], auth_provider="oidc")
        session = self.new_session(viewer)
        result = self.propose(session)
        self.assertIn("edit_config", result["error"])

    def test_propose_skill_on_builder_is_tracked_and_blocks_activation(self):
        session = self.new_session()
        proposed = self.call("propose_skill", {"markdown": SKILL_MD}, session)
        self.assertEqual(proposed["status"], "draft", proposed)

        bundle = builder.read_bundle(self.reload(session))
        self.assertEqual(bundle["skill_ids"], ["digest-writer"])
        drafts = json.dumps(
            [p.read_text() for p in (managed_root() / "drafts").rglob("*") if p.is_file()]
        )
        self.assertIn("alice", drafts)

        result = self.propose(self.reload(session))
        self.assertFalse(result["ready_to_activate"])
        self.assertEqual(result["skills"], [{"skill_id": "digest-writer", "status": "draft"}])

        approve_draft(root=managed_root(), skill_id="digest-writer", reviewer="alice")
        self.runtime.skill_catalog = SkillCatalog(roots=catalog_roots())
        self.runtime.skill_catalog.load()
        state = builder.builder_session_state(self.runtime, self.alice, session.session_id)
        self.assertEqual(state["skills"], [{"skill_id": "digest-writer", "status": "approved"}])
        self.assertTrue(state["ready"], state["problems"])


class ActivateAllTests(BuilderTestCase):
    def test_activate_all_in_order(self):
        session = self.new_session()
        self.propose(session)
        result = self.activate(session)

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            [(s["kind"], s["status"]) for s in result["steps"]],
            [("workflow", "activated"), ("program", "activated"), ("routing", "activated")],
        )
        self.assertIn("team_digest", get_program_definition_registry().list_types())
        active = routing_definition_store().get_active("channel_routing")
        self.assertEqual([i["id"] for i in active["intents"]], ["team_digest"])
        self.assertTrue(
            any(intent.id == "team_digest" for intent in self.runtime.config.routing.intents)
        )
        state = result["state"]
        for kind in builder.PARTS:
            self.assertEqual(state[kind]["status"], "activated")
        actions = [call.kwargs["action_type"] for call in self.runtime.audit.append.call_args_list]
        self.assertEqual(
            actions,
            [
                "workflow_definition_activated",
                "program_definition_activated",
                "routing_definition_activated",
            ],
        )

    def test_not_ready_bundle_is_refused(self):
        session = self.new_session()
        workflow = copy.deepcopy(WORKFLOW)
        workflow["stages"][1].pop("approval_required")
        self.propose(session, workflow=workflow)
        with self.assertRaises(builder.BuilderNotReadyError) as raised:
            self.activate(session)
        self.assertFalse(raised.exception.state["ready"])
        self.assertIsNone(routing_definition_store().get_active("channel_routing"))

    def test_stops_at_first_failure_then_retries_remaining(self):
        session = self.new_session()
        self.propose(session)
        state = builder.builder_session_state(self.runtime, self.alice, session.session_id)
        revisions = {kind: state[kind]["revision"] for kind in builder.PARTS}

        first = self.activate(session, revisions={**revisions, "program": "stale"})
        self.assertFalse(first["ok"])
        self.assertEqual(
            [(s["kind"], s["status"]) for s in first["steps"]],
            [("workflow", "activated"), ("program", "failed"), ("routing", "not_run")],
        )

        retry = self.activate(session)
        self.assertTrue(retry["ok"], retry)
        self.assertEqual(
            [(s["kind"], s["status"]) for s in retry["steps"]],
            [("workflow", "already_active"), ("program", "activated"), ("routing", "activated")],
        )

    def test_routing_reload_failure_is_a_failed_step(self):
        session = self.new_session()
        self.propose(session)
        with mock.patch(
            "ts_harness.gateway.routing_definitions_bff.reload_routing_into_runtime",
            return_value=False,
        ):
            result = self.activate(session)
        self.assertFalse(result["ok"])
        self.assertEqual(result["steps"][2]["kind"], "routing")
        self.assertEqual(result["steps"][2]["status"], "failed")

    def test_editing_after_activation_reuses_activated_types(self):
        session = self.new_session()
        self.propose(session)
        self.assertTrue(self.activate(session)["ok"])

        workflow = copy.deepcopy(WORKFLOW)
        workflow["definition_version"] = "1.1.0"
        result = self.propose(self.reload(session), workflow=workflow)
        self.assertNotIn("error", result)
        self.assertEqual(result["workflow"]["status"], "created")


class OwnershipTests(BuilderTestCase):
    def test_other_person_cannot_read_or_activate(self):
        session = self.new_session()
        self.propose(session)
        bob = _admin("bob")
        with self.assertRaises(AuthError) as read:
            builder.builder_session_state(self.runtime, bob, session.session_id)
        self.assertEqual(read.exception.status, 403)
        with self.assertRaises(AuthError):
            builder.activate_builder_bundle(self.runtime, bob, session.session_id, {})
        with self.assertRaises(AuthError):
            builder.handle_builder_message(
                self.runtime, bob, {"session_id": session.session_id, "text": "hi"}
            )
        self.assertEqual(builder.list_builder_sessions(bob)["sessions"], [])
        self.assertEqual(len(builder.list_builder_sessions(self.alice)["sessions"]), 1)

    def test_unknown_session_is_not_found(self):
        with self.assertRaises(KeyError):
            builder.builder_session_state(self.runtime, self.alice, "missing")


class MessageTests(BuilderTestCase):
    def _chat_result(self):
        return SimpleNamespace(status="succeeded", output={"summary": "Drafted."}, error=None, messages=[])

    def test_message_runs_builder_agent_as_the_logged_in_person(self):
        self.runtime.agent.run_channel_chat.return_value = self._chat_result()
        result = builder.handle_builder_message(
            self.runtime,
            self.alice,
            {
                "text": "Build it",
                "intake": {"name": "Team digest", "channels": ["#team"]},
                "builder_actor": {"actor_id": "mallory"},
            },
        )
        self.assertEqual(result["status"], "succeeded")
        kwargs = self.runtime.agent.run_channel_chat.call_args.kwargs
        self.assertEqual(kwargs["agent_id"], "builder-agent")
        self.assertEqual(kwargs["source"], builder.BUILDER_CHANNEL)
        self.assertEqual(kwargs["session_mode"], builder.BUILDER_CHANNEL)
        self.assertEqual(kwargs["actor_email"], "alice@example.com")
        self.assertIn("Program name: Team digest", kwargs["text"])
        self.assertIn("Channels: #team", kwargs["text"])

        session = get_session_store().get(result["session_id"])
        self.assertEqual(session.metadata[builder.OWNER_KEY], "alice")
        self.assertEqual(session.metadata[builder.ACTOR_KEY]["actor_id"], "alice")

        second = builder.handle_builder_message(
            self.runtime, self.alice, {"session_id": result["session_id"], "text": "More", "intake": {"name": "X"}}
        )
        self.assertEqual(second["session_id"], result["session_id"])
        self.assertEqual(self.runtime.agent.run_channel_chat.call_args.kwargs["text"], "More")

    def test_two_builds_get_two_sessions(self):
        self.runtime.agent.run_channel_chat.return_value = self._chat_result()
        first = builder.handle_builder_message(self.runtime, self.alice, {"text": "one"})
        second = builder.handle_builder_message(self.runtime, self.alice, {"text": "two"})
        self.assertNotEqual(first["session_id"], second["session_id"])

    def test_agent_loop_calls_propose_program_bundle(self):
        args = json.dumps(_bundle_args())
        provider = FakeProvider([
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-propose",
                                    "type": "function",
                                    "function": {"name": "propose_program_bundle", "arguments": args},
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"message": {"role": "assistant", "content": "Drafts are ready for review."}}]},
        ])
        self.runtime.agent = EnterpriseAgent(self.runtime.config, build_default_registry(), provider=provider)
        result = builder.handle_builder_message(self.runtime, self.alice, {"text": "Build a team digest"})
        self.assertEqual(result["status"], "succeeded", result)

        state = builder.builder_session_state(self.runtime, self.alice, result["session_id"])
        for kind in builder.PARTS:
            self.assertEqual(state[kind]["status"], "draft", state)
        self.assertTrue(state["ready"], state["problems"])
        advertised = {
            tool["function"]["name"] for tool in provider.calls[0]["tools"]
        }
        self.assertTrue({"builder_catalog", "propose_program_bundle", "propose_skill"} <= advertised)
        self.assertFalse({"exec", "write_file", "delegate_task"} & advertised)


class RouteTests(BuilderTestCase):
    def setUp(self):
        super().setUp()
        self.auth = self.alice
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(asgi_app, "_auth", side_effect=lambda request, body: self.auth),
            mock.patch.object(asgi_app, "_check_origin", return_value=None),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        super().tearDown()

    def test_routes_need_edit_config(self):
        self.auth = AuthContext(actor_id="vic", tenant_id="default", roles=["viewer"], auth_provider="oidc")
        self.assertEqual(self.client.post("/admin/program-builder/message", json={"text": "hi"}).status_code, 403)
        self.assertEqual(self.client.get("/admin/program-builder/sessions").status_code, 403)
        self.assertEqual(self.client.get("/admin/program-builder/sessions/s1").status_code, 403)
        self.assertEqual(
            self.client.post("/admin/program-builder/sessions/s1/activate", json={}).status_code, 403
        )
        self.runtime.agent.run_channel_chat.assert_not_called()

    def test_routes_end_to_end(self):
        session = self.new_session()
        self.propose(session)
        sid = session.session_id

        state = self.client.get(f"/admin/program-builder/sessions/{sid}")
        self.assertEqual(state.status_code, 200)
        self.assertTrue(state.json()["ready"])
        listed = self.client.get("/admin/program-builder/sessions").json()
        self.assertEqual([item["session_id"] for item in listed["sessions"]], [sid])
        self.assertEqual(self.client.get("/admin/program-builder/sessions/nope").status_code, 404)

        stale = self.client.post(
            f"/admin/program-builder/sessions/{sid}/activate",
            json={"expected_revisions": {"workflow": "stale"}},
        )
        self.assertEqual(stale.status_code, 200)
        self.assertFalse(stale.json()["ok"])
        self.assertEqual(stale.json()["steps"][0]["status"], "failed")

        revisions = {kind: state.json()[kind]["revision"] for kind in builder.PARTS}
        self.auth = _admin("bob")
        self.assertEqual(
            self.client.post(
                f"/admin/program-builder/sessions/{sid}/activate",
                json={"expected_revisions": revisions},
            ).status_code,
            403,
        )
        self.auth = self.alice
        done = self.client.post(
            f"/admin/program-builder/sessions/{sid}/activate",
            json={"expected_revisions": revisions},
        )
        self.assertEqual(done.status_code, 200, done.text)
        self.assertTrue(done.json()["ok"])

    def test_not_ready_activation_is_409(self):
        session = self.new_session()
        response = self.client.post(
            f"/admin/program-builder/sessions/{session.session_id}/activate", json={}
        )
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["state"]["ready"])


class ToolPolicyTests(unittest.TestCase):
    def test_builder_tools_only_on_builder_agent(self):
        config = load_runtime_config(TEST_CONFIG)
        from ts_harness.tools.toolsets import agent_effective_tools

        for agent in config.agents:
            tools = agent_effective_tools(agent, config)
            has = {"builder_catalog", "propose_program_bundle"} & set(tools)
            if agent.id == "builder-agent":
                self.assertEqual(has, {"builder_catalog", "propose_program_bundle"})
                self.assertFalse({"exec", "write_file", "patch_file", "delegate_task"} & set(tools))
                self.assertFalse([tool for tool in tools if tool.startswith("mcp-")])
            else:
                self.assertFalse(has, agent.id)
        self.assertEqual(config.program_builder.agent_id, "builder-agent")

    def test_builder_agent_is_in_every_config_file(self):
        import yaml

        names = (
            "ts-harness-runtime.yaml.tmpl",
            "ts-harness-runtime.test.yaml",
        )
        for name in names:
            with self.subTest(name=name):
                data = yaml.safe_load((REPO_ROOT / "config" / name).read_text())
                agents = {agent["id"]: agent for agent in data["agents"]["list"]}
                self.assertIn("builder-agent", agents)
                self.assertEqual(agents["builder-agent"]["skills"], ["program-builder"])
                self.assertEqual(
                    data["toolsets"]["program-builder"], ["builder_catalog", "propose_program_bundle"]
                )
                self.assertEqual(data["program_builder"], {"agent_id": "builder-agent"})


if __name__ == "__main__":
    unittest.main()
