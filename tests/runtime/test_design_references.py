"""Design references: Figma links and attached images, from the PRD request to coding and QA.

The Figma API is replayed from recorded response shapes (no token, no network); the real
account is checked live once a token exists (docs/runbooks/design-references.md).
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path

import httpx

from ts_harness.agent.loop import _with_images
from ts_harness.implementation.backends.base import CodingRequest
from ts_harness.implementation.backends.opencode import build_implement_prompt
from ts_harness.implementation.coding_job import build_coding_request, coding_request_from_spec
from ts_harness.implementation.design_files import DESIGN_DIR, materialize_design_refs
from ts_harness.implementation.test_env import HARNESS_LOCAL_PATHS
from ts_harness.intake import design
from ts_harness.intake.figma import FigmaClient, FigmaError, FigmaSource, build_figma_source, summarize_frame
from ts_harness.intake.resolve import resolve_intake_sources
from ts_harness.intake.sources import POINTER_KINDS, parse_figma_urls, pointers_from_message
from ts_harness.storage.objects import LocalObjectStore
from ts_harness.workflows import program_adapters
from ts_harness.workflows.design_input import design_input
from ts_harness.workflows.work_order_waves import SHARED_INPUT_KEYS

PNG = b"\x89PNG\r\n\x1a\n" + b"frame-pixels"

FRAME = {
    "id": "12:345",
    "name": "Checkout / Payment",
    "type": "FRAME",
    "absoluteBoundingBox": {"width": 1440, "height": 900},
    "fills": [{"type": "SOLID", "color": {"r": 1, "g": 1, "b": 1}}],
    "children": [
        {"id": "1", "type": "TEXT", "name": "title", "characters": "Pay  now",
         "style": {"fontFamily": "Inter", "fontWeight": 700, "fontSize": 24},
         "fills": [{"type": "SOLID", "color": {"r": 0.1, "g": 0.2, "b": 0.3}}]},
        {"id": "2", "type": "INSTANCE", "name": "Button/Primary", "children": [
            {"id": "3", "type": "TEXT", "characters": "Confirm", "style": {"fontFamily": "Inter", "fontWeight": 500, "fontSize": 16}}
        ]},
        {"id": "4", "type": "TEXT", "characters": "hidden", "visible": False},
    ],
}
FILE = {
    "version": "v2",
    "document": {"id": "0:0", "type": "DOCUMENT", "children": [
        {"id": "0:1", "type": "CANVAS", "children": [FRAME, {**FRAME, "id": "12:400", "name": "Checkout / Done"}]}
    ]},
}


class FakeFigmaApi:
    """Recorded-shape Figma REST responses; counts calls."""

    def __init__(self, *, version: str = "v1", status: int = 200, headers: dict | None = None):
        self.version = version
        self.status = status
        self.headers = headers or {}
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if request.url.host == "figma-alpha-api.s3.us-west-2.amazonaws.com":
            # Each frame renders to its own image (identical images are one reference).
            return httpx.Response(200, content=PNG if path.endswith("12:345.png") else PNG + path.encode())
        assert request.headers.get("X-Figma-Token") == "figd_test"
        if self.status != 200:
            return httpx.Response(self.status, headers=self.headers, json={"err": "nope"})
        if path.endswith("/nodes"):
            node = request.url.params["ids"]
            return httpx.Response(200, json={"version": self.version, "nodes": {node: {"document": {**FRAME, "id": node}}}})
        if path.startswith("/v1/images/"):
            ids = request.url.params["ids"].split(",")
            return httpx.Response(200, json={"err": None, "images": {i: f"https://figma-alpha-api.s3.us-west-2.amazonaws.com/img/{i}.png" for i in ids}})
        if path.startswith("/v1/files/"):
            return httpx.Response(200, json={**FILE, "version": self.version})
        return httpx.Response(404)

    def source(self, store) -> FigmaSource:
        client = FigmaClient("figd_test", http=httpx.Client(transport=httpx.MockTransport(self.handler)))
        return FigmaSource(client, store, tenant_id="default")


def figma_pointer(url: str) -> dict:
    return parse_figma_urls(url)[0]


class FigmaLinkTests(unittest.TestCase):
    def test_links_become_pointers(self):
        self.assertIn("figma", POINTER_KINDS)
        found = pointers_from_message(
            text="Design: https://www.figma.com/design/AbCdEf1234567890/MVP?node-id=12-345&t=x, "
            "file https://figma.com/file/ZZZZZZZZZZZZ/Name and again https://www.figma.com/design/AbCdEf1234567890/MVP?node-id=12-345",
            attachments=[],
        )
        self.assertEqual(
            [(p["kind"], p["file_key"], p["node_id"]) for p in found],
            [("figma", "AbCdEf1234567890", "12:345"), ("figma", "ZZZZZZZZZZZZ", "")],
        )
        self.assertEqual(figma_pointer("https://www.figma.com/proto/QQQQQQQQQQQQ/x?node-id=1%3A2")["node_id"], "1:2")


class FigmaSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalObjectStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_frame_link_renders_one_frame_with_summary(self):
        api = FakeFigmaApi()
        refs = api.source(self.store).frames(figma_pointer("https://www.figma.com/design/AbCdEf1234567890/MVP?node-id=12-345"))
        self.assertEqual(len(refs), 1)
        ref = refs[0]
        self.assertEqual((ref["title"], ref["source"], ref["mime"]), ("Checkout / Payment", "figma", "image/png"))
        self.assertEqual(self.store.get(ref["object_key"]), PNG)
        self.assertTrue(ref["object_key"].startswith("design/default/"))
        self.assertIn("Frame: Checkout / Payment (1440x900)", ref["summary"])
        self.assertIn("Text: Pay now | Confirm", ref["summary"])
        self.assertNotIn("hidden", ref["summary"])
        self.assertIn("Components: Button/Primary", ref["summary"])
        self.assertIn("#ffffff", ref["summary"])
        self.assertIn("Inter 700 24px", ref["summary"])
        self.assertEqual(ref["url"], "https://www.figma.com/design/AbCdEf1234567890/?node-id=12-345")

    def test_same_version_is_fetched_once(self):
        api = FakeFigmaApi(version="v1")
        pointer = figma_pointer("https://www.figma.com/design/AbCdEf1234567890/MVP?node-id=12-345")
        api.source(self.store).frames(pointer)
        first = len(api.calls)
        again = api.source(self.store).frames(pointer)
        # The second request only asks Figma for the node's version: no render, no download.
        self.assertEqual(api.calls[first:], ["/v1/files/AbCdEf1234567890/nodes"])
        self.assertEqual(again[0]["title"], "Checkout / Payment")
        changed = FakeFigmaApi(version="v2")
        changed.source(self.store).frames(pointer)
        self.assertIn("/v1/images/AbCdEf1234567890", changed.calls)

    def test_file_link_takes_top_level_frames(self):
        refs = FakeFigmaApi().source(self.store).frames(figma_pointer("https://www.figma.com/file/ZZZZZZZZZZZZ/Name"))
        self.assertEqual([r["title"] for r in refs], ["Checkout / Payment", "Checkout / Done"])

    def test_errors_explain_themselves(self):
        pointer = figma_pointer("https://www.figma.com/design/AbCdEf1234567890/MVP?node-id=12-345")
        cases = [
            (403, {}, "not shared with the harness's Figma account"),
            (404, {}, "not found"),
            (429, {"Retry-After": "60", "X-Figma-Rate-Limit-Type": "low"}, "rate limit reached (low); retry after 60s"),
        ]
        for status, headers, message in cases:
            with self.assertRaisesRegex(FigmaError, message.replace("(", r"\(").replace(")", r"\)")):
                FakeFigmaApi(status=status, headers=headers).source(self.store).frames(pointer)

    def test_without_a_token_the_link_is_reported_not_fetched(self):
        frames = build_figma_source(store=self.store, tenant_id="default", token="")
        with self.assertRaisesRegex(FigmaError, "not connected"):
            frames({"file_key": "x"})

    def test_summary_caps_text(self):
        many = {**FRAME, "children": [{"type": "TEXT", "characters": f"t{i}"} for i in range(100)]}
        self.assertEqual(summarize_frame(many).count(" | "), 39)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalObjectStore(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def resolve(self, pointers, **kwargs):
        def download(pointer):
            return pointer["name"], pointer["mime"], PNG + pointer["name"].encode()

        return resolve_intake_sources(
            pointers,
            download=download,
            drive=None,
            describe_image=lambda name, mime, data: f"A screen named {name}",
            store_design=lambda name, mime, data: design.store_design_image(self.store, tenant_id="t1", data=data, mime=mime),
            **kwargs,
        )

    def test_attached_images_become_numbered_design_refs(self):
        out = self.resolve(
            [
                {"kind": "slack_file", "file_id": "F1", "name": "home.png", "mime": "image/png", "size": 10},
                {"kind": "slack_file", "file_id": "F2", "name": "notes.txt", "mime": "text/plain", "size": 10},
                {"kind": "slack_file", "file_id": "F3", "name": "cart.png", "mime": "image/png", "size": 10},
            ]
        )
        refs = out["design_refs"]
        self.assertEqual([(r["id"], r["title"], r["source"]) for r in refs], [("D1", "home.png", "upload"), ("D2", "cart.png", "upload")])
        self.assertEqual(refs[0]["summary"], "A screen named home.png")
        self.assertTrue(self.store.get(refs[1]["object_key"]).startswith(PNG))
        self.assertIn("## Design references", out["sources_text"])
        self.assertIn("**D2** cart.png", out["sources_text"])

    def test_figma_link_adds_frames_and_a_text_record(self):
        api = FakeFigmaApi()
        out = self.resolve(
            [figma_pointer("https://www.figma.com/file/ZZZZZZZZZZZZ/Name")],
            figma=api.source(self.store).frames,
        )
        self.assertEqual([r["id"] for r in out["design_refs"]], ["D1", "D2"])
        record = out["sources"][0]
        self.assertEqual(record["kind"], "figma")
        self.assertIsNone(record["error"])
        self.assertIn("Checkout / Done", record["text"])

    def test_figma_without_token_is_an_error_record_not_a_failure(self):
        out = self.resolve(
            [figma_pointer("https://www.figma.com/file/ZZZZZZZZZZZZ/Name")],
            figma=build_figma_source(store=self.store, tenant_id="t1", token=""),
        )
        self.assertEqual(out["design_refs"], [])
        self.assertIn("not connected", out["sources"][0]["error"])
        self.assertIn("Could not be read", out["sources_text"])

    def test_an_image_that_cannot_be_described_is_still_a_design_ref(self):
        def failing(name, mime, data):
            raise RuntimeError("provider down")

        out = resolve_intake_sources(
            [{"kind": "slack_file", "file_id": "F1", "name": "home.png", "mime": "image/png", "size": 10}],
            download=lambda p: (p["name"], p["mime"], PNG),
            drive=None,
            describe_image=failing,
            store_design=lambda name, mime, data: design.store_design_image(self.store, tenant_id="t1", data=data, mime=mime),
        )
        self.assertEqual(out["sources"][0]["error"], "image could not be read")
        self.assertEqual([(r["id"], r["title"], r["summary"]) for r in out["design_refs"]], [("D1", "home.png", "")])

    def test_without_a_store_images_are_only_described(self):
        out = resolve_intake_sources(
            [{"kind": "slack_file", "file_id": "F1", "name": "home.png", "mime": "image/png", "size": 10}],
            download=lambda p: (p["name"], p["mime"], PNG),
            drive=None,
            describe_image=lambda *a: "described",
        )
        self.assertEqual(out["design_refs"], [])
        self.assertEqual(out["sources"][0]["text"], "described")


def ref(i: int, key: str = "", **extra) -> dict:
    return {"id": f"D{i}", "title": f"Screen {i}", "source": "upload", "mime": "image/png",
            "object_key": key or f"design/t1/{i}.png", "summary": f"summary {i}", **extra}


class DesignModuleTests(unittest.TestCase):
    def test_numbering_drops_duplicate_images_and_caps(self):
        raw = [{"object_key": "a"}, {"object_key": "a"}, {"object_key": "b"}] + [{"object_key": str(i)} for i in range(20)]
        numbered = design.number_refs(raw)
        self.assertEqual([r["id"] for r in numbered[:2]], ["D1", "D2"])
        self.assertEqual(len(numbered), design.MAX_DESIGN_REFS)

    def test_work_order_names_its_references(self):
        refs = [ref(1), ref(2), ref(3)]
        table = "# WO-1\n| Field | Value |\n|---|---|\n| Design | D3, D1, D9 |\n"
        bullet = "# WO-2\n## Context Graph\n- Depends on: WO-1\n- Design: D2\n"
        self.assertEqual([r["id"] for r in design.refs_named_by_work_order(refs, table)], ["D3", "D1"])
        self.assertEqual([r["id"] for r in design.refs_named_by_work_order(refs, bullet)], ["D2"])
        self.assertEqual(design.refs_named_by_work_order(refs, "# WO-3\nbackend only"), [])

    def test_store_is_content_addressed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalObjectStore(tmp)
            a = design.store_design_image(store, tenant_id="t 1", data=PNG, mime="image/png")
            b = design.store_design_image(store, tenant_id="t 1", data=PNG, mime="image/png")
            self.assertEqual(a, b)
            self.assertRegex(a, r"^design/t-1/[0-9a-f]{64}\.png$")

    def test_load_images_respects_caps_and_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalObjectStore(tmp)
            refs = [ref(i, design.store_design_image(store, tenant_id="t", data=PNG + bytes([i]) * 1000, mime="image/png")) for i in range(1, 5)]
            images = design.load_images(refs, store, limit=3)
            self.assertEqual(len(images), 3)
            self.assertEqual(images[0]["label"], "Design D1: Screen 1")
            self.assertTrue(images[0]["url"].startswith("data:image/png;base64,"))
            self.assertEqual(len(design.load_images(refs, store, max_bytes=2000)), 1)
            missing = design.load_images([ref(9, "design/t/missing.png")], store)
            self.assertEqual(missing, [])


class DesignInputTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = LocalObjectStore(self.tmp.name)
        self.refs = [ref(i, design.store_design_image(self.store, tenant_id="t", data=PNG + bytes([i]), mime="image/png")) for i in (1, 2, 3)]

    def tearDown(self):
        self.tmp.cleanup()

    def run_step(self, step, *, tasks=None, task_input=None):
        return design_input({"step_name": step}, tasks or [], task_input or {}, store_factory=lambda: self.store)

    def test_prd_steps_see_every_reference_from_the_gather_output(self):
        tasks = [{"status": "succeeded", "created_at": "1", "output": {"design_refs": self.refs}}]
        out = self.run_step("draft_prd", tasks=tasks)
        self.assertEqual([r["id"] for r in out["design_refs"]], ["D1", "D2", "D3"])
        self.assertNotIn("object_key", out["design_refs"][0])
        self.assertEqual(len(out["_images"]), 3)
        self.assertIn("## Design references", out["design_refs_text"])

    def test_planning_steps_read_references_from_the_workflow_input(self):
        out = self.run_step("plan_work_orders", tasks=[{"input": {"design_refs": self.refs}}])
        self.assertEqual(len(out["_images"]), 3)

    def test_coding_gets_only_named_references_with_images_inline(self):
        body = "# WO-7\n- Design: D2\n"
        out = self.run_step("deliver_work_order", task_input={"design_refs": self.refs, "work_order_body": body})
        self.assertEqual([r["id"] for r in out["design_refs"]], ["D2"])
        self.assertTrue(out["design_refs"][0]["data_url"].startswith("data:image/png;base64,"))
        self.assertNotIn("_images", out)

    def test_qa_appends_designs_after_screenshots(self):
        screenshots = [{"name": "step-1.jpeg", "url": "data:image/jpeg;base64,AAAA"}]
        body = "# WO-7\n| Design | D1, D3 |\n"
        out = self.run_step("qa_gate", task_input={"design_refs": self.refs, "work_order_body": body, "_images": screenshots})
        self.assertEqual([i["name"] for i in out["_images"]], ["step-1.jpeg", "D1 Screen 1", "D3 Screen 3"])
        self.assertIn("compare the screenshots", out["design_refs_text"])

    def test_steps_without_references_or_unrelated_steps_get_nothing(self):
        self.assertEqual(self.run_step("draft_prd"), {})
        self.assertEqual(self.run_step("approve_prd", task_input={"design_refs": self.refs}), {})
        self.assertEqual(self.run_step("deliver_work_order", task_input={"design_refs": self.refs, "work_order_body": "# WO-1"}), {})

    def test_a_missing_store_still_sends_the_text(self):
        def broken():
            raise RuntimeError("no bucket")

        out = design_input({"step_name": "draft_prd"}, [], {"design_refs": self.refs}, store_factory=broken)
        self.assertEqual(len(out["design_refs"]), 3)
        self.assertNotIn("_images", out)


class ProgramPlumbingTests(unittest.TestCase):
    def test_prd_outputs_carry_design_refs_into_planning_and_delivery(self):
        refs = [ref(1)]
        tasks = [
            {"status": "succeeded", "step_name": "gather_sources", "output": {"design_refs": refs, "sources_text": "x"}},
            {"status": "succeeded", "step_name": "approve_prd", "output": {"approved_prd": {"title": "P"}}},
        ]
        patch_out = program_adapters._extract_prd_outputs(workflow_type="prd_request", tasks=tasks)
        self.assertEqual(patch_out["design_refs"], refs)
        self.assertIn("design_refs", program_adapters.ADAPTER_DECLARED_OUTPUT_KEYS["extract_prd_outputs"])
        planning = program_adapters._build_work_orders_inputs(summary="s", artifacts={"design_refs": refs})
        self.assertEqual(planning["design_refs"], refs)
        implement = program_adapters._build_implement_inputs(summary="s", artifacts={"design_refs": refs})
        self.assertEqual(implement["design_refs"], refs)
        self.assertIn("design_refs", SHARED_INPUT_KEYS)


class CodingWorkspaceTests(unittest.TestCase):
    def test_request_and_spec_carry_design_refs(self):
        from tests.runtime.test_coding_job_request import configured

        data_url = "data:image/png;base64," + base64.b64encode(PNG).decode()
        with ExitStack() as stack:
            configured(stack)
            spec = build_coding_request(
                {"work_order_id": "WO-2", "work_order_body": "# WO-2\n- Design: D1", "design_refs": [{"id": "D1", "data_url": data_url}]},
                delivery_workflow_id=None,
            )
        self.assertEqual(json.loads(json.dumps(spec))["design_refs"][0]["id"], "D1")
        self.assertIsInstance(coding_request_from_spec(spec), CodingRequest)
        self.assertEqual(coding_request_from_spec(spec).design_refs[0]["data_url"], data_url)

    def test_images_land_outside_the_change_and_in_the_prompt(self):
        self.assertIn(".ts-harness/", HARNESS_LOCAL_PATHS)
        data_url = "data:image/png;base64," + base64.b64encode(PNG).decode()
        with tempfile.TemporaryDirectory() as tmp:
            files = materialize_design_refs(
                Path(tmp),
                [
                    {"id": "D1", "title": "Checkout", "summary": "Frame: Checkout\nText: Pay", "data_url": data_url, "url": "https://figma"},
                    {"id": "D2", "title": "Broken", "data_url": "data:image/png;base64,***"},
                ],
            )
            self.assertEqual((Path(tmp) / DESIGN_DIR / "D1.png").read_bytes(), PNG)
            self.assertTrue((Path(tmp) / DESIGN_DIR / "README.md").is_file())
            self.assertEqual(files[1]["path"], "")
        prompt = build_implement_prompt("WO-2", "# WO-2\nbody", design_files=files)
        self.assertIn("D1 Checkout: .ts-harness/design/D1.png (https://figma)", prompt)
        self.assertIn("Text: Pay", prompt)
        self.assertIn("image unavailable", prompt)
        self.assertIn("Do not copy these images into the repo", prompt)
        self.assertNotIn("Design references", build_implement_prompt("WO-2", "# WO-2\nbody"))


class AgentImageLabelTests(unittest.TestCase):
    def test_design_images_are_labelled_and_screenshots_keep_theirs(self):
        parts = _with_images("task", [
            {"name": "step-1.jpeg", "url": "data:image/jpeg;base64,AA"},
            {"name": "D1 Checkout", "label": "Design D1: Checkout", "url": "data:image/png;base64,AA"},
        ])
        texts = [p["text"] for p in parts if p["type"] == "text"]
        self.assertEqual(texts, ["task", "Screenshot: step-1.jpeg", "Design D1: Checkout"])


if __name__ == "__main__":
    unittest.main()
