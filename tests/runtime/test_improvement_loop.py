"""Self-improvement levels 2-4: the weekly digest and its proposals, offline replay, canaries."""

from __future__ import annotations

import copy
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tests.pg_support import PgTestCase, requires_pg

from ts_harness.improvement import canary as cn
from ts_harness.improvement import digest as dg
from ts_harness.improvement import loop as lp
from ts_harness.improvement import patches
from ts_harness.improvement import proposals as pp
from ts_harness.improvement import replay as rp
from ts_harness.improvement.budget import BudgetExhausted
from ts_harness.memory.client import MemoryApiConflict

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
SKILL = (REPO_ROOT / pp.GATE_SKILL_PATH).read_text()


def iso(stamp: datetime) -> str:
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def skill_diff(section: str = "Deep checks", line: str = "- check retries for duplicate writes") -> str:
    """A diff adding ``line`` right after the ``## section`` heading of the review skill."""
    lines = SKILL.split("\n")
    at = lines.index(f"## {section}")
    context = lines[at:at + 2]
    return (
        f"--- a/{pp.GATE_SKILL_PATH}\n+++ b/{pp.GATE_SKILL_PATH}\n"
        f"@@ -{at + 1},2 +{at + 1},3 @@\n"
        f" {context[0]}\n+{line}\n {context[1]}\n"
    )


class FakeMemory:
    """The memory API calls the improvement loop makes, in memory."""

    def __init__(self):
        self.kv: dict[tuple[str, str], tuple[object, int]] = {}
        self.proposals: dict[int, dict] = {}
        self.cases: dict[int, dict] = {}
        self.notes: dict[str, dict] = {}
        self.workflows: dict[str, dict] = {}
        self.next_id = 1

    # kv
    def kv_get(self, namespace, key):
        value, version = self.kv.get((namespace, key), (None, 0))
        return {"value": copy.deepcopy(value), "version": version}

    def kv_put(self, namespace, key, value, *, expected_version=None):
        _, version = self.kv.get((namespace, key), (None, 0))
        if expected_version is not None and expected_version != version:
            raise MemoryApiConflict("stale", {"code": "version_conflict"})
        self.kv[(namespace, key)] = (copy.deepcopy(value), version + 1)
        return version + 1

    # proposals
    def list_improvement_proposals(self, *, tenant_id="default", status="", pattern_key="", target="", limit=100):
        wanted = {s for s in status.split(",") if s}
        rows = [p for p in self.proposals.values()
                if (not wanted or p["status"] in wanted) and (not target or p["target"] == target)
                and (not pattern_key or p["pattern_key"] == pattern_key)]
        return copy.deepcopy(sorted(rows, key=lambda p: -p["id"])[:limit])

    def get_improvement_proposal(self, proposal_id, *, tenant_id="default"):
        return copy.deepcopy(self.proposals[int(str(proposal_id).removeprefix("proposal-"))])

    def create_improvement_proposal(self, **body):
        row = {"id": self.next_id, "status": "proposed", "evaluations": [], "history": [], "canary": None,
               "promotion": None, "created_at": iso(NOW), "updated_at": iso(NOW), "created_by": body.get("actor"),
               "evaluation": body.get("evaluation"), "cost_usd": float(body.get("cost_usd") or 0)}
        for key in ("type", "target", "title", "summary", "pattern_key", "metric", "expected_effect", "risk", "change",
                    "evidence", "digest_id"):
            row[key] = copy.deepcopy(body.get(key, "" if key not in {"change", "evidence"} else {}))
        self.proposals[self.next_id] = row
        self.next_id += 1
        return copy.deepcopy(row)

    def update_improvement_proposal(self, proposal_id, **body):
        row = self.proposals[int(proposal_id)]
        if body.get("expected_status") and body["expected_status"] != row["status"]:
            raise MemoryApiConflict("moved", {"code": "status_conflict"})
        status = body.get("status")
        if status and status != row["status"]:
            assert status in pp.TRANSITIONS[row["status"]], (row["status"], status)
            if status in {"canary", "awaiting_approval"} and any(
                    p["target"] == row["target"] and p["status"] in {"canary", "awaiting_approval"} and p["id"] != row["id"]
                    for p in self.proposals.values()):
                raise MemoryApiConflict("live", {"code": "status_conflict"})
            row["history"].append({"from": row["status"], "status": status, "actor": body.get("actor"),
                                   "reason": body.get("reason")})
            row["status"] = status
        if isinstance(body.get("evaluation"), dict):
            row["evaluation"] = copy.deepcopy(body["evaluation"])
            row["evaluations"].append(copy.deepcopy(body["evaluation"]))
        for key in ("canary", "promotion", "change"):
            if isinstance(body.get(key), dict):
                row[key] = copy.deepcopy(body[key])
        row["cost_usd"] = round(row["cost_usd"] + float(body.get("add_cost_usd") or 0), 4)
        return copy.deepcopy(row)

    # replay cases
    def list_replay_cases(self, *, tenant_id="default", kind="pre_push_review", limit=100, inputs=True, workflow_ids=None):
        rows = sorted(self.cases.values(), key=lambda c: str(c.get("gate_at") or ""), reverse=True)
        return copy.deepcopy(rows[:limit])

    def put_replay_case(self, **body):
        existing = next((c for c in self.cases.values() if c["workflow_id"] == body["workflow_id"]), None)
        case_id = existing["id"] if existing else len(self.cases) + 1
        self.cases[case_id] = {**copy.deepcopy(body), "id": case_id, "verdicts": existing["verdicts"] if existing else {}}
        return copy.deepcopy(self.cases[case_id])

    def put_replay_verdict(self, case_id, *, key, verdict, tenant_id="default"):
        self.cases[int(case_id)]["verdicts"][key] = copy.deepcopy(verdict)
        return {"id": case_id, "key": key}

    def prune_replay_cases(self, *, tenant_id="default", days=90):
        return {"deleted": 0, "days": days}

    # notes and workflows
    def list_knowledge_notes(self, *, tenant_id="default", status=None):
        return {"notes": [n for n in self.notes.values() if n["status"] == status]}

    def create_knowledge_note(self, **body):
        note_id = f"note-{len(self.notes) + 1}"
        self.notes[note_id] = {**body, "id": note_id}
        return {"id": note_id}

    def get_knowledge_note(self, note_id):
        return copy.deepcopy(self.notes[note_id])

    def get_workflow(self, workflow_id):
        return copy.deepcopy(self.workflows.get(workflow_id) or {})


