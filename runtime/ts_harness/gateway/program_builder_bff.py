"""Admin BFF for the agent-assisted program builder.

A builder session is a chat with ``builder-agent`` owned by one person. The agent writes
workflow, program, and routing drafts as that person; the person activates them here.
"""

from __future__ import annotations

import uuid
from typing import Any

from ts_harness.gateway.auth import AuthContext, AuthError
from ts_harness.workflows.versioned_definition_store import ConflictError

BUILDER_CHANNEL = "program_builder"
OWNER_KEY = "builder_owner"
ACTOR_KEY = "builder_actor"
BUNDLE_KEY = "builder_bundle"
ROUTING_IDENTITY = "channel_routing"
PARTS = ("workflow", "program", "routing")


class BuilderNotReadyError(ValueError):
    def __init__(self, message: str, state: dict[str, Any]):
        super().__init__(message)
        self.state = state


# ── identity ──────────────────────────────────────────────────────────────────


def auth_snapshot(auth: AuthContext) -> dict[str, Any]:
    return {
        "actor_id": auth.actor_id,
        "tenant_id": auth.tenant_id,
        "email": auth.email,
        "display_name": auth.display_name,
        "groups": list(auth.groups or []),
        "roles": list(auth.roles or []),
        "auth_provider": auth.auth_provider,
    }


def auth_from_snapshot(snapshot: dict[str, Any]) -> AuthContext:
    return AuthContext(
        actor_id=str(snapshot.get("actor_id") or ""),
        tenant_id=str(snapshot.get("tenant_id") or "default"),
        email=snapshot.get("email"),
        display_name=snapshot.get("display_name"),
        groups=list(snapshot.get("groups") or []),
        roles=list(snapshot.get("roles") or []),
        auth_provider=str(snapshot.get("auth_provider") or "static"),
    )


def builder_agent_id(config: Any) -> str:
    builder = getattr(config, "program_builder", None)
    return str(getattr(builder, "agent_id", "") or "builder-agent")


# ── sessions ──────────────────────────────────────────────────────────────────


def _sessions():
    from ts_harness.agent.sessions import get_session_store

    return get_session_store()


def owned_builder_session(session_id: str, auth: AuthContext):
    session = _sessions().get(session_id)
    if (
        session is None
        or session.tenant_id != auth.tenant_id
        or session.source != BUILDER_CHANNEL
    ):
        raise KeyError("builder session not found")
    if (session.metadata or {}).get(OWNER_KEY) != auth.actor_id:
        raise AuthError("builder session belongs to another person", status=403)
    return session


def _new_builder_session(runtime: Any, auth: AuthContext):
    session = _sessions().get_or_create(
        tenant_id=auth.tenant_id,
        source=BUILDER_CHANNEL,
        agent_id=builder_agent_id(runtime.config),
        channel=BUILDER_CHANNEL,
        thread_id=f"builder:{uuid.uuid4()}",
    )
    session.metadata = {**(session.metadata or {}), OWNER_KEY: auth.actor_id, "mode": BUILDER_CHANNEL}
    _sessions().save(session)
    return session


def read_bundle(session) -> dict[str, Any]:
    bundle = dict((session.metadata or {}).get(BUNDLE_KEY) or {})
    bundle.setdefault("skill_ids", [])
    bundle.setdefault("activated_types", {"workflow": [], "program": []})
    return bundle


def save_bundle(session, bundle: dict[str, Any]) -> None:
    session.metadata = {**(session.metadata or {}), BUNDLE_KEY: bundle}
    _sessions().save(session)


def builder_actor(session) -> AuthContext | None:
    snapshot = (session.metadata or {}).get(ACTOR_KEY)
    if not isinstance(snapshot, dict) or not snapshot.get("actor_id"):
        return None
    return auth_from_snapshot(snapshot)


def track_skill_proposal(session, skill_id: str) -> None:
    bundle = read_bundle(session)
    if skill_id and skill_id not in bundle["skill_ids"]:
        bundle["skill_ids"] = [*bundle["skill_ids"], skill_id]
        save_bundle(session, bundle)


