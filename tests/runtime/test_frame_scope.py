"""Frame scope: a ticket's design check judges only the part of a shared frame it owns."""

from __future__ import annotations

from ts_harness.implementation.frame_scope import (
    amendment_sections,
    attribute_mismatch,
    frame_scope_problems,
    parse_frame_scope,
    regions_overlap,
    scope_design_checks,
    scope_items,
    ticket_overlaps,
    ticket_scope_sections,
)
from ts_harness.workflows.stage_contracts import validate_work_order_artifacts

BLOCKS = """# PROJ-11 Schedule blocks

## In Scope
- schedule blocks on the week grid
- block progress bars

- Design: D1
"""
ROWS = """# PROJ-12 Grid rows

**In scope:** grid rows and their tones; row surfaces grey (#F3F4F6)

Design: D1, D2
"""
HEADER = """# PROJ-13 Week header and toolbar
Files: src/Header.tsx, src/Toolbar.tsx
Design: D1
"""
SIBLINGS = [
    {"ref": "PROJ-12", "title": "Grid rows", "body": ROWS},
    {"ref": "PROJ-13", "title": "Week header and toolbar", "body": HEADER},
]


def test_scope_items_come_from_in_scope_files_or_title() -> None:
    assert scope_items(BLOCKS) == ["schedule blocks on the week grid", "block progress bars"]
    assert scope_items(ROWS) == ["grid rows and their tones", "row surfaces grey (#F3F4F6)"]
    assert scope_items(HEADER) == ["files: src/Header.tsx, src/Toolbar.tsx"]
    assert scope_items("# PROJ-9 Settings page\n\nSome text") == ["Settings page"]


def test_ticket_sharing_a_frame_gets_shared_frames_and_frame_scope() -> None:
    text, shared = ticket_scope_sections("PROJ-11", BLOCKS, SIBLINGS)
    assert [item["frame"] for item in shared] == ["D1"]
    assert "## Shared frames" in text
    assert "  - PROJ-12: grid rows and their tones; row surfaces grey (#F3F4F6)" in text
    assert "  - PROJ-13: files: src/Header.tsx, src/Toolbar.tsx" in text
    scope = parse_frame_scope(BLOCKS + "\n" + text)["D1"]
    assert scope.owns == ["schedule blocks on the week grid", "block progress bars"]
    assert [o["owner"] for o in scope.others] == ["PROJ-12", "PROJ-13"]


def test_ticket_with_no_shared_frame_gets_nothing() -> None:
    text, shared = ticket_scope_sections("PROJ-11", BLOCKS, [{"ref": "PROJ-14", "title": "x", "body": "Design: D4"}])
    assert (text, shared) == ("", [])
    assert ticket_scope_sections("PROJ-11", "no design", SIBLINGS) == ("", [])


def test_existing_frame_scope_is_kept() -> None:
    body = BLOCKS + "\n## Frame scope\n\n### D1\n- Owns: blocks only\n- Owned by other tickets: PROJ-12: rows\n"
    text, _ = ticket_scope_sections("PROJ-11", body, SIBLINGS)
    assert "## Shared frames" in text and "## Frame scope" not in text
    assert parse_frame_scope(body)["D1"].owns == ["blocks only"]
    assert parse_frame_scope(body)["D1"].others == [{"owner": "PROJ-12", "region": "rows"}]


def test_overlap_between_tickets_is_a_warning() -> None:
    clash = "# PROJ-15 Toolbar polish\n\n## In Scope\n- week header and toolbar\n\nDesign: D1\n"
    _, shared = ticket_scope_sections(
        "PROJ-15", clash, [{"ref": "PROJ-13", "title": "", "body": "# PROJ-13 x\n## In Scope\n- toolbar\n\nDesign: D1"}]
    )
    warnings = ticket_overlaps("PROJ-15", clash, shared)
    assert warnings and "PROJ-13" in warnings[0] and "D1" in warnings[0]
    assert regions_overlap("toolbar", "week header and toolbar")
    assert not regions_overlap("grid rows", "schedule blocks")


PLAN = """# Work orders

## WO-001 Schedule blocks
- Design: D1

## Frame checklist

### D1 · Week
- Route: /hub/week
- State: planner signs in
- Shows:
  - blocks

{scope1}

## WO-002 Grid rows
- Design: D1

## Frame checklist

### D1 · Week
- Route: /hub/week
- State: planner signs in
- Shows:
  - rows grey (#F3F4F6)

{scope2}
"""


