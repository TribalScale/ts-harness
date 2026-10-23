"""Coding jobs: queued OpenCode runs that a launcher hands to worker containers.

A worker holds only a job token, which the memory API accepts on that job's
spec, heartbeat and complete endpoints. Finishing a job (or losing it) puts
the parked workflow task back to pending; the runner then reads the result,
checks the stage contract and carries on as if the agent had returned it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from typing import Any

ACTIVE = ("queued", "starting", "running")
RUNNING = ("starting", "running")
TERMINAL = ("succeeded", "failed", "lost", "cancelled")
WAIT_ERROR = "awaiting_external:coding.finished"
TOKEN_PREFIX = "cj1"
DEFAULT_TOKEN_TTL = 4 * 3600
MAX_LAUNCH_ATTEMPTS = 3
_LAST_OUTPUT_LIMIT = 2000


def _uuid(value: Any, field: str = "job_id") -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _secret() -> bytes:
    explicit = os.environ.get("CODING_JOB_SECRET", "").strip()
    if explicit:
        return explicit.encode()
    base = os.environ.get("MEMORY_API_TOKEN", "").strip()
    if not base:
        raise RuntimeError("CODING_JOB_SECRET or MEMORY_API_TOKEN is required for coding job tokens")
    return hmac.new(base.encode(), b"ts-harness-coding-job", hashlib.sha256).hexdigest().encode()


def issue_job_token(job_id: str, *, ttl_seconds: int = DEFAULT_TOKEN_TTL, now: float | None = None) -> str:
    expiry = int((now if now is not None else time.time()) + ttl_seconds)
    message = f"{job_id}|{expiry}".encode()
    signature = hmac.new(_secret(), message, hashlib.sha256).hexdigest()
    return f"{TOKEN_PREFIX}.{job_id}.{expiry}.{signature}"


def verify_job_token(token: str, job_id: str, *, now: float | None = None) -> bool:
    try:
        prefix, token_job, expiry, signature = str(token or "").split(".")
    except ValueError:
        return False
    if prefix != TOKEN_PREFIX or token_job != str(job_id):
        return False
    try:
        if int(expiry) < (now if now is not None else time.time()):
            return False
    except ValueError:
        return False
    expected = hmac.new(_secret(), f"{token_job}|{expiry}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _audit(cur, action_type: str, summary: str, details: dict[str, Any], actor: str) -> None:
    cur.execute(
        """
        INSERT INTO session_audit_log (id, session_id, agent_id, action_type, summary, details, occurred_at)
        VALUES (uuid_generate_v4(), %s, %s, %s, %s, %s::jsonb, now())
        """,
        (
            str(details.get("workflow_id") or details.get("job_id") or "coding-job"),
            actor,
            action_type,
            summary,
            json.dumps(details, default=str),
        ),
    )


def _public(row: dict[str, Any], *, spec: bool = False) -> dict[str, Any]:
    keys = (
        "id", "tenant_id", "workflow_id", "task_id", "program_id", "team_slug", "project_id",
        "requested_by", "repo_url", "base_branch", "work_order_id", "status", "launcher",
        "runner_ref", "attempt", "heartbeat_at", "last_output", "result", "error",
        "created_at", "claimed_at", "started_at", "finished_at", "consumed_at",
    )
    job = {key: row.get(key) for key in keys}
    for key in ("id", "workflow_id", "task_id", "program_id"):
        if job.get(key) is not None:
            job[key] = str(job[key])
    if spec:
        job["spec"] = row.get("spec")
    return job


def _resume_task(cur, task_id: Any, job_id: str, actor: str) -> bool:
    """Put the parked task back to pending so the runner reads the job result."""
    if not task_id:
        return False
    cur.execute(
        """
        UPDATE workflow_tasks
        SET status = 'pending',
            error = NULL,
            wait_token = NULL,
            expected_event_type = NULL,
            escalate_at = NULL,
            expires_at = NULL,
            escalated_at = NULL,
            locked_by = NULL,
            lock_expires_at = NULL,
            attempt = GREATEST(attempt - 1, 0),
            next_attempt_at = now()
        WHERE id = %s AND status = 'blocked' AND error = %s
        """,
        (task_id, WAIT_ERROR),
    )
    resumed = getattr(cur, "rowcount", 0) > 0
    if resumed:
        _audit(
            cur,
            "workflow_task_external_resumed",
            "Workflow task resumed via external event: coding.finished",
            {"task_id": str(task_id), "event_type": "coding.finished", "job_id": job_id},
            actor,
        )
    return resumed


def create_coding_job(conn, body: dict[str, Any]) -> dict[str, Any]:
    spec = body.get("spec")
    if not isinstance(spec, dict) or not str(spec.get("repo_url") or "").strip():
        raise ValueError("spec with repo_url is required")
    task_id = _uuid(body.get("task_id"), "task_id") if body.get("task_id") else None
    workflow_id = _uuid(body.get("workflow_id"), "workflow_id") if body.get("workflow_id") else None
    program_id = _uuid(body.get("program_id"), "program_id") if body.get("program_id") else None
    actor = str(body.get("actor") or "workflow-runner")
    with conn.cursor() as cur:
        if task_id:
            cur.execute(
                "SELECT * FROM coding_jobs WHERE task_id = %s AND status = ANY(%s) ORDER BY created_at DESC LIMIT 1",
                (task_id, list(ACTIVE)),
            )
            existing = cur.fetchone()
            if existing:
                return {**_public(existing), "created": False}
        cur.execute(
            """
            INSERT INTO coding_jobs
              (tenant_id, workflow_id, task_id, program_id, team_slug, project_id, requested_by,
               repo_url, base_branch, work_order_id, spec)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            RETURNING *
            """,
            (
                str(body.get("tenant_id") or "default"),
                workflow_id,
                task_id,
                program_id,
                body.get("team_slug"),
                body.get("project_id"),
                spec.get("requested_by"),
                spec["repo_url"],
                spec.get("base_branch"),
                spec.get("work_order_id"),
                json.dumps(spec),
            ),
        )
        row = cur.fetchone()
        _audit(
            cur,
            "coding_job_created",
            f"Coding job queued for {spec.get('work_order_id') or 'work order'}",
            {"job_id": str(row["id"]), "workflow_id": workflow_id, "task_id": task_id, "repo_url": spec["repo_url"]},
            actor,
        )
    conn.commit()
    return {**_public(row), "created": True}


def claim_coding_jobs(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Claim queued jobs up to ``max_running`` jobs starting or running in total."""
    launcher = str(body.get("launcher") or "local")
    try:
        max_running = max(0, int(body.get("max_running", 3)))
        limit = max(0, int(body.get("limit", max_running)))
    except (TypeError, ValueError) as exc:
        raise ValueError("max_running and limit must be integers") from exc
    ttl = int(body.get("token_ttl_seconds") or DEFAULT_TOKEN_TTL)
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext('coding_jobs_claim'))")
        cur.execute("SELECT count(*) AS n FROM coding_jobs WHERE status = ANY(%s)", (list(RUNNING),))
        running = int(cur.fetchone()["n"])
        room = min(limit, max_running - running)
        claimed: list[dict[str, Any]] = []
        if room > 0:
            cur.execute(
                """
                SELECT id FROM coding_jobs
                WHERE status = 'queued'
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (room,),
            )
            ids = [r["id"] for r in cur.fetchall()]
            if ids:
                cur.execute(
                    """
                    UPDATE coding_jobs
                    SET status = 'starting', claimed_at = now(), launcher = %s, attempt = attempt + 1,
                        heartbeat_at = now()
                    WHERE id = ANY(%s)
                    RETURNING *
                    """,
                    (launcher, ids),
                )
                claimed = sorted(cur.fetchall(), key=lambda r: r["created_at"])
    conn.commit()
    return {
        "running": running,
        "jobs": [{**_public(r), "token": issue_job_token(str(r["id"]), ttl_seconds=ttl)} for r in claimed],
    }


def record_launch(conn, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """The launcher started the worker (``runner_ref``) or failed to (``error``)."""
    jid = _uuid(job_id)
    actor = str(body.get("actor") or "coding-launcher")
    error = str(body.get("error") or "").strip()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM coding_jobs WHERE id = %s FOR UPDATE", (jid,))
        row = cur.fetchone()
        if not row:
            raise LookupError("coding job not found")
        if row["status"] != "starting":
            conn.commit()
            return _public(row)
        if not error:
            cur.execute(
                "UPDATE coding_jobs SET runner_ref = %s WHERE id = %s RETURNING *",
                (str(body.get("runner_ref") or ""), jid),
            )
            row = cur.fetchone()
        elif body.get("retryable") is True:
            # Capacity or quota limits: wait for room without spending a launch attempt.
            cur.execute(
                """
                UPDATE coding_jobs SET status = 'queued', error = %s, claimed_at = NULL,
                       attempt = GREATEST(attempt - 1, 0)
                WHERE id = %s RETURNING *
                """,
                (f"waiting for capacity: {error}", jid),
            )
            row = cur.fetchone()
        elif int(row["attempt"]) < MAX_LAUNCH_ATTEMPTS:
            cur.execute(
                "UPDATE coding_jobs SET status = 'queued', error = %s, claimed_at = NULL WHERE id = %s RETURNING *",
                (f"launch failed: {error}", jid),
            )
            row = cur.fetchone()
        else:
            cur.execute(
                """
                UPDATE coding_jobs SET status = 'failed', error = %s, finished_at = now(),
                       result = %s::jsonb
                WHERE id = %s RETURNING *
                """,
                (f"launch failed: {error}", json.dumps({"ok": False, "error": f"coding job could not start: {error}"}), jid),
            )
            row = cur.fetchone()
            _audit(cur, "coding_job_finished", "Coding job could not start", {"job_id": jid, "workflow_id": str(row.get("workflow_id") or ""), "status": "failed"}, actor)
            _resume_task(cur, row.get("task_id"), jid, actor)
    conn.commit()
    return _public(row)


def get_job_spec(conn, job_id: str) -> dict[str, Any]:
    """The worker's first call: returns the spec and marks the job running."""
    jid = _uuid(job_id)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE coding_jobs SET status = 'running', started_at = COALESCE(started_at, now()), heartbeat_at = now()
            WHERE id = %s AND status = 'starting'
            RETURNING *
            """,
            (jid,),
        )
        row = cur.fetchone()
        if row:
            _audit(cur, "coding_job_started", "Coding job started", {"job_id": jid, "workflow_id": str(row.get("workflow_id") or "")}, "coding-worker")
        else:
            cur.execute("SELECT * FROM coding_jobs WHERE id = %s", (jid,))
            row = cur.fetchone()
            if not row:
                raise LookupError("coding job not found")
    conn.commit()
    return _public(row, spec=True)


def heartbeat_coding_job(conn, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
    jid = _uuid(job_id)
    last_output = body.get("last_output")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE coding_jobs
            SET heartbeat_at = now(), last_output = COALESCE(%s, last_output)
            WHERE id = %s AND status = ANY(%s)
            RETURNING id, status
            """,
            (str(last_output)[-_LAST_OUTPUT_LIMIT:] if last_output is not None else None, jid, list(RUNNING)),
        )
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT id, status FROM coding_jobs WHERE id = %s", (jid,))
            row = cur.fetchone()
            if not row:
                raise LookupError("coding job not found")
    conn.commit()
    return {"id": jid, "status": row["status"]}


