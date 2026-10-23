"""Persist and validate file-backed cron jobs for admin CRUD."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator

from croniter import croniter

from ts_harness.skills.aliases import canonical_skill_id


def cron_config_path() -> Path:
    return Path(os.environ.get("TS_HARNESS_CRON_CONFIG", "config/runtime-cron-jobs.json"))


@contextlib.contextmanager
def editing_cron_file(actor: str = "") -> Iterator[None]:
    """Hold the cron file for a read-modify-write; in Postgres mode the change commits as one shared-tree push."""
    from ts_harness.storage.shared_tree import cron_tree

    tree = cron_tree()
    if tree is None:
        yield
        return
    with tree.transaction("cron-file", [cron_config_path().name], actor=actor):
        yield


def load_cron_file(path: Path | None = None) -> list[dict[str, Any]]:
    if path is None:
        from ts_harness.storage.shared_tree import sync_cron

        sync_cron()
    cfg = path or cron_config_path()
    if not cfg.exists():
        return []
    data = json.loads(cfg.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [dict(item) for item in data]
    return [dict(item) for item in list(data.get("jobs") or [])]


def _write_cron_file(jobs: list[dict[str, Any]], cfg: Path) -> None:
    cfg.parent.mkdir(parents=True, exist_ok=True)
    payload = {"jobs": jobs}
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(cfg.parent), delete=False
    ) as handle:
        handle.write(text)
        tmp = Path(handle.name)
    tmp.replace(cfg)


def save_cron_file(jobs: list[dict[str, Any]], path: Path | None = None) -> Path:
    cfg = path or cron_config_path()
    if path is None:
        with editing_cron_file():
            _write_cron_file(jobs, cfg)
    else:
        _write_cron_file(jobs, cfg)
    return cfg


def validate_cron_job(raw: dict[str, Any], *, existing_names: set[str] | None = None) -> dict[str, Any]:
    name = str(raw.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    if ":" in name:
        raise ValueError("name must not contain ':'")
    if existing_names is not None and name in existing_names:
        raise ValueError(f"cron job already exists: {name}")
    kind = str(raw.get("kind") or "cron").strip() or "cron"
    if kind not in {"cron", "system"}:
        raise ValueError("kind must be cron or system")
    handler = str(raw.get("handler") or "").strip()
    agent_id = str(raw.get("agent_id") or "").strip()
    if not agent_id and kind == "system":
        agent_id = "ops-agent"
    if not agent_id:
        raise ValueError("agent_id is required")
    program: dict[str, Any] | None = None
    if handler == "start_program":
        if kind != "system":
            raise ValueError("start_program jobs must use kind system")
        program = validate_program_job(raw.get("program"))
    cron_expr = raw.get("cron")
    if cron_expr:
        try:
            croniter(str(cron_expr))
        except (ValueError, KeyError) as exc:
            raise ValueError(f"invalid cron expression: {exc}") from exc
    interval = raw.get("interval_seconds")
    if not cron_expr and interval is None:
        raise ValueError("cron or interval_seconds is required")
    skill_id = str(raw.get("skill_id") or "").strip() or None
    hints: list[str] = []
    for item in list(raw.get("skill_hints") or []):
        value = str(item).strip()
        if value and value not in hints:
            hints.append(value)
    if skill_id and skill_id not in hints:
        hints.insert(0, skill_id)
    job: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "agent_id": agent_id,
        "prompt": str(raw.get("prompt") or ""),
        "timezone": str(raw.get("timezone") or "UTC"),
        "active_start": str(raw.get("active_start") or "00:00"),
        "active_end": str(raw.get("active_end") or "23:59"),
    }
    if cron_expr:
        job["cron"] = str(cron_expr)
    if interval is not None:
        job["interval_seconds"] = int(interval)
    if skill_id:
        job["skill_id"] = skill_id
    if hints:
        job["skill_hints"] = hints
    if raw.get("announce"):
        job["announce"] = dict(raw["announce"])
    if handler:
        job["handler"] = handler
    if program is not None:
        job["program"] = program
    return job


def _program_snapshot(program_type: str) -> dict[str, Any]:
    from ts_harness.workflows.program_registry import get_program_definition_registry

    registry = get_program_definition_registry()
    registry.reload_if_changed()
    try:
        return registry.load(program_type)
    except FileNotFoundError as exc:
        raise ValueError(f"unknown program_type: {program_type}") from exc


def validate_program_job(raw: Any) -> dict[str, Any]:
    """Validate the ``program`` block of a ``start_program`` system job."""
    from ts_harness.workflows.program_materialize import (
        seed_list_reentry,
        validate_entry_against_snapshot,
    )

    if not isinstance(raw, dict):
        raise ValueError("program must be an object")
    program_type = str(raw.get("program_type") or "").strip()
    if not program_type:
        raise ValueError("program.program_type is required")
    summary = str(raw.get("summary") or "").strip()
    if not summary:
        raise ValueError("program.summary is required")
    artifacts = raw.get("artifacts") or {}
    if not isinstance(artifacts, dict):
        raise ValueError("program.artifacts must be an object")
    extra_input = raw.get("input") or {}
    if not isinstance(extra_input, dict):
        raise ValueError("program.input must be an object")
    snapshot = _program_snapshot(program_type)
    entry_node = str(raw.get("entry_node") or "").strip()
    startable = [str(item) for item in list(snapshot.get("startable_nodes") or [])]
    resolved_entry = entry_node or (startable[0] if startable else "")
    if not resolved_entry:
        raise ValueError(f"program {program_type} has no startable nodes")
    seeded = seed_list_reentry(snapshot, resolved_entry, dict(artifacts))
    errors = validate_entry_against_snapshot(snapshot, resolved_entry, seeded, require_startable=True)
    if errors:
        raise ValueError("; ".join(errors))

    program: dict[str, Any] = {"program_type": program_type, "summary": summary}
    if entry_node:
        program["entry_node"] = entry_node
    if artifacts:
        program["artifacts"] = dict(artifacts)
    if extra_input:
        program["input"] = dict(extra_input)
    notify = raw.get("notify")
    if notify:
        if not isinstance(notify, dict):
            raise ValueError("program.notify must be an object")
        channel = str(notify.get("channel") or "").strip()
        if not channel or ":" in channel:
            raise ValueError("program.notify.channel must be a Slack channel id without ':'")
        text = notify.get("text")
        if text is not None and not isinstance(text, str):
            raise ValueError("program.notify.text must be a string")
        program["notify"] = {"channel": channel, **({"text": text} if text else {})}
    return program


def validate_skill_hints_against_catalog(hints: list[str], catalog: Any) -> None:
    """Fail closed when hints do not resolve for injectable skills."""
    if not hints:
        return
    if catalog is None:
        return
    index = getattr(catalog, "index", {}) or {}
    by_id = {getattr(meta, "id", None): meta for meta in index.values()}
    for skill_id in hints:
        meta = by_id.get(canonical_skill_id(skill_id))
        if meta is None:
            # Allow missing catalog in tests / empty roots; only fail when index loaded.
            if index:
                raise ValueError(f"unknown skill: {skill_id}")
            continue
        approval = getattr(meta, "approval_status", None)
        trust = getattr(meta, "trust_level", None)
        if approval in {"draft", "pending_review", "rejected", "blocked"}:
            raise ValueError(f"skill not injectable: {skill_id} ({approval})")
        if trust in {"agent-created"}:
            raise ValueError(f"skill not injectable: {skill_id} (agent-created)")
