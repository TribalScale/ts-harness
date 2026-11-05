"""Render active knowledge notes into an agent prompt."""

from __future__ import annotations

from typing import Any


def _lines(notes: list[dict[str, Any]]) -> list[str]:
    ordered = sorted(notes, key=lambda note: 0 if note.get("kind") == "avoid" else 1)
    lines = []
    for note in ordered:
        why = str(note.get("why") or "").strip()
        suffix = f" ({why})" if why else ""
        kind = str(note.get("kind") or "note")
        lines.append(f"- [{kind}] {note.get('title')}: {note.get('body')}{suffix}")
    return lines


def render_knowledge_notes(notes: Any, *, headings: bool = False) -> str:
    """Org rules first, then project notes. Empty input renders nothing."""
    if not isinstance(notes, list) or not notes:
        return ""
    org = [note for note in notes if isinstance(note, dict) and note.get("scope") != "project"]
    project = [note for note in notes if isinstance(note, dict) and note.get("scope") == "project"]
    chunks = []
    for title, group in (("Org rules", org), ("Project notes", project)):
        body = _lines(group)
        if not body:
            continue
        heading = f"## {title}" if headings else title
        chunks.append("\n".join([heading, *body]))
    if not chunks:
        return ""
    return "\n".join(chunks) + "\n"