def list_builder_sessions(auth: AuthContext) -> dict[str, Any]:
    sessions = [
        session
        for session in _sessions().list_for_tenant(auth.tenant_id)
        if session.source == BUILDER_CHANNEL
        and (session.metadata or {}).get(OWNER_KEY) == auth.actor_id
    ]
    sessions.sort(key=lambda item: str(item.started_at or ""), reverse=True)
    payload = []
    for session in sessions[:100]:
        bundle = read_bundle(session)
        payload.append(
            {
                "session_id": session.session_id,
                "agent_id": session.agent_id,
                "source": session.source,
                "started_at": session.started_at,
                "workflow_type": (bundle.get("workflow") or {}).get("workflow_type"),
                "program_type": (bundle.get("program") or {}).get("program_type"),
            }
        )
    return {"sessions": payload, "total": len(payload)}


# ── draft stores ──────────────────────────────────────────────────────────────


def _draft_api(kind: str) -> dict[str, Any]:
    if kind == "workflow":
        from ts_harness.gateway import workflow_definitions_bff as bff

        return {
            "get": bff.workflow_definition_store().get_draft,
            "create": bff.create_workflow_definition_draft_admin,
            "update": bff.update_workflow_definition_draft_admin,
            "activate": bff.activate_workflow_definition_draft_admin,
        }
    if kind == "program":
        from ts_harness.gateway import program_definitions_bff as bff

        return {
            "get": bff.program_definition_store().get_draft,
            "create": bff.create_program_definition_draft_admin,
            "update": bff.update_program_definition_draft_admin,
            "activate": bff.activate_program_definition_draft_admin,
        }
    from ts_harness.gateway import routing_definitions_bff as bff
    from ts_harness.workflows.routing_definition_store import routing_definition_store

    return {
        "get": routing_definition_store().get_draft,
        "create": bff.create_routing_definition_draft_admin,
        "update": bff.update_routing_definition_draft_admin,
        "activate": bff.activate_routing_definition_draft_admin,
    }


def _load_draft(kind: str, draft_id: str | None) -> dict[str, Any] | None:
    if not draft_id:
        return None
    try:
        return _draft_api(kind)["get"](draft_id)
    except (FileNotFoundError, ValueError):
        return None


def _write_draft(
    runtime: Any,
    auth: AuthContext,
    kind: str,
    part: dict[str, Any],
    definition: dict[str, Any],
) -> dict[str, Any]:
    """Create or update the bundle's draft for ``kind``; mutates ``part`` with id/revision."""
    api = _draft_api(kind)
    current = _load_draft(kind, part.get("draft_id"))
    try:
        if current is None:
            draft = api["create"](runtime, auth, {"definition": definition})["draft"]
            status = "created"
        elif current["revision"] != part.get("revision"):
            part["revision"] = current["revision"]
            return {
                "status": "revision_conflict",
                "draft_id": current["id"],
                "message": (
                    "the person edited this draft in its builder; merge your change into "
                    "current_definition and call propose_program_bundle again"
                ),
                "current_definition": current["definition"],
            }
        else:
            draft = api["update"](
                runtime,
                auth,
                current["id"],
                {"definition": definition, "expected_revision": current["revision"]},
            )["draft"]
            status = "updated"
    except ConflictError:
        latest = _load_draft(kind, part.get("draft_id"))
        if latest is not None:
            part["revision"] = latest["revision"]
        return {
            "status": "revision_conflict",
            "draft_id": part.get("draft_id"),
            "message": "draft changed while saving; read it again and retry",
            "current_definition": (latest or {}).get("definition"),
        }
    except (ValueError, KeyError, TypeError) as exc:
        return {"status": "error", "error": str(exc)}
    part["draft_id"] = draft["id"]
    part["revision"] = draft["revision"]
    part.pop("activated", None)
    return {"status": status, "draft_id": draft["id"], "revision": draft["revision"]}


# ── catalogs ──────────────────────────────────────────────────────────────────


