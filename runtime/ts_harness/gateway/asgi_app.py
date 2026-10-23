"""Starlette ASGI application for the TS Harness gateway."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route

from ts_harness.config.deployment_profile import current_profile
from ts_harness.gateway.program_access import ProgramAccessDenied
from ts_harness.gateway.auth import AuthContext, AuthError, authenticate_request, check_allowed_origin
from ts_harness.gateway.channels.router import get_channel_router
from ts_harness.gateway.contracts import DispatchResult
from ts_harness.gateway.google_oauth import oauth_google_callback, oauth_google_start
from ts_harness.gateway.request_timeout import resolve_request_timeout
from ts_harness.gateway.server import get_runtime
from ts_harness.memory.client import get_memory_client
from ts_harness.observability.logging import log_event
from ts_harness.tools.approvals import (
    ApprovalAlreadyDecided,
    decide_and_resume,
    get_approval_queue,
    scan_command,
)
from ts_harness.workflows.definition_store import ConflictError
from ts_harness.workflows.delivery_program import get_delivery_program_service
from ts_harness.workflows.engine import get_workflow_engine


def get_program_definition_registry() -> Any:
    """Return the process-wide program definition registry when available."""
    try:
        from ts_harness.workflows.program_registry import (  # type: ignore[import-not-found]
            get_program_definition_registry as _get,
        )
    except ImportError as exc:
        raise RuntimeError("program definition registry unavailable") from exc
    return _get()


# ── Middleware ────────────────────────────────────────────────────────────────


class _TimeoutMiddleware(BaseHTTPMiddleware):
    """Abort requests that exceed the configured wall-clock timeout."""

    def __init__(
        self,
        app: Any,
        timeout: float | None = None,
        dispatch_timeout: float | None = None,
    ) -> None:
        super().__init__(app)
        self.timeout = float(
            os.environ.get("TS_HARNESS_REQUEST_TIMEOUT", "300")
            if timeout is None
            else timeout
        )
        self.dispatch_timeout = float(
            os.environ.get("TS_HARNESS_DISPATCH_REQUEST_TIMEOUT", "7200")
            if dispatch_timeout is None
            else dispatch_timeout
        )

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        timeout = resolve_request_timeout(
            request.method,
            request.url.path,
            default_timeout=self.timeout,
            dispatch_timeout=self.dispatch_timeout,
        )
        try:
            return await asyncio.wait_for(call_next(request), timeout=timeout)
        except asyncio.TimeoutError:
            return JSONResponse({"error": "request timeout"}, status_code=504)


# ── Auth helpers ──────────────────────────────────────────────────────────────


def _hdrs(request: Request) -> dict[str, str]:
    return dict(request.headers)


def _check_origin(request: Request, *, require_origin: bool | None = None) -> None:
    runtime = get_runtime()
    mutating = request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    # Opt-in: browsers send Origin on console mutations; curl/scripts often omit it.
    enforce = os.environ.get("TS_HARNESS_REQUIRE_ADMIN_ORIGIN", "").lower() in {
        "1",
        "true",
        "yes",
    }
    check_allowed_origin(
        _hdrs(request),
        runtime.config.gateway.allowed_origins,
        require_origin=(enforce and mutating) if require_origin is None else require_origin,
    )


class _TraceMiddleware(BaseHTTPMiddleware):
    """Bind one trace id for the request and return it as X-Trace-Id."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        from ts_harness.observability.logging import (
            new_trace_id,
            parse_traceparent,
            server_span,
            trace_id_var,
        )

        trace_id = parse_traceparent(request.headers.get("traceparent", "")) or new_trace_id()
        token = trace_id_var.set(trace_id)
        try:
            with server_span(f"{request.method} {request.url.path}"):
                response = await call_next(request)
            response.headers["X-Trace-Id"] = trace_id
            return response
        finally:
            trace_id_var.reset(token)


