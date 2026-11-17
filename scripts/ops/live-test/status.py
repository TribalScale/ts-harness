#!/usr/bin/env python3
"""Show a workflow's steps, LLM cost and state, or a program's work-order progress.

    python3 /app/scripts/ops/live-test/status.py workflow <workflow-id>
    python3 /app/scripts/ops/live-test/status.py program <program-id>
    python3 /app/scripts/ops/live-test/status.py children <workflow-id>   # wave children

The last line of ``workflow`` is ``STATE MERGE_WAIT | DECIDE | <status>``, for wait loops.
"""

from __future__ import annotations

import json
import os
import sys


def _cursor():
    import psycopg

    return psycopg.connect(os.environ["TS_HARNESS_SESSION_DB_URL"]).cursor()


def workflow(wf: str) -> int:
    cur = _cursor()
    cur.execute("select status, created_at, updated_at from workflows where id=%s", (wf,))
    row = cur.fetchone()
    if not row:
        print("no workflow", wf, file=sys.stderr)
        return 1
    status, created, updated = row
    cur.execute(
        "select step_name, status, left(coalesce(error,''),140), input ->> '_awaiting_human_decision' is not null "
        "from workflow_tasks where workflow_id=%s order by created_at",
        (wf,),
    )
    rows = cur.fetchall()
    for step, state, error, _ in rows:
        if state != "pending":
            print("  ", step, state, error)
    merge_wait = any(r[0] == "await_human_merge" and r[1] == "blocked" and r[2].startswith("awaiting_external:github.merge") for r in rows)
    decide = any(r[1] == "blocked" and (r[3] or not r[2].startswith(("awaiting_external:", "awaiting_tool_approval:"))) for r in rows)
    cur.execute(
        "select coalesce(agent_id,'-'), model, count(*), round(sum(cost_usd),3) from llm_usage where workflow_id=%s group by 1,2 order by 4 desc",
        (wf,),
    )
    for usage in cur.fetchall():
        print("  usage", *usage)
    print("status", status, "| minutes", round((updated - created).total_seconds() / 60, 1))
    print("STATE", "MERGE_WAIT" if merge_wait else "DECIDE" if decide else status)
    return 0


def children(wf: str) -> int:
    cur = _cursor()
    cur.execute("select output from workflow_tasks where workflow_id=%s and step_name='deliver_wave'", (wf,))
    for (output,) in cur.fetchall():
        for child in (output or {}).get("children") or []:
            print(child.get("work_order_id"), child.get("workflow_id"))
    return 0


def program(pid: str) -> int:
    from ts_harness.memory.client import get_memory_client

    record = get_memory_client().get_program(pid)["program"]
    metadata = record.get("metadata") or {}
    metadata = json.loads(metadata) if isinstance(metadata, str) else metadata
    artifacts = metadata.get("artifacts") or {}
    keys = ("repo_onboarding", "onboarding_gates", "work_order_ids", "next_work_order_ids", "completed_work_order_ids")
    print(json.dumps({"phase": record.get("current_phase"), **{k: artifacts.get(k) for k in keys}}, indent=1))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] not in {"workflow", "program", "children"}:
        print(__doc__, file=sys.stderr)
        return 2
    return {"workflow": workflow, "program": program, "children": children}[argv[0]](argv[1])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
