"""Delivery quality metrics: do changes to the harness help?

Over the ticket and work-order runs that finished in a period (and, when asked, the ones still
running), filterable by project and repository like the History view, this reports per ticket
and in aggregate:

* first-try pass: merged with zero revisions of its coding step;
* revisions per ticket (median, mean, max), split by what triggered each one;
* time to merge (first coding start to merge), and time waiting for a person vs agent time;
* the pre-push review's catch rate: the share of coding runs whose review fixed at least one
  blocking issue, and how many it fixed;
* gate disagreement: the share of runs whose pre-push review passed (nothing blocking left)
  that the code review gate then blocked;
* cost per ticket (coding job usage), median and total, and the share spent on the stronger
  model;
* lost coding jobs;
* autonomy (the self-improvement target): the share of finished tickets that merged with no person
  stepping in, and which interventions the others had (see ts_harness.improvement.guard);
* the guard: problems after merge within ``guard_days`` (reverts, Jira bugs, nightly smoke
  failures) and arbiter overrules, with "unknown" where GitHub or Jira could not be read;
* the versions each ticket ran with (``_versions``, see ts_harness.improvement.versions), so every
  number can be grouped by any versions key (``group_by=variants.pre_push``).

Each ticket is read once: its workflow record (shared with History via
:func:`live_view.period_runs`) and its coding jobs (one query, inside :func:`ticket_story`).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any

from ts_harness.improvement import guard as gd
from ts_harness.improvement import versions as vs
from ts_harness.workflows import live_view as lv
from ts_harness.workflows import ticket_story as ts

AGENT_TOTALS = ("coding", "review", "qa")


def _input(task: dict[str, Any]) -> dict[str, Any]:
    return task.get("input") if isinstance(task.get("input"), dict) else {}


def _stats(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "mean": None, "max": None}
    return {"median": round(median(values), 2), "mean": round(mean(values), 2), "max": round(max(values), 2)}


def _share(part: float, whole: float) -> float | None:
    return round(part / whole, 3) if whole else None


def _review_ran(review: Any) -> bool:
    return isinstance(review, dict) and str(review.get("status") or "") not in {"", "no_review"}


def _review_passed(review: dict[str, Any]) -> bool:
    """The pre-push review's last round found nothing blocking (after any fixes)."""
    return str(review.get("status") or "") == "pass" and not review.get("open_blocking")


def ticket_metrics(story: dict[str, Any], tasks: list[dict[str, Any]], outcome: str,
                   strong_labels: set[str]) -> dict[str, Any]:
    """One ticket's numbers from its story (events and totals) and its tasks."""
    events = story.get("events") or []
    by_task = {str(t.get("id") or ""): t for t in tasks}
    coding = [e for e in events if e.get("kind") == "coding"]
    runs = [e for e in coding if e.get("job_status")]  # coding jobs, not steps without a job
    revisions = max((int(e.get("revision") or 0) for e in coding), default=0)
    by_trigger: dict[str, int] = {}
    seen: set[str] = set()
    for event in coding:
        task_id = str(event.get("task_id") or "")
        if int(event.get("revision") or 0) > 0 and task_id not in seen:
            seen.add(task_id)
            kind = str(event.get("trigger_kind") or "other")
            by_trigger[kind] = by_trigger.get(kind, 0) + 1

    first_start = min((lv._parse(e.get("at")) for e in coding if lv._parse(e.get("at"))), default=None)
    merged_at = next((lv._parse(e.get("at")) for e in events if e.get("kind") == "done" and e.get("title") == "Merged"), None)
    time_to_merge = ts._seconds(first_start, merged_at) if outcome == "merged" else None

    # Gate disagreement: the last successful run before each code review verdict.
    pending: dict[str, Any] | None = None
    compared = disagreed = 0
    for event in events:
        if event.get("kind") == "coding" and event.get("status") == "ok":
            pending = event
        elif event.get("kind") == "gate" and event.get("step") == "finalize_code_review" and event.get("status") in {"ok", "blocked"}:
            review = (pending or {}).get("review")
            if pending is not None and _review_ran(review) and _review_passed(review):
                compared += 1
                disagreed += event.get("status") == "blocked"
            pending = None

    reviewed = [e for e in runs if _review_ran(e.get("review"))]
    caught = [e for e in reviewed if int((e.get("review") or {}).get("fixed") or 0) >= 1]

    def strong(event: dict[str, Any]) -> bool:
        if str(_input(by_task.get(str(event.get("task_id") or "")) or {}).get("coding_model") or "").strip():
            return True
        return bool(event.get("model")) and event.get("model") in strong_labels

    cost = round(sum(float(e.get("cost_usd") or 0) for e in runs), 2)
    strong_cost = round(sum(float(e.get("cost_usd") or 0) for e in runs if strong(e)), 2)
    totals = {t["category"]: int(t["seconds"]) for t in story.get("totals") or []}
    stepped_in = gd.interventions(tasks)
    return {
        "revisions": revisions,
        "revisions_by_trigger": by_trigger,
        "first_try": outcome == "merged" and revisions == 0,
        "time_to_merge_seconds": time_to_merge,
        "you_seconds": totals.get("you", 0),
        "agent_seconds": sum(totals.get(k, 0) for k in AGENT_TOTALS),
        "ci_seconds": totals.get("ci", 0),
        "queue_seconds": totals.get("queue", 0),
        "coding_runs": len(runs),
        "reviewed_runs": len(reviewed),
        "review_caught_runs": len(caught),
        "review_fixed": sum(int((e.get("review") or {}).get("fixed") or 0) for e in reviewed),
        "gate_compared": compared,
        "gate_disagreed": disagreed,
        "cost_usd": cost,
        "strong_cost_usd": strong_cost,
        "lost_jobs": sum(1 for e in runs if e.get("job_status") == "lost"),
        "merged_at": lv._iso(merged_at) if outcome == "merged" else "",
        "interventions": stepped_in,
        "autonomous": outcome == "merged" and not stepped_in,
    }


