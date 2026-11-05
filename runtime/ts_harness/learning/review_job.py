"""Hourly review of programs that finished since the last cursor."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from ts_harness.learning.run_evidence import collect_run_evidence
from ts_harness.learning.staleness import propose_retires, stale_notes, team_note_candidates

logger = logging.getLogger(__name__)

CURSOR_NAMESPACE = "knowledge_notes"
CURSOR_KEY = "review_cursor"


def _now(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment


def _bundle(memory: Any, program: dict[str, Any]) -> dict[str, Any]:
    workflows = []
    for workflow in program.get("workflows") or []:
        workflow_id = str(workflow.get("id") or "")
        tasks: list[dict[str, Any]] = []
        if workflow_id and hasattr(memory, "get_workflow"):
            try:
                detail = memory.get_workflow(workflow_id)
                tasks = list(detail.get("tasks") or [])
            except Exception:
                logger.warning("note review could not load tasks for %s", workflow_id)
        workflows.append({"workflow": workflow, "tasks": tasks})
    return {"program": program.get("program") or program, "workflows": workflows}


def _similar(memory: Any, program: dict[str, Any], own: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Notes in other projects whose title or body overlaps this run's notes. Not written."""
    titles = {" ".join(str(note.get("title") or "").lower().split()) for note in own if note.get("title")}
    if not titles or not hasattr(memory, "list_knowledge_notes"):
        return []
    try:
        listed = memory.list_knowledge_notes(
            tenant_id=str(program.get("tenant_id") or "default"),
            scope="project",
            status="active",
        )
    except Exception:
        return []
    project_id = str(program.get("project_ref") or program.get("project_id") or "")
    found = []
    for note in listed.get("notes") or []:
        if str(note.get("project_id") or "") == project_id:
            continue
        title = " ".join(str(note.get("title") or "").lower().split())
        if title in titles:
            found.append({"id": note.get("id"), "project_id": note.get("project_id"), "title": note.get("title")})
    return found


def _default_starter(
    *,
    memory: Any,
    program: dict[str, Any],
    evidence: dict[str, Any],
    project_notes: list[dict[str, Any]],
    org_notes: list[dict[str, Any]],
    similar: list[dict[str, Any]],
) -> dict[str, Any]:
    from ts_harness.agent.sessions import SessionRecord
    from ts_harness.workflows.engine import get_workflow_engine

    tenant_id = str(program.get("tenant_id") or "default")
    program_id = str(program.get("id") or "")
    session = SessionRecord(
        session_id=f"notes-review-{program_id}",
        routing_key=f"notes-review:{program_id}",
        tenant_id=tenant_id,
        agent_id="ops-agent",
        source="system",
    )
    return get_workflow_engine().start_promotion(
        session=session,
        workflow_type="review_program_notes",
        summary=f"Review notes for program {program_id}",
        tenant_id=tenant_id,
        binding_key=f"notes-review:{program_id}",
        requester_actor_id="review",
        extra_input={
            "source_program_id": program_id,
            "project_id": str(program.get("project_id") or ""),
            "evidence": evidence,
            "project_notes": project_notes,
            "org_notes": org_notes,
            "similar_notes": similar,
        },
    )


def review_finished_programs(
    *,
    memory: Any = None,
    starter: Callable[..., Any] | None = None,
    now: datetime | None = None,
    github: Any = None,
) -> dict[str, Any]:
    """Review programs finished since the cursor. The first run only stores the cursor."""
    from ts_harness.memory.client import get_memory_client

    memory = memory or get_memory_client()
    moment = _now(now)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = memory.kv_get(CURSOR_NAMESPACE, CURSOR_KEY)
    value = cursor.get("value") if isinstance(cursor, dict) else None
    scanned = value.get("scanned_at") if isinstance(value, dict) else None
    if not scanned:
        memory.kv_put(CURSOR_NAMESPACE, CURSOR_KEY, {"scanned_at": stamp}, expected_version=int(cursor.get("version") or 0))
        return {"status": "ok", "bootstrapped": True, "reviewed": 0, "started": 0}

    pending = memory.list_programs_pending_note_review(since=str(scanned), limit=20)
    reviewed = 0
    started = 0
    errors: list[str] = []
    for row in pending.get("programs") or []:
        program_id = str(row.get("id") or "")
        try:
            loaded = memory.get_program(program_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{program_id}: {exc}")
            continue
        program = (loaded or {}).get("program") or row
        program = {**program, "workflows": (loaded or {}).get("workflows") or []}
        evidence = collect_run_evidence(_bundle(memory, program))
        if not evidence["useful"]:
            memory.mark_program_notes_reviewed(program_id, actor="review")
            reviewed += 1
            continue
        memory.mark_program_notes_reviewed(program_id, actor="review", key="notes_review_started_at")
        context = memory.knowledge_notes_context(
            tenant_id=str(program.get("tenant_id") or "default"),
            project_id=str(program.get("project_ref") or program.get("project_id") or ""),
        )
        notes = list(context.get("notes") or [])
        (starter or _default_starter)(
            memory=memory,
            program=program,
            evidence=evidence,
            project_notes=[note for note in notes if note.get("scope") == "project"],
            org_notes=[note for note in notes if note.get("scope") != "project"],
            similar=_similar(memory, program, notes),
        )
        started += 1

    team = _team_candidates(memory)
    retired = _stale(memory, now=moment, github=github)
    memory.kv_put(CURSOR_NAMESPACE, CURSOR_KEY, {"scanned_at": stamp})
    return {
        "status": "ok",
        "bootstrapped": False,
        "reviewed": reviewed,
        "started": started,
        "errors": errors,
        "team_note_candidates": team,
        "stale_proposed": len(retired),
    }


def _team_candidates(memory: Any) -> list[dict[str, Any]]:
    try:
        projects = memory.list_projects().get("projects") or []
        notes = memory.list_knowledge_notes(scope="project").get("notes") or []
    except Exception:
        logger.warning("team note candidate scan failed", exc_info=True)
        return []
    return team_note_candidates(projects, notes)


def _stale(memory: Any, *, now: datetime, github: Any) -> list[dict[str, Any]]:
    try:
        listed = memory.list_knowledge_notes(status="active")
    except Exception:
        logger.warning("staleness scan failed", exc_info=True)
        return []
    notes = list(listed.get("notes") or [])
    tree = _tree_paths(github)
    return propose_retires(memory, stale_notes(notes, now=now, tree_paths=tree))


def _tree_paths(github: Any) -> set[str] | None:
    if github is None or not hasattr(github, "get_tree"):
        return None
    try:
        payload = github.get_tree()
    except Exception:
        logger.warning("repo tree for note staleness failed", exc_info=True)
        return None
    paths = payload.get("paths") if isinstance(payload, dict) else payload
    if not isinstance(paths, (list, set)):
        return None
    return {str(path) for path in paths}
