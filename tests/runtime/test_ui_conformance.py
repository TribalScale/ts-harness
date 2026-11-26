"""UI work orders checked against their design frames: checklists, captures, test rewrites."""

from __future__ import annotations

import base64
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from ts_harness.implementation.coding_job import frame_capture_output
from ts_harness.implementation.frame_checklist import (
    frame_checklist_problems,
    frame_scenario_rule,
    frames_to_capture,
    parse_frame_checklist,
    work_order_frames,
)
from ts_harness.implementation.screenshots import publish_screenshots
from ts_harness.implementation.test_services import _frame_of, _frames_config_text, _report_tests
from ts_harness.implementation.verification import verification_summary
from ts_harness.intake.design import frame_size, link_design_refs
from ts_harness.workflows import program_adapters
from ts_harness.workflows.frame_review import frame_review_input, sample_colours
from ts_harness.workflows.stage_contracts import validate_work_order_artifacts
from ts_harness.workflows.test_changes import test_changes as changed_tests

CHECKLIST = """# WO-003 Week view

Repository: org/web

## Requirements
- Week shows the Carry-over lane.

## Frame checklist

### D1 · 02 Week · Base
- Route: /hub/week
- State: signed in as a planner; test data has placed and carry-over orders
- Shows:
  - content area white (#FFFFFF), area header rows grey (#F3F4F6)
  - cards with a progress bar along the bottom

### D7 · 02 Week · Sources · Carry-over
- Route: `/hub/week?source=carryOver`
- State: the Carry-over chip is selected
- Shows: a Carry-over panel on the right

## Context Graph
- Depends on: WO-001
- Design: D1, D7
"""


