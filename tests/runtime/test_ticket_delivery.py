"""prepare_ticket_delivery: read the ticket, resolve the repository, add design references and onboarding."""

import unittest
from unittest import mock

from ts_harness.jira.pointer_block import parse_pointer_block
from ts_harness.jira.ticket_delivery import prepare_ticket_delivery, resolve_target
from ts_harness.workflows.repo_onboarding_gate import Inspection

PROGRAM_REFS = [
    {"id": "D1", "title": "List", "object_key": "design/t/a.png", "mime": "image/png"},
    {"id": "D2", "title": "Drawer", "object_key": "design/t/b.png", "mime": "image/png"},
]

DESCRIPTION = """As a member, I want comment counts in the list.

Acceptance criteria
- Given a discussion with 3 comments, When I open the list, Then its row shows 3.

Repo: acme/web@fe-dev"""


class _Store:
    def __init__(self):
        self.objects = {}

    def head(self, key):
        return {} if key in self.objects else None

    def put(self, key, data):
        self.objects[key] = data


class _Jira:
    def __init__(self, description=DESCRIPTION, attachments=(), summary="Show the comment count"):
        self.description = description
        self.attachments = list(attachments)
        self.summary = summary

    def get_issue(self, key):
        return {"key": key, "fields": {"summary": self.summary, "description": self.description}}

    def image_attachments(self, key):
        return [{"id": str(i), "filename": f"shot-{i}.png", "mime": "image/png", "size": 10} for i, _ in enumerate(self.attachments)]

    def attachment_content(self, attachment_id):
        return self.attachments[int(attachment_id)]

    def transition(self, key, name):
        self.moved = (key, name)
        return True

    def comment(self, key, text):
        pass


def _memory(*, program_refs=None, owner_refs=None, owner_markdown=None):
    memory = mock.Mock()
    memory.get_workflow.return_value = {"workflow": {"id": "wf-1", "input": {"jira_issue_key": "THD-33", "base_branch": "dev"}}, "tasks": []}
    memory.task_program.return_value = {"program_id": "jira-prog", "workflow_id": "wf-1"}
    statuses = {
        "jira-prog": {"artifacts": {"design_refs": program_refs} if program_refs else {}},
        "prd-prog": {"artifacts": {
            "design_refs": owner_refs or [], "planner_artifacts": {"work_orders_markdown": "# WOs"},
            **({"work_orders_markdown": owner_markdown} if owner_markdown else {}),
        }},
    }
    memory.get_program_status.side_effect = lambda pid: statuses[pid]
    memory.get_program.side_effect = lambda pid: {"program": {"id": pid, "parent_program_id": None}}
    memory.list_projects.return_value = {"projects": []}
    return memory


TASK = {"id": "prep-1", "workflow_id": "wf-1", "tenant_id": "acme", "input": {"jira_issue_key": "THD-33"}}


def _not_needed(target, wo_id):
    return Inspection(False, "already has .ts-harness.yml")


def _run(jira=None, memory=None, inspect=_not_needed, configured=("", ""), store=None):
    return prepare_ticket_delivery(
        TASK, {}, memory=memory or _memory(), jira_factory=lambda: jira or _Jira(), store_factory=lambda: store or _Store(),
        inspect=inspect, configured=configured,
    )


