"""Pause and resume coding work: one ticket, a program or ticket queue, or everything."""

from __future__ import annotations

import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from ts_harness.agent.sessions import SessionRecord
from ts_harness.workflows import pause

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from memory_routes import coding_jobs, shared_state  # noqa: E402
from memory_routes import pause as pause_routes  # noqa: E402
from memory_routes import program_access as program_access_routes  # noqa: E402
from tests.pg_support import PgTestCase, requires_pg  # noqa: E402

AT = "2026-11-28T10:00:00+00:00"


class KvMemory:
    """runtime_kv with compare-and-set, plus the pause routes a resume calls."""

    def __init__(self, state=None, *, workflow_chain=("wf-1",), program_chain=("prog-1",)):
        self.value = copy.deepcopy(state)
        self.version = 1 if state is not None else 0
        self.workflow_chain = list(workflow_chain)
        self.program_chain = list(program_chain)
        self.released_calls = []
        self.shifted = []
        self.held = []

    def kv_get(self, namespace, key):
        assert (namespace, key) == (pause.NAMESPACE, pause.KEY)
        return {"value": copy.deepcopy(self.value), "version": self.version}

    def kv_put(self, namespace, key, value, *, expected_version=None):
        from ts_harness.memory.client import MemoryApiConflict

        if expected_version is not None and expected_version != self.version:
            raise MemoryApiConflict("stale", {"code": "version_conflict"})
        self.value, self.version = copy.deepcopy(value), self.version + 1
        return self.version

    def task_program(self, task_id):
        return {"task_id": task_id, "program_id": self.program_chain[0] if self.program_chain else None,
                "workflow_id": self.workflow_chain[0], "workflow_chain": self.workflow_chain,
                "program_chain": self.program_chain}

    def release_paused_tasks(self, *, tenant_id="default", actor=""):
        self.released_calls.append((tenant_id, actor))
        return {"released": [{"task_id": "t1", "workflow_id": "wf-1", "step_name": "deliver_work_order", "kind": "revision"}],
                "still_held": 0}

    def shift_wait_deadlines(self, **kwargs):
        self.shifted.append(kwargs)
        return {"shifted": 2}

    def list_paused_tasks(self, *, tenant_id="default"):
        return {"tasks": self.held}

    def running_ticket_work(self, *, tenant_id="default"):
        return {"tickets": [], "pools": []}


class PauseStateTests(unittest.TestCase):
    def test_pausing_twice_is_a_no_op_and_resuming_twice_says_not_paused(self):
        memory = KvMemory()
        first = pause.pause("ticket", "wf-1", actor="alice@example.com", reason="design review", label="PROJ-7", client=memory)
        again = pause.pause("ticket", "wf-1", actor="bob@example.com", client=memory)
        self.assertEqual((first["status"], again["status"]), ("paused", "already_paused"))
        self.assertEqual(again["entry"]["by"], "alice@example.com")  # the first pause stands
        self.assertEqual(memory.value["workflows"]["wf-1"]["reason"], "design review")
        resumed = pause.resume("ticket", "wf-1", actor="bob@example.com", client=memory)
        self.assertEqual(resumed["status"], "resumed")
        self.assertIn("paused_seconds", resumed)
        self.assertEqual(pause.resume("ticket", "wf-1", actor="bob@example.com", client=memory)["status"], "not_paused")
        self.assertEqual(memory.value["workflows"], {})

    def test_scopes_are_stored_separately_and_validated(self):
        memory = KvMemory()
        pause.pause("program", "prog-1", actor="a", client=memory)
        pause.pause("global", actor="admin", client=memory)
        self.assertIn("prog-1", memory.value["programs"])
        self.assertEqual(memory.value["global"]["by"], "admin")
        with self.assertRaises(pause.PauseError):
            pause.pause("ticket", "", actor="a", client=memory)
        with self.assertRaises(pause.PauseError):
            pause.pause("moon", "x", actor="a", client=memory)

    def test_the_closest_pause_covers_a_workflow(self):
        state = pause.normalize({
            "global": {"by": "admin"},
            "programs": {"parent-prog": {"by": "carol"}},
            "workflows": {"pool-1": {"by": "bob", "kind": "queue"}, "wf-1": {"by": "alice"}},
        })
        own = pause.entry_for(state, workflow_chain=["wf-1", "pool-1"], program_chain=["prog-1", "parent-prog"])
        self.assertEqual((own["scope"], own["by"]), ("ticket", "alice"))
        queue = pause.entry_for(state, workflow_chain=["wf-2", "pool-1"], program_chain=["prog-1"])
        self.assertEqual((queue["scope"], queue["target_id"]), ("queue", "pool-1"))
        program = pause.entry_for(state, workflow_chain=["wf-3"], program_chain=["prog-1", "parent-prog"])
        self.assertEqual((program["scope"], program["target_id"]), ("program", "parent-prog"))
        # The global pause holds the launcher, not tasks.
        self.assertIsNone(pause.entry_for(state, workflow_chain=["wf-4"], program_chain=["other"]))
        self.assertEqual(pause.entry_for(state, workflow_chain=["wf-4"], include_global=True)["scope"], "global")

    def test_hold_kinds_and_labels(self):
        self.assertEqual(pause.hold_kind({"review_feedback": "fix it", "_revision_source": "qa_gate"}), "revision")
        self.assertEqual(pause.hold_kind({"refresh": True}), "refresh")
        self.assertEqual(pause.hold_kind({}), "first_run")
        self.assertEqual(pause.hold_kind({"qa_feedback": "x"}, retry=True), "retry")
        self.assertEqual(pause.status_label("revision"), "Paused (revision waiting)")
        self.assertEqual(pause.status_label(""), "Paused")

    def test_an_unreadable_state_pauses_nothing(self):
        broken = MagicMock()
        broken.kv_get.side_effect = RuntimeError("memory api down")
        self.assertTrue(pause.is_empty(pause.read_state(broken)))
        with self.assertRaises(RuntimeError):
            pause.read_state(broken, strict=True)

    def test_pauses_of_finished_tickets_are_forgotten(self):
        memory = KvMemory({"workflows": {"wf-done": {"by": "a"}, "wf-live": {"by": "b"}}, "programs": {"p": {"by": "c"}}})
        memory.get_workflow = lambda wid: {"workflow": {"status": "cancelled" if wid == "wf-done" else "running"}}
        state = pause.forget_finished(memory, pause.read_state(memory))
        self.assertEqual(sorted(state["workflows"]), ["wf-live"])
        self.assertEqual(sorted(memory.value["workflows"]), ["wf-live"])
        self.assertIn("p", memory.value["programs"])

    def test_without_paused_drops_only_covered_rows(self):
        memory = KvMemory({"workflows": {"wf-1": {"by": "alice"}}})
        memory.task_program = lambda task_id: {"workflow_chain": [{"t1": "wf-1", "t2": "wf-2"}[task_id]], "program_chain": []}
        rows = [{"id": "t1", "workflow_id": "wf-1"}, {"id": "t2", "workflow_id": "wf-2"}]
        self.assertEqual([r["id"] for r in pause.without_paused(memory, rows)], ["t2"])
        self.assertEqual(pause.without_paused(KvMemory(), rows), rows)  # nothing paused: no chain lookups


