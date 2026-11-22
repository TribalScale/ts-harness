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


class EvidenceTextTests(unittest.TestCase):
    def test_terminal_colors_are_stripped(self):
        from ts_harness.implementation.verification import _check_record

        result = subprocess.CompletedProcess(["yarn", "test"], 0, stdout="\x1b[2m Tests \x1b[22m \x1b[1m\x1b[32m21 passed\x1b[39m\x1b[22m\x1b[1A\x1b[2K", stderr="")
        self.assertEqual(_check_record("unit", ["yarn", "test"], result)["output_tail"], "Tests  21 passed")


class StrayProcessTests(unittest.TestCase):
    def test_leftover_servers_are_stopped_and_the_job_keeps_its_own(self):
        import os

        me = os.getpid()
        table = {
            1: (0, "S", "/sbin/docker-init -- python3 /app/scripts/coding-worker.py"),
            7: (1, "S", "python3 /app/scripts/coding-worker.py"),
            me: (7, "R", "python3 verify"),
            300: (1, "S", "node yarn dev --port 3000"),
            301: (300, "S", "node vite"),
            400: (1, "S", "/usr/lib/postgresql/17/bin/postgres -D pgdata"),
            401: (400, "S", "postgres: checkpointer"),
            500: (1, "Z", "[chrome] <defunct>"),
        }
        killed = []
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            pidfile = ts.services_root(repo) / "pgdata" / "postmaster.pid"
            _write(pidfile, "400\n/data\n")
            try:
                stopped = ts.stop_stray_processes(repo, table=table, kill=lambda pid, sig: killed.append(pid))
            finally:
                import shutil

                shutil.rmtree(ts.services_root(repo), ignore_errors=True)
        self.assertEqual(sorted(killed), [300, 301])
        self.assertTrue(stopped[0].startswith("300: node yarn dev"))

    def test_outside_a_worker_nothing_is_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ts.stop_stray_processes(Path(tmp)), [])

    def test_launcher_runs_workers_with_an_init(self):
        from ts_harness.coding.launcher import LocalDockerConfig, LocalDockerLauncher

        command = LocalDockerLauncher(LocalDockerConfig(), environ={}).command({"id": "job-123456789"})
        self.assertIn("--init", command)


class MonorepoTests(unittest.TestCase):
    def _mono(self, root: Path) -> Path:
        repo = root / "repo"
        _write(repo / "package.json", json.dumps({"name": "mono", "scripts": {"test": "turbo run test"}, "packageManager": "pnpm@10.18.3"}))
        _write(repo / "pnpm-workspace.yaml", "packages:\n  - 'apps/*'\n  - 'packages/*'\n")
        _write(repo / "pnpm-lock.yaml")
        _write(repo / "apps" / "web" / "package.json", json.dumps({"name": "web", "scripts": {"build": "next build", "check-types": "tsc --noEmit"}, "dependencies": {"next": "15", "react-dom": "19"}, "devDependencies": {"@playwright/test": "1.59.1"}}))
        _write(repo / "apps" / "web" / "eslint.config.js", "export default []")
        _write(repo / "apps" / "web" / "playwright.config.ts", "export default {}")
        _write(repo / "apps" / "web" / "app" / "page.tsx", "export default 1")
        _write(repo / "apps" / "web" / "e2e" / "home.spec.ts", "test")
        _write(repo / "packages" / "logger" / "package.json", json.dumps({"name": "@repo/logger", "scripts": {"build": "tsup"}}))
        _write(repo / "packages" / "logger" / "tsconfig.json", "{}")
        _write(repo / "packages" / "logger" / "eslint.config.js", "export default []")
        _write(repo / "packages" / "logger" / "src" / "index.ts", "export {}")
        for where in ("apps/web", "packages/logger", "."):
            for tool in ("eslint", "tsc"):
                path = _write(repo / where / "node_modules" / ".bin" / tool, "#!/bin/sh\n")
                path.chmod(0o755)
        _write(repo / "node_modules" / ".bin" / "turbo", "#!/bin/sh\n").chmod(0o755)
        _write(repo / "apps" / "web" / "node_modules" / ".bin" / "playwright", "#!/bin/sh\n").chmod(0o755)
        return repo

    def test_checks_run_per_changed_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._mono(Path(tmp))
            checks = node_static_checks(repo, ["apps/web/app/page.tsx", "packages/logger/src/index.ts"])
            commands = {label + ":" + " ".join(cmd[3:] if cmd[0] == "env" else cmd).replace(str(repo), "R") for label, cmd in checks}
            self.assertIn("typecheck:pnpm --filter web run check-types", commands)
            self.assertIn("typecheck:R/packages/logger/node_modules/.bin/tsc --noEmit -p tsconfig.json", commands)
            self.assertIn("lint:R/apps/web/node_modules/.bin/eslint app/page.tsx", commands)
            self.assertIn("lint:R/packages/logger/node_modules/.bin/eslint src/index.ts", commands)
            self.assertIn("build:R/node_modules/.bin/turbo run build --filter=web --output-logs=errors-only", commands)
            lint = next(cmd for label, cmd in checks if label == "lint")
            self.assertEqual(lint[:2], ["env", "-C"])

    def test_package_playwright_suite_and_screenshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._mono(Path(tmp))
            changed = ["apps/web/app/page.tsx", "apps/web/e2e/home.spec.ts"]
            plan = ts.integration_plan(repo, services_running=False, changed_files=changed)
            self.assertIn(f"cd {repo / 'apps' / 'web'} &&", plan.command)
            self.assertEqual(ts.changed_playwright_specs(repo, changed), ["apps/web/e2e/home.spec.ts"])
            steps = [command for _m, command in _manifest_steps(repo, TestEnvironment(python="python3"))]
            self.assertTrue(steps[-1][0].endswith("apps/web/node_modules/.bin/playwright"))


