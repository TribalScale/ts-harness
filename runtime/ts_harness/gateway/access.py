"""Resolve a caller's permissions and enforce one product action."""

from __future__ import annotations

from typing import Any

from ts_harness.gateway.auth import AuthError
from ts_harness.policy.permissions import actor_permissions


def require_permission(runtime: Any, auth: Any, permission: str, *, resource: str = "") -> None:
    granted = actor_permissions(auth, getattr(runtime, "config", None))
    allowed = permission in granted
    if not allowed or permission != "view":
        audit = getattr(runtime, "audit", None)
        append = getattr(audit, "append", None)
        if append is not None:
            try:
                append(
                    tenant_id=str(getattr(auth, "tenant_id", None) or "default"),
                    action_type="permission_denied" if not allowed else "permission_allowed",
                    actor_id=str(getattr(auth, "actor_id", None) or "unknown"),
                    payload={
                        "permission": permission,
                        "roles": [str(role) for role in (getattr(auth, "roles", None) or [])],
                        "resource": resource,
                    },
                )
            except Exception:
                pass
    if not allowed:
        raise AuthError("missing permission", status=403)
