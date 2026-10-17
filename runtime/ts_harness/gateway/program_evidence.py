"""Per-program evidence document: canonical JSON with a verifiable SHA-256."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

EVIDENCE_SCHEMA = "ts-harness.program-evidence/v1"
HASH_METHOD = (
    "sha256 of this document with the sha256 field removed, serialized as JSON "
    "with sorted keys and separators (',', ':')"
)
_SECTIONS: tuple[tuple[str, Any], ...] = (
    ("program", dict),
    ("children", list),
    ("workflows", list),
    ("decisions", list),
    ("pull_requests", list),
    ("jira", list),
    ("other_refs", list),
    ("releases", list),
    ("compensation", dict),
    ("audit", list),
)


def canonical_json(document: dict[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def evidence_sha256(document: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in document.items() if key != "sha256"}
    return hashlib.sha256(canonical_json(unsigned)).hexdigest()


def build_evidence_document(
    raw: dict[str, Any],
    *,
    generated_by: str,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Assemble the evidence document from the memory-api payload and sign it."""
    safe = json.loads(json.dumps(raw or {}, default=str))
    moment = (generated_at or datetime.now(UTC)).astimezone(UTC)
    document: dict[str, Any] = {
        "schema": EVIDENCE_SCHEMA,
        "generated_at": moment.isoformat().replace("+00:00", "Z"),
        "generated_by": generated_by,
        "hash_method": HASH_METHOD,
        "audit_truncated": bool(safe.get("audit_truncated")),
    }
    for key, kind in _SECTIONS:
        value = safe.get(key)
        document[key] = value if isinstance(value, kind) else kind()
    document["sha256"] = evidence_sha256(document)
    return document
