"""The harness-level loop: the harness files bugs against its own repository and proposes fixes.

Once every 10 minutes the ``harness_self_issues`` system job (:func:`sweep`):

1. **Collects** failures that point at the harness, not at a client repository:
   * a stuck step the stuck-step arbiter labelled ``Harness issue`` (``_arbiter.category ==
     "harness_issue"``), one occurrence per stop of that step;
   * ops alerts about the harness itself: coding jobs lost (at or above the ops alert threshold,
     one occurrence per lost job) and the LLM proxy failing its health check (one per hour).
2. **Sanitises** every text (:mod:`ts_harness.improvement.sanitize`): the harness repository
   never receives client repository names, ticket keys or text, code, design content or customer
   names. If the install's client identifiers cannot be read, nothing is filed this round.
3. **Dedupes** by a fingerprint of the failure class (source, harness component and the
   normalised error signature) in ``harness_self_issues`` (migration 057). A new class files one
   GitHub issue on ``TS_HARNESS_SELF_REPO``; a known one gets a comment with the new count (at
   most one comment per issue every 6 hours).
4. **Self-fixes** (:mod:`ts_harness.improvement.self_fix`): a fixable issue whose files are not off
   limits gets one delivery against the harness repository, as a draft PR a person must merge.

Switches: ``TS_HARNESS_SELF_REPO`` (``owner/name``; unset means the whole loop is off),
``TS_HARNESS_SELF_ISSUES`` (filing, default on), ``TS_HARNESS_SELF_FIX`` (self-fixes, default on),
``TS_HARNESS_SELF_REPO_BRANCH`` (the PR base, default ``dev``). The same keys can be set in the
runtime config under ``program_defaults.harness_loop`` (``repo``, ``file_issues``, ``self_fix``,
``branch``); an environment variable wins.

The loop never merges, never deploys and never errors in a loop: every failure is logged and
recorded on the row (``last_error``) or in the status record, and the next run tries again.
See docs/runbooks/self-improvement.md ("Harness-level loop").
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ts_harness.improvement import sanitize as sz

logger = logging.getLogger(__name__)

ACTOR = "harness-self-improvement"
KV_NAMESPACE = "harness-loop"
STATUS_KEY = "status"
ISSUE_LABEL = "harness-bug"
NEEDS_HUMAN_LABEL = "needs-human"
FINGERPRINT_MARKER = "ts-harness-fingerprint"
MAX_NEW_ISSUES_PER_SWEEP = 3
MAX_COMMENTS_PER_SWEEP = 5
COMMENT_INTERVAL = timedelta(hours=6)
DEFAULT_BRANCH = "dev"
_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_OFF = {"0", "off", "false", "no"}
# Configuration or access problems: a person must act; code changes will not fix them.
_CONFIG_HINTS = re.compile(
    r"credential|secret|token|permission|forbidden|unauthori[sz]ed|access denied|not installed|"
    r"\b40[13]\b|api key|not configured|is required|missing access|no access|cannot read the repository",
    re.I,
)
TASK_TEXT_KEYS = ("work_order_body", "work_orders_markdown", "summary", "title", "ticket_text", "description",
                  "jira_summary", "jira_description", "requirements", "feedback", "change_requests")
TASK_TICKET_KEYS = ("work_order_id", "work_order_ref", "jira_issue_key", "ticket_key", "issue_key")


# --- settings -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class LoopSettings:
    repo: str = ""
    branch: str = DEFAULT_BRANCH
    file_issues: bool = True
    self_fix: bool = True
    problems: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.repo)

    @property
    def filing(self) -> bool:
        return self.enabled and self.file_issues

    @property
    def fixing(self) -> bool:
        return self.filing and self.self_fix

    @property
    def repo_url(self) -> str:
        return f"https://github.com/{self.repo}" if self.repo else ""

    def payload(self) -> dict[str, Any]:
        return {"repo": self.repo, "branch": self.branch, "file_issues": self.filing, "self_fix": self.fixing,
                "problems": list(self.problems)}


def _flag(raw: Any, default: bool) -> bool:
    if raw is None or str(raw).strip() == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in _OFF


def loop_settings(env: dict[str, str] | None = None, config: Any = None) -> LoopSettings:
    """The loop's switches: environment first, then ``program_defaults.harness_loop``.

    Without a valid ``TS_HARNESS_SELF_REPO`` both filing and self-fixes are off.
    """
    env = dict(os.environ if env is None else env)
    section: dict[str, Any] = {}
    defaults = getattr(config, "program_defaults", None)
    if isinstance(defaults, dict) and isinstance(defaults.get("harness_loop"), dict):
        section = dict(defaults["harness_loop"])
    repo = str(env.get("TS_HARNESS_SELF_REPO") or section.get("repo") or "").strip()
    repo = repo.removeprefix("https://github.com/").removesuffix(".git").strip("/")
    problems: list[str] = []
    if repo and not _SLUG.match(repo):
        problems.append(f"TS_HARNESS_SELF_REPO must be owner/name, got {repo[:80]!r}; the loop is off")
        repo = ""
    branch = str(env.get("TS_HARNESS_SELF_REPO_BRANCH") or section.get("branch") or DEFAULT_BRANCH).strip()
    if not re.fullmatch(r"[\w./-]{1,100}", branch):
        problems.append(f"TS_HARNESS_SELF_REPO_BRANCH {branch[:40]!r} is not a branch name; using {DEFAULT_BRANCH}")
        branch = DEFAULT_BRANCH
    file_issues = _flag(env.get("TS_HARNESS_SELF_ISSUES"), _flag(section.get("file_issues"), True))
    self_fix = _flag(env.get("TS_HARNESS_SELF_FIX"), _flag(section.get("self_fix"), True))
    return LoopSettings(repo=repo, branch=branch, file_issues=file_issues, self_fix=self_fix, problems=tuple(problems))


def _runtime_config() -> Any:
    from ts_harness.improvement.versions import _runtime_config as load

    return load()


# --- client identifiers -------------------------------------------------------------------------


def install_context(memory: Any, *, tenant_id: str, config: Any, harness_repo: str) -> sz.ClientContext:
    """Every client identifier the install knows: project repositories (with roles), project keys and
    names, teams, tenants, the repo allowlist, Jira projects and the GitHub org. Raises when the
    projects cannot be read (then nothing is filed: we cannot sanitise with confidence)."""
    ctx = sz.ClientContext(harness_repo=harness_repo)
    harness_owner = harness_repo.split("/", 1)[0].lower() if harness_repo else ""
    projects = (memory.list_projects(tenant_id=tenant_id) or {}).get("projects") or []
    for project in projects:
        ctx.add_names([project.get("key"), project.get("name"), project.get("team_slug")])
        listed = memory.list_project_repositories(str(project.get("id") or "")) or {}
        for repo in listed.get("repositories") or []:
            if isinstance(repo, dict) and repo.get("full_name"):
                ctx.add_repo(repo["full_name"], repo.get("role") or "")
    for tenant in getattr(config, "tenants", None) or []:
        ctx.add_names([getattr(tenant, "display_name", "")])
    defaults = getattr(config, "program_defaults", None)
    defaults = defaults if isinstance(defaults, dict) else {}
    for item in (defaults.get("repo_picker") or {}).get("repos") or []:
        if isinstance(item, dict):
            ctx.add_repo(item.get("id"), item.get("role") or "")
    ctx.add_names((defaults.get("jira_filing") or {}).get("allowed_projects") or [])
    org = str((defaults.get("github") or {}).get("org") or "").strip()
    if org and org.lower() != harness_owner:
        ctx.add_names([org])
    return ctx


def task_context(task: dict[str, Any], harness_repo: str = "") -> sz.ClientContext:
    """Client identifiers and texts carried by one task's input."""
    ctx = sz.ClientContext(harness_repo=harness_repo)
    data = task.get("input") if isinstance(task.get("input"), dict) else {}
    for key in ("repo_url", "repo", "repository"):
        if data.get(key):
            ctx.add_repo(data[key])
    ctx.add_tickets(data.get(key) for key in TASK_TICKET_KEYS)
    ctx.add_names([data.get("project_key"), data.get("customer"), data.get("client")])
    ctx.texts.extend(str(data.get(key)) for key in TASK_TEXT_KEYS if isinstance(data.get(key), str) and data.get(key))
    for item in data.get("project_repositories") or []:
        if isinstance(item, dict):
            ctx.add_repo(item.get("full_name"), item.get("role") or "")
    return ctx


