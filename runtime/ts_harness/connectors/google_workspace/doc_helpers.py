"""Shared Google Drive / Docs helpers for artifact publishing."""

from __future__ import annotations

import re
from typing import Any

from ts_harness.config.schema import RuntimeConfig
from ts_harness.connectors.google_workspace.client import GoogleClientFactory
from ts_harness.tools.policy import ToolPolicyContext

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)([-*]|\d+[.)])\s+(.*)$")

FOLDER_MIME = "application/vnd.google-apps.folder"
DOCUMENT_MIME = "application/vnd.google-apps.document"


def gw_config(runtime_config: RuntimeConfig):
    connectors = runtime_config.connectors
    if connectors is None or connectors.google_workspace is None:
        return None
    return connectors.google_workspace


def build_gw_clients(
    runtime_config: RuntimeConfig,
    *,
    tenant_id: str = "default",
    agent_id: str = "pm-agent",
    source: str = "workflow_presentation",
) -> tuple[Any, Any, GoogleClientFactory] | None:
    gw = gw_config(runtime_config)
    if gw is None or not gw.enabled:
        return None
    ctx = ToolPolicyContext(tenant_id=tenant_id, agent_id=agent_id, source=source)
    factory = GoogleClientFactory(gw, runtime_config)
    drive = factory.build("drive", "v3", ctx)
    docs = factory.build("docs", "v1", ctx)
    return drive, docs, factory


def doc_end_index(docs_api: Any, document_id: str) -> int:
    doc = docs_api.documents().get(documentId=document_id).execute()
    return int(doc["body"]["content"][-1]["endIndex"])


def build_formatted_insert_requests(
    content: str,
    start_index: int,
) -> tuple[list[dict[str, Any]], int]:
    requests: list[dict[str, Any]] = []
    index = start_index
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if not line:
            insert_text = "\n"
            style = "NORMAL_TEXT"
        else:
            heading = _HEADING_RE.match(line)
            bullet = _BULLET_RE.match(line)
            if heading:
                level = len(heading.group(1))
                insert_text = heading.group(2).strip() + "\n"
                style = {
                    1: "HEADING_1",
                    2: "HEADING_2",
                    3: "HEADING_3",
                }.get(level, "HEADING_3")
            elif bullet:
                insert_text = bullet.group(3).strip() + "\n"
                style = "NORMAL_TEXT"
            else:
                insert_text = line + "\n"
                style = "NORMAL_TEXT"

        requests.append(
            {
                "insertText": {
                    "location": {"index": index},
                    "text": insert_text,
                }
            }
        )
        end_index = index + len(insert_text)
        if style != "NORMAL_TEXT":
            requests.append(
                {
                    "updateParagraphStyle": {
                        "range": {"startIndex": index, "endIndex": end_index},
                        "paragraphStyle": {"namedStyleType": style},
                        "fields": "namedStyleType",
                    }
                }
            )
        index = end_index
    return requests, index


def insert_plain_chunks(
    docs_api: Any,
    document_id: str,
    content: str,
    chunk_chars: int,
) -> None:
    remaining = content
    while remaining:
        chunk = remaining[:chunk_chars]
        remaining = remaining[chunk_chars:]
        end_index = doc_end_index(docs_api, document_id)
        insert_at = max(1, end_index - 1)
        docs_api.documents().batchUpdate(
            documentId=document_id,
            body={
                "requests": [
                    {
                        "insertText": {
                            "location": {"index": insert_at},
                            "text": chunk,
                        }
                    }
                ]
            },
        ).execute()


def share_document(
    drive_api: Any,
    file_id: str,
    email: str,
) -> dict[str, Any] | None:
    cleaned = (email or "").strip()
    if not cleaned or "@" not in cleaned:
        return None
    try:
        drive_api.permissions().create(
            fileId=file_id,
            body={
                "type": "user",
                "role": "reader",
                "emailAddress": cleaned,
            },
            sendNotificationEmail=False,
            supportsAllDrives=True,
        ).execute()
        return {"email": cleaned, "role": "reader"}
    except Exception as exc:  # noqa: BLE001
        return {"email": cleaned, "error": str(exc)}


def create_drive_folder(
    drive_api: Any,
    *,
    name: str,
    parent_id: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": str(name or "Untitled").strip()[:200],
        "mimeType": FOLDER_MIME,
    }
    if parent_id:
        body["parents"] = [parent_id]
    return (
        drive_api.files()
        .create(body=body, fields="id,name,webViewLink", supportsAllDrives=True)
        .execute()
    )


def trash_drive_file(drive_api: Any, file_id: str) -> None:
    drive_api.files().update(
        fileId=file_id,
        body={"trashed": True},
        supportsAllDrives=True,
    ).execute()


def write_markdown_to_document(
    docs_api: Any,
    document_id: str,
    content: str,
    *,
    chunk_chars: int = 50000,
) -> None:
    body = (content or "").strip()
    if not body:
        return
    if len(body) <= chunk_chars:
        requests, _ = build_formatted_insert_requests(body, start_index=1)
        if requests:
            batch_size = 80
            for offset in range(0, len(requests), batch_size):
                docs_api.documents().batchUpdate(
                    documentId=document_id,
                    body={"requests": requests[offset: offset + batch_size]},
                ).execute()
    else:
        insert_plain_chunks(docs_api, document_id, body, chunk_chars)


def create_document_with_markdown(
    drive_api: Any,
    docs_api: Any,
    *,
    title: str,
    content: str,
    parent_folder_id: str | None = None,
    chunk_chars: int = 50000,
    existing_document_id: str | None = None,
) -> dict[str, Any]:
    """Create or replace a Google Doc with markdown content."""
    if existing_document_id:
        try:
            trash_drive_file(drive_api, existing_document_id)
        except Exception:
            pass

    create_body: dict[str, Any] = {
        "name": str(title or "Untitled").strip()[:200],
        "mimeType": DOCUMENT_MIME,
    }
    if parent_folder_id:
        create_body["parents"] = [parent_folder_id]

    created = (
        drive_api.files()
        .create(body=create_body, fields="id,name,webViewLink", supportsAllDrives=True)
        .execute()
    )
    document_id = str(created.get("id") or "")
    if not document_id:
        raise ValueError("drive create returned no document id")

    write_markdown_to_document(docs_api, document_id, content, chunk_chars=chunk_chars)
    return {
        "document_id": document_id,
        "document_title": created.get("name"),
        "web_view_link": created.get("webViewLink"),
    }
