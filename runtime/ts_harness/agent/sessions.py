"""Runtime session store with routing keys and subagent lineage."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any
import hashlib
import json
import os
import time
import uuid


class _DirectConnectPool:
    """Minimal shim matching the psycopg_pool.ConnectionPool.connection() API.

    Used as a fallback when psycopg_pool is not installed.  Each call to
    ``connection()`` opens and closes a raw psycopg connection.
    """

    def __init__(self, db_url: str) -> None:
        self._db_url = db_url

    def connection(self):
        import psycopg

        return psycopg.connect(self._db_url, connect_timeout=5)

    def close(self) -> None:
        pass


@dataclass
class SessionRecord:
    session_id: str
    routing_key: str
    tenant_id: str
    agent_id: str
    source: str
    parent_session_id: str | None = None
    subagent_id: str | None = None
    depth: int = 0
    status: str = "active"
    started_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    tool_count: int = 0
    compression_parent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SessionStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        db_url: str | None = None,
        pool_min: int = 1,
        pool_max: int = 5,
    ):
        self._by_routing: dict[str, SessionRecord] = {}
        self._by_id: dict[str, SessionRecord] = {}
        self._lock = RLock()
        self.path = Path(path) if path is not None else None
        self.db_url = db_url
        self._postgres_loaded = False
        self._pool_min = pool_min
        self._pool_max = pool_max
        self._pool: Any = None  # psycopg_pool.ConnectionPool, created lazily
        if self.path and self.path.exists():
            self._load()

    def routing_key(
        self,
        *,
        tenant_id: str,
        source: str,
        channel: str | None = None,
        thread_id: str | None = None,
        workflow_id: str | None = None,
        agent_id: str | None = None,
    ) -> str:
        parts = [tenant_id, source]
        if channel:
            parts.extend(["channel", channel])
        if thread_id:
            parts.append(thread_id)
        if workflow_id:
            parts.extend(["workflow", workflow_id])
        if agent_id:
            parts.append(agent_id)
        digest = hashlib.sha256(":".join(parts).encode()).hexdigest()[:16]
        return f"{source}:{digest}"

    def get_or_create(
        self,
        *,
        tenant_id: str,
        source: str,
        agent_id: str,
        channel: str | None = None,
        thread_id: str | None = None,
        workflow_id: str | None = None,
        parent_session_id: str | None = None,
        subagent_id: str | None = None,
        depth: int = 0,
    ) -> SessionRecord:
        key = self.routing_key(
            tenant_id=tenant_id,
            source=source,
            channel=channel,
            thread_id=thread_id,
            workflow_id=workflow_id,
            agent_id=agent_id,
        )
        with self._lock:
            if self.db_url:
                self._ensure_postgres_loaded()
            existing = self._by_routing.get(key)
            if existing:
                return existing
            session = SessionRecord(
                session_id=str(uuid.uuid4()),
                routing_key=key,
                tenant_id=tenant_id,
                agent_id=agent_id,
                source=source,
                parent_session_id=parent_session_id,
                subagent_id=subagent_id,
                depth=depth,
            )
            self._by_routing[key] = session
            self._by_id[session.session_id] = session
            self.save(session)
            return session

    def create_subagent_session(self, parent: SessionRecord, subagent_id: str) -> SessionRecord:
        return self.get_or_create(
            tenant_id=parent.tenant_id,
            source="subagent",
            agent_id=subagent_id,
            parent_session_id=parent.session_id,
            subagent_id=subagent_id,
            depth=parent.depth + 1,
        )

    def get(self, session_id: str) -> SessionRecord | None:
        if self.db_url:
            self._ensure_postgres_loaded()
        return self._by_id.get(session_id)

    def list_for_tenant(self, tenant_id: str) -> list[SessionRecord]:
        if self.db_url:
            self._ensure_postgres_loaded()
        return [session for session in self._by_id.values() if session.tenant_id == tenant_id]

    def save(self, session: SessionRecord) -> None:
        with self._lock:
            self._by_routing[session.routing_key] = session
            self._by_id[session.session_id] = session
        if self.db_url:
            self._ensure_postgres_loaded()
            self._save_postgres(session)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(self._to_dict(session), sort_keys=True) + "\n")

    def _load(self) -> None:
        assert self.path is not None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            session = SessionRecord(**row)
            self._by_routing[session.routing_key] = session
            self._by_id[session.session_id] = session

    def _ensure_postgres_loaded(self) -> None:
        if not self.db_url or self._postgres_loaded:
            return
        self._load_postgres()
        self._postgres_loaded = True

    def _get_pool(self) -> Any:
        """Return (lazily initialising) a psycopg_pool.ConnectionPool.

        Falls back to a thin ``_DirectConnect`` shim when psycopg_pool is not
        installed so callers can use the same context-manager API regardless.
        """
        if self._pool is not None:
            return self._pool
        if not self.db_url:
            raise RuntimeError("db_url is required for PostgreSQL session store")
        try:
            from psycopg_pool import ConnectionPool  # type: ignore[import]

            self._pool = ConnectionPool(
                self.db_url,
                min_size=self._pool_min,
                max_size=self._pool_max,
                open=True,
                max_lifetime=1800,
                check=ConnectionPool.check_connection,
                kwargs={"connect_timeout": 5},
            )
        except ImportError:
            # Fallback: psycopg_pool not installed – create a shim backed by
            # direct psycopg connections so existing code still works.
            self._pool = _DirectConnectPool(self.db_url)
        return self._pool

    def _connect(self):
        """Return a pooled connection context manager."""
        return self._get_pool().connection()

    def _load_postgres(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT session_id, routing_key, tenant_id, agent_id, source,
                           parent_session_id, subagent_id, depth, status, started_at,
                           tool_count, compression_parent_id, metadata
                    FROM runtime_sessions
                    """
                )
                for row in cur.fetchall():
                    session = SessionRecord(
                        session_id=row[0],
                        routing_key=row[1],
                        tenant_id=row[2],
                        agent_id=row[3],
                        source=row[4],
                        parent_session_id=row[5],
                        subagent_id=row[6],
                        depth=row[7],
                        status=row[8],
                        started_at=row[9].isoformat().replace("+00:00", "Z") if hasattr(row[9], "isoformat") else str(row[9]),
                        tool_count=row[10],
                        compression_parent_id=row[11],
                        metadata=row[12] or {},
                    )
                    self._by_routing[session.routing_key] = session
                    self._by_id[session.session_id] = session
            # psycopg_pool connections are returned to the pool on __exit__;
            # explicit commit is a no-op for read-only workloads but kept for
            # compatibility with the direct-connect shim.
            conn.commit()

    def _save_postgres(self, session: SessionRecord) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO runtime_sessions (
                        session_id, routing_key, tenant_id, agent_id, source,
                        parent_session_id, subagent_id, depth, status, started_at,
                        tool_count, compression_parent_id, metadata, updated_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (session_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        tool_count = EXCLUDED.tool_count,
                        compression_parent_id = EXCLUDED.compression_parent_id,
                        metadata = EXCLUDED.metadata,
                        updated_at = NOW()
                    """,
                    (
                        session.session_id,
                        session.routing_key,
                        session.tenant_id,
                        session.agent_id,
                        session.source,
                        session.parent_session_id,
                        session.subagent_id,
                        session.depth,
                        session.status,
                        session.started_at,
                        session.tool_count,
                        session.compression_parent_id,
                        json.dumps(session.metadata),
                    ),
                )
            conn.commit()

    def _to_dict(self, session: SessionRecord) -> dict[str, Any]:
        return {
            "session_id": session.session_id,
            "routing_key": session.routing_key,
            "tenant_id": session.tenant_id,
            "agent_id": session.agent_id,
            "source": session.source,
            "parent_session_id": session.parent_session_id,
            "subagent_id": session.subagent_id,
            "depth": session.depth,
            "status": session.status,
            "started_at": session.started_at,
            "tool_count": session.tool_count,
            "compression_parent_id": session.compression_parent_id,
            "metadata": session.metadata,
        }


def build_session_store(db_url: str | None = None) -> SessionStore:
    resolved = (db_url or "").strip() or os.environ.get("TS_HARNESS_SESSION_DB_URL")
    if resolved:
        pool_min = int(os.environ.get("TS_HARNESS_SESSION_POOL_MIN", "1"))
        pool_max = int(os.environ.get("TS_HARNESS_SESSION_POOL_MAX", "5"))
        return SessionStore(db_url=resolved, pool_min=pool_min, pool_max=pool_max)
    path = os.environ.get("TS_HARNESS_SESSION_STORE")
    return SessionStore(path=path or None)


_default_store: SessionStore | None = None


def get_session_store() -> SessionStore:
    global _default_store
    if _default_store is None:
        _default_store = build_session_store()
    return _default_store


def reset_session_store(store: SessionStore | None = None) -> None:
    global _default_store
    _default_store = store or build_session_store()


def session_status_handler(args: dict[str, Any], ctx) -> dict[str, Any]:
    store = get_session_store()
    session = store.get_or_create(
        tenant_id=ctx.tenant_id,
        source=ctx.source,
        agent_id=ctx.agent_id,
    )
    return {
        "session_id": session.session_id,
        "routing_key": session.routing_key,
        "tenant_id": session.tenant_id,
        "agent_id": session.agent_id,
        "parent_session_id": session.parent_session_id,
        "subagent_id": session.subagent_id,
        "depth": session.depth,
        "status": session.status,
        "started_at": session.started_at,
        "tool_count": session.tool_count,
        "compression_parent_id": session.compression_parent_id,
    }