def _existing_workflow_types() -> set[str]:
    from ts_harness.workflows.materialize import build_workflow_catalog

    return set(build_workflow_catalog())


def _existing_program_types() -> set[str]:
    from ts_harness.gateway.program_definitions_bff import (
        built_in_program_types,
        program_definition_store,
    )

    active = {
        str(item.get("program_type") or "")
        for item in program_definition_store().list_active()
    }
    return (built_in_program_types() | active) - {""}


def _active_routing() -> dict[str, Any]:
    from ts_harness.workflows.routing_definition_store import routing_definition_store

    return routing_definition_store().get_active(ROUTING_IDENTITY) or {
        "definition_type": ROUTING_IDENTITY,
        "definition_version": "0.0.0",
        "intents": [],
    }


def _next_routing_version() -> str:
    from ts_harness.workflows.routing_definition_store import routing_definition_store

    store = routing_definition_store()
    current = str(_active_routing().get("definition_version") or "0.0.0")
    parts = [int(piece) if piece.isdigit() else 0 for piece in current.split(".")[:3]]
    while len(parts) < 3:
        parts.append(0)
    while True:
        parts[2] += 1
        candidate = ".".join(str(piece) for piece in parts)
        if not store._history_version_revisions(ROUTING_IDENTITY, candidate):
            return candidate


def builder_catalog(runtime: Any, session) -> dict[str, Any]:
    from ts_harness.gateway.program_definitions_bff import program_definition_catalog_admin
    from ts_harness.gateway.workflow_definitions_bff import (
        _agent_tools,
        _catalogs,
        _publish_tools,
    )

    catalogs = _catalogs(runtime)
    agent_tools = _agent_tools(runtime)
    bundle = read_bundle(session)
    drafts: dict[str, Any] = {}
    for kind in PARTS:
        part = bundle.get(kind) or {}
        draft = _load_draft(kind, part.get("draft_id"))
        drafts[kind] = (
            {"draft_id": draft["id"], "definition": draft["definition"]}
            if draft is not None
            else ({"activated": part["activated"]} if part.get("activated") else None)
        )
    return {
        "hook_names": sorted(catalogs["hook_names"]),
        "agent_ids": sorted(catalogs["agent_ids"]),
        "skill_ids": sorted(catalogs["skill_ids"]),
        "built_in_workflow_types": sorted(catalogs["built_in_types"]),
        "existing_workflow_types": sorted(_existing_workflow_types()),
        "existing_program_types": sorted(_existing_program_types()),
        "agent_tools": {agent: sorted(tools) for agent, tools in agent_tools.items()},
        "program_catalog": program_definition_catalog_admin(runtime),
        "routing_intents": list(_active_routing().get("intents") or []),
        "publish_tools": sorted(_publish_tools(runtime, agent_tools)),
        "bundle_drafts": drafts,
        "proposed_skill_ids": list(bundle["skill_ids"]),
    }


# ── validation ────────────────────────────────────────────────────────────────


def _issue_dict(issue: Any) -> dict[str, str]:
    if isinstance(issue, dict):
        return {key: str(issue.get(key) or "") for key in ("severity", "code", "path", "message")}
    return {
        "severity": issue.severity,
        "code": issue.code,
        "path": issue.path,
        "message": issue.message,
    }


def _promote_publish_issues(issues: list[dict[str, str]]) -> list[dict[str, str]]:
    from ts_harness.workflows.definition_validation import PUBLISH_GATE_CODES

    return [
        {**issue, "severity": "error"} if issue["code"] in PUBLISH_GATE_CODES else issue
        for issue in issues
    ]


def _pending_workflow_entry(definition: dict[str, Any]) -> dict[str, Any]:
    from ts_harness.workflows.materialize import contract_output_keys

    version = str(definition.get("definition_version") or "").strip()
    return {
        "workflow_type": str(definition.get("workflow_type") or ""),
        "status": "pending",
        "available_versions": [version] if version else [],
        "active_version": version or None,
        "resolvable": True,
        "contract_output_keys": sorted(contract_output_keys(definition)),
        "workflow_input_keys": [],
    }