# --- occurrences --------------------------------------------------------------------------------


@dataclass
class Occurrence:
    source: str
    key: str
    kind: str
    component: str
    signature: str
    title: str
    diagnosis: str = ""
    stack: str = ""
    files: list[str] = field(default_factory=list)
    ref: dict[str, str] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.source, self.component, self.signature)

    def body(self, tenant_id: str) -> dict[str, Any]:
        return {"tenant_id": tenant_id, "fingerprint": self.fingerprint, "source": self.source, "kind": self.kind,
                "component": self.component, "title": self.title, "signature": self.signature,
                "diagnosis": self.diagnosis, "stack": self.stack, "files": self.files, "key": self.key,
                "ref": self.ref}


def normalise_signature(signature: str) -> str:
    text = str(signature or "").lower()
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", "<id>", text)
    text = re.sub(r"\b[0-9a-f]{7,}\b", "<hex>", text)
    text = re.sub(r"\d+", "N", text)
    return " ".join(text.split())[:200]


def fingerprint(source: str, component: str, signature: str) -> str:
    raw = f"{source}|{component}|{normalise_signature(signature)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def classify(text: str, files: list[str]) -> str:
    if _CONFIG_HINTS.search(text or ""):
        return "configuration"
    return "code" if files else "unknown"


def _when(value: Any) -> str:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _job_error(memory: Any, task_id: str) -> tuple[str, str]:
    try:
        jobs = memory.list_coding_jobs(task_id=task_id, limit=1) or []
    except Exception as exc:  # noqa: BLE001 - the task's own error is enough
        logger.info("coding job of %s unavailable: %s", task_id, exc)
        return "", ""
    if not jobs:
        return "", ""
    job = jobs[0]
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    return str(job.get("id") or ""), str(job.get("error") or result.get("error") or "")


