"""Draft knowledge notes from runs that failed and were then fixed.

A ticket's run hit a problem and a later attempt got past it:

* ``repeated_finding`` - code review or QA raised the same finding in two rounds and a later
  revision passed that gate (not accepted over it by a person);
* ``ci_fixed`` - CI failed on the PR and passed after a revision;
* ``guidance`` - a step stopped for a person, who retried or revised it with guidance (or the
  stuck-step arbiter retried or revised it), and the step then succeeded;
* ``coding_retry`` - a coding run failed for a reason in the work (not the machine) and a later
  run of the same step succeeded.

Finding these is deterministic. For each program with at least one, one call to the revision
diagnosis model (``revision_guidance.diagnosis_model``) turns them into at most two reusable
lessons ("In org/api, X; do Y"). Each lesson becomes a *proposed* note (``source='drafted'``)
with its evidence, unless an active or proposed note already says the same thing. Nothing is
activated here: drafts wait in the Inbox like every other proposal.

The system job ``draft_failure_notes`` runs hourly, makes at most ``max_model_calls`` model
calls, and stamps each program it looked at (``notes_drafted_at``) so it is drafted from once.
With no diagnosis model configured it does nothing.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable

from ts_harness.workflows import revision_guidance as rg

logger = logging.getLogger(__name__)

CURSOR_NAMESPACE = "knowledge_notes"
CURSOR_KEY = "failure_draft_cursor"
DRAFT_STAMP = "notes_drafted_at"
ACTOR = "harness-drafter"
ACCEPT_KEY = "_human_accept"
ARBITER_KEY = "_arbiter"
CI_STEP = "await_ci_on_sha"
MAX_EPISODES = 6
MAX_DRAFTS_PER_PROGRAM = 2
DEFAULT_MAX_MODEL_CALLS = 3
MAX_PROGRAMS = 20
DUPLICATE_WORDS = 0.5
DUPLICATE_SIMILARITY = 0.9
_INFRA = re.compile(
    r"signal|killed|sigkill|sigterm|timed? ?out|timeout|out of memory|oom|stalled|rate limit|429|"
    r"overloaded|connection (reset|refused)|network|provider|awaiting_external|lost",
    re.I,
)
_WORD = re.compile(r"[a-z0-9][a-z0-9_]{2,}")

ModelCall = Callable[[str], str]


def enabled() -> bool:
    return os.environ.get("TS_HARNESS_FAILURE_DRAFTS", "on").strip().lower() not in {"0", "off", "false", "no"}


def max_model_calls() -> int:
    try:
        return max(0, int(os.environ.get("TS_HARNESS_FAILURE_DRAFTS_MAX_CALLS") or DEFAULT_MAX_MODEL_CALLS))
    except ValueError:
        return DEFAULT_MAX_MODEL_CALLS


# --- episodes -------------------------------------------------------------------------------------


def _input(task: dict[str, Any]) -> dict[str, Any]:
    raw = task.get("input")
    return raw if isinstance(raw, dict) else {}


def _output(task: dict[str, Any]) -> dict[str, Any]:
    raw = task.get("output")
    return raw if isinstance(raw, dict) else {}


def _ordered(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted((t for t in tasks if isinstance(t, dict)), key=lambda t: str(t.get("created_at") or ""))


def _passed(task: dict[str, Any] | None) -> bool:
    """Succeeded on its own merits: a person accepting it over its findings is not a fix."""
    return bool(task) and task.get("status") == "succeeded" and not _input(task).get(ACCEPT_KEY)


def _latest(tasks: list[dict[str, Any]], step: str) -> dict[str, Any] | None:
    found = [t for t in tasks if t.get("step_name") == step]
    return found[-1] if found else None


def _later_pass(tasks: list[dict[str, Any]], task: dict[str, Any]) -> bool:
    """This step (this task, or a later task of the same step) succeeded."""
    if _passed(task):
        return True
    step = task.get("step_name")
    after = str(task.get("created_at") or "")
    return any(_passed(t) for t in tasks if t.get("step_name") == step and str(t.get("created_at") or "") > after)


def _repeated_findings(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    episodes = []
    for gate in sorted(rg.GUIDED_GATES):
        rounds = rg.review_rounds(tasks, gate)
        if len(rounds) < 2:
            continue
        repeated: list[dict[str, Any]] = []
        for end in range(2, len(rounds) + 1):
            for item in rg.repeated_findings(rounds[:end]):
                if item not in repeated:
                    repeated.append(item)
        if not repeated or not _passed(_latest(tasks, gate)):
            continue
        fixes = [
            str(_output(t).get("summary") or "").strip()
            for t in tasks
            if _input(t).get("_revision_source") == gate and t.get("status") == "succeeded"
        ]
        guided = [
            str((_input(t).get("_guidance") or {}).get("approach") or "")
            for t in tasks
            if _input(t).get("_guided") and isinstance(_input(t).get("_guidance"), dict)
        ]
        episodes.append(
            {
                "kind": "repeated_finding",
                "step": gate,
                "problem": "; ".join(rg._finding_line(item).lstrip("- ") for item in repeated[:4]),
                "fix": " | ".join(text for text in [*guided, *fixes][-3:] if text),
                "task_ids": [str(r.get("task_id")) for r in rounds if r.get("task_id")][-4:],
            }
        )
    return episodes


def _ci_fixed(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runs = [t for t in tasks if t.get("step_name") == CI_STEP]
    failed = [
        t for t in runs
        if _output(t).get("failed_checks") or (t.get("status") in {"failed", "blocked"} and not _INFRA.search(str(t.get("error") or "")))
    ]
    if not failed:
        return []
    last_failed = str(failed[-1].get("created_at") or "")
    fixed = [
        t for t in runs
        if str(t.get("created_at") or "") > last_failed and t.get("status") == "succeeded" and not _output(t).get("failed_checks")
    ]
    if not fixed:
        return []
    checks = []
    for t in failed:
        for name in _output(t).get("failed_checks") or []:
            if str(name) not in checks:
                checks.append(str(name))
    revisions = [t for t in tasks if _input(t).get("_revision_budget") == "checks"]
    feedback = " ".join(str(_input(t).get("qa_feedback") or _input(t).get("review_feedback") or "")[:600] for t in revisions[-2:])
    fixes = " | ".join(str(_output(t).get("summary") or "")[:400] for t in revisions[-2:] if t.get("status") == "succeeded")
    return [
        {
            "kind": "ci_fixed",
            "step": CI_STEP,
            "problem": f"CI failed: {', '.join(checks[:6]) or str(failed[-1].get('error') or 'checks failed')[:200]}. {feedback}".strip(),
            "fix": fixes,
            "task_ids": [str(t.get("id")) for t in [*failed[-2:], fixed[-1]] if t.get("id")],
        }
    ]


def _guidance(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    episodes = []
    for task in tasks:
        data = _input(task)
        grant = data.get("_revision_grant") if isinstance(data.get("_revision_grant"), dict) else {}
        arbiter = data.get(ARBITER_KEY) if isinstance(data.get(ARBITER_KEY), dict) else {}
        acted = [a for a in arbiter.get("actions") or [] if isinstance(a, dict) and a.get("action") in {"retry", "revise"}]
        text = str(data.get("human_guidance") or grant.get("guidance") or "").strip()
        who = "person"
        if not text and acted:
            text = str(arbiter.get("reason") or acted[-1].get("reason") or "").strip()
            who = "arbiter"
        if not text or not _later_pass(tasks, task):
            continue
        episodes.append(
            {
                "kind": "guidance",
                "step": str(task.get("step_name") or ""),
                "problem": str(task.get("error") or arbiter.get("category") or "the step stopped for a decision")[:600],
                "fix": f"{who} guidance: {text[:900]}",
                "task_ids": [str(task.get("id"))] if task.get("id") else [],
            }
        )
    return episodes


def _job_error(job: dict[str, Any]) -> str:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    parts = [str(result.get("error") or job.get("error") or "")]
    verification = result.get("verification") if isinstance(result.get("verification"), dict) else {}
    if verification:
        parts.append("verification: " + json.dumps(verification, default=str)[:500])
    if result.get("log_tail"):
        parts.append("log tail: " + str(result["log_tail"])[-500:])
    return " ".join(part for part in parts if part.strip())


def _coding_retries(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_task: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        if isinstance(job, dict) and job.get("task_id"):
            by_task.setdefault(str(job["task_id"]), []).append(job)
    episodes = []
    for task_id, runs in by_task.items():
        runs.sort(key=lambda job: str(job.get("created_at") or ""))
        failed = [job for job in runs if job.get("status") == "failed" and not _INFRA.search(_job_error(job)[:400])]
        if not failed:
            continue
        after = str(failed[-1].get("created_at") or "")
        fixed = [job for job in runs if job.get("status") == "succeeded" and str(job.get("created_at") or "") > after]
        if not fixed:
            continue
        result = fixed[-1].get("result") if isinstance(fixed[-1].get("result"), dict) else {}
        episodes.append(
            {
                "kind": "coding_retry",
                "step": "coding run",
                "problem": _job_error(failed[-1])[:900],
                "fix": str(result.get("summary") or "")[:600],
                "task_ids": [task_id],
            }
        )
    return episodes


def find_episodes(tasks: list[dict[str, Any]], jobs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Failed-then-fixed episodes in one workflow, at most ``MAX_EPISODES``. No model calls."""
    ordered = _ordered(tasks)
    found = [*_repeated_findings(ordered), *_ci_fixed(ordered), *_guidance(ordered), *_coding_retries(jobs or [])]
    return found[:MAX_EPISODES]