def _skill_statuses(runtime: Any, skill_ids: list[str]) -> list[dict[str, str]]:
    from ts_harness.gateway.workflow_definitions_bff import _catalogs
    from ts_harness.skills.repository import skill_repository

    approved = _catalogs(runtime)["skill_ids"]
    drafts: dict[str, str] = {}
    if any(skill_id not in approved for skill_id in skill_ids):
        try:
            drafts = {
                str(item.get("id")): str(item.get("approval_status") or "draft")
                for item in skill_repository().list_drafts()
            }
        except Exception:
            drafts = {}
    return [
        {
            "skill_id": skill_id,
            "status": "approved" if skill_id in approved else drafts.get(skill_id, "missing"),
        }
        for skill_id in skill_ids
    ]


def validate_bundle(runtime: Any, bundle: dict[str, Any]) -> dict[str, Any]:
    """Validate the bundle's drafts against live catalogs plus each other.

    Publish-gate warnings are errors here: an agent-built program must gate publishing.
    """
    from ts_harness.gateway.program_definitions_bff import _catalogs as program_catalogs
    from ts_harness.gateway.routing_definitions_bff import _known_catalogs
    from ts_harness.gateway.workflow_definitions_bff import workflow_validation_kwargs
    from ts_harness.routing.overlay import validate_overlay_payload
    from ts_harness.workflows.definition_validation import validate_workflow_definition
    from ts_harness.workflows.program_definition_validation import (
        validate_program_definition,
    )

    drafts = {kind: _load_draft(kind, (bundle.get(kind) or {}).get("draft_id")) for kind in PARTS}
    state: dict[str, Any] = {}
    for kind in PARTS:
        part = bundle.get(kind) or {}
        draft = drafts[kind]
        if draft is not None:
            status = "draft"
        elif part.get("activated"):
            status = "activated"
        elif part.get("draft_id"):
            status = "missing"
        else:
            status = "none"
        state[kind] = {
            "status": status,
            "draft_id": draft["id"] if draft else part.get("draft_id"),
            "revision": draft["revision"] if draft else None,
            "activated": part.get("activated"),
            "issues": [],
        }
        if kind == "workflow":
            state[kind]["workflow_type"] = part.get("workflow_type")
        elif kind == "program":
            state[kind]["program_type"] = part.get("program_type")
        else:
            state[kind]["intent_id"] = part.get("intent_id")
            state[kind]["signals"] = list(part.get("signals") or [])
            if part.get("blocked"):
                state[kind]["blocked"] = part["blocked"]

    workflow_draft = drafts["workflow"]
    if workflow_draft is not None:
        _skeleton, issues = validate_workflow_definition(
            workflow_draft["definition"],
            **workflow_validation_kwargs(runtime),
        )
        state["workflow"]["issues"] = _promote_publish_issues([_issue_dict(i) for i in issues])

    program_draft = drafts["program"]
    if program_draft is not None:
        catalogs = program_catalogs()
        if workflow_draft is not None:
            pending = _pending_workflow_entry(workflow_draft["definition"])
            if pending["workflow_type"] and pending["workflow_type"] not in catalogs["workflow_catalog"]:
                catalogs["workflow_catalog"] = {
                    **catalogs["workflow_catalog"],
                    pending["workflow_type"]: pending,
                }
        _loaded, issues = validate_program_definition(
            program_draft["definition"],
            config=getattr(runtime, "config", None),
            **catalogs,
        )
        state["program"]["issues"] = [_issue_dict(i) for i in issues]

    routing_draft = drafts["routing"]
    if routing_draft is not None:
        programs, workflows = _known_catalogs()
        if program_draft is not None:
            programs = programs | {str(program_draft["definition"].get("program_type") or "")}
        if workflow_draft is not None:
            workflows = workflows | {str(workflow_draft["definition"].get("workflow_type") or "")}
        _canonical, issues = validate_overlay_payload(
            routing_draft.get("definition") or {},
            known_program_types=programs,
            known_workflow_types=workflows,
        )
        state["routing"]["issues"] = [_issue_dict(i) for i in issues]

    for kind in PARTS:
        entry = state[kind]
        entry["valid"] = not any(issue["severity"] == "error" for issue in entry["issues"])

    skills = _skill_statuses(runtime, list(bundle.get("skill_ids") or []))
    state["skills"] = skills
    problems: list[str] = []
    for kind in ("workflow", "program"):
        if state[kind]["status"] not in {"draft", "activated"}:
            problems.append(f"{kind} draft is {state[kind]['status']}")
    if state["routing"]["status"] == "missing":
        problems.append("routing draft is missing")
    for kind in PARTS:
        if not state[kind]["valid"]:
            problems.append(f"{kind} draft has errors")
    for skill in skills:
        if skill["status"] != "approved":
            problems.append(f"skill {skill['skill_id']} is {skill['status']}")
    state["problems"] = problems
    state["ready"] = not problems
    return state


