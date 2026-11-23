#!/usr/bin/env python3
"""Where a ticket's coding runs spent their time.

    docker exec docker-agent-runtime-1 python3 /app/scripts/ops/coding-job-timeline.py PROJ-12
    docker exec docker-agent-runtime-1 python3 /app/scripts/ops/coding-job-timeline.py --last 10

For each coding job: queue wait, harness phases (setup, coding, verification, review, push),
each verification check, and, for jobs that recorded it, every OpenCode session with model
time vs tool time and its slowest tool calls (result.opencode_timeline).
"""

from __future__ import annotations

import argparse
import os
import sys

import psycopg


def _m(seconds) -> str:
    return f"{(seconds or 0) / 60:5.1f}m"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("work_order", nargs="?", help="work order id (e.g. PROJ-12) or job id prefix")
    parser.add_argument("--last", type=int, default=0, help="the last N finished jobs instead")
    args = parser.parse_args()
    if not args.work_order and not args.last:
        parser.error("give a work order id or --last N")
    with psycopg.connect(os.environ["TS_HARNESS_SESSION_DB_URL"]) as con:
        if args.last:
            rows = con.execute(
                "select * from (select id::text, work_order_id, status, created_at, started_at, finished_at, result, spec->>'model'"
                " from coding_jobs where finished_at is not null order by finished_at desc limit %s) j order by created_at",
                (args.last,),
            ).fetchall()
        else:
            rows = con.execute(
                "select id::text, work_order_id, status, created_at, started_at, finished_at, result, spec->>'model'"
                " from coding_jobs where work_order_id = %s or id::text like %s order by created_at",
                (args.work_order, args.work_order + "%"),
            ).fetchall()
    for job_id, wo, status, created, started, finished, result, model in rows:
        result = result or {}
        wait = (started - created).total_seconds() if started and created else 0
        run = (finished - started).total_seconds() if finished and started else 0
        print(f"\n=== {wo} {job_id[:8]} {status} {created:%m-%d %H:%M} queue {_m(wait)} run {_m(run)} model {model}")
        phases = result.get("timings") or {}
        if phases:
            print("  phases:  " + "  ".join(f"{k} {_m(v).strip()}" for k, v in phases.items()))
        review = result.get("pre_push_review") or {}
        if review:
            print(f"  pre-push review: {review.get('status')} reviews={review.get('reviews')} fixed={review.get('fixed')}")
        checks = (result.get("verification") or {}).get("checks") or []
        timed = [c for c in checks if isinstance(c, dict) and (c.get("duration_seconds") or c.get("seconds"))]
        if timed:
            print("  last verification pass: " + "  ".join(
                f"{str(c.get('name') or c.get('command'))[:28]} {(c.get('duration_seconds') or c.get('seconds')):.0f}s" for c in timed))
        timeline = result.get("opencode_timeline") or []
        for s in timeline:
            tools = ", ".join(f"{k} {_m(v).strip()}" for k, v in list((s.get("tools") or {}).items())[:4])
            print(f"  opencode {s.get('title')!r}: wall {_m(s.get('wall_s')).strip()}, model {_m(s.get('model_s')).strip()},"
                  f" tools {_m(s.get('tools_s')).strip()} ({tools}), {s.get('turns')} turns")
            for call in (s.get("slowest") or [])[:5]:
                print(f"      {_m(call.get('s'))} {call.get('tool'):6} {call.get('call')}")
        if not timeline:
            print("  (no OpenCode timeline recorded for this job)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
