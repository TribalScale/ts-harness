#!/usr/bin/env python3
"""HTTP adapter for TS Harness memory and workflow primitives."""

from __future__ import annotations

import hmac
import json
import os
import re
import subprocess
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from memory_routes.directory import (  # noqa: E402
    get_directory_user,
    search_directory,
    upsert_directory_user,
)


VALID_MEMORY_TYPES = {
    "episodic",
    "semantic",
    "procedural",
    "relational",
    "directive",
}
VALID_FORGET_ACTIONS = {"expire", "delete"}
ALLOWED_TRANSITIONS = {
    "pending": {"running", "cancelled"},
    "running": {"succeeded", "failed", "blocked", "cancelled"},
    # blocked -> blocked turns an armed external wait into a real block (new reason/output).
    "blocked": {"pending", "blocked", "cancelled"},
    "failed": {"pending", "cancelled"},
    "succeeded": set(),
    "cancelled": set(),
}
DEFAULT_LIMIT = 10
MAX_LIMIT = 50

def _db_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value is not None and str(value).strip():
            return str(value)
    return default


DB_HOST = _db_env("TS_HARNESS_DB_HOST", default="ts-harness-db")
DB_PORT = _db_env("TS_HARNESS_DB_PORT", default="5432")
DB_USER = _db_env("TS_HARNESS_DB_USER", default="ts_harness")
DB_NAME = _db_env("TS_HARNESS_DB_NAME", default="ts_harness_memory")
DB_PASS = _db_env("TS_HARNESS_DB_PASSWORD")
EMBEDDING_DIMENSIONS = int(os.environ.get("EMBEDDINGS_DIMENSIONS", "1536"))
SCRIPTS_DIR = os.environ.get("SCRIPTS_DIR", "/scripts")
EMBED_SCRIPT = os.path.join(SCRIPTS_DIR, "embed.sh")


def validate_memory_type(value: str) -> str:
    if value not in VALID_MEMORY_TYPES:
        allowed = ", ".join(sorted(VALID_MEMORY_TYPES))
        raise ValueError(f"type must be one of: {allowed}")
    return value


def parse_limit(value: Any) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if isinstance(value, bool):
        raise ValueError("limit must be an integer")
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if limit < 1 or limit > MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


def parse_uuid(value: Any) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("id must be a valid UUID") from exc


def parse_optional_uuid(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return parse_uuid(value)


def parse_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("confidence must be a number") from exc
    if confidence < 0 or confidence > 1:
        raise ValueError("confidence must be between 0 and 1")
    return confidence


def normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError("tags must be an array of strings")
    return value


def normalize_metadata(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    return value


def normalize_embedding(value: str) -> str:
    text = value.strip()
    match = re.fullmatch(r"'(?P<vector>\[[^\n]+\])'::vector\(\d+\)", text)
    if match:
        return match.group("vector")
    if re.fullmatch(r"\[[^\n]+\]", text):
        return text
    raise ValueError("embedding command returned an invalid vector literal")


def can_transition(current: str, desired: str) -> bool:
    return desired in ALLOWED_TRANSITIONS.get(current, set())


def validate_status(value: Any) -> str:
    status = str(value)
    if status not in ALLOWED_TRANSITIONS:
        raise ValueError("status is not supported")
    return status


def generate_embedding(text: str) -> str:
    result = subprocess.run(
        ["bash", EMBED_SCRIPT, text],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"embed.sh failed: {result.stderr.strip()}")
    return normalize_embedding(result.stdout)


def _db_connect_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASS,
        "dbname": DB_NAME,
    }
    # RDS and other managed Postgres endpoints require TLS; local docker-db does not.
    if DB_HOST not in {"ts-harness-db", "localhost", "127.0.0.1"}:
        kwargs["sslmode"] = os.environ.get("PGSSLMODE", "require")
    return kwargs


_pool = None
_pool_lock = threading.Lock()


def get_pool():
    """Lazy process-wide pool. check_connection drops connections killed by failover."""
    global _pool
    with _pool_lock:
        if _pool is None:
            try:
                from psycopg.rows import dict_row
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise RuntimeError("psycopg and psycopg-pool are required by memory-api") from exc
            _pool = ConnectionPool(
                min_size=1,
                max_size=int(os.environ.get("MEMORY_API_DB_POOL_MAX", "8")),
                check=ConnectionPool.check_connection,
                max_lifetime=1800,
                kwargs={**_db_connect_kwargs(), "row_factory": dict_row},
                open=True,
            )
        return _pool


@contextmanager
def connect_db():
    with get_pool().connection() as conn:
        yield conn


def remember(
    conn,
    body: dict[str, Any],
    embed: Callable[[str], str] = generate_embedding,
) -> dict[str, str]:
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("'content' is required")
    mem_type = validate_memory_type(body.get("type", "semantic"))
    scope = str(body.get("scope", "global"))
    if _closed_memory_scope(scope):
        raise ValueError("scope is closed")
    tenant_id = str(body.get("tenant_id", "default")).strip() or "default"
    source = str(body.get("source", "memory-api"))
    confidence = parse_confidence(body.get("confidence", 1.0))
    tags = normalize_tags(body.get("tags", []))
    metadata = normalize_metadata(body.get("metadata", {}))
    embedding = normalize_embedding(embed(content))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id FROM memories
            WHERE type = %s AND scope = %s AND tenant_id = %s AND content ILIKE %s
            LIMIT 1
            """,
            (mem_type, scope, tenant_id, f"%{content[:60]}%"),
        )
        existing = cur.fetchone()

        if existing:
            mem_id = str(existing["id"])
            action = "updated"
            cur.execute(
                """
                UPDATE memories
                SET content = %s,
                    embedding = %s::vector,
                    confidence = GREATEST(confidence, %s),
                    tags = %s,
                    metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb,
                    source = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (
                    content,
                    embedding,
                    confidence,
                    tags,
                    json.dumps(metadata),
                    source,
                    mem_id,
                ),
            )
        else:
            mem_id = str(uuid.uuid4())
            action = "created"
            cur.execute(
                """
                INSERT INTO memories
                  (id, type, content, embedding, scope, source, confidence,
                   created_at, updated_at, tags, metadata, tenant_id)
                VALUES
                  (%s, %s, %s, %s::vector, %s, %s, %s,
                   now(), now(), %s, %s::jsonb, %s)
                """,
                (
                    mem_id,
                    mem_type,
                    content,
                    embedding,
                    scope,
                    source,
                    confidence,
                    tags,
                    json.dumps(metadata),
                    tenant_id,
                ),
            )

        cur.execute(
            """
            INSERT INTO memory_audit
              (id, memory_id, action, actor, reason, occurred_at)
            VALUES (uuid_generate_v4(), %s, %s, %s, %s, now())
            """,
            (mem_id, action, source, "memory-api remember"),
        )

    conn.commit()
    return {"id": mem_id, "action": action}


def recall(
    conn,
    body: dict[str, Any],
    embed: Callable[[str], str] = generate_embedding,
) -> list[dict[str, Any]]:
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("'query' is required")
    limit = parse_limit(body.get("limit"))
    scope = body.get("scope")
    department_scope = _department_scope(body.get("department_scope"))
    tenant_id = str(body.get("tenant_id", "default")).strip() or "default"
    embedding = normalize_embedding(embed(query))
    if scope and _closed_memory_scope(str(scope)):
        raise ValueError("scope is closed")

    params: list[Any] = [embedding, tenant_id]
    scope_clause = "AND tenant_id = %s"
    if scope and department_scope:
        scope_clause += " AND (scope = %s OR scope = 'global' OR scope = %s)"
        params.extend([str(scope), department_scope])
    elif scope:
        scope_clause += " AND (scope = %s OR scope = 'global') AND scope NOT ILIKE 'department:%'"
        params.append(str(scope))
    elif department_scope:
        scope_clause += " AND (scope = 'global' OR scope = %s)"
        params.append(department_scope)
    else:
        scope_clause += " AND scope NOT ILIKE 'department:%'"
    params.extend([embedding, limit])

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, type, content, scope, confidence, created_at,
                   1 - (embedding <=> %s::vector) AS similarity
            FROM memories
            WHERE (expires_at IS NULL OR expires_at > now())
              {scope_clause}
            ORDER BY embedding <=> %s::vector
            LIMIT %s
            """,
            tuple(params),
        )
        return list(cur.fetchall())


def forget(conn, body: dict[str, Any]) -> dict[str, str | bool]:
    mem_id = parse_uuid(body.get("id"))
    action = body.get("action", "expire")
    if action not in VALID_FORGET_ACTIONS:
        raise ValueError("action must be 'expire' or 'delete'")
    reason = str(body.get("reason", ""))
    actor = str(body.get("actor", "memory-api"))
    audit_action = "deleted" if action == "delete" else "expired"

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memory_audit
              (id, memory_id, action, actor, reason, occurred_at)
            VALUES (uuid_generate_v4(), %s, %s, %s, %s, now())
            """,
            (mem_id, audit_action, actor, reason),
        )
        if action == "delete":
            cur.execute("DELETE FROM memories WHERE id = %s", (mem_id,))
        else:
            cur.execute(
                "UPDATE memories SET expires_at = now() WHERE id = %s",
                (mem_id,),
            )

    conn.commit()
    return {"ok": True, "action": action, "id": mem_id}


def audit_workflow_event(
    cur,
    action_type: str,
    summary: str,
    details: dict[str, Any],
    actor: str,
) -> None:
    cur.execute(
        """
        INSERT INTO session_audit_log
          (id, session_id, agent_id, action_type, summary, details, occurred_at)
        VALUES
          (uuid_generate_v4(), %s, %s, %s, %s, %s::jsonb, now())
        """,
        (
            str(details.get("workflow_id") or details.get("task_id") or "workflow"),
            actor,
            action_type,
            summary,
            json.dumps(details, default=str),
        ),
    )


def create_workflow_record(conn, body: dict[str, Any]) -> dict[str, str]:
    workflow_id = parse_uuid(body.get("id") or body.get("workflow_id"))
    workflow_type = str(body.get("workflow_type", "generic")).strip()
    if not workflow_type:
        raise ValueError("workflow_type is required")
    tenant_id = str(body.get("tenant_id", "default")).strip() or "default"
    actor = str(body.get("actor", "orchestrator"))
    definition_version = str(body.get("definition_version", "1.0.0"))
    source_channel = body.get("source_channel")
    parent_workflow_id = body.get("parent_workflow_id")
    requester_actor_id = body.get("requester_actor_id")
    correlation_id = body.get("correlation_id")
    program_id = parse_optional_uuid(body.get("program_id"))
    program_phase = body.get("program_phase")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflows
              (id, tenant_id, workflow_type, definition_version,
               source_channel, requester_actor_id, correlation_id, parent_workflow_id,
               program_id, program_phase)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                workflow_id,
                tenant_id,
                workflow_type,
                definition_version,
                source_channel,
                requester_actor_id,
                correlation_id,
                parse_uuid(parent_workflow_id) if parent_workflow_id else None,
                program_id,
                str(program_phase) if program_phase else None,
            ),
        )
        audit_workflow_event(
            cur,
            "workflow_record_created",
            f"Workflow record created: {workflow_type}",
            {
                "workflow_id": workflow_id,
                "workflow_type": workflow_type,
                "source_channel": source_channel,
            },
            actor,
        )
    conn.commit()
    return {"id": workflow_id, "workflow_type": workflow_type, "tenant_id": tenant_id}


def get_workflow_record(conn, workflow_id: str) -> dict[str, Any]:
    parsed_id = parse_uuid(workflow_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM workflows
            WHERE id = %s
            """,
            (parsed_id,),
        )
        record = cur.fetchone()
        if not record:
            raise ValueError("workflow not found")
        cur.execute(
            """
            SELECT *
            FROM workflow_tasks
            WHERE workflow_id = %s
            ORDER BY created_at
            """,
            (parsed_id,),
        )
        tasks = list(cur.fetchall())
    return {"workflow": record, "tasks": tasks}


def find_workflow_by_ref(conn, external_ref: str) -> dict[str, Any]:
    ref = str(external_ref or "").strip()
    if not ref:
        raise ValueError("external_ref is required")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM workflows
            WHERE source_channel = %s
              AND status = 'running'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (ref,),
        )
        record = cur.fetchone()
        if record:
            cur.execute(
                """
                SELECT *
                FROM workflow_tasks
                WHERE workflow_id = %s
                ORDER BY created_at
                """,
                (record["id"],),
            )
            return {"workflow": record, "tasks": list(cur.fetchall())}
        cur.execute(
            """
            SELECT workflow_id
            FROM workflow_tasks
            WHERE external_ref = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (ref,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError("workflow not found for external_ref")
        workflow_id = row["workflow_id"]
        cur.execute(
            """
            SELECT *
            FROM workflows
            WHERE id = %s
            """,
            (workflow_id,),
        )
        record = cur.fetchone()
        cur.execute(
            """
            SELECT *
            FROM workflow_tasks
            WHERE workflow_id = %s
            ORDER BY created_at
            """,
            (workflow_id,),
        )
        tasks = list(cur.fetchall())
    return {"workflow": record, "tasks": tasks}


def create_workflow_task(conn, body: dict[str, Any]) -> dict[str, str]:
    workflow_id = parse_uuid(body.get("workflow_id"))
    task_id = str(uuid.uuid4())
    step_name = str(body.get("step_name", "")).strip()
    agent_id = str(body.get("agent_id", "")).strip()
    if not step_name:
        raise ValueError("step_name is required")
    if not agent_id:
        raise ValueError("agent_id is required")
    approval_required = bool(body.get("approval_required", False))
    approval_status = "pending" if approval_required else "not_required"
    actor = str(body.get("actor", "orchestrator"))
    tenant_id = str(body.get("tenant_id", "default")).strip() or "default"

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_tasks
              (id, workflow_id, tenant_id, workflow_type, external_ref, step_name,
               agent_id, depends_on, input, approval_required,
               approval_status, max_retries, contract, definition_version)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                task_id,
                workflow_id,
                tenant_id,
                str(body.get("workflow_type", "generic")),
                body.get("external_ref"),
                step_name,
                agent_id,
                body.get("depends_on"),
                json.dumps(body.get("input", {})),
                approval_required,
                approval_status,
                int(body.get("max_retries", 0)),
                json.dumps(body.get("contract") or {}),
                body.get("definition_version"),
            ),
        )
        audit_workflow_event(
            cur,
            "workflow_task_created",
            f"Workflow task created: {step_name}",
            {"workflow_id": workflow_id, "task_id": task_id, "agent_id": agent_id},
            actor,
        )
        _reopen_workflow_for_new_tasks(cur, workflow_id)
    conn.commit()
    return {"id": task_id, "workflow_id": workflow_id}


def list_ready_workflow_tasks(conn, body: dict[str, Any]) -> list[dict[str, Any]]:
    limit = parse_limit(body.get("limit"))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM workflow_tasks
            WHERE status = 'pending'
              AND attempt <= max_retries
              AND (approval_required = false OR approval_status = 'approved')
              AND NOT EXISTS (
                  SELECT 1 FROM workflow_tasks dep
                  WHERE dep.id = ANY(workflow_tasks.depends_on)
                    AND dep.status NOT IN ('succeeded')
              )
            ORDER BY created_at
            LIMIT %s
            """,
            (limit,),
        )
        return list(cur.fetchall())


def retry_workflow_task(conn, body: dict[str, Any]) -> dict[str, str]:
    task_id = parse_uuid(body.get("id"))
    actor = str(body.get("actor", "workflow-runner"))
    reason = str(body.get("reason", "retry requested"))

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_tasks
            SET status = 'pending',
                error = NULL,
                completed_at = NULL,
                locked_by = NULL,
                lock_expires_at = NULL,
                next_attempt_at = now() + make_interval(
                    secs => LEAST(300, POWER(2, GREATEST(attempt, 1))::int)
                )
            WHERE id = %s
              AND status IN ('failed', 'blocked')
              AND attempt <= max_retries
            """,
            (task_id,),
        )
        if getattr(cur, "rowcount", 1) == 0:
            raise ValueError("workflow task retry was rejected")
        audit_workflow_event(
            cur,
            "workflow_task_retried",
            "Workflow task returned to pending",
            {"task_id": task_id, "reason": reason},
            actor,
        )
    conn.commit()
    return {"id": task_id, "status": "pending"}


def resume_tool_approval_task(conn, body: dict[str, Any]) -> dict[str, str]:
    task_id = parse_uuid(body.get("id"))
    approval_id = str(body.get("approval_id") or "").strip()
    actor = str(body.get("actor", "orchestrator"))
    if not approval_id:
        raise ValueError("approval_id is required")

    with conn.cursor() as cur:
        # Claim increments attempt, so give the paused attempt back.
        cur.execute(
            """
            UPDATE workflow_tasks
            SET status = 'pending',
                error = NULL,
                completed_at = NULL,
                locked_by = NULL,
                lock_expires_at = NULL,
                attempt = GREATEST(attempt - 1, 0),
                next_attempt_at = now()
            WHERE id = %s
              AND status = 'blocked'
              AND error = %s
            """,
            (task_id, f"awaiting_tool_approval:{approval_id}"),
        )
        if getattr(cur, "rowcount", 1) == 0:
            raise ValueError("workflow task is not awaiting this approval")
        audit_workflow_event(
            cur,
            "workflow_task_approval_resumed",
            "Workflow task resumed after tool approval decision",
            {"task_id": task_id, "approval_id": approval_id},
            actor,
        )
    conn.commit()
    return {"id": task_id, "status": "pending"}