# --- drafting -------------------------------------------------------------------------------------


PROMPT = """You maintain short knowledge notes that every future automated coding run for a project reads.

In the runs below a problem came up and a later attempt fixed it. Write the reusable lesson, if
there is one, so the next run gets it right the first time. Skip anything specific to this one
ticket, anything already obvious, and causes outside the code (flaky machines, outages).

Repository: {repo}
Project: {project}
Work order (head): {work_order}

Episodes:
{episodes}

Reply with JSON only, at most {max_notes} notes, or {{"notes": []}} when there is no reusable lesson:
{{"notes": [{{"scope": "project" or "org" (org only when it holds for any repository),
  "kind": "avoid" or "howto",
  "title": "at most 100 characters",
  "body": "at most 280 characters, in the form 'In {repo_short}, X; do Y'",
  "why": "one sentence on what went wrong without it",
  "applies_to": ["path globs such as src/db/** or stage names such as finalize_code_review"],
  "episodes": [1]}}]}}
"""


def _episode_lines(episodes: list[dict[str, Any]]) -> str:
    lines = []
    for number, item in enumerate(episodes, 1):
        lines.append(
            f"{number}. [{item['kind']}] step {item.get('step') or '?'}\n"
            f"   problem: {str(item.get('problem') or '')[:900]}\n"
            f"   what fixed it: {str(item.get('fix') or '(not recorded)')[:900]}"
        )
    return "\n".join(lines)