class ResumeTests(unittest.TestCase):
    def test_resuming_a_ticket_releases_its_held_tasks_and_moves_deadlines_on(self):
        from ts_harness.gateway import pause_bff

        started = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        memory = KvMemory({"workflows": {"wf-1": {"by": "alice", "at": started}}})
        target = {"target_id": "wf-1", "label": "PROJ-7", "kind": "ticket", "task_id": "t0", "program_id": "prog-1"}
        with patch("ts_harness.jira.ticket_pool.wake_resumed_pools", return_value=[]) as wake:
            result = pause_bff.apply(memory, "resume", "ticket", target, actor="bob@example.com")
        self.assertEqual(result["status"], "resumed")
        self.assertEqual(memory.released_calls, [("default", "bob@example.com")])
        self.assertEqual(memory.shifted[0]["workflow_ids"], ["wf-1"])
        self.assertGreaterEqual(memory.shifted[0]["seconds"], 7190)
        wake.assert_called_once()
        self.assertEqual(pause_bff.message(result), "Resumed PROJ-7. 1 held step picked up where it stopped.")

    def test_a_ticket_resumed_under_a_paused_program_says_it_stays_held(self):
        from ts_harness.gateway import pause_bff

        memory = KvMemory({"workflows": {"wf-1": {"by": "alice", "at": AT}}, "programs": {"prog-1": {"by": "carol", "at": AT}}})
        memory.release_paused_tasks = lambda **_: {"released": [], "still_held": 1}
        target = {"target_id": "wf-1", "label": "PROJ-7", "kind": "ticket", "task_id": "t0", "program_id": "prog-1"}
        with patch("ts_harness.jira.ticket_pool.wake_resumed_pools", return_value=[]):
            result = pause_bff.apply(memory, "resume", "ticket", target, actor="bob")
        self.assertEqual(result["still_paused_by"]["scope"], "program")
        self.assertIn("stays held because its program is still paused (by carol)", pause_bff.message(result))

    def test_resuming_nothing_is_a_clear_no_op(self):
        from ts_harness.gateway import pause_bff

        memory = KvMemory()
        result = pause_bff.apply(memory, "resume", "program", {"target_id": "prog-1", "label": "Checkout", "kind": "program"},
                                 actor="bob")
        self.assertEqual(result["status"], "not_paused")
        self.assertEqual(memory.released_calls, [])
        self.assertEqual(pause_bff.message(result), "Program Checkout is not paused, so there was nothing to resume.")

    def test_messages_confirm_each_level(self):
        from ts_harness.gateway import pause_bff

        memory = KvMemory()
        ticket = pause_bff.apply(memory, "pause", "ticket", {"target_id": "wf-1", "label": "PROJ-7", "kind": "ticket"}, actor="a")
        self.assertIn("Paused PROJ-7. Steps already running finish", pause_bff.message(ticket))
        program = pause_bff.apply(memory, "pause", "program", {"target_id": "p", "label": "Checkout", "kind": "program"}, actor="a")
        self.assertIn("No new tickets start.", pause_bff.message(program))
        everything = pause_bff.apply(memory, "pause", "global", None, actor="admin")
        self.assertIn("Paused all coding", pause_bff.message(everything))
        again = pause_bff.apply(memory, "pause", "global", None, actor="admin")
        self.assertIn("was already paused", pause_bff.message(again))


