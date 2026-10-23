"""Built-in core tools for workflow, memory, files, terminal, and delegation."""

from __future__ import annotations

from typing import Any

from ts_harness.tools.registry import ToolDefinition, ToolRegistry


def register_core_tools(registry: ToolRegistry) -> None:
    from ts_harness.tools.delegation import handle_delegate_task
    from ts_harness.tools.file_tools import handle_patch_file, handle_read_file, handle_search_files, handle_write_file
    from ts_harness.tools.terminal import handle_background_logs, handle_exec
    from ts_harness.agent.sessions import session_status_handler
    from ts_harness.memory.client import recall_memory_handler, workflow_update_handler
    from ts_harness.tools.workflow_tools import (
        advance_program_handler,
        bridge_workflow_handler,
        cancel_workflow_handler,
        continue_delivery_handler,
        program_status_handler,
        propose_advance_program_handler,
        propose_bridge_workflow_handler,
        propose_continue_delivery_handler,
        propose_link_program_handler,
        propose_set_program_project_handler,
        propose_start_program_handler,
        propose_start_workflow_handler,
        link_program_handler,
        set_program_autoshare_handler,
        set_program_project_handler,
        unlink_program_handler,
        remember_memory_handler,
        resume_external_event_handler,
        search_sessions_handler,
        sessions_history_handler,
        sessions_list_handler,
        start_program_handler,
        start_workflow_handler,
        workflow_approve_handler,
        workflow_reject_handler,
        workflow_reject_with_feedback_handler,
        workflow_status_handler,
    )

    registry.register(
        ToolDefinition(
            name="recall_memory",
            description="Recall tenant-scoped memories",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
            handler=lambda args, ctx: recall_memory_handler(args, ctx),
            toolsets=["core-read", "memory-api"],
        )
    )
    registry.register(
        ToolDefinition(
            name="read_file",
            description="Read a file from the agent workspace",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=lambda args, ctx: handle_read_file(args, ctx),
            toolsets=["core-read"],
        )
    )
    registry.register(
        ToolDefinition(
            name="write_file",
            description="Write a file in the agent workspace",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            handler=lambda args, ctx: handle_write_file(args, ctx),
            toolsets=["core-write"],
        )
    )
    registry.register(
        ToolDefinition(
            name="patch_file",
            description="Apply a patch to a workspace file",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "patch": {"type": "string"},
                },
                "required": ["path", "patch"],
            },
            handler=lambda args, ctx: handle_patch_file(args, ctx),
            toolsets=["core-write"],
        )
    )
    registry.register(
        ToolDefinition(
            name="search_files",
            description="Search workspace files for a literal string",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
            handler=lambda args, ctx: handle_search_files(args, ctx),
            toolsets=["core-read"],
        )
    )
    registry.register(
        ToolDefinition(
            name="exec",
            description="Execute a shell command in sandbox",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "background": {"type": "boolean"},
                },
                "required": ["command"],
            },
            handler=lambda args, ctx: handle_exec(args, ctx),
            toolsets=["terminal"],
        )
    )
    registry.register(
        ToolDefinition(
            name="background_logs",
            description="Read bounded logs for a background command",
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
            },
            handler=lambda args, ctx: handle_background_logs(args, ctx),
            toolsets=["terminal"],
        )
    )
    registry.register(
        ToolDefinition(
            name="workflow_update",
            description="Update workflow task state through memory-api",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "status": {"type": "string"},
                    "output": {"type": "object"},
                },
                "required": ["task_id", "status"],
            },
            handler=lambda args, ctx: workflow_update_handler(args, ctx),
            toolsets=["workflow"],
        )
    )
    registry.register(
        ToolDefinition(
            name="session_status",
            description="Return current session metadata",
            parameters={"type": "object", "properties": {}},
            handler=lambda args, ctx: session_status_handler(args, ctx),
            toolsets=["core-read"],
        )
    )
    registry.register(
        ToolDefinition(
            name="propose_delegate_task",
            description=(
                "Record a pending specialist delegation for channel chat. "
                "Ask the user to confirm before calling delegate_task."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "task": {"type": "string"},
                    "input": {"type": "object"},
                },
                "required": ["agent_id", "task"],
            },
            handler=lambda args, ctx: _handle_propose_delegate_task(args, ctx),
            toolsets=["delegation", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="delegate_task",
            description="Spawn an isolated specialist subagent",
            parameters={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "task": {"type": "string"},
                    "input": {"type": "object"},
                },
                "required": ["agent_id", "task"],
            },
            handler=lambda args, ctx: handle_delegate_task(args, ctx),
            toolsets=["delegation"],
        )
    )
    registry.register(
        ToolDefinition(
            name="read_skill_reference",
            description="Read a governed skill's body or one of its supporting files",
            parameters={
                "type": "object",
                "properties": {
                    "skill_id": {"type": "string"},
                    "version": {
                        "type": "string",
                        "description": "Optional; defaults to the indexed version.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Relative file inside the skill folder. Omit to get SKILL.md and the file list.",
                    },
                },
                "required": ["skill_id"],
            },
            handler=lambda args, ctx: _read_skill_reference(args, ctx),
            toolsets=["core-read"],
        )
    )
    registry.register(
        ToolDefinition(
            name="skill_doctor",
            description="Diagnose which skills are assigned or blocked for an agent",
            parameters={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                },
            },
            handler=lambda args, ctx: _skill_doctor(args, ctx),
            toolsets=["core-read"],
        )
    )
    registry.register(
        ToolDefinition(
            name="remember_memory",
            description="Persist a durable memory for later recall",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "type": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["content"],
            },
            handler=lambda args, ctx: remember_memory_handler(args, ctx),
            toolsets=["core-read", "memory-api"],
        )
    )
    registry.register(
        ToolDefinition(
            name="sessions_list",
            description="List runtime sessions for the current tenant",
            parameters={
                "type": "object",
                "properties": {"source": {"type": "string"}},
            },
            handler=lambda args, ctx: sessions_list_handler(args, ctx),
            toolsets=["core-read", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="sessions_history",
            description="Load transcript history for a session",
            parameters={
                "type": "object",
                "properties": {"session_id": {"type": "string"}},
            },
            handler=lambda args, ctx: sessions_history_handler(args, ctx),
            toolsets=["core-read", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="search_sessions",
            description=(
                "Search this tenant's channel/Chat transcripts. "
                "Returns snippets, not full threads."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
            handler=lambda args, ctx: search_sessions_handler(args, ctx),
            toolsets=["core-read", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="propose_start_workflow",
            description="Record a pending durable workflow start for channel chat",
            parameters={
                "type": "object",
                "properties": {
                    "workflow_type": {"type": "string"},
                    "summary": {"type": "string"},
                    "input": {"type": "object"},
                    "program_type": {"type": "string"},
                },
                "required": ["workflow_type", "summary"],
            },
            handler=lambda args, ctx: propose_start_workflow_handler(args, ctx),
            toolsets=["workflow-promotion", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="propose_bridge_workflow",
            description="Record pending downstream workflow bridge after source approval",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "target_workflow_type": {"type": "string"},
                    "target_node": {"type": "string"},
                    "program_type": {"type": "string"},
                },
                "required": ["summary"],
            },
            handler=lambda args, ctx: propose_bridge_workflow_handler(args, ctx),
            toolsets=["workflow-promotion", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="propose_continue_delivery",
            description="Deprecated alias for propose_bridge_workflow",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                },
                "required": ["summary"],
            },
            handler=lambda args, ctx: propose_continue_delivery_handler(args, ctx),
            toolsets=["workflow-promotion", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="bridge_workflow",
            description="Start downstream workflow after user confirmation",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "target_workflow_type": {"type": "string"},
                    "target_node": {"type": "string"},
                    "program_type": {"type": "string"},
                },
            },
            handler=lambda args, ctx: bridge_workflow_handler(args, ctx),
            toolsets=["workflow-promotion", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="continue_delivery",
            description="Deprecated alias for bridge_workflow",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                },
            },
            handler=lambda args, ctx: continue_delivery_handler(args, ctx),
            toolsets=["workflow-promotion"],
        )
    )
    registry.register(
        ToolDefinition(
            name="start_workflow",
            description="Start a durable workflow after user confirmation",
            parameters={
                "type": "object",
                "properties": {
                    "workflow_type": {"type": "string"},
                    "summary": {"type": "string"},
                    "program_type": {"type": "string"},
                },
            },
            handler=lambda args, ctx: start_workflow_handler(args, ctx),
            toolsets=["workflow-promotion"],
        )
    )
    registry.register(
        ToolDefinition(
            name="workflow_status",
            description="Return workflow task status from memory-api",
            parameters={
                "type": "object",
                "properties": {"workflow_id": {"type": "string"}},
            },
            handler=lambda args, ctx: workflow_status_handler(args, ctx),
            toolsets=["workflow-promotion", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="propose_start_program",
            description="Record a pending delivery program start at a specific phase",
            parameters={
                "type": "object",
                "properties": {
                    "entry_phase": {"type": "string"},
                    "summary": {"type": "string"},
                    "artifacts": {"type": "object"},
                    "input": {"type": "object"},
                    "program_type": {"type": "string"},
                    "project_id": {"type": "string"},
                    "link_program_ids": {"type": "array", "items": {"type": "string"}},
                    "parent_program_id": {"type": "string"},
                },
                "required": ["entry_phase", "summary"],
            },
            handler=lambda args, ctx: propose_start_program_handler(args, ctx),
            toolsets=["workflow-promotion", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="start_program",
            description="Start a delivery program after user confirmation",
            parameters={
                "type": "object",
                "properties": {
                    "entry_phase": {"type": "string"},
                    "summary": {"type": "string"},
                    "artifacts": {"type": "object"},
                    "program_type": {"type": "string"},
                    "project_id": {"type": "string"},
                    "link_program_ids": {"type": "array", "items": {"type": "string"}},
                    "parent_program_id": {"type": "string"},
                },
            },
            handler=lambda args, ctx: start_program_handler(args, ctx),
            toolsets=["workflow-promotion"],
        )
    )
    registry.register(
        ToolDefinition(
            name="propose_set_program_project",
            description="Propose setting the program project_id",
            parameters={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "program_id": {"type": "string"},
                },
                "required": ["project_id"],
            },
            handler=lambda args, ctx: propose_set_program_project_handler(args, ctx),
            toolsets=["workflow-promotion", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="set_program_project",
            description="Set the program project_id after confirmation",
            parameters={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "program_id": {"type": "string"},
                },
            },
            handler=lambda args, ctx: set_program_project_handler(args, ctx),
            toolsets=["workflow-promotion"],
        )
    )
    registry.register(
        ToolDefinition(
            name="propose_link_program",
            description="Propose a one-way program link so this program can read another",
            parameters={
                "type": "object",
                "properties": {
                    "to_program_id": {"type": "string"},
                    "from_program_id": {"type": "string"},
                    "program_id": {"type": "string"},
                },
                "required": ["to_program_id"],
            },
            handler=lambda args, ctx: propose_link_program_handler(args, ctx),
            toolsets=["workflow-promotion", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="link_program",
            description="Create a one-way program link after confirmation",
            parameters={
                "type": "object",
                "properties": {
                    "to_program_id": {"type": "string"},
                    "from_program_id": {"type": "string"},
                },
            },
            handler=lambda args, ctx: link_program_handler(args, ctx),
            toolsets=["workflow-promotion"],
        )
    )
    registry.register(
        ToolDefinition(
            name="unlink_program",
            description="Remove a one-way program link",
            parameters={
                "type": "object",
                "properties": {
                    "to_program_id": {"type": "string"},
                    "from_program_id": {"type": "string"},
                    "program_id": {"type": "string"},
                },
                "required": ["to_program_id"],
            },
            handler=lambda args, ctx: unlink_program_handler(args, ctx),
            toolsets=["workflow-promotion", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="set_program_autoshare",
            description="Exclude or include this program from project auto-share",
            parameters={
                "type": "object",
                "properties": {
                    "excluded": {"type": "boolean"},
                    "program_id": {"type": "string"},
                },
                "required": ["excluded"],
            },
            handler=lambda args, ctx: set_program_autoshare_handler(args, ctx),
            toolsets=["workflow-promotion", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="program_status",
            description=(
                "Return aggregated delivery program status. "
                "program_id is the program id; a unique display name is also accepted."
            ),
            parameters={
                "type": "object",
                "properties": {"program_id": {"type": "string"}},
            },
            handler=lambda args, ctx: program_status_handler(args, ctx),
            toolsets=["workflow-promotion", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="propose_advance_program",
            description="Record pending delivery program phase advance",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "target_phase": {"type": "string"},
                    "target_node": {"type": "string"},
                    "program_type": {"type": "string"},
                },
                "required": ["summary"],
            },
            handler=lambda args, ctx: propose_advance_program_handler(args, ctx),
            toolsets=["workflow-promotion", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="advance_program",
            description="Advance delivery program to next phase after confirmation",
            parameters={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "target_phase": {"type": "string"},
                    "target_node": {"type": "string"},
                    "program_type": {"type": "string"},
                    "artifacts": {"type": "object"},
                },
            },
            handler=lambda args, ctx: advance_program_handler(args, ctx),
            toolsets=["workflow-promotion", "channel-chat"],
        )
    )
    registry.register(
        ToolDefinition(
            name="workflow_approve",
            description="Approve the current pending workflow gate",
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
            },
            handler=lambda args, ctx: workflow_approve_handler(args, ctx),
            toolsets=["workflow-promotion"],
        )
    )
    registry.register(
        ToolDefinition(
            name="workflow_reject",
            description="Reject the current pending workflow gate",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "comments": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
            handler=lambda args, ctx: workflow_reject_handler(args, ctx),
            toolsets=["workflow-promotion"],
        )
    )
    registry.register(
        ToolDefinition(
            name="workflow_reject_with_feedback",
            description="Reject a workflow gate with revision comments",
            parameters={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "comments": {"type": "string"},
                },
                "required": ["comments"],
            },
            handler=lambda args, ctx: workflow_reject_with_feedback_handler(args, ctx),
            toolsets=["workflow-promotion"],
        )
    )
    registry.register(
        ToolDefinition(
            name="cancel_workflow",
            description="Cancel the linked durable workflow",
            parameters={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
            handler=lambda args, ctx: cancel_workflow_handler(args, ctx),
            toolsets=["workflow-promotion"],
        )
    )
    registry.register(
        ToolDefinition(
            name="resume_external_event",
            description="Resume a workflow task blocked on an external event",
            parameters={
                "type": "object",
                "properties": {
                    "workflow_id": {"type": "string"},
                    "event_type": {"type": "string"},
                    "payload": {"type": "object"},
                },
                "required": ["event_type"],
            },
            handler=lambda args, ctx: resume_external_event_handler(args, ctx),
            toolsets=["workflow-promotion"],
        )
    )
    registry.register(
        ToolDefinition(
            name="channel_send",
            description="Send a message to a configured channel",
            parameters={
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "text": {"type": "string"},
                    "thread_id": {"type": "string"},
                },
                "required": ["channel", "text"],
            },
            handler=lambda args, ctx: _channel_send(args, ctx),
            toolsets=["channel"],
        )
    )
    registry.register(
        ToolDefinition(
            name="propose_skill",
            description=(
                "Propose a new skill or revision as an Inbox draft. "
                "Writes skills/drafts/ only. Never writes an approved catalog skill."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "markdown": {"type": "string"},
                    "revision_of": {
                        "type": "string",
                        "description": (
                            "Optional live skill id to revise. Markdown id must equal "
                            "this live skill id, category must match live, version must "
                            "be strictly greater, and the tool writes drafts only."
                        ),
                    },
                },
                "required": ["markdown"],
            },
            handler=lambda args, ctx: _handle_propose_skill(args, ctx),
            toolsets=["skill-authoring"],
        )
    )
    from ts_harness.tools.coding_backend_tool import handle_run_coding_backend

    registry.register(
        ToolDefinition(
            name="run_coding_backend",
            description=(
                "Run the configured host coding backend (OpenCode) to implement a work order "
                "in a git worktree. Does not use the network-denied sandbox."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "work_order_id": {"type": "string"},
                    "work_order_body": {"type": "string"},
                    "repo_url": {"type": "string"},
                    "base_branch": {"type": "string"},
                    "provider": {"type": "string"},
                    "model": {"type": "string"},
                    "workflow_type": {"type": "string"},
                    "workspace_dir": {"type": "string"},
                    "refinery_artifacts": {"type": "object"},
                    "foundry_artifacts": {"type": "object"},
                    "planner_artifacts": {"type": "object"},
                    "approved_prd": {"type": "object"},
                    "approved_prd_markdown": {"type": "string"},
                    "qa_feedback": {"type": "string"},
                    "review_feedback": {"type": "string"},
                    "mode": {
                        "type": "string",
                        "enum": ["implement", "refresh"],
                        "description": "refresh merges the base branch into the existing PR (requires pr_url).",
                    },
                    "pr_url": {"type": "string"},
                    "program_id": {"type": "string"},
                    "requested_by": {"type": "string"},
                    "decision_id": {"type": "string"},
                    "workflow_id": {"type": "string"},
                },
                "required": ["work_order_id", "work_order_body"],
            },
            handler=lambda args, ctx: handle_run_coding_backend(args, ctx),
            toolsets=["coding-backend"],
        )
    )


