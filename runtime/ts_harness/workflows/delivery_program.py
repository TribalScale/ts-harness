"""Federated delivery program coordinator."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from typing import Any
import uuid

from ts_harness.agent.sessions import SessionRecord
from ts_harness.memory.client import MemoryClient, get_memory_client
from ts_harness.repo_paths import ensure_scripts_importable
from ts_harness.workflows.continuity import write_stage_snapshot
from ts_harness.workflows.materialize import load_skeleton, materialize_skeleton_tasks
from ts_harness.workflows.program_adapters import build_default_adapter_registry
from ts_harness.workflows.project_repos import with_project_repositories
from ts_harness.workflows.program_materialize import (
    automatic_edges_from,
    build_intake_for_node,
    extract_outputs_for_node,
    find_edge,
    load_pinned_skeleton,
    materialize_program_workflow,
    node_id_for_workflow_type,
    node_is_terminal,
    node_reentry,
    parse_program_snapshot,
    parse_resolved_workflow_versions,
    program_pin_fields,
    resolve_bridge_target_nodes_from_text,
    resolve_node_workflow,
    resolve_target_node,
    seed_list_reentry,
    select_workflow_type,
    validate_entry_against_snapshot,
)
from ts_harness.workflows.program_registry import get_program_definition_registry

ensure_scripts_importable()
from program_legacy_compat import frozen_legacy_compatibility_topology  # noqa: E402

_logger = logging.getLogger(__name__)

# Reverse maps retained for standalone workflow-type lookups. Topology itself
# comes from pinned snapshots or FROZEN_LEGACY_COMPATIBILITY_TOPOLOGY.
WORKFLOW_TYPE_TO_PHASE = {
    "prd_request": "prd",
    "jira_from_prd": "jira",
    "work_orders_from_prd": "work_orders",
    "implement_delivery": "implement",
    "implement_work_orders": "implement",
    "deploy_release": "deploy",
}
WORKFLOW_TYPE_TO_PLANNING_TRACK = {
    "jira_from_prd": "jira",
    "work_orders_from_prd": "work_orders",
}


def _compat_snapshot() -> dict[str, Any]:
    """In-memory topology for pre-migration rows with no persisted snapshot."""
    return frozen_legacy_compatibility_topology()


def resolve_workflow_type(phase: str, artifacts: dict[str, Any]) -> str:
    workflow_type = select_workflow_type(_compat_snapshot(), phase, artifacts)
    if not workflow_type:
        raise KeyError(f"unknown program phase: {phase}")
    return workflow_type


def phase_for_workflow_type(workflow_type: str) -> str | None:
    return WORKFLOW_TYPE_TO_PHASE.get(workflow_type)


def planning_track_for_workflow_type(workflow_type: str) -> str | None:
    return WORKFLOW_TYPE_TO_PLANNING_TRACK.get(workflow_type)


def bridge_target_for_source(source_workflow_type: str) -> str:
    skeleton = load_skeleton(source_workflow_type)
    bridge = dict(getattr(skeleton, "bridge", None) or {})
    fallback = select_workflow_type(_compat_snapshot(), "jira", {})
    return str(bridge.get("target_workflow_type") or fallback or "jira_from_prd")


def resolve_bridge_target_from_text(
    text: str,
    source_workflow_type: str = "prd_request",
    *,
    program_type: str = "software_delivery",
    snapshot: dict[str, Any] | None = None,
    source_node: str | None = None,
    artifacts: dict[str, Any] | None = None,
) -> str | None:
    """Resolve a bridge target workflow type from text via program snapshot edges.

    Falls back to legacy skeleton bridge hints when no program snapshot is available.
    When an explicit snapshot is provided, resolution is fail-closed to that graph.
    """
    explicit_snapshot = snapshot is not None
    active_snapshot = snapshot
    if active_snapshot is None and program_type:
        try:
            registry = get_program_definition_registry()
            registry.reload_if_changed()
            active_snapshot = registry.load(program_type)
        except Exception:
            active_snapshot = None

    if active_snapshot is not None:
        _target_node, target_workflow = resolve_bridge_target_nodes_from_text(
            text,
            source_workflow_type=source_workflow_type,
            snapshot=active_snapshot,
            source_node=source_node,
            artifacts=artifacts,
        )
        if target_workflow:
            return target_workflow
        if explicit_snapshot:
            return None

    # Legacy standalone skeleton bridge (non-program sessions / unavailable registry).
    lowered = (text or "").lower()
    try:
        skeleton = load_skeleton(source_workflow_type)
        bridge = dict(getattr(skeleton, "bridge", None) or {})
        targets = bridge.get("targets")
        if isinstance(targets, dict):
            for _key, cfg in targets.items():
                if not isinstance(cfg, dict):
                    continue
                signals = [str(s).lower() for s in (cfg.get("signals") or [])]
                if any(signal in lowered for signal in signals):
                    return str(cfg.get("target_workflow_type") or "") or None
        work_order_signals = [
            "work order",
            "work orders",
            "generate work orders",
            "plan work orders",
        ]
        if any(signal in lowered for signal in work_order_signals):
            return "work_orders_from_prd"
        jira_signals = [str(s).lower() for s in (bridge.get("signals") or [])]
        if any(signal in lowered for signal in jira_signals):
            return str(bridge.get("target_workflow_type") or "jira_from_prd")
    except FileNotFoundError:
        pass
    return None


def skeleton_program_phase(workflow_type: str) -> str | None:
    skeleton = load_skeleton(workflow_type)
    program = dict(getattr(skeleton, "program", None) or {})
    phase = str(program.get("phase") or "").strip()
    return phase or WORKFLOW_TYPE_TO_PHASE.get(workflow_type)


def _configured_tenant_ids() -> list[str]:
    try:
        from ts_harness.gateway.server import get_runtime

        tenants = getattr(getattr(get_runtime(), "config", None), "tenants", None) or []
        return [str(tenant.id) for tenant in tenants]
    except Exception:
        return []


def resolve_program_tenant(tenant_id: str | None) -> str:
    """Use the caller's tenant; fall back only when a single tenant is configured."""
    explicit = str(tenant_id or "").strip()
    if explicit:
        return explicit
    configured = _configured_tenant_ids()
    if len(configured) > 1:
        raise ValueError("tenant_id is required when more than one tenant is configured")
    fallback = configured[0] if configured else "default"
    _logger.warning("delivery program created without tenant_id; using %s", fallback)
    return fallback


