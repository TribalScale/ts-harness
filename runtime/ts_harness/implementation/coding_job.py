"""One OpenCode coding run: build the request, run it, and shape the stage output.

The ``run_coding_backend`` agent tool and the coding worker share these, so a
run started from chat and one started as a queued job behave the same. The
request is a JSON-safe dict (a job spec) so it can be stored and handed to a
worker in another process.
"""

from __future__ import annotations

import os
from dataclasses import asdict
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
from ts_harness.implementation.repo import delivery_suffix
from ts_harness.implementation.security_scan import SecurityConfig
from ts_harness.integrations.github_client import parse_github_pr_url

SPEC_VERSION = 1


class CodingRequestError(ValueError):
    def __init__(self, message: str, **extra: Any):
        super().__init__(message)
        self.extra = extra


def _live_runtime_config():
    try:
        from ts_harness.gateway.server import get_runtime

        return getattr(get_runtime(), "config", None)
    except Exception:
        return None


def _default_checkout_dir(agent_id: str, workspace_name: str) -> str:
    """In object-workspace mode in-process checkouts go to replica scratch, never the shared tree."""
    from ts_harness.storage.backend import object_backend, scratch_dir

    in_process = os.environ.get("CODING_JOBS_ENABLED", "").strip().lower() not in {"1", "true", "yes", "on"}
    if in_process and object_backend() != "local":
        return str(scratch_dir() / "coding-backend" / agent_id / workspace_name)
    return f"/workspace/{agent_id}/coding-backend/{workspace_name}"


def clean_text(value: Any) -> str | None:
    return str(value or "").replace("\x00", "").strip() or None


