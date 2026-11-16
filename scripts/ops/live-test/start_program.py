#!/usr/bin/env python3
"""Start a software_delivery program at the implement phase from planner-style work-order markdown.

The program targets the repository configured for implement_work_orders (or the project's
repositories); ``--base`` picks the base branch. Prints the program and workflow ids::

    python3 /app/scripts/ops/live-test/start_program.py work-orders.md --base onboard-be
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("work_orders", help="markdown with '## WO-<n>' sections and optional 'Depends on' rows")
    parser.add_argument("--base", required=True, help="base branch for the work orders")
    parser.add_argument("--summary", default="Live test program")
    parser.add_argument("--actor", default="live-test")
    args = parser.parse_args(argv)

    markdown = Path(args.work_orders).read_text(encoding="utf-8")
    ids = re.findall(r"^#{1,3}\s+(WO-\d+)", markdown, re.M)
    if not ids:
        print("no '## WO-<n>' sections in", args.work_orders, file=sys.stderr)
        return 1

    from ts_harness.workflows.delivery_program import DeliveryProgramService

    result = DeliveryProgramService().create_program(
        entry_phase="implement",
        summary=args.summary,
        actor=args.actor,
        artifacts={
            "work_orders_markdown": markdown,
            "work_order_ids": ids,
            "next_work_order_id": ids[0],
            "planner_complete": True,
            "work_orders_handoff": True,
            "planning_track": "work_orders",
            "default_branch": args.base,
            "completed_work_order_ids": [],
        },
    )
    print(json.dumps({"program_id": result.program_id, "workflow_id": result.workflow_id, "workflow_type": result.workflow_type}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
