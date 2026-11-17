from __future__ import annotations

import unittest

from ts_harness.integrations.adf import adf_from_text
from ts_harness.jira.filing import build_dependency_links, build_story_payload
from ts_harness.jira.spec_hydrate import GOVERNED_DOCS_MISSING, hydrate_spec_pack, open_blockers


class MemoryStub:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def get_program(self, program_id: str) -> dict:
        self.seen = program_id
        return self.payload


def _issue(description: str | dict, links: list | None = None) -> dict:
    return {
        "key": "PROJ-184",
        "fields": {
            "description": description,
            "issuelinks": links or [],
        },
    }


class SpecHydrateTests(unittest.TestCase):
    def test_full_pointers(self):
        desc = (
            "WO: WO-003\nRepo: org/checkout-service@dev\nProgram: prog-1\n"
            "Library: https://drive.google.com/f\nManifest: https://docs.google.com/m\n"
        )
        pack = hydrate_spec_pack(_issue(desc))
        self.assertEqual(pack.source, "pointer")
        self.assertEqual(pack.pointer.work_order_id, "WO-003")
        self.assertEqual(pack.base_branch, "dev")
        self.assertIsNone(pack.warning)

    def test_program_id_hits_drive_library(self):
        client = MemoryStub(
            {
                "program": {
                    "metadata": {
                        "artifacts": {
                            "drive_library": {"root_web_view_link": "https://drive.google.com/p"}
                        }
                    }
                }
            }
        )
        pack = hydrate_spec_pack(
            _issue("WO: WO-001\nRepo: org/svc@dev\nProgram: prog-9\n"),
            client,
        )
        self.assertEqual(pack.source, "drive_library")
        self.assertEqual(pack.drive_library["root_web_view_link"], "https://drive.google.com/p")
        self.assertEqual(client.seen, "prog-9")

    def test_adf_description_is_rendered_before_parsing(self):
        body = "## Summary\nRetry the cart.\n\nWO: WO-004\nRepo: org/checkout-service@dev\nProgram: prog-2"
        pack = hydrate_spec_pack(_issue(adf_from_text(body)))
        self.assertEqual(pack.pointer.work_order_id, "WO-004")
        self.assertEqual(pack.pointer.program_id, "prog-2")
        self.assertIn("## Summary", pack.issue_body)

    def test_no_pointers_uses_jira_body(self):
        pack = hydrate_spec_pack(_issue("Fix the cart retry in checkout."))
        self.assertEqual(pack.source, "jira_body")
        self.assertEqual(pack.warning, GOVERNED_DOCS_MISSING)
        self.assertEqual(pack.base_branch, "dev")

    def test_open_blockers_skips_done(self):
        links = [
            {
                "type": {"inward": "is blocked by", "outward": "blocks"},
                "inwardIssue": {
                    "key": "PROJ-188",
                    "fields": {"status": {"name": "In Progress"}},
                },
            },
            {
                "type": {"inward": "is blocked by", "outward": "blocks"},
                "inwardIssue": {
                    "key": "PROJ-10",
                    "fields": {"status": {"name": "Done", "statusCategory": {"key": "done"}}},
                },
            },
        ]
        self.assertEqual(open_blockers(links), ["PROJ-188"])

    def test_story_payload_has_pointer_and_no_assignee(self):
        payload = build_story_payload(
            project_key="PROJ",
            summary="Add cart retry",
            acceptance="Retry failed cart writes.",
            pointer={
                "work_order_id": "WO-003",
                "repo_id": "org/checkout-service",
                "base_branch": "dev",
                "program_id": "prog-1",
                "library_url": "https://drive.google.com/f",
                "manifest_url": "https://docs.google.com/m",
            },
            epic_key="PROJ-100",
        )
        fields = payload["fields"]
        self.assertNotIn("assignee", fields)
        self.assertIn("WO: WO-003", fields["description"])
        self.assertEqual(fields["issuetype"]["name"], "Story")
        self.assertEqual(fields["parent"]["key"], "PROJ-100")

    def test_dependency_links(self):
        links = build_dependency_links(
            {"WO-001": "PROJ-1", "WO-002": "PROJ-2"},
            {"WO-002": ["WO-001"]},
        )
        self.assertEqual(
            links,
            [{"inward_key": "PROJ-2", "outward_key": "PROJ-1", "type": "Blocks"}],
        )


if __name__ == "__main__":
    unittest.main()
