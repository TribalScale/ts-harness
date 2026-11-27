"""Is a hand-written Jira ticket ready to build? Gather its context, then decide, before any coding.

Tickets the harness files carry a pointer to the program that planned them (``Program:`` /
work order lines), so the coding job gets the approved plan. A ticket a person wrote has only
what it says, and coding from a title alone produced PRs that review and QA then argued about.
For such a ticket ``prepare_ticket_delivery`` calls :func:`check_ready`:

1. **Context**: the description, the comments, the parent epic, linked tickets, pull requests that
   mention the key, and the repository's file list (for the area it touches).
2. **Assessment** by the diagnosis model: ``ready``, ``assumptions`` or ``not_ready``, with a work
   order in the harness's format (requirements, acceptance tests, in and out of scope, likely
   files), its assumptions (each ``minor`` or ``behaviour``) and its questions. Acceptance criteria
   that can be derived from the description are enough.
3. **Policy** (code, not the model): ``ready``, or assumptions that are all minor, start coding
   from the work order, which is posted on the ticket with its assumptions listed. An assumption
   that changes behaviour or scope, or a ticket that is not ready, does not start: the questions
   go to the ticket as a comment, to the bound chat and to the Inbox (*Needs product decision*),
   and the step waits. Conservative projects always wait for a confirmation.
4. **Answers**: a reply on the ticket (``recheck_waiting_tickets``, every minute), ``/retry
   <answers>`` in chat or Retry with guidance in the Inbox runs the step again with the answers.
   "go ahead" (or "proceed") accepts the assumptions as they are.

Without a diagnosis model, or with ``TS_HARNESS_TICKET_READINESS=off``, tickets go straight
through as before.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

WAIT_REASON = "ticket_not_ready"
COMMENT_MARK = "[TS Harness]"
GO_AHEAD = re.compile(r"^\s*(go ahead|proceed|go|approved?|confirmed?|ship it|yes)\b", re.IGNORECASE)
MAX_TREE_PATHS = 400
_SKIP_TREE = re.compile(r"(^|/)(node_modules|dist|build|coverage|\.git|\.next|vendor|docs/planning)(/|$)")

PROMPT = """You decide whether a Jira ticket written by a person is ready for an automated coding agent to build.
The agent works alone: it can't ask anything while coding, and code review and QA will judge the PR
against the work order you write. Acceptance criteria may be derived from the description: they
don't have to be spelled out, but you must be able to state them as testable outcomes.

Ticket {key}: {summary}
Repository: {repo}

Description:
{description}

Comments, oldest first:
{comments}

Parent epic:
{parent}

Linked tickets:
{links}

Pull requests that mention the ticket:
{prs}

Repository files (a sample):
{tree}

Answers from people since the questions were asked:
{answers}

