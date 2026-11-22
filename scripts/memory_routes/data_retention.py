"""Data retention: trim or delete stored client data past its window, a small batch at a time.

The runtime's ``apply_data_retention`` system job calls ``apply_retention`` daily, then lists
uploaded design images of long-finished programs (``expired_uploads``), deletes them from the
object store and records that (``mark_uploads_purged``). ``purge_program`` deletes one program's
data on request (scripts/ops/purge-program-data.py). See docs/data-handling.md.

Nothing here touches a running or blocked workflow, an unfinished program or a coding job that
has not finished. Approval and decision audit rows have their own window, and the
``workflow_decisions`` table is never trimmed by the job.
"""

from __future__ import annotations

import uuid
from typing import Any

from psycopg.types.json import Jsonb

DEFAULT_BATCH = 500
MAX_BATCH = 5000
SCALAR_MAX_CHARS = 200
ERROR_MAX_CHARS = 500

FINISHED_WORKFLOW = ("succeeded", "failed", "cancelled", "superseded")
OPEN_WORKFLOW = ("running", "blocked")
FINISHED_PROGRAM = ("succeeded", "failed", "cancelled")
FINISHED_JOB = ("succeeded", "failed", "lost", "cancelled")
# Log text inside a coding job result: the run log, check output, captured streams.
LOG_KEYS = frozenset({"log_tail", "output_tail", "stdout", "stderr", "last_output"})
# Approval and decision audit (gates, approvals, rejections, permissions, access, governance,
# releases, evidence exports, purges) keeps its own window.
DECISION_AUDIT_PATTERN = r"(approv|reject|decision|gate|permission|access|governance|release|evidence|purge)"
DESIGN_PREFIX = "design/"


