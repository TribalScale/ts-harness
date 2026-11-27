"""Self-improvement foundation: run tagging, interventions, the post-merge guard, grouping, the budget, the off-limits list."""

from __future__ import annotations

import copy
import importlib.util
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.pg_support import PgTestCase, requires_pg

from ts_harness.improvement import budget as ib
from ts_harness.improvement import guard as gd
from ts_harness.improvement import versions as vs
from ts_harness.improvement.off_limits import OFF_LIMITS, is_off_limits
from ts_harness.memory.client import MemoryApiConflict

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 1, 14, 12, 0, tzinfo=timezone.utc)


class FakeKv:
    """runtime_kv with versions (the memory API's compare-and-swap)."""

    def __init__(self):
        self.kv: dict[tuple[str, str], tuple[object, int]] = {}

    def kv_get(self, namespace, key):
        value, version = self.kv.get((namespace, key), (None, 0))
        return {"value": copy.deepcopy(value), "version": version}

    def kv_put(self, namespace, key, value, *, expected_version=None):
        _, version = self.kv.get((namespace, key), (None, 0))
        if expected_version is not None and expected_version != version:
            raise MemoryApiConflict("stale", {"code": "version_conflict"})
        self.kv[(namespace, key)] = (copy.deepcopy(value), version + 1)
        return version + 1


# Run tagging


