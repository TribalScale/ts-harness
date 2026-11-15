"""Line coverage of the files a work order changed, as evidence for QA.

Runs only when the work order mentions coverage (or ``TS_HARNESS_COVERAGE=always``),
after verification passed, and never fails verification: QA compares the numbers
with the work order's targets. Supports Jest, Vitest (with a coverage provider
installed) and pytest with pytest-cov.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

_MENTIONS = re.compile(r"\bcoverage\b", re.IGNORECASE)
_SOURCE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs", ".py")


def coverage_wanted(work_order_body: str, environ: dict[str, str] | None = None) -> bool:
    mode = str((environ if environ is not None else os.environ).get("TS_HARNESS_COVERAGE") or "auto").strip().lower()
    if mode in {"0", "off", "never", "false"}:
        return False
    if mode in {"1", "on", "always", "true"}:
        return True
    return bool(_MENTIONS.search(work_order_body or ""))


def _package(repo_dir: Path) -> dict[str, Any]:
    try:
        return json.loads((Path(repo_dir) / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _script_args(script: str, tool: str) -> list[str]:
    """Arguments the repo's test script passes to ``tool`` (``jest --config x`` keeps ``--config x``)."""
    try:
        tokens = shlex.split(script)
    except ValueError:
        return []
    for index, token in enumerate(tokens):
        if Path(token).name == tool:
            args = []
            for arg in tokens[index + 1 :]:
                if arg in {"&&", "||", ";", "|"}:
                    break
                args.append(arg)
            return [arg for arg in args if arg not in {"--watch", "--watchAll", "watch"}]
    return []


def coverage_command(repo_dir: Path, out_dir: Path, *, python: str = "python3", env: dict[str, str] | None = None) -> tuple[str, list[str]] | None:
    """(tool, command) writing a coverage summary under ``out_dir``, or ``None`` when unsupported."""
    repo_dir = Path(repo_dir)
    package = _package(repo_dir)
    if package:
        script = str((package.get("scripts") or {}).get("test") or "")
        deps = {**(package.get("devDependencies") or {}), **(package.get("dependencies") or {})}
        jest = repo_dir / "node_modules" / ".bin" / "jest"
        vitest = repo_dir / "node_modules" / ".bin" / "vitest"
        if ("vitest" in script or "vitest" in deps) and vitest.exists():
            if not any(name in deps for name in ("@vitest/coverage-v8", "@vitest/coverage-istanbul")):
                return None
            args = [a for a in _script_args(script, "vitest") if a != "run"]
            return "vitest", [
                str(vitest), "run", *args, "--coverage.enabled=true", "--coverage.reporter=json-summary",
                f"--coverage.reportsDirectory={out_dir}", "--coverage.thresholds.lines=0",
            ]
        if ("jest" in script or "jest" in deps) and jest.exists():
            return "jest", [
                str(jest), *_script_args(script, "jest"), "--coverage", "--coverageReporters=json-summary",
                f"--coverageDirectory={out_dir}", "--coverageThreshold={}", "--passWithNoTests",
            ]
        return None
    probe = subprocess.run([python, "-c", "import pytest_cov"], capture_output=True, text=True, check=False, env=env)
    if probe.returncode != 0:
        return None
    return "pytest", [python, "-m", "pytest", "-q", "--cov=.", f"--cov-report=json:{out_dir / 'coverage.json'}", "--cov-fail-under=0"]


def _relative(path: str, repo_dir: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(repo_dir).resolve()))
    except ValueError:
        return path.lstrip("./")


def read_summary(tool: str, out_dir: Path, repo_dir: Path) -> tuple[float | None, dict[str, float]]:
    """(total line %, {repo-relative file: line %})."""
    if tool == "pytest":
        try:
            data = json.loads((out_dir / "coverage.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, {}
        files = {
            _relative(name, repo_dir): float((info.get("summary") or {}).get("percent_covered") or 0)
            for name, info in (data.get("files") or {}).items()
        }
        return float((data.get("totals") or {}).get("percent_covered") or 0), files
    try:
        data = json.loads((out_dir / "coverage-summary.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, {}
    total = (data.get("total") or {}).get("lines") or {}
    files = {
        _relative(name, repo_dir): float(((info or {}).get("lines") or {}).get("pct") or 0)
        for name, info in data.items()
        if name != "total" and isinstance(info, dict)
    }
    pct = total.get("pct")
    return (float(pct) if isinstance(pct, (int, float)) else None), files


def measure_coverage(
    repo_dir: Path,
    changed_files: list[str],
    *,
    out_dir: Path,
    runner=subprocess.run,
    env: dict[str, str] | None = None,
    python: str = "python3",
    timeout: int = 1800,
) -> dict[str, Any]:
    """Coverage evidence: tool, command, total and per changed source file. ``{}`` when unsupported."""
    out_dir.mkdir(parents=True, exist_ok=True)
    found = coverage_command(repo_dir, out_dir, python=python, env=env)
    if found is None:
        return {"measured": False, "reason": "no supported coverage tool (jest, vitest with a coverage provider, pytest-cov)"}
    tool, command = found
    try:
        result = runner(command, cwd=str(repo_dir), capture_output=True, text=True, check=False, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"measured": False, "tool": tool, "reason": f"timed out after {timeout}s"}
    total, files = read_summary(tool, out_dir, repo_dir)
    if total is None:
        tail = "\n".join(p for p in (result.stdout, result.stderr) if p).strip()[-600:]
        return {"measured": False, "tool": tool, "reason": f"no coverage summary (exit {result.returncode})", "output_tail": tail}
    from ts_harness.workflows.code_review import is_test_path

    changed = [
        path for path in changed_files if path.endswith(_SOURCE_SUFFIXES) and not is_test_path(path)
    ]
    return {
        "measured": True,
        "tool": tool,
        "command": " ".join(command),
        "exit_code": result.returncode,
        "total_lines_pct": round(total, 2),
        "changed_files": {path: (round(files[path], 2) if path in files else None) for path in changed},
    }


def coverage_summary(coverage: dict[str, Any] | None) -> list[str]:
    if not coverage:
        return []
    if not coverage.get("measured"):
        return [f"Coverage not measured: {coverage.get('reason')}."]
    lines = [f"Line coverage ({coverage.get('tool')}): {coverage.get('total_lines_pct')}% overall."]
    for path, pct in (coverage.get("changed_files") or {}).items():
        lines.append(f"- {path}: " + (f"{pct}% of lines" if pct is not None else "not loaded by any test (0%)"))
    return lines