class LauncherTests(unittest.TestCase):
    def test_a_global_pause_claims_and_starts_nothing_and_stops_no_container(self):
        from ts_harness.coding.launcher import launcher_tick

        memory = MagicMock()
        memory.kv_get.return_value = {"value": {"global": {"by": "admin", "at": AT}}, "version": 3}
        memory.list_coding_jobs.return_value = []
        launcher = MagicMock()
        launcher.name = "local"
        result = launcher_tick(memory, launcher, max_running=2)
        memory.claim_coding_jobs.assert_not_called()
        launcher.start.assert_not_called()
        launcher.stop.assert_not_called()
        self.assertTrue(result["paused"])

    def test_without_a_global_pause_the_launcher_claims_as_before(self):
        from ts_harness.coding.launcher import launcher_tick

        memory = MagicMock()
        memory.kv_get.return_value = {"value": {"programs": {"p": {"by": "a"}}}, "version": 3}
        memory.claim_coding_jobs.return_value = {"jobs": []}
        memory.list_coding_jobs.return_value = []
        result = launcher_tick(memory, MagicMock(name="local"), max_running=2)
        memory.claim_coding_jobs.assert_called_once()
        self.assertFalse(result["paused"])


class FakeCursor:
    def __init__(self, results):
        self.results = list(results)
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchone(self):
        return self.results.pop(0) if self.results else None

    def fetchall(self):
        return self.results.pop(0) if self.results else []


class ClaimHoldTests(unittest.TestCase):
    """Queued jobs a pause covers stay queued (never cancelled) and are not claimed."""

    ROWS = [{"id": "j1", "status": "queued", "workflow_id": "wf-1", "program": "p1"},
            {"id": "j2", "status": "queued", "workflow_id": "wf-2", "program": "p2"},
            {"id": "j3", "status": "running", "workflow_id": "wf-1", "program": "p1"}]

    def test_everything_paused_holds_every_queued_job(self):
        cur = FakeCursor([])
        held = pause_routes.held_job_ids(cur, self.ROWS, {"global": {"by": "admin"}})
        self.assertEqual(held, {"j1": pause_routes.GLOBAL_HELD_REASON, "j2": pause_routes.GLOBAL_HELD_REASON})

    def test_a_ticket_pause_holds_only_its_queued_jobs(self):
        ticket = "00000000-0000-4000-8000-000000000001"
        rows = [{**self.ROWS[0], "workflow_id": ticket}, self.ROWS[1]]
        cur = FakeCursor([[{"id": ticket, "k": "w"}]])
        held = pause_routes.held_job_ids(cur, rows, {"workflows": {ticket: {"by": "a"}}})
        self.assertEqual(held, {"j1": pause_routes.PAUSED_HELD_REASON})
        self.assertEqual(cur.queries[0][1]["workflows"], [ticket])

    def test_nothing_paused_holds_nothing_without_a_query(self):
        cur = FakeCursor([])
        self.assertEqual(pause_routes.held_job_ids(cur, self.ROWS, {}), {})
        self.assertEqual(cur.queries, [])

    def test_a_task_is_held_in_one_statement_with_its_marker(self):
        cur = FakeCursor([{"workflow_id": "wf-1"}])
        conn = MagicMock()
        conn.cursor.return_value = cur
        result = pause_routes.hold_paused_task(conn, {"id": "00000000-0000-4000-8000-000000000002",
                                                       "hold": {"kind": "revision", "by": "alice"}})
        self.assertEqual(result["error"], pause.WAIT_ERROR)
        sql, params = cur.queries[0]
        self.assertIn("UPDATE workflow_tasks", sql)
        self.assertIn("COALESCE(input, '{}'::jsonb) ||", sql)  # merged: the feedback in the input stays
        self.assertEqual(params[0], pause.WAIT_ERROR)
        self.assertIn('"kind": "revision"', params[4])
        conn.commit.assert_called_once()


