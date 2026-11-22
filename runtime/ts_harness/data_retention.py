"""How long the harness keeps client data, and the daily job that applies it.

Each window is a number of days from an environment variable (the runtime ``.env``); 0 keeps
that data forever. The SQL work runs in the memory API one batch per category per run
(``scripts/memory_routes/data_retention.py``); uploaded design images of long-finished programs
are deleted from the intake object store here. See docs/data-handling.md.
"""

from __future__ import annotations

import logging
import os
from dataclasses import asdict, dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetentionSetting:
    key: str
    env: str
    default: int
    what: str


SETTINGS: tuple[RetentionSetting, ...] = (
    RetentionSetting("transcript_days", "TS_HARNESS_RETENTION_TRANSCRIPT_DAYS", 180, "chat transcript messages"),
    RetentionSetting(
        "coding_log_days",
        "TS_HARNESS_RETENTION_CODING_LOG_DAYS",
        30,
        "log tails, check output and progress of finished coding jobs",
    ),
    RetentionSetting(
        "workflow_task_days",
        "TS_HARNESS_RETENTION_WORKFLOW_TASK_DAYS",
        365,
        "inputs and outputs of tasks in finished workflows (and their coding job specs)",
    ),
    RetentionSetting("audit_days", "TS_HARNESS_RETENTION_AUDIT_DAYS", 730, "runtime and session audit events"),
    RetentionSetting(
        "audit_decision_days",
        "TS_HARNESS_RETENTION_AUDIT_DECISION_DAYS",
        0,
        "approval, decision, access and purge audit events",
    ),
    RetentionSetting(
        "upload_days",
        "TS_HARNESS_RETENTION_UPLOAD_DAYS",
        180,
        "uploaded design images, counted from when their program finished",
    ),
)
BATCH_ENV = "TS_HARNESS_RETENTION_BATCH"
DEFAULT_BATCH = 500


@dataclass(frozen=True)
class RetentionPolicy:
    transcript_days: int
    coding_log_days: int
    workflow_task_days: int
    audit_days: int
    audit_decision_days: int
    upload_days: int
    batch: int = DEFAULT_BATCH


def _env_days(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s is not a whole number; using %s", name, default)
        return default
    return max(0, value)


def retention_policy() -> RetentionPolicy:
    values = {s.key: _env_days(s.env, s.default) for s in SETTINGS}
    return RetentionPolicy(**values, batch=max(1, _env_days(BATCH_ENV, DEFAULT_BATCH) or DEFAULT_BATCH))


def _purge_uploads(client: Any, days: int, batch: int) -> dict[str, Any]:
    if not days:
        return {"kept": "forever"}
    from ts_harness.intake.blobs import intake_store

    listing = client.expired_uploads(days=days, batch=min(batch, 50))
    programs = list(listing.get("programs") or [])
    if not programs:
        return {"programs": 0, "deleted": 0}
    store = intake_store()
    deleted = 0
    for key in listing.get("object_keys") or []:
        if not str(key).startswith("design/"):
            continue
        store.delete(str(key))
        deleted += 1
    client.mark_uploads_purged(programs)
    return {"programs": len(programs), "deleted": deleted, "kept_in_use": int(listing.get("kept_in_use") or 0)}


def apply_data_retention(*, client: Any = None) -> dict[str, Any]:
    """Run one retention pass; returns counts per category. Never raises."""
    policy = retention_policy()
    if client is None:
        from ts_harness.memory.client import get_memory_client

        client = get_memory_client()
    settings = asdict(policy)
    upload_days = settings.pop("upload_days")
    out: dict[str, Any] = {"status": "ok", "policy": asdict(policy)}
    try:
        out.update(client.apply_data_retention(**settings))
    except Exception as exc:  # noqa: BLE001
        logger.warning("data retention failed: %s", exc)
        return {"status": "error", "error": str(exc), "policy": asdict(policy)}
    try:
        out["uploads"] = _purge_uploads(client, upload_days, policy.batch)
    except Exception as exc:  # noqa: BLE001
        logger.warning("upload retention failed: %s", exc)
        out["status"] = "partial"
        out["uploads"] = {"error": str(exc)}
    return out
