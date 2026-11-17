import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation.backends.base import CodingRequest
from ts_harness.implementation.pr_description import pr_body, pr_title

WORK_ORDERS = """# Work Orders: Discussion Categories

# WO-001 Category Attribute

## Summary
Adds the category.

# WO-005 Category Filter in the Discussions List

## Summary
This work order adds the Category filter bar above the list, as D1 shows.

## Requirements
- REQ-1: The filter has four options.

## Out of Scope
- Editing a category.
- Multiple categories.

## E2E Acceptance Tests
- Given the list, When Idea is picked, Then only ideas show.
- Given ?category=bogus, When the page loads, Then All categories is selected (negative).

## Context Graph
- Depends on: WO-003, WO-004
- FRDs: FRD-002
"""


def _request(**overrides):
    values = dict(
        repo_url="https://github.com/o/r",
        base_branch="fe-dev",
        work_order_id="WO-005",
        work_order_body=WORK_ORDERS,
        workspace_dir=Path("/tmp/ws"),
        provider="anthropic",
        program_id="prog-1",
        requested_by="pm@example.com",
        design_refs=[{"id": "D1", "title": "List with filter", "url": "https://www.figma.com/design/k/?node-id=1-2"}],
    )
    values.update(overrides)
    return CodingRequest(**values)


VERIFICATION = {
    "result": "passed",
    "checks": [
        {"check": "unit", "command": "npm test", "exit_code": 0},
        {"check": "e2e", "command": "npx playwright test", "exit_code": 1},
    ],
    "skipped": ["bdd"],
    "screenshots": ["a.png"],
}


class PrDescriptionTests(unittest.TestCase):
    def test_title_uses_the_work_order_heading(self):
        self.assertEqual(pr_title(_request()), "WO-005: Category Filter in the Discussions List")
        self.assertEqual(pr_title(_request(work_order_body="no headings")), "WO-005: implement work order")

    def test_body_has_the_standard_sections_from_the_work_order(self):
        body = pr_body(
            _request(),
            changed_files=["src/a.tsx", "src/b.ts"],
            verification=VERIFICATION,
            security={"secret_scan": {"tool": "gitleaks", "findings": []}},
            docs_written=True,
            screenshots=True,
        )
        self.assertIn("## Summary\n\nThis work order adds the Category filter bar", body)
        self.assertNotIn("Adds the category.", body)  # another work order's summary
        self.assertIn("## Changes (2 files)", body)
        self.assertIn("- `src/a.tsx`", body)
        self.assertIn("Planning specs (Refinery, Foundry, Planner) under `docs/`", body)
        self.assertIn("- [ ] Given the list, When Idea is picked, Then only ideas show.", body)
        self.assertIn("Screenshots from the Playwright tests are in a comment below.", body)
        self.assertIn("## Verification (passed)", body)
        self.assertIn("| unit | `npm test` | passed |", body)
        self.assertIn("| e2e | `npx playwright test` | exit 1 |", body)
        self.assertIn("Not run: bdd", body)
        self.assertIn("## Out of scope\n\n- Editing a category.\n- Multiple categories.", body)
        self.assertIn("- D1: List with filter ([source](https://www.figma.com/design/k/?node-id=1-2))", body)
        self.assertIn("## Work order\n\n- `WO-005`\n- Depends on: WO-003, WO-004\n- FRDs: FRD-002\n", body)
        self.assertIn("- Requested by: pm@example.com", body)
        self.assertIn("- Program: `prog-1`", body)
        self.assertIn("Secret scan (gitleaks): no findings", body)
        self.assertNotIn("Requirements", body)

    def test_repository_template_is_appended(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".github").mkdir()
            (Path(tmp) / ".github" / "pull_request_template.md").write_text("- [ ] Docs updated\n")
            body = pr_body(_request(), changed_files=[], repo_dir=Path(tmp))
        self.assertIn("## Repository checklist\n\n- [ ] Docs updated", body)

    def test_body_without_a_work_order_section_still_reads(self):
        body = pr_body(_request(work_order_body="", design_refs=None), changed_files=["x.py"])
        self.assertIn("Implements work order `WO-005`.", body)
        self.assertIn("the work order lists no separate acceptance tests", body)
