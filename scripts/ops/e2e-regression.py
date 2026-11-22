#!/usr/bin/env python3
"""End-to-end regression: drive a fixed sample project through a running harness (staging).

The run talks to the gateway the way a person does: chat messages on ``/session/message``
(``channel: web``) and the admin API for gates (``/admin/inbox``, ``/admin/workflows/approve``,
``/admin/live``, ``/admin/programs/...``). It starts from the checked-in PRD
(``tests/fixtures/e2e/sample_prd.md``), approves every planning gate, has the work orders filed
in the sandbox Jira project, starts ``implement all <PROJECT>``, approves the queue, merges each
PR in the sandbox repository once it is "Ready to merge", and waits until everything is done.

    E2E_SANDBOX=1 E2E_GATEWAY_URL=https://staging.example E2E_TOKEN=... \\
    E2E_JIRA_PROJECT=DEMO E2E_REPO=example-org/bookshelf-sandbox E2E_ACTOR=e2e@example.com \\
    E2E_SANDBOX_REPOS=example-org/bookshelf-sandbox E2E_SANDBOX_JIRA_PROJECTS=DEMO \\
    E2E_GITHUB_TOKEN=... python3 scripts/ops/e2e-regression.py --out reports/e2e

``--dry-run`` checks the configuration and connectivity and changes nothing. ``--cleanup-only
--program <id>`` cancels programs and closes the sandbox repository's open PRs. The report is
written as ``report.json`` and ``report.md`` under ``--out``; the exit code is 0 only when every
assertion held. See docs/runbooks/e2e-regression.md.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRD = ROOT / "tests" / "fixtures" / "e2e" / "sample_prd.md"

STAGES = ("start", "planning", "implement_start", "implementing", "done")
TIMED_STAGES = ("planning", "implementing")
PROGRAM_OK = {"succeeded", "completed"}
PROGRAM_BAD = {"failed", "cancelled"}
PHASE_OK = {"succeeded", "complete", "completed"}
TICKET_OK = {"merged", "accepted", "done"}
READY_TO_MERGE = "Ready to merge"
PR_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)")


class ConfigError(Exception):
    """The run must not start: missing settings or a safety check failed."""


# ── configuration and safety ──────────────────────────────────────────────────


@dataclass
class Config:
    gateway_url: str
    token: str
    jira_project: str
    repo: str
    actor: str
    github_token: str = ""
    sandbox: bool = False
    sandbox_repos: tuple[str, ...] = ()
    sandbox_jira_projects: tuple[str, ...] = ()
    project_key: str = ""
    prd_path: str = str(DEFAULT_PRD)
    timeout_minutes: float = 240.0
    poll_seconds: float = 30.0
    start_grace_minutes: float = 15.0
    track_message: str = ""
    merge_method: str = "squash"

    @property
    def track_text(self) -> str:
        return self.track_message or f"file work orders in jira project {self.jira_project}"

    @property
    def implement_text(self) -> str:
        return f"implement all {self.jira_project}"


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def config_from_env(env: dict[str, str]) -> Config:
    def num(name: str, default: float) -> float:
        raw = env.get(name, "")
        try:
            return float(raw) if raw else default
        except ValueError as exc:
            raise ConfigError(f"{name} must be a number") from exc

    return Config(
        gateway_url=env.get("E2E_GATEWAY_URL", "").rstrip("/"),
        token=env.get("E2E_TOKEN", ""),
        jira_project=env.get("E2E_JIRA_PROJECT", "").strip(),
        repo=env.get("E2E_REPO", "").strip(),
        actor=env.get("E2E_ACTOR", "").strip(),
        github_token=env.get("E2E_GITHUB_TOKEN", ""),
        sandbox=env.get("E2E_SANDBOX", "") == "1",
        sandbox_repos=_csv(env.get("E2E_SANDBOX_REPOS", "")),
        sandbox_jira_projects=_csv(env.get("E2E_SANDBOX_JIRA_PROJECTS", "")),
        project_key=env.get("E2E_PROJECT_KEY", "").strip(),
        prd_path=env.get("E2E_PRD_PATH", "") or str(DEFAULT_PRD),
        timeout_minutes=num("E2E_TIMEOUT_MINUTES", 240.0),
        poll_seconds=num("E2E_POLL_SECONDS", 30.0),
        start_grace_minutes=num("E2E_START_GRACE_MINUTES", 15.0),
        track_message=env.get("E2E_TRACK_MESSAGE", "").strip(),
        merge_method=env.get("E2E_MERGE_METHOD", "squash").strip() or "squash",
    )


def safety_errors(cfg: Config, *, need_github: bool = True) -> list[str]:
    """Every reason the run must not start. Empty means it may run."""
    errors = []
    missing = [
        name
        for name, value in (
            ("E2E_GATEWAY_URL", cfg.gateway_url),
            ("E2E_TOKEN", cfg.token),
            ("E2E_JIRA_PROJECT", cfg.jira_project),
            ("E2E_REPO", cfg.repo),
            ("E2E_ACTOR", cfg.actor),
        )
        if not value
    ]
    if need_github and not cfg.github_token:
        missing.append("E2E_GITHUB_TOKEN")
    if missing:
        errors.append("missing settings: " + ", ".join(missing))
    if not cfg.sandbox:
        errors.append("E2E_SANDBOX=1 is required: this run files tickets and merges PRs")
    if cfg.gateway_url and not cfg.gateway_url.startswith(("https://", "http://")):
        errors.append("E2E_GATEWAY_URL must be an http(s) URL")
    if cfg.repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", cfg.repo):
        errors.append("E2E_REPO must be owner/name")
    if not cfg.sandbox_repos:
        errors.append("E2E_SANDBOX_REPOS must list the sandbox repositories")
    elif cfg.repo and cfg.repo.lower() not in {repo.lower() for repo in cfg.sandbox_repos}:
        errors.append(f"E2E_REPO {cfg.repo} is not a configured sandbox repository (E2E_SANDBOX_REPOS)")
    if not cfg.sandbox_jira_projects:
        errors.append("E2E_SANDBOX_JIRA_PROJECTS must list the sandbox Jira projects")
    elif cfg.jira_project and cfg.jira_project.upper() not in {key.upper() for key in cfg.sandbox_jira_projects}:
        errors.append(
            f"E2E_JIRA_PROJECT {cfg.jira_project} is not a configured sandbox project (E2E_SANDBOX_JIRA_PROJECTS)"
        )
    if cfg.merge_method not in {"merge", "squash", "rebase"}:
        errors.append("E2E_MERGE_METHOD must be merge, squash or rebase")
    if not Path(cfg.prd_path).is_file():
        errors.append(f"PRD file not found: {cfg.prd_path}")
    return errors


def pr_repo(url: str) -> tuple[str, int] | None:
    match = PR_URL.match(str(url or ""))
    return (match.group(1), int(match.group(2))) if match else None


def in_sandbox(url: str, cfg: Config) -> bool:
    parsed = pr_repo(url)
    return bool(parsed) and parsed[0].lower() == cfg.repo.lower()


# ── run state and the decision function ───────────────────────────────────────


@dataclass
class Action:
    kind: str  # send | approve | merge | stage | fail | wait
    value: str = ""
    note: str = ""


@dataclass
class Snapshot:
    """What the API showed in one poll, already narrowed to plain dicts."""

    now: float
    inbox: list[dict[str, Any]] = field(default_factory=list)
    program_ids: list[str] = field(default_factory=list)  # every listed program id
    programs: dict[str, dict[str, Any]] = field(default_factory=dict)  # ours: id -> status payload
    live: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RunState:
    started_at: float
    stage: str = "start"
    session_id: str = ""
    baseline: set[str] = field(default_factory=set)
    planning_programs: list[str] = field(default_factory=list)
    implement_programs: list[str] = field(default_factory=list)
    track_sent: bool = False
    implement_sent_at: float = 0.0
    approved: dict[str, str] = field(default_factory=dict)  # task id -> step
    answered: set[str] = field(default_factory=set)  # proposal ids answered "yes"
    prs: dict[str, str] = field(default_factory=dict)  # pr url -> open | merged | merge_failed
    tickets: dict[str, dict[str, Any]] = field(default_factory=dict)  # workflow id -> last live view
    stage_times: dict[str, dict[str, float]] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    log: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ours(self) -> list[str]:
        return [*self.planning_programs, *self.implement_programs]


def adopt_programs(state: RunState, snap: Snapshot) -> None:
    """Programs that appeared since the run began are ours: planning before ``implement all``, then implement."""
    for program_id in snap.program_ids:
        if program_id in state.baseline or program_id in state.ours:
            continue
        if state.implement_sent_at:
            state.implement_programs.append(program_id)
        else:
            state.planning_programs.append(program_id)


def _program_status(snap: Snapshot, program_id: str) -> str:
    return str((snap.programs.get(program_id) or {}).get("status") or "")


def _phase_done(snap: Snapshot, program_id: str, phase: str) -> bool:
    phases = (snap.programs.get(program_id) or {}).get("phases") or {}
    return str((phases.get(phase) or {}).get("state") or "") in PHASE_OK


def _meta(item: dict[str, Any]) -> dict[str, Any]:
    meta = item.get("meta")
    return meta if isinstance(meta, dict) else {}


def _problems(state: RunState, snap: Snapshot) -> list[str]:
    ours = set(state.ours)
    found = []
    for item in snap.inbox:
        if item.get("type") == "stuck_step" and str(_meta(item).get("program_id") or "") in ours:
            found.append(f"stuck step: {item.get('title')} ({item.get('subtitle') or 'waiting for a decision'})")
    for program_id in state.ours:
        status = _program_status(snap, program_id)
        if status in PROGRAM_BAD:
            found.append(f"program {program_id} ended {status}")
    for view in snap.live:
        if str(view.get("program_id") or "") in ours and view.get("kind") in {"stuck", "failed"}:
            found.append(f"ticket {view.get('jira_issue_key') or view.get('label')} {view.get('kind')}: {view.get('state')}")
    return found


def _common(state: RunState, snap: Snapshot, cfg: Config) -> list[Action]:
    """Answer our chat proposals, approve our gates, merge sandbox PRs that are ready."""
    actions: list[Action] = []
    ours = set(state.ours)
    for item in snap.inbox:
        meta = _meta(item)
        kind = item.get("type")
        item_id = str(item.get("id") or "")
        if kind == "pending_proposal" and state.session_id and meta.get("session_id") == state.session_id:
            if item_id not in state.answered:
                actions.append(Action("send", "yes", note=f"confirm: {item.get('title')}"))
                actions.append(Action("answered", item_id))
        elif kind == "workflow_gate" and str(meta.get("program_id") or "") in ours:
            task_id = str(meta.get("task_id") or item_id)
            if task_id and task_id not in state.approved:
                actions.append(Action("approve", task_id, note=str(item.get("title") or "gate")))
    for view in snap.live:
        if str(view.get("program_id") or "") not in ours:
            continue
        url = str(view.get("pr_url") or "")
        ready = view.get("state") == READY_TO_MERGE or view.get("current_step") == "await_human_merge"
        if not url or not ready or state.prs.get(url) in {"merged", "merge_failed"}:
            continue
        if not in_sandbox(url, cfg):
            actions.append(Action("fail", f"PR outside the sandbox repository, not merged: {url}"))
            continue
        actions.append(Action("merge", url, note=str(view.get("jira_issue_key") or view.get("label") or "")))
    return actions


def decide(state: RunState, snap: Snapshot, cfg: Config, prd_text: str) -> list[Action]:
    """The next actions for this poll. Pure: reads the state and snapshot, changes neither."""
    if state.stage == "done":
        return []
    elapsed = snap.now - state.started_at
    if elapsed > cfg.timeout_minutes * 60:
        return [Action("fail", f"global timeout after {round(elapsed / 60)} minutes in stage {state.stage}")]
    problems = _problems(state, snap)
    if problems:
        return [Action("fail", problem) for problem in problems]

    if state.stage == "start":
        return [Action("send", start_message(cfg, prd_text), note="PRD"), Action("stage", "planning")]

    actions = _common(state, snap, cfg)
    if state.stage == "planning":
        if not state.planning_programs:
            if elapsed > cfg.start_grace_minutes * 60:
                actions.append(Action("fail", f"no program started {round(elapsed / 60)} minutes after the PRD"))
            return actions or [Action("wait", "waiting for the program to start")]
        program_id = state.planning_programs[0]
        if not state.track_sent and _phase_done(snap, program_id, "prd"):
            actions.append(Action("send", cfg.track_text, note="planning track"))
            actions.append(Action("track_sent"))
        elif state.track_sent and (
            _phase_done(snap, program_id, "file_jira") or _program_status(snap, program_id) in PROGRAM_OK
        ):
            actions.append(Action("stage", "implement_start"))
        return actions or [Action("wait", "planning")]

    if state.stage == "implement_start":
        return [Action("send", cfg.implement_text, note="implement all"), Action("stage", "implementing")]

    if state.stage == "implementing":
        if not state.implement_programs:
            if snap.now - state.implement_sent_at > cfg.start_grace_minutes * 60:
                actions.append(Action("fail", "no implement program started after 'implement all'"))
            return actions or [Action("wait", "waiting for the implement program")]
        ours = set(state.ours)
        running = [view for view in snap.live if str(view.get("program_id") or "") in ours]
        finished = all(_program_status(snap, pid) in PROGRAM_OK for pid in state.implement_programs)
        if finished and not running and not actions:
            actions.append(Action("stage", "done"))
        return actions or [Action("wait", f"implementing: {len(running)} ticket(s) in flight")]
    return [Action("fail", f"unknown stage {state.stage}")]


def apply(state: RunState, action: Action, now: float, *, result: str = "") -> None:
    """Record an executed action on the state (the driver calls this after doing it)."""
    state.log.append({"at": round(now - state.started_at), "action": action.kind, "value": _short(action.value),
                      "note": action.note, "result": result})
    if action.kind == "stage":
        previous = state.stage
        if previous in TIMED_STAGES:
            state.stage_times.setdefault(previous, {"start": now})["end"] = now
        state.stage = action.value
        if action.value in TIMED_STAGES:
            state.stage_times[action.value] = {"start": now}
        if action.value == "implementing" and not state.implement_sent_at:
            state.implement_sent_at = now
    elif action.kind == "answered":
        state.answered.add(action.value)
    elif action.kind == "track_sent":
        state.track_sent = True
    elif action.kind == "approve":
        state.approved[action.value] = action.note
    elif action.kind == "merge":
        state.prs[action.value] = "merged" if result == "merged" else "merge_failed"
        if result != "merged":
            state.failures.append(f"merge of {action.value} failed: {result}")
    elif action.kind == "fail":
        if action.value not in state.failures:
            state.failures.append(action.value)


def observe(state: RunState, snap: Snapshot) -> None:
    """Keep the latest view of every ticket and PR of our programs."""
    ours = set(state.ours)
    for view in snap.live:
        if str(view.get("program_id") or "") not in ours:
            continue
        workflow_id = str(view.get("workflow_id") or "")
        if workflow_id:
            state.tickets[workflow_id] = {k: view.get(k) for k in (
                "program_id", "jira_issue_key", "label", "state", "kind", "pr_url", "revisions", "elapsed_seconds")}
        url = str(view.get("pr_url") or "")
        if url and url not in state.prs:
            state.prs[url] = "open"


def start_message(cfg: Config, prd_text: str) -> str:
    return (
        "Start a software delivery program for this PRD. It is an automated regression run on the sandbox "
        f"Jira project {cfg.jira_project} and the sandbox repository {cfg.repo}.\n\n{prd_text.strip()}"
    )


def _short(value: str, limit: int = 120) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── report ────────────────────────────────────────────────────────────────────


def build_report(
    state: RunState,
    *,
    finished_at: float,
    history: list[dict[str, Any]],
    costs: dict[str, float],
    jira_keys: list[str],
    cfg: Config,
) -> dict[str, Any]:
    """The assertions and figures of a finished run. ``ok`` is true only when every check passed."""
    ours = set(state.ours)
    outcomes = {
        str(item.get("workflow_id") or ""): str(item.get("outcome") or "")
        for item in history
        if str(item.get("program_id") or "") in ours
    }
    prs = dict(state.prs)
    for item in history:
        url = str(item.get("pr_url") or "")
        if str(item.get("program_id") or "") in ours and url:
            if item.get("outcome") == "merged" or url not in prs:
                prs[url] = "merged" if item.get("outcome") == "merged" else "open"
    lost = sorted(wf for wf in state.tickets if outcomes.get(wf, "") not in TICKET_OK)
    unmerged = sorted(url for url, status in prs.items() if status != "merged")
    stages = {
        stage: {"seconds": round(times["end"] - times["start"]) if "end" in times else None, "completed": "end" in times}
        for stage, times in ((s, state.stage_times.get(s, {})) for s in TIMED_STAGES)
    }
    checks = {
        "planning_completed": stages["planning"]["completed"],
        "implementing_completed": stages["implementing"]["completed"],
        "reached_done": state.stage == "done",
        "tickets_filed": bool(jira_keys) or bool(state.tickets),
        "prs_created": bool(prs),
        "prs_merged": bool(prs) and not unmerged,
        "no_stuck_or_failed_steps": not state.failures,
        "no_lost_coding_jobs": not lost,
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "failures": list(state.failures),
        "stage": state.stage,
        "stages": stages,
        "total_seconds": round(finished_at - state.started_at),
        "cost_usd": round(sum(costs.values()), 4),
        "cost_by_ticket": {key: round(value, 4) for key, value in costs.items()},
        "programs": {"planning": state.planning_programs, "implement": state.implement_programs},
        "session_id": state.session_id,
        "jira_keys": jira_keys,
        "prs": dict(sorted(prs.items())),
        "unmerged_prs": unmerged,
        "lost_tickets": lost,
        "tickets": state.tickets,
        "gates_approved": state.approved,
        "config": {"gateway_url": cfg.gateway_url, "jira_project": cfg.jira_project, "repo": cfg.repo,
                   "actor": cfg.actor, "timeout_minutes": cfg.timeout_minutes},
        "log": state.log,
    }


def render_markdown(report: dict[str, Any]) -> str:
    mark = {True: "pass", False: "FAIL"}
    lines = [
        f"# E2E regression: {'PASS' if report['ok'] else 'FAIL'}",
        "",
        f"- Total time: {round(report['total_seconds'] / 60, 1)} minutes",
        f"- Ticket coding cost: ${report['cost_usd']:.2f}",
        f"- Jira project {report['config']['jira_project']}, repository {report['config']['repo']}",
        f"- Programs: planning {', '.join(report['programs']['planning']) or '-'}; "
        f"implement {', '.join(report['programs']['implement']) or '-'}",
        "",
        "## Checks",
        "",
        "| Check | Result |",
        "| --- | --- |",
        *[f"| {name.replace('_', ' ')} | {mark[bool(value)]} |" for name, value in report["checks"].items()],
        "",
        "## Stages",
        "",
        "| Stage | Minutes | Completed |",
        "| --- | --- | --- |",
        *[
            f"| {stage} | {'-' if info['seconds'] is None else round(info['seconds'] / 60, 1)} | {info['completed']} |"
            for stage, info in report["stages"].items()
        ],
        "",
        "## Pull requests",
        "",
        *([f"- {url}: {status}" for url, status in report["prs"].items()] or ["- none"]),
    ]
    if report["failures"]:
        lines += ["", "## Failures", "", *[f"- {failure}" for failure in report["failures"]]]
    if report["lost_tickets"]:
        lines += ["", "## Tickets without a merged outcome", "", *[f"- {wf}" for wf in report["lost_tickets"]]]
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out_dir / "report.json", out_dir / "report.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, default=list) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


# ── HTTP clients (not used by the unit tests) ─────────────────────────────────


class HttpError(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(f"HTTP {status}: {detail[:300]}")
        self.status = status


def _request(method: str, url: str, *, headers: dict[str, str], body: Any = None, timeout: float = 60) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/json", "User-Agent": "ts-harness-e2e-regression", **headers,
        **({"Content-Type": "application/json"} if data is not None else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - configured https URL
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise HttpError(exc.code, exc.read().decode("utf-8", "replace")) from exc
    return json.loads(raw) if raw else {}


class Gateway:
    def __init__(self, cfg: Config, request: Callable[..., Any] = _request):
        self.cfg = cfg
        self._request = request
        self.binding = f"web:e2e-regression-{int(time.time())}"

    def _call(self, method: str, path: str, body: Any = None, timeout: float = 60) -> Any:
        headers = {"Authorization": f"Bearer {self.cfg.token}"}
        return self._request(method, self.cfg.gateway_url + path, headers=headers, body=body, timeout=timeout)

    def _actor(self) -> dict[str, str]:
        actor = {"actor_id": self.cfg.actor}
        if "@" in self.cfg.actor:
            actor["email"] = self.cfg.actor
        return actor

    def health(self) -> Any:
        return self._call("GET", "/healthz")

    def send(self, text: str, session_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text, "channel": "web", "source": "web", "thread_id": self.binding,
                                "binding_key": self.binding, "actor": self._actor()}
        if session_id:
            body["session_id"] = session_id
        if self.cfg.project_key:
            body["project_key"] = self.cfg.project_key
        return self._call("POST", "/session/message", body, timeout=900)

    def inbox(self) -> list[dict[str, Any]]:
        return list((self._call("GET", "/admin/inbox") or {}).get("items") or [])

    def program_ids(self) -> list[str]:
        listed = self._call("GET", "/admin/programs?limit=100") or {}
        return [str(p.get("id")) for p in listed.get("programs") or [] if p.get("id")]

    def program_status(self, program_id: str) -> dict[str, Any]:
        return self._call("GET", f"/admin/programs/{urllib.parse.quote(program_id)}/status") or {}

    def live(self) -> list[dict[str, Any]]:
        return list((self._call("GET", "/admin/live") or {}).get("items") or [])

    def history(self, days: int = 2) -> list[dict[str, Any]]:
        return list((self._call("GET", f"/admin/history?days={days}") or {}).get("items") or [])

    def story_cost(self, program_id: str, workflow_id: str) -> float:
        path = f"/admin/programs/{urllib.parse.quote(program_id)}/tickets/{urllib.parse.quote(workflow_id)}/story"
        return float((self._call("GET", path) or {}).get("cost_usd") or 0)

    def approve(self, task_id: str) -> Any:
        return self._call("POST", "/admin/workflows/approve",
                          {"id": task_id, "actor": self._actor(), "comments": "Approved by the e2e regression run"})

    def cancel_program(self, program_id: str) -> Any:
        return self._call("POST", f"/admin/programs/{urllib.parse.quote(program_id)}/cancel",
                          {"reason": "e2e regression cleanup", "actor": self._actor()})


class GitHub:
    """Merges and closes PRs, only in the configured sandbox repository."""

    def __init__(self, cfg: Config, request: Callable[..., Any] = _request):
        self.cfg = cfg
        self._request = request

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Authorization": f"Bearer {self.cfg.github_token}", "Accept": "application/vnd.github+json"}
        return self._request(method, "https://api.github.com" + path, headers=headers, body=body)

    def _guard(self, repo: str) -> None:
        if repo.lower() != self.cfg.repo.lower() or repo.lower() not in {r.lower() for r in self.cfg.sandbox_repos}:
            raise ConfigError(f"refusing to change {repo}: not the sandbox repository")

    def repo_info(self) -> dict[str, Any]:
        return self._call("GET", f"/repos/{self.cfg.repo}") or {}

    def merge(self, url: str) -> str:
        parsed = pr_repo(url)
        if not parsed:
            return "not a GitHub PR URL"
        repo, number = parsed
        self._guard(repo)
        try:
            out = self._call("PUT", f"/repos/{repo}/pulls/{number}/merge", {"merge_method": self.cfg.merge_method})
        except HttpError as exc:
            if exc.status == 405 and (self._call("GET", f"/repos/{repo}/pulls/{number}") or {}).get("merged"):
                return "merged"
            return str(exc)
        return "merged" if (out or {}).get("merged") else str((out or {}).get("message") or "not merged")

    def close_open_prs(self, only: set[str] | None = None) -> list[str]:
        self._guard(self.cfg.repo)
        closed = []
        for pr in self._call("GET", f"/repos/{self.cfg.repo}/pulls?state=open&per_page=100") or []:
            url = str(pr.get("html_url") or "")
            if only is not None and url not in only:
                continue
            self._call("PATCH", f"/repos/{self.cfg.repo}/pulls/{pr['number']}", {"state": "closed"})
            closed.append(url)
        return closed


# ── driver ────────────────────────────────────────────────────────────────────


def snapshot(gateway: Gateway, state: RunState, now: float) -> Snapshot:
    snap = Snapshot(now=now, inbox=gateway.inbox(), program_ids=gateway.program_ids(), live=gateway.live())
    adopt_programs(state, snap)
    for program_id in state.ours:
        try:
            snap.programs[program_id] = gateway.program_status(program_id)
        except HttpError as exc:
            snap.programs[program_id] = {"status": "", "error": str(exc)}
    return snap


def execute(action: Action, state: RunState, gateway: Gateway, github: GitHub) -> str:
    if action.kind == "send":
        reply = gateway.send(action.value, state.session_id)
        state.session_id = str(reply.get("session_id") or state.session_id)
        if reply.get("status") == "blocked" or reply.get("error"):
            return f"blocked: {_short(str(reply.get('error') or reply.get('output') or ''))}"
        return "sent"
    if action.kind == "approve":
        out = gateway.approve(action.value) or {}
        return str(out.get("status") or "approved")
    if action.kind == "merge":
        return github.merge(action.value)
    return ""


def run(cfg: Config, gateway: Gateway, github: GitHub, *, clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep, say: Callable[[str], None] = print) -> RunState:
    prd_text = Path(cfg.prd_path).read_text(encoding="utf-8")
    state = RunState(started_at=clock())
    state.baseline = set(gateway.program_ids())
    while state.stage != "done" and not state.failures:
        now = clock()
        try:
            snap = snapshot(gateway, state, now)
        except (HttpError, OSError) as exc:
            say(f"[e2e] poll failed, retrying: {exc}")
            snap = Snapshot(now=now)
            if now - state.started_at > cfg.timeout_minutes * 60:
                apply(state, Action("fail", f"global timeout (last poll error: {exc})"), now)
                break
            sleep(cfg.poll_seconds)
            continue
        observe(state, snap)
        actions = decide(state, snap, cfg, prd_text)
        acted = False
        for action in actions:
            if action.kind == "wait":
                continue
            acted = True
            try:
                result = execute(action, state, gateway, github)
            except (HttpError, OSError, ConfigError) as exc:
                result = f"error: {exc}"
                if action.kind in {"send", "approve"}:
                    state.failures.append(f"{action.kind} failed: {exc}")
            apply(state, action, clock(), result=result)
            say(f"[e2e] {state.stage}: {action.kind} {action.note or _short(action.value, 60)} -> {result or 'ok'}")
            if action.kind == "send" and result.startswith("blocked"):
                state.failures.append(f"chat message was blocked ({action.note}): {result}")
        if not acted:
            waiting = next((a.value for a in actions if a.kind == "wait"), "")
            say(f"[e2e] {state.stage}: {waiting}")
        if state.stage != "done" and not state.failures:
            sleep(cfg.poll_seconds if not acted or state.stage == "implementing" else 5)
    return state


def collect_and_report(cfg: Config, state: RunState, gateway: Gateway, out_dir: Path) -> dict[str, Any]:
    try:
        history = gateway.history()
    except (HttpError, OSError):
        history = []
    costs: dict[str, float] = {}
    for workflow_id, ticket in state.tickets.items():
        try:
            costs[str(ticket.get("jira_issue_key") or workflow_id)] = gateway.story_cost(str(ticket["program_id"]), workflow_id)
        except (HttpError, OSError, KeyError, ValueError):
            pass
    jira_keys: list[str] = []
    for program_id in state.ours:
        try:
            artifacts = gateway.program_status(program_id).get("artifacts") or {}
        except (HttpError, OSError):
            continue
        keys = list(artifacts.get("jira_story_keys") or artifacts.get("jira_issue_keys") or [])
        for key in keys:
            if str(key) not in jira_keys:
                jira_keys.append(str(key))
    report = build_report(state, finished_at=time.time(), history=history, costs=costs, jira_keys=jira_keys, cfg=cfg)
    write_report(report, out_dir)
    return report


def cleanup(cfg: Config, gateway: Gateway, github: GitHub, program_ids: list[str], *,
            all_open_prs: bool, run_prs: set[str], say: Callable[[str], None] = print) -> list[str]:
    """Cancel the run's programs still open and close the sandbox repository's leftover PRs."""
    problems = []
    for program_id in program_ids:
        try:
            status = str(gateway.program_status(program_id).get("status") or "")
            if status in PROGRAM_OK | PROGRAM_BAD:
                continue
            gateway.cancel_program(program_id)
            say(f"[e2e] cleanup: cancelled program {program_id}")
        except (HttpError, OSError) as exc:
            problems.append(f"cancel {program_id}: {exc}")
    if cfg.github_token:
        try:
            for url in github.close_open_prs(None if all_open_prs else run_prs):
                say(f"[e2e] cleanup: closed {url}")
        except (HttpError, OSError, ConfigError) as exc:
            problems.append(f"close PRs: {exc}")
    return problems


def dry_run(cfg: Config, gateway: Gateway, github: GitHub, say: Callable[[str], None] = print) -> int:
    failed = 0
    checks: list[tuple[str, Callable[[], Any]]] = [
        ("gateway health", gateway.health),
        ("admin inbox (token)", gateway.inbox),
        ("program list", gateway.program_ids),
        ("live view", gateway.live),
        ("sandbox repository", github.repo_info),
    ]
    for name, check in checks:
        try:
            out = check()
            detail = ""
            if name == "sandbox repository":
                perms = (out or {}).get("permissions") or {}
                if not perms.get("push"):
                    raise ConfigError("the E2E GitHub token cannot push (merge) to the sandbox repository")
                detail = f" ({out.get('full_name')})"
            say(f"[dry-run] ok: {name}{detail}")
        except (HttpError, OSError, ConfigError) as exc:
            failed += 1
            say(f"[dry-run] FAIL: {name}: {exc}")
    say("[dry-run] configuration and connectivity " + ("FAILED" if failed else "ok") + "; nothing was changed")
    return 1 if failed else 0


def main(argv: list[str], env: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", default="", help="report directory (default e2e-reports/<timestamp>)")
    parser.add_argument("--dry-run", action="store_true", help="validate config and connectivity only")
    parser.add_argument("--no-cleanup", action="store_true", help="leave programs and PRs as they are after the run")
    parser.add_argument("--cleanup-only", action="store_true", help="cancel --program ids and close open sandbox PRs")
    parser.add_argument("--program", action="append", default=[], help="with --cleanup-only: program to cancel")
    args = parser.parse_args(argv)

    try:
        cfg = config_from_env(dict(os.environ if env is None else env))
    except ConfigError as exc:
        print(f"[e2e] refusing to run: {exc}", file=sys.stderr)
        return 2
    errors = safety_errors(cfg)
    if errors:
        for error in errors:
            print(f"[e2e] refusing to run: {error}", file=sys.stderr)
        return 2
    gateway, github = Gateway(cfg), GitHub(cfg)
    if args.dry_run:
        return dry_run(cfg, gateway, github)
    if args.cleanup_only:
        problems = cleanup(cfg, gateway, github, args.program, all_open_prs=True, run_prs=set())
        for problem in problems:
            print(f"[e2e] cleanup: {problem}", file=sys.stderr)
        return 1 if problems else 0

    out_dir = Path(args.out or f"e2e-reports/{time.strftime('%Y%m%dT%H%M%S')}")
    state = run(cfg, gateway, github)
    report = collect_and_report(cfg, state, gateway, out_dir)
    if not args.no_cleanup:
        problems = cleanup(cfg, gateway, github, state.ours, all_open_prs=False,
                           run_prs={url for url, status in state.prs.items() if status != "merged"})
        for problem in problems:
            print(f"[e2e] cleanup: {problem}", file=sys.stderr)
    print(f"[e2e] {'PASS' if report['ok'] else 'FAIL'}; report in {out_dir}/report.md")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
