from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "tests"))

from object_store_support import FakeS3  # noqa: E402
from ts_harness.storage import objects as objects_mod  # noqa: E402
from ts_harness.storage.objects import S3ObjectStore  # noqa: E402
from ts_harness.storage.workspace import (  # noqa: E402
    FileWorkspace,
    ObjectWorkspace,
    WorkspaceTooLarge,
    clean_rel,
    workspace_store,
)
from ts_harness.tools import file_tools  # noqa: E402
from ts_harness.tools.policy import ToolPolicyContext  # noqa: E402

BUCKET = "ws-bucket"


class ObjectModeCase(unittest.TestCase):
    """Runs with TS_HARNESS_OBJECT_BACKEND=s3 against an in-memory S3."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s3 = FakeS3()
        self.store = S3ObjectStore(BUCKET, client=self.s3)
        env = mock.patch.dict(
            os.environ,
            {
                "TS_HARNESS_OBJECT_BACKEND": "s3",
                "TS_HARNESS_WORKSPACE_BUCKET": BUCKET,
                "TS_HARNESS_SCRATCH_DIR": str(Path(self.tmp.name) / "scratch"),
                "TS_HARNESS_WORKSPACE_ROOT": str(Path(self.tmp.name) / "disk-workspace"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        stores = mock.patch.dict(objects_mod._STORES, {("workspaces", "s3", BUCKET): self.store}, clear=True)
        stores.start()
        self.addCleanup(stores.stop)

    def keys(self) -> list[str]:
        return sorted(k for b, k in self.s3.objects if b == BUCKET)

    def get(self, key: str) -> bytes:
        return self.s3.objects[(BUCKET, key)][0]


class CleanRelTests(unittest.TestCase):
    def test_normalizes_and_rejects_escapes(self) -> None:
        self.assertEqual(clean_rel("a/./b/../c.md"), "a/c.md")
        self.assertEqual(clean_rel(""), "")
        self.assertEqual(clean_rel("."), "")
        for bad in ("../x", "/etc/passwd", "a/../../x", ".."):
            with self.assertRaises(ValueError):
                clean_rel(bad)


class FileWorkspaceTests(unittest.TestCase):
    def test_round_trip_under_agent_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = FileWorkspace("ops", tmp)
            self.assertEqual(store.write_text("notes/a.md", "hi"), 2)
            self.assertEqual((Path(tmp) / "ops" / "notes" / "a.md").read_text(), "hi")
            self.assertEqual(store.read_text_or_none("missing.md"), None)
            self.assertEqual([f.path for f in store.list()], ["notes/a.md"])
            store.delete("notes/a.md")
            self.assertFalse(store.exists("notes/a.md"))

    def test_rejects_bad_agent_ids(self) -> None:
        for bad in ("", "..", "a/b"):
            with self.assertRaises(ValueError):
                FileWorkspace(bad, "/tmp")


class ObjectWorkspaceTests(ObjectModeCase):
    def test_factory_picks_backend(self) -> None:
        self.assertIsInstance(workspace_store("ops"), ObjectWorkspace)
        with mock.patch.dict(os.environ, {"TS_HARNESS_OBJECT_BACKEND": "local"}):
            self.assertIsInstance(workspace_store("ops", root=self.tmp.name), FileWorkspace)

    def test_text_and_binary_round_trip(self) -> None:
        store = ObjectWorkspace("ops", self.store)
        store.write_text("MEMORY.md", "remember")
        store.write_bytes("img/logo.png", b"\x89PNG\x00\xff")
        self.assertEqual(self.keys(), ["workspaces/ops/MEMORY.md", "workspaces/ops/img/logo.png"])
        self.assertEqual(store.read_text("MEMORY.md"), "remember")
        self.assertEqual(store.read_bytes("img/logo.png"), b"\x89PNG\x00\xff")
        self.assertTrue(store.exists("img"))
        self.assertFalse(store.is_file("img"))
        self.assertEqual([f.path for f in store.list("img")], ["img/logo.png"])
        with self.assertRaises(FileNotFoundError):
            store.read_bytes("nope.md")
        with self.assertRaises(IsADirectoryError):
            store.write_text("", "x")

    def test_object_size_cap(self) -> None:
        store = ObjectWorkspace("ops", self.store, max_object_bytes=4)
        with self.assertRaises(WorkspaceTooLarge):
            store.write_bytes("big.bin", b"12345")
        self.assertEqual(self.keys(), [])

    def test_agents_do_not_see_each_other(self) -> None:
        ObjectWorkspace("ops", self.store).write_text("a.md", "ops")
        ObjectWorkspace("ops-2", self.store).write_text("a.md", "ops-2")
        self.assertEqual([f.path for f in ObjectWorkspace("ops", self.store).list()], ["a.md"])
        self.assertEqual(ObjectWorkspace("ops", self.store).read_text("a.md"), "ops")


class FileToolsOnStoreTests(ObjectModeCase):
    def ctx(self) -> ToolPolicyContext:
        return ToolPolicyContext(tenant_id="t1", agent_id="ops", source="test")

    def test_write_read_patch_search(self) -> None:
        ctx = self.ctx()
        written = file_tools.handle_write_file({"path": "notes/todo.md", "content": "alpha\nbeta\n"}, ctx)
        self.assertEqual(written, {"path": "/workspace/ops/notes/todo.md", "bytes_written": 11})
        self.assertEqual(self.get("workspaces/ops/notes/todo.md"), b"alpha\nbeta\n")
        self.assertFalse((Path(self.tmp.name) / "disk-workspace").exists())

        read = file_tools.handle_read_file({"path": "notes/todo.md"}, ctx)
        self.assertEqual(read["content"], "alpha\nbeta\n")
        self.assertEqual(file_tools.handle_read_file({"path": "absent.md"}, ctx), {"error": "file not found"})

        patched = file_tools.handle_patch_file({"path": "notes/todo.md", "old": "beta", "new": "gamma"}, ctx)
        self.assertEqual(patched, {"path": "/workspace/ops/notes/todo.md", "patched": True})
        self.assertEqual(self.get("workspaces/ops/notes/todo.md"), b"alpha\ngamma\n")
        self.assertEqual(
            file_tools.handle_patch_file({"path": "notes/todo.md", "old": "zzz", "new": "y"}, ctx),
            {"error": "patch context not found"},
        )
        self.assertEqual(
            file_tools.handle_patch_file({"path": "absent.md", "old": "a"}, ctx), {"error": "file not found"}
        )

        found = file_tools.handle_search_files({"query": "gamma"}, ctx)
        self.assertEqual(
            found["matches"], [{"path": "/workspace/ops/notes/todo.md", "line": 2, "preview": "gamma"}]
        )
        self.assertEqual(file_tools.handle_search_files({"query": ""}, ctx), {"matches": []})

    def test_absolute_workspace_path_is_accepted(self) -> None:
        ctx = self.ctx()
        file_tools.handle_write_file({"path": "/workspace/ops/abs.md", "content": "x"}, ctx)
        self.assertIn("workspaces/ops/abs.md", self.keys())

    def test_path_safety_messages_match_disk_mode(self) -> None:
        ctx = self.ctx()
        cases = {
            "../ops-2/secret.md": "path escapes workspace",
            "/workspace/ops-2/x.md": "path escapes workspace",
            "/etc/passwd": "sensitive path blocked",
            "/proc/self/environ": "device path blocked",
        }
        for path, message in cases.items():
            with self.assertRaises(ValueError) as caught:
                file_tools.handle_read_file({"path": path}, ctx)
            self.assertEqual(str(caught.exception), message, path)
        self.assertEqual(self.keys(), [])

    def test_search_skips_binary_and_large_objects_and_honours_limit(self) -> None:
        ctx = self.ctx()
        store = ObjectWorkspace("ops", self.store)
        store.write_bytes("bin.dat", b"\xff\xfeneedle")
        for i in range(5):
            store.write_text(f"f{i}.md", "needle\n")
        with mock.patch.object(file_tools, "SEARCH_MAX_OBJECT_BYTES", 6):
            self.assertEqual(file_tools.handle_search_files({"query": "needle"}, ctx), {"matches": []})
        found = file_tools.handle_search_files({"query": "needle", "limit": 2}, ctx)
        self.assertEqual([m["path"] for m in found["matches"]], ["/workspace/ops/f0.md", "/workspace/ops/f1.md"])

    def test_disk_mode_unchanged(self) -> None:
        with mock.patch.dict(os.environ, {"TS_HARNESS_OBJECT_BACKEND": "local"}):
            out = file_tools.handle_write_file({"path": "d.md", "content": "disk"}, self.ctx())
        disk = Path(self.tmp.name) / "disk-workspace" / "ops" / "d.md"
        self.assertEqual(disk.read_text(), "disk")
        self.assertEqual(out["path"], str(disk.resolve()))
        self.assertEqual(self.keys(), [])


class OverlayAndHeartbeatTests(ObjectModeCase):
    def test_overlay_agents_md_goes_to_store(self) -> None:
        from ts_harness.config.agents_overlay import write_overlay_agents_md

        path = write_overlay_agents_md(
            "helper", {"display_name": "Helper", "soul": "kind", "model": "m1"}, workspace_root=self.tmp.name
        )
        self.assertEqual(str(path), "/workspace/helper/AGENTS.md")
        self.assertIn(b"# Helper (helper)", self.get("workspaces/helper/AGENTS.md"))

    def test_heartbeat_file_read_through_store(self) -> None:
        from ts_harness.gateway.scheduler import Scheduler

        scheduler = Scheduler.__new__(Scheduler)
        scheduler.workspace_root = Path(self.tmp.name)
        scheduler._audit = mock.Mock()
        self.assertFalse(scheduler._heartbeat_file_empty("ops"))
        ObjectWorkspace("ops", self.store).write_text("HEARTBEAT.md", "  \n")
        self.assertTrue(scheduler._heartbeat_file_empty("ops"))
        ObjectWorkspace("ops", self.store).write_text("HEARTBEAT.md", "- check inbox\n")
        self.assertFalse(scheduler._heartbeat_file_empty("ops"))


class SandboxRoundTripTests(ObjectModeCase):
    def test_fill_run_upload_and_delete(self) -> None:
        from ts_harness.sandbox.docker_env import DockerExecutionEnvironment

        store = ObjectWorkspace("ops", self.store)
        store.write_text("keep.md", "same")
        store.write_text("edit.md", "old")
        store.write_text("gone.md", "bye")
        seen: dict[str, object] = {}

        def runner(cmd, **kwargs):
            cwd = Path(kwargs["cwd"])
            seen["cwd"] = cwd
            seen["files"] = sorted(p.name for p in cwd.iterdir())
            (cwd / "edit.md").write_text("new")
            (cwd / "gone.md").unlink()
            (cwd / "out").mkdir()
            (cwd / "out" / "result.txt").write_text("42")
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        env = DockerExecutionEnvironment(enabled=False, runner=runner, persistent_workspace=True)
        with mock.patch.dict(os.environ, {"SANDBOX_STRICT": "false"}):
            os.environ.pop("SANDBOX_WORKER_URL", None)
            result = env.run("make", workspace="/workspace/ops", tenant_id="t1")

        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(seen["files"], ["edit.md", "gone.md", "keep.md"])
        self.assertTrue(str(seen["cwd"]).startswith(os.environ["TS_HARNESS_SCRATCH_DIR"]))
        self.assertFalse(Path(seen["cwd"]).exists())
        self.assertEqual(
            result["workspace_sync"], {"uploaded": ["edit.md", "out/result.txt"], "deleted": ["gone.md"], "skipped": []}
        )
        self.assertEqual(self.keys(), ["workspaces/ops/edit.md", "workspaces/ops/keep.md", "workspaces/ops/out/result.txt"])
        self.assertEqual(self.get("workspaces/ops/edit.md"), b"new")

    def test_docker_mounts_scratch_at_agent_path(self) -> None:
        from ts_harness.sandbox.docker_env import DockerExecutionEnvironment

        calls = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        env = DockerExecutionEnvironment(enabled=True, runner=runner, persistent_workspace=True)
        os.environ.pop("SANDBOX_WORKER_URL", None)
        env.run("ls", workspace="/workspace/ops", tenant_id="t1")
        cmd = calls[0]
        mount = cmd[cmd.index("-v") + 1]
        host, target, mode = mount.split(":")
        self.assertTrue(host.startswith(os.environ["TS_HARNESS_SCRATCH_DIR"]))
        self.assertEqual((target, mode), ("/workspace/ops", "rw"))
        self.assertEqual(cmd[cmd.index("-w") + 1], "/workspace/ops")

    def test_caps_refuse_large_workspace(self) -> None:
        store = ObjectWorkspace("ops", self.store)
        for i in range(3):
            store.write_text(f"{i}.md", "x")
        with self.assertRaises(WorkspaceTooLarge):
            store.fill(Path(self.tmp.name) / "fill", max_files=2)
        with self.assertRaises(WorkspaceTooLarge):
            store.fill(Path(self.tmp.name) / "fill", max_bytes=2)

    def test_upload_skips_over_cap_and_bad_keys(self) -> None:
        store = ObjectWorkspace("ops", self.store, max_object_bytes=3)
        src = Path(self.tmp.name) / "src"
        src.mkdir()
        (src / "ok.md").write_text("ok")
        (src / "big.md").write_text("toolarge")
        (src / "bad name?.md").write_text("x")
        result = store.upload_changes(src, {})
        self.assertEqual(result["uploaded"], ["ok.md"])
        self.assertEqual(sorted(result["skipped"]), ["bad name?.md", "big.md"])

    def test_non_persistent_sandbox_ignores_store(self) -> None:
        from ts_harness.sandbox.docker_env import DockerExecutionEnvironment

        calls = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        os.environ.pop("SANDBOX_WORKER_URL", None)
        result = DockerExecutionEnvironment(enabled=True, runner=runner).run("ls", workspace="/workspace/ops", tenant_id="t1")
        self.assertNotIn("workspace_sync", result)
        self.assertNotIn("-v", calls[0])


class CodingCheckoutDirTests(unittest.TestCase):
    def test_scratch_only_for_in_process_object_mode(self) -> None:
        from ts_harness.implementation.coding_job import _default_checkout_dir

        base = {"TS_HARNESS_SCRATCH_DIR": "/var/tmp/x"}
        with mock.patch.dict(os.environ, {**base, "TS_HARNESS_OBJECT_BACKEND": "s3", "CODING_JOBS_ENABLED": ""}):
            self.assertEqual(_default_checkout_dir("ops", "wo-1"), "/var/tmp/x/coding-backend/ops/wo-1")
        with mock.patch.dict(os.environ, {**base, "TS_HARNESS_OBJECT_BACKEND": "s3", "CODING_JOBS_ENABLED": "true"}):
            self.assertEqual(_default_checkout_dir("ops", "wo-1"), "/workspace/ops/coding-backend/wo-1")
        with mock.patch.dict(os.environ, {**base, "TS_HARNESS_OBJECT_BACKEND": "local", "CODING_JOBS_ENABLED": ""}):
            self.assertEqual(_default_checkout_dir("ops", "wo-1"), "/workspace/ops/coding-backend/wo-1")


FAKE_AWS = r"""#!/usr/bin/env python3
import json, os, shutil, sys
from pathlib import Path
store = Path(os.environ["FAKE_BUCKET_DIR"])
log = Path(os.environ["FAKE_AWS_LOG"])
with log.open("a") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if args[:2] == ["s3api", "head-object"]:
    key = args[args.index("--key") + 1]
    if (store / key).is_file():
        print("{}")
        sys.exit(0)
    print("An error occurred (404) when calling the HeadObject operation: Not Found", file=sys.stderr)
    sys.exit(254)