def resume_blocked_task(conn, body: dict[str, Any]) -> dict[str, str]:
    """Return a step that is blocked for good (not waiting on an event or approval) to pending.

    A human decision uses this; ``input`` is merged into the task input so the
    runner knows what was decided.
    """
    task_id = parse_uuid(body.get("id"))
    actor = str(body.get("actor", "orchestrator"))
    patch = body.get("input") or {}
    if not isinstance(patch, dict):
        raise ValueError("input must be an object")

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_tasks
            SET status = 'pending',
                error = NULL,
                completed_at = NULL,
                locked_by = NULL,
                lock_expires_at = NULL,
                attempt = GREATEST(attempt - 1, 0),
                next_attempt_at = now(),
                input = COALESCE(input, '{}'::jsonb) || %s::jsonb
            WHERE id = %s
              AND status = 'blocked'
              AND COALESCE(error, '') NOT LIKE 'awaiting%%'
            """,
            (json.dumps(patch), task_id),
        )
        if getattr(cur, "rowcount", 1) == 0:
            raise ValueError("workflow task is not blocked for a human decision")
        audit_workflow_event(
            cur,
            "workflow_task_human_resumed",
            "Workflow task resumed by a human decision",
            {"task_id": task_id, "reason": str(body.get("reason") or "")},
            actor,
        )
    conn.commit()
    return {"id": task_id, "status": "pending"}


def list_stuck_tasks(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Steps parked for a human decision (revise, accept or stop) in running workflows."""
    tenant_id = str(body.get("tenant_id") or "default")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT t.id, t.workflow_id, t.workflow_type, t.step_name, t.agent_id,
                   t.error, t.input, t.output, t.created_at, t.updated_at,
                   w.parent_workflow_id, w.program_id, w.source_channel
            FROM workflow_tasks t
            JOIN workflows w ON w.id = t.workflow_id
            WHERE t.tenant_id = %s
              AND t.status = 'blocked'
              AND t.input ->> '_awaiting_human_decision' IS NOT NULL
              AND w.status = 'running'
            ORDER BY t.updated_at DESC
            LIMIT 100
            """,
            (tenant_id,),
        )
        return {"tasks": list(cur.fetchall())}


def record_runtime_task(conn, body: dict[str, Any]) -> dict[str, str]:
    task_id = parse_uuid(body.get("id"))
    runtime_task_id = str(body.get("runtime_task_id", "")).strip()
    actor = str(body.get("actor", "workflow-runner"))
    if not runtime_task_id:
        raise ValueError("runtime_task_id is required")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT runtime_task_id FROM workflow_tasks WHERE id = %s",
            (task_id,),
        )
        task = cur.fetchone()
        if not task:
            raise ValueError("workflow task not found")
        prior_runtime_task_id = task.get("runtime_task_id")
        cur.execute(
            """
            UPDATE workflow_tasks
            SET runtime_task_id = %s
            WHERE id = %s
            """,
            (runtime_task_id, task_id),
        )
        audit_workflow_event(
            cur,
            "workflow_task_runtime_recorded",
            "Workflow task runtime id recorded",
            {
                "task_id": task_id,
                "runtime_task_id": runtime_task_id,
                "prior_runtime_task_id": prior_runtime_task_id,
            },
            actor,
        )
    conn.commit()
    return {"id": task_id, "runtime_task_id": runtime_task_id}


def list_stale_workflow_tasks(conn, body: dict[str, Any]) -> list[dict[str, Any]]:
    limit = parse_limit(body.get("limit"))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM workflow_tasks
            WHERE status = 'running'
              AND lock_expires_at IS NOT NULL
              AND lock_expires_at < now()
            ORDER BY lock_expires_at
            LIMIT %s
            """,
            (limit,),
        )
        return list(cur.fetchall())


def _normalize_proposed_tasks(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise ValueError("proposed_tasks must be an array")
    return value


def _insert_workflow_tasks(
    cur,
    workflow_id: str,
    workflow_type: str,
    tasks: list[dict[str, Any]],
    actor: str,
    pre_approved: bool,
    default_max_retries: int,
    tenant_id: str = "default",
) -> list[str]:
    created: list[str] = []
    for task_body in tasks:
        if not isinstance(task_body, dict):
            raise ValueError("each task must be an object")
        task_id = str(uuid.uuid4())
        step_name = str(task_body.get("step_name", "")).strip()
        agent_id = str(task_body.get("agent_id", "")).strip()
        if not step_name:
            raise ValueError("step_name is required")
        if not agent_id:
            raise ValueError("agent_id is required")
        approval_required = bool(task_body.get("approval_required", False)) and not pre_approved
        approval_status = "pending" if approval_required else "not_required"
        tenant_id = str(task_body.get("tenant_id", tenant_id)).strip() or tenant_id
        cur.execute(
            """
            INSERT INTO workflow_tasks
              (id, workflow_id, tenant_id, workflow_type, external_ref, step_name,
               agent_id, depends_on, input, approval_required,
               approval_status, max_retries, contract, definition_version)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                task_id,
                workflow_id,
                tenant_id,
                str(task_body.get("workflow_type", workflow_type)),
                task_body.get("external_ref"),
                step_name,
                agent_id,
                task_body.get("depends_on"),
                json.dumps(task_body.get("input", {})),
                approval_required,
                approval_status,
                int(task_body.get("max_retries", default_max_retries)),
                json.dumps(task_body.get("contract") or {}),
                task_body.get("definition_version"),
            ),
        )
        created.append(task_id)
    audit_workflow_event(
        cur,
        "workflow_tasks_expanded",
        "Workflow tasks expanded",
        {"workflow_id": workflow_id, "task_ids": created},
        actor,
    )
    _reopen_workflow_for_new_tasks(cur, workflow_id)
    return created


def create_workflow_tasks(conn, body: dict[str, Any]) -> dict[str, Any]:
    workflow_id = parse_uuid(body.get("workflow_id"))
    workflow_type = str(body.get("workflow_type", "generic"))
    actor = str(body.get("actor", "workflow-runner"))
    tasks = body.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("tasks must be a non-empty array")
    tenant_id = str(body.get("tenant_id", "default")).strip() or "default"
    pre_approved = bool(body.get("pre_approved", False))

    with conn.cursor() as cur:
        created = _insert_workflow_tasks(
            cur,
            workflow_id,
            workflow_type,
            tasks,
            actor,
            pre_approved,
            int(body.get("max_retries", 0)),
            tenant_id=tenant_id,
        )
    conn.commit()
    return {"workflow_id": workflow_id, "task_ids": created}


def propose_workflow_plan(conn, body: dict[str, Any]) -> dict[str, str]:
    workflow_id = parse_uuid(body.get("workflow_id"))
    parent_task_id = parse_uuid(body.get("parent_task_id"))
    proposed_tasks = body.get("proposed_tasks")
    actor = str(body.get("actor", "workflow-runner"))
    if not isinstance(proposed_tasks, list) or not proposed_tasks:
        raise ValueError("proposed_tasks must be a non-empty array")

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO workflow_plans
              (id, workflow_id, parent_task_id, proposed_tasks, approval_status)
            VALUES (uuid_generate_v4(), %s, %s, %s::jsonb, 'pending')
            RETURNING id
            """,
            (workflow_id, parent_task_id, json.dumps(proposed_tasks)),
        )
        plan = cur.fetchone() or {}
        plan_id = str(plan.get("id"))
        audit_workflow_event(
            cur,
            "workflow_tasks_expanded",
            "Workflow plan proposed",
            {
                "workflow_id": workflow_id,
                "parent_task_id": parent_task_id,
                "plan_id": plan_id,
            },
            actor,
        )
    conn.commit()
    return {"plan_id": plan_id}


def get_workflow_plan(conn, plan_id: str) -> dict[str, Any]:
    parsed_plan_id = parse_uuid(plan_id)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM workflow_plans WHERE id = %s", (parsed_plan_id,))
        plan = cur.fetchone()
        if not plan:
            raise ValueError("workflow plan not found")
        return dict(plan)


# Static barrier stages whose depends_on must be patched with the dynamic task
# IDs materialized from an approved plan, so the barrier fans in over the exact
# children it aggregates (Phase 2 Jira delivery repair).
BARRIER_STEP_NAMES = ("collect_delivery_results",)


def _patch_barrier_dependencies(
    cur,
    workflow_id: str,
    created_ids: list[str],
) -> list[str]:
    """Append created task IDs to any static delivery barrier's depends_on.

    Runs inside the plan-approval transaction so the barrier can never become
    ready before the dynamic implement tasks it aggregates are recorded.
    """
    if not created_ids:
        return []
    patched: list[str] = []
    for step_name in BARRIER_STEP_NAMES:
        cur.execute(
            """
            UPDATE workflow_tasks
            SET depends_on = (
                SELECT ARRAY(
                    SELECT DISTINCT unnest(
                        COALESCE(depends_on, ARRAY[]::uuid[]) || %s::uuid[]
                    )
                )
            )
            WHERE workflow_id = %s
              AND step_name = %s
              AND status IN ('pending', 'blocked')
            RETURNING id
            """,
            (created_ids, workflow_id, step_name),
        )
        for row in cur.fetchall():
            patched.append(str(row.get("id")))
    return patched


def approve_workflow_plan(conn, body: dict[str, Any]) -> dict[str, Any]:
    plan_id = parse_uuid(body.get("plan_id"))
    actor = str(body.get("actor", "workflow-runner"))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM workflow_plans WHERE id = %s FOR UPDATE",
            (plan_id,),
        )
        plan = cur.fetchone()
        if not plan:
            raise ValueError("workflow plan not found")
        if plan.get("approval_status") != "pending":
            raise ValueError("workflow plan is not pending approval")
        workflow_id = str(plan.get("workflow_id"))
        proposed_tasks = _normalize_proposed_tasks(plan.get("proposed_tasks") or [])
        created = _insert_workflow_tasks(
            cur,
            workflow_id,
            "generic",
            proposed_tasks,
            actor,
            True,
            0,
        )
        barrier_patched = _patch_barrier_dependencies(cur, workflow_id, created)
        cur.execute(
            """
            UPDATE workflow_plans
            SET approval_status = 'approved',
                approved_by = %s,
                approved_at = now(),
                materialized_at = now()
            WHERE id = %s
            """,
            (actor, plan_id),
        )
        audit_workflow_event(
            cur,
            "workflow_plan_approved",
            "Workflow plan approved",
            {
                "workflow_id": workflow_id,
                "plan_id": plan_id,
                "task_ids": created,
                "barrier_patched": barrier_patched,
            },
            actor,
        )

    conn.commit()
    return {
        "plan_id": plan_id,
        "task_ids": created,
        "barrier_patched": barrier_patched,
    }


def reject_workflow_plan(conn, body: dict[str, Any]) -> dict[str, str]:
    plan_id = parse_uuid(body.get("plan_id"))
    actor = str(body.get("actor", "workflow-runner"))
    rejection_reason = str(body.get("rejection_reason", ""))

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_plans
            SET approval_status = 'rejected',
                approved_by = %s,
                approved_at = now(),
                rejection_reason = %s
            WHERE id = %s
              AND approval_status = 'pending'
            """,
            (actor, rejection_reason, plan_id),
        )
        if getattr(cur, "rowcount", 1) == 0:
            raise ValueError("workflow plan rejection was rejected")
        audit_workflow_event(
            cur,
            "workflow_plan_rejected",
            "Workflow plan rejected",
            {"plan_id": plan_id, "rejection_reason": rejection_reason},
            actor,
        )
    conn.commit()
    return {"plan_id": plan_id, "approval_status": "rejected"}


def claim_workflow_task(conn, body: dict[str, Any]) -> dict[str, str | bool]:
    task_id = parse_uuid(body.get("id"))
    agent_id = str(body.get("agent_id", "")).strip()
    actor = str(body.get("actor", agent_id or "orchestrator"))
    if not agent_id:
        raise ValueError("agent_id is required")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, approval_required, approval_status
            FROM workflow_tasks
            WHERE id = %s
            """,
            (task_id,),
        )
        task = cur.fetchone()
        if not task:
            raise ValueError("workflow task not found")
        if task.get("approval_status") == "rejected":
            raise ValueError("workflow task approval was rejected")
        if task.get("approval_required") and task.get("approval_status") != "approved":
            raise ValueError("workflow task requires approval before claim")

        locked_by = body.get("locked_by")
        lease_seconds = body.get("lease_seconds")
        if locked_by is not None or lease_seconds is not None:
            cur.execute(
                "SELECT claim_workflow_task(%s, %s, %s, %s) AS claimed",
                (
                    task_id,
                    agent_id,
                    None if locked_by is None else str(locked_by),
                    None if lease_seconds is None else int(lease_seconds),
                ),
            )
        else:
            cur.execute(
                "SELECT claim_workflow_task(%s, %s) AS claimed",
                (task_id, agent_id),
            )
        claimed_row = cur.fetchone() or {}
        claimed = bool(claimed_row.get("claimed"))
        if claimed:
            audit_workflow_event(
                cur,
                "workflow_task_claimed",
                f"Workflow task claimed by {agent_id}",
                {"task_id": task_id, "agent_id": agent_id},
                actor,
            )
    conn.commit()
    return {"claimed": claimed, "id": task_id}


_TASK_TERMINAL = frozenset({"succeeded", "cancelled", "skipped", "failed"})


def _reopen_workflow_for_new_tasks(cur, workflow_id: str) -> None:
    """A revision inserts tasks after completion may already have run. Keep the workflow running."""
    cur.execute(
        """
        UPDATE workflows
        SET status = 'running', completed_at = NULL, cancellation_reason = NULL
        WHERE id = %s AND status IN ('succeeded', 'failed')
        RETURNING id
        """,
        (workflow_id,),
    )
    if cur.fetchone() is None:
        return


def _revision_awaiting_tasks(cur, workflow_id: str) -> bool:
    """A cancel for a revision that never inserted its replacement tasks is not completion."""
    cur.execute(
        """
        SELECT 1
        FROM workflow_tasks cancelled
        WHERE cancelled.workflow_id = %s
          AND cancelled.status = 'cancelled'
          AND cancelled.error = 'superseded by revision'
          AND NOT EXISTS (
            SELECT 1 FROM workflow_tasks later
            WHERE later.workflow_id = cancelled.workflow_id
              AND later.created_at > cancelled.completed_at
          )
        LIMIT 1
        """,
        (workflow_id,),
    )
    return cur.fetchone() is not None


def maybe_complete_workflow(cur, workflow_id: str, actor: str) -> str | None:
    """Mark a running workflow succeeded or failed once every task is terminal."""
    cur.execute("SELECT status FROM workflows WHERE id = %s", (workflow_id,))
    row = cur.fetchone()
    if not row or row.get("status") != "running":
        return None
    cur.execute("SELECT status FROM workflow_tasks WHERE workflow_id = %s", (workflow_id,))
    statuses = [str(item.get("status") or "") for item in cur.fetchall()]
    if not statuses or any(status not in _TASK_TERMINAL for status in statuses):
        return None
    if any(status == "failed" for status in statuses):
        new_status, reason = "failed", "a task failed"
    elif any(status == "succeeded" for status in statuses):
        if any(status == "cancelled" for status in statuses) and _revision_awaiting_tasks(cur, workflow_id):
            return None
        new_status, reason = "succeeded", "all tasks terminal"
    else:
        return None
    cur.execute(
        """
        UPDATE workflows
        SET status = %s, completed_at = now(), cancellation_reason = %s
        WHERE id = %s AND status = 'running'
        """,
        (new_status, reason, workflow_id),
    )
    audit_workflow_event(
        cur,
        "workflow_completed" if new_status == "succeeded" else "workflow_failed",
        f"Workflow {new_status}: {reason}",
        {"workflow_id": workflow_id, "reason": reason, "status": new_status},
        actor,
    )
    return new_status


def sweep_settled_workflows(conn) -> int:
    """Complete running workflows whose tasks are already terminal. Returns how many changed."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM workflows WHERE status = 'running'")
        workflow_ids = [str(row["id"]) for row in cur.fetchall()]
        changed = 0
        for workflow_id in workflow_ids:
            if maybe_complete_workflow(cur, workflow_id, "workflow-sweep"):
                changed += 1
    conn.commit()
    return changed


def transition_workflow_task(conn, body: dict[str, Any]) -> dict[str, str]:
    task_id = parse_uuid(body.get("id"))
    desired = validate_status(body.get("status"))
    actor = str(body.get("actor", "orchestrator"))
    has_output = "output" in body
    output = body.get("output") if has_output else None
    error = body.get("error")

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, approval_required, approval_status, workflow_id
            FROM workflow_tasks
            WHERE id = %s
            """,
            (task_id,),
        )
        task = cur.fetchone()
        if not task:
            raise ValueError("workflow task not found")
        current = task["status"]
        if not can_transition(current, desired):
            raise ValueError(f"illegal workflow transition: {current} -> {desired}")
        if desired == "succeeded" and task.get("approval_required"):
            if task.get("approval_status") != "approved":
                raise ValueError("workflow task cannot complete before approval")

        if has_output:
            cur.execute(
                """
                UPDATE workflow_tasks
                SET status = %s,
                    output = COALESCE(%s::jsonb, output),
                    error = %s,
                    completed_at = CASE
                        WHEN %s IN ('succeeded', 'failed', 'cancelled') THEN now()
                        ELSE completed_at
                    END
                WHERE id = %s
                  AND (%s != 'succeeded'
                       OR approval_required = false
                       OR approval_status = 'approved')
                """,
                (
                    desired,
                    json.dumps(output if output is not None else {}),
                    error,
                    desired,
                    task_id,
                    desired,
                ),
            )
        else:
            cur.execute(
                """
                UPDATE workflow_tasks
                SET status = %s,
                    error = %s,
                    completed_at = CASE
                        WHEN %s IN ('succeeded', 'failed', 'cancelled') THEN now()
                        ELSE completed_at
                    END
                WHERE id = %s
                  AND (%s != 'succeeded'
                       OR approval_required = false
                       OR approval_status = 'approved')
                """,
                (desired, error, desired, task_id, desired),
            )
        if getattr(cur, "rowcount", 1) == 0:
            raise ValueError("workflow task transition was rejected")
        action_type = f"workflow_task_{desired}"
        if error == "task lease expired" and desired in {"failed", "blocked"}:
            action_type = "workflow_task_timed_out"
        audit_workflow_event(
            cur,
            action_type,
            f"Workflow task transitioned to {desired}",
            {"task_id": task_id, "from": current, "to": desired},
            actor,
        )
        if desired in _TASK_TERMINAL and task.get("workflow_id"):
            maybe_complete_workflow(cur, str(task["workflow_id"]), actor)
    conn.commit()
    return {"id": task_id, "status": desired}


VALID_DECISION_KINDS = {"gate", "track_choice", "phase_entry", "advance", "promote", "governance_setting"}