class SkipPausedTests(unittest.TestCase):
    """The arbiter, readiness re-check, stuck alert and wait escalation leave a paused ticket alone."""

    def memory(self):
        memory = KvMemory({"workflows": {"wf-1": {"by": "alice", "at": AT}}})
        memory.task_program = lambda task_id: {"workflow_chain": [{"t1": "wf-1", "t2": "wf-2"}[task_id]], "program_chain": []}
        memory.list_stuck_tasks = lambda tenant_id="default": [
            {"id": "t1", "workflow_id": "wf-1", "step_name": "finalize_code_review", "updated_at": "2026-11-01T00:00:00+00:00",
             "input": {"_awaiting_human_decision": {"reason": "ticket_not_ready", "jira_issue_key": "PROJ-1", "since": "2026-11-01T00:00:00+00:00"}}},
            {"id": "t2", "workflow_id": "wf-2", "step_name": "finalize_code_review", "updated_at": "2026-11-01T00:00:00+00:00",
             "input": {"_awaiting_human_decision": {"reason": "ticket_not_ready", "jira_issue_key": "PROJ-2", "since": "2026-11-01T00:00:00+00:00"}}},
        ]
        return memory

    def test_the_arbiter_skips_a_paused_ticket(self):
        from ts_harness.workflows import stuck_arbiter

        memory = self.memory()
        rows = memory.list_stuck_tasks()
        for row in rows:
            row["input"] = {"_awaiting_human_decision": {"reason": "revision_limit", "since": AT}}
        memory.list_stuck_tasks = lambda tenant_id="default": rows
        with patch.object(stuck_arbiter, "enabled", return_value=True), \
             patch.object(stuck_arbiter, "arbitrate_one", return_value={"acted": "retry"}) as one:
            stuck_arbiter.arbitrate_stuck(memory=memory, engine=MagicMock(), model_call=lambda prompt: "{}")
        self.assertEqual([c.args[1]["id"] for c in one.call_args_list], ["t2"])

    def test_the_readiness_recheck_skips_a_paused_ticket(self):
        from ts_harness.jira import ticket_readiness

        memory = self.memory()
        jira = MagicMock()
        with patch.object(ticket_readiness, "_comments", return_value=[]) as comments:
            result = ticket_readiness.recheck_waiting_tickets(memory=memory, engine=MagicMock(), jira=jira)
        self.assertEqual(result["checked"], 1)
        self.assertEqual([c.args[1] for c in comments.call_args_list], ["PROJ-2"])

    def test_the_stuck_alert_skips_a_paused_ticket(self):
        from ts_harness import ops_alerts

        memory = self.memory()
        settings = ops_alerts.AlertSettings(stuck_hours=1)
        alerts = ops_alerts.detect_stuck_steps(memory, settings, datetime(2026, 11, 28, tzinfo=timezone.utc))
        self.assertEqual([a.key for a in alerts], ["stuck:t2"])

    def test_waits_of_a_paused_ticket_do_not_escalate(self):
        from ts_harness.gateway import system_jobs

        memory = self.memory()
        memory.list_due_waits = lambda tenant_id="default": {"waits": [
            {"task_id": "t1", "workflow_id": "wf-1", "due": "escalated"},
            {"task_id": "t2", "workflow_id": "wf-2", "due": "escalated"},
        ]}
        marked = []
        memory.mark_wait_deadline = lambda task_id, **kw: marked.append(task_id)
        result = system_jobs.escalate_stale_waits(memory=memory, notify=lambda *a, **k: True)
        self.assertEqual(marked, ["t2"])
        self.assertEqual(result["checked"], 1)


class PoolTests(unittest.TestCase):
    def test_a_paused_pool_refill_starts_nothing(self):
        from ts_harness.jira import ticket_pool

        memory = KvMemory({"workflows": {"pool-1": {"by": "a", "kind": "queue"}}}, workflow_chain=("pool-1",))
        ctx = MagicMock(memory_client=memory)
        task = {"id": "join-1", "workflow_id": "pool-1",
                "input": {"_wait_options": {"children": [{"workflow_id": "c1", "label": "PROJ-1"}]}}}
        with patch("ts_harness.jira.ticket_pool.load_workflow_payload") as load:
            children = ticket_pool.refill(ctx, task)
        load.assert_not_called()
        self.assertEqual([c["label"] for c in children], ["PROJ-1"])

    def test_resume_wakes_only_the_resumed_programs_pools(self):
        from ts_harness.jira import ticket_pool

        memory = KvMemory({}, workflow_chain=("pool-1",), program_chain=("prog-1",))
        memory.running_ticket_work = lambda tenant_id="default": {"pools": [
            {"workflow_id": "pool-1", "join_task_id": "j1", "join_status": "blocked", "queue": []},
            {"workflow_id": "pool-2", "join_task_id": "j2", "join_status": "running", "queue": []},
        ]}
        engine = MagicMock()
        with patch("ts_harness.workflows.engine.get_workflow_engine", return_value=engine):
            woken = ticket_pool.wake_resumed_pools(memory, scope="program", target_id="prog-1")
            other = ticket_pool.wake_resumed_pools(memory, scope="program", target_id="prog-9")
        self.assertEqual((woken, other), (["pool-1"], []))
        self.assertEqual(engine.resume_external.call_args.kwargs["event_type"], "workflow.child_terminal")