def _handle_propose_skill(args: dict[str, Any], ctx) -> dict[str, Any]:
    from ts_harness.gateway.program_builder_bff import BUILDER_CHANNEL, track_skill_proposal
    from ts_harness.skills.authoring import SkillAuthoringError
    from ts_harness.skills.repository import skill_repository

    builder_session = None
    created_by = ""
    if getattr(ctx, "source", None) == BUILDER_CHANNEL:
        from ts_harness.tools.program_builder_tools import builder_session_for

        _runtime, builder_session, error = builder_session_for(ctx)
        if error:
            return error
        created_by = str((builder_session.metadata or {}).get("builder_actor", {}).get("actor_id") or "")
    try:
        result = skill_repository().propose_markdown(
            agent_id=str(getattr(ctx, "agent_id", "") or ""),
            tenant_id=str(getattr(ctx, "tenant_id", "") or ""),
            markdown=str(args.get("markdown") or ""),
            revision_of=str(args.get("revision_of") or "").strip() or None,
            source="learn",
            created_by=created_by,
        )
    except SkillAuthoringError as exc:
        payload = {"error": str(exc)}
        if exc.code:
            payload["error_code"] = exc.code
        return payload
    if builder_session is not None:
        track_skill_proposal(builder_session, str(result.get("skill_id") or ""))
    return result


