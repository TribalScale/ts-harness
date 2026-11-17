"""Stack detection and per-work-order test environment."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ts_harness.implementation.test_env import (
    bdd_test_command,
    detect_stack,
    install_declared_dependencies,
    prepare_test_environment,
    unit_test_command,
)


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class StackDetectionTests(unittest.TestCase):
    def detect(self, files: dict[str, str]) -> tuple[str | None, list[str] | None]:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for name, text in files.items():
                _write(repo / name, text)
            return detect_stack(repo), unit_test_command(repo)

    def test_manifests_select_their_runner(self):
        cases = {
            "go": ({"go.mod": "module x\n"}, ["go", "test", "./..."]),
            "cargo": ({"Cargo.toml": "[package]\n"}, ["cargo", "test"]),
            "maven": ({"pom.xml": "<project/>"}, ["mvn", "-B", "test"]),
            "gradle": ({"build.gradle.kts": ""}, ["gradle", "test"]),
            "python": ({"conftest.py": ""}, ["python3", "-m", "pytest"]),
            "make": ({"Makefile": "test:\n\techo ok\n"}, ["make", "test"]),
        }
        for stack, (files, command) in cases.items():
            with self.subTest(stack=stack):
                self.assertEqual(self.detect(files), (stack, command))

    def test_wrappers_are_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "pom.xml", "<project/>")
            os.chmod(_write(repo / "mvnw", "#!/bin/sh\n"), 0o755)
            self.assertEqual(unit_test_command(repo), ["./mvnw", "-B", "test"])
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "gradlew", "#!/bin/sh\n")
            self.assertEqual(unit_test_command(repo), ["sh", "./gradlew", "test"])

    def test_node_wins_over_other_manifests(self):
        stack, command = self.detect({"package.json": '{"scripts": {"test": "vitest"}}', "go.mod": "module x\n"})
        self.assertEqual((stack, command), ("node", ["npm", "test"]))

    def test_package_json_without_test_script_falls_through(self):
        self.assertEqual(self.detect({"package.json": "{}", "go.mod": "module x\n"})[0], "go")

    def test_python_signals(self):
        for files in (
            {"pyproject.toml": "[tool.pytest.ini_options]\n"},
            {"setup.cfg": "[tool:pytest]\n"},
            {"tox.ini": "[pytest]\n"},
            {"tests/test_a.py": ""},
        ):
            with self.subTest(files=files):
                self.assertEqual(self.detect(files)[0], "python")

    def test_empty_or_bare_dirs_have_no_stack(self):
        self.assertEqual(self.detect({}), (None, None))
        self.assertEqual(self.detect({"tests/README.md": "", "features/a.feature": ""}), (None, None))

    def test_bdd_command_uses_given_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "features" / "environment.py")
            self.assertEqual(bdd_test_command(repo, python="/v/bin/python"), ["/v/bin/python", "-m", "behave"])


class FakeRunner:
    def __init__(self, fail: set[str] | None = None):
        self.calls: list[tuple[list[str], dict]] = []
        self.fail = fail or set()

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        code = 1 if any(token in command for token in self.fail) else 0
        return subprocess.CompletedProcess(command, code, stdout="", stderr="boom" if code else "")

    def commands(self) -> list[list[str]]:
        return [c for c, _ in self.calls]


class PrepareTestEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "wo-1"
        self.repo = self.workspace / "repo"
        self.repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_venv_and_installs_runners_offline_once(self):
        runner = FakeRunner()
        with tempfile.TemporaryDirectory() as wheels, patch.dict(
            os.environ, {"TS_HARNESS_TEST_RUNNER_WHEELS": wheels, "TS_HARNESS_TEST_RUNNER_REQUIREMENTS": "/nope"}
        ):
            test_env, log = prepare_test_environment(self.workspace, self.repo, runner=runner)
            venv = self.workspace / ".ts-harness" / "venv"
            python = str(venv / "bin" / "python")
            self.assertEqual(
                runner.commands(),
                [
                    ["python3", "-m", "venv", str(venv)],
                    [python, "-m", "pip", "install", "-q", "--no-index", "--find-links", wheels, "pytest", "behave"],
                ],
            )
            self.assertIn("venv ready", log)
            prepare_test_environment(self.workspace, self.repo, runner=runner)
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(test_env.python, python)

    def test_env_puts_venv_first_and_shares_caches(self):
        test_env, _ = prepare_test_environment(self.workspace, self.repo, runner=FakeRunner())
        cache = Path(self.tmp.name) / ".cache"
        self.assertTrue(test_env.env["PATH"].startswith(str(self.workspace / ".ts-harness" / "venv" / "bin")))
        for key in ("GOPATH", "GOCACHE", "GOMODCACHE", "npm_config_cache", "CARGO_HOME", "GRADLE_USER_HOME"):
            self.assertTrue(test_env.env[key].startswith(str(cache)), key)

    def test_runner_install_failure_is_logged_and_retried_next_time(self):
        runner = FakeRunner(fail={"install"})
        _, log = prepare_test_environment(self.workspace, self.repo, runner=runner)
        self.assertIn("test runner install failed", log)
        prepare_test_environment(self.workspace, self.repo, runner=runner)
        self.assertEqual(sum("install" in c for c in runner.commands()), 2)

    def test_dependency_output_is_excluded_from_commits(self):
        _write(self.repo / "Cargo.toml", "[package]\n")
        prepare_test_environment(self.workspace, self.repo, runner=FakeRunner())
        prepare_test_environment(self.workspace, self.repo, runner=FakeRunner())
        exclude = (self.repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
        self.assertEqual(exclude.count("node_modules/"), 1)
        self.assertIn("target/", exclude)
        _write(self.repo / "node_modules" / "x" / "index.js")
        _write(self.repo / "target" / "debug" / "app")
        status = subprocess.run(
            ["git", "-C", str(self.repo), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        self.assertEqual(status.strip(), "?? Cargo.toml")


class InstallDeclaredDependenciesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "wo-1"
        self.repo = self.workspace / "repo"
        self.repo.mkdir(parents=True)
        self.test_env, _ = prepare_test_environment(self.workspace, self.repo, runner=FakeRunner())
        os.chmod(_write(Path(self.test_env.python), "#!/bin/sh\n"), 0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def test_python_manifests_install_into_venv_and_skip_when_unchanged(self):
        _write(self.repo / "requirements.txt", "flask\n")
        _write(self.repo / "pyproject.toml", "[project]\nname='app'\n")
        runner = FakeRunner()
        install_declared_dependencies(self.test_env, self.repo, runner=runner)
        python = self.test_env.python
        self.assertEqual(
            runner.commands(),
            [
                [python, "-m", "pip", "install", "-q", "-r", "requirements.txt"],
                [python, "-m", "pip", "install", "-q", "-e", "."],
            ],
        )
        self.assertTrue(all(kw["env"]["VIRTUAL_ENV"] == self.test_env.env["VIRTUAL_ENV"] for _, kw in runner.calls))
        install_declared_dependencies(self.test_env, self.repo, runner=runner)
        self.assertEqual(len(runner.calls), 2)
        _write(self.repo / "requirements.txt", "flask\nrequests\n")
        install_declared_dependencies(self.test_env, self.repo, runner=runner)
        self.assertEqual(runner.commands()[-1], [python, "-m", "pip", "install", "-q", "-r", "requirements.txt"])
        self.assertEqual(len(runner.calls), 3)

    def test_failed_install_is_retried(self):
        _write(self.repo / "requirements.txt", "flask\n")
        runner = FakeRunner(fail={"-r"})
        log = install_declared_dependencies(self.test_env, self.repo, runner=runner)
        self.assertIn("failed (exit 1)", log)
        install_declared_dependencies(self.test_env, self.repo, runner=runner)
        self.assertEqual(len(runner.calls), 2)

    def test_node_and_go_manifests(self):
        _write(self.repo / "package.json", "{}")
        _write(self.repo / "package-lock.json", "{}")
        _write(self.repo / "go.mod", "module x\n")
        runner = FakeRunner()
        with patch("ts_harness.implementation.test_env.missing_tools", return_value=[]):
            install_declared_dependencies(self.test_env, self.repo, runner=runner)
        self.assertEqual(runner.commands(), [["npm", "ci", "--no-audit", "--no-fund"], ["go", "mod", "download"]])

    def test_missing_tool_is_logged_not_run(self):
        _write(self.repo / "package.json", "{}")
        runner = FakeRunner()
        with patch("ts_harness.implementation.test_env.missing_tools", return_value=["npm"]):
            log = install_declared_dependencies(self.test_env, self.repo, runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertIn("missing npm", log)


if __name__ == "__main__":
    unittest.main()