class DisplayTests(unittest.TestCase):
    HELD = {"id": "t2", "step_name": "deliver_work_order", "agent_id": "dev-agent", "status": "blocked",
            "error": pause.WAIT_ERROR, "created_at": "2026-11-28T09:00:00+00:00", "updated_at": AT,
            "input": {"review_feedback": "Fix it", pause.HOLD_KEY: {"kind": "revision", "since": AT, "by": "alice@example.com",
                                                                     "scope": "ticket", "reason": "design review"}}}

    def workflow(self):
        first = {"id": "t1", "step_name": "deliver_work_order", "agent_id": "dev-agent", "status": "succeeded",
                 "created_at": "2026-11-28T08:00:00+00:00", "started_at": "2026-11-28T08:00:00+00:00",
                 "completed_at": "2026-11-28T08:30:00+00:00", "input": {}, "output": {}}
        return {"id": "wf-1", "status": "running", "workflow_type": "implement_jira_ticket", "tasks": [first, dict(self.HELD)]}

    def test_a_held_revision_reads_paused_revision_waiting(self):
        from ts_harness.workflows import live_view
        from ts_harness.workflows.phase_activity import summarize_workflow

        summary = summarize_workflow(self.workflow())
        self.assertEqual((summary["state"], summary["kind"]), ("Paused (revision waiting)", "paused"))
        self.assertEqual(live_view.waiting_on(summary), "paused")
        self.assertEqual(summary["stuck_task_id"], "")  # not a decision: not stuck

    def test_the_story_shows_who_paused_and_counts_no_coding_time(self):
        from ts_harness.workflows import ticket_story

        memory = MagicMock()
        memory.list_coding_jobs.return_value = []
        story = ticket_story.ticket_story(memory, self.workflow(), now=datetime(2026, 11, 28, 12, tzinfo=timezone.utc))
        paused = [e for e in story["events"] if e["kind"] == "paused"]
        self.assertEqual(len(paused), 1)
        self.assertIn("Paused (revision waiting), paused by alice@example.com", paused[0]["title"])
        self.assertEqual(paused[0]["seconds"], 7200)
        held_run = [e for e in story["events"] if e["kind"] == "coding" and e["task_id"] == "t2"][0]
        self.assertEqual((held_run["seconds"], held_run["status"]), (0, "paused"))
        totals = {t["category"]: t["seconds"] for t in story["totals"]}
        self.assertEqual(totals.get("paused"), 7200)
        self.assertNotIn("you", totals)

    def test_a_released_hold_shows_who_resumed(self):
        from ts_harness.workflows import ticket_story

        task = dict(self.HELD, status="succeeded", error=None,
                    input={pause.HOLD_KEY: None, pause.LAST_KEY: {"kind": "revision", "since": AT, "by": "alice",
                                                                  "resumed_at": "2026-11-28T11:00:00+00:00", "resumed_by": "bob"}})
        [event] = ticket_story._pause_events(task, datetime(2026, 11, 28, 12, tzinfo=timezone.utc))
        self.assertEqual((event["seconds"], event["status"]), (3600, "done"))
        self.assertIn("Resumed by bob", event["subtitle"])

    def test_live_items_carry_the_pause_that_covers_them(self):
        from ts_harness.workflows import live_view

        state = pause.normalize({"programs": {"prog-1": {"by": "carol", "at": AT, "reason": "release freeze"}}})
        view = live_view.with_pause({}, state, workflow_chain=["wf-1", "pool-1"], programs=["prog-1"])
        self.assertEqual((view["paused"]["scope"], view["paused"]["by"], view["paused"]["reason"]),
                         ("program", "carol", "release freeze"))
        self.assertEqual(live_view._column({"kind": "paused", "waiting_on": "paused", "current_step": ""},
                                           done=False, blocked_by=[]), "paused")

    def test_pausing_is_not_an_intervention_and_adds_no_coding_run(self):
        from ts_harness.improvement.guard import interventions
        from ts_harness.workflows.hooks.revise_on_reject import coding_runs

        tasks = self.workflow()["tasks"]
        self.assertEqual(interventions(tasks), [])  # the canary's autonomy is unaffected
        self.assertEqual(coding_runs(tasks, "deliver_work_order"), 2)  # the held revision counts once, as before


