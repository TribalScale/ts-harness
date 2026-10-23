"""Session transcript persistence for channel chat replay."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any
import json
import os


def repair_tool_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Close assistant tool calls and drop tool rows that have no matching call.

    Providers reject a replay when a `tool_call_id` has no tool message, and
    they also reject a tool message that is not answering an open call. A
    truncated transcript can start on either kind of orphan.
    """
    repaired: list[dict[str, Any]] = []
    open_ids: list[str] = []

    def close_open() -> None:
        nonlocal open_ids
        for missing in open_ids:
            repaired.append(
                {
                    "role": "tool",
                    "tool_call_id": missing,
                    "content": '{"error": "tool result unavailable"}',
                }
            )
        open_ids = []

    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            tool_id = str(msg.get("tool_call_id") or "")
            if tool_id and tool_id in open_ids:
                open_ids.remove(tool_id)
                repaired.append(msg)
            continue
        if open_ids:
            close_open()
        repaired.append(msg)
        if role == "assistant":
            for call in msg.get("tool_calls") or []:
                call_id = str(call.get("id") or "")
                if call_id:
                    open_ids.append(call_id)
    if open_ids:
        close_open()
    return repaired


@dataclass(frozen=True)
class TranscriptHit:
    session_id: str
    seq: int
    role: str
    snippet: str
    created_at: str
    agent_id: str
    source: str


