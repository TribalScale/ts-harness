"""Tests for workflow artifact sanitization and channel formatting."""

from __future__ import annotations

import unittest

from ts_harness.workflows.artifacts import (
    artifact_body,
    format_artifact_for_channel,
    sanitize_prd_markdown,
)


class ArtifactSanitizationTests(unittest.TestCase):
    def test_strips_markdown_fence_and_language_tag(self):
        raw = """```markdown
# PRD: Widget

## Overview
Details here.
```"""
        cleaned = sanitize_prd_markdown(raw)
        self.assertTrue(cleaned.startswith("# PRD: Widget"))
        self.assertNotIn("```", cleaned)
        self.assertNotIn("markdown", cleaned.splitlines()[0].lower())

    def test_removes_preamble_before_first_heading(self):
        raw = (
            "Here is the draft PRD you requested.\n\n"
            "# PRD: Widget\n\n"
            "## Overview\n"
            "Details here."
        )
        cleaned = sanitize_prd_markdown(raw)
        self.assertEqual(cleaned.splitlines()[0], "# PRD: Widget")

    def test_removes_trailing_next_steps_section(self):
        raw = (
            "# PRD: Widget\n\n"
            "## Overview\n"
            "Details here.\n\n"
            "### Next Steps\n"
            "Please review and let me know."
        )
        cleaned = sanitize_prd_markdown(raw)
        self.assertNotIn("Next Steps", cleaned)
        self.assertNotIn("Please review", cleaned)

    def test_removes_trailing_json_output_section(self):
        raw = (
            "# PRD: Widget\n\n"
            "## Rollout Plan\n"
            "- Flag name: widget-launch\n"
            "- Phasing: Internal testing -> Beta release\n"
            "```\n\n"
            "### JSON Output\n"
            "```json\n"
            '{"status":"ok","result":{"prd_markdown":"# PRD: Widget"}}\n'
            "```"
        )

        cleaned = sanitize_prd_markdown(raw)

        self.assertIn("## Rollout Plan", cleaned)
        self.assertNotIn("JSON Output", cleaned)
        self.assertNotIn('"status"', cleaned)
        self.assertNotIn("```", cleaned)

    def test_artifact_body_sanitizes_prd_markdown(self):
        output = {
            "result": {
                "prd_markdown": "```markdown\n# PRD: Widget\n\n## Overview\nBody.\n```",
            },
        }
        body = artifact_body(output)
        self.assertTrue(body.startswith("# PRD: Widget"))
        self.assertNotIn("```", body)

    def test_format_artifact_for_channel_includes_approval_cta(self):
        artifact = {
            "result": {
                "prd_title": "Widget PRD",
                "prd_markdown": (
                    "Here is your draft.\n\n"
                    "# PRD: Widget\n\n"
                    "## Overview\n"
                    "Body."
                ),
            },
        }
        message = format_artifact_for_channel(
            artifact,
            display_name="PRD review",
            present_prompt="approval",
            step_name="draft_prd",
            agent_id="pm-agent",
        )
        self.assertIn("Reply yes to approve, or no with feedback to revise.", message)
        self.assertIn("# PRD: Widget", message)
        self.assertNotIn("Here is your draft.", message)

    def test_format_artifact_for_channel_renders_intake_questions(self):
        message = format_artifact_for_channel(
            {
                "summary": "A few things to clarify before drafting.",
                "questions": ["Who is the audience?", "What is in scope?"],
            },
            display_name="PRD review",
            present_prompt="intake",
            step_name="intake",
            agent_id="pm-agent",
        )
        self.assertIn("Who is the audience?", message)
        self.assertIn("What is in scope?", message)
        self.assertIn("Reply in this thread with your answers.", message)
        self.assertNotIn("no readable content", message)

    def test_format_artifact_for_channel_renders_nested_intake_questions(self):
        message = format_artifact_for_channel(
            {"result": {"clarifying_questions": ["Which platforms?"]}},
            display_name="PRD review",
            present_prompt="intake",
            step_name="intake",
            agent_id="pm-agent",
        )
        self.assertIn("Which platforms?", message)
        self.assertNotIn("no readable content", message)

    def test_format_artifact_for_channel_prefers_prd_over_questions(self):
        message = format_artifact_for_channel(
            {
                "prd_markdown": "# PRD: Widget\n\n## Overview\nBody.",
                "questions": ["Who is the audience?"],
            },
            display_name="PRD review",
            present_prompt="approval",
            step_name="draft_prd",
            agent_id="pm-agent",
        )
        self.assertIn("# PRD: Widget", message)
        self.assertNotIn("Who is the audience?", message)

    def test_format_artifact_for_channel_renders_story_plan(self):
        plan = "## Jira plan: Widget\nProject: THD · Epic: new\n\n| # | Story |\n|---|---|\n| 1.1 | Show widget |"
        message = format_artifact_for_channel(
            {"plan_markdown": plan, "project_key": "THD", "stories": [{"number": "1.1"}]},
            display_name="Jira filing from approved PRD",
            present_prompt="approval",
            step_name="plan_stories",
            agent_id="pm-agent",
        )
        self.assertIn("## Jira plan: Widget", message)
        self.assertIn("| 1.1 | Show widget |", message)
        self.assertIn("Reply yes to approve", message)
        self.assertNotIn("no readable content", message)

    def test_artifact_body_renders_prd_review(self):
        body = artifact_body({"verdict": "gaps", "review_markdown": "# PRD Review: Widget\n\n## Strengths\n1. Clear."})
        self.assertTrue(body.startswith("# PRD Review: Widget"))

    def test_format_artifact_for_channel_does_not_approve_empty_json(self):
        message = format_artifact_for_channel(
            {"summary": "{}"},
            display_name="PRD review",
            present_prompt="approval",
            step_name="draft_prd",
            agent_id="pm-agent",
        )

        self.assertNotIn("\n\n{}", message)
        self.assertNotIn("Reply yes to approve", message)
        self.assertIn("no readable content", message)

    def test_format_artifact_for_channel_uses_pr_url_when_no_markdown(self):
        message = format_artifact_for_channel(
            {
                "work_order_id": "WO-003",
                "pr_url": "https://github.com/example-org/prd-dev-project/pull/8",
            },
            display_name="Implement work order delivery",
            present_prompt="inform",
            step_name="deliver_work_order",
            agent_id="dev-agent",
        )
        self.assertIn("WO-003", message)
        self.assertIn("https://github.com/example-org/prd-dev-project/pull/8", message)
        self.assertNotIn("no readable content", message)
        self.assertNotIn("request a revision", message)

    def test_format_artifact_for_channel_pr_url_without_work_order_id(self):
        message = format_artifact_for_channel(
            {"pr_url": "https://github.com/org/app/pull/12"},
            display_name="Implement work order delivery",
            present_prompt="none",
            step_name="deliver_work_order",
            agent_id="dev-agent",
        )
        self.assertIn("https://github.com/org/app/pull/12", message)
        self.assertNotIn("no readable content", message)
        self.assertNotIn("Let me know if you want to continue", message)

    def test_format_artifact_for_channel_qa_merge_ask(self):
        artifact = {
            "status": "success",
            "result": {
                "summary": "All delivered PRs meet acceptance criteria.",
                "prs": [
                    {
                        "pr_url": "https://github.com/example-org/prd-dev-project/pull/8",
                        "work_item_ref": "WO-003",
                        "status": "success",
                    }
                ],
            },
        }
        message = format_artifact_for_channel(
            artifact,
            display_name="Implement work order delivery",
            present_prompt="merge",
            step_name="qa_gate",
            agent_id="qa-agent",
        )
        self.assertIn("All delivered PRs meet acceptance criteria.", message)
        self.assertIn("Please merge", message)
        self.assertIn("WO-003", message)
        self.assertIn("https://github.com/example-org/prd-dev-project/pull/8", message)
        self.assertIn("I'll continue automatically after GitHub merge", message)
        self.assertNotIn("Let me know if you want to continue", message)

    def test_artifact_body_prefers_work_orders_artifacts_over_action_plan_summary(self):
        output = {
            "summary": (
                "### Next Steps:\n"
                "1. Refine Requirements\n"
                "2. Create Work Orders\n"
            ),
            "artifacts": {
                "frds_markdown": "# FRD-001 Lobby\n\nWhen a player opens the app, the system shall show lobbies.",
                "feature_tree_markdown": "# Feature Tree\n\n- Lobby\n- Matchmaking",
            },
        }
        body = artifact_body(output)
        self.assertIn("FRD-001", body)
        self.assertNotIn("Next Steps", body)

    def test_format_artifact_for_channel_shows_work_orders_artifacts(self):
        artifact = {
            "summary": "### Next Steps:\n1. Do something",
            "artifacts": {
                "work_orders_markdown": "# WO-001 Lobby\n\nImplement lobby matchmaking.",
            },
        }
        message = format_artifact_for_channel(
            artifact,
            display_name="Work orders from approved PRD",
            present_prompt="approval",
            step_name="plan_work_orders",
            agent_id="pm-agent",
        )
        self.assertIn("WO-001", message)
        self.assertNotIn("Next Steps", message)
        self.assertIn("Reply yes to approve", message)

    def test_action_plan_detects_steps_for_foundry(self):
        from ts_harness.workflows.artifacts import looks_like_action_plan

        text = (
            "### Steps for Foundry Stage\n"
            "1. Review FRDs.\n"
            "2. Draft blueprints."
        )
        self.assertTrue(looks_like_action_plan(text))

    def test_foundry_markers_require_ids_or_sections(self):
        from ts_harness.workflows.artifacts import (
            has_foundry_structural_markers,
            looks_like_invalid_foundry_content,
        )

        prose = (
            "We will design blueprints for the lobby "
            "and game board architecture."
        )
        self.assertFalse(has_foundry_structural_markers(prose))
        self.assertTrue(looks_like_invalid_foundry_content(prose))

        structured = (
            "## ADR-001 Sync\n"
            "Use WebSockets for player moves and session fan-out across regions.\n\n"
            "## FB-001 Lobby\n"
            "Queue players in the lobby service and expose matchmaking APIs.\n\n"
            "## COMP-002 Board\n"
            "Validate moves on the game board and publish game state events.\n"
            "Include undo rules, draw detection, and spectator-safe state snapshots.\n"
            "Document failure modes for disconnects and reconnection flows.\n"
            "Capture observability hooks for latency and error budgets.\n"
        )
        self.assertTrue(has_foundry_structural_markers(structured))
        self.assertFalse(looks_like_invalid_foundry_content(structured))

        comp_only = "## COMP-001 Lobby Service\nOwns matchmaking queues."
        self.assertFalse(has_foundry_structural_markers(comp_only))
        self.assertTrue(looks_like_invalid_foundry_content(comp_only))

    def test_foundry_rejects_ellipsis_only_sections(self):
        from ts_harness.workflows.artifacts import foundry_artifact_failure_reason

        placeholder = (
            "## ADR-001 Real-time Simulation\n\n"
            "## Context\n...\n\n"
            "## Alternatives Considered\n...\n\n"
            "## Decision\nUse WebSockets for real-time updates.\n\n"
            "## Rejection Rationale\n...\n\n"
            "## Trade-off Scorecard\n...\n\n"
            "## Consequences and Downstream Impact\n...\n\n"
            "## COMP-001 Simulation Engine\n\n"
            "Handles transaction simulations and price updates for the portfolio.\n\n"
            "## FB-001 Transaction Simulation\n\n"
            "Covers buy and sell transactions for FRD-001, including fee calculation.\n\n"
            "# Architecture Summary\n\n"
            "The architecture supports a simulation engine for real-time updates "
            "and portfolio tracking. COMP-001 and FB-001 align with ADR-001.\n"
        )
        reason = foundry_artifact_failure_reason(placeholder)
        self.assertIsNotNone(reason)
        self.assertIn("placeholder", (reason or "").lower())
        self.assertIn("Context", reason or "")

    def test_foundry_blocks_inline_adr_catalog_summary(self):
        from ts_harness.workflows.artifacts import foundry_artifact_failure_reason

        catalog = (
            "Successfully drafted and stored.\n"
            "### Artifacts Produced\n"
            "- **ADR-001**: Use WebSockets.\n"
            "- **ADR-002**: Deploy on cloud.\n"
        )
        reason = foundry_artifact_failure_reason(catalog)
        self.assertIsNotNone(reason)
        self.assertIn("enumerates artifacts", reason or "")

    def test_sanitize_stage_artifact_strips_preamble(self):
        from ts_harness.workflows.artifacts import (
            artifact_body,
            sanitize_stage_artifact_markdown,
        )

        raw = (
            "Stored in memory for future reference.\n\n"
            "markdown\n"
            "# Product Overview\n\n"
            "## Business Problem\n"
            "Players need online checkers.\n\n"
            "# FRD-001 Lobby\n\n"
            "When a player opens the app, show lobbies.\n"
        )
        cleaned = sanitize_stage_artifact_markdown(raw)
        self.assertTrue(cleaned.startswith("# Product Overview"))
        self.assertNotIn("stored in memory", cleaned.lower())

        output = {
            "artifacts": {
                "product_overview_markdown": raw,
                "frds_markdown": "# FRD-001 Lobby\n\nWhen a player opens the app, show lobbies.",
            },
        }
        body = artifact_body(output)
        self.assertNotIn("stored in memory", body.lower())
        self.assertIn("FRD-001", body)

    def test_work_orders_artifacts_body_dedupes_overlapping_sections(self):
        from ts_harness.workflows.artifacts import artifact_body

        shared = (
            "# FRD-001 Lobby\n\n"
            "When a player opens the app, show lobbies."
        )
        output = {
            "artifacts": {
                "frds_markdown": shared,
                "requirements_markdown": shared,
                "feature_tree_markdown": "# Feature Tree\n\n- Lobby",
            },
        }
        body = artifact_body(output)
        self.assertEqual(body.count("FRD-001"), 1)
        self.assertIn("Feature Tree", body)

    def test_memory_recall_summary_is_rejected_for_refinery(self):
        from ts_harness.workflows.artifacts import (
            looks_like_memory_recall_summary,
            work_orders_artifacts_failure_reason,
        )

        recall = (
            "I've recalled relevant memories for the PRD. "
            "Here's a summary of the key points:\n\n"
            "# FRD-001 Lobby\n\n"
            "When a player opens the app, show lobbies.\n\n"
            "# FRD-002 Match\n\n"
            "When matched, start a game session.\n\n"
            "# Product Overview\n\n"
            "## Business Problem\nPlayers need online checkers.\n"
        )
        self.assertTrue(looks_like_memory_recall_summary(recall))
        reason = work_orders_artifacts_failure_reason(
            {
                "requirements_markdown": recall,
                "frds_markdown": recall,
                "feature_tree_markdown": recall,
                "product_overview_markdown": recall,
            },
            "refinery",
        )
        self.assertIsNotNone(reason)
        self.assertTrue(
            "identical content" in (reason or "")
            or "memory-recall" in (reason or "")
        )

    def test_work_orders_requires_all_keys_and_rejects_duplicates(self):
        from ts_harness.workflows.artifacts import (
            work_orders_artifacts_failure_reason,
        )

        frd = (
            "# FRD-001 Lobby\n\n"
            "When a player opens the app, show available lobbies.\n\n"
            "# FRD-002 Match\n\n"
            "When two players match, create a game session.\n\n"
            "# Product Overview\n\n"
            "## Business Problem\nPlayers need online checkers with friends.\n"
        )
        missing = work_orders_artifacts_failure_reason(
            {
                "frds_markdown": frd,
                "feature_tree_markdown": "# Feature Tree\n\n- Lobby\n- Match\n- Chat\n",
                "product_overview_markdown": (
                    "# Product Overview\n\n## Business Problem\n"
                    "Players need online checkers.\n## Personas\nCasual gamers.\n"
                ),
            },
            "refinery",
        )
        self.assertIsNotNone(missing)
        self.assertIn("missing required artifact keys", missing or "")
        self.assertIn("requirements_markdown", missing or "")

        recall = (
            "I've recalled relevant memories for the PRD. "
            "Here's a summary of the key points from prior chats."
        )
        recall_partial = work_orders_artifacts_failure_reason(
            {
                "frds_markdown": recall,
                "feature_tree_markdown": recall,
                "product_overview_markdown": recall,
            },
            "refinery",
        )
        self.assertIsNotNone(recall_partial)
        self.assertIn("memory-recall", recall_partial or "")

        duplicated = work_orders_artifacts_failure_reason(
            {
                "requirements_markdown": frd,
                "frds_markdown": frd,
                "feature_tree_markdown": frd,
                "product_overview_markdown": frd,
            },
            "refinery",
        )
        self.assertIsNotNone(duplicated)
        self.assertIn("identical content", duplicated or "")

    def test_thin_refinery_artifacts_are_rejected(self):
        from ts_harness.workflows.artifacts import (
            refinery_artifact_failure_reason,
            work_orders_artifacts_failure_reason,
        )

        thin_frds = (
            "# FRD-001 Multiplayer Matchmaking\n\n"
            "## Overview\nEnable players to find opponents.\n\n"
            "## Personas\nCasual gamers.\n"
        )
        thin_tree = (
            "# Feature Tree\n\n"
            "- Multiplayer matchmaking\n"
            "- Private rooms\n"
            "- Standard rules adherence\n"
        )
        thin_requirements = (
            "# Requirements\n\n"
            "## Technical Requirements\n"
            "- Matchmaking service\n"
            "- Real-time messaging\n"
        )
        thin_overview = (
            "# Product Overview\n\n"
            "Casual gamers seek engaging games for short breaks.\n"
        )
        combined = "\n\n".join(
            [thin_frds, thin_tree, thin_requirements, thin_overview]
        )
        reason = refinery_artifact_failure_reason(combined)
        self.assertIsNotNone(reason)
        self.assertTrue(
            "two FRD" in (reason or "")
            or "too short" in (reason or "")
            or "section headings" in (reason or ""),
            reason,
        )
        bag_reason = work_orders_artifacts_failure_reason(
            {
                "frds_markdown": thin_frds,
                "feature_tree_markdown": thin_tree,
                "requirements_markdown": thin_requirements,
                "product_overview_markdown": thin_overview,
            },
            "refinery",
        )
        self.assertIsNotNone(bag_reason)

    def test_partition_foundry_markdown_splits_and_recognizes_architecture_summary(self):
        from ts_harness.workflows.artifacts import (
            partition_foundry_markdown,
            work_orders_artifacts_failure_reason,
        )

        blob = (
            "## ADR-001 Real-time Communication\n\n"
            "## Context\nNeed low-latency turns.\n\n"
            "## Alternatives Considered\nWebSockets vs long polling.\n\n"
            "## Decision\nUse WebSockets.\n\n"
            "## Rejection Rationale\nLong polling is slower.\n\n"
            "## Trade-off Scorecard\nLatency vs complexity.\n\n"
            "## Consequences and Downstream Impact\nClients need WS support.\n\n"
            "## ADR-002 Session Persistence\n\n"
            "## Context\nReconnect after drop.\n\n"
            "## Alternatives Considered\nSticky sessions vs token resume.\n\n"
            "## Decision\nToken resume.\n\n"
            "## Rejection Rationale\nSticky sessions hurt scale.\n\n"
            "## Trade-off Scorecard\nScale vs simplicity.\n\n"
            "## Consequences and Downstream Impact\nStore resume tokens.\n\n"
            "# Blueprints\n\n"
            "## COMP-001 Game Server\n\n"
            "Owns match state and turn validation for checkers sessions.\n\n"
            "## FB-001 Matchmaking\n\n"
            "Covers queueing, pairing, and private rooms for FRD-001.\n\n"
            "# Architecture Summary\n\n"
            "WebSocket game server plus token resume keep matches coherent.\n"
            "COMP-001 and FB-001 align with ADR-001 and ADR-002.\n"
        )
        parts = partition_foundry_markdown(blob)
        self.assertIn("architecture_decisions_markdown", parts)
        self.assertIn("blueprints_markdown", parts)
        self.assertIn("architecture_summary_markdown", parts)
        self.assertIn("ADR-001", parts["architecture_decisions_markdown"])
        self.assertIn("FB-001", parts["blueprints_markdown"])
        self.assertIn("Architecture Summary", parts["architecture_summary_markdown"])
        self.assertNotEqual(
            parts["architecture_decisions_markdown"],
            parts["architecture_summary_markdown"],
        )
        self.assertIsNone(work_orders_artifacts_failure_reason(parts, "foundry"))

    def test_planner_requires_wo_headings_and_keys(self):
        from ts_harness.workflows.artifacts import (
            planner_artifact_failure_reason,
            work_orders_artifacts_failure_reason,
        )

        thin = "# Notes\n\nSome work order ideas."
        self.assertIsNotNone(planner_artifact_failure_reason(thin))

        good_wo = (
            "# WO-001 Lobby Service\n\n"
            "## In Scope\n"
            "Build lobby list API, filter by region, and expose join endpoints "
            "for authenticated players with pagination and basic rate limits.\n\n"
            "## Out of Scope\n"
            "Mobile clients, ranked matchmaking, and tournament brackets.\n\n"
            "## Acceptance\n"
            "Given lobbies exist, when listed, then return ids and player counts.\n"
            "Given a valid join request, when accepted, then seat the player.\n\n"
            "# WO-002 Matchmaking\n\n"
            "## In Scope\n"
            "Pair players into games, create session records, and notify both "
            "clients when a match is ready with reconnect tokens.\n\n"
            "## Out of Scope\n"
            "Ranked seasons, ELO tuning, and spectator mode.\n\n"
            "## Acceptance\n"
            "Given two ready players, when matched, then create a session.\n"
            "Given a disconnect during matchmaking, when reconnected, then resume queue.\n"
        )
        self.assertIsNone(planner_artifact_failure_reason(good_wo))
        ok = work_orders_artifacts_failure_reason(
            {
                "work_orders_markdown": good_wo,
                "context_graph_markdown": (
                    "# Context Graph\n\n"
                    "- WO-001 -> none\n"
                    "- WO-002 -> WO-001\n"
                    "Dependencies are explicit for execution ordering.\n"
                ),
                "execution_phases_markdown": (
                    "# Execution Phases\n\n"
                    "## Phase 1\nWO-001\n\n"
                    "## Phase 2\nWO-002\n"
                ),
            },
            "planner",
        )
        self.assertIsNone(ok)

    def test_partition_refinery_markdown_assigns_distinct_sections(self):
        from ts_harness.workflows.artifacts import partition_refinery_markdown

        blob = (
            "Here is the refinery output.\n\n"
            "# Product Overview\n## Business Problem\nPlayers need checkers.\n\n"
            "# Feature Tree\n- Lobby\n- Board\n\n"
            "# Requirements\nREQ-001 server-side move validation.\n\n"
            "# FRD-001 Lobby\n## Acceptance Criteria\nShow lobbies.\n\n"
            "# FRD-002 Board\n## Acceptance Criteria\nReject illegal moves.\n"
        )
        parts = partition_refinery_markdown(blob)

        self.assertEqual(
            set(parts),
            {
                "product_overview_markdown",
                "feature_tree_markdown",
                "requirements_markdown",
                "frds_markdown",
            },
        )
        self.assertIn("## Business Problem", parts["product_overview_markdown"])
        self.assertNotIn("Here is the refinery output", parts["product_overview_markdown"])
        self.assertIn("FRD-001", parts["frds_markdown"])
        self.assertIn("FRD-002", parts["frds_markdown"])
        self.assertNotIn("FRD-", parts["feature_tree_markdown"])

    def test_catalog_summary_reported_before_missing_keys(self):
        from ts_harness.workflows.artifacts import (
            work_orders_artifacts_failure_reason,
        )

        reason = work_orders_artifacts_failure_reason(
            {"blueprints_markdown": "### Artifacts Produced\n- ADR-001"},
            "foundry",
        )

        self.assertIn("enumerates artifacts", reason or "")


if __name__ == "__main__":
    unittest.main()
