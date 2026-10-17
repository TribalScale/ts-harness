# flake8: noqa
import os
import unittest
from pathlib import Path
from unittest import mock

from ts_harness.repo_paths import config_dir, repo_root, scripts_dir, workflows_dir


REPO_ROOT = Path(__file__).resolve().parents[2]


class RepoPathsTests(unittest.TestCase):
    def test_repo_root_from_dev_checkout(self):
        self.assertEqual(repo_root(), REPO_ROOT.resolve())

    def test_scripts_dir_points_at_repo_scripts(self):
        self.assertTrue((scripts_dir() / "workflow_definitions.py").is_file())

    def test_workflows_dir_contains_skeletons(self):
        self.assertTrue(any(workflows_dir().glob("*.json")))

    def test_config_dir_has_release_gates_manifest(self):
        self.assertTrue((config_dir() / "release-gates.yaml").is_file())

    def test_repo_root_honors_env_override(self):
        with mock.patch.dict(os.environ, {"TS_HARNESS_ROOT": "/tmp/custom-root"}):
            from ts_harness import repo_paths

            repo_paths.repo_root.cache_clear()
            try:
                self.assertEqual(repo_paths.repo_root(), Path("/tmp/custom-root").resolve())
            finally:
                repo_paths.repo_root.cache_clear()

    def test_repo_root_falls_back_to_app_when_markers_present(self):
        with mock.patch.dict(os.environ, {"TS_HARNESS_ROOT": ""}, clear=False):
            from ts_harness import repo_paths

            repo_paths.repo_root.cache_clear()
            fake_app = Path("/app")
            with mock.patch.object(repo_paths.Path, "is_dir", autospec=True) as is_dir:
                is_dir.side_effect = lambda self: self == fake_app / "config" / "workflows"
                with mock.patch.object(
                    repo_paths.Path,
                    "exists",
                    autospec=True,
                    side_effect=lambda self: self.name == "pyproject.toml",
                ):
                    with mock.patch.object(
                        repo_paths.Path,
                        "resolve",
                        autospec=True,
                        side_effect=lambda self: self,
                    ):
                        with mock.patch.object(
                            repo_paths.Path,
                            "parents",
                            new_callable=mock.PropertyMock,
                            return_value=(),
                        ):
                            self.assertEqual(repo_paths.repo_root(), fake_app)
            repo_paths.repo_root.cache_clear()

    def test_repo_root_ignores_bare_config_workflows_at_filesystem_root(self):
        with mock.patch.dict(os.environ, {"TS_HARNESS_ROOT": ""}, clear=False):
            from ts_harness import repo_paths

            repo_paths.repo_root.cache_clear()
            with mock.patch.object(repo_paths.Path, "is_dir", autospec=True) as is_dir:
                # Mimic workflow-runner layout: /config/workflows exists, but / has no pyproject.
                is_dir.side_effect = lambda self: str(self) == "/config/workflows"
                with mock.patch.object(
                    repo_paths.Path,
                    "exists",
                    autospec=True,
                    side_effect=lambda self: False,
                ):
                    with mock.patch.object(
                        repo_paths.Path,
                        "resolve",
                        autospec=True,
                        side_effect=lambda self: self,
                    ):
                        with mock.patch.object(
                            repo_paths.Path,
                            "parents",
                            new_callable=mock.PropertyMock,
                            return_value=(Path("/"),),
                        ):
                            self.assertEqual(repo_paths.repo_root(), Path("/app"))
            repo_paths.repo_root.cache_clear()


if __name__ == "__main__":
    unittest.main()
