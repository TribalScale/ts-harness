#!/usr/bin/env python3
"""Backfill synthetic delivery programs for legacy governed_delivery chains."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path


def _repo_paths() -> tuple[Path, Path]:
    scripts_dir = Path(__file__).resolve().parents[1]
    runtime_dir = scripts_dir.parent / "runtime"
    return scripts_dir, runtime_dir


def _compat_pin_fields() -> dict:
    scripts_dir, runtime_dir = _repo_paths()
    for path in (scripts_dir, runtime_dir):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    from ts_harness.workflows.definition_paths import program_content_revision
    from ts_harness.workflows.program_materialize import (
        build_resolved_workflow_versions,
    )
    from program_legacy_compat import frozen_legacy_compatibility_topology

    snapshot = frozen_legacy_compatibility_topology()
    return {
        "program_type": str(snapshot.get("program_type") or "software_delivery"),
        "program_definition_version": str(
            snapshot.get("definition_version") or "legacy-compat"
        ),
        "program_definition_revision": program_content_revision(snapshot),
        "program_definition_snapshot": snapshot,
        "resolved_workflow_versions": build_resolved_workflow_versions(snapshot),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    parser.add_argument("--tenant-id", default="default")
    args = parser.parse_args()

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError:
        print("psycopg required", file=sys.stderr)
        return 1

    try:
        pin_fields = _compat_pin_fields()
    except Exception as exc:
        print(f"failed to build compatibility pins: {exc}", file=sys.stderr)
        return 1

    host = os.environ.get("TS_HARNESS_DB_HOST", "127.0.0.1")
    port = os.environ.get("TS_HARNESS_DB_PORT", "5432")
    user = os.environ.get("TS_HARNESS_DB_USER", "ts_harness")
    password = os.environ.get("TS_HARNESS_DB_PASSWORD", "")
    dbname = os.environ.get("TS_HARNESS_DB_NAME", "ts_harness_memory")

    dsn = f"host={host} port={port} user={user} password={password} dbname={dbname}"
    created = 0
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT child.id AS child_id,
                       child.workflow_type AS child_type,
                       child.parent_workflow_id AS parent_id,
                       parent.workflow_type AS parent_type,
                       child.source_channel,
                       child.requester_actor_id
                FROM workflows child
                JOIN workflows parent ON parent.id = child.parent_workflow_id
                WHERE child.workflow_type = 'governed_delivery'
                  AND child.program_id IS NULL
                  AND parent.workflow_type = 'prd_request'
                ORDER BY child.created_at
                """
            )
            rows = list(cur.fetchall())

        for row in rows:
            program_id = str(uuid.uuid4())
            summary = f"Backfilled program for {row['child_id']}"
            metadata = {"artifacts": {}, "backfill": True}
            if args.dry_run:
                print(
                    json.dumps(
                        {
                            "program_id": program_id,
                            "child_id": str(row["child_id"]),
                            "program_type": pin_fields["program_type"],
                            "program_definition_version": pin_fields[
                                "program_definition_version"
                            ],
                        }
                    )
                )
                created += 1
                continue

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO delivery_programs
                      (id, tenant_id, summary, binding_key, source_channel,
                       requester_actor_id, entry_phase, current_phase, status, metadata,
                       program_type, program_definition_version,
                       program_definition_revision, program_definition_snapshot,
                       resolved_workflow_versions)
                    VALUES
                      (%s, %s, %s, %s, %s, %s, 'prd', 'implement', 'running', %s::jsonb,
                       %s, %s, %s, %s::jsonb, %s::jsonb)
                    """,
                    (
                        program_id,
                        args.tenant_id,
                        summary,
                        row.get("source_channel"),
                        row.get("source_channel"),
                        row.get("requester_actor_id"),
                        json.dumps(metadata),
                        pin_fields["program_type"],
                        pin_fields["program_definition_version"],
                        pin_fields["program_definition_revision"],
                        json.dumps(pin_fields["program_definition_snapshot"], default=str),
                        json.dumps(pin_fields["resolved_workflow_versions"], default=str),
                    ),
                )
                cur.execute(
                    """
                    UPDATE workflows
                    SET program_id = %s,
                        program_phase = %s,
                        correlation_id = COALESCE(correlation_id, %s)
                    WHERE id = %s
                    """,
                    (program_id, "prd", f"program:{program_id}", row["parent_id"]),
                )
                cur.execute(
                    """
                    UPDATE workflows
                    SET program_id = %s,
                        program_phase = %s,
                        correlation_id = COALESCE(correlation_id, %s)
                    WHERE id = %s
                    """,
                    (program_id, "implement", f"program:{program_id}", row["child_id"]),
                )
            conn.commit()
            created += 1

    print(json.dumps({"backfilled_programs": created, "dry_run": args.dry_run}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
