"""The stuck-step arbiter: what it may do on its own, what it hands to a person, and how often."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from ts_harness.workflows import stuck_arbiter as sa
from ts_harness.workflows.work_order_waves import HUMAN_DECISION_KEY

SINCE = "2026-11-27T10:00:00+00:00"


def _summary(step="deliver_work_order", decisions=("retry", "revise", "stop"), **extra):
    return {"workflow_id": "wf-1", "task_id": "t-1", "step_name": step, "work_order": "PROJ-56",
            "decisions": list(decisions), "error": "opencode exit 1", "feedback": "", "pr_url": "", **extra}


def _task(step="deliver_work_order", findings=None, arbiter=None, task_id="t-1"):
    data = {HUMAN_DECISION_KEY: {"reason": "failed_run", "since": SINCE}}
    if arbiter:
        data[sa.ARBITER_KEY] = arbiter
    return {"id": task_id, "workflow_id": "wf-1", "step_name": step, "status": "blocked", "input": data,
            "output": {"findings": findings or []}, "created_at": "1"}


def _verdict(**fields):
    return {"category": "agent_off_track", "action": "revise", "guidance": "Keep only the badge.",
            "confidence": 0.9, "reason": "The run redesigned the panel the ticket leaves alone.", **fields}


SCOPE = {"severity": "major", "category": "scope", "file": "src/Panel.tsx", "message": "Redesigns the panel"}
AUTH = {"severity": "blocker", "category": "correctness", "file": "src/auth.ts", "message": "Token check removed"}


class PolicyTests(unittest.TestCase):
    def decide(self, verdict, *, summary=None, task=None, taken=0, autonomy="autonomous"):
        return sa.decide(verdict, summary=summary or _summary(), task=task or _task(), taken=taken, autonomy=autonomy)

    def test_an_agent_off_track_is_revised_with_the_arbiters_guidance(self):
        plan = self.decide(_verdict())
        self.assertEqual((plan["do"], plan["guidance"]), ("revise", "Keep only the badge."))

    def test_a_cause_outside_the_work_is_retried(self):
        self.assertEqual(self.decide(_verdict(category="infrastructure", action="retry"))["do"], "retry")

    def test_a_harness_issue_goes_to_the_inbox_labelled_and_never_acts(self):
        plan = self.decide(_verdict(category="harness_issue", action="retry", reason="The harness can't read the API repo."))
        self.assertIsNone(plan["do"])
        self.assertEqual(plan["label"], "Harness issue")

    def test_a_product_question_is_labelled_with_the_question(self):
        plan = self.decide(_verdict(category="product_decision", action="escalate", question="Dev only, or prod too?"))
        self.assertEqual((plan["label"], plan["why"]), ("Needs product decision", "Dev only, or prod too?"))

    def test_after_two_actions_it_hands_off_with_its_recommendation(self):
        plan = self.decide(_verdict(), taken=2)
        self.assertIsNone(plan["do"])
        self.assertEqual(plan["label"], "Arbiter handed off")
        self.assertEqual(plan["recommendation"], {"action": "revise", "guidance": "Keep only the badge."})

    def test_low_confidence_and_conservative_projects_hand_off(self):
        self.assertIsNone(self.decide(_verdict(confidence=0.3))["do"])
        self.assertIsNone(self.decide(_verdict(), autonomy="conservative")["do"])

    def test_a_revision_without_guidance_or_a_decision_the_step_lacks_hands_off(self):
        self.assertIsNone(self.decide(_verdict(guidance=""))["do"])
        self.assertIsNone(self.decide(_verdict(), summary=_summary(decisions=("retry", "stop")))["do"])

    def test_at_the_run_cap_it_starts_no_coding_run_but_may_rerun_qa(self):
        capped = _summary(step="finalize_code_review", decisions=("retry", "revise", "accept", "stop"), reason="run_limit")
        plan = self.decide(_verdict(), summary=capped)
        self.assertIsNone(plan["do"])
        self.assertEqual(plan["recommendation"], {"action": "revise", "guidance": "Keep only the badge."})
        qa = _summary(step="qa_gate", decisions=("retry", "revise", "accept", "stop"), reason="run_limit")
        self.assertEqual(self.decide(_verdict(category="infrastructure", action="retry"), summary=qa)["do"], "retry")
        coding = _summary(reason="run_limit")
        self.assertIsNone(self.decide(_verdict(category="infrastructure", action="retry"), summary=coding)["do"])

    def test_no_verdict_hands_off(self):
        self.assertEqual(self.decide(None)["label"], "Arbiter handed off")


class OverrideTests(unittest.TestCase):
    REVIEW = _summary(step="finalize_code_review", decisions=("retry", "revise", "accept", "stop"))

    def plan(self, findings, verdict):
        return sa.decide(verdict, summary=self.REVIEW, task=_task("finalize_code_review", findings), taken=0, autonomy="autonomous")

    def test_a_finding_that_contradicts_the_work_order_is_overridden_with_a_note(self):
        verdict = _verdict(category="reviewer_wrong", action="override", override=[
            {"finding": "Redesigns the panel", "grounds": "contradicts_work_order", "evidence": "AC 3: the panel opens on the right"}])
        plan = self.plan([SCOPE], verdict)
        self.assertEqual(plan["do"], "accept")
        self.assertIn("AC 3: the panel opens on the right", plan["guidance"])
        self.assertIn("a person merges", plan["guidance"])

    def test_never_over_a_security_finding(self):
        verdict = _verdict(category="reviewer_wrong", action="override", override=[
            {"finding": "Token check removed", "grounds": "already_addressed", "evidence": "src/auth.ts:12"}])
        plan = self.plan([AUTH], verdict)
        self.assertIsNone(plan["do"])
        self.assertIn("security", plan["why"])

    def test_every_blocking_finding_needs_grounds_and_evidence(self):
        two = [SCOPE, {**SCOPE, "message": "Second"}]
        one_ground = _verdict(action="override", override=[{"finding": "x", "grounds": "contradicts_work_order", "evidence": "AC 1"}])
        self.assertIsNone(self.plan(two, one_ground)["do"])
        no_evidence = _verdict(action="override", override=[{"finding": "x", "grounds": "contradicts_work_order", "evidence": ""}])
        self.assertIsNone(self.plan([SCOPE], no_evidence)["do"])
        bad_grounds = _verdict(action="override", override=[{"finding": "x", "grounds": "style", "evidence": "AC 1"}])
        self.assertIsNone(self.plan([SCOPE], bad_grounds)["do"])

    def test_only_a_code_review_step_can_be_overridden(self):
        verdict = _verdict(action="override", override=[{"finding": "x", "grounds": "contradicts_work_order", "evidence": "AC 1"}])
        plan = sa.decide(verdict, summary=_summary(step="qa_gate", decisions=("retry", "revise", "accept", "stop")),
                         task=_task("qa_gate", [SCOPE]), taken=0, autonomy="autonomous")
        self.assertIsNone(plan["do"])


class CountingTests(unittest.TestCase):
    def test_actions_count_across_the_steps_recreated_tasks(self):
        tasks = [
            _task("finalize_code_review", arbiter={"actions": [{"action": "revise"}]}, task_id="a"),
            _task("finalize_code_review", arbiter={"actions": [{"action": "override"}]}, task_id="b"),
            _task("deliver_work_order", arbiter={"actions": [{"action": "retry"}]}, task_id="c"),
        ]
        self.assertEqual(sa.actions_taken(tasks, "finalize_code_review"), 2)

    def test_a_stop_is_triaged_once_and_a_later_stop_again(self):
        self.assertFalse(sa.seen(_task()))
        self.assertTrue(sa.seen(_task(arbiter={"for_since": SINCE})))
        self.assertFalse(sa.seen(_task(arbiter={"for_since": "2026-11-26T09:00:00+00:00"})))


class ArbitrateOneTests(unittest.TestCase):
    def run_one(self, verdict, *, summary=None, task=None, decide_error=None):
        task = task or _task()
        engine = MagicMock()
        engine.client.get_workflow.return_value = {"workflow": {"id": "wf-1"}, "tasks": [task]}
        if decide_error:
            engine.decide_blocked_step.side_effect = decide_error
        memory = MagicMock()
        memory.list_coding_jobs.return_value = [{"id": "job-1", "status": "failed", "result": {"error": "opencode exit 1"}}]
        memory.list_coding_job_events.return_value = {"events": [{"kind": "edited", "path": "src/Panel.tsx", "additions": 80, "deletions": 40}]}
        prompts: list[str] = []

        def model(prompt):
            prompts.append(prompt)
            return json.dumps(verdict)

        with patch("ts_harness.workflows.human_decisions.stuck_summary", return_value=summary or _summary()), \
                patch.object(sa.triage, "project_autonomy", return_value="autonomous"):
            out = sa.arbitrate_one(engine, task, memory=memory, model_call=model)
        saved = engine.client.patch_task_input.call_args_list[-1].args[1][sa.ARBITER_KEY]
        return out, engine, saved, prompts

    def test_it_revises_through_the_inbox_decision_path_and_records_it(self):
        out, engine, saved, prompts = self.run_one(_verdict())
        self.assertEqual(out["acted"], "revise")
        args, kwargs = engine.decide_blocked_step.call_args
        self.assertEqual(args, ("wf-1", "t-1", "revise"))
        self.assertEqual(kwargs["actor"], "harness-arbiter")
        self.assertIn("Keep only the badge.", kwargs["guidance"])
        self.assertEqual(saved["actions"][-1]["action"], "revise")
        self.assertEqual(saved["for_since"], SINCE)
        self.assertIn("edited src/Panel.tsx (+80 -40)", prompts[0])

    def test_a_handoff_leaves_the_step_for_a_person_with_a_label(self):
        out, engine, saved, _ = self.run_one(_verdict(category="harness_issue", action="escalate"))
        engine.decide_blocked_step.assert_not_called()
        self.assertEqual(saved["label"], "Harness issue")
        self.assertEqual(out["handed_off"], "Harness issue")

    def test_if_acting_fails_the_step_is_labelled_and_the_action_not_counted(self):
        _out, _engine, saved, _ = self.run_one(_verdict(), decide_error=ValueError("step is not waiting for a decision"))
        self.assertEqual(saved["label"], "Arbiter handed off")
        self.assertEqual(saved["actions"], [])

    def test_an_override_posts_its_reasoning_on_the_pr(self):
        verdict = _verdict(category="reviewer_wrong", action="override", override=[
            {"finding": "Redesigns the panel", "grounds": "contradicts_human_request", "evidence": "the PR comment asking for it"}])
        summary = _summary(step="finalize_code_review", decisions=("retry", "revise", "accept", "stop"),
                           pr_url="https://github.com/org/web/pull/3")
        with patch.object(sa, "_comment_on_pr") as comment:
            out, engine, _saved, _ = self.run_one(verdict, summary=summary, task=_task("finalize_code_review", [SCOPE]))
        self.assertEqual(out["acted"], "override")
        self.assertEqual(engine.decide_blocked_step.call_args.args[2], "accept")
        comment.assert_called_once()
        self.assertIn("the PR comment asking for it", comment.call_args.args[1])


class SweepTests(unittest.TestCase):
    def test_only_unseen_stops_are_triaged_a_few_at_a_time(self):
        memory = MagicMock()
        memory.list_stuck_tasks.return_value = [
            _task(task_id="seen", arbiter={"for_since": SINCE}), _task(task_id="a"), _task(task_id="b"),
        ]
        with patch.object(sa, "arbitrate_one", side_effect=lambda engine, task, **_: {"task_id": task["id"]}) as one:
            out = sa.arbitrate_stuck(memory=memory, engine=MagicMock(), model_call=lambda p: "{}", limit=1)
        self.assertEqual([c.args[1]["id"] for c in one.call_args_list], ["a"])
        self.assertEqual(out["waiting"], 1)

    def test_a_ticket_waiting_for_answers_is_left_to_people(self):
        waiting = _task(task_id="t")
        waiting["input"][HUMAN_DECISION_KEY] = {"reason": "ticket_not_ready", "since": SINCE}
        memory = MagicMock()
        memory.list_stuck_tasks.return_value = [waiting]
        with patch.object(sa, "arbitrate_one") as one:
            out = sa.arbitrate_stuck(memory=memory, engine=MagicMock(), model_call=lambda p: "{}")
        one.assert_not_called()
        self.assertEqual(out["checked"], 0)

    def test_switched_off_or_without_a_model_it_does_nothing(self):
        with patch.dict("os.environ", {"TS_HARNESS_STUCK_ARBITER": "off"}):
            self.assertEqual(sa.arbitrate_stuck(memory=MagicMock(), engine=MagicMock())["status"], "off")
        with patch.object(sa.rg, "diagnosis_model", return_value=None):
            self.assertEqual(sa.arbitrate_stuck(memory=MagicMock(), engine=MagicMock())["status"], "skipped")


if __name__ == "__main__":
    unittest.main()