def arbiter_occurrence(task: dict[str, Any], memory: Any, base: sz.ClientContext) -> Occurrence | None:
    """The occurrence for a stuck step the arbiter labelled a harness issue, or None."""
    from ts_harness.workflows.stuck_arbiter import ARBITER_KEY, LABELS

    data = task.get("input") if isinstance(task.get("input"), dict) else {}
    rec = data.get(ARBITER_KEY) if isinstance(data.get(ARBITER_KEY), dict) else {}
    if rec.get("category") != "harness_issue" and rec.get("label") != LABELS["harness_issue"]:
        return None
    ctx = base.merged(task_context(task, base.harness_repo))
    task_id = str(task.get("id") or "")
    step = str(task.get("step_name") or "")
    job_id, job_error = _job_error(memory, task_id)
    raw = "\n".join(part for part in (str(task.get("error") or ""), job_error) if part)
    files = sz.harness_paths(raw)
    stack = sz.harness_stack(raw, ctx)
    exc, message = sz.exception_line(raw)
    first = next((line for line in raw.splitlines() if line.strip() and not line.strip().startswith("Traceback")), "")
    signature = sz.safe_field(f"{exc}: {message}" if exc else first, ctx, limit=300)
    if not signature or signature == sz.WITHHELD:
        signature = f"{exc or 'Stopped'} at step {step}" if step else (exc or "Stopped for a person")
    component = files[-1] if files else (f"step {step}" if step else "unknown")
    reason = " ".join(str(rec.get(key) or "") for key in ("reason", "why") if rec.get(key))
    diagnosis = sz.safe_field(reason, ctx, limit=800)
    kind = classify(f"{raw}\n{reason}", files)
    title = sz.safe_field(f"{component}: {signature}", ctx, limit=180) or component
    if title == sz.WITHHELD:
        title = component
    ref = {"workflow_id": str(task.get("workflow_id") or ""), "task_id": task_id, "job_id": job_id, "step": step,
           "workflow_type": str(task.get("workflow_type") or ""), "at": _when(rec.get("at") or task.get("updated_at"))}
    key = f"arbiter:{task_id}:{rec.get('for_since') or rec.get('at') or ''}"
    return Occurrence(source="arbiter", key=key, kind=kind, component=component, signature=signature, title=title,
                      diagnosis=diagnosis, stack=stack, files=files, ref={k: v for k, v in ref.items() if v})