class PrepareTicketDeliveryTests(unittest.TestCase):
    def test_reads_the_ticket_and_its_repository_line(self):
        jira = _Jira()
        out = _run(jira=jira)
        self.assertEqual(jira.moved, ("THD-33", "In Progress"))
        self.assertEqual((out["repo_url"], out["base_branch"]), ("https://github.com/acme/web", "fe-dev"))
        self.assertEqual(out["work_order_id"], "THD-33")
        self.assertTrue(out["work_order_body"].startswith("# THD-33 Show the comment count\n\nAs a member"))
        self.assertIn("Then its row shows 3.", out["work_order_body"])
        self.assertIn("PR against `fe-dev` on `acme/web` (from the ticket's Repo line).", out["summary"])
        self.assertEqual(out["onboarding"], {"checked": True, "needed": False, "reason": "already has .ts-harness.yml"})

    def test_a_branch_named_in_the_request_wins(self):
        task = {**TASK, "input": {"jira_issue_key": "THD-33", "summary": "implement THD-33 on branch release/2"}}
        out = prepare_ticket_delivery(
            task, {}, memory=_memory(), jira_factory=lambda: _Jira(), store_factory=_Store, inspect=_not_needed, configured=("", "")
        )
        self.assertEqual(out["base_branch"], "release/2")
        self.assertIn("PR against `release/2`", out["summary"])
        self.assertIn("Starting the coding job now.", out["summary"])

    def test_a_ticket_without_a_repository_blocks_with_what_to_add(self):
        with self.assertRaisesRegex(ValueError, "Repo: org/repo@branch"):
            _run(jira=_Jira(description="Just do it"))
        out = _run(jira=_Jira(description="Just do it"), configured=("https://github.com/acme/api", "main"))
        self.assertEqual((out["repo_url"], out["base_branch"]), ("https://github.com/acme/api", "main"))

    def test_a_ticket_another_run_is_building_is_not_started_twice(self):
        memory = _memory()
        memory.running_ticket_work.return_value = {"tickets": [{"key": "THD-33", "workflow_id": "other-run"}]}
        with self.assertRaisesRegex(ValueError, "THD-33 is already being built by another run"):
            _run(jira=_Jira(), memory=memory)

    def test_a_ticket_jira_cannot_return_blocks(self):
        jira = mock.Mock()
        jira.get_issue.side_effect = RuntimeError("401")
        with self.assertRaisesRegex(ValueError, "could not read THD-33 from Jira"):
            _run(jira=jira)

    def test_image_attachments_become_named_design_references(self):
        store = _Store()
        out = _run(jira=_Jira(attachments=[b"png-bytes"]), store=store)
        self.assertEqual([(r["id"], r["source"]) for r in out["design_refs"]], [("D1", "jira")])
        self.assertIn(out["design_refs"][0]["object_key"], store.objects)
        self.assertTrue(out["work_order_body"].rstrip().endswith("- Design: D1"))
        self.assertIn("Design: D1 (shot-0.png)", out["summary"])

    def test_a_ticket_filed_from_a_prd_brings_its_programs_references_and_planning(self):
        description = DESCRIPTION + "\nProgram: prd-prog\n\nDesign: D2"
        out = _run(jira=_Jira(description=description, attachments=[b"x"]), memory=_memory(owner_refs=PROGRAM_REFS))
        self.assertEqual([r["id"] for r in out["design_refs"]], ["D1", "D2", "D3"])
        self.assertEqual(out["owner_program_id"], "prd-prog")
        self.assertEqual(out["planner_artifacts"]["work_orders_markdown"], "# WOs")
        self.assertEqual(out["planner_artifacts"]["source_program_id"], "prd-prog")
        # The ticket named D2; the attachment joins that line.
        self.assertIn("Design: D2, D3", out["work_order_body"])
        self.assertEqual(out["work_order_body"].count("Design:"), 1)

    def test_a_ticket_that_names_no_frames_says_so_instead_of_listing_them_all(self):
        description = DESCRIPTION + "\nProgram: prd-prog\n"
        out = _run(jira=_Jira(description=description), memory=_memory(owner_refs=PROGRAM_REFS))
        self.assertIn("Design: none (the ticket names no design frames)", out["summary"])
        self.assertNotIn("D1 (", out["summary"])

    def test_a_ticket_filed_without_its_frames_gets_them_from_its_work_order(self):
        refs = [*PROGRAM_REFS[:1], {**PROGRAM_REFS[1], "url": "https://www.figma.com/design/F/?node-id=2-9"}]
        markdown = "# WO-001 List\n\n## Summary\nx\n\n# WO-002 Drawer\n\n## Context Graph\n- Design: D2\n"
        description = DESCRIPTION + "\nWO: WO-002\nProgram: prd-prog"
        out = _run(jira=_Jira(description=description), memory=_memory(owner_refs=refs, owner_markdown=markdown))
        body = out["work_order_body"]
        self.assertIn("## Design frames\n\nDesign: D2\n\n- D2 — [Drawer](https://www.figma.com/design/F/?node-id=2-9)", body)
        self.assertIn("Design: D2 (Drawer)", out["summary"])

    def test_a_repository_without_harness_config_is_set_up_in_the_same_pr(self):
        def needed(target, wo_id):
            self.assertEqual((target.repo_url, target.base_branch, wo_id), ("https://github.com/acme/web", "fe-dev", "WO-0000"))
            return Inspection(True, "CI runs an e2e suite", "# WO-0000 Onboard\n\nWrite .ts-harness.yml")

        out = _run(inspect=needed)
        self.assertIn("## Repository setup (also in this PR)", out["work_order_body"])
        self.assertIn("Write .ts-harness.yml", out["work_order_body"])
        self.assertIn("this PR also adds it", out["summary"])

    def test_lookups_that_fail_are_notes_and_the_ticket_still_runs(self):
        memory = _memory()
        memory.task_program.side_effect = RuntimeError("memory down")

        def boom(*_a):
            raise RuntimeError("clone failed")

        out = _run(memory=memory, inspect=boom)
        self.assertEqual(out["design_refs"], [])
        self.assertEqual(len(out["warnings"]), 2)
        self.assertIn("Note: onboarding check failed: clone failed", out["summary"])


