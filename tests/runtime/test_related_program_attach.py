from __future__ import annotations

import unittest

from ts_harness.workflows.program_brief import (
    attach_related_program_context,
    resolve_related_program_context,
)


class FakeMemory:
    def __init__(self, programs, links=None, briefs=None):
        self.programs = programs
        self.links = links or {}
        self.briefs = briefs or {}

    def list_programs(self, *, tenant_id, project_id=None, limit=100, **kwargs):
        rows = [
            row
            for row in self.programs
            if row.get("tenant_id") == tenant_id
            and (not project_id or row.get("project_id") == project_id)
        ]
        return {"programs": rows}

    def list_program_links(self, program_id):
        return self.links.get(program_id, {"outbound": [], "inbound": []})

    def get_program(self, program_id):
        for row in self.programs:
            if str(row.get("id")) == str(program_id):
                return {"program": row}
        return {"program": {}}

    def list_program_briefs(self, program_id):
        return {"entries": list(self.briefs.get(program_id, []))}


class RelatedProgramAttachTests(unittest.TestCase):
    def test_different_projects_are_isolated(self):
        memory = FakeMemory(
            [
                {"id": "a", "tenant_id": "default", "project_id": "A", "status": "running", "current_phase": "prd"},
                {"id": "b", "tenant_id": "default", "project_id": "B", "status": "running", "current_phase": "prd"},
            ]
        )
        context = resolve_related_program_context(
            memory, tenant_id="default", program_id="a", project_id="A"
        )
        self.assertEqual(context["sources"], [])

    def test_same_project_sees_approved_pointer_and_live_status(self):
        memory = FakeMemory(
            [
                {
                    "id": "a",
                    "tenant_id": "default",
                    "project_id": "TFS",
                    "status": "running",
                    "current_phase": "prd",
                },
                {
                    "id": "b",
                    "tenant_id": "default",
                    "project_id": "TFS",
                    "status": "running",
                    "current_phase": "implement",
                    "excluded_from_autoshare": False,
                },
            ],
            briefs={
                "b": [
                    {
                        "kind": "pointer",
                        "topic": "approved_prd",
                        "content": "Payments v2",
                        "phase": "prd",
                    }
                ]
            },
        )
        context = resolve_related_program_context(
            memory, tenant_id="default", program_id="a", project_id="TFS"
        )
        self.assertEqual(len(context["sources"]), 1)
        source = context["sources"][0]
        self.assertEqual(source["program_id"], "b")
        self.assertEqual(source["via"], "autoshare")
        self.assertEqual(source["live_status"]["state"], "running")
        self.assertEqual(source["brief_entries"][0]["topic"], "approved_prd")

    def test_excluded_omitted_unless_linked(self):
        programs = [
            {"id": "a", "tenant_id": "default", "project_id": "TFS", "status": "running"},
            {
                "id": "b",
                "tenant_id": "default",
                "project_id": "TFS",
                "status": "succeeded",
                "excluded_from_autoshare": True,
            },
        ]
        memory = FakeMemory(programs)
        context = resolve_related_program_context(
            memory, tenant_id="default", program_id="a", project_id="TFS"
        )
        self.assertEqual(context["sources"], [])

        memory.links["a"] = {"outbound": [{"to_program_id": "b"}], "inbound": []}
        context = resolve_related_program_context(
            memory, tenant_id="default", program_id="a", project_id="TFS"
        )
        self.assertEqual(context["sources"][0]["via"], "link")

    def test_one_way_link_across_projects(self):
        memory = FakeMemory(
            [
                {"id": "a", "tenant_id": "default", "project_id": "RFP", "status": "running"},
                {"id": "b", "tenant_id": "default", "project_id": "TFS", "status": "succeeded", "current_phase": "prd"},
            ],
            links={"a": {"outbound": [{"to_program_id": "b"}], "inbound": []}},
        )
        from_a = resolve_related_program_context(
            memory, tenant_id="default", program_id="a", project_id="RFP"
        )
        from_b = resolve_related_program_context(
            memory, tenant_id="default", program_id="b", project_id="TFS"
        )
        self.assertEqual(from_a["sources"][0]["program_id"], "b")
        self.assertEqual(from_b["sources"], [])

    def test_tenant_mismatch_never_appears(self):
        memory = FakeMemory(
            [
                {"id": "a", "tenant_id": "tenant-a", "project_id": "TFS", "status": "running"},
                {"id": "b", "tenant_id": "tenant-b", "project_id": "TFS", "status": "running"},
            ]
        )
        context = resolve_related_program_context(
            memory, tenant_id="tenant-a", program_id="a", project_id="TFS"
        )
        self.assertEqual(context["sources"], [])

    def test_attach_persist_failure_does_not_raise(self):
        memory = FakeMemory(
            [{"id": "a", "tenant_id": "default", "project_id": "TFS", "status": "running"}]
        )

        def boom(*_args, **_kwargs):
            raise RuntimeError("memory down")

        memory.patch_program_artifacts = boom
        context = attach_related_program_context(
            memory,
            program_id="a",
            tenant_id="default",
            project_id="TFS",
        )
        self.assertEqual(context["sources"], [])
        self.assertIn("related_program_context_persist_failed", context["warnings"])


if __name__ == "__main__":
    unittest.main()