def arbiter_occurrences(memory: Any, tenant_id: str, base: sz.ClientContext) -> list[Occurrence]:
    found = []
    for task in memory.list_stuck_tasks(tenant_id=tenant_id) or []:
        try:
            occurrence = arbiter_occurrence(task, memory, base)
        except Exception as exc:  # noqa: BLE001 - one task must not stop the others
            logger.warning("harness issue from task %s not read: %s", task.get("id"), exc)
            continue
        if occurrence is not None:
            found.append(occurrence)
    return found


def alert_occurrences(memory: Any, now: datetime, *, settings: Any = None, http_ok: Callable[[str], tuple[bool, str]] | None = None) -> list[Occurrence]:
    """Ops alerts that point at the harness itself: lost coding jobs and the LLM proxy's health."""
    from ts_harness.ops_alerts import AlertSettings, _http_ok

    settings = settings or AlertSettings.from_env()
    found: list[Occurrence] = []
    if settings.lost_jobs:
        try:
            jobs = memory.list_coding_jobs(status="lost", finished_within=3600, limit=200) or []
        except Exception as exc:  # noqa: BLE001
            logger.info("lost coding jobs unavailable: %s", exc)
            jobs = []
        if len(jobs) >= settings.lost_jobs:
            signature = "Coding jobs lost: their workers stopped heartbeating or overran"
            for job in jobs:
                job_id = str(job.get("id") or "")
                found.append(Occurrence(
                    source="ops_alert", key=f"lost_job:{job_id}", kind="operations", component="coding job workers",
                    signature=signature, title=f"coding job workers: {signature}",
                    diagnosis=(f"{len(jobs)} coding jobs were lost in the hour before {now.strftime('%Y-%m-%d %H:%M UTC')} "
                               f"(the ops alert fires at {settings.lost_jobs}). Check the coding launcher and worker logs."),
                    ref={k: v for k, v in {"job_id": job_id, "workflow_id": str(job.get("workflow_id") or ""),
                                           "task_id": str(job.get("task_id") or ""),
                                           "at": _when(job.get("finished_at") or job.get("updated_at"))}.items() if v},
                ))
    from ts_harness.llm_proxy.tokens import proxy_url

    url = proxy_url()
    if url:
        ok, detail = (http_ok or _http_ok)(f"{url}/health")
        if not ok:
            signature = "LLM proxy failing its health check"
            found.append(Occurrence(
                source="ops_alert", key=f"health:llm_proxy:{now.strftime('%Y-%m-%dT%H')}", kind="operations",
                component="runtime/ts_harness/llm_proxy/", signature=signature,
                title=f"runtime/ts_harness/llm_proxy/: {signature}",
                diagnosis=sz.safe_field(f"The LLM proxy did not answer its health check ({detail}); model calls fail.",
                                        sz.ClientContext(), limit=400),
                files=[], ref={"at": now.strftime("%Y-%m-%d %H:%M UTC")},
            ))
    return found


# --- issues -------------------------------------------------------------------------------------


SOURCE_LABELS = {"arbiter": "The stuck-step arbiter labelled a step \"Harness issue\"",
                 "ops_alert": "An ops alert about the harness itself fired"}
KIND_LABELS = {"code": "harness code", "configuration": "configuration or access", "operations": "operations",
               "unknown": "not yet known"}


def issue_title(row: dict[str, Any]) -> str:
    return f"[harness] {str(row.get('title') or row.get('component') or 'harness failure')}"[:200]


def _refs_lines(row: dict[str, Any]) -> list[str]:
    lines = []
    for ref in list(row.get("refs") or [])[-10:]:
        parts = [f"{key} `{ref[key]}`" for key in ("workflow_type", "step", "workflow_id", "task_id", "job_id") if ref.get(key)]
        when = ref.get("at") or ""
        lines.append(f"- {when + ': ' if when else ''}{', '.join(parts) or 'no ids'}")
    return lines


