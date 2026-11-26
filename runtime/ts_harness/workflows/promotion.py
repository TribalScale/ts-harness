"""Promote channel chat sessions to durable workflow instances."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ts_harness.agent.sessions import SessionRecord
from ts_harness.workflows.engine import WorkflowEngine, get_workflow_engine


@dataclass
class PromotionResult:
    workflow_id: str
    workflow_type: str
    task_ids: list[str]
    first_step: str
    binding_key: str


class WorkflowPromotionService:
    def __init__(self, engine: WorkflowEngine | None = None, client=None):
        if engine is not None:
            self.engine = engine
        elif client is not None:
            self.engine = WorkflowEngine(client=client)
        else:
            self.engine = get_workflow_engine()

    def start(
        self,
        *,
        session: SessionRecord,
        workflow_type: str,
        summary: str,
        tenant_id: str,
        binding_key: str,
        requester_actor_id: str | None = None,
        correlation_id: str | None = None,
        transcript_messages: list[dict[str, Any]] | None = None,
        extra_input: dict[str, Any] | None = None,
    ) -> PromotionResult:
        result = self.engine.start_promotion(
            session=session,
            workflow_type=workflow_type,
            summary=summary,
            tenant_id=tenant_id,
            binding_key=binding_key,
            requester_actor_id=requester_actor_id,
            correlation_id=correlation_id,
            transcript_messages=transcript_messages,
            extra_input=extra_input,
        )
        return PromotionResult(
            workflow_id=str(result["workflow_id"]),
            workflow_type=str(result["workflow_type"]),
            task_ids=list(result.get("task_ids") or []),
            first_step=str(result.get("first_step") or ""),
            binding_key=str(result.get("binding_key") or binding_key),
        )


def build_binding_key(channel: str, channel_id: str, thread_id: str) -> str:
    """``channel:channel_id:thread``, built once.

    A program started from a chat that already belongs to one receives that chat's binding key as
    its thread id (admin chat sends `web:web:<id>`), and composing it again stacked the prefix:
    `web:web:web:web:…`. A thread id that already carries the channel prefix is used as it is.
    """
    thread = str(thread_id or "")
    prefix = f"{channel}:{channel_id}:"
    while thread.startswith(prefix):
        thread = thread[len(prefix) :]
    if channel == "web":
        while thread.startswith("web:"):
            thread = thread[len("web:") :]
    return f"{channel}:{channel_id}:{thread}"