def _actor_roles(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return sorted({str(item).strip().lower() for item in raw if str(item).strip()})


# Default role bundles. This process does not install the ts_harness package, and
# effective_bundles(None) is these defaults. A caller that already resolved custom
# bundles sends them as actor_permissions.
_DEFAULT_ROLE_BUNDLES = {
    "viewer": frozenset({"view"}),
    "member": frozenset({"view"}),
    "requester": frozenset({"view", "start_program"}),
    "approver": frozenset({"view", "approve_gate", "confirm_delegation"}),
    "admin": frozenset(
        {
            "view",
            "start_program",
            "approve_gate",
            "confirm_delegation",
            "edit_config",
            "edit_models",
            "edit_allowlists",
            "promote_department",
        }
    ),
}
_DEFAULT_ROLE_BUNDLES["owner"] = _DEFAULT_ROLE_BUNDLES["admin"]


def _actor_permission_set(body: dict[str, Any], roles: list[str]) -> set[str]:
    explicit = body.get("actor_permissions")
    if isinstance(explicit, list):
        return {str(item).strip().lower() for item in explicit if str(item).strip()}
    granted: set[str] = set()
    for role in roles:
        granted.update(_DEFAULT_ROLE_BUNDLES.get(role, ()))
    return granted


def _permission_allows(granted: set[str], required: list[str]) -> bool:
    needed = {item for item in required if item}
    return bool(needed and granted & needed)


def _holds_required_role(roles: list[str], granted: set[str], required: str) -> bool:
    if required in roles:
        return True
    bundle = _DEFAULT_ROLE_BUNDLES.get(required)
    return bool(bundle) and set(bundle) <= granted


def _gate_permission_denial(
    required_roles: list[str],
    required_permissions: list[str],
    roles: list[str],
    granted: set[str],
    extra_permission: str = "",
) -> str:
    if required_permissions:
        denied = (
            ""
            if _permission_allows(granted, required_permissions)
            else f"approval requires one of permissions: {', '.join(required_permissions)}"
        )
    elif not required_roles:
        denied = ""
    elif any(_holds_required_role(roles, granted, name) for name in required_roles):
        denied = ""
    else:
        denied = f"approval requires one of roles: {', '.join(required_roles)}"
    extra = str(extra_permission or "").strip().lower()
    if extra and extra not in granted:
        if denied:
            return denied
        return f"approval requires permission: {extra}"
    return denied


def record_workflow_decision(cur, body: dict[str, Any]) -> str:
    """Insert one workflow_decisions row and audit it; caller commits."""
    kind = str(body.get("kind") or "").strip()
    if kind not in VALID_DECISION_KINDS:
        raise ValueError(f"decision kind must be one of: {', '.join(sorted(VALID_DECISION_KINDS))}")
    decision = str(body.get("decision") or "").strip()
    if not decision:
        raise ValueError("decision is required")
    actor_id = str(body.get("actor_id") or body.get("actor") or "").strip()
    if not actor_id:
        raise ValueError("actor_id is required")
    decision_id = str(uuid.uuid4())
    program_raw = str(body.get("program_id") or "").strip()
    workflow_raw = str(body.get("workflow_id") or "").strip()
    task_raw = str(body.get("task_id") or "").strip()
    details = body.get("details") if isinstance(body.get("details"), dict) else {}
    actor_email = str(body.get("actor_email") or "").strip().lower() or None
    roles = _actor_roles(body.get("actor_roles"))
    cur.execute(
        """
        INSERT INTO workflow_decisions
          (id, tenant_id, program_id, workflow_id, task_id, kind, decision,
           actor_id, actor_email, actor_roles, reason, details, expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s)
        """,
        (
            decision_id,
            str(body.get("tenant_id") or "default").strip() or "default",
            parse_uuid(program_raw) if program_raw else None,
            parse_uuid(workflow_raw) if workflow_raw else None,
            parse_uuid(task_raw) if task_raw else None,
            kind,
            decision,
            actor_id,
            actor_email,
            json.dumps(roles),
            str(body.get("reason") or "").strip() or None,
            json.dumps(details, default=str),
            body.get("expires_at"),
        ),
    )
    audit_workflow_event(
        cur,
        "workflow_decision_recorded",
        f"Decision recorded: {kind} {decision}",
        {
            "decision_id": decision_id,
            "kind": kind,
            "decision": decision,
            "program_id": program_raw or None,
            "workflow_id": workflow_raw or None,
            "task_id": task_raw or None,
            "actor_email": actor_email,
        },
        actor_id,
    )
    return decision_id


def create_workflow_decision(conn, body: dict[str, Any]) -> dict[str, Any]:
    with conn.cursor() as cur:
        decision_id = record_workflow_decision(cur, body)
    conn.commit()
    return {"id": decision_id, "kind": body.get("kind"), "decision": body.get("decision")}


def list_workflow_decisions(
    conn,
    *,
    program_id: str | None = None,
    workflow_id: str | None = None,
    kind: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    if program_id:
        clauses.append("program_id = %s")
        params.append(parse_uuid(program_id))
    if workflow_id:
        clauses.append("workflow_id = %s")
        params.append(parse_uuid(workflow_id))
    if kind:
        clauses.append("kind = %s")
        params.append(str(kind))
    if not clauses:
        raise ValueError("program_id or workflow_id is required")
    try:
        limit_n = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit_n = 100
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, tenant_id, program_id, workflow_id, task_id, kind, decision,
                   actor_id, actor_email, actor_roles, reason, details, expires_at, created_at
            FROM workflow_decisions
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at
            LIMIT %s
            """,
            tuple(params + [limit_n]),
        )
        rows = list(cur.fetchall())
    return {"decisions": rows}


def _task_approval_policy(task: dict[str, Any]) -> dict[str, Any]:
    raw_input = task.get("input") or {}
    if isinstance(raw_input, str):
        try:
            raw_input = json.loads(raw_input)
        except json.JSONDecodeError:
            raw_input = {}
    policy = raw_input.get("_approval_policy") if isinstance(raw_input, dict) else None
    return dict(policy) if isinstance(policy, dict) else {}


def _gate_expires_at(cur, task: dict[str, Any], policy: dict[str, Any]) -> datetime | None:
    explicit = task.get("expires_at")
    if isinstance(explicit, datetime):
        return explicit
    try:
        seconds = int(policy.get("expires_after_seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds <= 0:
        return None
    ready_at = None
    depends_on = [str(item) for item in (task.get("depends_on") or []) if str(item).strip()]
    if depends_on:
        cur.execute(
            "SELECT MAX(completed_at) AS ready_at FROM workflow_tasks WHERE id = ANY(%s::uuid[])",
            (depends_on,),
        )
        row = cur.fetchone() or {}
        ready_at = row.get("ready_at")
    if not isinstance(ready_at, datetime):
        ready_at = task.get("created_at")
    if not isinstance(ready_at, datetime):
        return None
    if ready_at.tzinfo is None:
        ready_at = ready_at.replace(tzinfo=timezone.utc)
    return ready_at + timedelta(seconds=seconds)


def _enforce_gate_policy(
    cur,
    conn,
    *,
    task: dict[str, Any],
    task_id: str,
    body: dict[str, Any],
    approved: bool,
    actor: str,
) -> None:
    policy = _task_approval_policy(task)
    if not policy:
        return
    roles = _actor_roles(body.get("actor_roles"))
    required = _actor_roles(policy.get("required_roles"))
    required_permissions = _actor_roles(policy.get("required_permissions"))
    granted = _actor_permission_set(body, roles)
    extra_permission = str(policy.get("required_permission") or "").strip().lower()
    decision_base = {
        "kind": _gate_decision_kind(policy),
        "tenant_id": task.get("tenant_id") or body.get("tenant_id") or "default",
        "program_id": body.get("program_id"),
        "workflow_id": str(task.get("workflow_id") or "") or None,
        "task_id": task_id,
        "actor_id": actor,
        "actor_email": body.get("actor_email"),
        "actor_roles": roles,
        "details": {
            "step_name": task.get("step_name"),
            "required_roles": required,
            "required_permissions": required_permissions,
            "required_permission": extra_permission,
        },
    }
    denied_reason = _gate_permission_denial(
        required,
        required_permissions,
        roles,
        granted,
        extra_permission,
    )
    if denied_reason:
        record_workflow_decision(cur, {**decision_base, "decision": "denied"})
        audit_workflow_event(
            cur,
            "workflow_gate_denied",
            f"Gate {task.get('step_name')} denied: {denied_reason}",
            {
                "task_id": task_id,
                "required_roles": required,
                "required_permissions": required_permissions,
                "actor_roles": roles,
                "actor_permissions": sorted(granted),
            },
            actor,
        )
        conn.commit()
        raise ValueError(denied_reason)
    if not approved:
        return
    if policy.get("require_dependencies_succeeded"):
        waiting = _unfinished_dependencies(cur, task)
        if waiting:
            raise ValueError(f"approval not allowed yet; waiting on: {', '.join(waiting)}")
    expires_at = _gate_expires_at(cur, task, policy)
    if expires_at is not None and datetime.now(timezone.utc) > expires_at:
        escalate_to = str(policy.get("escalate_to_role") or "").strip() or None
        record_workflow_decision(
            cur,
            {**decision_base, "decision": "expired", "expires_at": expires_at},
        )
        audit_workflow_event(
            cur,
            "workflow_gate_expired",
            f"Gate {task.get('step_name')} expired",
            {"task_id": task_id, "expires_at": expires_at, "escalate_to_role": escalate_to},
            actor,
        )
        cur.execute(
            "UPDATE workflow_tasks SET expires_at = COALESCE(expires_at, %s) WHERE id = %s",
            (expires_at, task_id),
        )
        conn.commit()
        suffix = f"; escalated to {escalate_to}" if escalate_to else ""
        raise ValueError(f"approval gate expired at {expires_at.isoformat()}{suffix}")


def _gate_decision_kind(policy: dict[str, Any]) -> str:
    kind = str(policy.get("decision_kind") or "gate")
    return kind if kind in {"gate", "promote"} else "gate"


def _unfinished_dependencies(cur, task: dict[str, Any]) -> list[str]:
    deps = [parse_uuid(str(dep)) for dep in task.get("depends_on") or [] if dep]
    if not deps:
        return []
    cur.execute(
        "SELECT step_name, status FROM workflow_tasks WHERE id = ANY(%s::uuid[])",
        (deps,),
    )
    return sorted(
        str(row.get("step_name") or "?") for row in cur.fetchall() if row.get("status") != "succeeded"
    )


def approve_workflow_task(conn, body: dict[str, Any], approved: bool) -> dict[str, str]:
    task_id = parse_uuid(body.get("id"))
    actor = str(body.get("actor", "orchestrator"))
    approval_status = "approved" if approved else "rejected"
    action = "workflow_task_approved" if approved else "workflow_task_rejected"
    output = body.get("output") if isinstance(body.get("output"), dict) else None

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, approval_required, step_name, input, workflow_id, tenant_id,
                   depends_on, expires_at, created_at
            FROM workflow_tasks
            WHERE id = %s
            """,
            (task_id,),
        )
        task = cur.fetchone()
        if not task:
            raise ValueError("workflow task not found")
        caller_tenant = str(body.get("tenant_id") or "").strip()
        if caller_tenant and str(task.get("tenant_id") or "default") != caller_tenant:
            raise ValueError("workflow task not found")
        _enforce_gate_policy(
            cur,
            conn,
            task=task,
            task_id=task_id,
            body=body,
            approved=approved,
            actor=actor,
        )

        # Approval gates are human decisions, not agent work. Mark approved gates
        # succeeded here so the ready-queue never dispatches them to orchestrator.
        complete_gate = bool(approved and task.get("approval_required"))
        gate_output = output or {
            "summary": f"Approved {task.get('step_name') or 'gate'}",
            "task_id": task_id,
            "step_name": task.get("step_name"),
        }
        if complete_gate:
            cur.execute(
                """
                UPDATE workflow_tasks
                SET approval_status = %s,
                    approved_by = %s,
                    approved_at = now(),
                    status = 'succeeded',
                    output = COALESCE(%s::jsonb, output),
                    error = NULL,
                    completed_at = now(),
                    locked_by = NULL,
                    lock_expires_at = NULL,
                    updated_at = now()
                WHERE id = %s
                  AND status IN ('pending', 'running', 'blocked')
                """,
                (approval_status, actor, json.dumps(gate_output), task_id),
            )
        else:
            cur.execute(
                """
                UPDATE workflow_tasks
                SET approval_status = %s,
                    approved_by = %s,
                    approved_at = now()
                WHERE id = %s
                """,
                (approval_status, actor, task_id),
            )
        if getattr(cur, "rowcount", 1) == 0:
            raise ValueError("workflow task approval was rejected")
        if complete_gate and task.get("workflow_id"):
            maybe_complete_workflow(cur, str(task["workflow_id"]), actor)
        audit_workflow_event(
            cur,
            action,
            f"Workflow task {approval_status}",
            {
                "task_id": task_id,
                "approval_status": approval_status,
                "completed_gate": complete_gate,
            },
            actor,
        )
        decision_id = None
        if task.get("approval_required"):
            policy = _task_approval_policy(task)
            decision_id = record_workflow_decision(
                cur,
                {
                    "kind": _gate_decision_kind(policy),
                    "decision": approval_status,
                    "tenant_id": task.get("tenant_id") or body.get("tenant_id") or "default",
                    "program_id": body.get("program_id"),
                    "workflow_id": str(task.get("workflow_id") or "") or None,
                    "task_id": task_id,
                    "actor_id": actor,
                    "actor_email": body.get("actor_email"),
                    "actor_roles": body.get("actor_roles"),
                    "reason": body.get("reason"),
                    "details": {
                        **(policy.get("details") if isinstance(policy.get("details"), dict) else {}),
                        "step_name": task.get("step_name"),
                    },
                },
            )
    conn.commit()
    result = {"id": task_id, "approval_status": approval_status}
    if decision_id:
        result["decision_id"] = decision_id
    if complete_gate:
        result["status"] = "succeeded"
    return result


def get_workflow(conn, workflow_id: str) -> dict[str, Any]:
    parsed_workflow_id = parse_uuid(workflow_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM workflow_tasks
            WHERE workflow_id = %s
            ORDER BY created_at
            """,
            (parsed_workflow_id,),
        )
        return {"workflow_id": parsed_workflow_id, "tasks": list(cur.fetchall())}


def get_workflow_children(conn, workflow_id: str) -> dict[str, Any]:
    parsed_id = parse_uuid(workflow_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM workflows
            WHERE parent_workflow_id = %s
            ORDER BY created_at
            """,
            (parsed_id,),
        )
        return {"children": list(cur.fetchall())}


def patch_workflow_task_input(conn, task_id: str, body: dict[str, Any]) -> dict[str, str]:
    parsed_id = parse_uuid(task_id)
    actor = str(body.get("actor", "orchestrator"))
    input_data = body.get("input")
    if not isinstance(input_data, dict):
        raise ValueError("input must be an object")

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_tasks
            SET input = %s::jsonb
            WHERE id = %s
            """,
            (json.dumps(input_data), parsed_id),
        )
        if getattr(cur, "rowcount", 1) == 0:
            raise ValueError("workflow task not found")
        audit_workflow_event(
            cur,
            "workflow_task_input_patched",
            "Workflow task input updated",
            {"task_id": parsed_id},
            actor,
        )
    conn.commit()
    return {"id": parsed_id}


def set_task_wait_state(conn, body: dict[str, Any]) -> dict[str, str]:
    task_id = parse_uuid(body.get("id"))
    wait_token = str(body.get("wait_token") or "").strip()
    expected_event_type = str(body.get("expected_event_type") or "").strip()
    actor = str(body.get("actor", "workflow-runner"))
    if not wait_token:
        raise ValueError("wait_token is required")
    now = datetime.now(timezone.utc)
    escalate_at = _deadline_after(now, body.get("escalate_after_seconds"))
    expires_at = _deadline_after(now, body.get("expires_after_seconds"))

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_tasks
            SET wait_token = %s,
                expected_event_type = %s,
                escalate_at = %s,
                expires_at = %s,
                escalated_at = NULL
            WHERE id = %s
            """,
            (wait_token, expected_event_type or None, escalate_at, expires_at, task_id),
        )
        audit_workflow_event(
            cur,
            "workflow_task_wait_set",
            "Workflow task external wait configured",
            {
                "task_id": task_id,
                "wait_token": wait_token,
                "expected_event_type": expected_event_type,
                "escalate_at": escalate_at,
                "expires_at": expires_at,
            },
            actor,
        )
    conn.commit()
    return {"id": task_id, "wait_token": wait_token, "escalate_at": escalate_at, "expires_at": expires_at}


def _deadline_after(now: datetime, seconds: Any) -> datetime | None:
    try:
        value = int(seconds or 0)
    except (TypeError, ValueError):
        return None
    return now + timedelta(seconds=value) if value > 0 else None


WAIT_DEADLINE_KINDS = {"escalated", "expired"}


def _stamp_gate_deadlines(cur, tenant_id: str) -> None:
    """Store expires_at/escalate_at on ready approval gates whose policy sets a deadline."""
    cur.execute(
        """
        SELECT *
        FROM workflow_tasks
        WHERE tenant_id = %s
          AND status = 'pending'
          AND approval_required = true
          AND COALESCE(approval_status, 'pending') = 'pending'
          AND expires_at IS NULL
          AND input -> '_approval_policy' ? 'expires_after_seconds'
        """,
        (tenant_id,),
    )
    for gate in list(cur.fetchall()):
        depends_on = [str(item) for item in (gate.get("depends_on") or []) if str(item).strip()]
        if depends_on:
            cur.execute(
                "SELECT COUNT(*) AS open FROM workflow_tasks WHERE id = ANY(%s::uuid[]) AND status <> 'succeeded'",
                (depends_on,),
            )
            if int((cur.fetchone() or {}).get("open") or 0) > 0:
                continue
        expires_at = _gate_expires_at(cur, gate, _task_approval_policy(gate))
        if expires_at is None:
            continue
        cur.execute(
            """
            UPDATE workflow_tasks
            SET expires_at = %s,
                escalate_at = COALESCE(escalate_at, %s)
            WHERE id = %s
            """,
            (expires_at, expires_at, str(gate.get("id"))),
        )