class VersionsTests(unittest.TestCase):
    def skeleton(self):
        return SimpleNamespace(workflow_type="implement_jira_ticket", definition_version="3.2.0",
                               includes=[{"name": "verified_merge", "ref": "verified_merge@1.10.0"}])

    def test_a_step_records_workflow_fragment_skill_model_and_variant_versions(self):
        task = {"id": "t1", "workflow_id": "wf-1", "workflow_type": "implement_jira_ticket", "definition_version": "3.2.0",
                "step_name": "code_review", "agent_id": "review-agent", "skill_hints": ["governed-code-review"]}
        out = vs.versions_for_task(
            task, skeleton=self.skeleton(),
            skill_versions={"governed-code-review": "1.11.0", "other": "2.0.0"},
            models={"review-agent": "anthropic/claude-sonnet-5-5", "qa-agent": "openai/gpt-5", "dev-agent": "x/y"},
            variants={"canary": "b"},
        )
        self.assertEqual(out, {
            "schema": 1,
            "workflow": {"type": "implement_jira_ticket", "version": "3.2.0"},
            "fragments": {"verified_merge": "1.10.0"},
            "step": "code_review",
            "skills": {"governed-code-review": "1.11.0"},
            "models": {"agent": "anthropic/claude-sonnet-5-5",
                       "gates": {"review-agent": "anthropic/claude-sonnet-5-5", "qa-agent": "openai/gpt-5"}},
            "variants": {"canary": "b"},
        })

    def test_a_coding_job_adds_its_models_the_review_variant_and_the_gate_skill(self):
        base = {"schema": 1, "step": "deliver_work_order", "variants": {"canary": "b"}}
        spec = {"model": "anthropic/claude-sonnet-5-5", "review_model": "anthropic/claude-opus-5-5", "mode": "implement",
                "review_variant": "gate_inputs", "review_rubric": "## Severity ..."}
        out = vs.with_coding_spec(base, spec, skill_versions={"governed-code-review": "1.11.0"})
        self.assertEqual(out["models"], {"coding": "anthropic/claude-sonnet-5-5", "review": "anthropic/claude-opus-5-5"})
        self.assertEqual(out["variants"], {"canary": "b", "pre_push": "gate_inputs"})
        self.assertEqual(out["skills"], {"governed-code-review": "1.11.0"})
        legacy = vs.with_coding_spec(None, {"model": "m", "review_model": "r", "mode": "implement"}, skill_versions={})
        self.assertEqual(legacy["variants"], {"pre_push": "legacy"})
        refresh = vs.with_coding_spec(None, {"model": "m", "mode": "refresh"}, skill_versions={})
        self.assertEqual(refresh["variants"], {})

    def test_ticket_versions_merge_oldest_first_and_lookup_reads_dotted_keys(self):
        tasks = [
            {"created_at": "2026-01-02", "input": {"_versions": {"step": "qa_gate", "skills": {"governed-qa-gate": "2.0.0"},
                                                                  "variants": {"pre_push": "legacy"}}}},
            {"created_at": "2026-01-01", "input": {"_versions": {"step": "deliver_work_order", "models": {"coding": "m1"},
                                                                  "variants": {"pre_push": "gate_rubric"}}}},
            {"created_at": "2026-01-03", "input": {}},
        ]
        merged = vs.ticket_versions(tasks)
        self.assertNotIn("step", merged)
        self.assertEqual(vs.lookup(merged, "variants.pre_push"), "legacy")  # the later step wins
        self.assertEqual(vs.lookup(merged, "skills.governed-qa-gate"), "2.0.0")
        self.assertEqual(vs.lookup(merged, "models.coding"), "m1")
        self.assertIsNone(vs.lookup(merged, "models"))  # a section is not a value
        self.assertIsNone(vs.lookup(merged, "variants.missing"))
        self.assertEqual(vs.lookup({"skills": {"a.b": "1.0"}}, "skills.a.b"), "1.0")

    def test_tagging_never_raises(self):
        self.assertEqual(vs.safe_versions(lambda: 1 / 0), {})
        self.assertEqual(vs.without_versions({"a": 1, "_versions": {}}), {"a": 1})

    def test_a_ticket_is_assigned_to_a_variant_and_cleared(self):
        memory = FakeKv()
        self.assertEqual(vs.set_ticket_variant("wf-1", "canary", "b", memory=memory), {"canary": "b"})
        self.assertEqual(vs.set_ticket_variant("wf-1", "prompt", "v2", memory=memory), {"canary": "b", "prompt": "v2"})
        self.assertEqual(vs.ticket_variants("wf-1", memory=memory), {"canary": "b", "prompt": "v2"})
        self.assertEqual(vs.set_ticket_variant("wf-1", "canary", None, memory=memory), {"prompt": "v2"})
        with self.assertRaises(ValueError):
            vs.set_ticket_variant("wf-1", "has space", "x", memory=memory)
        broken = mock.Mock(kv_get=mock.Mock(side_effect=RuntimeError("down")))
        self.assertEqual(vs.ticket_variants("wf-1", memory=broken), {})

    def test_the_coding_job_spec_carries_versions_but_the_run_does_not_see_them(self):
        from ts_harness.implementation.coding_job import coding_request_from_spec
        from ts_harness.workflows import coding_job_stage

        spec = coding_job_stage.with_versions(
            {"model": "m", "review_model": "r", "mode": "implement", "repo_url": "https://github.com/org/api",
             "base_branch": "main", "workspace_dir": "/tmp/x"},
            {"_versions": {"schema": 1, "variants": {"canary": "a"}}},
        )
        self.assertEqual(spec["versions"]["variants"], {"canary": "a", "pre_push": "legacy"})
        self.assertEqual(spec["versions"]["models"], {"coding": "m", "review": "r"})
        with mock.patch("ts_harness.implementation.coding_job.CodingRequest", side_effect=lambda **kw: kw):
            fields = coding_request_from_spec(spec)
        self.assertNotIn("versions", fields)
        with mock.patch.object(vs, "with_coding_spec", side_effect=RuntimeError("boom")):
            self.assertNotIn("versions", coding_job_stage.with_versions({"model": "m"}, {}))

    def test_the_agent_loop_drops_versions_before_the_model_sees_the_input(self):
        source = (REPO_ROOT / "runtime" / "ts_harness" / "agent" / "loop.py").read_text()
        drop = source.index('if k != "_versions"')
        self.assertLess(drop, source.index("system_prompt = self._build_workflow_system_prompt(agent, payload)"))