class ChatCommandTests(unittest.TestCase):
    def setUp(self):
        self.session = SessionRecord(session_id="s1", routing_key="default:web", tenant_id="default",
                                     agent_id="orchestrator", source="web", metadata={"program_id": "prog-1"})
        allow = patch("ts_harness.workflows.decisions.channel_permission_refusal", return_value=None)
        self.permission = allow.start()
        self.addCleanup(allow.stop)
        program_ok = patch("ts_harness.gateway.program_access.channel_program_refusal", return_value=None)
        program_ok.start()
        self.addCleanup(program_ok.stop)
        self.memory = KvMemory()
        self.targets = {"wf-7": {"target_id": "wf-7", "label": "PROJ-7", "kind": "ticket", "program_id": "prog-1", "task_id": "t7"},
                        "wf-8": {"target_id": "wf-8", "label": "PROJ-8", "kind": "ticket", "program_id": "prog-1", "task_id": "t8"}}
        items = patch("ts_harness.workflows.live_view._program_items", return_value=[{"id": "wf-7"}, {"id": "wf-8"}])
        items.start()
        self.addCleanup(items.stop)
        target = patch("ts_harness.gateway.pause_bff.ticket_target", side_effect=lambda memory, wid, tenant_id: self.targets[wid])
        target.start()
        self.addCleanup(target.stop)
        wake = patch("ts_harness.jira.ticket_pool.wake_resumed_pools", return_value=[])
        wake.start()
        self.addCleanup(wake.stop)

    def run_command(self, text):
        from ts_harness.gateway.channel_confirmations import execute_pause_command, format_pause_ack

        result = execute_pause_command(self.session, user_text=text, actor_id="alice", channel="web",
                                       auth=MagicMock(actor_id="alice"), actor_email="alice@example.com", memory=self.memory)
        return result, format_pause_ack(result)

    def test_parse(self):
        from ts_harness.gateway.channel_confirmations import parse_pause_command

        self.assertEqual(parse_pause_command("/pause PROJ-7 waiting on design"), ("pause", "PROJ-7 waiting on design"))
        self.assertEqual(parse_pause_command(" /RESUME program"), ("resume", "program"))
        self.assertIsNone(parse_pause_command("please pause it"))
        self.assertIsNone(parse_pause_command("/pausex"))

    def test_pause_and_resume_a_ticket_with_confirmations(self):
        result, text = self.run_command("/pause PROJ-7 waiting on design")
        self.assertEqual(result["status"], "paused")
        self.assertEqual(self.memory.value["workflows"]["wf-7"]["reason"], "waiting on design")
        self.assertEqual(self.memory.value["workflows"]["wf-7"]["by"], "alice@example.com")
        self.assertIn("Paused PROJ-7", text)
        _, again = self.run_command("/pause PROJ-7")
        self.assertIn("was already paused", again)
        result, text = self.run_command("/resume proj-7")
        self.assertEqual(result["status"], "resumed")
        self.assertIn("Resumed PROJ-7. 1 held step picked up where it stopped.", text)
        _, nothing = self.run_command("/resume PROJ-7")
        self.assertIn("is not paused, so there was nothing to resume", nothing)

    def test_the_program_variant(self):
        with patch("ts_harness.gateway.pause_bff.program_target",
                   return_value={"target_id": "prog-1", "label": "Checkout", "kind": "program", "program_id": "prog-1"}):
            result, text = self.run_command("/pause program release freeze")
        self.assertEqual((result["scope"], result["status"]), ("program", "paused"))
        self.assertIn("No new tickets start.", text)

    def test_which_ticket_when_several_run_and_an_unknown_one(self):
        _, ambiguous = self.run_command("/pause")
        self.assertIn("Several tickets are running (PROJ-7, PROJ-8)", ambiguous)
        _, unknown = self.run_command("/pause PROJ-99")
        self.assertIn("can't find ticket PROJ-99", unknown)

    def test_permissions(self):
        self.permission.return_value = "missing permission approve_gate"
        _, text = self.run_command("/pause PROJ-7")
        self.assertEqual(text, "You need the approve_gate permission to pause coding here.")
        self.permission.return_value = "missing permission edit_config"
        _, text = self.run_command("/pause all")
        self.assertIn("Only an admin can pause all coding", text)
        self.assertEqual(self.permission.call_args.kwargs["permission"], "edit_config")
        self.assertIsNone(self.memory.value)


class InboxTests(unittest.TestCase):
    def test_paused_items_say_who_and_what_waits(self):
        from ts_harness.gateway.admin_bff import _paused_items

        memory = KvMemory({"global": {"by": "admin", "at": AT},
                           "workflows": {"wf-7": {"by": "alice", "at": AT, "label": "PROJ-7", "kind": "ticket", "program_id": "p1"}}})
        memory.held = [{"task_id": "t1", "workflow_id": "wf-7", "program_id": "p1", "step_name": "deliver_work_order",
                        "hold": {"kind": "revision"}, "still_paused": True}]
        items = _paused_items(memory, MagicMock(tenant_id="default"), approvable=lambda pid: True, admin=False)
        by_id = {i["id"]: i for i in items}
        self.assertEqual(by_id["global:all"]["meta"]["can_resume"], False)
        ticket = by_id["ticket:wf-7"]
        self.assertEqual(ticket["title"], "Ticket PROJ-7 is paused")
        self.assertEqual(ticket["subtitle"], "Paused by alice · Paused (revision waiting)")
        hidden = _paused_items(memory, MagicMock(tenant_id="default"), approvable=lambda pid: False)
        self.assertEqual([i["id"] for i in hidden], ["global:all"])