# ── propose ───────────────────────────────────────────────────────────────────


def _routing_definition(
    intent: dict[str, Any],
    *,
    previous_intent_id: str | None,
) -> dict[str, Any]:
    active = _active_routing()
    intent_id = str(intent.get("id") or "").strip()
    drop = {intent_id, str(previous_intent_id or "")} - {""}
    intents = [
        dict(item)
        for item in list(active.get("intents") or [])
        if str(item.get("id") or "") not in drop
    ]
    intents.append(dict(intent))
    return {
        "definition_type": ROUTING_IDENTITY,
        "definition_version": _next_routing_version(),
        "intents": intents,
    }


def propose_bundle(runtime: Any, session, args: dict[str, Any]) -> dict[str, Any]:
    from ts_harness.workflows.routing_definition_store import routing_definition_store

    auth = builder_actor(session)
    if auth is None:
        return {"error": "builder session has no person attached"}
    workflow = args.get("workflow")
    program = args.get("program")
    intent = args.get("routing_intent")
    if not isinstance(workflow, dict) or not isinstance(program, dict):
        return {"error": "workflow and program definitions are required objects"}
    if intent is not None and not isinstance(intent, dict):
        return {"error": "routing_intent must be an object"}

    bundle = read_bundle(session)
    activated = bundle["activated_types"]
    workflow_type = str(workflow.get("workflow_type") or "").strip()
    program_type = str(program.get("program_type") or "").strip()
    taken: list[str] = []
    if workflow_type in _existing_workflow_types() and workflow_type not in activated.get("workflow", []):
        taken.append(f"workflow_type {workflow_type}")
    if program_type in _existing_program_types() and program_type not in activated.get("program", []):
        taken.append(f"program_type {program_type}")
    if taken:
        return {
            "error": "type_exists",
            "message": (
                ", ".join(taken)
                + " already exists. The builder creates new programs only; pick a new type."
            ),
        }

    results: dict[str, Any] = {}
    workflow_part = dict(bundle.get("workflow") or {})
    results["workflow"] = _write_draft(runtime, auth, "workflow", workflow_part, workflow)
    workflow_part["workflow_type"] = workflow_type
    bundle["workflow"] = workflow_part
    save_bundle(session, bundle)

    program_part = dict(bundle.get("program") or {})
    results["program"] = _write_draft(runtime, auth, "program", program_part, program)
    program_part["program_type"] = program_type
    bundle["program"] = program_part
    save_bundle(session, bundle)

    if intent is not None:
        routing_part = dict(bundle.get("routing") or {})
        routing_part.pop("blocked", None)
        others = [
            draft
            for draft in routing_definition_store().list_drafts()
            if draft.get("id") != routing_part.get("draft_id")
        ]
        if others:
            routing_part["blocked"] = "finish or discard the existing routing draft in Settings"
            results["routing"] = {"status": "routing_blocked", "message": routing_part["blocked"]}
        else:
            definition = _routing_definition(intent, previous_intent_id=routing_part.get("intent_id"))
            results["routing"] = _write_draft(runtime, auth, "routing", routing_part, definition)
            if results["routing"].get("status") in {"created", "updated"}:
                routing_part["intent_id"] = str(intent.get("id") or "")
                routing_part["signals"] = [str(item) for item in intent.get("signals") or []]
        bundle["routing"] = routing_part
        save_bundle(session, bundle)

    state = validate_bundle(runtime, bundle)
    for kind in PARTS:
        if kind in results:
            results[kind]["issues"] = state[kind]["issues"]
            results[kind]["valid"] = state[kind]["valid"]
    results["skills"] = state["skills"]
    results["ready_to_activate"] = state["ready"]
    results["problems"] = state["problems"]
    return results


