#!/usr/bin/env python3
"""Measure the pre-push review trial: does reviewing with the gate's rubric before the push get
tickets through the code review gate on the first try?

Run inside the agent-runtime container (it has TS_HARNESS_SESSION_DB_URL):

    docker exec docker-agent-runtime-1 python3 /app/scripts/ops/pre-push-review-trial.py [--days 14]

For each delivery whose first coding run had a pre-push review, it reads that run's review
variant (``gate_inputs`` since the reviewer gets the gate's inputs, ``gate_rubric`` before that,
``legacy`` before the trial) and the code review gate's first
verdict. The decision rule agreed for the trial: after at least ``--sample`` gate_rubric tickets,
fewer than ``--min-passes`` first-try passes means the pre-push review is turned off
(``TS_HARNESS_PRE_PUSH_REVIEW_ROUNDS=0``).
"""

from __future__ import annotations

import argparse
import os
import statistics
from typing import Any

import psycopg


# Newest first: gate_inputs (the gate's rubric and inputs), gate_rubric (rubric only), legacy.
VARIANTS = ("gate_inputs", "gate_rubric", "legacy")


def _first_runs(conn: Any, days: int) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        select workflow_id, work_order_id, created_at, result->'pre_push_review', result->'step_timings'->'review'
        from coding_jobs
        where created_at > now() - make_interval(days => %s) and result ? 'pre_push_review'
        order by created_at
        """,
        (days,),
    ).fetchall()
    first: dict[str, dict[str, Any]] = {}
    for workflow_id, work_order, _created, review, steps in rows:
        if not review or str(workflow_id) in first:
            continue
        seconds = sum(float(step.get("seconds") or 0) for step in steps or [] if isinstance(step, dict))
        first[str(workflow_id)] = {
            "work_order": work_order,
            "variant": str(review.get("variant") or "legacy"),
            "fixed": int(review.get("fixed") or 0),
            "minutes": seconds / 60,
        }
    return first


def _first_verdict(conn: Any, workflow_id: str) -> str:
    row = conn.execute(
        """
        select output->>'verdict' from workflow_tasks
        where workflow_id = %s and step_name = 'finalize_code_review' and output ? 'verdict'
        order by created_at limit 1
        """,
        (workflow_id,),
    ).fetchone()
    return str(row[0]) if row and row[0] else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--sample", type=int, default=10)
    parser.add_argument("--min-passes", type=int, default=3)
    args = parser.parse_args()

    with psycopg.connect(os.environ["TS_HARNESS_SESSION_DB_URL"]) as conn:
        runs = _first_runs(conn, args.days)
        for workflow_id, run in runs.items():
            run["verdict"] = _first_verdict(conn, workflow_id)

    decided = [run for run in runs.values() if run["verdict"]]
    for variant in VARIANTS:
        group = [run for run in decided if run["variant"] == variant]
        if not group:
            print(f"{variant}: no tickets with a gate verdict yet")
            continue
        passes = sum(1 for run in group if run["verdict"] == "pass")
        minutes = statistics.mean(run["minutes"] for run in group)
        # The review found nothing to fix, yet the gate blocked: what giving it the gate's inputs targets.
        missed = sum(1 for run in group if not run["fixed"] and run["verdict"] != "pass")
        print(
            f"{variant}: {len(group)} tickets, first-try gate pass {passes}/{len(group)} "
            f"({passes / len(group):.0%}), missed {missed} (nothing fixed, gate blocked), "
            f"pre-push review {minutes:.1f} min per ticket on average"
        )
        for run in group:
            print(f"  {run['work_order']}: fixed {run['fixed']}, gate {run['verdict']}, review {run['minutes']:.1f} min")

    # The rule applies to the newest variant with any tickets (gate_inputs, then gate_rubric).
    current = next((v for v in VARIANTS[:-1] if any(run["variant"] == v for run in decided)), "gate_rubric")
    trial = [run for run in decided if run["variant"] == current]
    passes = sum(1 for run in trial if run["verdict"] == "pass")
    if len(trial) < args.sample:
        print(f"\nDecision: keep collecting ({len(trial)} of {args.sample} {current} tickets have a gate verdict).")
    elif passes >= args.min_passes:
        print(f"\nDecision: keep the {current} pre-push review ({passes}/{len(trial)} passed the gate first time).")
    else:
        print(
            f"\nDecision: turn the pre-push review off ({passes}/{len(trial)} passed the gate first time, "
            f"below {args.min_passes}/{args.sample}). Set TS_HARNESS_PRE_PUSH_REVIEW_ROUNDS=0."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
