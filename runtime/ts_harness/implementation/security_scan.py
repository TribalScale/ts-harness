"""Host-side secret scan and dependency audit over a coding-backend change set.

Only lines the change adds are scanned for secrets. Findings carry file, line
and rule; the matched value is never stored, logged or put in a prompt.
"""

from __future__ import annotations

import fnmatch
import json
import math
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SEVERITIES = ("low", "moderate", "high", "critical")
_SEVERITY_ALIASES = {"medium": "moderate", "info": "low", "unknown": "critical"}
MANIFESTS = (
    "requirements*.txt",
    "pyproject.toml",
    "poetry.lock",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
)
_PYTHON_MANIFESTS = ("requirements*.txt", "pyproject.toml", "poetry.lock", "uv.lock")
_NODE_MANIFESTS = ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock")
AUDIT_UNAVAILABLE = "dependency_audit_unavailable"
STATIC_UNAVAILABLE = "static_analysis_unavailable"
_STATIC_HIGH = {"high", "critical", "error"}
_OSV_NO_PACKAGES = 128
_MIN_GENERIC_ENTROPY = 3.5
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

FALLBACK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws-access-token", re.compile(r"\b((?:AKIA|ASIA)[0-9A-Z]{16})\b")),
    ("github-token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,})\b")),
    ("slack-token", re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})\b")),
    ("private-key", re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)")),
    (
        "generic-api-key",
        re.compile(r"""(?i)(?:key|secret|token|password)\w*["']?\s*[:=]\s*["']([A-Za-z0-9/+_\-]{20,})["']"""),
    ),
)

Runner = Callable[..., subprocess.CompletedProcess]
Which = Callable[[str], str | None]


@dataclass
class SecurityConfig:
    secret_scan: bool = True
    dependency_audit: bool = True
    audit_severity: str = "high"
    static_analysis: bool = True

    @classmethod
    def from_program_defaults(cls, program_defaults: dict[str, Any] | None) -> SecurityConfig:
        readiness = dict((program_defaults or {}).get("merge_readiness") or {})
        raw = dict(dict(readiness.get("policy") or {}).get("host_security") or {})
        severity = str(raw.get("audit_severity") or "high").lower()
        static = raw.get("static_analysis", True)
        return cls(
            secret_scan=bool(raw.get("secret_scan", True)),
            dependency_audit=bool(raw.get("dependency_audit", True)),
            audit_severity=severity if severity in SEVERITIES else "high",
            static_analysis=static if isinstance(static, bool) else True,
        )


@dataclass
class SecretScan:
    tool: str
    findings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class StaticScan:
    tool: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    skipped_reason: str = ""


@dataclass
class DepAudit:
    tool: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    skipped_reason: str = ""


def _run(runner: Runner, command: list[str], cwd: Path) -> subprocess.CompletedProcess:
    try:
        return runner(command, cwd=str(cwd), capture_output=True, text=True)
    except Exception as exc:  # noqa: BLE001 - a missing binary or OS error is a failed run
        return subprocess.CompletedProcess(command, 127, stdout="", stderr=str(exc))


