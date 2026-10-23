"""GitHub App registrations, installations and processed webhook deliveries."""

from __future__ import annotations

import json
import re
from typing import Any

from psycopg.types.json import Jsonb

_HOST_RE = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?$")
DELIVERY_RETENTION_DAYS = 30


def _host(value: Any) -> str:
    host = str(value or "github.com").strip().lower()
    if not _HOST_RE.fullmatch(host):
        raise ValueError("host must be a hostname such as github.com or github.acme.dev")
    return host


def _audit(cur, action_type: str, summary: str, details: dict[str, Any], actor: str) -> None:
    cur.execute(
        """
        INSERT INTO session_audit_log (id, session_id, agent_id, action_type, summary, details, occurred_at)
        VALUES (uuid_generate_v4(), 'github', %s, %s, %s, %s::jsonb, now())
        """,
        (actor, action_type, summary, json.dumps(details, default=str)),
    )


def _registration_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": row["host"],
        "app_id": row["app_id"],
        "app_slug": row.get("app_slug"),
        "private_key_secret_arn": row["private_key_secret_arn"],
        "webhook_secret_arn": row.get("webhook_secret_arn"),
        "created_by": row.get("created_by"),
        "updated_at": row.get("updated_at"),
    }


def list_registrations(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM github_app_registrations ORDER BY host")
        return {"registrations": [_registration_json(r) for r in cur.fetchall()]}


def get_registration(conn, host: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM github_app_registrations WHERE host = %s", (_host(host),))
        row = cur.fetchone()
    if not row:
        raise LookupError("no GitHub App registered for this host")
    return _registration_json(row)


def put_registration(conn, host: str, body: dict[str, Any]) -> dict[str, Any]:
    host = _host(host)
    app_id = str(body.get("app_id") or "").strip()
    key_arn = str(body.get("private_key_secret_arn") or "").strip()
    if not app_id.isdigit():
        raise ValueError("app_id must be the numeric App ID")
    if not key_arn:
        raise ValueError("private_key_secret_arn is required")
    actor = str(body.get("actor") or "admin")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO github_app_registrations (host, app_id, app_slug, private_key_secret_arn, webhook_secret_arn, created_by)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (host) DO UPDATE
               SET app_id = EXCLUDED.app_id, app_slug = EXCLUDED.app_slug,
                   private_key_secret_arn = EXCLUDED.private_key_secret_arn,
                   webhook_secret_arn = EXCLUDED.webhook_secret_arn, updated_at = now()
            """,
            (host, app_id, str(body.get("app_slug") or "").strip() or None, key_arn,
             str(body.get("webhook_secret_arn") or "").strip() or None, actor),
        )
        _audit(cur, "github_registration_updated", f"GitHub App {app_id} registered for {host}",
               {"host": host, "app_id": app_id}, actor)
    conn.commit()
    return get_registration(conn, host)


def _installation_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": row["host"],
        "installation_id": int(row["installation_id"]),
        "account_login": row["account_login"],
        "account_type": row["account_type"],
        "repository_selection": row["repository_selection"],
        "suspended_at": row.get("suspended_at"),
        "repositories": list(row.get("repositories") or []),
        "synced_at": row.get("synced_at"),
    }


def list_installations(conn, query: dict[str, Any]) -> dict[str, Any]:
    params: list[Any] = []
    where = ""
    if query.get("host"):
        where = "WHERE host = %s"
        params.append(_host(query["host"]))
    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM github_installations {where} ORDER BY host, lower(account_login)", params)
        return {"installations": [_installation_json(r) for r in cur.fetchall()]}


def upsert_installation(conn, body: dict[str, Any]) -> dict[str, Any]:
    host = _host(body.get("host"))
    try:
        installation_id = int(body.get("installation_id"))
    except (TypeError, ValueError) as exc:
        raise ValueError("installation_id is required") from exc
    login = str(body.get("account_login") or "").strip()
    if not login:
        raise ValueError("account_login is required")
    repositories = sorted({str(r) for r in body.get("repositories") or []})
    actor = str(body.get("actor") or "github-webhook")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO github_installations
                (host, installation_id, account_login, account_type, repository_selection, suspended_at, repositories, synced_at)
            VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, %s, now())
            ON CONFLICT (host, installation_id) DO UPDATE
               SET account_login = EXCLUDED.account_login,
                   account_type = EXCLUDED.account_type,
                   repository_selection = EXCLUDED.repository_selection,
                   suspended_at = CASE WHEN %s THEN COALESCE(github_installations.suspended_at, now()) END,
                   repositories = EXCLUDED.repositories,
                   synced_at = now()
            RETURNING *
            """,
            (host, installation_id, login, str(body.get("account_type") or "Organization"),
             str(body.get("repository_selection") or "selected"), bool(body.get("suspended")),
             Jsonb(repositories), bool(body.get("suspended"))),
        )
        row = cur.fetchone()
        _audit(cur, "github_installation_changed", f"GitHub App installation on {login} synced",
               {"host": host, "installation_id": installation_id, "suspended": bool(body.get("suspended")),
                "repositories": len(repositories)}, actor)
    conn.commit()
    return _installation_json(row)


def change_installation_repositories(conn, host: str, installation_id: str, body: dict[str, Any]) -> dict[str, Any]:
    host = _host(host)
    added = {str(r) for r in body.get("added") or []}
    removed = {str(r) for r in body.get("removed") or []}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM github_installations WHERE host = %s AND installation_id = %s FOR UPDATE",
            (host, int(installation_id)),
        )
        row = cur.fetchone()
        if not row:
            raise LookupError("installation not found")
        repositories = sorted((set(row.get("repositories") or []) | added) - removed)
        cur.execute(
            "UPDATE github_installations SET repositories = %s, synced_at = now() WHERE host = %s AND installation_id = %s RETURNING *",
            (Jsonb(repositories), host, int(installation_id)),
        )
        row = cur.fetchone()
        _audit(cur, "github_installation_changed", f"GitHub App repositories changed on {row['account_login']}",
               {"host": host, "installation_id": int(installation_id), "added": sorted(added), "removed": sorted(removed)},
               str(body.get("actor") or "github-webhook"))
    conn.commit()
    return _installation_json(row)