def _repo_name(url: str) -> str:
    match = re.search(r"github\.com[/:]([^/\s]+/[^/\s#?]+?)(?:\.git)?(?:[/#?]|$)", str(url or ""))
    return match.group(1) if match else ""


def parse_drafts(reply: str) -> list[dict[str, Any]]:
    """Clean notes from the model reply (empty on anything unusable)."""
    match = re.search(r"\{.*\}", reply or "", re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    notes = data.get("notes") if isinstance(data, dict) else None
    drafts = []
    for raw in notes if isinstance(notes, list) else []:
        if not isinstance(raw, dict):
            continue
        title = " ".join(str(raw.get("title") or "").split())[:120]
        body = " ".join(str(raw.get("body") or "").split())[:300]
        if not title or not body:
            continue
        applies = [str(item).strip()[:200] for item in raw.get("applies_to") or [] if str(item).strip()][:20]
        refs = [int(n) for n in raw.get("episodes") or [] if isinstance(n, (int, float)) or str(n).isdigit()]
        drafts.append(
            {
                "scope": "org" if str(raw.get("scope") or "") == "org" else "project",
                "kind": "howto" if str(raw.get("kind") or "") == "howto" else "avoid",
                "title": title,
                "body": body,
                "why": " ".join(str(raw.get("why") or "").split())[:500],
                "applies_to": applies,
                "episodes": refs,
            }
        )
    return drafts[:MAX_DRAFTS_PER_PROGRAM]


def _words(note: dict[str, Any]) -> set[str]:
    return set(_WORD.findall(f"{note.get('title') or ''} {note.get('body') or ''}".lower()))


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def near_duplicate(draft: dict[str, Any], existing: list[dict[str, Any]], *, similar: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """The existing note this draft repeats, or None.

    Same title, at least half the words in common, or (when the search had embeddings) a cosine
    similarity of ``DUPLICATE_SIMILARITY`` or more.
    """
    words = _words(draft)
    for note in existing:
        if _norm(note.get("title")) == _norm(draft.get("title")):
            return note
        other = _words(note)
        if words and other and len(words & other) / len(words | other) >= DUPLICATE_WORDS:
            return note
    ids = {str(note.get("id")) for note in existing}
    for hit in similar or []:
        if str(hit.get("id")) in ids and float(hit.get("similarity") or 0) >= DUPLICATE_SIMILARITY:
            return hit
    return None


def _evidence(program_id: str, workflow_id: str, episodes: list[dict[str, Any]], pr_url: str) -> list[dict[str, str]]:
    first = episodes[0] if episodes else {}
    items = [
        {
            "type": "drafted_from",
            "ref": workflow_id,
            "url": pr_url[:500],
            "excerpt": f"{first.get('kind', 'run')}: {str(first.get('problem') or '')}"[:300],
        },
        {"type": "program", "ref": program_id, "url": "", "excerpt": ""},
        {"type": "workflow", "ref": workflow_id, "url": "", "excerpt": ""},
    ]
    if pr_url:
        items.append({"type": "pr", "ref": "", "url": pr_url[:500], "excerpt": ""})
    for item in episodes:
        for task_id in item.get("task_ids") or []:
            entry = {"type": "task", "ref": str(task_id)[:200], "url": "", "excerpt": str(item.get("kind") or "")}
            if entry not in items:
                items.append(entry)
    return items[:20]


def _existing(memory: Any, tenant_id: str, project_id: str) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []
    for status in ("active", "proposed"):
        try:
            listed = memory.list_knowledge_notes(tenant_id=tenant_id, status=status)
        except Exception:  # noqa: BLE001
            logger.warning("failure drafts could not list %s notes", status)
            continue
        for note in listed.get("notes") or []:
            if note.get("scope") == "org" or str(note.get("project_id") or "") == project_id:
                notes.append(note)
    return notes


def _similar(memory: Any, tenant_id: str, draft: dict[str, Any]) -> list[dict[str, Any]]:
    if not hasattr(memory, "search_knowledge_notes"):
        return []
    try:
        found = memory.search_knowledge_notes(tenant_id=tenant_id, q=f"{draft['title']} {draft['body']}", limit=5)
    except Exception:  # noqa: BLE001 - word overlap still applies
        return []
    return [hit for hit in found.get("notes") or [] if hit.get("status") in {"active", "proposed"}]


def draft_program(
    memory: Any,
    program: dict[str, Any],
    workflows: list[dict[str, Any]],
    *,
    model_call: ModelCall,
) -> dict[str, Any]:
    """Draft notes for one program's workflows ({'written': [...], 'skipped': [...]}). One model call."""
    tenant_id = str(program.get("tenant_id") or "default")
    program_id = str(program.get("id") or "")
    project_id = str(program.get("project_ref") or program.get("project_id") or "")
    episodes: list[dict[str, Any]] = []
    workflow_id = ""
    pr_url = ""
    work_order = ""
    for item in workflows:
        found = item.get("episodes") or []
        if found and not workflow_id:
            workflow_id = str(item.get("workflow_id") or "")
        episodes.extend(found)
        pr_url = pr_url or str(item.get("pr_url") or "")
        work_order = work_order or str(item.get("work_order") or "")
    episodes = episodes[:MAX_EPISODES]
    metadata = program.get("metadata") if isinstance(program.get("metadata"), dict) else {}
    artifacts = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), dict) else {}
    repo = _repo_name(str(artifacts.get("repo_url") or pr_url)) or "this repository"
    project_key = ""
    if project_id and hasattr(memory, "get_project"):
        try:
            project_key = str((memory.get_project(project_id) or {}).get("key") or "")
        except Exception:  # noqa: BLE001
            project_key = ""
    prompt = PROMPT.format(
        repo=repo,
        repo_short=repo,
        project=project_key or "(none)",
        work_order=(work_order or str(program.get("summary") or ""))[:1500] or "(not available)",
        episodes=_episode_lines(episodes),
        max_notes=MAX_DRAFTS_PER_PROGRAM,
    )
    drafts = parse_drafts(model_call(prompt))
    existing = _existing(memory, tenant_id, project_id)
    written: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for draft in drafts:
        scope = draft["scope"]
        if scope == "project" and not project_id:
            skipped.append({"title": draft["title"], "reason": "the program has no project"})
            continue
        duplicate = near_duplicate(draft, existing, similar=_similar(memory, tenant_id, draft))
        if duplicate is not None:
            skipped.append({"title": draft["title"], "reason": "near duplicate", "note_id": duplicate.get("id")})
            continue
        chosen = [episodes[n - 1] for n in draft["episodes"] if 0 < n <= len(episodes)] or episodes
        body: dict[str, Any] = {
            "tenant_id": tenant_id,
            "scope": scope,
            "kind": draft["kind"],
            "title": draft["title"],
            "body": draft["body"],
            "why": draft["why"],
            "applies_to": draft["applies_to"],
            "evidence": _evidence(program_id, workflow_id, chosen, pr_url),
            "source": "drafted",
            "status": "proposed",
            "proposal_action": "add",
            "actor": ACTOR,
            "change_reason": f"drafted from run {workflow_id or program_id}",
        }
        if scope == "project":
            body["project_id"] = project_id
        try:
            created = memory.create_knowledge_note(**body)
        except Exception as exc:  # noqa: BLE001 - the other draft can still be written
            skipped.append({"title": draft["title"], "reason": str(exc)[:200]})
            continue
        written.append(created if isinstance(created, dict) else {"id": created})
        existing.append({**body, "id": (created or {}).get("id") if isinstance(created, dict) else None})
    return {"written": written, "skipped": skipped}