# ── page endpoints ────────────────────────────────────────────────────────────


def _intake_text(intake: dict[str, Any]) -> str:
    labels = {
        "name": "Program name",
        "purpose": "Purpose",
        "stages": "Stages",
        "channels": "Channels",
        "trigger_phrases": "Trigger phrases",
    }
    lines = ["Program intake:"]
    for key, label in labels.items():
        value = intake.get(key)
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value if str(item).strip())
        text = str(value or "").strip()
        if text:
            lines.append(f"- {label}: {text}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _bundle_context(bundle: dict[str, Any]) -> str:
    lines = []
    for kind in PARTS:
        part = bundle.get(kind) or {}
        if part.get("draft_id"):
            lines.append(f"{kind} draft {part['draft_id']}")
        elif part.get("activated"):
            lines.append(f"{kind} activated")
    if bundle.get("skill_ids"):
        lines.append("proposed skills: " + ", ".join(bundle["skill_ids"]))
    if not lines:
        return ""
    return "[system context] Builder bundle so far: " + "; ".join(lines) + "."


def handle_builder_message(runtime: Any, auth: AuthContext, body: dict[str, Any]) -> dict[str, Any]:
    from ts_harness.agent.transcript import get_transcript_store

    text = str(body.get("text") or "").strip()
    if not text:
        raise ValueError("text is required")
    session_id = str(body.get("session_id") or "").strip()
    session = owned_builder_session(session_id, auth) if session_id else _new_builder_session(runtime, auth)

    with runtime._session_lock(session.session_id):
        session.metadata = {**(session.metadata or {}), ACTOR_KEY: auth_snapshot(auth)}
        _sessions().save(session)

        transcript = get_transcript_store()
        history = transcript.load_messages(session.session_id)
        intake = body.get("intake")
        model_text = text
        if not session_id and isinstance(intake, dict):
            intake_text = _intake_text(intake)
            if intake_text:
                model_text = f"{intake_text}\n\n{text}"

        agent_id = session.agent_id
        agent_cfg = runtime.config.get_agent(agent_id)
        skills, diagnostics = runtime.skill_catalog.resolve_for_channel_chat(
            agent_cfg,
            runtime.config,
            tenant_id=auth.tenant_id,
        )
        skill_bundle = runtime.skill_catalog.build_prompt_bundle(skills)
        system_context = _bundle_context(read_bundle(session))
        runtime.audit.append(
            tenant_id=auth.tenant_id,
            action_type="program_builder_chat",
            actor_id=auth.actor_id,
            payload={
                "session_id": session.session_id,
                "agent_id": agent_id,
                "skills": skill_bundle["audit"],
                "skill_diagnostics": diagnostics,
            },
        )
        initial_message_count = 1 + len(history) + (1 if system_context else 0) + 1
        result = runtime.agent.run_channel_chat(
            tenant_id=auth.tenant_id,
            agent_id=agent_id,
            text=model_text,
            session_id=session.session_id,
            history=history,
            skill_prompt=skill_bundle["prompt"],
            source=BUILDER_CHANNEL,
            session_mode=BUILDER_CHANNEL,
            system_context=system_context,
            actor_email=auth.email,
        )
        new_messages = (
            result.messages[initial_message_count:]
            if len(result.messages) > initial_message_count
            else []
        )
        if new_messages:
            transcript.append_turn(session.session_id, user_text=model_text, assistant_messages=new_messages)
        elif result.status == "succeeded":
            transcript.append_turn(
                session.session_id,
                user_text=model_text,
                assistant_messages=[
                    {"role": "assistant", "content": str((result.output or {}).get("summary") or "")}
                ],
            )
    return {
        "status": result.status,
        "session_id": session.session_id,
        "output": result.output,
        "error": result.error,
    }


def builder_session_state(runtime: Any, auth: AuthContext, session_id: str) -> dict[str, Any]:
    session = owned_builder_session(session_id, auth)
    return {"session_id": session.session_id, **validate_bundle(runtime, read_bundle(session))}


def _reload_after(kind: str, runtime: Any, result: dict[str, Any]) -> tuple[bool, str]:
    try:
        if kind == "workflow":
            from ts_harness.workflows.engine import get_workflow_engine

            reload = getattr(get_workflow_engine(), "reload_definitions", None)
            return (bool(reload()) if callable(reload) else True), ""
        if kind == "program":
            from ts_harness.workflows.program_registry import get_program_definition_registry

            reload = getattr(get_program_definition_registry(), "reload_definitions", None)
            return (bool(reload()) if callable(reload) else True), ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if result.get("reload_ok") is False:
        return False, str(result.get("warning") or "routing reload failed")
    return True, ""


def activate_builder_bundle(
    runtime: Any,
    auth: AuthContext,
    session_id: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    session = owned_builder_session(session_id, auth)
    expected = body.get("expected_revisions") or {}
    if not isinstance(expected, dict):
        raise ValueError("expected_revisions must be an object")
    with runtime._session_lock(session.session_id):
        bundle = read_bundle(session)
        state = validate_bundle(runtime, bundle)
        if not state["ready"]:
            raise BuilderNotReadyError("bundle is not ready: " + "; ".join(state["problems"]), state)
        steps: list[dict[str, Any]] = []
        failed = False
        for kind in PARTS:
            part = dict(bundle.get(kind) or {})
            if failed:
                steps.append({"kind": kind, "status": "not_run"})
                continue
            if not part.get("draft_id"):
                steps.append(
                    {"kind": kind, "status": "already_active" if part.get("activated") else "skipped"}
                )
                continue
            revision = str(expected.get(kind) or "")
            if not revision:
                steps.append({"kind": kind, "status": "failed", "error": f"expected_revisions.{kind} is required"})
                failed = True
                continue
            try:
                result = _draft_api(kind)["activate"](
                    runtime, auth, part["draft_id"], {"expected_revision": revision}
                )
            except ConflictError:
                steps.append(
                    {
                        "kind": kind,
                        "status": "failed",
                        "error": "draft changed since the page last loaded; reload and retry",
                    }
                )
                failed = True
                continue
            except Exception as exc:  # noqa: BLE001
                steps.append({"kind": kind, "status": "failed", "error": str(exc)})
                failed = True
                continue
            activated = dict(result.get("active") or result.get("activated") or {})
            part["activated"] = activated
            part["draft_id"] = None
            part["revision"] = None
            bundle[kind] = part
            identity = part.get("workflow_type") if kind == "workflow" else part.get("program_type")
            if kind in {"workflow", "program"} and identity:
                types = list(bundle["activated_types"].get(kind) or [])
                if identity not in types:
                    bundle["activated_types"][kind] = [*types, identity]
            save_bundle(session, bundle)
            runtime.audit.append(
                tenant_id=auth.tenant_id,
                action_type=f"{kind}_definition_activated",
                actor_id=auth.actor_id,
                payload={**activated, "source": BUILDER_CHANNEL, "session_id": session.session_id},
            )
            reload_ok, reload_error = _reload_after(kind, runtime, result)
            if not reload_ok:
                steps.append(
                    {
                        "kind": kind,
                        "status": "failed",
                        "error": reload_error or "activated on disk but runtime reload failed",
                        "activated": activated,
                    }
                )
                failed = True
                continue
            steps.append({"kind": kind, "status": "activated", "activated": activated})
    return {
        "ok": not failed,
        "steps": steps,
        "state": {"session_id": session.session_id, **validate_bundle(runtime, read_bundle(session))},
    }