def list_due_waits(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Blocked waits and pending gates past escalate_at (not yet escalated) or expires_at (not yet expired)."""
    tenant_id = str(body.get("tenant_id") or "default").strip() or "default"
    with conn.cursor() as cur:
        _stamp_gate_deadlines(cur, tenant_id)
        cur.execute(
            """
            SELECT *
            FROM workflow_tasks
            WHERE tenant_id = %s
              AND status IN ('pending', 'blocked')
              AND (
                (escalate_at IS NOT NULL AND escalate_at <= now() AND escalated_at IS NULL)
                OR (expires_at IS NOT NULL AND expires_at <= now()
                    AND COALESCE(input ->> '_recommended_action', '') = '')
              )
            ORDER BY COALESCE(expires_at, escalate_at)
            """,
            (tenant_id,),
        )
        rows = list(cur.fetchall())
    conn.commit()
    now = datetime.now(timezone.utc)
    waits = []
    for row in rows:
        task_input = row.get("input") or {}
        if isinstance(task_input, str):
            try:
                task_input = json.loads(task_input)
            except json.JSONDecodeError:
                task_input = {}
        expires_at = row.get("expires_at")
        expired = isinstance(expires_at, datetime) and expires_at <= now and not task_input.get("_recommended_action")
        waits.append(
            {
                "task_id": str(row.get("id")),
                "workflow_id": str(row.get("workflow_id") or ""),
                "workflow_type": row.get("workflow_type"),
                "step_name": row.get("step_name"),
                "status": row.get("status"),
                "approval_required": bool(row.get("approval_required")),
                "expected_event_type": row.get("expected_event_type"),
                "escalate_at": row.get("escalate_at"),
                "expires_at": expires_at,
                "escalated_at": row.get("escalated_at"),
                "due": "expired" if expired else "escalated",
                "approval_policy": _task_approval_policy(row),
            }
        )
    return {"tenant_id": tenant_id, "waits": waits}


def mark_wait_deadline(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Record an escalation or expiry; the task stays blocked or pending, never failed."""
    task_id = parse_uuid(body.get("id"))
    kind = str(body.get("kind") or "").strip()
    if kind not in WAIT_DEADLINE_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(sorted(WAIT_DEADLINE_KINDS))}")
    recommended_action = str(body.get("recommended_action") or "").strip()
    if kind == "expired" and not recommended_action:
        raise ValueError("recommended_action is required when a wait expires")
    details = body.get("details") if isinstance(body.get("details"), dict) else {}
    actor = str(body.get("actor") or "escalate-stale-waits")
    with conn.cursor() as cur:
        if kind == "expired":
            cur.execute(
                """
                UPDATE workflow_tasks
                SET escalated_at = COALESCE(escalated_at, now()),
                    input = COALESCE(input, '{}'::jsonb) || jsonb_build_object('_recommended_action', %s::text)
                WHERE id = %s
                """,
                (recommended_action, task_id),
            )
        else:
            cur.execute("UPDATE workflow_tasks SET escalated_at = now() WHERE id = %s", (task_id,))
        if getattr(cur, "rowcount", 1) == 0:
            raise ValueError("task not found")
        audit_workflow_event(
            cur,
            "workflow_wait_expired" if kind == "expired" else "workflow_wait_escalated",
            f"Workflow wait {kind}",
            {
                **details,
                "task_id": task_id,
                "workflow_id": details.get("workflow_id"),
                "recommended_action": recommended_action or None,
            },
            actor,
        )
    conn.commit()
    return {"id": task_id, "kind": kind, "recommended_action": recommended_action or None}


def clear_task_wait_state(conn, body: dict[str, Any]) -> dict[str, str]:
    task_id = parse_uuid(body.get("id"))
    actor = str(body.get("actor", "orchestrator"))
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_tasks
            SET wait_token = NULL,
                expected_event_type = NULL
            WHERE id = %s
            """,
            (task_id,),
        )
        audit_workflow_event(
            cur,
            "workflow_task_wait_cleared",
            "Workflow task external wait cleared",
            {"task_id": task_id},
            actor,
        )
    conn.commit()
    return {"id": task_id}


def cancel_workflow_bulk(conn, workflow_id: str, body: dict[str, Any]) -> dict[str, Any]:
    parsed_id = parse_uuid(workflow_id)
    actor = str(body.get("actor", "orchestrator"))
    reason = str(body.get("reason") or "cancelled")

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflow_tasks
            SET status = 'cancelled',
                error = %s,
                completed_at = now()
            WHERE workflow_id = %s
              AND status IN ('pending', 'running', 'blocked')
            """,
            (reason, parsed_id),
        )
        cur.execute(
            """
            UPDATE workflows
            SET status = 'cancelled',
                cancellation_reason = %s
            WHERE id = %s
            """,
            (reason, parsed_id),
        )
        audit_workflow_event(
            cur,
            "workflow_cancelled",
            f"Workflow cancelled: {reason}",
            {"workflow_id": parsed_id, "reason": reason},
            actor,
        )
    conn.commit()
    return {"workflow_id": parsed_id, "status": "cancelled", "reason": reason}


def complete_workflow_record(conn, workflow_id: str, body: dict[str, Any]) -> dict[str, Any]:
    parsed_id = parse_uuid(workflow_id)
    actor = str(body.get("actor", "orchestrator"))
    reason = str(body.get("reason") or "completed")
    superseded_by = body.get("superseded_by")
    status = "superseded" if superseded_by else "succeeded"
    superseded_id = parse_uuid(str(superseded_by)) if superseded_by else None
    if superseded_by:
        reason = f"superseded_by={superseded_by}:{reason}"

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflows
            SET status = %s,
                completed_at = now(),
                cancellation_reason = %s,
                superseded_by_workflow_id = %s
            WHERE id = %s
            """,
            (status, reason, superseded_id, parsed_id),
        )
        audit_workflow_event(
            cur,
            "workflow_completed" if status == "succeeded" else "workflow_superseded",
            f"Workflow {status}: {reason}",
            {
                "workflow_id": parsed_id,
                "reason": reason,
                "superseded_by": superseded_by,
                "status": status,
            },
            actor,
        )
    conn.commit()
    return {"workflow_id": parsed_id, "status": status, "reason": reason}


def _legacy_compatibility_topology() -> dict[str, Any]:
    """Frozen topology for pre-pinning rows with a null snapshot.

    Uses the explicitly named FROZEN_LEGACY_COMPATIBILITY_TOPOLOGY payload.
    Do not mutate persisted legacy rows to attach this payload.
    """
    import sys
    from pathlib import Path

    # memory-api runs /app/memory-gateway.py but mounts helpers at SCRIPTS_DIR.
    for candidate in (SCRIPTS_DIR, str(Path(__file__).resolve().parent)):
        if candidate and candidate not in sys.path:
            sys.path.insert(0, candidate)
    from program_legacy_compat import frozen_legacy_compatibility_topology

    return frozen_legacy_compatibility_topology()



def _parse_json_object(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
        return None
    return None


def _program_topology(program: dict[str, Any] | None = None, *, snapshot: Any = None) -> dict[str, Any]:
    raw = snapshot if snapshot is not None else (program or {}).get("program_definition_snapshot")
    parsed = _parse_json_object(raw)
    if parsed and isinstance(parsed.get("nodes"), list) and parsed.get("nodes"):
        return parsed
    return _legacy_compatibility_topology()


def _topology_node_ids(topology: dict[str, Any]) -> tuple[str, ...]:
    nodes = topology.get("nodes") or []
    ids: list[str] = []
    for node in nodes:
        if isinstance(node, dict):
            node_id = str(node.get("id") or "").strip()
            if node_id:
                ids.append(node_id)
    return tuple(ids)


def _topology_next_phases(topology: dict[str, Any], current: str) -> tuple[str, ...]:
    edges = topology.get("edges") or []
    targets: list[str] = []
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        if str(edge.get("source") or "").strip() != current:
            continue
        target = str(edge.get("target") or "").strip()
        if target and target not in targets:
            targets.append(target)
    return tuple(targets)


def _detached_startable_nodes(topology: dict[str, Any]) -> set[str]:
    """Startable nodes with no edges. Rollback is the builtin example."""
    node_ids = list(_topology_node_ids(topology))
    indegree = {node_id: 0 for node_id in node_ids}
    outdegree = {node_id: 0 for node_id in node_ids}
    for edge in topology.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        if source not in indegree or target not in indegree:
            continue
        indegree[target] += 1
        outdegree[source] += 1
    startable = {
        str(node).strip()
        for node in (topology.get("startable_nodes") or [])
        if str(node).strip()
    }
    return {
        node_id
        for node_id in node_ids
        if node_id in startable and indegree[node_id] == 0 and outdegree[node_id] == 0
    }


def _topology_phase_order(topology: dict[str, Any]) -> list[str]:
    node_ids = list(_topology_node_ids(topology))
    if not node_ids:
        return []
    indegree = {node_id: 0 for node_id in node_ids}
    children: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in topology.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        if source not in indegree or target not in indegree:
            continue
        children[source].append(target)
        indegree[target] += 1
    detached = _detached_startable_nodes(topology)
    queue = [
        node_id
        for node_id in node_ids
        if indegree[node_id] == 0 and node_id not in detached
    ]
    ordered: list[str] = []
    while queue:
        node_id = queue.pop(0)
        ordered.append(node_id)
        for child in children[node_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    for node_id in node_ids:
        if node_id not in ordered:
            ordered.append(node_id)
    return ordered


def _visible_phase_order(
    topology: dict[str, Any],
    *,
    current_phase: str,
    phases: dict[str, Any],
) -> list[str]:
    """Return topological order with untaken branch siblings hidden."""
    order = _topology_phase_order(topology)
    started = {
        phase
        for phase, info in phases.items()
        if str((info or {}).get("state") or "not_started") != "not_started"
    }
    if current_phase:
        started.add(current_phase)

    hidden: set[str] = set()
    by_source: dict[str, list[str]] = {}
    for edge in topology.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        source = str(edge.get("source") or "").strip()
        target = str(edge.get("target") or "").strip()
        if not source or not target:
            continue
        by_source.setdefault(source, []).append(target)

    for siblings in by_source.values():
        unique = []
        for sibling in siblings:
            if sibling not in unique:
                unique.append(sibling)
        if len(unique) < 2:
            continue
        taken = [sibling for sibling in unique if sibling in started]
        if not taken:
            continue
        for sibling in unique:
            if sibling not in taken:
                hidden.add(sibling)

    detached = _detached_startable_nodes(topology)
    for node_id in detached:
        state = str((phases.get(node_id) or {}).get("state") or "not_started")
        if state == "not_started":
            hidden.add(node_id)

    return [phase for phase in order if phase not in hidden]


def _validate_phase_id(phase: str, topology: dict[str, Any], *, field: str) -> str:
    node_id = str(phase or "").strip()
    allowed = _topology_node_ids(topology)
    if node_id not in allowed:
        raise ValueError(f"{field} must be one of: {', '.join(allowed)}")
    return node_id


def _program_metadata_artifacts(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    artifacts = metadata.get("artifacts")
    return dict(artifacts) if isinstance(artifacts, dict) else {}


VALID_PROJECT_SOURCES = {"inferred_jira", "inferred_repo", "user"}
VALID_BRIEF_KINDS = {"decision", "fact", "directive", "pointer"}
DEPARTMENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
BRIEF_KIND_MEMORY_TYPE = {
    "decision": "episodic",
    "directive": "directive",
    "fact": "semantic",
    "pointer": "semantic",
}


def _closed_memory_scope(scope: str) -> bool:
    folded = scope.strip().lower()
    return folded == "global" or folded.startswith("department:")


def _optional_department(value: Any) -> str | None:
    if value is None:
        return None
    name = str(value).strip().lower()
    if not name:
        return None
    if not DEPARTMENT_NAME_RE.fullmatch(name):
        raise ValueError("invalid department")
    return name


def _department_scope(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if not text.startswith("department:"):
        raise ValueError("invalid department_scope")
    name = text.removeprefix("department:")
    if not DEPARTMENT_NAME_RE.fullmatch(name):
        raise ValueError("invalid department_scope")
    return f"department:{name}"


def _normalize_project_fields(
    project_id: Any,
    project_id_source: Any,
) -> tuple[str | None, str | None]:
    raw = None if project_id is None else str(project_id).strip()
    normalized = raw or None
    source = None if project_id_source is None else str(project_id_source).strip() or None
    if source and source not in VALID_PROJECT_SOURCES:
        allowed = ", ".join(sorted(VALID_PROJECT_SOURCES))
        raise ValueError(f"project_id_source must be one of: {allowed}")
    if normalized and not source:
        source = "user"
    if not normalized:
        source = None
    return normalized, source


def _load_program_row(conn, program_id: str) -> dict[str, Any]:
    parsed_id = parse_uuid(program_id)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM delivery_programs WHERE id = %s", (parsed_id,))
        program = cur.fetchone()
    if not program:
        raise ValueError("program not found")
    return program


def create_delivery_program(
    conn,
    body: dict[str, Any],
    *,
    department: str | None = None,
) -> dict[str, Any]:
    program_id = parse_uuid(body.get("id") or body.get("program_id") or str(uuid.uuid4()))
    tenant_id = str(body.get("tenant_id", "default")).strip() or "default"
    summary = str(body.get("summary") or "").strip()
    if not summary:
        raise ValueError("summary is required")
    snapshot = _parse_json_object(body.get("program_definition_snapshot"))
    topology = _program_topology(snapshot=snapshot)
    entry_phase = _validate_phase_id(
        str(body.get("entry_phase") or body.get("current_phase") or ""),
        topology,
        field="entry_phase",
    )
    current_phase = _validate_phase_id(
        str(body.get("current_phase") or entry_phase),
        topology,
        field="current_phase",
    )
    actor = str(body.get("actor", "orchestrator"))
    metadata = dict(body.get("metadata") or {})
    artifacts = body.get("artifacts")
    if isinstance(artifacts, dict):
        metadata.setdefault("artifacts", artifacts)

    program_type = body.get("program_type")
    if program_type is not None:
        program_type = str(program_type).strip() or None
    program_definition_version = body.get("program_definition_version")
    if program_definition_version is not None:
        program_definition_version = str(program_definition_version).strip() or None
    program_definition_revision = body.get("program_definition_revision")
    if program_definition_revision is not None:
        program_definition_revision = str(program_definition_revision).strip() or None
    resolved_workflow_versions = _parse_json_object(body.get("resolved_workflow_versions"))
    snapshot_json = json.dumps(snapshot, default=str) if snapshot is not None else None
    resolved_json = (
        json.dumps(resolved_workflow_versions, default=str)
        if resolved_workflow_versions is not None
        else None
    )
    project_id, project_id_source = _normalize_project_fields(
        body.get("project_id"),
        body.get("project_id_source"),
    )
    excluded_from_autoshare = bool(body.get("excluded_from_autoshare", False))
    # Body field "department" is ignored. Callers pass the dedicated argument.
    stored_department = _optional_department(department)
    parent_program_id, root_program_id = _resolve_program_parent(
        conn,
        body.get("parent_program_id"),
        tenant_id=tenant_id,
        program_id=program_id,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO delivery_programs
              (id, tenant_id, summary, display_name, binding_key, source_channel,
               requester_actor_id, entry_phase, current_phase, status, metadata,
               program_type, program_definition_version, program_definition_revision,
               program_definition_snapshot, resolved_workflow_versions,
               project_id, project_id_source, excluded_from_autoshare,
               department,
               parent_program_id, root_program_id)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
               %s, %s, %s, %s::jsonb, %s::jsonb,
               %s, %s, %s,
               %s,
               %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (
                program_id,
                tenant_id,
                summary,
                body.get("display_name"),
                body.get("binding_key"),
                body.get("source_channel"),
                body.get("requester_actor_id"),
                entry_phase,
                current_phase,
                str(body.get("status") or "running"),
                json.dumps(metadata, default=str),
                program_type,
                program_definition_version,
                program_definition_revision,
                snapshot_json,
                resolved_json,
                project_id,
                project_id_source,
                excluded_from_autoshare,
                stored_department,
                parent_program_id,
                root_program_id,
            ),
        )
        audit_workflow_event(
            cur,
            "delivery_program_created",
            f"Delivery program created at {entry_phase}",
            {
                "program_id": program_id,
                "entry_phase": entry_phase,
                "program_type": program_type,
                "program_definition_version": program_definition_version,
                "program_definition_revision": program_definition_revision,
                "parent_program_id": parent_program_id,
            },
            actor,
        )
        if body.get("actor_email") or body.get("requester_actor_id"):
            record_workflow_decision(
                cur,
                {
                    "kind": "phase_entry",
                    "decision": entry_phase,
                    "tenant_id": tenant_id,
                    "program_id": program_id,
                    "actor_id": str(body.get("requester_actor_id") or actor),
                    "actor_email": body.get("actor_email"),
                    "actor_roles": body.get("actor_roles"),
                    "details": {"program_type": program_type},
                },
            )
        if parent_program_id:
            audit_workflow_event(
                cur,
                "program_child_attached",
                "Child program attached to parent",
                {
                    "program_id": program_id,
                    "parent_program_id": parent_program_id,
                    "root_program_id": root_program_id,
                    "program_type": program_type,
                },
                actor,
            )
    conn.commit()
    return {
        "id": program_id,
        "entry_phase": entry_phase,
        "current_phase": current_phase,
        "program_type": program_type,
        "program_definition_version": program_definition_version,
        "program_definition_revision": program_definition_revision,
        "project_id": project_id,
        "project_id_source": project_id_source,
        "excluded_from_autoshare": excluded_from_autoshare,
        "department": stored_department,
        "parent_program_id": parent_program_id,
        "root_program_id": root_program_id,
    }


def _resolve_program_parent(
    conn,
    raw_parent: Any,
    *,
    tenant_id: str,
    program_id: str,
) -> tuple[str | None, str | None]:
    """Validate a parent program and return (parent_id, root_id)."""
    text = str(raw_parent or "").strip()
    if not text:
        return None, None
    parent_id = parse_uuid(text)
    if parent_id == program_id:
        raise ValueError("a program cannot be its own parent")
    parent = _load_program_row(conn, parent_id)
    if str(parent.get("tenant_id") or "default") != tenant_id:
        raise ValueError("cross-tenant parent program rejected")
    if str(parent.get("status") or "") == "cancelled":
        raise ValueError("cannot attach a child to a cancelled program")
    root_id = str(parent.get("root_program_id") or "").strip() or parent_id
    if root_id == program_id:
        raise ValueError("program hierarchy cycle rejected")
    return parent_id, root_id


def list_program_children(conn, program_id: str) -> dict[str, Any]:
    parsed_id = parse_uuid(program_id)
    _load_program_row(conn, parsed_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, tenant_id, summary, display_name, program_type, current_phase,
                   status, metadata, parent_program_id, root_program_id,
                   created_at, updated_at
            FROM delivery_programs
            WHERE parent_program_id = %s
            ORDER BY created_at
            """,
            (parsed_id,),
        )
        rows = list(cur.fetchall())
    children = []
    for row in rows:
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        children.append(
            {
                "id": str(row.get("id")),
                "summary": row.get("summary"),
                "display_name": row.get("display_name"),
                "program_type": row.get("program_type"),
                "current_phase": row.get("current_phase"),
                "status": row.get("status"),
                "parent_program_id": str(row.get("parent_program_id") or "") or None,
                "root_program_id": str(row.get("root_program_id") or "") or None,
                "child_count": int(row.get("child_count") or 0),
                "artifacts": _program_metadata_artifacts(metadata),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
        )
    return {"program_id": parsed_id, "children": children}


def _descendant_program_ids(cur, program_id: str, *, active_only: bool = False) -> list[str]:
    status_filter = " WHERE p.status IN ('running', 'blocked')" if active_only else ""
    cur.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT id FROM delivery_programs WHERE parent_program_id = %s
            UNION
            SELECT p.id FROM delivery_programs p JOIN tree t ON p.parent_program_id = t.id
        )
        SELECT t.id FROM tree t JOIN delivery_programs p ON p.id = t.id
        """
        + status_filter,
        (program_id,),
    )
    return [str(row.get("id")) for row in cur.fetchall()]


VALID_EXTERNAL_REF_KINDS = {"jira_issue", "pull_request", "branch", "deployment", "confluence_page"}
VALID_EXTERNAL_REF_STATES = {"open", "merged", "closed", "deleted", "labelled", "failed"}


def record_program_external_ref(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    program = _load_program_row(conn, program_id)
    parsed_id = parse_uuid(program_id)
    tenant_id = str(body.get("tenant_id") or program.get("tenant_id") or "default").strip() or "default"
    if str(program.get("tenant_id") or "default") != tenant_id:
        raise ValueError("cross-tenant external ref rejected")
    kind = str(body.get("kind") or "").strip()
    if kind not in VALID_EXTERNAL_REF_KINDS:
        raise ValueError(f"kind must be one of: {', '.join(sorted(VALID_EXTERNAL_REF_KINDS))}")
    ref = str(body.get("ref") or "").strip()
    if not ref:
        raise ValueError("ref is required")
    state = str(body.get("state") or "open").strip()
    if state not in VALID_EXTERNAL_REF_STATES:
        raise ValueError(f"state must be one of: {', '.join(sorted(VALID_EXTERNAL_REF_STATES))}")
    workflow_raw = str(body.get("workflow_id") or "").strip()
    workflow_id = parse_uuid(workflow_raw) if workflow_raw else None
    metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    actor = str(body.get("actor") or "orchestrator")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO program_external_refs
              (id, tenant_id, program_id, workflow_id, kind, ref, state, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (program_id, kind, ref) DO UPDATE
              SET state = EXCLUDED.state,
                  workflow_id = COALESCE(EXCLUDED.workflow_id, program_external_refs.workflow_id),
                  metadata = program_external_refs.metadata || EXCLUDED.metadata,
                  updated_at = now()
            """,
            (
                str(uuid.uuid4()),
                tenant_id,
                parsed_id,
                workflow_id,
                kind,
                ref,
                state,
                json.dumps(metadata, default=str),
            ),
        )
        audit_workflow_event(
            cur,
            "program_external_ref_recorded",
            f"Program external {kind} recorded",
            {"program_id": parsed_id, "workflow_id": workflow_id, "kind": kind, "ref": ref, "state": state},
            actor,
        )
    conn.commit()
    return {"program_id": parsed_id, "kind": kind, "ref": ref, "state": state}