Reply with JSON only:
{{"status": "ready" | "assumptions" | "not_ready",
  "work_order": "for ready or assumptions: markdown with ## Summary, ## Requirements, ## Acceptance Tests (Given/When/Then), ## In Scope, ## Out of Scope, ## Files (likely paths from the repository list)",
  "assumptions": [{{"text": "what you assumed", "kind": "minor" if it changes nothing a user sees or the system promises (placement, naming, an internal helper), "behaviour" if it decides what users see or can do, data rules, an API's shape, a permission or the scope}}],
  "questions": ["for not_ready, or to confirm behaviour assumptions: specific questions a product owner can answer in a sentence"],
  "reason": "one sentence"}}
Use "not_ready" when the goal itself is unclear, or the ticket contradicts itself, or no testable outcome can be stated.
"""


def enabled() -> bool:
    return os.environ.get("TS_HARNESS_TICKET_READINESS", "on").strip().lower() not in {"0", "off", "false", "no"}


def hand_written(pointer: Any) -> bool:
    """No pointer to a planning program or work order: a person wrote it, not the harness."""
    return not (getattr(pointer, "program_id", None) or getattr(pointer, "work_order_id", None))


# --- context --------------------------------------------------------------------------------------


def _text(value: Any) -> str:
    if isinstance(value, dict):
        from ts_harness.integrations.adf import adf_to_text

        return adf_to_text(value)
    return str(value or "")


def _comments(jira: Any, key: str) -> list[dict[str, str]]:
    try:
        rows = jira.comments(key)
    except Exception as exc:  # noqa: BLE001
        logger.info("comments of %s not read: %s", key, exc)
        return []
    out = []
    for row in rows or []:
        author = str(((row.get("author") or {}).get("displayName")) or "someone")
        out.append({"id": str(row.get("id") or ""), "author": author, "created": str(row.get("created") or ""),
                    "text": _text(row.get("body")).strip()})
    return out


def _parent(jira: Any, fields: dict[str, Any]) -> str:
    parent = fields.get("parent") if isinstance(fields.get("parent"), dict) else {}
    key = str(parent.get("key") or "")
    if not key:
        return "(none)"
    try:
        f = (jira.get_issue(key) or {}).get("fields") or {}
    except Exception:  # noqa: BLE001
        return key
    return f"{key}: {f.get('summary') or ''}\n{_text(f.get('description'))[:2500]}"


def _links(jira: Any, fields: dict[str, Any]) -> str:
    lines = []
    for link in (fields.get("issuelinks") or [])[:8]:
        if not isinstance(link, dict):
            continue
        kind = link.get("type") if isinstance(link.get("type"), dict) else {}
        if isinstance(link.get("inwardIssue"), dict):
            other, relation = link["inwardIssue"], kind.get("inward")
        elif isinstance(link.get("outwardIssue"), dict):
            other, relation = link["outwardIssue"], kind.get("outward")
        else:
            continue
        other_fields = other.get("fields") or {}
        lines.append(f"- {relation} {other.get('key')}: {other_fields.get('summary') or ''} "
                     f"({(other_fields.get('status') or {}).get('name') or ''})")
    return "\n".join(lines) or "(none)"


def _prs(github: Any, owner: str, repo: str, key: str) -> str:
    try:
        found = github.request("GET", "/search/issues", query={"q": f'"{key}" repo:{owner}/{repo} is:pr', "per_page": 5})
    except Exception:  # noqa: BLE001
        return "(not searched)"
    items = (found or {}).get("items") or []
    return "\n".join(f"- {i.get('title')} ({i.get('state')}) {i.get('html_url')}" for i in items) or "(none)"


def _tree(github: Any, owner: str, repo: str, ref: str) -> str:
    try:
        tree = github.get_tree(owner, repo, ref)
    except Exception:  # noqa: BLE001
        return "(not read)"
    paths = [str(t.get("path") or "") for t in (tree or {}).get("tree") or [] if t.get("type") == "blob"]
    paths = [p for p in paths if p and not _SKIP_TREE.search(p)]
    return "\n".join(paths[:MAX_TREE_PATHS]) or "(empty)"


def gather(jira: Any, github: Any, *, key: str, issue: dict[str, Any], summary: str, description: str,
           repo_url: str, base_branch: str, answers: str) -> dict[str, str]:
    from ts_harness.integrations.github_client import parse_repo_slug

    fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
    slug = parse_repo_slug(repo_url) or ("", "")
    comments = [c for c in _comments(jira, key) if not c["text"].startswith(COMMENT_MARK)]
    return {
        "key": key,
        "summary": summary,
        "repo": f"{slug[0]}/{slug[1]}@{base_branch}",
        "description": description[:8000] or "(empty)",
        "comments": "\n".join(f"- {c['author']}: {c['text'][:1500]}" for c in comments[-15:]) or "(none)",
        "parent": _parent(jira, fields),
        "links": _links(jira, fields),
        "prs": _prs(github, slug[0], slug[1], key) if github is not None and slug[0] else "(not searched)",
        "tree": _tree(github, slug[0], slug[1], base_branch) if github is not None and slug[0] else "(not read)",
        "answers": answers or "(none)",
    }


def assess(facts: dict[str, str], model_call: Callable[[str], str]) -> dict[str, Any] | None:
    try:
        reply = model_call(PROMPT.format(**facts))
    except Exception as exc:  # noqa: BLE001 - no assessment: the ticket goes through as before
        logger.warning("ticket readiness check failed: %s", exc)
        return None
    match = re.search(r"\{.*\}", reply or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# --- policy ---------------------------------------------------------------------------------------


def decide(result: dict[str, Any], *, autonomy: str, go_ahead: bool) -> str:
    """``"go"`` or ``"wait"``."""
    status = str(result.get("status") or "")
    if not str(result.get("work_order") or "").strip() and status != "not_ready":
        return "wait"
    if go_ahead and str(result.get("work_order") or "").strip():
        return "go"
    if status == "not_ready":
        return "wait"
    behaviour = [a for a in result.get("assumptions") or [] if isinstance(a, dict) and a.get("kind") != "minor"]
    if behaviour:
        return "wait"
    if autonomy == "conservative" and (status == "assumptions" or result.get("assumptions")):
        return "wait"
    return "go"


def _assumption_lines(result: dict[str, Any]) -> list[str]:
    return [f"- {a.get('text')}" + (" (changes behaviour or scope)" if a.get("kind") != "minor" else "")
            for a in result.get("assumptions") or [] if isinstance(a, dict) and a.get("text")]


def work_order_body(key: str, summary: str, description: str, result: dict[str, Any]) -> str:
    body = [f"# {key} {summary}", "", str(result.get("work_order") or "").strip()]
    assumptions = _assumption_lines(result)
    if assumptions:
        body += ["", "## Assumptions", *assumptions]
    body += ["", "## Original ticket", "", description.strip() or "(no description)"]
    return "\n".join(body).rstrip() + "\n"


def questions_text(key: str, result: dict[str, Any]) -> str:
    lines = [f"{COMMENT_MARK} {key} isn't ready to build yet: {result.get('reason') or 'some things are unclear'}."]
    questions = [str(q) for q in result.get("questions") or [] if str(q).strip()]
    if questions:
        lines += ["", "Questions:", *[f"{i}. {q}" for i, q in enumerate(questions, 1)]]
    assumptions = _assumption_lines(result)
    if assumptions:
        lines += ["", "To build it, the harness would assume:", *assumptions]
    if str(result.get("work_order") or "").strip():
        lines += ["", "Draft work order:", "", str(result["work_order"]).strip()]
    lines += ["", "Answer here on the ticket, in the chat with `/retry <answers>`, or in the TS Harness Inbox. "
                  "Reply \"go ahead\" to build it with these assumptions."]
    return "\n".join(lines)


def started_text(key: str, result: dict[str, Any]) -> str:
    lines = [f"{COMMENT_MARK} Building {key} from this work order:", "", str(result.get("work_order") or "").strip()]
    assumptions = _assumption_lines(result)
    if assumptions:
        lines += ["", "Assumptions:", *assumptions]
    return "\n".join(lines)


# --- the step -------------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _when(value: str) -> datetime | None:
    """Jira's ``2026-11-27T10:00:00.000-0500`` or an ISO time, as an aware datetime."""
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_GUIDANCE_HEAD = re.compile(r"Guidance from [^\n:]{1,80}:\s*\n", re.IGNORECASE)
_AUTHOR = re.compile(r"^[^:\n]{1,60}:\s*")


def person_answers(human_guidance: str) -> str:
    """What people wrote, without the retry's "The previous run stopped: ..." preamble."""
    text = str(human_guidance or "")
    heads = list(_GUIDANCE_HEAD.finditer(text))
    return (text[heads[-1].end():] if heads else text).strip()


def said_go_ahead(answers: str) -> bool:
    """Any line that is (an author's) "go ahead", "proceed", "approved"..."""
    return any(GO_AHEAD.search(_AUTHOR.sub("", line, count=1)) or GO_AHEAD.search(line)
               for line in str(answers or "").splitlines() if line.strip())


def check_ready(
    *,
    memory: Any,
    task: dict[str, Any],
    task_input: dict[str, Any],
    jira: Any,
    key: str,
    issue: dict[str, Any],
    summary: str,
    description: str,
    repo_url: str,
    base_branch: str,
    github: Any = None,
    model_call: Callable[[str], str] | None = None,
    autonomy: str | None = None,
) -> dict[str, Any] | None:
    """``{"body", "note"}`` to build from, ``None`` to go on as before; raises ``StepBlocked`` to wait."""
    if not enabled():
        return None
    from ts_harness.workflows import revision_guidance as rg

    if model_call is None:
        model = rg.diagnosis_model()
        if not model:
            return None
        model_call = rg.build_model_call(model)
    if github is None:
        try:
            from ts_harness.integrations.github_client import github_for

            github = github_for(repo_url)
        except Exception:  # noqa: BLE001 - context without the repository
            github = None
    answers = person_answers(str(task_input.get("human_guidance") or ""))
    facts = gather(jira, github, key=key, issue=issue, summary=summary, description=description,
                   repo_url=repo_url, base_branch=base_branch, answers=answers)
    result = assess(facts, model_call)
    if result is None:
        return None
    if autonomy is None:
        from ts_harness.workflows.revision_triage import project_autonomy

        try:
            autonomy = project_autonomy(memory, {"program_id": (memory.task_program(str(task.get("id") or "")) or {}).get("program_id")})
        except Exception:  # noqa: BLE001
            autonomy = "autonomous"
    go_ahead = said_go_ahead(answers)
    if decide(result, autonomy=autonomy, go_ahead=go_ahead) == "go":
        try:
            jira.comment(key, started_text(key, result))
        except Exception as exc:  # noqa: BLE001 - building matters more than the write-back
            logger.warning("work order not posted on %s: %s", key, exc)
        note = f"Built from a work order the harness wrote from the ticket ({result.get('status')})"
        if result.get("assumptions"):
            note += f", with {len(result['assumptions'])} assumption(s) listed on the ticket"
        return {"body": work_order_body(key, summary, description, result), "note": note + "."}
    text = questions_text(key, result)
    try:
        jira.comment(key, text)
    except Exception as exc:  # noqa: BLE001 - the chat and the Inbox still get the questions
        logger.warning("questions not posted on %s: %s", key, exc)
    from ts_harness.workflows.human_decisions import _input
    from ts_harness.workflows.system_steps import StepBlocked
    from ts_harness.workflows.work_order_waves import HUMAN_DECISION_KEY

    try:
        memory.patch_task_input(str(task["id"]), {**_input(task), HUMAN_DECISION_KEY: {
            "reason": WAIT_REASON, "since": _now(), "asked_at": _now(), "jira_issue_key": key}})
    except Exception as exc:  # noqa: BLE001
        logger.warning("waiting mark not saved on %s: %s", key, exc)
    raise StepBlocked(text.removeprefix(COMMENT_MARK).strip(), output={"readiness": result})


def recheck_waiting_tickets(*, tenant_id: str = "default", memory: Any = None, engine: Any = None, jira: Any = None) -> dict[str, Any]:
    """Run a waiting ticket's step again when someone has replied on the ticket since the questions."""
    from ts_harness.workflows.work_order_waves import HUMAN_DECISION_KEY

    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    waiting = []
    for task in list(memory.list_stuck_tasks(tenant_id=tenant_id) or []):
        mark = (task.get("input") or {}).get(HUMAN_DECISION_KEY) if isinstance(task.get("input"), dict) else None
        if isinstance(mark, dict) and mark.get("reason") == WAIT_REASON and mark.get("jira_issue_key"):
            waiting.append((task, mark))
    if not waiting:
        return {"checked": 0, "resumed": 0}
    if jira is None:
        from ts_harness.integrations.jira_client import JiraClient

        jira = JiraClient.from_env()
    if engine is None:
        from ts_harness.workflows.engine import get_workflow_engine

        engine = get_workflow_engine()
    resumed = 0
    for task, mark in waiting:
        key = str(mark["jira_issue_key"])
        asked = _when(str(mark.get("asked_at") or ""))
        replies = [c for c in _comments(jira, key)
                   if not c["text"].startswith(COMMENT_MARK) and asked and (_when(c["created"]) or asked) > asked]
        if not replies:
            continue
        answers = "\n".join(f"{c['author']}: {c['text']}" for c in replies)[:4000]
        try:
            engine.decide_blocked_step(str(task["workflow_id"]), str(task["id"]), "retry", actor="jira-reply",
                                       guidance=answers, tenant_id=tenant_id)
            resumed += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("ticket %s not resumed after a reply: %s", key, exc)
    return {"checked": len(waiting), "resumed": resumed}