def delete_installation(conn, host: str, installation_id: str, body: dict[str, Any]) -> dict[str, Any]:
    host = _host(host)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM github_installations WHERE host = %s AND installation_id = %s RETURNING *",
            (host, int(installation_id)),
        )
        row = cur.fetchone()
        if not row:
            raise LookupError("installation not found")
        _audit(cur, "github_installation_changed", f"GitHub App uninstalled from {row['account_login']}",
               {"host": host, "installation_id": int(installation_id), "deleted": True},
               str(body.get("actor") or "github-webhook"))
    conn.commit()
    return {**_installation_json(row), "deleted": True}


def record_delivery(conn, body: dict[str, Any]) -> dict[str, Any]:
    """Remember a webhook delivery; ``duplicate`` is true when it was already processed."""
    delivery_id = str(body.get("delivery_id") or "").strip()
    if not delivery_id or len(delivery_id) > 128:
        raise ValueError("delivery_id is required")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO github_webhook_deliveries (delivery_id, host, event) VALUES (%s, %s, %s)
            ON CONFLICT (delivery_id) DO NOTHING
            RETURNING delivery_id
            """,
            (delivery_id, _host(body.get("host")), str(body.get("event") or "")[:64]),
        )
        duplicate = cur.fetchone() is None
        cur.execute(
            "DELETE FROM github_webhook_deliveries WHERE received_at < now() - make_interval(days => %s)",
            (DELIVERY_RETENTION_DAYS,),
        )
    conn.commit()
    return {"delivery_id": delivery_id, "duplicate": duplicate}


def delivery_seen(conn, delivery_id: str) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM github_webhook_deliveries WHERE delivery_id = %s", (str(delivery_id),))
        return {"delivery_id": delivery_id, "seen": cur.fetchone() is not None}


def webhook_status(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT host, max(received_at) AS last_received_at, count(*) FILTER (WHERE received_at > now() - interval '1 day') AS last_day
              FROM github_webhook_deliveries GROUP BY host ORDER BY host
            """
        )
        return {"hosts": [dict(r) for r in cur.fetchall()]}
