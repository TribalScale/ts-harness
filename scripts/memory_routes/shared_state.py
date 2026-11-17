"""Shared runtime state: file trees with generations, locks, key-value state, audit events and channel dedup."""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

from psycopg.types.json import Jsonb

MAX_FILE_BYTES = 512 * 1024
MAX_NAMESPACE_BYTES = 20 * 1024 * 1024
MAX_LOCK_TTL_SECONDS = 600
DEDUP_RETENTION_DAYS = 7

_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class StaleGeneration(Exception):
    """The commit was based on an older generation than the namespace has now."""

    def __init__(self, current: int):
        super().__init__(f"namespace changed since generation was read (now {current})")
        self.current = current


def _namespace(value: Any) -> str:
    ns = str(value or "").strip()
    if not _NAMESPACE_RE.fullmatch(ns):
        raise ValueError("namespace must be a lowercase slug")
    return ns


def _path(value: Any) -> str:
    path = str(value or "").strip()
    parts = path.split("/")
    if not path or path.startswith("/") or "\\" in path or "\x00" in path or any(p in {"", ".", ".."} for p in parts):
        raise ValueError(f"invalid path: {path!r}")
    return path


def _key(value: Any, field: str = "key") -> str:
    key = str(value or "").strip()
    if not _KEY_RE.fullmatch(key):
        raise ValueError(f"invalid {field}: {key!r}")
    return key


def _generation(cur, namespace: str, *, lock: bool = False) -> int:
    cur.execute(
        "INSERT INTO shared_file_namespaces (namespace) VALUES (%s) ON CONFLICT (namespace) DO NOTHING",
        (namespace,),
    )
    cur.execute(
        f"SELECT generation FROM shared_file_namespaces WHERE namespace = %s{' FOR UPDATE' if lock else ''}",
        (namespace,),
    )
    return int(cur.fetchone()["generation"])


def bump_generation(cur, namespace: str) -> int:
    """Lock the namespace row, increment its generation and return the new value."""
    _generation(cur, namespace, lock=True)
    cur.execute(
        "UPDATE shared_file_namespaces SET generation = generation + 1, updated_at = now() WHERE namespace = %s RETURNING generation",
        (namespace,),
    )
    return int(cur.fetchone()["generation"])


def namespace_generation(conn, namespace: Any) -> dict[str, Any]:
    ns = _namespace(namespace)
    with conn.cursor() as cur:
        generation = _generation(cur, ns)
    conn.commit()
    return {"namespace": ns, "generation": generation}


