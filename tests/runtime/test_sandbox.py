import json
import unittest
import unittest.mock
import os
import tempfile

from ts_harness.sandbox.docker_env import DockerExecutionEnvironment
import importlib.util
from ts_harness.tools import terminal
from ts_harness.tools.approvals import ApprovalQueue, scan_command
from ts_harness.tools.file_tools import _safe_path, handle_patch_file, handle_search_files, handle_write_file
from ts_harness.tools.policy import ToolPolicyContext
from pathlib import Path


class FakeCompleted:
    returncode = 0
    stdout = "x" * 5000
    stderr = "e" * 3000


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return FakeCompleted()


class SandboxTests(unittest.TestCase):
    def test_blocks_destructive_commands(self):
        allowed, reason = scan_command("rm -rf /")
        self.assertFalse(allowed)
        self.assertIn("blocked", reason or "")

    def test_blocks_additional_hostile_command_patterns(self):
        for command in (
            ":(){ :|:& };:",
            "mkfs /dev/sda1",
            "sudo rm -rf /",
            "cat /etc/shadow",
        ):
            allowed, reason = scan_command(command)
            self.assertFalse(allowed, command)
            self.assertIn("blocked", reason or "")

    def test_write_outside_workspace_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TS_HARNESS_WORKSPACE_ROOT"] = tmp
            try:
                ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="workflow")
                with self.assertRaises(ValueError):
                    handle_write_file({"path": "../../outside.txt", "content": "nope"}, ctx)
            finally:
                os.environ.pop("TS_HARNESS_WORKSPACE_ROOT", None)

    def test_approval_decide_records_approver_identity(self):
        queue = ApprovalQueue()
        request = queue.request(
            tenant_id="default",
            agent_id="dev-agent",
            tool_name="exec",
            args={"command": "deploy"},
            reason="approval required",
        )
        decided = queue.decide(
            request.approval_id,
            approved=True,
            actor_id="admin-1",
            roles=["admin"],
        )
        self.assertEqual(decided.decided_by_actor_id, "admin-1")
        self.assertEqual(decided.decided_by_roles, ["admin"])

    def test_blocks_host_sensitive_paths(self):
        base = Path("/workspace/dev-agent").resolve()
        with self.assertRaises(ValueError):
            _safe_path("../../etc/passwd", base)

    def test_worker_request_carries_applied_timeout_and_egress(self):
        from ts_harness.sandbox.docker_env import get_sandbox_limits, set_sandbox_limits

        previous = get_sandbox_limits()
        captured: dict = {}

        class Response:
            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(request, timeout=0):
            captured["body"] = json.loads(request.data.decode())
            captured["timeout"] = timeout
            return Response()

        set_sandbox_limits(
            {
                "timeout_seconds": 42,
                "egress_policy": "allow",
                "cpu_limit": 0.5,
                "memory_mb": 256,
            }
        )
        try:
            env = DockerExecutionEnvironment(enabled=True)
            with unittest.mock.patch.dict(os.environ, {"SANDBOX_WORKER_URL": "http://worker"}):
                with unittest.mock.patch(
                    "ts_harness.sandbox.docker_env.urllib.request.urlopen",
                    fake_urlopen,
                ):
                    env.run("echo hi", workspace="/tmp", tenant_id="default")
        finally:
            set_sandbox_limits(previous)
        self.assertEqual(captured["body"]["timeout"], 42)
        self.assertEqual(captured["body"]["egress_policy"], "allow")
        self.assertEqual(captured["body"]["memory_mb"], 256)
        self.assertEqual(captured["body"]["cpu_limit"], 0.5)

    def test_docker_backend_uses_hardened_flags_and_bounded_logs(self):
        runner = FakeRunner()
        env = DockerExecutionEnvironment(enabled=True, runner=runner)

        result = env.run("echo ok", workspace="/workspace/dev-agent", tenant_id="tenant-a")

        cmd, kwargs = runner.calls[0]
        self.assertEqual(cmd[:2], ["docker", "run"])
        self.assertIn("--cap-drop=ALL", cmd)
        self.assertIn("--security-opt=no-new-privileges", cmd)
        self.assertIn("--network=none", cmd)
        self.assertNotIn("-v", cmd)
        self.assertLessEqual(len(result["stdout"]), 4000)
        self.assertLessEqual(len(result["stderr"]), 2000)

    def test_docker_backend_mounts_workspace_only_when_persistent_enabled(self):
        runner = FakeRunner()
        env = DockerExecutionEnvironment(enabled=True, runner=runner, persistent_workspace=True)

        env.run("echo ok", workspace="/workspace/dev-agent", tenant_id="tenant-a")

        cmd, _ = runner.calls[0]
        self.assertIn("-v", cmd)
        self.assertIn("/workspace/dev-agent:/workspace/dev-agent:rw", cmd)

    def test_orphan_reaper_targets_sandbox_labels(self):
        runner = FakeRunner()
        env = DockerExecutionEnvironment(enabled=True, runner=runner)

        env.reap_orphans(older_than_seconds=300)

        cmd, _ = runner.calls[0]
        self.assertIn("ps", cmd)
        self.assertIn("label=ts-harness.sandbox=true", " ".join(cmd))

    def test_background_exec_returns_job_id_and_recovers_logs(self):
        old_env = terminal._env
        terminal._env = DockerExecutionEnvironment(enabled=False, runner=FakeRunner())
        try:
            ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="workflow")
            started = terminal.handle_exec({"command": "echo ok", "background": True}, ctx)
            logs = terminal.handle_background_logs({"job_id": started["job_id"]}, ctx)
        finally:
            terminal._env = old_env

        self.assertEqual(started["status"], "started")
        self.assertIn("job_id", started)
        self.assertIn("stdout", logs)

    def test_search_files_and_structured_patch_are_workspace_anchored(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TS_HARNESS_WORKSPACE_ROOT"] = tmp
            try:
                ctx = ToolPolicyContext(tenant_id="default", agent_id="dev-agent", source="workflow")
                handle_write_file({"path": "src/app.py", "content": "hello world"}, ctx)
                search = handle_search_files({"query": "hello", "path": "src"}, ctx)
                patch = handle_patch_file({"path": "src/app.py", "old": "hello", "new": "goodbye"}, ctx)
            finally:
                os.environ.pop("TS_HARNESS_WORKSPACE_ROOT", None)

        self.assertEqual(len(search["matches"]), 1)
        self.assertTrue(patch["patched"])

    def test_approval_queue_records_pending_human_decision(self):
        queue = ApprovalQueue()
        request = queue.request(
            tenant_id="default",
            agent_id="dev-agent",
            tool_name="exec",
            args={"command": "deploy"},
            reason="human approval required",
        )

        self.assertEqual(request.status, "pending")
        self.assertEqual(queue.get(request.approval_id).tool_name, "exec")

    def test_compose_uses_sandbox_worker_without_host_docker_socket(self):
        compose = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.agent-runtime.yml"
        text = compose.read_text()

        self.assertIn("sandbox-worker:", text)
        self.assertIn("SANDBOX_WORKER_URL", text)
        self.assertNotIn("/var/run/docker.sock:/var/run/docker.sock", text)

    def test_sandbox_worker_blocks_network_tools_when_egress_denied(self):
        worker_path = Path(__file__).resolve().parents[2] / "scripts" / "sandbox-worker.py"
        spec = importlib.util.spec_from_file_location("sandbox_worker", worker_path)
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)

        allowed, reason = worker.egress_allowed("curl https://example.com", policy="deny")

        self.assertFalse(allowed)
        self.assertIn("egress", reason)

    def test_sandbox_worker_runs_without_host_shell_true(self):
        worker_path = Path(__file__).resolve().parents[2] / "scripts" / "sandbox-worker.py"
        spec = importlib.util.spec_from_file_location("sandbox_worker", worker_path)
        worker = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(worker)

        calls = []

        def runner(argv, **kwargs):
            calls.append((argv, kwargs))
            return FakeCompleted()

        with unittest.mock.patch.object(worker.subprocess, "run", side_effect=runner):
            result = worker.run_isolated_command("echo ok", workspace="/tmp", timeout=5)

        self.assertTrue(result.get("isolated"))
        argv, kwargs = calls[0]
        self.assertFalse(kwargs.get("shell"))
        self.assertIn("bash", argv)
        self.assertIn("-lc", argv)


if __name__ == "__main__":
    unittest.main()
