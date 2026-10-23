#!/usr/bin/env python3
"""Turn the memory API scale-out snapshot into cloud adapter metrics.

Usage: scale_metrics_payload.py <snapshot.json> <out.json> [instance-id]

Cluster-wide values carry no InstanceId, so every host that publishes them
reports the same series. The instance id only names the monitored resource on
GCP. Standard library only: runs on the host.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _metric(name: str, value: float, unit: str = "None", **dimensions: str) -> dict[str, Any]:
    entry: dict[str, Any] = {"name": name, "value": value, "unit": unit}
    if dimensions:
        entry["dimensions"] = dimensions
    return entry


def cloudwatch_metrics(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for lane in snapshot.get("lanes") or []:
        metrics += [
            _metric("LaneReadyTasks", lane["ready"], Lane=lane["lane"]),
            _metric("LaneHeldTasks", lane["held"], Lane=lane["lane"]),
            _metric("LaneOldestReadySeconds", lane["oldest_seconds"], "Seconds", Lane=lane["lane"]),
        ]
    jobs = snapshot.get("coding_jobs") or {}
    finished = jobs.get("finished") or {}
    metrics += [
        _metric("CodingJobsQueued", jobs.get("queued") or 0),
        _metric("CodingJobOldestQueuedSeconds", jobs.get("oldest_queued_seconds") or 0, "Seconds"),
        _metric("CodingJobsRunning", jobs.get("running") or 0),
        _metric("CodingJobsLost", finished.get("lost") or 0),
        _metric("CodingJobsFinishedTotal", sum(finished.values())),
        _metric("CodingJobLostRate", jobs.get("lost_rate") or 0),
        _metric("CodingJobAvgSeconds", jobs.get("avg_duration_seconds") or 0, "Seconds"),
    ]
    for outcome, count in finished.items():
        metrics.append(_metric("CodingJobsFinished", count, Outcome=outcome))
    for row in jobs.get("running_by_team") or []:
        metrics.append(_metric("CodingJobsRunning", row["running"], Team=row["team"]))
    for gate in snapshot.get("gates") or []:
        metrics += [
            _metric("GateWaiting", gate["waiting"], Gate=gate["gate"]),
            _metric("GateOldestWaitSeconds", gate["oldest_seconds"], "Seconds", Gate=gate["gate"]),
        ]
        if gate.get("decided"):
            metrics.append(_metric("GateAvgWaitSeconds", gate["avg_wait_seconds"], "Seconds", Gate=gate["gate"]))
    lowest = None
    for item in snapshot.get("github") or []:
        installation = f"{item['host']}/{item['installation_id']}"
        metrics += [
            _metric("GitHubRateLimitRemaining", item["remaining"], Installation=installation),
            _metric("GitHubRateLimitRemainingPercent", item["remaining_percent"], "Percent", Installation=installation),
        ]
        lowest = item["remaining_percent"] if lowest is None else min(lowest, item["remaining_percent"])
    if lowest is not None:
        metrics.append(_metric("GitHubRateLimitLowestPercent", lowest, "Percent"))
    for row in snapshot.get("llm") or []:
        metrics += [
            _metric("LlmInputTokens", row["window_input_tokens"], "Count", Team=row["team"]),
            _metric("LlmOutputTokens", row["window_output_tokens"], "Count", Team=row["team"]),
            _metric("LlmCostUsd", row["window_cost_usd"], Team=row["team"]),
            _metric("LlmMonthCostUsd", row["month_cost_usd"], Team=row["team"]),
        ]
    return metrics


def main(argv: list[str]) -> int:
    snapshot_path, out_path = argv[1:3]
    instance_id = argv[3] if len(argv) > 3 else ""
    try:
        snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        snapshot = {}
    payload = {
        "namespace": "TSHarness",
        "dimensions": {},
        "instance_id": instance_id,
        "metrics": cloudwatch_metrics(snapshot) if snapshot else [],
    }
    Path(out_path).write_text(json.dumps(payload), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
