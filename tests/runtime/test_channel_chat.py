import json
import unittest

from ts_harness.agent.loop import EnterpriseAgent
from ts_harness.agent.provider import FakeProvider
from ts_harness.agent.sessions import reset_session_store
from ts_harness.agent.transcript import TranscriptStore, reset_transcript_store
from ts_harness.config.schema import load_runtime_config
from ts_harness.skills.catalog import SkillCatalog
from ts_harness.tools.registry import build_default_registry
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


class ChannelChatTests(unittest.TestCase):
    def setUp(self):
        reset_session_store()
        reset_transcript_store(TranscriptStore(path=None))

    def test_run_channel_chat_uses_plain_user_text(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        captured: list[list[dict]] = []

        class CapturingProvider(FakeProvider):
            def chat(self, messages, tools=None):
                captured.append(messages)
                return super().chat(messages, tools=tools)

        provider = CapturingProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "We have dev-agent, ops-agent, and qa-agent.",
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        result = agent.run_channel_chat(
            tenant_id="default",
            agent_id="orchestrator",
            text="what agents do we have?",
            session_id="channel-sess-1",
            history=[],
            skill_prompt="Step 0 skill content",
            source="slack",
        )
        self.assertEqual(result.status, "succeeded")
        self.assertIn("dev-agent", result.output["summary"])
        user_messages = [m for m in captured[0] if m.get("role") == "user"]
        self.assertEqual(user_messages[-1]["content"], "what agents do we have?")
        self.assertNotIn("step_name", user_messages[-1]["content"])

    def test_run_channel_chat_replays_history(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        captured: list[list[dict]] = []

        class CapturingProvider(FakeProvider):
            def chat(self, messages, tools=None):
                captured.append(messages)
                return super().chat(messages, tools=tools)

        provider = CapturingProvider(
            [
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": "Earlier you asked about agents."}},
                    ]
                }
            ]
        )
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
        )
        agent.run_channel_chat(
            tenant_id="default",
            agent_id="orchestrator",
            text="follow up",
            session_id="channel-sess-2",
            history=history,
            source="slack",
        )
        roles = [m.get("role") for m in captured[0]]
        self.assertEqual(roles[1:3], ["user", "assistant"])
        self.assertEqual(captured[0][-2]["content"], "follow up")

    def test_channel_system_prompt_is_conversational(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        agent = EnterpriseAgent(config, build_default_registry(), provider=FakeProvider([]))
        cfg = config.get_agent("orchestrator")
        prompt = agent._build_channel_system_prompt(cfg, "route-task excerpt")
        self.assertIn("conversational channel message", prompt)
        self.assertIn("Step 0", prompt)
        self.assertIn("call program_status", prompt)
        self.assertNotIn("Complete the assigned workflow step", prompt)

    def test_draft_prd_workflow_prompt_requires_prd_markdown(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        agent = EnterpriseAgent(config, build_default_registry(), provider=FakeProvider([]))
        cfg = config.get_agent("pm-agent")
        prompt = agent._build_workflow_system_prompt(
            cfg,
            {
                "step_name": "draft_prd",
                "required_artifacts": ["prd_draft"],
                "skill_hints": ["draft-prd"],
            },
        )

        self.assertIn("Draft / Revise PRD mode", prompt)
        self.assertIn("prd_markdown", prompt)
        self.assertIn("Do not ask for more intake information", prompt)

    def test_intake_workflow_prompt_requires_clarifying_questions(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=FakeProvider([]),
        )
        cfg = config.get_agent("pm-agent")
        prompt = agent._build_workflow_system_prompt(
            cfg,
            {
                "step_name": "intake",
                "required_artifacts": ["clarified_requirements"],
                "skill_hints": ["clarify-requirements"],
            },
        )

        self.assertIn("Clarifying Intake mode", prompt)
        self.assertIn("Ask", prompt)
        self.assertIn("questions", prompt)
        self.assertIn("Do not draft", prompt)
        self.assertNotIn("Produce a complete draft PRD", prompt)

    def test_work_orders_workflow_prompt_uses_governed_skill_hint(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        catalog = SkillCatalog(REPO_ROOT / "skills")
        catalog.load()
        resolved, _ = catalog.resolve_for_workflow_dispatch(
            config.get_agent("pm-agent"),
            config,
            tenant_id="default",
        )
        skill_bundle = catalog.build_prompt_bundle(resolved)
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=FakeProvider([]),
        )
        cfg = config.get_agent("pm-agent")
        prompt = agent._build_workflow_system_prompt(
            cfg,
            {
                "workflow_type": "work_orders_from_prd",
                "step_name": "design_blueprints",
                "skill_hints": ["prd-to-work-orders-governed"],
                "_skill_prompt": skill_bundle["prompt"],
                "input": {
                    "approved_prd": {"summary": "Online checkers PRD"},
                    "stage": "foundry",
                },
            },
        )

        self.assertNotIn("WORKFLOW DISPATCH OVERRIDE", prompt)
        self.assertIn("prd-to-work-orders-governed", prompt)
        self.assertIn("input.stage", prompt)
        self.assertIn("Never infer stage", prompt)

    def test_work_orders_dispatch_blocks_action_plan_without_artifacts(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        content = """### Planner Stage Execution

1. Review PRD.
2. Create Work Orders.
3. Define Execution Phases.
4. Prepare Context Graph.
5. Assess Executability.
"""
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": content,
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
        )

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "planner-1",
                "workflow_type": "work_orders_from_prd",
                "step_name": "plan_work_orders",
                "skill_hints": ["prd-to-work-orders-governed"],
                "input": {
                    "approved_prd": {"summary": "Online checkers PRD"},
                    "stage": "planner",
                },
            },
        )

        self.assertEqual(result.status, "blocked")
        self.assertIn("artifacts", result.error or "")

    def test_work_orders_dispatch_accepts_stage_artifacts(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        content = json.dumps(
            {
                "summary": "Planner artifacts ready.",
                "artifacts": {
                    "work_orders_markdown": (
                        "# Work Orders\n\n"
                        "## WO-001 Lobby service\n"
                        "Build the lobby API that lists open tables, lets a "
                        "player create a table, and lets a second player join. "
                        "Acceptance: a created table appears in the list within "
                        "one second and disappears once two players are seated.\n\n"
                        "## WO-002 Game board\n"
                        "Render the eight-by-eight board, validate diagonal "
                        "moves and captures on the server, and broadcast each "
                        "accepted move to both players over the realtime "
                        "channel. Acceptance: illegal moves are rejected with a "
                        "reason and never reach the opponent's board.\n\n"
                        "## WO-003 Matchmaking\n"
                        "Pair waiting players by rating band and seat them at a "
                        "new table. Acceptance: two players in the same band are "
                        "paired within five seconds of both joining the queue."
                    ),
                    "context_graph_markdown": (
                        "# Context Graph\n\nWO-002 depends on WO-001; "
                        "WO-003 depends on WO-001."
                    ),
                    "execution_phases_markdown": (
                        "# Execution Phases\n\nPhase A: WO-001. "
                        "Phase B: WO-002 and WO-003 in parallel."
                    ),
                },
                "confidence": "high",
                "warnings": [],
                "follow_up_needed": False,
            }
        )
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": content,
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
        )

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "planner-1",
                "workflow_type": "work_orders_from_prd",
                "step_name": "plan_work_orders",
                "skill_hints": ["prd-to-work-orders-governed"],
                "input": {
                    "approved_prd": {"summary": "Online checkers PRD"},
                    "stage": "planner",
                },
            },
        )

        self.assertEqual(result.status, "succeeded")
        self.assertIn("work_orders_markdown", result.output["artifacts"])

    def test_refinery_dispatch_accepts_plain_markdown_artifacts(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        content = """# Product Overview

## Business Problem
Players need online multiplayer checkers.

## Personas
- Casual player
- Competitive player

# Feature Tree
- Lobby: list open tables, create a table, join a table
- Matchmaking: queue by rating band, seat paired players
- Game board: render the board, validate moves, broadcast moves
- Chat: table chat between seated players

# Requirements
REQ-001: Moves are validated on the server before broadcast.
REQ-002: Lobby updates reach clients within one second.

# FRD-001 Lobby
## User Stories
As a player, I want to join a lobby so that I can find an opponent quickly.

## Acceptance Criteria
When a player opens the app, the system shall show available lobbies.
When a table fills, the system shall remove it from the lobby list.

# FRD-002 Game board
## User Stories
As a player, I want the board to reject illegal moves so the game stays fair.

## Acceptance Criteria
When a player submits an illegal move, the system shall reject it with a reason.
When a move is accepted, the system shall show it to both players.
"""
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": content,
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
        )

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "refinery-1",
                "workflow_type": "work_orders_from_prd",
                "step_name": "refine_requirements",
                "skill_hints": ["prd-to-work-orders-governed"],
                "input": {
                    "approved_prd": {"summary": "Online checkers PRD"},
                    "stage": "refinery",
                },
            },
        )

        self.assertEqual(result.status, "succeeded")
        self.assertIn("artifacts", result.output)
        artifacts = result.output["artifacts"]
        self.assertTrue(
            any(
                key in artifacts
                for key in (
                    "requirements_markdown",
                    "frds_markdown",
                    "feature_tree_markdown",
                    "product_overview_markdown",
                )
            )
        )

    def test_refinery_dispatch_blocks_prd_recap_without_stage_artifacts(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        content = (
            "### Key Points from the PRD:\n"
            "- **Problem Statement:** Casual gamers want checkers.\n"
            "- **Goals:** Launch web checkers with chat and leaderboard.\n"
            "- **Non-Goals:** Mobile apps.\n\n"
            "### Next Steps:\n"
            "- Human review of PRD\n"
        )
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": content,
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
        )

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "refinery-1",
                "workflow_type": "work_orders_from_prd",
                "step_name": "refine_requirements",
                "skill_hints": ["prd-to-work-orders-governed"],
                "input": {
                    "approved_prd": {"summary": "Online checkers PRD"},
                    "stage": "refinery",
                },
            },
        )

        self.assertEqual(result.status, "blocked")
        self.assertIn("refinery", result.error or "")

    def test_refinery_dispatch_blocks_catalog_without_frd_headings(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        content = json.dumps(
            {
                "summary": "Foundry artifacts ready.",
                "artifacts": {
                    "blueprints_markdown": (
                        "The blueprints have been successfully drafted and stored.\n\n"
                        "### Artifacts Produced\n\n"
                        "1. **Blueprints Markdown**:\n"
                        "   - **ADR-001**: Use WebSockets.\n"
                        "   - **ADR-002**: Deploy on Cloud Platform.\n"
                    ),
                },
                "confidence": "high",
                "warnings": [],
                "follow_up_needed": False,
            }
        )
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": content,
                            }
                        }
                    ]
                }
            ]
            * 3
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
        )

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "refinery-1",
                "workflow_type": "work_orders_from_prd",
                "step_name": "refine_requirements",
                "skill_hints": ["prd-to-work-orders-governed"],
                "input": {
                    "approved_prd": {"summary": "Online checkers PRD"},
                    "stage": "refinery",
                },
            },
        )

        self.assertEqual(result.status, "blocked")
        self.assertIn("enumerates artifacts", result.error or "")

    def test_foundry_dispatch_blocks_action_plan_without_artifacts(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        content = """### Steps for Foundry Stage

1. Review refined requirements and FRDs.
2. Design system blueprints and component boundaries.
3. Draft architecture decision records.
4. Prepare blueprint artifacts for planner review.
"""
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": content,
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
        )

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "foundry-1",
                "workflow_type": "work_orders_from_prd",
                "step_name": "design_blueprints",
                "skill_hints": ["prd-to-work-orders-governed"],
                "input": {
                    "approved_prd": {"summary": "Online checkers PRD"},
                    "stage": "foundry",
                },
            },
        )

        self.assertEqual(result.status, "blocked")
        self.assertIn("foundry", result.error or "")

    def test_foundry_dispatch_blocks_catalog_summary_with_inline_adr(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        content = json.dumps(
            {
                "summary": "Foundry artifacts ready.",
                "artifacts": {
                    "blueprints_markdown": (
                        "The blueprints have been successfully drafted and stored.\n\n"
                        "### Artifacts Produced\n\n"
                        "1. **Blueprints Markdown**:\n"
                        "   - Container Blueprints: Game Server, Chat Server\n"
                        "   - **ADR-001**: Use WebSockets for Real-Time Communication.\n"
                        "   - **ADR-002**: Deploy on Cloud Platform.\n"
                        "2. **Architecture Summary Markdown**:\n"
                        "   - Scalable cloud architecture overview.\n"
                    ),
                },
                "confidence": "high",
                "warnings": [],
                "follow_up_needed": False,
            }
        )
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": content,
                            }
                        }
                    ]
                }
            ]
            * 3
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
        )

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "foundry-1",
                "workflow_type": "work_orders_from_prd",
                "step_name": "design_blueprints",
                "skill_hints": ["prd-to-work-orders-governed"],
                "input": {
                    "approved_prd": {"summary": "Online checkers PRD"},
                    "stage": "foundry",
                },
            },
        )

        self.assertEqual(result.status, "blocked")
        self.assertIn("enumerates artifacts", result.error or "")

    def test_foundry_dispatch_accepts_structured_blueprint_artifacts(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        content = """# System Design

## FB-001 Lobby Service
The lobby service owns matchmaking queues and player sessions.
It exposes a REST API for lobby creation and WebSocket channels for live updates.

## COMP-002 Game Board Component
The game board renders the checkers grid, validates moves, and emits game events.
It depends on the lobby service for player identity and session state.

# Architecture Decision

## ADR-001 Real-time Sync
We will use WebSockets for move synchronization between players.
This keeps latency low for casual multiplayer sessions.

## ADR-002 Cloud Deployment
We will deploy stateless API containers behind a managed load balancer.
"""
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": content,
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
        )

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "foundry-1",
                "workflow_type": "work_orders_from_prd",
                "step_name": "design_blueprints",
                "skill_hints": ["prd-to-work-orders-governed"],
                "input": {
                    "approved_prd": {"summary": "Online checkers PRD"},
                    "stage": "foundry",
                },
            },
        )

        self.assertEqual(result.status, "succeeded")
        self.assertIn("artifacts", result.output)
        artifacts = result.output["artifacts"]
        self.assertTrue(
            any(
                key in artifacts
                for key in (
                    "blueprints_markdown",
                    "architecture_decisions_markdown",
                    "architecture_summary_markdown",
                )
            )
        )

    def test_prepare_handoff_dispatch_accepts_json_handoff(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        content = json.dumps(
            {
                "summary": "Prepared WO-001 for implement.",
                "planner_complete": True,
                "work_order_ids": ["WO-001", "WO-002"],
                "next_work_order_id": "WO-001",
                "work_orders_handoff": True,
                "work_order_ref": "WO-001",
                "confidence": "high",
                "warnings": [],
                "follow_up_needed": False,
            }
        )
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": content,
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
        )

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="orchestrator",
            payload={
                "workflow_id": "wf-1",
                "task_id": "prepare-1",
                "workflow_type": "work_orders_from_prd",
                "step_name": "prepare_work_order_delivery",
                "skill_hints": ["source-work-order-governed"],
                "input": {
                    "planner_artifacts": {
                        "work_orders_markdown": "# WO-001",
                    },
                },
            },
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.output["next_work_order_id"], "WO-001")
        self.assertTrue(result.output["work_orders_handoff"])

    def test_draft_prd_dispatch_blocks_without_prd_markdown(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": (
                                    "The draft PRD has been successfully created "
                                    "and the workflow task state has been updated."
                                ),
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
        )

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "draft-1",
                "step_name": "draft_prd",
                "required_artifacts": ["prd_draft"],
                "skill_hints": ["draft-prd"],
                "input": {"clarified_requirements": {"audience": "Casual gamers"}},
            },
        )

        self.assertEqual(result.status, "blocked")
        self.assertIn("prd_markdown", result.error or "")

    def test_intake_dispatch_blocks_prd_draft_without_questions(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        markdown = """### PRD: Online Multiplayer Checkers Game

#### 1. Problem Statement
Casual gamers need accessible online checkers for short play sessions.

#### 2. Goals
- Develop a web-based online multiplayer checkers game.

#### 3. User Stories
- As a gamer, I want to play checkers online.

#### 4. Acceptance Criteria
- Given a web browser, when a user opens the game, then they can start.

#### 5. Success Metrics
- Reach 500 daily active users within 30 days post-launch.

#### 6. Rollout Plan
- Internal testing -> Beta release -> Full launch.
"""
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": markdown,
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
        )

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "intake-1",
                "step_name": "intake",
                "required_artifacts": ["clarified_requirements"],
                "skill_hints": ["clarify-requirements"],
                "input": {"summary": "Create a PRD for checkers"},
            },
        )

        self.assertEqual(result.status, "blocked")
        self.assertIn("questions", result.error or "")

    def test_intake_dispatch_accepts_plain_text_questions(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        content = """To draft the PRD, please clarify:

1. Who is the target audience for the checkers game?
2. Should players be able to invite friends, match randomly, or both?
3. Which features are must-have for launch?
4. What success metrics should define launch success?
5. Is there a target launch timeline?
"""
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": content,
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "intake-1",
                "step_name": "intake",
                "required_artifacts": ["clarified_requirements"],
                "skill_hints": ["clarify-requirements"],
                "input": {"summary": "Create a PRD for checkers"},
            },
        )

        self.assertEqual(result.status, "succeeded")
        self.assertIn(
            "target audience",
            result.output["questions"][0],
        )
        self.assertEqual(result.output["follow_up_needed"], True)

    def test_draft_prd_dispatch_accepts_markdown_prd_without_json_wrapper(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        markdown = """Great, I've captured your responses. Here's a draft PRD:

## Product Requirements Document (PRD)

### Project Overview
Develop an online multiplayer checker game for casual gamers on the web.

### Target Audience
Casual gamers who want approachable strategic gameplay.

### Core Features
- Real-time multiplayer matches
- Turn-based play
- Chat functionality
- Leaderboards

### Success Metrics
Reach 500 active users after launch.
"""
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": markdown,
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "draft-1",
                "step_name": "draft_prd",
                "required_artifacts": ["prd_draft"],
                "skill_hints": ["draft-prd"],
                "input": {"clarified_requirements": {"audience": "Casual gamers"}},
            },
        )

        self.assertEqual(result.status, "succeeded")
        self.assertIn(
            "Product Requirements Document",
            result.output["result"]["prd_markdown"],
        )
        self.assertEqual(result.output["result"]["prd_status"], "Draft")

    def test_draft_prd_dispatch_accepts_pm_style_prd_sections(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        markdown = """### PRD: Online Multiplayer Checkers Game

#### 1. Problem Statement
Casual gamers need accessible online checkers for short play sessions.

#### 2. Goals
- Develop a web-based online multiplayer checkers game.
- Include chat functionality.
- Include a leaderboard to foster competition.

#### 3. User Stories
- As a gamer, I want to play checkers online with friends or random opponents.
- As a player, I want to chat with my opponent during the game.

#### 4. Acceptance Criteria
- Given a web browser, when a user opens the game, then they can start.
- Given an ongoing game, when a player sends chat, then the opponent sees it.

#### 5. Success Metrics
- Reach 500 daily active users within 30 days post-launch.

#### 6. Risks & Dependencies
- Technical risk around real-time multiplayer synchronization.

#### 7. Rollout Plan
- Internal testing -> Beta release -> Full launch.
"""
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": markdown,
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(
            config,
            build_default_registry(),
            provider=provider,
        )

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "draft-1",
                "step_name": "draft_prd",
                "required_artifacts": ["prd_draft"],
                "skill_hints": ["draft-prd"],
                "input": {
                    "clarified_requirements": {"audience": "Casual gamers"},
                },
            },
        )

        self.assertEqual(result.status, "succeeded")
        self.assertIn(
            "Online Multiplayer Checkers Game",
            result.output["result"]["prd_markdown"],
        )
        self.assertEqual(result.output["result"]["prd_status"], "Draft")

    def test_draft_prd_dispatch_accepts_artifacts_prd_markdown(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        content = json.dumps(
            {
                "summary": "Draft PRD ready.",
                "artifacts": {
                    "prd_title": "Online Checkers PRD",
                    "prd_markdown": (
                        "# Product Requirements Document\n\n"
                        "## Overview\n"
                        "Build a standard web checkers game for casual gamers.\n\n"
                        "## Target Audience\n"
                        "Casual gamers on the web.\n\n"
                        "## Core Features\n"
                        "- Chat functionality\n"
                        "- Leaderboard\n\n"
                        "## Success Metrics\n"
                        "Reach 500 active users."
                    ),
                },
            }
        )
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": content,
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "draft-1",
                "step_name": "draft_prd",
                "required_artifacts": ["prd_draft"],
                "skill_hints": ["draft-prd"],
                "input": {
                    "clarified_requirements": {"audience": "Casual gamers"},
                },
            },
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(
            result.output["result"]["prd_title"],
            "Online Checkers PRD",
        )
        self.assertIn(
            "standard web checkers",
            result.output["result"]["prd_markdown"],
        )

    def test_draft_prd_dispatch_accepts_fenced_json_result(self):
        config = load_runtime_config(
            REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"
        )
        content = (
            "```json\n"
            + json.dumps(
                {
                    "status": "ok",
                    "result": {
                        "prd_title": "Online Checkers PRD",
                        "prd_status": "Draft",
                        "prd_markdown": (
                            "# Product Requirements Document\n\n"
                            "## Overview\n"
                            "Build a standard web checkers game.\n\n"
                            "## Target Audience\n"
                            "Casual gamers on the web.\n\n"
                            "## Core Features\n"
                            "- Chat functionality\n"
                            "- Leaderboard\n\n"
                            "## Success Metrics\n"
                            "Reach 500 active users."
                        ),
                    },
                }
            )
            + "\n```"
        )
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": content,
                            }
                        }
                    ]
                }
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)

        result = agent.run_workflow_dispatch(
            tenant_id="default",
            agent_id="pm-agent",
            payload={
                "workflow_id": "wf-1",
                "task_id": "draft-1",
                "step_name": "draft_prd",
                "required_artifacts": ["prd_draft"],
                "skill_hints": ["draft-prd"],
                "input": {
                    "clarified_requirements": {"audience": "Casual gamers"},
                },
            },
        )

        self.assertEqual(result.status, "succeeded")
        self.assertIn(
            "standard web checkers",
            result.output["result"]["prd_markdown"],
        )

    def test_run_channel_chat_repairs_unpaired_tool_history(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        captured: list[list[dict]] = []

        class CapturingProvider(FakeProvider):
            def chat(self, messages, tools=None):
                captured.append(messages)
                return super().chat(messages, tools=tools)

        provider = CapturingProvider(
            [
                {
                    "choices": [
                        {"message": {"role": "assistant", "content": "Approvals are clear."}},
                    ]
                }
            ]
        )
        history = [
            {
                "role": "tool",
                "tool_call_id": "orphan",
                "content": "{\"error\": \"stale\"}",
            },
            {"role": "user", "content": "what is waiting?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-missing",
                        "type": "function",
                        "function": {"name": "session_status", "arguments": "{}"},
                    }
                ],
            },
        ]
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        result = agent.run_channel_chat(
            tenant_id="default",
            agent_id="orchestrator",
            text="what approvals are waiting?",
            session_id="repair-history",
            history=history,
            source="web",
        )
        self.assertEqual(result.status, "succeeded")
        replayed = captured[0]
        self.assertFalse(
            any(
                message.get("role") == "tool" and message.get("tool_call_id") == "orphan"
                for message in replayed
            )
        )
        missing = next(
            message
            for message in replayed
            if message.get("role") == "tool" and message.get("tool_call_id") == "call-missing"
        )
        self.assertIn("tool result unavailable", missing["content"])
        assistant_index = next(
            index
            for index, message in enumerate(replayed)
            if message.get("tool_calls")
        )
        self.assertLess(assistant_index, replayed.index(missing))

    def test_blank_channel_summary_is_replaced(self):
        config = load_runtime_config(REPO_ROOT / "config" / "ts-harness-runtime.test.yaml")
        provider = FakeProvider(
            [
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call-status",
                                        "type": "function",
                                        "function": {
                                            "name": "session_status",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"summary": ""}),
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": json.dumps({"summary": ""}),
                            }
                        }
                    ]
                },
            ]
        )
        agent = EnterpriseAgent(config, build_default_registry(), provider=provider)
        result = agent.run_channel_chat(
            tenant_id="default",
            agent_id="orchestrator",
            text="Draft the next step",
            session_id="blank-summary",
            source="web",
        )
        self.assertEqual(result.status, "succeeded")
        self.assertTrue(str(result.output.get("summary") or "").strip())


if __name__ == "__main__":
    unittest.main()
