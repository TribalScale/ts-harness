"""Reuse a passing code review or QA verdict when the PR's own diff has not changed.

A merge into the base branch sends every open PR on that base back through
refresh, CI, code review and QA. When the refresh merged cleanly, the PR still
changes the same files to the same content, so a second review or QA run has
nothing new to judge; the CI run against the new base is what covers the merge.
``review_precheck`` records a ``diff_fingerprint`` (file names, status and head
blob shas); a step reuses its last verdict only when that verdict passed and was
given on the same fingerprint. Anything else, including new human change
requests, runs the agent again.
"""

from __future__ import annotations

from typing import Any

CARRY_STEPS = frozenset({"code_review", "qa_gate"})
PRECHECK_STEP = "review_precheck"
# Any other status (blocked, failed, superseded, cancelled) is a verdict that did not pass.
UNSETTLED = frozenset({"pending", "ready", "running", ""})


def _by_id(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("id")): item for item in tasks if item.get("id")}


def _upstream_precheck(task: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """The review_precheck this task's inputs came from, following ``depends_on``."""
    seen: set[str] = set()
    frontier = [str(dep) for dep in task.get("depends_on") or []]
    while frontier:
        dep = frontier.pop(0)
        if dep in seen or dep not in by_id:
            continue
        seen.add(dep)
        item = by_id[dep]
        if item.get("step_name") == PRECHECK_STEP:
            return item
        frontier.extend(str(d) for d in item.get("depends_on") or [])
    return None


def _fingerprint(precheck: dict[str, Any] | None) -> str:
    if not precheck or precheck.get("status") != "succeeded":
        return ""
    output = precheck.get("output") if isinstance(precheck.get("output"), dict) else {}
    return str(output.get("diff_fingerprint") or "")


def _review_passed(review: dict[str, Any], tasks: list[dict[str, Any]]) -> bool:
    """finalize_code_review settles the verdict (agent findings plus deterministic ones)."""
    return any(
        item.get("step_name") == "finalize_code_review"
        and item.get("status") == "succeeded"
        and str(review.get("id")) in {str(dep) for dep in item.get("depends_on") or []}
        for item in tasks
    )


def _passed(prior: dict[str, Any], tasks: list[dict[str, Any]]) -> bool:
    if prior.get("status") != "succeeded" or not isinstance(prior.get("output"), dict):
        return False
    if prior.get("step_name") == "code_review":
        return _review_passed(prior, tasks)
    return str(prior["output"].get("status") or "") == "success"


def _with_current_heads(output: dict[str, Any], precheck: dict[str, Any]) -> dict[str, Any]:
    heads = {
        str(pr.get("pr_url")): str(pr.get("head_sha") or "")
        for pr in (precheck.get("output") or {}).get("prs") or []
        if isinstance(pr, dict)
    }
    result = dict(output.get("result") or {}) if isinstance(output.get("result"), dict) else None
    if result is None:
        return output
    prs = []
    for pr in result.get("prs") or []:
        if isinstance(pr, dict) and heads.get(str(pr.get("pr_url"))):
            pr = {**pr, "head_sha": heads[str(pr["pr_url"])]}
        prs.append(pr)
    return {**output, "result": {**result, "prs": prs}}


def carried_output(
    task: dict[str, Any], task_input: dict[str, Any], tasks: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """The earlier passing output for ``task``'s step, or ``None`` when the agent must run."""
    step = str(task.get("step_name") or "")
    if step not in CARRY_STEPS:
        return None
    if task_input.get("human_change_requests"):
        return None
    by_id = _by_id(tasks)
    current = _upstream_precheck(task, by_id)
    fingerprint = _fingerprint(current)
    if not fingerprint:
        return None
    created = str(task.get("created_at") or "")
    earlier = [
        item
        for item in tasks
        if item.get("step_name") == step
        and str(item.get("id")) != str(task.get("id"))
        and item.get("status") not in UNSETTLED
        and str(item.get("created_at") or "") < created
    ]
    if not earlier:
        return None
    prior = sorted(earlier, key=lambda item: str(item.get("created_at") or ""))[-1]
    if not _passed(prior, tasks):
        return None
    if _fingerprint(_upstream_precheck(prior, by_id)) != fingerprint:
        return None
    output = _with_current_heads(dict(prior["output"]), current or {})
    output["carried_forward"] = {
        "from_task_id": str(prior.get("id")),
        "diff_fingerprint": fingerprint,
        "reason": "The PR changes the same files to the same content as when this step last passed; "
        "only the base branch moved, and CI ran again on the new base.",
    }
    return output


def _identity_map(identities: list[Any]) -> dict[tuple[str, str], tuple[str, ...]]:
    out: dict[tuple[str, str], tuple[str, ...]] = {}
    for item in identities or []:
        if isinstance(item, (list, tuple)) and len(item) >= 5:
            out[(str(item[0]), str(item[1]))] = tuple(str(part) for part in item[2:])
    return out


def resolution_scope(
    precheck: dict[str, Any], tasks: list[dict[str, Any]], identities: list[Any]
) -> dict[str, Any] | None:
    """After an agent resolved a merge conflict: the PR files that differ from the last passing review.

    ``precheck`` is the running ``review_precheck`` task and ``identities`` its PR files as
    ``[pr_url, filename, previous_filename, status, blob]``. The newest code review that passed
    before it, and the precheck it read, give the reviewed identities; a file is in scope when it
    is new, gone or has other content. ``None`` when there is no passing review to compare with (or
    it predates ``file_identities``): the reviewer then reads the whole diff.
    """
    by_id = _by_id(tasks)
    created = str(precheck.get("created_at") or "")
    reviews = sorted(
        (
            item
            for item in tasks
            if item.get("step_name") == "code_review"
            and str(item.get("created_at") or "") < created
            and _passed(item, tasks)
        ),
        key=lambda item: str(item.get("created_at") or ""),
    )
    if not reviews:
        return None
    reviewed = _upstream_precheck(reviews[-1], by_id)
    output = (reviewed or {}).get("output") if isinstance((reviewed or {}).get("output"), dict) else {}
    if not isinstance(output.get("file_identities"), list):
        return None
    before, now = _identity_map(output["file_identities"]), _identity_map(identities)
    changed = sorted({key for key in set(before) | set(now) if before.get(key) != now.get(key)})
    return {
        "mode": "conflict_resolution",
        "files": [{"pr_url": pr_url, "filename": name, "removed": (pr_url, name) not in now} for pr_url, name in changed],
        "from_task_id": str(reviews[-1].get("id")),
    }