class FakeBudget:
    def __init__(self, remaining=100.0):
        self.remaining_usd = remaining

    def can_spend(self, estimate=0.0):
        return self.remaining_usd > 0 and estimate <= self.remaining_usd


class FakeCall:
    """A metered model call: replies from ``reply(prompt)`` and spends ``cost`` each time."""

    def __init__(self, reply, cost=0.1, budget=None):
        self.reply, self.cost, self.budget = reply, cost, budget
        self.spent = 0.0
        self.prompts: list[str] = []
        self.model = "fake/model"

    def __call__(self, prompt):
        if self.budget is not None and self.budget.remaining_usd <= 0:
            raise BudgetExhausted("used up")
        self.prompts.append(prompt)
        self.spent += self.cost
        if self.budget is not None:
            self.budget.remaining_usd -= self.cost
        return self.reply(prompt)


# Diffs


class PatchTests(unittest.TestCase):
    def test_a_diff_applies_and_names_the_changed_sections(self):
        changed = patches.apply(skill_diff())
        before, after = changed[pp.GATE_SKILL_PATH]
        self.assertEqual(before, SKILL)
        self.assertIn("- check retries for duplicate writes", after)
        self.assertEqual(patches.changed_sections(before, after), ["Deep checks"])

    def test_a_hunk_applies_where_its_context_matches_even_if_the_line_number_is_off(self):
        diff = skill_diff().replace("@@ -", "@@ -1", 1)  # line number far off
        self.assertIn("duplicate writes", patches.apply(diff)[pp.GATE_SKILL_PATH][1])

    def test_unsafe_paths_deletions_and_stale_context_are_refused(self):
        for bad in (
            "--- a/../etc/passwd\n+++ b/../etc/passwd\n@@ -1 +1 @@\n-a\n+b\n",
            "--- a/skills/x.md\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n",
            f"--- a/{pp.GATE_SKILL_PATH}\n+++ b/{pp.GATE_SKILL_PATH}\n@@ -1,1 +1,1 @@\n-no such line\n+x\n",
            "not a diff",
        ):
            with self.assertRaises(patches.PatchError):
                patches.apply(bad)

    def test_a_new_file_can_be_added(self):
        diff = "--- /dev/null\n+++ b/skills/learning/new-skill.md\n@@ -0,0 +1,2 @@\n+# New\n+text\n"
        self.assertEqual(patches.apply(diff)["skills/learning/new-skill.md"][1], "# New\ntext\n")


# Screening proposals against the off-limits list


