"""Server-side completion adapters for program definitions.

Definitions reference adapter IDs only; executable code cannot be uploaded.
Built-in adapters preserve the existing delivery-program artifact extraction
and intake-building behavior.
"""

from __future__ import annotations

from typing import Any, Callable


BuildInputsFn = Callable[..., dict[str, Any]]
ExtractOutputsFn = Callable[..., dict[str, Any]]

# Keys each completion adapter may patch onto program artifacts. Used by
# program-definition validation so produces_artifacts are not treated as
# unconfirmed when they come from adapters rather than workflow contracts.
# extract_declared_outputs trusts the node's produces_artifacts list (None).
ADAPTER_DECLARED_OUTPUT_KEYS: dict[str, frozenset[str] | None] = {
    "extract_prd_outputs": frozenset({"approved_prd"}),
    "extract_jira_outputs": frozenset({"jira_epic_key", "jira_story_keys", "planning_track"}),
    "extract_work_orders_outputs": frozenset(
        {
            "planner_complete",
            "work_orders_manifest_path",
            "work_order_ids",
            "next_work_order_id",
            "work_order_ref",
            "work_orders_handoff",
            "work_orders_markdown",
            "refinery_artifacts",
            "foundry_artifacts",
            "planner_artifacts",
            "planning_track",
        }
    ),
    "extract_implement_outputs": frozenset(
        {
            "pr_url",
            "pr_urls",
            "qa_status",
            "qa_summary",
            "qa_evidence",
            "completed_work_order_ids",
            "merge_sha",
        }
    ),
    "extract_deploy_outputs": frozenset({"merge_sha", "released_environments"}),
    "extract_file_jira_outputs": frozenset({"jira_epic_key", "jira_story_keys"}),
    "extract_pick_ticket_outputs": frozenset(
        {"jira_issue_key", "jira_project_key", "owner_program_id"}
    ),
    "extract_declared_outputs": None,
}


