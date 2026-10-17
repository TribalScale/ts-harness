"""File tools with workspace path safety adapted from Hermes file_tools.py (MIT)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import os

from ts_harness.tools.policy import ToolPolicyContext


DEVICE_PATHS = {"/dev", "/proc", "/sys"}
SENSITIVE_PREFIXES = ("/etc", "/root", "/var/run")


def _resolve_base_dir(ctx: ToolPolicyContext) -> Path:
    root = Path(os.environ.get("TS_HARNESS_WORKSPACE_ROOT", "/workspace")) / ctx.agent_id
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _safe_path(path: str, base: Path) -> Path:
    candidate = (base / path).resolve()
    if any(str(candidate).startswith(prefix) for prefix in SENSITIVE_PREFIXES):
        raise ValueError("sensitive path blocked")
    if any(str(candidate).startswith(device) for device in DEVICE_PATHS):
        raise ValueError("device path blocked")
    if base not in candidate.parents and candidate != base:
        raise ValueError("path escapes workspace")
    return candidate


def handle_read_file(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    base = _resolve_base_dir(ctx)
    target = _safe_path(str(args.get("path", "")), base)
    if not target.exists():
        return {"error": "file not found"}
    content = target.read_text(encoding="utf-8")
    if len(content) > 100_000:
        content = content[:100_000] + "\n...[truncated]"
    return {"path": str(target), "content": content}


def handle_write_file(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    base = _resolve_base_dir(ctx)
    target = _safe_path(str(args.get("path", "")), base)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(args.get("content", "")), encoding="utf-8")
    return {"path": str(target), "bytes_written": target.stat().st_size}


def handle_patch_file(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    base = _resolve_base_dir(ctx)
    target = _safe_path(str(args.get("path", "")), base)
    if not target.exists():
        return {"error": "file not found"}
    original = target.read_text(encoding="utf-8")
    if "old" in args or "new" in args:
        old = str(args.get("old", ""))
        new = str(args.get("new", ""))
    else:
        old = str(args.get("patch", ""))
        new = ""
    if old not in original:
        return {"error": "patch context not found"}
    updated = original.replace(old, new, 1)
    target.write_text(updated, encoding="utf-8")
    return {"path": str(target), "patched": True}


def handle_search_files(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    base = _resolve_base_dir(ctx)
    root = _safe_path(str(args.get("path", ".")), base)
    query = str(args.get("query", ""))
    if not query:
        return {"matches": []}
    matches: list[dict[str, Any]] = []
    paths = [root] if root.is_file() else sorted(root.rglob("*"))
    for path in paths:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for idx, line in enumerate(content.splitlines(), start=1):
            if query in line:
                matches.append({"path": str(path), "line": idx, "preview": line[:300]})
                break
        if len(matches) >= int(args.get("limit", 50)):
            break
    return {"matches": matches}
