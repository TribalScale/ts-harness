#!/usr/bin/env python3
"""Cancel live-test workflows (and their descendants). Pass workflow ids, or --program <id>.

    python3 /app/scripts/ops/live-test/cancel.py <workflow-id> [<workflow-id> ...]
    python3 /app/scripts/ops/live-test/cancel.py --program <program-id>

Closing the test PRs and deleting their branches is separate (``gh pr close -d``).
"""

from __future__ import annotations

import argparse
import os
import sys


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("workflow_ids", nargs="*")
    parser.add_argument("--program", help="cancel every unfinished workflow of this program")
    parser.add_argument("--reason", default="live test done")
    parser.add_argument("--actor", default="live-test")
    args = parser.parse_args(argv)

    ids = list(args.workflow_ids)
    if args.program:
        import psycopg

        cur = psycopg.connect(os.environ["TS_HARNESS_SESSION_DB_URL"]).cursor()
        cur.execute(
            "select id from workflows where program_id=%s and status not in ('succeeded','failed','cancelled','superseded','completed')",
            (args.program,),
        )
        ids += [str(row[0]) for row in cur.fetchall()]
    if not ids:
        print("nothing to cancel")
        return 0

    from ts_harness.workflows.engine import get_workflow_engine

    engine = get_workflow_engine()
    failed = 0
    for wf in ids:
        try:
            print(wf, engine.cancel(wf, actor=args.actor, reason=args.reason).get("status"))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(wf, "ERROR", exc, file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
