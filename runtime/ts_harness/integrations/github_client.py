"""GitHub REST client used by merge polling, merge readiness, release and compensation.

``github_for`` authenticates each repository call with a GitHub App
installation token for that repository when an App is registered for the
host, and falls back to the ``GITHUB_TOKEN`` personal access token otherwise.
The transport is injectable so tests never touch the network.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import time
from dataclasses import dataclass
from typing import Any, Callable

API_BASE = "https://api.github.com"
USER_AGENT = "ts-harness"

_PR_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)/?$"
)
_REPO_RE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:)?(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Returns (status, body) or (status, body, headers).
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple]
TokenProvider = Callable[[str, str], str]
_REPO_PATH_RE = re.compile(r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/?]+)")
MAX_RETRY_WAIT = 60
RATE_LIMITS: dict[str, dict[str, Any]] = {}

SUCCESS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
# Published by stages that run after the checks wait, so they cannot gate it unless named as required.
LATER_GATE_CONTEXTS = frozenset({"harness/independent-review", "harness/code-review", "harness/qa"})
PENDING_STATES = frozenset({"pending", "queued", "in_progress", "requested", "waiting", "expected"})


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API error ({status}): {message[:300]}")
        self.status = status


def classify_github_wait_error(exc: BaseException) -> tuple[bool, str]:
    """Return whether a wait-setup error should be retried, and the message to store."""
    if isinstance(exc, GitHubError) and exc.status in {401, 403}:
        permission = (
            "token needs contents, pull requests, commit statuses, and actions read; "
            "administration read when branch protection is enforced. "
            "Fine-grained tokens have no Checks permission; TS Harness falls back to Actions jobs."
        )
        return False, f"{exc} {permission}"
    if isinstance(exc, GitHubError) and (exc.status in {408, 429} or exc.status >= 500):
        return True, str(exc)
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True, str(exc)
    return False, str(exc)


def parse_github_pr_url(pr_url: str) -> tuple[str, str, int] | None:
    match = _PR_URL_RE.match(str(pr_url or "").strip())
    if not match:
        return None
    return match.group("owner"), match.group("repo"), int(match.group("number"))


def parse_repo_slug(repo: str) -> tuple[str, str] | None:
    """Accept ``owner/repo``, an https URL, or an ssh remote."""
    match = _REPO_RE.match(str(repo or "").strip())
    if not match:
        return None
    return match.group("owner"), match.group("repo")


def repo_host(repo_url: str | None) -> str:
    """Host of an https or ssh GitHub URL; empty for ``owner/repo`` or nothing."""
    raw = str(repo_url or "").strip()
    if raw.startswith("git@"):
        return raw[4:].split(":", 1)[0].lower()
    if "://" in raw:
        return (urllib.parse.urlparse(raw).hostname or "").lower()
    return ""


def _urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
    except urllib.error.HTTPError as exc:
        detail = exc.read() if hasattr(exc, "read") else str(exc).encode()
        response_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
        return exc.code, detail or b"", response_headers


def _record_rate_limit(host: str, headers: dict[str, str]) -> None:
    remaining = headers.get("x-ratelimit-remaining")
    if remaining is None:
        return
    entry = {
        "remaining": int(remaining),
        "limit": int(headers.get("x-ratelimit-limit") or 0),
        "reset": int(headers.get("x-ratelimit-reset") or 0),
        "observed_at": int(time.time()),
    }
    RATE_LIMITS[host] = entry
    from ts_harness.observability import prometheus

    if prometheus.PROMETHEUS_AVAILABLE:
        prometheus.github_rate_limit_remaining.labels(host=host).set(entry["remaining"])


def _retry_wait(status: int, headers: dict[str, str], now: float) -> float | None:
    """Seconds to wait before retrying a throttled call, or None when it is not throttling."""
    if status not in (403, 429):
        return None
    retry_after = headers.get("retry-after")
    if retry_after and retry_after.strip().isdigit():
        return min(float(retry_after), MAX_RETRY_WAIT)
    if headers.get("x-ratelimit-remaining") == "0" and headers.get("x-ratelimit-reset"):
        return min(max(float(headers["x-ratelimit-reset"]) - now, 1.0), MAX_RETRY_WAIT)
    return 5.0 if status == 429 else None


@dataclass
class GitHubClient:
    token: str = ""
    api_base: str = API_BASE
    transport: Transport = _urllib_transport
    token_provider: TokenProvider | None = None
    host: str = "github.com"
    sleep: Callable[[float], None] = time.sleep

    @classmethod
    def from_env(cls, *, transport: Transport | None = None) -> "GitHubClient":
        return github_for(None, transport=transport)

    def _token_for(self, path: str) -> str:
        match = _REPO_PATH_RE.match(path)
        if self.token_provider and match:
            return self.token_provider(match.group("owner"), match.group("repo"))
        if not self.token:
            raise GitHubError(401, "GITHUB_TOKEN is required for this call (the GitHub App only covers repositories)")
        return self.token

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        allow_404: bool = False,
    ) -> Any:
        url = f"{self.api_base}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})}"
        headers = {
            "Authorization": f"Bearer {self._token_for(path)}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        for attempt in range(3):
            response = self.transport(method, url, headers, data)
            status, raw = response[0], response[1]
            response_headers = response[2] if len(response) > 2 else {}
            _record_rate_limit(self.host, response_headers)
            wait = _retry_wait(status, response_headers, time.time()) if attempt < 2 else None
            if wait is None:
                break
            self.sleep(wait)
        if status == 404 and allow_404:
            return None
        if status >= 400:
            raise GitHubError(status, raw.decode(errors="replace") if raw else "")
        if not raw:
            return {}
        return json.loads(raw.decode())

    # Pulls and reviews

    def get_pull(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self.request("GET", f"/repos/{owner}/{repo}/pulls/{number}")

    def list_reviews(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        return list(self.request("GET", f"/repos/{owner}/{repo}/pulls/{number}/reviews", query={"per_page": 100}) or [])

    def list_review_comments(self, owner: str, repo: str, number: int, review_id: int | str) -> list[dict[str, Any]]:
        path = f"/repos/{owner}/{repo}/pulls/{number}/reviews/{review_id}/comments"
        return list(self.request("GET", path, query={"per_page": 100}) or [])

    def list_issue_comments(self, owner: str, repo: str, number: int, *, since: str | None = None) -> list[dict[str, Any]]:
        path = f"/repos/{owner}/{repo}/issues/{number}/comments"
        return list(self.request("GET", path, query={"per_page": 100, "since": since}) or [])

    def get_issue(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self.request("GET", f"/repos/{owner}/{repo}/issues/{number}")

    def get_commit(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
        return self.request("GET", f"/repos/{owner}/{repo}/commits/{sha}")

    def collaborator_permission(self, owner: str, repo: str, username: str) -> str:
        """``admin``, ``maintain``, ``write``, ``triage``, ``read`` or ``none``."""
        payload = self.request("GET", f"/repos/{owner}/{repo}/collaborators/{username}/permission", allow_404=True)
        return str((payload or {}).get("role_name") or (payload or {}).get("permission") or "none")

    def close_pull(self, owner: str, repo: str, number: int) -> dict[str, Any]:
        return self.request("PATCH", f"/repos/{owner}/{repo}/pulls/{number}", body={"state": "closed"})

    def comment(self, owner: str, repo: str, number: int, body: str) -> dict[str, Any]:
        return self.request("POST", f"/repos/{owner}/{repo}/issues/{number}/comments", body={"body": body})

    def list_pull_files(self, owner: str, repo: str, number: int) -> list[dict[str, Any]]:
        """Changed files with patches; GitHub caps the listing at 3000 files."""
        files: list[dict[str, Any]] = []
        for page in range(1, 31):
            batch = list(
                self.request(
                    "GET",
                    f"/repos/{owner}/{repo}/pulls/{number}/files",
                    query={"per_page": 100, "page": page},
                )
                or []
            )
            files.extend(batch)
            if len(batch) < 100:
                break
        return files

    def create_review(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        commit_id: str,
        body: str,
        comments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Post a COMMENT review; TS Harness never approves or requests changes."""
        payload: dict[str, Any] = {"commit_id": commit_id, "body": body, "event": "COMMENT"}
        if comments:
            payload["comments"] = comments
        return self.request("POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", body=payload)

    def get_tree(self, owner: str, repo: str, ref: str, *, recursive: bool = True) -> dict[str, Any]:
        """Git tree for ``ref``. ``recursive`` requests the full tree in one call."""
        quoted = urllib.parse.quote(str(ref), safe="")
        query = {"recursive": "1"} if recursive else None
        return self.request("GET", f"/repos/{owner}/{repo}/git/trees/{quoted}", query=query)

    def get_file(self, owner: str, repo: str, path: str, *, ref: str) -> Any:
        """Contents API payload for one path at ``ref``. A missing path returns None."""
        quoted = urllib.parse.quote(str(path).lstrip("/"), safe="/")
        return self.request(
            "GET",
            f"/repos/{owner}/{repo}/contents/{quoted}",
            query={"ref": ref},
            allow_404=True,
        )

    def compare(self, owner: str, repo: str, base: str, head: str) -> dict[str, Any]:
        path = f"/repos/{owner}/{repo}/compare/{urllib.parse.quote(base, safe='')}...{urllib.parse.quote(head, safe='')}"
        return self.request("GET", path)

    def update_branch(self, owner: str, repo: str, number: int, *, expected_head_sha: str) -> dict[str, Any]:
        """Merge the base into the PR branch server-side; a 422 means head moved or conflict."""
        return self.request(
            "PUT",
            f"/repos/{owner}/{repo}/pulls/{number}/update-branch",
            body={"expected_head_sha": expected_head_sha},
        )

    def delete_branch(self, owner: str, repo: str, branch: str) -> bool:
        ref = urllib.parse.quote(f"heads/{branch}", safe="/")
        return self.request("DELETE", f"/repos/{owner}/{repo}/git/refs/{ref}", allow_404=True) is not None

    # Checks and statuses

    def combined_status(self, owner: str, repo: str, ref: str) -> dict[str, Any]:
        return self.request("GET", f"/repos/{owner}/{repo}/commits/{ref}/status", query={"per_page": 100})

    def check_runs(self, owner: str, repo: str, ref: str) -> list[dict[str, Any]]:
        try:
            payload = self.request("GET", f"/repos/{owner}/{repo}/commits/{ref}/check-runs", query={"per_page": 100})
        except GitHubError as exc:
            # Fine-grained PATs cannot be granted Checks; Actions: read still exposes Actions-produced checks.
            if exc.status != 403:
                raise
            return self.actions_job_runs(owner, repo, ref)
        return list((payload or {}).get("check_runs") or [])

    def actions_job_runs(self, owner: str, repo: str, ref: str) -> list[dict[str, Any]]:
        """GitHub Actions jobs for ``ref``, shaped like check runs (job name == check name)."""
        sha = ref if _SHA_RE.match(ref) else str(self.request("GET", f"/repos/{owner}/{repo}/commits/{ref}").get("sha") or "")
        payload = self.request("GET", f"/repos/{owner}/{repo}/actions/runs", query={"head_sha": sha, "per_page": 100})
        runs = sorted((payload or {}).get("workflow_runs") or [], key=lambda run: str(run.get("created_at") or ""))
        jobs: list[dict[str, Any]] = []
        for run in runs:
            listing = self.request("GET", f"/repos/{owner}/{repo}/actions/runs/{run['id']}/jobs", query={"per_page": 100})
            jobs.extend(
                {"name": job.get("name"), "status": job.get("status"), "conclusion": job.get("conclusion")}
                for job in (listing or {}).get("jobs") or []
            )
        return jobs

    def create_status(
        self,
        owner: str,
        repo: str,
        sha: str,
        *,
        state: str,
        context: str,
        description: str = "",
        target_url: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            body["target_url"] = target_url
        return self.request("POST", f"/repos/{owner}/{repo}/statuses/{sha}", body=body)

    def check_summary(self, owner: str, repo: str, ref: str, *, required: list[str] | None = None) -> dict[str, Any]:
        return summarize_checks(
            self.combined_status(owner, repo, ref),
            self.check_runs(owner, repo, ref),
            required=required,
        )

    # Branch protection

    def branch_protection(self, owner: str, repo: str, branch: str) -> dict[str, Any] | None:
        return self.request("GET", f"/repos/{owner}/{repo}/branches/{urllib.parse.quote(branch, safe='')}/protection", allow_404=True)

    def required_status_contexts(self, owner: str, repo: str, branch: str) -> list[str] | None:
        """Return required contexts, or ``None`` when the branch is unprotected."""
        protection = self.branch_protection(owner, repo, branch)
        if protection is None:
            return None
        checks = protection.get("required_status_checks") or {}
        contexts = set(checks.get("contexts") or [])
        contexts |= {str(c.get("context")) for c in checks.get("checks") or [] if c.get("context")}
        return sorted(contexts)

    # Release

    def dispatch_workflow(
        self,
        owner: str,
        repo: str,
        workflow: str,
        *,
        ref: str,
        inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = f"/repos/{owner}/{repo}/actions/workflows/{urllib.parse.quote(workflow, safe='')}/dispatches"
        self.request("POST", path, body={"ref": ref, "inputs": {k: str(v) for k, v in (inputs or {}).items()}})
        return {"workflow": workflow, "ref": ref, "inputs": inputs or {}}

    def create_deployment(
        self,
        owner: str,
        repo: str,
        *,
        ref: str,
        environment: str,
        description: str = "",
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/repos/{owner}/{repo}/deployments",
            body={
                "ref": ref,
                "environment": environment,
                "description": description,
                "payload": payload or {},
                "auto_merge": False,
                "required_contexts": [],
            },
        )

    def list_deployments(self, owner: str, repo: str, *, sha: str | None = None, environment: str | None = None) -> list[dict[str, Any]]:
        return list(self.request("GET", f"/repos/{owner}/{repo}/deployments", query={"sha": sha, "environment": environment}) or [])

    def deployment_statuses(self, owner: str, repo: str, deployment_id: int | str) -> list[dict[str, Any]]:
        return list(self.request("GET", f"/repos/{owner}/{repo}/deployments/{deployment_id}/statuses") or [])


def _app_provider(app: Any, pat: str) -> TokenProvider:
    from ts_harness.integrations.github_app import NotInstalled

    def provider(owner: str, repo: str) -> str:
        try:
            return app.repo_token(owner, repo)
        except NotInstalled as exc:
            if pat:
                return pat
            raise GitHubError(403, str(exc)) from exc

    return provider


def _default_host() -> str:
    from ts_harness.integrations.github_app import DEFAULT_HOST

    return (os.environ.get("GITHUB_APP_HOST") or DEFAULT_HOST).strip().lower()


def github_for(repo_url: str | None = None, *, transport: Transport | None = None) -> GitHubClient:
    """Client for the repository's host: App installation tokens when registered, else the PAT."""
    from ts_harness.integrations.github_app import api_base_for_host, app_for

    host = repo_host(repo_url) or _default_host()
    pat = os.environ.get("GITHUB_TOKEN", "").strip()
    app = app_for(host)
    if app is None and not pat:
        raise GitHubError(401, "GITHUB_TOKEN is required (or register a GitHub App)")
    return GitHubClient(
        token=pat,
        api_base=api_base_for_host(host),
        transport=transport or _urllib_transport,
        token_provider=_app_provider(app, pat) if app else None,
        host=host,
    )


def repo_token(repo_url: str) -> str:
    """Token for git over https to ``repo_url``: an installation token, else the PAT, else empty."""
    from ts_harness.integrations.github_app import NotInstalled, app_for

    pat = os.environ.get("GITHUB_TOKEN", "").strip()
    parsed = parse_repo_slug(repo_url)
    app = app_for(repo_host(repo_url) or _default_host()) if parsed else None
    if app is None:
        return pat
    try:
        return app.repo_token(*parsed)
    except NotInstalled:
        return pat


def summarize_checks(
    combined: dict[str, Any] | None,
    check_runs: list[dict[str, Any]] | None,
    *,
    required: list[str] | None = None,
) -> dict[str, Any]:
    """Collapse statuses and check runs into ``success``, ``pending`` or ``failure``.

    With ``required`` set, only those names count and a missing one is pending.
    Without it, every check reported on the ref counts (except ``LATER_GATE_CONTEXTS``)
    and an empty set is pending.
    """
    checks: dict[str, str] = {}
    for status in (combined or {}).get("statuses") or []:
        name = str(status.get("context") or "")
        if name:
            checks[name] = str(status.get("state") or "pending").lower()
    for run in check_runs or []:
        name = str(run.get("name") or "")
        if not name:
            continue
        if str(run.get("status") or "").lower() != "completed":
            checks[name] = "pending"
        else:
            conclusion = str(run.get("conclusion") or "").lower()
            checks[name] = "success" if conclusion in SUCCESS_CONCLUSIONS else "failure"

    names = list(required) if required else sorted(n for n in checks if n not in LATER_GATE_CONTEXTS)
    results = {name: checks.get(name, "missing") for name in names}
    failed = sorted(n for n, s in results.items() if s in {"failure", "error"})
    pending = sorted(n for n, s in results.items() if s in PENDING_STATES or s == "missing")
    missing = sorted(n for n, s in results.items() if s == "missing")
    if failed:
        state = "failure"
    elif pending or not results:
        state = "pending"
    else:
        state = "success"
    return {"state": state, "checks": results, "failed": failed, "pending": pending, "missing": missing}
