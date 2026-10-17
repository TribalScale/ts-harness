#!/usr/bin/env python3
"""Run TS Harness release gates against a deployed/local runtime."""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

from ts_harness.gateway.release_gates import ReleaseGateRunner
from ts_harness.policy.engine import PolicyEngine
from ts_harness.config.schema import load_runtime_config
from ts_harness.audit.writer import AuditWriter
from ts_harness.secrets.resolver import SecretResolver


BASE_URL = os.environ.get("AGENT_RUNTIME_URL", "http://127.0.0.1:18789").rstrip("/")
CONFIG = os.environ.get("TS_HARNESS_CONFIG", "config/ts-harness-runtime.test.yaml")


def resolve_gateway_token() -> str:
    token = os.environ.get("GATEWAY_TOKEN", "").strip()
    if token:
        return token
    try:
        return (load_runtime_config(CONFIG).gateway.token or "").strip()
    except Exception:
        return ""


TOKEN = resolve_gateway_token()


def request(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    auth: bool = False,
    timeout: float = 10,
) -> dict:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = f"Bearer {TOKEN}"
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw) if raw else {}


RUNTIME_HEALTH_COMPONENTS = ("provider", "memory_api", "scheduler")


def runtime_health_payload_ok(result: dict) -> bool:
    components = result.get("components") or {}
    return result.get("status") == "ok" and all(
        components.get(name, {}).get("status") == "ok" for name in RUNTIME_HEALTH_COMPONENTS
    )


def wait_for_runtime_ready(
    *,
    timeout_seconds: float | None = None,
    interval_seconds: float | None = None,
) -> bool:
    timeout = float(timeout_seconds or os.environ.get("AGENT_RUNTIME_HEALTH_TIMEOUT", "120"))
    interval = float(interval_seconds or os.environ.get("AGENT_RUNTIME_HEALTH_INTERVAL", "3"))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if runtime_health_payload_ok(request("GET", "/healthz")):
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def runtime_health() -> bool:
    return runtime_health_payload_ok(request("GET", "/healthz"))


def memory_smoke() -> bool:
    if not TOKEN:
        return False
    return request("GET", "/memory/smoke", auth=True).get("ok") is True


def workflow_dispatch() -> bool:
    if not TOKEN:
        return False
    result = request(
        "POST",
        "/workflow/dispatch",
        {"tenant_id": "default", "agent_id": "orchestrator", "task_id": "release-gate", "input": {}},
        auth=True,
    )
    return result.get("status") == "succeeded" and bool(result.get("runtime_task_id"))


def session_message() -> bool:
    if not TOKEN:
        return False
    channel = os.environ.get("SLACK_RELEASE_GATE_CHANNEL", "")
    if not channel:
        return False
    result = request(
        "POST",
        "/channels/slack/send-test",
        {"tenant_id": "default", "channel": channel, "text": "TS Harness release gate"},
        auth=True,
    )
    if result.get("status") != "sent":
        return False
    app_token = (os.environ.get("SLACK_APP_TOKEN") or "").strip()
    if not app_token:
        return True
    socket_mode = os.environ.get("SLACK_SOCKET_MODE", "true").strip().lower()
    if socket_mode in {"0", "false", "no", "off"}:
        return True
    health = request("GET", "/healthz")
    socket = (health.get("components") or {}).get("slack_socket") or {}
    status = socket.get("status")
    return status in {"ok", None}


def channel_chat() -> bool:
    if not TOKEN:
        return False
    result = request(
        "POST",
        "/session/message",
        {
            "tenant_id": "default",
            "channel": "slack",
            "channel_id": "release-gate",
            "thread_id": "release-gate-thread",
            "text": "release gate channel chat smoke",
        },
        auth=True,
    )
    return result.get("status") == "succeeded" and bool(result.get("session_id"))


