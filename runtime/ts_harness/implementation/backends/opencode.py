"""OpenCode host-subprocess coding backend."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ts_harness.implementation.auth import (
    provider_child_env,
    resolve_coding_backend_credentials,
)
from ts_harness.implementation.backends.base import (
    CodingRequest,
    CodingResult,
    attribution_markdown,
    commit_message,
    security_markdown,
)
from ts_harness.implementation.coding_backend_skills import (
    ensure_opencode_project_config,
    materialize_coding_backend_skills,
)
from ts_harness.implementation.planning_docs import (
    materialize_planning_docs,
    planning_docs_file_map,
)
from ts_harness.implementation.repo import (
    commit_all_changes,
    create_pull_request,
    create_work_branch,
    ensure_repo_clone,
    list_changed_files,
    push_branch,
    work_order_branch_name,
)
from ts_harness.implementation.security_scan import SecurityConfig
from ts_harness.implementation.verification import (
    build_verification_fix_prompt,
    prepare_test_dependencies,
    verify_implementation,
)
from ts_harness.integrations.github_client import (
    GitHubClient,
    GitHubError,
    parse_github_pr_url,
)
from ts_harness.observability.logging import log_event

_OPENCODE_PROGRESS_INTERVAL_SECONDS = 30.0
_OPENCODE_LOG_TAIL_CHARS = 5000


def opencode_progress_log_path(workspace_dir: Path, work_order_id: str) -> Path:
    safe_id = "".join(
        ch if ch.isalnum() or ch in {"-", "_"} else "-"
        for ch in str(work_order_id or "wo").strip()
    ) or "wo"
    return Path(workspace_dir) / ".ts-harness" / f"opencode-{safe_id}.log"


def stream_opencode_process(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    log_path: Path,
    work_order_id: str,
) -> subprocess.CompletedProcess[str]:
    """Run OpenCode while streaming output to a tailable log and runtime events."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    collected: list[str] = []
    line_count = 0
    last_progress = time.monotonic()
    last_line = ""
    with log_path.open("a", encoding="utf-8") as log_fh:
        log_fh.write(f"\n--- opencode start work_order_id={work_order_id} ---\n")
        log_fh.flush()
        log_event(
            "opencode_progress",
            work_order_id=work_order_id,
            phase="start",
            log_path=str(log_path),
        )
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                log_fh.write(line)
                log_fh.flush()
                collected.append(line)
                line_count += 1
                last_line = line.rstrip("\n")
                now = time.monotonic()
                if now - last_progress >= _OPENCODE_PROGRESS_INTERVAL_SECONDS:
                    last_progress = now
                    log_event(
                        "opencode_progress",
                        work_order_id=work_order_id,
                        phase="running",
                        lines=line_count,
                        last_line=last_line[-300:],
                        log_path=str(log_path),
                    )
        finally:
            proc.stdout.close()
        returncode = proc.wait()
        log_fh.write(f"--- opencode end rc={returncode} lines={line_count} ---\n")
        log_fh.flush()
    log_event(
        "opencode_progress",
        work_order_id=work_order_id,
        phase="end",
        returncode=returncode,
        lines=line_count,
        log_path=str(log_path),
    )
    output = "".join(collected)
    return subprocess.CompletedProcess(cmd, returncode, stdout=output, stderr="")


def resolve_opencode_binary(*, tools_root: str, binary_name: str = "opencode") -> str:
    if tools_root:
        candidate = Path(tools_root) / "opencode" / "current" / "bin" / binary_name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil.which(binary_name)
    if found:
        return found
    raise FileNotFoundError(
        f"OpenCode binary '{binary_name}' not found under tools_root={tools_root!r} or PATH"
    )


def build_opencode_command(
    *,
    binary: str,
    repo_dir: Path,
    prompt: str,
    run_args: list[str],
    provider: str,
    model: str | None,
) -> list[str]:
    cmd = [binary, *run_args, "--dir", str(repo_dir)]
    if model:
        cmd.extend(["-m", f"{provider}/{model}"])
    cmd.append(prompt)
    return cmd


