"""Verification helpers for coding-backend implementation runs."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ts_harness.implementation.security_scan import SecurityConfig, run_security_scan
from ts_harness.implementation.test_env import (
    BDD_IN_UNIT_STACKS,
    HOST_TOOLCHAINS,
    TestEnvironment,
    bdd_test_command,
    detect_stack,
    missing_tools,
    unit_test_command,
)

_LOG_TAIL_CHARS = 4000
_SHADOW_MODULES = {"pytest": ("pytest", "_pytest"), "behave": ("behave",)}
_GWT_RE = re.compile(r"\bgiven\b.*\bwhen\b.*\bthen\b", re.IGNORECASE | re.DOTALL)
_NON_BEHAVIORAL_RE = re.compile(r"\b(chore|refactor|docs-only|rename only)\b", re.IGNORECASE)
_ACCEPTANCE_RE = re.compile(r"\bacceptance criteria\b|\bacceptance\b", re.IGNORECASE)
_CODE_SUFFIXES = (
    ".py",
    ".pyi",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
)


@dataclass
class VerifyResult:
    ok: bool
    reasons: list[str]
    summary: str
    log_tail: str
    unit_command: list[str] | None = None
    bdd_command: list[str] | None = None
    bdd_required: bool = False
    skipped_unit: bool = False
    skipped_bdd: bool = False
    security: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def looks_behavioral(work_order_body: str) -> bool:
    if has_gwt_acceptance(work_order_body):
        return True
    if _NON_BEHAVIORAL_RE.search(work_order_body) and not _ACCEPTANCE_RE.search(work_order_body):
        return False
    return True


def has_gwt_acceptance(*texts: str) -> bool:
    return any(_GWT_RE.search(text or "") for text in texts)


def normalize_changed_path(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    if " -> " in text:
        text = text.split(" -> ")[-1].strip()
    if text.startswith("./"):
        text = text[2:]
    return text


def is_junk_path(path: str) -> bool:
    norm = normalize_changed_path(path)
    parts = norm.split("/")
    return (
        "__pycache__" in parts
        or norm.endswith(".pyc")
        or norm.endswith(".pyo")
        or any(part.endswith(".egg-info") for part in parts)
        or "/.pytest_cache/" in f"/{norm}/"
        or norm.startswith(".pytest_cache/")
    )


def is_substantive_implementation_path(path: str) -> bool:
    """True for product/test/feature changes that count as WO delivery work."""
    norm = normalize_changed_path(path)
    if not norm or is_junk_path(norm):
        return False
    if norm.startswith(".opencode/"):
        return False
    if norm.startswith("docs/"):
        return False
    if norm.endswith(".feature"):
        return True
    if "/steps/" in f"/{norm}/" or norm.startswith("features/steps/"):
        return True
    if "/tests/" in f"/{norm}/" or norm.startswith("tests/") or norm.startswith("test/"):
        return True
    if norm.endswith(_CODE_SUFFIXES):
        return True
    return False


def validate_work_order_changeset(
    changed_files: list[str],
    *,
    work_order_id: str,
    work_order_body: str,
) -> list[str]:
    """Return verification reasons when the diff does not implement the work order."""
    reasons: list[str] = []
    paths = [normalize_changed_path(p) for p in changed_files if normalize_changed_path(p)]
    non_junk = [p for p in paths if not is_junk_path(p)]
    if not non_junk:
        reasons.append("no_substantive_changes")
        return reasons

    # Chores/docs-only WOs: any non-junk change is enough.
    if not looks_behavioral(work_order_body):
        return reasons

    substantive = [p for p in non_junk if is_substantive_implementation_path(p)]
    if not substantive:
        reasons.append("no_substantive_changes")

    if has_gwt_acceptance(work_order_body):
        wo = str(work_order_id or "").strip().lower()
        feature_paths = [p for p in paths if p.endswith(".feature")]
        wo_feature = [
            p
            for p in feature_paths
            if wo and wo.replace("_", "-") in p.lower().replace("_", "-")
        ]
        if not wo_feature:
            reasons.append("missing_bdd_feature_for_work_order")
        code_or_test = [
            p
            for p in substantive
            if p.endswith(_CODE_SUFFIXES) or "/tests/" in f"/{p}/" or p.startswith("tests/")
        ]
        if not code_or_test:
            reasons.append("missing_code_or_test_changes")
    return reasons


def detect_unit_test_command(repo_dir: Path, *, python: str = "python3") -> list[str] | None:
    return unit_test_command(Path(repo_dir), python=python)


def detect_bdd_command(repo_dir: Path, *, python: str = "python3") -> list[str] | None:
    return bdd_test_command(Path(repo_dir), python=python)


def shadowed_runner_modules(repo_dir: Path, runner: str | None = None) -> list[str]:
    """Repo-root modules that `python -m pytest|behave` would import instead of the real runner."""
    found = []
    for runner_name, modules in _SHADOW_MODULES.items():
        if runner is not None and runner != runner_name:
            continue
        for name in modules:
            if (Path(repo_dir) / f"{name}.py").is_file():
                found.append(f"{name}.py")
            if (Path(repo_dir) / name).is_dir():
                found.append(f"{name}/")
    return found


def _python_runner(command: list[str] | None) -> str | None:
    if command and len(command) >= 3 and command[1] == "-m" and command[2] in _SHADOW_MODULES:
        return command[2]
    return None


def repo_has_feature_files(repo_dir: Path) -> bool:
    return any(repo_dir.glob("**/*.feature"))


def collect_docs_text(repo_dir: Path) -> str:
    docs_parts: list[str] = []
    for name in ("refinery", "foundry", "planner"):
        docs_dir = repo_dir / "docs" / name
        if not docs_dir.is_dir():
            continue
        for path in sorted(docs_dir.rglob("*")):
            if path.is_file():
                docs_parts.append(_read_text(path))
    return "\n\n".join(part for part in docs_parts if part)


def prepare_test_dependencies(
    repo_dir: Path,
    *,
    runner=subprocess.run,
) -> str:
    """Best-effort install of declared Python deps so host verify can run."""
    req = Path(repo_dir) / "requirements.txt"
    if not req.is_file():
        return ""
    completed = runner(
        ["python3", "-m", "pip", "install", "-q", "-r", str(req)],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    detail = (completed.stderr or completed.stdout or "").strip()
    if completed.returncode != 0:
        return f"pip install failed (exit {completed.returncode}): {detail[-1500:]}"
    return "pip install -r requirements.txt ok"


def verify_implementation(
    repo_dir: Path,
    *,
    work_order_body: str,
    work_order_id: str = "",
    changed_files: list[str] | None = None,
    runner=subprocess.run,
    security: SecurityConfig | None = None,
    base_ref: str | None = None,
    which=None,
    test_env: TestEnvironment | None = None,
) -> VerifyResult:
    reasons: list[str] = []
    logs: list[str] = []
    unit_command: list[str] | None = None
    bdd_command: list[str] | None = None
    skipped_unit = False
    skipped_bdd = False
    python = test_env.python if test_env is not None else "python3"
    env = test_env.merged_env() if test_env is not None else None

    def blocked(command: list[str]) -> bool:
        """Add a reason and skip the run when the command cannot be trusted or started."""
        runner_name = _python_runner(command)
        shadowed = shadowed_runner_modules(repo_dir, runner_name) if runner_name else []
        if shadowed:
            reasons.append("shadowed_test_runner:" + ",".join(shadowed))
            return True
        if env is not None:
            missing = missing_tools(command, env)
            if missing:
                reasons.extend(f"missing_toolchain:{tool}" for tool in missing)
                return True
        return False

    behavioral = looks_behavioral(work_order_body)
    docs_text = collect_docs_text(repo_dir)
    bdd_required = has_gwt_acceptance(work_order_body, docs_text)

    if changed_files is not None:
        reasons.extend(
            validate_work_order_changeset(
                changed_files,
                work_order_id=work_order_id,
                work_order_body=work_order_body,
            )
        )

    unit_ran = False
    if behavioral:
        unit_command = detect_unit_test_command(repo_dir, python=python)
        if unit_command is None:
            reasons.append("no_test_runner")
            skipped_unit = True
        elif blocked(unit_command):
            skipped_unit = True
        else:
            unit_result = _run_command(runner, unit_command, repo_dir, env=env)
            unit_ran = True
            logs.append(_format_command_log("unit", unit_command, unit_result))
            if unit_result.returncode != 0:
                reasons.append("unit_tests_failed")
    else:
        skipped_unit = True

    if bdd_required:
        has_features = repo_has_feature_files(repo_dir)
        if not has_features:
            reasons.append("no_bdd_runner_or_features")
            skipped_bdd = True
        else:
            bdd_command = detect_bdd_command(repo_dir, python=python)
            bdd_in_unit = bdd_command is None and detect_stack(repo_dir) in BDD_IN_UNIT_STACKS
            if bdd_in_unit:
                bdd_command = unit_command or detect_unit_test_command(repo_dir, python=python)
            if bdd_command is None:
                reasons.append("no_bdd_runner_or_features")
                skipped_bdd = True
            elif bdd_in_unit and behavioral:
                skipped_bdd = not unit_ran
            elif blocked(bdd_command):
                skipped_bdd = True
            else:
                bdd_result = _run_command(runner, bdd_command, repo_dir, env=env)
                logs.append(_format_command_log("bdd", bdd_command, bdd_result))
                if bdd_result.returncode != 0:
                    reasons.append("bdd_tests_failed")
    else:
        skipped_bdd = True

    security_block: dict[str, Any] = {}
    warnings: list[str] = []
    if security is not None:
        scan_kwargs = {"which": which} if which is not None else {}
        security_reasons, security_block, warnings = run_security_scan(
            repo_dir,
            config=security,
            base_ref=base_ref,
            changed_files=changed_files,
            runner=runner,
            **scan_kwargs,
        )
        reasons.extend(security_reasons)
        logs.append(_format_security_log(security_block))

    # Deduplicate while preserving order.
    deduped: list[str] = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    reasons = deduped

    ok = not reasons
    summary = "verification passed" if ok else "verification failed: " + ", ".join(reasons)
    log_tail = "\n".join(logs)[-_LOG_TAIL_CHARS:]
    return VerifyResult(
        ok=ok,
        reasons=reasons,
        summary=summary,
        log_tail=log_tail,
        unit_command=unit_command,
        bdd_command=bdd_command,
        bdd_required=bdd_required,
        skipped_unit=skipped_unit,
        skipped_bdd=skipped_bdd,
        security=security_block,
        warnings=warnings,
    )


def build_verification_fix_prompt(
    work_order_id: str,
    verify: VerifyResult,
    *,
    skills_materialized: bool = False,
) -> str:
    reasons = ", ".join(verify.reasons) if verify.reasons else "unknown"
    log = verify.log_tail or "(no verification log captured)"
    skill_line = "Load coding-practices and bdd-acceptance from .opencode/skills/ when available."
    if skills_materialized:
        skill_line = "Load coding-practices and bdd-acceptance from .opencode/skills/."
    lines = [
        f"Host verification failed for {work_order_id}. Fix the failures below in this worktree.",
        f"Reasons: {reasons}",
        "This is a non-interactive host run: do not use brainstorming, do not ask for approval, "
        "and do not wait for a human — implement the work order immediately.",
        "Do not commit, push, or open a PR - the host owns git/gh.",
        skill_line,
        "Fixing runner/deps alone is not enough: deliver the work-order behavior with tests.",
    ]
    if "no_test_runner" in verify.reasons:
        lines.append(
            "Add the standard test runner for this repo's language, declared in its manifest, "
            "and tests that cover this work order's behavior. "
            f"The host provides: {', '.join(HOST_TOOLCHAINS)}."
        )
    shadowed = [r.split(":", 1)[1] for r in verify.reasons if r.startswith("shadowed_test_runner:")]
    if shadowed:
        lines.append(
            f"Delete the stand-in test framework modules ({', '.join(shadowed)}) and use the real "
            "framework; it is installed on the host. Do not recreate them under another name."
        )
    missing = [r.split(":", 1)[1] for r in verify.reasons if r.startswith("missing_toolchain:")]
    if missing:
        lines.append(
            f"The host lacks {', '.join(missing)}; do not try to install it or replace it with a stand-in. "
            "Fix the other reasons only."
        )
    if "no_bdd_runner_or_features" in verify.reasons or "missing_bdd_feature_for_work_order" in verify.reasons:
        lines.append(
            f"Add a Gherkin feature file for {work_order_id} (name should include the WO id) "
            "plus step definitions covering the acceptance criteria."
        )
    if "no_substantive_changes" in verify.reasons or "missing_code_or_test_changes" in verify.reasons:
        lines.append(
            "The current diff is not a valid implementation (e.g. only requirements/__pycache__). "
            "Implement production code and tests for this work order."
        )
    if "secrets_detected" in verify.reasons:
        locations = _secret_locations(verify.security)
        lines.append(
            "The change adds what look like secrets"
            + (f" at {locations}" if locations else "")
            + ". Remove them from the code and read the value from configuration or an "
            "environment variable instead. Do not commit real credentials, even in tests; "
            "use obvious placeholders."
        )
    if "vulnerable_dependency" in verify.reasons:
        packages = _vulnerable_packages(verify.security)
        lines.append(
            "A changed dependency has known vulnerabilities"
            + (f" ({packages})" if packages else "")
            + ". Upgrade to a patched version (or drop the dependency) in the manifest that declares it, "
            "and update its lockfile only if the repo already has one; do not add, delete or rewrite "
            "other dependency files."
        )
    lines.extend(["Verification log:", log])
    return "\n".join(lines)


def _secret_locations(security: dict[str, Any]) -> str:
    findings = (security.get("secret_scan") or {}).get("findings") or []
    return ", ".join(f"{item.get('file')}:{item.get('line')}" for item in findings[:20])


def _vulnerable_packages(security: dict[str, Any]) -> str:
    findings = (security.get("dependency_audit") or {}).get("findings") or []
    names = [
        f"{item.get('package')} {item.get('id') or item.get('severity') or ''}".strip()
        for item in findings[:20]
    ]
    return ", ".join(dict.fromkeys(names))


def _format_security_log(security: dict[str, Any]) -> str:
    """One-line scan summary; never includes matched secret values."""
    parts: list[str] = []
    secrets = security.get("secret_scan")
    if secrets is not None:
        found = secrets.get("findings") or []
        where = f" ({_secret_locations(security)})" if found else ""
        parts.append(f"secret scan [{secrets.get('tool')}]: {len(found)} finding(s){where}")
    audit = security.get("dependency_audit")
    if audit is not None:
        if audit.get("skipped_reason"):
            parts.append(f"dependency audit: {audit['skipped_reason']}")
        else:
            parts.append(f"dependency audit [{audit.get('tool')}]: {len(audit.get('findings') or [])} finding(s)")
    return "$ host security scan\n" + "\n".join(parts)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _run_command(
    runner,
    command: list[str],
    repo_dir: Path,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    kwargs = {"env": env} if env is not None else {}
    try:
        return runner(command, cwd=repo_dir, capture_output=True, text=True, **kwargs)
    except Exception as exc:  # pragma: no cover - exercised through subprocess-like runners.
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=str(exc))


def _format_command_log(
    label: str,
    command: list[str],
    result: subprocess.CompletedProcess,
) -> str:
    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    return f"$ {' '.join(command)} ({label}, exit {result.returncode})\n{output}".rstrip()
