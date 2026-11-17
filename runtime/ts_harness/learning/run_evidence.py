"""Deterministic evidence from a finished program. No model calls."""

from __future__ import annotations

from typing import Any


def collect_run_evidence(bundle: dict[str, Any]) -> dict[str, Any]:
    """Signals a reviewer can turn into a note. A clean run has ``useful`` false."""
    signals: list[dict[str, str]] = []
    pr_urls: list[str] = []
    changed: list[str] = []
    for workflow in bundle.get("workflows") or []:
        for task in workflow.get("tasks") or []:
            if not isinstance(task, dict):
                continue
            output = task.get("output") if isinstance(task.get("output"), dict) else {}
            incoming = task.get("input") if isinstance(task.get("input"), dict) else {}
            ref = str(task.get("id") or task.get("step_name") or "")
            status = str(task.get("status") or "")
            if status in {"failed", "blocked"}:
                signals.append(
                    {
                        "kind": "task_status",
                        "ref": ref,
                        "excerpt": str(task.get("error") or output.get("error") or status)[:300],
                    }
                )
            for key in ("review_feedback", "qa_feedback"):
                text = str(output.get(key) or incoming.get(key) or "").strip()
                if text:
                    signals.append({"kind": key, "ref": ref, "excerpt": text[:300]})
            requests = output.get("human_change_requests") or incoming.get("human_change_requests") or []
            if isinstance(requests, list) and any(str(item).strip() for item in requests):
                signals.append(
                    {
                        "kind": "human_change_requests",
                        "ref": ref,
                        "excerpt": "; ".join(str(item).strip() for item in requests if str(item).strip())[:300],
                    }
                )
            findings = output.get("blocking_findings") or output.get("findings") or []
            if isinstance(findings, list) and findings:
                signals.append({"kind": "findings", "ref": ref, "excerpt": str(findings)[:300]})
            if str(output.get("merge_status") or "").lower() in {"rejected", "changes_requested", "closed"}:
                signals.append(
                    {
                        "kind": "merge_rejected",
                        "ref": ref,
                        "excerpt": str(output.get("merge_status")),
                    }
                )
            pr = str(output.get("pr_url") or "").strip()
            if pr:
                pr_urls.append(pr)
            files = output.get("changed_files") or []
            if isinstance(files, list):
                changed.extend(str(path) for path in files if str(path).strip())
            calls = output.get("tool_calls") or task.get("tool_calls") or []
            if isinstance(calls, list):
                for call in calls:
                    if isinstance(call, dict) and call.get("error"):
                        signals.append(
                            {
                                "kind": "tool_error",
                                "ref": str(call.get("name") or ref),
                                "excerpt": str(call.get("error"))[:300],
                            }
                        )
    return {
        "useful": bool(signals),
        "signals": signals,
        "pr_urls": pr_urls,
        "changed_files": changed[:40],
    }