if args[:2] == ["s3", "cp"]:
    src, dst = args[-2], args[-1]
    key = dst.split("/", 3)[3]
    (store / key).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, store / key)
    sys.exit(0)
sys.exit(3)
"""


class SeedWorkspaceObjectsTests(unittest.TestCase):
    """Phase 10b's no-overwrite contract: rendered files go up only where the object is missing."""

    SCRIPT = ROOT / "scripts" / "ops" / "seed-workspace-objects.py"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)], check=False))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        aws = self.bin / "aws"
        aws.write_text(FAKE_AWS)
        aws.chmod(aws.stat().st_mode | stat.S_IEXEC)
        self.bucket = self.tmp / "bucket"
        self.bucket.mkdir()
        self.log = self.tmp / "aws.log"
        self.src = self.tmp / "rendered"
        (self.src / "ops").mkdir(parents=True)
        (self.src / "ops" / "MEMORY.md").write_text("template memory")
        (self.src / "ops" / "AGENTS.md").write_text("template agents")
        (self.src / "ops" / "coding-backend" / "wo-1").mkdir(parents=True)
        (self.src / "ops" / "coding-backend" / "wo-1" / "x.py").write_text("checkout")
        (self.src / "README.md").write_text("root")

    def run_seed(self) -> subprocess.CompletedProcess:
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_BUCKET_DIR": str(self.bucket),
            "FAKE_AWS_LOG": str(self.log),
        }
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), "--src", str(self.src), "--backend", "s3", "--bucket", "ws"],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_uploads_missing_and_keeps_agent_edits(self) -> None:
        edited = self.bucket / "workspaces" / "ops" / "MEMORY.md"
        edited.parent.mkdir(parents=True)
        edited.write_text("agent edited memory")

        result = self.run_seed()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"uploaded": 2, "kept": 1})
        self.assertEqual(edited.read_text(), "agent edited memory")
        self.assertEqual((self.bucket / "workspaces" / "ops" / "AGENTS.md").read_text(), "template agents")
        self.assertTrue((self.bucket / "workspaces" / "README.md").is_file())
        self.assertFalse((self.bucket / "workspaces" / "ops" / "coding-backend").exists())

        again = self.run_seed()
        self.assertEqual(json.loads(again.stdout), {"uploaded": 0, "kept": 3})
        cps = [json.loads(line) for line in self.log.read_text().splitlines() if '"cp"' in line]
        self.assertEqual(len(cps), 2)

    def test_head_errors_other_than_404_fail(self) -> None:
        aws = self.bin / "aws"
        aws.write_text("#!/bin/sh\necho 'AccessDenied (403)' >&2\nexit 255\n")
        result = self.run_seed()
        self.assertEqual(result.returncode, 1)
        self.assertIn("head-object failed", result.stderr)

    def test_phase_10b_contract(self) -> None:
        text = (ROOT / "scripts" / "phases" / "10b-render-workspace.sh").read_text()
        self.assertIn('OBJECT_BACKEND="${TS_HARNESS_OBJECT_BACKEND:-local}"', text)
        self.assertIn('"$OBJECT_BACKEND" == "s3" || "$OBJECT_BACKEND" == "gcs"', text)
        self.assertIn("scripts/ops/seed-workspace-objects.py", text)
        self.assertIn('"object-backend:${OBJECT_BACKEND}:${TS_HARNESS_WORKSPACE_BUCKET:-}"', text)
        self.assertLess(text.index('python3 "$RENDERER"'), text.index("seed-workspace-objects.py"))
        self.assertLess(text.index("seed-workspace-objects.py"), text.index('write_phase_marker "$PHASE_DONE"'))


if __name__ == "__main__":
    unittest.main()