def issue_body(row: dict[str, Any]) -> str:
    """The issue text: harness-side facts only (sanitised fields, harness paths, counts and internal ids)."""
    files = [f"`{f}`" for f in row.get("files") or []]
    lines = [
        "Filed automatically by TS Harness about itself. It holds harness-side facts only: no client "
        "repository, ticket, code or text. Look the internal ids up in the harness's own console.",
        "",
        "## What happened",
        f"{SOURCE_LABELS.get(str(row.get('source')), str(row.get('source')))}.",
        "",
        f"**Signature:** {row.get('signature') or '(none)'}",
        "",
        "## Component",
        f"{row.get('component') or 'unknown'}" + (f" (files: {', '.join(files)})" if files else ""),
        f"Kind: {KIND_LABELS.get(str(row.get('kind')), str(row.get('kind')))}",
        "",
        "## Diagnosis",
        str(row.get("diagnosis") or "(none recorded)"),
    ]
    if row.get("stack"):
        lines += ["", "## Harness stack", "```", str(row["stack"]), "```"]
    lines += [
        "",
        "## How often",
        f"{int(row.get('occurrences') or 0)} occurrence(s); first {_when(row.get('first_seen')) or '?'}, "
        f"last {_when(row.get('last_seen')) or '?'}.",
        "",
        "## Internal references",
        *(_refs_lines(row) or ["- none"]),
        "",
        f"<!-- {FINGERPRINT_MARKER}: {row.get('fingerprint')} -->",
    ]
    return "\n".join(lines)


def comment_body(row: dict[str, Any]) -> str:
    latest = _refs_lines({"refs": list(row.get("refs") or [])[-3:]})
    return "\n".join([
        f"Seen again: {int(row.get('occurrences') or 0)} occurrence(s) in total "
        f"(was {int(row.get('reported_occurrences') or 0)}); last {_when(row.get('last_seen')) or '?'}.",
        "",
        "Latest internal references:",
        *(latest or ["- none"]),
    ])


def _checked(text: str, ctx: sz.ClientContext) -> str:
    """``text`` if it carries no client data, else a minimal text with ids and counts only."""
    try:
        sz.assert_clean(text, ctx)
        return text
    except ValueError as exc:
        logger.warning("harness issue text withheld: %s", exc)
        return ("Filed automatically by TS Harness about itself. The details were withheld because they may "
                "contain client data; look the failure up by its fingerprint in the harness console.")


def _owner_repo(settings: LoopSettings) -> tuple[str, str]:
    owner, _, name = settings.repo.partition("/")
    return owner, name


def existing_issues(github: Any, settings: LoopSettings) -> dict[str, dict[str, Any]]:
    """Issues on the harness repository that carry a fingerprint marker, by fingerprint."""
    owner, name = _owner_repo(settings)
    issues = github.request("GET", f"/repos/{owner}/{name}/issues",
                            query={"labels": ISSUE_LABEL, "state": "all", "per_page": 100}) or []
    found: dict[str, dict[str, Any]] = {}
    for issue in issues if isinstance(issues, list) else []:
        match = re.search(rf"{FINGERPRINT_MARKER}: ([0-9a-f]{{8,64}})", str(issue.get("body") or ""))
        if match and "pull_request" not in issue:
            found.setdefault(match.group(1), issue)
    return found


def add_labels(github: Any, settings: LoopSettings, number: int, labels: list[str]) -> None:
    owner, name = _owner_repo(settings)
    github.request("POST", f"/repos/{owner}/{name}/issues/{number}/labels", body={"labels": labels})