class ScreenTests(unittest.TestCase):
    def test_an_edit_to_a_reviewer_section_is_replayable(self):
        out = pp.screen({"type": "skill_edit", "diff": skill_diff()})
        self.assertEqual(out["type"], "skill_edit")
        self.assertEqual(out["offline_check"], pp.PRE_PUSH_TARGET)
        self.assertEqual(out["target"], pp.PRE_PUSH_TARGET)
        self.assertEqual(out["change"]["sections"][pp.GATE_SKILL_PATH], ["Deep checks"])

    def test_an_edit_to_the_severity_section_needs_a_person(self):
        out = pp.screen({"type": "skill_edit", "diff": skill_diff("Severity", "- minor findings block too"),
                         "summary": "tighten severities"})
        self.assertEqual(out["type"], "needs_design")
        self.assertNotIn("diff", out["change"])
        self.assertEqual(out["change"]["off_limits"]["section"], "Severity")
        self.assertEqual(out["change"]["idea"], "tighten severities")

    def test_an_edit_outside_the_reviewer_sections_has_no_offline_check(self):
        out = pp.screen({"type": "skill_edit", "diff": skill_diff("Start Here", "- read the work order twice")})
        self.assertEqual(out["type"], "skill_edit")
        self.assertEqual(out["offline_check"], "")
        self.assertEqual(out["target"], pp.GATE_SKILL_PATH)

    def test_off_limits_paths_and_settings_become_needs_design(self):
        code = "--- a/runtime/ts_harness/workflows/code_review.py\n+++ b/runtime/ts_harness/workflows/code_review.py\n@@ -1 +1 @@\n-a\n+b\n"
        self.assertEqual(pp.screen({"type": "fragment_change", "diff": code})["type"], "needs_design")
        out = pp.screen({"type": "setting_change", "setting": {"name": "budgets.improvement_monthly_usd", "old": 150, "new": 500}})
        self.assertEqual(out["type"], "needs_design")
        self.assertEqual(pp.screen({"type": "setting_change", "setting": {"name": "TS_HARNESS_STUCK_ARBITER", "old": "on",
                                                                         "new": "off"}})["type"], "needs_design")
        for name in ("TS_HARNESS_CANARY_SHARE", "TS_HARNESS_IMPROVEMENT_MODEL", "ts_harness.improvement.canary:MIN_TICKETS"):
            self.assertEqual(pp.screen({"type": "setting_change", "setting": {"name": name, "old": 1, "new": 2}})["type"],
                             "needs_design", name)

    def test_a_safe_setting_change_keeps_old_and_new(self):
        out = pp.screen({"type": "setting_change", "setting": {"name": "TS_HARNESS_PRE_PUSH_REVIEW_ROUNDS", "old": 2, "new": 3}})
        self.assertEqual(out["type"], "setting_change")
        self.assertEqual(out["change"]["setting"], {"name": "TS_HARNESS_PRE_PUSH_REVIEW_ROUNDS", "old": 2, "new": 3})
        with self.assertRaises(pp.Rejected):
            pp.screen({"type": "setting_change", "setting": {"name": "X", "old": 1, "new": 1}})

    def test_malformed_or_misplaced_changes_are_dropped(self):
        outside = "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n"
        for draft in ({"type": "skill_edit", "diff": outside}, {"type": "skill_edit", "diff": "nonsense"},
                      {"type": "rewrite_everything"}, {"type": "knowledge_note", "note": {"title": "t"}}):
            with self.assertRaises(pp.Rejected):
                pp.screen(draft, reader=lambda path: "a\n")

    def test_a_fragment_change_must_leave_valid_json(self):
        original = '{\n  "a": 1\n}\n'
        diff = "--- a/config/fragments/x.json\n+++ b/config/fragments/x.json\n@@ -1,3 +1,3 @@\n {\n-  \"a\": 1\n+  \"a\": 1,\n }\n"
        with self.assertRaises(pp.Rejected):
            pp.screen({"type": "fragment_change", "diff": diff}, reader=lambda path: original)
        good = diff.replace('+  "a": 1,', '+  "a": 2')
        self.assertEqual(pp.screen({"type": "fragment_change", "diff": good}, reader=lambda path: original)["type"],
                         "fragment_change")

    def test_the_route_lifecycle_matches_the_runtime(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from memory_routes import improvements

        self.assertEqual(improvements.TRANSITIONS, pp.TRANSITIONS)
        self.assertEqual(improvements.TYPES, pp.TYPES)

    def test_the_migration_is_in_the_order(self):
        from tests.pg_support import migration_order

        self.assertIn("056_improvement_proposals.sql", migration_order())


# The digest


def review_task(task_id, at, findings, *, status="blocked", step="finalize_code_review", workflow="wf"):
    return {"id": task_id, "workflow_id": workflow, "step_name": step, "status": status, "created_at": at,
            "input": {"work_order_body": "Requirements: R1"},
            "output": {"verdict": "block" if status == "blocked" else "pass", "findings": findings,
                       "prs": [{"pr_url": f"https://github.com/org/api/pull/{task_id}", "head_sha": f"sha-{task_id}"}]}}


def ticket_run(workflow_id, tasks, *, finished=NOW - timedelta(days=1), outcome="merged", project="PROJ"):
    return {"program_id": f"prog-{workflow_id}", "project": project, "workflow": {"id": workflow_id},
            "tasks": tasks, "ended": {"outcome": outcome, "finished_at": iso(finished)}}


TEST_FINDING = {"severity": "blocker", "category": "tests", "file": "src/a.ts", "message": "no unit test for the retry"}


class NoGuard:
    """A guard context with no lookups (post-merge signals unknown)."""

    days = 14
    now = NOW
    memory = None
    max_lookups = 0

    def allow(self):
        return False

    def jira(self):
        return None


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.memory = FakeMemory()
        self.runs = [ticket_run(f"wf{n}", [review_task(f"t{n}", "2026-03-09T10:00:00Z", [TEST_FINDING])])
                     for n in range(3)]
        self.runs.append(ticket_run("wf9", [review_task("t9", "2026-03-09T10:00:00Z", [
            {"severity": "minor", "category": "maintainability", "message": "naming"}])]))

    def test_signals_cover_review_ci_stuck_arbiter_readiness_and_interventions(self):
        tasks = [
            review_task("a", "2026-03-01T00:00:00Z", [TEST_FINDING]),
            review_task("b", "2026-03-01T01:00:00Z", [TEST_FINDING]),
            {"id": "c", "step_name": "await_pr_checks", "status": "blocked", "created_at": "2026-03-01T02:00:00Z",
             "output": {"failed_checks": ["test (20.x)"]}, "input": {}},
            {"id": "d", "step_name": "deliver_work_order", "status": "blocked", "created_at": "2026-03-01T03:00:00Z",
             "error": "stalled", "input": {"_awaiting_human_decision": {"reason": "revision_budget_exhausted"},
                                          "_arbiter": {"category": "agent_off_track", "reason": "kept editing the wrong file"},
                                          "_decision_log": [{"actor": "lead@example.com", "action": "retry"}]}},
            {"id": "e", "step_name": "prepare_ticket_delivery", "status": "blocked", "created_at": "2026-03-01T04:00:00Z",
             "input": {"_awaiting_human_decision": {"reason": "ticket_not_ready"}},
             "output": {"readiness": {"questions": ["Which currency?"]}}},
        ]
        signals = dg.ticket_signals(tasks)
        self.assertIn("review:tests", signals)
        self.assertIn("repeated:finalize_code_review", signals)
        self.assertEqual(signals["ci:test"], ["test (20.x)"])
        self.assertIn("stuck:revision-budget-exhausted", signals)
        self.assertEqual(signals["arbiter:agent-off-track"], ["kept editing the wrong file"])
        self.assertEqual(signals["readiness:questions"], ["Which currency?"])
        self.assertIn("intervention:decision_retry", signals)

    def test_a_pattern_needs_three_tickets(self):
        tickets = dg.collect(self.memory, now=NOW, runs=self.runs, guard_context=NoGuard())
        found = dg.patterns(tickets)
        self.assertEqual([p["key"] for p in found], ["review:tests"])
        self.assertEqual(found[0]["count"], 3)
        self.assertEqual(found[0]["examples"], ["no unit test for the retry"])

    def test_old_tickets_count_only_for_post_merge_problems(self):
        old = ticket_run("old", [review_task("o", "2026-02-20T00:00:00Z", [TEST_FINDING])], finished=NOW - timedelta(days=12))
        tickets = dg.collect(self.memory, now=NOW, runs=[old], guard_context=NoGuard())
        self.assertEqual(tickets, [])

    def run_digest(self, reply, **kwargs):
        call = FakeCall(reply)
        result = dg.run_digest(memory=self.memory, now=NOW, model_call=call, budget=kwargs.pop("budget", FakeBudget()),
                               runs=self.runs, guard_context=NoGuard(), **kwargs)
        return result, call

    def test_a_replayable_skill_edit_is_stored_with_its_evidence(self):
        reply = json.dumps({"type": "skill_edit", "title": "Ask for a test per retry path", "summary": "s",
                            "metric": "autonomy", "expected_effect": "fewer test blocks", "risk": "more findings",
                            "diff": skill_diff()})
        result, call = self.run_digest(lambda prompt: reply)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["created"]), 1)
        row = self.memory.proposals[1]
        self.assertEqual((row["type"], row["target"], row["status"]), ("skill_edit", pp.PRE_PUSH_TARGET, "proposed"))
        self.assertEqual(row["change"]["offline_check"], pp.PRE_PUSH_TARGET)
        self.assertEqual(row["evidence"]["tickets"], 3)
        self.assertEqual({r["workflow_id"] for r in row["evidence"]["refs"]}, {"wf0", "wf1", "wf2"})
        self.assertEqual(row["pattern_key"], "review:tests")
        self.assertAlmostEqual(row["cost_usd"], 0.1)
        self.assertIsNone(row["evaluation"])
        self.assertIn("## Deep checks", call.prompts[0])  # the reviewer's skill is shown for an exact diff
        self.assertIn("security scan rules", call.prompts[0])  # the off-limits list

    def test_the_digest_runs_once_a_week(self):
        reply = json.dumps({"skip": True, "reason": "nothing to change"})
        self.run_digest(lambda prompt: reply)
        again, call = self.run_digest(lambda prompt: reply)
        self.assertEqual(again["status"], "skipped")
        self.assertEqual(call.prompts, [])

    def test_open_patterns_and_recent_rejections_are_not_proposed_again(self):
        self.memory.create_improvement_proposal(type="skill_edit", target="x", title="Earlier", pattern_key="review:tests",
                                                actor=pp.ACTOR)
        result, call = self.run_digest(lambda prompt: "{}")
        self.assertEqual(call.prompts, [])
        self.assertEqual(result["skipped"][0]["reason"], "covered by an open or recently declined proposal")

    def test_at_most_five_a_week(self):
        for n in range(5):
            self.memory.create_improvement_proposal(type="setting_change", target=f"setting:{n}", title=f"p{n}",
                                                    pattern_key=f"other:{n}", actor=pp.ACTOR)
            self.memory.proposals[n + 1]["status"] = "rejected"
            self.memory.proposals[n + 1]["updated_at"] = iso(NOW - timedelta(days=60))
        result, call = self.run_digest(lambda prompt: "{}")
        self.assertEqual(call.prompts, [])
        self.assertIn("weekly cap", result["skipped"][0]["reason"])

    def test_an_off_limits_proposal_is_stored_as_needs_design(self):
        reply = json.dumps({"type": "skill_edit", "title": "Block on missing tests", "summary": "make it a blocker",
                            "diff": skill_diff("Severity", "- a missing test always blocks")})
        self.run_digest(lambda prompt: reply)
        row = self.memory.proposals[1]
        self.assertEqual(row["type"], "needs_design")
        self.assertEqual(row["evaluation"]["status"], pp.EVAL_NOT_NEEDED)
        self.assertTrue(pp.needs_person(row))

    def test_a_note_goes_through_the_proposed_notes_path(self):
        reply = json.dumps({"type": "knowledge_note", "title": "Test every retry", "summary": "s",
                            "note": {"scope": "project", "kind": "howto", "title": "Test every retry path",
                                     "body": "In org/api, add a unit test for each retry branch.", "why": "w"}})
        self.run_digest(lambda prompt: reply)
        note = self.memory.notes["note-1"]
        self.assertEqual((note["status"], note["source"], note["scope"], note["project_id"]),
                         ("proposed", "drafted", "project", "PROJ"))
        row = self.memory.proposals[1]
        self.assertEqual(row["change"]["note_id"], "note-1")
        self.assertFalse(pp.needs_person(row))  # the note itself is in the Inbox
        self.memory.notes["note-1"]["status"] = "active"
        lp.sync_notes(self.memory, self.memory.list_improvement_proposals(), tenant_id="default")
        self.assertEqual(self.memory.proposals[1]["status"], "promoted")

    def test_a_duplicate_note_is_skipped(self):
        self.memory.notes["n0"] = {"id": "n0", "status": "active", "scope": "org", "title": "Test every retry path",
                                   "body": "x"}
        reply = json.dumps({"type": "knowledge_note", "title": "t",
                            "note": {"scope": "org", "title": "Test every retry path", "body": "In org/api, add tests."}})
        result, _ = self.run_digest(lambda prompt: reply)
        self.assertEqual(result["created"], [])
        self.assertEqual(result["skipped"][0]["reason"], "near-duplicate of an existing note")

    def test_a_proposal_without_an_offline_check_waits_for_a_person(self):
        reply = json.dumps({"type": "setting_change", "title": "One more review round",
                            "setting": {"name": "TS_HARNESS_PRE_PUSH_REVIEW_ROUNDS", "old": 2, "new": 3}})
        self.run_digest(lambda prompt: reply)
        row = self.memory.proposals[1]
        self.assertEqual(row["evaluation"], {"status": pp.EVAL_UNAVAILABLE, "reason": pp.NO_OFFLINE_CHECK})
        self.assertTrue(pp.needs_person(row))

    def test_the_digest_pauses_quietly_when_the_budget_is_used_up(self):
        result, call = self.run_digest(lambda prompt: "{}", budget=FakeBudget(remaining=0))
        self.assertEqual(result["status"], "paused")
        self.assertEqual(call.prompts, [])
        self.assertEqual(self.memory.kv, {})  # not recorded as this week's run


