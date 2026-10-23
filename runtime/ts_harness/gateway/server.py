"""TS Harness agent runtime HTTP gateway."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import hashlib
import hmac
import json
import os
import signal
import threading
import time
from pathlib import Path

from ts_harness.agent.loop import EnterpriseAgent
from ts_harness.agent.provider import FakeProvider
from ts_harness.agent.sessions import get_session_store
from ts_harness.audit.writer import AuditWriter
from ts_harness.config.reload import default_lkg_path, reload_runtime_config
from ts_harness.secrets.resolver import SecretResolver
from ts_harness.gateway.auth import AuthContext, AuthError, authenticate_request, check_allowed_origin
from ts_harness.gateway.contracts import DispatchResult, normalize_runtime_body
from ts_harness.gateway.health import build_health_report
from ts_harness.gateway.channels.router import get_channel_router
from ts_harness.gateway.channels.slack_socket import SlackSocketListener
from ts_harness.gateway.scheduler import Scheduler
from ts_harness.memory.client import get_memory_client
from ts_harness.observability.logging import log_event
from ts_harness.observability.metrics import MetricsRegistry
from ts_harness.policy.engine import PolicyEngine
from ts_harness.skills.aliases import canonical_skill_ids
from ts_harness.skills.catalog import SkillCatalog
from ts_harness.sandbox.docker_env import DockerExecutionEnvironment
from ts_harness.tools.audit import configure_tool_audit
from ts_harness.tools.delegation import configure_delegation
from ts_harness.tools.approvals import scan_command
from ts_harness.tools.mcp_client import get_mcp_manager
from ts_harness.tools.registry import build_default_registry
from ts_harness.state import get_state_store


def _build_channel_system_context(
    session,
    *,
    delegation_confirmed: bool,
    workflow_confirmed: bool,
    continue_delivery_confirmed: bool = False,
    bridge_confirmed: bool = False,
    program_confirmed: bool = False,
    program_advance_confirmed: bool = False,
    channel_intent: str | None = None,
    orchestrator_hint: str | None = None,
) -> str:
    parts: list[str] = []
    metadata = session.metadata or {}
    if delegation_confirmed:
        pending = metadata.get("pending_delegate") or {}
        parts.append(
            "[system context] Pending delegation confirmed. "
            f"Call delegate_task with agent_id={pending.get('agent_id')!r}, "
            f"task={pending.get('task')!r}, input={pending.get('input')!r}."
        )
    if workflow_confirmed:
        pending = metadata.get("pending_workflow") or {}
        parts.append(
            "[system context] Pending workflow confirmed. "
            f"Call start_workflow with workflow_type={pending.get('workflow_type')!r}, "
            f"summary={pending.get('summary')!r}."
        )
    if program_confirmed:
        pending = metadata.get("pending_program") or {}
        parts.append(
            "[system context] Pending delivery program confirmed. "
            f"Call start_program with entry_phase={pending.get('entry_phase')!r}, "
            f"summary={pending.get('summary')!r}."
        )
    if program_advance_confirmed:
        pending = metadata.get("pending_program_advance") or {}
        parts.append(
            "[system context] Pending program advance confirmed. "
            f"Call advance_program with program_id={pending.get('program_id')!r}, "
            f"summary={pending.get('summary')!r}."
        )
    confirmed = bridge_confirmed or continue_delivery_confirmed
    if confirmed:
        from ts_harness.workflows.session_compat import read_pending_bridge

        pending = read_pending_bridge(metadata) or {}
        parts.append(
            "[system context] Pending bridge confirmed. "
            f"Call bridge_workflow with summary={pending.get('summary')!r}."
        )
    if orchestrator_hint and not metadata.get("workflow_id"):
        parts.append(f"[system context] {orchestrator_hint.strip()}")
    from ts_harness.workflows.session_compat import read_specialist_handoff

    handoff = read_specialist_handoff(metadata)
    if isinstance(handoff, dict):
        from ts_harness.workflows.artifacts import artifact_body, artifact_title

        output = handoff.get("output") if isinstance(handoff.get("output"), dict) else {}
        step_name = str(handoff.get("step_name") or "")
        agent_id = str(handoff.get("agent_id") or "")
        title = artifact_title(output, fallback=step_name.replace("_", " ") or "Specialist output")
        parts.append(
            "[system context] Specialist output was posted to this thread "
            f"({agent_id or 'specialist'} / {step_name or 'task'}: {title}). "
            "Do not claim it lives in email or external tools. "
            "Help the user review, approve, or steer next steps from the posted content."
        )
        body = artifact_body(output)
        if body:
            excerpt = body if len(body) <= 1200 else f"{body[:1200].rstrip()}..."
            parts.append(f"[system context] Latest specialist output excerpt:\n{excerpt}")
    return "\n".join(parts)


class RuntimeState:
    def __init__(self, config_path: str):
        config_file = Path(config_path)
        self.config = reload_runtime_config(config_file, lkg_path=default_lkg_path(config_file))
        # YAML baselines for managed overlay merges (fail-closed reload).
        from ts_harness.config.runtime_settings import yaml_settings_baseline

        self._yaml_settings_baseline = yaml_settings_baseline(self.config)
        self._yaml_routing_baseline = self.config.routing
        from copy import deepcopy

        self._yaml_mcp_baseline = deepcopy(dict(self.config.mcp_servers or {}))
        try:
            from ts_harness.config.runtime_settings import (
                reload_runtime_settings_into_runtime,
            )

            reload_runtime_settings_into_runtime(self)
        except Exception:
            pass
        try:
            from ts_harness.gateway.routing_definitions_bff import (
                reload_routing_into_runtime,
            )

            reload_routing_into_runtime(self)
        except Exception:
            pass
        try:
            from ts_harness.config.mcp_settings import reload_mcp_settings_into_runtime

            reload_mcp_settings_into_runtime(self)
        except Exception:
            pass
        self._yaml_agents_baseline = deepcopy(list(self.config.agents))
        try:
            from ts_harness.config.agents_overlay import reload_agents_overlay_into_runtime

            reload_agents_overlay_into_runtime(self)
        except Exception:
            pass
        self._dispatch_cache: dict[str, tuple[float, DispatchResult]] = {}
        self._dispatch_cache_ttl = int(os.environ.get("TS_HARNESS_DISPATCH_CACHE_TTL", "300"))
        self._state_store = get_state_store()
        self.secret_resolver = SecretResolver()
        self.registry = build_default_registry()
        self.audit = AuditWriter(os.environ.get("TS_HARNESS_AUDIT_LOG") or None)
        self.metrics = MetricsRegistry()
        self.policy = PolicyEngine(self.config)
        self.skill_catalog = SkillCatalog.default()
        self.skill_catalog.load()
        configure_tool_audit(
            lambda event: self.audit.append(
                tenant_id=event.tenant_id,
                action_type="tool_call",
                actor_id=event.agent_id,
                correlation_id=event.correlation_id,
                payload={
                    "tool_name": event.tool_name,
                    "decision": event.decision,
                    "policy_id": event.policy_id,
                    "args": event.args_redacted,
                    "result_preview": event.result_preview,
                },
            )
        )
        from ts_harness.policy.opa import configure_opa

        if getattr(self.config.policy, "opa_enabled", False) or os.environ.get("TS_HARNESS_OPA_ENABLED", "").lower() == "true":
            configure_opa()
        provider = self._build_shadow_provider()
        self.agent = EnterpriseAgent(
            self.config,
            self.registry,
            provider=provider,
            on_audit=self._on_audit,
            policy_engine=self.policy,
        )
        self.cron_agent = EnterpriseAgent(
            self.config,
            self.registry,
            provider=provider,
            on_audit=self._on_audit,
            policy_engine=self.policy,
        )
        configure_delegation(self.config, self.agent)
        self._shutdown = threading.Event()
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()
        self._start_sandbox_reaper()
        self.mcp = get_mcp_manager()
        skip_mcp = os.environ.get("TS_HARNESS_SKIP_MCP_DISCOVERY", "").lower() == "true"
        for name, server_cfg in self.config.mcp_servers.items():
            if server_cfg.get("enabled", True):
                self.mcp.register_server(name, server_cfg)
                if not skip_mcp:
                    self.mcp.discover_tools(name)
        if not skip_mcp:
            self.mcp.register_tools(self.registry)
        from ts_harness.connectors.registry import ConnectorRegistry, set_connector_registry

        self.connector_registry = ConnectorRegistry(self.config, self.secret_resolver)
        tenant_id = self.config.gateway.default_tenant_id
        self.connector_registry.register_enabled(self.registry, tenant_id=tenant_id)
        set_connector_registry(self.connector_registry)
        self.scheduler = Scheduler(
            self.config,
            self._cron_dispatch,
            cron_jobs=self._load_cron_jobs(),
            state_path=Path(os.environ["TS_HARNESS_SCHEDULER_STATE"])
            if os.environ.get("TS_HARNESS_SCHEDULER_STATE")
            else None,
            audit_sink=lambda event: self.audit.append(
                tenant_id=self.config.gateway.default_tenant_id,
                action_type=event.get("event", "scheduler_event"),
                actor_id="scheduler",
                payload=event,
            ),
        )
        self.scheduler.start()
        self.slack_socket = SlackSocketListener(self)
        self.slack_socket.start()

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock

    def _slack_socket_health(self) -> tuple[str | None, str | None]:
        health = self.slack_socket.health()
        if health.status == "disabled":
            return None, None
        return health.status, health.detail

    def _build_health_report(self):
        socket_status, socket_detail = self._slack_socket_health()
        fake_provider = os.environ.get("RUNTIME_FAKE_PROVIDER", "false").lower() == "true"
        provider_ok = fake_provider or bool(
            os.environ.get("OPENAI_API_KEY")
            or os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("MODEL_API_KEY")
        )
        channels_ok = bool(
            os.environ.get("SLACK_BOT_TOKEN")
            or os.environ.get("DISCORD_WEBHOOK_URL")
            or os.environ.get("TELEGRAM_BOT_TOKEN")
        )
        channels_detail = None
        if not channels_ok:
            channels_detail = (
                "No Slack, Discord, or Telegram token is configured. "
                "Set SLACK_BOT_TOKEN, DISCORD_WEBHOOK_URL, or TELEGRAM_BOT_TOKEN."
            )
        sandbox_ok = (
            os.environ.get("SANDBOX_ENABLED", "true").lower() != "true"
            or bool(os.environ.get("SANDBOX_WORKER_URL"))
        )
        sandbox_detail = None
        if not sandbox_ok:
            sandbox_detail = "Set SANDBOX_WORKER_URL or set SANDBOX_ENABLED=false."
        gw_ok, gw_detail = self._google_workspace_health()
        return build_health_report(
            provider_ok=provider_ok,
            memory_ok=get_memory_client().health(),
            mcp_ok=self.mcp.health(),
            google_workspace_ok=gw_ok,
            google_workspace_detail=gw_detail,
            channels_ok=channels_ok,
            channels_detail=channels_detail,
            sandbox_ok=sandbox_ok,
            sandbox_detail=sandbox_detail,
            scheduler_ok=self.scheduler.health(),
            slack_socket=socket_status,
            slack_socket_detail=socket_detail,
            stale_jobs=self.scheduler.stale_jobs(),
        )

    def _google_workspace_health(self) -> tuple[bool, str | None]:
        registry = getattr(self, "connector_registry", None)
        if registry is None:
            return True, None
        health = registry.health(tenant_id=self.config.gateway.default_tenant_id)
        if health.status == "skipped":
            return True, None
        if health.status == "ok":
            return True, None
        return False, health.detail

    def _build_shadow_provider(self):
        if os.environ.get("RUNTIME_FAKE_PROVIDER", "false").lower() == "true":
            return FakeProvider(
                [
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": json.dumps(
                                        {
                                            "summary": "Workflow step completed by shadow runtime.",
                                            "artifacts": {},
                                        }
                                    ),
                                }
                            }
                        ]
                    }
                ]
            )
        return None

    def _load_cron_jobs(self) -> list[dict[str, Any]]:
        path = Path(os.environ.get("TS_HARNESS_CRON_CONFIG", "config/runtime-cron-jobs.json"))
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        return list(data.get("jobs") or [])

    def _on_audit(self, payload: dict[str, Any]) -> None:
        self.audit.append(
            tenant_id=str(payload.get("tenant_id", "default")),
            action_type="agent_run_completed",
            actor_id=str(payload.get("agent_id", "system")),
            payload=payload,
            correlation_id=payload.get("correlation_id"),
        )
        self.metrics.inc("agent_runs")

    def _start_sandbox_reaper(self) -> None:
        if os.environ.get("SANDBOX_REAPER_ENABLED", "false").lower() != "true":
            return

        def loop() -> None:
            env = DockerExecutionEnvironment(enabled=True)
            while not self._shutdown.is_set():
                env.reap_orphans(older_than_seconds=3600)
                self._shutdown.wait(300)

        threading.Thread(target=loop, daemon=True, name="ec-sandbox-reaper").start()

    def rebuild_mcp_runtime(self) -> dict[str, Any]:
        skip_mcp = os.environ.get("TS_HARNESS_SKIP_MCP_DISCOVERY", "").lower() == "true"
        self.mcp.close_all()
        servers = getattr(self.mcp, "_servers", None)
        if isinstance(servers, dict):
            servers.clear()
        for name in list(self.registry.snapshot()):
            if str(name).startswith("mcp-"):
                self.registry.deregister(name)
        degraded: list[str] = []
        from ts_harness.config.mcp_settings import resolve_mcp_secret_refs

        for name, server_cfg in dict(self.config.mcp_servers or {}).items():
            if server_cfg.get("enabled", True) is False:
                continue
            resolved = resolve_mcp_secret_refs(server_cfg, getattr(self, "secret_resolver", None))
            self.mcp.register_server(name, resolved)
            if skip_mcp:
                continue
            try:
                self.mcp.discover_tools(name)
            except Exception as exc:  # noqa: BLE001
                degraded.append(str(name))
                state = getattr(self.mcp, "_servers", {}).get(name)
                if state is not None:
                    setattr(state, "healthy", False)
                    config = getattr(state, "config", None)
                    if isinstance(config, dict):
                        config["last_error"] = str(exc)
                continue
            state = None
            if hasattr(self.mcp, "server_state"):
                try:
                    state = self.mcp.server_state(name)
                except Exception:
                    state = None
            if state is None:
                state = getattr(self.mcp, "_servers", {}).get(name)
            if state is not None and getattr(state, "healthy", True) is False:
                degraded.append(str(name))
        if not skip_mcp:
            self.mcp.register_tools(self.registry)
        return {"degraded": degraded}

    def shutdown(self) -> None:
        self._shutdown.set()
        self.slack_socket.stop()
        self.scheduler.stop()
        self.mcp.close_all()
        self.audit.append(
            tenant_id=self.config.gateway.default_tenant_id,
            action_type="runtime_shutdown",
            actor_id="system",
            payload={"status": "stopped"},
        )

    def _cron_dispatch(self, payload: dict[str, Any]) -> None:
        if str(payload.get("kind") or "") == "system":
            from ts_harness.gateway.system_jobs import dispatch_system_job

            result = dispatch_system_job(payload)
            self.audit.append(
                tenant_id=self.config.gateway.default_tenant_id,
                action_type="system_cron_job",
                actor_id="scheduler",
                payload={
                    "handler": payload.get("handler"),
                    "result": result if isinstance(result, dict) else {"status": "ok"},
                },
            )
            self.metrics.inc("system_cron_runs")
            if payload.get("handler") == "start_program" and isinstance(result, dict):
                if result.get("status") == "started":
                    self.metrics.inc("scheduled_program_starts")
                elif result.get("status") == "skipped":
                    self.metrics.inc("scheduled_program_skips")
            return

        agent_id = payload.get("agent_id", self.config.default_agent().id)
        result = self.cron_agent.run_workflow_dispatch(
            tenant_id=self.config.gateway.default_tenant_id,
            agent_id=agent_id,
            payload={"input": {"prompt": payload.get("prompt", "")}, "step_name": "heartbeat"},
            source="cron",
        )
        announce = payload.get("announce")
        if announce:
            self._deliver_announcement(announce, result, payload)
        self.metrics.inc("heartbeat_runs")

    def _deliver_announcement(
        self,
        announce: dict[str, Any],
        result,
        payload: dict[str, Any],
    ) -> None:
        channel = str(announce.get("channel", "slack"))
        target = str(announce.get("to") or announce.get("channel_id") or "")
        if target.startswith("channel:"):
            target = target.split(":", 1)[1]
        text = str(
            result.output.get("summary")
            or result.output.get("message")
            or payload.get("prompt", "")
        )
        if not target:
            return
        send_result = get_channel_router().send(
            channel,
            {"channel": target, "text": text, "thread_id": announce.get("thread_id")},
            tenant_id=self.config.gateway.default_tenant_id,
        )
        self.audit.append(
            tenant_id=self.config.gateway.default_tenant_id,
            action_type="scheduler_announcement",
            actor_id="scheduler",
            payload={
                "channel": channel,
                "target": target,
                "status": send_result.get("status"),
                "error": send_result.get("error"),
            },
        )

    def dispatch_workflow(self, body: dict[str, Any], auth) -> DispatchResult:
        req = normalize_runtime_body(body, default_tenant=self.config.gateway.default_tenant_id)
        task_id = str(body.get("task_id") or "")
        idem_key = f"{req.correlation_id}:{task_id}" if task_id else ""
        if idem_key:
            redis_cached = self._state_store.get_json(f"dispatch:{idem_key}")
            if redis_cached:
                return DispatchResult(**redis_cached)
            cached = self._dispatch_cache.get(idem_key)
            if cached and time.time() - cached[0] < self._dispatch_cache_ttl:
                return cached[1]
        agent_id = body.get("agent_id") or self.config.default_agent().id
        if req.tenant_id not in {tenant.id for tenant in self.config.tenants}:
            raise AuthError("tenant not configured", status=403)
        from ts_harness.gateway.access import require_permission

        require_permission(self, auth, "start_program", resource="workflow_dispatch")
        budget = self.policy.evaluate_budget(tenant_id=req.tenant_id, cost_usd=0.0)
        if budget.decision == "deny":
            raise AuthError(budget.reason, status=429)
        session = get_session_store().get_or_create(
            tenant_id=req.tenant_id,
            source=req.source,
            agent_id=agent_id,
            workflow_id=body.get("workflow_id"),
        )
        skill_hints = canonical_skill_ids(body.get("skill_hints"))
        skills, diagnostics = self.skill_catalog.resolve_for_workflow_dispatch(
            self.config.get_agent(agent_id),
            self.config,
            tenant_id=req.tenant_id,
            fail_on_required=True,
            skill_hints=skill_hints,
        )
        skill_bundle = self.skill_catalog.build_prompt_bundle(skills)
        skill_prompt = skill_bundle["prompt"]
        task_input = body.get("input") if isinstance(body.get("input"), dict) else {}
        stage = str(task_input.get("stage") or "").strip().lower()
        if not stage:
            step_name = str(body.get("step_name") or "")
            stage_by_step = {
                "refine_requirements": "refinery",
                "design_blueprints": "foundry",
                "plan_work_orders": "planner",
            }
            stage = stage_by_step.get(step_name, "")
        if stage and "prd-to-work-orders-governed" in skill_hints:
            skill_prompt = self.skill_catalog.append_governed_workflow_stage_context(
                skill_prompt,
                "prd-to-work-orders-governed",
                stage,
            )
        body = {**body, "_skill_prompt": skill_prompt}
        self.audit.append(
            tenant_id=req.tenant_id,
            action_type="workflow_dispatch",
            actor_id=auth.actor_id,
            correlation_id=req.correlation_id,
            payload={
                "workflow_id": body.get("workflow_id"),
                "task_id": body.get("task_id"),
                "agent_id": agent_id,
                "session_id": session.session_id,
                "skills": skill_bundle["audit"],
                "skill_diagnostics": diagnostics,
            },
        )
        started = time.time()
        result = self.agent.run_workflow_dispatch(
            tenant_id=req.tenant_id,
            agent_id=agent_id,
            payload=body,
            source=req.source,
            correlation_id=req.correlation_id,
        )
        self.metrics.observe("agent_run_latency_ms", (time.time() - started) * 1000)
        dispatch_result = DispatchResult(
            status=result.status,
            output=result.output,
            runtime_task_id=result.runtime_task_id,
            proposed_tasks=result.proposed_tasks,
            error=result.error,
        )
        if idem_key:
            self._dispatch_cache[idem_key] = (time.time(), dispatch_result)
            self._state_store.set_json(
                f"dispatch:{idem_key}",
                dispatch_result.to_dict(),
                ttl_seconds=self._dispatch_cache_ttl,
            )
        return dispatch_result

    def handle_workflow_present_artifact(self, body: dict[str, Any], auth) -> dict[str, Any]:
        from ts_harness.gateway.workflow_presentation import present_workflow_artifact

        req = normalize_runtime_body(body, default_tenant=self.config.gateway.default_tenant_id)
        workflow_id = str(body.get("workflow_id") or "")
        artifact = body.get("artifact")
        if not workflow_id:
            return {"status": "error", "error": "workflow_id is required"}
        if not isinstance(artifact, dict) or not artifact:
            return {"status": "error", "error": "artifact is required"}

        result = present_workflow_artifact(
            workflow_id=workflow_id,
            workflow_type=str(body.get("workflow_type") or ""),
            tenant_id=req.tenant_id,
            step_name=str(body.get("step_name") or ""),
            agent_id=str(body.get("agent_id") or ""),
            present_prompt=str(body.get("present_prompt") or "inform"),
            artifact=artifact,
            source_channel=str(body.get("source_channel") or ""),
            actor_id=auth.actor_id,
            runtime_config=self.config,
            share_with_email=str(body.get("share_with_email") or body.get("actor_email") or ""),
            requester_actor_id=str(body.get("requester_actor_id") or auth.actor_id or ""),
            program_id=str(body.get("program_id") or ""),
            program_summary=str(body.get("program_summary") or ""),
        )
        self.audit.append(
            tenant_id=req.tenant_id,
            action_type="workflow_artifact_presented",
            actor_id=auth.actor_id,
            correlation_id=req.correlation_id,
            payload={
                "workflow_id": workflow_id,
                "workflow_type": body.get("workflow_type"),
                "step_name": body.get("step_name"),
                "agent_id": body.get("agent_id"),
                "present_prompt": body.get("present_prompt"),
                "status": result.get("status"),
                "error": result.get("error"),
                "session_id": result.get("session_id"),
            },
        )
        return result

    def _session_for_message(
        self,
        body: dict[str, Any],
        *,
        tenant_id: str,
        channel: str,
        agent_id: str,
    ):
        """Route a chat post to the open session when the client names one.

        Admin chat keeps a single browser binding and also sends ``session_id``
        for the thread on screen. The binding can still point at an older
        conversation, so the named session wins.
        """
        store = get_session_store()
        requested = str(body.get("session_id") or "").strip()
        if requested:
            existing = store.get(requested)
            if existing is not None and existing.tenant_id == tenant_id:
                return existing
        return store.get_or_create(
            tenant_id=tenant_id,
            source=channel,
            agent_id=agent_id,
            channel=channel,
            thread_id=body.get("thread_id"),
        )

    def handle_session_message(self, body: dict[str, Any], auth) -> dict[str, Any]:
        req = normalize_runtime_body(body, default_tenant=self.config.gateway.default_tenant_id)
        channel = body.get("channel", "slack")
        agent_id = self.config.bindings.get(channel, self.config.default_agent().id)
        agent_cfg = self.config.get_agent(agent_id)
        session = self._session_for_message(
            body,
            tenant_id=req.tenant_id,
            channel=channel,
            agent_id=agent_id,
        )
        with self._session_lock(session.session_id):
            return self._handle_session_message_locked(
                body,
                auth,
                req=req,
                session=session,
                channel=channel,
                agent_id=agent_id,
                agent_cfg=agent_cfg,
            )

    def _handle_session_message_locked(
        self,
        body: dict[str, Any],
        auth,
        *,
        req,
        session,
        channel: str,
        agent_id: str,
        agent_cfg,
    ) -> dict[str, Any]:
        from ts_harness.agent.transcript import get_transcript_store
        from ts_harness.gateway.delegate_confirmation import (
            apply_confirmation_to_session,
            apply_continue_delivery_confirmation_to_session,
            apply_program_advance_confirmation_to_session,
            apply_program_confirmation_to_session,
            apply_workflow_confirmation_to_session,
            resolve_continue_delivery_confirmation,
            resolve_delegate_confirmation,
            resolve_program_advance_confirmation,
            resolve_program_confirmation,
            resolve_workflow_confirmation,
        )
        from ts_harness.routing.intents import classify_channel_intent

        metadata = dict(session.metadata or {})
        metadata["channel"] = channel
        # An open chat posts its own session id. A stale browser binding must
        # not retarget that chat onto a different thread.
        if not str(metadata.get("channel_id") or "").strip():
            metadata["channel_id"] = body.get("channel_id") or channel
        if not str(metadata.get("thread_id") or "").strip():
            incoming_thread = str(body.get("thread_id") or "").strip()
            if incoming_thread:
                metadata["thread_id"] = incoming_thread
        if not str(metadata.get("binding_key") or "").strip():
            binding_key = str(body.get("binding_key") or "").strip()
            if binding_key:
                metadata["binding_key"] = binding_key
        session.metadata = metadata
        get_session_store().save(session)

        user_text = str(body.get("text") or "")
        actor_email = body.get("sender_email")
        if not actor_email and channel == "slack":
            from ts_harness.gateway.channels.slack_users import resolve_slack_user_email

            raw_user = str((body.get("actor") or {}).get("actor_id") or auth.actor_id or "")
            user_id = raw_user.removeprefix("slack:")
            actor_email = resolve_slack_user_email(user_id, config=self.config)

        if actor_email:
            metadata = dict(session.metadata or {})
            if metadata.get("actor_email") != actor_email:
                metadata["actor_email"] = actor_email
                session.metadata = metadata
                get_session_store().save(session)

        if channel == "slack" and getattr(self.config.identity, "provider", "static") == "oidc":
            from ts_harness.gateway.directory_links import link_channel_session

            linked = link_channel_session(dict(session.metadata or {}), actor_email, auth.tenant_id)
            if linked != (session.metadata or {}):
                session.metadata = linked
                get_session_store().save(session)

        raw_user = str((body.get("actor") or {}).get("actor_id") or auth.actor_id or "")
        slack_user_id = raw_user.removeprefix("slack:")
        if slack_user_id.startswith(("U", "W")) and len(slack_user_id) >= 8:
            metadata = dict(session.metadata or {})
            if metadata.get("slack_user_id") != slack_user_id:
                metadata["slack_user_id"] = slack_user_id
                session.metadata = metadata
                get_session_store().save(session)

        confirm_kwargs = {
            "channel": channel,
            "auth": auth,
            "actor_email": actor_email,
            "config": self.config,
        }
        confirmation = resolve_delegate_confirmation(session, user_text)
        apply_confirmation_to_session(session, confirmation)
        workflow_confirmation = resolve_workflow_confirmation(session, user_text, **confirm_kwargs)
        apply_workflow_confirmation_to_session(session, workflow_confirmation)
        program_confirmation = resolve_program_confirmation(session, user_text, **confirm_kwargs)
        apply_program_confirmation_to_session(session, program_confirmation)
        program_advance_confirmation = resolve_program_advance_confirmation(session, user_text, **confirm_kwargs)
        apply_program_advance_confirmation_to_session(session, program_advance_confirmation)
        continue_confirmation = resolve_continue_delivery_confirmation(session, user_text, **confirm_kwargs)
        apply_continue_delivery_confirmation_to_session(session, continue_confirmation)
        session = get_session_store().get(session.session_id) or session

        from ts_harness.gateway.channel_confirmations import (
            execute_confirmed_bridge,
            execute_confirmed_program_advance,
            execute_confirmed_program_start,
            execute_confirmed_tool_approval,
            execute_confirmed_workflow_start,
            execute_workflow_tool_approval_reply,
            format_workflow_tool_approval_ack,
            should_intercept_workflow_tool_approval_reply,
            execute_stuck_step_command,
            format_stuck_step_ack,
            parse_stuck_step_command,
            execute_bridge_workflow_proposal,
            execute_governed_workflow_proposal,
            execute_intake_response,
            execute_workflow_gate_response,
            execute_workflow_artifact_query,
            execute_workflow_artifact_provenance_query,
            execute_workflow_status_query,
            format_approval_gate_clarification,
            format_bridge_proposal_message,
            format_bridge_started_message,
            format_intake_response_ack,
            format_program_advanced_message,
            format_program_started_message,
            format_tool_approval_ack,
            format_workflow_gate_response_ack,
            format_workflow_artifact_message,
            format_workflow_artifact_provenance_message,
            format_auto_action_failure_message,
            format_program_proposal_message,
            format_workflow_proposal_message,
            format_workflow_started_message,
            format_workflow_status_message,
            should_auto_propose_program,
            should_auto_propose_workflow,
            execute_governed_program_proposal,
            should_auto_handle_intake_response,
            should_intercept_approval_gate_reply,
            should_auto_report_workflow_status,
            should_auto_show_workflow_artifact,
            should_auto_explain_workflow_artifact,
            should_intercept_tool_approval_reply,
        )

        denied_reply = next(
            (
                item.denied
                for item in (
                    confirmation,
                    workflow_confirmation,
                    program_confirmation,
                    program_advance_confirmation,
                    continue_confirmation,
                )
                if getattr(item, "denied", "")
            ),
            "",
        )
        if denied_reply:
            transcript = get_transcript_store()
            transcript.append_turn(
                session.session_id,
                user_text=user_text,
                assistant_messages=[{"role": "assistant", "content": denied_reply}],
            )
            self.audit.append(
                tenant_id=req.tenant_id,
                action_type="permission_denied",
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={"session_id": session.session_id, "channel": channel, "error": denied_reply},
            )
            return {
                "status": "denied",
                "session_id": session.session_id,
                "output": {"summary": denied_reply},
            }

        auto_action_error: str | None = None
        effective_workflow_confirmed = workflow_confirmation.confirmed
        effective_program_confirmed = program_confirmation.confirmed
        effective_program_advance_confirmed = program_advance_confirmation.confirmed
        effective_bridge_confirmed = continue_confirmation.confirmed
        if should_intercept_tool_approval_reply(session.metadata or {}, user_text):
            try:
                tool_result = execute_confirmed_tool_approval(
                    session,
                    tenant_id=req.tenant_id,
                    agent_id=agent_id,
                    source=channel,
                    user_text=user_text,
                    actor_id=auth.actor_id,
                    registry=self.registry,
                    config=self.config,
                    actor_email=actor_email,
                    channel=channel,
                    auth=auth,
                )
            except Exception as exc:  # noqa: BLE001
                tool_result = {"error": str(exc)}
            summary = format_tool_approval_ack(tool_result)
            transcript = get_transcript_store()
            transcript.append_turn(
                session.session_id,
                user_text=user_text,
                assistant_messages=[{"role": "assistant", "content": summary}],
            )
            self.audit.append(
                tenant_id=req.tenant_id,
                action_type="tool_approval_resumed",
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={
                    "session_id": session.session_id,
                    "channel": channel,
                    "tool_name": tool_result.get("tool_name"),
                    "status": tool_result.get("status"),
                    "error": tool_result.get("error"),
                    "actor_email": actor_email,
                },
            )
            return {
                "status": "succeeded",
                "session_id": session.session_id,
                "output": {**tool_result, "summary": summary},
            }
        if should_intercept_workflow_tool_approval_reply(session.metadata or {}, user_text):
            try:
                decision_result = execute_workflow_tool_approval_reply(
                    session,
                    user_text=user_text,
                    actor_id=auth.actor_id,
                    channel=channel,
                    auth=auth,
                    actor_email=actor_email,
                    config=self.config,
                )
            except Exception as exc:  # noqa: BLE001
                decision_result = {"error": str(exc)}
            summary = format_workflow_tool_approval_ack(decision_result)
            get_transcript_store().append_turn(
                session.session_id,
                user_text=user_text,
                assistant_messages=[{"role": "assistant", "content": summary}],
            )
            self.audit.append(
                tenant_id=req.tenant_id,
                action_type="workflow_tool_approval_decided",
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={
                    "session_id": session.session_id,
                    "workflow_id": (session.metadata or {}).get("workflow_id"),
                    "channel": channel,
                    "approval_id": decision_result.get("approval_id"),
                    "tool_name": decision_result.get("tool_name"),
                    "status": decision_result.get("status"),
                    "resume": decision_result.get("resume"),
                    "error": decision_result.get("error"),
                    "actor_email": actor_email,
                },
            )
            return {
                "status": "succeeded",
                "session_id": session.session_id,
                "output": {**decision_result, "summary": summary},
            }
        if parse_stuck_step_command(user_text) is not None:
            try:
                stuck_result = execute_stuck_step_command(
                    session,
                    user_text=user_text,
                    actor_id=auth.actor_id,
                    channel=channel,
                    auth=auth,
                    actor_email=actor_email,
                    config=self.config,
                )
            except Exception as exc:  # noqa: BLE001
                stuck_result = {"error": str(exc)}
            summary = format_stuck_step_ack(stuck_result)
            get_transcript_store().append_turn(
                session.session_id,
                user_text=user_text,
                assistant_messages=[{"role": "assistant", "content": summary}],
            )
            self.audit.append(
                tenant_id=req.tenant_id,
                action_type="stuck_step_decided",
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={
                    "session_id": session.session_id,
                    "channel": channel,
                    "action": stuck_result.get("action"),
                    "work_order": stuck_result.get("work_order"),
                    "status": stuck_result.get("status"),
                    "error": stuck_result.get("error"),
                    "actor_email": actor_email,
                },
            )
            return {
                "status": "succeeded",
                "session_id": session.session_id,
                "output": {**stuck_result, "summary": summary},
            }
        if workflow_confirmation.confirmed:
            try:
                start_result = execute_confirmed_workflow_start(
                    session,
                    tenant_id=req.tenant_id,
                    agent_id=agent_id,
                    source=channel,
                )
            except Exception as exc:  # noqa: BLE001
                auto_action_error = str(exc)
                effective_workflow_confirmed = False
                start_result = None
            if start_result:
                if start_result.get("error"):
                    auto_action_error = str(start_result["error"])
                    effective_workflow_confirmed = False
                elif start_result.get("workflow_id") or start_result.get("status") == "started":
                    session = get_session_store().get(session.session_id) or session
                    summary = format_workflow_started_message(start_result)
                    transcript = get_transcript_store()
                    transcript.append_turn(
                        session.session_id,
                        user_text=user_text,
                        assistant_messages=[{"role": "assistant", "content": summary}],
                    )
                    self.audit.append(
                        tenant_id=req.tenant_id,
                        action_type="workflow_started",
                        actor_id=auth.actor_id,
                        correlation_id=req.correlation_id,
                        payload={
                            "session_id": session.session_id,
                            "workflow_id": start_result.get("workflow_id"),
                            "workflow_type": start_result.get("workflow_type"),
                            "channel": channel,
                            "auto_confirmed": True,
                        },
                    )
                    return {
                        "status": "succeeded",
                        "session_id": session.session_id,
                        "output": {**start_result, "summary": summary},
                    }
            elif not auto_action_error:
                auto_action_error = "no pending workflow found for this session"
                effective_workflow_confirmed = False
        elif program_confirmation.confirmed:
            try:
                program_result = execute_confirmed_program_start(
                    session,
                    tenant_id=req.tenant_id,
                    agent_id=agent_id,
                    source=channel,
                    actor_email=actor_email,
                )
            except Exception as exc:  # noqa: BLE001
                auto_action_error = str(exc)
                effective_program_confirmed = False
                program_result = None
            if program_result:
                if program_result.get("error"):
                    auto_action_error = str(program_result["error"])
                    effective_program_confirmed = False
                elif program_result.get("workflow_id") or program_result.get("status") == "started":
                    session = get_session_store().get(session.session_id) or session
                    summary = format_program_started_message(program_result)
                    transcript = get_transcript_store()
                    transcript.append_turn(
                        session.session_id,
                        user_text=user_text,
                        assistant_messages=[{"role": "assistant", "content": summary}],
                    )
                    self.audit.append(
                        tenant_id=req.tenant_id,
                        action_type="program_started",
                        actor_id=auth.actor_id,
                        correlation_id=req.correlation_id,
                        payload={
                            "session_id": session.session_id,
                            "program_id": program_result.get("program_id"),
                            "workflow_id": program_result.get("workflow_id"),
                            "entry_phase": program_result.get("entry_phase"),
                            "channel": channel,
                            "auto_confirmed": True,
                        },
                    )
                    return {
                        "status": "succeeded",
                        "session_id": session.session_id,
                        "output": {**program_result, "summary": summary},
                    }
            elif not auto_action_error:
                auto_action_error = "no pending delivery program found for this session"
                effective_program_confirmed = False
        elif should_auto_handle_intake_response(session.metadata or {}, user_text):
            try:
                intake_result = execute_intake_response(
                    session,
                    tenant_id=req.tenant_id,
                    agent_id=agent_id,
                    source=channel,
                    user_text=user_text,
                )
            except Exception as exc:  # noqa: BLE001
                intake_result = {"error": str(exc)}
            summary = format_intake_response_ack(intake_result)
            transcript = get_transcript_store()
            transcript.append_turn(
                session.session_id,
                user_text=user_text,
                assistant_messages=[{"role": "assistant", "content": summary}],
            )
            self.audit.append(
                tenant_id=req.tenant_id,
                action_type="workflow_intake_resumed",
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={
                    "session_id": session.session_id,
                    "workflow_id": (session.metadata or {}).get("workflow_id"),
                    "channel": channel,
                    "error": intake_result.get("error"),
                    "task_id": intake_result.get("task_id"),
                },
            )
            return {
                "status": "succeeded",
                "session_id": session.session_id,
                "output": {**intake_result, "summary": summary},
            }
        elif should_intercept_approval_gate_reply(session.metadata or {}, user_text):
            from ts_harness.agent.provider import build_provider

            provider = self.agent._provider_override or build_provider(agent_cfg.model)
            try:
                gate_result = execute_workflow_gate_response(
                    session,
                    tenant_id=req.tenant_id,
                    agent_id=agent_id,
                    source=channel,
                    user_text=user_text,
                    provider=provider,
                    use_llm_fallback=True,
                )
            except Exception as exc:  # noqa: BLE001
                gate_result = {"error": str(exc)}
            if gate_result.get("status") == "none":
                gate_result = {
                    "status": "ambiguous",
                    "summary": format_approval_gate_clarification(),
                }
            summary = format_workflow_gate_response_ack(
                gate_result,
                workflow_type=str((session.metadata or {}).get("workflow_type") or ""),
            )
            transcript = get_transcript_store()
            transcript.append_turn(
                session.session_id,
                user_text=user_text,
                assistant_messages=[{"role": "assistant", "content": summary}],
            )
            if gate_result.get("status") == "rejected":
                action_type = "workflow_task_rejected"
            elif gate_result.get("status") == "approved":
                action_type = "workflow_task_approved"
            elif gate_result.get("status") == "ambiguous":
                action_type = "workflow_gate_clarification"
            else:
                action_type = "workflow_gate_response"
            self.audit.append(
                tenant_id=req.tenant_id,
                action_type=action_type,
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={
                    "session_id": session.session_id,
                    "workflow_id": (session.metadata or {}).get("workflow_id"),
                    "channel": channel,
                    "status": gate_result.get("status"),
                    "task_id": gate_result.get("task_id"),
                    "error": gate_result.get("error"),
                    "gate_clarification": gate_result.get("status") == "ambiguous",
                },
            )
            return {
                "status": "succeeded",
                "session_id": session.session_id,
                "output": {**gate_result, "summary": summary},
            }
        elif program_advance_confirmation.confirmed:
            try:
                advance_result = execute_confirmed_program_advance(
                    session,
                    tenant_id=req.tenant_id,
                    agent_id=agent_id,
                    source=channel,
                )
            except Exception as exc:  # noqa: BLE001
                auto_action_error = str(exc)
                effective_program_advance_confirmed = False
                advance_result = None
            if advance_result:
                if advance_result.get("error"):
                    auto_action_error = str(advance_result["error"])
                    effective_program_advance_confirmed = False
                elif advance_result.get("workflow_id") or advance_result.get("status") == "started":
                    session = get_session_store().get(session.session_id) or session
                    summary = format_program_advanced_message(advance_result)
                    transcript = get_transcript_store()
                    transcript.append_turn(
                        session.session_id,
                        user_text=user_text,
                        assistant_messages=[{"role": "assistant", "content": summary}],
                    )
                    self.audit.append(
                        tenant_id=req.tenant_id,
                        action_type="program_advanced",
                        actor_id=auth.actor_id,
                        correlation_id=req.correlation_id,
                        payload={
                            "session_id": session.session_id,
                            "program_id": advance_result.get("program_id"),
                            "workflow_id": advance_result.get("workflow_id"),
                            "target_phase": advance_result.get("target_phase"),
                            "channel": channel,
                            "auto_confirmed": True,
                        },
                    )
                    return {
                        "status": "succeeded",
                        "session_id": session.session_id,
                        "output": {**advance_result, "summary": summary},
                    }
            elif not auto_action_error:
                auto_action_error = "no pending program advance found for this session"
                effective_program_advance_confirmed = False
        elif continue_confirmation.confirmed:
            try:
                bridge_result = execute_confirmed_bridge(
                    session,
                    tenant_id=req.tenant_id,
                    agent_id=agent_id,
                    source=channel,
                )
            except Exception as exc:  # noqa: BLE001
                auto_action_error = str(exc)
                effective_bridge_confirmed = False
                bridge_result = None
            if bridge_result:
                if bridge_result.get("error"):
                    auto_action_error = str(bridge_result["error"])
                    effective_bridge_confirmed = False
                elif bridge_result.get("workflow_id") or bridge_result.get(
                    "status"
                ) in {"started", "bridge_started"}:
                    session = get_session_store().get(session.session_id) or session
                    summary = format_bridge_started_message(bridge_result)
                    transcript = get_transcript_store()
                    transcript.append_turn(
                        session.session_id,
                        user_text=user_text,
                        assistant_messages=[{"role": "assistant", "content": summary}],
                    )
                    self.audit.append(
                        tenant_id=req.tenant_id,
                        action_type="workflow_bridged",
                        actor_id=auth.actor_id,
                        correlation_id=req.correlation_id,
                        payload={
                            "session_id": session.session_id,
                            "workflow_id": bridge_result.get("workflow_id"),
                            "workflow_type": bridge_result.get("workflow_type"),
                            "channel": channel,
                            "auto_confirmed": True,
                        },
                    )
                    return {
                        "status": "succeeded",
                        "session_id": session.session_id,
                        "output": {**bridge_result, "summary": summary},
                    }
            elif not auto_action_error:
                auto_action_error = "no pending bridge workflow found for this session"
                effective_bridge_confirmed = False

        if auto_action_error and (
            workflow_confirmation.confirmed
            or program_confirmation.confirmed
            or program_advance_confirmation.confirmed
            or continue_confirmation.confirmed
        ):
            pending = (session.metadata or {}).get("pending_workflow") or {}
            pending_program = (session.metadata or {}).get("pending_program") or {}
            pending_advance = (session.metadata or {}).get("pending_program_advance") or {}
            pending_bridge = (session.metadata or {}).get("pending_bridge") or (
                session.metadata or {}
            ).get("pending_continue_delivery") or {}
            if workflow_confirmation.confirmed:
                workflow_label = str(
                    pending.get("workflow_type")
                    or (session.metadata or {}).get("workflow_type")
                    or "workflow"
                ).replace("_", " ")
                action_label = f"start the {workflow_label} workflow"
                audit_action = "workflow_start_failed"
                audit_payload = {
                    "workflow_type": pending.get("workflow_type"),
                    "auto_confirmed": True,
                }
            elif program_confirmation.confirmed:
                entry_phase = str(pending_program.get("entry_phase") or "delivery").replace("_", " ")
                action_label = f"start the delivery program at the {entry_phase} phase"
                audit_action = "program_start_failed"
                audit_payload = {"entry_phase": pending_program.get("entry_phase"), "auto_confirmed": True}
            elif program_advance_confirmation.confirmed:
                target_phase = str(pending_advance.get("target_phase") or "next").replace("_", " ")
                action_label = f"advance the delivery program to the {target_phase} phase"
                audit_action = "program_advance_failed"
                audit_payload = {
                    "target_phase": pending_advance.get("target_phase"),
                    "auto_confirmed": True,
                }
            else:
                workflow_label = str(
                    pending_bridge.get("target_workflow_type")
                    or pending_bridge.get("workflow_type")
                    or "downstream"
                ).replace("_", " ")
                action_label = f"start the {workflow_label} workflow"
                audit_action = "workflow_bridge_failed"
                audit_payload = {
                    "workflow_type": pending_bridge.get("target_workflow_type")
                    or pending_bridge.get("workflow_type"),
                    "auto_confirmed": True,
                }
            summary = format_auto_action_failure_message(
                action_label=action_label,
                error_reason=auto_action_error,
            )
            transcript = get_transcript_store()
            transcript.append_turn(
                session.session_id,
                user_text=user_text,
                assistant_messages=[{"role": "assistant", "content": summary}],
            )
            self.audit.append(
                tenant_id=req.tenant_id,
                action_type=audit_action,
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={
                    "session_id": session.session_id,
                    "channel": channel,
                    "error": auto_action_error,
                    **audit_payload,
                },
            )
            return {
                "status": "succeeded",
                "session_id": session.session_id,
                "output": {"summary": summary, "error": auto_action_error},
            }

        from ts_harness.workflows.workflow_session import has_session_tracking, sync_workflow_session_phase
        from ts_harness.workflows.materialize import load_skeleton
        from ts_harness.workflows.session_compat import read_pending_bridge, read_workflow_phase
        from ts_harness.routing.matcher import find_routing_rule

        metadata = session.metadata or {}
        workflow_type = str(metadata.get("workflow_type") or "")
        if workflow_type and metadata.get("workflow_id"):
            try:
                skeleton = load_skeleton(workflow_type)
                if has_session_tracking(skeleton):
                    sync_workflow_session_phase(session, skeleton, tenant_id=req.tenant_id)
                    session = get_session_store().get(session.session_id) or session
            except FileNotFoundError:
                pass

        metadata = session.metadata or {}
        intent = classify_channel_intent(user_text, metadata, config=self.config)
        matched_rule = find_routing_rule(self.config, intent.rule_id or intent.kind)
        orchestrator_hint = matched_rule.orchestrator_hint if matched_rule else None
        metadata = dict(metadata)
        metadata["last_intent"] = intent.kind
        session.metadata = metadata
        get_session_store().save(session)

        if should_auto_propose_program(intent, session.metadata or {}):
            try:
                proposal = execute_governed_program_proposal(
                    session,
                    tenant_id=req.tenant_id,
                    agent_id=agent_id,
                    source=channel,
                    program_type=str(intent.program_type),
                    entry_phase=intent.entry_phase,
                    summary=str(intent.summary or user_text).strip()[:240],
                )
            except Exception as exc:  # noqa: BLE001
                proposal = {"error": str(exc)}
            if proposal.get("status") == "awaiting_confirmation":
                session = get_session_store().get(session.session_id) or session
                summary = format_program_proposal_message(proposal)
                transcript = get_transcript_store()
                transcript.append_turn(
                    session.session_id,
                    user_text=user_text,
                    assistant_messages=[{"role": "assistant", "content": summary}],
                )
                self.audit.append(
                    tenant_id=req.tenant_id,
                    action_type="program_proposed",
                    actor_id=auth.actor_id,
                    correlation_id=req.correlation_id,
                    payload={
                        "session_id": session.session_id,
                        "program_type": proposal.get("program_type"),
                        "entry_phase": proposal.get("entry_phase"),
                        "channel": channel,
                        "auto_proposed": True,
                    },
                )
                return {
                    "status": "succeeded",
                    "session_id": session.session_id,
                    "output": {**proposal, "summary": summary},
                }

        if should_auto_propose_workflow(intent, session.metadata or {}):
            try:
                proposal = execute_governed_workflow_proposal(
                    session,
                    tenant_id=req.tenant_id,
                    agent_id=agent_id,
                    source=channel,
                    workflow_type=str(intent.workflow_type),
                    summary=str(intent.summary or user_text).strip()[:240],
                )
            except Exception as exc:  # noqa: BLE001
                proposal = {"error": str(exc)}
            if proposal.get("status") == "awaiting_confirmation":
                session = get_session_store().get(session.session_id) or session
                summary = format_workflow_proposal_message(proposal)
                transcript = get_transcript_store()
                transcript.append_turn(
                    session.session_id,
                    user_text=user_text,
                    assistant_messages=[{"role": "assistant", "content": summary}],
                )
                self.audit.append(
                    tenant_id=req.tenant_id,
                    action_type="workflow_proposed",
                    actor_id=auth.actor_id,
                    correlation_id=req.correlation_id,
                    payload={
                        "session_id": session.session_id,
                        "workflow_type": proposal.get("workflow_type"),
                        "channel": channel,
                        "auto_proposed": True,
                    },
                )
                return {
                    "status": "succeeded",
                    "session_id": session.session_id,
                    "output": {**proposal, "summary": summary},
                }
            if proposal.get("error"):
                workflow_label = str(intent.workflow_type or "workflow").replace("_", " ")
                error_reason = str(proposal["error"])
                summary = (
                    f"I could not start the {workflow_label} workflow: "
                    f"{error_reason}. Please try again."
                )
                transcript = get_transcript_store()
                transcript.append_turn(
                    session.session_id,
                    user_text=user_text,
                    assistant_messages=[{"role": "assistant", "content": summary}],
                )
                self.audit.append(
                    tenant_id=req.tenant_id,
                    action_type="workflow_propose_failed",
                    actor_id=auth.actor_id,
                    correlation_id=req.correlation_id,
                    payload={
                        "session_id": session.session_id,
                        "workflow_type": intent.workflow_type,
                        "channel": channel,
                        "error": error_reason,
                        "auto_proposed": True,
                    },
                )
                return {
                    "status": "succeeded",
                    "session_id": session.session_id,
                    "output": {"summary": summary, "error": error_reason},
                }

        if intent.kind == "continue_bridge":
            try:
                proposal = execute_bridge_workflow_proposal(
                    session,
                    tenant_id=req.tenant_id,
                    agent_id=agent_id,
                    source=channel,
                    summary=str(intent.summary or user_text).strip()[:240],
                )
            except Exception as exc:  # noqa: BLE001
                proposal = {"error": str(exc)}
            if proposal.get("status") == "awaiting_confirmation":
                session = get_session_store().get(session.session_id) or session
                summary = format_bridge_proposal_message(proposal)
                transcript = get_transcript_store()
                transcript.append_turn(
                    session.session_id,
                    user_text=user_text,
                    assistant_messages=[{"role": "assistant", "content": summary}],
                )
                self.audit.append(
                    tenant_id=req.tenant_id,
                    action_type="workflow_bridge_proposed",
                    actor_id=auth.actor_id,
                    correlation_id=req.correlation_id,
                    payload={
                        "session_id": session.session_id,
                        "workflow_type": proposal.get("target_workflow_type"),
                        "channel": channel,
                        "auto_proposed": True,
                    },
                )
                return {
                    "status": "succeeded",
                    "session_id": session.session_id,
                    "output": {**proposal, "summary": summary},
                }
            if proposal.get("error"):
                error_reason = str(proposal["error"])
                summary = format_auto_action_failure_message(
                    action_label="prepare the downstream workflow",
                    error_reason=error_reason,
                )
                transcript = get_transcript_store()
                transcript.append_turn(
                    session.session_id,
                    user_text=user_text,
                    assistant_messages=[{"role": "assistant", "content": summary}],
                )
                return {
                    "status": "succeeded",
                    "session_id": session.session_id,
                    "output": {"summary": summary, "error": error_reason},
                }

        if should_auto_explain_workflow_artifact(session.metadata or {}, user_text):
            provenance_result = execute_workflow_artifact_provenance_query(session)
            summary = str(
                provenance_result.get("summary")
                or format_workflow_artifact_provenance_message(session.metadata or {})
            )
            transcript = get_transcript_store()
            transcript.append_turn(
                session.session_id,
                user_text=user_text,
                assistant_messages=[{"role": "assistant", "content": summary}],
            )
            self.audit.append(
                tenant_id=req.tenant_id,
                action_type="workflow_artifact_provenance_reported",
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={
                    "session_id": session.session_id,
                    "workflow_id": (session.metadata or {}).get("workflow_id"),
                    "step_name": provenance_result.get("step_name"),
                    "agent_id": provenance_result.get("agent_id"),
                    "channel": channel,
                    "auto_reported": True,
                    "error": provenance_result.get("error"),
                },
            )
            return {
                "status": "succeeded",
                "session_id": session.session_id,
                "output": {**provenance_result, "summary": summary},
            }

        if should_auto_report_workflow_status(session.metadata or {}, user_text):
            try:
                status_result = execute_workflow_status_query(
                    session,
                    tenant_id=req.tenant_id,
                    agent_id=agent_id,
                    source=channel,
                )
            except Exception as exc:  # noqa: BLE001
                status_result = {"error": str(exc)}
            summary = format_workflow_status_message(status_result)
            transcript = get_transcript_store()
            transcript.append_turn(
                session.session_id,
                user_text=user_text,
                assistant_messages=[{"role": "assistant", "content": summary}],
            )
            self.audit.append(
                tenant_id=req.tenant_id,
                action_type="workflow_status_reported",
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={
                    "session_id": session.session_id,
                    "workflow_id": (session.metadata or {}).get("workflow_id"),
                    "workflow_type": status_result.get("workflow_type"),
                    "channel": channel,
                    "auto_reported": True,
                    "error": status_result.get("error"),
                },
            )
            return {
                "status": "succeeded",
                "session_id": session.session_id,
                "output": {**status_result, "summary": summary},
            }

        if should_auto_show_workflow_artifact(session.metadata or {}, user_text):
            try:
                artifact_result = execute_workflow_artifact_query(
                    session,
                    tenant_id=req.tenant_id,
                    agent_id=agent_id,
                    source=channel,
                )
            except Exception as exc:  # noqa: BLE001
                artifact_result = {"error": str(exc)}
            summary = format_workflow_artifact_message(
                artifact_result,
                runtime_config=self.config,
                session=session,
            )
            transcript = get_transcript_store()
            transcript.append_turn(
                session.session_id,
                user_text=user_text,
                assistant_messages=[{"role": "assistant", "content": summary}],
            )
            self.audit.append(
                tenant_id=req.tenant_id,
                action_type="workflow_artifact_reported",
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={
                    "session_id": session.session_id,
                    "workflow_id": (session.metadata or {}).get("workflow_id"),
                    "workflow_type": artifact_result.get("workflow_type"),
                    "artifact_step": artifact_result.get("artifact_step"),
                    "channel": channel,
                    "auto_reported": True,
                    "error": artifact_result.get("error"),
                },
            )
            return {
                "status": "succeeded",
                "session_id": session.session_id,
                "output": {**artifact_result, "summary": summary},
            }

        from ts_harness.skills.learn import (
            LEARN_SYSTEM_INSTRUCTION,
            blocked_learn_payload,
            parse_learn,
            select_authoring_agent_id,
        )

        learned = parse_learn(user_text)
        if learned.is_learn:
            if not learned.source:
                self.audit.append(
                    tenant_id=req.tenant_id,
                    action_type="skill_learn_blocked",
                    actor_id=auth.actor_id,
                    correlation_id=req.correlation_id,
                    payload={"error": "skill_learn_missing_source"},
                )
                return blocked_learn_payload(
                    session_id=session.session_id,
                    error="skill_learn_missing_source",
                    summary="Need a source after /learn (URL, path, or notes).",
                )
            authoring_id = select_authoring_agent_id(
                agent_cfg,
                self.config,
                registry_tool_names=set(self.registry.snapshot()),
            )
            if not authoring_id:
                self.audit.append(
                    tenant_id=req.tenant_id,
                    action_type="skill_learn_blocked",
                    actor_id=auth.actor_id,
                    correlation_id=req.correlation_id,
                    payload={"error": "skill_learn_authoring_unavailable"},
                )
                return blocked_learn_payload(
                    session_id=session.session_id,
                    error="skill_learn_authoring_unavailable",
                    summary="Cannot learn a skill: no authoring agent with propose_skill is available.",
                )
            self.audit.append(
                tenant_id=req.tenant_id,
                action_type="skill_learn_started",
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={
                    "source": learned.source,
                    "session_agent_id": agent_id,
                    "authoring_agent_id": authoring_id,
                },
            )
            transcript = get_transcript_store()
            history = transcript.load_messages(session.session_id)
            started = time.time()
            result = self.agent.run_channel_chat(
                tenant_id=req.tenant_id,
                agent_id=authoring_id,
                text=user_text,
                session_id=session.session_id,
                history=history,
                skill_prompt="",
                source=channel,
                correlation_id=req.correlation_id,
                system_context=LEARN_SYSTEM_INSTRUCTION,
                actor_email=actor_email,
            )
            self.metrics.observe("agent_run_latency_ms", (time.time() - started) * 1000)

            def _learn_tool_preview(record: dict[str, Any]) -> dict[str, Any]:
                preview = record.get("result_preview") or ""
                if isinstance(preview, dict):
                    return preview
                if isinstance(preview, str) and preview.strip():
                    try:
                        candidate = json.loads(preview)
                    except json.JSONDecodeError:
                        return {}
                    if isinstance(candidate, dict):
                        return candidate
                return {}

            propose_seen = False
            proposed_skill_id: Any = None
            blocked_error: str | None = None
            blocked_summary: str | None = None
            for record in result.tool_calls:
                if record.get("tool") != "propose_skill":
                    continue
                propose_seen = True
                parsed_preview = _learn_tool_preview(record)
                preview_error = str(
                    parsed_preview.get("error")
                    or parsed_preview.get("reason")
                    or ""
                ).strip()
                if record.get("decision") != "allow":
                    blocked_error = preview_error or "propose_skill denied"
                    blocked_summary = blocked_error
                    continue
                error_code = str(parsed_preview.get("error_code") or "").strip()
                if error_code == "skill_learn_use_revision":
                    self.audit.append(
                        tenant_id=req.tenant_id,
                        action_type="skill_learn_blocked",
                        actor_id=auth.actor_id,
                        correlation_id=req.correlation_id,
                        payload={
                            "error": "skill_learn_use_revision",
                            "source": learned.source,
                        },
                    )
                    return blocked_learn_payload(
                        session_id=session.session_id,
                        error="skill_learn_use_revision",
                        summary=(
                            "Cannot use /learn to overwrite a live skill. "
                            "Ask to revise it instead."
                        ),
                    )
                if parsed_preview.get("status") == "draft":
                    proposed_skill_id = parsed_preview.get("skill_id")
                    continue
                if preview_error:
                    blocked_error = preview_error
                    blocked_summary = preview_error

            if proposed_skill_id is None:
                if propose_seen and blocked_error:
                    error = blocked_error
                    summary = blocked_summary or blocked_error
                else:
                    error = "skill_learn_no_draft"
                    summary = (
                        "Learn did not produce a skill draft. The authoring turn "
                        "must call propose_skill."
                    )
                self.audit.append(
                    tenant_id=req.tenant_id,
                    action_type="skill_learn_blocked",
                    actor_id=auth.actor_id,
                    correlation_id=req.correlation_id,
                    payload={"error": error, "source": learned.source},
                )
                return blocked_learn_payload(
                    session_id=session.session_id,
                    error=error,
                    summary=summary,
                )

            self.audit.append(
                tenant_id=req.tenant_id,
                action_type="skill_proposed",
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={"skill_id": proposed_skill_id, "revision_of": None},
            )

            initial_message_count = 1 + len(history) + (1 if LEARN_SYSTEM_INSTRUCTION else 0) + 1
            new_messages = (
                result.messages[initial_message_count:]
                if len(result.messages) > initial_message_count
                else []
            )
            if new_messages:
                transcript.append_turn(
                    session.session_id,
                    user_text=user_text,
                    assistant_messages=new_messages,
                )
            elif result.status == "succeeded":
                transcript.append_turn(
                    session.session_id,
                    user_text=user_text,
                    assistant_messages=[
                        {"role": "assistant", "content": str((result.output or {}).get("summary") or "")},
                    ],
                )
            return {
                "status": result.status,
                "session_id": session.session_id,
                "output": result.output,
                "error": result.error,
            }

        from ts_harness.skills.slash_invoke import (
            blocked_unresolved_payload,
            catalog_index_ids,
            parse_slash_invoke,
            resolve_slash_pins,
        )

        parsed = parse_slash_invoke(
            user_text,
            catalog_ids=catalog_index_ids(self.skill_catalog),
        )

        from ts_harness.skills.nudge import (
            REVISE_SYSTEM_INSTRUCTION,
            parse_revise_intent,
        )
        from ts_harness.skills.authoring import is_live_skill_meta

        revise_id = None
        authoring_id = None
        if not parsed.pins:
            live_ids = {
                meta.id
                for meta in self.skill_catalog.index.values()
                if is_live_skill_meta(meta)
            }
            revise_id = parse_revise_intent(user_text, live_ids)
            if revise_id:
                authoring_id = select_authoring_agent_id(
                    agent_cfg,
                    self.config,
                    registry_tool_names=set(self.registry.snapshot()),
                )
        if revise_id and not authoring_id:
            self.audit.append(
                tenant_id=req.tenant_id,
                action_type="skill_learn_blocked",
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={
                    "error": "skill_learn_authoring_unavailable",
                    "revision_of": revise_id,
                },
            )
            return blocked_learn_payload(
                session_id=session.session_id,
                error="skill_learn_authoring_unavailable",
                summary=(
                    "Cannot revise a skill: no authoring agent with "
                    "propose_skill is available."
                ),
            )
        if revise_id and authoring_id and authoring_id != agent_id:
            self.audit.append(
                tenant_id=req.tenant_id,
                action_type="skill_revise_started",
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={
                    "revision_of": revise_id,
                    "session_agent_id": agent_id,
                    "authoring_agent_id": authoring_id,
                },
            )
            transcript = get_transcript_store()
            history = transcript.load_messages(session.session_id)
            started = time.time()
            result = self.agent.run_channel_chat(
                tenant_id=req.tenant_id,
                agent_id=authoring_id,
                text=user_text,
                session_id=session.session_id,
                history=history,
                skill_prompt="",
                source=channel,
                correlation_id=req.correlation_id,
                system_context=REVISE_SYSTEM_INSTRUCTION,
                actor_email=actor_email,
            )
            self.metrics.observe(
                "agent_run_latency_ms",
                (time.time() - started) * 1000,
            )

            def _revise_tool_preview(record: dict[str, Any]) -> dict[str, Any]:
                preview = record.get("result_preview") or ""
                if isinstance(preview, dict):
                    return preview
                if isinstance(preview, str) and preview.strip():
                    try:
                        candidate = json.loads(preview)
                    except json.JSONDecodeError:
                        return {}
                    if isinstance(candidate, dict):
                        return candidate
                return {}

            propose_seen = False
            proposed_skill_id: Any = None
            proposed_revision_of: Any = None
            blocked_error: str | None = None
            blocked_summary: str | None = None
            for record in result.tool_calls:
                if record.get("tool") != "propose_skill":
                    continue
                propose_seen = True
                parsed_preview = _revise_tool_preview(record)
                preview_error = str(
                    parsed_preview.get("error")
                    or parsed_preview.get("reason")
                    or ""
                ).strip()
                if record.get("decision") != "allow":
                    blocked_error = preview_error or "propose_skill denied"
                    blocked_summary = blocked_error
                    continue
                if (
                    parsed_preview.get("status") == "draft"
                    and parsed_preview.get("skill_id") == revise_id
                    and parsed_preview.get("revision_of") == revise_id
                ):
                    proposed_skill_id = parsed_preview.get("skill_id")
                    proposed_revision_of = parsed_preview.get("revision_of")
                    continue
                if parsed_preview.get("status") == "draft":
                    blocked_error = "skill_learn_no_draft"
                    blocked_summary = (
                        "Revise did not produce the requested skill revision draft."
                    )
                    continue
                if preview_error:
                    blocked_error = preview_error
                    blocked_summary = preview_error

            if proposed_skill_id is None:
                if propose_seen and blocked_error:
                    error = blocked_error
                    summary = blocked_summary or blocked_error
                else:
                    error = "skill_learn_no_draft"
                    summary = (
                        "Revise did not produce a skill draft. The authoring turn "
                        "must call propose_skill with revision_of."
                    )
                self.audit.append(
                    tenant_id=req.tenant_id,
                    action_type="skill_learn_blocked",
                    actor_id=auth.actor_id,
                    correlation_id=req.correlation_id,
                    payload={"error": error, "revision_of": revise_id},
                )
                return blocked_learn_payload(
                    session_id=session.session_id,
                    error=error,
                    summary=summary,
                )

            self.audit.append(
                tenant_id=req.tenant_id,
                action_type="skill_proposed",
                actor_id=auth.actor_id,
                correlation_id=req.correlation_id,
                payload={
                    "skill_id": proposed_skill_id,
                    "revision_of": proposed_revision_of or revise_id,
                },
            )

            initial_message_count = (
                1
                + len(history)
                + (1 if REVISE_SYSTEM_INSTRUCTION else 0)
                + 1
            )
            new_messages = (
                result.messages[initial_message_count:]
                if len(result.messages) > initial_message_count
                else []
            )
            if new_messages:
                transcript.append_turn(
                    session.session_id,
                    user_text=user_text,
                    assistant_messages=new_messages,
                )
            elif result.status == "succeeded":
                transcript.append_turn(
                    session.session_id,
                    user_text=user_text,
                    assistant_messages=[
                        {
                            "role": "assistant",
                            "content": str(
                                (result.output or {}).get("summary") or ""
                            ),
                        },
                    ],
                )
            return {
                "status": result.status,
                "session_id": session.session_id,
                "output": result.output,
                "error": result.error,
            }

        model_text = user_text
        if parsed.pins:
            resolved = resolve_slash_pins(
                self.skill_catalog,
                agent_cfg,
                self.config,
                tenant_id=req.tenant_id,
                pins=parsed.pins,
            )
            if resolved.unresolved:
                self.audit.append(
                    tenant_id=req.tenant_id,
                    action_type="channel_chat",
                    actor_id=auth.actor_id,
                    correlation_id=req.correlation_id,
                    payload={
                        "channel": channel,
                        "agent_id": agent_id,
                        "session_id": session.session_id,
                        "skill_pins": parsed.pins,
                        "skill_invoke": "blocked",
                    },
                )
                return blocked_unresolved_payload(
                    session_id=session.session_id,
                    unresolved=resolved.unresolved,
                )
            skills, diagnostics = resolved.skills, resolved.diagnostics
            model_text = parsed.remainder
            skill_invoke = "replaced"
        else:
            skills, diagnostics = self.skill_catalog.resolve_for_channel_chat(
                agent_cfg,
                self.config,
                tenant_id=req.tenant_id,
            )
            skill_invoke = "none"
        skill_bundle = self.skill_catalog.build_prompt_bundle(skills)
        transcript = get_transcript_store()
        history = transcript.load_messages(session.session_id)
        linked_workflow_id = (session.metadata or {}).get("workflow_id")
        workflow_phase = read_workflow_phase(session.metadata or {})
        pending_bridge = read_pending_bridge(session.metadata or {})
        bridge_available = False
        if workflow_type:
            try:
                bridge_available = bool(getattr(load_skeleton(workflow_type), "bridge", None)) and workflow_phase == "approved"
            except FileNotFoundError:
                bridge_available = False
        system_context = _build_channel_system_context(
            session,
            delegation_confirmed=confirmation.confirmed,
            workflow_confirmed=effective_workflow_confirmed,
            program_confirmed=effective_program_confirmed,
            program_advance_confirmed=effective_program_advance_confirmed,
            continue_delivery_confirmed=effective_bridge_confirmed,
            bridge_confirmed=effective_bridge_confirmed,
            channel_intent=intent.kind,
            orchestrator_hint=orchestrator_hint,
        )
        if auto_action_error:
            failure_context = (
                "[system context] Automatic workflow action failed: "
                f"{auto_action_error}. Explain the issue clearly to the user."
            )
            system_context = f"{system_context}\n{failure_context}".strip() if system_context else failure_context
        if revise_id and authoring_id == agent_id:
            system_context = (
                f"{REVISE_SYSTEM_INSTRUCTION}\n{system_context}".strip()
                if system_context
                else REVISE_SYSTEM_INSTRUCTION
            )

        self.audit.append(
            tenant_id=req.tenant_id,
            action_type="channel_chat",
            actor_id=auth.actor_id,
            correlation_id=req.correlation_id,
            payload={
                "channel": channel,
                "agent_id": agent_id,
                "session_id": session.session_id,
                "skills": skill_bundle["audit"],
                "skill_diagnostics": diagnostics,
                "delegation_confirmed": confirmation.confirmed,
                "workflow_confirmed": workflow_confirmation.confirmed,
                "program_confirmed": program_confirmation.confirmed,
                "program_advance_confirmed": program_advance_confirmation.confirmed,
                "continue_delivery_confirmed": continue_confirmation.confirmed,
                "channel_intent": intent.kind,
                "workflow_linked": bool(linked_workflow_id),
                "actor_email": actor_email,
                "skill_pins": parsed.pins,
                "skill_invoke": skill_invoke,
            },
        )

        started = time.time()
        initial_message_count = 1 + len(history) + (1 if system_context else 0) + 1
        result = self.agent.run_channel_chat(
            tenant_id=req.tenant_id,
            agent_id=agent_id,
            text=model_text,
            session_id=session.session_id,
            history=history,
            skill_prompt=skill_bundle["prompt"],
            source=channel,
            correlation_id=req.correlation_id,
            delegation_confirmed=confirmation.confirmed,
            pending_delegation=(session.metadata or {}).get("pending_delegate"),
            workflow_confirmed=effective_workflow_confirmed,
            pending_workflow=(session.metadata or {}).get("pending_workflow"),
            program_confirmed=effective_program_confirmed,
            pending_program=(session.metadata or {}).get("pending_program"),
            program_advance_confirmed=effective_program_advance_confirmed,
            pending_program_advance=(session.metadata or {}).get("pending_program_advance"),
            workflow_linked=bool(linked_workflow_id),
            linked_workflow_id=str(linked_workflow_id) if linked_workflow_id else None,
            continue_delivery_confirmed=effective_bridge_confirmed,
            pending_continue_delivery=pending_bridge,
            bridge_confirmed=effective_bridge_confirmed,
            pending_bridge=pending_bridge,
            prd_phase=workflow_phase,
            workflow_phase=workflow_phase,
            channel_intent=intent.kind,
            informal_prd_opt_out=intent.kind == "informal_prd",
            session_mode=(session.metadata or {}).get("mode"),
            bridge_available=bridge_available,
            system_context=system_context,
            actor_email=actor_email,
        )
        self.metrics.observe("agent_run_latency_ms", (time.time() - started) * 1000)

        if revise_id and authoring_id == agent_id:
            for record in result.tool_calls:
                if record.get("tool") != "propose_skill":
                    continue
                preview = record.get("result_preview") or ""
                parsed_preview: dict[str, Any] = {}
                if isinstance(preview, dict):
                    parsed_preview = preview
                elif isinstance(preview, str) and preview.strip():
                    try:
                        candidate = json.loads(preview)
                    except json.JSONDecodeError:
                        candidate = {}
                    if isinstance(candidate, dict):
                        parsed_preview = candidate
                if (
                    record.get("decision") == "allow"
                    and parsed_preview.get("status") == "draft"
                    and parsed_preview.get("skill_id") == revise_id
                    and parsed_preview.get("revision_of") == revise_id
                ):
                    self.audit.append(
                        tenant_id=req.tenant_id,
                        action_type="skill_proposed",
                        actor_id=auth.actor_id,
                        correlation_id=req.correlation_id,
                        payload={
                            "skill_id": parsed_preview.get("skill_id"),
                            "revision_of": parsed_preview.get("revision_of"),
                        },
                    )
                    break

        from ts_harness.skills.nudge import (
            NUDGE_TEXT,
            apply_nudge_to_summary,
            should_nudge,
        )

        if should_nudge(
            status=result.status,
            tool_calls=result.tool_calls,
            already_sent=bool(
                (session.metadata or {}).get("skill_nudge_sent")
            ),
        ):
            output = dict(result.output or {})
            current_summary = str(output.get("summary") or "")
            nudged_summary = apply_nudge_to_summary(current_summary)
            if nudged_summary != current_summary:
                output["summary"] = nudged_summary
                result.output = output
                session.metadata = dict(session.metadata or {})
                session.metadata["skill_nudge_sent"] = True
                get_session_store().save(session)
                self.audit.append(
                    tenant_id=req.tenant_id,
                    action_type="skill_nudge_sent",
                    actor_id=auth.actor_id,
                    correlation_id=req.correlation_id,
                    payload={
                        "session_id": session.session_id,
                        "tool_call_count": len(result.tool_calls),
                    },
                )

        new_messages = (
            result.messages[initial_message_count:]
            if len(result.messages) > initial_message_count
            else []
        )
        if new_messages:
            nudged_summary = str((result.output or {}).get("summary") or "")
            if NUDGE_TEXT in nudged_summary:
                for idx in range(len(new_messages) - 1, -1, -1):
                    if new_messages[idx].get("role") != "assistant":
                        continue
                    content = str(new_messages[idx].get("content") or "")
                    new_messages = list(new_messages)
                    new_messages[idx] = {
                        **new_messages[idx],
                        "content": apply_nudge_to_summary(content),
                    }
                    break
            transcript.append_turn(
                session.session_id,
                user_text=user_text,
                assistant_messages=new_messages,
            )
        elif result.status == "succeeded":
            transcript.append_turn(
                session.session_id,
                user_text=user_text,
                assistant_messages=[
                    {"role": "assistant", "content": str((result.output or {}).get("summary") or "")},
                ],
            )

        return {
            "status": result.status,
            "session_id": session.session_id,
            "output": result.output,
            "error": result.error,
        }

    def handle_channel_webhook(self, channel: str, body: dict[str, Any], auth) -> dict[str, Any]:
        if channel == "slack" and body.get("type") == "url_verification":
            return {"status": "challenge", "challenge": body.get("challenge", "")}
        event = get_channel_router().normalize(channel, body, tenant_id=body.get("tenant_id") or self.config.gateway.default_tenant_id)
        sender_email = event.sender_email
        if not sender_email and channel == "slack":
            from ts_harness.gateway.channels.slack_users import resolve_slack_user_email

            sender_email = resolve_slack_user_email(event.sender_id, config=self.config)
        message_body = {
            "tenant_id": event.tenant_id,
            "source": channel,
            "channel": channel,
            "channel_id": event.metadata.get("channel_id"),
            "text": event.text,
            "thread_id": event.thread_id,
            "sender_email": sender_email,
            "actor": {"actor_id": event.sender_id, "auth_provider": channel},
        }
        result = self.handle_session_message(message_body, auth)
        result["channel"] = channel
        if result.get("status") == "blocked" and result.get("error") == "max iterations exceeded":
            reply_text = (
                "I hit an internal step limit while handling that message. "
                "Please try again in a new thread."
            )
        else:
            reply_text = str(
                (result.get("output") or {}).get("summary")
                or result.get("error")
                or "TS Harness processed your message."
            )
        target_channel = str(event.metadata.get("channel_id") or event.metadata.get("channel") or "")
        if target_channel:
            send_result = get_channel_router().send(
                channel,
                {"channel": target_channel, "text": reply_text, "thread_id": event.thread_id},
                tenant_id=event.tenant_id,
            )
            result["reply"] = send_result
            self.audit.append(
                tenant_id=event.tenant_id,
                action_type="channel_reply",
                actor_id=event.sender_id,
                payload={"channel": channel, "target": target_channel, "status": send_result.get("status")},
            )
        return result


_RUNTIME: RuntimeState | None = None
_RUNTIME_LOCK = threading.Lock()


def get_runtime() -> RuntimeState:
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            config_path = os.environ.get(
                "TS_HARNESS_CONFIG",
                "config/ts-harness-runtime.yaml.tmpl",
            )
            _RUNTIME = RuntimeState(config_path)
            _register_group_resolvers(_RUNTIME.config)
        return _RUNTIME


def _register_group_resolvers(config: Any) -> None:
    try:
        from ts_harness.identity_directory import register_group_resolvers

        register_group_resolvers(config)
    except Exception as exc:  # noqa: BLE001 - sign-in falls back to token groups
        log_event("group_resolver_registration_failed", error=str(exc))


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "TSHarness/0.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        self._last_raw_body = raw
        return json.loads(raw or b"{}")

    def _send(self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _authenticate(self, body: dict[str, Any]):
        from ts_harness.gateway.browser_session import enforce_cookie_origin, read_session_cookie

        runtime = get_runtime()
        headers = {str(key).lower(): value for key, value in self.headers.items()}
        used_cookie = False
        if not headers.get("authorization"):
            cookie = read_session_cookie(headers)
            if cookie:
                headers["authorization"] = f"Bearer {cookie}"
                used_cookie = True
        identity_provider = None
        if getattr(runtime.config.identity, "provider", "static") == "oidc":
            from ts_harness.gateway.identity import build_identity_provider

            identity_provider = build_identity_provider(runtime.config.identity)
        auth = authenticate_request(
            headers,
            expected_token=runtime.config.gateway.token,
            body=body,
            single_tenant_mode=runtime.config.gateway.single_tenant_mode,
            default_tenant_id=runtime.config.gateway.default_tenant_id,
            identity_provider=identity_provider,
        )
        if used_cookie and self.command in {"POST", "PUT", "PATCH", "DELETE"}:
            enforce_cookie_origin(
                headers,
                allowed_origins=list(runtime.config.gateway.allowed_origins or []),
                scheme="http",
                host=headers.get("host", ""),
            )
        if isinstance(body, dict):
            body["tenant_id"] = auth.tenant_id
        return auth

    def _authenticate_channel(self, channel: str, body: dict[str, Any]) -> AuthContext:
        tenant_id = str(body.get("tenant_id") or get_runtime().config.gateway.default_tenant_id)
        if channel == "slack" and os.environ.get("SLACK_SIGNING_SECRET"):
            timestamp = self.headers.get("X-Slack-Request-Timestamp", "")
            signature = self.headers.get("X-Slack-Signature", "")
            if not timestamp or abs(time.time() - int(timestamp)) > 60 * 5:
                raise AuthError("invalid slack timestamp", status=401)
            base = b"v0:" + timestamp.encode() + b":" + getattr(self, "_last_raw_body", b"")
            expected = "v0=" + hmac.new(os.environ["SLACK_SIGNING_SECRET"].encode(), base, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                raise AuthError("invalid slack signature", status=401)
            return AuthContext(actor_id="slack:webhook", tenant_id=tenant_id, auth_provider="slack")
        if channel == "telegram" and os.environ.get("TELEGRAM_WEBHOOK_SECRET"):
            if self.headers.get("X-Telegram-Bot-Api-Secret-Token") != os.environ["TELEGRAM_WEBHOOK_SECRET"]:
                raise AuthError("invalid telegram webhook secret", status=401)
            return AuthContext(actor_id="telegram:webhook", tenant_id=tenant_id, auth_provider="telegram")
        if channel == "discord" and os.environ.get("DISCORD_WEBHOOK_SECRET"):
            if self.headers.get("X-Discord-Webhook-Secret") != os.environ["DISCORD_WEBHOOK_SECRET"]:
                raise AuthError("invalid discord webhook secret", status=401)
            return AuthContext(actor_id="discord:webhook", tenant_id=tenant_id, auth_provider="discord")
        return self._authenticate(body)

    def _check_origin(self) -> None:
        runtime = get_runtime()
        check_allowed_origin(dict(self.headers), runtime.config.gateway.allowed_origins)

    def _read_approval_store(self) -> list[dict[str, Any]]:
        store_path = os.environ.get("TS_HARNESS_APPROVAL_STORE")
        if not store_path:
            return []
        try:
            lines = Path(store_path).read_text(encoding="utf-8").splitlines()
            records = []
            for line in lines:
                if line.strip():
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            return records
        except FileNotFoundError:
            return []

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._check_origin()
        except AuthError as exc:
            self._send(exc.status, {"error": str(exc)})
            return
        if self.path == "/healthz":
            runtime = get_runtime()
            report = runtime._build_health_report()
            payload = report.to_dict()
            payload["metrics"] = runtime.metrics.snapshot()
            self._send(200, payload)
            return
        if self.path.startswith("/skills/doctor"):
            try:
                self._authenticate({})
            except AuthError as exc:
                self._send(exc.status, {"error": str(exc)})
                return
            from urllib.parse import parse_qs, urlparse

            runtime = get_runtime()
            query = parse_qs(urlparse(self.path).query)
            agent_id = (query.get("agent_id") or ["orchestrator"])[0]
            tenant_id = (query.get("tenant_id") or [runtime.config.gateway.default_tenant_id])[0]
            lines = runtime.skill_catalog.doctor(
                runtime.config.get_agent(str(agent_id)),
                runtime.config,
                tenant_id=str(tenant_id),
            )
            self._send(200, {"agent_id": agent_id, "tenant_id": tenant_id, "diagnostics": lines})
            return
        if self.path == "/mcp/tools":
            try:
                self._authenticate({})
            except AuthError as exc:
                self._send(exc.status, {"error": str(exc)})
                return
            runtime = get_runtime()
            tools = []
            for server_name in runtime.config.mcp_servers:
                tools.extend(runtime.mcp.list_tools(server_name))
            self._send(200, {"tools": tools})
            return
        if (
            self.path.startswith("/admin/sessions/")
            and self.path.endswith("/messages")
        ):
            try:
                auth = self._authenticate({})
            except AuthError as exc:
                self._send(exc.status, {"error": str(exc)})
                return
            from ts_harness.gateway.admin_bff import session_messages_admin

            session_id = self.path.removeprefix(
                "/admin/sessions/",
            ).removesuffix("/messages")
            try:
                self._send(200, session_messages_admin(session_id, auth))
            except KeyError:
                self._send(404, {"error": "not found"})
            return
        if self.path == "/memory/smoke":
            try:
                self._authenticate({})
            except AuthError as exc:
                self._send(exc.status, {"error": str(exc)})
                return
            self._send(200, {"ok": get_memory_client().health()})
            return
        if self.path == "/admin/approvals":
            try:
                self._authenticate({})
            except AuthError as exc:
                self._send(exc.status, {"error": str(exc)})
                return
            approvals = self._read_approval_store()
            self._send(200, {"approvals": approvals})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._check_origin()
            body = self._read_json()
            if self.path == "/admin/auth/session":
                from ts_harness.gateway.browser_session import open_browser_session

                status, payload, cookie = open_browser_session(
                    {str(key).lower(): value for key, value in self.headers.items()},
                    body,
                    runtime=get_runtime(),
                )
                extra = {"Set-Cookie": cookie} if cookie else None
                self._send(status, payload, extra)
                return
            if self.path == "/admin/auth/logout":
                from ts_harness.gateway.browser_session import close_browser_session

                status, payload, cookie = close_browser_session(
                    {str(key).lower(): value for key, value in self.headers.items()}
                )
                self._send(status, payload, {"Set-Cookie": cookie})
                return
            if self.path.startswith("/channels/"):
                if self.path.endswith("/send-test"):
                    auth = self._authenticate(body)
                else:
                    channel = self.path.split("/")[2]
                    auth = self._authenticate_channel(channel, body)
            elif self.path == "/webhooks/github" and os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip():
                # Signature verified later; skip bearer auth for GitHub delivery.
                auth = AuthContext(
                    actor_id="github:webhook",
                    tenant_id=str(body.get("tenant_id") or get_runtime().config.gateway.default_tenant_id),
                    auth_provider="github",
                )
            else:
                auth = self._authenticate(body)
        except AuthError as exc:
            get_runtime().audit.append(
                tenant_id="unknown",
                action_type="auth_failure",
                actor_id="unknown",
                payload={"error": str(exc)},
            )
            self._send(exc.status, {"error": str(exc)})
            return
        except json.JSONDecodeError:
            self._send(400, {"error": "invalid json"})
            return

        runtime = get_runtime()
        try:
            if self.path == "/admin/secrets/reload":
                from ts_harness.gateway.access import require_permission

                require_permission(runtime, auth, "edit_config", resource="secrets_reload")
                secret_name = str(body.get("name", ""))
                tenant_id = str(body.get("tenant_id") or runtime.config.gateway.default_tenant_id)
                runtime.secret_resolver.reload(secret_name, tenant_id=tenant_id)
                runtime.audit.append(
                    tenant_id=tenant_id,
                    action_type="secret_reload",
                    actor_id=auth.actor_id,
                    payload={"name": secret_name},
                )
                self._send(200, {"status": "reloaded", "name": secret_name})
                return
            if self.path == "/workflow/dispatch":
                result = runtime.dispatch_workflow(body, auth)
                runtime.metrics.inc("workflow_dispatch")
                self._send(200, result.to_dict())
                return
            if self.path == "/workflow/present-artifact":
                from ts_harness.gateway.access import require_permission

                require_permission(runtime, auth, "start_program", resource="workflow_present_artifact")
                result = runtime.handle_workflow_present_artifact(body, auth)
                runtime.metrics.inc("workflow_present_artifact")
                self._send(200, result)
                return
            if self.path == "/workflow/cancel":
                workflow_id = str(body.get("workflow_id") or "")
                task_id = str(body.get("task_id") or "")
                reason = str(body.get("reason") or "cancelled")
                if not workflow_id and not task_id:
                    self._send(400, {"error": "workflow_id or task_id is required"})
                    return
                mem = get_memory_client()
                cancel_body: dict[str, Any] = {"reason": reason, "actor": auth.actor_id}
                if task_id:
                    cancel_body["id"] = task_id
                if workflow_id:
                    cancel_body["workflow_id"] = workflow_id
                try:
                    result_cancel = mem._request("POST", "/workflow/cancel", cancel_body)
                except Exception as exc:
                    result_cancel = {"status": "error", "error": str(exc)}
                runtime.audit.append(
                    tenant_id=str(body.get("tenant_id") or runtime.config.gateway.default_tenant_id),
                    action_type="workflow_cancel",
                    actor_id=auth.actor_id,
                    payload={"workflow_id": workflow_id, "task_id": task_id, "reason": reason},
                )
                runtime.metrics.inc("workflow_cancel")
                self._send(200, result_cancel)
                return
            if self.path == "/session/message":
                result = runtime.handle_session_message(body, auth)
                runtime.metrics.inc("session_message")
                self._send(200, result)
                return
            if self.path == "/cron/trigger":
                from ts_harness.gateway.access import require_permission

                require_permission(runtime, auth, "edit_config", resource="cron_trigger")
                runtime.scheduler.run_due_once(force=True)
                self._send(200, {"status": "ok", "scheduler": "triggered"})
                return
            if self.path == "/sandbox/deny-check":
                allowed, reason = scan_command(str(body.get("command", "")))
                self._send(200, {"denied": not allowed, "reason": reason})
                return
            if self.path == "/webhooks/github":
                from ts_harness.gateway.github_webhook import (
                    delivery_already_handled,
                    github_webhook_secret,
                    handle_github_webhook,
                    remember_delivery,
                    verify_github_signature,
                )

                secret = github_webhook_secret()
                delivery_id = str(self.headers.get("X-GitHub-Delivery", "") or "").strip()
                if secret:
                    signature = self.headers.get("X-Hub-Signature-256", "")
                    raw = getattr(self, "_last_raw_body", b"")
                    if not verify_github_signature(raw, signature, secret):
                        self._send(401, {"error": "invalid github signature"})
                        return
                    if not delivery_id:
                        self._send(400, {"error": "missing X-GitHub-Delivery"})
                        return
                    if delivery_already_handled(delivery_id):
                        self._send(200, {"status": "duplicate", "delivery_id": delivery_id})
                        return
                result = handle_github_webhook(
                    body,
                    tenant_id=str(body.get("tenant_id") or runtime.config.gateway.default_tenant_id),
                )
                runtime.audit.append(
                    tenant_id=str(body.get("tenant_id") or runtime.config.gateway.default_tenant_id),
                    action_type="github_webhook",
                    actor_id="github-webhook",
                    payload={"status": result.get("status"), "pr_url": result.get("pr_url")},
                )
                runtime.metrics.inc("github_webhook")
                if delivery_id:
                    remember_delivery(delivery_id)
                self._send(200, result)
                return
            if self.path.startswith("/channels/"):
                if self.path.endswith("/send-test"):
                    channel = self.path.split("/")[-2]
                    result = get_channel_router().send(
                        channel,
                        {
                            "channel": body.get("channel") or body.get("to") or "",
                            "text": body.get("text", "TS Harness release gate"),
                            "thread_id": body.get("thread_id"),
                        },
                        tenant_id=body.get("tenant_id") or runtime.config.gateway.default_tenant_id,
                    )
                    self._send(200, result)
                    return
                result = runtime.handle_channel_webhook(channel, body, auth)
                self._send(200, result)
                return
            self._send(404, {"error": "not found"})
        except AuthError as exc:
            self._send(exc.status, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            log_event("request_error", path=self.path, error=str(exc))
            if self.path == "/workflow/dispatch":
                self._send(200, DispatchResult(status="blocked", error=str(exc)).to_dict())
            else:
                self._send(500, {"error": str(exc)})


def main() -> None:
    host = os.environ.get("AGENT_RUNTIME_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENT_RUNTIME_PORT", "18789"))
    token = os.environ.get("GATEWAY_TOKEN", "dev-token")
    config_path = os.environ.get("TS_HARNESS_CONFIG", "config/ts-harness-runtime.yaml.tmpl")
    # Ensure token is available for config validation during boot.
    os.environ.setdefault("GATEWAY_TOKEN", token)
    runtime = get_runtime()
    if not runtime.config.gateway.token:
        runtime.config.gateway.token = token

    use_asgi = os.environ.get("TS_HARNESS_ASGI", "true").lower() != "false"

    if use_asgi:
        import uvicorn
        from ts_harness.gateway.asgi_app import app  # noqa: PLC0415

        def _handle_signal_asgi(signum, _frame) -> None:
            log_event("runtime_shutdown_signal", signal=signum)
            runtime.shutdown()

        def _reload_secrets_on_sighup_asgi(signum, _frame) -> None:
            log_event("runtime_secrets_reload_signal", signal=signum)
            runtime.secret_resolver.reload_all()

        signal.signal(signal.SIGTERM, _handle_signal_asgi)
        signal.signal(signal.SIGINT, _handle_signal_asgi)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _reload_secrets_on_sighup_asgi)

        log_event("runtime_started", host=host, port=port, config=config_path, server="asgi")
        uvicorn.run(app, host=host, port=port)
        return

    server = ThreadingHTTPServer((host, port), GatewayHandler)

    def _handle_signal(signum, _frame) -> None:
        log_event("runtime_shutdown_signal", signal=signum)
        runtime.shutdown()
        server.shutdown()

    def _reload_secrets_on_sighup(signum, _frame) -> None:
        log_event("runtime_secrets_reload_signal", signal=signum)
        runtime.secret_resolver.reload_all()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _reload_secrets_on_sighup)

    log_event("runtime_started", host=host, port=port, config=config_path, server="threaded")
    server.serve_forever()


if __name__ == "__main__":
    main()