class OutOfMemoryTests(unittest.TestCase):
    def test_kernel_kill_is_named(self):
        from ts_harness.implementation.backends.opencode import opencode_exit_error

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "memory.events", "low 0\nhigh 0\nmax 12\noom 1\noom_kill 1\n")
            _write(root / "memory.max", str(2 * 1024**3))
            killed = subprocess.CompletedProcess(["opencode"], -9, "", "")
            self.assertTrue(opencode_exit_error(killed, cgroup_root=root).startswith("out_of_memory: the coding worker hit its memory limit (2048 MB)"))
            _write(root / "memory.events", "oom_kill 0\n")
            self.assertEqual(opencode_exit_error(killed, cgroup_root=root), "opencode exit -9")

    def test_job_spec_memory_wins_at_claim(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location("coding_jobs_routes", Path(__file__).resolve().parents[2] / "scripts" / "memory_routes" / "coding_jobs.py")
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except ImportError as exc:  # pragma: no cover - routes need the memory-api deps
            self.skipTest(str(exc))
        self.assertEqual(module._spec_sizing({"spec": json.dumps({"repo_url": "x", "worker_memory_mb": 4096})}), {"worker_memory_mb": 4096})
        self.assertEqual(module._spec_sizing({"spec": {"repo_url": "x"}}), {})


class FormatCheckTests(unittest.TestCase):
    def _repo(self, root: Path, workflow: str, scripts: dict) -> Path:
        repo = root / "repo"
        _write(repo / "package.json", json.dumps({"scripts": {"test": "vitest", **scripts}}))
        _write(repo / ".github" / "workflows" / "ci.yml", workflow)
        _write(repo / "src" / "a.tsx", "export {}")
        _write(repo / "node_modules" / ".bin" / "prettier", "#!/bin/sh\n").chmod(0o755)
        return repo

    def test_runs_when_ci_calls_a_prettier_check_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp), "steps:\n  - run: yarn test:other\n", {"test:other": "yarn prettier --list-different"})
            checks = dict(node_static_checks(repo, ["src/a.tsx"]))
            self.assertEqual(checks["format"][1:], ["--check", "--no-error-on-unmatched-pattern", "src/a.tsx"])

    def test_not_enforced_without_ci_formatting(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp), "steps:\n  - run: yarn lint\n", {"format": "prettier --write ."})
            self.assertNotIn("format", dict(node_static_checks(repo, ["src/a.tsx"])))