class _AdminGuardMiddleware(BaseHTTPMiddleware):
    """Rate-limit /admin/* after routing; origin still checked in handlers."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.url.path.startswith("/admin"):
            from ts_harness.gateway.rate_limit import check_admin_rate_limit

            client = request.client.host if request.client else "unknown"
            try:
                check_admin_rate_limit(client)
            except AuthError as exc:
                return _json({"error": str(exc)}, exc.status)
        return await call_next(request)


def _auth(request: Request, body: dict[str, Any]) -> AuthContext:
    from ts_harness.gateway.browser_session import enforce_cookie_origin, read_session_cookie

    runtime = get_runtime()
    headers = _hdrs(request)
    used_cookie = False
    if not any(str(key).lower() == "authorization" for key in headers):
        cookie = read_session_cookie(headers)
        if cookie:
            headers = {**headers, "authorization": f"Bearer {cookie}"}
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
    if used_cookie and request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        enforce_cookie_origin(
            headers,
            allowed_origins=list(runtime.config.gateway.allowed_origins or []),
            scheme=request.url.scheme,
            host=request.url.netloc,
        )
    if isinstance(body, dict):
        body["tenant_id"] = auth.tenant_id
    return auth


async def _auth_channel(
    request: Request, channel: str, body: dict[str, Any], raw: bytes
) -> AuthContext:
    runtime = get_runtime()
    tenant_id = str(body.get("tenant_id") or runtime.config.gateway.default_tenant_id)

    if channel == "slack" and os.environ.get("SLACK_SIGNING_SECRET"):
        timestamp = request.headers.get("x-slack-request-timestamp", "")
        signature = request.headers.get("x-slack-signature", "")
        if not timestamp or abs(time.time() - float(timestamp)) > 300:
            raise AuthError("invalid slack timestamp", status=401)
        base = b"v0:" + timestamp.encode() + b":" + raw
        expected = (
            "v0="
            + hmac.new(
                os.environ["SLACK_SIGNING_SECRET"].encode(), base, hashlib.sha256
            ).hexdigest()
        )
        if not hmac.compare_digest(expected, signature):
            raise AuthError("invalid slack signature", status=401)
        return AuthContext(actor_id="slack:webhook", tenant_id=tenant_id, auth_provider="slack")

    if channel == "telegram" and os.environ.get("TELEGRAM_WEBHOOK_SECRET"):
        if (
            request.headers.get("x-telegram-bot-api-secret-token")
            != os.environ["TELEGRAM_WEBHOOK_SECRET"]
        ):
            raise AuthError("invalid telegram webhook secret", status=401)
        return AuthContext(
            actor_id="telegram:webhook", tenant_id=tenant_id, auth_provider="telegram"
        )

    if channel == "discord" and os.environ.get("DISCORD_WEBHOOK_SECRET"):
        if (
            request.headers.get("x-discord-webhook-secret")
            != os.environ["DISCORD_WEBHOOK_SECRET"]
        ):
            raise AuthError("invalid discord webhook secret", status=401)
        return AuthContext(
            actor_id="discord:webhook", tenant_id=tenant_id, auth_provider="discord"
        )

    return _auth(request, body)


def _json(
    data: dict[str, Any],
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=headers)


class _BadJsonBody(ValueError):
    pass


async def _json_object_body(
    request: Request,
    *,
    allow_empty: bool = False,
) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        if allow_empty:
            return {}
        raise ValueError("invalid json")
    try:
        body = json.loads(raw)
    except Exception as exc:
        raise ValueError("invalid json") from exc
    if not isinstance(body, dict):
        raise _BadJsonBody("request body must be a JSON object")
    return body


# ── GET /healthz ──────────────────────────────────────────────────────────────


async def healthz(request: Request) -> Response:
    runtime = get_runtime()
    report = runtime._build_health_report()
    payload = report.to_dict()
    payload["metrics"] = runtime.metrics.snapshot()
    try:
        payload["deployment_profile"] = current_profile(getattr(runtime, "config", None))
    except ValueError:
        payload["deployment_profile"] = "unknown"
    return _json(payload)


# ── GET /metrics ──────────────────────────────────────────────────────────────


async def metrics(request: Request) -> Response:
    try:
        from ts_harness.observability.prometheus import (  # type: ignore[import]
            PROMETHEUS_AVAILABLE,
            content_type,
            generate_latest,
        )

        if PROMETHEUS_AVAILABLE:
            return PlainTextResponse(generate_latest(), media_type=content_type())
    except ImportError:
        pass

    runtime = get_runtime()
    snap = runtime.metrics.snapshot()
    lines = ["# TS Harness runtime metrics"]
    for name, value in snap.items():
        lines.append(f"ts_harness_{name} {value}")
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")


# ── GET /skills/doctor ────────────────────────────────────────────────────────


async def skills_doctor(request: Request) -> Response:
    try:
        _check_origin(request)
        _auth(request, {})
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)

    from urllib.parse import parse_qs

    runtime = get_runtime()
    query = parse_qs(request.url.query)
    agent_id = (query.get("agent_id") or ["orchestrator"])[0]
    tenant_id = (query.get("tenant_id") or [runtime.config.gateway.default_tenant_id])[0]
    lines = runtime.skill_catalog.doctor(
        runtime.config.get_agent(str(agent_id)),
        runtime.config,
        tenant_id=str(tenant_id),
    )
    return _json({"agent_id": agent_id, "tenant_id": tenant_id, "diagnostics": lines})


# ── GET /mcp/tools ────────────────────────────────────────────────────────────


async def mcp_tools(request: Request) -> Response:
    try:
        _check_origin(request)
        _auth(request, {})
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)

    runtime = get_runtime()
    tools: list[Any] = []
    for server_name in runtime.config.mcp_servers:
        tools.extend(runtime.mcp.list_tools(server_name))
    return _json({"tools": tools})


async def connector_google_workspace_tools(request: Request) -> Response:
    try:
        _check_origin(request)
        _auth(request, {})
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)

    runtime = get_runtime()
    registry = getattr(runtime, "connector_registry", None)
    if registry is None:
        return _json({"tools": []})
    return _json({"tools": registry.list_tools("google-workspace")})


# ── GET /memory/smoke ─────────────────────────────────────────────────────────


async def memory_smoke(request: Request) -> Response:
    try:
        _check_origin(request)
        _auth(request, {})
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)

    return _json({"ok": get_memory_client().health()})


# ── POST /workflow/dispatch ───────────────────────────────────────────────────


async def workflow_dispatch(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)

    runtime = get_runtime()
    try:
        # Agent turns (and coding-backend runs) last minutes; keep the event loop free.
        result = await asyncio.to_thread(runtime.dispatch_workflow, body, auth)
        runtime.metrics.inc("workflow_dispatch")
        return _json(result.to_dict())
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception as exc:
        log_event("workflow_dispatch_error", error=str(exc))
        payload = DispatchResult(status="blocked", error=str(exc)).to_dict()
        return JSONResponse(payload, status_code=422, headers={"X-Legacy-Status": "200"})


async def workflow_present_artifact(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body)
        _require_admin(get_runtime(), auth, "start_program")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)

    runtime = get_runtime()
    try:
        result = runtime.handle_workflow_present_artifact(body, auth)
        runtime.metrics.inc("workflow_present_artifact")
        return _json(result)
    except Exception as exc:
        log_event("workflow_present_artifact_error", error=str(exc))
        return _json({"status": "error", "error": str(exc)}, 422)


# ── POST /workflow/cancel ─────────────────────────────────────────────────────


async def workflow_cancel(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)

    runtime = get_runtime()
    task_id = str(body.get("task_id") or "")
    runtime.audit.append(
        tenant_id=str(body.get("tenant_id") or runtime.config.gateway.default_tenant_id),
        action_type="workflow_cancel",
        actor_id=auth.actor_id,
        payload={"task_id": task_id or None},
    )
    return _json({"status": "ok", "cancelled": True, "task_id": task_id or None})


# ── POST /session/message ─────────────────────────────────────────────────────


async def session_message(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)

    runtime = get_runtime()
    try:
        result = await asyncio.to_thread(runtime.handle_session_message, body, auth)
        runtime.metrics.inc("session_message")
        return _json(result)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception as exc:
        log_event("session_message_error", error=str(exc))
        return _json({"error": str(exc)}, 500)


# ── POST /cron/trigger ────────────────────────────────────────────────────────


async def cron_trigger(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body)
        _require_admin(get_runtime(), auth, "edit_config")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)

    get_runtime().scheduler.run_due_once(force=True)
    return _json({"status": "ok", "scheduler": "triggered"})


# ── POST /sandbox/deny-check ──────────────────────────────────────────────────


async def sandbox_deny_check(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        _auth(request, body)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)

    allowed, reason = scan_command(str(body.get("command", "")))
    return _json({"denied": not allowed, "reason": reason})


# ── POST /admin/secrets/reload ────────────────────────────────────────────────


async def admin_secrets_reload(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)

    runtime = get_runtime()
    try:
        _require_admin(runtime, auth, "edit_config")
        secret_name = str(body.get("name", ""))
        tenant_id = str(body.get("tenant_id") or runtime.config.gateway.default_tenant_id)
        runtime.secret_resolver.reload(secret_name, tenant_id=tenant_id)
        runtime.audit.append(
            tenant_id=tenant_id,
            action_type="secret_reload",
            actor_id=auth.actor_id,
            payload={"name": secret_name},
        )
        return _json({"status": "reloaded", "name": secret_name})
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception as exc:
        log_event("admin_secrets_reload_error", error=str(exc))
        return _json({"error": str(exc)}, 500)


def _require_admin(runtime, auth, permission: str = "edit_config") -> None:
    from ts_harness.gateway.access import require_permission

    require_permission(runtime, auth, permission, resource=permission)


def _skills_root() -> str:
    from ts_harness.skills.roots import managed_root

    return str(managed_root())


def _skills_read_roots() -> list[str]:
    from ts_harness.skills.roots import catalog_roots

    return [str(item) for item in catalog_roots()]


# ── GET /admin/skills/drafts ──────────────────────────────────────────────────


async def admin_skills_catalog_list(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.skills_catalog_bff import list_skills_catalog_admin

    return _json(list_skills_catalog_admin(runtime))


async def admin_skills_catalog_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.skills_catalog_bff import get_skills_catalog_item_admin

    try:
        return _json(get_skills_catalog_item_admin(runtime, request.path_params["skill_id"]))
    except KeyError as exc:
        return _json({"error": str(exc)}, 404)


async def admin_skills_drafts_list(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)

    from ts_harness.skills.authoring import list_drafts

    try:
        drafts = list_drafts(_skills_root())
        return _json({"drafts": drafts, "total": len(drafts)})
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


# ── GET /admin/skills/drafts/{skill_id} ───────────────────────────────────────


async def admin_skills_drafts_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)

    from ts_harness.skills.authoring import SkillAuthoringError, get_draft

    skill_id = request.path_params["skill_id"]
    try:
        return _json(get_draft(_skills_root(), skill_id, read_roots=_skills_read_roots()))
    except SkillAuthoringError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


MAX_SKILL_DRAFT_BODY_BYTES = 3 * 1024 * 1024


class _BodyTooLarge(Exception):
    pass


async def _read_json_capped(request: Request, max_bytes: int) -> Any:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise _BodyTooLarge()
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise _BodyTooLarge()
        chunks.append(chunk)
    return json.loads(b"".join(chunks) or b"{}")


def _skill_error_payload(exc: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {"error": str(exc)}
    code = getattr(exc, "code", None)
    path = getattr(exc, "path", None)
    if code:
        payload["error_code"] = code
    if path:
        payload["path"] = path
    return payload


# ── POST /admin/skills/drafts ─────────────────────────────────────────────────


async def admin_skills_drafts_create(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _read_json_capped(request, MAX_SKILL_DRAFT_BODY_BYTES)
        auth = _auth(request, body if isinstance(body, dict) else {})
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BodyTooLarge:
        return _json(
            {
                "error": f"request body is larger than {MAX_SKILL_DRAFT_BODY_BYTES // (1024 * 1024)} MB",
                "error_code": "skill_upload_total_size",
            },
            413,
        )
    except Exception:
        return _json({"error": "invalid json"}, 400)
    if not isinstance(body, dict):
        return _json({"error": "body must be an object"}, 400)

    from ts_harness.skills.authoring import (
        SkillAuthoringError,
        build_skill_markdown,
        propose_skill_draft,
        propose_skill_folder_draft,
    )

    mode = str(body.get("mode") or "").strip()
    actor = str(auth.actor_id or "admin")
    tenant_id = str(getattr(auth, "tenant_id", "") or runtime.config.gateway.default_tenant_id)
    revision_of = str(body.get("revision_of") or "").strip() or None
    try:
        if mode == "text":
            markdown = build_skill_markdown(
                str(body.get("markdown") or ""),
                skill_id=str(body.get("id") or ""),
                category=str(body.get("category") or ""),
                description=str(body.get("description") or ""),
                version=str(body.get("version") or ""),
            )
            result = propose_skill_draft(
                root=_skills_root(),
                agent_id=actor,
                tenant_id=tenant_id,
                markdown=markdown,
                revision_of=revision_of,
                source="admin_paste",
                created_by=actor,
                read_roots=_skills_read_roots(),
            )
        elif mode == "folder":
            result = propose_skill_folder_draft(
                root=_skills_root(),
                actor_id=actor,
                tenant_id=tenant_id,
                files=body.get("files"),
                revision_of=revision_of,
                source="admin_upload",
                read_roots=_skills_read_roots(),
            )
        else:
            return _json({"error": "mode must be text or folder"}, 400)
    except (SkillAuthoringError, ValueError) as exc:
        return _json(_skill_error_payload(exc), 400)

    from urllib.parse import quote

    skill_id = str(result.get("skill_id") or "")
    runtime.audit.append(
        tenant_id=tenant_id,
        action_type="skill_proposed",
        actor_id=auth.actor_id,
        payload={
            "skill_id": skill_id,
            "revision_of": revision_of,
            "source": "admin_paste" if mode == "text" else "admin_upload",
            "layout": result.get("layout"),
        },
    )
    return _json({**result, "inbox_href": f"#/inbox/skill_draft/{quote(skill_id, safe='')}"})


# ── GET /admin/skills/drafts/{skill_id}/files/{path} ──────────────────────────


async def admin_skills_drafts_file(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)

    from ts_harness.skills.authoring import SkillAuthoringError, read_draft_file

    try:
        return _json(
            read_draft_file(
                _skills_root(),
                request.path_params["skill_id"],
                request.path_params["file_path"],
            )
        )
    except SkillAuthoringError as exc:
        return _json(_skill_error_payload(exc), 404)
    except ValueError as exc:
        return _json(_skill_error_payload(exc), 400)


# ── POST /admin/skills/drafts/{skill_id}/approve ──────────────────────────────


async def admin_skills_drafts_approve(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body if isinstance(body, dict) else {})
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)

    from ts_harness.skills.authoring import SkillAuthoringError, approve_draft

    skill_id = request.path_params["skill_id"]
    try:
        result = approve_draft(
            root=_skills_root(),
            skill_id=skill_id,
            reviewer=str(auth.actor_id or "admin"),
            read_roots=_skills_read_roots(),
        )
        runtime.skill_catalog.reload()
        tenant_id = runtime.config.gateway.default_tenant_id
        runtime.audit.append(
            tenant_id=tenant_id,
            action_type="skill_approved",
            actor_id=auth.actor_id,
            payload=result,
        )
        if result.get("revision_of"):
            runtime.audit.append(
                tenant_id=tenant_id,
                action_type="skill_revised",
                actor_id=auth.actor_id,
                payload=result,
            )
        runtime.audit.append(
            tenant_id=tenant_id,
            action_type="skill_catalog_reloaded",
            actor_id=auth.actor_id,
            payload={"skill_id": skill_id},
        )
        return _json(result)
    except SkillAuthoringError as exc:
        return _json({"error": str(exc)}, 400)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


# ── POST /admin/skills/drafts/{skill_id}/reject ───────────────────────────────


async def admin_skills_drafts_reject(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body if isinstance(body, dict) else {})
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)

    from ts_harness.skills.authoring import SkillAuthoringError, reject_draft

    skill_id = request.path_params["skill_id"]
    reason = str((body or {}).get("reason") or "")
    try:
        result = reject_draft(
            root=_skills_root(),
            skill_id=skill_id,
            reviewer=str(auth.actor_id or "admin"),
            reason=reason,
        )
        runtime.skill_catalog.reload()
        runtime.audit.append(
            tenant_id=runtime.config.gateway.default_tenant_id,
            action_type="skill_rejected",
            actor_id=auth.actor_id,
            payload=result,
        )
        return _json(result)
    except SkillAuthoringError as exc:
        return _json({"error": str(exc)}, 400)


# ── GET /admin/approvals ──────────────────────────────────────────────────────


async def admin_approvals_list(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        _require_admin(get_runtime(), auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)

    queue = get_approval_queue()
    items = [v.__dict__ for v in queue._requests.values()]
    return _json({"approvals": items, "total": len(items)})


# ── GET /admin/approvals/{id} ─────────────────────────────────────────────────


async def admin_approvals_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        _require_admin(get_runtime(), auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)

    approval_id = request.path_params["id"]
    try:
        approval = get_approval_queue().get(approval_id)
        return _json(approval.__dict__)
    except KeyError:
        return _json({"error": "not found"}, 404)


# ── GET /admin/auth/config ────────────────────────────────────────────────────


async def admin_auth_session(request: Request) -> Response:
    from ts_harness.gateway.browser_session import open_browser_session

    try:
        _check_origin(request)
        body = await _json_object_body(request)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except (ValueError, _BadJsonBody) as exc:
        return _json({"error": str(exc)}, 400)
    status, payload, cookie = open_browser_session(_hdrs(request), body, runtime=get_runtime())
    headers = {"set-cookie": cookie} if cookie else None
    return _json(payload, status, headers)


async def admin_auth_logout(request: Request) -> Response:
    from ts_harness.gateway.browser_session import close_browser_session

    try:
        _check_origin(request)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    status, payload, cookie = close_browser_session(_hdrs(request))
    return _json(payload, status, {"set-cookie": cookie})


async def admin_auth_config(request: Request) -> Response:
    try:
        _check_origin(request)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.admin_bff import auth_config_for_request

    runtime = get_runtime()
    auth = None
    try:
        auth = _auth(request, {})
    except AuthError:
        auth = None
    return _json(auth_config_for_request(runtime, auth))


# ── GET /admin/inbox ──────────────────────────────────────────────────────────


async def admin_inbox(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.admin_bff import build_inbox

    return _json(
        build_inbox(
            runtime,
            auth,
            memory=get_memory_client(),
            skills_root=_skills_root(),
        )
    )


# ── Programs BFF ──────────────────────────────────────────────────────────────


class _NotInTenant(LookupError):
    pass


def _program_for_tenant(memory: Any, program_id: str, auth: Any) -> dict[str, Any]:
    """Load a program, hiding programs of other tenants behind the same 404 as missing ones."""
    try:
        payload = memory.get_program(program_id) or {}
    except Exception as exc:  # noqa: BLE001
        raise _NotInTenant("program not found") from exc
    program = payload.get("program") if isinstance(payload.get("program"), dict) else payload
    if str(program.get("tenant_id") or "default") != str(auth.tenant_id or "default"):
        raise _NotInTenant("program not found")
    return payload


def _require_program_level(
    program_id: str, auth: Any, need: str, *, route: str, memory: Any = None
) -> Any:
    """Raise ``ProgramAccessDenied`` unless the caller has *need* on the program."""
    from ts_harness.gateway.program_access import actor_context, check_program_access

    return check_program_access(
        memory or get_memory_client(),
        program_id,
        actor_context(auth),
        need,
        tenant_id=str(auth.tenant_id or "default"),
        route=route,
    )


def _effective_level(access: Any) -> str:
    """What the console may offer: everything unless access is enforced."""
    return access.level if access.enforced else "admin"


def _with_access(payload: dict[str, Any], access: Any) -> dict[str, Any]:
    return {
        **payload,
        "access_level": _effective_level(access),
        "visibility": access.visibility or payload.get("visibility"),
    }


def _program_mutation_gate(
    program_id: str,
    auth: Any,
    *,
    node_id: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """Load a program and the permission required to mutate it.

    A missing program still requires ``start_program``, so a caller who cannot
    mutate programs gets 403 rather than a 404 that reveals the id.
    """
    from ts_harness.gateway.program_definitions_bff import program_action_permission

    try:
        payload = _program_for_tenant(get_memory_client(), program_id, auth)
    except _NotInTenant:
        return None, "start_program"
    program = payload.get("program") if isinstance(payload.get("program"), dict) else {}
    return payload, program_action_permission(str(program.get("program_type") or ""), node_id)


def _advance_node_permission(program: dict[str, Any], body: dict[str, Any]) -> str:
    """Permission for the node an advance will enter when the body omits a target."""
    del body
    from ts_harness.gateway.program_definitions_bff import program_action_permission
    from ts_harness.workflows.program_materialize import parse_program_snapshot, resolve_target_node

    program_type = str(program.get("program_type") or "")
    current = str(program.get("current_phase") or "")
    snapshot = parse_program_snapshot(program.get("program_definition_snapshot")) or {}
    try:
        target = resolve_target_node(snapshot, current, None) or None
    except Exception:
        target = None
    return program_action_permission(program_type, target)


async def admin_programs_list(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        _require_admin(get_runtime(), auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    status = request.query_params.get("status")
    try:
        limit = int(request.query_params.get("limit") or "50")
        offset = int(request.query_params.get("offset") or "0")
    except ValueError:
        return _json({"error": "invalid limit/offset"}, 400)
    if (request.query_params.get("portfolio") or "").lower() in {"1", "true", "yes"}:
        try:
            raw_hours = request.query_params.get("waiting_hours")
            waiting_hours = float(raw_hours) if raw_hours else None
        except ValueError:
            return _json({"error": "invalid waiting_hours"}, 400)
        try:
            from ts_harness.gateway.admin_bff import build_portfolio

            return _json(
                await asyncio.to_thread(
                    build_portfolio,
                    get_memory_client(),
                    auth,
                    status=status,
                    phase=request.query_params.get("phase") or None,
                    owner=request.query_params.get("owner") or None,
                    waiting_hours=waiting_hours,
                    limit=limit,
                    offset=offset,
                )
            )
        except Exception as exc:
            return _json({"error": str(exc)}, 502)
    from ts_harness.gateway.program_access import list_actor_params

    try:
        return _json(
            get_memory_client().list_programs(
                tenant_id=auth.tenant_id,
                status=status,
                limit=limit,
                offset=offset,
                actor_params=list_actor_params(auth),
            )
        )
    except Exception as exc:
        return _json({"error": str(exc)}, 502)


async def admin_programs_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        _require_admin(get_runtime(), auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    program_id = request.path_params["id"]
    memory = get_memory_client()
    try:
        payload = _program_for_tenant(memory, program_id, auth)
        access = _require_program_level(program_id, auth, "read", route="get", memory=memory)
        return _json(_with_access(payload, access))
    except _NotInTenant as exc:
        return _json({"error": str(exc)}, 404)
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)


async def admin_programs_evidence(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        _require_admin(get_runtime(), auth, "edit_config")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    program_id = request.path_params["id"]
    memory = get_memory_client()
    try:
        _program_for_tenant(memory, program_id, auth)
        _require_program_level(program_id, auth, "read", route="evidence", memory=memory)
    except _NotInTenant as exc:
        return _json({"error": str(exc)}, 404)
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.program_evidence import build_evidence_document, canonical_json

    actor = str(auth.email or auth.actor_id or "admin")
    try:
        raw = await asyncio.to_thread(memory.get_program_evidence, program_id)
        document = build_evidence_document(raw, generated_by=actor)
        await asyncio.to_thread(
            memory.record_program_evidence_export,
            program_id,
            sha256=document["sha256"],
            actor=actor,
            tenant_id=str(auth.tenant_id or "default"),
            generated_at=document["generated_at"],
        )
    except Exception as exc:  # noqa: BLE001
        log_event("admin_program_evidence_error", program_id=program_id, error=str(exc))
        return _json({"error": f"evidence export failed: {exc}"}, 502)
    filename = f"program-{program_id}-evidence.json"
    return Response(
        canonical_json(document),
        media_type="application/json",
        headers={
            "X-Evidence-SHA256": document["sha256"],
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Access-Control-Expose-Headers": "X-Evidence-SHA256, Content-Disposition",
        },
    )


async def admin_programs_status(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        _require_admin(get_runtime(), auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    program_id = request.path_params["id"]
    memory = get_memory_client()
    try:
        _program_for_tenant(memory, program_id, auth)
        access = _require_program_level(program_id, auth, "read", route="status", memory=memory)
        return _json(_with_access(memory.get_program_status(program_id), access))
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception as exc:
        return _json({"error": str(exc)}, 404)


async def admin_programs_phase_activity(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        _require_admin(get_runtime(), auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    program_id = request.path_params["id"]
    phase = request.path_params["phase"]
    memory = get_memory_client()
    try:
        _program_for_tenant(memory, program_id, auth)
        _require_program_level(program_id, auth, "read", route="phase_activity", memory=memory)
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception as exc:
        return _json({"error": str(exc)}, 404)
    from ts_harness.workflows import phase_activity

    try:
        return _json(phase_activity.phase_activity(memory, program_id, phase))
    except Exception as exc:  # noqa: BLE001
        log_event("admin_program_phase_activity_error", program_id=program_id, phase=phase, error=str(exc))
        return _json({"error": str(exc)}, 500)


async def admin_programs_advance(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body if isinstance(body, dict) else {})
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    if not isinstance(body, dict):
        body = {}
    program_id = request.path_params["id"]
    target = str(body.get("target_phase") or body.get("target_node") or "").strip() or None
    try:
        payload, permission = _program_mutation_gate(program_id, auth, node_id=target)
        if payload is not None and not target:
            program = payload.get("program") if isinstance(payload.get("program"), dict) else {}
            permission = _advance_node_permission(program, body)
        _require_admin(get_runtime(), auth, permission)
        if payload is None:
            raise _NotInTenant("program not found")
        _require_program_level(program_id, auth, "write", route="advance")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _NotInTenant as exc:
        return _json({"error": str(exc)}, 404)
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)
    try:
        service = get_delivery_program_service()
        result = service.advance_program(
            program_id=program_id,
            target_phase=str(body.get("target_phase") or body.get("target_node") or "").strip()
            or None,
            summary=str(body.get("summary") or ""),
            artifacts=body.get("artifacts") if isinstance(body.get("artifacts"), dict) else None,
            tenant_id=str(auth.tenant_id or "default"),
            actor=str(auth.actor_id or "admin"),
            requester_actor_id=str(auth.actor_id or "admin"),
            source_workflow_id=str(body.get("source_workflow_id") or "") or None,
        )
        return _json(result)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    except Exception as exc:
        return _json({"error": str(exc)}, 400)


async def admin_programs_patch(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body if isinstance(body, dict) else {})
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    if not isinstance(body, dict):
        body = {}
    program_id = request.path_params["id"]
    try:
        payload, permission = _program_mutation_gate(program_id, auth)
        _require_admin(get_runtime(), auth, permission)
        if payload is None:
            raise _NotInTenant("program not found")
        _require_program_level(program_id, auth, "admin", route="patch")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _NotInTenant as exc:
        return _json({"error": str(exc)}, 404)
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)
    try:
        result = get_memory_client().patch_program(
            program_id,
            tenant_id=str(auth.tenant_id or "default"),
            project_id=body.get("project_id"),
            project_id_source=body.get("project_id_source") or ("user" if "project_id" in body else None),
            excluded_from_autoshare=body.get("excluded_from_autoshare"),
            actor=str(auth.actor_id or "admin"),
        )
        from ts_harness.workflows.program_brief import attach_related_program_context

        program = (result or {}).get("program") or {}
        attach_related_program_context(
            get_memory_client(),
            program_id=program_id,
            tenant_id=str(auth.tenant_id or "default"),
            project_id=program.get("project_id") if "project_id" not in body else body.get("project_id"),
            actor=str(auth.actor_id or "admin"),
        )
        return _json(result)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    except Exception as exc:
        return _json({"error": str(exc)}, 400)


async def admin_programs_department_memory(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth, "promote_department")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    brief_id = str(body.get("brief_id") or "").strip()
    if not brief_id:
        return _json({"error": "brief_id is required"}, 400)
    program_id = request.path_params["id"]
    try:
        payload = _program_for_tenant(get_memory_client(), program_id, auth)
        _require_program_level(program_id, auth, "write", route="department_memory")
    except _NotInTenant as exc:
        return _json({"error": str(exc)}, 404)
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)
    program = payload.get("program") if isinstance(payload.get("program"), dict) else payload
    from ts_harness.policy.permissions import department_for_email

    caller_department = department_for_email(
        getattr(runtime, "config", None), getattr(auth, "email", None)
    )
    program_department = str((program or {}).get("department") or "").strip().lower()
    if not caller_department or caller_department != program_department:
        return _json({"error": "department mismatch"}, 403)
    try:
        result = get_memory_client().promote_department_memory(
            program_id,
            brief_id=brief_id,
            tenant_id=str(auth.tenant_id or "default"),
            actor=str(auth.actor_id or "admin"),
        )
        return _json(result)
    except Exception as exc:
        text = str(exc)
        if "brief not found" in text:
            return _json({"error": "brief not found"}, 404)
        return _json({"error": text}, 400)


async def admin_programs_link(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body if isinstance(body, dict) else {})
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    if not isinstance(body, dict):
        body = {}
    program_id = request.path_params["id"]
    to_program_id = str(body.get("to_program_id") or "").strip()
    if not to_program_id:
        return _json({"error": "to_program_id is required"}, 400)
    try:
        payload, permission = _program_mutation_gate(program_id, auth)
        _require_admin(get_runtime(), auth, permission)
        if payload is None:
            raise _NotInTenant("program not found")
        _require_program_level(program_id, auth, "admin", route="link")
        _program_for_tenant(get_memory_client(), to_program_id, auth)
        _require_program_level(to_program_id, auth, "read", route="link")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _NotInTenant as exc:
        return _json({"error": str(exc)}, 404)
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)
    try:
        result = get_memory_client().create_program_link(
            program_id,
            to_program_id=to_program_id,
            tenant_id=str(auth.tenant_id or "default"),
            actor=str(auth.actor_id or "admin"),
        )
        from ts_harness.workflows.program_brief import rebuild_related_program_context

        rebuild_related_program_context(
            get_memory_client(),
            program_id=program_id,
            tenant_id=str(auth.tenant_id or "default"),
            actor=str(auth.actor_id or "admin"),
        )
        return _json(result)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    except Exception as exc:
        return _json({"error": str(exc)}, 400)


async def admin_programs_unlink(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    program_id = request.path_params["id"]
    to_program_id = request.path_params["to_id"]
    try:
        payload, permission = _program_mutation_gate(program_id, auth)
        _require_admin(get_runtime(), auth, permission)
        if payload is None:
            raise _NotInTenant("program not found")
        _require_program_level(program_id, auth, "admin", route="unlink")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _NotInTenant as exc:
        return _json({"error": str(exc)}, 404)
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)
    try:
        result = get_memory_client().delete_program_link(
            program_id,
            to_program_id,
            tenant_id=str(auth.tenant_id or "default"),
            actor=str(auth.actor_id or "admin"),
        )
        from ts_harness.workflows.program_brief import rebuild_related_program_context

        rebuild_related_program_context(
            get_memory_client(),
            program_id=program_id,
            tenant_id=str(auth.tenant_id or "default"),
            actor=str(auth.actor_id or "admin"),
        )
        return _json(result)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    except Exception as exc:
        return _json({"error": str(exc)}, 400)


async def admin_programs_cancel(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body if isinstance(body, dict) else {})
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    program_id = request.path_params["id"]
    actor = str(auth.actor_id or "admin")
    reason = str((body or {}).get("reason") or "")
    memory = get_memory_client()
    try:
        payload, permission = _program_mutation_gate(program_id, auth)
        _require_admin(get_runtime(), auth, permission)
        if payload is None:
            raise _NotInTenant("program not found")
        _require_program_level(program_id, auth, "admin", route="cancel", memory=memory)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _NotInTenant as exc:
        return _json({"error": str(exc)}, 404)
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)
    try:
        result = memory.cancel_program(program_id, actor=actor, reason=reason)
    except Exception as exc:
        return _json({"error": str(exc)}, 400)
    try:
        from ts_harness.workflows.program_compensation import compensate_cancelled_program

        compensation = await asyncio.to_thread(
            compensate_cancelled_program,
            memory,
            program_id,
            reason=reason,
            actor=actor,
            defaults=dict(getattr(get_runtime().config, "program_defaults", None) or {}),
        )
        result = {**result, "compensation": {"status": compensation["status"], "actions": compensation["actions"]}}
    except Exception as exc:  # noqa: BLE001
        result = {**result, "compensation": {"status": "failed", "error": str(exc)}}
    return _json(result)


async def admin_programs_access_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        _require_admin(get_runtime(), auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    program_id = request.path_params["id"]
    memory = get_memory_client()
    try:
        _program_for_tenant(memory, program_id, auth)
        access = _require_program_level(program_id, auth, "read", route="access_get", memory=memory)
        return _json({**memory.list_program_access(program_id), "access_level": _effective_level(access)})
    except _NotInTenant as exc:
        return _json({"error": str(exc)}, 404)
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception as exc:  # noqa: BLE001
        return _json({"error": str(exc)}, 502)


def _shared_access_json(result: dict[str, Any]) -> Response:
    return _json({**result, "access_level": "admin"})


async def _share_gate(request: Request, body: dict[str, Any], route: str) -> tuple[Any, str] | Response:
    try:
        _check_origin(request)
        auth = _auth(request, body)
        _require_admin(get_runtime(), auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    program_id = request.path_params["id"]
    try:
        _program_for_tenant(get_memory_client(), program_id, auth)
        _require_program_level(program_id, auth, "admin", route=route)
    except _NotInTenant as exc:
        return _json({"error": str(exc)}, 404)
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)
    return auth, program_id


async def admin_programs_access_grant(request: Request) -> Response:
    try:
        body = await _json_object_body(request)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    gate = await _share_gate(request, body, "access_grant")
    if isinstance(gate, Response):
        return gate
    auth, program_id = gate
    try:
        return _shared_access_json(
            get_memory_client().grant_program_access(
                program_id,
                principal_type=str(body.get("principal_type") or ""),
                principal_id=str(body.get("principal_id") or ""),
                level=str(body.get("level") or ""),
                actor=str(auth.actor_id or "admin"),
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _json({"error": str(exc)}, 400)


async def admin_programs_access_revoke(request: Request) -> Response:
    gate = await _share_gate(request, {}, "access_revoke")
    if isinstance(gate, Response):
        return gate
    auth, program_id = gate
    try:
        return _shared_access_json(
            get_memory_client().revoke_program_access(
                program_id,
                principal_type=request.path_params["principal_type"],
                principal_id=request.path_params["principal_id"],
                actor=str(auth.actor_id or "admin"),
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _json({"error": str(exc)}, 400)


async def admin_programs_visibility(request: Request) -> Response:
    try:
        body = await _json_object_body(request)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    gate = await _share_gate(request, body, "visibility")
    if isinstance(gate, Response):
        return gate
    auth, program_id = gate
    try:
        return _shared_access_json(
            get_memory_client().set_program_visibility(
                program_id,
                visibility=str(body.get("visibility") or ""),
                actor=str(auth.actor_id or "admin"),
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _json({"error": str(exc)}, 400)


async def admin_directory_search(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        _require_admin(get_runtime(), auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    try:
        limit = int(request.query_params.get("limit") or "20")
    except ValueError:
        return _json({"error": "invalid limit"}, 400)
    try:
        return _json(
            await asyncio.to_thread(
                get_memory_client().directory_search,
                tenant_id=str(auth.tenant_id or "default"),
                q=request.query_params.get("q") or "",
                limit=limit,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _json({"error": str(exc)}, 502)


async def admin_workflows_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        _require_admin(get_runtime(), auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    workflow_id = request.path_params["id"]
    try:
        payload = get_memory_client().get_workflow_tasks(workflow_id)
    except Exception as exc:
        return _json({"error": str(exc)}, 404)
    tenant = str(auth.tenant_id or "default")
    if any(str(task.get("tenant_id") or "default") != tenant for task in (payload or {}).get("tasks") or []):
        return _json({"error": "workflow not found"}, 404)
    return _json(payload)


def _require_task_program_level(task_id: str, auth: Any, need: str, *, route: str) -> None:
    """Gate actions need *need* on the task's program; tasks outside programs only need the permission."""
    from ts_harness.gateway.program_access import access_mode, actor_context

    actor = actor_context(auth)
    if actor is None or access_mode() == "off":
        return
    memory = get_memory_client()
    try:
        program_id = (memory.task_program(task_id) or {}).get("program_id")
    except Exception as exc:  # noqa: BLE001
        if access_mode() == "enforce":
            raise ProgramAccessDenied("task not found", 404) from exc
        return
    if program_id:
        _require_program_level(str(program_id), auth, need, route=route, memory=memory)


