"""Verification helpers for coding-backend implementation runs."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ts_harness.implementation.coverage import coverage_summary, coverage_wanted, measure_coverage
from ts_harness.implementation.security_scan import SecurityConfig, run_security_scan
from ts_harness.implementation.test_env import (
    BDD_IN_UNIT_STACKS,
    HOST_TOOLCHAINS,
    TestEnvironment,
    bdd_test_command,
    detect_stack,
    missing_tools,
    node_static_checks,
    repo_feature_files,
    repo_runs_bdd,
    unit_test_command,
)
from ts_harness.implementation.test_services import (
    changed_playwright_specs,
    integration_plan,
    playwright_config,
    run_integration,
    screenshot_files,
    screenshot_name,
)

ONBOARD_MARKER = "<!-- ts-harness:onboard-repo -->"

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
    # One entry per command the host ran: what reviewers and QA see as test evidence.
    checks: list[dict[str, Any]] = field(default_factory=list)
    # Line coverage of the changed files when the work order asks about coverage (never blocks).
    coverage: dict[str, Any] = field(default_factory=dict)
    # Names of the screenshots the change's Playwright specs produced (files under screenshots_dir).
    screenshots: list[str] = field(default_factory=list)
    # One per design frame the work order names: its scenario's last state at the frame's size.
    frame_captures: list[dict[str, Any]] = field(default_factory=list)


_EVIDENCE_TAIL = 700


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _check_record(label: str, command: list[str], result: subprocess.CompletedProcess) -> dict[str, Any]:
    output = _ANSI.sub("", "\n".join(part for part in (result.stdout, result.stderr) if part)).strip()
    record = {"check": label, "command": " ".join(command), "exit_code": result.returncode, "output_tail": output[-_EVIDENCE_TAIL:]}
    seconds = getattr(result, "seconds", None)
    if isinstance(seconds, (int, float)):
        record["seconds"] = round(float(seconds), 1)
    return record


def verification_evidence(verify: "VerifyResult") -> dict[str, Any]:
    """What the host ran and how it ended, for the review and QA gates."""
    return {
        "result": "passed" if verify.ok else "failed",
        "reasons": list(verify.reasons),
        "checks": list(verify.checks),
        "skipped": [name for name, skipped in (("unit", verify.skipped_unit), ("bdd", verify.skipped_bdd)) if skipped],
        "coverage": dict(verify.coverage),
        "screenshots": list(verify.screenshots),
        "frame_captures": [
            {k: v for k, v in capture.items() if k != "file"} | {"captured": bool(capture.get("file"))}
            for capture in verify.frame_captures
        ],
        "warnings": list(verify.warnings),
    }


def verification_summary(evidence: dict[str, Any] | None) -> str:
    """Plain-text form of ``verification_evidence`` for the QA prompt."""
    if not evidence:
        return ""
    lines = [f"Host verification {evidence.get('result')} on the final commit's worktree."]
    for check in evidence.get("checks") or []:
        lines.append(f"- {check.get('check')}: `{check.get('command')}` exit {check.get('exit_code')}")
        tail = str(check.get("output_tail") or "").strip()
        if tail:
            lines.append("  output (end): " + tail.replace("\n", "\n  "))
    if evidence.get("skipped"):
        lines.append("Not run: " + ", ".join(evidence["skipped"]) + " (not required for this repo or work order).")
    lines.extend(coverage_summary(evidence.get("coverage")))
    if evidence.get("screenshots"):
        lines.append(
            f"Screenshots: {len(evidence['screenshots'])} from the Playwright tests this change added or edited "
            "(attached to QA and linked on the PR)."
        )
    captures = evidence.get("frame_captures") or []
    if captures:
        done = [c["id"] for c in captures if c.get("status") == "captured"]
        lines.append(
            f"Frame captures: {len(done)} of {len(captures)} design frames captured at their own size"
            + (f" ({', '.join(done)})" if done else "")
            + "."
        )
        for capture in captures:
            if capture.get("status") != "captured":
                lines.append(f"- {capture.get('id')}: {capture.get('status')} ({capture.get('note') or 'no detail'})")
    for warning in evidence.get("warnings") or []:
        lines.append(f"Note: {warning}")
    return "\n".join(lines)


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
    bdd: bool = True,
) -> list[str]:
    """Return verification reasons when the diff does not implement the work order.

    ``bdd`` False (a repo whose acceptance tests use its own runner, not Gherkin) drops the
    feature-file requirement; the code-or-test requirement stays.
    """
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
        if bdd and not wo_feature:
            reasons.append("missing_bdd_feature_for_work_order")
        code_or_test = [
            p
            for p in substantive
            if p.endswith(_CODE_SUFFIXES) or "/tests/" in f"/{p}/" or p.startswith("tests/")
        ]
        if not code_or_test:
            reasons.append("missing_code_or_test_changes")
    return reasons


def untouched_required_files(repo_dir: Path, work_order_body: str, base_ref: str | None, *, runner=subprocess.run) -> list[str]:
    """Files the work order's ``## Files`` list says to create, change or delete that the change
    left as they are on the base (a deletion counts as a change).

    A run once "succeeded" with a pull request that touched none of the files its work order named,
    because its edit scope kept OpenCode out of them; nothing compared the result with the list.
    """
    from ts_harness.workflows.code_review import required_paths

    required = required_paths(work_order_body)
    if not required:
        return []
    # `features/*-badge.feature` (from a `<JIRA-KEY>` placeholder) is a glob pathspec for git.
    pathspecs = [f":(glob){path}" if "*" in path else path for path in required]
    touched: set[str] = set()
    if base_ref:
        diff = runner(
            ["git", "-C", str(repo_dir), "diff", "--name-only", base_ref, "--", *pathspecs],
            capture_output=True, text=True, errors="replace", check=False,
        )
        if diff.returncode != 0:
            return []  # the base cannot be compared: do not block on it
        touched.update(line.strip() for line in (diff.stdout or "").splitlines() if line.strip())
    status = runner(
        ["git", "-C", str(repo_dir), "status", "--porcelain", "--untracked-files=all", "--", *pathspecs],
        capture_output=True, text=True, errors="replace", check=False,
    )
    for line in (status.stdout or "").splitlines():
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            touched.add(path)
    import fnmatch

    return [
        path
        for path in required
        if path not in touched and not ("*" in path and any(fnmatch.fnmatch(t, path) for t in touched))
    ]


ALREADY_CURRENT_FILE = ".ts-harness/already-current.json"


def already_current_files(repo_dir: Path) -> dict[str, str]:
    """``{path: evidence}`` from ``.ts-harness/already-current.json``: listed files the agent found
    already up to date on the base (e.g. regenerated by another ticket), with how it checked."""
    import json

    try:
        data = json.loads((Path(repo_dir) / ALREADY_CURRENT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = data if isinstance(data, list) else data.get("files") if isinstance(data, dict) else []
    out: dict[str, str] = {}
    for item in items or []:
        if isinstance(item, dict) and str(item.get("path") or "").strip() and str(item.get("evidence") or "").strip():
            out[str(item["path"]).strip()] = str(item["evidence"]).strip()[:500]
    return out


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
    return bool(repo_feature_files(repo_dir))


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


RELATED_TESTS_THRESHOLD = 120
_TEST_FILE = re.compile(r"\.(test|spec)\.(ts|tsx|js|jsx|mts|cts|mjs)$")


def count_test_files(repo_dir: Path, limit: int) -> int:
    """Test files in the repo, counted up to ``limit`` (dependencies and build output skipped)."""
    count = 0
    for root, dirs, files in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in {"node_modules", ".git", "dist", "build", ".next", ".turbo", "coverage"}]
        count += sum(1 for name in files if _TEST_FILE.search(name))
        if count > limit:
            break
    return count


def related_tests_command(repo_dir: Path, changed_files: list[str] | None, *, force: bool = False) -> list[str] | None:
    """For a large Vitest/Jest repo, only the tests that import the changed files.

    The full suite of a repo with hundreds of test files takes most of a coding job on two
    CPUs and reruns on every fix pass; CI still runs all of it on the PR.
    """
    from ts_harness.implementation.coverage import _package, _script_args

    sources = [p for p in (changed_files or []) if p.endswith((".ts", ".tsx", ".js", ".jsx", ".mts", ".cts", ".mjs")) and (Path(repo_dir) / p).is_file()]
    raw = os.environ.get("TS_HARNESS_RELATED_TESTS_THRESHOLD", "")
    threshold = int(raw) if raw.isdigit() else RELATED_TESTS_THRESHOLD
    if not sources:
        return None
    if not force and (threshold <= 0 or count_test_files(Path(repo_dir), threshold) <= threshold):
        return None
    package = _package(repo_dir)
    script = str((package.get("scripts") or {}).get("test") or "")
    bin_dir = Path(repo_dir) / "node_modules" / ".bin"
    deps = {**(package.get("devDependencies") or {}), **(package.get("dependencies") or {})}
    # Coverage off: a repo's thresholds measure the whole suite, and a partial run would fail
    # them. CI still enforces them on the full suite.
    if "vitest" in deps and (bin_dir / "vitest").exists():
        args = [a for a in _script_args(script, "vitest") if a not in {"run", "--watch"} and not a.startswith("--coverage")]
        return [str(bin_dir / "vitest"), "related", "--run", *args, "--coverage.enabled=false", *sources]
    if "jest" in deps and (bin_dir / "jest").exists():
        args = [a for a in _script_args(script, "jest") if not a.startswith("--coverage")]
        return [str(bin_dir / "jest"), *args, "--coverage=false", "--findRelatedTests", *sources, "--passWithNoTests"]
    return None


def same_suite_as_bdd(repo_dir: Path, plan: Any, bdd_command: list[str]) -> bool:
    """The integration plan runs the same tests as the BDD command."""
    import shlex

    from ts_harness.implementation.test_env import package_scripts

    integration = str(getattr(plan, "command", "") or "")
    bdd_text = shlex.join(bdd_command)
    if integration.strip() == bdd_text.strip() or integration.startswith(bdd_text + " "):
        return True
    scripts = package_scripts(repo_dir)
    bdd_script = next((name for name in ("test:bdd", "bdd", "test:acceptance") if name in bdd_text.split()), "")
    if bdd_script and f"run {bdd_script}" in integration:
        return True
    # Playwright with nothing declared runs every spec; in a playwright-bdd repo those are the features.
    if getattr(plan, "source", "") == "playwright" and "playwright" in str(scripts.get(bdd_script, "")):
        return True
    return False


def _touches_bdd(changed_files: list[str] | None) -> bool:
    """A feature file or a step definition changed, so a quick re-check still runs BDD."""
    for path in changed_files or []:
        lowered = path.lower()
        if lowered.endswith(".feature") or "/steps/" in lowered or "step_definitions" in lowered or lowered.startswith("features/"):
            return True
    return False


def screenshots_dir(test_env: TestEnvironment) -> Path:
    return Path(test_env.state_dir or ".") / "screenshots"


def frames_dir(test_env: TestEnvironment) -> Path:
    return Path(test_env.state_dir or ".") / "frames"


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
        text=True, errors="replace",
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
    integration_runner=run_integration,
    quick: bool = False,
) -> VerifyResult:
    """Run the repository's checks on the change.

    ``quick`` is the re-check after a small follow-up fix (the pre-push review's): the full
    verification already passed, so it runs the tests related to the changed files, the static
    checks and the security scan, and leaves BDD (unless a feature changed), the integration
    suite and coverage to the full run and CI.
    """
    reasons: list[str] = []
    warnings: list[str] = []
    logs: list[str] = []
    failed_logs: set[int] = set()
    from ts_harness.implementation.cgroup import oom_kills

    kills_before, memory_limit = oom_kills()
    checks: list[dict[str, Any]] = []
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
    # The work order's `BDD: required|off` (the program's per-repository decision) wins over
    # what the repository holds today; without it, decide from the repository.
    from ts_harness.workflows.bdd_policy import marker as bdd_marker

    declared = bdd_marker(work_order_body)
    runs_bdd = repo_runs_bdd(repo_dir) if declared is None else declared
    bdd_required = runs_bdd and has_gwt_acceptance(work_order_body, docs_text)

    if changed_files is not None:
        reasons.extend(
            validate_work_order_changeset(
                changed_files,
                work_order_id=work_order_id,
                work_order_body=work_order_body,
                bdd=runs_bdd,
            )
        )

    if changed_files is not None:
        untouched = untouched_required_files(repo_dir, work_order_body, base_ref, runner=runner)
        # Files the agent showed are already exactly what the work order's commands produce (another
        # ticket already did it) don't block; code review sees the evidence (ALREADY_CURRENT_FILE).
        current = already_current_files(repo_dir)
        untouched = [path for path in untouched if path not in current]
        if untouched:
            reasons.append("work_order_files_untouched:" + ",".join(untouched[:20]))

    # Files CI regenerates and checks with `git diff --exit-code` (openapi.json, codegen): regenerate
    # them here, so the change ships with them up to date instead of failing CI.
    if changed_files is not None:
        from ts_harness.implementation.generated_artifacts import regenerate

        for item in regenerate(repo_dir, env=env, runner=runner):
            result = subprocess.CompletedProcess(["sh", "-c", item["command"]], item["exit_code"],
                                                 stdout=item["output_tail"], stderr="")
            logs.append(_format_command_log("generate", ["sh", "-c", item["command"]], result))
            checks.append({**_check_record("generate", ["sh", "-c", item["command"]], result), "changed": item["changed"]})
            if item["exit_code"] != 0:
                failed_logs.add(len(logs) - 1)
                reasons.append("generated_files_failed")
            elif item["changed"]:
                warnings.append(f"regenerated {', '.join(item['changed'])} ({item['command']}), as CI checks")

    unit_ran = False
    if behavioral:
        unit_command = detect_unit_test_command(repo_dir, python=python)
        related = related_tests_command(repo_dir, changed_files, force=quick) if unit_command is not None else None
        if related is not None:
            unit_command = related
            warnings.append(
                "quick re-check: host ran the tests related to the changed files; CI runs the full suite" if quick
                else "large repo: host ran the tests related to the changed files; CI runs the full suite"
            )
        if unit_command is None:
            reasons.append("no_test_runner")
            skipped_unit = True
        elif blocked(unit_command):
            skipped_unit = True
        else:
            unit_result = _run_command(runner, unit_command, repo_dir, env=env)
            unit_ran = True
            logs.append(_format_command_log("unit", unit_command, unit_result))
            checks.append(_check_record("unit", unit_command, unit_result))
            if unit_result.returncode != 0:
                failed_logs.add(len(logs) - 1)
                reasons.append("unit_tests_failed")
    else:
        skipped_unit = True

    bdd_passed_command: list[str] | None = None
    if bdd_required and quick and not _touches_bdd(changed_files):
        bdd_required = False
        warnings.append("quick re-check: BDD not re-run (no feature or step file changed)")
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
                checks.append(_check_record("bdd", bdd_command, bdd_result))
                if bdd_result.returncode == 0:
                    bdd_passed_command = list(bdd_command)
                if bdd_result.returncode != 0:
                    failed_logs.add(len(logs) - 1)
                    reasons.append("bdd_tests_failed")
    else:
        skipped_bdd = True

    # What a Node repo's CI runs besides the tests; a red check here would only come back
    # later as a failed PR check and a revision round.
    if changed_files is not None:
        for label, command in node_static_checks(repo_dir, changed_files):
            if blocked(command):
                continue
            result = _run_command(runner, command, repo_dir, env=env)
            logs.append(_format_command_log(label, command, result))
            checks.append(_check_record(label, command, result))
            if result.returncode != 0:
                failed_logs.add(len(logs) - 1)
                reasons.append(f"{label}_failed")

    screenshots: list[str] = []
    if ONBOARD_MARKER in work_order_body and changed_files is not None:
        from ts_harness.implementation.repo_onboarding import repo_has_e2e_suite
        from ts_harness.implementation.test_services import load_repo_config

        declared = load_repo_config(repo_dir).get("integration")
        if repo_has_e2e_suite(repo_dir) and not (isinstance(declared, dict) and declared.get("command")):
            reasons.append("integration_suite_not_declared")
    if test_env is not None and test_env.state_dir is not None:
        shutil.rmtree(screenshots_dir(test_env), ignore_errors=True)
    plan = None
    if test_env is not None and changed_files is not None and not quick:
        plan = integration_plan(repo_dir, services_running=test_env.services_running, changed_files=changed_files)
    if plan is not None and bdd_passed_command and same_suite_as_bdd(repo_dir, plan, bdd_passed_command):
        # The integration suite is the Gherkin suite the bdd check just passed (a playwright-bdd
        # repo declares `npm run test:bdd`, or Playwright runs the generated specs): not twice.
        checks.append({"check": "integration", "command": plan.command, "skipped": "same suite as the bdd check, which passed"})
        plan = None
    if plan is not None:
        command = ["sh", "-c", plan.command]
        if plan.blocking and reasons:
            # A suite that resets databases and starts the app costs minutes; run it once the cheap checks pass.
            warnings.append("integration suite not run: earlier checks failed")
        else:
            specs = changed_playwright_specs(repo_dir, changed_files) if playwright_config(repo_dir, changed_files) else []
            if specs and test_env.state_dir is not None:
                plan.evidence_specs = specs
                plan.evidence_dir = str(screenshots_dir(test_env))
            result = integration_runner(repo_dir, plan, test_env.merged_env(), log_dir=test_env.state_dir)
            if result.returncode == 0 and plan.evidence_dir:
                screenshots = [screenshot_name(path) for path in screenshot_files(Path(plan.evidence_dir))]
            label = "integration" if plan.blocking else "integration (advisory)"
            logs.append(_format_command_log(label, command, result))
            checks.append(_check_record(label, command, result))
            if result.returncode != 0:
                failed_logs.add(len(logs) - 1)
                if plan.blocking:
                    reasons.append("integration_tests_failed")
                else:
                    warnings.append(f"integration suite ({plan.source}) failed; advisory until declared in .ts-harness.yml")

    if (
        not screenshots and not reasons and not quick and bdd_passed_command
        and test_env is not None and test_env.state_dir is not None and changed_files is not None
    ):
        from ts_harness.implementation.test_services import (
            capture_bdd_screenshots,
            changed_features,
            uses_playwright_bdd,
        )

        features = changed_features(changed_files)
        if features and uses_playwright_bdd(repo_dir):
            # The features run in Playwright: capture the change's own scenarios like plain specs.
            note = capture_bdd_screenshots(repo_dir, features, screenshots_dir(test_env), test_env.merged_env())
            logs.append(note)
            screenshots = [screenshot_name(path) for path in screenshot_files(screenshots_dir(test_env))]

    frame_captures: list[dict[str, Any]] = []
    if not reasons and not quick and test_env is not None and test_env.state_dir is not None:
        from ts_harness.implementation.frame_checklist import frames_to_capture

        frames = frames_to_capture(work_order_body)
        if frames:
            from ts_harness.implementation.test_services import capture_frames

            frame_captures, note = capture_frames(repo_dir, frames, frames_dir(test_env), test_env.merged_env())
            logs.append(note)

    coverage: dict[str, Any] = {}
    if not reasons and changed_files is not None and not quick and coverage_wanted(work_order_body):
        out_dir = (test_env.state_dir if test_env is not None and test_env.state_dir else Path(repo_dir).parent) / "coverage"
        coverage = measure_coverage(repo_dir, changed_files, out_dir=out_dir, runner=runner, env=env, python=python)

    security_block: dict[str, Any] = {}
    if security is not None:
        scan_kwargs = {"which": which} if which is not None else {}
        security_reasons, security_block, scan_warnings = run_security_scan(
            repo_dir,
            config=security,
            base_ref=base_ref,
            changed_files=changed_files,
            runner=runner,
            **scan_kwargs,
        )
        reasons.extend(security_reasons)
        warnings = [*warnings, *scan_warnings]
        logs.append(_format_security_log(security_block))

    if oom_kills()[0] > kills_before:
        # A check failed because the kernel killed it, not because of the code.
        reasons.insert(0, "out_of_memory")
        warnings.append(f"the kernel killed a verification command for memory (limit {memory_limit or 'unknown'})")

    # Deduplicate while preserving order.
    deduped: list[str] = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    reasons = deduped

    ok = not reasons
    summary = "verification passed" if ok else "verification failed: " + ", ".join(reasons)
    log_tail = _compose_log_tail(logs, failed_logs)
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
        checks=checks,
        coverage=coverage,
        screenshots=screenshots,
        frame_captures=frame_captures,
    )


def build_verification_fix_prompt(
    work_order_id: str,
    verify: VerifyResult,
    *,
    skills_materialized: bool = False,
    reverted_paths: list[str] | None = None,
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
        "Re-run only the failing tests and the tests of the files you change (`vitest related <files> --run`, "
        "the one failing feature with `npx playwright test -g \"<scenario>\"`), not the whole unit suite, the "
        "whole BDD suite or the build: the host runs all of them again right after you.",
    ]
    if reverted_paths:
        lines.append(
            "These files are protected (CI workflows, code owners) and the work order is not about them, "
            "so they were reverted: "
            + ", ".join(reverted_paths)
            + ". Do not change them again; make the change work without them."
        )
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
        if "no_bdd_runner_or_features" in verify.reasons:
            lines.append(
                "This repository has no Gherkin runner yet and its work uses BDD: add the lightest one for "
                "its stack (Cucumber.js or playwright-bdd for Node, pytest-bdd or behave for Python, godog "
                "for Go) with a `test:bdd` script or equivalent, so the host and CI run the features."
            )
    untouched = [r.split(":", 1)[1] for r in verify.reasons if r.startswith("work_order_files_untouched:")]
    if untouched:
        lines.append(
            "The work order's Files list names files this change has not touched yet: "
            + untouched[0].replace(",", ", ")
            + ". Create, change or delete each one as the work order says; the work is not done until they are. "
            "If one is already exactly what the work order's commands produce (you ran them and nothing "
            f"changed), list it in {ALREADY_CURRENT_FILE} as "
            '[{"path": "...", "evidence": "the commands you ran and what showed it is current"}] '
            "instead of editing it by hand; code review checks that evidence."
        )
    if "typecheck_failed" in verify.reasons:
        lines.append(
            "The type check failed. Fix the type errors in the code you changed or added; do not "
            "silence them with `any`, `@ts-ignore` or `@ts-expect-error`, and do not loosen tsconfig."
        )
    if "lint_failed" in verify.reasons:
        lines.append(
            "ESLint failed on files you changed. Fix what it reports (running it with --fix is fine "
            "for formatting); do not disable rules or add eslint-disable comments."
        )
    if "format_failed" in verify.reasons:
        lines.append(
            "Prettier reports unformatted files you changed, and the repo's CI checks formatting. Run the repo's "
            "Prettier with --write on exactly those files; do not reformat other files."
        )
    if "integration_suite_not_declared" in verify.reasons:
        lines.append(
            "This repo runs an e2e/integration suite in CI (or has a Playwright config), but .ts-harness.yml "
            "declares no `integration` block with a `command`. Declare it and make `ts-harness-integration` pass."
        )
    if "integration_tests_failed" in verify.reasons:
        lines.append(
            "The integration suite failed against this job's databases. Read its log below, fix the "
            "code or the tests, and rerun it with `ts-harness-integration` until it passes. Do not skip "
            "or delete failing tests, and do not change the repo's .ts-harness.yml to avoid them."
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
    timeout = _command_timeout(env)
    started = time.monotonic()
    try:
        result = runner(
            command, cwd=repo_dir, capture_output=True, text=True, errors="replace", stdin=subprocess.DEVNULL, timeout=timeout, **kwargs
        )
        try:
            result.seconds = time.monotonic() - started  # how long each check takes, in the evidence
        except AttributeError:
            pass
        return result
    except subprocess.TimeoutExpired as exc:
        output = "\n".join(str(part) for part in (exc.stdout, exc.stderr) if part)
        return subprocess.CompletedProcess(
            command,
            124,
            stdout=output[-6000:],
            stderr=f"timed out after {timeout}s: a test left running (watch mode, an open handle or a server) "
            "keeps the command from exiting",
        )
    except Exception as exc:  # pragma: no cover - exercised through subprocess-like runners.
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=str(exc))


def _command_timeout(env: dict[str, str] | None) -> int:
    import os

    raw = str((env or os.environ).get("TS_HARNESS_VERIFY_COMMAND_TIMEOUT") or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else 1800


def _compose_log_tail(logs: list[str], failed: set[int]) -> str:
    """The verification log within budget, keeping the end of every failing command's output.

    A plain tail of the joined logs dropped the unit test failures whenever a later check
    (type check, lint) also failed with long output.
    """
    if not failed:
        return "\n".join(logs)[-_LOG_TAIL_CHARS:]
    passed = [logs[i].splitlines()[0] for i in range(len(logs)) if i not in failed and logs[i]]
    share = max(400, (_LOG_TAIL_CHARS - sum(len(line) + 1 for line in passed)) // len(failed))
    parts = list(passed)
    for index in sorted(failed):
        text = logs[index]
        if len(text) > share:
            head = text.splitlines()[0]
            text = head + "\n...\n" + text[-(share - len(head) - 5):]
        parts.append(text)
    return "\n".join(parts)


def _format_command_log(
    label: str,
    command: list[str],
    result: subprocess.CompletedProcess,
) -> str:
    output = _ANSI.sub("", "\n".join(part for part in (result.stdout, result.stderr) if part))
    return f"$ {' '.join(command)} ({label}, exit {result.returncode})\n{output}".rstrip()
