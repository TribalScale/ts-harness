#!/usr/bin/env python3
"""Run the agent red-team set once per case at temperature 0.

uv run --extra eval python scripts/evals/run_agent_redteam.py

Inspect AI is the live runner when it is installed. Without it, the same
cases go through EnterpriseAgent and the in-repo scorer.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runtime"))


def _run_inspect() -> int | None:
    try:
        from inspect_ai import eval as inspect_eval
    except ImportError:
        return None
    task_file = ROOT / "evals" / "agent_redteam" / "task.py"
    logs = inspect_eval(str(task_file))
    if not logs:
        return 1
    for log in logs:
        results = getattr(log, "results", None)
        if results is not None and getattr(results, "scores", None):
            for score in results.scores:
                metrics = getattr(score, "metrics", {}) or {}
                accuracy = metrics.get("accuracy")
                value = getattr(accuracy, "value", accuracy)
                if value is not None and float(value) < 1:
                    return 1
    return 0


def main() -> int:
    inspected = _run_inspect()
    if inspected is not None:
        return inspected
    from ts_harness.evals.redteam import run_live

    return 0 if run_live() else 1


if __name__ == "__main__":
    raise SystemExit(main())