def _program_workflows(memory: Any, loaded: dict[str, Any]) -> list[dict[str, Any]]:
    """Episodes per workflow of a program, with the PR and work order for the prompt."""
    items = []
    for workflow in (loaded or {}).get("workflows") or []:
        workflow_id = str((workflow or {}).get("id") or "")
        if not workflow_id:
            continue
        try:
            detail = memory.get_workflow(workflow_id)
        except Exception:  # noqa: BLE001
            logger.warning("failure drafts could not load workflow %s", workflow_id)
            continue
        tasks = list((detail or {}).get("tasks") or [])
        jobs: list[dict[str, Any]] = []
        if hasattr(memory, "list_coding_jobs"):
            try:
                jobs = list(memory.list_coding_jobs(workflow_id=workflow_id, limit=50) or [])
            except Exception:  # noqa: BLE001 - the task signals still count
                jobs = []
        pr_url = next((str(_output(t).get("pr_url")) for t in reversed(_ordered(tasks)) if _output(t).get("pr_url")), "")
        items.append(
            {
                "workflow_id": workflow_id,
                "episodes": find_episodes(tasks, jobs),
                "pr_url": pr_url,
                "work_order": rg.work_order_text(tasks),
            }
        )
    return items


def _now(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def draft_failure_notes(
    *,
    memory: Any = None,
    model_call: ModelCall | None = None,
    now: datetime | None = None,
    max_calls: int | None = None,
) -> dict[str, Any]:
    """System job: draft proposed notes from programs finished since the cursor.

    First run stores the cursor only. Also backfills a few note embeddings for hybrid recall.
    """
    from ts_harness.memory.client import get_memory_client

    memory = memory or get_memory_client()
    backfill: dict[str, Any] = {}
    if hasattr(memory, "embed_pending_knowledge_notes"):
        try:
            backfill = memory.embed_pending_knowledge_notes(limit=25) or {}
        except Exception as exc:  # noqa: BLE001 - search stays full-text for those notes
            backfill = {"error": str(exc)[:200]}
    if not enabled():
        return {"status": "skipped", "reason": "TS_HARNESS_FAILURE_DRAFTS is off", "embeddings": backfill}
    if model_call is None:
        model = rg.diagnosis_model()
        if not model:
            return {"status": "skipped", "reason": "no diagnosis model configured", "embeddings": backfill}
        model_call = rg.build_model_call(model)
    budget = max_model_calls() if max_calls is None else max_calls
    stamp = _now(now).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = memory.kv_get(CURSOR_NAMESPACE, CURSOR_KEY)
    value = cursor.get("value") if isinstance(cursor, dict) else None
    scanned = value.get("scanned_at") if isinstance(value, dict) else None
    if not scanned:
        memory.kv_put(CURSOR_NAMESPACE, CURSOR_KEY, {"scanned_at": stamp}, expected_version=int((cursor or {}).get("version") or 0))
        return {"status": "ok", "bootstrapped": True, "drafted": 0, "model_calls": 0, "embeddings": backfill}

    pending = memory.list_programs_pending_note_review(since=str(scanned), limit=MAX_PROGRAMS, stamp=DRAFT_STAMP)
    calls = 0
    looked = 0
    drafted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    finished = True
    for row in pending.get("programs") or []:
        program_id = str(row.get("id") or "")
        try:
            loaded = memory.get_program(program_id) or {}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{program_id}: {exc}")
            continue
        program = {**row, **((loaded or {}).get("program") or {})}
        workflows = _program_workflows(memory, loaded)
        if any(item["episodes"] for item in workflows):
            if calls >= budget:
                finished = False
                break
            calls += 1
            try:
                result = draft_program(memory, program, workflows, model_call=model_call)
            except Exception as exc:  # noqa: BLE001 - not stamped, so the next run tries again
                errors.append(f"{program_id}: {exc}"[:300])
                finished = False
                continue
            drafted.extend(result["written"])
            skipped.extend(result["skipped"])
        memory.mark_program_notes_reviewed(program_id, actor=ACTOR, key=DRAFT_STAMP)
        looked += 1
    if finished and len(pending.get("programs") or []) < MAX_PROGRAMS:
        memory.kv_put(CURSOR_NAMESPACE, CURSOR_KEY, {"scanned_at": stamp})
    return {
        "status": "ok",
        "bootstrapped": False,
        "programs": looked,
        "model_calls": calls,
        "drafted": len(drafted),
        "drafted_ids": [str(item.get("id")) for item in drafted if isinstance(item, dict) and item.get("id")],
        "skipped": skipped,
        "errors": errors,
        "embeddings": backfill,
    }
