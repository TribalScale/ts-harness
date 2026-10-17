"""Workflow hook implementations."""

from ts_harness.workflows.registry import HookRegistry

from . import (
    approve_gate,
    bridge_workflow,
    cancel_workflow,
    collect_delivery_results,
    complete_program_phase,
    compensate_on_failure,
    continue_delivery,
    expand_on_complete,
    fork_subworkflow,
    hydrate_input,
    mark_artifact_approved,
    mark_prd_approved,
    merge_outputs,
    notify_bound_channel,
    present_artifact_for_review,
    promote,
    register_program,
    revise_on_reject,
    wait_external,
)


def build_default_registry() -> HookRegistry:
    registry = HookRegistry()
    registry.register("promote", promote.handle)
    registry.register("revise_on_reject", revise_on_reject.handle)
    registry.register("hydrate_input", hydrate_input.handle)
    registry.register("approve_gate", approve_gate.handle)
    registry.register("expand_on_complete", expand_on_complete.handle)
    registry.register("notify_bound_channel", notify_bound_channel.handle)
    registry.register("present_specialist_output", present_artifact_for_review.handle)
    registry.register("present_artifact_for_review", present_artifact_for_review.handle)
    registry.register("compensate_on_failure", compensate_on_failure.handle)
    registry.register("cancel_workflow", cancel_workflow.handle)
    registry.register("wait_external", wait_external.handle)
    registry.register("fork_subworkflow", fork_subworkflow.handle)
    registry.register("merge_outputs", merge_outputs.handle)
    registry.register("mark_artifact_approved", mark_artifact_approved.handle)
    registry.register("mark_prd_approved", mark_prd_approved.handle)
    registry.register("bridge_workflow", bridge_workflow.handle)
    registry.register("continue_delivery", continue_delivery.handle)
    registry.register("register_program", register_program.handle)
    registry.register("complete_program_phase", complete_program_phase.handle)
    registry.register("collect_delivery_results", collect_delivery_results.handle)
    return registry
