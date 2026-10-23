"""Projects and their package registries.

A project is keyed by ``(tenant_id, key)``; ``key`` matches the
``delivery_programs.project_id`` programs already carry. Registry credentials
are stored in the cloud secret store by the admin runtime; rows keep only the
reference. A coding worker reads its job's registries, with credentials, from
``GET /coding-jobs/{id}/registries`` using its job token.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Callable
from urllib.parse import urlparse

REGISTRY_TYPES = ("npm", "pypi", "maven", "docker")
WORKER_CPU = (1024, 2048, 4096, 8192, 16384)
# Fargate's allowed memory range (MiB) per CPU size.
_FARGATE_MEMORY = {1024: (2048, 8192), 2048: (4096, 16384), 4096: (8192, 30720), 8192: (16384, 61440), 16384: (32768, 122880)}
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _audit(cur, action_type: str, summary: str, details: dict[str, Any], actor: str) -> None:
    cur.execute(
        """
        INSERT INTO session_audit_log (id, session_id, agent_id, action_type, summary, details, occurred_at)
        VALUES (uuid_generate_v4(), %s, %s, %s, %s, %s::jsonb, now())
        """,
        (str(details.get("project_id") or "project"), actor, action_type, summary, json.dumps(details, default=str)),
    )


def _clean(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _optional_int(value: Any, field: str) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc


def _project_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "tenant_id": row["tenant_id"],
        "key": row["key"],
        "name": row["name"],
        "team_id": str(row["team_id"]) if row.get("team_id") else None,
        "team_slug": row.get("team_slug"),
        "jira_project_key": row.get("jira_project_key"),
        "default_base_branch": row.get("default_base_branch"),
        "worker_cpu": row.get("worker_cpu"),
        "worker_memory_mb": row.get("worker_memory_mb"),
        "registry_count": int(row.get("registry_count") or 0),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _registry_json(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "type": row["type"],
        "url": row["url"],
        "scope": row.get("scope"),
        "username": row.get("username"),
        "has_credential": bool(row.get("secret_arn")),
        "secret_arn": row.get("secret_arn"),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_tested_at": row.get("last_tested_at"),
        "last_test_ok": row.get("last_test_ok"),
        "last_test_error": row.get("last_test_error"),
    }


_PROJECT_SELECT = """
    SELECT p.*, t.slug AS team_slug,
           (SELECT count(*) FROM project_registries r WHERE r.project_id = p.id) AS registry_count
      FROM projects p
      LEFT JOIN teams t ON t.id = p.team_id
