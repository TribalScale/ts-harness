"""GitHub webhook bridge: merges, reviews, checks and deployments.

Each delivery is normalized into one event with a ``kind``:

* ``github.merge`` resumes the merge wait matched on ``pr_url``.
* ``github.pr_checks`` resumes a checks wait matched on ``head_sha`` once the
  checks on that SHA are terminal.
* ``github.deployment_status`` resumes a deployment wait matched on ``sha`` and
  ``environment`` once the deployment is terminal.
* ``github.pr_opened`` and ``github.pr_review`` go to subscribers (the
  independent-review publisher) instead of waits.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import logging
import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from ts_harness.memory.client import get_memory_client
from ts_harness.workflows.engine import get_workflow_engine

logger = logging.getLogger(__name__)

# Signed payloads carry no timestamp, so replays are refused by delivery id.
# Process-local: a restart forgets ids, which is acceptable for a single gateway.
DELIVERY_TTL_SECONDS = 24 * 3600
MAX_TRACKED_DELIVERIES = 10_000
_handled_deliveries: OrderedDict[str, float] = OrderedDict()
_deliveries_lock = threading.Lock()


def _prune_deliveries(now: float) -> None:
    while _handled_deliveries:
        oldest_id, handled_at = next(iter(_handled_deliveries.items()))
        if now - handled_at <= DELIVERY_TTL_SECONDS and len(_handled_deliveries) <= MAX_TRACKED_DELIVERIES:
            break
        _handled_deliveries.pop(oldest_id, None)


def delivery_already_handled(delivery_id: str, *, now: float | None = None) -> bool:
    with _deliveries_lock:
        _prune_deliveries(time.time() if now is None else now)
        return delivery_id in _handled_deliveries


def remember_delivery(delivery_id: str, *, now: float | None = None) -> None:
    """Record a delivery once it was handled, so a failed one can still be redelivered."""
    with _deliveries_lock:
        _handled_deliveries[delivery_id] = time.time() if now is None else now
        _handled_deliveries.move_to_end(delivery_id)
        _prune_deliveries(time.time() if now is None else now)

TERMINAL_DEPLOYMENT_STATES = frozenset({"success", "failure", "error", "inactive"})
TERMINAL_CHECK_STATES = frozenset({"success", "failure"})

# Lazily imported "module:function" subscribers per event kind.
EVENT_SUBSCRIBERS: dict[str, list[str]] = {
    "github.pr_opened": ["ts_harness.workflows.independent_review:on_github_event"],
    "github.pr_review": ["ts_harness.workflows.independent_review:on_github_event"],
    "github.merge": ["ts_harness.workflows.merge_readiness:on_merge"],
}


def verify_github_signature(raw_body: bytes, signature_header: str, secret: str) -> bool:
    """Validate ``X-Hub-Signature-256`` against ``secret``."""
    if not secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _repo_full_name(body: dict[str, Any]) -> str:
    return str((body.get("repository") or {}).get("full_name") or "")


def _login(user: Any) -> str:
    return str((user or {}).get("login") or "") if isinstance(user, dict) else ""


def parse_github_merge_event(body: dict[str, Any]) -> dict[str, str] | None:
    """Extract pr_url, merge_sha and merged_by from a pull_request closed+merged event."""
    if str(body.get("action") or "") != "closed":
        return None
    pr = body.get("pull_request")
    if not isinstance(pr, dict) or not pr.get("merged"):
        return None
    pr_url = str(pr.get("html_url") or "").strip()
    merge_sha = str(pr.get("merge_commit_sha") or "").strip()
    if not pr_url:
        return None
    return {
        "pr_url": pr_url,
        "merge_sha": merge_sha,
        "merged_by": _login(pr.get("merged_by")),
        "head_branch": str((pr.get("head") or {}).get("ref") or ""),
        "base_branch": str((pr.get("base") or {}).get("ref") or ""),
    }


def _infer_event_name(body: dict[str, Any]) -> str:
    if "deployment_status" in body:
        return "deployment_status"
    if "review" in body and "pull_request" in body:
        return "pull_request_review"
    if "check_suite" in body:
        return "check_suite"
    if "check_run" in body:
        return "check_run"
    if "pull_request" in body:
        return "pull_request"
    if "state" in body and "sha" in body and "context" in body:
        return "status"
    return ""


def parse_github_event(body: dict[str, Any], event_name: str | None = None) -> dict[str, Any] | None:
    """Normalize a webhook delivery; ``None`` for events TS Harness does not act on."""
    name = str(event_name or "").strip() or _infer_event_name(body)
    action = str(body.get("action") or "")
    repo = _repo_full_name(body)

    if name == "pull_request":
        merge = parse_github_merge_event(body)
        if merge:
            return {"kind": "github.merge", "repo": repo, **merge}
        if action in {"opened", "reopened", "synchronize", "ready_for_review"}:
            pr = body.get("pull_request") or {}
            return {
                "kind": "github.pr_opened",
                "repo": repo,
                "action": action,
                "pr_url": str(pr.get("html_url") or ""),
                "number": pr.get("number"),
                "head_sha": str((pr.get("head") or {}).get("sha") or ""),
                "base_branch": str((pr.get("base") or {}).get("ref") or ""),
                "author": _login(pr.get("user")),
            }
        return None

    if name == "pull_request_review":
        if action not in {"submitted", "dismissed", "edited"}:
            return None
        pr = body.get("pull_request") or {}
        review = body.get("review") or {}
        return {
            "kind": "github.pr_review",
            "repo": repo,
            "action": action,
            "pr_url": str(pr.get("html_url") or ""),
            "number": pr.get("number"),
            "head_sha": str((pr.get("head") or {}).get("sha") or ""),
            "reviewer": _login(review.get("user")),
            "review_state": str(review.get("state") or "").lower(),
            "commit_id": str(review.get("commit_id") or ""),
        }

    if name in {"check_suite", "check_run"}:
        item = body.get(name) or {}
        if action != "completed":
            return None
        return {
            "kind": "github.pr_checks",
            "repo": repo,
            "head_sha": str(item.get("head_sha") or ""),
            "conclusion": str(item.get("conclusion") or "").lower(),
            "source": name,
            "pr_urls": [
                f"https://github.com/{repo}/pull/{pr.get('number')}"
                for pr in item.get("pull_requests") or []
                if pr.get("number") and repo
            ],
        }

    if name == "status":
        return {
            "kind": "github.pr_checks",
            "repo": repo,
            "head_sha": str(body.get("sha") or ""),
            "context": str(body.get("context") or ""),
            "conclusion": str(body.get("state") or "").lower(),
            "source": "status",
        }

    if name == "deployment_status":
        status = body.get("deployment_status") or {}
        deployment = body.get("deployment") or {}
        return {
            "kind": "github.deployment_status",
            "repo": repo,
            "sha": str(deployment.get("sha") or ""),
            "ref": str(deployment.get("ref") or ""),
            "environment": str(status.get("environment") or deployment.get("environment") or ""),
            "state": str(status.get("state") or "").lower(),
            "deployment_id": deployment.get("id"),
            "target_url": str(status.get("target_url") or status.get("environment_url") or ""),
            "creator": _login(deployment.get("creator")),
        }
    return None


def wait_matches_event(wait: dict[str, Any], event: dict[str, Any]) -> bool:
    """Every key the wait pinned must match the event; multi-PR waits match any pinned head."""
    match = dict(wait.get("match") or {})
    if match:
        return all(key in event and str(event[key]) == str(expected) for key, expected in match.items())
    head_sha = str(event.get("head_sha") or "")
    heads = (wait.get("options") or {}).get("heads") or []
    return bool(head_sha) and any(str(h.get("head_sha") or "") == head_sha for h in heads if isinstance(h, dict))


def _resolve_subscriber(path: str) -> Callable[..., Any]:
    module_name, _, attr = path.partition(":")
    return getattr(importlib.import_module(module_name), attr)


def notify_subscribers(event: dict[str, Any], *, tenant_id: str) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for path in EVENT_SUBSCRIBERS.get(str(event.get("kind")), []):
        try:
            results.append({"subscriber": path, "result": _resolve_subscriber(path)(event, tenant_id=tenant_id)})
        except Exception as exc:  # noqa: BLE001
            logger.warning("github webhook subscriber %s failed: %s", path, exc)
            results.append({"subscriber": path, "error": str(exc)})
    return results


def _resume(workflow_id: str, event_type: str, payload: dict[str, Any], *, actor: str, tenant_id: str) -> dict[str, Any]:
    task_output = {**payload, "event_type": event_type, "external_event": payload}
    return get_workflow_engine().resume_external(
        workflow_id,
        event_type=event_type,
        payload=payload,
        actor=actor,
        tenant_id=tenant_id,
        task_output=task_output,
    )


def _handle_merge(event: dict[str, Any], *, tenant_id: str, actor: str) -> dict[str, Any]:
    memory = get_memory_client()
    waits = list(
        (memory.find_external_waits(event_type="github.merge", pr_url=event["pr_url"], tenant_id=tenant_id) or {}).get("waits")
        or []
    )
    base = {
        "kind": "github.merge",
        "pr_url": event["pr_url"],
        "merge_sha": event["merge_sha"],
        "merged_by": event.get("merged_by") or "",
    }
    if not waits:
        return {**base, "status": "no_match", "reason": "no blocked github.merge wait matched pr_url"}
    workflow_id = str(waits[0].get("workflow_id") or "")
    if not workflow_id:
        return {"status": "error", "error": "matched wait missing workflow_id"}
    payload = {
        "pr_url": event["pr_url"],
        "merge_sha": event["merge_sha"],
        "merge_commit_sha": event["merge_sha"],
    }
    if event.get("merged_by"):
        payload["merged_by"] = event["merged_by"]
    result = _resume(workflow_id, "github.merge", payload, actor=actor, tenant_id=tenant_id)
    return {**base, "status": result.get("status") or "resumed", "workflow_id": workflow_id, "resume": result}


def default_check_resolver(event: dict[str, Any], wait: dict[str, Any]) -> dict[str, Any]:
    """Recompute the aggregate check state over every PR head the wait pinned."""
    from ts_harness.integrations.github_client import GitHubClient
    from ts_harness.workflows.merge_readiness import aggregate_checks, pinned_heads

    heads = pinned_heads(wait)
    if not heads:
        raise ValueError("checks wait has no pinned PR heads")
    return aggregate_checks(GitHubClient.from_env(), heads)


def _handle_sha_wait(
    event: dict[str, Any],
    *,
    tenant_id: str,
    actor: str,
    ready: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any] | None],
) -> dict[str, Any]:
    memory = get_memory_client()
    kind = str(event["kind"])
    waits = list((memory.find_external_waits(event_type=kind, tenant_id=tenant_id) or {}).get("waits") or [])
    matched = [w for w in waits if wait_matches_event(w, event)]
    if not matched:
        return {"status": "no_match", "kind": kind, "reason": f"no blocked {kind} wait matched"}
    resumed: list[dict[str, Any]] = []
    pending: list[str] = []
    for wait in matched:
        workflow_id = str(wait.get("workflow_id") or "")
        try:
            payload = ready(event, wait)
        except Exception as exc:  # noqa: BLE001
            logger.warning("github %s wait %s not evaluated: %s", kind, workflow_id, exc)
            pending.append(workflow_id)
            continue
        if payload is None:
            pending.append(workflow_id)
            continue
        result = _resume(workflow_id, kind, payload, actor=actor, tenant_id=tenant_id)
        resumed.append({"workflow_id": workflow_id, "resume": result})
    return {
        "status": "resumed" if resumed else "pending",
        "kind": kind,
        "resumed": resumed,
        "pending": pending,
    }


def _checks_ready(resolver: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]):
    def ready(event: dict[str, Any], wait: dict[str, Any]) -> dict[str, Any] | None:
        from ts_harness.workflows.merge_readiness import checks_payload

        aggregate = resolver(event, wait)
        if aggregate.get("state") not in TERMINAL_CHECK_STATES:
            return None
        return checks_payload(aggregate, warnings=(wait.get("options") or {}).get("warnings"))

    return ready


def _deployment_ready(event: dict[str, Any], wait: dict[str, Any]) -> dict[str, Any] | None:
    if event.get("state") not in TERMINAL_DEPLOYMENT_STATES:
        return None
    return {
        key: event.get(key)
        for key in ("sha", "environment", "state", "deployment_id", "target_url", "repo", "creator")
    }


def handle_github_webhook(
    body: dict[str, Any],
    *,
    tenant_id: str = "default",
    actor: str = "github-webhook",
    event_name: str | None = None,
    check_resolver: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Route one delivery to its wait and subscribers."""
    event = parse_github_event(body, event_name)
    if event is None:
        return {"status": "ignored", "reason": "event not handled"}

    kind = event["kind"]
    subscribers = notify_subscribers(event, tenant_id=tenant_id)
    if kind == "github.merge":
        result = _handle_merge(event, tenant_id=tenant_id, actor=actor)
    elif kind == "github.pr_checks":
        if not event.get("head_sha"):
            return {"status": "ignored", "reason": "checks event without head_sha"}
        result = _handle_sha_wait(
            event, tenant_id=tenant_id, actor=actor, ready=_checks_ready(check_resolver or default_check_resolver)
        )
    elif kind == "github.deployment_status":
        result = _handle_sha_wait(event, tenant_id=tenant_id, actor=actor, ready=_deployment_ready)
    else:
        result = {"status": "notified", "kind": kind, "pr_url": event.get("pr_url")}
    if subscribers:
        result["subscribers"] = subscribers
    return result


def github_webhook_secret() -> str:
    return os.environ.get("GITHUB_WEBHOOK_SECRET", "").strip()