def _norm(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    if " -> " in text:
        text = text.split(" -> ")[-1].strip()
    return text.removeprefix("./")


def changed_since(repo_dir: Path, base_ref: str | None, runner: Runner) -> list[str]:
    """Files changed against ``base_ref`` (committed or not) plus untracked files."""
    files: list[str] = []
    if base_ref:
        diff = _run(runner, ["git", "diff", "--name-only", "--diff-filter=d", base_ref], repo_dir)
        if diff.returncode == 0:
            files.extend(line for line in (diff.stdout or "").splitlines() if line.strip())
    untracked = _run(runner, ["git", "ls-files", "--others", "--exclude-standard"], repo_dir)
    if untracked.returncode == 0:
        files.extend(line for line in (untracked.stdout or "").splitlines() if line.strip())
    return list(dict.fromkeys(_norm(path) for path in files))


def _parse_added(diff_text: str) -> list[tuple[int, str]]:
    added: list[tuple[int, str]] = []
    line_no = 0
    for raw in diff_text.splitlines():
        hunk = _HUNK_RE.match(raw)
        if hunk:
            line_no = int(hunk.group(1))
            continue
        if raw.startswith(("+++", "---")):
            continue
        if raw.startswith("+") and line_no:
            added.append((line_no, raw[1:]))
            line_no += 1
    return added


def _all_lines(path: Path) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return list(enumerate(text.splitlines(), start=1))


def added_content(
    repo_dir: Path, base_ref: str | None, changed_files: list[str], runner: Runner
) -> dict[str, list[tuple[int, str]]]:
    """Map each changed file to the lines it adds relative to ``base_ref``.

    Files git cannot diff (untracked, or no usable base) count as fully added.
    """
    content: dict[str, list[tuple[int, str]]] = {}
    for rel in dict.fromkeys(_norm(path) for path in changed_files if _norm(path)):
        path = repo_dir / rel
        if not path.is_file():
            continue
        lines: list[tuple[int, str]] | None = None
        if base_ref:
            diff = _run(runner, ["git", "diff", "-U0", "--no-color", base_ref, "--", rel], repo_dir)
            if diff.returncode == 0 and (diff.stdout or "").strip():
                lines = _parse_added(diff.stdout)
            elif diff.returncode == 0:
                tracked = _run(runner, ["git", "ls-files", "--error-unmatch", "--", rel], repo_dir)
                lines = [] if tracked.returncode == 0 else None
        if lines is None:
            lines = _all_lines(path)
        if lines:
            content[rel] = lines
    return content


def _shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    total = len(value)
    return -sum((n / total) * math.log2(n / total) for n in counts.values())


def _ignore_entries(repo_dir: Path) -> list[str]:
    path = repo_dir / ".gitleaksignore"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def _ignored(entries: list[str], file: str, rule: str, line: int) -> bool:
    fingerprint = f"{file}:{rule}:{line}"
    return any(entry == fingerprint or entry.endswith(f":{fingerprint}") for entry in entries)


def _fallback_scan(content: dict[str, list[tuple[int, str]]], ignore: list[str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for file, lines in content.items():
        for line_no, text in lines:
            for rule, pattern in FALLBACK_PATTERNS:
                match = pattern.search(text)
                if not match:
                    continue
                if rule == "generic-api-key" and _shannon_entropy(match.group(1)) < _MIN_GENERIC_ENTROPY:
                    continue
                if _ignored(ignore, file, rule, line_no):
                    continue
                findings.append({"file": file, "line": line_no, "rule": rule})
                break
    return findings


def _gitleaks_scan(
    content: dict[str, list[tuple[int, str]]], ignore: list[str], binary: str, runner: Runner
) -> list[dict[str, Any]] | None:
    """Run gitleaks over a mirror holding only added lines; ``None`` when it fails."""
    with tempfile.TemporaryDirectory(prefix="claw-gitleaks-") as tmp:
        root = Path(tmp)
        mirror = root / "src"
        for file, lines in content.items():
            target = mirror / file
            target.parent.mkdir(parents=True, exist_ok=True)
            by_line = dict(lines)
            last = max(by_line) if by_line else 0
            target.write_text("\n".join(by_line.get(n, "") for n in range(1, last + 1)) + "\n", encoding="utf-8")
        if ignore:
            (mirror / ".gitleaksignore").write_text("\n".join(ignore) + "\n", encoding="utf-8")
        report = root / "report.json"
        completed = _run(
            runner,
            [binary, "dir", str(mirror), "--report-format", "json", "--report-path", str(report), "--no-banner", "--redact"],
            root,
        )
        if completed.returncode not in (0, 1) or not report.is_file():
            return None
        try:
            items = json.loads(report.read_text(encoding="utf-8") or "[]")
        except json.JSONDecodeError:
            return None
    findings: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        raw_file = _norm(str(item.get("File") or ""))
        prefix = _norm(str(mirror)).rstrip("/") + "/"
        file = raw_file.removeprefix(prefix)
        rule = str(item.get("RuleID") or "secret")
        line = int(item.get("StartLine") or 0)
        if _ignored(ignore, file, rule, line):
            continue
        findings.append({"file": file, "line": line, "rule": rule})
    return findings


def _added_lines(content: dict[str, list[tuple[int, str]]]) -> dict[str, set[int]]:
    return {file: {line for line, _text in lines} for file, lines in content.items()}


def _static_finding(file: str, line: int, rule: str, added: dict[str, set[int]]) -> dict[str, Any] | None:
    rel = _norm(file)
    for known, lines in added.items():
        if rel == known or rel.endswith("/" + known):
            if line in lines:
                return {"file": known, "line": line, "rule": rule}
    return None


def _semgrep_scan(
    repo_dir: Path,
    content: dict[str, list[tuple[int, str]]],
    binary: str,
    runner: Runner,
) -> list[dict[str, Any]] | None:
    files = list(content)
    if not files:
        return []
    completed = _run(runner, [binary, "--json", "--quiet", *files], repo_dir)
    payload = _json_output(completed, (0, 1))
    if not isinstance(payload, dict):
        return None
    added = _added_lines(content)
    findings: list[dict[str, Any]] = []
    for item in payload.get("results") or []:
        extra = dict(item.get("extra") or {})
        severity = str(extra.get("severity") or "").lower()
        if severity not in _STATIC_HIGH:
            continue
        start = dict(item.get("start") or {})
        finding = _static_finding(
            str(item.get("path") or ""),
            int(start.get("line") or 0),
            str(item.get("check_id") or "semgrep"),
            added,
        )
        if finding:
            findings.append(finding)
    return findings


def _bandit_scan(
    repo_dir: Path,
    content: dict[str, list[tuple[int, str]]],
    binary: str,
    runner: Runner,
) -> list[dict[str, Any]] | None:
    files = [path for path in content if path.endswith(".py")]
    if not files:
        return []
    completed = _run(runner, [binary, "-f", "json", *files], repo_dir)
    payload = _json_output(completed, (0, 1))
    if not isinstance(payload, dict):
        return None
    added = _added_lines(content)
    findings: list[dict[str, Any]] = []
    for item in payload.get("results") or []:
        severity = str(item.get("issue_severity") or "").lower()
        if severity not in _STATIC_HIGH:
            continue
        finding = _static_finding(
            str(item.get("filename") or ""),
            int(item.get("line_number") or 0),
            str(item.get("test_id") or "bandit"),
            added,
        )
        if finding:
            findings.append(finding)
    return findings


def scan_static(
    repo_dir: Path,
    base_ref: str | None,
    changed_files: list[str],
    runner: Runner = subprocess.run,
    which: Which = shutil.which,
) -> StaticScan:
    content = added_content(repo_dir, base_ref, changed_files, runner)
    semgrep = which("semgrep")
    if semgrep and content:
        findings = _semgrep_scan(repo_dir, content, semgrep, runner)
        if findings is not None:
            return StaticScan(tool="semgrep", findings=findings)
    bandit = which("bandit")
    if bandit and any(path.endswith(".py") for path in content):
        findings = _bandit_scan(repo_dir, content, bandit, runner)
        if findings is not None:
            return StaticScan(tool="bandit", findings=findings)
    return StaticScan(tool="", skipped_reason=STATIC_UNAVAILABLE)


def scan_secrets(
    repo_dir: Path,
    base_ref: str | None,
    changed_files: list[str],
    runner: Runner = subprocess.run,
    which: Which = shutil.which,
) -> SecretScan:
    content = added_content(repo_dir, base_ref, changed_files, runner)
    ignore = _ignore_entries(repo_dir)
    binary = which("gitleaks")
    if binary and content:
        findings = _gitleaks_scan(content, ignore, binary, runner)
        if findings is not None:
            return SecretScan(tool="gitleaks", findings=findings)
    return SecretScan(tool="builtin", findings=_fallback_scan(content, ignore))


def _is_manifest(path: str, patterns: tuple[str, ...] = MANIFESTS) -> bool:
    name = _norm(path).rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, pattern) for pattern in patterns)


def _severity_rank(value: str) -> int:
    name = _SEVERITY_ALIASES.get(str(value or "").lower(), str(value or "").lower())
    return SEVERITIES.index(name) if name in SEVERITIES else len(SEVERITIES) - 1


def _cvss_severity(score: Any) -> str:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if value >= 9:
        return "critical"
    if value >= 7:
        return "high"
    if value >= 4:
        return "moderate"
    return "low"


def _osv_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for result in payload.get("results") or []:
        source = str(((result.get("source") or {}).get("path")) or "")
        for pkg in result.get("packages") or []:
            info = pkg.get("package") or {}
            groups = {
                vuln_id: group.get("max_severity")
                for group in pkg.get("groups") or []
                for vuln_id in group.get("ids") or []
            }
            for vuln in pkg.get("vulnerabilities") or []:
                vuln_id = str(vuln.get("id") or "")
                severity = str(((vuln.get("database_specific") or {}).get("severity")) or "").lower()
                if not severity and vuln_id in groups:
                    severity = _cvss_severity(groups[vuln_id])
                findings.append(
                    {
                        "package": str(info.get("name") or ""),
                        "version": str(info.get("version") or ""),
                        "id": vuln_id,
                        "severity": severity or "unknown",
                        "source": source,
                    }
                )
    return findings


def _pip_audit_findings(payload: Any) -> list[dict[str, Any]]:
    deps = payload.get("dependencies") if isinstance(payload, dict) else payload
    findings: list[dict[str, Any]] = []
    for dep in deps or []:
        for vuln in dep.get("vulns") or []:
            findings.append(
                {
                    "package": str(dep.get("name") or ""),
                    "version": str(dep.get("version") or ""),
                    "id": str(vuln.get("id") or ""),
                    "severity": "unknown",
                    "fix_versions": list(vuln.get("fix_versions") or []),
                }
            )
    return findings


def _npm_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for name, vuln in dict(payload.get("vulnerabilities") or {}).items():
        findings.append(
            {
                "package": str(name),
                "range": str(vuln.get("range") or ""),
                "severity": str(vuln.get("severity") or "unknown"),
                "fix_available": bool(vuln.get("fixAvailable")),
            }
        )
    return findings


def _json_output(completed: subprocess.CompletedProcess, ok_codes: tuple[int, ...]) -> Any:
    if completed.returncode not in ok_codes:
        return None
    try:
        return json.loads(completed.stdout or "")
    except json.JSONDecodeError:
        return None


def audit_dependencies(
    repo_dir: Path,
    changed_files: list[str],
    severity: str = "high",
    runner: Runner = subprocess.run,
    which: Which = shutil.which,
) -> DepAudit:
    manifests = [path for path in changed_files if _is_manifest(path)]
    if not manifests:
        return DepAudit(tool="", skipped_reason="no_manifest_changes")
    threshold = _severity_rank(severity)

    def at_threshold(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [item for item in findings if _severity_rank(item.get("severity", "")) >= threshold]

    osv = which("osv-scanner")
    if osv:
        completed = _run(runner, [osv, "scan", "source", "--format", "json", "-r", "."], repo_dir)
        if completed.returncode == _OSV_NO_PACKAGES:
            return DepAudit(tool="osv-scanner")
        payload = _json_output(completed, (0, 1))
        if isinstance(payload, dict):
            return DepAudit(tool="osv-scanner", findings=at_threshold(_osv_findings(payload)))
        return DepAudit(tool="osv-scanner", skipped_reason=AUDIT_UNAVAILABLE)

    tools: list[str] = []
    findings: list[dict[str, Any]] = []
    unavailable = False
    python_changed = [path for path in manifests if _is_manifest(path, _PYTHON_MANIFESTS)]
    if python_changed:
        pip_audit = which("pip-audit")
        requirements = [path for path in python_changed if fnmatch.fnmatch(path.rsplit("/", 1)[-1], "requirements*.txt")]
        if not pip_audit:
            unavailable = True
        else:
            tools.append("pip-audit")
            targets = [["-r", path] for path in requirements] or [["."]]
            for target in targets:
                payload = _json_output(_run(runner, [pip_audit, "-f", "json", *target], repo_dir), (0, 1))
                if payload is None:
                    unavailable = True
                    continue
                findings.extend(_pip_audit_findings(payload))
    if any(_is_manifest(path, _NODE_MANIFESTS) for path in manifests):
        npm = which("npm")
        if not npm:
            unavailable = True
        else:
            tools.append("npm-audit")
            payload = _json_output(
                _run(runner, [npm, "audit", "--json", f"--audit-level={severity}"], repo_dir), (0, 1)
            )
            if isinstance(payload, dict):
                findings.extend(_npm_findings(payload))
            else:
                unavailable = True
    return DepAudit(
        tool="+".join(tools),
        findings=at_threshold(findings),
        skipped_reason=AUDIT_UNAVAILABLE if unavailable else "",
    )


def run_security_scan(
    repo_dir: Path,
    *,
    config: SecurityConfig,
    base_ref: str | None,
    changed_files: list[str] | None = None,
    runner: Runner = subprocess.run,
    which: Which = shutil.which,
) -> tuple[list[str], dict[str, Any], list[str]]:
    """Return (verify reasons, security block, warnings) for a change set."""
    changed = list(dict.fromkeys([*changed_since(repo_dir, base_ref, runner), *(changed_files or [])]))
    reasons: list[str] = []
    warnings: list[str] = []
    security: dict[str, Any] = {}
    if config.secret_scan:
        secrets = scan_secrets(repo_dir, base_ref, changed, runner, which)
        security["secret_scan"] = {"tool": secrets.tool, "findings": secrets.findings}
        if secrets.findings:
            reasons.append("secrets_detected")
    if config.dependency_audit:
        audit = audit_dependencies(repo_dir, changed, config.audit_severity, runner, which)
        security["dependency_audit"] = {
            "tool": audit.tool,
            "findings": audit.findings,
            "skipped_reason": audit.skipped_reason,
            "severity": config.audit_severity,
        }
        if audit.findings:
            reasons.append("vulnerable_dependency")
        if audit.skipped_reason == AUDIT_UNAVAILABLE:
            warnings.append(AUDIT_UNAVAILABLE)
    if config.static_analysis:
        static = scan_static(repo_dir, base_ref, changed, runner, which)
        security["static_analysis"] = {
            "tool": static.tool,
            "findings": static.findings,
            "skipped_reason": static.skipped_reason,
        }
        if static.findings:
            reasons.append("static_analysis_finding")
        if static.skipped_reason == STATIC_UNAVAILABLE:
            warnings.append(STATIC_UNAVAILABLE)
    security["warnings"] = list(warnings)
    return reasons, security, warnings
