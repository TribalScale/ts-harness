"""Attachments on chat messages, held on the session until a program or workflow starts.

Sources of pointers, by trust:

- ``body["attachments"]`` on ``/session/message`` comes from the client, so only ``stored``
  pointers (console uploads) are accepted, each checked against the caller's tenant.
- ``channel_attachments`` is passed by ``handle_channel_webhook`` from a signed channel
  event (Slack, Telegram, Discord). Clients cannot set it, so a console user cannot name
  a Slack or Discord file the bot would then download.
- Drive links are parsed from the message text.

Pointers accumulate in ``session.metadata["intake_pointers"]`` (small dicts, never bytes).
``set_pending_*`` copies them onto the proposal, the start handlers pass them to the first
task as ``intake_sources``, and they are cleared after a start or a declined proposal.
"""

from __future__ import annotations

from typing import Any

from ts_harness.intake.sources import apply_arrival_caps, dedupe_pointers, parse_drive_urls

SESSION_KEY = "intake_pointers"
FILES_ONLY_TEXT = "Shared files are attached."
_CHANNEL_KINDS = frozenset({"slack_file", "telegram_file", "discord_file"})


def client_attachment_pointers(attachments: Any, *, tenant_id: str) -> list[dict[str, Any]]:
    """Pointers from a client body: ``stored`` only, validated; anything else becomes an error."""
    from ts_harness.intake.blobs import clean_name, validate_stored_pointer

    out: list[dict[str, Any]] = []
    if not isinstance(attachments, list):
        return out
    for item in attachments[:100]:
        if not isinstance(item, dict):
            continue
        name = clean_name(str(item.get("name") or "attachment"))
        if "content_base64" in item:
            out.append({"kind": "stored", "name": name, "error": "upload the file through /intake/uploads"})
        elif item.get("kind") == "stored":
            try:
                out.append(validate_stored_pointer(item, tenant_id=tenant_id))
            except Exception:  # noqa: BLE001 - storage outage: report it, keep the message
                out.append({"kind": "stored", "name": name, "error": "attachment could not be checked"})
        else:
            out.append({"kind": "stored", "name": name, "error": "unsupported attachment"})
    return out


def message_pointers(
    body: dict[str, Any], *, tenant_id: str, text: str, channel_attachments: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    trusted = [dict(p) for p in channel_attachments or [] if isinstance(p, dict) and p.get("kind") in _CHANNEL_KINDS]
    client = client_attachment_pointers(body.get("attachments"), tenant_id=tenant_id)
    return dedupe_pointers([*trusted, *client, *parse_drive_urls(text)])


def session_pointers(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    value = (metadata or {}).get(SESSION_KEY)
    return [dict(p) for p in value if isinstance(p, dict)] if isinstance(value, list) else []


def remember_pointers(metadata: dict[str, Any], pointers: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Add ``pointers`` to the session's list under the caps; return (metadata, the new entries)."""
    existing = session_pointers(metadata)
    added = apply_arrival_caps(pointers, existing=existing)
    if not added:
        return metadata, []
    updated = dict(metadata)
    updated[SESSION_KEY] = existing + added
    for key in ("pending_program", "pending_workflow"):
        pending = updated.get(key)
        if isinstance(pending, dict):
            updated[key] = with_pending_sources(pending, updated[SESSION_KEY], workflow=key == "pending_workflow")
    return updated, added


def with_pending_sources(pending: dict[str, Any], pointers: list[dict[str, Any]], *, workflow: bool) -> dict[str, Any]:
    """The proposal with the session's pointers attached (programs: top level; workflows: ``input``)."""
    out = dict(pending)
    if not pointers:
        return out
    if workflow:
        data = dict(out.get("input") or {}) if isinstance(out.get("input"), dict) else {}
        data["intake_sources"] = list(pointers)
        out["input"] = data
    else:
        out["intake_sources"] = list(pointers)
    return out


def start_sources(metadata: dict[str, Any] | None, pending: dict[str, Any] | None) -> list[dict[str, Any]]:
    """What the first task gets: the session's pointers, else what the proposal carried."""
    found = session_pointers(metadata)
    if found:
        return found
    pending = pending or {}
    carried = pending.get("intake_sources")
    if not isinstance(carried, list) and isinstance(pending.get("input"), dict):
        carried = pending["input"].get("intake_sources")
    return [dict(p) for p in carried if isinstance(p, dict)] if isinstance(carried, list) else []


def clear_pointers(metadata: dict[str, Any]) -> dict[str, Any]:
    updated = dict(metadata)
    updated.pop(SESSION_KEY, None)
    return updated


def attachment_note(pointers: list[dict[str, Any]]) -> str:
    """One line for the agent: names only, never extracted text."""
    readable = [str(p.get("name") or p.get("url") or "attachment") for p in pointers if not p.get("error")]
    refused = [f"{p.get('name') or p.get('url') or 'attachment'} ({p['error']})" for p in pointers if p.get("error")]
    parts: list[str] = []
    if readable:
        parts.append(f"Attachments: {', '.join(readable)}. They are read at intake.")
    if refused:
        parts.append(f"Not attached: {'; '.join(refused)}.")
    return " ".join(parts)
