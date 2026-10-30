"""Deliver specialist workflow output back to the bound channel session."""

from __future__ import annotations

from typing import Any

from ts_harness.agent.sessions import SessionRecord, get_session_store
from ts_harness.agent.transcript import get_transcript_store
from ts_harness.config.schema import RuntimeConfig
from ts_harness.connectors.google_workspace.artifact_library import publish_program_artifacts
from ts_harness.connectors.google_workspace.doc_publish import (
    publish_markdown_to_google_doc,
    should_publish_artifact_text,
)
from ts_harness.gateway.channels.router import get_channel_router
from ts_harness.gateway.channels.slack_users import (
    resolve_slack_user_email,
    slack_user_id_from_actor,
)
from ts_harness.workflows.artifacts import (
    artifact_title,
    build_artifact_channel_text,
    format_artifact_for_channel,
)
from ts_harness.workflows.materialize import load_skeleton
from ts_harness.workflows.session_compat import (
    read_workflow_phase,
    write_specialist_handoff,
    write_workflow_phase,
)
from ts_harness.workflows.workflow_session import get_phase_tracking


def _session_for_workflow(
    workflow_id: str,
    tenant_id: str,
    source_channel: str = "",
    program_id: str = "",
) -> SessionRecord | None:
    """Find the chat that owns this workflow.

    Admin chat stores ``binding_key`` as ``web:<id>`` while the canonical
    channel key is ``web:web:<id>``. Later phases keep the original
    ``workflow_id`` and are linked by ``program_id`` or ``active_workflow_id``.
    """
    raw = str(source_channel or "").strip()
    canonical = binding_key_from_source_channel(raw) or ""
    bindings = {item for item in (raw, canonical) if item}
    workflow = str(workflow_id or "").strip()
    program = str(program_id or "").strip()
    for session in get_session_store().list_for_tenant(tenant_id):
        metadata = session.metadata or {}
        linked = {
            str(metadata.get("workflow_id") or "").strip(),
            str(metadata.get("active_workflow_id") or "").strip(),
        }
        if workflow and workflow in linked:
            return session
        stored_binding = str(metadata.get("binding_key") or "").strip()
        stored_key = binding_key_from_source_channel(stored_binding)
        candidate_keys = {
            key
            for item in bindings
            if (key := binding_key_from_source_channel(item))
        }
        if stored_binding and (
            stored_binding in bindings or (stored_key and stored_key in candidate_keys)
        ):
            return session
        if program and str(metadata.get("program_id") or "").strip() == program:
            return session
    return None


def _resolve_share_email(
    *,
    runtime_config: RuntimeConfig | None,
    session: SessionRecord | None,
    share_with_email: str | None,
    source_channel: str,
    requester_actor_id: str | None = None,
) -> str | None:
    explicit = (share_with_email or "").strip()
    if explicit and "@" in explicit:
        return explicit
    if session is not None:
        metadata_email = str((session.metadata or {}).get("actor_email") or "").strip()
        if metadata_email and "@" in metadata_email:
            return metadata_email
    candidate_ids: list[str] = []
    for raw_actor in (
        requester_actor_id,
        (session.metadata or {}).get("slack_user_id") if session is not None else None,
        (session.metadata or {}).get("requester_actor_id") if session is not None else None,
    ):
        user_id = slack_user_id_from_actor(str(raw_actor or ""))
        if user_id and user_id not in candidate_ids:
            candidate_ids.append(user_id)
    if runtime_config is not None:
        for user_id in candidate_ids:
            resolved = resolve_slack_user_email(user_id, config=runtime_config)
            if resolved and "@" in resolved:
                return resolved
    return None


def _parse_source_channel(source_channel: str) -> tuple[str, str, str] | None:
    """Return (channel, channel_id, thread_id) for a workflow binding key."""
    raw = str(source_channel or "").strip()
    if not raw:
        return None
    # Admin chat rewrites `web:<id>` to `web:web:<id>`, and a later rewrite can
    # leave `web:web:web:<id>`. Collapse those to one thread id.
    if raw.startswith("web:"):
        tail = [part for part in raw.split(":") if part and part != "web"]
        if len(tail) == 1:
            return "web", "web", tail[0]
    parts = raw.split(":", 2)
    if len(parts) == 3 and parts[0] and parts[1]:
        return parts[0], parts[1], parts[2]
    # Admin Chat uses `web:{client-binding-id}` before program start rewrites it.
    if len(parts) == 2 and parts[0] == "web" and parts[1]:
        return "web", "web", parts[1]
    return None


