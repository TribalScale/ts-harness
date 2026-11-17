#!/usr/bin/env python3
"""Merge the system cron jobs into the runtime cron file without dropping jobs added in the admin console.

Jobs with the same name are replaced in place; every other job is kept.

  merge-cron-jobs.py --jobs SYSTEM.json --file config/runtime-cron-jobs.json
  merge-cron-jobs.py --jobs SYSTEM.json --memory-api http://127.0.0.1:18792

The second form updates the shared cron tree in Postgres (TS_HARNESS_STATE_BACKEND=postgres)
and needs MEMORY_API_TOKEN in the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CRON_NAMESPACE = "cron"
CRON_FILE = "runtime-cron-jobs.json"


def merge_jobs(existing: list[dict[str, Any]], system: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name = {str(job.get("name")): job for job in system}
    merged = [by_name.pop(str(job.get("name")), job) for job in existing]
    merged.extend(job for job in system if str(job.get("name")) in by_name)
    return merged


def render(jobs: list[dict[str, Any]]) -> str:
    return json.dumps({"jobs": jobs}, indent=2, sort_keys=True) + "\n"


def parse_jobs(text: str) -> list[dict[str, Any]]:
    data = json.loads(text or "{}")
    if isinstance(data, list):
        return [dict(item) for item in data]
    return [dict(item) for item in list(data.get("jobs") or [])]


def merge_file(path: Path, system: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing = parse_jobs(path.read_text(encoding="utf-8")) if path.is_file() else []
    merged = merge_jobs(existing, system)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as handle:
        handle.write(render(merged))
    Path(handle.name).replace(path)
    return merged


def _request(base: str, token: str, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{base.rstrip('/')}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def merge_shared(base: str, token: str, system: list[dict[str, Any]], *, attempts: int = 10) -> list[dict[str, Any]]:
    last_error = ""
    for attempt in range(attempts):
        try:
            status, pulled = _request(base, token, "GET", f"/shared-files/{CRON_NAMESPACE}?since=0")
        except (urllib.error.URLError, OSError) as exc:
            last_error = f"memory API unreachable: {exc}"
            time.sleep(min(2 * (attempt + 1), 10))
            continue
        if status != 200:
            raise SystemExit(f"could not read the cron tree: HTTP {status} {pulled.get('error', '')}")
        current = next((f for f in pulled.get("files") or [] if f.get("path") == CRON_FILE and not f.get("deleted")), None)
        merged = merge_jobs(parse_jobs(current["content"]) if current else [], system)
        status, out = _request(
            base,
            token,
            "POST",
            f"/shared-files/{CRON_NAMESPACE}/commit",
            {
                "base_generation": int(pulled.get("generation") or 0),
                "puts": [{"path": CRON_FILE, "content": render(merged)}],
                "deletes": [],
                "actor": "phase-15",
                "scope": [CRON_FILE],
            },
        )
        if status == 200:
            return merged
        if status == 409 and out.get("code") == "stale_generation":
            last_error = "cron file changed during the merge"
            continue
        raise SystemExit(f"could not commit the cron tree: HTTP {status} {out.get('error', '')}")
    raise SystemExit(f"cron merge gave up: {last_error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--jobs", required=True, type=Path, help="JSON file with the system jobs")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--file", type=Path, help="local cron file to merge into")
    target.add_argument("--memory-api", help="memory API base URL; merges into the shared cron tree")
    args = parser.parse_args(argv)

    system = parse_jobs(args.jobs.read_text(encoding="utf-8"))
    if args.file is not None:
        merged = merge_file(args.file, system)
        where = str(args.file)
    else:
        token = os.environ.get("MEMORY_API_TOKEN", "").strip()
        if not token:
            raise SystemExit("MEMORY_API_TOKEN is required with --memory-api")
        merged = merge_shared(args.memory_api, token, system)
        where = "the shared cron tree"
    print(f"merged {len(system)} system jobs into {where} ({len(merged)} jobs total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