# Replay


def replay_case(case_id, truth, *, at):
    return {"id": case_id, "workflow_id": f"wf{case_id}", "pr_url": f"https://github.com/org/api/pull/{case_id}",
            "truth_verdict": truth, "gate_at": at, "verdicts": {},
            "inputs": {"work_order_id": "WO-1", "work_order_body": "R1", "files": [
                {"filename": "src/a.ts", "status": "modified", "patch": f"@@ case {case_id} {truth} @@"}]}}


def reviewer(prompt: str) -> str:
    """Current rubric: catches nothing. Candidate (has the marker): catches every gate block."""
    blocks = "duplicate writes" in prompt and " block @@" in prompt
    findings = [{"severity": "blocker", "category": "tests", "message": "m"}] if blocks else []
    return json.dumps({"summary": "s", "findings": findings})


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.memory = FakeMemory()
        for n in range(1, 11):
            self.memory.cases[n] = replay_case(n, "block" if n % 2 else "pass", at=f"2026-03-0{n % 9 + 1}T00:00:00Z")
        self.proposal = {"id": 7, "type": "skill_edit", "status": "evaluating", "target": pp.PRE_PUSH_TARGET,
                         "change": {"diff": skill_diff(), "offline_check": pp.PRE_PUSH_TARGET}}

    def test_a_case_comes_from_the_gates_first_verdict(self):
        tasks = [
            {"id": "p", "step_name": "review_precheck", "status": "succeeded", "created_at": "2026-03-01T00:00:00Z",
             "input": {"work_order_body": "Requirements: R1", "work_order_id": "WO-1"},
             "output": {"files": [{"filename": "src/a.ts", "status": "modified", "patch": "+x"}],
                        "scope_findings": [], "test_changes": []}},
            review_task("g1", "2026-03-01T01:00:00Z", [TEST_FINDING, {"severity": "minor", "category": "tests", "message": "nit"}]),
            review_task("g2", "2026-03-02T01:00:00Z", [], status="succeeded"),
        ]
        case = rp.case_from_tasks(tasks)
        self.assertEqual((case["truth_verdict"], case["head_sha"]), ("block", "sha-g1"))
        self.assertEqual(len(case["truth_findings"]), 1)  # blocking findings only
        self.assertEqual(case["inputs"]["files"][0]["filename"], "src/a.ts")
        self.assertEqual(case["inputs"]["work_order_id"], "WO-1")
        self.assertIsNone(rp.case_from_tasks([tasks[0]]))

    def test_cases_without_stored_files_read_the_diff_from_github(self):
        class GitHub:
            def get_pull(self, owner, repo, number):
                return {"base": {"ref": "main"}}

            def compare(self, owner, repo, base, head):
                assert (base, head) == ("main", "sha-g1")
                return {"files": [{"filename": "src/b.ts", "status": "added", "patch": "+y"}]}

        tasks = [review_task("g1", "2026-03-01T01:00:00Z", [TEST_FINDING])]
        tasks[0]["output"]["prs"][0]["pr_url"] = "https://github.com/org/api/pull/12"
        out = rp.build_cases(self.memory, runs=[ticket_run("wfx", tasks)], github=GitHub())
        self.assertEqual(out["built"], 1)
        stored = next(c for c in self.memory.cases.values() if c["workflow_id"] == "wf")
        self.assertEqual(stored["inputs"]["files"], [{"filename": "src/b.ts", "status": "added", "patch": "+y"}])

    def test_balanced_selection(self):
        cases = [replay_case(n, "block", at="x") for n in range(8)] + [replay_case(9, "pass", at="x")]
        chosen = rp.select_cases(cases, 6)
        self.assertEqual(len(chosen), 6)
        self.assertEqual(sum(1 for c in chosen if c["truth_verdict"] == "pass"), 1)

    def test_a_better_candidate_passes_and_verdicts_are_cached(self):
        call = FakeCall(reviewer)
        result = rp.evaluate(self.proposal, self.memory, model_call=call, model="m", now=NOW)
        self.assertEqual(result["status"], pp.EVAL_PASSED, result["reason"])
        self.assertEqual(result["scores"]["candidate"]["catch_rate"], 1.0)
        self.assertEqual(result["scores"]["current"]["catch_rate"], 0.0)
        self.assertEqual(result["scores"]["candidate"]["false_block_rate"], 0.0)
        self.assertEqual(result["scores"]["candidate"]["agreement"], 1.0)
        self.assertEqual(len(call.prompts), 20)
        self.assertAlmostEqual(result["cost_usd"], 2.0)
        again = FakeCall(reviewer)
        self.assertEqual(rp.evaluate(self.proposal, self.memory, model_call=again, model="m", now=NOW)["status"],
                         pp.EVAL_PASSED)
        self.assertEqual(again.prompts, [])  # cached

    def test_more_false_blocks_fail(self):
        def noisy(prompt):
            blocks = "duplicate writes" in prompt
            return json.dumps({"findings": [{"severity": "blocker", "category": "tests", "message": "m"}] if blocks else []})

        result = rp.evaluate(self.proposal, self.memory, model_call=FakeCall(noisy), model="m", now=NOW)
        self.assertEqual(result["status"], pp.EVAL_FAILED)
        self.assertIn("false blocks", result["reason"])

    def test_no_better_catch_rate_fails(self):
        result = rp.evaluate(self.proposal, self.memory, model_call=FakeCall(lambda p: '{"findings": []}'), model="m", now=NOW)
        self.assertEqual(result["status"], pp.EVAL_FAILED)

    def test_too_few_cases_or_no_budget_is_inconclusive(self):
        few = FakeMemory()
        few.cases = {n: replay_case(n, "block", at="x") for n in range(1, 6)}
        self.assertEqual(rp.evaluate(self.proposal, few, model_call=FakeCall(reviewer), model="m")["status"],
                         pp.EVAL_INCONCLUSIVE)
        budget = FakeBudget(remaining=0.5)
        call = FakeCall(reviewer, cost=0.2, budget=budget)
        result = rp.evaluate(self.proposal, self.memory, model_call=call, model="m", budget=budget)
        self.assertEqual(result["status"], pp.EVAL_INCONCLUSIVE)
        self.assertTrue(result["paused"])
        self.assertLessEqual(len(call.prompts), 3)

    def test_a_diff_that_no_longer_applies_fails(self):
        stale = {**self.proposal, "change": {"diff": skill_diff().replace("## Deep checks", "## Gone")}}
        self.assertEqual(rp.evaluate(stale, self.memory, model_call=FakeCall(reviewer))["status"], pp.EVAL_FAILED)


