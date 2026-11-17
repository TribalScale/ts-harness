"""Tests for coding-backend verification config and helpers."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from ts_harness.config.schema import OpenCodeBackendConfig, parse_runtime_config
from ts_harness.implementation.backends.opencode import OpenCodeBackend
from ts_harness.implementation.test_env import TestEnvironment
from ts_harness.implementation.verification import (
    build_verification_fix_prompt,
    detect_unit_test_command,
    has_gwt_acceptance,
    looks_behavioral,
    verify_implementation,
)


class VerificationFixPassesConfigTests(unittest.TestCase):
    def test_opencode_verification_fix_passes_default(self):
        self.assertEqual(OpenCodeBackendConfig().verification_fix_passes, 2)

    def test_parse_verification_fix_passes(self):
        cfg = parse_runtime_config(
            {
                "gateway": {"token": "x"},
                "agents": {
                    "list": [
                        {
                            "id": "orchestrator",
                            "default": True,
                            "workspace": "/w",
                            "model": "openai/gpt-4o-mini",
                        }
                    ]
                },
                "implementation": {
                    "backends": {"opencode": {"verification_fix_passes": 3}},
                },
            }
        )
        self.assertEqual(cfg.implementation.backends.opencode.verification_fix_passes, 3)

    def test_parse_verification_fix_passes_zero(self):
        cfg = parse_runtime_config(
            {
                "gateway": {"token": "x"},
                "agents": {
                    "list": [
                        {
                            "id": "orchestrator",
                            "default": True,
                            "workspace": "/w",
                            "model": "openai/gpt-4o-mini",
                        }
                    ]
                },
                "implementation": {
                    "backends": {"opencode": {"verification_fix_passes": 0}},
                },
            }
        )
        self.assertEqual(cfg.implementation.backends.opencode.verification_fix_passes, 0)


class OpenCodeBackendVerificationTests(unittest.TestCase):
    def test_verification_fix_passes_default_on_backend(self):
        backend = OpenCodeBackend()
        self.assertEqual(backend.verification_fix_passes, 2)

    def test_verification_fix_passes_clamped_to_non_negative(self):
        backend = OpenCodeBackend(verification_fix_passes=-1)
        self.assertEqual(backend.verification_fix_passes, 0)


class VerificationHelperTests(unittest.TestCase):
    def test_detect_unit_test_command_uses_package_json_test_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "package.json").write_text(
                '{"scripts": {"test": "jest"}}',
                encoding="utf-8",
            )

            self.assertEqual(detect_unit_test_command(repo_dir), ["npm", "test"])

    def test_detect_unit_test_command_uses_pytest_ini(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

            self.assertEqual(detect_unit_test_command(repo_dir), ["python3", "-m", "pytest"])

    def test_detect_bdd_command_prefers_package_json_test_bdd_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "package.json").write_text(
                '{"scripts": {"test:bdd": "cucumber-js"}}',
                encoding="utf-8",
            )

            from ts_harness.implementation.verification import detect_bdd_command

            self.assertEqual(detect_bdd_command(repo_dir), ["npm", "run", "test:bdd"])

    def test_repo_has_feature_files_detects_nested_feature(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            nested = repo_dir / "specs" / "checkout"
            nested.mkdir(parents=True)
            (nested / "order.feature").write_text("Feature: orders\n", encoding="utf-8")

            from ts_harness.implementation.verification import repo_has_feature_files

            self.assertTrue(repo_has_feature_files(repo_dir))

    def test_collect_docs_text_reads_refinery_foundry_and_planner_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            for name in ("refinery", "foundry", "planner"):
                docs_dir = repo_dir / "docs" / name
                docs_dir.mkdir(parents=True)
                (docs_dir / f"{name}.md").write_text(f"{name} docs", encoding="utf-8")
            (repo_dir / "docs" / "other").mkdir()
            (repo_dir / "docs" / "other" / "skip.md").write_text("skip", encoding="utf-8")

            from ts_harness.implementation.verification import collect_docs_text

            docs_text = collect_docs_text(repo_dir)

            self.assertIn("refinery docs", docs_text)
            self.assertIn("foundry docs", docs_text)
            self.assertIn("planner docs", docs_text)
            self.assertNotIn("skip", docs_text)

    def test_has_gwt_acceptance_true_for_given_when_then(self):
        text = """
        Acceptance Criteria:
        Given a signed-in user
        When they submit a valid request
        Then the request is saved
        """

        self.assertTrue(has_gwt_acceptance(text))

    def test_has_gwt_acceptance_false_for_chore_text(self):
        self.assertFalse(has_gwt_acceptance("chore: bump deps and update lockfile"))

    def test_looks_behavioral_false_for_chore_without_acceptance(self):
        self.assertFalse(looks_behavioral("chore: bump deps / refactor rename only"))

    def test_behavioral_work_without_runner_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = verify_implementation(
                Path(tmp),
                work_order_body="Add login validation for invalid credentials",
            )

            self.assertFalse(result.ok)
            self.assertIn("no_test_runner", result.reasons)
            self.assertTrue(result.skipped_unit)

    def test_gwt_work_without_features_or_runner_fails_bdd(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = verify_implementation(
                Path(tmp),
                work_order_body="""
                Given a customer has an open cart
                When they check out
                Then an order is created
                """,
            )

            self.assertFalse(result.ok)
            self.assertIn("no_bdd_runner_or_features", result.reasons)
            self.assertTrue(result.bdd_required)
            self.assertTrue(result.skipped_bdd)

    def test_detected_unit_command_exit_zero_succeeds(self):
        calls: list[tuple[list[str], Path]] = []

        def runner(command, **kwargs):
            calls.append((command, kwargs["cwd"]))
            return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")

            result = verify_implementation(
                repo_dir,
                work_order_body="Add login validation for invalid credentials",
                runner=runner,
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.unit_command, ["python3", "-m", "pytest"])
            self.assertEqual(calls, [(["python3", "-m", "pytest"], repo_dir)])
            self.assertIn("ok", result.log_tail)

    def test_build_verification_fix_prompt_includes_reasons_log_and_do_not_commit(self):
        prompt = build_verification_fix_prompt(
            "WO-123",
            verify_implementation(
                Path("/tmp/no-such-repo"),
                work_order_body="Add login validation for invalid credentials",
            ),
        )

        self.assertIn("Host verification failed for WO-123", prompt)
        self.assertIn("Reasons: no_test_runner", prompt)
        self.assertIn("Do not commit", prompt)
        self.assertIn("Verification log:", prompt)

    def test_build_verification_fix_prompt_includes_unit_setup_guidance(self):
        prompt = build_verification_fix_prompt(
            "WO-123",
            verify_implementation(
                Path("/tmp/no-such-repo"),
                work_order_body="Add login validation for invalid credentials",
            ),
        )

        self.assertIn("standard test runner for this repo's language", prompt)
        self.assertIn("cover this work order's behavior", prompt)
        self.assertNotIn("pytest, npm test, or make test", prompt)

    def test_build_verification_fix_prompt_includes_bdd_setup_guidance(self):
        prompt = build_verification_fix_prompt(
            "WO-456",
            verify_implementation(
                Path("/tmp/no-such-repo"),
                work_order_body="""
                Given a customer has an open cart
                When they check out
                Then an order is created
                """,
            ),
        )

        self.assertIn("Gherkin feature file", prompt)
        self.assertIn("WO-456", prompt)
        self.assertIn("acceptance criteria", prompt)


class TestRunnerGuardTests(unittest.TestCase):
    GWT = "Given a cart\nWhen they check out\nThen an order is created"

    def _recording_runner(self, calls):
        def runner(command, **kwargs):
            calls.append((list(command), kwargs.get("env")))
            return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

        return runner

    def test_repo_root_pytest_module_is_rejected_without_running(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            (repo_dir / "pytest.py").write_text("print('all passed')\n", encoding="utf-8")
            result = verify_implementation(
                repo_dir, work_order_body="Add login validation", runner=self._recording_runner(calls)
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reasons, ["shadowed_test_runner:pytest.py"])
        self.assertEqual(calls, [])
        prompt = build_verification_fix_prompt("WO-1", result)
        self.assertIn("Delete the stand-in test framework modules (pytest.py)", prompt)

    def test_behave_package_is_rejected_for_bdd(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "features" / "steps").mkdir(parents=True)
            (repo_dir / "features" / "steps" / "cart.py").write_text("", encoding="utf-8")
            (repo_dir / "features" / "cart.feature").write_text("Feature: cart\n", encoding="utf-8")
            (repo_dir / "behave").mkdir()
            (repo_dir / "package.json").write_text('{"scripts": {"test": "jest"}}', encoding="utf-8")
            result = verify_implementation(repo_dir, work_order_body=self.GWT, runner=self._recording_runner(calls))
        self.assertIn("shadowed_test_runner:behave/", result.reasons)
        self.assertEqual([c[0] for c in calls], [["npm", "test"]])

    def test_behave_stand_in_does_not_block_real_pytest(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            (repo_dir / "features" / "steps").mkdir(parents=True)
            (repo_dir / "features" / "steps" / "cart.py").write_text("", encoding="utf-8")
            (repo_dir / "features" / "cart.feature").write_text("Feature: cart\n", encoding="utf-8")
            (repo_dir / "behave").mkdir()
            result = verify_implementation(repo_dir, work_order_body=self.GWT, runner=self._recording_runner(calls))
        self.assertEqual(result.reasons, ["shadowed_test_runner:behave/"])
        self.assertEqual([c[0] for c in calls], [["python3", "-m", "pytest"]])

    def test_missing_toolchain_is_reported_and_not_run(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
            test_env = TestEnvironment(python="python3", env={"PATH": tmp})
            result = verify_implementation(
                repo_dir,
                work_order_body="Add login validation",
                runner=self._recording_runner(calls),
                test_env=test_env,
            )
        self.assertEqual(result.reasons, ["missing_toolchain:go"])
        self.assertEqual(calls, [])

    def test_go_features_run_inside_unit_command(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "go.mod").write_text("module example.com/app\n", encoding="utf-8")
            (repo_dir / "features").mkdir()
            (repo_dir / "features" / "cart.feature").write_text("Feature: cart\n", encoding="utf-8")
            result = verify_implementation(repo_dir, work_order_body=self.GWT, runner=self._recording_runner(calls))
        self.assertTrue(result.ok, result.reasons)
        self.assertEqual(result.bdd_command, ["go", "test", "./..."])
        self.assertEqual([c[0] for c in calls], [["go", "test", "./..."]])

    def test_test_env_supplies_interpreter_and_env(self):
        calls: list = []
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            venv_bin = Path(tmp) / "venv" / "bin"
            venv_bin.mkdir(parents=True)
            python = venv_bin / "python"
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            python.chmod(0o755)
            test_env = TestEnvironment(python=str(python), env={"PATH": str(venv_bin), "VIRTUAL_ENV": "v"})
            result = verify_implementation(
                repo_dir,
                work_order_body="Add login validation",
                runner=self._recording_runner(calls),
                test_env=test_env,
            )
        self.assertTrue(result.ok, result.reasons)
        command, env = calls[0]
        self.assertEqual(command, [str(python), "-m", "pytest"])
        self.assertEqual(env["VIRTUAL_ENV"], "v")
        self.assertEqual(env["PATH"], str(venv_bin))


class SecurityVerificationTests(unittest.TestCase):
    SECRET = "AKIA" + "ZYXWVUTSRQPONMLK"

    def _runner(self, calls):
        def runner(command, **kwargs):
            calls.append(list(command))
            if command[:3] == ["git", "ls-files", "--others"]:
                return subprocess.CompletedProcess(command, 0, stdout="app/settings.py\n", stderr="")
            if command[:3] == ["git", "ls-files", "--error-unmatch"]:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
            if command[:2] == ["git", "diff"]:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

        return runner

    def test_secret_fails_verify_and_prompt_shows_location_not_value(self):
        from ts_harness.implementation.security_scan import SecurityConfig

        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            (repo_dir / "app").mkdir()
            (repo_dir / "app" / "settings.py").write_text(f"KEY = '{self.SECRET}'\n", encoding="utf-8")
            result = verify_implementation(
                repo_dir,
                work_order_body="Add login validation for invalid credentials",
                runner=self._runner(calls),
                security=SecurityConfig(),
                base_ref="origin/main",
                which=lambda name: None,
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.reasons, ["secrets_detected"])
        self.assertEqual(result.security["secret_scan"]["findings"], [{"file": "app/settings.py", "line": 1, "rule": "aws-access-token"}])
        prompt = build_verification_fix_prompt("WO-9", result)
        self.assertIn("app/settings.py:1", prompt)
        self.assertIn("environment variable", prompt)
        self.assertNotIn(self.SECRET, prompt)
        self.assertNotIn(self.SECRET, result.log_tail)
        self.assertIn(["git", "diff", "--name-only", "--diff-filter=d", "origin/main"], calls)

    def test_without_security_config_no_scan_runs(self):
        calls: list[list[str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            result = verify_implementation(repo_dir, work_order_body="Add login validation", runner=self._runner(calls))
        self.assertTrue(result.ok)
        self.assertEqual((result.security, result.warnings), ({}, []))
        self.assertEqual(calls, [["python3", "-m", "pytest"]])

    def test_vulnerable_dependency_prompt_and_unavailable_audit_warning(self):
        from ts_harness.implementation.verification import VerifyResult

        verify = VerifyResult(
            ok=False,
            reasons=["vulnerable_dependency"],
            summary="",
            log_tail="",
            security={"dependency_audit": {"findings": [{"package": "lodash", "id": "GHSA-x"}]}},
        )
        prompt = build_verification_fix_prompt("WO-9", verify)
        self.assertIn("lodash GHSA-x", prompt)
        self.assertIn("patched version", prompt)
        self.assertIn("only if the repo already has one", prompt)
        self.assertIn("do not add, delete or rewrite other dependency files", prompt)

    def test_backend_defaults_to_secret_scan_and_audit(self):
        backend = OpenCodeBackend()
        self.assertTrue(backend.security.secret_scan)
        self.assertTrue(backend.security.dependency_audit)
        self.assertEqual(backend.security.audit_severity, "high")

    def test_security_markdown_never_lists_values(self):
        from ts_harness.implementation.backends.base import security_markdown

        text = security_markdown(
            {
                "secret_scan": {"tool": "gitleaks", "findings": []},
                "dependency_audit": {"tool": "", "findings": [], "skipped_reason": "no_manifest_changes"},
            }
        )
        self.assertIn("## Security scan", text)
        self.assertIn("Secret scan (gitleaks): no findings", text)
        self.assertIn("Dependency audit: no manifest changes", text)
        self.assertEqual(security_markdown({}), "")


if __name__ == "__main__":
    unittest.main()