def revision_files(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    files = [str(item).strip() for item in value if str(item).strip()]
    return files or None


def _as_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


_BOUND_TEXT_KEYS = ("work_order_body", "review_feedback", "qa_feedback")


def binding_from_task_input(task_input: dict[str, Any]) -> dict[str, Any]:
    """The repo, branch, PR, work order and feedback a deliver task was created with."""
    binding: dict[str, Any] = {}
    for key in ("repo_url", "base_branch", "pr_url", *_BOUND_TEXT_KEYS):
        value = str(task_input.get(key) or "").strip()
        if value:
            binding[key] = value
    for key in ("revision_files", "human_change_requests"):
        items = revision_files(task_input.get(key))
        if items:
            binding[key] = items
    if task_input.get("refresh") is True:
        binding["refresh"] = True
    return binding


def _directory_github_login(email: str | None, tenant_id: str) -> str | None:
    """GitHub login the identity source recorded for this person, when there is one."""
    if not email or "@" not in email:
        return None
    try:
        from ts_harness.memory.client import get_memory_client

        person = get_memory_client().directory_user(tenant_id=tenant_id or "default", email=email)
    except Exception:  # noqa: BLE001
        return None
    return str(((person or {}).get("user") or {}).get("github_login") or "").strip() or None


def build_coding_request(
    task_input: dict[str, Any],
    *,
    delivery_workflow_id: str | None,
    program_id: str | None = None,
    requested_by: str | None = None,
    bound: dict[str, Any] | None = None,
    agent_id: str = "dev-agent",
) -> dict[str, Any]:
    """Return the job spec for one coding run, or raise ``CodingRequestError``.

    ``bound`` is the repo, branch, PR and feedback stored on the delivery task;
    it wins over ``task_input`` because agents can retype those wrongly.
    """
    args = task_input
    bound = bound or {}
    runtime = load_active_runtime_config()
    impl = implementation_config(runtime)
    if impl.coding_backend != "opencode":
        raise CodingRequestError(f"unsupported coding_backend: {impl.coding_backend}")

    workflow_type = str(args.get("workflow_type") or "implement_work_orders")
    repo = repo_for_workflow(workflow_type, runtime=runtime)
    backend_cfg = opencode_backend_config(runtime=runtime)

    work_order_id = str(args.get("work_order_id") or "").strip()
    work_order_body = clean_text(bound.get("work_order_body") or args.get("work_order_body")) or ""
    if not work_order_body:
        markdown = str(args.get("work_orders_markdown") or "").strip()
        if markdown and work_order_id:
            from ts_harness.implementation.work_order_context import extract_work_order_section

            work_order_body = extract_work_order_section(markdown, work_order_id)
    # Prefer runtime-configured repo defaults. Agents often echo a stale/wrong
    # base_branch from a poorly materialized task input (e.g. default "main").
    configured_url = str(repo.url or "").strip()
    configured_branch = str(repo.base_branch or "").strip()
    repo_url = str(bound.get("repo_url") or args.get("repo_url") or configured_url or "").strip()
    mode = str(args.get("mode") or "implement").strip().lower()
    if mode not in {"implement", "refresh"}:
        raise CodingRequestError(f"unsupported mode: {mode}")
    if mode == "refresh" and bound and not bound.get("refresh"):
        raise CodingRequestError(
            "this delivery task asks for implementation, not a refresh; "
            "call run_coding_backend again with mode implement"
        )
    pr_url = str(bound.get("pr_url") or args.get("pr_url") or "").strip() or None
    if mode == "refresh":
        parsed_pr = parse_github_pr_url(pr_url or "")
        if parsed_pr is None:
            raise CodingRequestError("refresh requires a GitHub pr_url")
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
        str(args.get("model") or os.environ.get("CODING_BACKEND_MODEL", "") or backend_cfg.model or "").strip()
        or None
    )

    approved_prd_markdown = None
    approved_prd = args.get("approved_prd")
    if isinstance(approved_prd, dict):
        approved_prd_markdown = (
            str(approved_prd.get("prd_markdown") or approved_prd.get("markdown") or "").strip() or None
        )
    elif isinstance(args.get("approved_prd_markdown"), str):
        approved_prd_markdown = str(args.get("approved_prd_markdown") or "").strip() or None

    workspace_name = work_order_id or "wo"
    if suffix := delivery_suffix(delivery_workflow_id):
        workspace_name = f"{workspace_name}-{suffix}"
    workspace = str(
        args.get("workspace_dir")
        or os.environ.get("CODING_BACKEND_WORKSPACE")
        or _default_checkout_dir(agent_id, workspace_name)
    )

    live = _live_runtime_config() or runtime
    binding_error = check_repo_allowed(repo_url, tenant_bindings(getattr(live, "program_defaults", None)))
    if binding_error:
        raise CodingRequestError(binding_error, repo_url=repo_url)
    requester = str(requested_by or "").strip().lower() or None
    github_logins = dict(getattr(getattr(live, "identity", None), "github_logins", None) or {})
    feedback_source = bound if bound else args

    return {
        "spec_version": SPEC_VERSION,
        "repo_url": repo_url,
        "base_branch": base_branch,
        "work_order_id": work_order_id,
        "work_order_body": work_order_body,
        "workspace_dir": workspace,
        "provider": provider,
        "model": model,
        "clone_depth": int(repo.clone_depth or 1),
        "refinery_artifacts": _as_dict(args.get("refinery_artifacts")),
        "foundry_artifacts": _as_dict(args.get("foundry_artifacts")),
        "planner_artifacts": _as_dict(args.get("planner_artifacts")),
        "approved_prd_markdown": approved_prd_markdown,
        "qa_feedback": clean_text(feedback_source.get("qa_feedback")),
        "pr_url": pr_url,
        "requested_by": requester,
        "requested_by_github_login": github_logins.get(requester or "")
        or _directory_github_login(requester, getattr(getattr(live, "gateway", None), "default_tenant_id", "default")),
        "program_id": str(program_id or "").strip() or None,
        "delivery_workflow_id": delivery_workflow_id,
        "decision_id": str(args.get("decision_id") or "").strip() or None,
        "mode": mode,
        "review_feedback": clean_text(feedback_source.get("review_feedback")),
        "revision_files": revision_files(feedback_source.get("revision_files")),
        "human_change_requests": [
            text for item in revision_files(feedback_source.get("human_change_requests")) or [] if (text := clean_text(item))
        ]
        or None,
        "record_workflow_id": str(args.get("workflow_id") or "").strip() or None,
        # Workers on Fargate have no runtime config file; carry what the run needs.
        "backend": {
            "binary": backend_cfg.binary,
            "run_args": list(backend_cfg.run_args),
            "verification_fix_passes": int(backend_cfg.verification_fix_passes),
            "security": asdict(SecurityConfig.from_program_defaults(getattr(live, "program_defaults", None))),
        },
    }