# Canary


def metrics_for(candidate, control):
    def fake(memory, **kwargs):
        def group(value, side):
            finished, autonomous, problems = side
            return {"value": value, "tickets": finished, "summary": {
                "finished": finished, "merged": finished, "autonomy": {"rate": autonomous / finished if finished else None},
                "guard": {"problem_rate": problems / finished if finished else None, "tickets_with_problems": problems,
                          "fully_checked": finished},
                "cost_usd": {"median": 1.0}, "time_to_merge_seconds": {"median": 3600}}}
        fake.calls.append(kwargs)
        return {"groups": [group(cn.CANDIDATE, candidate), group(cn.CONTROL, control)]}
    fake.calls = []
    return fake


class CanaryTests(unittest.TestCase):
    def setUp(self):
        cn.clear_cache()
        self.memory = FakeMemory()
        self.proposal = self.memory.create_improvement_proposal(
            type="skill_edit", target=pp.PRE_PUSH_TARGET, title="Ask for retry tests",
            change={"diff": skill_diff(), "offline_check": pp.PRE_PUSH_TARGET}, actor=pp.ACTOR)
        self.memory.proposals[1]["status"] = "evaluating"
        self.memory.proposals[1]["evaluation"] = {"status": pp.EVAL_PASSED}

    def started(self):
        cn.start(self.memory, self.memory.get_improvement_proposal(1), now=NOW - timedelta(days=3))
        return self.memory.proposals[1]

    def test_the_loop_starts_a_canary_on_its_own(self):
        row = self.started()
        self.assertEqual(row["status"], "canary")
        self.assertEqual(row["canary"]["variant"], "proposal-1")
        self.assertEqual(row["canary"]["share"], 0.5)

    def test_one_live_canary_per_target(self):
        self.started()
        other = self.memory.create_improvement_proposal(type="skill_edit", target=pp.PRE_PUSH_TARGET, title="Other",
                                                        actor=pp.ACTOR)
        self.memory.proposals[other["id"]]["status"] = "evaluating"
        moved = cn.start(self.memory, self.memory.get_improvement_proposal(other["id"]))
        self.assertEqual(moved["status"], "proposed")
        self.assertIn("waiting for proposal 1", moved["history"][-1]["reason"])

    def test_assignment_is_stable_split_and_only_for_new_coding_tickets(self):
        self.started()
        buckets = [cn.bucket(1, f"wf{n}", 0.5) for n in range(400)]
        self.assertTrue(150 < buckets.count(cn.CANDIDATE) < 250)
        self.assertEqual(cn.bucket(1, "wf3", 0.5), cn.bucket(1, "wf3", 0.5))
        self.memory.workflows = {"new": {"workflow": {"created_at": iso(NOW)}},
                                 "old": {"workflow": {"created_at": iso(NOW - timedelta(days=10))}}}
        task = {"workflow_id": "new", "workflow_type": "implement_jira_ticket"}
        assigned = cn.assign_for_task(task, memory=self.memory)
        self.assertEqual(assigned, {"proposal-1": cn.bucket(1, "new", 0.5)})
        self.assertEqual(cn.assign_for_task(task, memory=self.memory), {})  # already assigned
        self.assertEqual(cn.assign_for_task({**task, "workflow_id": "old"}, memory=self.memory), {})
        self.assertEqual(cn.assign_for_task({**task, "workflow_id": "new2", "workflow_type": "intake"}, memory=self.memory), {})

    def test_a_candidate_ticket_reviews_with_the_candidate_rubric(self):
        self.started()
        spec = {"review_rubric": "current rubric", "review_model": "m"}

        def task_input(value):
            return {"_versions": {"variants": {"proposal-1": value}}}

        out = cn.apply_to_spec(spec, task_input(cn.CANDIDATE), memory=self.memory)
        self.assertIn("duplicate writes", out["review_rubric"])
        self.assertIn("## Deep checks", out["review_rubric"])
        self.assertNotIn("## Output Contract", out["review_rubric"])
        self.assertEqual(cn.apply_to_spec(spec, task_input(cn.CONTROL), memory=self.memory), spec)
        cn.rollback(self.memory, self.memory.get_improvement_proposal(1), "test")
        self.assertEqual(cn.apply_to_spec(spec, task_input(cn.CANDIDATE), memory=self.memory), spec)

    def test_with_canary_in_the_coding_stage_never_fails_the_job(self):
        from ts_harness.workflows.coding_job_stage import with_canary

        spec = {"review_rubric": "r"}
        self.assertIs(with_canary(spec, {}), spec)
        with mock.patch("ts_harness.memory.client.get_memory_client", side_effect=RuntimeError("down")):
            self.assertEqual(with_canary(spec, {"_versions": {"variants": {"proposal-1": "candidate"}}}), spec)

    def test_the_stop_rule(self):
        self.assertEqual(cn.verdict(cn._side([], "x") | {"tickets_with_problems": 1}, cn._side([], "y"), age_days=1)[0],
                         "rollback")

        def side(finished, rate, problems=0, problem_rate=0.0):
            return {"finished": finished, "autonomy_rate": rate, "tickets_with_problems": problems,
                    "problem_rate": problem_rate}

        self.assertEqual(cn.verdict(side(5, 1.0), side(9, 0.5), age_days=3)[0], "wait")
        self.assertEqual(cn.verdict(side(5, 1.0), side(9, 0.5), age_days=50)[0], "rollback")
        self.assertEqual(cn.verdict(side(8, 0.5), side(8, 0.75), age_days=9)[0], "rollback")  # 25 points worse
        self.assertEqual(cn.verdict(side(8, 0.625), side(8, 0.75), age_days=9)[0], "approve")  # one ticket in eight
        self.assertEqual(cn.verdict(side(8, 0.9), side(8, 0.75), age_days=9)[0], "approve")

    def test_check_rolls_back_on_a_guard_problem(self):
        self.started()
        fake = metrics_for((3, 3, 1), (3, 3, 0))
        out = cn.check(self.memory, now=NOW, metrics=fake)
        self.assertEqual(out["results"][0]["action"], "rollback")
        row = self.memory.proposals[1]
        self.assertEqual(row["status"], "rolled_back")
        self.assertEqual(row["canary"]["latest"]["candidate"]["tickets_with_problems"], 1)
        self.assertEqual(fake.calls[0]["group_by"], "variants.proposal-1")
        self.assertTrue(fake.calls[0]["guard"])

    def test_check_puts_a_good_canary_in_the_inbox(self):
        self.started()
        cn.check(self.memory, now=NOW, metrics=metrics_for((8, 7, 0), (8, 6, 0)))
        row = self.memory.proposals[1]
        self.assertEqual(row["status"], "awaiting_approval")
        self.assertTrue(pp.needs_person(row))
        self.assertEqual(row["canary"]["latest"]["decision"]["action"], "approve")
        # Still watched while it waits: a later problem rolls it back.
        cn.check(self.memory, now=NOW, metrics=metrics_for((9, 8, 1), (8, 6, 0)))
        self.assertEqual(self.memory.proposals[1]["status"], "rolled_back")

    def test_check_waits_for_enough_tickets(self):
        self.started()
        cn.check(self.memory, now=NOW, metrics=metrics_for((4, 4, 0), (5, 3, 0)))
        row = self.memory.proposals[1]
        self.assertEqual(row["status"], "canary")
        self.assertEqual(row["canary"]["latest"]["decision"]["action"], "wait")

    def test_promotion_without_a_repository_is_recorded_for_a_person(self):
        self.started()
        cn.check(self.memory, now=NOW, metrics=metrics_for((8, 7, 0), (8, 6, 0)))
        with mock.patch.dict(os.environ, {"TS_HARNESS_IMPROVEMENT_PR_REPO": ""}):
            row = cn.promote(self.memory, self.memory.get_improvement_proposal(1), actor="lead@example.com")
        self.assertEqual(row["status"], "promoted")
        self.assertEqual(row["promotion"]["method"], "manual")
        self.assertEqual(row["promotion"]["by"], "lead@example.com")

    def test_promotion_opens_a_pull_request_and_never_merges(self):
        import base64

        self.started()
        cn.check(self.memory, now=NOW, metrics=metrics_for((8, 7, 0), (8, 6, 0)))
        calls = []

        class GitHub:
            def request(self, method, path, *, body=None, query=None, allow_404=False):
                calls.append((method, path, body))
                if path.endswith("/git/ref/heads/dev"):
                    return {"object": {"sha": "base-sha"}}
                if path.endswith("/pulls"):
                    return {"html_url": "https://github.com/org/harness/pull/5"}
                return {}

            def get_file(self, owner, repo, path, *, ref):
                return {"sha": "blob", "content": base64.b64encode(SKILL.encode()).decode()}

        with mock.patch.dict(os.environ, {"TS_HARNESS_IMPROVEMENT_PR_REPO": "org/harness"}):
            row = cn.promote(self.memory, self.memory.get_improvement_proposal(1), actor="lead@example.com", github=GitHub())
        self.assertEqual(row["promotion"]["pr_url"], "https://github.com/org/harness/pull/5")
        methods = [(m, p.split("/repos/org/harness")[1]) for m, p, _ in calls]
        self.assertIn(("POST", "/pulls"), methods)
        self.assertFalse(any("merge" in p for _, p in methods))
        put = next(body for m, p, body in calls if m == "PUT")
        self.assertIn("duplicate writes", base64.b64decode(put["content"]).decode())
        self.assertEqual(put["sha"], "blob")
        pull = next(body for m, p, body in calls if p.endswith("/pulls"))
        self.assertEqual(pull["base"], "dev")
        self.assertTrue(pull["title"].startswith("[SO] "))

    def test_decisions_a_person_cannot_make(self):
        needs = self.memory.create_improvement_proposal(type="needs_design", target="x", title="t", actor=pp.ACTOR)
        with self.assertRaises(ValueError):
            cn.promote(self.memory, needs, actor="a")
        self.assertEqual(cn.reject(self.memory, needs, actor="a")["status"], "rejected")
        with self.assertRaises(ValueError):  # has an offline check: promoted only after its canary
            cn.promote(self.memory, {**self.memory.get_improvement_proposal(1), "status": "proposed"}, actor="a")
        self.started()
        self.assertEqual(cn.reject(self.memory, self.memory.get_improvement_proposal(1), actor="a")["status"],
                         "rolled_back")


