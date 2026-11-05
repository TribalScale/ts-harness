"""Knowledge notes on the read path, and the post-run review."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from unittest import mock

from ts_harness.implementation.backends.opencode import build_implement_prompt
from ts_harness.knowledge.prompt import render_knowledge_notes
from ts_harness.learning.proposals import apply_proposals
from ts_harness.learning.review_job import review_finished_programs
from ts_harness.learning.run_evidence import collect_run_evidence
from ts_harness.learning.staleness import paths_gone, stale_notes, team_note_candidates
from ts_harness.tools.workflow_tools import propose_knowledge_note_handler


NOTES = [
    {"scope": "org", "kind": "howto", "title": "Titles", "body": "Start with the Jira key.", "why": "Reviewers look for it."},
    {"scope": "project", "kind": "avoid", "title": "Main", "body": "Do not commit to main.", "why": ""},
]


class PromptTests(unittest.TestCase):
    def test_org_rules_come_before_project_notes_and_avoid_leads(self):
        text = render_knowledge_notes(NOTES, headings=True)
        self.assertLess(text.index("## Org rules"), text.index("## Project notes"))
        self.assertIn("[howto] Titles", text)
        self.assertIn("[avoid] Main", text)
        self.assertNotIn("()", text)

    def test_empty_notes_render_nothing(self):
        self.assertEqual(render_knowledge_notes([]), "")
        self.assertEqual(render_knowledge_notes(None), "")

    def test_opencode_prompt_includes_both_sections(self):
        prompt = build_implement_prompt("WO-1", "Ship it.", knowledge_notes=NOTES)
        self.assertIn("## Org rules", prompt)
        self.assertIn("## Project notes", prompt)
        self.assertLess(prompt.index("host provides"), prompt.index("## Org rules"))


class EvidenceTests(unittest.TestCase):
    def test_a_clean_run_is_not_useful(self):
        bundle = {"workflows": [{"tasks": [{"id": "t", "status": "succeeded", "output": {"pr_url": "https://github.com/a/b/pull/1"}}]}]}
        evidence = collect_run_evidence(bundle)
        self.assertFalse(evidence["useful"])
        self.assertEqual(evidence["pr_urls"], ["https://github.com/a/b/pull/1"])

    def test_a_blocker_is_kept_with_its_ref(self):
        bundle = {
            "workflows": [
                {
                    "tasks": [
                        {
                            "id": "task-9",
                            "status": "failed",
                            "error": "tests failed",
                            "output": {"review_feedback": "Rename the PR."},
                        }
                    ]
                }
            ]
        }
        evidence = collect_run_evidence(bundle)
        self.assertTrue(evidence["useful"])
        self.assertIn("task-9", {item["ref"] for item in evidence["signals"]})
        self.assertIn("review_feedback", {item["kind"] for item in evidence["signals"]})


class ProposalTests(unittest.TestCase):
    def test_project_add_is_proposed_and_the_program_is_marked_reviewed(self):
        memory = mock.Mock()
        memory.create_knowledge_note.return_value = {"id": "n1", "status": "proposed"}
        result = apply_proposals(
            memory,
            tenant_id="default",
            project_id="proj",
            source_program_id="prog",
            proposals=[{"action": "add", "kind": "avoid", "title": "Main", "body": "Do not commit to main."}],
        )
        body = memory.create_knowledge_note.call_args.kwargs
        self.assertEqual(body["scope"], "project")
        self.assertEqual(body["status"], "proposed")
        self.assertEqual(body["source"], "review")
        memory.mark_program_notes_reviewed.assert_called_once()
        self.assertEqual(result["written"][0]["id"], "n1")

    def test_org_add_needs_two_programs(self):
        memory = mock.Mock()
        result = apply_proposals(
            memory,
            tenant_id="default",
            project_id="proj",
            source_program_id="prog",
            proposals=[{"action": "add", "scope": "org", "title": "Titles", "body": "Start with the key."}],
        )
        memory.create_knowledge_note.assert_not_called()
        self.assertTrue(result["skipped"])

    def test_promote_uses_the_promote_api(self):
        memory = mock.Mock()
        memory.promote_knowledge_note.return_value = {"id": "org-1", "status": "proposed"}
        apply_proposals(
            memory,
            tenant_id="default",
            project_id="proj",
            source_program_id="prog",
            proposals=[
                {
                    "action": "promote",
                    "note_id": "note-1",
                    "evidence": [{"type": "program", "ref": "prog"}, {"type": "program", "ref": "other"}],
                }
            ],
        )
        memory.promote_knowledge_note.assert_called_once()

    def test_a_program_without_a_project_cannot_add_a_project_note(self):
        memory = mock.Mock()
        result = apply_proposals(
            memory,
            tenant_id="default",
            project_id="",
            source_program_id="prog",
            proposals=[{"action": "add", "title": "Titles", "body": "Start with the key."}],
        )
        memory.create_knowledge_note.assert_not_called()
        self.assertEqual(result["skipped"][0]["action"], "add")

    def test_the_same_program_is_not_reviewed_twice_by_the_job(self):
        memory = mock.Mock()
        memory.kv_get.return_value = {"value": {"scanned_at": "2026-01-01T00:00:00Z"}, "version": 1}
        memory.list_programs_pending_note_review.return_value = {"programs": []}
        memory.list_projects.return_value = {"projects": []}
        memory.list_knowledge_notes.return_value = {"notes": []}
        review_finished_programs(memory=memory, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
        memory.list_programs_pending_note_review.assert_called_once()
        memory.kv_put.assert_called_once()

    def test_first_run_bootstraps_the_cursor(self):
        memory = mock.Mock()
        memory.kv_get.return_value = {"value": None, "version": 0}
        result = review_finished_programs(memory=memory, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
        self.assertTrue(result["bootstrapped"])
        memory.list_programs_pending_note_review.assert_not_called()

    def test_a_clean_bundle_is_marked_reviewed_without_starting_a_workflow(self):
        memory = mock.Mock()
        memory.kv_get.return_value = {"value": {"scanned_at": "2026-01-01T00:00:00Z"}, "version": 1}
        memory.list_programs_pending_note_review.return_value = {"programs": [{"id": "prog"}]}
        memory.get_program.return_value = {
            "program": {"id": "prog", "tenant_id": "default", "project_id": "THD"},
            "workflows": [{"id": "wf"}],
        }
        memory.get_workflow.return_value = {"tasks": [{"id": "t", "status": "succeeded", "output": {}}]}
        memory.list_projects.return_value = {"projects": []}
        memory.list_knowledge_notes.return_value = {"notes": []}
        starter = mock.Mock()
        result = review_finished_programs(memory=memory, starter=starter, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
        starter.assert_not_called()
        memory.mark_program_notes_reviewed.assert_called_once()
        self.assertEqual(result["reviewed"], 1)
        self.assertEqual(result["started"], 0)


class StaleAndTeamTests(unittest.TestCase):
    def test_unused_project_note_is_stale_and_a_stage_name_is_not_a_path(self):
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        notes = [
            {"id": "old", "scope": "project", "status": "active", "last_used_at": "2026-01-01T00:00:00Z", "applies_to": ["implement"]},
            {"id": "new", "scope": "project", "status": "active", "last_used_at": now.isoformat(), "applies_to": ["src/missing.py"]},
        ]
        stale = stale_notes(notes, now=now, tree_paths={"src/app.py"})
        self.assertEqual({note["id"] for note in stale}, {"old", "new"})
        self.assertFalse(paths_gone(["implement"], {"src/app.py"}))

    def test_team_candidates_need_two_projects_and_write_nothing(self):
        projects = [{"id": "a", "team_slug": "web"}, {"id": "b", "team_slug": "web"}, {"id": "c", "team_slug": "api"}]
        notes = [
            {"scope": "project", "status": "active", "project_id": "a", "title": "PR titles"},
            {"scope": "project", "status": "active", "project_id": "b", "title": "PR titles"},
            {"scope": "project", "status": "active", "project_id": "c", "title": "PR titles"},
        ]
        found = team_note_candidates(projects, notes)
        self.assertEqual(found, [{"team": "web", "title": "pr titles", "project_ids": ["a", "b"]}])


class ProposeToolTests(unittest.TestCase):
    def _ctx(self):
        return mock.Mock(session_id="s1", tenant_id="default", agent_id="dev-agent")

    def test_writes_a_proposed_project_note(self):
        session = mock.Mock(metadata={"program_id": "prog", "workflow_id": "wf"})
        memory = mock.Mock()
        memory.get_program.return_value = {"program": {"project_ref": "proj", "tenant_id": "default"}}
        memory.list_knowledge_notes.return_value = {"notes": []}
        memory.create_knowledge_note.return_value = {"id": "n", "status": "proposed"}
        with mock.patch("ts_harness.agent.sessions.get_session_store") as store, mock.patch(
            "ts_harness.tools.workflow_tools.get_memory_client", return_value=memory
        ):
            store.return_value.get.return_value = session
            result = propose_knowledge_note_handler(
                {"kind": "avoid", "title": "Main", "body": "Do not commit to main."}, self._ctx()
            )
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(memory.create_knowledge_note.call_args.kwargs["scope"], "project")

    def test_limit_and_missing_project_and_org_scope(self):
        session = mock.Mock(metadata={"program_id": "prog", "workflow_id": "wf"})
        memory = mock.Mock()
        memory.get_program.return_value = {"program": {"project_id": "proj"}}
        memory.list_knowledge_notes.return_value = {
            "notes": [{"evidence": [{"ref": "wf"}]} for _ in range(3)]
        }
        with mock.patch("ts_harness.agent.sessions.get_session_store") as store, mock.patch(
            "ts_harness.tools.workflow_tools.get_memory_client", return_value=memory
        ):
            store.return_value.get.return_value = session
            limited = propose_knowledge_note_handler({"kind": "avoid", "title": "A", "body": "B"}, self._ctx())
            memory.get_program.return_value = {"program": {}}
            missing = propose_knowledge_note_handler({"kind": "avoid", "title": "A", "body": "B"}, self._ctx())
        refused = propose_knowledge_note_handler({"scope": "org", "kind": "avoid", "title": "A", "body": "B"}, self._ctx())
        self.assertIn("3", limited["error"])
        self.assertIn("no project", missing["error"])
        self.assertIn("org", refused["error"])


if __name__ == "__main__":
    unittest.main()
