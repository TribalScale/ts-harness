"""Host secret scan and dependency audit."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation import security_scan as ss

AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
GITHUB = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
SLACK = "xoxb-" + "1234567890-abcdefghij"
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----"
GENERIC = 'api_key = "' + "Zx9Qw3Er7Ty1Ui5Op2As8Df4" + '"'


class FakeGit:
    """Answers the git and scanner commands the module runs."""

    def __init__(self, repo: Path, *, diffs=None, tracked=(), changed=(), untracked=(), tools=None):
        self.repo = repo
        self.diffs = dict(diffs or {})
        self.tracked = set(tracked)
        self.changed = list(changed)
        self.untracked = list(untracked)
        self.tools = dict(tools or {})
        self.calls: list[list[str]] = []

    def __call__(self, command, cwd=None, capture_output=True, text=True):
        self.calls.append(list(command))
        if command[:2] == ["git", "diff"] and "--name-only" in command:
            return subprocess.CompletedProcess(command, 0, "\n".join(self.changed), "")
        if command[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(command, 0, self.diffs.get(command[-1], ""), "")
        if command[:3] == ["git", "ls-files", "--others"]:
            return subprocess.CompletedProcess(command, 0, "\n".join(self.untracked), "")
        if command[:3] == ["git", "ls-files", "--error-unmatch"]:
            return subprocess.CompletedProcess(command, 0 if command[-1] in self.tracked else 1, "", "")
        handler = self.tools.get(Path(command[0]).name)
        if handler is None:
            raise FileNotFoundError(command[0])
        return handler(command)


def _diff(start: int, *added: str) -> str:
    body = "\n".join(f"+{line}" for line in added)
    return f"diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -0,0 +{start},{len(added)} @@\n{body}\n"


def _which(*available):
    return lambda name: f"/usr/bin/{name}" if name in available else None


class SecretScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_fallback_finds_each_pattern_without_storing_values(self):
        lines = [AWS, GITHUB, SLACK, PEM, GENERIC]
        self._write("src/config.py", "\n".join(lines) + "\n")
        git = FakeGit(self.repo, untracked=["src/config.py"])
        scan = ss.scan_secrets(self.repo, "origin/main", ["src/config.py"], git, _which())
        self.assertEqual(scan.tool, "builtin")
        self.assertEqual(
            [(f["line"], f["rule"]) for f in scan.findings],
            [(1, "aws-access-token"), (2, "github-token"), (3, "slack-token"), (4, "private-key"), (5, "generic-api-key")],
        )
        dumped = json.dumps(scan.findings)
        for value in lines:
            self.assertNotIn(value.split('"')[-2] if '"' in value else value, dumped)

    def test_low_entropy_generic_assignment_is_ignored(self):
        self._write("a.py", 'password = "aaaaaaaaaaaaaaaaaaaaaaaa"\n')
        git = FakeGit(self.repo, untracked=["a.py"])
        self.assertEqual(ss.scan_secrets(self.repo, "origin/main", ["a.py"], git, _which()).findings, [])

    def test_only_added_lines_are_scanned(self):
        self._write("app.py", f"OLD = '{AWS}'\nnew = 1\n")
        git = FakeGit(self.repo, diffs={"app.py": _diff(2, "new = 1")}, tracked=["app.py"], changed=["app.py"])
        self.assertEqual(ss.scan_secrets(self.repo, "origin/main", [], git, _which()).findings, [])
        git.diffs["app.py"] = _diff(1, f"OLD = '{AWS}'")
        findings = ss.scan_secrets(self.repo, "origin/main", ["app.py"], git, _which()).findings
        self.assertEqual(findings, [{"file": "app.py", "line": 1, "rule": "aws-access-token"}])

    def test_tracked_file_without_diff_is_not_rescanned(self):
        self._write("app.py", f"KEY = '{AWS}'\n")
        git = FakeGit(self.repo, tracked=["app.py"])
        self.assertEqual(ss.added_content(self.repo, "origin/main", ["app.py"], git), {})

    def test_gitleaksignore_is_honoured(self):
        self._write("a.py", f"x = '{AWS}'\n")
        self._write(".gitleaksignore", "# known test key\na.py:aws-access-token:1\n")
        git = FakeGit(self.repo, untracked=["a.py"])
        self.assertEqual(ss.scan_secrets(self.repo, "origin/main", ["a.py"], git, _which()).findings, [])

    def test_gitleaks_path_scans_a_mirror_of_added_lines(self):
        self._write("pkg/app.py", f"one\nx = '{AWS}'\n")
        seen = {}

        def gitleaks(command):
            mirror = Path(command[2])
            seen["text"] = (mirror / "pkg" / "app.py").read_text()
            report = Path(command[command.index("--report-path") + 1])
            report.write_text(
                json.dumps([{"File": str(mirror / "pkg" / "app.py"), "StartLine": 2, "RuleID": "aws-access-token", "Secret": "REDACTED"}])
            )
            return subprocess.CompletedProcess(command, 1, "", "")

        git = FakeGit(self.repo, diffs={"pkg/app.py": _diff(2, f"x = '{AWS}'")}, tracked=["pkg/app.py"], tools={"gitleaks": gitleaks})
        scan = ss.scan_secrets(self.repo, "origin/main", ["pkg/app.py"], git, _which("gitleaks"))
        self.assertEqual(scan.tool, "gitleaks")
        self.assertEqual(scan.findings, [{"file": "pkg/app.py", "line": 2, "rule": "aws-access-token"}])
        self.assertEqual(seen["text"], f"\nx = '{AWS}'\n")
        self.assertIn("--redact", git.calls[-1])

    def test_gitleaks_failure_falls_back_to_builtin(self):
        self._write("a.py", f"x = '{AWS}'\n")
        git = FakeGit(
            self.repo, untracked=["a.py"], tools={"gitleaks": lambda c: subprocess.CompletedProcess(c, 2, "", "boom")}
        )
        scan = ss.scan_secrets(self.repo, "origin/main", ["a.py"], git, _which("gitleaks"))
        self.assertEqual((scan.tool, len(scan.findings)), ("builtin", 1))


class DependencyAuditTests(unittest.TestCase):
    repo = Path("/nonexistent")

    def _osv(self, severity):
        payload = {
            "results": [
                {
                    "source": {"path": "requirements.txt"},
                    "packages": [
                        {
                            "package": {"name": "django", "version": "3.0"},
                            "vulnerabilities": [{"id": "GHSA-1", "database_specific": {"severity": severity}}],
                        }
                    ],
                }
            ]
        }
        return lambda command: subprocess.CompletedProcess(command, 1, json.dumps(payload), "")

    def test_runs_only_when_a_manifest_changed(self):
        git = FakeGit(self.repo, tools={"osv-scanner": self._osv("HIGH")})
        audit = ss.audit_dependencies(self.repo, ["src/app.py"], "high", git, _which("osv-scanner"))
        self.assertEqual(audit.skipped_reason, "no_manifest_changes")
        self.assertEqual(git.calls, [])
        audit = ss.audit_dependencies(self.repo, ["svc/requirements-dev.txt"], "high", git, _which("osv-scanner"))
        self.assertEqual((audit.tool, len(audit.findings)), ("osv-scanner", 1))

    def test_severity_threshold(self):
        git = FakeGit(self.repo, tools={"osv-scanner": self._osv("MODERATE")})
        self.assertEqual(ss.audit_dependencies(self.repo, ["package.json"], "high", git, _which("osv-scanner")).findings, [])
        self.assertEqual(
            len(ss.audit_dependencies(self.repo, ["package.json"], "moderate", git, _which("osv-scanner")).findings), 1
        )

    def test_npm_audit_fallback_filters_by_severity(self):
        payload = {"vulnerabilities": {"lodash": {"severity": "critical"}, "minimist": {"severity": "low"}}}
        git = FakeGit(self.repo, tools={"npm": lambda c: subprocess.CompletedProcess(c, 1, json.dumps(payload), "")})
        audit = ss.audit_dependencies(self.repo, ["web/package-lock.json"], "high", git, _which("npm"))
        self.assertEqual(audit.tool, "npm-audit")
        self.assertEqual([f["package"] for f in audit.findings], ["lodash"])
        self.assertIn("--audit-level=high", git.calls[0])

    def test_pip_audit_findings_have_no_severity_and_always_count(self):
        payload = {"dependencies": [{"name": "flask", "version": "0.1", "vulns": [{"id": "PYSEC-1", "fix_versions": ["2.0"]}]}]}
        git = FakeGit(self.repo, tools={"pip-audit": lambda c: subprocess.CompletedProcess(c, 1, json.dumps(payload), "")})
        audit = ss.audit_dependencies(self.repo, ["requirements.txt"], "critical", git, _which("pip-audit"))
        self.assertEqual([f["id"] for f in audit.findings], ["PYSEC-1"])
        self.assertEqual(git.calls[0][-2:], ["-r", "requirements.txt"])

    def test_osv_scanner_uses_scan_source_and_no_packages_is_clean(self):
        git = FakeGit(self.repo, tools={"osv-scanner": lambda c: subprocess.CompletedProcess(c, 128, "", "")})
        audit = ss.audit_dependencies(self.repo, ["package.json"], "high", git, _which("osv-scanner"))
        self.assertEqual((audit.tool, audit.findings, audit.skipped_reason), ("osv-scanner", [], ""))
        self.assertEqual(git.calls[0][1:3], ["scan", "source"])

    def test_unavailable_scanner_is_a_warning_not_a_failure(self):
        audit = ss.audit_dependencies(self.repo, ["pyproject.toml"], "high", FakeGit(self.repo), _which())
        self.assertEqual((audit.findings, audit.skipped_reason), ([], ss.AUDIT_UNAVAILABLE))
        git = FakeGit(self.repo, tools={"osv-scanner": lambda c: subprocess.CompletedProcess(c, 127, "", "offline")})
        audit = ss.audit_dependencies(self.repo, ["pyproject.toml"], "high", git, _which("osv-scanner"))
        self.assertEqual(audit.skipped_reason, ss.AUDIT_UNAVAILABLE)


class RunSecurityScanTests(unittest.TestCase):
    def test_reasons_block_and_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "a.py").write_text(f"k = '{AWS}'\n")
            (repo / "requirements.txt").write_text("flask\n")
            git = FakeGit(repo, untracked=["a.py", "requirements.txt"])
            reasons, security, warnings = ss.run_security_scan(
                repo, config=ss.SecurityConfig(), base_ref="origin/main", runner=git, which=_which()
            )
        self.assertEqual(reasons, ["secrets_detected"])
        self.assertEqual(warnings, [ss.AUDIT_UNAVAILABLE, ss.STATIC_UNAVAILABLE])
        self.assertEqual(security["secret_scan"]["findings"], [{"file": "a.py", "line": 1, "rule": "aws-access-token"}])
        self.assertEqual(security["dependency_audit"]["skipped_reason"], ss.AUDIT_UNAVAILABLE)

    def test_config_from_program_defaults(self):
        config = ss.SecurityConfig.from_program_defaults(
            {"merge_readiness": {"policy": {"host_security": {"dependency_audit": False, "audit_severity": "CRITICAL"}}}}
        )
        self.assertEqual(
            (config.secret_scan, config.dependency_audit, config.audit_severity, config.static_analysis),
            (True, False, "critical", True),
        )
        self.assertEqual(ss.SecurityConfig.from_program_defaults(None), ss.SecurityConfig())


class StaticScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_semgrep_finding_on_an_added_line_omits_source(self):
        (self.repo / "a.py").write_text("eval(user)\n")
        git = FakeGit(
            self.repo,
            untracked=["a.py"],
            tools={
                "semgrep": lambda c: subprocess.CompletedProcess(
                    c,
                    1,
                    json.dumps(
                        {
                            "results": [
                                {
                                    "check_id": "python.lang.security.audit.eval",
                                    "path": "a.py",
                                    "start": {"line": 1},
                                    "extra": {"severity": "ERROR", "lines": "eval(user)"},
                                }
                            ]
                        }
                    ),
                    "",
                )
            },
        )
        scan = ss.scan_static(self.repo, "origin/main", ["a.py"], git, _which("semgrep"))
        self.assertEqual(scan.tool, "semgrep")
        self.assertEqual(
            scan.findings,
            [{"file": "a.py", "line": 1, "rule": "python.lang.security.audit.eval"}],
        )
        self.assertNotIn("eval(user)", json.dumps(scan.findings))

    def test_bandit_used_for_python_when_semgrep_is_absent(self):
        (self.repo / "a.py").write_text("eval(user)\n")
        (self.repo / "app.js").write_text("eval(user)\n")
        seen: list[list[str]] = []

        def bandit(command):
            seen.append(command)
            return subprocess.CompletedProcess(
                command,
                1,
                json.dumps(
                    {
                        "results": [
                            {
                                "filename": "a.py",
                                "line_number": 1,
                                "issue_severity": "HIGH",
                                "test_id": "B307",
                                "issue_text": "use of eval",
                            }
                        ]
                    }
                ),
                "",
            )

        git = FakeGit(self.repo, untracked=["a.py", "app.js"], tools={"bandit": bandit})
        scan = ss.scan_static(self.repo, "origin/main", ["a.py", "app.js"], git, _which("bandit"))
        self.assertEqual(scan.tool, "bandit")
        self.assertEqual(scan.findings, [{"file": "a.py", "line": 1, "rule": "B307"}])
        self.assertTrue(all(arg.endswith(".py") for arg in seen[0] if arg not in {"bandit", "-f", "json"} and not arg.startswith("/")))

    def test_missing_scanners_are_a_warning_only(self):
        (self.repo / "a.py").write_text("eval(user)\n")
        git = FakeGit(self.repo, untracked=["a.py"])
        reasons, security, warnings = ss.run_security_scan(
            self.repo,
            config=ss.SecurityConfig(secret_scan=False, dependency_audit=False),
            base_ref="origin/main",
            changed_files=["a.py"],
            runner=git,
            which=_which(),
        )
        self.assertEqual(reasons, [])
        self.assertEqual(warnings, [ss.STATIC_UNAVAILABLE])
        self.assertEqual(security["static_analysis"]["findings"], [])
        self.assertNotIn("eval(user)", json.dumps(security["static_analysis"]))


if __name__ == "__main__":
    unittest.main()