class ProjectRepositoryTests(unittest.TestCase):
    def _memory(self, repositories, *, default_base="fe-dev"):
        memory = _memory()
        memory.list_projects.return_value = {"projects": [
            {"id": "p-other", "key": "OTHER", "jira_project_key": "ABC"},
            {"id": "p-thd", "key": "DISC", "name": "Discussions", "jira_project_key": "THD", "default_base_branch": default_base},
        ]}
        memory.list_project_repositories.return_value = {"repositories": repositories}
        return memory

    def test_a_ticket_without_a_repo_line_uses_its_jira_projects_repository(self):
        memory = self._memory([{"full_name": "acme/web", "github_host": "github.com", "default_branch": None}])
        out = _run(jira=_Jira(description="Add the count column."), memory=memory)
        self.assertEqual((out["repo_url"], out["base_branch"]), ("https://github.com/acme/web", "fe-dev"))
        self.assertIn("(from project Discussions)", out["summary"])
        memory.list_project_repositories.assert_called_with("p-thd")

    def test_the_repositorys_own_branch_wins_over_the_projects(self):
        memory = self._memory([{"full_name": "acme/web", "default_branch": "release"}])
        self.assertEqual(_run(jira=_Jira(description="x"), memory=memory)["base_branch"], "release")

    def test_several_repositories_need_the_ticket_to_name_one(self):
        repos = [{"full_name": "acme/web"}, {"full_name": "acme/api"}]
        out = _run(jira=_Jira(description="Change acme/api only."), memory=self._memory(repos))
        self.assertEqual(out["repo_url"], "https://github.com/acme/api")
        with self.assertRaisesRegex(ValueError, "several repositories \\(acme/web, acme/api\\)"):
            _run(jira=_Jira(description="Change something."), memory=self._memory(repos))

    def test_a_repo_line_still_wins(self):
        memory = self._memory([{"full_name": "acme/other"}])
        self.assertEqual(_run(memory=memory)["repo_url"], "https://github.com/acme/web")

    def test_an_empty_repository_waits_for_its_bootstrap_pr(self):
        from ts_harness.workflows.repo_bootstrap import Readiness

        memory = self._memory([{"full_name": "acme/web", "role": "web"}])
        pending = Readiness(repo="acme/web", ready=False, template="react-web", pr_url="https://github.com/acme/web/pull/9")
        ready = mock.Mock(return_value=pending)
        with self.assertRaisesRegex(ValueError, "starts from the `react-web` template. Waiting on the bootstrap PR: https://github.com/acme/web/pull/9\nMerge it; this ticket continues on its own") as caught:
            prepare_ticket_delivery(
                TASK, {}, memory=memory, jira_factory=lambda: _Jira(description="x"), store_factory=_Store,
                inspect=_not_needed, configured=("", ""), ready=ready,
            )
        # The runner resumes the step once this PR merges (block_recovery.resume_after_bootstrap).
        self.assertEqual(caught.exception.output["bootstrap_prs"], ["https://github.com/acme/web/pull/9"])
        self.assertEqual(ready.call_args.kwargs, {"role": "web", "title": "Discussions"})
        ready.return_value = Readiness(repo="acme/web", ready=True, notes=["acme/web: made checks required"])
        out = prepare_ticket_delivery(
            TASK, {}, memory=memory, jira_factory=lambda: _Jira(description="x"), store_factory=_Store,
            inspect=_not_needed, configured=("", ""), ready=ready,
        )
        self.assertIn("Note: acme/web: made checks required", out["summary"])


