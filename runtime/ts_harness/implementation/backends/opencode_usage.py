"""Token usage of OpenCode runs, read back from ``opencode export`` after the run.

OpenCode calls the model provider itself, so the runtime never sees its responses. Each
session it writes keeps per-message token counts; after a run we export the sessions created
in the repo since the run began and record their totals in ``llm_usage`` as ``coding-backend``.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping

from ts_harness.llm_proxy.metering import Usage

logger = logging.getLogger(__name__)


def _run_json(cmd: list[str], *, env: Mapping[str, str], cwd: str, timeout: float = 60) -> Any:
    completed = subprocess.run(cmd, env=dict(env), cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "")[-300:])
    return json.loads(completed.stdout or "null")


def session_usage(export: Mapping[str, Any]) -> dict[tuple[str, str], dict[str, float]]:
    """Totals per (provider, model) from one ``opencode export`` document."""
    totals: dict[tuple[str, str], dict[str, float]] = {}
    for message in export.get("messages") or []:
        info = message.get("info") if isinstance(message, Mapping) else None
        if not isinstance(info, Mapping) or info.get("role") != "assistant":
            continue
        tokens = info.get("tokens") if isinstance(info.get("tokens"), Mapping) else {}
        cache = tokens.get("cache") if isinstance(tokens.get("cache"), Mapping) else {}
        key = (str(info.get("providerID") or "unknown"), str(info.get("modelID") or "unknown"))
        row = totals.setdefault(key, {"input": 0, "output": 0, "cache_read": 0, "cost": 0.0})
        row["input"] += int(tokens.get("input") or 0) + int(cache.get("write") or 0)
        row["output"] += int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
        row["cache_read"] += int(cache.get("read") or 0)
        row["cost"] += float(info.get("cost") or 0)
    return totals


def record_opencode_usage(
    *,
    binary: str,
    env: Mapping[str, str],
    repo_dir: Path,
    since_ms: int,
    scope: Mapping[str, Any],
    run_json: Callable[..., Any] = _run_json,
    record: Callable[..., bool] | None = None,
) -> int:
    """Record every session OpenCode created in ``repo_dir`` since ``since_ms``. Returns rows written."""
    if record is None:
        from ts_harness.llm_usage import record_usage, recording_enabled

        if not recording_enabled():
            return 0
        record = record_usage
    written = 0
    try:
        sessions = run_json([binary, "session", "list", "--format", "json", "-n", "50"], env=env, cwd=str(repo_dir))
    except Exception as exc:  # noqa: BLE001
        logger.warning("opencode usage: session list failed: %s", exc)
        return 0
    wanted = str(Path(repo_dir).resolve())
    for session in sessions or []:
        if not isinstance(session, Mapping):
            continue
        if int(session.get("created") or 0) < since_ms:
            continue
        directory = str(session.get("directory") or "")
        if directory and str(Path(directory).resolve()) != wanted:
            continue
        session_id = str(session.get("id") or "")
        try:
            export = run_json([binary, "export", session_id], env=env, cwd=str(repo_dir))
        except Exception as exc:  # noqa: BLE001
            logger.warning("opencode usage: export %s failed: %s", session_id, exc)
            continue
        for (provider, model), row in session_usage(export or {}).items():
            usage = Usage(
                input_tokens=int(row["input"]), output_tokens=int(row["output"]), cache_read_tokens=int(row["cache_read"])
            )
            from ts_harness.llm_usage import pricing
            from ts_harness.llm_proxy.metering import match_model

            # Our price table wins; OpenCode's own figure covers models the table does not list.
            cost = None if match_model(pricing(), provider, model) else row["cost"]
            if record(scope, provider=provider, model=model, usage=usage, cost_usd=cost, session_id=session_id):
                written += 1
    return written
