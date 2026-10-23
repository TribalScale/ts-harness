"""Agent tool: run_coding_backend (host OpenCode implement)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ts_harness.config.bindings import check_repo_allowed, github_repo_slug, tenant_bindings
from ts_harness.implementation.backends.base import CodingRequest
from ts_harness.implementation.backends.opencode import OpenCodeBackend
from ts_harness.implementation.config import (
    implementation_config,
    load_active_runtime_config,
    opencode_backend_config,
    repo_for_workflow,
)
from ts_harness.implementation.security_scan import SecurityConfig
from ts_harness.integrations.github_client import parse_github_pr_url
from ts_harness.tools.policy import ToolPolicyContext


def _live_runtime_config():
    try:
        from ts_harness.gateway.server import get_runtime

        return getattr(get_runtime(), "config", None)
    except Exception:
        return None


def delivery_binding(ctx: ToolPolicyContext, *, client: Any = None) -> dict[str, str]:
    """Repo, branch, and PR already stored on this workflow's deliver task.

    A revision must not follow the agent's default repository when the work order named one.
    """
    workflow_id = str(getattr(ctx, "linked_workflow_id", None) or "").strip()
    if not workflow_id:
        return {}
    if client is None:
        try:
            from ts_harness.memory.client import get_memory_client

            client = get_memory_client()
        except Exception:
            return {}
    try:
        payload = client.get_workflow(workflow_id)
    except Exception:
        return {}
    chosen: dict[str, Any] = {}
    for task in payload.get("tasks") or []:
        if str(task.get("step_name") or "") != "deliver_work_order":
            continue
        task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
        if str(task_input.get("repo_url") or "").strip():
            chosen = task_input
    binding: dict[str, str] = {}
    for key in ("repo_url", "base_branch", "pr_url"):
        value = str(chosen.get(key) or "").strip()
        if value:
            binding[key] = value
    return binding


def _revision_files(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    files = [str(item).strip() for item in value if str(item).strip()]
    return files or None


def record_coding_refs(
    program_id: str,
    *,
    workflow_id: str | None,
    repo_url: str,
    branch: str | None,
    pr_url: str | None,
    work_order_id: str,
    client: Any = None,
) -> list[str]:
    """Record the branch and PR a coding run produced; failures never fail the run."""
    from ts_harness.config.bindings import github_repo_slug

    warnings: list[str] = []
    try:
        if client is None:
            from ts_harness.memory.client import get_memory_client

            client = get_memory_client()
    except Exception:
        return ["external_ref_client_unavailable"]
    slug = github_repo_slug(repo_url) or repo_url
    refs: list[tuple[str, str]] = []
    if branch:
        refs.append(("branch", f"{slug}#{branch}"))
    if pr_url:
        refs.append(("pull_request", pr_url))
    for kind, ref in refs:
        try:
            client.record_program_external_ref(
                program_id,
                kind=kind,
                ref=ref,
                workflow_id=workflow_id,
                metadata={
                    "repo": slug,
                    "branch": branch,
                    "work_order_id": work_order_id,
                    "created_by_harness": True,
                },
                actor="dev-agent",
            )
        except Exception:
            warnings.append(f"external_ref_failed:{kind}")
    return warnings


def handle_run_coding_backend(args: dict[str, Any], ctx: ToolPolicyContext) -> dict[str, Any]:
    if ctx.agent_id != "dev-agent":
        return {"ok": False, "error": "run_coding_backend is restricted to dev-agent"}

    runtime = load_active_runtime_config()
    impl = implementation_config(runtime)
    if impl.coding_backend != "opencode":
        return {
            "ok": False,
            "error": f"unsupported coding_backend: {impl.coding_backend}",
        }

    workflow_type = str(args.get("workflow_type") or "implement_work_orders")
    repo = repo_for_workflow(workflow_type, runtime=runtime)
    backend_cfg = opencode_backend_config(runtime=runtime)

    work_order_id = str(args.get("work_order_id") or "").strip()
    work_order_body = str(args.get("work_order_body") or "").strip()
    if not work_order_body:
        markdown = str(args.get("work_orders_markdown") or "").strip()
        if markdown and work_order_id:
            from ts_harness.implementation.work_order_context import (
                extract_work_order_section,
            )

            work_order_body = extract_work_order_section(markdown, work_order_id)
    # Prefer runtime-configured repo defaults. Agents often echo a stale/wrong
    # base_branch from a poorly materialized task input (e.g. default "main").
    configured_url = str(repo.url or "").strip()
    configured_branch = str(repo.base_branch or "").strip()
    bound = delivery_binding(ctx)
    repo_url = str(bound.get("repo_url") or args.get("repo_url") or configured_url or "").strip()
    mode = str(args.get("mode") or "implement").strip().lower()
    if mode not in {"implement", "refresh"}:
        return {"ok": False, "error": f"unsupported mode: {mode}"}
    pr_url_arg = str(bound.get("pr_url") or args.get("pr_url") or "").strip() or None
    if mode == "refresh":
        parsed_pr = parse_github_pr_url(pr_url_arg or "")
        if parsed_pr is None:
            return {"ok": False, "error": "refresh requires a GitHub pr_url"}
        repo_url = f"https://github.com/{parsed_pr[0]}/{parsed_pr[1]}.git"
    # The configured branch belongs to the configured repo; any other repo
    # keeps its own base branch.
    targets_configured_repo = github_repo_slug(repo_url) == github_repo_slug(configured_url)
    arg_branch = str(bound.get("base_branch") or args.get("base_branch") or "").strip()
    prefer_configured_branch = (
        targets_configured_repo
        and bool(configured_branch)
        and (not arg_branch or (arg_branch == "main" and configured_branch != "main"))
    )
    if prefer_configured_branch:
        base_branch = configured_branch
    else:
        base_branch = arg_branch or (configured_branch if targets_configured_repo else "") or "main"
    provider = str(args.get("provider") or backend_cfg.provider or "").strip()
    # Secrets/env (CODING_BACKEND_MODEL) win over baked runtime YAML so rotate-secrets
    # takes effect without re-rendering ts-harness-runtime.yaml.
    model = (
        str(
            args.get("model")
            or os.environ.get("CODING_BACKEND_MODEL", "")
            or backend_cfg.model
            or ""
        ).strip()
        or None
    )

    def _as_dict(value: Any) -> dict[str, Any] | None:
        return value if isinstance(value, dict) else None

    refinery_artifacts = _as_dict(args.get("refinery_artifacts"))
    foundry_artifacts = _as_dict(args.get("foundry_artifacts"))
    planner_artifacts = _as_dict(args.get("planner_artifacts"))
    approved_prd_markdown = None
    approved_prd = args.get("approved_prd")
    if isinstance(approved_prd, dict):
        approved_prd_markdown = str(
            approved_prd.get("prd_markdown") or approved_prd.get("markdown") or ""
        ).strip() or None
    elif isinstance(args.get("approved_prd_markdown"), str):
        approved_prd_markdown = str(args.get("approved_prd_markdown") or "").strip() or None

    workspace = Path(
        str(
            args.get("workspace_dir")
            or os.environ.get("CODING_BACKEND_WORKSPACE")
            or f"/workspace/{ctx.agent_id}/coding-backend/{work_order_id or 'wo'}"
        )
    )

    live = _live_runtime_config() or runtime
    binding_error = check_repo_allowed(
        repo_url, tenant_bindings(getattr(live, "program_defaults", None))
    )
    if binding_error:
        return {"ok": False, "error": binding_error, "repo_url": repo_url}
    requested_by = str(args.get("requested_by") or ctx.actor_email or "").strip().lower() or None
    github_logins = dict(getattr(getattr(live, "identity", None), "github_logins", None) or {})
    requested_login = github_logins.get(requested_by or "") or None
    program_id = str(args.get("program_id") or "").strip() or None

    tools_root = impl.tools_root or os.environ.get("TS_HARNESS_TOOLS_ROOT", "")
    backend = OpenCodeBackend(
        tools_root=tools_root,
        binary_name=backend_cfg.binary,
        run_args=list(backend_cfg.run_args),
        verification_fix_passes=int(backend_cfg.verification_fix_passes),
        security=SecurityConfig.from_program_defaults(getattr(live, "program_defaults", None)),
    )
    result = backend.implement(
        CodingRequest(
            repo_url=repo_url,
            base_branch=base_branch,
            work_order_id=work_order_id,
            work_order_body=work_order_body,
            workspace_dir=workspace,
            provider=provider,
            model=model,
            clone_depth=int(repo.clone_depth or 1),
            refinery_artifacts=refinery_artifacts,
            foundry_artifacts=foundry_artifacts,
            planner_artifacts=planner_artifacts,
            approved_prd_markdown=approved_prd_markdown,
            qa_feedback=str(args.get("qa_feedback") or "").strip() or None,
            pr_url=pr_url_arg,
            requested_by=requested_by,
            requested_by_github_login=requested_login,
            program_id=program_id,
            decision_id=str(args.get("decision_id") or "").strip() or None,
            mode=mode,
            review_feedback=str(args.get("review_feedback") or "").strip() or None,
            revision_files=_revision_files(args.get("revision_files")),
        )
    )
    if program_id:
        record_coding_refs(
            program_id,
            workflow_id=str(args.get("workflow_id") or "").strip() or None,
            repo_url=repo_url,
            branch=result.branch,
            pr_url=result.pr_url,
            work_order_id=work_order_id,
        )
    return {
        "ok": result.ok,
        "branch": result.branch,
        "summary": result.summary,
        "changed_files": result.changed_files,
        "log_tail": result.log_tail,
        "log_path": result.log_path,
        "error": result.error,
        "pr_url": result.pr_url,
        "head_sha": result.head_sha,
        "refresh_method": result.refresh_method,
        "resolved_files": result.resolved_files,
        "security": result.security,
        "mode": mode,
        "work_order_id": work_order_id,
        "repo_url": repo_url,
        "base_branch": base_branch,
        "provider": provider,
    }
