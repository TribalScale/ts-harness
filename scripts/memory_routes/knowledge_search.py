"""Hybrid recall for knowledge notes: full-text search and pgvector, fused with RRF.

Each list (Postgres full-text rank, cosine similarity of the note embedding) ranks the
candidate notes on its own; reciprocal rank fusion adds ``1 / (RRF_K + rank)`` from each list a
note appears in. A note found by both lists beats one found by either.

Embeddings come from the memory service's own provider (``scripts/embed.sh``, the same path as
memories). When embedding fails or is switched off the search is full-text only, and a failure
of either list never fails the caller: run selection falls back to the applies_to/kind/recency
order.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from collections import OrderedDict
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

RRF_K = 60
CANDIDATES = 50
MAX_QUERY_TERMS = 32
MAX_QUERY_CHARS = 2000
_TERM = re.compile(r"[a-z0-9][a-z0-9_]{2,}")
_STOP = frozenset(
    "the and that this with for from into when then than are was were not but can its use using have has "
    "only also each which while should must would could does instead you your our all any".split()
)

Embed = Callable[[str], str]

_CACHE_SIZE = 256
_cache: OrderedDict[str, str] = OrderedDict()
_cache_lock = threading.Lock()


def vector_enabled() -> bool:
    return os.environ.get("KNOWLEDGE_NOTES_VECTOR", "on").strip().lower() not in {"0", "off", "false", "no"}


def note_text(note: dict[str, Any]) -> str:
    """The text a note is embedded from. Matches the hash computed in SQL."""
    return f"{note.get('title') or ''}\n{note.get('body') or ''}\n{note.get('why') or ''}"


def text_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()  # noqa: S324 - change detection, not security


def query_terms(text: str) -> list[str]:
    """Distinct lowercase words, safe to splice into ``to_tsquery`` (letters, digits, underscore)."""
    seen: list[str] = []
    for word in _TERM.findall(str(text or "").lower()[:MAX_QUERY_CHARS]):
        if word in _STOP or word in seen:
            continue
        seen.append(word)
        if len(seen) >= MAX_QUERY_TERMS:
            break
    return seen


def tsquery(text: str, *, prefix: bool = False) -> str:
    """An OR query over the words of ``text`` (``''`` when it has none)."""
    suffix = ":*" if prefix else ""
    return " | ".join(f"{term}{suffix}" for term in query_terms(text))


def embed_query(embed: Embed | None, text: str) -> str | None:
    """A vector literal for ``text``, or None when embedding is off or fails. Cached in-process."""
    text = str(text or "").strip()[:MAX_QUERY_CHARS]
    if embed is None or not text or not vector_enabled():
        return None
    key = text_hash(text)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    try:
        vector = _normalize(embed(text))
    except Exception as exc:  # noqa: BLE001 - full-text only
        logger.info("knowledge note query embedding unavailable: %s", exc)
        return None
    with _cache_lock:
        _cache[key] = vector
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
    return vector


def _normalize(value: str) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"'(?P<vector>\[[^\n]+\])'::vector\(\d+\)", text)
    if match:
        return match.group("vector")
    if re.fullmatch(r"\[[^\n]+\]", text):
        return text
    raise ValueError("embedding command returned an invalid vector literal")


def rrf(rankings: Iterable[list[str]], *, k: int = RRF_K) -> dict[str, float]:
    """Reciprocal rank fusion of ranked id lists (best first)."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, note_id in enumerate(ranking, 1):
            scores[note_id] = scores.get(note_id, 0.0) + 1.0 / (k + position)
    return scores


def text_ranking(cur, ids: list[str] | None, query: str, *, where: str = "", params: list[Any] | None = None,
                 limit: int = CANDIDATES, prefix: bool = False) -> list[tuple[str, float]]:
    """(id, ts_rank_cd) best first, over ``ids`` or the rows matching ``where``."""
    expr = tsquery(query, prefix=prefix)
    if not expr:
        return []
    clauses = ["search_tsv @@ q.query"]
    args: list[Any] = [expr]
    if ids is not None:
        clauses.append("id = ANY(%s::uuid[])")
        args.append(ids)
    if where:
        clauses.append(where)
        args.extend(params or [])
    args.append(limit)
    cur.execute(
        f"""
        SELECT id, ts_rank_cd(search_tsv, q.query) AS rank
          FROM knowledge_notes, to_tsquery('english', %s) AS q(query)
         WHERE {' AND '.join(clauses)}
         ORDER BY rank DESC, updated_at DESC
         LIMIT %s
        """,
        args,
    )
    return [(str(row["id"]), float(row["rank"] or 0)) for row in cur.fetchall()]


def vector_ranking(cur, ids: list[str] | None, vector: str | None, *, where: str = "",
                   params: list[Any] | None = None, limit: int = CANDIDATES) -> list[tuple[str, float]]:
    """(id, cosine similarity) best first, over notes that have an embedding."""
    if not vector:
        return []
    clauses = ["embedding IS NOT NULL"]
    args: list[Any] = [vector]
    if ids is not None:
        clauses.append("id = ANY(%s::uuid[])")
        args.append(ids)
    if where:
        clauses.append(where)
        args.extend(params or [])
    args.extend([vector, limit])
    cur.execute(
        f"""
        SELECT id, 1 - (embedding <=> %s::vector) AS similarity
          FROM knowledge_notes
         WHERE {' AND '.join(clauses)}
         ORDER BY embedding <=> %s::vector
         LIMIT %s
        """,
        args,
    )
    return [(str(row["id"]), float(row["similarity"] or 0)) for row in cur.fetchall()]