"""


def _team_id(cur, tenant_id: str, team_slug: Any) -> str | None:
    slug = _clean(team_slug)
    if slug is None:
        return None
    cur.execute("SELECT id FROM teams WHERE tenant_id = %s AND slug = %s", (tenant_id, slug))
    row = cur.fetchone()
    if not row:
        raise ValueError(f"unknown team: {slug}")
    return str(row["id"])


def _sizing(body: dict[str, Any]) -> tuple[int | None, int | None]:
    cpu = _optional_int(body.get("worker_cpu"), "worker_cpu")
    memory = _optional_int(body.get("worker_memory_mb"), "worker_memory_mb")
    if (cpu is None) != (memory is None):
        raise ValueError("set worker_cpu and worker_memory_mb together, or neither")
    if cpu is None or memory is None:
        return None, None
    if cpu not in WORKER_CPU:
        raise ValueError(f"worker_cpu must be one of {', '.join(map(str, WORKER_CPU))}")
    low, high = _FARGATE_MEMORY[cpu]
    if not low <= memory <= high:
        raise ValueError(f"worker_memory_mb for {cpu} CPU units must be between {low} and {high}")
    return cpu, memory


def list_projects(conn, query: dict[str, Any]) -> dict[str, Any]:
    tenant_id = str(query.get("tenant_id") or "default")
    params: list[Any] = [tenant_id]
    where = "WHERE p.tenant_id = %s"
    if query.get("key"):
        where += " AND p.key = %s"
        params.append(str(query["key"]))
    if query.get("team"):
        where += " AND t.slug = %s"
        params.append(str(query["team"]))
    with conn.cursor() as cur:
        cur.execute(f"{_PROJECT_SELECT} {where} ORDER BY p.name, p.key", params)
        rows = cur.fetchall()
    return {"projects": [_project_json(r) for r in rows]}


def get_project(conn, project_id: str) -> dict[str, Any]:
    pid = _uuid(project_id, "project_id")
    with conn.cursor() as cur:
        cur.execute(f"{_PROJECT_SELECT} WHERE p.id = %s", (pid,))
        row = cur.fetchone()
    if not row:
        raise LookupError("project not found")
    return _project_json(row)


def create_project(conn, body: dict[str, Any]) -> dict[str, Any]:
    tenant_id = str(body.get("tenant_id") or "default")
    key = str(body.get("key") or "").strip()
    if not _KEY_RE.fullmatch(key):
        raise ValueError("key must be 1-64 letters, digits, dots, dashes or underscores")
    name = _clean(body.get("name")) or key
    cpu, memory = _sizing(body)
    actor = str(body.get("actor") or "admin")
    with conn.cursor() as cur:
        team_id = _team_id(cur, tenant_id, body.get("team_slug"))
        cur.execute(
            """
            INSERT INTO projects (tenant_id, team_id, key, name, jira_project_key, default_base_branch, worker_cpu, worker_memory_mb)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, key) DO NOTHING
            RETURNING id
            """,
            (tenant_id, team_id, key, name, _clean(body.get("jira_project_key")),
             _clean(body.get("default_base_branch")), cpu, memory),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"project {key} already exists")
        pid = str(row["id"])
        cur.execute(
            "UPDATE delivery_programs SET project_ref = %s WHERE tenant_id = %s AND project_id = %s AND project_ref IS NULL",
            (pid, tenant_id, key),
        )
        _audit(cur, "project_created", f"Project {key} created", {"project_id": pid, "key": key}, actor)
    conn.commit()
    return get_project(conn, pid)


_PROJECT_FIELDS = ("name", "jira_project_key", "default_base_branch")


def update_project(conn, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
    current = get_project(conn, project_id)
    sets: list[str] = []
    params: list[Any] = []
    for field in _PROJECT_FIELDS:
        if field in body:
            value = _clean(body.get(field))
            if field == "name" and value is None:
                raise ValueError("name cannot be empty")
            sets.append(f"{field} = %s")
            params.append(value)
    if "worker_cpu" in body or "worker_memory_mb" in body:
        cpu, memory = _sizing(
            {
                "worker_cpu": body.get("worker_cpu", current["worker_cpu"]),
                "worker_memory_mb": body.get("worker_memory_mb", current["worker_memory_mb"]),
            }
        )
        sets += ["worker_cpu = %s", "worker_memory_mb = %s"]
        params += [cpu, memory]
    actor = str(body.get("actor") or "admin")
    with conn.cursor() as cur:
        if "team_slug" in body:
            sets.append("team_id = %s")
            params.append(_team_id(cur, current["tenant_id"], body.get("team_slug")))
        if not sets:
            return current
        cur.execute(
            f"UPDATE projects SET {', '.join(sets)}, updated_at = now() WHERE id = %s",
            (*params, current["id"]),
        )
        changed = sorted(k for k in body if k in (*_PROJECT_FIELDS, "worker_cpu", "worker_memory_mb", "team_slug"))
        _audit(cur, "project_updated", f"Project {current['key']} updated", {"project_id": current["id"], "fields": changed}, actor)
    conn.commit()
    return get_project(conn, current["id"])


def normalize_registry_url(registry_type: str, url: Any) -> str:
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("url is required")
    if "://" not in raw and registry_type == "docker":
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("url must be an https URL")
    if parsed.username or parsed.password:
        raise ValueError("put credentials in the credential field, not the url")
    return raw.rstrip("/")


def registry_host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _registry_row(cur, project_id: str, registry_id: str) -> dict[str, Any]:
    cur.execute(
        "SELECT * FROM project_registries WHERE id = %s AND project_id = %s",
        (_uuid(registry_id, "registry_id"), project_id),
    )
    row = cur.fetchone()
    if not row:
        raise LookupError("registry not found")
    return row


def list_registries(conn, project_id: str) -> dict[str, Any]:
    pid = get_project(conn, project_id)["id"]
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM project_registries WHERE project_id = %s ORDER BY type, url", (pid,))
        rows = cur.fetchall()
    return {"registries": [_registry_json(r) for r in rows]}


def create_registry(conn, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
    pid = get_project(conn, project_id)["id"]
    registry_type = str(body.get("type") or "").strip().lower()
    if registry_type not in REGISTRY_TYPES:
        raise ValueError(f"type must be one of {', '.join(REGISTRY_TYPES)}")
    url = normalize_registry_url(registry_type, body.get("url"))
    actor = str(body.get("actor") or "admin")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO project_registries (project_id, type, url, scope, username, secret_arn, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, type, url) DO NOTHING
            RETURNING *
            """,
            (pid, registry_type, url, _clean(body.get("scope")), _clean(body.get("username")),
             _clean(body.get("secret_arn")), actor),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"{registry_type} registry {url} already exists for this project")
        _audit(
            cur,
            "registry_added",
            f"{registry_type} registry added",
            {"project_id": pid, "registry_id": str(row["id"]), "type": registry_type, "host": registry_host(url)},
            actor,
        )
    conn.commit()
    return _registry_json(row)


