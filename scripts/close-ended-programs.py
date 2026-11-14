#!/usr/bin/env python3
"""Close software delivery programs left running after their PRD or Jira step.

Before ``software_delivery`` 1.4.0, the ``prd`` and ``jira`` nodes did not end the
program: after the PRD was approved (or the Jira tickets filed) and nobody asked for the
next step, the program stayed ``running`` with nothing active. Programs created from
1.4.0 end there on their own (``may_end``), but older programs keep their pinned
snapshot. This script marks those older ones succeeded with ``ended_at_phase``, the same
state 1.4.0 leaves, so they can still be continued later.

A program is selected when all of these hold:

- status ``running``, program type ``software_delivery`` (or unset, for legacy rows),
  and not a child program;
- current phase ``prd`` or ``jira``, and a workflow for that phase succeeded;
- no workflow of the program is still running or blocked, and no child program is open;
- nothing has happened in its workflows for ``--idle-days`` days (default 7).

Usage::

    python scripts/close-ended-programs.py                 # dry run: list what would close
    python scripts/close-ended-programs.py --apply         # close them
    python scripts/close-ended-programs.py --idle-days 0   # ignore idle time

Exit codes: 0 done (or nothing to do), 2 usage or environment error.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ENDABLE_PHASES = ("prd", "jira")

SELECT_CANDIDATES = """
SELECT p.id, p.tenant_id, p.current_phase, p.summary, p.metadata,
       (SELECT max(w.updated_at) FROM workflows w WHERE w.program_id = p.id) AS last_activity
FROM delivery_programs p
WHERE p.status = 'running'
  AND COALESCE(p.program_type, 'software_delivery') = 'software_delivery'
  AND p.parent_program_id IS NULL
  AND p.current_phase = ANY(%s)
  AND EXISTS (
      SELECT 1 FROM workflows w
      WHERE w.program_id = p.id AND w.program_phase = p.current_phase AND w.status = 'succeeded'
  )
  AND NOT EXISTS (
      SELECT 1 FROM workflows w
      WHERE w.program_id = p.id AND w.status IN ('running', 'blocked')
  )
  AND NOT EXISTS (
      SELECT 1 FROM delivery_programs c
      WHERE c.parent_program_id = p.id AND c.status IN ('running', 'blocked')
  )
  AND (SELECT max(w.updated_at) FROM workflows w WHERE w.program_id = p.id)
      < now() - make_interval(days => %s)
ORDER BY last_activity
"""


def _load_gateway():
    spec = importlib.util.spec_from_file_location("_close_ended_gateway", SCRIPT_DIR / "memory-gateway.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(value) if isinstance(value, dict) else {}


def close_ended_programs(conn, gateway, *, idle_days: int, apply: bool, actor: str) -> dict[str, Any]:
    """Select candidates and, with ``apply``, mark each succeeded where it stopped."""
    closed: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(SELECT_CANDIDATES, (list(ENDABLE_PHASES), int(idle_days)))
        rows = list(cur.fetchall())
        for row in rows:
            program_id = str(row.get("id"))
            phase = str(row.get("current_phase") or "")
            entry = {
                "program_id": program_id,
                "tenant_id": row.get("tenant_id"),
                "phase": phase,
                "summary": str(row.get("summary") or "")[:80],
                "last_activity": row.get("last_activity"),
            }
            if apply:
                metadata = _metadata(row.get("metadata"))
                artifacts = dict(metadata.get("artifacts") or {})
                artifacts.update(
                    {
                        "program_status": "succeeded",
                        "ended_at_phase": phase,
                        "completed_at_phase": phase,
                        "closed_by_backfill": True,
                    }
                )
                metadata["artifacts"] = artifacts
                cur.execute(
                    """
                    UPDATE delivery_programs
                    SET status = 'succeeded', metadata = %s::jsonb, updated_at = now()
                    WHERE id = %s AND status = 'running'
                    """,
                    (json.dumps(metadata, default=str), program_id),
                )
                gateway.audit_workflow_event(
                    cur,
                    "delivery_program_closed_by_backfill",
                    f"Delivery program closed at {phase}",
                    {"program_id": program_id, "phase": phase, "idle_days": int(idle_days)},
                    actor,
                )
            closed.append(entry)
    if apply:
        conn.commit()
    return {"applied": apply, "idle_days": int(idle_days), "count": len(closed), "programs": closed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="close the selected programs (default: dry run)")
    parser.add_argument("--idle-days", type=int, default=7, help="days without workflow activity (default 7)")
    parser.add_argument("--actor", default="close-ended-programs", help="actor recorded in the audit log")
    args = parser.parse_args(argv)
    if args.idle_days < 0:
        print(json.dumps({"error": "--idle-days must be 0 or more"}))
        return 2
    gateway = _load_gateway()
    with ExitStack() as stack:
        try:
            conn = stack.enter_context(gateway.connect_db())
        except Exception as exc:  # noqa: BLE001 - report any connection problem the same way
            print(json.dumps({"error": f"could not connect to the memory database: {exc}"}))
            return 2
        report = close_ended_programs(conn, gateway, idle_days=args.idle_days, apply=args.apply, actor=args.actor)
    pool = getattr(gateway, "_pool", None)
    if pool is not None:
        pool.close()
    print(json.dumps(report, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