def _auth_decision_fields(auth: Any, body: Any) -> dict[str, Any]:
    from ts_harness.workflows.decisions import roles_for_email

    email = str(getattr(auth, "email", None) or "").strip().lower() or None
    roles = {str(role).strip().lower() for role in (getattr(auth, "roles", None) or []) if str(role).strip()}
    roles.update(roles_for_email(email))
    fields: dict[str, Any] = {}
    if email:
        fields["actor_email"] = email
    if roles:
        fields["actor_roles"] = sorted(roles)
    from ts_harness.policy.permissions import actor_permissions

    try:
        from ts_harness.gateway.server import get_runtime

        granted = actor_permissions(auth, get_runtime().config)
    except Exception:
        granted = set()
    if granted:
        fields["actor_permissions"] = sorted(granted)
    reason = str((body or {}).get("reason") or "").strip() if isinstance(body, dict) else ""
    if reason:
        fields["reason"] = reason
    return fields


async def admin_workflows_approve(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body if isinstance(body, dict) else {})
        _require_admin(get_runtime(), auth, "approve_gate")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    task_id = str((body or {}).get("id") or (body or {}).get("task_id") or "")
    if not task_id:
        return _json({"error": "id is required"}, 400)
    try:
        _require_task_program_level(task_id, auth, "approve", route="approve")
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)
    try:
        return _json(
            get_memory_client().approve_task(
                task_id,
                actor=str(auth.actor_id or "admin"),
                tenant_id=str(auth.tenant_id or "default"),
                **_auth_decision_fields(auth, body),
            )
        )
    except Exception as exc:
        return _json({"error": str(exc)}, 400)