def update_registry(conn, project_id: str, registry_id: str, body: dict[str, Any]) -> dict[str, Any]:
    pid = get_project(conn, project_id)["id"]
    actor = str(body.get("actor") or "admin")
    with conn.cursor() as cur:
        row = _registry_row(cur, pid, registry_id)
        url = normalize_registry_url(row["type"], body["url"]) if "url" in body else row["url"]
        scope = _clean(body.get("scope")) if "scope" in body else row.get("scope")
        username = _clean(body.get("username")) if "username" in body else row.get("username")
        secret_arn = _clean(body.get("secret_arn")) if "secret_arn" in body else row.get("secret_arn")
        cur.execute(
            """
            UPDATE project_registries
               SET url = %s, scope = %s, username = %s, secret_arn = %s, updated_at = now(),
                   last_tested_at = NULL, last_test_ok = NULL, last_test_error = NULL
             WHERE id = %s
            RETURNING *
            """,
            (url, scope, username, secret_arn, row["id"]),
        )
        updated = cur.fetchone()
        changed = sorted(k for k in body if k in ("url", "scope", "username", "secret_arn"))
        _audit(
            cur,
            "registry_updated",
            f"{row['type']} registry updated",
            {"project_id": pid, "registry_id": str(row["id"]), "fields": changed, "host": registry_host(url)},
            actor,
        )
    conn.commit()
    return _registry_json(updated)


def delete_registry(conn, project_id: str, registry_id: str, body: dict[str, Any]) -> dict[str, Any]:
    pid = get_project(conn, project_id)["id"]
    actor = str(body.get("actor") or "admin")
    with conn.cursor() as cur:
        row = _registry_row(cur, pid, registry_id)
        cur.execute("DELETE FROM project_registries WHERE id = %s", (row["id"],))
        _audit(
            cur,
            "registry_deleted",
            f"{row['type']} registry deleted",
            {"project_id": pid, "registry_id": str(row["id"]), "type": row["type"], "host": registry_host(row["url"])},
            actor,
        )
    conn.commit()
    return _registry_json(row)


_REPO_PART_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_HOST_RE = re.compile(r"^[a-z0-9.-]+(:[0-9]+)?$")


def _repository_json(row: dict[str, Any]) -> dict[str, Any]:
    host = row.get("github_host") or "github.com"
    full_name = f"{row['owner']}/{row['repo']}"
    return {
        "id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "github_host": host,
        "owner": row["owner"],
        "repo": row["repo"],
        "full_name": full_name,
        "repo_url": f"https://{host}/{full_name}",
        "installation_id": row.get("installation_id"),
        "default_branch": row.get("default_branch"),
        "role": row.get("role"),
        "created_at": row.get("created_at"),
    }


def _repository_parts(body: dict[str, Any]) -> tuple[str, str, str]:
    host = str(body.get("github_host") or "github.com").strip().lower()
    owner = str(body.get("owner") or "").strip()
    repo = str(body.get("repo") or "").strip()
    full_name = str(body.get("full_name") or "").strip()
    if full_name and not (owner and repo):
        owner, _, repo = full_name.partition("/")
    repo = repo.removesuffix(".git")
    if not _HOST_RE.fullmatch(host):
        raise ValueError("github_host must be a hostname")
    if not (_REPO_PART_RE.fullmatch(owner) and _REPO_PART_RE.fullmatch(repo)):
        raise ValueError("repository must be owner/name")
    return host, owner, repo


def list_repositories(conn, project_id: str) -> dict[str, Any]:
    pid = get_project(conn, project_id)["id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM project_repositories WHERE project_id = %s ORDER BY github_host, lower(owner), lower(repo)",
            (pid,),
        )
        rows = cur.fetchall()
    return {"repositories": [_repository_json(r) for r in rows]}