def _week(iso: str) -> str:
    stamp = lv._parse(iso)
    if stamp is None:
        return ""
    return (stamp - timedelta(days=stamp.weekday())).date().isoformat()


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline numbers over ticket rows (see the module docstring)."""
    finished = [r for r in rows if r["outcome"] not in {"running", "replaced"}]
    merged = [r for r in rows if r["outcome"] == "merged"]
    revisions = [r["revisions"] for r in rows]
    by_trigger: dict[str, int] = {}
    for row in rows:
        for kind, count in row["revisions_by_trigger"].items():
            by_trigger[kind] = by_trigger.get(kind, 0) + count
    costs = [r["cost_usd"] for r in rows]
    total_cost = round(sum(costs), 2)
    reviewed = sum(r["reviewed_runs"] for r in rows)
    compared = sum(r["gate_compared"] for r in rows)
    you = sum(r["you_seconds"] for r in rows)
    agent = sum(r["agent_seconds"] for r in rows)
    first_try = sum(1 for r in finished if r["first_try"])
    autonomous = sum(1 for r in finished if r.get("autonomous"))
    by_kind: dict[str, int] = {}
    for row in finished:
        for kind in row.get("interventions") or []:
            by_kind[kind] = by_kind.get(kind, 0) + 1
    return {
        "tickets": len(rows),
        "finished": len(finished),
        "merged": len(merged),
        "first_try": first_try,
        "first_try_rate": _share(first_try, len(finished)),
        "revisions": {**_stats(revisions), "total": sum(revisions),
                      "by_trigger": {k: by_trigger[k] for k in ts.TRIGGERS if by_trigger.get(k)}},
        "time_to_merge_seconds": _stats([r["time_to_merge_seconds"] for r in merged if r["time_to_merge_seconds"] is not None]),
        "you_seconds": you,
        "agent_seconds": agent,
        "you_share": _share(you, you + agent),
        "coding_runs": sum(r["coding_runs"] for r in rows),
        "reviewed_runs": reviewed,
        "review_catch_rate": _share(sum(r["review_caught_runs"] for r in rows), reviewed),
        "review_fixed": sum(r["review_fixed"] for r in rows),
        "gate_compared": compared,
        "gate_disagreed": sum(r["gate_disagreed"] for r in rows),
        "gate_disagreement_rate": _share(sum(r["gate_disagreed"] for r in rows), compared),
        "cost_usd": {"median": round(median(costs), 2) if costs else None, "total": total_cost},
        "strong_cost_share": _share(sum(r["strong_cost_usd"] for r in rows), total_cost),
        "lost_jobs": sum(r["lost_jobs"] for r in rows),
        "autonomy": {
            "autonomous": autonomous,
            "rate": _share(autonomous, len(finished)),
            "merged_share": _share(autonomous, len(merged)),
            "intervened": sum(1 for r in finished if r.get("interventions")),
            "by_kind": {k: by_kind[k] for k in gd.INTERVENTION_KINDS if by_kind.get(k)},
        },
        "guard": gd.summarize(rows, next((int(r["guard"]["window_days"]) for r in rows
                                           if isinstance(r.get("guard"), dict) and r["guard"].get("window_days")),
                                          gd.DEFAULT_DAYS)),
    }


def weekly(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """First-try pass rate and mean revisions per week a ticket finished in, oldest first."""
    weeks: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        week = _week(row["finished_at"])
        if week and row["outcome"] not in {"running", "replaced"}:
            weeks.setdefault(week, []).append(row)
    return [
        {"week": week, "tickets": len(items),
         "first_try_rate": _share(sum(1 for r in items if r["first_try"]), len(items)),
         "autonomy_rate": _share(sum(1 for r in items if r.get("autonomous")), len(items)),
         "revisions_mean": round(mean(r["revisions"] for r in items), 2)}
        for week, items in sorted(weeks.items())
    ]


def delivery_metrics(
    memory: Any,
    *,
    tenant_id: str = "default",
    allowed: Any = None,
    days: int = 14,
    project: str = "",
    repo: str = "",
    include_running: bool = False,
    now: datetime | None = None,
    group_by: str = "",
    guard: bool = True,
    guard_days: int = gd.DEFAULT_DAYS,
    guard_context: gd.GuardContext | None = None,
) -> dict[str, Any]:
    """Delivery metrics for the period; see the module docstring.

    ``group_by`` is a dotted versions key (``variants.pre_push``, ``skills.governed-code-review``,
    ``models.coding``): the response then has ``groups``, one aggregate per value (tickets with no
    value are ``(none)``). ``guard=False`` skips the external post-merge lookups.
    """
    now = now or datetime.now(timezone.utc)
    collected: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], dict[str, Any]]] = []
    projects: set[str] = set()
    repos: set[str] = set()
    for run in lv.period_runs(memory, tenant_id=tenant_id, allowed=allowed, days=days,
                              include_running=include_running, now=now):
        view = lv.item_view(memory, run["workflow"], now=now)
        repo_name = lv._repo(view["pr_url"])
        if run["project"]:
            projects.add(run["project"])
        if repo_name:
            repos.add(repo_name)
        if (project and run["project"] != project) or (repo and repo_name != repo.lower()):
            continue
        base = {
            "workflow_id": view["workflow_id"], "program_id": run["program_id"], "program_label": run["program_label"],
            "jira_issue_key": view["jira_issue_key"], "label": view["label"], "pr_url": view["pr_url"],
            "project": run["project"], "repo": repo_name, **run["ended"],
        }
        collected.append((base, ts.ticket_story(memory, run["workflow"], now=now), run["tasks"], run["ended"]))
    # The stronger model is whatever a coding step was explicitly sent to (a revision, a guided
    # attempt): runs on that model count as stronger-model runs wherever they appear.
    strong_labels = {
        ts.model_label(str(_input(t).get("coding_model")))
        for _, _, tasks, _ in collected for t in tasks if str(_input(t).get("coding_model") or "").strip()
    }
    rows = [{**base, **ticket_metrics(story, tasks, ended["outcome"], strong_labels), "versions": vs.ticket_versions(tasks)}
            for base, story, tasks, ended in collected]
    days_window = max(1, min(int(guard_days or gd.DEFAULT_DAYS), 90))
    # Without the guard no lookup runs: the post-merge signals are "unknown", arbiter overrules still count.
    ctx = guard_context or gd.GuardContext(days=days_window, now=now, memory=memory,
                                           max_lookups=gd.GuardContext.max_lookups if guard else 0)
    tasks_by_row = {id(row): tasks for row, (_, _, tasks, _) in zip(rows, collected)}
    # The most recent merges first: they are the ones a change being tried would affect.
    for row in sorted(rows, key=lambda r: r.get("merged_at") or "", reverse=True):
        row["guard"] = {**gd.post_merge(ctx, row, tasks_by_row[id(row)], lv._parse(row.get("merged_at"))),
                        "window_days": ctx.days}
    rows.sort(key=lambda r: (-r["cost_usd"], -r["revisions"]))
    result = {
        "generated_at": lv._iso(now),
        "days": int(days or 14),
        "include_running": include_running,
        "summary": aggregate(rows),
        "weekly": weekly(rows),
        "tickets": rows,
        "facets": {"projects": sorted(projects), "repos": sorted(repos), "group_keys": group_keys(rows)},
    }
    if group_by.strip():
        result["group_by"] = group_by.strip()
        result["groups"] = groups(rows, group_by.strip())
    return result


NO_VALUE = "(none)"


def _leaves(versions: dict[str, Any], prefix: str = "") -> list[str]:
    keys: list[str] = []
    for key, value in versions.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            keys.extend(_leaves(value, f"{path}."))
        elif key != "schema" and value not in (None, ""):
            keys.append(path)
    return keys


def group_keys(rows: list[dict[str, Any]]) -> list[str]:
    """Every versions key some ticket in the period has, for the group-by choice."""
    return sorted({key for row in rows for key in _leaves(row.get("versions") or {})})


def groups(rows: list[dict[str, Any]], path: str) -> list[dict[str, Any]]:
    """One aggregate per value of the versions key ``path``, largest group first."""
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(vs.lookup(row.get("versions"), path) or NO_VALUE, []).append(row)
    return [
        {"value": value, "tickets": len(items), "summary": aggregate(items)}
        for value, items in sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0]))
    ]
