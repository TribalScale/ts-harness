"""OPA (Open Policy Agent) HTTP client for tool policy evaluation.

The client sends tool call context to an OPA sidecar over the HTTP REST API
(``POST /v1/data/<policy_path>``).  The Rego policy lives in
``policy/opa/allow.rego`` and evaluates to ``{"result": {"allow": true|false}}``.

Environment variables
---------------------
TS_HARNESS_OPA_URL
    Base URL of the OPA server (default: ``http://localhost:8181``).

Fail-closed semantics
---------------------
By default (``fail_closed=True``) the client returns ``"deny"`` whenever OPA
is unreachable or returns an unexpected response.  Set ``fail_closed=False``
to fail-open (e.g. during OPA warm-up in development); this is NOT recommended
for production.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Literal

import httpx

if TYPE_CHECKING:
    from ts_harness.tools.policy import ToolPolicyContext


Decision = Literal["allow", "deny"]

_DEFAULT_OPA_URL = "http://localhost:8181"
_DEFAULT_POLICY_PATH = "ts_harness/tools"


class OpaError(Exception):
    """Raised when OPA returns an unexpected HTTP error response."""


class OpaClient:
    """Thin HTTP client wrapping a running OPA sidecar.

    Parameters
    ----------
    opa_url:
        OPA base URL.  Overrides the ``TS_HARNESS_OPA_URL`` environment
        variable when provided explicitly.
    fail_closed:
        When ``True`` (default), any network or OPA error results in ``"deny"``.
        When ``False``, errors yield ``"allow"`` (fail-open / dev mode).
    timeout:
        HTTP request timeout in seconds.
    policy_path:
        OPA data path for the ``allow`` rule.  Corresponds to the Rego
        ``package`` declaration without the trailing ``/allow`` segment.
    """

    def __init__(
        self,
        opa_url: str | None = None,
        *,
        fail_closed: bool = True,
        timeout: float = 2.0,
        policy_path: str = _DEFAULT_POLICY_PATH,
    ):
        self.opa_url = (
            opa_url
            or os.environ.get("TS_HARNESS_OPA_URL")
            or _DEFAULT_OPA_URL
        ).rstrip("/")
        self.fail_closed = fail_closed
        self.timeout = timeout
        self.policy_path = policy_path.strip("/")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate_opa(self, tool_name: str, ctx: "ToolPolicyContext") -> Decision:
        """Query OPA and return ``'allow'`` or ``'deny'``.

        The full tool-call context is forwarded as the OPA ``input`` document
        so Rego rules can reference any field from ``ToolPolicyContext``.

        On network failure or unexpected response the client honours
        ``fail_closed``: returning ``'deny'`` (safe) or ``'allow'`` (dev).
        """
        input_doc = {
            "tool": tool_name,
            "tenant_id": ctx.tenant_id,
            "agent_id": ctx.agent_id,
            "source": ctx.source,
            "is_subagent": ctx.is_subagent,
            "channel_ingress": ctx.channel_ingress,
            "sandbox_restricted": ctx.sandbox_restricted,
            "model_id": ctx.model_id,
            "requires_egress": ctx.requires_egress,
            "workflow_linked": ctx.workflow_linked,
            "workflow_confirmed": ctx.workflow_confirmed,
            "linked_workflow_id": ctx.linked_workflow_id,
        }
        url = f"{self.opa_url}/v1/data/{self.policy_path}"
        try:
            resp = httpx.post(
                url,
                json={"input": input_doc},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            allowed = bool(data.get("result", {}).get("allow", False))
            return "allow" if allowed else "deny"
        except Exception:
            return "deny" if self.fail_closed else "allow"


# ---------------------------------------------------------------------------
# Module-level singleton — override in tests or at startup.
# ---------------------------------------------------------------------------

_opa_client: OpaClient | None = None


def get_opa_client() -> OpaClient | None:
    """Return the module-level OPA client, or ``None`` if OPA is not configured."""
    return _opa_client


def configure_opa(
    opa_url: str | None = None,
    *,
    fail_closed: bool = True,
    timeout: float = 2.0,
    policy_path: str = _DEFAULT_POLICY_PATH,
) -> OpaClient:
    """Initialise (or replace) the module-level OPA client and return it."""
    global _opa_client
    _opa_client = OpaClient(
        opa_url=opa_url,
        fail_closed=fail_closed,
        timeout=timeout,
        policy_path=policy_path,
    )
    return _opa_client
