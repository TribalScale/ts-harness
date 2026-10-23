"""File tools with workspace path safety adapted from Hermes file_tools.py (MIT)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import os
import posixpath

from ts_harness.tools.policy import ToolPolicyContext


DEVICE_PATHS = {"/dev", "/proc", "/sys"}
SENSITIVE_PREFIXES = ("/etc", "/root", "/var/run")
MAX_READ_CHARS = 100_000
SEARCH_MAX_OBJECTS = 500
SEARCH_MAX_OBJECT_BYTES = 1024 * 1024


def _resolve_base_dir(ctx: ToolPolicyContext) -> Path:
    root = Path(os.environ.get("TS_HARNESS_WORKSPACE_ROOT", "/workspace")) / ctx.agent_id
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _check_candidate(candidate: str) -> None:
    if any(candidate.startswith(prefix) for prefix in SENSITIVE_PREFIXES):
        raise ValueError("sensitive path blocked")
    if any(candidate.startswith(device) for device in DEVICE_PATHS):
        raise ValueError("device path blocked")


def _safe_path(path: str, base: Path) -> Path:
    candidate = (base / path).resolve()
    _check_candidate(str(candidate))
    if base not in candidate.parents and candidate != base:
        raise ValueError("path escapes workspace")
    return candidate


def _object_store(ctx: ToolPolicyContext):
    """The agent's object workspace when an object backend is on, else None."""
    from ts_harness.storage.workspace import ObjectWorkspace, objects_enabled

    return ObjectWorkspace(ctx.agent_id) if objects_enabled() else None


def _safe_rel(path: str, store) -> str:
    """Same checks as ``_safe_path``, on the virtual ``/workspace/{agent}`` path; returns the relative key."""
    base = store.display_path("")
    candidate = posixpath.normpath(posixpath.join(base, str(path or "")))
    _check_candidate(candidate)
    if candidate != base and not candidate.startswith(base + "/"):
        raise ValueError("path escapes workspace")
    return candidate[len(base) + 1:] if candidate != base else ""


def _truncate(content: str) -> str:
    if len(content) > MAX_READ_CHARS:
        return content[:MAX_READ_CHARS] + "\n...[truncated]"
    return content


def handle_read_file(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    store = _object_store(ctx)
    if store is not None:
        rel = _safe_rel(str(args.get("path", "")), store)
        try:
            content = store.read_text(rel) if rel else None
        except FileNotFoundError:
            content = None
        if content is None:
            return {"error": "file not found"}
        return {"path": store.display_path(rel), "content": _truncate(content)}
    base = _resolve_base_dir(ctx)
    target = _safe_path(str(args.get("path", "")), base)
    if not target.exists():
        return {"error": "file not found"}
    return {"path": str(target), "content": _truncate(target.read_text(encoding="utf-8"))}


def handle_write_file(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    store = _object_store(ctx)
    if store is not None:
        rel = _safe_rel(str(args.get("path", "")), store)
        if not rel:
            return {"error": "path is a directory"}
        written = store.write_text(rel, str(args.get("content", "")))
        return {"path": store.display_path(rel), "bytes_written": written}
    base = _resolve_base_dir(ctx)
    target = _safe_path(str(args.get("path", "")), base)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(args.get("content", "")), encoding="utf-8")
    return {"path": str(target), "bytes_written": target.stat().st_size}


def _apply_patch(original: str, args: dict[str, Any]) -> str | None:
    if "old" in args or "new" in args:
        old = str(args.get("old", ""))
        new = str(args.get("new", ""))
    else:
        old = str(args.get("patch", ""))
        new = ""
    if old not in original:
        return None
    return original.replace(old, new, 1)


def handle_patch_file(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    store = _object_store(ctx)
    if store is not None:
        rel = _safe_rel(str(args.get("path", "")), store)
        original = store.read_text_or_none(rel) if rel else None
        if original is None:
            return {"error": "file not found"}
        updated = _apply_patch(original, args)
        if updated is None:
            return {"error": "patch context not found"}
        store.write_text(rel, updated)
        return {"path": store.display_path(rel), "patched": True}
    base = _resolve_base_dir(ctx)
    target = _safe_path(str(args.get("path", "")), base)
    if not target.exists():
        return {"error": "file not found"}
    updated = _apply_patch(target.read_text(encoding="utf-8"), args)
    if updated is None:
        return {"error": "patch context not found"}
    target.write_text(updated, encoding="utf-8")
    return {"path": str(target), "patched": True}


def _first_match(content: str, query: str) -> tuple[int, str] | None:
    for idx, line in enumerate(content.splitlines(), start=1):
        if query in line:
            return idx, line[:300]
    return None


def handle_search_files(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    query = str(args.get("query", ""))
    limit = int(args.get("limit", 50))
    store = _object_store(ctx)
    if store is not None:
        rel = _safe_rel(str(args.get("path", ".")), store)
        if not query:
            return {"matches": []}
        matches: list[dict[str, Any]] = []
        candidates = [f for f in store.list(rel) if f.size <= SEARCH_MAX_OBJECT_BYTES][:SEARCH_MAX_OBJECTS]
        for item in candidates:
            try:
                content = store.read_text(item.path)
            except (UnicodeDecodeError, FileNotFoundError):
                continue
            found = _first_match(content, query)
            if found:
                matches.append({"path": store.display_path(item.path), "line": found[0], "preview": found[1]})
            if len(matches) >= limit:
                break
        return {"matches": matches}
    base = _resolve_base_dir(ctx)
    root = _safe_path(str(args.get("path", ".")), base)
    if not query:
        return {"matches": []}
    matches = []
    paths = [root] if root.is_file() else sorted(root.rglob("*"))
    for path in paths:
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found = _first_match(content, query)
        if found:
            matches.append({"path": str(path), "line": found[0], "preview": found[1]})
        if len(matches) >= limit:
            break
    return {"matches": matches}
