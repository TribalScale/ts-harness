"""Tools for builder-agent on the admin program builder page."""

from __future__ import annotations

from typing import Any

from ts_harness.tools.registry import ToolDefinition, ToolRegistry


def _runtime():
    from ts_harness.gateway.server import get_runtime

    return get_runtime()


def builder_session_for(ctx) -> tuple[Any, Any, dict[str, Any] | None]:
    """Return ``(runtime, session, error)`` for a builder tool call.

    Builder tools act as the person on the builder page, so they only run there, for a
    session that has a person attached who still holds ``edit_config``.
    """
    from ts_harness.agent.sessions import get_session_store
    from ts_harness.gateway.program_builder_bff import BUILDER_CHANNEL, builder_actor
    from ts_harness.policy.permissions import actor_permissions

    if getattr(ctx, "source", None) != BUILDER_CHANNEL:
        return None, None, {"error": "builder tools run only on the program builder page"}
    session = get_session_store().get(ctx.session_id) if ctx.session_id else None
    if session is None or session.source != BUILDER_CHANNEL:
        return None, None, {"error": "builder session not found"}
    auth = builder_actor(session)
    if auth is None:
        return None, None, {"error": "builder session has no person attached"}
    runtime = _runtime()
    if "edit_config" not in actor_permissions(auth, runtime.config):
        return None, None, {"error": "the person on this builder session no longer has edit_config"}
    return runtime, session, None


def _handle_builder_catalog(args: dict[str, Any], ctx) -> dict[str, Any]:
    from ts_harness.gateway.program_builder_bff import builder_catalog

    del args
    runtime, session, error = builder_session_for(ctx)
    if error:
        return error
    return builder_catalog(runtime, session)


def _handle_propose_program_bundle(args: dict[str, Any], ctx) -> dict[str, Any]:
    from ts_harness.gateway.program_builder_bff import propose_bundle

    runtime, session, error = builder_session_for(ctx)
    if error:
        return error
    return propose_bundle(runtime, session, args)


def register_program_builder_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolDefinition(
            name="builder_catalog",
            description=(
                "Read the live catalogs for drafting a program: agents and their tools, "
                "approved skills, hooks, workflow and program types already in use, adapters, "
                "routing intents, publish tools that need an approval gate, and this "
                "session's current drafts. Read-only."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_handle_builder_catalog,
            toolsets=["program-builder"],
        )
    )
    registry.register(
        ToolDefinition(
            name="propose_program_bundle",
            description=(
                "Create or update this session's workflow, program, and routing drafts as the "
                "person on the builder page, then validate them together. Drafts only; never "
                "activates. Types must be new. Returns draft ids and issues per draft."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "workflow": {
                        "type": "object",
                        "description": "Full workflow definition (workflow_type, definition_version, stages, ...).",
                    },
                    "program": {
                        "type": "object",
                        "description": "Full program definition (program_type, definition_version, nodes, edges, ...).",
                    },
                    "routing_intent": {
                        "type": "object",
                        "description": (
                            "Optional channel routing intent: id, signals, program_type, "
                            "entry_phase, and optional exclude/orchestrator_hint/policy."
                        ),
                    },
                    "editor_note": {"type": "string"},
                },
                "required": ["workflow", "program"],
            },
            handler=_handle_propose_program_bundle,
            toolsets=["program-builder"],
        )
    )