def pull_files(conn, namespace: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Files changed after generation ``since`` (all live files when 0), with tombstones."""
    ns = _namespace(namespace)
    try:
        since = int(params.get("since") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("since must be a whole number") from exc
    with conn.cursor() as cur:
        generation = _generation(cur, ns)
        if since >= generation:
            files: list[dict[str, Any]] = []
        else:
            cur.execute(
                """
                SELECT path, content, sha256, size, generation, deleted
                  FROM shared_files
                 WHERE namespace = %s AND generation > %s AND (%s > 0 OR NOT deleted)
                 ORDER BY path
                """,
                (ns, since, since),
            )
            files = [
                {
                    "path": r["path"],
                    "content": None if r["deleted"] else r["content"],
                    "sha256": r["sha256"],
                    "size": int(r["size"]),
                    "generation": int(r["generation"]),
                    "deleted": bool(r["deleted"]),
                }
                for r in cur.fetchall()
            ]
    conn.commit()
    return {"namespace": ns, "generation": generation, "since": since, "files": files}


def commit_files(conn, namespace: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Apply puts and deletes in one transaction; refuse when ``base_generation`` is stale.

    With ``scope`` (path prefixes), only files under those prefixes count: several
    writers can each own part of one namespace without refusing each other.
    """
    ns = _namespace(namespace)
    actor = str(body.get("actor") or "").strip()[:200]
    puts: dict[str, str] = {}
    for item in body.get("puts") or []:
        path = _path(item.get("path"))
        content = item.get("content")
        if not isinstance(content, str):
            raise ValueError(f"content must be text: {path}")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError(f"file is larger than {MAX_FILE_BYTES // 1024} KB: {path}")
        puts[path] = content
    deletes = sorted({_path(p) for p in body.get("deletes") or []} - set(puts))
    base = body.get("base_generation")
    scope = [str(p).strip().strip("/") for p in body.get("scope") or [] if str(p).strip().strip("/")]
    for prefix in scope:
        _path(prefix)
    with conn.cursor() as cur:
        current = _generation(cur, ns, lock=True)
        if base is not None and scope:
            cur.execute(
                """
                SELECT COALESCE(max(generation), 0) AS latest FROM shared_files
                 WHERE namespace = %s AND (path = ANY(%s) OR path LIKE ANY(%s))
                """,
                (ns, scope, [p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%" for p in scope]),
            )
            if int(cur.fetchone()["latest"]) > int(base):
                conn.rollback()
                raise StaleGeneration(current)
        elif base is not None and int(base) != current:
            conn.rollback()
            raise StaleGeneration(current)
        if not puts and not deletes:
            conn.commit()
            return {"namespace": ns, "generation": current, "changed": 0}
        if puts:
            cur.execute(
                "SELECT COALESCE(sum(size), 0) AS total FROM shared_files WHERE namespace = %s AND NOT deleted AND NOT path = ANY(%s)",
                (ns, list(puts)),
            )
            total = int(cur.fetchone()["total"]) + sum(len(c.encode("utf-8")) for c in puts.values())
            if total > MAX_NAMESPACE_BYTES:
                conn.rollback()
                raise ValueError(f"namespace {ns} would exceed {MAX_NAMESPACE_BYTES // (1024 * 1024)} MB")
        generation = bump_generation(cur, ns)
        for path, content in puts.items():
            data = content.encode("utf-8")
            cur.execute(
                """
                INSERT INTO shared_files (namespace, path, content, sha256, size, generation, deleted, updated_at, updated_by)
                VALUES (%s, %s, %s, %s, %s, %s, false, now(), %s)
                ON CONFLICT (namespace, path) DO UPDATE
                   SET content = EXCLUDED.content, sha256 = EXCLUDED.sha256, size = EXCLUDED.size,
                       generation = EXCLUDED.generation, deleted = false,
                       updated_at = now(), updated_by = EXCLUDED.updated_by
                """,
                (ns, path, content, hashlib.sha256(data).hexdigest(), len(data), generation, actor),
            )
        if deletes:
            cur.execute(
                """
                UPDATE shared_files
                   SET deleted = true, content = '', sha256 = '', size = 0, generation = %s, updated_at = now(), updated_by = %s
                 WHERE namespace = %s AND path = ANY(%s) AND NOT deleted
                """,
                (generation, actor, ns, deletes),
            )
    conn.commit()
    return {"namespace": ns, "generation": generation, "changed": len(puts) + len(deletes)}


def acquire_lock(conn, namespace: Any, name: Any, body: dict[str, Any]) -> dict[str, Any]:
    ns, lock_name = _namespace(namespace), _key(name, "lock name")
    holder = str(body.get("holder") or "").strip()
    if not holder:
        raise ValueError("holder is required")
    ttl = min(max(1, int(body.get("ttl_seconds") or 30)), MAX_LOCK_TTL_SECONDS)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO shared_file_locks (namespace, name, holder, acquired_at, expires_at)
            VALUES (%s, %s, %s, now(), now() + make_interval(secs => %s))
            ON CONFLICT (namespace, name) DO UPDATE
               SET holder = EXCLUDED.holder,
                   acquired_at = CASE WHEN shared_file_locks.holder = EXCLUDED.holder
                                      THEN shared_file_locks.acquired_at ELSE now() END,
                   expires_at = EXCLUDED.expires_at
             WHERE shared_file_locks.holder = EXCLUDED.holder OR shared_file_locks.expires_at < now()
            RETURNING holder
            """,
            (ns, lock_name, holder, ttl),
        )
        row = cur.fetchone()
        current = holder
        if row is None:
            cur.execute("SELECT holder FROM shared_file_locks WHERE namespace = %s AND name = %s", (ns, lock_name))
            found = cur.fetchone()
            current = str(found["holder"]) if found else ""
    conn.commit()
    return {"locked": row is not None, "holder": current}


def release_lock(conn, namespace: Any, name: Any, body: dict[str, Any]) -> dict[str, Any]:
    ns, lock_name = _namespace(namespace), _key(name, "lock name")
    holder = str(body.get("holder") or "").strip()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM shared_file_locks WHERE namespace = %s AND name = %s AND holder = %s",
            (ns, lock_name, holder),
        )
        released = cur.rowcount > 0
    conn.commit()
    return {"released": released}


class VersionConflict(Exception):
    def __init__(self, current: int):
        super().__init__(f"value changed (now version {current})")
        self.current = current


def kv_get(conn, namespace: Any, key: Any) -> dict[str, Any]:
    ns, k = _namespace(namespace), _key(key)
    with conn.cursor() as cur:
        cur.execute("SELECT value, version, updated_at FROM runtime_kv WHERE namespace = %s AND key = %s", (ns, k))
        row = cur.fetchone()
    conn.commit()
    if row is None:
        return {"namespace": ns, "key": k, "value": None, "version": 0}
    return {"namespace": ns, "key": k, "value": row["value"], "version": int(row["version"]), "updated_at": row["updated_at"]}


def kv_put(conn, namespace: Any, key: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Write a value; with ``expected_version`` it only succeeds if nobody wrote since (0 means absent)."""
    ns, k = _namespace(namespace), _key(key)
    if "value" not in body:
        raise ValueError("value is required")
    value = body["value"]
    expected = body.get("expected_version")
    with conn.cursor() as cur:
        if expected is None:
            cur.execute(
                """
                INSERT INTO runtime_kv (namespace, key, value) VALUES (%s, %s, %s)
                ON CONFLICT (namespace, key) DO UPDATE
                   SET value = EXCLUDED.value, version = runtime_kv.version + 1, updated_at = now()
                RETURNING version
                """,
                (ns, k, Jsonb(value)),
            )
            row = cur.fetchone()
        elif int(expected) == 0:
            cur.execute(
                "INSERT INTO runtime_kv (namespace, key, value) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING version",
                (ns, k, Jsonb(value)),
            )
            row = cur.fetchone()
        else:
            cur.execute(
                """
                UPDATE runtime_kv SET value = %s, version = version + 1, updated_at = now()
                 WHERE namespace = %s AND key = %s AND version = %s
                RETURNING version
                """,
                (Jsonb(value), ns, k, int(expected)),
            )
            row = cur.fetchone()
        if row is None:
            cur.execute("SELECT version FROM runtime_kv WHERE namespace = %s AND key = %s", (ns, k))
            found = cur.fetchone()
            conn.rollback()
            raise VersionConflict(int(found["version"]) if found else 0)
    conn.commit()
    return {"namespace": ns, "key": k, "version": int(row["version"])}


def _json_safe(value: Any) -> Any:
    """Postgres json/jsonb rejects NUL. Drop it from text rather than failing the write."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key).replace("\x00", ""): _json_safe(item) for key, item in value.items()}
    return value


def append_audit_events(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Insert audit events; events already stored (same event_id) are skipped."""
    events = body.get("events")
    if events is None:
        events = [body]
    inserted = 0
    with conn.cursor() as cur:
        for event in events:
            try:
                event_id = str(uuid.UUID(str(event.get("event_id"))))
            except (TypeError, ValueError) as exc:
                raise ValueError("event_id must be a UUID") from exc
            tenant = _json_safe(str(event.get("tenant_id") or "")).strip()
            action = _json_safe(str(event.get("action_type") or "")).strip()
            actor = _json_safe(str(event.get("actor_id") or "")).strip()
            if not tenant or not action or not actor:
                raise ValueError("tenant_id, action_type and actor_id are required")
            correlation = event.get("correlation_id")
            if correlation is not None:
                correlation = _json_safe(str(correlation))
            cur.execute(
                """
                INSERT INTO runtime_audit_events (event_id, tenant_id, action_type, actor_id, correlation_id, payload, occurred_at)
                VALUES (%s, %s, %s, %s, %s, %s, COALESCE(%s::timestamptz, now()))
                ON CONFLICT (event_id) DO NOTHING
                """,
                (event_id, tenant, action, actor, correlation, Jsonb(_json_safe(event.get("payload") or {})),
                 event.get("timestamp") or None),
            )
            inserted += cur.rowcount
    conn.commit()
    return {"inserted": inserted, "received": len(events)}


def list_audit_events(conn, params: dict[str, Any]) -> dict[str, Any]:
    clauses, values = [], []
    if params.get("tenant_id"):
        clauses.append("tenant_id = %s")
        values.append(str(params["tenant_id"]))
    if params.get("since"):
        clauses.append("occurred_at >= %s::timestamptz")
        values.append(str(params["since"]))
    if params.get("until"):
        clauses.append("occurred_at <= %s::timestamptz")
        values.append(str(params["until"]))
    limit = min(max(1, int(params.get("limit") or 10000)), 50000)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT sequence, event_id, tenant_id, action_type, actor_id, correlation_id, payload, occurred_at
              FROM runtime_audit_events {where}
             ORDER BY sequence LIMIT %s
            """,
            (*values, limit),
        )
        rows = cur.fetchall()
    conn.commit()
    return {
        "events": [
            {
                "event_id": str(r["event_id"]),
                "sequence": int(r["sequence"]),
                "tenant_id": r["tenant_id"],
                "action_type": r["action_type"],
                "actor_id": r["actor_id"],
                "correlation_id": r["correlation_id"],
                "payload": r["payload"],
                "timestamp": r["occurred_at"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            for r in rows
        ]
    }


def mark_channel_event(conn, body: dict[str, Any]) -> dict[str, Any]:
    """``{"new": true}`` the first time a key is seen, ``false`` afterwards."""
    key = str(body.get("key") or "").strip()
    if not key or len(key) > 512:
        raise ValueError("key is required (at most 512 characters)")
    with conn.cursor() as cur:
        cur.execute("INSERT INTO channel_event_dedup (key) VALUES (%s) ON CONFLICT DO NOTHING", (key,))
        new = cur.rowcount > 0
    conn.commit()
    return {"new": new}


def sweep_channel_dedup(conn, body: dict[str, Any]) -> dict[str, Any]:
    days = max(1, int(body.get("retention_days") or DEDUP_RETENTION_DAYS))
    with conn.cursor() as cur:
        cur.execute("DELETE FROM channel_event_dedup WHERE seen_at < now() - make_interval(days => %s)", (days,))
        deleted = cur.rowcount
    conn.commit()
    return {"deleted": deleted}