def _github_slug(repo_url: str) -> str:
    raw = str(repo_url or "").strip()
    if raw.startswith("git@"):
        path = raw.partition(":")[2]
    elif "://" in raw:
        host, _, path = raw.split("://", 1)[1].partition("/")
        if host.lower() not in {"github.com", "www.github.com"}:
            return raw
    else:
        path = raw
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) < 2:
        return raw
    repo = parts[1][:-4] if parts[1].endswith(".git") else parts[1]
    return f"{parts[0]}/{repo}".lower()


def _record_refs(cur, row: dict[str, Any], result: dict[str, Any]) -> list[str]:
    """Record the branch and PR a finished job produced against its program."""
    program_id = row.get("program_id")
    if not program_id:
        return []
    spec = row.get("spec") if isinstance(row.get("spec"), dict) else {}
    slug = _github_slug(str(result.get("repo_url") or row.get("repo_url") or ""))
    workflow_id = spec.get("record_workflow_id") or row.get("workflow_id")
    refs: list[tuple[str, str]] = []
    if result.get("branch"):
        refs.append(("branch", f"{slug}#{result['branch']}"))
    if result.get("pr_url"):
        refs.append(("pull_request", str(result["pr_url"])))
    metadata = json.dumps(
        {
            "repo": slug,
            "branch": result.get("branch"),
            "work_order_id": result.get("work_order_id") or row.get("work_order_id") or "",
            "created_by_harness": True,
            "coding_job_id": str(row["id"]),
        }
    )
    for kind, ref in refs:
        cur.execute(
            """
            INSERT INTO program_external_refs (id, tenant_id, program_id, workflow_id, kind, ref, state, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, 'open', %s::jsonb)
            ON CONFLICT (program_id, kind, ref) DO UPDATE
              SET workflow_id = COALESCE(EXCLUDED.workflow_id, program_external_refs.workflow_id),
                  metadata = program_external_refs.metadata || EXCLUDED.metadata,
                  updated_at = now()
            """,
            (str(uuid.uuid4()), row.get("tenant_id") or "default", program_id, workflow_id, kind, ref, metadata),
        )
        _audit(
            cur,
            "program_external_ref_recorded",
            f"Program external {kind} recorded",
            {"program_id": str(program_id), "workflow_id": str(workflow_id or ""), "kind": kind, "ref": ref, "state": "open"},
            "coding-worker",
        )
    return [ref for _, ref in refs]