# The hourly loop


class LoopTests(unittest.TestCase):
    def setUp(self):
        cn.clear_cache()
        self.memory = FakeMemory()
        for n in range(1, 11):
            self.memory.cases[n] = replay_case(n, "block" if n % 2 else "pass", at=f"2026-03-0{n % 9 + 1}T00:00:00Z")
        self.memory.create_improvement_proposal(type="skill_edit", target=pp.PRE_PUSH_TARGET, title="Retry tests",
                                                change={"diff": skill_diff(), "offline_check": pp.PRE_PUSH_TARGET},
                                                actor=pp.ACTOR)

    def run_loop(self, reply, metrics=None):
        return lp.run_loop(memory=self.memory, now=NOW, model_call=FakeCall(reply), budget=FakeBudget(),
                           metrics=metrics or metrics_for((0, 0, 0), (0, 0, 0)), runs=[])

    def test_a_passing_proposal_goes_to_canary_with_its_evaluation_and_cost(self):
        out = self.run_loop(reviewer)
        self.assertEqual(out["evaluated"]["evaluation"], pp.EVAL_PASSED)
        row = self.memory.proposals[1]
        self.assertEqual(row["status"], "canary")
        self.assertEqual(row["evaluation"]["status"], pp.EVAL_PASSED)
        self.assertAlmostEqual(row["cost_usd"], 2.0)
        self.assertEqual([h["status"] for h in row["history"]], ["evaluating", "canary"])

    def test_a_failing_proposal_is_rejected(self):
        self.run_loop(lambda prompt: '{"findings": []}')
        self.assertEqual(self.memory.proposals[1]["status"], "rejected")

    def test_an_inconclusive_one_waits_a_week(self):
        self.memory.cases = {}
        self.run_loop(reviewer)
        row = self.memory.proposals[1]
        self.assertEqual((row["status"], row["evaluation"]["status"]), ("proposed", pp.EVAL_INCONCLUSIVE))
        self.assertIsNone(lp.evaluate_next(self.memory, self.memory.list_improvement_proposals(), tenant_id="default",
                                           now=NOW + timedelta(days=1), model_call=FakeCall(reviewer), budget=FakeBudget()))

    def test_a_proposal_left_evaluating_is_released(self):
        self.memory.proposals[1]["status"] = "evaluating"
        self.memory.proposals[1]["updated_at"] = iso(NOW - timedelta(hours=7))
        released = lp.release_stale(self.memory, self.memory.list_improvement_proposals(), tenant_id="default", now=NOW)
        self.assertEqual(released, [1])

    def test_the_loop_is_registered_as_system_jobs(self):
        from ts_harness.gateway.system_jobs import SYSTEM_HANDLERS

        self.assertIn("improvement_digest", SYSTEM_HANDLERS)
        self.assertIn("improvement_loop", SYSTEM_HANDLERS)


