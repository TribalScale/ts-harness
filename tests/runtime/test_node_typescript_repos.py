"""Node/TypeScript repos: package managers, type check and lint, and where BDD applies."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation.test_env import (
    node_install_command,
    node_package_manager,
    node_static_checks,
    prepare_test_environment,
    repo_runs_bdd,
    unit_test_command,
)
from ts_harness.implementation.verification import (
    build_verification_fix_prompt,
    validate_work_order_changeset,
    verify_implementation,
)

GWT_WORK_ORDER = """# WO-101 Reject malformed queries

## E2E Acceptance Tests
- Given an admin token, When GET /api/v1/users?filters=not-json, Then the response is 400.
"""


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _package(repo: Path, **fields) -> None:
    _write(repo / "package.json", json.dumps({"name": "app", **fields}))


def _bin(repo: Path, name: str) -> Path:
    path = _write(repo / "node_modules" / ".bin" / name, "#!/bin/sh\n")
    path.chmod(0o755)
    return path


class Runner:
    """Records commands; fails the ones whose label is in ``fail``."""

    def __init__(self, fail: dict[str, str] | None = None):
        self.fail = fail or {}
        self.commands: list[list[str]] = []

    def __call__(self, command, **_kwargs):
        self.commands.append(list(command))
        joined = " ".join(command)
        for needle, output in self.fail.items():
            if needle in joined:
                return subprocess.CompletedProcess(command, 1, stdout=output, stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")


class PackageManagerTests(unittest.TestCase):
    def test_lockfile_or_package_manager_field_picks_the_manager(self):
        cases = [
            ({}, [], "npm"),
            ({}, ["pnpm-lock.yaml"], "pnpm"),
            ({}, ["yarn.lock"], "yarn"),
            ({"packageManager": "pnpm@9.12.0"}, ["package-lock.json"], "pnpm"),
        ]
        for fields, lockfiles, expected in cases:
            with self.subTest(expected=expected, lockfiles=lockfiles), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                _package(repo, **fields)
                for name in lockfiles:
                    _write(repo / name)
                self.assertEqual(node_package_manager(repo), expected)

    def test_install_is_frozen_for_each_manager(self):
        cases = [
            (["package-lock.json"], {}, ["npm", "ci", "--no-audit", "--no-fund"]),
            ([], {}, ["npm", "install", "--no-audit", "--no-fund"]),
            (["pnpm-lock.yaml"], {}, ["pnpm", "install", "--frozen-lockfile"]),
            (["yarn.lock"], {}, ["yarn", "install", "--frozen-lockfile"]),
            (["yarn.lock", ".yarnrc.yml"], {}, ["yarn", "install", "--immutable"]),
            (["yarn.lock"], {"packageManager": "yarn@4.5.0"}, ["yarn", "install", "--immutable"]),
        ]
        for files, fields, expected in cases:
            with self.subTest(files=files, fields=fields), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                _package(repo, **fields)
                for name in files:
                    _write(repo / name)
                self.assertEqual(node_install_command(repo)[1], expected)

    def test_tests_run_with_the_repo_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _package(repo, scripts={"test": "vitest run"})
            _write(repo / "pnpm-lock.yaml")
            self.assertEqual(unit_test_command(repo), ["pnpm", "test"])
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _package(repo, scripts={"test:unit": "jest"})
            _write(repo / "yarn.lock")
            self.assertEqual(unit_test_command(repo), ["yarn", "run", "test:unit"])

    def test_corepack_shims_go_on_the_work_order_path_for_pnpm(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            repo = workspace / "repo"
            _package(repo, scripts={"test": "vitest"})
            _write(repo / "pnpm-lock.yaml")
            runner = Runner()
            env, log = prepare_test_environment(workspace, repo, runner=runner)
            enable = [c for c in runner.commands if c[:2] == ["corepack", "enable"]]
            if enable:
                self.assertIn(str(workspace / ".ts-harness" / "venv" / "bin"), enable[0])
                self.assertIn("pnpm ready through corepack", log)
            else:  # no corepack on this machine: say so instead of failing later
                self.assertIn("corepack is not installed", log)
            self.assertIn("COREPACK_HOME", env.env)

    def test_npm_repo_needs_no_corepack(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            repo = workspace / "repo"
            _package(repo, scripts={"test": "jest"})
            runner = Runner()
            prepare_test_environment(workspace, repo, runner=runner)
            self.assertFalse([c for c in runner.commands if c[0] == "corepack"])

    def test_harness_tooling_is_kept_out_of_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            repo = workspace / "repo"
            (repo / ".git" / "info").mkdir(parents=True)
            _package(repo, scripts={"test": "jest"})
            prepare_test_environment(workspace, repo, runner=Runner())
            exclude = (repo / ".git" / "info" / "exclude").read_text().splitlines()
            self.assertIn(".opencode/", exclude)
            self.assertIn("node_modules/", exclude)


class CommitHygieneTests(unittest.TestCase):
    def test_commit_leaves_the_repo_gitignore_alone(self):
        from ts_harness.implementation.repo import commit_all_changes

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git = ["git", "-C", str(repo)]
            subprocess.run([*git, "init", "-q"], check=True)
            subprocess.run([*git, "config", "user.email", "t@example.com"], check=True)
            subprocess.run([*git, "config", "user.name", "t"], check=True)
            _write(repo / ".gitignore", "node_modules\n")
            _write(repo / "src" / "a.ts", "export {}\n")
            subprocess.run([*git, "add", "-A"], check=True)
            subprocess.run([*git, "commit", "-qm", "base"], check=True)
            _write(repo / "src" / "a.ts", "export const a = 1;\n")
            _write(repo / "__pycache__" / "x.pyc", "junk")
            self.assertTrue(commit_all_changes(repo, message="change"))
            shown = subprocess.run([*git, "show", "--name-only", "--format="], capture_output=True, text=True).stdout
            self.assertEqual(shown.split(), ["src/a.ts"])
            self.assertEqual((repo / ".gitignore").read_text(), "node_modules\n")


class StaticCheckTests(unittest.TestCase):
    def test_typecheck_script_wins_and_lint_covers_changed_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _package(repo, scripts={"test": "jest", "typecheck": "tsc --noEmit"})
            _write(repo / "tsconfig.json", "{}")
            _write(repo / "eslint.config.mjs", "export default [];")
            eslint = _bin(repo, "eslint")
            _bin(repo, "tsc")
            _write(repo / "src" / "a.ts")
            _write(repo / "src" / "b.spec.ts")
            checks = node_static_checks(repo, ["src/a.ts", "src/b.spec.ts", "README.md", "src/gone.ts"])
            self.assertEqual(
                checks,
                [("typecheck", ["npm", "run", "typecheck"]), ("lint", [str(eslint), "src/a.ts", "src/b.spec.ts"])],
            )

    def test_tsc_fallback_without_a_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _package(repo, scripts={"test": "jest"})
            _write(repo / "tsconfig.json", '{"compilerOptions": {}}')
            tsc = _bin(repo, "tsc")
            self.assertEqual(node_static_checks(repo, []), [("typecheck", [str(tsc), "--noEmit", "-p", "tsconfig.json"])])

    def test_solution_tsconfig_and_missing_tools_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _package(repo, scripts={"test": "jest"})
            _write(repo / "tsconfig.json", '{"files": [], "references": [{"path": "./app"}]}')
            _bin(repo, "tsc")
            _write(repo / "src" / "a.ts")
            # a references-only tsconfig, and eslint without a config: nothing to run
            _bin(repo, "eslint")
            self.assertEqual(node_static_checks(repo, ["src/a.ts"]), [])

    def test_other_stacks_have_no_node_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "go.mod", "module x\n")
            self.assertEqual(node_static_checks(repo, ["main.go"]), [])


class BddScopeTests(unittest.TestCase):
    def test_jest_repo_without_gherkin_does_not_run_bdd(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _package(repo, scripts={"test": "jest"})
            # a dependency's feature file is not the repo's
            _write(repo / "node_modules" / "pkg" / "x.feature", "Feature: x")
            self.assertFalse(repo_runs_bdd(repo))

    def test_repos_that_use_gherkin_or_have_no_tests_run_bdd(self):
        cases = {
            "features": {"package.json": '{"scripts": {"test": "jest"}}', "features/a.feature": "Feature: a"},
            "cucumber dep": {"package.json": '{"scripts": {"test": "jest"}, "devDependencies": {"@cucumber/cucumber": "11"}}'},
            "bdd script": {"package.json": '{"scripts": {"test": "jest", "test:bdd": "cucumber-js"}}'},
            "no tests": {"README.md": "# empty"},
        }
        for name, files in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp)
                for path, text in files.items():
                    _write(repo / path, text)
                self.assertTrue(repo_runs_bdd(repo))

    def test_changeset_needs_a_feature_file_only_for_bdd_repos(self):
        changed = ["src/users/dto/query-user.dto.ts", "test/admin/users.e2e-spec.ts"]
        self.assertEqual(
            validate_work_order_changeset(changed, work_order_id="WO-101", work_order_body=GWT_WORK_ORDER, bdd=False), []
        )
        self.assertIn(
            "missing_bdd_feature_for_work_order",
            validate_work_order_changeset(changed, work_order_id="WO-101", work_order_body=GWT_WORK_ORDER),
        )


class VerifyNodeRepoTests(unittest.TestCase):
    def _repo(self, tmp: str) -> Path:
        repo = Path(tmp)
        _package(repo, scripts={"test": "jest --passWithNoTests", "typecheck": "tsc --noEmit"})
        _write(repo / "eslint.config.mjs", "export default [];")
        _bin(repo, "eslint")
        _write(repo / "src" / "users" / "dto" / "query-user.dto.ts", "export {}")
        return repo

    def test_gwt_work_order_in_a_jest_repo_passes_without_feature_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            runner = Runner()
            result = verify_implementation(
                repo,
                work_order_body=GWT_WORK_ORDER,
                work_order_id="WO-101",
                changed_files=["src/users/dto/query-user.dto.ts"],
                runner=runner,
            )
            self.assertTrue(result.ok, result.reasons)
            self.assertFalse(result.bdd_required)
            ran = [" ".join(c) for c in runner.commands]
            self.assertIn("npm test", ran)
            self.assertIn("npm run typecheck", ran)
            self.assertTrue(any(c.endswith("eslint src/users/dto/query-user.dto.ts") for c in ran))

    def test_failed_checks_are_reasons_and_every_failure_keeps_its_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(tmp)
            runner = Runner(
                fail={
                    "npm test": "FAIL src/users/dto/query-user.dto.spec.ts\n" + "x" * 6000 + "\nUNIT_END",
                    "typecheck": "src/a.ts(3,1): error TS2322\n" + "y" * 6000 + "\nTSC_END",
                }
            )
            result = verify_implementation(
                repo,
                work_order_body=GWT_WORK_ORDER,
                work_order_id="WO-101",
                changed_files=["src/users/dto/query-user.dto.ts"],
                runner=runner,
            )
            self.assertEqual(result.reasons, ["unit_tests_failed", "typecheck_failed"])
            self.assertIn("UNIT_END", result.log_tail)
            self.assertIn("TSC_END", result.log_tail)
            self.assertLessEqual(len(result.log_tail), 4500)
            prompt = build_verification_fix_prompt("WO-101", result)
            self.assertIn("do not silence them", prompt)


if __name__ == "__main__":
    unittest.main()


class ReviewEvidenceTests(unittest.TestCase):
    def test_patch_budget_trims_the_largest_files_first(self):
        from ts_harness.workflows.code_review import MIN_FILE_PATCH, fit_patch_budget

        files = [{"filename": "a", "patch": "a" * 1000}, {"filename": "b", "patch": "b" * 50_000}, {"filename": "c", "patch": "c" * 50_000}]
        fit_patch_budget(files, budget=41_000)
        self.assertEqual(len(files[0]["patch"]), 1000)
        self.assertNotIn("patch_truncated", files[0])
        self.assertEqual(len(files[1]["patch"]), 20_000)
        self.assertTrue(files[2]["patch_truncated"])
        small = [{"filename": "x", "patch": "x" * 100_000}]
        fit_patch_budget(small, budget=10)
        self.assertEqual(len(small[0]["patch"]), MIN_FILE_PATCH)

    def test_new_file_without_a_patch_is_fetched_from_the_head(self):
        import base64

        from ts_harness.workflows.code_review import review_precheck

        class GitHub:
            def get_pull(self, owner, repo, number):
                return {"head": {"sha": "abc"}, "title": "WO-104"}

            def list_pull_files(self, owner, repo, number):
                return [{"filename": "src/auth/auth.service.spec.ts", "status": "added", "additions": 2, "deletions": 0}]

            def get_file(self, owner, repo, path, *, ref):
                assert ref == "abc"
                return {"encoding": "base64", "content": base64.b64encode(b"it('a', () => {});\nit('b', () => {});").decode()}

        task = {"workflow_id": "wf", "input": {"pr_url": "https://github.com/o/r/pull/38", "work_order_body": "# WO-104"}}
        from unittest.mock import patch as mock_patch

        with mock_patch("ts_harness.workflows.code_review._context", return_value=({}, [task])):
            out = review_precheck(
                task, {"pr_url": "https://github.com/o/r/pull/38"}, memory=None, github=GitHub(), program_defaults={}
            )
        self.assertEqual(out["files"][0]["patch"], "@@ -0,0 +1,2 @@\n+it('a', () => {});\n+it('b', () => {});")

    def test_stage_output_carries_host_verification_for_qa(self):
        from ts_harness.implementation.coding_job import coding_stage_output

        evidence = {
            "result": "passed",
            "reasons": [],
            "checks": [{"check": "unit", "command": "npm test", "exit_code": 0, "output_tail": "Tests: 37 passed"}],
            "skipped": ["bdd"],
        }
        output = coding_stage_output({"ok": True, "work_order_id": "WO-104", "mode": "implement", "verification": evidence})
        self.assertEqual(output["verification"], evidence)
        self.assertIn("unit: `npm test` exit 0", output["verify_summary"])
        self.assertIn("Tests: 37 passed", output["verify_summary"])

    def test_verify_records_each_command(self):
        from ts_harness.implementation.verification import verification_evidence

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _package(repo, scripts={"test": "jest", "typecheck": "tsc --noEmit"})
            _write(repo / "src" / "a.ts", "export {}")
            result = verify_implementation(
                repo, work_order_body=GWT_WORK_ORDER, work_order_id="WO-101", changed_files=["src/a.ts"], runner=Runner()
            )
            evidence = verification_evidence(result)
            self.assertEqual([c["check"] for c in evidence["checks"]], ["unit", "typecheck"])
            self.assertEqual(evidence["result"], "passed")