def _live_program_defaults() -> dict[str, Any]:
    try:
        from ts_harness.gateway.server import get_runtime

        config = getattr(get_runtime(), "config", None)
        return dict(getattr(config, "program_defaults", None) or {})
    except Exception:
        return {}


def _parse_metadata(metadata: Any) -> dict[str, Any]:
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    return dict(metadata) if isinstance(metadata, dict) else {}


def _program_artifacts(program: dict[str, Any]) -> dict[str, Any]:
    metadata = _parse_metadata(program.get("metadata"))
    artifacts = metadata.get("artifacts")
    return dict(artifacts) if isinstance(artifacts, dict) else {}


@dataclass
class ProgramStartResult:
    program_id: str
    workflow_id: str
    workflow_type: str
    entry_phase: str
    task_ids: list[str]
    first_step: str


class DeliveryProgramService:
    def __init__(self, client: MemoryClient | None = None):
        self.client = client or get_memory_client()
        self._adapters = build_default_adapter_registry()

    def validate_entry(self, entry_phase: str, artifacts: dict[str, Any]) -> list[str]:
        return validate_entry_against_snapshot(
            _compat_snapshot(),
            entry_phase,
            artifacts,
            require_startable=True,
        )

    def create_program(
        self,
        *,
        entry_phase: str | None = None,
        entry_node: str | None = None,
        program_type: str = "software_delivery",
        summary: str,
        tenant_id: str | None = None,
        binding_key: str | None = None,
        source_channel: str | None = None,
        requester_actor_id: str | None = None,
        artifacts: dict[str, Any] | None = None,
        transcript_messages: list[dict[str, Any]] | None = None,
        extra_input: dict[str, Any] | None = None,
        actor: str = "orchestrator",
        pin_definition: bool = True,
        project_id: str | None = None,
        project_id_source: str | None = None,
        link_program_ids: list[str] | None = None,
        parent_program_id: str | None = None,
        department: str | None = None,
        owner_id: str | None = None,
    ) -> ProgramStartResult:
        resolved_entry = str(entry_node or entry_phase or "").strip()
        if not resolved_entry:
            raise ValueError("entry_phase or entry_node is required")
        tenant_id = resolve_program_tenant(tenant_id)

        artifact_bag = dict(artifacts or {})
        pin_fields: dict[str, Any] = {}
        snapshot: dict[str, Any] | None = None
        resolved_versions: dict[str, Any] = {}

        if pin_definition and program_type:
            try:
                registry = get_program_definition_registry()
                if not registry.reload_if_changed():
                    # Keep last-good registry; still allow built-ins already loaded.
                    pass
                pin_fields = program_pin_fields(program_type, registry=registry)
                snapshot = pin_fields["program_definition_snapshot"]
                resolved_versions = pin_fields["resolved_workflow_versions"]
            except FileNotFoundError:
                raise
            except Exception as exc:
                raise ValueError(
                    f"program definition unavailable for start: {program_type}: {exc}"
                ) from exc

        effective_snapshot = snapshot if snapshot is not None else _compat_snapshot()
        artifact_bag = seed_list_reentry(effective_snapshot, resolved_entry, artifact_bag)
        artifact_bag = with_project_repositories(
            self.client, artifact_bag, tenant_id=tenant_id, project_key=project_id
        )
        errors = validate_entry_against_snapshot(
            effective_snapshot,
            resolved_entry,
            artifact_bag,
            require_startable=True,
        )
        if errors:
            raise ValueError("; ".join(errors))

        channel = source_channel or binding_key
        program_id = str(uuid.uuid4())
        if not project_id:
            from ts_harness.workflows.project_identity import resolve_program_project

            project_id, project_id_source = resolve_program_project(
                artifact_bag,
                project_id=project_id,
            )
        from ts_harness.config.governance import governance_snapshot

        create_kwargs: dict[str, Any] = {
            "metadata": {"governance_snapshot": governance_snapshot(_live_program_defaults())}
        }
        parent = str(parent_program_id or "").strip() or None
        if parent:
            create_kwargs["parent_program_id"] = parent
        if owner_id:
            create_kwargs["owner_id"] = owner_id
        created = self.client.create_program(
            program_id=program_id,
            tenant_id=tenant_id,
            summary=summary,
            entry_phase=resolved_entry,
            current_phase=resolved_entry,
            binding_key=binding_key,
            source_channel=channel,
            requester_actor_id=requester_actor_id,
            artifacts=artifact_bag,
            actor=actor,
            project_id=project_id,
            project_id_source=project_id_source,
            department=department,
            **create_kwargs,
            **pin_fields,
        )
        root_program_id = (
            str((created or {}).get("root_program_id") or "").strip()
            if isinstance(created, dict)
            else ""
        ) or program_id
        link_warnings: list[str] = []
        for target_id in list(link_program_ids or []):
            try:
                self.client.create_program_link(
                    program_id,
                    to_program_id=str(target_id),
                    tenant_id=tenant_id,
                    actor=actor,
                )
            except Exception:
                link_warnings.append(f"program_link_failed:{target_id}")
        from ts_harness.workflows.program_brief import attach_related_program_context

        attach_related_program_context(
            self.client,
            program_id=program_id,
            tenant_id=tenant_id,
            project_id=project_id,
            actor=actor,
            extra_warnings=link_warnings,
        )

        workflow_type, definition_version = resolve_node_workflow(
            effective_snapshot,
            resolved_entry,
            artifact_bag,
            resolved_versions,
        )
        intake = build_intake_for_node(
            snapshot=effective_snapshot,
            node_id=resolved_entry,
            workflow_type=workflow_type,
            summary=summary,
            artifacts=artifact_bag,
            transcript_messages=transcript_messages,
            extra_input=extra_input,
            adapters=self._adapters,
        )
        intake.setdefault("program_id", program_id)
        intake.setdefault("root_program_id", root_program_id)
        skeleton = load_pinned_skeleton(workflow_type, definition_version)
        workflow_id, task_ids, first_step = materialize_program_workflow(
            client=self.client,
            skeleton=skeleton,
            workflow_type=workflow_type,
            program_id=program_id,
            program_phase=resolved_entry,
            tenant_id=tenant_id,
            binding_key=channel,
            requester_actor_id=requester_actor_id,
            intake_input=intake,
            actor=actor,
        )
        return ProgramStartResult(
            program_id=program_id,
            workflow_id=workflow_id,
            workflow_type=workflow_type,
            entry_phase=resolved_entry,
            task_ids=task_ids,
            first_step=first_step,
        )

    def link_workflow(
        self,
        *,
        program_id: str,
        phase: str,
        workflow_id: str,
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self.client.link_workflow_to_program(
            program_id,
            workflow_id=workflow_id,
            program_phase=phase,
            actor=actor,
        )

    def advance_program(
        self,
        *,
        program_id: str,
        target_phase: str | None = None,
        target_workflow_type: str | None = None,
        summary: str = "",
        artifacts: dict[str, Any] | None = None,
        tenant_id: str = "default",
        binding_key: str | None = None,
        requester_actor_id: str | None = None,
        source_workflow_id: str | None = None,
        actor: str = "orchestrator",
        decision: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        program_payload = self.client.get_program(program_id)
        program = program_payload.get("program") or {}
        current_phase = str(program.get("current_phase") or "")
        snapshot = parse_program_snapshot(program.get("program_definition_snapshot"))
        # Pre-migration rows keep null snapshots in DB; use frozen payload in-memory only.
        effective_snapshot = snapshot if snapshot is not None else _compat_snapshot()
        resolved_versions = parse_resolved_workflow_versions(
            program.get("resolved_workflow_versions")
        )

        if target_workflow_type and not target_phase:
            target_phase = node_id_for_workflow_type(
                effective_snapshot,
                target_workflow_type,
            )
            if not target_phase:
                target_phase = phase_for_workflow_type(target_workflow_type)

        existing_artifacts = _program_artifacts(program)
        merged_artifacts = with_project_repositories(
            self.client,
            {**existing_artifacts, **(artifacts or {})},
            tenant_id=str(program.get("tenant_id") or "default"),
            project_key=program.get("project_id"),
        )

        reentry = node_reentry(effective_snapshot, current_phase)
        allow_reentry = (
            bool(current_phase)
            and current_phase == target_phase
            and (str(reentry.get("target_node") or "") == current_phase or current_phase == "implement")
        )
        resolved_target = resolve_target_node(
            effective_snapshot,
            current_phase,
            target_phase,
            allow_reentry=allow_reentry,
        )
        edge = find_edge(
            effective_snapshot,
            source=current_phase,
            target=resolved_target,
        )
        if edge is not None and isinstance(edge.get("artifact_patch"), dict):
            merged_artifacts.update(dict(edge["artifact_patch"]))
        elif target_workflow_type and snapshot is None:
            track = planning_track_for_workflow_type(target_workflow_type)
            if track:
                merged_artifacts["planning_track"] = track
        if resolved_target != current_phase:
            merged_artifacts = seed_list_reentry(effective_snapshot, resolved_target, merged_artifacts)
        errors = validate_entry_against_snapshot(
            effective_snapshot,
            resolved_target,
            merged_artifacts,
            require_startable=False,
        )
        if errors:
            raise ValueError("; ".join(errors))

        if artifacts or target_workflow_type or target_phase:
            self.client.patch_program_artifacts(
                program_id,
                merged_artifacts,
                merge=True,
                actor=actor,
            )

        decision_fields = {
            key: value
            for key, value in dict(decision or {}).items()
            if key in {"actor_email", "actor_roles", "reason"} and value
        }
        self.client.advance_program(
            program_id,
            target_phase=resolved_target,
            actor=actor,
            **decision_fields,
        )

        workflow_type, definition_version = resolve_node_workflow(
            effective_snapshot,
            resolved_target,
            merged_artifacts,
            resolved_versions,
        )
        if target_workflow_type:
            workflow_type = target_workflow_type
            track = planning_track_for_workflow_type(target_workflow_type)
            artifact_override = dict(merged_artifacts)
            if track:
                artifact_override["planning_track"] = track
            _, definition_version = resolve_node_workflow(
                effective_snapshot,
                resolved_target,
                artifact_override,
                resolved_versions,
            )
            # Prefer explicit target type; keep pin for that type when present.
            pin_entry = resolved_versions.get(resolved_target) or {}
            pins = pin_entry.get("pins") if isinstance(pin_entry, dict) else {}
            if isinstance(pins, dict) and target_workflow_type in pins:
                definition_version = str(pins[target_workflow_type])
        intake = build_intake_for_node(
            snapshot=effective_snapshot,
            node_id=resolved_target,
            workflow_type=workflow_type,
            summary=summary or str(program.get("summary") or ""),
            artifacts=merged_artifacts,
            source_workflow_id=source_workflow_id,
            adapters=self._adapters,
        )
        intake.setdefault("program_id", program_id)
        intake.setdefault(
            "root_program_id",
            str(program.get("root_program_id") or "").strip() or program_id,
        )
        skeleton = load_pinned_skeleton(workflow_type, definition_version)
        workflow_id, task_ids, first_step = materialize_program_workflow(
            client=self.client,
            skeleton=skeleton,
            workflow_type=workflow_type,
            program_id=program_id,
            program_phase=resolved_target,
            tenant_id=tenant_id,
            binding_key=binding_key or program.get("source_channel") or program.get("binding_key"),
            requester_actor_id=requester_actor_id or program.get("requester_actor_id"),
            intake_input=intake,
            source_workflow_id=source_workflow_id,
            actor=actor,
        )
        return {
            "program_id": program_id,
            "target_phase": resolved_target,
            "workflow_id": workflow_id,
            "workflow_type": workflow_type,
            "task_ids": task_ids,
            "first_step": first_step,
        }

    def get_status(self, program_id: str) -> dict[str, Any]:
        return self.client.get_program_status(program_id)

    def resolve_active_workflow(self, session: SessionRecord) -> str | None:
        metadata = session.metadata or {}
        active = metadata.get("active_workflow_id") or metadata.get("workflow_id")
        return str(active) if active else None

    def has_remaining_work_orders(self, artifacts: dict[str, Any]) -> bool:
        all_ids = list(artifacts.get("work_order_ids") or [])
        if not all_ids:
            return False
        completed = set(artifacts.get("completed_work_order_ids") or [])
        return any(wo_id not in completed for wo_id in all_ids)

    def on_workflow_terminal(
        self,
        workflow_id: str,
        *,
        actor: str = "workflow-runner",
    ) -> dict[str, Any] | None:
        payload = self.client.get_workflow(workflow_id)
        workflow = payload.get("workflow") or {}
        program_id = workflow.get("program_id")
        if not program_id:
            return None

        tasks = list(payload.get("tasks") or [])
        program_phase = str(workflow.get("program_phase") or "")
        workflow_type = str(workflow.get("workflow_type") or "")

        program_payload = self.client.get_program(str(program_id))
        program = program_payload.get("program") or {}
        snapshot = parse_program_snapshot(program.get("program_definition_snapshot"))
        effective_snapshot = snapshot if snapshot is not None else _compat_snapshot()
        existing_artifacts = _program_artifacts(program)

        if program_phase:
            artifact_patch = extract_outputs_for_node(
                snapshot=effective_snapshot,
                node_id=program_phase,
                workflow_type=workflow_type,
                tasks=tasks,
                artifacts=existing_artifacts,
                adapters=self._adapters,
            )
            terminal = node_is_terminal(effective_snapshot, program_phase)
        else:
            artifact_patch = self._legacy_extract_outputs(
                workflow_type=workflow_type,
                tasks=tasks,
                existing_artifacts=existing_artifacts,
            )
            skeleton = load_skeleton(workflow_type)
            program_cfg = dict(getattr(skeleton, "program", None) or {})
            terminal = bool(program_cfg.get("terminal"))

        if artifact_patch:
            write_stage_snapshot(
                tenant_id=str(workflow.get("tenant_id") or "default"),
                workflow_id=str(workflow_id),
                program_id=str(program_id),
                stage_name=program_phase or workflow_type or "stage",
                output=artifact_patch,
                memory_client=self.client,
            )
            self._adopt_owner_program(program, artifact_patch, actor=actor)
            self._record_artifact_refs(
                str(program_id),
                workflow_id=str(workflow_id),
                artifact_patch=artifact_patch,
                actor=actor,
            )

        if terminal and all(task.get("status") == "succeeded" for task in tasks):
            self.client.patch_program_artifacts(
                str(program_id),
                {"completed_at_phase": program_phase},
                merge=True,
                actor=actor,
            )

        return {
            "program_id": str(program_id),
            "program_phase": program_phase,
            "artifacts_patched": list(artifact_patch.keys()),
            "terminal": terminal,
            "automatic_targets": [
                str(edge.get("target"))
                for edge in (
                    automatic_edges_from(effective_snapshot, program_phase)
                    if program_phase
                    else []
                )
                if edge.get("target")
            ],
        }

    def _adopt_owner_program(
        self,
        program: dict[str, Any],
        artifact_patch: dict[str, Any],
        *,
        actor: str,
    ) -> str | None:
        """Attach a program to the owner named in a Jira pointer block."""
        owner = str(artifact_patch.get("owner_program_id") or "").strip()
        program_id = str(program.get("id") or "").strip()
        if not owner or not program_id or owner == program_id:
            return None
        if str(program.get("parent_program_id") or "").strip():
            return None
        try:
            self.client.patch_program(
                program_id,
                tenant_id=str(program.get("tenant_id") or "default"),
                parent_program_id=owner,
                actor=actor,
            )
        except Exception as exc:
            _logger.warning("owner program adoption failed for %s: %s", program_id, exc)
            return None
        return owner

    def _record_artifact_refs(
        self,
        program_id: str,
        *,
        workflow_id: str,
        artifact_patch: dict[str, Any],
        actor: str,
    ) -> list[tuple[str, str]]:
        refs: list[tuple[str, str]] = []
        epic = str(artifact_patch.get("jira_epic_key") or "").strip()
        if epic:
            refs.append(("jira_issue", epic))
        for key in artifact_patch.get("jira_story_keys") or []:
            if str(key).strip():
                refs.append(("jira_issue", str(key).strip()))
        pr_urls = list(artifact_patch.get("pr_urls") or [])
        if artifact_patch.get("pr_url"):
            pr_urls.append(artifact_patch["pr_url"])
        for url in dict.fromkeys(str(u).strip() for u in pr_urls if str(u).strip()):
            refs.append(("pull_request", url))
        recorder = getattr(self.client, "record_program_external_ref", None)
        if not callable(recorder):
            return []
        recorded: list[tuple[str, str]] = []
        for kind, ref in refs:
            try:
                recorder(program_id, kind=kind, ref=ref, workflow_id=workflow_id, actor=actor)
                recorded.append((kind, ref))
            except Exception as exc:
                _logger.warning("external ref %s %s not recorded: %s", kind, ref, exc)
        return recorded

    def _legacy_extract_outputs(
        self,
        *,
        workflow_type: str,
        tasks: list[dict[str, Any]],
        existing_artifacts: dict[str, Any],
    ) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        for task in tasks:
            if task.get("status") == "succeeded" and isinstance(task.get("output"), dict):
                outputs.update(task["output"])

        artifact_patch: dict[str, Any] = {}
        self._record_qa_artifacts(tasks, artifact_patch)
        if outputs.get("approved_prd") or outputs.get("prd"):
            artifact_patch["approved_prd"] = outputs.get("approved_prd") or outputs.get("prd")
        if outputs.get("epic_key") or outputs.get("jira_epic_key"):
            artifact_patch["jira_epic_key"] = outputs.get("epic_key") or outputs.get("jira_epic_key")
        if outputs.get("pr_url"):
            artifact_patch["pr_url"] = outputs.get("pr_url")
        merge_sha = (
            outputs.get("merge_sha")
            or outputs.get("merge_commit_sha")
            or outputs.get("sha")
        )
        if merge_sha:
            artifact_patch["merge_sha"] = merge_sha
        if outputs.get("planner_complete") is not None:
            artifact_patch["planner_complete"] = outputs.get("planner_complete")
        if outputs.get("work_orders_manifest_path"):
            artifact_patch["work_orders_manifest_path"] = outputs.get("work_orders_manifest_path")
        if outputs.get("work_order_ids"):
            artifact_patch["work_order_ids"] = outputs.get("work_order_ids")
        if outputs.get("next_work_order_id"):
            artifact_patch["next_work_order_id"] = outputs.get("next_work_order_id")
        if outputs.get("work_order_ref"):
            artifact_patch["work_order_ref"] = outputs.get("work_order_ref")
        if outputs.get("work_orders_handoff") is not None:
            artifact_patch["work_orders_handoff"] = outputs.get("work_orders_handoff")

        markdown = outputs.get("work_orders_markdown")
        if not markdown and isinstance(outputs.get("artifacts"), dict):
            markdown = outputs["artifacts"].get("work_orders_markdown")
        if markdown:
            artifact_patch["work_orders_markdown"] = markdown

        if workflow_type == "work_orders_from_prd":
            stage_artifact_steps = {
                "refine_requirements": "refinery_artifacts",
                "design_blueprints": "foundry_artifacts",
                "plan_work_orders": "planner_artifacts",
            }
            for task in tasks:
                if task.get("status") != "succeeded":
                    continue
                step = str(task.get("step_name") or "")
                bag_key = stage_artifact_steps.get(step)
                if not bag_key:
                    continue
                output = task.get("output") or {}
                if not isinstance(output, dict):
                    continue
                bag = output.get("artifacts")
                if isinstance(bag, dict) and bag:
                    artifact_patch[bag_key] = bag

        wo_id = outputs.get("work_order_id") or outputs.get("next_work_order_id")
        if wo_id and workflow_type == "implement_work_orders":
            completed = list(existing_artifacts.get("completed_work_order_ids") or [])
            if str(wo_id) not in completed:
                completed.append(str(wo_id))
            artifact_patch["completed_work_order_ids"] = completed
        return artifact_patch

    @staticmethod
    def _record_qa_artifacts(
        tasks: list[dict[str, Any]],
        artifact_patch: dict[str, Any],
    ) -> None:
        """Record qa_gate outcome into the program bag when present."""
        qa_task = next(
            (
                task
                for task in tasks
                if str(task.get("step_name") or "") == "qa_gate"
                and task.get("status") == "succeeded"
                and isinstance(task.get("output"), dict)
            ),
            None,
        )
        if qa_task is None:
            return
        output = dict(qa_task.get("output") or {})
        result = output.get("result") if isinstance(output.get("result"), dict) else {}
        prs = result.get("prs") if isinstance(result.get("prs"), list) else []
        pr_urls = [
            str(pr.get("pr_url")).strip()
            for pr in prs
            if isinstance(pr, dict) and pr.get("pr_url")
        ]
        if output.get("status"):
            artifact_patch["qa_status"] = output.get("status")
        summary = result.get("summary") or output.get("summary")
        if summary:
            artifact_patch["qa_summary"] = summary
        if prs:
            artifact_patch["qa_evidence"] = prs
        if pr_urls:
            artifact_patch["pr_urls"] = pr_urls
            artifact_patch.setdefault("pr_url", pr_urls[0])

    def _materialize_phase_workflow(
        self,
        *,
        workflow_type: str,
        program_id: str,
        program_phase: str,
        tenant_id: str,
        binding_key: str | None,
        requester_actor_id: str | None,
        summary: str,
        artifacts: dict[str, Any],
        transcript_messages: list[dict[str, Any]] | None = None,
        extra_input: dict[str, Any] | None = None,
        source_workflow_id: str | None = None,
        actor: str = "orchestrator",
        definition_version: str | None = None,
    ) -> tuple[str, list[str], str]:
        if definition_version:
            skeleton = load_pinned_skeleton(workflow_type, definition_version)
        else:
            skeleton = load_skeleton(workflow_type)

        # Prefer adapter intake when phase maps to a known adapter.
        adapter_names = {
            "prd": "extract_prd_outputs",
            "jira": "extract_jira_outputs",
            "work_orders": "extract_work_orders_outputs",
            "implement": "extract_implement_outputs",
            "deploy": "extract_deploy_outputs",
        }
        adapter_name = adapter_names.get(program_phase)
        if adapter_name:
            intake_input = self._adapters.build_inputs(
                adapter_name,
                summary=summary,
                artifacts=artifacts,
                transcript_messages=transcript_messages,
                extra_input=extra_input,
                workflow_type=workflow_type,
            )
            if source_workflow_id:
                intake_input["source_workflow_id"] = source_workflow_id
            return materialize_program_workflow(
                client=self.client,
                skeleton=skeleton,
                workflow_type=workflow_type,
                program_id=program_id,
                program_phase=program_phase,
                tenant_id=tenant_id,
                binding_key=binding_key,
                requester_actor_id=requester_actor_id,
                intake_input=intake_input,
                source_workflow_id=source_workflow_id,
                actor=actor,
            )

        # Fallback legacy intake shaping for unknown phases.
        workflow_id = str(uuid.uuid4())
        self.client.create_workflow(
            workflow_id=workflow_id,
            workflow_type=workflow_type,
            tenant_id=tenant_id,
            definition_version=skeleton.definition_version,
            source_channel=binding_key,
            requester_actor_id=requester_actor_id,
            correlation_id=f"program:{program_id}",
            parent_workflow_id=source_workflow_id,
            program_id=program_id,
            program_phase=program_phase,
            actor=actor,
        )
        self.client.link_workflow_to_program(
            program_id,
            workflow_id=workflow_id,
            program_phase=program_phase,
            actor=actor,
        )
        intake = skeleton.intake_stage_name()
        stage_inputs: dict[str, dict[str, Any]] = {
            intake: {"summary": summary, **(extra_input or {})}
        }
        if source_workflow_id:
            stage_inputs[intake]["source_workflow_id"] = source_workflow_id
        task_bodies = materialize_skeleton_tasks(
            skeleton,
            workflow_id=workflow_id,
            tenant_id=tenant_id,
            external_ref=binding_key,
            stage_inputs=stage_inputs,
        )
        name_to_id: dict[str, str] = {}
        task_ids: list[str] = []
        for body in task_bodies:
            depends_on_names = list(body.get("depends_on") or [])
            payload = {
                "workflow_id": workflow_id,
                "workflow_type": workflow_type,
                "tenant_id": tenant_id,
                "step_name": body["step_name"],
                "agent_id": body["agent_id"],
                "input": body.get("input") or {},
                "approval_required": body.get("approval_required", False),
                "max_retries": body.get("max_retries", 0),
                "external_ref": body.get("external_ref"),
                "contract": body.get("contract") or {},
                "definition_version": body.get("definition_version"),
                "actor": actor,
            }
            if depends_on_names:
                payload["depends_on"] = [
                    name_to_id[name] for name in depends_on_names if name in name_to_id
                ]
            result = self.client.create_task(payload)
            task_id = str(result["id"])
            name_to_id[body["step_name"]] = task_id
            task_ids.append(task_id)
        return workflow_id, task_ids, intake


_default_service: DeliveryProgramService | None = None


def get_delivery_program_service() -> DeliveryProgramService:
    global _default_service
    if _default_service is None:
        _default_service = DeliveryProgramService()
    return _default_service