def png(
    color: tuple[int, int, int], size=(240, 150), band: tuple[int, int, int] | None = None
) -> bytes:
    image = Image.new("RGB", size, color)
    if band:
        for x in range(size[0]):
            for y in range(40, 70):
                image.putpixel((x, y), band)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class FrameChecklistTests(unittest.TestCase):
    def test_entries_are_parsed_with_route_state_and_shows(self):
        entries = parse_frame_checklist(CHECKLIST)
        self.assertEqual([e.id for e in entries], ["D1", "D7"])
        self.assertEqual(entries[0].route, "/hub/week")
        self.assertEqual(entries[1].route, "/hub/week?source=carryOver")
        self.assertEqual(len(entries[0].shows), 2)
        self.assertEqual(entries[1].shows, ["a Carry-over panel on the right"])

    def test_a_complete_checklist_has_no_problems(self):
        self.assertEqual(frame_checklist_problems(CHECKLIST), [])

    def test_frames_without_a_checklist_are_rejected(self):
        problems = frame_checklist_problems("# WO-004 Day\n\n## Context Graph\n- Design: D2\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("no `## Frame checklist` section", problems[0])

    def test_too_many_frames_missing_and_incomplete_entries(self):
        body = CHECKLIST.replace(
            "- Design: D1, D7", "- Design: D1, D2, D3, D4, D5, D6, D7"
        ).replace("- Route: /hub/week\n", "")
        problems = frame_checklist_problems(body)
        self.assertTrue(any("names 7 design frames" in p for p in problems))
        self.assertTrue(any("no entry for D2, D3, D4, D5, D6" in p for p in problems))
        self.assertTrue(any("entry for D1 has no Route" in p for p in problems))

    def test_backend_work_orders_are_left_alone(self):
        self.assertEqual(frame_checklist_problems("# WO-002 API\n\nRepository: org/api\n"), [])

    def test_the_planner_contract_reports_checklist_problems(self):
        output = {
            "artifacts": {
                "work_orders_markdown": (
                    CHECKLIST.split("## Frame checklist")[0]
                    + "## Acceptance\n- Given a planner, When Week opens, Then the lane shows.\n"
                    * 30
                    + "## Context Graph\n- Design: D2\n"
                ),
                "context_graph_markdown": "## Context graph\n\n- WO-003 depends on WO-001 for the shared grid.\n"
                * 3,
                "execution_phases_markdown": "## Phases\n\n1. WO-001, the shell and grid.\n2. WO-003, the Week view.\n"
                * 3,
            }
        }
        violations = validate_work_order_artifacts({}, output, {"stage": "planner"}, [])
        self.assertTrue(any("Frame checklist" in v.message for v in violations))

    def test_the_coding_prompt_asks_for_a_tagged_scenario_per_frame(self):
        rule = frame_scenario_rule(work_order_frames(CHECKLIST))
        self.assertIn("@frame-D7", rule)
        self.assertIn("Shows: a Carry-over panel on the right", rule)
        self.assertEqual(frame_scenario_rule([]), "")


class FrameSizeTests(unittest.TestCase):
    refs = [
        {
            "id": "D1",
            "title": "02 Week · Base",
            "url": "https://figma/x?node-id=1",
            "summary": "Frame: Week (1440x900)\nText: a",
        },
        {
            "id": "D7",
            "title": "Carry-over",
            "url": "https://figma/x?node-id=7",
            "width": 390,
            "height": 844,
        },
    ]

    def test_size_from_fields_or_summary(self):
        self.assertEqual(frame_size(self.refs[0]), (1440, 900))
        self.assertEqual(frame_size(self.refs[1]), (390, 844))
        self.assertIsNone(frame_size({"summary": "Text: none"}))

    def test_linked_work_orders_carry_sizes_the_capture_reads(self):
        linked = link_design_refs(CHECKLIST, self.refs)
        self.assertIn("· 1440×900", linked)
        frames = frames_to_capture(linked)
        self.assertEqual(
            [(f["id"], f["width"], f["height"]) for f in frames],
            [("D1", 1440, 900), ("D7", 390, 844)],
        )
        self.assertEqual(frames[0]["title"], "02 Week · Base")

    def test_unknown_size_defaults_to_desktop(self):
        frames = frames_to_capture(CHECKLIST)
        self.assertEqual({(f["width"], f["height"]) for f in frames}, {(1440, 900)})


class CaptureReportTests(unittest.TestCase):
    def test_config_sets_the_frame_viewport_and_a_json_report(self):
        text = _frames_config_text(
            Path("playwright.config.ts"), Path("/out"), Path("/out/r.json"), 390, 844
        )
        self.assertIn("viewport: { width: 390, height: 844 }", text)
        self.assertIn("'json'", text)
        self.assertIn("screenshot: 'on'", text)

    def test_report_tests_are_mapped_to_frames_by_tag(self):
        report = {
            "suites": [
                {
                    "title": "features/week.feature",
                    "specs": [
                        {
                            "title": "Carry-over panel",
                            "tags": ["@frame-D7"],
                            "tests": [
                                {
                                    "results": [
                                        {
                                            "status": "passed",
                                            "attachments": [
                                                {
                                                    "name": "screenshot",
                                                    "contentType": "image/png",
                                                    "path": "/x/a.png",
                                                }
                                            ],
                                        }
                                    ]
                                }
                            ],
                        },
                        {
                            "title": "D17 lookalike",
                            "tags": ["@frame-D17"],
                            "tests": [{"results": [{"status": "passed"}]}],
                        },
                    ],
                }
            ]
        }
        tests = _report_tests(report)
        self.assertEqual(tests[0]["screenshot"], "/x/a.png")
        self.assertEqual(_frame_of(tests[0], ["D1", "D7"]), "D7")
        self.assertIsNone(_frame_of(tests[1], ["D1", "D7"]))


class CaptureOutputTests(unittest.TestCase):
    def test_published_captures_point_at_the_evidence_commit(self):
        verification = {
            "frame_captures": [
                {"id": "D1", "title": "Week", "status": "captured", "width": 1440, "height": 900},
                {"id": "D7", "status": "missing", "note": "no scenario tagged @frame-D7"},
                {"id": "D9", "status": "captured"},
            ]
        }
        screenshots = {"repo": "org/web", "commit": "abc", "frames": ["frame-D1.png"]}
        out = frame_capture_output(verification, screenshots)
        self.assertEqual(out[0]["file"], "frame-D1.png")
        self.assertEqual(out[0]["commit"], "abc")
        self.assertEqual(out[1]["status"], "missing")
        self.assertEqual(out[2]["status"], "unpublished")

    def test_verification_summary_names_frames_that_were_not_captured(self):
        text = verification_summary(
            {
                "result": "passed",
                "frame_captures": [
                    {"id": "D1", "status": "captured"},
                    {"id": "D7", "status": "missing", "note": "no scenario"},
                ],
            }
        )
        self.assertIn("1 of 2 design frames captured", text)
        self.assertIn("D7: missing (no scenario)", text)


class FakeStore:
    def __init__(self, blobs):
        self.blobs = blobs

    def get(self, key):
        return self.blobs[key]


class FakeFiles:
    def __init__(self, files):
        self.files = files

    def get_file(self, owner, repo, path, ref=None):
        return {"content": base64.b64encode(self.files[path]).decode()}


class FrameReviewTests(unittest.TestCase):
    def test_sampling_finds_a_different_background_and_header_colour(self):
        design = Image.open(io.BytesIO(png((255, 255, 255), band=(243, 244, 246))))
        capture = Image.open(io.BytesIO(png((247, 245, 238), band=(255, 255, 255))))
        result = sample_colours(design.convert("RGB"), capture.convert("RGB"))
        pairs = {(m["design"], m["capture"]) for m in result["mismatches"]}
        self.assertIn(("#FFFFFF", "#F7F5EE"), pairs)
        self.assertIn(("#F3F4F6", "#FFFFFF"), pairs)

    def test_identical_images_have_no_mismatches(self):
        image = Image.open(io.BytesIO(png((255, 255, 255)))).convert("RGB")
        self.assertEqual(sample_colours(image, image)["mismatches"], [])

    def test_each_frame_is_laid_next_to_its_capture(self):
        refs = [
            {
                "id": "D1",
                "title": "Week",
                "object_key": "design/d1.png",
                "summary": "Frame: Week (240x150)\nFonts: Inter 400 14.0px",
            },
            {"id": "D7", "title": "Carry-over", "object_key": "design/d7.png"},
        ]
        captures = [
            {
                "id": "D1",
                "status": "captured",
                "file": "frame-D1.png",
                "repo": "org/web",
                "commit": "abc",
            },
            {"id": "D7", "status": "missing", "note": "no scenario tagged @frame-D7"},
        ]
        store = FakeStore(
            {"design/d1.png": png((255, 255, 255)), "design/d7.png": png((255, 255, 255))}
        )
        out = frame_review_input(
            refs, captures, store, github=FakeFiles({"frame-D1.png": png((247, 245, 238))})
        )
        self.assertEqual([c["status"] for c in out["frame_captures"]], ["captured", "missing"])
        self.assertEqual([i["name"] for i in out["_images"]], ["frame-D1"])
        self.assertTrue(out["_images"][0]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(out["design_checks"][0]["design_fonts"], "Inter 400 14.0px")
        self.assertEqual(out["design_checks"][0]["colour_mismatches"][0]["design"], "#FFFFFF")

    def test_no_captures_means_no_frame_review(self):
        self.assertEqual(frame_review_input([{"id": "D1"}], None, FakeStore({})), {})


class TestChangesTests(unittest.TestCase):
    PATCH = """@@ -1,8 +1,8 @@
-  it('lets a planner pick up any block and Carry-over card', async () => {
+  it('lets a planner pick up any block, but not a Carry-over card', async () => {
     renderApp();
-    expect(draggable('1001')).toBe('draggable');
+    expect(draggable('1001')).toBeNull();
     expect(screen.getByText('x')).toBeInTheDocument();
"""

    def test_a_flipped_test_and_assertion_are_reversals(self):
        out = changed_tests(
            [
                {
                    "filename": "src/features/moves/MoveDnd.test.tsx",
                    "status": "modified",
                    "patch": self.PATCH,
                }
            ]
        )
        self.assertEqual(len(out), 1)
        reversals = out[0]["reversals"]
        self.assertEqual(len(reversals), 2)
        self.assertIn("but not a Carry-over card", reversals[0]["after"])
        self.assertEqual(reversals[1]["after"], "expect(draggable('1001')).toBeNull();")

    def test_new_test_files_and_source_files_are_ignored(self):
        files = [
            {"filename": "src/a.test.ts", "status": "added", "patch": "+expect(a).toBe(1)"},
            {
                "filename": "src/a.ts",
                "status": "modified",
                "patch": "-expect(a).toBe(1)\n+expect(a).toBe(2)",
            },
        ]
        self.assertEqual(changed_tests(files), [])

    def test_updated_values_are_listed_but_not_reversals(self):
        patch = "-    expect(row.date).toBe('Nov 9');\n+    expect(row.date).toBe('Nov 23');\n"
        out = changed_tests([{"filename": "src/row.test.ts", "status": "modified", "patch": patch}])
        self.assertEqual(out[0]["reversals"], [])
        self.assertEqual(out[0]["removed_assertions"], ["expect(row.date).toBe('Nov 9');"])

    def test_gherkin_scenarios_and_then_steps(self):
        patch = "-  Scenario: A planner can drag a Carry-over card\n+  Scenario: A planner cannot drag a Carry-over card\n"
        out = changed_tests(
            [{"filename": "features/moves.feature", "status": "modified", "patch": patch}]
        )
        self.assertEqual(len(out[0]["reversals"]), 1)


class ScreenSpecPlumbingTests(unittest.TestCase):
    def test_screen_specs_follow_the_prd_into_planning(self):
        tasks = [
            {
                "status": "succeeded",
                "step_name": "draft_screen_specs",
                "output": {"screen_specs_text": "SC1 Week: panel"},
            },
            {
                "status": "succeeded",
                "step_name": "approve_prd",
                "output": {"approved_prd": {"title": "P"}},
            },
        ]
        patch = program_adapters._extract_prd_outputs(workflow_type="prd_request", tasks=tasks)
        self.assertEqual(patch["screen_specs_text"], "SC1 Week: panel")
        intake = program_adapters._build_work_orders_inputs(summary="s", artifacts=patch)
        self.assertEqual(intake["screen_specs_text"], "SC1 Week: panel")
        jira = program_adapters._build_jira_inputs(summary="s", artifacts=patch)
        self.assertEqual(jira["screen_specs_text"], "SC1 Week: panel")

    def test_planner_and_story_steps_map_the_specs(self):
        root = Path(__file__).resolve().parents[2] / "config" / "workflows"
        for name, step in (
            ("work_orders_from_prd.json", "plan_work_orders"),
            ("jira_from_prd.json", "plan_stories"),
        ):
            definition = json.loads((root / name).read_text())
            steps = {s["name"]: s for s in definition["stages"]}
            self.assertEqual(
                steps[step]["input_map"]["screen_specs"], "$workflow.screen_specs_text"
            )


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class FakeGitHub:
    def __init__(self):
        self.created = []

    def list_issue_comments(self, owner, repo, number):
        return []

    def comment(self, owner, repo, number, body):
        self.created.append(body)
        return {"html_url": "https://github.com/org/web/pull/9#c"}


class PublishFramesTests(unittest.TestCase):
    def test_frames_are_published_under_their_own_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            remote = tmp / "remote.git"
            subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
            repo = tmp / "repo"
            subprocess.run(["git", "init", "-q", "-b", "wo/wo-3", str(repo)], check=True)
            (repo / "a.txt").write_text("a")
            _git("add", "a.txt", cwd=repo)
            _git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "w", cwd=repo)
            _git("remote", "add", "origin", str(remote), cwd=repo)
            frames = tmp / "frames"
            frames.mkdir()
            (frames / "frame-D1.png").write_bytes(png((255, 255, 255)))
            (frames / "report-1440x900.json").write_text("{}")
            github = FakeGitHub()
            result = publish_screenshots(
                repo,
                tmp / "no-shots",
                work_branch="wo/wo-3",
                pr_url="https://github.com/org/web/pull/9",
                github=github,
                frames_dir=frames,
            )
            self.assertEqual(result["frames"], ["frame-D1.png"])
            self.assertEqual(result["files"], ["frame-D1.png"])
            self.assertIn("frame-D1.png?raw=true", github.created[0])


if __name__ == "__main__":
    unittest.main()


# --- nightly design conformance -------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

from starlette.testclient import TestClient  # noqa: E402

from ts_harness.workflows import design_conformance as dc  # noqa: E402
from ts_harness.workflows.nightly_smoke import report_token  # noqa: E402

SECRET = "smoke-secret"


class KvMemory:
    def __init__(self):
        self.kv = {}

    def kv_get(self, namespace, key):
        value, version = self.kv.get((namespace, key), (None, 0))
        return {
            "value": json.loads(json.dumps(value)) if value is not None else None,
            "version": version,
        }

    def kv_put(self, namespace, key, value, *, expected_version=None):
        _, version = self.kv.get((namespace, key), (None, 0))
        self.kv[(namespace, key)] = (json.loads(json.dumps(value)), version + 1)
        return version + 1


class Service:
    def __init__(self):
        self.calls = []

    def create_program(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(program_id=f"prog-{len(self.calls)}")


REFS = [
    {
        "id": "D1",
        "title": "02 Week · Base",
        "object_key": "design/d1.png",
        "url": "https://figma/x?node-id=1",
    },
    {
        "id": "D7",
        "title": "Carry-over",
        "object_key": "design/d7.png",
        "url": "https://figma/x?node-id=7",
    },
]


class DesignConformanceTests(unittest.TestCase):
    def setUp(self):
        self.memory = KvMemory()
        body = link_design_refs(CHECKLIST, [{**REFS[0], "summary": "Frame: W (240x150)"}, REFS[1]])
        frames = dc.delivered_frames(body, REFS, work_order_ref="PROJ-12")
        dc.record_frames("org/web", frames, memory=self.memory, now=1000.0)
        self.keys = {f["id"]: f["key"] for f in frames}

    def test_passed_frames_are_listed_with_route_and_size(self):
        manifest = dc.frames_manifest("org/web", memory=self.memory)
        by_id = {f["id"]: f for f in manifest["frames"]}
        self.assertEqual(by_id["D1"]["route"], "/hub/week")
        self.assertEqual((by_id["D1"]["width"], by_id["D1"]["height"]), (240, 150))
        self.assertEqual(by_id["D7"]["route"], "/hub/week?source=carryOver")
        self.assertEqual(dc.frames_manifest("org/other", memory=self.memory), {"frames": []})

    def test_repo_names_from_urls(self):
        self.assertEqual(dc.repo_name("https://github.com/org/web.git"), "org/web")
        self.assertEqual(dc.repo_name("org/web"), "org/web")
        self.assertEqual(dc.repo_name("not a repo"), "")

    def _run(self, files, *, now=2000.0, service=None, token=None):
        manifest = {"frames": files}
        blobs = {
            "manifest.json": json.dumps(manifest).encode(),
            "d1.png": png((247, 245, 238)),
            "d7.png": png((255, 255, 255)),
        }
        store = FakeStore(
            {"design/d1.png": png((255, 255, 255)), "design/d7.png": png((255, 255, 255))}
        )
        body = {
            "repo": "org/web",
            "commit": "abc1234",
            "run_url": "https://github.com/org/web/actions/runs/9",
        }
        return dc.handle_frames_report(
            body,
            token or report_token("org/web", SECRET),
            memory=self.memory,
            service=service or Service(),
            store=store,
            github=FakeFiles(blobs),
            secret=SECRET,
            now=now,
        )

    def test_a_drifted_colour_and_a_failed_route_become_one_proposed_bug(self):
        service = Service()
        result = self._run(
            [
                {"key": self.keys["D1"], "file": "d1.png"},
                {"key": self.keys["D7"], "error": "timeout"},
            ],
            service=service,
        )
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["new_findings"], 2)
        summary = service.calls[0]["summary"]
        self.assertIn("Design #FFFFFF, app #F7F5EE", summary)
        self.assertIn("Could not be captured: timeout", summary)
        self.assertIn("built by PROJ-12", summary)
        self.assertIn("https://github.com/org/web/blob/abc1234/d1.png?raw=true", summary)

    def test_the_same_findings_next_night_are_duplicates(self):
        self._run([{"key": self.keys["D1"], "file": "d1.png"}])
        again = self._run([{"key": self.keys["D1"], "file": "d1.png"}], now=3000.0)
        self.assertEqual(again["status"], "duplicate")

    def test_matching_captures_conform(self):
        result = self._run([{"key": self.keys["D7"], "file": "d7.png"}])
        self.assertEqual(result["status"], "conforms")

    def test_a_wrong_token_is_refused(self):
        from ts_harness.workflows.nightly_smoke import SmokeReportError

        with self.assertRaises(SmokeReportError) as caught:
            self._run([], token="wrong")
        self.assertEqual(caught.exception.status, 401)


class FrameEndpointTests(unittest.TestCase):
    def setUp(self):
        from ts_harness.gateway import asgi_app

        self.runtime = mock.Mock()
        self.runtime.config.gateway.default_tenant_id = "default"
        self.memory = KvMemory()
        dc.record_frames(
            "org/web",
            [
                {
                    "key": "fabc",
                    "id": "D1",
                    "title": "Week",
                    "route": "/hub/week",
                    "width": 1440,
                    "height": 900,
                }
            ],
            memory=self.memory,
        )
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.dict("os.environ", {"SMOKE_REPORT_SECRET": SECRET}),
            mock.patch("ts_harness.memory.client.get_memory_client", return_value=self.memory),
            mock.patch.object(
                dc, "handle_frames_report", return_value={"status": "proposed", "program_id": "p1"}
            ),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    def test_the_frame_list_needs_the_repository_token(self):
        ok = self.client.get(
            "/webhooks/smoke/frames?repo=org/web",
            headers={"Authorization": f"Bearer {report_token('org/web', SECRET)}"},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["frames"][0]["route"], "/hub/week")
        bad = self.client.get(
            "/webhooks/smoke/frames?repo=org/web", headers={"Authorization": "Bearer nope"}
        )
        self.assertEqual(bad.status_code, 401)

    def test_a_report_answers_202_when_a_bug_is_proposed(self):
        response = self.client.post(
            "/webhooks/smoke/frames",
            content=json.dumps({"repo": "org/web", "commit": "abc1234"}),
            headers={"Authorization": "Bearer tok"},
        )
        self.assertEqual(response.status_code, 202)
