#!/usr/bin/env python3
"""Artemis/local E2E: builder agent proposes a list-reentry program, activate, run two loop iterations.

Uses FakeProvider only for the builder chat turn; workflow steps are completed via the same
after_task_succeeded path the workflow-runner uses (no LLM per stage).
"""

from __future__ import annotations

import copy
import json
import sys
import time
from typing import Any

from ts_harness.agent.loop import EnterpriseAgent
from ts_harness.agent.provider import FakeProvider
from ts_harness.gateway import program_builder_bff as builder
from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.server import get_runtime
from ts_harness.agent.sessions import get_session_store
from ts_harness.tools.policy import ToolPolicyContext
from ts_harness.tools.registry import build_default_registry
from ts_harness.workflows.delivery_program import get_delivery_program_service
from ts_harness.workflows.engine import get_workflow_engine


def _admin() -> AuthContext:
    return AuthContext(
        actor_id="e2e-loop-admin",
        tenant_id="default",
        email="e2e-loop@example.com",
        roles=["admin"],
        auth_provider="static",
    )


def _builder_agent_available(runtime: Any) -> bool:
    agent_id = builder.builder_agent_id(runtime.config)
    try:
        runtime.config.get_agent(agent_id)
        return True
    except KeyError:
        return False


def _propose_via_builder_tools(runtime: Any, auth: AuthContext, bundle_args: dict[str, Any]):
    """Same tool the builder agent calls when propose_program_bundle runs."""
    session = builder._new_builder_session(runtime, auth)
    session.metadata = {
        **(session.metadata or {}),
        builder.ACTOR_KEY: builder.auth_snapshot(auth),
    }
    get_session_store().save(session)
    ctx = ToolPolicyContext(
        tenant_id=auth.tenant_id,
        agent_id=builder.builder_agent_id(runtime.config),
        session_id=session.session_id,
        source=builder.BUILDER_CHANNEL,
    )
    result = build_default_registry().dispatch(
        "propose_program_bundle",
        copy.deepcopy(bundle_args),
        context=ctx,
    )
    return session, result


def _propose_via_agent_chat(runtime: Any, auth: AuthContext, bundle_args: dict[str, Any], program_type: str):
    old_agent = runtime.agent
    args_json = json.dumps(bundle_args)
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
                                    "id": "call-propose",
                                    "type": "function",
                                    "function": {
                                        "name": "propose_program_bundle",
                                        "arguments": args_json,
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
                            "content": "E2E loop bundle drafted.",
                        }
                    }
                ]
            },
        ]
    )
    runtime.agent = EnterpriseAgent(runtime.config, build_default_registry(), provider=provider)
    try:
        chat = builder.handle_builder_message(
            runtime,
            auth,
            {"text": f"Build the e2e list-reentry program {program_type} with two account iterations."},
        )
    finally:
        runtime.agent = old_agent
    if chat.get("status") != "succeeded":
        raise RuntimeError(f"builder chat failed: {chat}")
    return builder.owned_builder_session(str(chat["session_id"]), auth), chat


