"""React/Vite repos: Vitest without watch mode, the app build, Playwright suites, env files."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation import test_services as ts
from ts_harness.implementation.test_env import (
    _manifest_steps,
    is_web_app,
    node_static_checks,
    prepare_test_environment,
    TestEnvironment,
)
from ts_harness.implementation.verification import _run_command

VITE_APP = {
    "scripts": {"test": "vitest", "build": "tsc && vite build", "check-types": "tsc --noEmit", "lint": "eslint src"},
    "dependencies": {"react": "18", "react-dom": "18"},
    "devDependencies": {"vite": "5", "vitest": "2", "@playwright/test": "1.59.1"},
}


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _ok(command, **_kwargs):
    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def _app(root: Path) -> Path:
    repo = root / "repo"
    _write(repo / "package.json", json.dumps(VITE_APP))
    _write(repo / "yarn.lock")
    _write(repo / "playwright.config.ts", "export default {}")
    return repo


class FrontendEnvironmentTests(unittest.TestCase):
    def test_vitest_repos_run_with_ci_so_tests_do_not_watch(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _app(Path(tmp))
            env, _log = prepare_test_environment(Path(tmp), repo, runner=_ok)
            self.assertEqual(env.env["CI"], "true")
            self.assertEqual(env.env["PLAYWRIGHT_HTML_OPEN"], "never")

    def test_service_repos_without_vitest_keep_ci_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _write(repo / "package.json", json.dumps({"scripts": {"test": "jest"}, "devDependencies": {"jest": "30"}}))
            env, _log = prepare_test_environment(Path(tmp), repo, runner=_ok)
            self.assertNotIn("CI", env.env)

    def test_playwright_browser_install_follows_the_dependency_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _app(Path(tmp))
            steps = _manifest_steps(repo, TestEnvironment(python="python3"))
            commands = [command for _manifests, command in steps]
            self.assertEqual(commands[0][:2], ["yarn", "install"])
            self.assertEqual(commands[1][1:], ["install", "chromium"])
            self.assertTrue(commands[1][0].endswith("node_modules/.bin/playwright"))


class FrontendChecksTests(unittest.TestCase):
    def test_web_app_builds_after_type_check_and_lint(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _app(Path(tmp))
            _write(repo / ".eslintrc.cjs", "module.exports = {}")
            eslint = _write(repo / "node_modules" / ".bin" / "eslint", "#!/bin/sh\n")
            eslint.chmod(0o755)
            _write(repo / "src" / "app.tsx", "export {}")
            labels = [label for label, _command in node_static_checks(repo, ["src/app.tsx"])]
            self.assertEqual(labels, ["typecheck", "lint", "build"])

    def test_node_service_is_not_built(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "package.json", json.dumps({"scripts": {"build": "nest build"}, "dependencies": {"@nestjs/core": "11"}}))
            self.assertFalse(is_web_app(repo))
            self.assertNotIn("build", [label for label, _ in node_static_checks(repo, [])])

    def test_hanging_command_times_out_with_a_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _run_command(
                subprocess.run, ["sh", "-c", "echo started; sleep 5"], Path(tmp),
                env={"PATH": "/usr/bin:/bin", "TS_HARNESS_VERIFY_COMMAND_TIMEOUT": "1"},
            )
            self.assertEqual(result.returncode, 124)
            self.assertIn("watch mode", result.stderr)


class PlaywrightSuiteTests(unittest.TestCase):
    def test_detected_playwright_suite_is_advisory_without_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _app(Path(tmp))
            plan = ts.integration_plan(repo, services_running=False)
            self.assertEqual(plan.source, "playwright")
            self.assertFalse(plan.blocking)
            self.assertIn("--reporter=line", plan.command)

    def test_declared_env_file_is_written_without_any_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _app(Path(tmp))
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _write(repo / ".gitignore", ".env\n")
            _write(repo / ".env.example-e2e", "VITE_APP_API_URL=http://localhost:8080/api\nVITE_APP_ENABLE_API_MOCKING=false\n")
            _write(repo / ".ts-harness.yml", "env_file: .env.example-e2e\nenv: {VITE_APP_URL: 'http://localhost:3000'}\n")
            started = ts.start_test_services(repo)
            self.assertEqual(started.services, {})
            text = (repo / ".env").read_text()
            self.assertIn("VITE_APP_ENABLE_API_MOCKING=false", text)
            self.assertIn("VITE_APP_URL=http://localhost:3000", text)


if __name__ == "__main__":
    unittest.main()
