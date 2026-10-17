package ts_harness.tools

# Default: deny unless a rule explicitly allows.
default allow := false

# ------------------------------------------------------------------
# Global hard-deny list — never allow these tool names regardless
# of any other context.  This mirrors GLOBAL_DENY_TOOLS in schema.py.
# ------------------------------------------------------------------
_global_deny := {"browser", "gateway"}

# ------------------------------------------------------------------
# Allow: tool is not in the global deny list and not otherwise blocked.
# ------------------------------------------------------------------
allow {
    not _global_deny[input.tool]
    not _blocked
}

# ------------------------------------------------------------------
# Blocking rules — each adds a reason to block the tool call.
# ------------------------------------------------------------------

# Sandbox-restricted mode blocks write/exec tools.
_blocked {
    input.sandbox_restricted == true
    _sandbox_tools[input.tool]
}

# Channel ingress path may not invoke elevated tools.
_blocked {
    input.channel_ingress == true
    _channel_ingress_deny[input.tool]
}

# Workflow gate tools require a linked workflow session.
_blocked {
    input.channel_ingress == true
    input.workflow_linked != true
    _workflow_gate_tools[input.tool]
}

# start_workflow requires explicit confirmation on channel ingress.
_blocked {
    input.channel_ingress == true
    input.tool == "start_workflow"
    input.workflow_confirmed != true
}

# Subagents may not re-delegate.
_blocked {
    input.is_subagent == true
    input.tool == "delegate_task"
}

# ------------------------------------------------------------------
# Tool sets referenced by blocking rules
# ------------------------------------------------------------------

_sandbox_tools := {"exec", "write_file", "patch_file"}

_channel_ingress_deny := {"exec", "write_file", "delegation", "workflow_update"}

_workflow_gate_tools := {
    "workflow_approve",
    "workflow_reject",
    "workflow_reject_with_feedback",
}
