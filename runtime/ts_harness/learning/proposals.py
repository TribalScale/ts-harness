"""Turn a review's proposals into proposed notes. Nothing here is published."""

from __future__ import annotations

from typing import Any


def _norm(title: str) -> str:
    return " ".join(str(title or "").lower().split())


def _program_refs(evidence: list[dict[str, Any]]) -> set[str]:
    refs = set()
    for item in evidence:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "") in {"program", "workflow"} and str(item.get("ref") or "").strip():
            refs.add(str(item["ref"]).strip())
    return refs


def _matching_note(notes: list[dict[str, Any]], *, scope: str, title: str) -> dict[str, Any] | None:
    wanted = _norm(title)
    for note in notes:
        if note.get("scope") == scope and note.get("status") == "active" and _norm(str(note.get("title") or "")) == wanted:
            return note
    return None


def apply_proposals(
    memory: Any,
    *,
    tenant_id: str,
    project_id: str,
    source_program_id: str,
    proposals: list[dict[str, Any]],
    existing_notes: list[dict[str, Any]] | None = None,
    skills_root: str = "skills",
) -> dict[str, Any]:
    """Write proposed notes. Org adds need evidence from two programs. No project means org promote only."""
    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    notes = list(existing_notes or [])
    for raw in proposals:
        if not isinstance(raw, dict):
            continue
        action = str(raw.get("action") or raw.get("proposal_action") or "add").strip()
        if action == "suggest_skill":
            written.append(_suggest_skill(raw, tenant_id=tenant_id, skills_root=skills_root, skipped=skipped))
            continue
        evidence = list(raw.get("evidence") or [])
        if not any(isinstance(item, dict) and item.get("ref") for item in evidence):
            evidence.append({"type": "program", "ref": source_program_id})
        scope = "org" if str(raw.get("scope") or "project") == "org" else "project"
        refs = _program_refs(evidence)
        if not project_id:
            if action != "promote" or len(refs) < 2:
                skipped.append({"action": action, "reason": "a program with no project can only promote a lesson seen in two programs"})
                continue
        if action == "promote":
            note_id = str(raw.get("note_id") or raw.get("promoted_from") or "").strip()
            if not note_id or len(refs) < 2:
                skipped.append({"action": "promote", "reason": "promote needs a source note and evidence from two programs"})
                continue
            written.append(memory.promote_knowledge_note(note_id, actor="review", source="review"))
            continue
        if scope == "org" and len(refs) < 2:
            skipped.append({"action": action, "reason": "an org note needs the same lesson in two programs"})
            continue
        if action == "add":
            match = _matching_note(notes, scope=scope, title=str(raw.get("title") or ""))
            if match is not None:
                action = "edit"
                raw = {**raw, "revision_of": match["id"], "base_version": match.get("version")}
        body: dict[str, Any] = {
            "tenant_id": tenant_id,
            "scope": scope,
            "kind": raw.get("kind") or "avoid",
            "title": raw.get("title") or "",
            "body": raw.get("body") or "",
            "why": raw.get("why") or "",
            "applies_to": raw.get("applies_to") or [],
            "evidence": evidence,
            "source": "review",
            "status": "proposed",
            "proposal_action": action if action in {"add", "edit", "retire"} else "add",
            "actor": "review",
        }
        if scope == "project":
            body["project_id"] = project_id
        revision_of = str(raw.get("revision_of") or raw.get("note_id") or "").strip()
        if action in {"edit", "retire"}:
            if not revision_of:
                skipped.append({"action": action, "reason": "edit and retire name the note they change"})
                continue
            body["revision_of"] = revision_of
        if action == "edit" and raw.get("base_version") not in (None, ""):
            body["base_version"] = raw.get("base_version")
        created = memory.create_knowledge_note(**body)
        written.append(created if isinstance(created, dict) else {"id": created})
    if source_program_id:
        memory.mark_program_notes_reviewed(source_program_id, actor="review")
    return {"written": [item for item in written if item], "skipped": skipped}


def _suggest_skill(
    raw: dict[str, Any], *, tenant_id: str, skills_root: str, skipped: list[dict[str, Any]]
) -> dict[str, Any]:
    from ts_harness.skills.authoring import propose_skill_draft

    title = str(raw.get("title") or "learned-note").strip()
    slug = "-".join(title.lower().split())[:60] or "learned-note"
    markdown = str(raw.get("markdown") or "").strip()
    if not markdown:
        markdown = (
            f"---\nname: {slug}\ndescription: {raw.get('why') or title}\n"
            f"category: learning\nversion: 1.0.0\n---\n\n{raw.get('body') or title}\n"
        )
    try:
        draft = propose_skill_draft(
            root=skills_root,
            agent_id="ops-agent",
            tenant_id=tenant_id,
            markdown=markdown,
            source="review",
            created_by="review",
        )
    except Exception as exc:  # noqa: BLE001 — a bad draft must not drop the other proposals
        skipped.append({"action": "suggest_skill", "reason": str(exc)})
        return {}
    return {"action": "suggest_skill", "id": draft.get("id") or draft.get("skill_id")}
