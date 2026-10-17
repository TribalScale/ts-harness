"""Command approval guards adapted from Hermes approval patterns (MIT)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import json
import os
import re
import time
import uuid
from typing import Any

AWAITING_TOOL_APPROVAL_PREFIX = "awaiting_tool_approval:"

BLOCKED_PATTERNS = [
    re.compile(r"rm\s+-rf\s+/", re.I),
    re.compile(r"mkfs", re.I),
    re.compile(r":\(\)\s*\{", re.I),
    re.compile(r"\|\s*:\s*&", re.I),
    re.compile(r"sudo\s+", re.I),
    re.compile(r"passwd\b", re.I),
    re.compile(r"/etc/shadow", re.I),
    re.compile(r"\.env", re.I),
    re.compile(r"AWS_SECRET", re.I),
]


def scan_command(command: str) -> tuple[bool, str | None]:
    for pattern in BLOCKED_PATTERNS:
        if pattern.search(command):
            return False, f"blocked by approval policy: {pattern.pattern}"
    return True, None


def args_fingerprint(args: dict[str, Any]) -> str:
    payload = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass
class ApprovalRequest:
    approval_id: str
    tenant_id: str
    agent_id: str
    tool_name: str
    args: dict[str, Any]
    reason: str
    status: str = "pending"
    requested_at: str = ""
    decided_by_actor_id: str | None = None
    decided_by_roles: list[str] | None = None
    args_fingerprint: str = ""
    consumed_at: str | None = None
    workflow_id: str | None = None
    task_id: str | None = None


class ApprovalQueue:
    def __init__(self, path: str | Path | None = None):
        self._requests: dict[str, ApprovalRequest] = {}
        self.path = Path(path) if path is not None else None
        if self.path and self.path.exists():
            self._load()

    def request(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        tool_name: str,
        args: dict[str, Any],
        reason: str,
        workflow_id: str | None = None,
        task_id: str | None = None,
    ) -> ApprovalRequest:
        clean_args = {k: v for k, v in (args or {}).items() if k != "approval_id"}
        approval = ApprovalRequest(
            approval_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            agent_id=agent_id,
            tool_name=tool_name,
            args=clean_args,
            reason=reason,
            requested_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            args_fingerprint=args_fingerprint(clean_args),
            workflow_id=workflow_id,
            task_id=task_id,
        )
        self._requests[approval.approval_id] = approval
        self._persist(approval)
        return approval

    def get(self, approval_id: str) -> ApprovalRequest:
        return self._requests[approval_id]

    def list_pending(self, *, tenant_id: str | None = None) -> list[ApprovalRequest]:
        pending = [r for r in self._requests.values() if r.status == "pending"]
        if tenant_id:
            pending = [r for r in pending if r.tenant_id == tenant_id]
        return sorted(pending, key=lambda r: r.requested_at)

    def decided_for_task(self, workflow_id: str, task_id: str) -> list[ApprovalRequest]:
        decided = [
            r
            for r in self._requests.values()
            if r.workflow_id == workflow_id
            and r.task_id == task_id
            and r.status in {"approved", "denied"}
            and not r.consumed_at
        ]
        return sorted(decided, key=lambda r: r.requested_at)

    def decide(
        self,
        approval_id: str,
        *,
        approved: bool,
        actor_id: str = "system",
        roles: list[str] | None = None,
    ) -> ApprovalRequest:
        approval = self.get(approval_id)
        approval.status = "approved" if approved else "denied"
        approval.decided_by_actor_id = actor_id
        approval.decided_by_roles = list(roles or [])
        self._persist(approval)
        return approval

    def is_approved_and_matches(
        self,
        approval_id: str,
        *,
        tool_name: str,
        args: dict[str, Any],
        tenant_id: str,
        agent_id: str,
    ) -> bool:
        try:
            approval = self.get(approval_id)
        except KeyError:
            return False
        clean_args = {k: v for k, v in (args or {}).items() if k != "approval_id"}
        if approval.status != "approved":
            return False
        if approval.consumed_at:
            return False
        if approval.tenant_id != tenant_id or approval.agent_id != agent_id:
            return False
        if approval.tool_name != tool_name:
            return False
        return approval.args_fingerprint == args_fingerprint(clean_args)

    def consume(self, approval_id: str) -> ApprovalRequest:
        approval = self.get(approval_id)
        approval.consumed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._persist(approval)
        return approval

    def _persist(self, approval: ApprovalRequest) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(approval.__dict__, sort_keys=True) + "\n")

    def _load(self) -> None:
        assert self.path is not None
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not row.get("args_fingerprint"):
                row["args_fingerprint"] = args_fingerprint(row.get("args") or {})
            self._requests[row["approval_id"]] = ApprovalRequest(**row)


class ApprovalAlreadyDecided(ValueError):
    def __init__(self, approval: ApprovalRequest):
        super().__init__(f"approval already {approval.status}")
        self.approval = approval


def decide_and_resume(
    queue: ApprovalQueue,
    memory: Any,
    approval_id: str,
    *,
    approved: bool,
    actor_id: str,
    roles: list[str] | None = None,
) -> tuple[ApprovalRequest, dict[str, Any] | None]:
    """Record one decision and requeue the workflow task parked on it.

    Raises ``KeyError`` for an unknown id and ``ApprovalAlreadyDecided`` when the
    request is no longer pending. The resume dict is ``None`` for an unlinked request.
    """
    approval = queue.get(approval_id)
    if approval.status != "pending":
        raise ApprovalAlreadyDecided(approval)
    approval = queue.decide(approval_id, approved=approved, actor_id=actor_id, roles=roles)
    if not approval.task_id:
        return approval, None
    try:
        memory.resume_tool_approval(approval.task_id, approval.approval_id, actor=actor_id)
    except Exception as exc:  # noqa: BLE001
        from ts_harness.observability.logging import log_event

        log_event(
            "approval_resume_error",
            approval_id=approval_id,
            task_id=approval.task_id,
            error=str(exc),
        )
        return approval, {"status": "failed", "error": str(exc)}
    return approval, {"status": "resumed"}


_approval_queue = ApprovalQueue(os.environ.get("TS_HARNESS_APPROVAL_STORE") or None)


def get_approval_queue() -> ApprovalQueue:
    return _approval_queue
