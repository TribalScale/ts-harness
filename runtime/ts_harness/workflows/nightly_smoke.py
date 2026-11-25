"""Nightly smoke failures become proposed bugs.

A web repository started from the ``react-web`` template and linked to its API runs a nightly
smoke workflow (``.github/workflows/nightly-smoke.yml``): the built app against the real API and
a real Postgres. Its acceptance tests only ever see a fake API, so this is what catches the app
and the API drifting apart. When the run fails it POSTs a report to ``/webhooks/smoke``; this
module checks it and starts a ``bug_to_jira_work_orders`` program at ``bug_intake`` with the
report as its summary. That program's own gates (pick the repository, approve the bug, draft
and file the work order) each wait for a person, so nothing reaches Jira unapproved: the start
is the proposal.

- **Auth.** Each web repository has its own report token, derived from the harness secret
  ``SMOKE_REPORT_SECRET`` and the repository's name (HMAC), so a token posted from one
  repository cannot report for another and the harness keeps no token table. Rotating the
  secret rotates every token. No secret: the endpoint is off.
- **De-duplication.** A failure is keyed by (web repository, scenario, route). A key already
  proposed in the last 24 hours, or whose program is still open, is known; a report with only
  known failures proposes nothing. The rest go into one program per report, so a broken night
  yields one proposal, not one per page. The keys live in ``runtime_kv`` (namespace
  ``nightly-smoke``), shared by every replica.

See docs/runbooks/nightly-smoke.md.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SECRET_ENV = "SMOKE_REPORT_SECRET"
PROGRAM_TYPE = "bug_to_jira_work_orders"
ENTRY_NODE = "bug_intake"
KV_NAMESPACE = "nightly-smoke"
ACTOR = "nightly-smoke"
# A failure proposed this recently counts as known even if its program already ended: a rerun
# of the same broken night, or a second run on demand, proposes nothing new.
RECENT_SECONDS = 24 * 3600
# Derived program statuses that mean "still being worked on" (see system_jobs).
OPEN_PROGRAM_STATUSES = frozenset({"running", "blocked"})

MAX_FAILURES = 50
MAX_TEXT = 2000
MAX_SUMMARY = 8000
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")


class SmokeReportError(Exception):
    """A report that cannot be accepted; ``status`` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SmokeFailure:
    scenario: str
    route: str = ""
    step: str = ""
    kind: str = ""
    error: str = ""
    feature: str = ""

    def key(self) -> str:
        """What makes two failures the same bug: the scenario and the route, not the error text
        (which changes with the data, such as how many rows failed)."""
        return f"{self.scenario.strip().lower()}|{self.route.strip().lower()}"


@dataclass(frozen=True)
class SmokeReport:
    repo: str
    run_url: str
    failures: tuple[SmokeFailure, ...]
    api_repo: str = ""
    api_ref: str = ""
    run_id: str = ""
    web_sha: str = ""
    api_sha: str = ""
    artifact_url: str = ""


# Auth

def report_secret() -> str:
    return os.environ.get(SECRET_ENV, "").strip()


def report_token(repo: str, secret: str | None = None) -> str:
    """The report token for one web repository (store it there as ``HARNESS_SMOKE_TOKEN``)."""
    key = (report_secret() if secret is None else secret).encode()
    if not key:
        raise SmokeReportError(503, f"smoke reports are off: {SECRET_ENV} is not set")
    return hmac.new(key, f"ts-harness-smoke:{repo.strip().lower()}".encode(), hashlib.sha256).hexdigest()


def token_matches(repo: str, presented: str, secret: str | None = None) -> bool:
    if not presented:
        return False
    return hmac.compare_digest(report_token(repo, secret), presented.strip())