def build_implement_prompt(
    work_order_id: str,
    work_order_body: str,
    *,
    primary_spec_path: str | None = None,
    docs_materialized: bool = False,
    skills_materialized: bool = False,
    qa_feedback: str | None = None,
    pr_url: str | None = None,
    review_feedback: str | None = None,
    revision_files: list[str] | None = None,
) -> str:
    hard_rules = (
        "This is a non-interactive host run: implement immediately.\n"
        "Do not use brainstorming (or any Superpowers planning skill).\n"
        "Do not ask for approval, present a design for approval, or wait for a human.\n"
        "Do not stop after analysis — ship code + tests for this work order.\n"
        "Do not expand scope beyond this work order.\n"
        "Do not commit, push, or open a pull request — the host backend handles that.\n"
    )
    cited = [path for path in (revision_files or []) if str(path).strip()]
    if cited:
        hard_rules += (
            "You may edit only these files: "
            + ", ".join(cited)
            + ". Revert every other change. You may also update an existing lockfile of a manifest in that list. "
            "Do not add a new dependency file.\n"
        )
    pr_line = (
        f"Update the existing pull request {pr_url}.\n"
        if str(pr_url or "").strip()
        else "Update the existing pull request for this work-order branch; do not open a new PR.\n"
    )
    qa_section = ""
    review = str(review_feedback or "").strip()
    if review:
        qa_section += (
            "Code review blocked this delivery. Fix every blocking finding before finishing.\n"
            f"{pr_line}"
            f"Code review findings:\n{review}\n"
        )
    feedback = str(qa_feedback or "").strip()
    if feedback:
        qa_section += (
            "QA rejected this delivery. Address every defect before finishing.\n"
            f"{'' if review else pr_line}"
            f"QA feedback:\n{feedback}\n"
        )
    if docs_materialized and primary_spec_path:
        skill_line = (
            "Load implement-work-order, coding-practices, and bdd-acceptance "
            "from .opencode/skills/ when available.\n"
            if skills_materialized
            else ""
        )
        return (
            f"{qa_section}"
            f"Implement work order {work_order_id} only.\n"
            f"Primary spec: {primary_spec_path}\n"
            "Use docs/refinery/, docs/foundry/, and docs/planner/ as supporting context.\n"
            f"{skill_line}"
            "Follow acceptance criteria exactly. Prefer small, testable changes.\n"
            f"{hard_rules}"
            "Planning docs under docs/ are intentional and must remain in the change set.\n"
        )
    return (
        f"{qa_section}"
        f"Implement work order {work_order_id} only.\n"
        "Follow acceptance criteria exactly. Prefer small, testable changes.\n"
        f"{hard_rules}\n"
        f"{work_order_body.strip()}"
    )


def build_refresh_prompt(work_order_id: str, work_order_body: str, conflicted: list[str]) -> str:
    files = "\n".join(f"- {path}" for path in conflicted)
    return (
        f"The branch for work order {work_order_id} was merged with its updated base branch "
        "and the merge stopped on conflicts.\n"
        "Resolve the merge conflicts only. Keep the intent of both sides: the base branch "
        "changes and this work order's changes.\n"
        "Do not change unrelated code, do not add features, and remove every conflict marker.\n"
        "This is a non-interactive host run: do not ask for approval or wait for a human.\n"
        "Do not commit, push, or open a pull request — the host backend handles that.\n"
        f"Conflicted files:\n{files}\n\n"
        f"Work order for context:\n{str(work_order_body or '').strip()}"
    )


_CONFLICT_MARKER_RE = re.compile(r"^(<{7}|>{7})( |$)", re.MULTILINE)


def files_with_conflict_markers(repo_dir: Path, paths: list[str]) -> list[str]:
    remaining = []
    for path in paths:
        target = Path(repo_dir) / path
        if not target.is_file():
            continue
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _CONFLICT_MARKER_RE.search(text):
            remaining.append(path)
    return remaining