def load_runner():
    spec = importlib.util.spec_from_file_location("workflow_runner_si", REPO_ROOT / "scripts" / "workflow-runner.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunnerTaggingTests(unittest.TestCase):
    class Memory(FakeKv):
        def __init__(self, task):
            super().__init__()
            self.ready = [task]
            self.patched: list[tuple[str, dict]] = []
            self.completed: list[tuple[str, dict]] = []

        def list_stale_tasks(self):
            return []

        def list_ready_tasks(self, limit):
            return self.ready[:limit]

        def claim_task(self, task, locked_by, lease_seconds):
            return True

        def record_runtime_task(self, task_id, runtime_task_id):
            pass

        def complete_task(self, task_id, output):
            self.completed.append((task_id, output))

        def acquire_leader(self, runner_id, ttl_seconds):
            return True

        def patch_task_input(self, task_id, input_data, *, actor="orchestrator"):
            self.patched.append((task_id, input_data))
            return {}

    def run_task(self, memory_cls=None, **env):
        runner = load_runner()
        wd = runner.workflow_definitions
        skeleton = wd.load_skeleton_payload({"workflow_type": "campaign", "definition_version": "1.4.0", "stages": [
            {"name": "publish", "kind": "task", "agent_id": "pm-agent"}]})
        task = {"id": "task-1", "workflow_id": "wf-1", "workflow_type": "campaign", "step_name": "publish",
                "agent_id": "pm-agent", "definition_version": "1.4.0", "input": {"topic": "x"}}
        memory = (memory_cls or self.Memory)(task)
        vs.set_ticket_variant("wf-1", "canary", "b", memory=memory)
        adapter = mock.Mock()
        adapter.dispatch.return_value = {"status": "succeeded", "output": {"summary": "done"}}
        with mock.patch.dict(os.environ, {"TS_HARNESS_HOOK_ENGINE": "false", **env}):
            runner.WorkflowRunner(memory, adapter, runner_id="r1", skeletons={"campaign": skeleton}).tick()
        return memory, adapter.dispatch.call_args.args[0]

    def test_dispatch_stores_versions_on_the_task_and_keeps_them_from_the_agent(self):
        memory, payload = self.run_task()
        self.assertNotIn("_versions", payload["input"])
        ((task_id, stored),) = memory.patched
        self.assertEqual(task_id, "task-1")
        self.assertEqual(stored["topic"], "x")
        self.assertEqual(stored["_versions"]["workflow"], {"type": "campaign", "version": "1.4.0"})
        self.assertEqual(stored["_versions"]["variants"], {"canary": "b"})
        self.assertEqual(stored["_versions"]["step"], "publish")

    def test_tagging_failures_never_stop_the_dispatch(self):
        class Broken(self.Memory):
            def patch_task_input(self, *args, **kwargs):
                raise RuntimeError("memory api down")

        memory, payload = self.run_task(Broken)
        self.assertEqual(memory.completed, [("task-1", {"summary": "done"})])
        with mock.patch.object(vs, "versions_for_task", side_effect=RuntimeError("boom")):
            memory, payload = self.run_task()
        self.assertEqual(memory.patched, [])
        self.assertEqual(memory.completed, [("task-1", {"summary": "done"})])

    def test_tagging_can_be_turned_off(self):
        memory, payload = self.run_task(TS_HARNESS_RUN_TAGGING="off")
        self.assertEqual(memory.patched, [])


# Interventions and the decision log


class InterventionTests(unittest.TestCase):
    def test_each_kind_of_person_stepping_in(self):
        tasks = [
            {"input": {"_decision_log": [
                {"action": "retry", "actor": "harness-arbiter"},
                {"action": "revise", "actor": "dana@example.com", "after_arbiter": "retry"},
                {"action": "retry", "actor": "jira-reply"},
            ]}},
            {"input": {"_revision_grant": {"actor": "dana@example.com"}, "human_change_requests": ["rename it"]}},
            {"input": {"_revision_grant": {"actor": "harness-triage"}}},
            {"input": {"_human_accept": {"actor": "lee@example.com"}}},
            {"error": "stopped_by_human: lee@example.com: no longer needed", "input": {}},
            {"error": "external_blocked:github.pr_changes_requested by lee", "input": {}},
            {"error": "external_blocked:github.pr_closed by lee", "input": {}},
        ]
        self.assertEqual(gd.interventions(tasks), sorted([
            "decision_revise", "jira_reply", "revise_grant", "human_change_requests", "accepted_by_person",
            "stopped_by_person", "pr_changes_requested", "pr_closed",
        ]))
        self.assertEqual(gd.arbiter_overrules(tasks), 1)

    def test_the_harness_on_its_own_is_not_a_person(self):
        tasks = [
            {"input": {"_decision_log": [{"action": "override", "actor": "harness-arbiter"},
                                         {"action": "retry", "actor": "harness-arbiter"}],
                       "_auto_grant": True, "_human_accept": {"actor": "harness-triage"}}},
            {"error": "stopped_by_human: harness-cancelled: superseded", "input": {}},
        ]
        self.assertEqual(gd.interventions(tasks), [])

    def test_a_decision_is_logged_on_the_step_with_the_arbiter_it_overruled(self):
        from ts_harness.workflows import human_decisions
        from ts_harness.workflows.materialize import load_skeleton

        skeleton = load_skeleton("deliver_work_order")
        task = {"id": "t1", "step_name": "deliver_work_order", "status": "blocked", "workflow_type": "deliver_work_order",
                "error": "transport_error: coding job lost",
                "input": {"work_order_id": "WO-1", "_awaiting_human_decision": {"reason": "transport"},
                          "_arbiter": {"actions": [{"action": "retry", "at": "2026-01-01T00:00:00Z"}]}}}
        engine = mock.MagicMock()
        engine.client.get_workflow.return_value = {"workflow": {"id": "wf"}, "tasks": [task]}
        engine._context.return_value = mock.MagicMock(stage=skeleton.stage("deliver_work_order"), extras={}, workflow={})
        with mock.patch("ts_harness.workflows.hooks.notify_bound_channel.handle"):
            human_decisions.decide(engine, "wf", "t1", "retry", actor="dana@example.com")
        task_id, stored = engine.client.patch_task_input.call_args.args
        self.assertEqual(task_id, "t1")
        (entry,) = stored["_decision_log"]
        self.assertEqual((entry["action"], entry["actor"], entry["after_arbiter"]), ("retry", "dana@example.com", "retry"))
        self.assertEqual(gd.arbiter_overrules([{"input": stored}]), 1)

    def test_a_decision_stands_when_the_log_cannot_be_written(self):
        from ts_harness.workflows import human_decisions
        from ts_harness.workflows.materialize import load_skeleton

        skeleton = load_skeleton("deliver_work_order")
        task = {"id": "t1", "step_name": "deliver_work_order", "status": "blocked", "workflow_type": "deliver_work_order",
                "error": "transport_error: coding job lost", "input": {"_awaiting_human_decision": {"reason": "transport"}}}
        engine = mock.MagicMock()
        engine.client.get_workflow.return_value = {"workflow": {"id": "wf"}, "tasks": [task]}
        engine.client.patch_task_input.side_effect = RuntimeError("down")
        engine._context.return_value = mock.MagicMock(stage=skeleton.stage("deliver_work_order"), extras={}, workflow={})
        with mock.patch("ts_harness.workflows.hooks.notify_bound_channel.handle"):
            result = human_decisions.decide(engine, "wf", "t1", "retry", actor="dana@example.com")
        self.assertEqual(result["status"], "retried")

    def test_harness_actors(self):
        from ts_harness.workflows.human_decisions import is_harness_actor

        self.assertTrue(is_harness_actor("harness-arbiter"))
        self.assertTrue(is_harness_actor("harness-anything-new"))
        self.assertTrue(is_harness_actor("workflow-runner"))
        self.assertFalse(is_harness_actor("jira-reply"))
        self.assertFalse(is_harness_actor("admin"))  # a shared-token sign-in is a person


# Post-merge guard


class FakeGitHub:
    def __init__(self, commits=None, fail=False):
        self.commits = commits or []
        self.fail = fail
        self.calls: list[str] = []

    def get_pull(self, owner, repo, number):
        self.calls.append("pull")
        if self.fail:
            raise RuntimeError("rate limited")
        return {"base": {"ref": "main"}, "merge_commit_sha": "abc1234def5678", "title": "Add notes"}

    def request(self, method, path, *, query=None, **kwargs):
        self.calls.append(path)
        assert query and query["sha"] == "main"
        return self.commits


class FakeJira:
    def __init__(self, issues=None, fail=False):
        self.issues = issues or []
        self.fail = fail
        self.jql: list[str] = []

    def search(self, jql, *, max_results=10, fields=None):
        self.jql.append(jql)
        if self.fail:
            raise RuntimeError("401")
        return self.issues


def merged_row(**extra):
    return {"outcome": "merged", "pr_url": "https://github.com/org/api/pull/7", "jira_issue_key": "PROJ-7",
            "repo": "org/api", **extra}


class GuardTests(unittest.TestCase):
    def setUp(self):
        gd.clear_cache()
        self.addCleanup(gd.clear_cache)
        self.merged_at = NOW - timedelta(days=3)

    def ctx(self, github=None, jira=None, memory=None, **kwargs):
        return gd.GuardContext(now=NOW, github_for=lambda url: github or FakeGitHub(),
                               jira_factory=lambda: jira or FakeJira(), memory=memory or FakeKv(), **kwargs)

    def test_reverts_jira_bugs_and_smoke_failures_after_the_merge(self):
        from ts_harness.workflows import nightly_smoke as ns

        github = FakeGitHub(commits=[
            {"sha": "r1", "commit": {"message": 'Revert "Add notes" (#9)\n\nThis reverts commit abc1234.'}},
            {"sha": "r2", "commit": {"message": "Reverts org/api#7"}},
            {"sha": "x", "commit": {"message": "Unrelated fix"}},
        ])
        jira = FakeJira(issues=[{"key": "PROJ-20"}])
        memory = FakeKv()
        start = self.merged_at.timestamp()
        memory.kv_put(ns.KV_NAMESPACE, ns.api_index_key("org/api"), {"failures": {
            "a": {"at": start + 3600, "web_repo": "org/web"},
            "b": {"at": start - 3600, "web_repo": "org/web"},  # before the merge
        }})
        tasks = [{"step_name": "await_human_merge", "status": "succeeded", "output": {"merge_commit_sha": "abc1234def5678"}}]
        out = gd.post_merge(self.ctx(github, jira, memory), merged_row(), tasks, self.merged_at)
        self.assertEqual((out["reverts"], out["jira_bugs"], out["smoke_failures"], out["problems"]), (2, 1, 1, 4))
        self.assertEqual(out["unknown"], [])
        self.assertTrue(out["window_open"])
        self.assertIn('linkedIssues("PROJ-7")', jira.jql[0])
        self.assertIn("issuetype = Bug", jira.jql[0])

    def test_unreadable_sources_are_unknown_not_errors(self):
        out = gd.post_merge(self.ctx(FakeGitHub(fail=True), FakeJira(fail=True),
                                     mock.Mock(kv_get=mock.Mock(side_effect=RuntimeError("down")))),
                            merged_row(), [], self.merged_at)
        self.assertEqual((out["reverts"], out["jira_bugs"], out["smoke_failures"]), (None, None, None))
        self.assertEqual(out["unknown"], ["reverts", "jira_bugs", "smoke_failures"])
        self.assertEqual(out["problems"], 0)

    def test_lookups_are_bounded_and_cached(self):
        github = FakeGitHub()
        ctx = self.ctx(github, max_lookups=1)
        self.assertEqual(gd.reverts(ctx, "https://github.com/org/api/pull/7", self.merged_at), 0)
        self.assertIsNone(gd.reverts(ctx, "https://github.com/org/api/pull/8", self.merged_at))  # over the limit
        calls = len(github.calls)
        self.assertEqual(gd.reverts(self.ctx(github), "https://github.com/org/api/pull/7", self.merged_at), 0)
        self.assertEqual(len(github.calls), calls)  # served from the cache

    def test_an_invalid_key_is_never_sent_to_jira(self):
        jira = FakeJira()
        self.assertIsNone(gd.jira_bugs(self.ctx(jira=jira), 'PROJ-1" OR project = X', self.merged_at))
        self.assertEqual(jira.jql, [])

    def test_unmerged_tickets_only_count_arbiter_overrules(self):
        tasks = [{"input": {"_decision_log": [{"action": "accept", "actor": "dana", "after_arbiter": "override"}]}}]
        out = gd.post_merge(self.ctx(), {"outcome": "stopped", "pr_url": "", "jira_issue_key": "", "repo": ""}, tasks, None)
        self.assertEqual((out["arbiter_overruled"], out["reverts"], out["problems"], out["unknown"]), (1, None, 0, []))

    def test_summary_reports_unknown_counts(self):
        rows = [
            {**merged_row(), "guard": {"reverts": 1, "jira_bugs": None, "smoke_failures": 0, "arbiter_overruled": 0,
                                       "problems": 1, "unknown": ["jira_bugs"], "window_open": True}},
            {**merged_row(), "guard": {"reverts": 0, "jira_bugs": None, "smoke_failures": 0, "arbiter_overruled": 2,
                                       "problems": 0, "unknown": ["jira_bugs"], "window_open": False}},
        ]
        summary = gd.summarize(rows, 14)
        self.assertEqual((summary["tickets_with_problems"], summary["problem_rate"], summary["fully_checked"]), (1, 0.5, 0))
        self.assertEqual(summary["signals"]["reverts"], {"count": 1, "checked": 2, "unknown": 0})
        self.assertEqual(summary["signals"]["jira_bugs"], {"count": None, "checked": 0, "unknown": 2})
        self.assertEqual(summary["signals"]["arbiter_overruled"]["count"], 2)


# Metrics: autonomy, guard and grouping by versions


class MetricsTests(unittest.TestCase):
    def setUp(self):
        gd.clear_cache()
        self.addCleanup(gd.clear_cache)

    def metrics(self, **kwargs):
        from tests.runtime import test_delivery_metrics as base
        from ts_harness.workflows import delivery_metrics as dm

        first_key, first, first_jobs = base.first_try_ticket()
        revised_key, revised, revised_jobs = base.revised_ticket()
        first = copy.deepcopy(first)
        first[1]["input"] = {"_versions": {"variants": {"pre_push": "gate_inputs"}, "models": {"coding": "m1"}}}
        revised = copy.deepcopy(revised)
        revised[1]["input"] = {"_versions": {"variants": {"pre_push": "legacy"}}}
        memory = base.memory_for([(first_key, first, first_jobs), (revised_key, revised, revised_jobs)])
        return dm.delivery_metrics(memory, allowed=lambda pid: pid == "p1", now=base.NOW, **kwargs)

    def test_autonomy_counts_merges_no_person_stepped_in(self):
        out = self.metrics(guard=False)
        rows = {r["jira_issue_key"]: r for r in out["tickets"]}
        self.assertTrue(rows["PROJ-1"]["autonomous"])
        self.assertEqual(rows["PROJ-2"]["interventions"], ["revise_grant"])
        self.assertEqual(out["summary"]["autonomy"], {"autonomous": 1, "rate": 0.5, "merged_share": 0.5, "intervened": 1,
                                                      "by_kind": {"revise_grant": 1}})

    def test_without_the_guard_post_merge_signals_are_unknown(self):
        out = self.metrics(guard=False)
        signals = out["summary"]["guard"]["signals"]
        self.assertIsNone(signals["reverts"]["count"])
        self.assertEqual(signals["arbiter_overruled"]["count"], 0)

    def test_grouped_by_a_versions_key(self):
        out = self.metrics(guard=False, group_by="variants.pre_push")
        self.assertIn("variants.pre_push", out["facets"]["group_keys"])
        self.assertIn("models.coding", out["facets"]["group_keys"])
        groups = {g["value"]: g for g in out["groups"]}
        self.assertEqual(set(groups), {"gate_inputs", "legacy"})
        self.assertEqual(groups["gate_inputs"]["summary"]["autonomy"]["rate"], 1.0)
        self.assertEqual(groups["legacy"]["summary"]["autonomy"]["rate"], 0.0)
        none = self.metrics(guard=False, group_by="skills.nothing")
        self.assertEqual([(g["value"], g["tickets"]) for g in none["groups"]], [("(none)", 2)])

    def test_the_guard_reads_github_once_per_merged_ticket(self):
        from tests.runtime import test_delivery_metrics as base

        github = FakeGitHub()
        ctx = gd.GuardContext(now=base.NOW, github_for=lambda url: github, jira_factory=lambda: FakeJira(), memory=FakeKv())
        out = self.metrics(guard_context=ctx)
        self.assertEqual(out["summary"]["guard"]["signals"]["reverts"], {"count": 0, "checked": 2, "unknown": 0})
        self.assertEqual(github.calls.count("pull"), 2)


class MetricsEndpointTests(unittest.TestCase):
    def test_the_endpoint_passes_group_by_and_shows_the_improvement_budget(self):
        from starlette.testclient import TestClient

        from tests.runtime import test_delivery_metrics as base
        from ts_harness.gateway import asgi_app
        from ts_harness.gateway.auth import AuthContext
        from ts_harness.workflows import delivery_metrics as dm

        runtime = mock.Mock()
        runtime.config = SimpleNamespace(identity=SimpleNamespace(role_bundles={}, service_permissions=None))
        memory = base.memory_for([base.first_try_ticket()])
        memory.improvement_spend.return_value = {"spent_usd": 20.5, "by_purpose": {"digest": {"spent_usd": 20.5, "charges": 2}}}
        seen = {}

        def fake_metrics(*args, **kwargs):
            seen.update(kwargs)
            return {"summary": {}, "tickets": []}

        auth = AuthContext(actor_id="ada", tenant_id="default", roles=["viewer"], auth_provider="oidc")
        with mock.patch.object(asgi_app, "get_runtime", return_value=runtime), \
                mock.patch.object(asgi_app, "_auth", return_value=auth), \
                mock.patch.object(asgi_app, "_check_origin", return_value=None), \
                mock.patch.object(asgi_app, "get_memory_client", return_value=memory), \
                mock.patch.object(dm, "delivery_metrics", side_effect=fake_metrics), \
                mock.patch.dict(os.environ, {ib.CAP_ENV: "100"}):
            response = TestClient(asgi_app.app).get("/admin/metrics?group_by=variants.pre_push&guard=0&guard_days=30")
        self.assertEqual(response.status_code, 200)
        self.assertEqual((seen["group_by"], seen["guard"], seen["guard_days"]), ("variants.pre_push", False, 30))
        budget = response.json()["improvement_budget"]
        self.assertEqual((budget["cap_usd"], budget["spent_usd"], budget["remaining_usd"], budget["paused"]), (100.0, 20.5, 79.5, False))


# Improvement budget


class FakeLedger:
    def __init__(self, spent=0.0, fail=False):
        self.spent = spent
        self.fail = fail
        self.charges: list[dict] = []

    def improvement_spend(self, *, tenant_id="default", month=""):
        if self.fail:
            raise RuntimeError("memory api down")
        return {"spent_usd": self.spent, "by_purpose": {}, "recent": []}

    def charge_improvement(self, **body):
        if self.fail:
            raise RuntimeError("memory api down")
        self.charges.append(body)
        self.spent += body["cost_usd"]
        return {"spent_usd": self.spent}


class FakeProvider:
    def __init__(self, usage):
        self.usage = usage
        self.prompts: list[str] = []

    def chat(self, messages, tools=None, *, temperature=0.2):
        self.prompts.append(messages[0]["content"])
        return {"choices": [{"message": {"content": "ok"}}], "usage": self.usage}


class BudgetTests(unittest.TestCase):
    def budget(self, ledger, cap=150.0):
        return ib.ImprovementBudget(memory=ledger, cap_usd=cap, now=lambda: NOW)

    def test_remaining_can_spend_and_pause_at_the_cap(self):
        budget = self.budget(FakeLedger(spent=140.0))
        self.assertEqual(budget.remaining(), 10.0)
        self.assertTrue(budget.can_spend(5))
        self.assertFalse(budget.can_spend(11))
        status = self.budget(FakeLedger(spent=150.0)).status()
        self.assertTrue(status["paused"])
        self.assertEqual((status["month"], status["remaining_usd"]), ("2026-01", 0.0))
        self.assertFalse(self.budget(FakeLedger(spent=150.0)).can_spend(0))

    def test_an_unreadable_ledger_pauses_improvement_work(self):
        budget = self.budget(FakeLedger(fail=True))
        self.assertFalse(budget.can_spend(0))
        self.assertEqual(budget.remaining(), 0.0)
        self.assertFalse(budget.status()["available"])
        self.assertIsNone(budget.charge(1.0, "digest"))  # logged, never raised

    def test_charge_records_purpose_and_ref(self):
        ledger = FakeLedger()
        self.budget(ledger).charge(0.25, "weekly_digest", "digest-1", model="m", input_tokens=10, output_tokens=5)
        self.assertEqual(ledger.charges, [{"tenant_id": "default", "cost_usd": 0.25, "purpose": "weekly_digest",
                                           "ref": "digest-1", "model": "m", "actor": "self-improvement",
                                           "input_tokens": 10, "output_tokens": 5}])

    def test_tracked_model_calls_are_charged_at_their_actual_cost(self):
        ledger = FakeLedger()
        provider = FakeProvider({"prompt_tokens": 1_000_000, "completion_tokens": 100_000})
        budget = self.budget(ledger)
        from ts_harness.llm_proxy.metering import ModelPrice

        with mock.patch("ts_harness.llm_usage.pricing", return_value={"test/model": ModelPrice(3.0, 15.0)}), \
                mock.patch("ts_harness.agent.provider.resolve_provider", side_effect=RuntimeError("no keys")):
            with budget.track("proposal", ref="p-1", model="test/model", provider_factory=lambda m: provider) as call:
                self.assertEqual(call("first"), "ok")
                call("second")
        (charge,) = ledger.charges
        self.assertEqual((charge["purpose"], charge["ref"], charge["cost_usd"]), ("proposal", "p-1", 9.0))
        self.assertEqual((charge["input_tokens"], charge["output_tokens"]), (2_000_000, 200_000))

    def test_a_used_up_budget_stops_the_call_before_it_runs(self):
        provider = FakeProvider({})
        call = self.budget(FakeLedger(spent=150.0)).model_call("replay", model="test/model", provider_factory=lambda m: provider)
        with self.assertRaises(ib.BudgetExhausted):
            call("anything")
        self.assertEqual(provider.prompts, [])

    def test_the_cap_comes_from_the_env_then_the_config_then_150(self):
        config = SimpleNamespace(budgets=SimpleNamespace(improvement_monthly_usd=80.0))
        with mock.patch.dict(os.environ, {ib.CAP_ENV: "40"}):
            self.assertEqual(ib.monthly_cap(config), 40.0)
        with mock.patch.dict(os.environ, {ib.CAP_ENV: ""}):
            self.assertEqual(ib.monthly_cap(config), 80.0)
            self.assertEqual(ib.monthly_cap(SimpleNamespace()), 150.0)
        from ts_harness.config.schema import parse_budgets

        self.assertEqual(parse_budgets({}).improvement_monthly_usd, 150.0)
        self.assertEqual(parse_budgets({"improvement_monthly_usd": 25}).improvement_monthly_usd, 25.0)
        with self.assertRaises(ValueError):
            parse_budgets({"improvement_monthly_usd": -1})


@requires_pg
class ImprovementLedgerPgTests(PgTestCase):
    def setUp(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        with self.db.connect() as conn:
            conn.execute("DELETE FROM improvement_spend")
            conn.commit()

    def test_charges_are_summed_per_month_and_purpose(self):
        from memory_routes import budgets

        with self.db.connect() as conn:
            budgets.charge_improvement(conn, {"cost_usd": 1.25, "purpose": "digest", "ref": "d-1", "model": "m"})
            out = budgets.charge_improvement(conn, {"cost_usd": "0.5", "purpose": "replay", "input_tokens": 10})
            conn.execute("INSERT INTO improvement_spend (cost_usd, purpose, month) VALUES (99, 'digest', '2025-12-01')")
            conn.commit()
            self.assertEqual(out["spent_usd"], 1.75)
            self.assertEqual(out["by_purpose"]["digest"], {"spent_usd": 1.25, "charges": 1})
            self.assertEqual(len(out["recent"]), 2)
            self.assertEqual(budgets.improvement_spend(conn, {"month": "2025-12"})["spent_usd"], 99.0)
            self.assertEqual(budgets.improvement_spend(conn, {"tenant_id": "other"})["spent_usd"], 0)

    def test_bad_charges_are_refused(self):
        from memory_routes import budgets

        with self.db.connect() as conn:
            for body in ({"purpose": "digest"}, {"cost_usd": -1, "purpose": "digest"}, {"cost_usd": 1},
                         {"cost_usd": 5000, "purpose": "digest"}):
                with self.assertRaises(ValueError):
                    budgets.charge_improvement(conn, body)
            with self.assertRaises(ValueError):
                budgets.improvement_spend(conn, {"month": "2026-13"})


class LedgerRouteTests(unittest.TestCase):
    def test_month_parsing(self):
        sys.path.insert(0, str(REPO_ROOT / "scripts"))
        from memory_routes import budgets

        self.assertEqual(budgets._month("2026-02"), "2026-02-01")
        self.assertEqual(budgets._month("2026-02-17"), "2026-02-01")
        self.assertIsNone(budgets._month(""))
        with self.assertRaises(ValueError):
            budgets._month("Feb 2026")

    def test_the_migration_is_in_the_order(self):
        from tests.pg_support import migration_order

        self.assertIn("055_improvement_budget.sql", migration_order())


# Off-limits list


class OffLimitsTests(unittest.TestCase):
    def test_every_listed_path_exists(self):
        for entry in OFF_LIMITS:
            if entry["kind"] != "path":
                continue
            path, _, section = entry["match"].partition("#")
            target = REPO_ROOT / path
            self.assertTrue(target.exists(), path)
            if section:
                self.assertIn(f"\n## {section}\n", target.read_text(), entry["match"])

    def test_every_listed_constant_exists(self):
        import importlib

        for entry in OFF_LIMITS:
            if entry["kind"] == "constant":
                module, name = entry["match"].split(":")
                self.assertTrue(hasattr(importlib.import_module(module), name), entry["match"])

    def test_matching(self):
        self.assertIsNotNone(is_off_limits(path="runtime/ts_harness/policy/permissions.py"))
        self.assertIsNotNone(is_off_limits(path="./runtime/ts_harness/workflows/code_review.py"))
        self.assertIsNotNone(is_off_limits(path="skills/review/governed-code-review/SKILL.md", section="Severity"))
        self.assertIsNotNone(is_off_limits(path="skills/review/governed-code-review/SKILL.md"))  # a whole-file change
        self.assertIsNone(is_off_limits(path="skills/review/governed-code-review/SKILL.md", section="Deep checks"))
        self.assertIsNone(is_off_limits(path="skills/dev/some-skill/SKILL.md"))
        self.assertIsNotNone(is_off_limits(setting="budgets.improvement_monthly_usd"))
        self.assertIsNotNone(is_off_limits(setting="TS_HARNESS_STUCK_ARBITER"))
        self.assertIsNotNone(is_off_limits(constant="ts_harness.workflows.stuck_arbiter:MAX_ACTIONS_PER_STEP"))
        self.assertIsNone(is_off_limits(setting="program_defaults.delivery.max_parallel_work_orders"))
        self.assertIsNotNone(is_off_limits(path="runtime/ts_harness/improvement/off_limits.py"))


if __name__ == "__main__":
    unittest.main()
