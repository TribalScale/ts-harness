"""Hardening for non-interactive OpenCode implement (no brainstorm, no junk PRs)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation.backends.opencode import build_implement_prompt
from ts_harness.implementation.coding_backend_skills import (
    SUPERPOWERS_OPENCODE_PLUGIN,
    ensure_opencode_project_config,
)
from ts_harness.implementation.repo import commit_all_changes
from ts_harness.implementation.verification import (
    VerifyResult,
    build_verification_fix_prompt,
    detect_bdd_command,
    detect_unit_test_command,
    validate_work_order_changeset,
    verify_implementation,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "dev"], cwd=repo, check=True, capture_output=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


class SuperpowersDisabledTests(unittest.TestCase):
    def test_ensure_opencode_project_config_strips_superpowers_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".opencode"
            target.mkdir()
            (target / "opencode.json").write_text(
                json.dumps(
                    {
                        "plugin": [SUPERPOWERS_OPENCODE_PLUGIN, "other@npm:example"],
                        "model": "openai/gpt",
                    }
                ),
                encoding="utf-8",
            )
            path = ensure_opencode_project_config(target)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["model"], "openai/gpt")
            self.assertIn("other@npm:example", data["plugin"])
            self.assertNotIn(SUPERPOWERS_OPENCODE_PLUGIN, data["plugin"])
            self.assertFalse(any("superpowers" in str(p).lower() for p in data["plugin"]))

    def test_ensure_opencode_project_config_creates_json_without_superpowers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / ".opencode"
            path = ensure_opencode_project_config(target)
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data.get("plugin"), [])


class PromptHardeningTests(unittest.TestCase):
    def test_implement_prompt_forbids_brainstorming_and_approval_gates(self) -> None:
        prompt = build_implement_prompt(
            "WO-002",
            "# WO-002\n\nGiven a game when chat then deliver.",
            primary_spec_path="docs/planner/work-orders/WO-002-chat.md",
            docs_materialized=True,
            skills_materialized=True,
        )
        lower = prompt.lower()
        self.assertIn("brainstorming", lower)
        self.assertIn("do not", lower)
        self.assertIn("approval", lower)
        self.assertIn("implement immediately", lower)

    def test_verification_fix_prompt_requires_work_order_behavior(self) -> None:
        prompt = build_verification_fix_prompt(
            "WO-002",
            VerifyResult(
                ok=False,
                reasons=["no_substantive_changes", "missing_bdd_feature_for_work_order"],
                summary="verification failed",
                log_tail="",
            ),
        )
        lower = prompt.lower()
        self.assertIn("brainstorming", lower)
        self.assertIn("approval", lower)
        self.assertIn("work order", lower)


class ChangesetValidationTests(unittest.TestCase):
    def test_junk_and_requirements_only_fails_gwt_work_order(self) -> None:
        reasons = validate_work_order_changeset(
            [
                "requirements.txt",
                "gameplay/__pycache__/service.cpython-311.pyc",
                "tests/__pycache__/test_x.cpython-311-pytest-8.2.2.pyc",
            ],
            work_order_id="WO-002",
            work_order_body=(
                "# WO-002\nGiven a game in progress, when a player sends a message, "
                "then the opponent should see it in real-time."
            ),
        )
        self.assertIn("no_substantive_changes", reasons)
        self.assertIn("missing_bdd_feature_for_work_order", reasons)

    def test_feature_and_code_changes_pass(self) -> None:
        reasons = validate_work_order_changeset(
            [
                "features/wo-002-integrated-chat.feature",
                "features/steps/chat_steps.py",
                "gameplay/chat.py",
                "tests/test_chat.py",
            ],
            work_order_id="WO-002",
            work_order_body=(
                "Given a game when chat then deliver in real-time."
            ),
        )
        self.assertEqual(reasons, [])


class CommitIgnoresPycacheTests(unittest.TestCase):
    def test_commit_all_changes_skips_pycache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _init_repo(repo)
            (repo / "gameplay").mkdir()
            (repo / "gameplay" / "__pycache__").mkdir()
            (repo / "gameplay" / "__pycache__" / "x.cpython-311.pyc").write_bytes(b"\0\0")
            (repo / "requirements.txt").write_text("pytest==8.2.2\n", encoding="utf-8")
            (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")

            committed = commit_all_changes(repo, message="Implement WO-002")
            self.assertTrue(committed)
            names = subprocess.run(
                ["git", "-C", str(repo), "show", "--name-only", "--pretty=format:", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            self.assertIn("app.py", names)
            self.assertIn("requirements.txt", names)
            self.assertFalse(any("__pycache__" in n or n.endswith(".pyc") for n in names))


class RunnerDetectionTests(unittest.TestCase):
    def test_bare_tests_dir_is_not_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "tests").mkdir()
            self.assertIsNone(detect_unit_test_command(repo_dir))
            (repo_dir / "tests" / "test_app.py").write_text("def test_ok():\n    pass\n", encoding="utf-8")
            self.assertEqual(detect_unit_test_command(repo_dir), ["python3", "-m", "pytest"])

    def test_bare_features_dir_is_not_behave(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "features").mkdir()
            self.assertIsNone(detect_bdd_command(repo_dir))
            (repo_dir / "features" / "steps").mkdir()
            (repo_dir / "features" / "steps" / "steps.py").write_text("", encoding="utf-8")
            self.assertEqual(detect_bdd_command(repo_dir), ["python3", "-m", "behave"])

    def test_verify_rejects_requirements_only_changeset_for_gwt(self) -> None:
        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="ok\n", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            repo_dir = Path(tmp)
            (repo_dir / "tests").mkdir()
            (repo_dir / "features").mkdir()
            (repo_dir / "features" / "old.feature").write_text("Feature: old\n", encoding="utf-8")
            result = verify_implementation(
                repo_dir,
                work_order_id="WO-002",
                work_order_body=(
                    "Given a game when a player sends a message then the opponent sees it."
                ),
                changed_files=["requirements.txt", "tests/__pycache__/x.pyc"],
                runner=runner,
            )
            self.assertFalse(result.ok)
            self.assertIn("no_substantive_changes", result.reasons)


if __name__ == "__main__":
    unittest.main()
