"""Drafting proposed knowledge notes from runs that failed and were then fixed."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from unittest import mock

from ts_harness.gateway.system_jobs import SYSTEM_HANDLERS
from ts_harness.learning import failure_drafts as fd


def _gate(task_id, at, status, findings=None, accept=False):
    output = {"result": {"verdict": "block" if status == "blocked" else "pass", "findings": findings or []}}
    task_input = {"_human_accept": {"actor": "lead"}} if accept else {}
    return {"id": task_id, "step_name": "finalize_code_review", "created_at": at, "status": status, "output": output, "input": task_input}


FINDING = {"file": "src/db/migrate.py", "line": 12, "message": "migration runs without the advisory lock", "severity": "blocking"}


def _review_loop(accept=False):
    return [
        {"id": "impl", "step_name": "deliver_work_order", "created_at": "01", "status": "succeeded",
         "input": {"work_order_body": "Add an orders table"}, "output": {"pr_url": "https://github.com/org/api/pull/7"}},
        _gate("g1", "02", "blocked", [FINDING]),
        {"id": "r1", "step_name": "revise", "created_at": "03", "status": "succeeded",
         "input": {"_revision_source": "finalize_code_review"}, "output": {"summary": "added a retry"}},
        _gate("g2", "04", "blocked", [{**FINDING, "line": 14}]),
        {"id": "r2", "step_name": "revise", "created_at": "05", "status": "succeeded",
         "input": {"_revision_source": "finalize_code_review"}, "output": {"summary": "wrapped migrate() in the advisory lock"}},
        _gate("g3", "06", "succeeded", accept=accept),
    ]


class EpisodeTests(unittest.TestCase):
    def test_a_repeated_finding_fixed_by_a_revision(self):
        episodes = fd.find_episodes(_review_loop())
        self.assertEqual([e["kind"] for e in episodes], ["repeated_finding"])
        self.assertIn("advisory lock", episodes[0]["problem"])
        self.assertIn("wrapped migrate()", episodes[0]["fix"])
        self.assertIn("g2", episodes[0]["task_ids"])

    def test_accepting_over_the_finding_is_not_a_fix(self):
        self.assertEqual(fd.find_episodes(_review_loop(accept=True)), [])

    def test_ci_fixed_by_a_revision(self):
        tasks = [
            {"id": "c1", "step_name": "await_ci_on_sha", "created_at": "01", "status": "blocked",
             "output": {"failed_checks": ["lint", "unit"]}},
            {"id": "r", "step_name": "revise", "created_at": "02", "status": "succeeded",
             "input": {"_revision_budget": "checks", "qa_feedback": "ruff: unused import in src/app.py"},
             "output": {"summary": "removed the import"}},
            {"id": "c2", "step_name": "await_ci_on_sha", "created_at": "03", "status": "succeeded", "output": {}},
        ]
        episodes = fd.find_episodes(tasks)
        self.assertEqual(episodes[0]["kind"], "ci_fixed")
        self.assertIn("lint, unit", episodes[0]["problem"])
        self.assertIn("removed the import", episodes[0]["fix"])
        # CI still failing is not a fix
        self.assertEqual(fd.find_episodes(tasks[:2]), [])

    def test_guidance_from_a_person_or_the_arbiter_that_unstuck_a_step(self):
        person = {"id": "s1", "step_name": "deliver_work_order", "created_at": "01", "status": "succeeded",
                  "error": "", "input": {"human_guidance": "Guidance from lead:\nUse the shared http client"}}
        arbiter = {"id": "s2", "step_name": "qa_gate", "created_at": "02", "status": "blocked",
                   "input": {"_arbiter": {"category": "agent_off_track", "reason": "the agent edited generated files",
                                          "actions": [{"action": "revise"}]}}}
        later = {"id": "s3", "step_name": "qa_gate", "created_at": "03", "status": "succeeded", "input": {}}
        episodes = fd.find_episodes([person, arbiter, later])
        self.assertEqual([e["kind"] for e in episodes], ["guidance", "guidance"])
        self.assertIn("shared http client", episodes[0]["fix"])
        self.assertIn("arbiter guidance", episodes[1]["fix"])
        # still stuck: nothing to learn yet
        self.assertEqual(fd.find_episodes([arbiter]), [])

    def test_coding_retry_ignores_machine_failures(self):
        jobs = [
            {"task_id": "t", "created_at": "01", "status": "failed", "result": {"error": "tests failed: missing fixture"}},
            {"task_id": "t", "created_at": "02", "status": "succeeded", "result": {"summary": "added the fixture"}},
            {"task_id": "u", "created_at": "01", "status": "failed", "result": {"error": "killed by signal 9"}},
            {"task_id": "u", "created_at": "02", "status": "succeeded", "result": {}},
        ]
        episodes = fd.find_episodes([], jobs)
        self.assertEqual([(e["kind"], e["task_ids"]) for e in episodes], [("coding_retry", ["t"])])


class ParseAndDedupTests(unittest.TestCase):
    def test_parse_cleans_and_caps(self):
        reply = "Here:\n" + json.dumps({"notes": [
            {"scope": "team", "kind": "weird", "title": "T" * 200, "body": "In org/api, X; do Y", "applies_to": ["src/db/**", ""], "episodes": [1, "2"]},
            {"title": "", "body": "no title"},
            {"title": "b", "body": "c"},
            {"title": "d", "body": "e"},
        ]})
        drafts = fd.parse_drafts(reply)
        self.assertEqual(len(drafts), 2)
        self.assertEqual((drafts[0]["scope"], drafts[0]["kind"], len(drafts[0]["title"])), ("project", "avoid", 120))
        self.assertEqual(drafts[0]["applies_to"], ["src/db/**"])
        self.assertEqual(drafts[0]["episodes"], [1, 2])
        self.assertEqual(fd.parse_drafts("no json"), [])

    def test_near_duplicates(self):
        existing = [{"id": "n1", "title": "Take the migration lock", "body": "In org/api, take the advisory lock before migrations."}]
        same_words = {"title": "Advisory lock for migrations", "body": "In org/api, take the advisory lock before migrations run."}
        self.assertEqual(fd.near_duplicate(same_words, existing)["id"], "n1")
        different = {"title": "Stream CSV", "body": "In org/api, stream CSV rows instead of building files."}
        self.assertIsNone(fd.near_duplicate(different, existing))
        self.assertEqual(fd.near_duplicate(different, existing, similar=[{"id": "n1", "similarity": 0.95}])["id"], "n1")


class FakeMemory:
    def __init__(self, tasks, *, existing=None, cursor="2026-01-01T00:00:00Z"):
        self.tasks = tasks
        self.existing = existing or []
        self.cursor = {"value": {"scanned_at": cursor}, "version": 1} if cursor else {}
        self.created = []
        self.stamped = []
        self.kv = []
        self.embedded = 0

    def embed_pending_knowledge_notes(self, *, limit):
        self.embedded += 1
        return {"embedded": 0}

    def kv_get(self, _ns, _key):
        return self.cursor

    def kv_put(self, _ns, _key, value, **_kwargs):
        self.kv.append(value)

    def list_programs_pending_note_review(self, *, since, limit, stamp):
        assert stamp == "notes_drafted_at"
        return {"programs": [{"id": "prog-1", "tenant_id": "default", "project_ref": "p1"},
                             {"id": "prog-2", "tenant_id": "default", "project_ref": "p1"}]}

    def get_program(self, program_id):
        return {"program": {"id": program_id, "tenant_id": "default", "project_ref": "p1",
                            "metadata": {"artifacts": {"repo_url": "https://github.com/org/api"}}},
                "workflows": [{"id": f"wf-{program_id}"}]}

    def get_workflow(self, workflow_id):
        return {"tasks": self.tasks if workflow_id == "wf-prog-1" else []}

    def list_coding_jobs(self, **_kwargs):
        return []

    def get_project(self, _project_id):
        return {"key": "PROJ"}

    def list_knowledge_notes(self, **kwargs):
        return {"notes": [n for n in self.existing if n.get("status") == kwargs.get("status")]}

    def search_knowledge_notes(self, **_kwargs):
        return {"notes": []}

    def create_knowledge_note(self, **body):
        self.created.append(body)
        return {"id": f"note-{len(self.created)}", **body}

    def mark_program_notes_reviewed(self, program_id, *, actor, key):
        self.stamped.append((program_id, key))


REPLY = json.dumps({"notes": [{
    "scope": "project", "kind": "avoid", "title": "Migrations need the advisory lock",
    "body": "In org/api, run migrations inside the advisory lock; a retry alone does not fix concurrent runs.",
    "why": "Review sent it back twice.", "applies_to": ["src/db/**"], "episodes": [1]}]})


class JobTests(unittest.TestCase):
    def test_drafts_a_proposed_note_with_evidence_and_stamps_every_program(self):
        memory = FakeMemory(_review_loop())
        prompts = []
        result = fd.draft_failure_notes(memory=memory, model_call=lambda p: prompts.append(p) or REPLY)
        self.assertEqual(result["drafted"], 1)
        self.assertEqual(result["model_calls"], 1)  # prog-2 had no episode: no call
        note = memory.created[0]
        self.assertEqual((note["status"], note["source"], note["scope"], note["project_id"]), ("proposed", "drafted", "project", "p1"))
        types = [item["type"] for item in note["evidence"]]
        self.assertEqual(types[0], "drafted_from")
        self.assertIn("pr", types)
        self.assertIn("task", types)
        self.assertEqual(note["evidence"][0]["url"], "https://github.com/org/api/pull/7")
        self.assertIn("org/api", prompts[0])
        self.assertEqual(memory.stamped, [("prog-1", "notes_drafted_at"), ("prog-2", "notes_drafted_at")])
        self.assertEqual(len(memory.kv), 1)  # cursor advanced
        self.assertEqual(memory.embedded, 1)

    def test_skips_near_duplicates(self):
        existing = [{"id": "n1", "status": "active", "scope": "project", "project_id": "p1",
                     "title": "Migrations need the advisory lock", "body": "already known"}]
        memory = FakeMemory(_review_loop(), existing=existing)
        result = fd.draft_failure_notes(memory=memory, model_call=lambda _p: REPLY)
        self.assertEqual(result["drafted"], 0)
        self.assertEqual(result["skipped"][0]["reason"], "near duplicate")
        self.assertEqual(memory.created, [])

    def test_budget_leaves_the_rest_for_the_next_run(self):
        memory = FakeMemory(_review_loop())
        result = fd.draft_failure_notes(memory=memory, model_call=lambda _p: REPLY, max_calls=0)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(memory.stamped, [])
        self.assertEqual(memory.kv, [])  # cursor kept

    def test_a_model_error_is_retried_next_run(self):
        def broken(_prompt):
            raise RuntimeError("provider down")

        memory = FakeMemory(_review_loop())
        result = fd.draft_failure_notes(memory=memory, model_call=broken)
        self.assertEqual(result["drafted"], 0)
        self.assertNotIn(("prog-1", "notes_drafted_at"), memory.stamped)
        self.assertEqual(memory.kv, [])

    def test_no_model_configured_does_nothing(self):
        memory = FakeMemory(_review_loop())
        with mock.patch.object(fd.rg, "diagnosis_model", return_value=None):
            result = fd.draft_failure_notes(memory=memory)
        self.assertEqual(result["status"], "skipped")
        self.assertEqual((memory.created, memory.stamped), ([], []))

    def test_first_run_only_stores_the_cursor(self):
        memory = FakeMemory(_review_loop(), cursor="")
        result = fd.draft_failure_notes(memory=memory, model_call=lambda _p: REPLY, now=datetime(2026, 5, 1, tzinfo=timezone.utc))
        self.assertTrue(result["bootstrapped"])
        self.assertEqual(memory.kv, [{"scanned_at": "2026-05-01T00:00:00Z"}])
        self.assertEqual(memory.created, [])

    def test_org_lesson_and_no_project(self):
        memory = FakeMemory(_review_loop())
        memory.get_program = lambda pid: {"program": {"id": pid, "tenant_id": "default"}, "workflows": [{"id": f"wf-{pid}"}]}
        memory.list_programs_pending_note_review = lambda **_kw: {"programs": [{"id": "prog-1", "tenant_id": "default"}]}
        reply = json.dumps({"notes": [
            {"scope": "project", "title": "p", "body": "In org/api, x; do y"},
            {"scope": "org", "kind": "howto", "title": "Pin tool versions", "body": "Pin CLI versions in CI; do not use latest."},
        ]})
        fd.draft_failure_notes(memory=memory, model_call=lambda _p: reply)
        self.assertEqual([(n["scope"], "project_id" in n) for n in memory.created], [("org", False)])

    def test_registered_as_a_system_job(self):
        self.assertIn("draft_failure_notes", SYSTEM_HANDLERS)


if __name__ == "__main__":
    unittest.main()