class TranscriptStore:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        db_url: str | None = None,
    ):
        self.path = Path(path) if path is not None else None
        self.db_url = db_url
        self._lock = RLock()
        self._seq: dict[str, int] = {}
        self._postgres_loaded = False
        if self.path and self.path.exists():
            self._load_jsonl_seq()

    def load_messages(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        if not session_id:
            return []
        with self._lock:
            if self.db_url:
                self._ensure_postgres_loaded()
                return self._load_postgres(session_id, limit=limit)
            if self.path and self.path.exists():
                return self._load_jsonl(session_id, limit=limit)
        return []

    def append_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        if not session_id or not messages:
            return
        with self._lock:
            if self.db_url:
                self._ensure_postgres_loaded()
                self._append_postgres(session_id, messages)
            elif self.path is not None:
                self._append_jsonl(session_id, messages)

    def clear(self, session_id: str) -> None:
        if not session_id:
            return
        with self._lock:
            self._seq.pop(session_id, None)
            if self.db_url:
                self._clear_postgres(session_id)
            elif self.path and self.path.exists():
                self._clear_jsonl(session_id)

    def search(self, *, tenant_id: str, query: str, limit: int = 20) -> list[TranscriptHit]:
        q = str(query or "").strip()
        if not q:
            return []
        clamped = max(1, min(int(limit or 20), 50))
        if self.db_url:
            return self._search_postgres(tenant_id=tenant_id, query=q, limit=clamped)
        return self._search_jsonl(tenant_id=tenant_id, query=q, limit=clamped)

    def append_turn(
        self,
        session_id: str,
        *,
        user_text: str,
        assistant_messages: list[dict[str, Any]],
    ) -> None:
        """Persist one channel turn: user message plus new assistant/tool tail."""
        to_store: list[dict[str, Any]] = [{"role": "user", "content": user_text}]
        for msg in assistant_messages:
            role = msg.get("role")
            if role not in {"assistant", "tool"}:
                continue
            row: dict[str, Any] = {"role": role}
            if msg.get("content") is not None:
                row["content"] = msg.get("content")
            if msg.get("tool_calls"):
                row["tool_calls"] = msg.get("tool_calls")
            if msg.get("tool_call_id"):
                row["tool_call_id"] = msg.get("tool_call_id")
            to_store.append(row)
        self.append_messages(session_id, to_store)

    def _next_seq(self, session_id: str, count: int) -> int:
        start = self._seq.get(session_id, 0)
        self._seq[session_id] = start + count
        return start

    def _load_jsonl_seq(self) -> None:
        assert self.path is not None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            sid = str(row.get("session_id", ""))
            seq = int(row.get("seq", 0))
            self._seq[sid] = max(self._seq.get(sid, 0), seq)

    def _load_jsonl(self, session_id: str, *, limit: int) -> list[dict[str, Any]]:
        assert self.path is not None
        rows: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("session_id") != session_id:
                continue
            rows.append(self._row_to_message(row))
        if len(rows) > limit:
            rows = rows[-limit:]
        return rows

    def _append_jsonl(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        start = self._next_seq(session_id, len(messages))
        with self.path.open("a", encoding="utf-8") as handle:
            for offset, msg in enumerate(messages):
                row = {
                    "session_id": session_id,
                    "seq": start + offset + 1,
                    **self._message_to_row(msg),
                }
                handle.write(json.dumps(row, sort_keys=True) + "\n")

    def _clear_jsonl(self, session_id: str) -> None:
        assert self.path is not None
        if not self.path.exists():
            return
        kept = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("session_id") != session_id:
                kept.append(line)
        self.path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")

    def _connect(self):
        from ts_harness.agent.sessions import build_session_store

        return build_session_store(db_url=self.db_url)._connect()

    def _ensure_postgres_loaded(self) -> None:
        if not self.db_url or self._postgres_loaded:
            return
        self._load_postgres_seq()
        self._postgres_loaded = True

    def _load_postgres_seq(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT session_id, MAX(seq)
                    FROM runtime_transcript_messages
                    GROUP BY session_id
                    """
                )
                for row in cur.fetchall():
                    self._seq[str(row[0])] = int(row[1])
            conn.commit()

    def _load_postgres(self, session_id: str, *, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT role, content, tool_calls, tool_call_id
                    FROM runtime_transcript_messages
                    WHERE session_id = %s
                    ORDER BY seq DESC
                    LIMIT %s
                    """,
                    (session_id, limit),
                )
                rows = list(reversed(cur.fetchall()))
            conn.commit()
        messages: list[dict[str, Any]] = []
        for role, content, tool_calls, tool_call_id in rows:
            msg: dict[str, Any] = {"role": role}
            if content is not None:
                msg["content"] = content
            if tool_calls:
                msg["tool_calls"] = tool_calls
            if tool_call_id:
                msg["tool_call_id"] = tool_call_id
            messages.append(msg)
        return messages

    def _append_postgres(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        # The runtime and the workflow runner both append. A process-local counter
        # hands both of them the same seq. Lock the session and read MAX(seq) here.
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s)::bigint)",
                    (session_id,),
                )
                cur.execute(
                    """
                    SELECT COALESCE(MAX(seq), 0)
                    FROM runtime_transcript_messages
                    WHERE session_id = %s
                    """,
                    (session_id,),
                )
                start = int(cur.fetchone()[0])
                for offset, msg in enumerate(messages):
                    row = self._message_to_row(msg)
                    seq = start + offset + 1
                    cur.execute(
                        """
                        INSERT INTO runtime_transcript_messages (
                            session_id, seq, role, content, tool_calls, tool_call_id
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            session_id,
                            seq,
                            row["role"],
                            row.get("content"),
                            row.get("tool_calls"),
                            row.get("tool_call_id"),
                        ),
                    )
            conn.commit()
        self._seq[session_id] = start + len(messages)

    def _clear_postgres(self, session_id: str) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM runtime_transcript_messages WHERE session_id = %s",
                    (session_id,),
                )
            conn.commit()

    @staticmethod
    def _message_to_row(msg: dict[str, Any]) -> dict[str, Any]:
        row: dict[str, Any] = {"role": str(msg.get("role", "user"))}
        if msg.get("content") is not None:
            row["content"] = str(msg.get("content"))
        if msg.get("tool_calls"):
            row["tool_calls"] = msg.get("tool_calls")
        if msg.get("tool_call_id"):
            row["tool_call_id"] = str(msg.get("tool_call_id"))
        return row

    @staticmethod
    def _row_to_message(row: dict[str, Any]) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": row.get("role", "user")}
        if row.get("content") is not None:
            msg["content"] = row.get("content")
        if row.get("tool_calls"):
            msg["tool_calls"] = row.get("tool_calls")
        if row.get("tool_call_id"):
            msg["tool_call_id"] = row.get("tool_call_id")
        return msg

    def _search_jsonl(self, *, tenant_id: str, query: str, limit: int) -> list[TranscriptHit]:
        from ts_harness.agent.sessions import get_session_store
        from ts_harness.tools.policy import redact_snippet

        if not self.path or not self.path.exists():
            return []

        needle = query.lower()
        hits: list[TranscriptHit] = []
        session_store = get_session_store()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            role = str(row.get("role", ""))
            if role not in {"user", "assistant"}:
                continue
            content = row.get("content")
            if content is None or needle not in str(content).lower():
                continue
            session_id = str(row.get("session_id", ""))
            session = session_store.get(session_id)
            if session is None or session.tenant_id != tenant_id:
                continue
            created_at = str(row.get("created_at", "") or "")
            hits.append(
                TranscriptHit(
                    session_id=session_id,
                    seq=int(row.get("seq", 0)),
                    role=role,
                    snippet=redact_snippet(str(content)),
                    created_at=created_at,
                    agent_id=session.agent_id,
                    source=session.source,
                )
            )
        return list(reversed(hits[-limit:]))

    def _search_postgres(self, *, tenant_id: str, query: str, limit: int) -> list[TranscriptHit]:
        from ts_harness.tools.policy import redact_snippet

        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT search_tsquery")
                try:
                    cur.execute(
                        "SELECT websearch_to_tsquery('english', %s)",
                        (query,),
                    )
                    tsquery_row = cur.fetchone()
                    cur.execute("RELEASE SAVEPOINT search_tsquery")
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT search_tsquery")
                    cur.execute(
                        "SELECT plainto_tsquery('english', %s)",
                        (query,),
                    )
                    tsquery_row = cur.fetchone()

                tsquery = tsquery_row[0] if tsquery_row else None
                if tsquery is None:
                    return []

                cur.execute(
                    """
                    SELECT m.session_id, m.seq, m.role,
                           ts_headline(
                               'english',
                               coalesce(m.content, ''),
                               %s,
                               'StartSel=,StopSel=,MaxWords=40,MinWords=20'
                           ) AS snippet,
                           m.created_at, s.agent_id, s.source
                    FROM runtime_transcript_messages m
                    JOIN runtime_sessions s ON s.session_id = m.session_id
                    WHERE s.tenant_id = %s
                      AND m.role IN ('user', 'assistant')
                      AND m.content_tsv @@ %s
                    ORDER BY m.created_at DESC
                    LIMIT %s
                    """,
                    (tsquery, tenant_id, tsquery, limit),
                )
                rows = cur.fetchall()
            conn.commit()

        hits: list[TranscriptHit] = []
        for session_id, seq, role, snippet, created_at, agent_id, source in rows:
            created = (
                created_at.isoformat().replace("+00:00", "Z")
                if hasattr(created_at, "isoformat")
                else str(created_at or "")
            )
            hits.append(
                TranscriptHit(
                    session_id=str(session_id),
                    seq=int(seq),
                    role=str(role),
                    snippet=redact_snippet(str(snippet or "")),
                    created_at=created,
                    agent_id=str(agent_id),
                    source=str(source),
                )
            )
        return hits


def build_transcript_store() -> TranscriptStore:
    db_url = os.environ.get("TS_HARNESS_SESSION_DB_URL")
    if db_url:
        return TranscriptStore(db_url=db_url)
    session_path = os.environ.get("TS_HARNESS_SESSION_STORE")
    transcript_path = os.environ.get("TS_HARNESS_TRANSCRIPT_STORE")
    if transcript_path:
        return TranscriptStore(path=transcript_path)
    if session_path:
        return TranscriptStore(path=str(Path(session_path).with_name("transcripts.jsonl")))
    return TranscriptStore(path=None)


_default_store: TranscriptStore | None = None


def get_transcript_store() -> TranscriptStore:
    global _default_store
    if _default_store is None:
        _default_store = build_transcript_store()
    return _default_store


def reset_transcript_store(store: TranscriptStore | None = None) -> None:
    global _default_store
    _default_store = store or build_transcript_store()
