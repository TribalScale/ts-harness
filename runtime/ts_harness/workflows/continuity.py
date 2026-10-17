"""Program continuity: versioned artifact bags + immutable memory snapshots.

Phase 3 of the Platform Durability Upgrades makes versioned artifact bags plus
semantic memory snapshots the handoff medium for governed programs and ad-hoc
delegation chains, replacing chat-excerpt handoffs.

Continuity model (see design doc "Continuity model"):

1. Latest values live in program ``metadata.artifacts`` (the canonical bag).
2. Each successful stage write also creates an immutable semantic memory tagged
   ``continuity``, ``workflow:<id>``, ``stage:<name>``, ``version:<n>`` (and
   ``session:<id>`` for ad-hoc chains).
3. All ops require a matching ``tenant_id``.
4. Secrets/tokens are redacted before persistence.
5. Ad-hoc delegation keys by tenant + parent/child session (no fake program).

Versioning is monotonically increasing per ``(tenant, workflow|session, stage)``
and idempotent: writing the same redacted output again returns the existing
snapshot without creating a duplicate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
import copy
import hashlib
import json

from ts_harness.workflows.stage_contracts import extract_required_artifacts


# Substrings that mark a key as sensitive; matched case-insensitively.
SENSITIVE_KEY_PATTERNS: tuple[str, ...] = (
    "token",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "authorization",
    "private_key",
    "access_key",
    "credential",
)
REDACTED = "***REDACTED***"

# Reserved key inside program ``metadata.artifacts`` holding the version index.
CONTINUITY_INDEX_KEY = "_continuity"
# Agent scope used for continuity memory snapshots.
CONTINUITY_AGENT_ID = "workflow-continuity"

# Process-local version index for ad-hoc (session-keyed) chains. Governed program
# stages persist their index inside program metadata instead.
_SESSION_VERSION_INDEX: dict[str, dict[str, Any]] = {}


@dataclass
class ContinuityRef:
    tenant_id: str
    workflow_id: str
    program_id: str | None
    stage_name: str
    version: int
    content_hash: str
    artifact_keys: list[str] = field(default_factory=list)
    memory_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Redaction + hashing
# ---------------------------------------------------------------------------


def _is_sensitive_key(key: Any) -> bool:
    lowered = str(key).lower()
    return any(pattern in lowered for pattern in SENSITIVE_KEY_PATTERNS)


def redact_sensitive(value: Any) -> Any:
    """Recursively redact values under keys that look like credentials/tokens."""
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(key):
                redacted[key] = REDACTED
            else:
                redacted[key] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value


def content_hash(payload: Any) -> str:
    """Stable SHA-256 hash of a (already redacted) JSON-serializable payload."""
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _artifact_keys(output: dict[str, Any], contract: dict[str, Any] | None) -> list[str]:
    """Artifact keys via a contract when available, else top-level/artifacts keys."""
    if contract:
        extracted = extract_required_artifacts(contract, output)
        if extracted:
            return sorted(extracted.keys())
    keys: set[str] = set()
    if isinstance(output, dict):
        for key in output.keys():
            text = str(key)
            if text.startswith("_"):
                continue
            keys.add(text)
        nested = output.get("artifacts")
        if isinstance(nested, dict):
            keys.update(str(key) for key in nested.keys())
    return sorted(keys)


# ---------------------------------------------------------------------------
# Program metadata helpers
# ---------------------------------------------------------------------------


def _program_artifacts_and_index(
    memory_client: Any,
    program_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = memory_client.get_program(program_id)
    program = (payload or {}).get("program") or {}
    metadata = program.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    artifacts = dict((metadata.get("artifacts") or {}) if isinstance(metadata, dict) else {})
    index = artifacts.get(CONTINUITY_INDEX_KEY)
    if not isinstance(index, dict):
        index = {}
    return artifacts, dict(index)


def _write_memory_snapshot(
    memory_client: Any,
    *,
    tenant_id: str,
    workflow_id: str,
    program_id: str | None,
    stage_name: str,
    version: int,
    digest: str,
    artifact_keys: list[str],
    redacted_output: dict[str, Any],
    session_id: str | None,
) -> str | None:
    tags = [
        "continuity",
        f"workflow:{workflow_id}",
        f"stage:{stage_name}",
        f"version:{version}",
    ]
    if session_id:
        tags.append(f"session:{session_id}")
    content = json.dumps(
        {
            "tenant_id": tenant_id,
            "workflow_id": workflow_id,
            "program_id": program_id,
            "stage_name": stage_name,
            "version": version,
            "content_hash": digest,
            "artifact_keys": artifact_keys,
            "output": redacted_output,
        },
        sort_keys=True,
        default=str,
    )
    result = memory_client.remember(
        tenant_id=tenant_id,
        agent_id=CONTINUITY_AGENT_ID,
        content=content,
        memory_type="semantic",
        tags=tags,
    )
    if isinstance(result, dict):
        memory_id = result.get("id") or result.get("memory_id")
        return str(memory_id) if memory_id is not None else None
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_stage_snapshot(
    *,
    tenant_id: str,
    workflow_id: str,
    program_id: str | None,
    stage_name: str,
    output: dict[str, Any],
    memory_client: Any,
    session_id: str | None = None,
    contract: dict[str, Any] | None = None,
) -> ContinuityRef:
    """Persist a stage output as a versioned artifact bag + memory snapshot.

    Governed program stages (``program_id`` set) patch canonical latest values
    into ``metadata.artifacts`` and fail closed when memory is unavailable.
    Ad-hoc chains (``session_id`` set, no program) persist best-effort keyed by
    tenant + session.
    """
    tenant_id = str(tenant_id)
    stage_name = str(stage_name)
    output = output if isinstance(output, dict) else {}
    if not program_id and not session_id:
        raise ValueError("write_stage_snapshot requires program_id or session_id")

    redacted_output = redact_sensitive(copy.deepcopy(output))
    digest = content_hash(redacted_output)
    artifact_keys = _artifact_keys(redacted_output, contract)

    if program_id:
        return _write_program_snapshot(
            memory_client,
            tenant_id=tenant_id,
            workflow_id=str(workflow_id),
            program_id=str(program_id),
            stage_name=stage_name,
            redacted_output=redacted_output,
            digest=digest,
            artifact_keys=artifact_keys,
            session_id=session_id,
        )
    return _write_session_snapshot(
        memory_client,
        tenant_id=tenant_id,
        workflow_id=str(workflow_id),
        stage_name=stage_name,
        redacted_output=redacted_output,
        digest=digest,
        artifact_keys=artifact_keys,
        session_id=str(session_id),
    )


def _write_program_snapshot(
    memory_client: Any,
    *,
    tenant_id: str,
    workflow_id: str,
    program_id: str,
    stage_name: str,
    redacted_output: dict[str, Any],
    digest: str,
    artifact_keys: list[str],
    session_id: str | None,
) -> ContinuityRef:
    artifacts, index = _program_artifacts_and_index(memory_client, program_id)
    index_key = f"{workflow_id}:{stage_name}"
    entry = index.get(index_key) if isinstance(index.get(index_key), dict) else {}

    if entry.get("content_hash") == digest:
        return ContinuityRef(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            program_id=program_id,
            stage_name=stage_name,
            version=int(entry.get("version") or 1),
            content_hash=digest,
            artifact_keys=list(entry.get("artifact_keys") or artifact_keys),
            memory_id=entry.get("memory_id"),
        )

    version = int(entry.get("version") or 0) + 1
    # Fail closed for governed program stages: a memory failure raises before we
    # mutate the canonical artifact bag.
    memory_id = _write_memory_snapshot(
        memory_client,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        program_id=program_id,
        stage_name=stage_name,
        version=version,
        digest=digest,
        artifact_keys=artifact_keys,
        redacted_output=redacted_output,
        session_id=session_id,
    )

    index[index_key] = {
        "version": version,
        "content_hash": digest,
        "memory_id": memory_id,
        "artifact_keys": artifact_keys,
    }
    patch = {**redacted_output, CONTINUITY_INDEX_KEY: index}
    memory_client.patch_program_artifacts(program_id, patch, merge=True, actor=CONTINUITY_AGENT_ID)

    return ContinuityRef(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        program_id=program_id,
        stage_name=stage_name,
        version=version,
        content_hash=digest,
        artifact_keys=artifact_keys,
        memory_id=memory_id,
    )


def _write_session_snapshot(
    memory_client: Any,
    *,
    tenant_id: str,
    workflow_id: str,
    stage_name: str,
    redacted_output: dict[str, Any],
    digest: str,
    artifact_keys: list[str],
    session_id: str,
) -> ContinuityRef:
    index_key = f"{tenant_id}:session:{session_id}:{stage_name}"
    entry = _SESSION_VERSION_INDEX.get(index_key) or {}

    if entry.get("content_hash") == digest:
        return ContinuityRef(
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            program_id=None,
            stage_name=stage_name,
            version=int(entry.get("version") or 1),
            content_hash=digest,
            artifact_keys=list(entry.get("artifact_keys") or artifact_keys),
            memory_id=entry.get("memory_id"),
        )

    version = int(entry.get("version") or 0) + 1
    # Ad-hoc chains persist best-effort; a memory outage must not crash delegation.
    try:
        memory_id = _write_memory_snapshot(
            memory_client,
            tenant_id=tenant_id,
            workflow_id=workflow_id,
            program_id=None,
            stage_name=stage_name,
            version=version,
            digest=digest,
            artifact_keys=artifact_keys,
            redacted_output=redacted_output,
            session_id=session_id,
        )
    except Exception:
        memory_id = None

    _SESSION_VERSION_INDEX[index_key] = {
        "version": version,
        "content_hash": digest,
        "memory_id": memory_id,
        "artifact_keys": artifact_keys,
    }

    return ContinuityRef(
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        program_id=None,
        stage_name=stage_name,
        version=version,
        content_hash=digest,
        artifact_keys=artifact_keys,
        memory_id=memory_id,
    )


def _coerce_ref(ref: ContinuityRef | dict[str, Any]) -> ContinuityRef:
    if isinstance(ref, ContinuityRef):
        return ref
    if isinstance(ref, dict):
        return ContinuityRef(
            tenant_id=str(ref.get("tenant_id") or ""),
            workflow_id=str(ref.get("workflow_id") or ""),
            program_id=ref.get("program_id"),
            stage_name=str(ref.get("stage_name") or ""),
            version=int(ref.get("version") or 0),
            content_hash=str(ref.get("content_hash") or ""),
            artifact_keys=list(ref.get("artifact_keys") or []),
            memory_id=ref.get("memory_id"),
        )
    raise TypeError(f"unsupported continuity ref: {type(ref)!r}")


def read_handoff_context(
    *,
    tenant_id: str,
    refs: list[ContinuityRef | dict[str, Any]],
    required_keys: list[str],
    memory_client: Any,
) -> dict[str, Any]:
    """Reconstruct handoff context for ``required_keys`` from continuity refs.

    Rejects any ref whose ``tenant_id`` does not match the caller. Reads the
    canonical program artifact bag (falling back for compatibility) and, when a
    ref has a memory snapshot, prefers the immutable snapshot output.
    """
    tenant_id = str(tenant_id)
    coerced = [_coerce_ref(ref) for ref in (refs or [])]
    for ref in coerced:
        if str(ref.tenant_id) != tenant_id:
            raise ValueError(
                f"cross-tenant continuity ref rejected: {ref.tenant_id} != {tenant_id}"
            )

    wanted = list(required_keys or [])
    context: dict[str, Any] = {}
    program_cache: dict[str, dict[str, Any]] = {}

    for ref in coerced:
        values = _read_ref_values(ref, memory_client, program_cache)
        for key in wanted:
            if key in values and key not in context:
                context[key] = values[key]

    return context


def _read_ref_values(
    ref: ContinuityRef,
    memory_client: Any,
    program_cache: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    values: dict[str, Any] = {}

    if ref.program_id:
        program_id = str(ref.program_id)
        if program_id not in program_cache:
            try:
                artifacts, _index = _program_artifacts_and_index(memory_client, program_id)
            except Exception:
                artifacts = {}
            program_cache[program_id] = {
                key: value
                for key, value in artifacts.items()
                if key != CONTINUITY_INDEX_KEY
            }
        values.update(program_cache[program_id])

    snapshot = _recall_snapshot_output(ref, memory_client)
    if snapshot:
        # Immutable snapshot wins over the mutable bag for this ref's keys.
        values.update(snapshot)

    return values


def _recall_snapshot_output(ref: ContinuityRef, memory_client: Any) -> dict[str, Any]:
    recall = getattr(memory_client, "recall", None)
    if recall is None:
        return {}
    try:
        memories = recall(
            tenant_id=ref.tenant_id,
            agent_id=CONTINUITY_AGENT_ID,
            query=f"continuity {ref.stage_name}",
            limit=10,
        )
    except Exception:
        return {}
    for memory in memories or []:
        if not isinstance(memory, dict):
            continue
        content = memory.get("content")
        parsed: dict[str, Any] | None = None
        if isinstance(content, dict):
            parsed = content
        elif isinstance(content, str) and content.strip():
            try:
                candidate = json.loads(content)
            except json.JSONDecodeError:
                candidate = None
            if isinstance(candidate, dict):
                parsed = candidate
        if not parsed:
            continue
        if parsed.get("content_hash") != ref.content_hash:
            continue
        output = parsed.get("output")
        if isinstance(output, dict):
            return output
    return {}
