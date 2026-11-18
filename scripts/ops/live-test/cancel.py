#!/usr/bin/env python3
"""Cancel live-test workflows (and their descendants), or a whole program.

    python3 /app/scripts/ops/live-test/cancel.py <workflow-id> [<workflow-id> ...]
    python3 /app/scripts/ops/live-test/cancel.py --program <program-id> [--compensate]

``--program`` cancels the program the way the console's Cancel does (``cancel_program``):
the program record is closed and its workflows are cancelled with it. Cancelling only the
workflows left the program "running" in the console. ``--compensate`` also runs the
console's cancel compensation (open PRs, Jira, per the tenant's settings).

Closing the test PRs and deleting their branches is otherwise separate (``gh pr close -d``).
"""

from __future__ import annotations

import argparse
import sys


def cancel_program(program_id: str, *, actor: str, reason: str, compensate: bool = False, memory=None) -> int:
    """Close the program and cascade to its workflows; return 1 on failure."""
    from ts_harness.memory.client import get_memory_client

    memory = memory or get_memory_client()
    try:
        result = memory.cancel_program(program_id, actor=actor, reason=reason)
    except Exception as exc:  # noqa: BLE001
        print(program_id, "ERROR", exc, file=sys.stderr)
        return 1
    print(program_id, (result or {}).get("status", "cancelled"))
    if compensate:
        from ts_harness.workflows.program_compensation import compensate_cancelled_program

        try:
            out = compensate_cancelled_program(memory, program_id, reason=reason, actor=actor)
            print(program_id, "compensation", out.get("status"), out.get("actions"))
        except Exception as exc:  # noqa: BLE001
            print(program_id, "compensation ERROR", exc, file=sys.stderr)
            return 1
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("workflow_ids", nargs="*")
    parser.add_argument("--program", help="cancel this program: closes the program and its workflows")
    parser.add_argument("--compensate", action="store_true", help="with --program, also run the console's cancel compensation")
    parser.add_argument("--reason", default="live test done")
    parser.add_argument("--actor", default="live-test")
    args = parser.parse_args(argv)

    failed = 0
    if args.program:
        failed += cancel_program(args.program, actor=args.actor, reason=args.reason, compensate=args.compensate)
    ids = list(args.workflow_ids)
    if not ids:
        if not args.program:
            print("nothing to cancel")
        return 1 if failed else 0

    from ts_harness.workflows.engine import get_workflow_engine

    engine = get_workflow_engine()
    for wf in ids:
        try:
            print(wf, engine.cancel(wf, actor=args.actor, reason=args.reason).get("status"))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(wf, "ERROR", exc, file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
