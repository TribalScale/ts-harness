"""Validate and merge managed MCP settings overlays."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import os
import re

import yaml

from ts_harness.repo_paths import repo_root

DEFINITION_TYPE = "mcp_settings"
ALLOWED_TOP = ("definition_type", "definition_version", "servers")
ALLOWED_SERVER = (
    "transport",
    "url",
    "command",
    "args",
    "env",
    "headers",
    "enabled",
    "include_tools",
    "exclude_tools",
)
HTTP_TRANSPORTS = {"streamable_http", "sse"}
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SECRET_REF_RE = re.compile(r"^secret:[A-Za-z0-9_.-]+$")
SENSITIVE_TOKENS = ("TOKEN", "SECRET", "PASSWORD", "KEY", "AUTH")


def _issue(code: str, message: str, *, path: str = "$") -> dict[str, str]:
    return {"severity": "error", "code": code, "path": path, "message": message}


def _unknown_keys(payload: dict[str, Any], allowed: tuple[str, ...], path: str) -> list[dict[str, str]]:
    unknown = sorted(str(key) for key in payload if key not in allowed)
    if not unknown:
        return []
    return [
        _issue(
            "unknown_field",
            f"{path} contains unknown field(s): {', '.join(unknown)}",
            path=path,
        )
    ]


def default_mcp_catalog_path() -> Path:
    override = os.environ.get("TS_HARNESS_MCP_CATALOG", "").strip()
    if override:
        return Path(override)
    return repo_root() / "config" / "mcp-catalog.yaml"


def load_mcp_catalog(path: Path | None = None) -> list[dict[str, Any]]:
    catalog_path = path or default_mcp_catalog_path()
    if not catalog_path.exists():
        return []
    raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
    entries = raw.get("entries") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    out: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "id": str(item.get("id") or "").strip(),
                "display_name": str(item.get("display_name") or item.get("id") or "").strip(),
                "transport": str(item.get("transport") or "stdio").strip(),
                "command": str(item.get("command") or "").strip(),
                "args": [str(arg) for arg in (item.get("args") or [])],
                "url": str(item.get("url") or "").strip(),
                "env_names": [str(name) for name in (item.get("env_names") or [])],
                "notes": str(item.get("notes") or "").strip(),
            }
        )
    return [entry for entry in out if entry["id"]]


def _catalog_match(server: dict[str, Any], catalog: list[dict[str, Any]]) -> bool:
    transport = str(server.get("transport") or "")
    command = str(server.get("command") or "")
    args = [str(arg) for arg in (server.get("args") or [])]
    for entry in catalog:
        if (
            str(entry.get("transport") or "") == transport
            and str(entry.get("command") or "") == command
            and [str(arg) for arg in (entry.get("args") or [])] == args
        ):
            return True
    return False


def _canonicalize_secret_map(raw: Any, *, path: str, issues: list[dict[str, str]]) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_map", f"{path} must be an object", path=path))
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        ref = str(value or "").strip()
        if not name:
            continue
        if not SECRET_REF_RE.match(ref):
            issues.append(
                _issue("invalid_secret_ref", f"{path}.{name} must be secret:<NAME>", path=f"{path}.{name}")
            )
            continue
        out[name] = ref
    return out


def _canonicalize_server(
    name: str,
    raw: Any,
    *,
    catalog: list[dict[str, Any]],
    issues: list[dict[str, str]],
) -> dict[str, Any]:
    path = f"$.servers.{name}"
    if not isinstance(raw, dict):
        issues.append(_issue("invalid_server", f"{path} must be an object", path=path))
        return {}
    issues.extend(_unknown_keys(raw, ALLOWED_SERVER, path))
    transport = str(raw.get("transport") or "").strip()
    out: dict[str, Any] = {}
    if "enabled" in raw:
        out["enabled"] = bool(raw.get("enabled"))
    if "include_tools" in raw:
        tools = raw.get("include_tools") or []
        out["include_tools"] = [str(item) for item in tools] if isinstance(tools, list) else []
    if "exclude_tools" in raw:
        tools = raw.get("exclude_tools") or []
        out["exclude_tools"] = [str(item) for item in tools] if isinstance(tools, list) else []
    if "headers" in raw:
        out["headers"] = _canonicalize_secret_map(raw.get("headers"), path=f"{path}.headers", issues=issues)

    has_command = "command" in raw or "args" in raw
    if "env" in raw:
        if not has_command:
            issues.append(
                _issue(
                    "env_not_allowed",
                    f"{path}.env is only allowed on catalog-matching stdio servers",
                    path=f"{path}.env",
                )
            )
        else:
            out["env"] = _canonicalize_secret_map(raw.get("env"), path=f"{path}.env", issues=issues)

    if has_command:
        out["transport"] = transport or "stdio"
        out["command"] = str(raw.get("command") or "").strip()
        out["args"] = [str(arg) for arg in (raw.get("args") or [])]
        if not _catalog_match(out, catalog):
            issues.append(
                _issue(
                    "catalog_mismatch",
                    f"{path} stdio command must match a reviewed catalog entry",
                    path=path,
                )
            )
        return out

    if transport and transport not in HTTP_TRANSPORTS and "url" in raw:
        issues.append(_issue("invalid_transport", f"{path} transport must be streamable_http or sse", path=path))
    if "url" in raw or transport in HTTP_TRANSPORTS:
        url = str(raw.get("url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            issues.append(_issue("invalid_url", f"{path}.url must be an http(s) URL", path=f"{path}.url"))
        out["transport"] = transport or "streamable_http"
        out["url"] = url
        return out

    if transport:
        out["transport"] = transport
    return out


def validate_mcp_settings_payload(
    payload: dict[str, Any] | None,
    *,
    catalog: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    entries = catalog if catalog is not None else load_mcp_catalog()
    if not isinstance(payload, dict):
        return None, [_issue("invalid_payload", "mcp settings must be an object")]
    issues.extend(_unknown_keys(payload, ALLOWED_TOP, "$"))
    if str(payload.get("definition_type") or "") != DEFINITION_TYPE:
        issues.append(
            _issue(
                "invalid_definition_type",
                "definition_type must be mcp_settings",
                path="$.definition_type",
            )
        )
    version = str(payload.get("definition_version") or "").strip()
    if not version:
        issues.append(_issue("missing_version", "definition_version is required", path="$.definition_version"))
    servers_raw = payload.get("servers") if "servers" in payload else {}
    if servers_raw is None:
        servers_raw = {}
    if not isinstance(servers_raw, dict):
        issues.append(_issue("invalid_servers", "servers must be an object", path="$.servers"))
        servers_raw = {}
    servers: dict[str, Any] = {}
    for raw_name, raw_server in servers_raw.items():
        name = str(raw_name or "").strip()
        if not SERVER_NAME_RE.match(name):
            issues.append(_issue("invalid_server_name", f"invalid server name: {raw_name}", path="$.servers"))
            continue
        servers[name] = _canonicalize_server(name, raw_server, catalog=entries, issues=issues)
    if any(issue.get("severity") == "error" for issue in issues):
        return None, issues
    canonical: dict[str, Any] = {
        "definition_type": DEFINITION_TYPE,
        "definition_version": version,
    }
    if "servers" in payload:
        canonical["servers"] = servers
    return canonical, issues


def _overlay_servers(overlay: dict[str, Any] | None) -> dict[str, Any]:
    payload = overlay if isinstance(overlay, dict) else {}
    servers = payload.get("servers")
    if servers is None and "servers" not in payload:
        return {}
    return dict(servers) if isinstance(servers, dict) else {}


def merge_mcp_servers(yaml_servers: dict[str, Any], overlay: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    merged = {str(name): deepcopy(dict(config or {})) for name, config in dict(yaml_servers or {}).items()}
    for name, config in _overlay_servers(overlay).items():
        key = str(name)
        if not isinstance(config, dict):
            continue
        current = merged.get(key) or {}
        if key in merged:
            updated = deepcopy(current)
            updated.update(deepcopy(config))
            merged[key] = updated
        else:
            merged[key] = deepcopy(config)
    return merged


def annotate_mcp_sources(yaml_servers: dict[str, Any], overlay: dict[str, Any] | None) -> dict[str, str]:
    overlay_names = set(_overlay_servers(overlay))
    names = set(dict(yaml_servers or {})) | overlay_names
    return {name: ("overlay" if name in overlay_names else "inherited") for name in names}


def redact_mcp_server(server: dict[str, Any]) -> dict[str, Any]:
    redacted = deepcopy(dict(server or {}))
    for field in ("env", "headers"):
        values = redacted.get(field)
        if not isinstance(values, dict):
            continue
        cleaned = {}
        for key, value in values.items():
            text = str(value or "")
            if text.startswith("secret:") or any(token in str(key).upper() for token in SENSITIVE_TOKENS):
                cleaned[str(key)] = "***"
            else:
                cleaned[str(key)] = text
        redacted[field] = cleaned
    return redacted


def resolve_mcp_secret_refs(server: dict[str, Any], resolver: Any) -> dict[str, Any]:
    resolved = deepcopy(dict(server or {}))
    if resolver is None:
        return resolved
    for field in ("env", "headers"):
        values = resolved.get(field)
        if not isinstance(values, dict):
            continue
        out: dict[str, str] = {}
        for key, value in values.items():
            text = str(value or "")
            if text.startswith("secret:"):
                out[str(key)] = str(resolver.resolve(text.split(":", 1)[1]) or "")
            else:
                out[str(key)] = text
        resolved[field] = out
    return resolved


def yaml_mcp_baseline(config: Any) -> dict[str, dict[str, Any]]:
    return deepcopy(dict(getattr(config, "mcp_servers", None) or {}))


def apply_mcp_to_config(
    config: Any,
    overlay: dict[str, Any] | None,
    *,
    baseline: dict[str, Any] | None = None,
) -> None:
    source = deepcopy(baseline) if baseline is not None else yaml_mcp_baseline(config)
    config.mcp_servers = merge_mcp_servers(source, overlay or {})


def reload_mcp_settings_into_runtime(runtime: Any) -> bool:
    if runtime is None or not hasattr(runtime, "config"):
        return False
    baseline = getattr(runtime, "_yaml_mcp_baseline", None)
    if baseline is None:
        baseline = yaml_mcp_baseline(runtime.config)
        runtime._yaml_mcp_baseline = baseline
    try:
        from ts_harness.workflows.mcp_settings_store import mcp_settings_store

        active = mcp_settings_store().get_active(DEFINITION_TYPE)
        apply_mcp_to_config(runtime.config, active, baseline=baseline)
        return True
    except Exception:
        apply_mcp_to_config(runtime.config, None, baseline=baseline)
        return False
