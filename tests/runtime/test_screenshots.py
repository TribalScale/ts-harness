"""Screenshots from a change's Playwright tests: capture config, evidence branch, PR comment, QA images."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ts_harness.agent.loop import _with_images
from ts_harness.implementation import test_services as ts
from ts_harness.implementation.screenshots import COMMENT_MARKER, publish_screenshots

REPO_ROOT = Path(__file__).resolve().parents[2]
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


def _write(path: Path, data: bytes | str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class CaptureTests(unittest.TestCase):
    def test_only_changed_e2e_specs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for path in ("e2e/tests/search.spec.ts", "src/a.test.tsx", "e2e/tests/helpers.ts"):
                _write(repo / path, "x")
            specs = ts.changed_playwright_specs(repo, ["e2e/tests/search.spec.ts", "src/a.test.tsx", "e2e/tests/helpers.ts", "e2e/gone.spec.ts"])
            self.assertEqual(specs, ["e2e/tests/search.spec.ts"])

    def test_evidence_config_keeps_the_repo_config_and_turns_screenshots_on(self):
        text = ts._evidence_config_text(Path("/r/playwright.config.ts"), Path("/out"))
        self.assertIn("import base from './playwright.config';", text)
        self.assertIn("screenshot: 'on'", text)
        self.assertIn('outputDir: "/out"', text)
        self.assertIn("projects: (base.projects ?? []).map", text)

    def test_files_prefer_the_change_over_setup_and_are_named_by_test(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            _write(out / "tests-auth.setup.ts-authenticate-setup" / "test-finished-1.png", PNG)
            _write(out / "tests-discussions-search-search-by-title-chromium" / "test-finished-1.png", PNG)
            files = ts.screenshot_files(out)
            self.assertIn("search", str(files[0]))
            self.assertEqual(ts.screenshot_name(files[0]), "tests-discussions-search-search-by-title-chromium.png")


class FakeGitHub:
    def __init__(self, existing=None):
        self.comments = list(existing or [])
        self.created, self.updated = [], []

    def list_issue_comments(self, owner, repo, number):
        return self.comments

    def comment(self, owner, repo, number, body):
        self.created.append(body)
        return {"html_url": "https://github.com/acme/web/pull/9#issuecomment-1"}

    def update_comment(self, owner, repo, comment_id, body):
        self.updated.append((comment_id, body))
        return {"html_url": f"https://github.com/acme/web/pull/9#issuecomment-{comment_id}"}


class PublishTests(unittest.TestCase):
    def _repo(self, tmp: Path) -> tuple[Path, Path]:
        remote = tmp / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        repo = tmp / "repo"
        subprocess.run(["git", "init", "-q", "-b", "wo/wo-201", str(repo)], check=True)
        _write(repo / "a.txt", "a")
        _git("add", "a.txt", cwd=repo)
        _git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "work", cwd=repo)
        _git("remote", "add", "origin", str(remote), cwd=repo)
        shots = tmp / "shots"
        _write(shots / "tests-search-chromium" / "test-finished-1.png", PNG)
        return repo, remote

    def test_pushes_a_parentless_evidence_commit_and_comments_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, remote = self._repo(Path(tmp))
            github = FakeGitHub()
            result = publish_screenshots(
                repo, Path(tmp) / "shots", work_branch="wo/wo-201", pr_url="https://github.com/acme/web/pull/9", github=github
            )
            self.assertEqual(result["files"], ["tests-search-chromium.png"])
            self.assertEqual(result["branch"], "ts-harness-evidence/wo/wo-201")
            tip = _git("rev-parse", "refs/heads/ts-harness-evidence/wo/wo-201", cwd=remote)
            self.assertEqual(tip, result["commit"])
            self.assertEqual(_git("rev-list", "--count", tip, cwd=remote), "1")
            self.assertEqual(_git("rev-parse", "HEAD", cwd=repo), result["head_sha"])  # work branch untouched
            self.assertIn(f"blob/{result['commit']}/tests-search-chromium.png?raw=true", github.created[0])
            github2 = FakeGitHub(existing=[{"id": 77, "body": COMMENT_MARKER + " old"}])
            publish_screenshots(repo, Path(tmp) / "shots", work_branch="wo/wo-201", pr_url="https://github.com/acme/web/pull/9", github=github2)
            self.assertEqual((github2.created, github2.updated[0][0]), ([], 77))

    def test_nothing_to_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                publish_screenshots(Path(tmp), Path(tmp) / "none", work_branch="b", pr_url="https://github.com/a/b/pull/1", github=FakeGitHub()),
                {},
            )


def _runner_module():
    spec = importlib.util.spec_from_file_location("workflow_runner_screens", REPO_ROOT / "scripts" / "workflow-runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QaImagesTests(unittest.TestCase):
    def test_latest_delivery_screenshots_become_image_parts(self):
        import base64

        class Client:
            def get_file(self, owner, repo, path, ref):
                assert (owner, repo, ref) == ("acme", "web", "abc123")
                return {"content": base64.b64encode(PNG).decode()}

        tasks = [
            {"status": "succeeded", "created_at": "01", "output": {"verify_summary": "x", "screenshots": {"repo": "acme/web", "commit": "old", "files": ["a.png"]}}},
            {"status": "succeeded", "created_at": "02", "output": {"verify_summary": "x", "screenshots": {"repo": "acme/web", "commit": "abc123", "files": ["search.png"], "comment_url": "u"}}},
        ]
        extra = _runner_module().qa_screenshot_input(tasks, github=Client())
        self.assertEqual(extra["screenshots"]["files"], ["search.png"])
        content = _with_images(json.dumps({"input": {}}), extra["_images"])
        self.assertEqual([part["type"] for part in content], ["text", "text", "image_url"])
        self.assertTrue(content[2]["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_no_screenshots_keeps_plain_text(self):
        self.assertEqual(_runner_module().qa_screenshot_input([{"status": "succeeded", "output": {"verify_summary": "x"}}]), {})
        self.assertEqual(_with_images("text", None), "text")


if __name__ == "__main__":
    unittest.main()


class AssertionFrameTests(unittest.TestCase):
    def _trace(self, path: Path, expects: int) -> None:
        import zipfile

        events, resources = [], {}
        for i in range(expects):
            call = f"call@{i}"
            events.append({"type": "before", "callId": call, "method": "expect", "startTime": 100 * i + 10})
            events.append({"type": "screencast-frame", "sha1": f"stale-{i}.jpeg", "timestamp": 100 * i + 20})
            events.append({"type": "after", "callId": call, "endTime": 100 * i + 30})
            # painted just after the assertion passed, before the next action
            events.append({"type": "screencast-frame", "sha1": f"page-{i}.jpeg", "timestamp": 100 * i + 40})
            resources[f"resources/stale-{i}.jpeg"] = b"stale"
            resources[f"resources/page-{i}.jpeg"] = f"frame{i}".encode()
        events.append({"type": "screencast-frame", "sha1": "page-late.jpeg", "timestamp": 99999})
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("0-trace.trace", "\n".join(json.dumps(e) for e in events))
            for name, data in resources.items():
                archive.writestr(name, data)

    def test_frames_at_assertions_spread_and_capped(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "tests-search-chromium"
            out.mkdir()
            self._trace(out / "trace.zip", expects=7)
            frames = ts.assertion_frames(out / "trace.zip", out)
            self.assertEqual([p.read_bytes() for p in frames], [b"frame0", b"frame2", b"frame4", b"frame6"])
            _write(out / "test-finished-1.png", PNG)
            files = ts.screenshot_files(Path(tmp))
            self.assertEqual([p.name for p in files], ["step-1.jpeg", "step-2.jpeg", "step-3.jpeg", "step-4.jpeg"])
            self.assertEqual(ts.screenshot_name(files[0]), "tests-search-chromium-step-1.jpeg")



class QaScreenshotFallbackTests(unittest.TestCase):
    def test_after_a_base_update_with_no_captures_qa_gets_the_last_published_ones(self):
        published = {"repo": "org/web", "files": ["frame-D1.png"], "commit": "abc"}

        class Client:
            def get_file(self, owner, repo, name, ref):
                return {"content": "aGVsbG8="}

        tasks = [
            {"status": "succeeded", "created_at": "1", "output": {"verify_summary": "x", "screenshots": published}},
            {"status": "succeeded", "created_at": "2", "output": {"verify_summary": "x", "screenshots": {}, "refresh_method": "server_merge"}},
        ]
        extra = _runner_module().qa_screenshot_input(tasks, github=Client())
        self.assertEqual(extra["screenshots"]["files"], ["frame-D1.png"])
        self.assertIn("last run that published captures", extra["screenshots"]["note"])
        # A coding run that published nothing does not borrow older pictures.
        tasks[1]["output"].pop("refresh_method")
        self.assertEqual(_runner_module().qa_screenshot_input(tasks, github=Client()), {})