class RelatedTestsTests(unittest.TestCase):
    def _repo(self, root: Path, tests: int, runner: str = "vitest") -> Path:
        repo = root / "repo"
        _write(repo / "package.json", json.dumps({"scripts": {"test": "yarn test:app", "test:app": runner}, "devDependencies": {runner: "1"}}))
        _write(repo / "node_modules" / ".bin" / runner, "#!/bin/sh\n").chmod(0o755)
        _write(repo / "src" / "stats.tsx", "export {}")
        for i in range(tests):
            _write(repo / "src" / "t" / f"case{i}.test.tsx", "")
        return repo

    def test_large_vitest_repo_runs_related_tests(self):
        from ts_harness.implementation.verification import related_tests_command

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp), 130)
            command = related_tests_command(repo, ["src/stats.tsx", "README.md"])
            # Coverage off: thresholds measure the full suite, which CI runs.
            self.assertEqual(command[1:], ["related", "--run", "--coverage.enabled=false", "src/stats.tsx"])

    def test_large_jest_repo_and_small_repos(self):
        from ts_harness.implementation.verification import related_tests_command

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp), 130, runner="jest")
            self.assertEqual(related_tests_command(repo, ["src/stats.tsx"])[1:], ["--coverage=false", "--findRelatedTests", "src/stats.tsx", "--passWithNoTests"])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(related_tests_command(self._repo(Path(tmp), 10), ["src/stats.tsx"]))


class QuickRecheckTests(unittest.TestCase):
    def test_a_small_repo_gets_related_tests_when_forced(self):
        from ts_harness.implementation.verification import related_tests_command

        with tempfile.TemporaryDirectory() as tmp:
            repo = RelatedTestsTests._repo(self, Path(tmp), 10)
            self.assertIsNone(related_tests_command(repo, ["src/stats.tsx"]))
            self.assertEqual(related_tests_command(repo, ["src/stats.tsx"], force=True)[1:3], ["related", "--run"])

    def test_quick_runs_related_tests_and_records_how_long_each_check_took(self):
        from ts_harness.implementation.verification import _touches_bdd, verify_implementation

        seen: list[list[str]] = []

        def runner(command, **kwargs):
            seen.append(list(command))
            return _ok(command, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            repo = RelatedTestsTests._repo(self, Path(tmp), 5)
            result = verify_implementation(repo, work_order_body="feat: given a user when x then y", work_order_id="WO-1",
                                           changed_files=["src/stats.tsx"], runner=runner, quick=True)
        unit = next(check for check in result.checks if check["check"] == "unit")
        self.assertIn("related", unit["command"])
        self.assertIn("seconds", unit)
        self.assertTrue(any("quick re-check" in w for w in result.warnings))
        self.assertTrue(_touches_bdd(["features/x.feature"]))
        self.assertTrue(_touches_bdd(["test/steps/orders.steps.ts"]))
        self.assertFalse(_touches_bdd(["src/stats.tsx"]))


class VerificationOutOfMemoryTests(unittest.TestCase):
    def test_a_kill_during_verification_is_out_of_memory(self):
        from unittest.mock import patch

        from ts_harness.implementation.verification import verify_implementation

        counts = iter([(3, "2048 MB"), (4, "2048 MB")])
        with tempfile.TemporaryDirectory() as tmp:
            repo = _app(Path(tmp))
            with patch("ts_harness.implementation.cgroup.oom_kills", side_effect=lambda *a: next(counts)):
                result = verify_implementation(repo, work_order_body="chore: x", work_order_id="WO-1", changed_files=["src/x.ts"], runner=_ok)
            self.assertEqual(result.reasons[0], "out_of_memory")
            self.assertTrue(any("2048 MB" in w for w in result.warnings))


class RefreshedTokenTests(unittest.TestCase):
    def test_client_from_env_follows_the_refreshed_token(self):
        from unittest.mock import patch

        from ts_harness.integrations.github_client import github_for

        seen = []

        def transport(method, url, headers, data):
            seen.append(headers["Authorization"])
            return 200, b"{}", {}

        with patch.dict("os.environ", {"GITHUB_TOKEN": "ghs_first"}), patch("ts_harness.integrations.github_app.app_for", return_value=None):
            client = github_for("https://github.com/acme/web", transport=transport)
            client.get_pull("acme", "web", 1)
            import os

            os.environ["GITHUB_TOKEN"] = "ghs_second"  # the worker's refresher swapped it
            client.get_pull("acme", "web", 1)
        self.assertEqual(seen, ["Bearer ghs_first", "Bearer ghs_second"])