def relevance(conn, ids: list[str], query: str, embed: Embed | None) -> dict[str, float]:
    """RRF score per note id among ``ids`` for ``query``. ``{}`` on any failure or no query.

    Runs in a savepoint so a failed list leaves the caller's transaction usable.
    """
    if not ids or not str(query or "").strip():
        return {}
    vector = embed_query(embed, query)
    lists: list[list[str]] = []
    for name, fetch in (
        ("text", lambda cur: text_ranking(cur, ids, query, limit=len(ids))),
        ("vector", lambda cur: vector_ranking(cur, ids, vector, limit=len(ids))),
    ):
        try:
            with conn.transaction(), conn.cursor() as cur:
                lists.append([note_id for note_id, _ in fetch(cur)])
        except Exception as exc:  # noqa: BLE001 - recall is a hint, never a failure
            logger.warning("knowledge note %s ranking failed: %s", name, exc)
    return rrf(lists)


def search(conn, filters: dict[str, Any], embed: Embed | None, *, public: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    """Filtered notes for ``filters['q']``: hybrid when an embedding is available, else full-text.

    ``filters`` holds ``where`` (SQL over knowledge_notes) and ``params`` already validated by the
    caller, plus ``q`` and ``limit``.
    """
    where = str(filters.get("where") or "TRUE")
    params = list(filters.get("params") or [])
    limit = int(filters.get("limit") or 50)
    query = str(filters.get("q") or "").strip()
    with conn.cursor() as cur:
        if not query:
            cur.execute(
                f"SELECT * FROM knowledge_notes WHERE {where} ORDER BY updated_at DESC, id LIMIT %s",
                [*params, limit],
            )
            return {"notes": [public(row) for row in cur.fetchall()], "mode": "list"}
    texts: list[tuple[str, float]] = []
    vectors: list[tuple[str, float]] = []
    try:
        with conn.transaction(), conn.cursor() as cur:
            texts = text_ranking(cur, None, query, where=where, params=params, prefix=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("knowledge note text search failed: %s", exc)
    vector = embed_query(embed, query)
    if vector:
        try:
            with conn.transaction(), conn.cursor() as cur:
                vectors = vector_ranking(cur, None, vector, where=where, params=params)
        except Exception as exc:  # noqa: BLE001
            logger.warning("knowledge note vector search failed: %s", exc)
            vector = None
    scores = rrf([[note_id for note_id, _ in texts], [note_id for note_id, _ in vectors]])
    ranked = sorted(scores, key=lambda note_id: scores[note_id], reverse=True)[:limit]
    rows: dict[str, dict[str, Any]] = {}
    if ranked:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM knowledge_notes WHERE id = ANY(%s::uuid[])", (ranked,))
            rows = {str(row["id"]): row for row in cur.fetchall()}
    text_rank = dict(texts)
    similarity = dict(vectors)
    notes = []
    for note_id in ranked:
        if note_id not in rows:
            continue
        item = public(rows[note_id])
        item["score"] = round(scores[note_id], 6)
        if note_id in text_rank:
            item["text_rank"] = round(text_rank[note_id], 6)
        if note_id in similarity:
            item["similarity"] = round(similarity[note_id], 6)
        notes.append(item)
    return {"notes": notes, "mode": "hybrid" if vector else "text"}


def embed_pending(conn, embed: Embed | None, *, ids: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
    """Embed notes with no embedding or whose text changed since. Best effort, at most ``limit``."""
    if embed is None or not vector_enabled():
        return {"embedded": 0, "failed": 0, "skipped": "embeddings are off"}
    where = [
        "status IN ('active', 'proposed')",
        "(embedding IS NULL OR embedding_hash IS DISTINCT FROM "
        "md5(coalesce(title, '') || E'\\n' || coalesce(body, '') || E'\\n' || coalesce(why, '')))",
    ]
    params: list[Any] = []
    if ids is not None:
        clean = [str(item) for item in ids if item]
        if not clean:
            return {"embedded": 0, "failed": 0}
        where.append("id = ANY(%s::uuid[])")
        params.append(clean)
    params.append(max(1, min(int(limit), 200)))
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, title, body, why FROM knowledge_notes WHERE {' AND '.join(where)} "
            "ORDER BY updated_at DESC LIMIT %s",
            params,
        )
        rows = list(cur.fetchall())
    conn.commit()
    embedded = 0
    failed = 0
    for row in rows:
        text = note_text(row)
        try:
            vector = _normalize(embed(text))
        except Exception as exc:  # noqa: BLE001 - stays full-text only until the next try
            logger.info("knowledge note embedding failed for %s: %s", row["id"], exc)
            failed += 1
            if failed >= 2 and embedded == 0:
                break  # the provider is down; do not spend the whole batch on it
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE knowledge_notes
                       SET embedding = %s::vector, embedding_hash = %s, embedded_at = now()
                     WHERE id = %s
                    """,
                    (vector, text_hash(text), row["id"]),
                )
            conn.commit()
            embedded += 1
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            logger.warning("knowledge note embedding not stored for %s: %s", row["id"], exc)
            failed += 1
    return {"embedded": embedded, "failed": failed, "pending": len(rows) - embedded}