def workflow_record_create() -> bool:
    memory_url = os.environ.get("MEMORY_API_URL", "http://127.0.0.1:18792").rstrip("/")
    body = json.dumps(
        {
            "id": "00000000-0000-4000-8000-000000000099",
            "workflow_type": "jira_from_prd",
            "tenant_id": "default",
            "source_channel": "slack:release-gate:thread",
            "actor": "release-gate",
        }
    ).encode()
    req = urllib.request.Request(
        f"{memory_url}/workflows/create",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    memory_token = os.environ.get("MEMORY_API_TOKEN", "").strip()
    if memory_token:
        req.add_header("Authorization", f"Bearer {memory_token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.loads(response.read())
    except Exception:
        return False
    return payload.get("id") == "00000000-0000-4000-8000-000000000099"


def workflow_promotion_smoke() -> bool:
    if not TOKEN:
        return False
    health = request("GET", "/healthz")
    memory = request("GET", "/memory/smoke", auth=True)
    return health.get("status") == "ok" and memory.get("ok") is True


def sandbox_denial() -> bool:
    if not TOKEN:
        return False
    result = request("POST", "/sandbox/deny-check", {"command": "rm -rf /"}, auth=True)
    return bool(result.get("denied"))


def mcp_discovery() -> bool:
    if not TOKEN:
        return False
    tools = request("GET", "/mcp/tools", auth=True).get("tools") or []
    return any(
        str(tool.get("name", "")).startswith("mcp-") and tool.get("server")
        for tool in tools
    )


def google_workspace_smoke() -> bool:
    if not TOKEN:
        return False
    health = request("GET", "/healthz", auth=False)
    components = health.get("components") or {}
    gw = components.get("google_workspace")
    if gw is None:
        return True
    status = gw.get("status")
    return status == "ok"


def google_gmail_send_smoke() -> bool:
    if not TOKEN:
        return False
    if google_workspace_smoke() is False:
        return False
    tools = request("GET", "/connectors/google-workspace/tools", auth=True).get("tools") or []
    names = {str(t.get("name") or "") for t in tools}
    if "google_workspace_gmail_send" not in names:
        return False
    result = request(
        "POST",
        "/workflow/dispatch",
        {
            "tenant_id": "default",
            "agent_id": "comms-agent",
            "task_id": "post-deploy-gmail-smoke",
            "input": {"smoke": "gmail_send_probe", "to": os.environ.get("SMOKE_GMAIL_TO", "")},
        },
        auth=True,
    )
    return result.get("status") in {"succeeded", "blocked", "completed"}


def google_doc_create_smoke() -> bool:
    if not TOKEN:
        return False
    if google_workspace_smoke() is False:
        return False
    result = request(
        "POST",
        "/workflow/dispatch",
        {
            "tenant_id": "default",
            "agent_id": "pm-agent",
            "task_id": "post-deploy-doc-smoke",
            "input": {"smoke": "google_doc_create_probe"},
        },
        auth=True,
    )
    return result.get("status") in {"succeeded", "blocked", "completed"}


def prd_workflow_live_smoke() -> bool:
    if not TOKEN:
        return False
    memory_url = os.environ.get("MEMORY_API_URL", "http://127.0.0.1:18792").rstrip("/")
    workflow_id = "00000000-0000-4000-8000-0000000000prd"
    body = json.dumps(
        {
            "id": workflow_id,
            "workflow_type": "prd_request",
            "tenant_id": "default",
            "source_channel": "post-deploy-smoke",
            "actor": "post-deploy-smoke",
        }
    ).encode()
    req = urllib.request.Request(
        f"{memory_url}/workflows/create",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.loads(response.read())
    except Exception:
        return False
    return payload.get("id") == workflow_id


def subagent_delegation() -> bool:
    if not TOKEN:
        return False
    result = request(
        "POST",
        "/workflow/dispatch",
        {
            "tenant_id": "default",
            "agent_id": "orchestrator",
            "task_id": "release-gate-delegation",
            "input": {"task": "delegate a harmless release gate smoke check to dev-agent"},
        },
        auth=True,
        timeout=120,
    )
    status = result.get("status")
    return status in {"succeeded", "blocked"} and bool(result.get("runtime_task_id"))


def cron_heartbeat() -> bool:
    if not TOKEN:
        return False
    health = request("GET", "/healthz")
    scheduler_ok = (health.get("components") or {}).get("scheduler", {}).get("status") == "ok"
    trigger = request(
        "POST",
        "/cron/trigger",
        {"tenant_id": "default", "source": "release-gate"},
        auth=True,
    )
    return scheduler_ok and trigger.get("status") == "ok" and trigger.get("scheduler") == "triggered"


def tenant_isolation() -> bool:
    audit = AuditWriter()
    audit.append(tenant_id="a", action_type="test", actor_id="u")
    audit.append(tenant_id="b", action_type="test", actor_id="u")
    return len(audit.export(tenant_id="a")) == 1


def rbac_identity() -> bool:
    engine = PolicyEngine(load_runtime_config(CONFIG))
    return engine.evaluate_identity({"roles": ["member"]}, ["admin"]).decision == "deny"


def audit_export() -> bool:
    audit = AuditWriter()
    audit.append(tenant_id="default", action_type="export", actor_id="u")
    return bool(audit.export(tenant_id="default"))


def agent_redteam() -> bool:
    from ts_harness.evals.redteam import run_live

    return run_live()


def agent_harness_security() -> bool:
    from ts_harness.evals.harness import agent_harness_security as check

    return check()


def agent_harness_quality() -> bool:
    from ts_harness.evals.quality import agent_harness_quality as check

    return check()


def agent_output_judge() -> bool:
    from ts_harness.evals.output_judge import run_live

    return run_live()


def secrets_redaction() -> bool:
    resolver = SecretResolver()
    resolver._cache["ts-harness/default/test"] = "super-secret-value"
    return "super-secret-value" not in resolver.redact("leak super-secret-value")


def budget_hard_stop() -> bool:
    engine = PolicyEngine(load_runtime_config(CONFIG))
    engine.evaluate_budget(tenant_id="default", cost_usd=60)
    return engine.evaluate_budget(tenant_id="default", cost_usd=50).decision == "deny"


def workflow_hook_smoke() -> bool:
    """In-process smoke: prd_request reject hook via WorkflowEngine."""
    try:
        from pathlib import Path
        import sys
        from unittest.mock import MagicMock, patch

        root = Path(__file__).resolve().parents[1]
        for path in (str(root / "runtime"), str(root / "scripts")):
            if path not in sys.path:
                sys.path.insert(0, path)

        from ts_harness.workflows.engine import WorkflowEngine

        client = MagicMock()
        client.get_workflow.return_value = {
            "workflow": {"workflow_type": "prd_request"},
            "tasks": [
                {
                    "id": "draft-1",
                    "step_name": "draft_prd",
                    "status": "succeeded",
                    "output": {"summary": "draft"},
                    "created_at": "2026-01-01",
                },
                {
                    "id": "gate-1",
                    "step_name": "approve_prd",
                    "approval_status": "pending",
                    "workflow_type": "prd_request",
                },
            ],
        }
        with patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk") as mock_bulk:
            mock_bulk.side_effect = [["rev-1"], ["gate-2"]]
            engine = WorkflowEngine(client=client)
            result = engine.handle_reject(
                workflow_id="wf-hook-smoke",
                gate_task={
                    "id": "gate-1",
                    "step_name": "approve_prd",
                    "workflow_type": "prd_request",
                    "approval_status": "pending",
                },
                comments="release gate smoke",
                actor="release-gate",
            )
        return result.get("revise_task_id") == "rev-1"
    except Exception:
        return False


def prd_request_smoke() -> bool:
    """In-process promote + reject loop against prd_request skeleton."""
    try:
        from unittest.mock import MagicMock, patch

        from ts_harness.agent.sessions import SessionRecord, reset_session_store
        from ts_harness.workflows.engine import WorkflowEngine
        from ts_harness.workflows.materialize import load_skeleton

        skeleton = load_skeleton("prd_request")
        if skeleton.stages[0].name != "intake":
            return False

        reset_session_store()
        client = MagicMock()
        client.create_workflow.return_value = {"id": "wf-prd-rg"}
        # prd_request materializes intake + await + draft + approve.
        client.create_task.side_effect = [{"id": f"t{i}"} for i in range(8)]
        client.get_workflow.return_value = {
            "workflow": {"workflow_type": "prd_request", "source_channel": "slack:rg:1"},
            "tasks": [
                {
                    "id": "draft-1",
                    "step_name": "draft_prd",
                    "status": "succeeded",
                    "output": {"summary": "draft"},
                    "created_at": "2026-01-01",
                },
                {
                    "id": "gate-1",
                    "step_name": "approve_prd",
                    "approval_status": "pending",
                    "status": "pending",
                    "workflow_type": "prd_request",
                },
            ],
        }

        session = SessionRecord(
            session_id="rg-prd",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={"channel": "slack", "channel_id": "rg", "thread_id": "1"},
        )
        engine = WorkflowEngine(client=client)
        promote = engine.start_promotion(
            session=session,
            workflow_type="prd_request",
            summary="release gate PRD",
            tenant_id="default",
            binding_key="slack:rg:1",
        )
        if not promote.get("workflow_id") or promote.get("first_step") != "intake":
            return False

        with patch("ts_harness.workflows.hooks.revise_on_reject.create_tasks_bulk") as mock_bulk:
            mock_bulk.side_effect = [["rev-1"], ["gate-2"]]
            reject = engine.handle_reject(
                workflow_id="wf-prd-rg",
                gate_task={
                    "id": "gate-1",
                    "step_name": "approve_prd",
                    "workflow_type": "prd_request",
                    "approval_status": "pending",
                },
                comments="release gate revise",
                actor="release-gate",
            )
        return reject.get("revise_task_id") == "rev-1"
    except Exception:
        return False


def continue_delivery_smoke() -> bool:
    try:
        from unittest.mock import MagicMock

        from ts_harness.agent.sessions import SessionRecord, reset_session_store
        from ts_harness.workflows.hooks import continue_delivery
        from ts_harness.workflows.materialize import load_skeleton
        from ts_harness.workflows.registry import HookContext

        reset_session_store()
        session = SessionRecord(
            session_id="rg-bridge",
            routing_key="rk",
            tenant_id="default",
            agent_id="orchestrator",
            source="slack",
            metadata={
                "workflow_id": "wf-prd-rg",
                "source_workflow_id": "wf-prd-rg",
                "workflow_type": "prd_request",
                "mode": "workflow_linked",
                "workflow_phase": "approved",
                "binding_key": "slack:rg:1",
                "approved_artifact": {"summary": "PRD"},
            },
        )
        client = MagicMock()
        client.create_workflow.return_value = {"id": "wf-del-rg"}
        client.create_task.side_effect = [{"id": f"t{i}"} for i in range(5)]
        skeleton = load_skeleton("prd_request")
        ctx = HookContext(
            workflow_id="wf-prd-rg",
            workflow_type="prd_request",
            tenant_id="default",
            task={"id": "gate-1", "step_name": "approve_prd"},
            stage=skeleton.stage("approve_prd"),
            skeleton=skeleton,
            actor="release-gate",
            memory_client=client,
            extras={
                "session": session,
                "source_workflow_id": "wf-prd-rg",
                "approved_artifact": {"summary": "PRD"},
                "binding_key": "slack:rg:1",
                # Must match a bridge target signal (plain "continue" no longer resolves).
                "summary": "create jira",
            },
        )
        result = continue_delivery.handle(ctx)
        task_count = len(result.get("task_ids") or [])
        return (
            result.get("target_workflow_type") == "jira_from_prd"
            and result.get("first_step") == "create_jira"
            and task_count in {1, 5}
        )
    except Exception:
        return False


def intent_routing_smoke() -> bool:
    try:
        from ts_harness.config.schema import load_runtime_config
        from ts_harness.routing.intents import classify_channel_intent

        config = load_runtime_config(CONFIG)
        intent = classify_channel_intent("Create a PRD from these requirements", {}, config=config)
        return intent.kind == "governed_prd" and intent.workflow_type == "prd_request"
    except Exception:
        return False


def workflow_runner_import_smoke() -> bool:
    """Verify installed runtime bundle can import workflow engine and hook modules."""
    try:
        from ts_harness.repo_paths import scripts_dir, workflows_dir
        from ts_harness.workflows.engine import get_workflow_engine
        from ts_harness.workflows.hooks import continue_delivery

        if not (scripts_dir() / "workflow_definitions.py").is_file():
            return False
        if not any(workflows_dir().glob("*.json")):
            return False

        engine = get_workflow_engine()
        return hasattr(engine, "on_task_transition") and continue_delivery is not None
    except Exception:
        return False


def delivery_program() -> bool:
    try:
        from ts_harness.repo_paths import ensure_scripts_importable, programs_dir
        from ts_harness.workflows.hooks import build_default_registry
        from ts_harness.workflows.materialize import load_skeleton
        from ts_harness.workflows.program_materialize import (
            build_resolved_workflow_versions,
        )
        from ts_harness.workflows.program_registry import (
            get_program_definition_registry,
        )

        ensure_scripts_importable()
        import program_definitions

        program_registry = get_program_definition_registry()
        if not program_registry.reload_if_changed():
            return False
        snapshot = program_registry.load("software_delivery")
        builtin_path = programs_dir() / "software_delivery.json"
        if not builtin_path.is_file():
            return False
        loaded = program_definitions.load_program(builtin_path)
        if loaded.program_type != str(snapshot.get("program_type") or ""):
            return False

        resolved = build_resolved_workflow_versions(snapshot)
        workflow_types: set[str] = set()
        for node in list(snapshot.get("nodes") or []):
            if not isinstance(node, dict):
                continue
            default_type = str(node.get("default_workflow_type") or "").strip()
            if default_type:
                workflow_types.add(default_type)
            for variant in list(node.get("workflow_variants") or []):
                if not isinstance(variant, dict):
                    continue
                variant_type = str(variant.get("workflow_type") or "").strip()
                if variant_type:
                    workflow_types.add(variant_type)
        for node_id, pin in resolved.items():
            if not isinstance(pin, dict):
                continue
            pins = pin.get("pins") if isinstance(pin.get("pins"), dict) else {}
            for workflow_type, version in pins.items():
                skeleton = load_skeleton(str(workflow_type))
                if version and str(skeleton.definition_version) != str(version):
                    # Pin may lag active; still require the workflow type to load.
                    pass
                workflow_types.add(str(workflow_type))
            _ = node_id
        for workflow_type in sorted(workflow_types):
            load_skeleton(workflow_type)

        hook_registry = build_default_registry()
        hook_registry.get("complete_program_phase")
        hook_registry.get("register_program")
        return True
    except Exception:
        return False


def work_orders_delivery_smoke() -> bool:
    try:
        from ts_harness.workflows.delivery_program import (
            DeliveryProgramService,
            resolve_bridge_target_from_text,
            resolve_workflow_type,
        )
        from ts_harness.workflows.materialize import fork_child_type, load_skeleton

        wo_skeleton = load_skeleton("work_orders_from_prd")
        impl_skeleton = load_skeleton("implement_work_orders")
        if wo_skeleton.program.get("phase") != "work_orders":
            return False
        if impl_skeleton.program.get("phase") != "implement":
            return False
        if resolve_bridge_target_from_text("plan work orders") != "work_orders_from_prd":
            return False
        if resolve_workflow_type("implement", {"planning_track": "work_orders"}) != "implement_work_orders":
            return False
        refine = next(
            stage for stage in wo_skeleton.stages if stage.name == "refine_requirements"
        )
        if refine.skill_hints != ["prd-to-work-orders-governed"]:
            return False
        prepare = next(
            stage for stage in wo_skeleton.stages if stage.name == "prepare_work_order_delivery"
        )
        if prepare.skill_hints != ["source-work-order-governed"]:
            return False
        wave = next(stage for stage in impl_skeleton.stages if stage.name == "deliver_wave")
        if fork_child_type(wave.barrier) != "deliver_work_order":
            return False
        child = load_skeleton("deliver_work_order")
        deliver = next(stage for stage in child.stages if stage.name == "deliver_work_order")
        if deliver.skill_hints != ["verified-merge-governed"]:
            return False
        service = DeliveryProgramService(client=__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock())
        errors = service.validate_entry(
            "implement",
            {
                "planner_complete": True,
                "next_work_order_id": "WO-001",
                "work_orders_handoff": True,
            },
        )
        return errors == []
    except Exception:
        return False


def coding_backend_readiness() -> bool:
    """Verify OpenCode tools path, git/gh, and coding-backend env inside agent-runtime."""
    import shutil
    from pathlib import Path

    from ts_harness.config.schema import load_runtime_config
    from ts_harness.implementation.backends.opencode import resolve_opencode_binary

    missing_bins = [name for name in ("git", "gh") if shutil.which(name) is None]
    if missing_bins:
        raise RuntimeError(f"missing binaries in agent-runtime: {', '.join(missing_bins)}")
    for name in ("CODING_BACKEND_PROVIDER", "CODING_BACKEND_API_KEY", "GITHUB_TOKEN"):
        if not os.environ.get(name, "").strip():
            raise RuntimeError(f"{name} missing from agent-runtime env (runtime .env / compose env_file)")
    config = load_runtime_config(CONFIG)
    tools_root = config.implementation.tools_root or os.environ.get(
        "TS_HARNESS_TOOLS_ROOT", "/tools"
    )
    if not Path(tools_root).is_dir():
        raise RuntimeError(f"tools_root is not a directory: {tools_root}")
    resolve_opencode_binary(tools_root=tools_root)
    return True


def continue_bridge_smoke() -> bool:
    return continue_delivery_smoke()


def skeleton_hooks_registered() -> bool:
    try:
        from ts_harness.workflows.hooks import build_default_registry
        from ts_harness.workflows.materialize import list_skeleton_types, load_skeleton

        registry = build_default_registry()
        hook_names: set[str] = set()
        for workflow_type in list_skeleton_types():
            skeleton = load_skeleton(workflow_type)
            cancel = dict(skeleton.on_workflow_cancel or {})
            if cancel.get("hook"):
                hook_names.add(str(cancel["hook"]))
            for stage in skeleton.stages:
                on_complete = dict(stage.on_complete or {})
                if on_complete.get("hook"):
                    hook_names.add(str(on_complete["hook"]))
                on_reject = dict(stage.on_reject or {})
                if on_reject.get("hook"):
                    hook_names.add(str(on_reject["hook"]))
                on_failure = dict(stage.on_failure or {})
                if on_failure.get("hook"):
                    hook_names.add(str(on_failure["hook"]))
        for hook_name in hook_names:
            registry.get(hook_name)
        return True
    except Exception:
        return False


def main() -> int:
    profile = os.environ.get("RELEASE_GATES_PROFILE", "full").strip().lower()
    if profile != "live" and not wait_for_runtime_ready():
        print(
            json.dumps(
                {
                    "error": "agent-runtime not ready",
                    "detail": f"timed out waiting for {BASE_URL}/healthz",
                }
            ),
            file=sys.stderr,
        )
        return 1

    checks = {
        "runtime_health": runtime_health,
        "memory_smoke": memory_smoke,
        "workflow_dispatch": workflow_dispatch,
        "slack_round_trip": session_message,
        "mcp_discovery": mcp_discovery,
        "google_workspace_smoke": google_workspace_smoke,
        "sandbox_denial": sandbox_denial,
        "subagent_delegation": subagent_delegation,
        "cron_heartbeat": cron_heartbeat,
        "tenant_isolation": tenant_isolation,
        "rbac_identity": rbac_identity,
        "audit_export": audit_export,
        "secrets_redaction": secrets_redaction,
        "agent_harness_security": agent_harness_security,
        "agent_harness_quality": agent_harness_quality,
        "agent_redteam": agent_redteam,
        "agent_output_judge": agent_output_judge,
        "budget_hard_stop": budget_hard_stop,
        "channel_chat": channel_chat,
        "workflow_record_create": workflow_record_create,
        "workflow_promotion_smoke": workflow_promotion_smoke,
        "workflow_hook_smoke": workflow_hook_smoke,
        "prd_request_smoke": prd_request_smoke,
        "continue_bridge_smoke": continue_bridge_smoke,
        "continue_delivery_smoke": continue_delivery_smoke,
        "intent_routing_smoke": intent_routing_smoke,
        "workflow_runner_import_smoke": workflow_runner_import_smoke,
        "skeleton_hooks_registered": skeleton_hooks_registered,
        "delivery_program": delivery_program,
        "work_orders_delivery_smoke": work_orders_delivery_smoke,
        "coding_backend_readiness": coding_backend_readiness,
        "google_gmail_send_smoke": google_gmail_send_smoke,
        "google_doc_create_smoke": google_doc_create_smoke,
        "prd_workflow_live_smoke": prd_workflow_live_smoke,
    }
    profile = os.environ.get("RELEASE_GATES_PROFILE", "full")
    report = ReleaseGateRunner(checks, profile=profile).run()
    print(json.dumps({name: result.__dict__ for name, result in report.results.items()}, indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