class PauseApiTests(unittest.TestCase):
    """POST /admin/pause and /admin/resume: permissions, audit, idempotency; GET /admin/pause."""

    def setUp(self):
        from starlette.testclient import TestClient

        from ts_harness.gateway import asgi_app
        from ts_harness.gateway.auth import AuthError

        self.asgi_app = asgi_app
        self.runtime = MagicMock()
        self.auth = MagicMock(actor_id="u-1", tenant_id="default", email="lee@example.com")
        self.granted = {"view", "approve_gate"}
        self.memory = KvMemory()
        self.memory.get_workflow = lambda wid: {"workflow": {"id": wid, "tenant_id": "default", "status": "running",
                                                             "workflow_type": "implement_jira_ticket", "program_id": "p1"},
                                                "tasks": [{"id": "t1", "input": {"jira_issue_key": "PROJ-7"}}]} if wid == "wf-7" else {}
        self.memory.get_program = lambda pid: {"program": {"id": pid, "tenant_id": "default", "display_name": "Checkout"}}

        def require_admin(runtime, auth, permission="edit_config"):
            if permission not in self.granted:
                raise AuthError(f"missing permission {permission}", 403)

        self.program_checks = []
        patches = [
            patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            patch.object(asgi_app, "_auth", return_value=self.auth),
            patch.object(asgi_app, "_check_origin", return_value=None),
            patch.object(asgi_app, "get_memory_client", return_value=self.memory),
            patch.object(asgi_app, "_require_admin", side_effect=require_admin),
            patch.object(asgi_app, "_require_task_program_level",
                         side_effect=lambda task_id, auth, need, route: self.program_checks.append(("task", task_id, need))),
            patch.object(asgi_app, "_require_program_level",
                         side_effect=lambda pid, auth, need, route, memory=None: self.program_checks.append(("program", pid, need))),
            patch("ts_harness.jira.ticket_pool.wake_resumed_pools", return_value=[]),
            patch("ts_harness.gateway.admin_bff.is_admin", side_effect=lambda auth, runtime: "edit_config" in self.granted),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.client = TestClient(asgi_app.app)

    def audits(self):
        return [c.kwargs for c in self.runtime.audit.append.call_args_list]

    def test_pause_and_resume_a_ticket_with_program_access_and_audit(self):
        response = self.client.post("/admin/pause", json={"scope": "ticket", "workflow_id": "wf-7", "reason": "design"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "paused")
        self.assertIn("Paused PROJ-7", response.json()["message"])
        self.assertEqual(self.program_checks, [("task", "t1", "approve")])
        self.assertEqual(self.memory.value["workflows"]["wf-7"]["by"], "lee@example.com")
        again = self.client.post("/admin/pause", json={"scope": "ticket", "workflow_id": "wf-7"}).json()
        self.assertEqual(again["status"], "already_paused")
        resumed = self.client.post("/admin/resume", json={"scope": "ticket", "workflow_id": "wf-7"}).json()
        self.assertEqual(resumed["status"], "resumed")
        nothing = self.client.post("/admin/resume", json={"scope": "ticket", "workflow_id": "wf-7"}).json()
        self.assertEqual(nothing["status"], "not_paused")
        actions = [(a["action_type"], a["payload"]["status"]) for a in self.audits()]
        self.assertEqual(actions, [("coding_paused", "paused"), ("coding_paused", "already_paused"),
                                   ("coding_resumed", "resumed"), ("coding_resumed", "not_paused")])
        self.assertEqual(self.audits()[0]["payload"]["reason"], "design")

    def test_a_program_pause_needs_approve_on_the_program(self):
        response = self.client.post("/admin/pause", json={"scope": "program", "program_id": "p1"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.program_checks, [("program", "p1", "approve")])
        self.assertIn("p1", self.memory.value["programs"])

    def test_without_approve_gate_nothing_changes(self):
        self.granted = {"view"}
        response = self.client.post("/admin/pause", json={"scope": "ticket", "workflow_id": "wf-7"})
        self.assertEqual(response.status_code, 403)
        self.assertIsNone(self.memory.value)
        self.assertEqual(self.audits(), [])

    def test_the_global_pause_is_admin_only(self):
        response = self.client.post("/admin/pause", json={"scope": "global"})
        self.assertEqual(response.status_code, 403)
        self.granted = {"view", "approve_gate", "edit_config"}
        response = self.client.post("/admin/pause", json={"scope": "global", "reason": "maintenance"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.memory.value["global"]["reason"], "maintenance")
        state = self.client.get("/admin/pause").json()
        self.assertEqual(state["global"]["by"], "lee@example.com")
        self.assertTrue(state["can_pause_global"])

    def test_unknown_targets_and_scopes(self):
        self.assertEqual(self.client.post("/admin/pause", json={"scope": "ticket", "workflow_id": "nope"}).status_code, 404)
        self.assertEqual(self.client.post("/admin/pause", json={"scope": "ticket"}).status_code, 400)
        self.assertEqual(self.client.post("/admin/pause", json={"scope": "moon"}).status_code, 400)


@requires_pg
class PauseSqlTests(PgTestCase):
    """The memory API side against Postgres: hold, release, the claim and the deadlines."""

    SPEC = {"spec_version": 1, "repo_url": "https://github.com/org/web", "base_branch": "main", "work_order_id": "PROJ-1"}

    def setUp(self):
        with self.db.connect() as conn:
            for table in ("coding_jobs", "workflow_tasks", "workflows", "delivery_programs", "runtime_kv"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    def one(self, sql, *params):
        with self.db.connect() as conn:
            row = conn.execute(sql, params).fetchone()
            conn.commit()
        return row

    def program(self, parent=None):
        return str(self.one("INSERT INTO delivery_programs (summary, entry_phase, current_phase, parent_program_id) "
                            "VALUES ('p', 'prd', 'prd', %s) RETURNING id", parent)["id"])

    def workflow(self, program=None, parent=None):
        return str(self.one("INSERT INTO workflows (workflow_type, program_id, parent_workflow_id) "
                            "VALUES ('implement_jira_ticket', %s, %s) RETURNING id", program, parent)["id"])

    def task(self, workflow, data=None, status="running"):
        import json

        return str(self.one("INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, status, input) "
                            "VALUES (%s, 'deliver_work_order', 'dev-agent', %s, %s::jsonb) RETURNING id",
                            workflow, status, json.dumps(data or {}))["id"])

    def state(self, value):
        with self.db.connect() as conn:
            shared_state.kv_put(conn, pause.NAMESPACE, pause.KEY, {"value": value})

    def call(self, fn, *args):
        with self.db.connect() as conn:
            return fn(conn, *args)

    def stored(self, task_id):
        return self.one("SELECT status, error, expected_event_type, input FROM workflow_tasks WHERE id = %s", task_id)

    def test_a_held_revision_resumes_with_its_feedback_once_no_pause_covers_it(self):
        parent = self.program()
        program = self.program(parent=parent)
        ticket = self.workflow(parent=self.workflow(program=program))
        task = self.task(ticket, {"review_feedback": "Persist the empty state", "_revision_source": "qa_gate"})
        self.state({"programs": {parent: {"by": "carol"}}})  # a parent program's pause covers it
        self.call(pause_routes.hold_paused_task, {"id": task, "hold": {"kind": "revision", "by": "carol"}})
        row = self.stored(task)
        self.assertEqual((row["status"], row["error"], row["expected_event_type"]),
                         ("blocked", pause.WAIT_ERROR, pause.EVENT_TYPE))
        self.assertEqual(row["input"]["review_feedback"], "Persist the empty state")
        [listed] = self.call(pause_routes.list_paused_tasks, {})["tasks"]
        self.assertTrue(listed["still_paused"])
        self.assertEqual(listed["hold"]["kind"], "revision")
        self.assertEqual(self.call(pause_routes.release_paused_tasks, {"actor": "bob"}), {"released": [], "still_held": 1})
        self.state({"programs": {}})
        released = self.call(pause_routes.release_paused_tasks, {"actor": "bob"})
        self.assertEqual([r["task_id"] for r in released["released"]], [task])
        row = self.stored(task)
        self.assertEqual((row["status"], row["error"], row["expected_event_type"]), ("pending", None, None))
        self.assertEqual(row["input"]["review_feedback"], "Persist the empty state")
        self.assertIsNone(row["input"][pause.HOLD_KEY])
        self.assertEqual((row["input"][pause.LAST_KEY]["kind"], row["input"][pause.LAST_KEY]["resumed_by"]), ("revision", "bob"))

    def test_the_claim_skips_queued_jobs_of_a_paused_ticket_and_never_cancels_them(self):
        paused_ticket, other = self.workflow(), self.workflow()
        with patch.dict("os.environ", {"CODING_JOB_SECRET": "s3cret"}):
            held = self.call(coding_jobs.create_coding_job, {"spec": self.SPEC, "workflow_id": paused_ticket})["id"]
            free = self.call(coding_jobs.create_coding_job, {"spec": self.SPEC, "workflow_id": other})["id"]
            self.state({"workflows": {paused_ticket: {"by": "alice"}}})
            first = self.call(coding_jobs.claim_coding_jobs, {"max_running": 5})
            self.assertEqual(([j["id"] for j in first["jobs"]], first["held_by_pause"]), ([free], 1))
            row = self.one("SELECT status, held_reason FROM coding_jobs WHERE id = %s", held)
            self.assertEqual((row["status"], row["held_reason"]), ("queued", "paused"))
            self.state({"workflows": {}})
            second = self.call(coding_jobs.claim_coding_jobs, {"max_running": 5})
        self.assertEqual([j["id"] for j in second["jobs"]], [held])

    def test_everything_paused_claims_nothing(self):
        with patch.dict("os.environ", {"CODING_JOB_SECRET": "s3cret"}):
            job = self.call(coding_jobs.create_coding_job, {"spec": self.SPEC, "workflow_id": self.workflow()})["id"]
            self.state({"global": {"by": "admin"}})
            claimed = self.call(coding_jobs.claim_coding_jobs, {"max_running": 5})
        self.assertEqual(claimed["jobs"], [])
        self.assertEqual(self.one("SELECT held_reason FROM coding_jobs WHERE id = %s", job)["held_reason"],
                         pause_routes.GLOBAL_HELD_REASON)

    def test_resume_moves_the_scopes_wait_deadlines_on(self):
        program = self.program()
        inside, outside = self.task(self.workflow(program=program), status="blocked"), self.task(self.workflow(), status="blocked")
        self.one("UPDATE workflow_tasks SET escalate_at = '2026-11-28T10:00:00+00' WHERE id IN (%s, %s) RETURNING id", inside, outside)
        shifted = self.call(pause_routes.shift_wait_deadlines, {"program_ids": [program], "seconds": 3600})
        self.assertEqual(shifted["shifted"], 1)
        moved = self.one("SELECT escalate_at FROM workflow_tasks WHERE id = %s", inside)["escalate_at"]
        kept = self.one("SELECT escalate_at FROM workflow_tasks WHERE id = %s", outside)["escalate_at"]
        self.assertEqual((moved - kept).total_seconds(), 3600)

    def test_task_program_returns_both_chains(self):
        parent = self.program()
        program = self.program(parent=parent)
        pool = self.workflow(program=program)
        ticket = self.workflow(parent=pool)
        found = self.call(program_access_routes.task_program, self.task(ticket))
        self.assertEqual(found["workflow_chain"], [ticket, pool])
        self.assertEqual(found["program_chain"], [program, parent])


if __name__ == "__main__":
    unittest.main()
