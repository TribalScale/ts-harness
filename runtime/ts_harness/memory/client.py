"""Memory-api client with tenant scope compatibility layer."""

from __future__ import annotations

from typing import Any
import json
import os
import urllib.error
import urllib.request

from ts_harness.tools.policy import ToolPolicyContext


DEFAULT_MEMORY_API_URL = os.environ.get("MEMORY_API_URL", "http://127.0.0.1:18792")


class ProgramNotFound(RuntimeError):
    """The memory API has no program for this path."""

_DECISION_KEYS = ("actor_email", "actor_roles", "actor_permissions", "reason", "program_id", "tenant_id")


def _decision_fields(decision: dict[str, Any]) -> dict[str, Any]:
    return {key: decision[key] for key in _DECISION_KEYS if decision.get(key)}


class MemoryClient:
    def __init__(self, base_url: str = DEFAULT_MEMORY_API_URL):
        self.base_url = base_url.rstrip("/")

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if os.environ.get("MEMORY_API_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['MEMORY_API_TOKEN']}"
        from ts_harness.observability.logging import current_trace_id, traceparent_header

        trace_id = current_trace_id()
        if trace_id:
            headers["traceparent"] = traceparent_header(trace_id)
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode() or str(exc)
            if exc.code == 404:
                raise ProgramNotFound(body) from exc
            raise RuntimeError(body) from exc
        return json.loads(raw) if raw else {}

    def _program_path(self, program_id: str, suffix: str = "") -> str:
        from urllib.parse import quote

        path = f"/programs/{quote(str(program_id), safe='')}"
        if suffix:
            path = f"{path}/{suffix.lstrip('/')}"
        return path

    def remember(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        content: str,
        memory_type: str = "semantic",
        tags: list[str] | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/memory/remember",
            {
                "tenant_id": tenant_id,
                "scope": scope or f"tenant:{tenant_id}:agent:{agent_id}",
                "content": content,
                "type": memory_type,
                "tags": tags or [],
            },
        )

    def recall(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        query: str,
        limit: int = 5,
        scope: str | None = None,
        department_scope: str | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "tenant_id": tenant_id,
            "scope": scope or f"tenant:{tenant_id}:agent:{agent_id}",
            "query": query,
            "limit": limit,
        }
        if department_scope:
            payload["department_scope"] = department_scope
        result = self._request(
            "POST",
            "/memory/recall",
            payload,
        )
        return list(result.get("memories") or result.get("results") or [])

    def forget(self, *, tenant_id: str, memory_id: str, action: str = "expire") -> dict[str, Any]:
        return self._request(
            "POST",
            "/memory/forget",
            {"tenant_id": tenant_id, "scope": f"tenant:{tenant_id}", "id": memory_id, "action": action},
        )

    def health(self) -> bool:
        try:
            self._request("GET", "/health")
            return True
        except Exception:
            return False

    def create_workflow(
        self,
        *,
        workflow_id: str,
        workflow_type: str,
        tenant_id: str = "default",
        definition_version: str = "1.0.0",
        source_channel: str | None = None,
        requester_actor_id: str | None = None,
        correlation_id: str | None = None,
        parent_workflow_id: str | None = None,
        program_id: str | None = None,
        program_phase: str | None = None,
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": workflow_id,
            "workflow_type": workflow_type,
            "tenant_id": tenant_id,
            "definition_version": definition_version,
            "source_channel": source_channel,
            "requester_actor_id": requester_actor_id,
            "correlation_id": correlation_id,
            "actor": actor,
        }
        if parent_workflow_id:
            body["parent_workflow_id"] = parent_workflow_id
        if program_id:
            body["program_id"] = program_id
        if program_phase:
            body["program_phase"] = program_phase
        return self._request("POST", "/workflows/create", body)

    def create_task(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/workflow/task", body)

    def create_tasks_bulk(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/workflow/tasks", body)

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workflows/{workflow_id}")

    def get_workflow_children(self, workflow_id: str) -> list[dict[str, Any]]:
        result = self._request("GET", f"/workflows/{workflow_id}/children")
        return list(result.get("children") or [])

    def find_workflow_by_ref(self, external_ref: str) -> dict[str, Any]:
        from urllib.parse import quote

        return self._request("GET", f"/workflows/by-ref?external_ref={quote(external_ref, safe='')}")

    def get_workflow_tasks(self, workflow_id: str) -> dict[str, Any]:
        return self._request("GET", f"/workflow/{workflow_id}")

    def approve_task(self, task_id: str, *, actor: str, tenant_id: str | None = None, **decision: Any) -> dict[str, Any]:
        body = {"id": task_id, "actor": actor, **_decision_fields(decision)}
        if tenant_id:
            body["tenant_id"] = tenant_id
        return self._request("POST", "/workflow/approve", body)

    def reject_task(self, task_id: str, *, actor: str, tenant_id: str | None = None, **decision: Any) -> dict[str, Any]:
        body = {"id": task_id, "actor": actor, **_decision_fields(decision)}
        if tenant_id:
            body["tenant_id"] = tenant_id
        return self._request("POST", "/workflow/reject", body)

    def record_decision(
        self,
        *,
        kind: str,
        decision: str,
        actor_id: str,
        tenant_id: str = "default",
        program_id: str | None = None,
        workflow_id: str | None = None,
        task_id: str | None = None,
        actor_email: str | None = None,
        actor_roles: list[str] | None = None,
        reason: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "kind": kind,
            "decision": decision,
            "actor_id": actor_id,
            "tenant_id": tenant_id,
        }
        for key, value in (
            ("program_id", program_id),
            ("workflow_id", workflow_id),
            ("task_id", task_id),
            ("actor_email", actor_email),
            ("actor_roles", actor_roles),
            ("reason", reason),
            ("details", details),
        ):
            if value:
                body[key] = value
        return self._request("POST", "/decisions", body)

    def list_decisions(
        self,
        *,
        program_id: str | None = None,
        workflow_id: str | None = None,
        kind: str | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import urlencode

        params = {
            key: value
            for key, value in (
                ("program_id", program_id),
                ("workflow_id", workflow_id),
                ("kind", kind),
            )
            if value
        }
        return self._request("GET", f"/decisions?{urlencode(params)}")

    def patch_task_input(self, task_id: str, input_data: dict[str, Any], *, actor: str = "orchestrator") -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/workflow/task/{task_id}/input",
            {"input": input_data, "actor": actor},
        )

    def propose_plan(
        self,
        workflow_id: str,
        parent_task_id: str,
        proposed_tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/workflow/plan/propose",
            {
                "workflow_id": workflow_id,
                "parent_task_id": parent_task_id,
                "proposed_tasks": proposed_tasks,
                "actor": "workflow-runner",
            },
        )

    def block_task(
        self,
        task_id: str,
        error: str,
        output: dict[str, Any] | None = None,
        *,
        actor: str = "workflow-runner",
    ) -> dict[str, Any]:
        return self.block_task_api(task_id, error, output=output, actor=actor)

    def block_task_api(
        self,
        task_id: str,
        error: str,
        output: dict[str, Any] | None = None,
        *,
        actor: str = "workflow-runner",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"id": task_id, "error": error, "actor": actor}
        if output is not None:
            body["output"] = output
        return self._request("POST", "/workflow/block", body)

    def complete_task_api(
        self,
        task_id: str,
        output: dict[str, Any],
        *,
        actor: str = "workflow-runner",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/workflow/complete",
            {"id": task_id, "output": output, "actor": actor},
        )

    def cancel_task(
        self,
        task_id: str,
        *,
        actor: str = "orchestrator",
        reason: str = "",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"id": task_id, "actor": actor}
        if reason:
            body["error"] = reason
        return self._request("POST", "/workflow/cancel", body)

    def set_task_wait(
        self,
        task_id: str,
        *,
        wait_token: str,
        expected_event_type: str,
        actor: str = "workflow-runner",
        escalate_after_seconds: int | None = None,
        expires_after_seconds: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": task_id,
            "wait_token": wait_token,
            "expected_event_type": expected_event_type,
            "actor": actor,
        }
        if escalate_after_seconds:
            body["escalate_after_seconds"] = escalate_after_seconds
        if expires_after_seconds:
            body["expires_after_seconds"] = expires_after_seconds
        return self._request("POST", "/workflow/task/wait", body)

    def list_due_waits(self, *, tenant_id: str = "default") -> dict[str, Any]:
        return self._request("POST", "/workflow/waits/due", {"tenant_id": tenant_id})

    def mark_wait_deadline(
        self,
        task_id: str,
        *,
        kind: str,
        recommended_action: str = "",
        details: dict[str, Any] | None = None,
        actor: str = "escalate-stale-waits",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"id": task_id, "kind": kind, "actor": actor}
        if recommended_action:
            body["recommended_action"] = recommended_action
        if details:
            body["details"] = details
        return self._request("POST", "/workflow/task/deadline", body)

    def clear_task_wait(self, task_id: str, *, actor: str = "orchestrator") -> dict[str, Any]:
        return self._request("POST", "/workflow/task/wait/clear", {"id": task_id, "actor": actor})

    def resume_tool_approval(self, task_id: str, approval_id: str, *, actor: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/workflow/task/resume-approval",
            {"id": task_id, "approval_id": approval_id, "actor": actor},
        )

    def cancel_workflow(self, workflow_id: str, *, actor: str, reason: str = "") -> dict[str, Any]:
        return self._request(
            "POST",
            f"/workflows/{workflow_id}/cancel",
            {"actor": actor, "reason": reason},
        )

    def complete_workflow(
        self,
        workflow_id: str,
        *,
        actor: str,
        reason: str = "completed",
        superseded_by: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"actor": actor, "reason": reason}
        if superseded_by:
            body["superseded_by"] = superseded_by
        return self._request("POST", f"/workflows/{workflow_id}/complete", body)

    def update_workflow_compensation(self, workflow_id: str, status: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/workflows/{workflow_id}/compensation",
            {"compensation_status": status},
        )

    def resume_external(
        self,
        workflow_id: str,
        *,
        event_type: str,
        payload: dict[str, Any],
        actor: str = "orchestrator",
        task_output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "workflow_id": workflow_id,
            "event_type": event_type,
            "payload": payload,
            "actor": actor,
        }
        if task_output:
            body["task_output"] = task_output
        return self._request("POST", "/workflow/resume-external", body)

    def find_external_waits(
        self,
        *,
        event_type: str = "",
        pr_url: str = "",
        tenant_id: str = "default",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/workflow/find-external-waits",
            {
                "event_type": event_type,
                "pr_url": pr_url,
                "tenant_id": tenant_id,
            },
        )

    def get_workflow_status(self, workflow_id: str) -> dict[str, Any]:
        from ts_harness.workflows.operability import build_operability_status

        payload = self.get_workflow(workflow_id)
        tasks = list(payload.get("tasks") or [])
        workflow = payload.get("workflow") or {}
        by_id = {str(task.get("id")): task for task in tasks if task.get("id")}
        pending_approvals = []
        for task in tasks:
            if not task.get("approval_required"):
                continue
            if task.get("approval_status") != "pending" or task.get("status") != "pending":
                continue
            deps = [str(dep_id) for dep_id in (task.get("depends_on") or [])]
            if all(str(by_id.get(dep_id, {}).get("status") or "").lower() == "succeeded" for dep_id in deps):
                pending_approvals.append(task)
        running = [task for task in tasks if task.get("status") == "running"]
        succeeded = [task for task in tasks if task.get("status") == "succeeded"]
        artifacts: dict[str, Any] = {}
        program_id = workflow.get("program_id")
        if program_id and hasattr(self, "get_program"):
            try:
                program_payload = self.get_program(str(program_id))
                program = (program_payload or {}).get("program") or {}
                metadata = program.get("metadata") or {}
                if isinstance(metadata, str):
                    import json

                    metadata = json.loads(metadata)
                raw_artifacts = metadata.get("artifacts") if isinstance(metadata, dict) else {}
                if isinstance(raw_artifacts, dict):
                    artifacts = raw_artifacts
            except Exception:
                artifacts = {}
        operability = build_operability_status(
            workflow=workflow,
            tasks=tasks,
            artifacts=artifacts,
        )
        return {
            "workflow_id": workflow_id,
            "workflow_type": workflow.get("workflow_type"),
            "status": workflow.get("status", "running"),
            "tasks": tasks,
            "pending_approvals": pending_approvals,
            "running": running,
            "succeeded_count": len(succeeded),
            "total_tasks": len(tasks),
            **operability,
        }

    def create_program(
        self,
        *,
        program_id: str | None = None,
        tenant_id: str = "default",
        summary: str,
        entry_phase: str,
        current_phase: str | None = None,
        binding_key: str | None = None,
        source_channel: str | None = None,
        requester_actor_id: str | None = None,
        artifacts: dict[str, Any] | None = None,
        program_type: str | None = None,
        program_definition_version: str | None = None,
        program_definition_revision: str | None = None,
        program_definition_snapshot: dict[str, Any] | None = None,
        resolved_workflow_versions: dict[str, Any] | None = None,
        project_id: str | None = None,
        project_id_source: str | None = None,
        excluded_from_autoshare: bool = False,
        parent_program_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor_email: str | None = None,
        department: str | None = None,
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "tenant_id": tenant_id,
            "summary": summary,
            "entry_phase": entry_phase,
            "current_phase": current_phase or entry_phase,
            "binding_key": binding_key,
            "source_channel": source_channel,
            "requester_actor_id": requester_actor_id,
            "actor": actor,
        }
        if program_id:
            body["id"] = program_id
        if artifacts:
            body["artifacts"] = artifacts
        if program_type is not None:
            body["program_type"] = program_type
        if program_definition_version is not None:
            body["program_definition_version"] = program_definition_version
        if program_definition_revision is not None:
            body["program_definition_revision"] = program_definition_revision
        if program_definition_snapshot is not None:
            body["program_definition_snapshot"] = program_definition_snapshot
        if resolved_workflow_versions is not None:
            body["resolved_workflow_versions"] = resolved_workflow_versions
        if project_id is not None:
            body["project_id"] = project_id
        if project_id_source is not None:
            body["project_id_source"] = project_id_source
        if excluded_from_autoshare:
            body["excluded_from_autoshare"] = excluded_from_autoshare
        if parent_program_id:
            body["parent_program_id"] = parent_program_id
        if metadata:
            body["metadata"] = metadata
        if actor_email:
            body["actor_email"] = actor_email
        if department:
            body["department"] = department
        return self._request("POST", "/programs/create", body)

    def list_program_children(self, program_id: str) -> dict[str, Any]:
        return self._request("GET", self._program_path(program_id, "children"))

    def record_program_external_ref(
        self,
        program_id: str,
        *,
        kind: str,
        ref: str,
        state: str = "open",
        workflow_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"kind": kind, "ref": ref, "state": state, "actor": actor}
        if workflow_id:
            body["workflow_id"] = workflow_id
        if metadata:
            body["metadata"] = metadata
        return self._request("POST", self._program_path(program_id, "external-refs"), body)

    def list_program_external_refs(
        self,
        program_id: str,
        *,
        include_descendants: bool = False,
    ) -> dict[str, Any]:
        suffix = "?include_descendants=true" if include_descendants else ""
        return self._request("GET", self._program_path(program_id, "external-refs") + suffix)

    def record_program_compensation(
        self,
        program_id: str,
        *,
        status: str,
        actions: list[dict[str, Any]],
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            self._program_path(program_id, "compensation"),
            {"status": status, "actions": actions, "actor": actor},
        )

    def record_release(self, program_id: str, **fields: Any) -> dict[str, Any]:
        body = {key: value for key, value in fields.items() if value is not None}
        return self._request("POST", self._program_path(program_id, "releases"), body)

    def list_releases(self, program_id: str, *, include_descendants: bool = False) -> dict[str, Any]:
        suffix = "?include_descendants=true" if include_descendants else ""
        return self._request("GET", self._program_path(program_id, "releases") + suffix)

    def get_program_evidence(self, program_id: str) -> dict[str, Any]:
        return self._request("GET", self._program_path(program_id, "evidence"))

    def record_program_evidence_export(
        self,
        program_id: str,
        *,
        sha256: str,
        actor: str,
        tenant_id: str,
        generated_at: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            self._program_path(program_id, "evidence/exported"),
            {
                "sha256": sha256,
                "actor": actor,
                "tenant_id": tenant_id,
                "generated_at": generated_at,
            },
        )

    def last_successful_release(
        self,
        *,
        repo: str,
        environment: str,
        tenant_id: str = "default",
        exclude_sha: str | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import urlencode

        params = {"tenant_id": tenant_id, "repo": repo, "environment": environment}
        if exclude_sha:
            params["exclude_sha"] = exclude_sha
        return self._request("GET", f"/releases/last-success?{urlencode(params)}")

    def find_external_refs(self, *, kind: str, ref: str, tenant_id: str | None = None) -> dict[str, Any]:
        from urllib.parse import urlencode

        params = {"kind": kind, "ref": ref}
        if tenant_id:
            params["tenant_id"] = tenant_id
        return self._request("GET", f"/external-refs?{urlencode(params)}")

    def list_programs(
        self,
        *,
        tenant_id: str = "default",
        status: str | None = None,
        project_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        roots_only: bool = False,
        phase: str | None = None,
        owner: str | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import urlencode

        params: dict[str, Any] = {
            "tenant_id": tenant_id,
            "limit": limit,
            "offset": offset,
        }
        if status:
            params["status"] = status
        if project_id:
            params["project_id"] = project_id
        if roots_only:
            params["roots_only"] = "true"
        if phase:
            params["phase"] = phase
        if owner:
            params["owner"] = owner
        return self._request("GET", f"/programs?{urlencode(params)}")

    def get_program(self, program_id: str) -> dict[str, Any]:
        return self._request("GET", self._program_path(program_id))

    def get_program_status(self, program_id: str) -> dict[str, Any]:
        return self._request("GET", self._program_path(program_id, "status"))

    def find_program_by_binding(self, binding_key: str, *, tenant_id: str = "default") -> dict[str, Any]:
        from urllib.parse import quote

        return self._request(
            "GET",
            f"/programs/by-ref?binding_key={quote(binding_key, safe='')}&tenant_id={quote(tenant_id, safe='')}",
        )

    def advance_program(
        self,
        program_id: str,
        *,
        target_phase: str | None = None,
        actor: str = "orchestrator",
        **decision: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"actor": actor, **_decision_fields(decision)}
        if target_phase:
            body["target_phase"] = target_phase
        return self._request("POST", self._program_path(program_id, "advance"), body)

    def cancel_program(
        self,
        program_id: str,
        *,
        actor: str,
        reason: str = "",
        cascade: bool = True,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            self._program_path(program_id, "cancel"),
            {"actor": actor, "reason": reason, "cascade": cascade},
        )

    def patch_program_artifacts(
        self,
        program_id: str,
        artifacts: dict[str, Any],
        *,
        merge: bool = True,
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            self._program_path(program_id, "artifacts"),
            {"artifacts": artifacts, "merge": merge, "actor": actor},
        )

    def link_workflow_to_program(
        self,
        program_id: str,
        *,
        workflow_id: str,
        program_phase: str,
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            self._program_path(program_id, "link-workflow"),
            {
                "workflow_id": workflow_id,
                "program_phase": program_phase,
                "actor": actor,
            },
        )

    def patch_program(
        self,
        program_id: str,
        *,
        tenant_id: str = "default",
        project_id: str | None = None,
        project_id_source: str | None = None,
        excluded_from_autoshare: bool | None = None,
        parent_program_id: str | None = None,
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"tenant_id": tenant_id, "actor": actor}
        if project_id is not None:
            body["project_id"] = project_id
        if project_id_source is not None:
            body["project_id_source"] = project_id_source
        if excluded_from_autoshare is not None:
            body["excluded_from_autoshare"] = excluded_from_autoshare
        if parent_program_id is not None:
            body["parent_program_id"] = parent_program_id
        return self._request("PATCH", self._program_path(program_id), body)

    def create_program_link(
        self,
        program_id: str,
        *,
        to_program_id: str,
        tenant_id: str = "default",
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            self._program_path(program_id, "links"),
            {"to_program_id": to_program_id, "tenant_id": tenant_id, "actor": actor},
        )

    def delete_program_link(
        self,
        program_id: str,
        to_program_id: str,
        *,
        tenant_id: str = "default",
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        from urllib.parse import quote

        return self._request(
            "DELETE",
            f"{self._program_path(program_id, 'links')}/{quote(str(to_program_id), safe='')}",
            {"tenant_id": tenant_id, "actor": actor},
        )

    def list_program_links(self, program_id: str) -> dict[str, Any]:
        return self._request("GET", self._program_path(program_id, "links"))

    def upsert_program_briefs(
        self,
        program_id: str,
        entries: list[dict[str, Any]],
        *,
        tenant_id: str = "default",
        project_id: str | None = None,
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "entries": entries,
            "tenant_id": tenant_id,
            "actor": actor,
        }
        if project_id is not None:
            body["project_id"] = project_id
        return self._request("POST", self._program_path(program_id, "briefs"), body)

    def promote_department_memory(
        self,
        program_id: str,
        *,
        brief_id: str,
        tenant_id: str = "default",
        actor: str = "orchestrator",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            self._program_path(program_id, "department-memory"),
            {"brief_id": brief_id, "tenant_id": tenant_id, "actor": actor},
        )

    def list_program_briefs(self, program_id: str) -> dict[str, Any]:
        return self._request("GET", self._program_path(program_id, "briefs"))

    def list_briefs(
        self,
        *,
        tenant_id: str = "default",
        project_id: str | None = None,
        source_program_id: str | None = None,
    ) -> dict[str, Any]:
        from urllib.parse import urlencode

        params: dict[str, Any] = {"tenant_id": tenant_id}
        if project_id:
            params["project_id"] = project_id
        if source_program_id:
            params["source_program_id"] = source_program_id
        return self._request("GET", f"/briefs?{urlencode(params)}")


_default_client: MemoryClient | None = None


def get_memory_client() -> MemoryClient:
    global _default_client
    if _default_client is None:
        _default_client = MemoryClient()
    return _default_client


def _program_department_scope(ctx: ToolPolicyContext) -> str | None:
    """Department scope for the session's program. The model cannot choose it."""
    session_id = str(getattr(ctx, "session_id", None) or "").strip()
    if not session_id:
        return None
    try:
        from ts_harness.agent.sessions import get_session_store
        from ts_harness.workflows.session_compat import read_program_id

        session = get_session_store().get(session_id)
        if session is None or str(getattr(session, "tenant_id", "") or "") != str(ctx.tenant_id or ""):
            return None
        program_id = read_program_id(getattr(session, "metadata", None))
        if not program_id:
            return None
        payload = get_memory_client().get_program(program_id) or {}
        program = payload.get("program") if isinstance(payload.get("program"), dict) else payload
        if str(program.get("tenant_id") or "default") != str(ctx.tenant_id or "default"):
            return None
        name = str(program.get("department") or "").strip().lower()
        if not name:
            return None
        from ts_harness.policy.permissions import DEPARTMENT_NAME_RE

        if not DEPARTMENT_NAME_RE.fullmatch(name):
            return None
        return f"department:{name}"
    except Exception:
        return None


def recall_memory_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    client = get_memory_client()
    memories = client.recall(
        tenant_id=ctx.tenant_id,
        agent_id=ctx.agent_id,
        query=str(args.get("query", "")),
        limit=int(args.get("limit", 5)),
        department_scope=_program_department_scope(ctx),
    )
    return {"memories": memories}


def workflow_update_handler(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    client = get_memory_client()
    status = str(args.get("status", "succeeded"))
    path = {
        "succeeded": "/workflow/complete",
        "failed": "/workflow/fail",
        "blocked": "/workflow/block",
    }.get(status, "/workflow/complete")
    body = {"id": args.get("task_id"), "actor": ctx.agent_id}
    if status == "succeeded":
        body["output"] = args.get("output") or {}
    else:
        body["error"] = str(args.get("error") or args.get("output") or "updated by agent")
    return client._request("POST", path, body)