def _bundle(tag: str) -> dict[str, Any]:
    wf_type = f"e2e_loop_wf_{tag}"
    program_type = f"e2e_loop_prog_{tag}"
    workflow = {
        "workflow_type": wf_type,
        "definition_version": "1.0.0",
        "display_name": f"E2E loop workflow {tag}",
        "stages": [
            {
                "name": "run",
                "kind": "task",
                "agent_id": "ops-agent",
            }
        ],
    }
    reentry = {
        "mode": "list",
        "target_node": "summarize",
        "remaining_list_artifact": "account_ids",
        "next_item_artifact": "account_id",
    }
    program = {
        "program_type": program_type,
        "definition_version": "1.0.0",
        "display_name": f"E2E loop program {tag}",
        "startable_nodes": ["collect"],
        "nodes": [
            {
                "id": "collect",
                "default_workflow_type": wf_type,
                "workflow_variants": [],
                "required_artifacts": [],
                "produces_artifacts": ["account_ids"],
                "input_bindings": [],
                "completion_adapter": "extract_declared_outputs",
                "terminal": False,
            },
            {
                "id": "summarize",
                "default_workflow_type": wf_type,
                "workflow_variants": [],
                "required_artifacts": ["account_id"],
                "produces_artifacts": ["account_summary"],
                "input_bindings": [],
                "completion_adapter": "extract_declared_outputs",
                "terminal": True,
                "reentry": reentry,
            },
        ],
        "edges": [
            {
                "source": "collect",
                "target": "summarize",
                "mode": "automatic",
                "confirm_required": False,
                "signals": [],
            }
        ],
    }
    routing = {
        "id": f"e2e_loop_intent_{tag}",
        "signals": [f"start e2e loop {tag}"],
        "program_type": program_type,
        "entry_phase": "collect",
    }
    return {"workflow": workflow, "program": program, "routing_intent": routing}


def _claim_task(client: Any, task: dict[str, Any]) -> None:
    status = str(task.get("status") or "")
    if status != "pending":
        return
    client._request(
        "POST",
        "/workflow/claim",
        {
            "id": task["id"],
            "agent_id": task.get("agent_id") or "ops-agent",
            "locked_by": "e2e-loop",
            "lease_seconds": 300,
            "actor": "e2e-loop",
        },
    )


def _finish_open_task(client: Any, engine: Any, workflow_id: str, output: dict[str, Any]) -> dict[str, Any]:
    payload = client.get_workflow_tasks(workflow_id)
    tasks = list(payload.get("tasks") or [])
    terminal = {"succeeded", "cancelled", "failed"}
    active = [t for t in tasks if str(t.get("status") or "") not in terminal]
    if not active:
        raise RuntimeError(f"no active task on workflow {workflow_id}: {tasks}")
    task = active[0]
    task_id = str(task["id"])
    _claim_task(client, task)
    client.complete_task_api(task_id, output, actor="e2e-loop")
    dispatch = {"status": "succeeded", "output": output}
    merged = {**task, "status": "succeeded", "output": output}
    hook = engine.after_task_succeeded(merged, dispatch_result=dispatch)
    return {"task_id": task_id, "step": task.get("step_name"), "hook": hook}


def _active_workflow_id(status: dict[str, Any]) -> str | None:
    wid = status.get("active_workflow_id")
    return str(wid).strip() if wid else None


def _program_artifacts(client: Any, program_id: str) -> dict[str, Any]:
    prog = (client.get_program(program_id).get("program") or {})
    meta = prog.get("metadata") or {}
    if isinstance(meta, str):
        meta = json.loads(meta)
    return dict((meta.get("artifacts") or {}) if isinstance(meta, dict) else {})


