"""Per-repository release tracks: ordering, upstream waits, failure isolation and single-repo parity."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from ts_harness.workflows import phase_activity, release, release_tracks
from ts_harness.workflows.hooks import wait_external
from ts_harness.workflows.program_adapters import build_default_adapter_registry

REPO_ROOT = Path(__file__).resolve().parents[2]
SHA_API = "a" * 40
SHA_API_OLD = "d" * 40
SHA_WEB = "b" * 40
SHA_JOBS = "c" * 40

MARKDOWN = """
## WO-1 Add the orders endpoint
Repository: acme/api
| Depends on | None |

## WO-2 Show orders
Repository: acme/web
| Depends on | WO-1 |

## WO-3 Nightly export
Repository: acme/jobs
| Depends on | None |
"""


def _merged(wo: str, repo: str, sha: str, merged_at: str, number: int) -> dict[str, Any]:
    return {
        "work_order_id": wo,
        "pr_url": f"https://github.com/{repo}/pull/{number}",
        "merge_sha": sha,
        "merged_at": merged_at,
    }


MERGED = [
    _merged("WO-1", "acme/api", SHA_API_OLD, "2026-10-01T10:00:00Z", 1),
    _merged("WO-1", "acme/api", SHA_API, "2026-10-02T10:00:00Z", 2),
    _merged("WO-2", "acme/web", SHA_WEB, "2026-10-02T11:00:00Z", 3),
    _merged("WO-3", "acme/jobs", SHA_JOBS, "2026-10-02T12:00:00Z", 4),
]


def _task(workflow_id: str, step: str, *, status: str = "pending", output: dict | None = None, input: dict | None = None):
    return {
        "id": f"{workflow_id}:{step}",
        "workflow_id": workflow_id,
        "tenant_id": "acme",
        "step_name": step,
        "status": status,
        "output": output or {},
        "input": input or {},
    }


class FakeMemory:
    """Several workflows; forked children get a ``plan_track`` intake task."""

    def __init__(self) -> None:
        self.workflows: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, list[dict[str, Any]]] = {}
        self.patched: dict[str, dict[str, Any]] = {}
        self.blocked: list[tuple[str, str]] = []
        self.resumed: list[dict[str, Any]] = []
        self.recorded: list[dict[str, Any]] = []

    def add(self, workflow_id: str, tasks: list[dict[str, Any]], **fields: Any) -> None:
        self.workflows[workflow_id] = {"id": workflow_id, "tenant_id": "acme", **fields}
        self.tasks[workflow_id] = tasks

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        return {"workflow": self.workflows[workflow_id], "tasks": self.tasks[workflow_id]}

    def patch_task_input(self, task_id: str, input_data: dict[str, Any], *, actor: str = "") -> dict[str, Any]:
        self.patched[task_id] = input_data
        for tasks in self.tasks.values():
            for task in tasks:
                if task["id"] == task_id:
                    task["input"] = dict(input_data)
        return {"status": "ok"}

    def block_task_api(self, task_id: str, reason: str, *, output=None, actor: str = "") -> dict[str, Any]:
        self.blocked.append((task_id, reason))
        return {"status": "blocked"}

    def resume_external(self, workflow_id: str, **kwargs: Any) -> dict[str, Any]:
        self.resumed.append({"workflow_id": workflow_id, **kwargs})
        return {"status": "resumed"}

    def record_release(self, program_id: str, **fields: Any) -> dict[str, Any]:
        self.recorded.append({"program_id": program_id, **fields})
        return {"id": "rel-1", "status": fields.get("status")}


def _fork(memory: FakeMemory):
    counter = iter(range(1, 100))

    def fork_child(_memory, *, parent_workflow_id, tenant_id, child_type, intake_input, actor, definition_version=None):
        child_id = f"track-{next(counter)}"
        memory.add(
            child_id,
            [_task(child_id, "plan_track", input=intake_input)],
            parent_workflow_id=parent_workflow_id,
            workflow_type=child_type,
        )
        return child_id

    return fork_child


def _plan_parent(memory: FakeMemory, intake: dict[str, Any]) -> dict[str, Any]:
    memory.add("parent", [_task("parent", "plan_release_tracks", status="running", input=intake)], program_id="prog-1")
    with mock.patch.object(release_tracks, "fork_child", _fork(memory)):
        output = release_tracks.plan_release_tracks(
            memory.tasks["parent"][0], {"workflow_type": "release_track"}, memory=memory, program_defaults={}
        )
    memory.tasks["parent"][0].update(status="succeeded", output=output)
    for child in output["children"]:
        plan_task = memory.tasks[child["workflow_id"]][0]
        plan_task.update(
            status="succeeded",
            output={"repo": plan_task["input"]["track_repo"], "upstream": plan_task["input"]["upstream"], "environments": ["staging", "production"]},
        )
    return output


def _children(output: dict[str, Any]) -> dict[str, str]:
    return {child["label"]: child["workflow_id"] for child in output["children"]}


class PlanTracksTests(unittest.TestCase):
    def test_one_track_per_repository_upstreams_first(self):
        tracks = release_tracks.plan_tracks({"merged_work_orders": MERGED, "work_orders_markdown": MARKDOWN})

        self.assertEqual([t["repo"] for t in tracks], ["acme/api", "acme/jobs", "acme/web"])
        by_repo = {t["repo"]: t for t in tracks}
        self.assertEqual(by_repo["acme/web"]["upstream"], ["acme/api"])
        self.assertEqual(by_repo["acme/jobs"]["upstream"], [])
        self.assertEqual(by_repo["acme/api"]["sha"], SHA_API)
        self.assertEqual(by_repo["acme/api"]["work_order_ids"], ["WO-1"])

    def test_registered_repositories_supply_url_and_branch(self):
        repositories = [{"full_name": "Acme/Web", "repo_url": "https://ghe.acme.dev/acme/web", "default_branch": "develop"}]
        tracks = release_tracks.plan_tracks(
            {"merged_work_orders": MERGED, "work_orders_markdown": MARKDOWN, "project_repositories": repositories, "base_branch": "main"}
        )
        web = next(t for t in tracks if t["repo"] == "acme/web")
        api = next(t for t in tracks if t["repo"] == "acme/api")
        self.assertEqual((web["repo_url"], web["base_branch"]), ("https://ghe.acme.dev/acme/web", "develop"))
        self.assertEqual((api["repo_url"], api["base_branch"]), ("https://github.com/acme/api", "main"))

    def test_dependency_cycle_between_repositories_is_rejected(self):
        markdown = MARKDOWN.replace("| Depends on | None |\n\n## WO-2", "| Depends on | WO-2 |\n\n## WO-2", 1)
        with self.assertRaisesRegex(release.ReleaseBlocked, "depend on each other"):
            release_tracks.plan_tracks({"merged_work_orders": MERGED, "work_orders_markdown": markdown})

    def test_single_repository_without_merged_work_orders_keeps_one_track(self):
        tracks = release_tracks.plan_tracks(
            {"repo_url": "https://github.com/Acme/Shop", "merge_sha": SHA_API, "pr_urls": ["https://github.com/Acme/Shop/pull/7"]}
        )
        self.assertEqual(len(tracks), 1)
        self.assertEqual((tracks[0]["repo"], tracks[0]["sha"], tracks[0]["upstream"]), ("acme/shop", SHA_API, []))

    def test_missing_sha_blocks(self):
        with self.assertRaisesRegex(release.ReleaseBlocked, "merge_sha"):
            release_tracks.plan_tracks({"repo_url": "https://github.com/acme/shop"})


class ForkTracksTests(unittest.TestCase):
    def test_forks_one_child_per_track_with_its_intake(self):
        memory = FakeMemory()
        output = _plan_parent(memory, {"merged_work_orders": MERGED, "work_orders_markdown": MARKDOWN})

        self.assertEqual(output["order"], ["acme/api", "acme/jobs", "acme/web"])
        web = memory.tasks[_children(output)["acme/web"]][0]["input"]
        self.assertEqual(web["upstream"], ["acme/api"])
        self.assertEqual(web["merge_sha"], SHA_WEB)
        self.assertEqual(web["repo_url"], "https://github.com/acme/web")

    def test_retry_does_not_fork_twice(self):
        memory = FakeMemory()
        memory.add("parent", [_task("parent", "plan_release_tracks", status="running", input={"merged_work_orders": MERGED})])
        fork = _fork(memory)
        with mock.patch.object(release_tracks, "fork_child", fork):
            first = release_tracks.plan_release_tracks(memory.tasks["parent"][0], {}, memory=memory, program_defaults={})
            second = release_tracks.plan_release_tracks(memory.tasks["parent"][0], {}, memory=memory, program_defaults={})
        self.assertEqual(first["children"], second["children"])
        self.assertEqual(len(memory.workflows), 4)

    def test_disallowed_repository_blocks_before_forking(self):
        memory = FakeMemory()
        memory.add("parent", [_task("parent", "plan_release_tracks", status="running", input={"merged_work_orders": MERGED})])
        with (
            mock.patch.object(release_tracks, "check_repo_allowed", lambda repo, _b: "not allowed" if repo == "acme/jobs" else None),
            mock.patch.object(release_tracks, "fork_child") as fork,
        ):
            with self.assertRaisesRegex(release.ReleaseBlocked, "not allowed"):
                release_tracks.plan_release_tracks(memory.tasks["parent"][0], {}, memory=memory, program_defaults={})
        fork.assert_not_called()


class UpstreamWaitTests(unittest.TestCase):
    def setUp(self):
        self.memory = FakeMemory()
        self.output = _plan_parent(self.memory, {"merged_work_orders": MERGED, "work_orders_markdown": MARKDOWN})
        self.ids = _children(self.output)

    def _wait_task(self, repo: str, slot: str) -> dict[str, Any]:
        workflow_id = self.ids[repo]
        task = _task(workflow_id, f"await_upstream_{slot}", status="blocked")
        task["error"] = f"awaiting_external:{release_tracks.UPSTREAM_EVENT}"
        task["expected_event_type"] = release_tracks.UPSTREAM_EVENT
        self.memory.tasks[workflow_id].append(task)
        return task

    def _record(self, repo: str, slot: str, *, status: str = "succeeded") -> None:
        workflow_id = self.ids[repo]
        self.memory.tasks[workflow_id].append(_task(workflow_id, f"record_release_{slot}", status=status))

    def test_track_without_upstream_is_ready_at_once(self):
        task = self._wait_task("acme/jobs", "staging")
        prepared = release_tracks.prepare_upstream_wait(task, {"upstream": {"environment": "staging"}}, memory=self.memory)
        self.assertEqual(prepared["ready"]["environment"], "staging")

    def test_dependent_track_waits_until_upstream_records_the_environment(self):
        task = self._wait_task("acme/web", "staging")
        prepared = release_tracks.prepare_upstream_wait(task, {"upstream": {"environment": "staging"}}, memory=self.memory)
        self.assertNotIn("ready", prepared)
        self.assertEqual(prepared["options"]["pending"], ["acme/api"])
        self.assertEqual(prepared["match"], {"repo": "acme/web", "environment": "staging"})

        self._record("acme/api", "staging")
        status = release_tracks.upstream_status(
            self.memory, self.memory.workflows[self.ids["acme/web"]], self.memory.tasks[self.ids["acme/web"]], "staging"
        )
        self.assertEqual(status["state"], "ready")
        production = release_tracks.upstream_status(
            self.memory, self.memory.workflows[self.ids["acme/web"]], self.memory.tasks[self.ids["acme/web"]], "production"
        )
        self.assertEqual(production["state"], "pending")

    def test_failed_upstream_blocks_only_its_dependents(self):
        self._record("acme/api", "staging", status="failed")
        web = release_tracks.upstream_status(
            self.memory, self.memory.workflows[self.ids["acme/web"]], self.memory.tasks[self.ids["acme/web"]], "staging"
        )
        jobs = release_tracks.upstream_status(
            self.memory, self.memory.workflows[self.ids["acme/jobs"]], self.memory.tasks[self.ids["acme/jobs"]], "staging"
        )
        self.assertEqual((web["state"], web["failed"]), ("failed", ["acme/api"]))
        self.assertEqual(jobs["state"], "ready")
        task = self._wait_task("acme/web", "staging")
        with self.assertRaisesRegex(release.ReleaseBlocked, "upstream release failed: acme/api staging"):
            release_tracks.prepare_upstream_wait(task, {"upstream": {"environment": "staging"}}, memory=self.memory)

    def _resume(self, repo: str, slot: str, payload: dict[str, Any]) -> dict[str, Any]:
        workflow_id = self.ids[repo]
        stage = SimpleNamespace(wait_external={"upstream": {"environment": slot}}, approval_required=False)
        ctx = SimpleNamespace(
            memory_client=self.memory,
            workflow_id=workflow_id,
            actor="workflow-runner",
            extras={"event_type": release_tracks.UPSTREAM_EVENT, "payload": payload},
            skeleton=SimpleNamespace(stage=lambda _name: stage),
        )
        return wait_external.resume(ctx)

    def test_resume_keeps_waiting_then_resumes_when_upstream_released(self):
        self._wait_task("acme/web", "staging")
        pending = self._resume("acme/web", "staging", {"environment": "staging", "upstream_repo": "acme/api"})
        self.assertEqual(pending["status"], "awaiting_more_upstream")
        self.assertEqual(self.memory.resumed, [])

        self._record("acme/api", "staging")
        self._resume("acme/web", "staging", {"environment": "staging", "upstream_repo": "acme/api"})
        self.assertEqual(self.memory.resumed[0]["workflow_id"], self.ids["acme/web"])
        self.assertEqual(self.memory.resumed[0]["task_output"]["upstream"], ["acme/api"])

    def test_resume_ignores_another_environment(self):
        self._wait_task("acme/web", "production")
        with self.assertRaisesRegex(ValueError, "no matching external event"):
            self._resume("acme/web", "production", {"environment": "staging"})

    def test_resume_blocks_when_upstream_failed(self):
        self._wait_task("acme/web", "staging")
        self._record("acme/api", "staging", status="failed")
        result = self._resume("acme/web", "staging", {"environment": "staging", "state": "failed"})
        self.assertEqual(result["status"], "blocked")
        self.assertIn("upstream release failed: acme/api staging", self.memory.blocked[0][1])

    def test_record_release_wakes_dependents_and_uses_the_parent_program(self):
        api_id = self.ids["acme/api"]
        self.memory.tasks[api_id] += [
            _task(api_id, "trigger_deploy_staging", status="succeeded", output={"release_id": "rel-1", "environment": "staging", "repo": "acme/api", "sha": SHA_API}),
            _task(api_id, "await_deployment_staging", status="succeeded", output={"state": "success", "environment_url": "https://staging"}),
        ]
        record = _task(api_id, "record_release_staging", status="running")
        self.memory.tasks[api_id].append(record)
        engine = mock.MagicMock()
        with mock.patch("ts_harness.workflows.engine.get_workflow_engine", return_value=engine):
            release.record_release(
                record,
                {"trigger_step": "trigger_deploy_staging", "wait_step": "await_deployment_staging", "slot": "staging"},
                memory=self.memory,
            )

        self.assertEqual(self.memory.recorded[0]["program_id"], "prog-1")
        engine.resume_external.assert_called_once()
        args, kwargs = engine.resume_external.call_args
        self.assertEqual(args, (self.ids["acme/web"],))
        self.assertEqual(kwargs["event_type"], release_tracks.UPSTREAM_EVENT)
        self.assertEqual(kwargs["payload"], {"repo": "acme/web", "environment": "staging", "upstream_repo": "acme/api", "state": "succeeded"})


class PresentationAndAdapterTests(unittest.TestCase):
    def test_phase_activity_labels_tracks_by_repository_and_names_the_upstream(self):
        wait = _task("track-2", "await_upstream_production", status="blocked", input={"_wait_options": {"environment": "production", "pending": ["acme/payments-api"]}})
        wait["error"] = f"awaiting_external:{release_tracks.UPSTREAM_EVENT}"
        summary = phase_activity.summarize_workflow(
            {
                "id": "track-2",
                "status": "running",
                "workflow_type": "release_track",
                "tasks": [_task("track-2", "plan_track", status="succeeded", input={"track_repo": "acme/web"}), wait],
            }
        )
        self.assertEqual(summary["label"], "acme/web")
        self.assertEqual((summary["state"], summary["kind"]), ("Waits for payments-api production", "waiting"))

    def test_implement_outputs_accumulate_merged_work_orders(self):
        registry = build_default_adapter_registry()
        wave = {"prs": [{"work_order_id": "WO-2", "pr_url": MERGED[2]["pr_url"], "merge_sha": SHA_WEB, "merged_at": "t2"}]}
        patch = registry.extract_outputs(
            "extract_implement_outputs",
            workflow_type="implement_work_orders",
            tasks=[{"step_name": "join_wave_1", "status": "succeeded", "output": wave}],
            artifacts={"merged_work_orders": [MERGED[1]]},
        )
        self.assertEqual([item["work_order_id"] for item in patch["merged_work_orders"]], ["WO-1", "WO-2"])

    def test_deploy_inputs_carry_work_orders_and_outputs_read_the_join(self):
        registry = build_default_adapter_registry()
        intake = registry.build_inputs(
            "extract_deploy_outputs",
            summary="ship",
            artifacts={"merged_work_orders": MERGED, "work_orders_markdown": MARKDOWN, "merge_sha": SHA_API},
        )
        self.assertEqual(intake["merged_work_orders"], MERGED)
        self.assertEqual(intake["work_orders_markdown"], MARKDOWN)
        join = {"children_outputs": {"acme/api": {"environments": ["staging", "production"]}, "acme/web": {"environments": ["staging", "production"]}}}
        patch = registry.extract_outputs(
            "extract_deploy_outputs",
            workflow_type="deploy_release",
            tasks=[{"step_name": "join_tracks", "status": "succeeded", "output": join}],
        )
        self.assertEqual(patch["released_environments"], ["staging", "production"])


class DefinitionTests(unittest.TestCase):
    def test_deploy_release_forks_tracks_and_joins_them(self):
        parent = json.loads((REPO_ROOT / "config/workflows/deploy_release.json").read_text())
        self.assertEqual(parent["definition_version"], "3.0.0")
        self.assertEqual([s["name"] for s in parent["stages"]], ["plan_release_tracks", "join_tracks"])
        self.assertEqual(parent["stages"][0]["barrier"]["workflow_type"], "release_track")

    def test_release_track_waits_for_upstreams_before_each_gate(self):
        track = json.loads((REPO_ROOT / "config/workflows/release_track.json").read_text())
        stages = {s["name"]: s for s in track["stages"]}
        self.assertEqual(stages["approve_promote_staging"]["depends_on"], ["await_upstream_staging"])
        self.assertEqual(stages["await_upstream_production"]["depends_on"], ["record_release_staging"])
        self.assertEqual(stages["trigger_deploy_production"]["barrier"]["plan_step"], "plan_track")
        self.assertEqual(stages["record_release_production"]["barrier"]["slot"], "production")
