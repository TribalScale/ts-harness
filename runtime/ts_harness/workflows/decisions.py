"""Resolve the human behind a workflow decision (gate, advance, track choice)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class DecisionActor:
    actor_id: str
    email: str | None = None
    roles: tuple[str, ...] = field(default_factory=tuple)

    def as_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if self.email:
            fields["actor_email"] = self.email
        if self.roles:
            fields["actor_roles"] = list(self.roles)
        granted = _permissions_for_actor(self, config=None)
        if granted:
            fields["actor_permissions"] = sorted(granted)
        return fields


def decision_request_fields(
    decision: dict[str, Any] | None,
    workflow: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build approve/reject kwargs for the memory client from hook decision extras."""
    fields = {
        key: value
        for key, value in dict(decision or {}).items()
        if key in {"actor_email", "actor_roles", "reason"} and value
    }
    program_id = workflow.get("program_id") if isinstance(workflow, dict) else None
    if isinstance(program_id, str) and program_id.strip():
        program_id = program_id.strip()
        fields["program_id"] = program_id
    return fields


def _live_config():
    try:
        from ts_harness.gateway.server import get_runtime

        return getattr(get_runtime(), "config", None)
    except Exception:
        return None


def _permissions_for_actor(actor: DecisionActor, config: Any = None) -> set[str]:
    from ts_harness.policy.permissions import effective_bundles, permissions_for_roles

    cfg = config if config is not None else _live_config()
    return permissions_for_roles(actor.roles, effective_bundles(cfg))


def refusal_for_permission(actor: DecisionActor, permission: str, config: Any = None) -> str | None:
    """Deny a resolved human who lacks ``permission``. Unresolved service actors pass."""
    human = bool(actor.email) or str(actor.actor_id or "").startswith("slack:")
    if not human:
        return None
    cfg = config if config is not None else _live_config()
    if permission in _permissions_for_actor(actor, cfg):
        return None
    _audit_permission(actor, permission, allowed=False, resource=permission)
    return f"missing permission {permission}"


def _audit_permission(actor: DecisionActor, permission: str, *, allowed: bool, resource: str) -> None:
    try:
        from ts_harness.gateway.server import get_runtime

        runtime = get_runtime()
    except Exception:
        return
    audit = getattr(runtime, "audit", None)
    if audit is None:
        return
    try:
        audit.append(
            tenant_id="default",
            action_type="permission_denied" if not allowed else "permission_allowed",
            actor_id=actor.actor_id or "unknown",
            payload={"permission": permission, "roles": list(actor.roles), "resource": resource},
        )
    except Exception:
        return


CHANNEL_SOURCES = frozenset({"slack", "discord", "telegram"})


def channel_permission_refusal(
    session_metadata: dict[str, Any] | None,
    *,
    channel: str,
    auth: Any,
    permission: str,
    actor_email: str | None = None,
    config: Any = None,
) -> str | None:
    """Deny unless the human behind this channel reply holds ``permission``.

    Slack, Discord, and Telegram replies authenticate as the webhook, so the
    human comes from the session. The web console is already an authenticated
    user, so the check uses that caller's permission bundle.
    """
    cfg = config if config is not None else _live_config()
    if channel not in CHANNEL_SOURCES:
        from ts_harness.policy.permissions import actor_permissions

        if permission in actor_permissions(auth, cfg):
            return None
        _audit_permission(
            DecisionActor(actor_id=str(getattr(auth, "actor_id", "") or "")),
            permission,
            allowed=False,
            resource=permission,
        )
        return f"missing permission {permission}"
    actor = resolve_decision_actor(
        session_metadata,
        fallback_actor="",
        actor_email=actor_email,
        config=cfg,
    )
    if not actor.email and not actor.actor_id:
        return f"missing permission {permission}"
    if permission in _permissions_for_actor(actor, cfg):
        return None
    _audit_permission(actor, permission, allowed=False, resource=permission)
    return f"missing permission {permission}"


def roles_for_email(email: str | None, config: Any = None) -> list[str]:
    if not email:
        return []
    identity = getattr(config if config is not None else _live_config(), "identity", None)
    roles_map = dict(getattr(identity, "roles", None) or {})
    return [str(role).strip().lower() for role in roles_map.get(email.strip().lower()) or [] if str(role).strip()]


def resolve_decision_actor(
    session_metadata: dict[str, Any] | None,
    *,
    fallback_actor: str,
    actor_email: str | None = None,
    extra_roles: list[str] | None = None,
    config: Any = None,
) -> DecisionActor:
    """Return the channel/API user for a decision, never the agent id when a human is known."""
    from ts_harness.gateway.channels.slack_users import (
        channel_requester_actor_id,
        resolve_slack_user_email,
    )

    meta = dict(session_metadata or {})
    actor_id = channel_requester_actor_id(meta, fallback_actor)
    cfg = config if config is not None else _live_config()
    email = str(actor_email or meta.get("actor_email") or "").strip().lower() or None
    if not email and actor_id.startswith("slack:"):
        try:
            resolved = resolve_slack_user_email(actor_id, config=cfg)
        except Exception:
            resolved = None
        email = str(resolved or "").strip().lower() or None
    roles = set(roles_for_email(email, cfg))
    roles.update(str(role).strip().lower() for role in extra_roles or [] if str(role).strip())
    return DecisionActor(actor_id=actor_id, email=email, roles=tuple(sorted(roles)))
