"""Subagent delegation adapted from Hermes delegate_tool.py (MIT)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import os

from ts_harness.agent.loop import EnterpriseAgent
from ts_harness.agent.sessions import get_session_store
from ts_harness.config.schema import RuntimeConfig
from ts_harness.memory.client import get_memory_client
from ts_harness.tools.policy import STAGE_ALWAYS_ALLOWED_TOOLS, ToolPolicyContext
from ts_harness.state import get_state_store
from ts_harness.tools.toolsets import agent_effective_tools
from ts_harness.workflows.continuity import write_stage_snapshot


_runtime_config: RuntimeConfig | None = None
_runtime_agent: EnterpriseAgent | None = None
_subagent_children: dict[str, list[str]] = {}
_MAX_DEPTH = 2
_MAX_CHILDREN = 5
_MAX_CONCURRENT = 8
_ACTIVE_CHILDREN = 0
_STATE_PATH = os.environ.get("TS_HARNESS_DELEGATION_STATE") or None


DELEGATION_STATE_NAMESPACE = "delegation"
DELEGATION_STATE_KEY = "state"


def _shared() -> bool:
    from ts_harness.storage.backend import shared_state_enabled

    return shared_state_enabled()


def _apply_shared(state: dict[str, Any]) -> None:
    global _ACTIVE_CHILDREN, _subagent_children
    _ACTIVE_CHILDREN = int(state.get("active_children", 0))
    _subagent_children = {str(k): list(v) for k, v in (state.get("children") or {}).items()}


def _update_shared(change) -> None:
    """Apply ``change`` to the shared delegation state with compare-and-set, since any replica can delegate."""
    from ts_harness.storage.kv import kv_update

    def apply(state: dict[str, Any]) -> dict[str, Any]:
        state.setdefault("active_children", 0)
        state.setdefault("children", {})
        change(state)
        return state

    _apply_shared(kv_update(DELEGATION_STATE_NAMESPACE, DELEGATION_STATE_KEY, apply, default={}))


def _children_of(parent_session_id: str) -> list[str]:
    if _shared():
        from ts_harness.storage.kv import kv_read

        _apply_shared(kv_read(DELEGATION_STATE_NAMESPACE, DELEGATION_STATE_KEY, default={}))
    return _subagent_children.setdefault(parent_session_id, [])


def _record_child(parent_session_id: str, child_session_id: str) -> None:
    if _shared():
        _update_shared(lambda s: s["children"].setdefault(parent_session_id, []).append(child_session_id))
        return
    _subagent_children.setdefault(parent_session_id, []).append(child_session_id)
    _save_state()


def _adjust_active(delta: int) -> None:
    global _ACTIVE_CHILDREN
    if _shared():
        _update_shared(lambda s: s.update(active_children=max(0, int(s["active_children"]) + delta)))
        return
    _ACTIVE_CHILDREN += delta
    _save_state()


def _load_state() -> None:
    global _ACTIVE_CHILDREN, _subagent_children
    if _shared():
        from ts_harness.storage.kv import kv_read

        try:
            _apply_shared(kv_read(DELEGATION_STATE_NAMESPACE, DELEGATION_STATE_KEY, default={}))
        except Exception:  # noqa: BLE001 - delegation works on an empty view until the memory API is back
            pass
        return
    if not _STATE_PATH:
        return
    path = Path(_STATE_PATH)
    if not path.exists():
        return
    data = json.loads(path.read_text(encoding="utf-8"))
    _ACTIVE_CHILDREN = int(data.get("active_children", 0))
    _subagent_children = {str(k): list(v) for k, v in (data.get("children") or {}).items()}


def _save_state() -> None:
    if not _STATE_PATH:
        return
    path = Path(_STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "active_children": _ACTIVE_CHILDREN,
                "children": _subagent_children,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def configure_delegation(
    config: RuntimeConfig,
    agent: EnterpriseAgent,
    *,
    max_depth: int | None = None,
    max_children: int | None = None,
    max_concurrent: int | None = None,
) -> None:
    global _runtime_config, _runtime_agent, _MAX_DEPTH, _MAX_CHILDREN, _MAX_CONCURRENT, _ACTIVE_CHILDREN
    _runtime_config = config
    _runtime_agent = agent
    _MAX_DEPTH = max_depth if max_depth is not None else config.subagents.max_depth
    _MAX_CHILDREN = max_children if max_children is not None else config.subagents.max_children_per_agent
    _MAX_CONCURRENT = max_concurrent if max_concurrent is not None else config.subagents.max_concurrent
    _load_state()


def handle_delegate_task(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    if _runtime_config is None or _runtime_agent is None:
        return {"error": "delegation runtime not configured"}
    child_id = str(args.get("agent_id", ""))
    try:
        parent_agent = _runtime_config.get_agent(ctx.agent_id)
        child_agent = _runtime_config.get_agent(child_id)
    except KeyError as exc:
        return {"error": str(exc)}

    if child_id not in set(parent_agent.subagent_allow):
        return {"error": f"agent {ctx.agent_id} cannot delegate to {child_id}"}

    session_store = get_session_store()
    parent_session = session_store.get_or_create(
        tenant_id=ctx.tenant_id,
        source=ctx.source,
        agent_id=ctx.agent_id,
    )
    if parent_session.depth >= _MAX_DEPTH:
        return {"error": "max delegation depth exceeded"}

    children = _children_of(parent_session.session_id)
    if len(children) >= _MAX_CHILDREN:
        return {"error": "max children per parent exceeded"}
    state_store = get_state_store()
    active = int(state_store.get("delegation:active_children") or str(_ACTIVE_CHILDREN))
    if active >= _MAX_CONCURRENT:
        return {"error": "max concurrent subagents exceeded"}

    child_session = session_store.create_subagent_session(parent_session, child_id)
    _record_child(parent_session.session_id, child_session.session_id)
    children = _subagent_children.get(parent_session.session_id, [])
    parent_tools = agent_effective_tools(parent_agent, _runtime_config)
    if ctx.stage_tool_allow is not None:
        parent_tools &= set(ctx.stage_tool_allow) | STAGE_ALWAYS_ALLOWED_TOOLS

    payload = {
        "workflow_id": args.get("workflow_id"),
        "task_id": child_session.session_id,
        "step_name": "delegated_task",
        "input": {
            "task": args.get("task"),
            **(args.get("input") or {}),
        },
        "required_artifacts": [],
        "skill_hints": list(child_agent.skills),
    }
    _adjust_active(1)
    state_store.set("delegation:active_children", str(active + 1))
    state_store.set_json(f"delegation:children:{parent_session.session_id}", children)
    try:
        result = _runtime_agent.run_workflow_dispatch(
            tenant_id=ctx.tenant_id,
            agent_id=child_id,
            payload=payload,
            source="subagent",
            is_subagent=True,
            parent_tools=parent_tools,
        )
    finally:
        _adjust_active(-1)
        remaining = max(0, int(state_store.get("delegation:active_children") or "1") - 1)
        state_store.set("delegation:active_children", str(remaining))

    warnings: list[str] = []
    if result.error:
        warnings.append(str(result.error))

    # Persist the full typed child output as a continuity snapshot keyed by the
    # child session (no governed program). The parent only receives refs, never
    # the full child output dumped inline.
    continuity_refs: list[dict[str, Any]] = []
    try:
        ref = write_stage_snapshot(
            tenant_id=ctx.tenant_id,
            workflow_id=str(args.get("workflow_id") or child_session.session_id),
            program_id=None,
            stage_name="delegated_task",
            output=result.output or {},
            memory_client=get_memory_client(),
            session_id=child_session.session_id,
        )
        continuity_refs.append(ref.to_dict())
    except Exception as exc:  # noqa: BLE001 - snapshot failures are non-fatal here
        warnings.append(f"continuity snapshot failed: {exc}")

    return {
        "status": result.status,
        "summary": result.output.get("summary"),
        "child_session_id": child_session.session_id,
        "continuity_refs": continuity_refs,
        "warnings": warnings,
    }