def binding_key_from_source_channel(source_channel: str) -> str | None:
    """Canonical three-part binding key stored on workflows and sessions."""
    parsed = _parse_source_channel(source_channel)
    if parsed is None:
        return None
    channel, channel_id, thread_id = parsed
    return f"{channel}:{channel_id}:{thread_id}"


def _artifact_doc_title(
    artifact: dict[str, Any],
    *,
    display_name: str,
    workflow_type: str,
    step_name: str,
    workflow_id: str,
) -> str:
    title = artifact_title(artifact, fallback=display_name or "Workflow artifact")
    short_id = workflow_id.split("-")[0] if workflow_id else ""
    parts = [title, workflow_type.replace("_", " "), step_name.replace("_", " ")]
    if short_id:
        parts.append(short_id)
    return " — ".join(part for part in parts if part)


def _library_enabled(runtime_config: RuntimeConfig | None) -> bool:
    if runtime_config is None or runtime_config.connectors is None:
        return False
    gw = runtime_config.connectors.google_workspace
    if gw is None or not gw.enabled or not gw.artifact_publish.enabled:
        return False
    return bool(gw.artifact_publish.library.enabled)


def _maybe_publish_artifact_library(
    *,
    artifact: dict[str, Any],
    runtime_config: RuntimeConfig | None,
    tenant_id: str,
    agent_id: str,
    workflow_type: str,
    workflow_id: str,
    step_name: str,
    share_with_email: str | None,
    program_id: str = "",
    program_summary: str = "",
    existing_drive_library: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if runtime_config is None or not _library_enabled(runtime_config):
        return None
    return publish_program_artifacts(
        artifact=artifact,
        runtime_config=runtime_config,
        workflow_type=workflow_type,
        workflow_id=workflow_id,
        step_name=step_name,
        tenant_id=tenant_id,
        agent_id=agent_id or "pm-agent",
        program_id=program_id,
        program_summary=program_summary,
        share_with_email=share_with_email,
        existing_drive_library=existing_drive_library,
    )


def _maybe_publish_artifact_doc(
    *,
    artifact: dict[str, Any],
    runtime_config: RuntimeConfig | None,
    tenant_id: str,
    agent_id: str,
    display_name: str,
    workflow_type: str,
    step_name: str,
    workflow_id: str,
    share_with_email: str | None,
    max_length: int = 3900,
    skip: bool = False,
    presentation: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    if skip or runtime_config is None:
        return None
    gw = runtime_config.connectors.google_workspace if runtime_config.connectors else None
    if gw is None or not gw.enabled or not gw.artifact_publish.enabled:
        return None

    full_text = build_artifact_channel_text(
        artifact,
        display_name=display_name,
        step_name=step_name,
        agent_id=agent_id,
        presentation=presentation,
    )
    if full_text.startswith("I finished ") and "no readable content" in full_text:
        return None

    publish_cfg = gw.artifact_publish
    if not should_publish_artifact_text(
        len(full_text),
        max_channel_length=max_length,
        min_chars=int(publish_cfg.min_chars),
    ):
        return None

    publish_agent = agent_id or "pm-agent"
    return publish_markdown_to_google_doc(
        title=_artifact_doc_title(
            artifact,
            display_name=display_name,
            workflow_type=workflow_type,
            step_name=step_name,
            workflow_id=workflow_id,
        ),
        content=full_text,
        runtime_config=runtime_config,
        tenant_id=tenant_id,
        agent_id=publish_agent,
        share_with_email=share_with_email,
    )


def _append_library_links(message: str, library_result: dict[str, Any] | None) -> str:
    if not isinstance(library_result, dict):
        return message
    status = str(library_result.get("status") or "")
    if status not in {"published", "partial"}:
        if status == "error":
            error = str(library_result.get("error") or "unknown error")
            return message + (
                f"\n\n(Google Drive artifact library publish failed: {error})"
            )
        return message

    folder_link = str(library_result.get("root_web_view_link") or "")
    manifest_link = str(library_result.get("manifest_web_view_link") or "")
    if folder_link:
        message += f"\n\nArtifact library: {folder_link}"
    if manifest_link:
        message += f"\nManifest: {manifest_link}"

    folder_share = library_result.get("folder_shared_with")
    if isinstance(folder_share, dict) and folder_share.get("error"):
        message += (
            "\n\n(Google Drive folder sharing failed for your email. "
            "Ask an admin to grant access or open with your workspace account.)"
        )
    elif isinstance(folder_share, dict) and folder_share.get("email"):
        message += f"\n\n(Google Drive folder shared with {folder_share['email']}.)"

    errors = library_result.get("errors")
    if isinstance(errors, list) and errors:
        message += f"\n\n(Library publish warnings: {'; '.join(str(e) for e in errors)})"
    return message


def build_workflow_artifact_channel_message(
    artifact: dict[str, Any],
    *,
    display_name: str,
    workflow_type: str,
    workflow_id: str,
    step_name: str,
    agent_id: str,
    present_prompt: str,
    runtime_config: RuntimeConfig | None = None,
    session: SessionRecord | None = None,
    source_channel: str = "",
    share_with_email: str | None = None,
    requester_actor_id: str | None = None,
    program_id: str = "",
    program_summary: str = "",
    existing_drive_library: dict[str, Any] | None = None,
    max_length: int = 3900,
) -> dict[str, Any]:
    from ts_harness.workflows.presentation import workflow_step_presentation

    presentation = workflow_step_presentation(workflow_type, step_name)
    resolved_share_email = _resolve_share_email(
        runtime_config=runtime_config,
        session=session,
        share_with_email=share_with_email,
        source_channel=source_channel,
        requester_actor_id=requester_actor_id,
    )
    publish_cfg = None
    if runtime_config and runtime_config.connectors and runtime_config.connectors.google_workspace:
        publish_cfg = runtime_config.connectors.google_workspace.artifact_publish

    library_result = _maybe_publish_artifact_library(
        artifact=artifact,
        runtime_config=runtime_config,
        tenant_id=session.tenant_id if session is not None else "default",
        agent_id=agent_id,
        workflow_type=workflow_type,
        workflow_id=workflow_id,
        step_name=step_name,
        share_with_email=resolved_share_email,
        program_id=program_id,
        program_summary=program_summary,
        existing_drive_library=existing_drive_library,
    )
    library_ok = isinstance(library_result, dict) and library_result.get("status") in {
        "published",
        "partial",
    }

    doc_result = _maybe_publish_artifact_doc(
        artifact=artifact,
        runtime_config=runtime_config,
        tenant_id=session.tenant_id if session is not None else "default",
        agent_id=agent_id,
        display_name=display_name,
        workflow_type=workflow_type,
        step_name=step_name,
        workflow_id=workflow_id,
        share_with_email=resolved_share_email,
        max_length=max_length,
        skip=library_ok,
        presentation=presentation,
    )
    doc_url = None
    publish_full_text = False
    if isinstance(doc_result, dict) and doc_result.get("status") == "published":
        doc_url = str(doc_result.get("webViewLink") or "") or None
        publish_full_text = True
    elif library_ok:
        doc_url = str(library_result.get("manifest_web_view_link") or "") or None
        publish_full_text = bool(doc_url)

    message = format_artifact_for_channel(
        artifact,
        display_name=display_name,
        present_prompt=present_prompt,
        step_name=step_name,
        agent_id=agent_id,
        max_length=max_length,
        min_chars_for_doc=int(publish_cfg.min_chars if publish_cfg else 3000),
        doc_url=doc_url,
        publish_full_text=publish_full_text,
        presentation=presentation,
    )
    share_info = doc_result.get("sharedWith") if isinstance(doc_result, dict) else None
    if isinstance(share_info, dict) and share_info.get("error"):
        message += (
            "\n\n(Google Doc sharing failed for your email. "
            "Ask an admin to grant access or open with your workspace account.)"
        )
    elif doc_url and resolved_share_email and isinstance(share_info, dict) and share_info.get("email"):
        message += f"\n\n(Google Doc shared with {share_info['email']}.)"

    message = _append_library_links(message, library_result)

    return {
        "message": message,
        "google_doc": doc_result,
        "doc_url": doc_url,
        "drive_library": library_result,
    }


def _load_existing_drive_library(program_id: str) -> dict[str, Any] | None:
    if not program_id:
        return None
    try:
        from ts_harness.memory.client import get_memory_client

        payload = get_memory_client().get_program(program_id)
        program = payload.get("program") or {}
        metadata = program.get("metadata") or {}
        if isinstance(metadata, str):
            import json

            metadata = json.loads(metadata)
        artifacts = (metadata.get("artifacts") or {}) if isinstance(metadata, dict) else {}
        drive_library = artifacts.get("drive_library")
        return dict(drive_library) if isinstance(drive_library, dict) else None
    except Exception:
        return None


def _persist_drive_library(
    program_id: str,
    drive_library: dict[str, Any],
    *,
    actor_id: str = "workflow-runner",
) -> None:
    if not program_id or not drive_library:
        return
    try:
        from ts_harness.memory.client import get_memory_client

        get_memory_client().patch_program_artifacts(
            program_id,
            {"drive_library": drive_library},
            merge=True,
            actor=actor_id,
        )
    except Exception:
        pass


def present_workflow_artifact(
    *,
    workflow_id: str,
    workflow_type: str,
    tenant_id: str,
    step_name: str,
    artifact: dict[str, Any],
    source_channel: str,
    actor_id: str = "workflow-runner",
    agent_id: str = "",
    present_prompt: str = "inform",
    runtime_config: RuntimeConfig | None = None,
    share_with_email: str | None = None,
    requester_actor_id: str | None = None,
    program_id: str = "",
    program_summary: str = "",
) -> dict[str, Any]:
    if not artifact:
        return {"status": "skipped", "reason": "empty artifact"}

    binding = _parse_source_channel(source_channel)
    if binding is None:
        return {"status": "skipped", "reason": "no bound channel"}

    channel, channel_id, thread_id = binding
    display_name = workflow_type.replace("_", " ")
    skeleton = None
    try:
        skeleton = load_skeleton(workflow_type)
        display_name = str(getattr(skeleton, "display_name", "") or display_name)
    except FileNotFoundError:
        pass

    session = _session_for_workflow(
        workflow_id,
        tenant_id,
        source_channel,
        program_id=program_id,
    )
    existing_drive_library = _load_existing_drive_library(program_id)

    built = build_workflow_artifact_channel_message(
        artifact,
        display_name=display_name,
        workflow_type=workflow_type,
        workflow_id=workflow_id,
        step_name=step_name,
        agent_id=agent_id,
        present_prompt=present_prompt,
        runtime_config=runtime_config,
        session=session,
        source_channel=source_channel,
        share_with_email=share_with_email,
        requester_actor_id=requester_actor_id or actor_id,
        program_id=program_id,
        program_summary=program_summary,
        existing_drive_library=existing_drive_library,
    )
    message = str(built.get("message") or "")
    doc_result = built.get("google_doc")
    doc_url = built.get("doc_url")
    library_result = built.get("drive_library")

    if isinstance(library_result, dict) and library_result.get("drive_library") and program_id:
        _persist_drive_library(
            program_id,
            dict(library_result["drive_library"]),
            actor_id=actor_id,
        )

    if session is not None:
        metadata = write_specialist_handoff(
            dict(session.metadata or {}),
            step_name=step_name,
            agent_id=agent_id,
            output=artifact,
        )
        if doc_url:
            metadata["presentation_doc_url"] = doc_url
            if isinstance(doc_result, dict) and doc_result.get("documentId"):
                metadata["presentation_doc_id"] = doc_result.get("documentId")
            if isinstance(doc_result, dict) and doc_result.get("sharedWith"):
                metadata["presentation_doc_share"] = doc_result.get("sharedWith")
        if isinstance(library_result, dict) and library_result.get("root_web_view_link"):
            metadata["drive_library_folder_url"] = library_result.get("root_web_view_link")
            if library_result.get("manifest_web_view_link"):
                metadata["drive_library_manifest_url"] = library_result.get(
                    "manifest_web_view_link"
                )
        if skeleton is not None and present_prompt == "approval":
            tracking = get_phase_tracking(skeleton)
            if (
                tracking is not None
                and read_workflow_phase(metadata) != tracking.approved_value
            ):
                metadata = write_workflow_phase(
                    metadata,
                    tracking.awaiting_approval_value,
                )
        session.metadata = metadata
        get_session_store().save(session)
        get_transcript_store().append_messages(
            session.session_id,
            [{"role": "assistant", "content": message}],
        )

    if channel == "web":
        if session is None:
            return {
                "status": "error",
                "error": "no linked session for workflow",
                "step_name": step_name,
                "agent_id": agent_id,
            }
        send_result = {"status": "transcript"}
    else:
        send_result = get_channel_router().send(
            channel,
            {"channel": channel_id, "text": message, "thread_id": thread_id},
            tenant_id=tenant_id,
        )
        if send_result.get("error"):
            return {
                "status": "error",
                "error": str(send_result["error"]),
                "step_name": step_name,
                "agent_id": agent_id,
                "artifact_title": artifact_title(
                    artifact,
                    fallback=display_name,
                ),
            }

    result: dict[str, Any] = {
        "status": "presented",
        "workflow_id": workflow_id,
        "workflow_type": workflow_type,
        "step_name": step_name,
        "agent_id": agent_id,
        "present_prompt": present_prompt,
        "artifact_title": artifact_title(
            artifact,
            fallback=display_name,
        ),
        "channel": channel,
        "session_id": session.session_id if session is not None else None,
        "reply": send_result,
    }
    if isinstance(doc_result, dict):
        result["google_doc"] = doc_result
    if isinstance(library_result, dict):
        result["drive_library"] = library_result
    return result
