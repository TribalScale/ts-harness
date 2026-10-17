"""Permission catalog and role bundles.

Call sites check a permission. A role is a named set of permissions. An unknown
role grants nothing. A configured bundle replaces that role's default.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

DEPARTMENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

PERMISSIONS = frozenset(
    {
        "view",
        "start_program",
        "approve_gate",
        "confirm_delegation",
        "edit_config",
        "edit_models",
        "edit_allowlists",
        "promote_department",
    }
)

_VIEW = frozenset({"view"})
_REQUESTER = frozenset({"view", "start_program"})
_APPROVER = frozenset({"view", "approve_gate", "confirm_delegation"})
_ADMIN = frozenset(PERMISSIONS)

DEFAULT_BUNDLES: dict[str, frozenset[str]] = {
    "viewer": _VIEW,
    "member": _VIEW,
    "requester": _REQUESTER,
    "approver": _APPROVER,
    "admin": _ADMIN,
    "owner": _ADMIN,
}

DEFAULT_SERVICE_PERMISSIONS = frozenset(_ADMIN)


def _clean_role(value: Any) -> str:
    return str(value or "").strip().lower()


def _bundle_permissions(raw_perms: Any) -> frozenset[str]:
    if not isinstance(raw_perms, (list, tuple, set, frozenset)):
        return frozenset()
    return frozenset(_clean_role(item) for item in raw_perms if _clean_role(item))


def effective_bundles(config: Any | None = None) -> dict[str, frozenset[str]]:
    bundles = dict(DEFAULT_BUNDLES)
    identity = getattr(config, "identity", None)
    custom = getattr(identity, "role_bundles", None)
    if not isinstance(custom, dict):
        custom = {}
    replaced: set[str] = set()
    declared: set[str] = set()
    for raw_role, raw_perms in custom.items():
        role = _clean_role(raw_role)
        if not role:
            continue
        perms = _bundle_permissions(raw_perms)
        bundles[role] = perms
        replaced.add(role)
        declared.update(perms)
    for role in ("admin", "owner"):
        if role not in replaced:
            bundles[role] = frozenset(set(bundles[role]) | declared)
    return bundles


def known_permission_names(config: Any | None = None) -> set[str]:
    """Catalog permissions plus any name listed in ``identity.role_bundles``."""
    names = set(PERMISSIONS)
    identity = getattr(config, "identity", None)
    custom = getattr(identity, "role_bundles", None)
    if not isinstance(custom, dict):
        return names
    for raw_perms in custom.values():
        names.update(_bundle_permissions(raw_perms))
    return names


def service_permissions(config: Any | None = None) -> frozenset[str]:
    identity = getattr(config, "identity", None)
    configured = getattr(identity, "service_permissions", None)
    if not isinstance(configured, (list, tuple, set, frozenset)):
        return DEFAULT_SERVICE_PERMISSIONS
    return frozenset(_clean_role(item) for item in configured if _clean_role(item))


def permissions_for_roles(roles: Iterable[Any], bundles: dict[str, frozenset[str]] | None = None) -> set[str]:
    catalog = bundles if bundles is not None else DEFAULT_BUNDLES
    granted: set[str] = set()
    for raw in roles or []:
        bundle = catalog.get(_clean_role(raw))
        if bundle:
            granted.update(bundle)
    return granted


def department_for_email(config: Any | None, email: str | None) -> str | None:
    """Return the one department configured for this email, or None."""
    address = str(email or "").strip().lower()
    if not address:
        return None
    identity = getattr(config, "identity", None)
    departments = getattr(identity, "departments", None)
    if not isinstance(departments, dict):
        return None
    raw = departments.get(address)
    if raw is None:
        return None
    name = str(raw).strip().lower()
    if not DEPARTMENT_NAME_RE.fullmatch(name):
        return None
    return name


def actor_permissions(auth: Any, config: Any | None = None) -> set[str]:
    """Resolve the caller's permission set.

    A static or service token with no roles uses ``service_permissions``.
    OIDC users, including those whose only role is ``member``, use bundles.
    """
    roles = list(getattr(auth, "roles", None) or [])
    provider = str(getattr(auth, "auth_provider", "") or "").strip().lower()
    if not roles and provider in {"static", "service"}:
        return set(service_permissions(config))
    return permissions_for_roles(roles, effective_bundles(config))


def allows(actor_permissions: Iterable[str], required: Iterable[str]) -> bool:
    needed = {_clean_role(item) for item in required if _clean_role(item)}
    if not needed:
        return False
    return bool(set(actor_permissions) & needed)


def covers_role(actor_permissions: Iterable[str], role: str, bundles: dict[str, frozenset[str]] | None = None) -> bool:
    catalog = bundles if bundles is not None else DEFAULT_BUNDLES
    bundle = catalog.get(_clean_role(role))
    if not bundle:
        return False
    return set(bundle) <= set(actor_permissions)


def resolve_actor_roles(
    *,
    claim_roles: Iterable[Any] | None = None,
    groups: Iterable[Any] | None = None,
    email: str | None = None,
    external_id: str | None = None,
    config: Any | None = None,
) -> list[str]:
    """JWT roles, then group bundles, then the email map, then SCIM, else member."""
    names: list[str] = []

    def add(raw: Any) -> None:
        name = _clean_role(raw)
        if name and name not in names:
            names.append(name)

    for role in claim_roles or []:
        add(role)
    identity = getattr(config, "identity", config)
    group_map = getattr(identity, "group_roles", None) or {}
    if not isinstance(group_map, dict):
        group_map = {}
    folded = {_clean_role(key): value for key, value in group_map.items()}
    scim_roles: list[str] = []
    scim_groups: list[str] = []
    if getattr(identity, "scim_enabled", False):
        scim_roles, scim_groups = _scim_roles_and_groups(email, external_id)
    for group in list(groups or []) + scim_groups:
        for role in folded.get(_clean_role(group)) or []:
            add(role)
    email_key = str(email or "").strip().lower()
    email_map = getattr(identity, "roles", None) or {}
    if isinstance(email_map, dict) and email_key:
        for role in email_map.get(email_key) or []:
            add(role)
    for role in scim_roles:
        add(role)
    return names or ["member"]


def _scim_roles_and_groups(email: str | None, external_id: str | None) -> tuple[list[str], list[str]]:
    try:
        from ts_harness.gateway.scim import get_scim_store
    except Exception:
        return [], []
    return get_scim_store().roles_and_groups(email=email, external_id=external_id)


def holds_required_role(actor_roles: Iterable[Any], actor_perms: Iterable[str], required: str, bundles: dict[str, frozenset[str]] | None = None) -> bool:
    """A required role is held by name, or by a permission set that covers its bundle.

    Names that are not a known bundle, such as ``release_manager``, match only
    when the actor's role list contains that name.
    """
    catalog = bundles if bundles is not None else DEFAULT_BUNDLES
    name = _clean_role(required)
    if not name:
        return False
    held = {_clean_role(role) for role in actor_roles if _clean_role(role)}
    if name in held:
        return True
    if name not in catalog:
        return False
    return covers_role(actor_perms, name, catalog)