def _handle_propose_delegate_task(args: dict[str, Any], ctx) -> dict[str, Any]:
    from ts_harness.agent.sessions import get_session_store
    from ts_harness.gateway.delegate_confirmation import set_pending_delegate

    session_id = ctx.session_id
    if not session_id:
        return {"error": "session_id required for propose_delegate_task"}
    session = get_session_store().get(session_id)
    if session is None:
        return {"error": "session not found"}
    agent_id = str(args.get("agent_id", ""))
    task = str(args.get("task", ""))
    if not agent_id or not task:
        return {"error": "agent_id and task are required"}
    input_data = args.get("input") if isinstance(args.get("input"), dict) else {}
    set_pending_delegate(session, agent_id=agent_id, task=task, input_data=input_data)
    return {
        "status": "awaiting_confirmation",
        "agent_id": agent_id,
        "task": task,
        "message": "Ask the user to confirm before delegate_task runs.",
    }


def _skill_doctor(args: dict[str, Any], ctx) -> dict[str, Any]:
    import os

    from ts_harness.config.schema import load_runtime_config
    from ts_harness.skills.catalog import SkillCatalog

    config_path = os.environ.get("TS_HARNESS_CONFIG", "config/ts-harness-runtime.test.yaml")
    config = load_runtime_config(config_path)
    catalog = SkillCatalog.default()
    catalog.load()
    agent_id = str(args.get("agent_id") or ctx.agent_id)
    lines = catalog.doctor(config.get_agent(agent_id), config, tenant_id=ctx.tenant_id)
    return {"agent_id": agent_id, "diagnostics": lines}


def _read_skill_reference(args: dict[str, Any], ctx) -> dict[str, Any]:
    import os

    from ts_harness.config.schema import load_runtime_config
    from ts_harness.skills.catalog import SkillCatalog

    config_path = os.environ.get("TS_HARNESS_CONFIG", "config/ts-harness-runtime.test.yaml")
    config = load_runtime_config(config_path)
    try:
        agent = config.get_agent(str(ctx.agent_id))
    except (KeyError, ValueError):
        return {"error": "skill not available to this agent"}
    catalog = SkillCatalog.default()
    catalog.load()
    return catalog.read_skill_reference(
        str(args.get("skill_id", "")),
        version=str(args.get("version") or "").strip() or None,
        path=str(args.get("path") or "").strip() or None,
        agent=agent,
        config=config,
        tenant_id=str(getattr(ctx, "tenant_id", "") or ""),
    )


def _channel_send(args: dict[str, Any], ctx) -> dict[str, Any]:
    from ts_harness.gateway.channels.router import get_channel_router

    router = get_channel_router()
    return router.send(args.get("channel", "slack"), args, tenant_id=ctx.tenant_id)
