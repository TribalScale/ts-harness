from __future__ import annotations

import unittest

from ts_harness.workflows.program_brief import resolve_related_program_context


class ProgramRelationTests(unittest.TestCase):
    def test_resolve_swallows_client_errors(self):
        class Broken:
            def list_programs(self, **kwargs):
                raise RuntimeError("down")

        context = resolve_related_program_context(
            Broken(), tenant_id="default", program_id="a", project_id="TFS"
        )
        self.assertEqual(context["sources"], [])
        self.assertIn("related_program_context_unavailable", context["warnings"])

    def test_empty_project_skips_autoshare(self):
        class EmptyProject:
            def list_programs(self, **kwargs):
                return {
                    "programs": [
                        {"id": "b", "tenant_id": "default", "project_id": "TFS", "status": "running"}
                    ]
                }

            def list_program_links(self, program_id):
                return {"outbound": [], "inbound": []}

        context = resolve_related_program_context(
            EmptyProject(), tenant_id="default", program_id="a", project_id=None
        )
        self.assertEqual(context["sources"], [])


if __name__ == "__main__":
    unittest.main()
