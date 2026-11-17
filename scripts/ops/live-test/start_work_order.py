#!/usr/bin/env python3
"""Start one deliver_work_order workflow from a work-order markdown file; prints the workflow id.

Run inside the agent-runtime container (see docs/runbooks/rebuild-a-host.md#live-test-a-change)::

    python3 /app/scripts/ops/live-test/start_work_order.py WO-501.md \\
        --repo https://github.com/acme/test-repo --base dev
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("work_order", help="markdown file whose first heading starts with the WO id")
    parser.add_argument("--repo", required=True, help="repository URL")
    parser.add_argument("--base", default="dev", help="base branch (default: dev)")
    parser.add_argument("--actor", default="live-test")
    args = parser.parse_args(argv)

    body = Path(args.work_order).read_text(encoding="utf-8")
    match = re.search(r"^#{1,3}\s*(WO-[A-Za-z0-9-]+)", body, re.M)
    if not match:
        print("no '# WO-<id>' heading in", args.work_order, file=sys.stderr)
        return 1
    wo = match.group(1)

    from ts_harness.memory.client import get_memory_client
    from ts_harness.workflows.hooks.fork_subworkflow import fork_child

    intake = {
        "summary": f"Live test {wo}",
        "repo_url": args.repo,
        "base_branch": args.base,
        "work_order_id": wo,
        "work_order_ref": wo,
        "work_order_body": body,
        "completed_work_order_ids": [],
    }
    print(fork_child(get_memory_client(), parent_workflow_id=None, tenant_id="default", child_type="deliver_work_order", intake_input=intake, actor=args.actor))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