def _primary_work_order_path(written: list[str], work_order_id: str) -> str | None:
    wo_id = str(work_order_id or "").strip().upper()
    if not wo_id:
        return None
    prefix = f"docs/planner/work-orders/{wo_id}"
    for path in written:
        name = Path(path).name.upper()
        if path.startswith("docs/planner/work-orders/") and name.startswith(wo_id):
            return path
    # Prefer exact prefix match even if slug missing from list order
    for path in written:
        if path.startswith(prefix):
            return path
    return None


def _has_planning_packs(request: CodingRequest) -> bool:
    return any(
        isinstance(pack, dict) and pack
        for pack in (
            request.refinery_artifacts,
            request.foundry_artifacts,
            request.planner_artifacts,
        )
    )


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    token = env.get("GITHUB_TOKEN", "").strip()
    if token:
        env.setdefault("GH_TOKEN", token)
    return env


class OpenCodeBackend:
    def __init__(
        self,
        *,
        tools_root: str = "",
        binary_name: str = "opencode",
        run_args: list[str] | None = None,
        verification_fix_passes: int = 2,
        runner=subprocess.run,
        github: GitHubClient | None = None,
        sleep: Callable[[float], None] = time.sleep,
        security: SecurityConfig | None = None,
    ):
        self.tools_root = tools_root
        self.binary_name = binary_name
        self.run_args = list(run_args or ["run", "--auto"])
        self.verification_fix_passes = max(0, int(verification_fix_passes))
        self.runner = runner
        self.github = github
        self.sleep = sleep
        self.security = security or SecurityConfig()

    def _run_opencode(
        self,
        cmd: list[str],
        *,
        cwd: str,
        env: dict[str, str],
        log_path: Path,
        work_order_id: str,
    ) -> subprocess.CompletedProcess[str]:
        # Injected test runners keep the capture_output contract; production streams.
        if self.runner is not subprocess.run:
            return self.runner(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
        return stream_opencode_process(
            cmd,
            cwd=cwd,
            env=env,
            log_path=log_path,
            work_order_id=work_order_id,
        )

    def _verify_with_fixes(
        self,
        repo_dir: Path,
        request: CodingRequest,
        *,
        binary: str,
        provider: str,
        env: dict[str, str],
        branch: str,
        log: str,
        log_path: Path,
        skills_materialized: bool,
        check_changeset: bool = True,
    ) -> tuple[CodingResult | None, str, int, list[str], dict[str, Any]]:
        """Run host verification with OpenCode fix passes.

        Returns (failure, log, passes, changed, security block of the last scan).
        """
        fix_passes_used = 0
        verify_logs: list[str] = []
        base_ref = f"origin/{request.base_branch}" if request.base_branch else None
        while True:
            self._revert_undeclared(repo_dir, request)
            changed = list_changed_files(repo_dir, base_ref=base_ref, runner=self.runner)
            verify = verify_implementation(
                repo_dir,
                work_order_id=request.work_order_id,
                work_order_body=request.work_order_body,
                changed_files=changed if check_changeset else None,
                runner=self.runner,
                security=self.security,
                base_ref=base_ref,
            )
            verify_logs.append(
                "\n".join(part for part in [verify.summary, verify.log_tail] if part)
            )
            if verify.ok:
                return None, log, fix_passes_used, changed, verify.security
            if fix_passes_used >= self.verification_fix_passes:
                changed = list_changed_files(repo_dir, runner=self.runner)
                return (
                    CodingResult(
                        ok=False,
                        branch=branch,
                        summary=(
                            "Host verification failed after fix passes "
                            f"(fix_passes_used={fix_passes_used})"
                        ),
                        changed_files=changed,
                        log_tail="\n".join([log, *verify_logs])[-8000:],
                        log_path=str(log_path),
                        error=(
                            "verification_failed: "
                            + (", ".join(verify.reasons) or "unknown")
                            + f"; fix_passes_used={fix_passes_used}"
                        ),
                        security=verify.security,
                    ),
                    log,
                    fix_passes_used,
                    changed,
                    verify.security,
                )
            fix_prompt = build_verification_fix_prompt(
                request.work_order_id,
                verify,
                skills_materialized=skills_materialized,
            )
            fix_cmd = build_opencode_command(
                binary=binary,
                repo_dir=repo_dir,
                prompt=fix_prompt,
                run_args=self.run_args,
                provider=provider,
                model=request.model,
            )
            fix_completed = self._run_opencode(
                fix_cmd,
                cwd=str(repo_dir),
                env=env,
                log_path=log_path,
                work_order_id=request.work_order_id,
            )
            fix_passes_used += 1
            fix_log = "\n".join(
                part
                for part in [
                    (fix_completed.stdout or "")[-3000:],
                    (fix_completed.stderr or "")[-2000:],
                ]
                if part
            )
            log = "\n".join(part for part in [log, fix_log] if part)[-8000:]
            changed = list_changed_files(repo_dir, runner=self.runner)
            if fix_completed.returncode != 0:
                return (
                    CodingResult(
                        ok=False,
                        branch=branch,
                        summary=(
                            "OpenCode verification fix exited with failure "
                            f"(fix_passes_used={fix_passes_used})"
                        ),
                        changed_files=changed,
                        log_tail="\n".join([log, *verify_logs])[-8000:],
                        log_path=str(log_path),
                        error=(
                            f"opencode fix exit {fix_completed.returncode}; "
                            f"fix_passes_used={fix_passes_used}"
                        ),
                        security=verify.security,
                    ),
                    log,
                    fix_passes_used,
                    changed,
                    verify.security,
                )

    def _head_sha(self, repo_dir: Path) -> str:
        completed = self.runner(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        return str(completed.stdout or "").strip()

    def _names_since(self, repo_dir: Path, baseline: str) -> list[str]:
        completed = self.runner(
            ["git", "diff", "--name-only", f"{baseline}..HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=False,
        )
        return [line.strip() for line in str(completed.stdout or "").splitlines() if line.strip()]

    def _working_tree_names(self, repo_dir: Path) -> list[str]:
        names: list[str] = []
        for args in (
            ["git", "diff", "--name-only", "HEAD"],
            ["git", "diff", "--name-only", "--cached", "HEAD"],
            ["git", "ls-files", "--others", "--exclude-standard"],
        ):
            completed = self.runner(
                args, cwd=str(repo_dir), capture_output=True, text=True, check=False
            )
            names.extend(line.strip() for line in str(completed.stdout or "").splitlines() if line.strip())
        return sorted(set(names))

    def _revert_undeclared(self, repo_dir: Path, request: CodingRequest) -> None:
        """Drop files the work order did not name before verification. Review revisions keep the reject path."""
        if request.revision_files:
            return
        from ts_harness.workflows.code_review import undeclared_paths

        for path in undeclared_paths(self._working_tree_names(repo_dir), request.work_order_body):
            self.runner(
                ["git", "checkout", "HEAD", "--", path],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                check=False,
            )
            self.runner(
                ["git", "clean", "-f", "--", path],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                check=False,
            )

    def _with_scanned_sha(self, repo_dir: Path, security: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(security, dict):
            return security
        sha = self._head_sha(repo_dir)
        if not sha:
            return security
        return {**security, "scanned_sha": sha}

    def _reject_out_of_scope_revision(
        self,
        repo_dir: Path,
        request: CodingRequest,
        *,
        baseline: str,
        binary: str,
        provider: str,
        env: dict[str, str],
        branch: str,
        log: str,
        log_path: Path,
    ) -> CodingResult | None:
        cited = [path for path in (request.revision_files or []) if str(path).strip()]
        if not cited or not baseline:
            return None
        from ts_harness.implementation.revision_scope import (
            allowed_revision_paths,
            unexpected_revision_paths,
        )

        def extras() -> list[str]:
            allowed = allowed_revision_paths(cited, repo_dir)
            return unexpected_revision_paths(self._names_since(repo_dir, baseline), allowed)

        extra = extras()
        if not extra:
            return None
        prompt = (
            "Revert changes to these files: "
            + ", ".join(extra)
            + ". You may only edit: "
            + ", ".join(cited)
            + ". Do not add a new dependency file.\n"
        )
        fix_cmd = build_opencode_command(
            binary=binary,
            repo_dir=repo_dir,
            prompt=prompt,
            run_args=self.run_args,
            provider=provider,
            model=request.model,
        )
        self._run_opencode(
            fix_cmd,
            cwd=str(repo_dir),
            env=env,
            log_path=log_path,
            work_order_id=request.work_order_id,
        )
        commit_all_changes(repo_dir, message=commit_message(request), runner=self.runner)
        extra = extras()
        if not extra:
            return None
        changed = list_changed_files(repo_dir, runner=self.runner)
        return CodingResult(
            ok=False,
            branch=branch,
            summary="Revision edited files the blocking findings did not name",
            changed_files=changed,
            log_tail=log,
            log_path=str(log_path),
            error="revision_scope: " + ", ".join(extra),
        )

    def implement(self, request: CodingRequest) -> CodingResult:
        if request.mode == "refresh":
            return self.refresh(request)
        provider, api_key = resolve_coding_backend_credentials(provider=request.provider)
        if not provider:
            return CodingResult(
                ok=False,
                branch="",
                summary="",
                error="CODING_BACKEND_PROVIDER is required",
            )
        if not api_key:
            return CodingResult(
                ok=False,
                branch="",
                summary="",
                error="CODING_BACKEND_API_KEY is required",
            )
        if not str(request.repo_url or "").strip():
            return CodingResult(
                ok=False,
                branch="",
                summary="",
                error="implementation repo url is required",
            )
        if not str(request.work_order_body or "").strip():
            return CodingResult(
                ok=False,
                branch="",
                summary="",
                error="work_order_body is required (governed markdown missing)",
            )
        github_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not github_token:
            return CodingResult(
                ok=False,
                branch="",
                summary="",
                error="GITHUB_TOKEN is required for clone/push/PR",
            )

        branch = work_order_branch_name(request.work_order_id)
        log_path = opencode_progress_log_path(
            request.workspace_dir, request.work_order_id
        )
        try:
            binary = resolve_opencode_binary(tools_root=self.tools_root, binary_name=self.binary_name)
            repo_dir = ensure_repo_clone(
                repo_url=request.repo_url,
                workspace_dir=request.workspace_dir,
                base_branch=request.base_branch,
                clone_depth=request.clone_depth,
                runner=self.runner,
                github_token=github_token,
            )
            create_work_branch(
                repo_dir,
                branch=branch,
                base_branch=request.base_branch,
                runner=self.runner,
            )
            baseline = self._head_sha(repo_dir) if request.revision_files else ""
            docs_written: list[str] = []
            skills_written: list[str] = []
            primary_spec: str | None = None
            try:
                skills_written = materialize_coding_backend_skills(
                    repo_dir,
                    coding_backend="opencode",
                )
            except Exception:
                skills_written = []
            try:
                # Never enable Superpowers here — brainstorming blocks non-interactive runs.
                ensure_opencode_project_config(repo_dir / ".opencode")
            except Exception:
                pass
            if _has_planning_packs(request):
                planner = dict(request.planner_artifacts or {})
                if request.work_order_body and not planner.get("work_orders_markdown"):
                    planner["work_orders_markdown"] = request.work_order_body
                files = planning_docs_file_map(
                    request.refinery_artifacts,
                    request.foundry_artifacts,
                    planner,
                    approved_prd_markdown=request.approved_prd_markdown,
                    active_work_order_id=request.work_order_id,
                )
                docs_written = materialize_planning_docs(repo_dir, files)
                primary_spec = _primary_work_order_path(docs_written, request.work_order_id)
            # Install declared deps before OpenCode/verify so runners exist on PATH/modules.
            prep_log = prepare_test_dependencies(repo_dir, runner=self.runner)
            prompt = build_implement_prompt(
                request.work_order_id,
                request.work_order_body,
                primary_spec_path=primary_spec,
                docs_materialized=bool(docs_written),
                skills_materialized=bool(skills_written),
                qa_feedback=request.qa_feedback,
                pr_url=request.pr_url,
                review_feedback=request.review_feedback,
                revision_files=request.revision_files,
            )
            cmd = build_opencode_command(
                binary=binary,
                repo_dir=repo_dir,
                prompt=prompt,
                run_args=self.run_args,
                provider=provider,
                model=request.model,
            )
            env = os.environ.copy()
            env.update(provider_child_env(provider, api_key))
            completed = self._run_opencode(
                cmd,
                cwd=str(repo_dir),
                env=env,
                log_path=log_path,
                work_order_id=request.work_order_id,
            )
            log = "\n".join(
                part
                for part in [
                    prep_log,
                    (completed.stdout or "")[-3000:],
                    (completed.stderr or "")[-2000:],
                ]
                if part
            )
            if len(log) > _OPENCODE_LOG_TAIL_CHARS:
                log = log[-_OPENCODE_LOG_TAIL_CHARS:]
            changed = list_changed_files(repo_dir, runner=self.runner)
            if completed.returncode != 0:
                return CodingResult(
                    ok=False,
                    branch=branch,
                    summary="OpenCode exited with failure",
                    changed_files=changed,
                    log_tail=log,
                    log_path=str(log_path),
                    error=f"opencode exit {completed.returncode}",
                )

            self._revert_undeclared(repo_dir, request)
            failure, log, fix_passes_used, changed, security = self._verify_with_fixes(
                repo_dir,
                request,
                binary=binary,
                provider=provider,
                env=env,
                branch=branch,
                log=log,
                log_path=log_path,
                skills_materialized=bool(skills_written),
            )
            if failure is not None:
                return failure

            committed = commit_all_changes(
                repo_dir,
                message=commit_message(request),
                runner=self.runner,
            )
            if not committed and not changed:
                return CodingResult(
                    ok=False,
                    branch=branch,
                    summary="OpenCode produced no file changes",
                    changed_files=changed,
                    log_tail=log,
                    log_path=str(log_path),
                    error="no changes to commit after OpenCode run",
                )
            # Re-list after commit (porcelain may be empty when committed).
            if committed:
                changed = changed or list_changed_files(repo_dir, runner=self.runner)

            scope_failure = self._reject_out_of_scope_revision(
                repo_dir,
                request,
                baseline=baseline,
                binary=binary,
                provider=provider,
                env=env,
                branch=branch,
                log=log,
                log_path=log_path,
            )
            if scope_failure is not None:
                return scope_failure

            security = self._with_scanned_sha(repo_dir, security)
            git_env = _git_env()
            push_branch(repo_dir, branch=branch, runner=self.runner, env=git_env)
            pr_body = (
                f"## Work order\n\n`{request.work_order_id}`\n\n"
                "Opened by ts-harness `run_coding_backend` (OpenCode).\n"
                "Human merge required before the delivery program resumes.\n"
            )
            pr_body += attribution_markdown(request)
            pr_body += security_markdown(security)
            if docs_written:
                pr_body += (
                    "\n## Planning docs\n\n"
                    "Approved Refinery / Foundry / Planner specs were materialized under `docs/` "
                    "for this change and are included in the PR.\n"
                )
            if skills_written:
                pr_body += (
                    "\n## Coding-backend skills\n\n"
                    "Enterprise coding-backend skills were copied under `.opencode/skills/` "
                    "for OpenCode to load during implementation.\n"
                )
            pr_url = create_pull_request(
                repo_dir,
                title=f"{request.work_order_id}: governed delivery",
                body=pr_body,
                base_branch=request.base_branch,
                head_branch=branch,
                runner=self.runner,
                env=git_env,
            )
            return CodingResult(
                ok=True,
                branch=branch,
                summary=(
                    f"OpenCode implemented {request.work_order_id}; "
                    f"verification fix passes used {fix_passes_used}; PR {pr_url}"
                ),
                changed_files=changed,
                log_tail=log,
                log_path=str(log_path),
                pr_url=pr_url,
                security=security,
            )
        except Exception as exc:  # noqa: BLE001 - surface to agent tool
            return CodingResult(
                ok=False,
                branch=branch,
                summary="",
                log_path=str(log_path),
                error=str(exc),
            )

    # Refresh: bring an open PR up to date with its base branch.

    def _git(self, repo_dir: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        kwargs: dict[str, Any] = {"capture_output": True, "text": True, "check": False}
        if env is not None:
            kwargs["env"] = env
        return self.runner(["git", "-C", str(repo_dir), *args], **kwargs)

    def _wait_for_new_head(self, owner: str, repo: str, number: int, old_head: str) -> str:
        head = old_head
        for _ in range(10):
            pull = self.github.get_pull(owner, repo, number)
            head = str((pull.get("head") or {}).get("sha") or "")
            if head and head != old_head:
                return head
            self.sleep(2)
        return head

    def refresh(self, request: CodingRequest) -> CodingResult:
        pr_url = str(request.pr_url or "").strip()
        parsed = parse_github_pr_url(pr_url)
        if parsed is None:
            return CodingResult(ok=False, branch="", summary="", error="refresh requires a GitHub pr_url")
        owner, repo, number = parsed
        try:
            if self.github is None:
                self.github = GitHubClient.from_env()
            pull = self.github.get_pull(owner, repo, number)
        except GitHubError as exc:
            return CodingResult(ok=False, branch="", summary="", pr_url=pr_url, error=str(exc))
        head_ref = str((pull.get("head") or {}).get("ref") or "")
        head_sha = str((pull.get("head") or {}).get("sha") or "")
        base_ref = str((pull.get("base") or {}).get("ref") or request.base_branch)
        if pull.get("merged") or pull.get("state") == "closed":
            return CodingResult(
                ok=True,
                branch=head_ref,
                summary=f"PR {pr_url} is no longer open; nothing to refresh",
                pr_url=pr_url,
                head_sha=head_sha,
                refresh_method="skipped",
            )

        for attempt in range(2):
            try:
                self.github.update_branch(owner, repo, number, expected_head_sha=head_sha)
            except GitHubError as exc:
                message = str(exc).lower()
                if exc.status == 422 and "expected_head_sha" in message and attempt == 0:
                    pull = self.github.get_pull(owner, repo, number)
                    head_sha = str((pull.get("head") or {}).get("sha") or head_sha)
                    continue
                if exc.status == 422:
                    break
                return CodingResult(ok=False, branch=head_ref, summary="", pr_url=pr_url, error=str(exc))
            new_head = self._wait_for_new_head(owner, repo, number, head_sha)
            return CodingResult(
                ok=True,
                branch=head_ref,
                summary=f"GitHub merged {base_ref} into {head_ref} for {pr_url}",
                pr_url=pr_url,
                head_sha=new_head,
                refresh_method="server_merge",
            )
        return self._refresh_locally(request, pr_url=pr_url, owner=owner, repo=repo, head_ref=head_ref, base_ref=base_ref)

    def _refresh_locally(
        self,
        request: CodingRequest,
        *,
        pr_url: str,
        owner: str,
        repo: str,
        head_ref: str,
        base_ref: str,
    ) -> CodingResult:
        github_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not github_token:
            return CodingResult(ok=False, branch=head_ref, summary="", pr_url=pr_url, error="GITHUB_TOKEN is required for refresh")
        log_path = opencode_progress_log_path(request.workspace_dir, request.work_order_id)
        log = ""
        try:
            repo_dir = ensure_repo_clone(
                repo_url=request.repo_url or f"https://github.com/{owner}/{repo}.git",
                workspace_dir=request.workspace_dir,
                base_branch=base_ref,
                clone_depth=request.clone_depth,
                runner=self.runner,
                github_token=github_token,
            )
            refspecs = [
                f"+refs/heads/{head_ref}:refs/remotes/origin/{head_ref}",
                f"+refs/heads/{base_ref}:refs/remotes/origin/{base_ref}",
            ]
            fetched = self._git(repo_dir, "fetch", "--unshallow", "origin", *refspecs)
            if fetched.returncode != 0:
                fetched = self._git(repo_dir, "fetch", "origin", *refspecs)
            if fetched.returncode != 0:
                raise RuntimeError(f"git fetch failed: {(fetched.stderr or fetched.stdout or '').strip()[-2000:]}")
            checkout = self._git(repo_dir, "checkout", "-B", head_ref, f"origin/{head_ref}")
            if checkout.returncode != 0:
                raise RuntimeError(f"git checkout {head_ref} failed: {(checkout.stderr or checkout.stdout or '').strip()[-2000:]}")
            merged = self._git(repo_dir, "merge", f"origin/{base_ref}", "--no-edit")
            resolved: list[str] = []
            method = "local_merge"
            provider, api_key = resolve_coding_backend_credentials(provider=request.provider)
            env = os.environ.copy()
            if provider and api_key:
                env.update(provider_child_env(provider, api_key))
            binary = ""
            if merged.returncode != 0:
                conflicts = self._git(repo_dir, "diff", "--name-only", "--diff-filter=U")
                resolved = [line.strip() for line in (conflicts.stdout or "").splitlines() if line.strip()]
                if not resolved:
                    raise RuntimeError(f"git merge failed: {(merged.stderr or merged.stdout or '').strip()[-2000:]}")
                if not provider or not api_key:
                    raise RuntimeError("CODING_BACKEND_PROVIDER and CODING_BACKEND_API_KEY are required to resolve conflicts")
                binary = resolve_opencode_binary(tools_root=self.tools_root, binary_name=self.binary_name)
                completed = self._run_opencode(
                    build_opencode_command(
                        binary=binary,
                        repo_dir=repo_dir,
                        prompt=build_refresh_prompt(request.work_order_id, request.work_order_body, resolved),
                        run_args=self.run_args,
                        provider=provider,
                        model=request.model,
                    ),
                    cwd=str(repo_dir),
                    env=env,
                    log_path=log_path,
                    work_order_id=request.work_order_id,
                )
                log = "\n".join(part for part in [(completed.stdout or "")[-3000:], (completed.stderr or "")[-2000:]] if part)
                if completed.returncode != 0:
                    return CodingResult(
                        ok=False,
                        branch=head_ref,
                        summary="OpenCode conflict resolution exited with failure",
                        log_tail=log,
                        log_path=str(log_path),
                        pr_url=pr_url,
                        resolved_files=resolved,
                        error=f"opencode exit {completed.returncode}",
                    )
                remaining = files_with_conflict_markers(repo_dir, resolved)
                if remaining:
                    return CodingResult(
                        ok=False,
                        branch=head_ref,
                        summary="Conflict markers remain after OpenCode resolution",
                        log_tail=log,
                        log_path=str(log_path),
                        pr_url=pr_url,
                        resolved_files=resolved,
                        error="conflict_markers_remain: " + ", ".join(remaining),
                    )
                method = "conflict_resolution"
            failure, log, _passes, _changed, security = self._verify_with_fixes(
                repo_dir,
                request,
                binary=binary or self._optional_binary(),
                provider=provider or "",
                env=env,
                branch=head_ref,
                log=log,
                log_path=log_path,
                skills_materialized=False,
                check_changeset=False,
            )
            if failure is not None:
                failure.pr_url = pr_url
                failure.resolved_files = resolved
                return failure
            commit_all_changes(
                repo_dir,
                message=f"{request.work_order_id}: merge {base_ref} into branch",
                runner=self.runner,
            )
            security = self._with_scanned_sha(repo_dir, security)
            push_branch(repo_dir, branch=head_ref, runner=self.runner, env=_git_env())
            head_sha = (self._git(repo_dir, "rev-parse", "HEAD").stdout or "").strip() or None
            return CodingResult(
                ok=True,
                branch=head_ref,
                summary=f"Merged {base_ref} into {head_ref} ({method}) for {pr_url}",
                changed_files=resolved,
                log_tail=log,
                log_path=str(log_path),
                pr_url=pr_url,
                head_sha=head_sha,
                refresh_method=method,
                resolved_files=resolved,
                security=security,
            )
        except Exception as exc:  # noqa: BLE001 - surface to agent tool
            return CodingResult(
                ok=False,
                branch=head_ref,
                summary="",
                log_path=str(log_path),
                pr_url=pr_url,
                error=str(exc),
            )

    def _optional_binary(self) -> str:
        try:
            return resolve_opencode_binary(tools_root=self.tools_root, binary_name=self.binary_name)
        except FileNotFoundError:
            return self.binary_name
