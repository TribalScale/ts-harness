#!/usr/bin/env python3
# flake8: noqa
"""Backfill immutable stage-contract snapshots onto in-flight workflow tasks.

Phase 1 of the Platform Durability Upgrades requires every non-terminal task to
carry a resolved ``contract`` (and ``definition_version``) before a bumped
skeleton is activated. This script materializes those snapshots for
already-scheduled tasks that predate the contract columns.

Usage::

    python scripts/backfill-workflow-task-contracts.py --report   # dry run
    python scripts/backfill-workflow-task-contracts.py --apply     # write

Exit codes:
    0  no contract-bearing tasks remain uncontracted
    1  contract-bearing tasks remain uncontracted (rollout must not proceed)
    2  usage / environment error
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
RUNTIME_DIR = REPO_ROOT / "runtime"
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

import workflow_definitions  # noqa: E402


TERMINAL_STATES = ("succeeded", "failed", "cancelled")


def load_skeletons(directory: Path) -> dict[str, workflow_definitions.WorkflowSkeleton]:
    skeletons: dict[str, workflow_definitions.WorkflowSkeleton] = {}
    for path in sorted(directory.glob("*.json")):
        if path.name == "schema.json":
            continue
        skeleton = workflow_definitions.load_skeleton(path)
        skeletons[skeleton.workflow_type] = skeleton
    return skeletons


def _resolve_contract(
    skeletons: dict[str, workflow_definitions.WorkflowSkeleton],
    workflow_type: str,
    step_name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (contract, definition_version) for a task, or (None, None)."""
    skeleton = skeletons.get(workflow_type)
    if skeleton is None:
        return None, None
    try:
        stage = skeleton.stage(step_name)
    except KeyError:
        return None, None
    contract = skeleton.resolve_stage_contract(stage)
    return contract, skeleton.definition_version


def fetch_uncontracted_tasks(cur) -> list[dict[str, Any]]:
    """Non-terminal tasks whose contract snapshot is missing/empty."""
    placeholders = ", ".join(["%s"] * len(TERMINAL_STATES))
    cur.execute(
        f"""
        SELECT id, workflow_type, step_name, status, contract
        FROM workflow_tasks
        WHERE status NOT IN ({placeholders})
          AND (contract IS NULL OR contract = '{{}}'::jsonb)
        ORDER BY created_at
        """,
        TERMINAL_STATES,
    )
    return list(cur.fetchall())


def backfill(
    conn,
    skeletons: dict[str, workflow_definitions.WorkflowSkeleton],
    *,
    apply: bool,
) -> dict[str, Any]:
    """Scan uncontracted non-terminal tasks and (optionally) persist contracts.

    Returns a report dict. A task is a *candidate* only when its skeleton stage
    resolves a contract; stages that legitimately have no contract (approvals,
    external waits) are ignored.
    """
    updated = 0
    remaining: list[str] = []
    scanned = 0
    with conn.cursor() as cur:
        rows = fetch_uncontracted_tasks(cur)
        for row in rows:
            scanned += 1
            task_id = str(row.get("id"))
            contract, definition_version = _resolve_contract(
                skeletons,
                str(row.get("workflow_type") or ""),
                str(row.get("step_name") or ""),
            )
            if not contract:
                # Stage has no declarative contract; nothing to backfill.
                continue
            if apply:
                cur.execute(
                    """
                    UPDATE workflow_tasks
                    SET contract = %s::jsonb,
                        definition_version = COALESCE(definition_version, %s)
                    WHERE id = %s
                      AND (contract IS NULL OR contract = '{}'::jsonb)
                    """,
                    (json.dumps(contract), definition_version, task_id),
                )
                updated += 1
            else:
                remaining.append(task_id)
    if apply:
        conn.commit()
        # Re-scan to confirm nothing contract-bearing remains uncontracted.
        with conn.cursor() as cur:
            rows = fetch_uncontracted_tasks(cur)
            for row in rows:
                contract, _ = _resolve_contract(
                    skeletons,
                    str(row.get("workflow_type") or ""),
                    str(row.get("step_name") or ""),
                )
                if contract:
                    remaining.append(str(row.get("id")))
    return {
        "scanned": scanned,
        "updated": updated,
        "remaining": remaining,
        "clean": len(remaining) == 0,
    }


def _connect():
    from importlib import import_module

    gateway_path = SCRIPT_DIR / "memory-gateway.py"
    import importlib.util

    spec = importlib.util.spec_from_file_location("_backfill_gateway", gateway_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.connect_db()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="persist contract snapshots")
    mode.add_argument(
        "--report",
        "--dry-run",
        action="store_true",
        dest="report",
        help="report only; exit non-zero if any tasks remain uncontracted",
    )
    parser.add_argument(
        "--workflow-dir",
        default=str(REPO_ROOT / "config" / "workflows"),
        help="workflow skeleton directory",
    )
    args = parser.parse_args(argv)

    skeletons = load_skeletons(Path(args.workflow_dir))
    if not skeletons:
        print(json.dumps({"error": "no skeletons found"}))
        return 2

    with _connect() as conn:
        report = backfill(conn, skeletons, apply=bool(args.apply))

    print(json.dumps(report, default=str))
    return 0 if report["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
