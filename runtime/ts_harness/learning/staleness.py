"""Notes that have gone quiet, or whose paths are no longer in the repo."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

PROJECT_UNUSED_DAYS = 60
ORG_UNUSED_DAYS = 90


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _parse(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return _aware(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def _path_like(item: str) -> bool:
    return any(char in item for char in "/*.")


def paths_gone(applies_to: list[str], tree_paths: set[str] | None) -> bool:
    """True when a path-like entry is absent from the tree. Stage names are ignored."""
    if tree_paths is None:
        return False
    import fnmatch

    patterns = [item for item in applies_to if _path_like(item)]
    if not patterns:
        return False
    for pattern in patterns:
        if any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern) for path in tree_paths):
            continue
        return True
    return False


def stale_notes(
    notes: list[dict[str, Any]],
    *,
    now: datetime,
    tree_paths: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Active notes past their unused window, or whose repo paths have disappeared."""
    found = []
    moment = _aware(now)
    for note in notes:
        if note.get("status") != "active":
            continue
        days = ORG_UNUSED_DAYS if note.get("scope") == "org" else PROJECT_UNUSED_DAYS
        seen = _parse(note.get("last_used_at")) or _parse(note.get("updated_at"))
        unused = seen is None or seen <= moment - timedelta(days=days)
        missing = note.get("scope") == "project" and paths_gone(list(note.get("applies_to") or []), tree_paths)
        if unused or missing:
            found.append(note)
    return found


def propose_retires(memory: Any, notes: list[dict[str, Any]], *, actor: str = "review") -> list[dict[str, Any]]:
    """One proposed retire per note. A retire already waiting is left alone."""
    created = []
    for note in notes:
        existing = memory.list_knowledge_notes(
            tenant_id=str(note.get("tenant_id") or "default"),
            status="proposed",
            proposal_action="retire",
            revision_of=str(note["id"]),
        )
        if existing.get("notes"):
            continue
        created.append(
            memory.create_knowledge_note(
                tenant_id=note.get("tenant_id") or "default",
                scope=note.get("scope"),
                project_id=note.get("project_id"),
                kind=note.get("kind") or "avoid",
                title=note.get("title") or "",
                body=note.get("body") or "",
                why=note.get("why") or "Unused or its paths are gone.",
                applies_to=note.get("applies_to") or [],
                source="review",
                status="proposed",
                proposal_action="retire",
                revision_of=note["id"],
                actor=actor,
                evidence=[{"type": "note", "ref": str(note["id"]), "excerpt": "stale"}],
            )
        )
    return created


def team_note_candidates(projects: list[dict[str, Any]], notes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Same lesson in two projects of one team. Reported only; no team note is written."""
    team_of = {str(project.get("id")): str(project.get("team_slug") or "") for project in projects}
    grouped: dict[tuple[str, str], set[str]] = {}
    for note in notes:
        if note.get("scope") != "project" or note.get("status") not in {"active", "proposed"}:
            continue
        project_id = str(note.get("project_id") or "")
        team = team_of.get(project_id) or ""
        if not team:
            continue
        key = (team, _title(note))
        grouped.setdefault(key, set()).add(project_id)
    return [
        {"team": team, "title": title, "project_ids": sorted(ids)}
        for (team, title), ids in sorted(grouped.items())
        if len(ids) >= 2
    ]


def _title(note: dict[str, Any]) -> str:
    return " ".join(str(note.get("title") or "").lower().split())
