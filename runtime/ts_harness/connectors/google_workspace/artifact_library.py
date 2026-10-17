"""Structured Google Drive artifact library per delivery program."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from ts_harness.config.schema import (
    GoogleWorkspaceArtifactLibraryConfig,
    GoogleWorkspaceArtifactPublishConfig,
    RuntimeConfig,
)
from ts_harness.connectors.google_workspace.doc_helpers import (
    build_gw_clients,
    create_document_with_markdown,
    create_drive_folder,
    gw_config,
    share_document,
)

DEFAULT_STEP_PHASE_MAP: dict[str, dict[str, str]] = {
    "prd_request": {
        "draft_prd": "prd",
        "revise_prd": "prd",
    },
    "work_orders_from_prd": {
        "refine_requirements": "refinery",
        "design_blueprints": "foundry",
        "plan_work_orders": "planner",
    },
}

PHASE_FOLDERS = ("prd", "refinery", "foundry", "planner")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _slugify(text: str, *, max_len: int = 40) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", str(text or "").lower())
    cleaned = re.sub(r"[\s_]+", "-", cleaned).strip("-")
    return cleaned[:max_len] if cleaned else "program"


def program_slug(program_id: str, summary: str) -> str:
    short_id = (program_id or "unknown").split("-")[0][:8]
    base = _slugify(summary)
    combined = f"{base}-{short_id}" if base else short_id
    return combined[:60]


def workflow_fallback_slug(workflow_type: str, workflow_id: str) -> str:
    wt = _slugify(workflow_type.replace("_", "-"), max_len=30)
    short_id = (workflow_id or "unknown").split("-")[0][:8]
    return f"{wt}-{short_id}"[:60]


def resolve_phase_folder(
    workflow_type: str,
    step_name: str,
    library_cfg: GoogleWorkspaceArtifactLibraryConfig,
) -> str | None:
    step_map = dict(DEFAULT_STEP_PHASE_MAP)
    for wf_type, mapping in (library_cfg.step_phase_map or {}).items():
        step_map[str(wf_type)] = {str(k): str(v) for k, v in mapping.items()}
    phase = (step_map.get(workflow_type) or {}).get(step_name)
    return str(phase) if phase else None


def extract_publishable_markdown(artifact: dict[str, Any]) -> dict[str, str]:
    """Return markdown fields suitable for Drive publishing."""
    result: dict[str, str] = {}
    for key, value in artifact.items():
        if not isinstance(value, str) or not value.strip():
            continue
        if key.endswith("_markdown") or key == "markdown":
            result[str(key)] = value.strip()
    return result


def _doc_display_name(artifact_key: str) -> str:
    name = str(artifact_key or "artifact")
    if name.endswith("_markdown"):
        name = name[: -len("_markdown")]
    return name.replace("_", " ").strip() or "artifact"


def _library_config(
    publish_cfg: GoogleWorkspaceArtifactPublishConfig,
) -> GoogleWorkspaceArtifactLibraryConfig | None:
    library = publish_cfg.library
    if not library.enabled:
        return None
    return library


def _ensure_folder(
    drive: Any,
    *,
    name: str,
    parent_id: str,
    cache: dict[str, str],
    cache_key: str,
) -> dict[str, str]:
    if cache_key in cache:
        folder_id = cache[cache_key]
        return {"id": folder_id, "webViewLink": cache.get(f"{cache_key}:link", "")}

    query = (
        f"mimeType='application/vnd.google-apps.folder' and "
        f"name='{name.replace(chr(39), '')}' and "
        f"'{parent_id}' in parents and trashed=false"
    )
    existing = (
        drive.files()
        .list(
            q=query,
            fields="files(id,name,webViewLink)",
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageSize=1,
        )
        .execute()
    )
    files = existing.get("files") or []
    if files:
        folder_id = str(files[0]["id"])
        link = str(files[0].get("webViewLink") or "")
    else:
        created = create_drive_folder(drive, name=name, parent_id=parent_id)
        folder_id = str(created["id"])
        link = str(created.get("webViewLink") or "")

    cache[cache_key] = folder_id
    cache[f"{cache_key}:link"] = link
    return {"id": folder_id, "webViewLink": link}


def _ensure_program_root(
    drive: Any,
    *,
    publish_cfg: GoogleWorkspaceArtifactPublishConfig,
    library_cfg: GoogleWorkspaceArtifactLibraryConfig,
    program_id: str,
    program_summary: str,
    workflow_type: str,
    workflow_id: str,
    existing_index: dict[str, Any],
) -> dict[str, Any]:
    cache: dict[str, str] = {}
    if existing_index.get("root_folder_id"):
        cache["program"] = str(existing_index["root_folder_id"])
        if existing_index.get("root_web_view_link"):
            cache["program:link"] = str(existing_index["root_web_view_link"])

    root_id = (publish_cfg.folder_id or "").strip()
    if not root_id:
        raise ValueError("artifact_publish.folder_id is required for artifact library")

    parent_for_program = root_id
    subfolder = (library_cfg.program_subfolder or "").strip()
    if subfolder:
        sub = _ensure_folder(
            drive,
            name=subfolder,
            parent_id=root_id,
            cache=cache,
            cache_key="programs_parent",
        )
        parent_for_program = sub["id"]

    slug = (
        program_slug(program_id, program_summary)
        if program_id
        else workflow_fallback_slug(workflow_type, workflow_id)
    )
    program_folder = _ensure_folder(
        drive,
        name=slug,
        parent_id=parent_for_program,
        cache=cache,
        cache_key="program",
    )

    phase_folder_ids: dict[str, str] = dict(existing_index.get("phase_folder_ids") or {})
    for phase in PHASE_FOLDERS:
        if phase in phase_folder_ids:
            cache[f"phase:{phase}"] = phase_folder_ids[phase]
            continue
        phase_folder = _ensure_folder(
            drive,
            name=phase,
            parent_id=program_folder["id"],
            cache=cache,
            cache_key=f"phase:{phase}",
        )
        phase_folder_ids[phase] = phase_folder["id"]

    return {
        "root_folder_id": program_folder["id"],
        "root_web_view_link": program_folder.get("webViewLink", ""),
        "program_slug": slug,
        "phase_folder_ids": phase_folder_ids,
    }


def _build_manifest_markdown(
    *,
    program_id: str,
    program_summary: str,
    entries: list[dict[str, Any]],
) -> str:
    lines = [
        "# Delivery artifact library",
        "",
        f"Program: {program_summary or '(no summary)'}",
        f"Program ID: {program_id or '(none)'}",
        f"Last updated: {_utc_now_iso()}",
        "",
        "| Phase | Step | Document | Link | Updated |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in sorted(
        entries,
        key=lambda row: (
            str(row.get("phase_folder") or ""),
            str(row.get("step_name") or ""),
            str(row.get("document_title") or ""),
        ),
    ):
        phase = str(entry.get("phase_folder") or "")
        step = str(entry.get("step_name") or "")
        title = str(entry.get("document_title") or entry.get("artifact_key") or "")
        link = str(entry.get("web_view_link") or "")
        updated = str(entry.get("updated_at") or "")
        lines.append(f"| {phase} | {step} | {title} | {link} | {updated} |")
    lines.append("")
    return "\n".join(lines)


def _flatten_manifest_entries(index: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for entry in index.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        phase_folder = str(entry.get("phase_folder") or "")
        step_name = str(entry.get("step_name") or "")
        updated_at = str(entry.get("presented_at") or "")
        documents = entry.get("documents") or {}
        if not isinstance(documents, dict):
            continue
        for artifact_key, doc_meta in documents.items():
            if not isinstance(doc_meta, dict):
                continue
            rows.append(
                {
                    "phase_folder": phase_folder,
                    "step_name": step_name,
                    "artifact_key": artifact_key,
                    "document_title": doc_meta.get("title") or _doc_display_name(artifact_key),
                    "web_view_link": doc_meta.get("web_view_link") or "",
                    "updated_at": doc_meta.get("updated_at") or updated_at,
                }
            )
    return rows


def _upsert_entry(
    index: dict[str, Any],
    *,
    workflow_type: str,
    workflow_id: str,
    step_name: str,
    phase_folder: str,
    documents: dict[str, Any],
) -> None:
    entries = list(index.get("entries") or [])
    now = _utc_now_iso()
    updated_entry: dict[str, Any] | None = None
    remaining: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if (
            str(entry.get("workflow_id") or "") == workflow_id
            and str(entry.get("step_name") or "") == step_name
        ):
            updated_entry = dict(entry)
        else:
            remaining.append(entry)

    if updated_entry is None:
        updated_entry = {
            "workflow_type": workflow_type,
            "workflow_id": workflow_id,
            "step_name": step_name,
            "phase_folder": phase_folder,
            "presented_at": now,
            "documents": {},
        }

    doc_map = dict(updated_entry.get("documents") or {})
    for artifact_key, doc_meta in documents.items():
        doc_map[str(artifact_key)] = doc_meta
    updated_entry["documents"] = doc_map
    updated_entry["presented_at"] = now
    updated_entry["phase_folder"] = phase_folder
    remaining.append(updated_entry)
    index["entries"] = remaining


def publish_program_artifacts(
    *,
    artifact: dict[str, Any],
    runtime_config: RuntimeConfig,
    workflow_type: str,
    workflow_id: str,
    step_name: str,
    tenant_id: str = "default",
    agent_id: str = "pm-agent",
    program_id: str = "",
    program_summary: str = "",
    share_with_email: str | None = None,
    existing_drive_library: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish artifact markdown keys to a structured Drive library folder."""
    gw = gw_config(runtime_config)
    if gw is None or not gw.enabled:
        return {"status": "skipped", "reason": "google-workspace disabled"}

    publish_cfg = gw.artifact_publish
    if not publish_cfg.enabled:
        return {"status": "skipped", "reason": "artifact_publish disabled"}

    library_cfg = _library_config(publish_cfg)
    if library_cfg is None:
        return {"status": "skipped", "reason": "artifact library disabled"}

    if library_cfg.publish_on not in {"present", "both"}:
        return {"status": "skipped", "reason": f"publish_on={library_cfg.publish_on}"}

    phase_folder = resolve_phase_folder(workflow_type, step_name, library_cfg)
    if not phase_folder:
        return {"status": "skipped", "reason": "step not mapped to artifact library phase"}

    markdown_fields = extract_publishable_markdown(artifact)
    if not markdown_fields:
        return {"status": "skipped", "reason": "no markdown artifacts to publish"}

    clients = build_gw_clients(runtime_config, tenant_id=tenant_id, agent_id=agent_id)
    if clients is None:
        return {"status": "error", "error": "google-workspace client unavailable"}

    drive, docs, _factory = clients
    chunk_chars = max(1000, int(publish_cfg.chunk_chars or 50000))
    index: dict[str, Any] = dict(existing_drive_library or {})

    try:
        root_info = _ensure_program_root(
            drive,
            publish_cfg=publish_cfg,
            library_cfg=library_cfg,
            program_id=program_id,
            program_summary=program_summary,
            workflow_type=workflow_type,
            workflow_id=workflow_id,
            existing_index=index,
        )
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "error": f"folder setup failed: {exc}"}

    index.update(root_info)
    phase_folder_id = (index.get("phase_folder_ids") or {}).get(phase_folder)
    if not phase_folder_id:
        return {"status": "error", "error": f"phase folder missing: {phase_folder}"}

    published_docs: dict[str, Any] = {}
    errors: list[str] = []
    now = _utc_now_iso()

    prior_entry_docs: dict[str, Any] = {}
    for entry in index.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        if (
            str(entry.get("workflow_id") or "") == workflow_id
            and str(entry.get("step_name") or "") == step_name
        ):
            prior_entry_docs = dict(entry.get("documents") or {})
            break

    for artifact_key, content in markdown_fields.items():
        title = _doc_display_name(artifact_key)
        prior = prior_entry_docs.get(artifact_key) if isinstance(prior_entry_docs, dict) else None
        existing_doc_id = None
        if isinstance(prior, dict):
            existing_doc_id = prior.get("document_id")
        try:
            created = create_document_with_markdown(
                drive,
                docs,
                title=title,
                content=content,
                parent_folder_id=str(phase_folder_id),
                chunk_chars=chunk_chars,
                existing_document_id=str(existing_doc_id) if existing_doc_id else None,
            )
            doc_id = str(created.get("document_id") or "")
            share_result = None
            if share_with_email:
                share_result = share_document(drive, doc_id, share_with_email)
            published_docs[artifact_key] = {
                "document_id": doc_id,
                "web_view_link": created.get("web_view_link"),
                "title": title,
                "updated_at": now,
                "shared_with": share_result,
            }
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{artifact_key}: {exc}")

    if not published_docs:
        return {
            "status": "error",
            "error": "; ".join(errors) if errors else "no documents published",
        }

    _upsert_entry(
        index,
        workflow_type=workflow_type,
        workflow_id=workflow_id,
        step_name=step_name,
        phase_folder=phase_folder,
        documents=published_docs,
    )

    manifest_rows = _flatten_manifest_entries(index)
    manifest_md = _build_manifest_markdown(
        program_id=program_id,
        program_summary=program_summary,
        entries=manifest_rows,
    )
    try:
        manifest_doc = create_document_with_markdown(
            drive,
            docs,
            title="manifest",
            content=manifest_md,
            parent_folder_id=str(index["root_folder_id"]),
            chunk_chars=chunk_chars,
            existing_document_id=str(index.get("manifest_document_id") or "") or None,
        )
        index["manifest_document_id"] = manifest_doc.get("document_id")
        index["manifest_web_view_link"] = manifest_doc.get("web_view_link")
        if share_with_email and index.get("manifest_document_id"):
            index["manifest_shared_with"] = share_document(
                drive,
                str(index["manifest_document_id"]),
                share_with_email,
            )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"manifest: {exc}")

    folder_share = None
    if library_cfg.share_folder_with_requester and share_with_email and index.get("root_folder_id"):
        folder_share = share_document(drive, str(index["root_folder_id"]), share_with_email)
        index["root_shared_with"] = folder_share

    status = "published" if not errors else "partial"
    return {
        "status": status,
        "drive_library": index,
        "root_web_view_link": index.get("root_web_view_link"),
        "manifest_web_view_link": index.get("manifest_web_view_link"),
        "documents_published": len(published_docs),
        "errors": errors,
        "folder_shared_with": folder_share,
    }