async def admin_workflows_reject(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body if isinstance(body, dict) else {})
        _require_admin(get_runtime(), auth, "approve_gate")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    task_id = str((body or {}).get("id") or (body or {}).get("task_id") or "")
    if not task_id:
        return _json({"error": "id is required"}, 400)
    try:
        _require_task_program_level(task_id, auth, "approve", route="reject")
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)
    try:
        return _json(
            get_memory_client().reject_task(
                task_id,
                actor=str(auth.actor_id or "admin"),
                tenant_id=str(auth.tenant_id or "default"),
                **_auth_decision_fields(auth, body),
            )
        )
    except Exception as exc:
        return _json({"error": str(exc)}, 400)


async def admin_workflows_decide(request: Request) -> Response:
    """Revise again, accept, or stop a step parked for a human decision."""
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body if isinstance(body, dict) else {})
        _require_admin(get_runtime(), auth, "approve_gate")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    body = body if isinstance(body, dict) else {}
    workflow_id = str(body.get("workflow_id") or "").strip()
    task_id = str(body.get("task_id") or "").strip()
    action = str(body.get("action") or "").strip()
    if not workflow_id or not task_id or not action:
        return _json({"error": "workflow_id, task_id and action are required"}, 400)
    tenant_id = str(auth.tenant_id or "default")
    try:
        workflow = get_memory_client().get_workflow(workflow_id).get("workflow") or {}
    except Exception:
        workflow = {}
    if not workflow or str(workflow.get("tenant_id") or "default") != tenant_id:
        return _json({"error": "workflow not found"}, 404)
    try:
        _require_task_program_level(task_id, auth, "approve", route="decide")
    except ProgramAccessDenied as exc:
        return _json({"error": str(exc)}, exc.status)
    try:
        result = get_workflow_engine().decide_blocked_step(
            workflow_id,
            task_id,
            action,
            actor=str(auth.actor_id or "admin"),
            guidance=str(body.get("guidance") or ""),
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        return _json({"error": str(exc)}, 409)
    except Exception as exc:  # noqa: BLE001
        log_event("admin_workflow_decide_error", workflow_id=workflow_id, task_id=task_id, error=str(exc))
        return _json({"error": str(exc)}, 500)
    return _json(result)


# ── Workflow definition admin BFF ─────────────────────────────────────────────


async def admin_workflow_definitions_list(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.workflow_definitions_bff import (
        list_workflow_definitions_admin,
    )

    try:
        return _json(list_workflow_definitions_admin(runtime))
    except Exception as exc:
        log_event("admin_workflow_definitions_list_error", error=str(exc))
        return _json({"error": str(exc)}, 500)


async def admin_workflow_definitions_catalog(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.workflow_definitions_bff import (
        workflow_definition_catalog_admin,
    )

    try:
        return _json(workflow_definition_catalog_admin(runtime))
    except Exception as exc:
        log_event("admin_workflow_definitions_catalog_error", error=str(exc))
        return _json({"error": str(exc)}, 500)


async def admin_workflow_definitions_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.workflow_definitions_bff import (
        get_workflow_definition_admin,
    )

    try:
        return _json(get_workflow_definition_admin(request.path_params["workflow_type"]))
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_workflow_definition_drafts_create(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.workflow_definitions_bff import (
        create_workflow_definition_draft_admin,
    )

    try:
        result = create_workflow_definition_draft_admin(runtime, auth, body)
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="workflow_definition_draft_created",
            actor_id=auth.actor_id,
            payload=result["draft"],
        )
        return _json(result, status=201)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_workflow_definition_drafts_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.workflow_definitions_bff import (
        get_workflow_definition_draft_admin,
    )

    try:
        return _json(get_workflow_definition_draft_admin(request.path_params["draft_id"]))
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_workflow_definition_drafts_update(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.workflow_definitions_bff import (
        update_workflow_definition_draft_admin,
    )

    try:
        result = update_workflow_definition_draft_admin(
            runtime,
            auth,
            request.path_params["draft_id"],
            body,
        )
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="workflow_definition_draft_updated",
            actor_id=auth.actor_id,
            payload=result["draft"],
        )
        return _json(result)
    except ConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_workflow_definition_drafts_delete(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.workflow_definitions_bff import (
        delete_workflow_definition_draft_admin,
    )

    try:
        result = delete_workflow_definition_draft_admin(request.path_params["draft_id"])
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="workflow_definition_draft_deleted",
            actor_id=auth.actor_id,
            payload=result,
        )
        return _json(result)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_workflow_definition_drafts_validate(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request, allow_empty=True)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.workflow_definitions_bff import (
        validate_workflow_definition_draft_admin,
    )

    try:
        result = validate_workflow_definition_draft_admin(
            runtime,
            request.path_params["draft_id"],
            body,
        )
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="workflow_definition_validated",
            actor_id=auth.actor_id,
            payload={
                "draft_id": request.path_params["draft_id"],
                "valid": result["valid"],
                "issue_count": len(result["issues"]),
            },
        )
        return _json(result)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_workflow_definition_drafts_activate(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.workflow_definitions_bff import (
        activate_workflow_definition_draft_admin,
    )

    try:
        result = activate_workflow_definition_draft_admin(
            runtime,
            auth,
            request.path_params["draft_id"],
            body,
        )
        reload_ok = None
        try:
            engine = get_workflow_engine()
            reload_definitions = getattr(engine, "reload_definitions", None)
            if callable(reload_definitions):
                reload_ok = bool(reload_definitions())
        except Exception as exc:
            reload_ok = False
            reload_error = str(exc)
        else:
            reload_error = ""
        if reload_ok is False:
            payload = {
                "draft_id": request.path_params["draft_id"],
                "workflow_type": result.get("active", {}).get("workflow_type"),
                "definition_version": result.get("active", {}).get(
                    "definition_version"
                ),
                "reload": {"ok": False},
            }
            if reload_error:
                payload["error"] = reload_error
            log_event("workflow_definition_reload_failed", **payload)
            runtime.audit.append(
                tenant_id=auth.tenant_id,
                action_type="workflow_definition_reload_failed",
                actor_id=auth.actor_id,
                payload=payload,
            )
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="workflow_definition_activated",
            actor_id=auth.actor_id,
            payload=result["active"],
        )
        if reload_ok is False:
            result = {
                **result,
                "reload_ok": False,
                "reload": {"ok": False},
                "warning": "workflow definition activated but reload failed",
            }
        elif reload_ok is True:
            result = {**result, "reload_ok": True, "reload": {"ok": True}}
        return _json(result)
    except ConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_workflow_definition_start(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth, "start_program")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.workflow_definitions_bff import (
        start_workflow_definition_admin,
    )

    workflow_type = request.path_params["workflow_type"]
    try:
        try:
            engine = get_workflow_engine()
        except Exception as exc:
            return _json(
                {
                    "error": f"workflow engine unavailable: {exc}",
                },
                503,
            )
        reload_if_changed = getattr(engine, "reload_if_changed", None)
        if callable(reload_if_changed):
            if not reload_if_changed():
                return _json(
                    {
                        "error": (
                            "workflow definition reload failed; engine may be stale"
                        )
                    },
                    503,
                )
        result = start_workflow_definition_admin(
            runtime,
            auth,
            workflow_type,
            body,
            engine,
        )
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="workflow_definition_started",
            actor_id=auth.actor_id,
            payload={
                "workflow_type": workflow_type,
                "workflow_id": result.get("workflow_id"),
                "session_id": result.get("session_id"),
            },
        )
        return _json(result)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


# ── Workflow fragment admin BFF ───────────────────────────────────────────────


async def _admin_fragment_call(
    request: Request,
    call: Any,
    *,
    body_mode: str = "none",
    audit_action: str = "",
    audit_payload: Any = None,
    status: int = 200,
) -> Response:
    """Run a fragment BFF call behind origin, auth and admin checks, auditing mutations.

    ``body_mode`` is ``none`` (no body), ``required`` or ``optional``.
    """
    try:
        _check_origin(request)
        body: dict[str, Any] = {}
        if body_mode != "none":
            body = await _json_object_body(request, allow_empty=body_mode == "optional")
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth, "view" if body_mode == "none" else "edit_config")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    try:
        result = call(runtime, auth, body)
        if audit_action:
            runtime.audit.append(
                tenant_id=auth.tenant_id,
                action_type=audit_action,
                actor_id=auth.actor_id,
                payload=audit_payload(result) if audit_payload else result,
            )
        return _json(result, status=status)
    except ConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_workflow_fragments_list(request: Request) -> Response:
    from ts_harness.gateway import workflow_fragments_bff as bff

    return await _admin_fragment_call(request, lambda _r, _a, _b: bff.list_workflow_fragments_admin())