def complete_coding_job(conn, job_id: str, body: dict[str, Any]) -> dict[str, Any]:
    jid = _uuid(job_id)
    result = body.get("result") if isinstance(body.get("result"), dict) else None
    error = str(body.get("error") or "").strip() or None
    if result is None:
        result = {"ok": False, "error": error or "coding worker returned no result"}
    status = "succeeded" if result.get("ok") is True else "failed"
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE coding_jobs
            SET status = %s, result = %s::jsonb, error = %s, finished_at = now(), heartbeat_at = now()
            WHERE id = %s AND status = ANY(%s)
            RETURNING *
            """,
            (status, json.dumps(result, default=str), error or result.get("error"), jid, list(RUNNING)),
        )
        row = cur.fetchone()
        if not row:
            cur.execute("SELECT * FROM coding_jobs WHERE id = %s", (jid,))
            row = cur.fetchone()
            if not row:
                raise LookupError("coding job not found")
            conn.commit()
            return {**_public(row), "ignored": True, "task_resumed": False}
        _audit(
            cur,
            "coding_job_finished",
            f"Coding job {status}",
            {"job_id": jid, "workflow_id": str(row.get("workflow_id") or ""), "status": status, "pr_url": result.get("pr_url")},
            "coding-worker",
        )
        refs = _record_refs(cur, row, result)
        resumed = _resume_task(cur, row.get("task_id"), jid, "coding-worker")
    conn.commit()
    return {**_public(row), "task_resumed": resumed, "refs_recorded": refs}


def _mark_lost(cur, row: dict[str, Any], reason: str, actor: str) -> dict[str, Any]:
    jid = str(row["id"])
    cur.execute(
        """
        UPDATE coding_jobs SET status = 'lost', error = %s, finished_at = now(), result = %s::jsonb
        WHERE id = %s RETURNING *
        """,
        (reason, json.dumps({"ok": False, "error": f"coding job lost: {reason}"}), jid),
    )
    lost = cur.fetchone()
    _audit(cur, "coding_job_lost", f"Coding job lost: {reason}", {"job_id": jid, "workflow_id": str(row.get("workflow_id") or ""), "reason": reason}, actor)
    _resume_task(cur, row.get("task_id"), jid, actor)
    return _public(lost)


def sweep_stale_coding_jobs(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Mark jobs lost when their heartbeat stopped or they ran too long."""
    heartbeat_seconds = int(body.get("heartbeat_seconds") or 300)
    max_runtime_seconds = int(body.get("max_runtime_seconds") or 3600)
    actor = str(body.get("actor") or "coding-watchdog")
    lost: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT *,
                   heartbeat_at < now() - make_interval(secs => %s) AS silent,
                   started_at < now() - make_interval(secs => %s) AS overtime
            FROM coding_jobs
            WHERE status = ANY(%s)
              AND (heartbeat_at < now() - make_interval(secs => %s)
                   OR started_at < now() - make_interval(secs => %s))
            FOR UPDATE SKIP LOCKED
            """,
            (heartbeat_seconds, max_runtime_seconds, list(RUNNING), heartbeat_seconds, max_runtime_seconds),
        )
        for row in cur.fetchall():
            if row.get("overtime"):
                reason = f"ran longer than {max_runtime_seconds // 60} minutes"
            else:
                reason = f"no heartbeat for {heartbeat_seconds // 60} minutes"
            lost.append(_mark_lost(cur, row, reason, actor))
        # A job can end before the runner has parked its task; pick those up here.
        cur.execute(
            """
            SELECT DISTINCT j.task_id, j.id
            FROM coding_jobs j
            JOIN workflow_tasks t ON t.id = j.task_id
            WHERE j.status = ANY(%s) AND j.consumed_at IS NULL
              AND t.status = 'blocked' AND t.error = %s
            """,
            (list(TERMINAL), WAIT_ERROR),
        )
        resumed = [str(r["task_id"]) for r in cur.fetchall() if _resume_task(cur, r["task_id"], str(r["id"]), actor)]
    conn.commit()
    return {"lost": lost, "resumed_tasks": resumed}


def cancel_coding_jobs(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Cancel active jobs by id or by workflow; returns them so the caller can stop workers."""
    job_ids = [_uuid(j) for j in body.get("job_ids") or []]
    workflow_ids = [_uuid(w, "workflow_id") for w in body.get("workflow_ids") or []]
    if not job_ids and not workflow_ids:
        raise ValueError("job_ids or workflow_ids is required")
    actor = str(body.get("actor") or "orchestrator")
    reason = str(body.get("reason") or "cancelled")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE coding_jobs
            SET status = 'cancelled', error = %s, finished_at = now()
            WHERE status = ANY(%s) AND (id = ANY(%s::uuid[]) OR workflow_id = ANY(%s::uuid[]))
            RETURNING *
            """,
            (reason, list(ACTIVE), job_ids, workflow_ids),
        )
        rows = list(cur.fetchall())
        for row in rows:
            _audit(cur, "coding_job_cancelled", f"Coding job cancelled: {reason}", {"job_id": str(row["id"]), "workflow_id": str(row.get("workflow_id") or "")}, actor)
    conn.commit()
    return {"cancelled": [_public(r) for r in rows]}


def consume_coding_job(conn, job_id: str) -> dict[str, Any]:
    jid = _uuid(job_id)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE coding_jobs SET consumed_at = COALESCE(consumed_at, now()) WHERE id = %s RETURNING id, consumed_at",
            (jid,),
        )
        row = cur.fetchone()
        if not row:
            raise LookupError("coding job not found")
    conn.commit()
    return {"id": jid, "consumed_at": row["consumed_at"]}


def list_coding_jobs(conn, query: dict[str, Any]) -> dict[str, Any]:
    clauses: list[str] = []
    params: list[Any] = []
    for key in ("workflow_id", "task_id"):
        if query.get(key):
            clauses.append(f"{key} = %s")
            params.append(_uuid(query[key], key))
    if query.get("status"):
        statuses = [s.strip() for s in str(query["status"]).split(",") if s.strip()]
        clauses.append("status = ANY(%s)")
        params.append(statuses)
    if str(query.get("unconsumed") or "").lower() in {"1", "true"}:
        clauses.append("consumed_at IS NULL")
    if query.get("finished_within"):
        try:
            seconds = int(query["finished_within"])
        except (TypeError, ValueError) as exc:
            raise ValueError("finished_within must be seconds") from exc
        clauses.append("finished_at > now() - make_interval(secs => %s)")
        params.append(seconds)
    try:
        limit = max(1, min(int(query.get("limit") or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM coding_jobs {where} ORDER BY created_at DESC LIMIT %s", [*params, limit])
        rows = list(cur.fetchall())
    return {"jobs": [_public(r) for r in rows]}