def test_planner_rejects_shared_frame_without_scope() -> None:
    problems = frame_scope_problems(PLAN.format(scope1="", scope2=""))
    assert len(problems) == 2
    assert "WO-001 names design frame D1, which WO-002 also names" in problems[0]


def test_planner_rejects_two_work_orders_claiming_one_region() -> None:
    s1 = "## Frame scope\n\n### D1\n- Owns:\n  - schedule blocks\n  - toolbar\n- Owned by other work orders:\n  - WO-002: grid rows"
    s2 = "## Frame scope\n\n### D1\n- Owns:\n  - grid rows and tones\n  - week toolbar\n- Owned by other work orders:\n  - WO-001: blocks"
    problems = frame_scope_problems(PLAN.format(scope1=s1, scope2=s2))
    assert len(problems) == 1 and "both claim the same region" in problems[0]
    fine = s2.replace("  - week toolbar\n", "")
    assert frame_scope_problems(PLAN.format(scope1=s1, scope2=fine)) == []
    # The planner contract carries the problem.
    output = {
        "artifacts": {
            "work_orders_markdown": PLAN.format(scope1=s1, scope2=s2)
            + "## Acceptance\n- Given a planner, When Week opens, Then the blocks show.\n" * 30,
            "context_graph_markdown": "## Context graph\n\n- WO-002 depends on WO-001 for the shared grid.\n" * 3,
            "execution_phases_markdown": "## Phases\n\n1. WO-001, the blocks.\n2. WO-002, the rows.\n" * 3,
        }
    }
    violations = validate_work_order_artifacts({}, output, {"stage": "planner"}, [])
    assert any("both claim the same region" in v.message for v in violations)


def test_colour_mismatch_in_a_region_another_ticket_owns_is_not_blocking() -> None:
    body = PLAN.format(scope1="", scope2="").split("## WO-002")[0] + (
        "## Frame scope\n\n### D1\n- Owns:\n  - schedule blocks\n- Owned by other tickets:\n  - PROJ-12: grid rows and their tones\n"
    )
    checks = [
        {"id": "D1", "colour_mismatches": [{"design": "#F3F4F6", "capture": "#FFFFFF", "points": 30}]},
        {"id": "D2", "colour_mismatches": [{"design": "#111111", "capture": "#222222", "points": 5}]},
    ]
    # The checklist line naming the colour does not say whose it is: owned.
    scoped = scope_design_checks(checks, body)
    assert scoped[0]["colour_mismatches"][0]["owned"] is True
    assert "scope_note" in scoped[0]
    # The checklist line names grid rows, which PROJ-12 owns.
    body2 = body.replace("  - blocks\n", "  - blocks\n  - grid rows grey (#F3F4F6)\n", 1)
    scoped = scope_design_checks(checks, body2)
    assert scoped[0]["colour_mismatches"][0]["owned"] is False
    assert scoped[0]["colour_mismatches"][0]["owner"] == "PROJ-12"
    assert scoped[1]["colour_mismatches"][0]["owned"] is True


def test_scope_amendment_attributes_a_colour_to_another_ticket() -> None:
    body = BLOCKS + "\n## Scope amendment (by lee@example.com, 2026-11-20)\n\nRow surfaces #F3F4F6 belong to PROJ-12.\n"
    assert amendment_sections(body)[0].startswith("## Scope amendment (by lee@example.com")
    verdict = attribute_mismatch("#F3F4F5", scope=None, shows=[], amendments=amendment_sections(body), own_ref="PROJ-11")
    assert verdict["owned"] is False and verdict["owner"] == "PROJ-12"
    checks = [{"id": "D1", "colour_mismatches": [{"design": "#F3F4F6", "capture": "#FFFFFF", "points": 30}]}]
    assert scope_design_checks(checks, body, own_ref="PROJ-11")[0]["colour_mismatches"][0]["owned"] is False
    # Nothing to scope: the checks come back as they were.
    assert scope_design_checks(checks, BLOCKS) == checks