async def admin_workflow_fragments_get(request: Request) -> Response:
    from ts_harness.gateway import workflow_fragments_bff as bff

    fragment_id = request.path_params["fragment_id"]
    version = str(request.query_params.get("version") or "")
    return await _admin_fragment_call(
        request, lambda _r, _a, _b: bff.get_workflow_fragment_admin(fragment_id, version)
    )


async def admin_workflow_fragment_drafts_create(request: Request) -> Response:
    from ts_harness.gateway import workflow_fragments_bff as bff

    return await _admin_fragment_call(
        request,
        lambda _r, auth, body: bff.create_workflow_fragment_draft_admin(auth, body),
        body_mode="required",
        audit_action="workflow_fragment_draft_created",
        audit_payload=lambda result: result["draft"],
        status=201,
    )


async def admin_workflow_fragment_drafts_get(request: Request) -> Response:
    from ts_harness.gateway import workflow_fragments_bff as bff

    draft_id = request.path_params["draft_id"]
    return await _admin_fragment_call(
        request, lambda _r, _a, _b: bff.get_workflow_fragment_draft_admin(draft_id)
    )


async def admin_workflow_fragment_drafts_update(request: Request) -> Response:
    from ts_harness.gateway import workflow_fragments_bff as bff

    draft_id = request.path_params["draft_id"]
    return await _admin_fragment_call(
        request,
        lambda _r, auth, body: bff.update_workflow_fragment_draft_admin(auth, draft_id, body),
        body_mode="required",
        audit_action="workflow_fragment_draft_updated",
        audit_payload=lambda result: result["draft"],
    )


async def admin_workflow_fragment_drafts_delete(request: Request) -> Response:
    from ts_harness.gateway import workflow_fragments_bff as bff

    draft_id = request.path_params["draft_id"]
    return await _admin_fragment_call(
        request,
        lambda _r, _a, _b: bff.delete_workflow_fragment_draft_admin(draft_id),
        audit_action="workflow_fragment_draft_deleted",
    )


async def admin_workflow_fragment_drafts_validate(request: Request) -> Response:
    from ts_harness.gateway import workflow_fragments_bff as bff

    draft_id = request.path_params["draft_id"]
    return await _admin_fragment_call(
        request,
        lambda runtime, _a, body: bff.validate_workflow_fragment_draft_admin(runtime, draft_id, body),
        body_mode="optional",
        audit_action="workflow_fragment_validated",
        audit_payload=lambda result: {
            "draft_id": draft_id,
            "valid": result["valid"],
            "issue_count": len(result["issues"]),
        },
    )


async def admin_workflow_fragment_drafts_activate(request: Request) -> Response:
    from ts_harness.gateway import workflow_fragments_bff as bff

    draft_id = request.path_params["draft_id"]
    return await _admin_fragment_call(
        request,
        lambda runtime, auth, body: bff.activate_workflow_fragment_draft_admin(runtime, auth, draft_id, body),
        body_mode="required",
        audit_action="workflow_fragment_activated",
        audit_payload=lambda result: result["active"],
    )


# ── Routing definition admin BFF ──────────────────────────────────────────────


async def admin_routing_definitions_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.routing_definitions_bff import (
        get_routing_definition_admin,
    )

    return _json(get_routing_definition_admin(runtime))


async def admin_routing_definition_drafts_create(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.routing_definitions_bff import (
        create_routing_definition_draft_admin,
    )

    try:
        return _json(create_routing_definition_draft_admin(runtime, auth, body))
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_routing_definition_drafts_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.routing_definitions_bff import (
        get_routing_definition_draft_admin,
    )

    try:
        return _json(get_routing_definition_draft_admin(request.path_params["draft_id"]))
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)