def bearer(authorization: str | None) -> str:
    scheme, _, value = str(authorization or "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" else ""


# Validation

def _text(value: Any, limit: int = MAX_TEXT) -> str:
    return str(value or "").strip()[:limit] if isinstance(value, (str, int, float)) or value is None else ""


def _https_url(value: Any, name: str) -> str:
    text = _text(value, 1000)
    if text and not text.startswith("https://"):
        raise SmokeReportError(400, f"{name} must be an https URL")
    return text


def validate_report(body: Any) -> SmokeReport:
    """The report, or ``SmokeReportError(400)`` saying what is wrong with it."""
    if not isinstance(body, dict):
        raise SmokeReportError(400, "the report must be a JSON object")
    repo = _text(body.get("repo"), 200)
    if not _REPO.match(repo):
        raise SmokeReportError(400, "repo must be owner/name")
    api_repo = _text(body.get("api_repo"), 200)
    if api_repo and not _REPO.match(api_repo):
        raise SmokeReportError(400, "api_repo must be owner/name")
    run_url = _https_url(body.get("run_url"), "run_url")
    if not run_url:
        raise SmokeReportError(400, "run_url is required")
    shas = {}
    for name in ("web_sha", "api_sha"):
        sha = _text(body.get(name), 64)
        if sha and not _SHA.match(sha):
            raise SmokeReportError(400, f"{name} must be a commit SHA")
        shas[name] = sha
    raw = body.get("failures")
    if not isinstance(raw, list) or not raw:
        raise SmokeReportError(400, "failures must be a non-empty list")
    failures = []
    for item in raw[:MAX_FAILURES]:
        if not isinstance(item, dict) or not _text(item.get("scenario"), 300):
            raise SmokeReportError(400, "each failure needs a scenario")
        failures.append(SmokeFailure(
            scenario=_text(item.get("scenario"), 300),
            route=_text(item.get("route"), 300),
            step=_text(item.get("step"), 500),
            kind=_text(item.get("kind"), 60),
            error=_text(item.get("error")),
            feature=_text(item.get("feature"), 300),
        ))
    return SmokeReport(
        repo=repo,
        api_repo=api_repo,
        api_ref=_text(body.get("api_ref"), 200),
        run_id=_text(body.get("run_id"), 40),
        run_url=run_url,
        artifact_url=_https_url(body.get("artifact_url"), "artifact_url"),
        failures=tuple(failures),
        **shas,
    )


# The proposal

def fingerprint(repo: str, failure: SmokeFailure) -> str:
    return hashlib.sha256(f"{repo.lower()}|{failure.key()}".encode()).hexdigest()[:24]


def bug_summary(report: SmokeReport, failures: list[SmokeFailure]) -> str:
    """The bug report the program starts from: what failed, where, and how to see it."""
    against = f" against {report.api_repo}" + (f" ({report.api_ref})" if report.api_ref else "") if report.api_repo else ""
    lines = [f"The nightly smoke run failed for {report.repo}{against}.", "", "What failed:"]
    for f in failures:
        where = ", ".join(part for part in (
            f'scenario "{f.scenario}"',
            f'step "{f.step}"' if f.step else "",
            f"route {f.route}" if f.route else "",
        ) if part)
        kind = f"{f.kind}: " if f.kind else ""
        lines.append(f"- {where}: {kind}{f.error or 'failed'}")
    lines += ["", f"Run: {report.run_url}"]
    if report.artifact_url:
        lines.append(f"Screenshots and traces: {report.artifact_url}")
    commits = ", ".join(part for part in (
        f"web {report.web_sha}" if report.web_sha else "",
        f"API {report.api_sha}" if report.api_sha else "",
    ) if part)
    if commits:
        lines.append(f"Commits: {commits}")
    lines += [
        "",
        "The smoke run opens the built web app against the real API and a real database (the "
        "features tagged @smoke: sign in, open every page, one write) and checks every API "
        "response against the API's openapi.json. Proposed automatically: approve the "
        "repository, the diagnosis and the bug before anything is filed in Jira.",
    ]
    text = "\n".join(lines)
    return text if len(text) <= MAX_SUMMARY else text[: MAX_SUMMARY - 20] + "\n… (truncated)"


def _kv_key(repo: str) -> str:
    return repo.lower().replace("/", "--")


def _program_open(memory: Any, program_id: str) -> bool:
    if not program_id:
        return False
    try:
        status = memory.get_program_status(program_id) or {}
    except Exception as exc:  # noqa: BLE001 - unknown counts as closed: better a duplicate than a silent miss
        logger.info("smoke dedupe: status of program %s unknown: %s", program_id, exc)
        return False
    return str(status.get("status") or "") in OPEN_PROGRAM_STATUSES


def _known(entry: Any, memory: Any, now: float) -> bool:
    if not isinstance(entry, dict):
        return False
    if now - float(entry.get("at") or 0) < RECENT_SECONDS:
        return True
    return _program_open(memory, str(entry.get("program_id") or ""))


def handle_smoke_report(
    body: Any,
    token: str,
    *,
    tenant_id: str | None = None,
    memory: Any = None,
    service: Any = None,
    secret: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Check a report and propose one bug for its new failures.

    ``{"status": "proposed", "program_id", ...}``, or ``{"status": "duplicate", ...}`` when every
    failure is already known. Raises ``SmokeReportError`` (503 off, 401 bad token, 400 invalid).
    """
    from ts_harness.storage.kv import kv_update

    if not (report_secret() if secret is None else secret):
        raise SmokeReportError(503, f"smoke reports are off: {SECRET_ENV} is not set")
    repo = _text(body.get("repo"), 200) if isinstance(body, dict) else ""
    # The token is checked before anything else in the report is looked at.
    if not repo or not token_matches(repo, token, secret):
        raise SmokeReportError(401, "invalid smoke report token for this repository")
    report = validate_report(body)
    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    if service is None:
        from ts_harness.workflows.delivery_program import get_delivery_program_service

        service = get_delivery_program_service()
    now = time.time() if now is None else now

    by_print: dict[str, SmokeFailure] = {}
    for failure in report.failures:
        by_print.setdefault(fingerprint(report.repo, failure), failure)
    # Decided outside the compare-and-swap (status lookups are network calls), then claimed in it.
    before = (memory.kv_get(KV_NAMESPACE, _kv_key(report.repo)).get("value") or {}).get("failures") or {}
    candidates = [fp for fp in by_print if not _known(before.get(fp), memory, now)]
    claimed: list[str] = []

    def claim(value: Any) -> Any:
        state = dict(value or {})
        seen = dict(state.get("failures") or {})
        claimed.clear()
        for fp in candidates:
            # Changed since we looked: a concurrent report claimed it, and theirs stands.
            if fp in seen and seen[fp] != before.get(fp):
                continue
            seen[fp] = {"at": now, "program_id": "", "scenario": by_print[fp].scenario, "route": by_print[fp].route}
            claimed.append(fp)
        state["failures"] = _pruned(seen, now)
        return state

    kv_update(KV_NAMESPACE, _kv_key(report.repo), claim, default={}, client=memory)
    known = [fp for fp in by_print if fp not in claimed]
    if not claimed:
        return {"status": "duplicate", "repo": report.repo, "known_failures": len(known), "new_failures": 0}

    failures = [by_print[fp] for fp in claimed]
    try:
        from ts_harness.workflows.delivery_program import resolve_program_tenant

        result = service.create_program(
            entry_node=ENTRY_NODE,
            program_type=PROGRAM_TYPE,
            summary=bug_summary(report, failures),
            tenant_id=resolve_program_tenant(tenant_id),
            # A console (web) thread, like a program started from the console: its gates and
            # chat show there and in the Inbox.
            binding_key=f"web:web:nightly-smoke-{report.repo.replace('/', '-')}-{report.run_id or int(now)}",
            requester_actor_id=ACTOR,
            artifacts={"repo_url": f"https://github.com/{report.repo}"},
            actor=ACTOR,
        )
    except Exception:
        # Release the claims so the next report (or a rerun) can propose it.
        kv_update(KV_NAMESPACE, _kv_key(report.repo), lambda v: _released(v, claimed), default={}, client=memory)
        raise
    program_id = str(getattr(result, "program_id", "") or "")
    kv_update(KV_NAMESPACE, _kv_key(report.repo), lambda v: _recorded(v, claimed, program_id), default={}, client=memory)
    logger.info("smoke report for %s proposed program %s (%d new failures)", report.repo, program_id, len(failures))
    return {
        "status": "proposed",
        "repo": report.repo,
        "program_id": program_id,
        "program_type": PROGRAM_TYPE,
        "entry_phase": ENTRY_NODE,
        "new_failures": len(failures),
        "known_failures": len(known),
    }


def _pruned(seen: dict[str, Any], now: float) -> dict[str, Any]:
    """Forget failures not seen for 30 days, so the record stays small."""
    return {fp: e for fp, e in seen.items() if isinstance(e, dict) and now - float(e.get("at") or 0) < 30 * 86400}


def _released(value: Any, prints: list[str]) -> Any:
    state = dict(value or {})
    state["failures"] = {fp: e for fp, e in (state.get("failures") or {}).items() if fp not in prints}
    return state


def _recorded(value: Any, prints: list[str], program_id: str) -> Any:
    state = dict(value or {})
    seen = dict(state.get("failures") or {})
    for fp in prints:
        if isinstance(seen.get(fp), dict):
            seen[fp] = {**seen[fp], "program_id": program_id}
    state["failures"] = seen
    return state


def main(argv: list[str] | None = None) -> int:
    """``python3 -m ts_harness.workflows.nightly_smoke token owner/web | gh secret set HARNESS_SMOKE_TOKEN -R owner/web``

    Prints a repository's report token for the manual setup. Refuses to print to a terminal, so
    the token goes straight into ``gh secret set`` and not into a scrollback or a shell history.
    """
    import sys

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "token" or not _REPO.match(args[1]):
        print("usage: python3 -m ts_harness.workflows.nightly_smoke token owner/web | gh secret set ...", file=sys.stderr)
        return 2
    if sys.stdout.isatty():
        print("refusing to print the token to a terminal: pipe it into `gh secret set`", file=sys.stderr)
        return 2
    try:
        sys.stdout.write(report_token(args[1]))
    except SmokeReportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