def _merge_task_outputs(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    outputs: dict[str, Any] = {}
    for task in tasks:
        if task.get("status") == "succeeded" and isinstance(task.get("output"), dict):
            outputs.update(task["output"])
    return outputs


def _record_qa_artifacts(
    tasks: list[dict[str, Any]],
    artifact_patch: dict[str, Any],
) -> None:
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


def _build_prd_inputs(
    *,
    summary: str,
    artifacts: dict[str, Any],
    transcript_messages: list[dict[str, Any]] | None = None,
    extra_input: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    del artifacts
    return {
        "summary": summary,
        "context": {
            "summary": summary,
            "transcript": list(transcript_messages or [])[-20:],
        },
        **(extra_input or {}),
    }


def _extract_prd_outputs(
    *,
    workflow_type: str,
    tasks: list[dict[str, Any]],
    outputs: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    del workflow_type, artifacts
    merged = dict(outputs or {})
    merged.update(_merge_task_outputs(tasks))
    patch: dict[str, Any] = {}
    if merged.get("approved_prd") or merged.get("prd"):
        patch["approved_prd"] = merged.get("approved_prd") or merged.get("prd")
    return patch


def _build_jira_inputs(
    *,
    summary: str,
    artifacts: dict[str, Any],
    transcript_messages: list[dict[str, Any]] | None = None,
    extra_input: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    del transcript_messages
    intake: dict[str, Any] = {"summary": summary, **(extra_input or {})}
    if artifacts.get("approved_prd"):
        intake["approved_prd"] = artifacts["approved_prd"]
    return intake


def _extract_jira_outputs(
    *,
    workflow_type: str,
    tasks: list[dict[str, Any]],
    outputs: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    del workflow_type, artifacts
    merged = dict(outputs or {})
    merged.update(_merge_task_outputs(tasks))
    if isinstance(merged.get("result"), dict):
        merged = {**merged["result"], **merged}
    patch: dict[str, Any] = {}
    if merged.get("epic_key") or merged.get("jira_epic_key"):
        patch["jira_epic_key"] = merged.get("jira_epic_key") or merged.get("epic_key")
    story_keys = merged.get("jira_story_keys") or merged.get("child_keys")
    if isinstance(story_keys, list) and story_keys:
        patch["jira_story_keys"] = [str(key) for key in story_keys if str(key or "").strip()]
    patch["planning_track"] = "jira"
    return patch


def _build_work_orders_inputs(
    *,
    summary: str,
    artifacts: dict[str, Any],
    transcript_messages: list[dict[str, Any]] | None = None,
    extra_input: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    del transcript_messages
    intake: dict[str, Any] = {
        "summary": summary,
        "stage": "refinery",
        **(extra_input or {}),
    }
    if artifacts.get("approved_prd"):
        intake["approved_prd"] = artifacts["approved_prd"]
    if artifacts.get("completed_work_order_ids") is not None:
        intake["completed_work_order_ids"] = artifacts.get("completed_work_order_ids")
    if artifacts.get("project_repositories"):
        intake["project_repositories"] = artifacts["project_repositories"]
    return intake


def _extract_work_orders_outputs(
    *,
    workflow_type: str,
    tasks: list[dict[str, Any]],
    outputs: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    del artifacts
    merged = dict(outputs or {})
    merged.update(_merge_task_outputs(tasks))
    patch: dict[str, Any] = {"planning_track": "work_orders"}
    for key in (
        "planner_complete",
        "work_orders_manifest_path",
        "work_order_ids",
        "next_work_order_id",
        "work_order_ref",
        "work_orders_handoff",
    ):
        if merged.get(key) is not None:
            patch[key] = merged.get(key)

    markdown = merged.get("work_orders_markdown")
    if not markdown and isinstance(merged.get("artifacts"), dict):
        markdown = merged["artifacts"].get("work_orders_markdown")
    if markdown:
        patch["work_orders_markdown"] = markdown

    workflow_type_name = str(workflow_type or "")
    tracks_work_orders = (
        "work_orders_from_prd" in workflow_type_name
        or patch.get("planning_track") == "work_orders"
        or bool(merged.get("work_orders_handoff"))
    )
    if tracks_work_orders:
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
                patch[bag_key] = bag
    return patch


def _live_program_defaults() -> dict[str, Any]:
    try:
        from ts_harness.gateway.server import get_runtime

        return dict(getattr(get_runtime().config, "program_defaults", None) or {})
    except Exception:  # noqa: BLE001
        return {}


def _work_order_wave(artifacts: dict[str, Any]) -> list[str]:
    """``next_work_order_ids`` when set, else the ready wave computed from the manifest."""
    from ts_harness.implementation.work_order_context import (
        dependencies_from_artifacts,
        max_parallel_work_orders,
        select_ready_work_order_ids,
    )

    explicit = [str(item) for item in artifacts.get("next_work_order_ids") or [] if str(item).strip()]
    if explicit:
        return explicit
    if not isinstance(artifacts.get("work_order_ids"), list):
        return [str(artifacts["next_work_order_id"])] if artifacts.get("next_work_order_id") else []
    return select_ready_work_order_ids(
        artifacts["work_order_ids"],
        list(artifacts.get("completed_work_order_ids") or []),
        dependencies_from_artifacts(artifacts),
        max_parallel=max_parallel_work_orders(_live_program_defaults()),
    )


def _build_implement_inputs(
    *,
    summary: str,
    artifacts: dict[str, Any],
    transcript_messages: list[dict[str, Any]] | None = None,
    extra_input: dict[str, Any] | None = None,
    workflow_type: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    del transcript_messages
    intake: dict[str, Any] = {"summary": summary, **(extra_input or {})}
    for key in (
        "jira_epic_key",
        "jira_story_keys",
        "work_order_ref",
        "work_orders_manifest_path",
        "work_orders_markdown",
        "refinery_artifacts",
        "foundry_artifacts",
        "planner_artifacts",
        "jira_issue_key",
    ):
        if artifacts.get(key) is not None:
            intake[key] = artifacts[key]
    if artifacts.get("next_work_order_id"):
        intake["work_order_id"] = artifacts["next_work_order_id"]
    if artifacts.get("completed_work_order_ids") is not None:
        intake["completed_work_order_ids"] = artifacts.get("completed_work_order_ids")
    wave = _work_order_wave(artifacts)
    if wave:
        intake["work_orders"] = [{"work_order_id": wo_id, "work_order_ref": wo_id} for wo_id in wave]
    if artifacts.get("work_orders_markdown") and artifacts.get("next_work_order_id"):
        try:
            from ts_harness.implementation.work_order_context import (
                extract_work_order_section,
            )

            body = extract_work_order_section(
                str(artifacts["work_orders_markdown"]),
                str(artifacts["next_work_order_id"]),
            )
            if body:
                intake["work_order_body"] = body
        except Exception:  # noqa: BLE001
            pass
    repositories = [r for r in artifacts.get("project_repositories") or [] if isinstance(r, dict)]
    project_default = repositories[0] if len(repositories) == 1 else None
    if project_default:
        intake["repo_url"] = project_default["repo_url"]
        if project_default.get("default_branch"):
            intake["base_branch"] = project_default["default_branch"]
    elif workflow_type:
        try:
            from ts_harness.implementation.config import repo_for_workflow

            repo = repo_for_workflow(workflow_type)
            if repo.url:
                intake["repo_url"] = repo.url
            if repo.base_branch:
                intake["base_branch"] = repo.base_branch
        except Exception:  # noqa: BLE001
            pass
    if artifacts.get("default_branch"):
        intake["base_branch"] = artifacts["default_branch"]
    elif workflow_type == "implement_jira_ticket" and str(intake.get("base_branch") or "") in {
        "",
        "main",
    }:
        intake["base_branch"] = "dev"
    if repositories and artifacts.get("work_orders_markdown"):
        _route_work_orders_to_repositories(intake, str(artifacts["work_orders_markdown"]), repositories)
    return intake


def _route_work_orders_to_repositories(
    intake: dict[str, Any], markdown: str, repositories: list[dict[str, Any]]
) -> None:
    """Give each work order the repo_url and base_branch of the repository it names."""
    from ts_harness.implementation.work_order_context import parse_work_order_repositories, resolve_repository

    named = parse_work_order_repositories(markdown)

    def target(work_order_id: Any) -> dict[str, Any]:
        repository = resolve_repository(named.get(str(work_order_id or "").upper()), repositories)
        if repository is None:
            return {}
        out = {"repo_url": repository["repo_url"]}
        if repository.get("default_branch"):
            out["base_branch"] = repository["default_branch"]
        return out

    for item in intake.get("work_orders") or []:
        item.update(target(item.get("work_order_id")))
    intake.update(target(intake.get("work_order_id")))


def _extract_implement_outputs(
    *,
    workflow_type: str,
    tasks: list[dict[str, Any]],
    outputs: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    merged = dict(outputs or {})
    merged.update(_merge_task_outputs(tasks))
    patch: dict[str, Any] = {}
    _record_qa_artifacts(tasks, patch)
    if merged.get("pr_url"):
        patch["pr_url"] = merged.get("pr_url")
    existing = dict(artifacts or {})
    if merged.get("pr_urls"):
        patch["pr_urls"] = list(dict.fromkeys([*(existing.get("pr_urls") or []), *merged["pr_urls"]]))
    merge_sha = merged.get("merge_sha") or merged.get("merge_commit_sha")
    if merge_sha:
        patch["merge_sha"] = merge_sha
    wo_id = merged.get("work_order_id") or merged.get("next_work_order_id")
    workflow_type_name = str(workflow_type or "")
    tracks_work_orders = (
        "implement_work_orders" in workflow_type_name
        or existing.get("planning_track") == "work_orders"
        or bool(existing.get("work_orders_handoff"))
        or isinstance(existing.get("work_order_ids"), list)
    )
    delivered = [str(item) for item in merged.get("wave_work_order_ids") or []]
    if wo_id and not delivered:
        delivered = [str(wo_id)]
    if delivered and tracks_work_orders:
        completed = list(existing.get("completed_work_order_ids") or [])
        for item in delivered:
            if item not in completed:
                completed.append(item)
        patch["completed_work_order_ids"] = completed
    merged_work_orders = _merged_work_orders(tasks, merged, existing)
    if merged_work_orders:
        patch["merged_work_orders"] = merged_work_orders
    return patch


def _merged_work_orders(
    tasks: list[dict[str, Any]], merged: dict[str, Any], existing: dict[str, Any]
) -> list[dict[str, Any]]:
    """Every merged PR of the program so far, one entry per PR URL, for per-repository releases."""
    by_pr: dict[str, dict[str, Any]] = {
        str(item["pr_url"]): dict(item)
        for item in existing.get("merged_work_orders") or []
        if isinstance(item, dict) and item.get("pr_url")
    }

    def add(work_order_id: Any, pr_url: Any, merge_sha: Any, merged_at: Any) -> None:
        if pr_url and merge_sha:
            by_pr[str(pr_url)] = {
                "work_order_id": str(work_order_id or ""),
                "pr_url": str(pr_url),
                "merge_sha": str(merge_sha),
                "merged_at": str(merged_at or ""),
            }

    found_prs = False
    for task in tasks:
        output = task.get("output") if task.get("status") == "succeeded" else None
        for pr in (output or {}).get("prs") or [] if isinstance(output, dict) else []:
            if isinstance(pr, dict):
                found_prs = True
                add(pr.get("work_order_id"), pr.get("pr_url"), pr.get("merge_sha"), pr.get("merged_at"))
    if not found_prs:
        add(
            merged.get("work_order_id"),
            merged.get("pr_url"),
            merged.get("merge_sha") or merged.get("merge_commit_sha"),
            merged.get("merged_at"),
        )
    return list(by_pr.values())


def _build_deploy_inputs(
    *,
    summary: str,
    artifacts: dict[str, Any],
    transcript_messages: list[dict[str, Any]] | None = None,
    extra_input: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    del transcript_messages
    intake: dict[str, Any] = {"summary": summary, **(extra_input or {})}
    for key in (
        "pr_url",
        "pr_urls",
        "merge_sha",
        "repo_url",
        "rollback_environment",
        "rollback_sha",
        "merged_work_orders",
        "work_orders_markdown",
        "project_repositories",
    ):
        if artifacts.get(key):
            intake[key] = artifacts[key]
    base_branch = artifacts.get("base_branch") or artifacts.get("default_branch")
    if base_branch:
        intake["base_branch"] = base_branch
    return intake


def _extract_deploy_outputs(
    *,
    workflow_type: str,
    tasks: list[dict[str, Any]],
    outputs: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    del workflow_type, artifacts
    merged = dict(outputs or {})
    merged.update(_merge_task_outputs(tasks))
    patch: dict[str, Any] = {}
    merge_sha = (
        merged.get("merge_sha")
        or merged.get("merge_commit_sha")
        or merged.get("sha")
    )
    if merge_sha:
        patch["merge_sha"] = merge_sha
    released = [
        str(task["output"]["environment"])
        for task in tasks
        if task.get("status") == "succeeded"
        and isinstance(task.get("output"), dict)
        and task["output"].get("status") == "succeeded"
        and task["output"].get("release_id")
        and task["output"].get("environment")
    ]
    for output in (merged.get("children_outputs") or {}).values():
        if isinstance(output, dict):
            released.extend(str(env) for env in output.get("environments") or [])
    if released:
        patch["released_environments"] = list(dict.fromkeys(released))
    return patch


class ProgramAdapterRegistry:
    def __init__(self) -> None:
        self._build_inputs: dict[str, BuildInputsFn] = {}
        self._extract_outputs: dict[str, ExtractOutputsFn] = {}

    def register(
        self,
        name: str,
        *,
        build_inputs: BuildInputsFn,
        extract_outputs: ExtractOutputsFn,
    ) -> None:
        key = str(name or "").strip()
        if not key:
            raise ValueError("adapter name is required")
        self._build_inputs[key] = build_inputs
        self._extract_outputs[key] = extract_outputs

    def names(self) -> list[str]:
        return sorted(self._build_inputs)

    def build_inputs(self, name: str, **kwargs: Any) -> dict[str, Any]:
        try:
            handler = self._build_inputs[name]
        except KeyError as exc:
            raise KeyError(f"unknown program adapter: {name}") from exc
        return handler(**kwargs)

    def extract_outputs(self, name: str, **kwargs: Any) -> dict[str, Any]:
        try:
            handler = self._extract_outputs[name]
        except KeyError as exc:
            raise KeyError(f"unknown program adapter: {name}") from exc
        return handler(**kwargs)


def _build_declared_inputs(
    *,
    summary: str,
    artifacts: dict[str, Any],
    transcript_messages: list[dict[str, Any]] | None = None,
    extra_input: dict[str, Any] | None = None,
    required_artifacts: list[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    del transcript_messages
    intake: dict[str, Any] = {"summary": summary, **(extra_input or {})}
    for name in list(required_artifacts or []):
        key = str(name or "").strip()
        if key and artifacts.get(key) is not None:
            intake[key] = artifacts[key]
    return intake


def _extract_declared_outputs(
    *,
    workflow_type: str,
    tasks: list[dict[str, Any]],
    outputs: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    produces_artifacts: list[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    del workflow_type, artifacts
    merged = dict(outputs or {})
    merged.update(_merge_task_outputs(tasks))
    produces = [str(name).strip() for name in list(produces_artifacts or []) if str(name).strip()]
    patch: dict[str, Any] = {}
    for key in produces:
        if key in merged and merged.get(key) is not None:
            patch[key] = merged[key]
    approved = merged.get("approved_artifact") or merged.get("approved_prd")
    if approved is not None and produces:
        first = produces[0]
        patch.setdefault(first, approved)
    return patch


def _build_file_jira_inputs(
    *,
    summary: str,
    artifacts: dict[str, Any],
    extra_input: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    from ts_harness.jira.program_config import resolved_program_config

    intake: dict[str, Any] = {"summary": summary, **(extra_input or {})}
    for key in (
        "approved_prd",
        "work_order_ids",
        "work_orders_markdown",
        "bug_report",
        "jira_epic_key",
        "repo",
        "default_branch",
        "planner_complete",
        "drive_library",
    ):
        if artifacts.get(key) is not None:
            intake[key] = artifacts[key]
    if snapshot:
        filing = resolved_program_config(snapshot).get("jira_filing") or {}
        default_type = str(filing.get("default_issue_type") or "").strip()
        if default_type:
            intake["default_issue_type"] = default_type
        types = filing.get("issue_type_by_project")
        if isinstance(types, dict):
            intake["issue_type_by_project"] = dict(types)
    return intake


def _extract_file_jira_outputs(
    *,
    workflow_type: str,
    tasks: list[dict[str, Any]],
    outputs: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    del workflow_type, artifacts
    merged = dict(outputs or {})
    merged.update(_merge_task_outputs(tasks))
    patch: dict[str, Any] = {}
    if merged.get("epic_key") or merged.get("jira_epic_key"):
        patch["jira_epic_key"] = merged.get("epic_key") or merged.get("jira_epic_key")
    if merged.get("jira_story_keys") or merged.get("story_keys"):
        patch["jira_story_keys"] = merged.get("jira_story_keys") or merged.get("story_keys")
    return patch


def _build_pick_ticket_inputs(
    *,
    summary: str,
    artifacts: dict[str, Any],
    extra_input: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    intake: dict[str, Any] = {"summary": summary, **(extra_input or {})}
    for key in ("jira_issue_key", "jira_project_key"):
        if artifacts.get(key) is not None:
            intake[key] = artifacts[key]
    return intake


def _extract_pick_ticket_outputs(
    *,
    workflow_type: str,
    tasks: list[dict[str, Any]],
    outputs: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    del workflow_type, artifacts
    merged = dict(outputs or {})
    merged.update(_merge_task_outputs(tasks))
    patch: dict[str, Any] = {}
    if merged.get("jira_issue_key") or merged.get("issue_key"):
        patch["jira_issue_key"] = merged.get("jira_issue_key") or merged.get("issue_key")
    if merged.get("jira_project_key") or merged.get("project_key"):
        patch["jira_project_key"] = merged.get("jira_project_key") or merged.get("project_key")
    owner = owner_program_from_pick_output(merged)
    if owner:
        patch["owner_program_id"] = owner
    return patch


def owner_program_from_pick_output(output: dict[str, Any]) -> str | None:
    """Return the Program pointer from a picked ticket's description, if any."""
    from ts_harness.jira.pointer_block import parse_pointer_block

    explicit = str(output.get("owner_program_id") or "").strip()
    if explicit:
        return explicit
    for key in ("jira_issue_description", "issue_description", "description"):
        text = output.get(key)
        if isinstance(text, str) and text.strip():
            program_id = parse_pointer_block(text).program_id
            if program_id:
                return program_id
    return None


def build_default_adapter_registry() -> ProgramAdapterRegistry:
    registry = ProgramAdapterRegistry()
    registry.register(
        "extract_prd_outputs",
        build_inputs=_build_prd_inputs,
        extract_outputs=_extract_prd_outputs,
    )
    registry.register(
        "extract_jira_outputs",
        build_inputs=_build_jira_inputs,
        extract_outputs=_extract_jira_outputs,
    )
    registry.register(
        "extract_work_orders_outputs",
        build_inputs=_build_work_orders_inputs,
        extract_outputs=_extract_work_orders_outputs,
    )
    registry.register(
        "extract_implement_outputs",
        build_inputs=_build_implement_inputs,
        extract_outputs=_extract_implement_outputs,
    )
    registry.register(
        "extract_deploy_outputs",
        build_inputs=_build_deploy_inputs,
        extract_outputs=_extract_deploy_outputs,
    )
    registry.register(
        "extract_declared_outputs",
        build_inputs=_build_declared_inputs,
        extract_outputs=_extract_declared_outputs,
    )
    registry.register(
        "extract_file_jira_outputs",
        build_inputs=_build_file_jira_inputs,
        extract_outputs=_extract_file_jira_outputs,
    )
    registry.register(
        "extract_pick_ticket_outputs",
        build_inputs=_build_pick_ticket_inputs,
        extract_outputs=_extract_pick_ticket_outputs,
    )
    return registry
