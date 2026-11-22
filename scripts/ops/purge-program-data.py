#!/usr/bin/env python3
"""Delete everything the harness stores for one finished program (a client's deletion request).

  purge-program-data.py <program_id> --dry-run
  purge-program-data.py <program_id> --yes [--include-decisions] [--session-id ID ...] [--actor EMAIL]

Removes the program and its child programs, their workflows, tasks, coding jobs, plans, tool
approvals, audit events, program memories, transcripts of sessions tagged with the program (and
of each ``--session-id``) and design images no other program or open workflow uses. LLM usage
rows keep their cost and lose their program, workflow and task ids. Decision records
(``workflow_decisions``, ``program_releases``) are kept unless ``--include-decisions``.
A ``program_data_purged`` audit event records the counts, never content.

Refuses while any of the programs or their workflows is still running or blocked. Run it where
the runtime runs (it needs MEMORY_API_URL, MEMORY_API_TOKEN and the object store settings), e.g.

  sudo docker exec docker-agent-runtime-1 python3 /app/scripts/ops/purge-program-data.py <id> --dry-run

See docs/data-handling.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if (REPO_ROOT / "runtime" / "ts_harness").is_dir():
    sys.path.insert(0, str(REPO_ROOT / "runtime"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Delete one finished program's stored data.")
    parser.add_argument("program_id")
    parser.add_argument("--dry-run", action="store_true", help="count what would be deleted; change nothing")
    parser.add_argument("--yes", action="store_true", help="confirm the deletion (required without --dry-run)")
    parser.add_argument("--include-decisions", action="store_true", help="also delete decision and release records")
    parser.add_argument("--session-id", action="append", default=[], help="also delete this chat session's transcript")
    parser.add_argument("--actor", default="ops", help="who asked, for the audit event")
    return parser.parse_args(argv)


def purge(args: argparse.Namespace, *, client: Any = None, store: Any = None) -> dict[str, Any]:
    if client is None:
        from ts_harness.memory.client import get_memory_client

        client = get_memory_client()
    result = client.purge_program_data(
        args.program_id,
        dry_run=bool(args.dry_run),
        include_decisions=bool(args.include_decisions),
        session_ids=list(args.session_id or []),
        actor=str(args.actor or "ops"),
    )
    keys = [str(k) for k in result.get("object_keys") or [] if str(k).startswith("design/")]
    if args.dry_run:
        result["objects_to_delete"] = len(keys)
        return result
    if keys:
        if store is None:
            from ts_harness.intake.blobs import intake_store

            store = intake_store()
        for key in keys:
            store.delete(key)
    result["objects_deleted"] = len(keys)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.dry_run and not args.yes:
        print("refusing to delete without --yes (run with --dry-run first)", file=sys.stderr)
        return 2
    try:
        result = purge(args)
    except Exception as exc:  # noqa: BLE001 - report and exit non-zero
        print(f"purge failed: {exc}", file=sys.stderr)
        return 1
    result.pop("object_keys", None)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