def _parse_time(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def file_issues(memory: Any, github: Any, settings: LoopSettings, ctx: sz.ClientContext, *, tenant_id: str,
                rows: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """File new failure classes and comment new counts on known ones (bounded per run)."""
    owner, name = _owner_repo(settings)
    filed: list[int] = []
    commented: list[int] = []
    errors: list[str] = []
    known: dict[str, dict[str, Any]] | None = None
    for row in rows:
        try:
            if not row.get("issue_number"):
                if len(filed) >= MAX_NEW_ISSUES_PER_SWEEP:
                    continue
                if known is None:
                    known = existing_issues(github, settings)
                issue = known.get(str(row.get("fingerprint")))
                if issue is None:
                    title = issue_title(row)
                    if sz.leaks(title, ctx):
                        title = f"[harness] {row.get('component') or 'harness failure'}"[:200]
                    issue = github.request("POST", f"/repos/{owner}/{name}/issues", body={
                        "title": title,
                        "body": _checked(issue_body(row), ctx),
                        "labels": [ISSUE_LABEL, f"harness:{row.get('source')}"],
                    }) or {}
                    filed.append(int(issue.get("number") or 0))
                update = memory.update_harness_issue(
                    str(row["id"]), issue_repo=settings.repo, issue_number=int(issue.get("number") or 0),
                    issue_url=str(issue.get("html_url") or ""), issue_state=str(issue.get("state") or "open"),
                    reported_occurrences=int(row.get("occurrences") or 0), reported=True, last_error="",
                )
                row.update((update or {}).get("issue") or {})
                continue
            new = int(row.get("occurrences") or 0) > int(row.get("reported_occurrences") or 0)
            last = _parse_time(row.get("last_reported_at"))
            if new and (last is None or now - last >= COMMENT_INTERVAL) and len(commented) < MAX_COMMENTS_PER_SWEEP:
                github.comment(owner, name, int(row["issue_number"]), _checked(comment_body(row), ctx))
                commented.append(int(row["issue_number"]))
                update = memory.update_harness_issue(
                    str(row["id"]), reported_occurrences=int(row.get("occurrences") or 0), reported=True, last_error="",
                )
                row.update((update or {}).get("issue") or {})
        except Exception as exc:  # noqa: BLE001 - recorded on the row; the next run tries again
            message = f"{type(exc).__name__}: {str(exc)[:300]}"
            errors.append(message)
            logger.warning("harness issue %s not filed or updated: %s", row.get("fingerprint"), message)
            try:
                memory.update_harness_issue(str(row["id"]), last_error=sz.sanitize_text(message, ctx, limit=500))
            except Exception:  # noqa: BLE001
                pass
    return {"filed": [n for n in filed if n], "commented": commented, "errors": errors}


def refresh_issue_states(memory: Any, github: Any, settings: LoopSettings, rows: list[dict[str, Any]]) -> None:
    """Follow issues a person closed or reopened (one listing call)."""
    if not any(row.get("issue_number") for row in rows):
        return
    try:
        listed = existing_issues(github, settings)
    except Exception as exc:  # noqa: BLE001
        logger.info("harness issue states not refreshed: %s", exc)
        return
    for row in rows:
        issue = listed.get(str(row.get("fingerprint")))
        if issue and row.get("issue_number") and str(issue.get("state") or "") != str(row.get("issue_state") or ""):
            try:
                update = memory.update_harness_issue(str(row["id"]), issue_state=str(issue.get("state") or ""))
                row.update((update or {}).get("issue") or {})
            except Exception as exc:  # noqa: BLE001
                logger.info("issue state of %s not saved: %s", row.get("fingerprint"), exc)


# --- the sweep ----------------------------------------------------------------------------------


def _save_status(memory: Any, status: dict[str, Any]) -> None:
    try:
        memory.kv_put(KV_NAMESPACE, STATUS_KEY, status)
    except Exception as exc:  # noqa: BLE001
        logger.info("harness loop status not saved: %s", exc)


def sweep(
    *,
    tenant_id: str = "default",
    memory: Any = None,
    github: Any = None,
    config: Any = None,
    env: dict[str, str] | None = None,
    now: datetime | None = None,
    budget: Any = None,
    fork: Callable[..., str] | None = None,
    make_draft: Callable[[str], bool] | None = None,
    http_ok: Callable[[str], tuple[bool, str]] | None = None,
    alert_settings: Any = None,
) -> dict[str, Any]:
    """System job: collect, file and comment harness issues, then run self-fixes."""
    now = now or datetime.now(timezone.utc)
    config = config if config is not None else _runtime_config()
    settings = loop_settings(env, config)
    if not settings.filing:
        reason = "TS_HARNESS_SELF_REPO is not set" if not settings.enabled else "issue filing is off (TS_HARNESS_SELF_ISSUES)"
        return {"status": "off", "reason": reason, "problems": list(settings.problems)}
    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    status: dict[str, Any] = {"checked_at": now.isoformat(), "settings": settings.payload(), "problems": list(settings.problems)}
    try:
        ctx = install_context(memory, tenant_id=tenant_id, config=config, harness_repo=settings.repo)
    except Exception as exc:  # noqa: BLE001 - cannot sanitise with confidence: file nothing
        status["problems"].append(f"client identifiers could not be read ({type(exc).__name__}); nothing filed this round")
        _save_status(memory, status)
        return {"status": "skipped", "reason": "client identifiers unavailable", "problems": status["problems"]}

    occurrences: list[Occurrence] = []
    try:
        occurrences.extend(arbiter_occurrences(memory, tenant_id, ctx))
    except Exception as exc:  # noqa: BLE001
        logger.warning("arbiter harness issues not read: %s", exc)
    try:
        occurrences.extend(alert_occurrences(memory, now, settings=alert_settings, http_ok=http_ok))
    except Exception as exc:  # noqa: BLE001
        logger.warning("harness ops alerts not read: %s", exc)
    recorded = new = 0
    for occurrence in occurrences:
        try:
            result = memory.record_harness_occurrence(**occurrence.body(tenant_id)) or {}
            recorded += 1
            new += bool(result.get("new_occurrence"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("harness occurrence %s not recorded: %s", occurrence.fingerprint, exc)

    try:
        listing = memory.list_harness_issues(tenant_id=tenant_id, limit=100) or {}
    except Exception as exc:  # noqa: BLE001
        status["problems"].append(f"harness issues could not be read ({type(exc).__name__}); is migration 057 applied?")
        _save_status(memory, status)
        return {"status": "error", "reason": "harness issues unavailable", "problems": status["problems"]}
    rows = list(listing.get("issues") or [])
    if github is None:
        try:
            from ts_harness.integrations.github_client import github_for

            github = github_for(settings.repo_url)
        except Exception as exc:  # noqa: BLE001
            status["problems"].append(f"no GitHub client for {settings.repo} ({str(exc)[:160]})")
            _save_status(memory, status)
            return {"status": "skipped", "reason": "no GitHub client", "problems": status["problems"]}
    refresh_issue_states(memory, github, settings, rows)
    filing = file_issues(memory, github, settings, ctx, tenant_id=tenant_id, rows=rows, now=now)
    if filing["errors"]:
        status["problems"].append(f"GitHub refused an issue write on {settings.repo}: {filing['errors'][0][:200]} "
                                  "(the GitHub App or token needs issues: read and write)")

    from ts_harness.improvement import self_fix

    fixes = self_fix.run(memory, github, settings, ctx, tenant_id=tenant_id, rows=rows, listing=listing, now=now,
                         config=config, budget=budget, fork=fork, make_draft=make_draft)
    status["problems"].extend(fixes.get("problems") or [])
    status["self_fix"] = {k: v for k, v in fixes.items() if k != "problems"}
    _save_status(memory, status)
    return {"status": "ok", "occurrences": recorded, "new_occurrences": new, **{k: v for k, v in filing.items()},
            "self_fix": fixes}


def loop_status(memory: Any, *, tenant_id: str = "default", env: dict[str, str] | None = None, config: Any = None) -> dict[str, Any]:
    """What the admin console shows: switches, problems from the last run, issues and self-fix PRs."""
    settings = loop_settings(env, config if config is not None else _runtime_config())
    out: dict[str, Any] = {"settings": settings.payload(), "available": True, "issues": [], "problems": list(settings.problems)}
    if not settings.enabled:
        return out
    try:
        last = (memory.kv_get(KV_NAMESPACE, STATUS_KEY) or {}).get("value") or {}
        out["checked_at"] = last.get("checked_at")
        out["problems"] = list(dict.fromkeys(list(settings.problems) + list(last.get("problems") or [])))
        listing = memory.list_harness_issues(tenant_id=tenant_id, limit=50) or {}
    except Exception as exc:  # noqa: BLE001
        logger.info("harness loop status unavailable: %s", exc)
        out["available"] = False
        return out
    from ts_harness.improvement.self_fix import MAX_FIXES_PER_WEEK, MAX_OPEN_FIXES

    keep = ("id", "fingerprint", "source", "kind", "component", "title", "occurrences", "first_seen", "last_seen",
            "issue_number", "issue_url", "issue_state", "fix_status", "fix_reason", "fix_pr_url", "fix_started_at",
            "fix_charged_usd", "last_error")
    out["issues"] = [{key: row.get(key) for key in keep} for row in listing.get("issues") or []]
    out["limits"] = {"open_fixes": int(listing.get("open_fixes") or 0), "max_open": MAX_OPEN_FIXES,
                     "fixes_last_7_days": int(listing.get("fixes_last_7_days") or 0), "max_per_week": MAX_FIXES_PER_WEEK}
    return out