class ResolveTargetTests(unittest.TestCase):
    def test_branch_order(self):
        with_branch = parse_pointer_block("Repo: acme/web@release")
        bare = parse_pointer_block("Repo: acme/web")
        configured = ("https://github.com/acme/web", "fe-dev")
        self.assertEqual(resolve_target(with_branch, "dev", configured)[1], "release")
        self.assertEqual(resolve_target(bare, "dev", configured)[1], "fe-dev")
        self.assertEqual(resolve_target(bare, "main", ("", ""))[1], "main")
        self.assertEqual(resolve_target(bare, "", ("", ""))[1], "dev")
        self.assertEqual(resolve_target(parse_pointer_block(""), "", ("", "")), ("", "", ""))


class _QueueJira(_Jira):
    """Each ticket of the queue has its own description."""

    def __init__(self, issues):
        super().__init__()
        self.issues = issues

    def get_issue(self, key):
        summary, description = self.issues[key]
        return {"key": key, "fields": {"summary": summary, "description": description}}


class SharedFrameTests(unittest.TestCase):
    """Tickets in one queue that build parts of the same frame each own their part."""

    def _memory(self):
        memory = _memory(program_refs=PROGRAM_REFS)
        workflows = {
            "wf-1": {"workflow": {"id": "wf-1", "parent_workflow_id": "pool-1", "input": {"jira_issue_key": "THD-33"}}, "tasks": []},
            "pool-1": {"workflow": {"id": "pool-1", "input": {"jira_issue_keys": ["THD-33", "THD-34", "THD-35"]}}, "tasks": []},
        }
        memory.get_workflow.side_effect = lambda wid: workflows[wid]
        return memory

    def test_siblings_naming_the_same_frame_are_listed_with_what_they_cover(self):
        jira = _QueueJira({
            "THD-33": ("Schedule blocks", "## In Scope\n- schedule blocks\n\nDesign: D1\n\nRepo: acme/web@fe-dev"),
            "THD-34": ("Grid rows", "## In Scope\n- grid rows and their tones\n\nDesign: D1\n\nRepo: acme/web@fe-dev"),
            "THD-35": ("Settings", "Design: D2\n\nRepo: acme/web@fe-dev"),
        })
        out = _run(jira=jira, memory=self._memory())
        body = out["work_order_body"]
        self.assertIn("## Shared frames", body)
        self.assertIn("  - THD-34: grid rows and their tones", body)
        self.assertNotIn("THD-35", body)
        self.assertIn("## Frame scope\n\n### D1\n- Owns:\n  - schedule blocks", body)
        self.assertEqual([s["frame"] for s in out["shared_frames"]], ["D1"])
        self.assertIn("D1 is shared with THD-34", out["summary"])

    def test_overlapping_siblings_are_a_warning(self):
        jira = _QueueJira({
            "THD-33": ("Toolbar", "## In Scope\n- week toolbar\n\nDesign: D1\n\nRepo: acme/web@fe-dev"),
            "THD-34": ("Header", "## In Scope\n- week header and toolbar\n\nDesign: D1\n\nRepo: acme/web@fe-dev"),
            "THD-35": ("Settings", "Design: D2\n\nRepo: acme/web@fe-dev"),
        })
        out = _run(jira=jira, memory=self._memory())
        self.assertTrue(any("THD-33 and THD-34 both seem to build" in w for w in out["warnings"]))


if __name__ == "__main__":
    unittest.main()
