#!/usr/bin/env python3
"""Import session records from a JSONL file into the PostgreSQL session store.

Usage:
    SESSION_DB_URL=postgres://... python3 scripts/ops/import-sessions-jsonl.py sessions.jsonl

Arguments:
    FILE        Path to the source JSONL file (each line is a JSON session record)

Environment:
    SESSION_DB_URL   PostgreSQL connection string (required)
    DRY_RUN          Set to "true" to parse and report without writing (default: false)
    BATCH_SIZE       Number of rows per INSERT batch (default: 500)
    UPSERT           Set to "false" to INSERT only, skip conflicts (default: true)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {msg}", flush=True)


def load_records(path: Path) -> list[dict]:
    records = []
    errors = 0
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if not isinstance(rec, dict) or not rec.get("session_id"):
                log(f"  warn: line {i} missing session_id – skipped")
                continue
            records.append(rec)
        except json.JSONDecodeError as exc:
            log(f"  warn: line {i} JSON parse error – {exc}")
            errors += 1
    log(f"Loaded {len(records)} records ({errors} parse errors)")
    return records


def upsert_batch(cur, batch: list[dict], do_upsert: bool) -> int:
    rows = []
    for rec in batch:
        rows.append(
            (
                rec.get("session_id"),
                rec.get("routing_key", ""),
                rec.get("tenant_id", ""),
                rec.get("agent_id", ""),
                rec.get("source", ""),
                rec.get("parent_session_id"),
                rec.get("subagent_id"),
                int(rec.get("depth", 0)),
                rec.get("status", "active"),
                rec.get("started_at"),
                int(rec.get("tool_count", 0)),
                rec.get("compression_parent_id"),
                json.dumps(rec.get("metadata") or {}),
            )
        )

    if do_upsert:
        sql = """
        INSERT INTO runtime_sessions (
            session_id, routing_key, tenant_id, agent_id, source,
            parent_session_id, subagent_id, depth, status, started_at,
            tool_count, compression_parent_id, metadata, updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (session_id) DO UPDATE SET
            status                = EXCLUDED.status,
            tool_count            = EXCLUDED.tool_count,
            compression_parent_id = EXCLUDED.compression_parent_id,
            metadata              = EXCLUDED.metadata,
            updated_at            = NOW()
        """
    else:
        sql = """
        INSERT INTO runtime_sessions (
            session_id, routing_key, tenant_id, agent_id, source,
            parent_session_id, subagent_id, depth, status, started_at,
            tool_count, compression_parent_id, metadata, updated_at
        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        ON CONFLICT (session_id) DO NOTHING
        """

    cur.executemany(sql, rows)
    return len(rows)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    src_path = Path(sys.argv[1])
    if not src_path.exists():
        log(f"ERROR: file not found: {src_path}")
        sys.exit(1)

    db_url = (
        os.environ.get("SESSION_DB_URL")
        or os.environ.get("TS_HARNESS_SESSION_DB_URL")
    )
    if not db_url:
        log("ERROR: SESSION_DB_URL environment variable is required")
        sys.exit(1)

    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    batch_size = int(os.environ.get("BATCH_SIZE", "500"))
    do_upsert = os.environ.get("UPSERT", "true").lower() != "false"

    log(f"Source: {src_path}")
    log(f"Target: {db_url.split('@')[-1] if '@' in db_url else db_url}")
    log(f"Dry-run: {dry_run} | Batch size: {batch_size} | Upsert: {do_upsert}")

    records = load_records(src_path)
    if not records:
        log("Nothing to import")
        return

    if dry_run:
        log(f"Dry-run complete: would import {len(records)} sessions")
        return

    try:
        import psycopg
    except ImportError:
        log("ERROR: psycopg is required (pip install psycopg[binary])")
        sys.exit(1)

    imported = 0
    start = time.time()

    with psycopg.connect(db_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            for offset in range(0, len(records), batch_size):
                batch = records[offset : offset + batch_size]
                count = upsert_batch(cur, batch, do_upsert)
                imported += count
                log(f"  batch {offset // batch_size + 1}: {count} rows")
        conn.commit()

    elapsed = time.time() - start
    log(f"Import complete: {imported} sessions in {elapsed:.2f}s")


if __name__ == "__main__":
    main()