def list_program_external_refs(
    conn,
    program_id: str,
    *,
    include_descendants: bool = False,
) -> dict[str, Any]:
    parsed_id = parse_uuid(program_id)
    _load_program_row(conn, parsed_id)
    with conn.cursor() as cur:
        program_ids = [parsed_id]
        if include_descendants:
            program_ids.extend(_descendant_program_ids(cur, parsed_id))
        cur.execute(
            """
            SELECT id, tenant_id, program_id, workflow_id, kind, ref, state, metadata,
                   created_at, updated_at
            FROM program_external_refs
            WHERE program_id = ANY(%s::uuid[])
            ORDER BY created_at
            """,
            (program_ids,),
        )
        rows = list(cur.fetchall())
    refs = [
        {
            "id": str(row.get("id")),
            "program_id": str(row.get("program_id")),
            "workflow_id": str(row.get("workflow_id") or "") or None,
            "kind": row.get("kind"),
            "ref": row.get("ref"),
            "state": row.get("state"),
            "metadata": row.get("metadata") or {},
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
        }
        for row in rows
    ]
    return {"program_id": parsed_id, "refs": refs}


VALID_RELEASE_STATUSES = {"pending", "in_progress", "succeeded", "failed"}
RELEASE_COLUMNS = (
    "id, tenant_id, program_id, workflow_id, repo, sha, environment, deployment_ref, status, "
    "evidence_url, promoted_by, decision_id, rollback_sha, is_rollback, created_at, completed_at"
)


def _release_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    for key in ("id", "program_id", "workflow_id", "decision_id"):
        if out.get(key) is not None:
            out[key] = str(out[key])
    return out


def record_program_release(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Insert a release row, or update status/evidence when ``id`` is given."""
    program = _load_program_row(conn, program_id)
    parsed_id = parse_uuid(program_id)
    tenant_id = str(program.get("tenant_id") or "default")
    status = str(body.get("status") or "pending").strip()
    if status not in VALID_RELEASE_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(VALID_RELEASE_STATUSES))}")
    actor = str(body.get("actor") or "orchestrator")
    terminal = status in {"succeeded", "failed"}
    decision_raw = str(body.get("decision_id") or "").strip()
    decision_id = parse_uuid(decision_raw) if decision_raw else None
    release_raw = str(body.get("id") or "").strip()
    with conn.cursor() as cur:
        if release_raw:
            release_id = parse_uuid(release_raw)
            cur.execute(
                """
                UPDATE program_releases
                SET status = %s,
                    deployment_ref = COALESCE(%s, deployment_ref),
                    evidence_url = COALESCE(%s, evidence_url),
                    promoted_by = COALESCE(%s, promoted_by),
                    decision_id = COALESCE(%s, decision_id),
                    completed_at = CASE WHEN %s THEN now() ELSE completed_at END
                WHERE id = %s AND program_id = %s
                """,
                (
                    status,
                    body.get("deployment_ref"),
                    body.get("evidence_url"),
                    body.get("promoted_by"),
                    decision_id,
                    terminal,
                    release_id,
                    parsed_id,
                ),
            )
            if getattr(cur, "rowcount", 1) == 0:
                raise ValueError("release not found for program")
        else:
            repo = str(body.get("repo") or "").strip()
            sha = str(body.get("sha") or "").strip()
            environment = str(body.get("environment") or "").strip()
            if not repo or not sha or not environment:
                raise ValueError("repo, sha and environment are required")
            workflow_raw = str(body.get("workflow_id") or "").strip()
            release_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO program_releases
                  (id, tenant_id, program_id, workflow_id, repo, sha, environment, deployment_ref,
                   status, evidence_url, promoted_by, decision_id, rollback_sha, is_rollback,
                   completed_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s THEN now() ELSE NULL END)
                """,
                (
                    release_id,
                    tenant_id,
                    parsed_id,
                    parse_uuid(workflow_raw) if workflow_raw else None,
                    repo.lower(),
                    sha,
                    environment,
                    body.get("deployment_ref"),
                    status,
                    body.get("evidence_url"),
                    body.get("promoted_by"),
                    decision_id,
                    body.get("rollback_sha"),
                    bool(body.get("is_rollback")),
                    terminal,
                ),
            )
        audit_workflow_event(
            cur,
            "program_release_recorded",
            f"Release {status}",
            {
                "program_id": parsed_id,
                "release_id": release_id,
                "status": status,
                "environment": body.get("environment"),
                "sha": body.get("sha"),
                "promoted_by": body.get("promoted_by"),
            },
            actor,
        )
    conn.commit()
    return {"id": release_id, "program_id": parsed_id, "status": status}


def list_program_releases(conn, program_id: str, *, include_descendants: bool = False) -> dict[str, Any]:
    parsed_id = parse_uuid(program_id)
    _load_program_row(conn, parsed_id)
    with conn.cursor() as cur:
        program_ids = [parsed_id]
        if include_descendants:
            program_ids.extend(_descendant_program_ids(cur, parsed_id))
        cur.execute(
            f"SELECT {RELEASE_COLUMNS} FROM program_releases WHERE program_id = ANY(%s::uuid[]) ORDER BY created_at",
            (program_ids,),
        )
        rows = list(cur.fetchall())
    return {"program_id": parsed_id, "releases": [_release_row(row) for row in rows]}


EVIDENCE_AUDIT_LIMIT = 5000
_EVIDENCE_REF_ROWS = (
    "id, tenant_id, program_id, workflow_id, kind, ref, state, metadata, created_at, updated_at"
)


def get_program_evidence(conn, program_id: str, *, audit_limit: int = EVIDENCE_AUDIT_LIMIT) -> dict[str, Any]:
    """Collect the governance record for a program tree (the program plus all descendants)."""
    parsed_id = parse_uuid(program_id)
    program = _load_program_row(conn, parsed_id)
    with conn.cursor() as cur:
        child_ids = _descendant_program_ids(cur, parsed_id)
        tree_ids = [parsed_id, *child_ids]
        children: list[dict[str, Any]] = []
        if child_ids:
            cur.execute(
                "SELECT * FROM delivery_programs WHERE id = ANY(%s::uuid[]) ORDER BY created_at",
                (child_ids,),
            )
            children = list(cur.fetchall())
        cur.execute(
            """
            SELECT id, tenant_id, program_id, workflow_type, definition_version, status,
                   compensation_status, parent_workflow_id, created_at, updated_at
            FROM workflows
            WHERE program_id = ANY(%s::uuid[])
            ORDER BY created_at
            """,
            (tree_ids,),
        )
        workflows = list(cur.fetchall())
        workflow_ids = [str(row.get("id")) for row in workflows]
        task_ids: list[str] = []
        if workflow_ids:
            cur.execute(
                "SELECT id FROM workflow_tasks WHERE workflow_id = ANY(%s::uuid[])",
                (workflow_ids,),
            )
            task_ids = [str(row.get("id")) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT id, tenant_id, program_id, workflow_id, task_id, kind, decision,
                   actor_id, actor_email, actor_roles, reason, details, expires_at, created_at
            FROM workflow_decisions
            WHERE program_id = ANY(%s::uuid[]) OR workflow_id = ANY(%s::uuid[])
            ORDER BY created_at, id
            """,
            (tree_ids, workflow_ids),
        )
        decisions = list(cur.fetchall())
        cur.execute(
            f"""
            SELECT {_EVIDENCE_REF_ROWS}
            FROM program_external_refs
            WHERE program_id = ANY(%s::uuid[])
            ORDER BY created_at, id
            """,
            (tree_ids,),
        )
        refs = list(cur.fetchall())
        cur.execute(
            f"""
            SELECT {RELEASE_COLUMNS}
            FROM program_releases
            WHERE program_id = ANY(%s::uuid[])
            ORDER BY created_at, id
            """,
            (tree_ids,),
        )
        releases = [_release_row(row) for row in cur.fetchall()]
        cur.execute(
            """
            SELECT id, session_id, agent_id, action_type, summary, details, occurred_at
            FROM session_audit_log
            WHERE details->>'program_id' = ANY(%s::text[])
               OR details->>'workflow_id' = ANY(%s::text[])
               OR details->>'task_id' = ANY(%s::text[])
            ORDER BY occurred_at, id
            LIMIT %s
            """,
            (tree_ids, workflow_ids, task_ids, audit_limit + 1),
        )
        audit = list(cur.fetchall())
    audit_truncated = len(audit) > audit_limit
    metadata = program.get("metadata") if isinstance(program.get("metadata"), dict) else {}
    artifacts = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), dict) else {}
    compensation = artifacts.get("compensation") if isinstance(artifacts.get("compensation"), dict) else {}
    return {
        "program": program,
        "children": children,
        "workflows": workflows,
        "decisions": decisions,
        "pull_requests": [ref for ref in refs if ref.get("kind") in {"pull_request", "branch"}],
        "jira": [ref for ref in refs if ref.get("kind") == "jira_issue"],
        "other_refs": [
            ref for ref in refs if ref.get("kind") not in {"pull_request", "branch", "jira_issue"}
        ],
        "releases": releases,
        "compensation": compensation,
        "audit": audit[:audit_limit],
        "audit_truncated": audit_truncated,
    }


def record_program_evidence_export(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    parsed_id = parse_uuid(program_id)
    _load_program_row(conn, parsed_id)
    sha256 = str(body.get("sha256") or "").strip().lower()
    if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
        raise ValueError("sha256 must be a 64-character hex digest")
    actor = str(body.get("actor") or "admin")
    with conn.cursor() as cur:
        audit_workflow_event(
            cur,
            "program_evidence_exported",
            "Program evidence exported",
            {
                "program_id": parsed_id,
                "sha256": sha256,
                "generated_at": body.get("generated_at"),
                "tenant_id": body.get("tenant_id"),
            },
            actor,
        )
    conn.commit()
    return {"program_id": parsed_id, "sha256": sha256}


def last_successful_release(
    conn,
    *,
    tenant_id: str,
    repo: str,
    environment: str,
    exclude_sha: str | None = None,
) -> dict[str, Any]:
    """Latest succeeded release for ``repo``/``environment``; the rollback target."""
    if not repo or not environment:
        raise ValueError("repo and environment are required")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {RELEASE_COLUMNS}
            FROM program_releases
            WHERE tenant_id = %s AND repo = %s AND environment = %s AND status = 'succeeded'
              AND (%s::text IS NULL OR sha <> %s)
            ORDER BY completed_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """,
            (tenant_id, repo.lower(), environment, exclude_sha, exclude_sha),
        )
        row = cur.fetchone()
    return {"release": _release_row(row) if row else None}


def find_program_external_refs(conn, *, kind: str, ref: str, tenant_id: str | None = None) -> dict[str, Any]:
    """Find the programs that recorded ``ref`` (e.g. which program owns a PR URL)."""
    kind = str(kind or "").strip()
    ref = str(ref or "").strip()
    if not kind or not ref:
        raise ValueError("kind and ref are required")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.tenant_id, r.program_id, r.workflow_id, r.kind, r.ref, r.state,
                   r.metadata, p.root_program_id, p.parent_program_id, p.metadata AS program_metadata
            FROM program_external_refs r
            JOIN delivery_programs p ON p.id = r.program_id
            WHERE r.kind = %s AND r.ref = %s AND (%s::text IS NULL OR r.tenant_id = %s)
            ORDER BY r.created_at
            """,
            (kind, ref, tenant_id, tenant_id),
        )
        rows = list(cur.fetchall())
    return {
        "refs": [
            {
                "id": str(row.get("id")),
                "tenant_id": row.get("tenant_id"),
                "program_id": str(row.get("program_id")),
                "root_program_id": str(row.get("root_program_id") or "") or None,
                "parent_program_id": str(row.get("parent_program_id") or "") or None,
                "workflow_id": str(row.get("workflow_id") or "") or None,
                "kind": row.get("kind"),
                "ref": row.get("ref"),
                "state": row.get("state"),
                "metadata": row.get("metadata") or {},
                "program_metadata": row.get("program_metadata") or {},
            }
            for row in rows
        ]
    }


def _child_rollup(conn, children: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize child programs for the parent status view."""
    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for child in children:
        status = str(child.get("status") or "running")
        counts[status] = counts.get(status, 0) + 1
        item = {
            "id": child.get("id"),
            "summary": child.get("summary"),
            "program_type": child.get("program_type"),
            "current_phase": child.get("current_phase"),
            "status": status,
        }
        try:
            child_status = get_delivery_program_status(conn, str(child.get("id")), include_children=False)
        except Exception:
            child_status = {}
        if child_status:
            item["status"] = child_status.get("status") or status
            for key in ("waiting_on", "last_error", "active_step", "active_workflow_id"):
                if child_status.get(key):
                    item[key] = child_status.get(key)
        artifacts = child.get("artifacts") if isinstance(child.get("artifacts"), dict) else {}
        pr_urls = artifacts.get("pr_urls") if isinstance(artifacts.get("pr_urls"), list) else []
        if artifacts.get("pr_url") and artifacts.get("pr_url") not in pr_urls:
            pr_urls = [*pr_urls, artifacts.get("pr_url")]
        if pr_urls:
            item["pr_urls"] = pr_urls
        items.append(item)
    active = sum(counts.get(s, 0) for s in ("running", "blocked"))
    return {"children": items, "child_status_counts": counts, "active_child_count": active}


def get_delivery_program(conn, program_id: str) -> dict[str, Any]:
    parsed_id = parse_uuid(program_id)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM delivery_programs WHERE id = %s", (parsed_id,))
        program = cur.fetchone()
        if not program:
            raise ValueError("program not found")
        cur.execute(
            """
            SELECT *
            FROM workflows
            WHERE program_id = %s
            ORDER BY created_at
            """,
            (parsed_id,),
        )
        workflows = list(cur.fetchall())
    return {"program": program, "workflows": workflows}