def create_repository(conn, project_id: str, body: dict[str, Any]) -> dict[str, Any]:
    pid = get_project(conn, project_id)["id"]
    host, owner, repo = _repository_parts(body)
    actor = str(body.get("actor") or "admin")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO project_repositories (project_id, github_host, owner, repo, installation_id, default_branch, role, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING *
            """,
            (pid, host, owner, repo, _optional_int(body.get("installation_id"), "installation_id"),
             _clean(body.get("default_branch")), _clean(body.get("role")), actor),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"{owner}/{repo} is already in this project")
        _audit(cur, "project_updated", f"Repository {owner}/{repo} added",
               {"project_id": pid, "repository": f"{host}/{owner}/{repo}", "change": "repository_added"}, actor)
    conn.commit()
    return _repository_json(row)


def update_repository(conn, project_id: str, repository_id: str, body: dict[str, Any]) -> dict[str, Any]:
    pid = get_project(conn, project_id)["id"]
    actor = str(body.get("actor") or "admin")
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE project_repositories
               SET default_branch = CASE WHEN %s THEN %s ELSE default_branch END,
                   role = CASE WHEN %s THEN %s ELSE role END,
                   updated_at = now()
             WHERE id = %s AND project_id = %s
            RETURNING *
            """,
            ("default_branch" in body, _clean(body.get("default_branch")), "role" in body, _clean(body.get("role")),
             _uuid(repository_id, "repository_id"), pid),
        )
        row = cur.fetchone()
        if not row:
            raise LookupError("repository not found")
        _audit(cur, "project_updated", f"Repository {row['owner']}/{row['repo']} updated",
               {"project_id": pid, "repository_id": str(row["id"]), "change": "repository_updated"}, actor)
    conn.commit()
    return _repository_json(row)


def delete_repository(conn, project_id: str, repository_id: str, body: dict[str, Any]) -> dict[str, Any]:
    pid = get_project(conn, project_id)["id"]
    actor = str(body.get("actor") or "admin")
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM project_repositories WHERE id = %s AND project_id = %s RETURNING *",
            (_uuid(repository_id, "repository_id"), pid),
        )
        row = cur.fetchone()
        if not row:
            raise LookupError("repository not found")
        _audit(cur, "project_updated", f"Repository {row['owner']}/{row['repo']} removed",
               {"project_id": pid, "repository_id": str(row["id"]), "change": "repository_removed"}, actor)
    conn.commit()
    return _repository_json(row)


def record_registry_test(conn, project_id: str, registry_id: str, body: dict[str, Any]) -> dict[str, Any]:
    pid = get_project(conn, project_id)["id"]
    ok = body.get("ok") is True
    error = None if ok else (_clean(body.get("error")) or "test failed")[:500]
    actor = str(body.get("actor") or "admin")
    with conn.cursor() as cur:
        row = _registry_row(cur, pid, registry_id)
        cur.execute(
            """
            UPDATE project_registries SET last_tested_at = now(), last_test_ok = %s, last_test_error = %s
             WHERE id = %s RETURNING *
            """,
            (ok, error, row["id"]),
        )
        updated = cur.fetchone()
        _audit(
            cur,
            "registry_tested",
            f"{row['type']} registry test {'passed' if ok else 'failed'}",
            {"project_id": pid, "registry_id": str(row["id"]), "ok": ok, "error": error},
            actor,
        )
    conn.commit()
    return _registry_json(updated)


def registry_hosts(conn) -> dict[str, Any]:
    """Every registry host across tenants, for the shared egress proxy allowlist."""
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT url FROM project_registries")
        hosts = sorted({registry_host(r["url"]) for r in cur.fetchall()} - {""})
    return {"hosts": hosts}


def _aws_secret(secret_id: str) -> str:
    import boto3

    region = os.environ.get("CLOUD_REGION") or os.environ.get("AWS_REGION") or None
    response = boto3.client("secretsmanager", region_name=region).get_secret_value(SecretId=secret_id)
    return str(response.get("SecretString") or "")


def job_registries(
    conn, job_id: str, *, fetch_secret: Callable[[str], str] = _aws_secret
) -> dict[str, Any]:
    """The registries, with credentials, for one coding job's project."""
    jid = _uuid(job_id, "job_id")
    with conn.cursor() as cur:
        cur.execute("SELECT tenant_id, project_id, status FROM coding_jobs WHERE id = %s", (jid,))
        job = cur.fetchone()
        if not job:
            raise LookupError("coding job not found")
        if job["status"] not in ("starting", "running"):
            raise ValueError("coding job is not running")
        if not job.get("project_id"):
            return {"registries": []}
        cur.execute(
            """
            SELECT r.* FROM project_registries r
              JOIN projects p ON p.id = r.project_id
             WHERE p.tenant_id = %s AND p.key = %s
             ORDER BY r.type, r.url
            """,
            (job["tenant_id"], job["project_id"]),
        )
        rows = cur.fetchall()
    registries: list[dict[str, Any]] = []
    for row in rows:
        entry = {"type": row["type"], "url": row["url"], "scope": row.get("scope"), "username": row.get("username")}
        if row.get("secret_arn"):
            raw = fetch_secret(row["secret_arn"])
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"token": raw}
            entry["token"] = str(payload.get("token") or "")
            entry["username"] = payload.get("username") or entry["username"]
        registries.append(entry)
    return {"registries": registries}