def _days(body: dict[str, Any], key: str) -> int:
    try:
        value = int(body.get(key) or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a whole number of days") from exc
    if value < 0:
        raise ValueError(f"{key} must be 0 (keep forever) or more")
    return value


def _batch(body: dict[str, Any]) -> int:
    try:
        value = int(body.get("batch") or DEFAULT_BATCH)
    except (TypeError, ValueError) as exc:
        raise ValueError("batch must be a whole number") from exc
    return max(1, min(MAX_BATCH, value))


def trim_logs(value: Any) -> Any:
    """``value`` without log text at any depth; every other field is kept."""
    if isinstance(value, dict):
        return {k: trim_logs(v) for k, v in value.items() if k not in LOG_KEYS}
    if isinstance(value, list):
        return [trim_logs(item) for item in value]
    return value


def small_fields(value: Any) -> dict[str, Any]:
    """The top-level scalars of a JSON object (ids, status, outcome, URLs), without long text."""
    if not isinstance(value, dict):
        return {}
    out: dict[str, Any] = {}
    for key, item in value.items():
        if item is None or isinstance(item, (bool, int, float)):
            out[key] = item
        elif isinstance(item, str) and len(item) <= SCALAR_MAX_CHARS:
            out[key] = item
    out["retention_trimmed"] = True
    return out


def _clip(text: Any) -> Any:
    if isinstance(text, str) and len(text) > ERROR_MAX_CHARS:
        return text[:ERROR_MAX_CHARS] + " [trimmed]"
    return text


def _transcripts(cur, days: int, batch: int) -> dict[str, Any]:
    if not days:
        return {"kept": "forever"}
    # Replays read the latest messages and repair a history that starts mid tool call,
    # so old messages can go from a session that is still active.
    cur.execute(
        """
        DELETE FROM runtime_transcript_messages
         WHERE (session_id, seq) IN (
               SELECT session_id, seq FROM runtime_transcript_messages
                WHERE created_at < now() - make_interval(days => %s)
                LIMIT %s)
        """,
        (days, batch),
    )
    return {"deleted": cur.rowcount}


def _coding_logs(cur, days: int, batch: int) -> dict[str, Any]:
    if not days:
        return {"kept": "forever"}
    cur.execute(
        """
        SELECT j.id, j.result, j.error
          FROM coding_jobs j
          LEFT JOIN workflows w ON w.id = j.workflow_id
         WHERE j.retention_trimmed_at IS NULL
           AND j.finished_at IS NOT NULL
           AND j.finished_at < now() - make_interval(days => %s)
           AND j.status = ANY(%s::text[])
           AND (w.id IS NULL OR NOT (w.status = ANY(%s::text[])))
         ORDER BY j.finished_at
         LIMIT %s
           FOR UPDATE OF j SKIP LOCKED
        """,
        (days, list(FINISHED_JOB), list(OPEN_WORKFLOW), batch),
    )
    rows = cur.fetchall()
    for row in rows:
        result = row["result"]
        cur.execute(
            """
            UPDATE coding_jobs
               SET result = %s, error = %s, progress = NULL, last_output = NULL, retention_trimmed_at = now()
             WHERE id = %s
            """,
            (Jsonb(trim_logs(result)) if result is not None else None, _clip(row["error"]), row["id"]),
        )
    return {"trimmed": len(rows)}


def _workflow_tasks(cur, days: int, batch: int) -> dict[str, Any]:
    if not days:
        return {"kept": "forever"}
    cur.execute(
        """
        SELECT t.id, t.input, t.output, t.error
          FROM workflow_tasks t
          JOIN workflows w ON w.id = t.workflow_id
          LEFT JOIN delivery_programs p ON p.id = workflow_program_id(w.id)
          LEFT JOIN delivery_programs root ON root.id = p.root_program_id
         WHERE t.retention_trimmed_at IS NULL
           AND t.status <> 'running'
           AND w.status = ANY(%s::text[])
           AND COALESCE(w.completed_at, w.updated_at) < now() - make_interval(days => %s)
           AND (p.id IS NULL OR p.status = ANY(%s::text[]))
           AND (root.id IS NULL OR root.status = ANY(%s::text[]))
         LIMIT %s
           FOR UPDATE OF t SKIP LOCKED
        """,
        (list(FINISHED_WORKFLOW), days, list(FINISHED_PROGRAM), list(FINISHED_PROGRAM), batch),
    )
    rows = cur.fetchall()
    for row in rows:
        cur.execute(
            """
            UPDATE workflow_tasks
               SET input = %s, output = %s, error = %s, retention_trimmed_at = now()
             WHERE id = %s
            """,
            (Jsonb(small_fields(row["input"])), Jsonb(small_fields(row["output"])), _clip(row["error"]), row["id"]),
        )
    jobs = 0
    if rows:
        # The coding job spec carries the same work order the task input did.
        cur.execute("SELECT id, spec FROM coding_jobs WHERE task_id = ANY(%s::uuid[])", ([row["id"] for row in rows],))
        for job in cur.fetchall():
            cur.execute("UPDATE coding_jobs SET spec = %s WHERE id = %s", (Jsonb(small_fields(job["spec"])), job["id"]))
            jobs += 1
    return {"trimmed": len(rows), "coding_job_specs": jobs}


def _audit(cur, days: int, decision_days: int, batch: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for label, window, match in (("deleted", days, "!~*"), ("decisions_deleted", decision_days, "~*")):
        if not window:
            out[label] = 0
            continue
        cur.execute(
            f"""
            DELETE FROM runtime_audit_events
             WHERE sequence IN (
                   SELECT sequence FROM runtime_audit_events
                    WHERE occurred_at < now() - make_interval(days => %s)
                      AND action_type {match} %s
                    LIMIT %s)
            """,
            (window, DECISION_AUDIT_PATTERN, batch),
        )
        deleted = cur.rowcount
        cur.execute(
            f"""
            DELETE FROM session_audit_log
             WHERE id IN (
                   SELECT id FROM session_audit_log
                    WHERE occurred_at < now() - make_interval(days => %s)
                      AND action_type {match} %s
                    LIMIT %s)
            """,
            (window, DECISION_AUDIT_PATTERN, batch),
        )
        out[label] = deleted + cur.rowcount
    if not days and not decision_days:
        out["kept"] = "forever"
    return out


def apply_retention(conn, body: dict[str, Any]) -> dict[str, Any]:
    """One batch per category; each category commits on its own. Days of 0 keep that data forever."""
    batch = _batch(body)
    transcript_days = _days(body, "transcript_days")
    coding_log_days = _days(body, "coding_log_days")
    task_days = _days(body, "workflow_task_days")
    audit_days = _days(body, "audit_days")
    decision_days = _days(body, "audit_decision_days")
    steps = (
        ("transcripts", lambda cur: _transcripts(cur, transcript_days, batch)),
        ("coding_job_logs", lambda cur: _coding_logs(cur, coding_log_days, batch)),
        ("workflow_tasks", lambda cur: _workflow_tasks(cur, task_days, batch)),
        ("audit_events", lambda cur: _audit(cur, audit_days, decision_days, batch)),
    )
    out: dict[str, Any] = {"batch": batch}
    for name, step in steps:
        with conn.cursor() as cur:
            out[name] = step(cur)
        conn.commit()
    return out


def _design_keys(refs: Any) -> set[str]:
    if not isinstance(refs, list):
        return set()
    return {
        str(ref.get("object_key"))
        for ref in refs
        if isinstance(ref, dict) and str(ref.get("object_key") or "").startswith(DESIGN_PREFIX)
    }


def _keys_in_use(cur, keys: set[str], *, exclude_programs: list[Any]) -> set[str]:
    """Keys another program still lists, or a running or blocked workflow's tasks mention."""
    if not keys:
        return set()
    cur.execute(
        """
        SELECT DISTINCT ref->>'object_key' AS key
          FROM delivery_programs p
          LEFT JOIN program_upload_purges done ON done.program_id = p.id
         CROSS JOIN LATERAL jsonb_array_elements(
               CASE WHEN jsonb_typeof(p.metadata->'artifacts'->'design_refs') = 'array'
                    THEN p.metadata->'artifacts'->'design_refs' ELSE '[]'::jsonb END) ref
         WHERE done.program_id IS NULL
           AND NOT (p.id = ANY(%s::uuid[]))
           AND ref->>'object_key' = ANY(%s::text[])
        """,
        (exclude_programs, sorted(keys)),
    )
    used = {row["key"] for row in cur.fetchall()}
    cur.execute(
        """
        SELECT k AS key FROM unnest(%s::text[]) k
         WHERE EXISTS (
               SELECT 1 FROM workflow_tasks t JOIN workflows w ON w.id = t.workflow_id
                WHERE w.status = ANY(%s::text[])
                  AND (strpos(t.input::text, k) > 0 OR strpos(t.output::text, k) > 0))
        """,
        (sorted(keys - used), list(OPEN_WORKFLOW)),
    )
    return used | {row["key"] for row in cur.fetchall()}


def expired_uploads(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Programs finished more than ``days`` ago with design images to delete, and the keys no one else uses."""
    days = _days(body, "days")
    if not days:
        return {"programs": [], "object_keys": [], "kept": "forever"}
    batch = min(_batch(body), 200)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id, p.metadata->'artifacts'->'design_refs' AS refs
              FROM delivery_programs p
              LEFT JOIN delivery_programs root ON root.id = p.root_program_id
              LEFT JOIN program_upload_purges done ON done.program_id = p.id
             WHERE done.program_id IS NULL
               AND p.status = ANY(%s::text[])
               AND p.updated_at < now() - make_interval(days => %s)
               AND (root.id IS NULL OR root.status = ANY(%s::text[]))
               AND jsonb_typeof(p.metadata->'artifacts'->'design_refs') = 'array'
             ORDER BY p.updated_at
             LIMIT %s
            """,
            (list(FINISHED_PROGRAM), days, list(FINISHED_PROGRAM), batch),
        )
        rows = cur.fetchall()
        programs = [row["id"] for row in rows]
        keys: set[str] = set()
        for row in rows:
            keys |= _design_keys(row["refs"])
        in_use = _keys_in_use(cur, keys, exclude_programs=programs)
    conn.commit()
    return {
        "programs": [str(pid) for pid in programs],
        "object_keys": sorted(keys - in_use),
        "kept_in_use": len(keys & in_use),
    }


def mark_uploads_purged(conn, body: dict[str, Any]) -> dict[str, Any]:
    programs = [str(uuid.UUID(str(pid))) for pid in body.get("programs") or []]
    with conn.cursor() as cur:
        for pid in programs:
            cur.execute(
                """
                INSERT INTO program_upload_purges (program_id)
                SELECT id FROM delivery_programs WHERE id = %s
                ON CONFLICT (program_id) DO NOTHING
                """,
                (pid,),
            )
    conn.commit()
    return {"marked": len(programs)}


# --- Deleting one program's data on request -------------------------------------------------

def _program_tree(cur, program_id: str) -> list[dict[str, Any]]:
    cur.execute(
        """
        WITH RECURSIVE tree AS (
            SELECT id FROM delivery_programs WHERE id = %s
            UNION
            SELECT c.id FROM delivery_programs c JOIN tree ON c.parent_program_id = tree.id OR c.root_program_id = tree.id
        )
        SELECT p.id, p.tenant_id, p.status, p.metadata->'artifacts'->'design_refs' AS refs
          FROM delivery_programs p JOIN tree ON tree.id = p.id
        """,
        (program_id,),
    )
    return cur.fetchall()


def _program_workflows(cur, programs: list[Any]) -> list[dict[str, Any]]:
    cur.execute(
        """
        WITH RECURSIVE wf AS (
            SELECT id FROM workflows WHERE program_id = ANY(%s::uuid[])
            UNION
            SELECT w.id FROM workflows w JOIN wf ON w.parent_workflow_id = wf.id
        )
        SELECT w.id, w.status FROM workflows w JOIN wf ON wf.id = w.id
        """,
        (programs,),
    )
    return cur.fetchall()


def purge_program(conn, program_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """Delete a finished program, its child programs, their workflows and everything stored for them.

    ``dry_run`` counts without deleting. Decision records (``workflow_decisions``,
    ``program_releases``) are kept unless ``include_decisions``; LLM usage rows keep their cost
    and lose their program, workflow and task ids. Transcripts go for sessions tagged with the
    program and for ``session_ids``. The caller deletes the returned design ``object_keys``.
    """
    try:
        program_id = str(uuid.UUID(str(program_id)))
    except ValueError as exc:
        raise ValueError("program id must be a UUID") from exc
    dry_run = bool(body.get("dry_run"))
    include_decisions = bool(body.get("include_decisions"))
    session_ids = [str(s).strip() for s in body.get("session_ids") or [] if str(s).strip()]
    actor = str(body.get("actor") or "ops").strip()[:200] or "ops"
    with conn.cursor() as cur:
        tree = _program_tree(cur, program_id)
        if not tree:
            raise LookupError(f"program {program_id} not found")
        open_programs = [str(p["id"]) for p in tree if p["status"] not in FINISHED_PROGRAM]
        if open_programs:
            raise ValueError(f"program has unfinished programs ({', '.join(open_programs)}); cancel them first")
        programs = [p["id"] for p in tree]
        program_text = [str(p) for p in programs]
        workflows = _program_workflows(cur, programs)
        open_workflows = [str(w["id"]) for w in workflows if w["status"] in OPEN_WORKFLOW]
        if open_workflows:
            raise ValueError(f"program has running or blocked workflows ({', '.join(open_workflows)}); cancel them first")
        wf_ids = [w["id"] for w in workflows]
        wf_text = [str(w) for w in wf_ids]
        correlations = wf_text + program_text + [f"program:{p}" for p in program_text]
        tags = [f"program:{p}" for p in program_text]
        cur.execute(
            "SELECT session_id FROM runtime_sessions WHERE metadata->>'program_id' = ANY(%s::text[])", (program_text,)
        )
        sessions = sorted({row["session_id"] for row in cur.fetchall()} | set(session_ids))

        steps: list[tuple[str, str, tuple[Any, ...]]] = [
            ("coding_jobs", "FROM coding_jobs WHERE workflow_id = ANY(%s::uuid[]) OR program_id = ANY(%s::uuid[])", (wf_ids, programs)),
            ("tool_approvals", "FROM tool_approvals WHERE workflow_id = ANY(%s::text[])", (wf_text,)),
            ("workflow_plans", "FROM workflow_plans WHERE workflow_id = ANY(%s::uuid[])", (wf_ids,)),
            ("workflow_locks", "FROM workflow_locks WHERE workflow_id = ANY(%s::uuid[])", (wf_ids,)),
            ("workflow_tasks", "FROM workflow_tasks WHERE workflow_id = ANY(%s::uuid[])", (wf_ids,)),
            (
                "session_audit_log",
                "FROM session_audit_log WHERE details->>'program_id' = ANY(%s::text[]) OR details->>'workflow_id' = ANY(%s::text[])",
                (program_text, wf_text),
            ),
            (
                "runtime_audit_events",
                "FROM runtime_audit_events WHERE correlation_id = ANY(%s::text[])"
                " OR payload->>'program_id' = ANY(%s::text[]) OR payload->>'workflow_id' = ANY(%s::text[])",
                (correlations, program_text, wf_text),
            ),
            ("memories", "FROM memories WHERE tags && %s::text[]", (tags,)),
            ("transcript_messages", "FROM runtime_transcript_messages WHERE session_id = ANY(%s::text[])", (sessions,)),
        ]
        if include_decisions:
            steps += [
                ("workflow_decisions", "FROM workflow_decisions WHERE program_id = ANY(%s::uuid[]) OR workflow_id = ANY(%s::uuid[])", (programs, wf_ids)),
                ("program_releases", "FROM program_releases WHERE program_id = ANY(%s::uuid[]) OR workflow_id = ANY(%s::uuid[])", (programs, wf_ids)),
            ]
        steps += [
            ("workflows", "FROM workflows WHERE id = ANY(%s::uuid[])", (wf_ids,)),
            ("programs", "FROM delivery_programs WHERE id = ANY(%s::uuid[])", (programs,)),
        ]
        counts: dict[str, int] = {}
        for label, where, params in steps:
            cur.execute(f"SELECT count(*) AS n {where}", params)
            counts[label] = int(cur.fetchone()["n"])
        cur.execute(
            "SELECT count(*) AS n FROM llm_usage WHERE program_id = ANY(%s::text[]) OR workflow_id = ANY(%s::text[])",
            (program_text, wf_text),
        )
        counts["llm_usage_detached"] = int(cur.fetchone()["n"])

        keys: set[str] = set()
        for program in tree:
            keys |= _design_keys(program["refs"])
        object_keys = sorted(keys - _keys_in_use(cur, keys, exclude_programs=programs))

        if dry_run:
            conn.rollback()
            return {"dry_run": True, "program_id": program_id, "programs": program_text, "workflows": len(wf_ids),
                    "sessions": len(sessions), "counts": counts, "object_keys": object_keys}

        for label, where, params in steps:
            cur.execute(f"DELETE {where}", params)
        cur.execute(
            """
            UPDATE llm_usage SET program_id = NULL, workflow_id = NULL, task_id = NULL
             WHERE program_id = ANY(%s::text[]) OR workflow_id = ANY(%s::text[])
            """,
            (program_text, wf_text),
        )
        cur.execute(
            """
            INSERT INTO runtime_audit_events (event_id, tenant_id, action_type, actor_id, correlation_id, payload)
            VALUES (%s, %s, 'program_data_purged', %s, %s, %s)
            """,
            (
                str(uuid.uuid4()),
                str(next(p["tenant_id"] for p in tree if str(p["id"]) == program_id) or "default"),
                actor,
                f"program:{program_id}",
                Jsonb({"programs": program_text, "workflows": len(wf_ids), "counts": counts,
                       "object_keys": len(object_keys), "include_decisions": include_decisions}),
            ),
        )
    conn.commit()
    return {"dry_run": False, "program_id": program_id, "programs": program_text, "workflows": len(wf_ids),
            "sessions": len(sessions), "counts": counts, "object_keys": object_keys}
