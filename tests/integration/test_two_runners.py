"""Two workflow runners on one database: no double claims, no concurrent writes to a workflow.

Each runner talks to Postgres through the memory API route functions, and a
third thread plays a webhook that writes to the same workflows under the lock.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from tests.pg_support import PgTestCase, requires_pg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from memory_routes import task_claims  # noqa: E402

from ts_harness.workflows.workflow_locks import workflow_lock  # noqa: E402

pytestmark = pytest.mark.integration

WORKFLOWS = 8
STEPS = 3


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


class Writers:
    """Counts writers inside each workflow; more than one at a time is a violation."""

    def __init__(self):
        self.guard = threading.Lock()
        self.active: Counter[str] = Counter()
        self.violations: list[str] = []

    def enter(self, workflow_id: str) -> None:
        with self.guard:
            self.active[workflow_id] += 1
            if self.active[workflow_id] > 1:
                self.violations.append(workflow_id)

    def leave(self, workflow_id: str) -> None:
        with self.guard:
            self.active[workflow_id] -= 1


class DirectMemory:
    """The runner's memory client, backed by the route functions instead of HTTP."""

    def __init__(self, db, gateway, writers: Writers, claimed: list[str], claim_guard: threading.Lock):
        self.db = db
        self.gateway = gateway
        self.writers = writers
        self.claimed = claimed
        self.claim_guard = claim_guard

    def _call(self, fn, *args):
        with self.db.connect() as conn:
            result = fn(conn, *args)
            conn.commit()
            return _plain(result)

    def _workflow_of(self, task_id: str) -> str:
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT workflow_id FROM workflow_tasks WHERE id = %s", (task_id,))
            return str(cur.fetchone()["workflow_id"])

    def _transition(self, task_id: str, body: dict[str, Any]) -> None:
        workflow_id = self._workflow_of(task_id)
        self.writers.enter(workflow_id)
        try:
            time.sleep(0.01)
            self._call(self.gateway.transition_workflow_task, {"id": task_id, **body})
        finally:
            self.writers.leave(workflow_id)

    def acquire_leader(self, runner_id, ttl_seconds):
        return self._call(self.gateway.acquire_runner_lease, {"runner_id": runner_id, "ttl_seconds": ttl_seconds}).get("leader", False)

    def list_stale_tasks(self):
        return []

    def claim_ready_tasks(self, *, runner, lane, limit, lease_seconds):
        tasks = self._call(task_claims.claim_batch, {"runner": runner, "lane": lane, "limit": limit, "lease_seconds": lease_seconds})["tasks"]
        with self.claim_guard:
            self.claimed.extend(str(task["id"]) for task in tasks)
        return tasks

    def renew_task_lease(self, task_id, *, runner, lease_seconds):
        return self._call(task_claims.renew_lease, task_id, {"runner": runner, "lease_seconds": lease_seconds}).get("renewed", False)

    def acquire_workflow_lock(self, workflow_id, *, owner, ttl_seconds):
        return self._call(task_claims.acquire_workflow_lock, workflow_id, {"owner": owner, "ttl_seconds": ttl_seconds})["acquired"]

    def release_workflow_lock(self, workflow_id, *, owner):
        return self._call(task_claims.release_workflow_lock, workflow_id, {"owner": owner})["released"]

    def record_runtime_task(self, task_id, runtime_task_id):
        return None

    def create_workflow(self, workflow_id, workflow_type, tenant_id, definition_version=None):
        return {"id": workflow_id}

    def get_workflow_record(self, workflow_id):
        return {"workflow": {"id": workflow_id}, "tasks": []}

    def complete_task(self, task_id, output):
        self._transition(task_id, {"status": "succeeded", "output": output})

    def fail_task(self, task_id, error):
        self._transition(task_id, {"status": "failed", "error": error})

    def block_task(self, task_id, error, output=None):
        self._transition(task_id, {"status": "blocked", "error": error})

    def retry_task(self, task_id, reason):
        raise AssertionError(f"unexpected retry of {task_id}: {reason}")


class Adapter:
    def dispatch(self, payload):
        time.sleep(0.02)
        return {"status": "succeeded", "output": {"step": payload["step_name"]}}


@requires_pg
class TwoRunnersTests(PgTestCase):
    def setUp(self):
        self.workflow_ids: list[str] = []
        with self.db.connect() as conn, conn.cursor() as cur:
            for _ in range(WORKFLOWS):
                cur.execute("INSERT INTO workflows (workflow_type) VALUES ('t') RETURNING id")
                workflow_id = str(cur.fetchone()["id"])
                self.workflow_ids.append(workflow_id)
                previous = None
                for step in range(STEPS):
                    cur.execute(
                        """
                        INSERT INTO workflow_tasks (workflow_id, step_name, agent_id, lane, depends_on, max_retries)
                        VALUES (%s, %s, 'dev-agent', 'agent', %s, 0) RETURNING id
                        """,
                        (workflow_id, f"step_{step}", [previous] if previous else []),
                    )
                    previous = str(cur.fetchone()["id"])
            conn.commit()

    def _statuses(self) -> Counter[str]:
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT status FROM workflow_tasks")
            return Counter(row["status"] for row in cur.fetchall())

    def test_runners_share_the_queue_without_double_claims_or_concurrent_writes(self):
        runner_module = _load("workflow_runner_two", REPO_ROOT / "scripts" / "workflow-runner.py")
        gateway = _load("memory_gateway_two", REPO_ROOT / "scripts" / "memory-gateway.py")
        writers = Writers()
        claimed: list[str] = []
        claim_guard = threading.Lock()
        stop = threading.Event()

        def run(runner_id: str) -> None:
            memory = DirectMemory(self.db, gateway, writers, claimed, claim_guard)
            runner = runner_module.WorkflowRunner(memory, Adapter(), runner_id=runner_id, lane_caps={"agent": 3})
            while not stop.is_set():
                runner.tick()
                time.sleep(0.01)

        def webhook() -> None:
            memory = DirectMemory(self.db, gateway, writers, claimed, claim_guard)
            while not stop.is_set():
                for workflow_id in self.workflow_ids:
                    with workflow_lock(memory, workflow_id, wait_seconds=10):
                        writers.enter(workflow_id)
                        time.sleep(0.005)
                        writers.leave(workflow_id)

        with patch.dict(os.environ, {"TS_HARNESS_HOOK_ENGINE": "false", "WORKFLOW_LOCKS_ENABLED": "true"}):
            threads = [threading.Thread(target=run, args=(name,)) for name in ("runner-a", "runner-b")]
            threads.append(threading.Thread(target=webhook))
            for thread in threads:
                thread.start()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and self._statuses().get("succeeded", 0) < WORKFLOWS * STEPS:
                time.sleep(0.1)
            stop.set()
            for thread in threads:
                thread.join(30)

        self.assertEqual(self._statuses(), Counter({"succeeded": WORKFLOWS * STEPS}))
        self.assertEqual(len(claimed), WORKFLOWS * STEPS)
        self.assertEqual(len(set(claimed)), len(claimed), "a task was claimed twice")
        self.assertEqual(writers.violations, [])
        with self.db.connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT locked_by FROM workflow_tasks")
            self.assertEqual({row["locked_by"] for row in cur.fetchall()}, {"runner-a", "runner-b"})
            cur.execute("SELECT count(*) AS n FROM workflow_locks")
            self.assertEqual(cur.fetchone()["n"], 0)
