"""The QA gate's earlier rounds on the same work order, so its next verdict stays consistent.

A QA gate that blocks sends the PR back; the revised PR comes back to a fresh QA call that
knew nothing of the last one. It then found new differences each round (first the surfaces,
then the progress bars, then a missing cluster), and reversed its own layout rulings. The
runner now gives every ``qa_gate`` call ``previous_qa_rounds``: each earlier QA verdict on this
workflow, oldest first, with its defects and failed criteria. The QA skill keeps its earlier
rulings unless the work order changed (a ``## Scope amendment``), and checks each earlier
defect again before looking for new ones.
"""

from __future__ import annotations

from typing import Any

MAX_ROUNDS = 4
MAX_DEFECTS = 20
MAX_TEXT = 600


def _short(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_TEXT]
    if isinstance(value, dict):
        return {k: _short(v) for k, v in list(value.items())[:12]}
    return value


def previous_qa_rounds(tasks: list[dict[str, Any]], current_task_id: str = "") -> list[dict[str, Any]]:
    """Earlier QA verdicts of this workflow, oldest first (at most ``MAX_ROUNDS``, the latest kept)."""
    rounds: list[dict[str, Any]] = []
    earlier = sorted(
        (t for t in tasks if t.get("step_name") == "qa_gate" and str(t.get("id")) != str(current_task_id)),
        key=lambda t: str(t.get("created_at") or ""),
    )
    for task in earlier:
        output = task.get("output") if isinstance(task.get("output"), dict) else {}
        result = output.get("result") if isinstance(output.get("result"), dict) else {}
        prs = [pr for pr in result.get("prs") or [] if isinstance(pr, dict)]
        if not prs:
            continue
        rounds.append(
            {
                "round": len(rounds) + 1,
                "status": str(output.get("status") or task.get("status") or ""),
                "at": str(task.get("updated_at") or task.get("created_at") or ""),
                "prs": [
                    {
                        "pr_url": str(pr.get("pr_url") or ""),
                        "status": str(pr.get("status") or ""),
                        "defects": [_short(d) for d in (pr.get("defects") or [])[:MAX_DEFECTS]],
                        "failed_criteria": [
                            _short(str(c.get("criterion") or ""))
                            for c in pr.get("acceptance_criteria") or []
                            if isinstance(c, dict) and str(c.get("status") or "").lower() == "fail"
                        ][:MAX_DEFECTS],
                        "notes": [_short(n) for n in (pr.get("notes") or [])[:MAX_DEFECTS]],
                    }
                    for pr in prs
                ],
            }
        )
    return rounds[-MAX_ROUNDS:]