def test_design_conformance_names_every_ticket_of_a_shared_frame() -> None:
    from ts_harness.workflows import design_conformance as dc

    class Kv:
        def __init__(self) -> None:
            self.value, self.version = None, 0

        def kv_get(self, namespace: str, key: str) -> dict:
            return {"value": self.value, "version": self.version}

        def kv_put(self, namespace: str, key: str, value: object, *, expected_version: int | None = None) -> int:
            self.value, self.version = value, self.version + 1
            return self.version

    body = (
        "- Design: D1\n\n## Frame checklist\n\n### D1 · Week\n- Route: /hub/week\n- State: s\n- Shows:\n  - blocks\n\n"
        "## Frame scope\n\n### D1\n- Owns:\n  - schedule blocks\n- Owned by other tickets:\n  - PROJ-12: grid rows\n"
        "  - PROJ-13: toolbar\n"
    )
    refs = [{"id": "D1", "title": "Week", "object_key": "design/d1.png"}]
    memory = Kv()
    frames = dc.delivered_frames(body, refs, work_order_ref="PROJ-11")
    assert frames[0]["owns"] == ["schedule blocks"] and frames[0]["shared_with"] == ["PROJ-12", "PROJ-13"]
    dc.record_frames("org/web", frames, memory=memory, now=1.0)
    dc.record_frames("org/web", [{**frames[0], "work_order_ref": "PROJ-12", "shared_with": ["PROJ-11"]}], memory=memory, now=2.0)
    recorded = next(iter(memory.value["frames"].values()))
    assert recorded["work_order_refs"] == ["PROJ-11", "PROJ-12"]
    assert recorded["shared_with"] == ["PROJ-13"]
    summary = dc.bug_summary("org/web", "", [{**recorded, "kind": "not_captured", "detail": "timeout"}])
    assert "built in parts by PROJ-11, PROJ-12, PROJ-13" in summary
    assert "not delivered yet: PROJ-13" in summary


def test_one_test_data_defect_covers_every_criterion_it_lists() -> None:
    from ts_harness.workflows.stage_contracts import validate_qa_pr_url_set

    pr = "https://github.com/org/web/pull/7"
    failed = [{"criterion": "matches D1", "status": "fail"}, {"criterion": "late orders stack", "status": "fail"}]
    one = {"category": "test_data", "summary": "the frame scenario opens the plan while it loads",
           "criteria": ["matches D1", "late orders stack"], "fix": "seed late and stacked orders in the week scenario"}

    def output(defects: list) -> dict:
        return {"status": "failed", "result": {"prs": [
            {"pr_url": pr, "work_item_ref": "PROJ-11", "status": "failed", "acceptance_criteria": failed, "defects": defects}]}}

    def messages(defects: list) -> list[str]:
        return [v.message for v in validate_qa_pr_url_set({}, output(defects), {"pr_url": pr}, [])]

    assert not any("defect(s)" in m for m in messages([one]))
    assert any("defect(s)" in m for m in messages(["only one plain defect"]))


def test_coding_prompt_builds_only_the_owned_part() -> None:
    from ts_harness.implementation.frame_checklist import frame_scenario_rule, work_order_frames

    body = PLAN.format(scope1="", scope2="").split("## WO-002")[0] + (
        "## Frame scope\n\n### D1\n- Owns:\n  - schedule blocks\n- Owned by other tickets:\n  - PROJ-12: grid rows\n"
    )
    rule = frame_scenario_rule(work_order_frames(body), parse_frame_scope(body))
    assert "Frame D1 is shared with other tickets. Build only the part this work order owns: schedule blocks." in rule
    assert "Leave the rest to its owners (PROJ-12: grid rows)" in rule
    assert "Build only" not in frame_scenario_rule(work_order_frames(body))


def test_queue_start_names_shared_frames_and_warns_on_overlap() -> None:
    from ts_harness.jira.ticket_pool import frame_overlap_notes

    def issue(summary: str, description: str) -> dict:
        return {"fields": {"summary": summary, "description": description}}

    issues = {
        "PROJ-11": issue("Schedule blocks", "## In Scope\n- schedule blocks\n\nDesign: D1"),
        "PROJ-12": issue("Grid rows", "## In Scope\n- grid rows and tones\n- week toolbar\n\nDesign: D1"),
        "PROJ-13": issue("Header", "## In Scope\n- week header and toolbar\n\nDesign: D1"),
        "PROJ-14": issue("Settings", "Design: D2"),
    }
    shared, overlaps = frame_overlap_notes(issues)
    assert shared == ["D1 is built in parts by PROJ-11, PROJ-12 and PROJ-13: each ticket's design check covers only its own part"]
    assert len(overlaps) == 1 and "PROJ-12 and PROJ-13" in overlaps[0]