def main() -> int:
    tag = str(int(time.time()))
    bundle = _bundle(tag)
    program_type = bundle["program"]["program_type"]
    auth = _admin()
    runtime = get_runtime()
    print(f"=== builder (tag={tag}) ===")
    if _builder_agent_available(runtime):
        print("path: agent chat + propose_program_bundle tool")
        session, _chat = _propose_via_agent_chat(runtime, auth, bundle, program_type)
    else:
        print(
            "path: propose_program_bundle tool only "
            "(builder-agent missing from runtime config; add it for full chat E2E)"
        )
        session, tool_result = _propose_via_builder_tools(runtime, auth, bundle)
        if tool_result.get("error"):
            print("propose failed:", json.dumps(tool_result, indent=2))
            return 1
    session_id = session.session_id
    state = builder.builder_session_state(runtime, auth, session_id)
    if not state.get("ready"):
        print("bundle not ready:", json.dumps(state, indent=2))
        return 1

    revisions = {
        kind: state[kind]["revision"]
        for kind in builder.PARTS
        if state[kind].get("revision")
    }
    activated = builder.activate_builder_bundle(
        runtime,
        auth,
        session_id,
        {"expected_revisions": revisions},
    )
    if not activated.get("ok"):
        print("activate failed:", json.dumps(activated, indent=2))
        return 1
    print("activated:", json.dumps(activated.get("steps"), indent=2))

    delivery = get_delivery_program_service()
    start = delivery.create_program(
        program_type=program_type,
        entry_node="collect",
        summary=f"E2E loop verify {tag}",
        tenant_id="default",
        binding_key=f"e2e:loop:{tag}",
        source_channel=f"e2e:loop:{tag}",
        requester_actor_id=auth.actor_id,
        actor="e2e-loop",
    )
    program_id = start.program_id
    print(f"=== program started program_id={program_id} wf={start.workflow_id} ===")

    client = delivery.client
    engine = get_workflow_engine()
    reentry_events = 0

    # Phase 1: collect -> account_ids
    wid = start.workflow_id
    step1 = _finish_open_task(
        client,
        engine,
        wid,
        {"account_ids": ["acct-alpha", "acct-beta"]},
    )
    print("collect complete:", json.dumps(step1, indent=2, default=str))

    status = client.get_program_status(program_id)
    wid = _active_workflow_id(status) or str(
        ((step1.get("hook") or {}).get("advance") or {}).get("workflow_id") or ""
    ) or wid
    arts = _program_artifacts(client, program_id)
    print("after collect:", json.dumps({"phase": status.get("phase"), "account_id": arts.get("account_id")}))

    # Phase 2–3: summarize twice (list reentry)
    for label in ("acct-alpha", "acct-beta"):
        if not wid:
            status = client.get_program_status(program_id)
            wid = _active_workflow_id(status)
        if not wid:
            raise RuntimeError("no active workflow for summarize iteration")
        step = _finish_open_task(
            client,
            engine,
            wid,
            {"account_summary": f"summary-for-{label}"},
        )
        hook = step.get("hook") or {}
        if hook.get("status") == "phase_reentered":
            reentry_events += 1
        advance = hook.get("advance") if isinstance(hook.get("advance"), dict) else {}
        wid = str(advance.get("workflow_id") or "") or None
        print(f"summarize ({label}):", json.dumps(step, indent=2, default=str))
        status = client.get_program_status(program_id)
        if not wid:
            wid = _active_workflow_id(status)
        arts = _program_artifacts(client, program_id)
        print(
            "status:",
            json.dumps(
                {
                    "program_status": status.get("status"),
                    "phase": status.get("phase"),
                    "active_workflow_id": wid,
                    "account_id": arts.get("account_id"),
                    "completed_account_ids": arts.get("completed_account_ids"),
                    "reentry_iterations": arts.get("reentry_iterations"),
                },
                indent=2,
            ),
        )

    final = client.get_program_status(program_id)
    final_arts = _program_artifacts(client, program_id)
    ok = (
        reentry_events == 1
        and str(final.get("status") or "").lower() in {"completed", "complete", "succeeded"}
        or (
            reentry_events == 1
            and final.get("phase") == "summarize"
            and not _active_workflow_id(final)
        )
    )
    # Program may show completed or idle with no active wf after terminal node.
    if not _active_workflow_id(final):
        completed_list = list(final_arts.get("completed_account_ids") or [])
        ok = (
            reentry_events == 1
            and completed_list == ["acct-alpha", "acct-beta"]
            and final_arts.get("account_id") == "acct-beta"
        )

    print("=== summary ===")
    print(
        json.dumps(
            {
                "tag": tag,
                "program_type": program_type,
                "program_id": program_id,
                "reentry_events": reentry_events,
                "final_status": final,
                "artifacts": final_arts,
                "pass": ok,
            },
            indent=2,
            default=str,
        )
    )
    if not ok:
        return 1
    try:
        delivery.client.cancel_program(program_id, actor="e2e-loop", reason="e2e cleanup")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
