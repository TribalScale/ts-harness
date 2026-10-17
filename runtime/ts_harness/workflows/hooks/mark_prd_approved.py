"""Deprecated alias for mark_artifact_approved."""

from __future__ import annotations

from ts_harness.workflows.hooks import mark_artifact_approved
from ts_harness.workflows.registry import HookContext


def handle(ctx: HookContext):
    return mark_artifact_approved.handle(ctx)