def list_delivery_programs(
    conn,
    *,
    tenant_id: str = "default",
    status: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    roots_only: bool = False,
    phase: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """List delivery programs for a tenant, newest updated first."""
    tenant = str(tenant_id or "default").strip() or "default"
    try:
        limit_n = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        limit_n = 50
    try:
        offset_n = max(0, int(offset))
    except (TypeError, ValueError):
        offset_n = 0
    status_filter = str(status or "").strip() or None
    allowed_status = {"running", "blocked", "failed", "succeeded", "cancelled"}
    if status_filter and status_filter not in allowed_status:
        raise ValueError(f"status must be one of: {', '.join(sorted(allowed_status))}")

    clauses = ["tenant_id = %s"]
    params: list[Any] = [tenant]
    if status_filter:
        clauses.append("status = %s")
        params.append(status_filter)
    project_filter = str(project_id or "").strip() or None
    if project_filter:
        clauses.append("project_id = %s")
        params.append(project_filter)
    if roots_only:
        clauses.append("parent_program_id IS NULL")
    phase_filter = str(phase or "").strip() or None
    if phase_filter:
        clauses.append("current_phase = %s")
        params.append(phase_filter)
    owner_filter = str(owner or "").strip() or None
    if owner_filter:
        clauses.append("requester_actor_id = %s")
        params.append(owner_filter)
    where = " AND ".join(clauses)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT COUNT(*) AS count FROM delivery_programs WHERE {where}",
            tuple(params),
        )
        count_row = cur.fetchone() or {}
        total = int(count_row.get("count") or 0)
        cur.execute(
            f"""
            SELECT id, tenant_id, summary, display_name, binding_key, source_channel,
                   requester_actor_id, entry_phase, current_phase, status, metadata,
                   program_type, program_definition_version, program_definition_revision,
                   project_id, project_id_source, excluded_from_autoshare,
                   parent_program_id, root_program_id,
                   (SELECT COUNT(*) FROM delivery_programs child
                     WHERE child.parent_program_id = delivery_programs.id) AS child_count,
                   created_at, updated_at
            FROM delivery_programs
            WHERE {where}
            ORDER BY updated_at DESC NULLS LAST, created_at DESC
            LIMIT %s OFFSET %s
            """,
            tuple(params + [limit_n, offset_n]),
        )
        rows = list(cur.fetchall())

    programs: list[dict[str, Any]] = []
    for row in rows:
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except json.JSONDecodeError:
                metadata = {}
        programs.append(
            {
                "id": str(row.get("id")),
                "tenant_id": row.get("tenant_id"),
                "summary": row.get("summary"),
                "display_name": row.get("display_name"),
                "binding_key": row.get("binding_key"),
                "source_channel": row.get("source_channel"),
                "requester_actor_id": row.get("requester_actor_id"),
                "entry_phase": row.get("entry_phase"),
                "current_phase": row.get("current_phase"),
                "status": row.get("status"),
                "program_type": row.get("program_type"),
                "program_definition_version": row.get("program_definition_version"),
                "program_definition_revision": row.get("program_definition_revision"),
                "project_id": row.get("project_id"),
                "project_id_source": row.get("project_id_source"),
                "excluded_from_autoshare": bool(row.get("excluded_from_autoshare")),
                "parent_program_id": str(row.get("parent_program_id") or "") or None,
                "root_program_id": str(row.get("root_program_id") or "") or None,
                "child_count": int(row.get("child_count") or 0),
                "artifacts": _program_metadata_artifacts(metadata),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
        )
    return {
        "programs": programs,
        "total": total,
        "limit": limit_n,
        "offset": offset_n,
    }


def patch_delivery_program(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    program = _load_program_row(conn, program_id)
    tenant_id = str(body.get("tenant_id") or program.get("tenant_id") or "default").strip() or "default"
    if str(program.get("tenant_id") or "default") != tenant_id:
        raise ValueError("cross-tenant program update rejected")

    assignments: list[str] = []
    params: list[Any] = []
    if "project_id" in body or "project_id_source" in body:
        current_project = body["project_id"] if "project_id" in body else program.get("project_id")
        current_source = (
            body["project_id_source"] if "project_id_source" in body else program.get("project_id_source")
        )
        project_id, project_id_source = _normalize_project_fields(current_project, current_source)
        assignments.extend(["project_id = %s", "project_id_source = %s"])
        params.extend([project_id, project_id_source])
    if "excluded_from_autoshare" in body:
        assignments.append("excluded_from_autoshare = %s")
        params.append(bool(body.get("excluded_from_autoshare")))
    adopted_parent: tuple[str | None, str | None] = (None, None)
    if str(body.get("parent_program_id") or "").strip():
        if str(program.get("parent_program_id") or "").strip():
            raise ValueError("program already has a parent")
        adopted_parent = _resolve_program_parent(
            conn,
            body.get("parent_program_id"),
            tenant_id=tenant_id,
            program_id=str(program.get("id") or parse_uuid(program_id)),
        )
        assignments.extend(["parent_program_id = %s", "root_program_id = %s"])
        params.extend(list(adopted_parent))
    if not assignments:
        raise ValueError("no program fields to patch")

    actor = str(body.get("actor") or "orchestrator")
    parsed_id = parse_uuid(program_id)
    params.append(parsed_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE delivery_programs
            SET {", ".join(assignments)}, updated_at = now()
            WHERE id = %s
            """,
            tuple(params),
        )
        audit_workflow_event(
            cur,
            "delivery_program_patched",
            "Delivery program project fields updated",
            {"program_id": parsed_id, "fields": [item.split(" = ")[0] for item in assignments]},
            actor,
        )
        if adopted_parent[0]:
            cur.execute(
                "UPDATE delivery_programs SET root_program_id = %s WHERE root_program_id = %s",
                (adopted_parent[1], parsed_id),
            )
            audit_workflow_event(
                cur,
                "program_child_attached",
                "Program adopted a parent",
                {
                    "program_id": parsed_id,
                    "parent_program_id": adopted_parent[0],
                    "root_program_id": adopted_parent[1],
                },
                actor,
            )
    conn.commit()
    return get_delivery_program(conn, program_id)


def create_program_link(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    from_id = parse_uuid(program_id)
    to_id = parse_uuid(body.get("to_program_id"))
    if from_id == to_id:
        raise ValueError("cannot link a program to itself")
    source = _load_program_row(conn, from_id)
    target = _load_program_row(conn, to_id)
    source_tenant = str(source.get("tenant_id") or "default")
    target_tenant = str(target.get("tenant_id") or "default")
    tenant_id = str(body.get("tenant_id") or source_tenant).strip() or "default"
    if source_tenant != tenant_id or target_tenant != tenant_id or source_tenant != target_tenant:
        raise ValueError("cross-tenant program link rejected")
    actor = str(body.get("actor") or "orchestrator")
    link_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO program_links
              (id, tenant_id, from_program_id, to_program_id, created_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (from_program_id, to_program_id) DO NOTHING
            """,
            (link_id, tenant_id, from_id, to_id, actor),
        )
        audit_workflow_event(
            cur,
            "program_link_created",
            "Program link created",
            {"from_program_id": from_id, "to_program_id": to_id},
            actor,
        )
    conn.commit()
    return {"from_program_id": from_id, "to_program_id": to_id, "tenant_id": tenant_id}


def delete_program_link(conn, program_id: str, to_program_id: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    from_id = parse_uuid(program_id)
    to_id = parse_uuid(to_program_id)
    source = _load_program_row(conn, from_id)
    tenant_id = str((body or {}).get("tenant_id") or source.get("tenant_id") or "default").strip() or "default"
    if str(source.get("tenant_id") or "default") != tenant_id:
        raise ValueError("cross-tenant program unlink rejected")
    actor = str((body or {}).get("actor") or "orchestrator")
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM program_links
            WHERE from_program_id = %s AND to_program_id = %s AND tenant_id = %s
            """,
            (from_id, to_id, tenant_id),
        )
        audit_workflow_event(
            cur,
            "program_link_deleted",
            "Program link removed",
            {"from_program_id": from_id, "to_program_id": to_id},
            actor,
        )
    conn.commit()
    return {"from_program_id": from_id, "to_program_id": to_id, "deleted": True}


def list_program_links(conn, program_id: str) -> dict[str, Any]:
    parsed_id = parse_uuid(program_id)
    _load_program_row(conn, parsed_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, tenant_id, from_program_id, to_program_id, created_by, created_at
            FROM program_links
            WHERE from_program_id = %s OR to_program_id = %s
            ORDER BY created_at
            """,
            (parsed_id, parsed_id),
        )
        rows = list(cur.fetchall())
    outbound = []
    inbound = []
    for row in rows:
        item = {
            "id": str(row.get("id")),
            "tenant_id": row.get("tenant_id"),
            "from_program_id": str(row.get("from_program_id")),
            "to_program_id": str(row.get("to_program_id")),
            "created_by": row.get("created_by"),
            "created_at": row.get("created_at"),
        }
        if str(row.get("from_program_id")) == parsed_id:
            outbound.append(item)
        if str(row.get("to_program_id")) == parsed_id:
            inbound.append(item)
    return {"program_id": parsed_id, "outbound": outbound, "inbound": inbound}


def upsert_program_briefs(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    source = _load_program_row(conn, program_id)
    parsed_id = parse_uuid(program_id)
    tenant_id = str(body.get("tenant_id") or source.get("tenant_id") or "default").strip() or "default"
    if str(source.get("tenant_id") or "default") != tenant_id:
        raise ValueError("cross-tenant brief update rejected")
    entries = body.get("entries") or body.get("briefs") or []
    if not isinstance(entries, list):
        raise ValueError("entries must be an array")
    actor = str(body.get("actor") or "orchestrator")
    project_id = body.get("project_id")
    if project_id is None:
        project_id = source.get("project_id")
    project_id = str(project_id).strip() if project_id else None
    written: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        for raw in entries:
            if not isinstance(raw, dict):
                raise ValueError("brief entries must be objects")
            kind = str(raw.get("kind") or "").strip()
            if kind not in VALID_BRIEF_KINDS:
                raise ValueError(f"kind must be one of: {', '.join(sorted(VALID_BRIEF_KINDS))}")
            topic = str(raw.get("topic") or "").strip()
            content = str(raw.get("content") or "").strip()
            phase = str(raw.get("phase") or "").strip()
            if not topic or not content or not phase:
                raise ValueError("phase, topic, and content are required")
            metadata = normalize_metadata(raw.get("metadata"))
            artifact_ref = raw.get("artifact_ref")
            artifact_ref = str(artifact_ref).strip() if artifact_ref else None
            brief_id = str(uuid.uuid4())
            cur.execute(
                """
                INSERT INTO program_briefs
                  (id, tenant_id, project_id, source_program_id, phase, kind, topic,
                   content, artifact_ref, metadata)
                VALUES
                  (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (source_program_id, phase, kind, topic)
                DO UPDATE SET
                  content = EXCLUDED.content,
                  artifact_ref = EXCLUDED.artifact_ref,
                  metadata = EXCLUDED.metadata,
                  project_id = EXCLUDED.project_id,
                  tenant_id = EXCLUDED.tenant_id,
                  updated_at = now()
                RETURNING id
                """,
                (
                    brief_id,
                    tenant_id,
                    project_id,
                    parsed_id,
                    phase,
                    kind,
                    topic,
                    content,
                    artifact_ref,
                    json.dumps(metadata, default=str),
                ),
            )
            row = cur.fetchone() or {}
            written.append(
                {
                    "id": str(row.get("id") or brief_id),
                    "phase": phase,
                    "kind": kind,
                    "topic": topic,
                    "content": content,
                    "artifact_ref": artifact_ref,
                }
            )
        audit_workflow_event(
            cur,
            "program_briefs_upserted",
            "Program briefs upserted",
            {"program_id": parsed_id, "count": len(written)},
            actor,
        )
    conn.commit()
    return {"program_id": parsed_id, "entries": written}


def promote_department_memory(
    conn,
    program_id: str,
    body: dict[str, Any],
    embed: Callable[[str], str] = generate_embedding,
) -> dict[str, Any]:
    """Copy one approved brief into department memory. Does not call remember."""
    source = _load_program_row(conn, program_id)
    parsed_id = parse_uuid(program_id)
    tenant_id = str(body.get("tenant_id") or source.get("tenant_id") or "default").strip() or "default"
    if str(source.get("tenant_id") or "default") != tenant_id:
        raise ValueError("program not found")
    department = str(source.get("department") or "").strip().lower()
    if not DEPARTMENT_NAME_RE.fullmatch(department):
        raise ValueError("department mismatch")
    brief_id = parse_uuid(body.get("brief_id"))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, phase, kind, topic, content
            FROM program_briefs
            WHERE id = %s AND source_program_id = %s AND tenant_id = %s
            """,
            (brief_id, parsed_id, tenant_id),
        )
        brief = cur.fetchone()
        if not brief:
            raise ValueError("brief not found")
        kind = str(brief.get("kind") or "")
        mem_type = BRIEF_KIND_MEMORY_TYPE.get(kind)
        if mem_type is None:
            raise ValueError("brief not found")
        content = str(brief.get("content") or "").strip()
        if not content:
            raise ValueError("brief not found")
        scope = f"department:{department}"
        metadata = {
            "brief_id": brief_id,
            "source_program_id": parsed_id,
            "phase": str(brief.get("phase") or ""),
            "kind": kind,
            "topic": str(brief.get("topic") or ""),
        }
        embedding = normalize_embedding(embed(content))
        cur.execute(
            """
            SELECT id FROM memories
            WHERE tenant_id = %s AND scope = %s AND metadata->>'brief_id' = %s
            LIMIT 1
            """,
            (tenant_id, scope, brief_id),
        )
        existing = cur.fetchone()
        actor = str(body.get("actor") or "orchestrator")
        if existing:
            mem_id = str(existing["id"])
            action = "updated"
            cur.execute(
                """
                UPDATE memories
                SET type = %s,
                    content = %s,
                    embedding = %s::vector,
                    metadata = %s::jsonb,
                    source = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (mem_type, content, embedding, json.dumps(metadata), "department-promote", mem_id),
            )
        else:
            mem_id = str(uuid.uuid4())
            action = "created"
            cur.execute(
                """
                INSERT INTO memories
                  (id, type, content, embedding, scope, source, confidence,
                   created_at, updated_at, tags, metadata, tenant_id)
                VALUES
                  (%s, %s, %s, %s::vector, %s, %s, %s,
                   now(), now(), %s, %s::jsonb, %s)
                """,
                (
                    mem_id,
                    mem_type,
                    content,
                    embedding,
                    scope,
                    "department-promote",
                    1.0,
                    ["department", f"program:{parsed_id}", kind],
                    json.dumps(metadata),
                    tenant_id,
                ),
            )
        cur.execute(
            """
            INSERT INTO memory_audit
              (id, memory_id, action, actor, reason, occurred_at)
            VALUES (uuid_generate_v4(), %s, %s, %s, %s, now())
            """,
            (mem_id, action, actor, "department memory promoted"),
        )
        audit_workflow_event(
            cur,
            "department_memory_promoted",
            "Approved brief promoted to department memory",
            {
                "program_id": parsed_id,
                "brief_id": brief_id,
                "scope": scope,
                "memory_id": mem_id,
            },
            actor,
        )
    conn.commit()
    return {"id": mem_id, "action": action, "scope": scope}


def list_program_briefs(
    conn,
    *,
    program_id: str | None = None,
    tenant_id: str = "default",
    project_id: str | None = None,
    source_program_id: str | None = None,
) -> dict[str, Any]:
    tenant = str(tenant_id or "default").strip() or "default"
    if program_id and not source_program_id:
        program = _load_program_row(conn, program_id)
        tenant = str(program.get("tenant_id") or tenant)
        source_program_id = program_id
    clauses = ["tenant_id = %s"]
    params: list[Any] = [tenant]
    source = source_program_id or program_id
    if source:
        clauses.append("source_program_id = %s")
        params.append(parse_uuid(source))
    if project_id:
        clauses.append("project_id = %s")
        params.append(str(project_id).strip())
    where = " AND ".join(clauses)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT id, tenant_id, project_id, source_program_id, phase, kind, topic,
                   content, artifact_ref, metadata, created_at, updated_at
            FROM program_briefs
            WHERE {where}
            ORDER BY updated_at DESC, created_at DESC
            """,
            tuple(params),
        )
        rows = list(cur.fetchall())
    entries = []
    for row in rows:
        entries.append(
            {
                "id": str(row.get("id")),
                "tenant_id": row.get("tenant_id"),
                "project_id": row.get("project_id"),
                "source_program_id": str(row.get("source_program_id")),
                "phase": row.get("phase"),
                "kind": row.get("kind"),
                "topic": row.get("topic"),
                "content": row.get("content"),
                "artifact_ref": row.get("artifact_ref"),
                "metadata": row.get("metadata") or {},
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
        )
    return {"entries": entries}


def find_program_by_binding(conn, binding_key: str, *, tenant_id: str = "default") -> dict[str, Any]:
    ref = str(binding_key or "").strip()
    if not ref:
        raise ValueError("binding_key is required")
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM delivery_programs
            WHERE tenant_id = %s
              AND binding_key = %s
              AND status IN ('running', 'blocked')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (tenant_id, ref),
        )
        program = cur.fetchone()
    if not program:
        raise ValueError("program not found")
    return {"program": program}


def _phase_state_from_workflow(workflow: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    wf_status = str(workflow.get("status") or "running")
    phase_info: dict[str, Any] = {
        "workflow_id": str(workflow.get("id")),
        "workflow_type": workflow.get("workflow_type"),
    }
    if wf_status in {"succeeded", "superseded"}:
        phase_info["state"] = "succeeded"
        return phase_info
    if wf_status in {"failed"}:
        phase_info["state"] = "failed"
        return phase_info
    if wf_status in {"cancelled"}:
        phase_info["state"] = "cancelled"
        return phase_info

    by_id = {str(task.get("id")): task for task in tasks if task.get("id")}
    pending_approvals = []
    for task in tasks:
        if not task.get("approval_required"):
            continue
        if task.get("approval_status") != "pending" or task.get("status") != "pending":
            continue
        deps = [str(dep_id) for dep_id in (task.get("depends_on") or [])]
        if all(str(by_id.get(dep_id, {}).get("status") or "").lower() == "succeeded" for dep_id in deps):
            pending_approvals.append(task)

    running = [task for task in tasks if task.get("status") == "running"]
    active_step = None
    if running:
        active_step = running[0].get("step_name")
    elif pending_approvals:
        active_step = pending_approvals[0].get("step_name")
    else:
        pending_tasks = [task for task in tasks if task.get("status") == "pending"]
        if pending_tasks:
            active_step = pending_tasks[0].get("step_name")

    if pending_approvals:
        phase_info["state"] = "blocked"
        phase_info["pending_approvals"] = pending_approvals
    elif wf_status == "blocked" or any(task.get("status") == "blocked" for task in tasks):
        phase_info["state"] = "blocked"
    else:
        phase_info["state"] = "running"
    if active_step:
        phase_info["active_step"] = active_step
    return phase_info


def get_delivery_program_status(
    conn,
    program_id: str,
    *,
    include_children: bool = True,
) -> dict[str, Any]:
    payload = get_delivery_program(conn, program_id)
    program = payload["program"]
    workflows = payload["workflows"]
    metadata = program.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)

    topology = _program_topology(program)
    phase_ids = _topology_node_ids(topology)
    phases: dict[str, Any] = {}
    active_workflow_id = None
    for phase in phase_ids:
        member = next((wf for wf in workflows if wf.get("program_phase") == phase), None)
        if member is None:
            phases[phase] = {"state": "not_started"}
            continue
        wf_id = str(member.get("id"))
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM workflow_tasks
                WHERE workflow_id = %s
                ORDER BY created_at
                """,
                (wf_id,),
            )
            tasks = list(cur.fetchall())
        phase_info = _phase_state_from_workflow(member, tasks)
        phases[phase] = phase_info
        if phase == program.get("current_phase") and phase_info.get("state") in {
            "running",
            "blocked",
        }:
            active_workflow_id = wf_id

    current_phase = str(program.get("current_phase") or "")
    phase_order = _visible_phase_order(
        topology,
        current_phase=current_phase,
        phases=phases,
    )

    terminal_nodes = {
        str(node.get("id") or "").strip()
        for node in (topology.get("nodes") or [])
        if isinstance(node, dict) and node.get("terminal")
    }
    if not terminal_nodes and "implement" in phase_ids:
        terminal_nodes = {"implement"}
    elif not terminal_nodes and "deploy" in phase_ids:
        terminal_nodes = {"deploy"}

    stored_status = str(program.get("status") or "running")
    if stored_status in {"cancelled", "succeeded", "failed"}:
        program_status = stored_status
    elif any(phases.get(p, {}).get("state") == "failed" for p in phase_ids):
        program_status = "failed"
    elif any(phases.get(p, {}).get("state") == "succeeded" for p in terminal_nodes):
        program_status = "succeeded"
    elif phases.get(current_phase, {}).get("state") == "blocked":
        program_status = "blocked"
    else:
        program_status = stored_status

    artifacts = _program_metadata_artifacts(metadata)
    operability: dict[str, Any] = {}
    if active_workflow_id:
        member = next((wf for wf in workflows if str(wf.get("id")) == active_workflow_id), None)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM workflow_tasks
                WHERE workflow_id = %s
                ORDER BY created_at
                """,
                (active_workflow_id,),
            )
            active_tasks = list(cur.fetchall())
        try:
            from ts_harness.workflows.operability import build_operability_status

            operability = build_operability_status(
                workflow=dict(member or {}),
                tasks=active_tasks,
                artifacts=artifacts if isinstance(artifacts, dict) else {},
            )
        except Exception:
            operability = {}
    resolved_workflow_versions = _parse_json_object(program.get("resolved_workflow_versions")) or {}
    for phase_id, phase_info in phases.items():
        pin = resolved_workflow_versions.get(phase_id)
        if not isinstance(pin, dict):
            continue
        if pin.get("workflow_type") and not phase_info.get("workflow_type"):
            phase_info["workflow_type"] = pin.get("workflow_type")
        if pin.get("definition_version"):
            phase_info["workflow_definition_version"] = pin.get("definition_version")

    links = list_program_links(conn, str(program.get("id")))
    parent_id = str(program.get("parent_program_id") or "") or None
    hierarchy: dict[str, Any] = {
        "parent_program_id": parent_id,
        "root_program_id": str(program.get("root_program_id") or "") or None,
        "owner_program": parent_id or "none",
    }
    if include_children:
        children = list_program_children(conn, str(program.get("id"))).get("children") or []
        rollup = _child_rollup(conn, children)
        hierarchy.update(rollup)
        hierarchy["external_refs"] = list_program_external_refs(
            conn, str(program.get("id")), include_descendants=True
        ).get("refs") or []
        warnings: list[str] = []
        for ref in hierarchy["external_refs"]:
            if ref.get("state") != "open":
                continue
            for warning in (ref.get("metadata") or {}).get("merge_readiness_warnings") or []:
                if warning not in warnings:
                    warnings.append(str(warning))
        hierarchy["warnings"] = warnings
        if program_status == "succeeded" and rollup.get("active_child_count"):
            program_status = "running"
    return {
        "program_id": str(program.get("id")),
        **hierarchy,
        "summary": program.get("summary"),
        "current_phase": program.get("current_phase"),
        "entry_phase": program.get("entry_phase"),
        "status": program_status,
        "project_id": program.get("project_id"),
        "project_id_source": program.get("project_id_source"),
        "excluded_from_autoshare": bool(program.get("excluded_from_autoshare")),
        "outbound_link_count": len(links.get("outbound") or []),
        "inbound_link_count": len(links.get("inbound") or []),
        "links": links,
        "program_type": program.get("program_type") or topology.get("program_type"),
        "program_definition_version": program.get("program_definition_version")
        or topology.get("definition_version"),
        "program_definition_revision": program.get("program_definition_revision"),
        "phase_order": phase_order,
        "phases": phases,
        "topology": {
            "program_type": topology.get("program_type"),
            "definition_version": topology.get("definition_version"),
            "display_name": topology.get("display_name"),
            "startable_nodes": list(topology.get("startable_nodes") or []),
            "nodes": list(topology.get("nodes") or []),
            "edges": list(topology.get("edges") or []),
        },
        "resolved_workflow_versions": resolved_workflow_versions,
        "active_workflow_id": active_workflow_id,
        "artifacts": artifacts,
        **operability,
    }


def advance_delivery_program(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    parsed_id = parse_uuid(program_id)
    target_phase = str(body.get("target_phase") or body.get("current_phase") or "").strip()
    actor = str(body.get("actor", "orchestrator"))

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM delivery_programs WHERE id = %s", (parsed_id,))
        program = cur.fetchone()
        if not program:
            raise ValueError("program not found")
        topology = _program_topology(program)
        current = str(program.get("current_phase") or "")
        successors = _topology_next_phases(topology, current)
        if target_phase:
            target_phase = _validate_phase_id(target_phase, topology, field="target_phase")
            # Same-phase reentry is required for work-order loops that stay on
            # implement. Python resolve_target_node already allows this; the
            # gateway graph check must not reject it.
            if (
                current
                and successors
                and target_phase not in successors
                and target_phase != current
            ):
                raise ValueError(
                    f"target_phase {target_phase} is not reachable from {current}; "
                    f"allowed: {', '.join(successors)}"
                )
        else:
            if len(successors) == 1:
                target_phase = successors[0]
            elif len(successors) > 1:
                raise ValueError(f"explicit target_phase required after {current}")
            else:
                raise ValueError("no next phase available")
        cur.execute(
            """
            UPDATE delivery_programs
            SET current_phase = %s,
                status = 'running'
            WHERE id = %s
            """,
            (target_phase, parsed_id),
        )
        audit_workflow_event(
            cur,
            "delivery_program_advanced",
            f"Delivery program advanced to {target_phase}",
            {
                "program_id": parsed_id,
                "target_phase": target_phase,
                "program_type": program.get("program_type"),
                "program_definition_version": program.get("program_definition_version"),
                "program_definition_revision": program.get("program_definition_revision"),
            },
            actor,
        )
        decision_id = record_workflow_decision(
            cur,
            {
                "kind": "track_choice" if len(successors) > 1 else "advance",
                "decision": target_phase,
                "tenant_id": program.get("tenant_id") or "default",
                "program_id": parsed_id,
                "actor_id": actor,
                "actor_email": body.get("actor_email"),
                "actor_roles": body.get("actor_roles"),
                "reason": body.get("reason"),
                "details": {"source_phase": current, "candidates": list(successors)},
            },
        )
    conn.commit()
    return {
        "program_id": parsed_id,
        "decision_id": decision_id,
        "current_phase": target_phase,
        "program_type": program.get("program_type"),
        "program_definition_version": program.get("program_definition_version"),
        "program_definition_revision": program.get("program_definition_revision"),
    }


def patch_program_artifacts(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    parsed_id = parse_uuid(program_id)
    artifacts = body.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("artifacts must be an object")
    actor = str(body.get("actor", "orchestrator"))
    merge = bool(body.get("merge", True))

    with conn.cursor() as cur:
        cur.execute("SELECT metadata FROM delivery_programs WHERE id = %s", (parsed_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError("program not found")
        metadata = row.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        existing = _program_metadata_artifacts(metadata)
        if merge:
            existing.update(artifacts)
        else:
            existing = dict(artifacts)
        metadata["artifacts"] = existing
        cur.execute(
            """
            UPDATE delivery_programs
            SET metadata = %s::jsonb
            WHERE id = %s
            """,
            (json.dumps(metadata, default=str), parsed_id),
        )
        audit_workflow_event(
            cur,
            "delivery_program_artifacts_patched",
            "Delivery program artifacts updated",
            {"program_id": parsed_id, "keys": list(artifacts.keys())},
            actor,
        )
    conn.commit()
    return {"program_id": parsed_id, "artifacts": existing}


def cancel_delivery_program(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    parsed_id = parse_uuid(program_id)
    actor = str(body.get("actor", "orchestrator"))
    reason = str(body.get("reason") or "cancelled")
    cascade = body.get("cascade", True) is not False

    with conn.cursor() as cur:
        descendants = _descendant_program_ids(cur, parsed_id, active_only=True) if cascade else []
        cancelled_children: list[str] = []
        for target_id in [parsed_id, *descendants]:
            is_root = target_id == parsed_id
            cur.execute(
                """
                UPDATE delivery_programs
                SET status = 'cancelled'
                WHERE id = %s
                """,
                (target_id,),
            )
            cur.execute(
                """
                SELECT id
                FROM workflows
                WHERE program_id = %s
                  AND status IN ('running', 'blocked')
                """,
                (target_id,),
            )
            active_ids = [str(row.get("id")) for row in cur.fetchall()]
            for wf_id in active_ids:
                cancel_workflow_bulk(conn, wf_id, {"actor": actor, "reason": reason})
            if not is_root:
                cancelled_children.append(target_id)
            audit_workflow_event(
                cur,
                "delivery_program_cancelled",
                f"Delivery program cancelled: {reason}",
                {
                    "program_id": target_id,
                    "reason": reason,
                    "cascaded_from": None if is_root else parsed_id,
                },
                actor,
            )
    conn.commit()
    return {
        "program_id": parsed_id,
        "status": "cancelled",
        "reason": reason,
        "cancelled_children": cancelled_children,
    }


VALID_COMPENSATION_STATUSES = {"pending", "completed", "partial", "failed", "skipped"}


def record_program_compensation(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Store the cancel-compensation outcome on the program and audit it."""
    parsed_id = parse_uuid(program_id)
    status = str(body.get("status") or "").strip()
    if status not in VALID_COMPENSATION_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(VALID_COMPENSATION_STATUSES))}")
    actions = [item for item in (body.get("actions") or []) if isinstance(item, dict)]
    actor = str(body.get("actor") or "orchestrator")
    compensation = {
        "status": status,
        "actions": actions,
        "failures": [item for item in actions if item.get("result") == "failed"],
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    patch_program_artifacts(
        conn,
        parsed_id,
        {"artifacts": {"compensation": compensation, "compensation_status": status}, "actor": actor},
    )
    with conn.cursor() as cur:
        audit_workflow_event(
            cur,
            "program_compensation_recorded",
            f"Program cancel compensation {status}",
            {"program_id": parsed_id, "status": status, "actions": actions},
            actor,
        )
    conn.commit()
    return {"program_id": parsed_id, "compensation": compensation}


def link_workflow_to_program(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    parsed_program = parse_uuid(program_id)
    workflow_id = parse_uuid(body.get("workflow_id"))
    actor = str(body.get("actor", "orchestrator"))

    with conn.cursor() as cur:
        cur.execute("SELECT * FROM delivery_programs WHERE id = %s", (parsed_program,))
        program = cur.fetchone()
        if not program:
            raise ValueError("program not found")
        topology = _program_topology(program)
        program_phase = _validate_phase_id(
            str(body.get("program_phase") or ""),
            topology,
            field="program_phase",
        )
        cur.execute(
            """
            UPDATE workflows
            SET program_id = %s,
                program_phase = %s,
                correlation_id = COALESCE(correlation_id, %s)
            WHERE id = %s
            """,
            (parsed_program, program_phase, f"program:{parsed_program}", workflow_id),
        )
        if getattr(cur, "rowcount", 1) == 0:
            raise ValueError("workflow not found")
        audit_workflow_event(
            cur,
            "workflow_linked_to_program",
            f"Workflow linked to program phase {program_phase}",
            {
                "program_id": parsed_program,
                "workflow_id": workflow_id,
                "program_phase": program_phase,
                "program_type": program.get("program_type"),
                "program_definition_version": program.get("program_definition_version"),
                "program_definition_revision": program.get("program_definition_revision"),
            },
            actor,
        )
    conn.commit()
    return {
        "program_id": parsed_program,
        "workflow_id": workflow_id,
        "program_phase": program_phase,
        "program_type": program.get("program_type"),
        "program_definition_version": program.get("program_definition_version"),
        "program_definition_revision": program.get("program_definition_revision"),
    }


def update_workflow_compensation_status(conn, workflow_id: str, body: dict[str, Any]) -> dict[str, str]:
    parsed_id = parse_uuid(workflow_id)
    status = str(body.get("compensation_status") or "pending")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE workflows
            SET compensation_status = %s
            WHERE id = %s
            """,
            (status, parsed_id),
        )
    conn.commit()
    return {"workflow_id": parsed_id, "compensation_status": status}


def resume_external_event(conn, body: dict[str, Any]) -> dict[str, Any]:
    workflow_id = parse_uuid(body.get("workflow_id"))
    event_type = str(body.get("event_type") or "")
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    task_output = (
        body.get("task_output")
        if isinstance(body.get("task_output"), dict)
        else {}
    )
    actor = str(body.get("actor", "orchestrator"))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *
            FROM workflow_tasks
            WHERE workflow_id = %s
              AND status = 'blocked'
              AND error LIKE 'awaiting_external:%%'
            ORDER BY created_at
            """,
            (workflow_id,),
        )
        tasks = list(cur.fetchall())

    for task in tasks:
        expected = str(task.get("expected_event_type") or "")
        if expected and expected != event_type:
            continue
        task_id = task["id"]
        output = task_output or {
            "external_event": payload,
            "event_type": event_type,
        }
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE workflow_tasks
                SET wait_token = NULL,
                    expected_event_type = NULL,
                    status = 'succeeded',
                    output = COALESCE(%s::jsonb, output),
                    error = NULL,
                    completed_at = now()
                WHERE id = %s
                """,
                (json.dumps(output), task_id),
            )
            audit_workflow_event(
                cur,
                "workflow_task_external_resumed",
                f"Workflow task resumed via external event: {event_type}",
                {"task_id": task_id, "event_type": event_type},
                actor,
            )
            maybe_complete_workflow(cur, workflow_id, actor)
        conn.commit()
        return {"status": "resumed", "task_id": str(task_id)}

    raise ValueError("no matching external wait task found")


def find_external_waits(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Find blocked external waits, optionally filtered by event_type / pr_url."""
    event_type = str(body.get("event_type") or "").strip()
    pr_url = str(body.get("pr_url") or "").strip()
    tenant_id = str(body.get("tenant_id") or "default").strip() or "default"

    with conn.cursor() as cur:
        if event_type:
            cur.execute(
                """
                SELECT *
                FROM workflow_tasks
                WHERE status = 'blocked'
                  AND error LIKE 'awaiting_external:%%'
                  AND expected_event_type = %s
                  AND tenant_id = %s
                ORDER BY created_at
                """,
                (event_type, tenant_id),
            )
        else:
            cur.execute(
                """
                SELECT *
                FROM workflow_tasks
                WHERE status = 'blocked'
                  AND error LIKE 'awaiting_external:%%'
                  AND tenant_id = %s
                ORDER BY created_at
                """,
                (tenant_id,),
            )
        waiting = list(cur.fetchall())

    waits: list[dict[str, Any]] = []
    for task in waiting:
        workflow_id = str(task.get("workflow_id") or "")
        pr_url_set: set[str] = set()
        # The wait task itself may already have recorded expected/received URLs
        # for a match_all merge wait.
        pr_url_set |= _wait_task_pr_urls(task.get("input"))
        if workflow_id:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT output
                    FROM workflow_tasks
                    WHERE workflow_id = %s
                      AND status = 'succeeded'
                      AND step_name IN (
                          'deliver_work_order', 'open_pr', 'create_pr',
                          'qa_gate', 'collect_delivery_results'
                      )
                    ORDER BY completed_at DESC NULLS LAST, created_at DESC
                    """,
                    (parse_uuid(workflow_id),),
                )
                siblings = list(cur.fetchall())
            for sibling in siblings:
                output = sibling.get("output") or {}
                if isinstance(output, str):
                    try:
                        output = json.loads(output)
                    except json.JSONDecodeError:
                        output = {}
                pr_url_set |= _output_pr_urls(output)
        match_pr_url = sorted(pr_url_set)[0] if pr_url_set else ""
        if pr_url and pr_url_set and pr_url not in pr_url_set:
            continue
        if pr_url and not pr_url_set:
            continue
        waits.append(
            {
                "workflow_id": workflow_id,
                "task_id": str(task.get("id") or ""),
                "step_name": task.get("step_name"),
                "expected_event_type": task.get("expected_event_type"),
                "match_pr_url": match_pr_url,
                "match_pr_urls": sorted(pr_url_set),
                "match": _wait_task_match(task.get("input")),
                "options": _wait_task_match(task.get("input"), key="_wait_options"),
            }
        )
    return {"waits": waits}


def _wait_task_match(task_input: Any, *, key: str = "_wait_match") -> dict[str, Any]:
    if isinstance(task_input, str):
        try:
            task_input = json.loads(task_input)
        except json.JSONDecodeError:
            return {}
    if not isinstance(task_input, dict):
        return {}
    match = task_input.get(key)
    return dict(match) if isinstance(match, dict) else {}


def _output_pr_urls(output: Any) -> set[str]:
    """Extract every PR URL an output could contribute to a merge-wait match."""
    if not isinstance(output, dict):
        return set()
    urls: set[str] = set()
    single = output.get("pr_url")
    if isinstance(single, str) and single.strip():
        urls.add(single.strip())
    for key in ("pr_urls", "merged_pr_urls"):
        values = output.get(key)
        if isinstance(values, list):
            urls |= {str(v).strip() for v in values if isinstance(v, str) and v.strip()}
    result = output.get("result")
    if isinstance(result, dict):
        prs = result.get("prs")
        if isinstance(prs, list):
            for pr in prs:
                if isinstance(pr, dict) and isinstance(pr.get("pr_url"), str):
                    if pr["pr_url"].strip():
                        urls.add(pr["pr_url"].strip())
        result_urls = result.get("pr_urls")
        if isinstance(result_urls, list):
            urls |= {
                str(v).strip() for v in result_urls if isinstance(v, str) and v.strip()
            }
    return urls


def _wait_task_pr_urls(task_input: Any) -> set[str]:
    if isinstance(task_input, str):
        try:
            task_input = json.loads(task_input)
        except json.JSONDecodeError:
            return set()
    if not isinstance(task_input, dict):
        return set()
    urls: set[str] = set()
    for key in ("pr_urls", "_merge_received"):
        values = task_input.get(key)
        if isinstance(values, list):
            urls |= {str(v).strip() for v in values if isinstance(v, str) and v.strip()}
    single = task_input.get("pr_url")
    if isinstance(single, str) and single.strip():
        urls.add(single.strip())
    return urls


def acquire_runner_lease(conn, body: dict[str, Any]) -> dict[str, Any]:
    runner_id = str(body.get("runner_id") or "").strip()
    if not runner_id:
        raise ValueError("runner_id is required")
    ttl = int(body.get("ttl_seconds") or 30)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO runner_leader_lease (id, runner_id, expires_at)
            VALUES (1, %(runner_id)s, now() + make_interval(secs => %(ttl)s))
            ON CONFLICT (id) DO UPDATE
              SET runner_id = EXCLUDED.runner_id, expires_at = EXCLUDED.expires_at
              WHERE runner_leader_lease.runner_id = EXCLUDED.runner_id
                 OR runner_leader_lease.expires_at < now()
            RETURNING runner_id
            """,
            {"runner_id": runner_id, "ttl": ttl},
        )
        row = cur.fetchone()
        if row is not None:
            return {"leader": True, "runner_id": str(row["runner_id"])}
        cur.execute("SELECT runner_id FROM runner_leader_lease WHERE id = 1")
        current = cur.fetchone()
    holder = str(current["runner_id"]) if current else ""
    return {"leader": False, "runner_id": holder}


class MemoryHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # suppress default access logs
        pass

    def _trace_id(self) -> str:
        raw = self.headers.get("traceparent") or ""
        parts = raw.split("-")
        if len(parts) == 4 and len(parts[1]) == 32:
            return parts[1].lower()
        return ""

    def _send_json(self, status: int, data: Any):
        if status >= 400:
            print(
                json.dumps(
                    {
                        "event": "memory_api_error",
                        "path": self.path,
                        "status": status,
                        "trace_id": self._trace_id(),
                    }
                ),
                flush=True,
            )
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        return json.loads(raw) if raw else {}

    def _authorized(self) -> bool:
        token = os.environ.get("MEMORY_API_TOKEN", "").strip()
        if not token:
            allow = os.environ.get("MEMORY_API_ALLOW_UNAUTHENTICATED", "").strip().lower()
            return allow in {"1", "true", "yes"}
        presented = self.headers.get("Authorization") or ""
        return hmac.compare_digest(presented, f"Bearer {token}")

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {"ok": True, "service": "memory-api"})
            return
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        if self.path.startswith("/workflow/plan/"):
            plan_id = self.path.removeprefix("/workflow/plan/")
            try:
                with connect_db() as conn:
                    self._send_json(200, get_workflow_plan(conn, plan_id))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/workflows/by-ref?"):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            external_ref = (query.get("external_ref") or [""])[0]
            try:
                with connect_db() as conn:
                    self._send_json(200, find_workflow_by_ref(conn, external_ref))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/directory/search"):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            try:
                with connect_db() as conn:
                    self._send_json(
                        200,
                        search_directory(
                            conn,
                            tenant_id=(query.get("tenant_id") or ["default"])[0],
                            q=(query.get("q") or [""])[0],
                            limit=(query.get("limit") or ["20"])[0],
                        ),
                    )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/directory/user?"):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            try:
                with connect_db() as conn:
                    self._send_json(
                        200,
                        get_directory_user(
                            conn,
                            tenant_id=(query.get("tenant_id") or ["default"])[0],
                            subject=(query.get("subject") or [""])[0],
                            email=(query.get("email") or [""])[0],
                        ),
                    )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/programs/by-ref?"):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            binding_key = (query.get("binding_key") or [""])[0]
            tenant_id = (query.get("tenant_id") or ["default"])[0]
            try:
                with connect_db() as conn:
                    self._send_json(200, find_program_by_binding(conn, binding_key, tenant_id=tenant_id))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path == "/programs" or self.path.startswith("/programs?"):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            tenant_id = (query.get("tenant_id") or ["default"])[0]
            status = (query.get("status") or [""])[0] or None
            project_id = (query.get("project_id") or [""])[0] or None
            limit = (query.get("limit") or ["50"])[0]
            offset = (query.get("offset") or ["0"])[0]
            try:
                with connect_db() as conn:
                    self._send_json(
                        200,
                        list_delivery_programs(
                            conn,
                            tenant_id=tenant_id,
                            status=status,
                            project_id=project_id,
                            limit=limit,
                            offset=offset,
                            roots_only=(query.get("roots_only") or [""])[0].lower() in {"1", "true", "yes"},
                            phase=(query.get("phase") or [""])[0] or None,
                            owner=(query.get("owner") or [""])[0] or None,
                        ),
                    )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/programs/") and self.path.endswith("/status"):
            program_id = self.path.removeprefix("/programs/").removesuffix("/status")
            try:
                with connect_db() as conn:
                    self._send_json(200, get_delivery_program_status(conn, program_id))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/programs/") and self.path.endswith("/links"):
            program_id = self.path.removeprefix("/programs/").removesuffix("/links")
            try:
                with connect_db() as conn:
                    self._send_json(200, list_program_links(conn, program_id))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/programs/") and "/releases" in self.path:
            from urllib.parse import parse_qs, urlparse

            parsed_url = urlparse(self.path)
            program_id = parsed_url.path.removeprefix("/programs/").removesuffix("/releases")
            query = parse_qs(parsed_url.query)
            include_descendants = (query.get("include_descendants") or ["false"])[0].lower() in {"1", "true", "yes"}
            try:
                with connect_db() as conn:
                    self._send_json(
                        200, list_program_releases(conn, program_id, include_descendants=include_descendants)
                    )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/releases/last-success?"):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            try:
                with connect_db() as conn:
                    self._send_json(
                        200,
                        last_successful_release(
                            conn,
                            tenant_id=(query.get("tenant_id") or ["default"])[0],
                            repo=(query.get("repo") or [""])[0],
                            environment=(query.get("environment") or [""])[0],
                            exclude_sha=(query.get("exclude_sha") or [""])[0] or None,
                        ),
                    )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/external-refs?"):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            try:
                with connect_db() as conn:
                    self._send_json(
                        200,
                        find_program_external_refs(
                            conn,
                            kind=(query.get("kind") or [""])[0],
                            ref=(query.get("ref") or [""])[0],
                            tenant_id=(query.get("tenant_id") or [""])[0] or None,
                        ),
                    )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/decisions?"):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            try:
                with connect_db() as conn:
                    self._send_json(
                        200,
                        list_workflow_decisions(
                            conn,
                            program_id=(query.get("program_id") or [""])[0] or None,
                            workflow_id=(query.get("workflow_id") or [""])[0] or None,
                            kind=(query.get("kind") or [""])[0] or None,
                            limit=(query.get("limit") or ["100"])[0],
                        ),
                    )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/programs/") and self.path.endswith("/evidence"):
            program_id = self.path.removeprefix("/programs/").removesuffix("/evidence")
            try:
                with connect_db() as conn:
                    self._send_json(200, get_program_evidence(conn, program_id))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/programs/") and self.path.endswith("/children"):
            program_id = self.path.removeprefix("/programs/").removesuffix("/children")
            try:
                with connect_db() as conn:
                    self._send_json(200, list_program_children(conn, program_id))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/programs/") and "/external-refs" in self.path:
            from urllib.parse import parse_qs, urlparse

            parsed_url = urlparse(self.path)
            program_id = parsed_url.path.removeprefix("/programs/").removesuffix("/external-refs")
            query = parse_qs(parsed_url.query)
            include_descendants = (query.get("include_descendants") or ["false"])[0].lower() in {
                "1",
                "true",
                "yes",
            }
            try:
                with connect_db() as conn:
                    self._send_json(
                        200,
                        list_program_external_refs(
                            conn, program_id, include_descendants=include_descendants
                        ),
                    )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/programs/") and self.path.endswith("/briefs"):
            program_id = self.path.removeprefix("/programs/").removesuffix("/briefs")
            try:
                with connect_db() as conn:
                    self._send_json(200, list_program_briefs(conn, program_id=program_id))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path == "/briefs" or self.path.startswith("/briefs?"):
            from urllib.parse import parse_qs, urlparse

            query = parse_qs(urlparse(self.path).query)
            tenant_id = (query.get("tenant_id") or ["default"])[0]
            project_id = (query.get("project_id") or [""])[0] or None
            source_program_id = (query.get("source_program_id") or [""])[0] or None
            try:
                with connect_db() as conn:
                    self._send_json(
                        200,
                        list_program_briefs(
                            conn,
                            tenant_id=tenant_id,
                            project_id=project_id,
                            source_program_id=source_program_id,
                        ),
                    )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/programs/"):
            program_id = self.path.removeprefix("/programs/")
            try:
                with connect_db() as conn:
                    self._send_json(200, get_delivery_program(conn, program_id))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/workflows/") and self.path.endswith("/children"):
            workflow_id = self.path.removeprefix("/workflows/").removesuffix("/children")
            try:
                with connect_db() as conn:
                    self._send_json(200, get_workflow_children(conn, workflow_id))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/workflows/"):
            workflow_id = self.path.removeprefix("/workflows/")
            try:
                with connect_db() as conn:
                    self._send_json(200, get_workflow_record(conn, workflow_id))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        elif self.path.startswith("/workflow/"):
            workflow_id = self.path.removeprefix("/workflow/")
            try:
                with connect_db() as conn:
                    self._send_json(200, get_workflow(conn, workflow_id))
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            body = self._read_body()
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"invalid JSON: {exc}"})
            return

        try:
            with connect_db() as conn:
                if self.path == "/memory/remember":
                    self._send_json(200, remember(conn, body))
                elif self.path == "/memory/recall":
                    self._send_json(200, {"results": recall(conn, body)})
                elif self.path == "/memory/forget":
                    self._send_json(200, forget(conn, body))
                elif self.path == "/workflows/create":
                    self._send_json(200, create_workflow_record(conn, body))
                elif self.path == "/programs/create":
                    self._send_json(
                        200,
                        create_delivery_program(
                            conn,
                            body,
                            department=_optional_department(body.get("department")),
                        ),
                    )
                elif self.path == "/decisions":
                    self._send_json(200, create_workflow_decision(conn, body))
                elif self.path == "/directory/upsert-user":
                    self._send_json(200, upsert_directory_user(conn, body))
                elif self.path.startswith("/programs/") and self.path.endswith("/advance"):
                    program_id = self.path.removeprefix("/programs/").removesuffix("/advance")
                    self._send_json(200, advance_delivery_program(conn, program_id, body))
                elif self.path.startswith("/programs/") and self.path.endswith("/cancel"):
                    program_id = self.path.removeprefix("/programs/").removesuffix("/cancel")
                    self._send_json(200, cancel_delivery_program(conn, program_id, body))
                elif self.path.startswith("/programs/") and self.path.endswith("/compensation"):
                    program_id = self.path.removeprefix("/programs/").removesuffix("/compensation")
                    self._send_json(200, record_program_compensation(conn, program_id, body))
                elif self.path.startswith("/programs/") and self.path.endswith("/artifacts"):
                    program_id = self.path.removeprefix("/programs/").removesuffix("/artifacts")
                    self._send_json(200, patch_program_artifacts(conn, program_id, body))
                elif self.path.startswith("/programs/") and self.path.endswith("/link-workflow"):
                    program_id = self.path.removeprefix("/programs/").removesuffix("/link-workflow")
                    self._send_json(200, link_workflow_to_program(conn, program_id, body))
                elif self.path.startswith("/programs/") and self.path.endswith("/links"):
                    program_id = self.path.removeprefix("/programs/").removesuffix("/links")
                    self._send_json(200, create_program_link(conn, program_id, body))
                elif self.path.startswith("/programs/") and self.path.endswith("/department-memory"):
                    program_id = self.path.removeprefix("/programs/").removesuffix("/department-memory")
                    try:
                        self._send_json(200, promote_department_memory(conn, program_id, body))
                    except ValueError as exc:
                        message = str(exc)
                        status = 404 if message in {"brief not found", "program not found"} else 400
                        self._send_json(status, {"error": message})
                        return
                elif self.path.startswith("/programs/") and self.path.endswith("/briefs"):
                    program_id = self.path.removeprefix("/programs/").removesuffix("/briefs")
                    self._send_json(200, upsert_program_briefs(conn, program_id, body))
                elif self.path.startswith("/programs/") and self.path.endswith("/external-refs"):
                    program_id = self.path.removeprefix("/programs/").removesuffix("/external-refs")
                    self._send_json(200, record_program_external_ref(conn, program_id, body))
                elif self.path.startswith("/programs/") and self.path.endswith("/releases"):
                    program_id = self.path.removeprefix("/programs/").removesuffix("/releases")
                    self._send_json(200, record_program_release(conn, program_id, body))
                elif self.path.startswith("/programs/") and self.path.endswith("/evidence/exported"):
                    program_id = self.path.removeprefix("/programs/").removesuffix("/evidence/exported")
                    self._send_json(200, record_program_evidence_export(conn, program_id, body))
                elif self.path == "/workflow/task":
                    self._send_json(200, create_workflow_task(conn, body))
                elif self.path == "/workflow/create":
                    self._send_json(200, create_workflow_task(conn, body))
                elif self.path == "/workflow/tasks":
                    self._send_json(200, create_workflow_tasks(conn, body))
                elif self.path == "/runner/leader":
                    self._send_json(200, acquire_runner_lease(conn, body))
                elif self.path == "/workflow/ready":
                    self._send_json(200, {"tasks": list_ready_workflow_tasks(conn, body)})
                elif self.path == "/workflow/retry":
                    self._send_json(200, retry_workflow_task(conn, body))
                elif self.path == "/workflow/task/resume-approval":
                    self._send_json(200, resume_tool_approval_task(conn, body))
                elif self.path == "/workflow/task/resume-blocked":
                    self._send_json(200, resume_blocked_task(conn, body))
                elif self.path == "/workflow/stuck":
                    self._send_json(200, list_stuck_tasks(conn, body))
                elif self.path == "/workflow/runtime":
                    self._send_json(200, record_runtime_task(conn, body))
                elif self.path == "/workflow/stale":
                    self._send_json(200, {"tasks": list_stale_workflow_tasks(conn, body)})
                elif self.path == "/workflow/plan/propose":
                    self._send_json(200, propose_workflow_plan(conn, body))
                elif self.path == "/workflow/plan/approve":
                    self._send_json(200, approve_workflow_plan(conn, body))
                elif self.path == "/workflow/plan/reject":
                    self._send_json(200, reject_workflow_plan(conn, body))
                elif self.path == "/workflow/claim":
                    self._send_json(200, claim_workflow_task(conn, body))
                elif self.path == "/workflow/complete":
                    body["status"] = "succeeded"
                    self._send_json(200, transition_workflow_task(conn, body))
                elif self.path == "/workflow/fail":
                    body["status"] = "failed"
                    self._send_json(200, transition_workflow_task(conn, body))
                elif self.path == "/workflow/block":
                    body["status"] = "blocked"
                    self._send_json(200, transition_workflow_task(conn, body))
                elif self.path == "/workflow/cancel":
                    body["status"] = "cancelled"
                    self._send_json(200, transition_workflow_task(conn, body))
                elif self.path == "/workflow/approve":
                    self._send_json(200, approve_workflow_task(conn, body, True))
                elif self.path == "/workflow/reject":
                    self._send_json(200, approve_workflow_task(conn, body, False))
                elif self.path == "/workflow/resume-external":
                    self._send_json(200, resume_external_event(conn, body))
                elif self.path == "/workflow/find-external-waits":
                    self._send_json(200, find_external_waits(conn, body))
                elif self.path == "/workflow/task/wait":
                    self._send_json(200, set_task_wait_state(conn, body))
                elif self.path == "/workflow/task/wait/clear":
                    self._send_json(200, clear_task_wait_state(conn, body))
                elif self.path == "/workflow/waits/due":
                    self._send_json(200, list_due_waits(conn, body))
                elif self.path == "/workflow/task/deadline":
                    self._send_json(200, mark_wait_deadline(conn, body))
                elif self.path.startswith("/workflows/") and self.path.endswith("/cancel"):
                    workflow_id = self.path.removeprefix("/workflows/").removesuffix("/cancel")
                    self._send_json(200, cancel_workflow_bulk(conn, workflow_id, body))
                elif self.path.startswith("/workflows/") and self.path.endswith("/complete"):
                    workflow_id = self.path.removeprefix("/workflows/").removesuffix("/complete")
                    self._send_json(200, complete_workflow_record(conn, workflow_id, body))
                elif self.path.startswith("/workflows/") and self.path.endswith("/compensation"):
                    workflow_id = self.path.removeprefix("/workflows/").removesuffix("/compensation")
                    self._send_json(200, update_workflow_compensation_status(conn, workflow_id, body))
                else:
                    self._send_json(404, {"error": "not found"})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": str(exc)})

    def do_PATCH(self):
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        try:
            body = self._read_body()
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"invalid JSON: {exc}"})
            return

        try:
            with connect_db() as conn:
                if self.path.startswith("/workflow/task/") and self.path.endswith("/input"):
                    task_id = self.path.removeprefix("/workflow/task/").removesuffix("/input")
                    self._send_json(200, patch_workflow_task_input(conn, task_id, body))
                elif self.path.startswith("/programs/"):
                    program_id = self.path.removeprefix("/programs/")
                    self._send_json(200, patch_delivery_program(conn, program_id, body))
                else:
                    self._send_json(404, {"error": "not found"})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": str(exc)})

    def do_DELETE(self):
        if not self._authorized():
            self._send_json(401, {"error": "unauthorized"})
            return
        if "/links/" not in self.path or not self.path.startswith("/programs/"):
            self._send_json(404, {"error": "not found"})
            return
        rest = self.path.removeprefix("/programs/")
        program_id, _, to_program_id = rest.partition("/links/")
        try:
            body = self._read_body()
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": f"invalid JSON: {exc}"})
            return
        try:
            with connect_db() as conn:
                self._send_json(200, delete_program_link(conn, program_id, to_program_id, body))
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"error": str(exc)})


def main() -> None:
    port = int(os.environ.get("MEMORY_API_PORT", "18792"))
    server = ThreadingHTTPServer(("127.0.0.1", port), MemoryHandler)
    server.daemon_threads = True
    print(f"memory-api listening on 127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    finally:
        if _pool is not None:
            _pool.close()


if __name__ == "__main__":
    main()
