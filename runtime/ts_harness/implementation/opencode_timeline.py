"""Where an OpenCode run spent its time, read from OpenCode's own session database.

OpenCode keeps every session in SQLite under ``XDG_DATA_HOME`` (see
``backends.opencode.opencode_data_env``). The database lives in the worker's
temp directory and is gone when the worker exits, so the coding job stores this
summary in its result instead.

Only the ``session``, ``message`` and ``part`` tables are read. Commands are cut
short and anything that looks like a credential is masked.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_SECRET = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{10,}|github_pat_\w+|sk-[A-Za-z0-9_-]{10,}|x-access-token:[^@\s]+"
    r"|Bearer\s+\S+|(?i:(?:token|key|secret|password)=)\S+)"
)
SLOWEST_CALLS = 8
COMMAND_CHARS = 120


def _clean(value: Any) -> str:
    return _SECRET.sub("<redacted>", str(value or "")).replace("\n", " ")[:COMMAND_CHARS]


def _label(part: dict[str, Any]) -> str:
    state = part.get("state") or {}
    data = state.get("input") or {}
    return _clean(data.get("command") or data.get("filePath") or data.get("pattern") or data.get("description") or "")


def summarize(data_dir: str | Path, *, since_ms: int = 0) -> list[dict[str, Any]]:
    """One entry per OpenCode session started at or after ``since_ms``, oldest first.

    ``model_s`` is time spent waiting for and streaming model output (time to first
    token, reasoning and text); ``tools_s`` is time inside tool calls by tool, as
    the part rows record it from start to finish.
    """
    path = Path(data_dir) / "opencode" / "opencode.db"
    if not path.is_file():
        return []
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        sessions = con.execute(
            "select id, title, time_created, time_updated from session where time_created >= ? order by time_created",
            (since_ms,),
        ).fetchall()
        out = []
        for sid, title, t0, t1 in sessions:
            out.append(_session(con, sid, title, t0, t1))
        return out
    finally:
        con.close()


def _session(con: sqlite3.Connection, sid: str, title: str, t0: int, t1: int) -> dict[str, Any]:
    first_part: dict[str, int] = {}
    tools: dict[str, float] = defaultdict(float)
    calls: list[tuple[float, str, str]] = []
    generation = 0.0
    for message_id, created, updated, raw in con.execute(
        "select message_id, time_created, time_updated, data from part where session_id = ? order by time_created", (sid,)
    ):
        part = json.loads(raw)
        kind = part.get("type")
        seconds = max(0, (updated or 0) - (created or 0)) / 1000
        if kind != "step-start":
            first_part.setdefault(message_id, created)
        if kind == "tool":
            tool = str(part.get("tool") or "?")
            tools[tool] += seconds
            calls.append((seconds, tool, _label(part)))
        elif kind in ("reasoning", "text"):
            generation += seconds
    waiting = 0.0
    turns = 0
    models: Counter[str] = Counter()
    output_tokens = 0
    for message_id, created, raw in con.execute(
        "select id, time_created, data from message where session_id = ? order by time_created", (sid,)
    ):
        message = json.loads(raw)
        if message.get("role") != "assistant":
            continue
        turns += 1
        models[str(message.get("modelID") or "?")] += 1
        output_tokens += int((message.get("tokens") or {}).get("output") or 0)
        if message_id in first_part:
            waiting += max(0, first_part[message_id] - created) / 1000
    tools_s = sum(tools.values())
    return {
        "title": _clean(title)[:80],
        "wall_s": round(max(0, (t1 or 0) - (t0 or 0)) / 1000, 1),
        "model_s": round(waiting + generation, 1),
        "tools_s": round(tools_s, 1),
        "tools": {name: round(value, 1) for name, value in sorted(tools.items(), key=lambda kv: -kv[1])},
        "turns": turns,
        "models": dict(models),
        "output_tokens": output_tokens,
        "slowest": [
            {"s": round(seconds, 1), "tool": tool, "call": label}
            for seconds, tool, label in sorted(calls, key=lambda c: -c[0])[:SLOWEST_CALLS]
        ],
    }
