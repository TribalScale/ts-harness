"""Health and readiness reporting for the agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ComponentHealth:
    name: str
    status: str  # ok | degraded | unavailable
    detail: str | None = None
    extra: dict[str, Any] | None = None


@dataclass
class HealthReport:
    status: str = "ok"
    components: list[ComponentHealth] = field(default_factory=list)
    version: str = "0.1.0"

    def to_dict(self) -> dict[str, Any]:
        overall = "ok"
        for component in self.components:
            if component.status == "unavailable":
                overall = "unavailable"
                break
            if component.status == "degraded" and overall == "ok":
                overall = "degraded"
        return {
            "status": overall,
            "version": self.version,
            "components": {
                c.name: {"status": c.status, "detail": c.detail, **(c.extra or {})}
                for c in self.components
            },
        }


def _credential_missing(detail: str | None) -> bool:
    text = detail or ""
    return "GOOGLE_WORKSPACE_SA_JSON" in text or "must be set" in text


def _optional_status(ok: bool, *, unconfigured: bool) -> str:
    if ok:
        return "ok"
    if unconfigured:
        return "not_configured"
    return "degraded"


def build_health_report(
    *,
    provider_ok: bool = True,
    memory_ok: bool = True,
    mcp_ok: bool = True,
    google_workspace_ok: bool = True,
    google_workspace_detail: str | None = None,
    channels_ok: bool = True,
    channels_detail: str | None = None,
    sandbox_ok: bool = True,
    sandbox_detail: str | None = None,
    scheduler_ok: bool = True,
    slack_socket: str | None = None,
    slack_socket_detail: str | None = None,
    stale_jobs: list[dict[str, Any]] | None = None,
) -> HealthReport:
    components = [
        ComponentHealth("provider", "ok" if provider_ok else "unavailable"),
        ComponentHealth("memory_api", "ok" if memory_ok else "unavailable"),
        ComponentHealth("mcp", "ok" if mcp_ok else "degraded"),
        ComponentHealth(
            "google_workspace",
            _optional_status(
                google_workspace_ok,
                unconfigured=_credential_missing(google_workspace_detail),
            ),
            detail=google_workspace_detail,
        ),
        ComponentHealth(
            "channels",
            "ok" if channels_ok else "not_configured",
            detail=None if channels_ok else channels_detail,
        ),
        ComponentHealth(
            "sandbox",
            "ok" if sandbox_ok else "not_configured",
            detail=None if sandbox_ok else sandbox_detail,
        ),
        ComponentHealth("scheduler", "ok" if scheduler_ok else "degraded"),
    ]
    if stale_jobs is not None:
        names = [str(job.get("name")) for job in stale_jobs]
        components.append(
            ComponentHealth(
                "scheduler_jobs",
                "degraded" if names else "ok",
                detail=f"stale: {', '.join(names)}" if names else None,
                extra={"stale_count": len(names), "stale_jobs": stale_jobs},
            )
        )
    if slack_socket is not None:
        status = slack_socket
        if status == "connected":
            component_status = "ok"
        elif status in {"disabled", "stopped"}:
            component_status = "ok" if status == "disabled" else "degraded"
        elif status == "connecting":
            component_status = "degraded"
        else:
            component_status = "degraded"
        components.append(
            ComponentHealth("slack_socket", component_status, detail=slack_socket_detail)
        )
    return HealthReport(components=components)