# Inbox


class InboxTests(unittest.TestCase):
    def test_only_proposals_waiting_for_a_person_are_listed(self):
        from ts_harness.gateway.admin_bff import _improvement_items
        from ts_harness.gateway.auth import AuthContext

        memory = FakeMemory()
        memory.create_improvement_proposal(type="skill_edit", target=pp.PRE_PUSH_TARGET, title="Replayable", actor="x")
        memory.create_improvement_proposal(type="setting_change", target="setting:x", title="Manual", actor="x",
                                           evaluation={"status": pp.EVAL_UNAVAILABLE})
        memory.create_improvement_proposal(type="needs_design", target="x", title="Design", actor="x")
        memory.create_improvement_proposal(type="skill_edit", target="y", title="Canary passed", actor="x")
        memory.proposals[4]["status"] = "awaiting_approval"
        auth = AuthContext(tenant_id="default", actor_id="admin")
        items = _improvement_items(memory, auth)
        self.assertEqual(sorted(i["title"] for i in items), ["Canary passed", "Design", "Manual"])
        self.assertTrue(all(i["type"] == "improvement_proposal" for i in items))
        self.assertEqual(_improvement_items(object(), auth), [])


# Postgres


@requires_pg
class ProposalRoutesPgTests(PgTestCase):
    def setUp(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        with self.db.connect() as conn:
            conn.execute("DELETE FROM improvement_proposals")
            conn.execute("DELETE FROM improvement_replay_cases")
            conn.commit()

    def test_lifecycle_conflicts_and_one_live_proposal_per_target(self):
        from memory_routes import improvements as routes

        with self.db.connect() as conn:
            first = routes.create_proposal(conn, {"type": "skill_edit", "title": "A", "target": "pre_push_review",
                                                  "evidence": {"tickets": 3}, "cost_usd": 0.25})
            self.assertEqual(first["status"], "proposed")
            moved = routes.update_proposal(conn, first["id"], {"status": "evaluating", "expected_status": "proposed"})
            self.assertEqual(moved["status"], "evaluating")
            with self.assertRaises(routes.ProposalConflict):
                routes.update_proposal(conn, first["id"], {"status": "evaluating", "expected_status": "proposed"})
            with self.assertRaises(routes.ProposalConflict):
                routes.update_proposal(conn, first["id"], {"status": "promoted"})
            out = routes.update_proposal(conn, first["id"], {"status": "canary", "evaluation": {"status": "passed"},
                                                             "add_cost_usd": 1.5, "canary": {"share": 0.5}})
            self.assertEqual((out["status"], out["cost_usd"], len(out["evaluations"])), ("canary", 1.75, 1))
            second = routes.create_proposal(conn, {"type": "skill_edit", "title": "B", "target": "pre_push_review"})
            routes.update_proposal(conn, second["id"], {"status": "evaluating"})
            with self.assertRaises(routes.ProposalConflict):
                routes.update_proposal(conn, second["id"], {"status": "canary"})
            done = routes.update_proposal(conn, first["id"], {"status": "rolled_back", "actor": "harness-improver"})
            self.assertEqual(done["decided_by"], "harness-improver")
            self.assertEqual(routes.update_proposal(conn, second["id"], {"status": "canary"})["status"], "canary")
            listed = routes.list_proposals(conn, {"status": "canary,awaiting_approval"})["proposals"]
            self.assertEqual([p["title"] for p in listed], ["B"])
            with self.assertRaises(ValueError):
                routes.create_proposal(conn, {"type": "nonsense", "title": "x"})
            with self.assertRaises(LookupError):
                routes.get_proposal(conn, 999999)

    def test_replay_cases_keep_cached_verdicts_until_the_head_moves(self):
        from memory_routes import improvements as routes

        with self.db.connect() as conn:
            case = routes.put_replay_case(conn, {"workflow_id": "wf1", "truth_verdict": "block", "head_sha": "a",
                                                 "inputs": {"files": []}, "gate_at": "2026-03-01T00:00:00Z"})
            routes.put_replay_verdict(conn, case["id"], {"key": "k", "verdict": {"verdict": "pass"}})
            same = routes.put_replay_case(conn, {"workflow_id": "wf1", "truth_verdict": "block", "head_sha": "a"})
            self.assertEqual(same["verdicts"], {"k": {"verdict": "pass"}})
            moved = routes.put_replay_case(conn, {"workflow_id": "wf1", "truth_verdict": "pass", "head_sha": "b"})
            self.assertEqual(moved["verdicts"], {})
            listed = routes.list_replay_cases(conn, {"inputs": "0"})["cases"]
            self.assertNotIn("inputs", listed[0])
            conn.execute("UPDATE improvement_replay_cases SET gate_at = now() - interval '200 days'")
            conn.commit()
            self.assertEqual(routes.prune_replay_cases(conn, {"days": 90})["deleted"], 1)


if __name__ == "__main__":
    unittest.main()