async def admin_routing_definition_drafts_update(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.routing_definitions_bff import (
        update_routing_definition_draft_admin,
    )
    from ts_harness.workflows.routing_definition_store import ConflictError

    try:
        return _json(
            update_routing_definition_draft_admin(
                runtime,
                auth,
                request.path_params["draft_id"],
                body,
            )
        )
    except ConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_routing_definition_drafts_validate(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.routing_definitions_bff import (
        validate_routing_definition_draft_admin,
    )

    try:
        return _json(
            validate_routing_definition_draft_admin(request.path_params["draft_id"])
        )
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_routing_definition_drafts_activate(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.routing_definitions_bff import (
        activate_routing_definition_draft_admin,
    )
    from ts_harness.workflows.routing_definition_store import ConflictError

    try:
        result = activate_routing_definition_draft_admin(
            runtime,
            auth,
            request.path_params["draft_id"],
            body,
        )
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="routing_definition_activated",
            actor_id=auth.actor_id,
            payload={
                "draft_id": request.path_params["draft_id"],
                "reload_ok": result.get("reload_ok"),
            },
        )
        return _json(result)
    except ConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


# ── Runtime settings admin BFF ────────────────────────────────────────────────


async def admin_runtime_settings_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.runtime_settings_bff import get_runtime_settings_admin

    return _json(get_runtime_settings_admin(runtime))


async def admin_runtime_settings_drafts_create(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.runtime_settings_bff import (
        create_runtime_settings_draft_admin,
    )

    try:
        return _json(create_runtime_settings_draft_admin(runtime, auth, body))
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_runtime_settings_drafts_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.runtime_settings_bff import (
        get_runtime_settings_draft_admin,
    )

    try:
        return _json(get_runtime_settings_draft_admin(request.path_params["draft_id"]))
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)


async def admin_runtime_settings_drafts_update(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.runtime_settings_bff import (
        update_runtime_settings_draft_admin,
    )
    from ts_harness.workflows.runtime_settings_store import ConflictError

    try:
        return _json(
            update_runtime_settings_draft_admin(
                runtime,
                auth,
                request.path_params["draft_id"],
                body,
            )
        )
    except ConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_runtime_settings_drafts_validate(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.runtime_settings_bff import (
        validate_runtime_settings_draft_admin,
    )

    try:
        return _json(
            validate_runtime_settings_draft_admin(runtime, request.path_params["draft_id"])
        )
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_runtime_settings_drafts_activate(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.runtime_settings_bff import (
        activate_runtime_settings_draft_admin,
    )
    from ts_harness.workflows.runtime_settings_store import ConflictError

    try:
        result = activate_runtime_settings_draft_admin(
            runtime,
            auth,
            request.path_params["draft_id"],
            body,
        )
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="runtime_settings_activated",
            actor_id=auth.actor_id,
            payload={
                "draft_id": request.path_params["draft_id"],
                "reload_ok": result.get("reload_ok"),
            },
        )
        return _json(result)
    except ConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except PermissionError as exc:
        return _json({"error": str(exc)}, 403)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_mcp_settings_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.mcp_settings_bff import get_mcp_settings_admin

    return _json(get_mcp_settings_admin(runtime))


async def internal_coding_job_github_token(request: Request) -> Response:
    from ts_harness.coding.github_tokens import JobTokenRejected, NoAppForRepository, mint_job_github_token
    from ts_harness.gateway.auth import extract_bearer_token

    job_id = request.path_params["job_id"]
    token = extract_bearer_token(dict(request.headers)) or ""
    try:
        payload = await asyncio.to_thread(mint_job_github_token, job_id, token)
    except JobTokenRejected as exc:
        return _json({"error": str(exc)}, 403)
    except NoAppForRepository as exc:
        return _json({"error": str(exc)}, 404)
    except Exception as exc:  # noqa: BLE001
        log_event("coding_job_github_token_failed", job_id=job_id, error=str(exc)[:200])
        return _json({"error": "token minting failed"}, 502)
    return _json(payload)


async def _admin_projects_call(request: Request, call) -> Response:
    from ts_harness.gateway.projects_bff import ProjectAccessDenied
    from ts_harness.memory.client import ProgramNotFound

    body: dict[str, Any] = {}
    try:
        _check_origin(request)
        if request.method in {"POST", "PUT"}:
            body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    try:
        payload = await asyncio.to_thread(call, get_memory_client(), auth, getattr(runtime, "config", None), body)
    except ProjectAccessDenied as exc:
        return _json({"error": str(exc)}, 403)
    except (LookupError, ProgramNotFound):
        return _json({"error": "not found"}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    except RuntimeError as exc:
        try:
            message = str(json.loads(str(exc)).get("error") or exc)
        except (ValueError, AttributeError):
            message = str(exc)
        return _json({"error": message[:300]}, 400)
    return _json(payload)


async def admin_github(request: Request) -> Response:
    from ts_harness.gateway import github_bff as bff

    return await _admin_projects_call(request, bff.github_overview)


async def admin_github_sync(request: Request) -> Response:
    from ts_harness.gateway import github_bff as bff

    return await _admin_projects_call(request, bff.sync_installations)


async def admin_projects(request: Request) -> Response:
    from ts_harness.gateway import projects_bff as bff

    if request.method == "POST":
        return await _admin_projects_call(request, bff.create_project_admin)
    return await _admin_projects_call(request, lambda m, a, c, _b: bff.list_projects_admin(m, a, c))


async def admin_project(request: Request) -> Response:
    from ts_harness.gateway import projects_bff as bff

    pid = request.path_params["project_id"]
    if request.method == "PUT":
        return await _admin_projects_call(request, lambda m, a, c, b: bff.update_project_admin(m, a, c, pid, b))
    return await _admin_projects_call(request, lambda m, a, c, _b: bff.get_project_admin(m, a, c, pid))


async def admin_project_registries(request: Request) -> Response:
    from ts_harness.gateway import projects_bff as bff

    pid = request.path_params["project_id"]
    if request.method == "POST":
        return await _admin_projects_call(request, lambda m, a, c, b: bff.create_registry_admin(m, a, c, pid, b))
    return await _admin_projects_call(request, lambda m, a, c, _b: bff.list_registries_admin(m, a, c, pid))


async def admin_project_registry(request: Request) -> Response:
    from ts_harness.gateway import projects_bff as bff

    pid = request.path_params["project_id"]
    rid = request.path_params["registry_id"]
    if request.method == "DELETE":
        return await _admin_projects_call(request, lambda m, a, c, _b: bff.delete_registry_admin(m, a, c, pid, rid))
    return await _admin_projects_call(request, lambda m, a, c, b: bff.update_registry_admin(m, a, c, pid, rid, b))


async def admin_project_registry_test(request: Request) -> Response:
    from ts_harness.gateway import projects_bff as bff

    pid = request.path_params["project_id"]
    rid = request.path_params["registry_id"]
    return await _admin_projects_call(request, lambda m, a, c, _b: bff.test_registry_admin(m, a, c, pid, rid))


async def admin_mcp_settings_drafts_create(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth, "edit_allowlists")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.mcp_settings_bff import create_mcp_settings_draft_admin

    try:
        return _json(create_mcp_settings_draft_admin(runtime, auth, body))
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_mcp_settings_drafts_update(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth, "edit_allowlists")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.mcp_settings_bff import update_mcp_settings_draft_admin
    from ts_harness.workflows.mcp_settings_store import ConflictError as McpConflictError

    try:
        return _json(
            update_mcp_settings_draft_admin(
                runtime,
                auth,
                request.path_params["draft_id"],
                body,
            )
        )
    except McpConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_mcp_settings_drafts_validate(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "edit_allowlists")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.mcp_settings_bff import validate_mcp_settings_draft_admin

    try:
        return _json(validate_mcp_settings_draft_admin(request.path_params["draft_id"]))
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_mcp_settings_activate(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth, "edit_allowlists")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.mcp_settings_bff import activate_mcp_settings_admin
    from ts_harness.workflows.mcp_settings_store import ConflictError as McpConflictError

    try:
        result = activate_mcp_settings_admin(runtime, auth, body)
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="mcp_settings_activated",
            actor_id=auth.actor_id,
            payload={
                "draft_id": body.get("draft_id"),
                "degraded": result.get("degraded"),
            },
        )
        return _json(result)
    except McpConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_agents_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.agents_overlay_bff import get_agents_admin

    return _json(get_agents_admin(runtime))


async def admin_agents_drafts_create(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.agents_overlay_bff import create_agents_draft_admin

    try:
        return _json(create_agents_draft_admin(runtime, auth, body))
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_agents_drafts_update(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.agents_overlay_bff import update_agents_draft_admin
    from ts_harness.workflows.agents_overlay_store import ConflictError as AgentsConflictError

    try:
        return _json(
            update_agents_draft_admin(
                runtime, auth, request.path_params["draft_id"], body
            )
        )
    except AgentsConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_agents_drafts_validate(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.agents_overlay_bff import validate_agents_draft_admin

    try:
        return _json(
            validate_agents_draft_admin(
                runtime,
                request.path_params["draft_id"],
                tenant_id=auth.tenant_id,
            )
        )
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_agents_activate(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.agents_overlay_bff import activate_agents_admin
    from ts_harness.workflows.agents_overlay_store import ConflictError as AgentsConflictError

    try:
        result = activate_agents_admin(runtime, auth, body)
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="agents_overlay_activated",
            actor_id=auth.actor_id,
            payload={"draft_id": body.get("draft_id")},
        )
        return _json(result)
    except AgentsConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_mcp_catalog_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.mcp_settings_bff import get_mcp_catalog_admin

    return _json(get_mcp_catalog_admin())


async def admin_mcp_server_test(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "edit_allowlists")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.mcp_settings_bff import probe_mcp_server_admin

    try:
        return _json(probe_mcp_server_admin(runtime, request.path_params["name"]))
    except KeyError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_runtime_settings_activate(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.runtime_settings_bff import (
        activate_runtime_settings_page_admin,
    )
    from ts_harness.workflows.runtime_settings_store import ConflictError

    try:
        result = activate_runtime_settings_page_admin(runtime, auth, body)
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="runtime_settings_page_activated",
            actor_id=auth.actor_id,
            payload={
                "reload_ok": result.get("reload_ok"),
                "governance_change": bool(result.get("governance_change")),
            },
        )
        return _json(result)
    except ConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except PermissionError as exc:
        return _json({"error": str(exc)}, 403)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


# ── Program definition admin BFF ──────────────────────────────────────────────


async def admin_program_definitions_list(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.program_definitions_bff import (
        list_program_definitions_admin,
    )

    try:
        return _json(list_program_definitions_admin(runtime))
    except Exception as exc:
        log_event("admin_program_definitions_list_error", error=str(exc))
        return _json({"error": str(exc)}, 500)


async def admin_program_definitions_catalog(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.program_definitions_bff import (
        program_definition_catalog_admin,
    )

    try:
        return _json(program_definition_catalog_admin(runtime))
    except Exception as exc:
        log_event("admin_program_definitions_catalog_error", error=str(exc))
        return _json({"error": str(exc)}, 500)


async def admin_program_definitions_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.program_definitions_bff import (
        get_program_definition_admin,
    )

    try:
        return _json(
            get_program_definition_admin(request.path_params["program_type"])
        )
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_program_definition_drafts_create(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.program_definitions_bff import (
        create_program_definition_draft_admin,
    )

    try:
        result = create_program_definition_draft_admin(runtime, auth, body)
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="program_definition_draft_created",
            actor_id=auth.actor_id,
            payload=result["draft"],
        )
        return _json(result, status=201)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_program_definition_drafts_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.program_definitions_bff import (
        get_program_definition_draft_admin,
    )

    try:
        return _json(
            get_program_definition_draft_admin(request.path_params["draft_id"])
        )
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_program_definition_drafts_update(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.program_definitions_bff import (
        update_program_definition_draft_admin,
    )

    try:
        result = update_program_definition_draft_admin(
            runtime,
            auth,
            request.path_params["draft_id"],
            body,
        )
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="program_definition_draft_updated",
            actor_id=auth.actor_id,
            payload=result["draft"],
        )
        return _json(result)
    except ConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_program_definition_drafts_delete(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.program_definitions_bff import (
        delete_program_definition_draft_admin,
    )

    try:
        result = delete_program_definition_draft_admin(
            request.path_params["draft_id"]
        )
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="program_definition_draft_deleted",
            actor_id=auth.actor_id,
            payload=result,
        )
        return _json(result)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_program_definition_drafts_validate(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request, allow_empty=True)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.program_definitions_bff import (
        validate_program_definition_draft_admin,
    )

    try:
        result = validate_program_definition_draft_admin(
            runtime,
            request.path_params["draft_id"],
            body,
        )
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="program_definition_validated",
            actor_id=auth.actor_id,
            payload={
                "draft_id": request.path_params["draft_id"],
                "valid": result["valid"],
                "issue_count": len(result["issues"]),
            },
        )
        return _json(result)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_program_definition_drafts_activate(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.program_definitions_bff import (
        activate_program_definition_draft_admin,
    )

    try:
        result = activate_program_definition_draft_admin(
            runtime,
            auth,
            request.path_params["draft_id"],
            body,
        )
        reload_ok = None
        try:
            registry = get_program_definition_registry()
            reload_definitions = getattr(registry, "reload_definitions", None)
            if callable(reload_definitions):
                reload_ok = bool(reload_definitions())
        except Exception as exc:
            reload_ok = False
            reload_error = str(exc)
        else:
            reload_error = ""
        if reload_ok is False:
            payload = {
                "draft_id": request.path_params["draft_id"],
                "program_type": result.get("active", {}).get("program_type"),
                "definition_version": result.get("active", {}).get(
                    "definition_version"
                ),
                "reload": {"ok": False},
            }
            if reload_error:
                payload["error"] = reload_error
            log_event("program_definition_reload_failed", **payload)
            runtime.audit.append(
                tenant_id=auth.tenant_id,
                action_type="program_definition_reload_failed",
                actor_id=auth.actor_id,
                payload=payload,
            )
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="program_definition_activated",
            actor_id=auth.actor_id,
            payload=result["active"],
        )
        if reload_ok is False:
            result = {
                **result,
                "reload_ok": False,
                "reload": {"ok": False},
                "warning": "program definition activated but reload failed",
            }
        elif reload_ok is True:
            result = {**result, "reload_ok": True, "reload": {"ok": True}}
        return _json(result)
    except ConflictError as exc:
        return _json({"error": str(exc)}, 409)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_program_definition_start(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        from ts_harness.gateway.program_definitions_bff import program_action_permission

        program_type = request.path_params["program_type"]
        entry = str(body.get("entry_phase") or body.get("entry_node") or "").strip()
        if not entry:
            from ts_harness.gateway.program_definitions_bff import _resolve_program_definition

            try:
                definition = _resolve_program_definition(program_type)
                startable = list(definition.get("startable_nodes") or [])
                entry = str(startable[0] if startable else "")
            except Exception:
                entry = ""
        _require_admin(runtime, auth, program_action_permission(program_type, entry or None))
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.program_definitions_bff import (
        start_program_definition_admin,
    )

    program_type = request.path_params["program_type"]
    try:
        try:
            service = get_delivery_program_service()
        except Exception as exc:
            return _json(
                {
                    "error": f"delivery program service unavailable: {exc}",
                },
                503,
            )
        try:
            registry = get_program_definition_registry()
        except Exception:
            registry = None
        if registry is not None:
            reload_if_changed = getattr(registry, "reload_if_changed", None)
            if callable(reload_if_changed):
                if not reload_if_changed():
                    return _json(
                        {
                            "error": (
                                "program definition reload failed; "
                                "registry may be stale"
                            )
                        },
                        503,
                    )
        result = start_program_definition_admin(
            runtime,
            auth,
            program_type,
            body,
            service,
        )
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="program_definition_started",
            actor_id=auth.actor_id,
            payload={
                "program_type": program_type,
                "program_id": result.get("program_id"),
                "workflow_id": result.get("workflow_id"),
                "session_id": result.get("session_id"),
            },
        )
        return _json(result)
    except FileNotFoundError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


# ── Cron admin CRUD ───────────────────────────────────────────────────────────


async def admin_cron_jobs_list(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.admin_bff import list_cron_jobs_admin

    return _json(list_cron_jobs_admin(runtime))


async def admin_cron_jobs_create(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body if isinstance(body, dict) else {})
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.admin_bff import create_cron_job_admin

    try:
        result = create_cron_job_admin(runtime, body if isinstance(body, dict) else {})
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="cron_job_created",
            actor_id=auth.actor_id,
            payload=result,
        )
        return _json(result, status=201)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_cron_jobs_update(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body if isinstance(body, dict) else {})
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.admin_bff import update_cron_job_admin

    name = request.path_params["name"]
    try:
        result = update_cron_job_admin(runtime, name, body if isinstance(body, dict) else {})
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="cron_job_updated",
            actor_id=auth.actor_id,
            payload=result,
        )
        return _json(result)
    except KeyError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


async def admin_cron_jobs_delete(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.admin_bff import delete_cron_job_admin

    name = request.path_params["name"]
    try:
        result = delete_cron_job_admin(runtime, name)
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="cron_job_deleted",
            actor_id=auth.actor_id,
            payload=result,
        )
        return _json(result)
    except KeyError as exc:
        return _json({"error": str(exc)}, 404)


async def admin_cron_jobs_trigger(request: Request) -> Response:
    try:
        _check_origin(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        auth = _auth(request, body if isinstance(body, dict) else {})
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    name = request.path_params["name"]
    try:
        result = runtime.scheduler.trigger_job(name)
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="cron_job_triggered",
            actor_id=auth.actor_id,
            payload=result,
        )
        return _json(result)
    except KeyError as exc:
        return _json({"error": str(exc)}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)


# ── Program builder ───────────────────────────────────────────────────────────


async def admin_program_builder_message(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.program_builder_bff import handle_builder_message

    try:
        return _json(handle_builder_message(runtime, auth, body))
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except KeyError as exc:
        return _json({"error": str(exc).strip("'")}, 404)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    except Exception as exc:
        log_event("program_builder_message_error", error=str(exc))
        return _json({"error": str(exc)}, 500)


async def admin_program_builder_sessions_list(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.program_builder_bff import list_builder_sessions

    return _json(list_builder_sessions(auth))


async def admin_program_builder_session_get(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.program_builder_bff import builder_session_state

    try:
        return _json(builder_session_state(runtime, auth, request.path_params["session_id"]))
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except KeyError as exc:
        return _json({"error": str(exc).strip("'")}, 404)


async def admin_program_builder_activate(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await _json_object_body(request)
        auth = _auth(request, body)
        runtime = get_runtime()
        _require_admin(runtime, auth)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except _BadJsonBody as exc:
        return _json({"error": str(exc)}, 400)
    except Exception:
        return _json({"error": "invalid json"}, 400)
    from ts_harness.gateway.program_builder_bff import (
        BuilderNotReadyError,
        activate_builder_bundle,
    )

    try:
        result = activate_builder_bundle(runtime, auth, request.path_params["session_id"], body)
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except KeyError as exc:
        return _json({"error": str(exc).strip("'")}, 404)
    except BuilderNotReadyError as exc:
        return _json({"error": str(exc), "state": exc.state}, 409)
    except ValueError as exc:
        return _json({"error": str(exc)}, 400)
    return _json(result)


# ── Sessions ──────────────────────────────────────────────────────────────────


async def admin_sessions_list(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.admin_bff import list_sessions_admin

    channel = request.query_params.get("channel", "web")
    return _json(list_sessions_admin(runtime, auth, channel=channel or None))


async def admin_sessions_search(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        runtime = get_runtime()
        _require_admin(runtime, auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.admin_bff import search_sessions_admin

    q = request.query_params.get("q", "")
    if not str(q or "").strip():
        return _json({"hits": []})
    try:
        limit = int(request.query_params.get("limit", "20") or "20")
    except ValueError:
        return _json({"error": "invalid limit"}, 400)
    result = search_sessions_admin(auth, q=q, limit=limit)
    runtime.audit.append(
        tenant_id=auth.tenant_id,
        action_type="sessions_searched",
        actor_id=auth.actor_id,
        payload={
            "query_length": len(q),
            "hit_count": len(result.get("hits") or []),
        },
    )
    return _json(result)


async def admin_sessions_messages(request: Request) -> Response:
    try:
        _check_origin(request)
        auth = _auth(request, {})
        _require_admin(get_runtime(), auth, "view")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    from ts_harness.gateway.admin_bff import session_messages_admin

    session_id = request.path_params["session_id"]
    try:
        return _json(session_messages_admin(session_id, auth))
    except KeyError:
        return _json({"error": "not found"}, 404)


# ── POST /admin/approvals/{id}/decide ────────────────────────────────────────


async def admin_approvals_decide(request: Request) -> Response:
    try:
        _check_origin(request)
        body = await request.json()
        auth = _auth(request, body)
        _require_admin(get_runtime(), auth, "approve_gate")
    except AuthError as exc:
        return _json({"error": str(exc)}, exc.status)
    except Exception:
        return _json({"error": "invalid json"}, 400)

    approval_id = request.path_params["id"]
    approved = bool(body.get("approved", False))
    try:
        approval, resume = decide_and_resume(
            get_approval_queue(),
            get_memory_client(),
            approval_id,
            approved=approved,
            actor_id=auth.actor_id,
            roles=list(auth.roles or []),
        )
    except KeyError:
        return _json({"error": "not found"}, 404)
    except ApprovalAlreadyDecided as exc:
        return _json({"error": str(exc), "status": exc.approval.status}, 409)
    except Exception as exc:
        log_event("approvals_decide_error", approval_id=approval_id, error=str(exc))
        return _json({"error": str(exc)}, 500)

    payload = dict(approval.__dict__)
    if resume is not None:
        payload["resume"] = resume
    return _json(payload)


# ── POST /webhooks/github ─────────────────────────────────────────────────────


async def github_webhook(request: Request) -> Response:
    raw = await request.body()
    try:
        body: dict[str, Any] = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return _json({"error": "invalid json"}, 400)

    from ts_harness.gateway.github_webhook import (
        delivery_already_handled,
        delivery_host,
        handle_github_webhook,
        remember_delivery,
        verify_delivery,
        webhook_secrets,
    )

    host = delivery_host(dict(request.headers))
    secrets = await asyncio.to_thread(webhook_secrets, host)
    delivery_id = request.headers.get("x-github-delivery", "").strip()
    event_name = request.headers.get("x-github-event", "")
    if secrets:
        signature = request.headers.get("x-hub-signature-256", "")
        if not verify_delivery(raw, signature, secrets):
            return _json({"error": "invalid github signature"}, 401)
        if not delivery_id:
            return _json({"error": "missing X-GitHub-Delivery"}, 400)
        if await asyncio.to_thread(delivery_already_handled, delivery_id):
            return _json({"status": "duplicate", "delivery_id": delivery_id})
    else:
        # Fall back to gateway token auth when webhook secret is unset (dev/smoke).
        try:
            _check_origin(request)
            _auth(request, body)
        except AuthError as exc:
            return _json({"error": str(exc)}, exc.status)

    runtime = get_runtime()
    try:
        result = await asyncio.to_thread(
            handle_github_webhook,
            body,
            tenant_id=str(body.get("tenant_id") or runtime.config.gateway.default_tenant_id),
            event_name=event_name,
            host=host,
        )
        audit_payload = {
            "event": event_name,
            "kind": result.get("kind"),
            "status": result.get("status"),
            "pr_url": result.get("pr_url"),
        }
        if result.get("merged_by"):
            audit_payload["merged_by"] = result["merged_by"]
            audit_payload["merge_sha"] = result.get("merge_sha")
        runtime.audit.append(
            tenant_id=str(body.get("tenant_id") or runtime.config.gateway.default_tenant_id),
            action_type="github_webhook",
            actor_id="github-webhook",
            payload=audit_payload,
        )
        if delivery_id:
            await asyncio.to_thread(remember_delivery, delivery_id, host=host, event=event_name)
        return _json(result)
    except Exception as exc:
        log_event("github_webhook_error", error=str(exc))
        return _json({"error": str(exc)}, 500)


# ── POST /channels/{channel} and /channels/{channel}/{path:path} ──────────────


async def channel_webhook(request: Request) -> Response:
    channel = request.path_params["channel"]
    raw = await request.body()
    try:
        body: dict[str, Any] = json.loads(raw or b"{}")
    except json.JSONDecodeError:
        return _json({"error": "invalid json"}, 400)

    runtime = get_runtime()
    url_path: str = request.url.path

    try:
        _check_origin(request)
        if url_path.endswith("/send-test"):
            auth = _auth(request, body)
        else:
            auth = await _auth_channel(request, channel, body, raw)
    except AuthError as exc:
        get_runtime().audit.append(
            tenant_id="unknown",
            action_type="auth_failure",
            actor_id="unknown",
            payload={"error": str(exc), "channel": channel},
        )
        return _json({"error": str(exc)}, exc.status)

    if url_path.endswith("/send-test"):
        result = get_channel_router().send(
            channel,
            {
                "channel": body.get("channel") or body.get("to") or "",
                "text": body.get("text", "TS Harness release gate"),
                "thread_id": body.get("thread_id"),
            },
            tenant_id=body.get("tenant_id") or runtime.config.gateway.default_tenant_id,
        )
        return _json(result)

    try:
        result = runtime.handle_channel_webhook(channel, body, auth)
        return _json(result)
    except Exception as exc:
        log_event("channel_webhook_error", channel=channel, error=str(exc))
        return _json({"error": str(exc)}, 500)


# ── SCIM v2 ───────────────────────────────────────────────────────────────────

_SCIM_MEDIA_TYPE = "application/scim+json"
_SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"


def _scim_json(data: Any, status: int = 200) -> Response:
    return Response(json.dumps(data, default=str), status_code=status, media_type=_SCIM_MEDIA_TYPE)


def _scim_error(detail: str, status: int) -> Response:
    return _scim_json({"schemas": [_SCIM_ERROR_SCHEMA], "detail": detail, "status": str(status)}, status)


def _scim_denied(request: Request) -> Response | None:
    """SCIM uses its own bearer token (``TS_HARNESS_SCIM_TOKEN``); unset means SCIM is off."""
    expected = os.environ.get("TS_HARNESS_SCIM_TOKEN", "").strip()
    if not expected:
        return _scim_error("SCIM provisioning is not enabled", 404)
    presented = str(request.headers.get("authorization") or "")
    if not hmac.compare_digest(presented.encode(), f"Bearer {expected}".encode()):
        return _scim_error("invalid SCIM token", 401)
    return None


async def scim_resource(request: Request) -> Response:
    denied = _scim_denied(request)
    if denied is not None:
        return denied
    resource = request.path_params["resource"]
    resource_id = request.path_params.get("id")
    body: dict[str, Any] | None = None
    if request.method in {"POST", "PUT", "PATCH"}:
        try:
            body = await request.json()
        except Exception:
            return _scim_error("invalid JSON", 400)
        if not isinstance(body, dict):
            return _scim_error("request body must be a JSON object", 400)
    query = {key: request.query_params.get(key) or "" for key in ("filter", "startIndex", "count")}
    path = resource if not resource_id else f"{resource}/{resource_id}"
    try:
        status, data = await asyncio.to_thread(
            get_memory_client().scim_request,
            request.method,
            path,
            tenant_id=str(get_runtime().config.gateway.default_tenant_id or "default"),
            query=query,
            body=body,
        )
    except Exception as exc:  # noqa: BLE001
        return _scim_error(f"directory unavailable: {exc}", 502)
    if request.method == "DELETE" and status < 400:
        return Response(status_code=204)
    if status < 400:
        log_event("scim_provisioning", method=request.method, resource=resource, status=status)
    return _scim_json(data, status)


async def scim_service_provider_config(request: Request) -> Response:
    denied = _scim_denied(request)
    if denied is not None:
        return denied
    return _scim_json(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": 500},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {"type": "oauthbearertoken", "name": "Bearer token", "description": "TS_HARNESS_SCIM_TOKEN"}
            ],
        }
    )


# ── Application assembly ──────────────────────────────────────────────────────

routes = [
    # Read-only probes
    Route("/healthz", endpoint=healthz, methods=["GET"]),
    Route("/metrics", endpoint=metrics, methods=["GET"]),
    Route("/skills/doctor", endpoint=skills_doctor, methods=["GET"]),
    Route("/mcp/tools", endpoint=mcp_tools, methods=["GET"]),
    Route("/connectors/google-workspace/tools", endpoint=connector_google_workspace_tools, methods=["GET"]),
    Route("/oauth/google/start", endpoint=oauth_google_start, methods=["GET"]),
    Route("/oauth/google/callback", endpoint=oauth_google_callback, methods=["GET"]),
    Route("/memory/smoke", endpoint=memory_smoke, methods=["GET"]),
    # Workflow
    Route("/workflow/dispatch", endpoint=workflow_dispatch, methods=["POST"]),
    Route("/workflow/present-artifact", endpoint=workflow_present_artifact, methods=["POST"]),
    Route("/workflow/cancel", endpoint=workflow_cancel, methods=["POST"]),
    # Session
    Route("/session/message", endpoint=session_message, methods=["POST"]),
    # Scheduler / cron
    Route("/cron/trigger", endpoint=cron_trigger, methods=["POST"]),
    # Sandbox
    Route("/sandbox/deny-check", endpoint=sandbox_deny_check, methods=["POST"]),
    # Admin
    Route("/admin/secrets/reload", endpoint=admin_secrets_reload, methods=["POST"]),
    Route("/admin/skills/drafts", endpoint=admin_skills_drafts_list, methods=["GET"]),
    Route("/admin/skills/drafts", endpoint=admin_skills_drafts_create, methods=["POST"]),
    Route(
        "/admin/skills/drafts/{skill_id}/files/{file_path:path}",
        endpoint=admin_skills_drafts_file,
        methods=["GET"],
    ),
    Route(
        "/admin/skills/drafts/{skill_id:path}/approve",
        endpoint=admin_skills_drafts_approve,
        methods=["POST"],
    ),
    Route(
        "/admin/skills/drafts/{skill_id:path}/reject",
        endpoint=admin_skills_drafts_reject,
        methods=["POST"],
    ),
    Route("/admin/skills/drafts/{skill_id:path}", endpoint=admin_skills_drafts_get, methods=["GET"]),
    Route("/admin/skills", endpoint=admin_skills_catalog_list, methods=["GET"]),
    Route("/admin/skills/{skill_id:path}", endpoint=admin_skills_catalog_get, methods=["GET"]),
    Route("/admin/approvals", endpoint=admin_approvals_list, methods=["GET"]),
    Route("/admin/approvals/{id}", endpoint=admin_approvals_get, methods=["GET"]),
    Route("/admin/approvals/{id}/decide", endpoint=admin_approvals_decide, methods=["POST"]),
    Route("/admin/auth/session", endpoint=admin_auth_session, methods=["POST"]),
    Route("/admin/auth/logout", endpoint=admin_auth_logout, methods=["POST"]),
    Route("/admin/auth/config", endpoint=admin_auth_config, methods=["GET"]),
    Route("/admin/inbox", endpoint=admin_inbox, methods=["GET"]),
    Route("/admin/programs", endpoint=admin_programs_list, methods=["GET"]),
    Route("/admin/programs/{id}/status", endpoint=admin_programs_status, methods=["GET"]),
    Route("/admin/programs/{id}/evidence", endpoint=admin_programs_evidence, methods=["GET"]),
    Route("/admin/programs/{id}/phases/{phase}/activity", endpoint=admin_programs_phase_activity, methods=["GET"]),
    Route("/admin/programs/{id}/advance", endpoint=admin_programs_advance, methods=["POST"]),
    Route(
        "/admin/programs/{id}/department-memory",
        endpoint=admin_programs_department_memory,
        methods=["POST"],
    ),
    Route("/admin/programs/{id}/cancel", endpoint=admin_programs_cancel, methods=["POST"]),
    Route("/admin/programs/{id}/access", endpoint=admin_programs_access_get, methods=["GET"]),
    Route("/admin/programs/{id}/access", endpoint=admin_programs_access_grant, methods=["POST"]),
    Route(
        "/admin/programs/{id}/access/{principal_type}/{principal_id:path}",
        endpoint=admin_programs_access_revoke,
        methods=["DELETE"],
    ),
    Route("/admin/programs/{id}/visibility", endpoint=admin_programs_visibility, methods=["PATCH"]),
    Route("/admin/directory/search", endpoint=admin_directory_search, methods=["GET"]),
    Route("/admin/programs/{id}/links/{to_id}", endpoint=admin_programs_unlink, methods=["DELETE"]),
    Route("/admin/programs/{id}/links", endpoint=admin_programs_link, methods=["POST"]),
    Route("/admin/programs/{id}", endpoint=admin_programs_get, methods=["GET"]),
    Route("/admin/programs/{id}", endpoint=admin_programs_patch, methods=["PATCH"]),
    Route("/admin/workflows/approve", endpoint=admin_workflows_approve, methods=["POST"]),
    Route("/admin/workflows/decide", endpoint=admin_workflows_decide, methods=["POST"]),
    Route("/admin/workflows/reject", endpoint=admin_workflows_reject, methods=["POST"]),
    Route("/admin/workflow-definitions", endpoint=admin_workflow_definitions_list, methods=["GET"]),
    Route(
        "/admin/workflow-definitions/catalog",
        endpoint=admin_workflow_definitions_catalog,
        methods=["GET"],
    ),
    Route(
        "/admin/workflow-definitions/drafts",
        endpoint=admin_workflow_definition_drafts_create,
        methods=["POST"],
    ),
    Route(
        "/admin/workflow-definitions/drafts/{draft_id}/validate",
        endpoint=admin_workflow_definition_drafts_validate,
        methods=["POST"],
    ),
    Route(
        "/admin/workflow-definitions/drafts/{draft_id}/activate",
        endpoint=admin_workflow_definition_drafts_activate,
        methods=["POST"],
    ),
    Route(
        "/admin/workflow-definitions/drafts/{draft_id}",
        endpoint=admin_workflow_definition_drafts_get,
        methods=["GET"],
    ),
    Route(
        "/admin/workflow-definitions/drafts/{draft_id}",
        endpoint=admin_workflow_definition_drafts_update,
        methods=["PUT"],
    ),
    Route(
        "/admin/workflow-definitions/drafts/{draft_id}",
        endpoint=admin_workflow_definition_drafts_delete,
        methods=["DELETE"],
    ),
    Route(
        "/admin/workflow-definitions/{workflow_type}/start",
        endpoint=admin_workflow_definition_start,
        methods=["POST"],
    ),
    Route(
        "/admin/workflow-definitions/{workflow_type}",
        endpoint=admin_workflow_definitions_get,
        methods=["GET"],
    ),
    Route(
        "/admin/routing-definitions",
        endpoint=admin_routing_definitions_get,
        methods=["GET"],
    ),
    Route(
        "/admin/routing-definitions/drafts",
        endpoint=admin_routing_definition_drafts_create,
        methods=["POST"],
    ),
    Route(
        "/admin/routing-definitions/drafts/{draft_id}/validate",
        endpoint=admin_routing_definition_drafts_validate,
        methods=["POST"],
    ),
    Route(
        "/admin/routing-definitions/drafts/{draft_id}/activate",
        endpoint=admin_routing_definition_drafts_activate,
        methods=["POST"],
    ),
    Route(
        "/admin/routing-definitions/drafts/{draft_id}",
        endpoint=admin_routing_definition_drafts_get,
        methods=["GET"],
    ),
    Route(
        "/admin/routing-definitions/drafts/{draft_id}",
        endpoint=admin_routing_definition_drafts_update,
        methods=["PUT"],
    ),
    Route(
        "/admin/mcp-settings",
        endpoint=admin_mcp_settings_get,
        methods=["GET"],
    ),
    Route(
        "/admin/mcp-settings/activate",
        endpoint=admin_mcp_settings_activate,
        methods=["POST"],
    ),
    Route(
        "/admin/mcp-settings/drafts",
        endpoint=admin_mcp_settings_drafts_create,
        methods=["POST"],
    ),
    Route(
        "/admin/mcp-settings/drafts/{draft_id}/validate",
        endpoint=admin_mcp_settings_drafts_validate,
        methods=["POST"],
    ),
    Route(
        "/admin/mcp-settings/drafts/{draft_id}",
        endpoint=admin_mcp_settings_drafts_update,
        methods=["PUT"],
    ),
    Route(
        "/internal/coding-jobs/{job_id}/github-token",
        endpoint=internal_coding_job_github_token,
        methods=["POST"],
    ),
    Route("/admin/github", endpoint=admin_github, methods=["GET"]),
    Route("/admin/github/sync", endpoint=admin_github_sync, methods=["POST"]),
    Route("/admin/projects", endpoint=admin_projects, methods=["GET", "POST"]),
    Route("/admin/projects/{project_id}", endpoint=admin_project, methods=["GET", "PUT"]),
    Route(
        "/admin/projects/{project_id}/registries",
        endpoint=admin_project_registries,
        methods=["GET", "POST"],
    ),
    Route(
        "/admin/projects/{project_id}/registries/{registry_id}",
        endpoint=admin_project_registry,
        methods=["PUT", "DELETE"],
    ),
    Route(
        "/admin/projects/{project_id}/registries/{registry_id}/test",
        endpoint=admin_project_registry_test,
        methods=["POST"],
    ),
    Route("/admin/agents", endpoint=admin_agents_get, methods=["GET"]),
    Route("/admin/agents/drafts", endpoint=admin_agents_drafts_create, methods=["POST"]),
    Route(
        "/admin/agents/drafts/{draft_id}",
        endpoint=admin_agents_drafts_update,
        methods=["PUT"],
    ),
    Route(
        "/admin/agents/drafts/{draft_id}/validate",
        endpoint=admin_agents_drafts_validate,
        methods=["POST"],
    ),
    Route("/admin/agents/activate", endpoint=admin_agents_activate, methods=["POST"]),
    Route(
        "/admin/mcp/catalog",
        endpoint=admin_mcp_catalog_get,
        methods=["GET"],
    ),
    Route(
        "/admin/mcp/servers/{name}/test",
        endpoint=admin_mcp_server_test,
        methods=["POST"],
    ),
    Route(
        "/admin/runtime-settings",
        endpoint=admin_runtime_settings_get,
        methods=["GET"],
    ),
    Route(
        "/admin/runtime-settings/activate",
        endpoint=admin_runtime_settings_activate,
        methods=["POST"],
    ),
    Route(
        "/admin/runtime-settings/drafts",
        endpoint=admin_runtime_settings_drafts_create,
        methods=["POST"],
    ),
    Route(
        "/admin/runtime-settings/drafts/{draft_id}/validate",
        endpoint=admin_runtime_settings_drafts_validate,
        methods=["POST"],
    ),
    Route(
        "/admin/runtime-settings/drafts/{draft_id}/activate",
        endpoint=admin_runtime_settings_drafts_activate,
        methods=["POST"],
    ),
    Route(
        "/admin/runtime-settings/drafts/{draft_id}",
        endpoint=admin_runtime_settings_drafts_get,
        methods=["GET"],
    ),
    Route(
        "/admin/runtime-settings/drafts/{draft_id}",
        endpoint=admin_runtime_settings_drafts_update,
        methods=["PUT"],
    ),
    Route("/admin/workflow-fragments", endpoint=admin_workflow_fragments_list, methods=["GET"]),
    Route(
        "/admin/workflow-fragments/drafts",
        endpoint=admin_workflow_fragment_drafts_create,
        methods=["POST"],
    ),
    Route(
        "/admin/workflow-fragments/drafts/{draft_id}/validate",
        endpoint=admin_workflow_fragment_drafts_validate,
        methods=["POST"],
    ),
    Route(
        "/admin/workflow-fragments/drafts/{draft_id}/activate",
        endpoint=admin_workflow_fragment_drafts_activate,
        methods=["POST"],
    ),
    Route(
        "/admin/workflow-fragments/drafts/{draft_id}",
        endpoint=admin_workflow_fragment_drafts_get,
        methods=["GET"],
    ),
    Route(
        "/admin/workflow-fragments/drafts/{draft_id}",
        endpoint=admin_workflow_fragment_drafts_update,
        methods=["PUT"],
    ),
    Route(
        "/admin/workflow-fragments/drafts/{draft_id}",
        endpoint=admin_workflow_fragment_drafts_delete,
        methods=["DELETE"],
    ),
    Route(
        "/admin/workflow-fragments/{fragment_id}",
        endpoint=admin_workflow_fragments_get,
        methods=["GET"],
    ),
    Route(
        "/admin/program-definitions",
        endpoint=admin_program_definitions_list,
        methods=["GET"],
    ),
    Route(
        "/admin/program-definitions/catalog",
        endpoint=admin_program_definitions_catalog,
        methods=["GET"],
    ),
    Route(
        "/admin/program-definitions/drafts",
        endpoint=admin_program_definition_drafts_create,
        methods=["POST"],
    ),
    Route(
        "/admin/program-definitions/drafts/{draft_id}/validate",
        endpoint=admin_program_definition_drafts_validate,
        methods=["POST"],
    ),
    Route(
        "/admin/program-definitions/drafts/{draft_id}/activate",
        endpoint=admin_program_definition_drafts_activate,
        methods=["POST"],
    ),
    Route(
        "/admin/program-definitions/drafts/{draft_id}",
        endpoint=admin_program_definition_drafts_get,
        methods=["GET"],
    ),
    Route(
        "/admin/program-definitions/drafts/{draft_id}",
        endpoint=admin_program_definition_drafts_update,
        methods=["PUT"],
    ),
    Route(
        "/admin/program-definitions/drafts/{draft_id}",
        endpoint=admin_program_definition_drafts_delete,
        methods=["DELETE"],
    ),
    Route(
        "/admin/program-builder/message",
        endpoint=admin_program_builder_message,
        methods=["POST"],
    ),
    Route(
        "/admin/program-builder/sessions",
        endpoint=admin_program_builder_sessions_list,
        methods=["GET"],
    ),
    Route(
        "/admin/program-builder/sessions/{session_id}/activate",
        endpoint=admin_program_builder_activate,
        methods=["POST"],
    ),
    Route(
        "/admin/program-builder/sessions/{session_id}",
        endpoint=admin_program_builder_session_get,
        methods=["GET"],
    ),
    Route(
        "/admin/program-definitions/{program_type}/start",
        endpoint=admin_program_definition_start,
        methods=["POST"],
    ),
    Route(
        "/admin/program-definitions/{program_type}",
        endpoint=admin_program_definitions_get,
        methods=["GET"],
    ),
    Route("/admin/workflows/{id}", endpoint=admin_workflows_get, methods=["GET"]),
    Route("/admin/cron/jobs", endpoint=admin_cron_jobs_list, methods=["GET"]),
    Route("/admin/cron/jobs", endpoint=admin_cron_jobs_create, methods=["POST"]),
    Route("/admin/cron/jobs/{name}/trigger", endpoint=admin_cron_jobs_trigger, methods=["POST"]),
    Route("/admin/cron/jobs/{name}", endpoint=admin_cron_jobs_update, methods=["PUT"]),
    Route("/admin/cron/jobs/{name}", endpoint=admin_cron_jobs_delete, methods=["DELETE"]),
    Route("/admin/sessions", endpoint=admin_sessions_list, methods=["GET"]),
    Route("/admin/sessions/search", endpoint=admin_sessions_search, methods=["GET"]),
    Route(
        "/admin/sessions/{session_id}/messages",
        endpoint=admin_sessions_messages,
        methods=["GET"],
    ),
    # SCIM v2
    Route("/scim/v2/ServiceProviderConfig", endpoint=scim_service_provider_config, methods=["GET"]),
    Route("/scim/v2/{resource:str}", endpoint=scim_resource, methods=["GET", "POST"]),
    Route("/scim/v2/{resource:str}/{id:str}", endpoint=scim_resource, methods=["GET", "PUT", "PATCH", "DELETE"]),
    # GitHub merge webhook bridge
    Route("/webhooks/github", endpoint=github_webhook, methods=["POST"]),
    # Channel webhooks — specific sub-paths before the bare channel route
    Mount(
        "/channels",
        routes=[
            Route("/{channel}/{path:path}", endpoint=channel_webhook, methods=["POST"]),
            Route("/{channel}", endpoint=channel_webhook, methods=["POST"]),
        ],
    ),
]

app = Starlette(
    routes=routes,
    middleware=[
        Middleware(
            _TimeoutMiddleware,
            timeout=float(os.environ.get("TS_HARNESS_REQUEST_TIMEOUT", "300")),
            dispatch_timeout=float(
                os.environ.get("TS_HARNESS_DISPATCH_REQUEST_TIMEOUT", "7200")
            ),
        ),
        Middleware(_AdminGuardMiddleware),
        Middleware(_TraceMiddleware),
    ],
)