_SPEC_ONLY_KEYS = {"spec_version", "record_workflow_id", "backend"}


def _backend_from_spec(backend: dict[str, Any]) -> OpenCodeBackend:
    security = backend.get("security") if isinstance(backend.get("security"), dict) else {}
    known = SecurityConfig.__dataclass_fields__
    return OpenCodeBackend(
        tools_root=os.environ.get("TS_HARNESS_TOOLS_ROOT", ""),
        binary_name=str(backend.get("binary") or "opencode"),
        run_args=[str(arg) for arg in backend.get("run_args") or []],
        verification_fix_passes=int(backend.get("verification_fix_passes") or 0),
        security=SecurityConfig(**{k: v for k, v in security.items() if k in known}),
    )


def _backend_from_config() -> OpenCodeBackend:
    runtime = load_active_runtime_config()
    impl = implementation_config(runtime)
    backend_cfg = opencode_backend_config(runtime=runtime)
    live = _live_runtime_config() or runtime
    return OpenCodeBackend(
        tools_root=impl.tools_root or os.environ.get("TS_HARNESS_TOOLS_ROOT", ""),
        binary_name=backend_cfg.binary,
        run_args=list(backend_cfg.run_args),
        verification_fix_passes=int(backend_cfg.verification_fix_passes),
        security=SecurityConfig.from_program_defaults(getattr(live, "program_defaults", None)),
    )


def coding_request_from_spec(spec: dict[str, Any]) -> CodingRequest:
    fields = {k: v for k, v in spec.items() if k not in _SPEC_ONLY_KEYS}
    fields["workspace_dir"] = Path(str(fields["workspace_dir"]))
    return CodingRequest(**fields)


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


def run_coding_request(spec: dict[str, Any], *, record_refs: bool = True) -> dict[str, Any]:
    """Run OpenCode for a spec and return the ``run_coding_backend`` result dict.

    Workers pass ``record_refs=False``: the memory API records branch and PR
    refs when the job completes, since a worker holds only its job token.
    """
    backend_spec = spec.get("backend")
    backend = _backend_from_spec(backend_spec) if isinstance(backend_spec, dict) else _backend_from_config()
    result = backend.implement(coding_request_from_spec(spec))
    if record_refs and spec.get("program_id"):
        record_coding_refs(
            spec["program_id"],
            workflow_id=spec.get("record_workflow_id"),
            repo_url=spec["repo_url"],
            branch=result.branch,
            pr_url=result.pr_url,
            work_order_id=spec.get("work_order_id") or "",
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
        "mode": spec.get("mode") or "implement",
        "work_order_id": spec.get("work_order_id") or "",
        "repo_url": spec["repo_url"],
        "base_branch": spec["base_branch"],
        "provider": spec.get("provider") or "",
    }


STAGE_OUTPUT_KEYS = (
    "work_order_id",
    "pr_url",
    "branch",
    "head_sha",
    "changed_files",
    "summary",
    "security",
    "log_path",
)


def coding_stage_output(result: dict[str, Any]) -> dict[str, Any]:
    """The ``deliver_work_order`` stage output for a coding run result."""
    output = {key: result[key] for key in STAGE_OUTPUT_KEYS if result.get(key) is not None}
    output["changed_files"] = list(result.get("changed_files") or [])
    output["security"] = result.get("security") if isinstance(result.get("security"), dict) else {}
    if result.get("mode") == "refresh":
        output["refresh_method"] = result.get("refresh_method")
        output["resolved_files"] = list(result.get("resolved_files") or [])
    if not result.get("ok"):
        output["error"] = result.get("error") or "coding run failed"
    return output
