"""Deprecated alias for bridge_workflow."""

from __future__ import annotations

from ts_harness.workflows.hooks import bridge_workflow
from ts_harness.workflows.registry import HookContext


def handle(ctx: HookContext):
    extras = dict(ctx.extras or {})
    metadata = (ctx.extras.get("session").metadata or {}) if ctx.extras.get("session") else {}
    if "approved_artifact" not in extras:
        artifact = extras.get("approved_prd_artifact") or metadata.get("approved_prd_artifact")
        if artifact is not None:
            extras["approved_artifact"] = artifact
    ctx.extras = extras
    return bridge_workflow.handle(ctx)
