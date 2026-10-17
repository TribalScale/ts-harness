"""Tests for workflow-runner bundle layout smoke."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class WorkflowRunnerBundleTests(unittest.TestCase):
    def test_dockerfile_bundles_runtime_and_installs_package(self):
        dockerfile = (REPO_ROOT / "docker" / "Dockerfile.workflow-runner").read_text()
        self.assertIn("COPY runtime", dockerfile)
        self.assertIn("pip install", dockerfile)
        self.assertIn("workflow-runner.py", dockerfile)

    def test_runner_pythonpath_imports_engine_and_hooks(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "runtime"), str(REPO_ROOT / "scripts")])
        probe = (
            "from ts_harness.workflows.engine import get_workflow_engine; "
            "from ts_harness.workflows.hooks import continue_delivery; "
            "engine = get_workflow_engine(); "
            "assert hasattr(engine, 'on_task_transition'); "
            "assert hasattr(engine, 'after_task_succeeded')"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            env=env,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)


if __name__ == "__main__":
    unittest.main()
