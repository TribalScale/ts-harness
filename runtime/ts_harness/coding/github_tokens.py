"""Installation tokens for coding workers, limited to the job's repository.

The worker presents its job token; the memory API (which holds the signing
secret) confirms it and returns the job's repository, and the runtime mints a
GitHub App token for that repository only.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from ts_harness.integrations.github_client import parse_repo_slug, repo_host


class JobTokenRejected(PermissionError):
    pass


class NoAppForRepository(LookupError):
    pass


def fetch_job_identity(job_id: str, job_token: str, *, base_url: str | None = None) -> dict[str, Any]:
    base = (base_url or os.environ.get("MEMORY_API_URL") or "http://127.0.0.1:18792").rstrip("/")
    request = urllib.request.Request(
        f"{base}/coding-jobs/{job_id}/identity",
        headers={"Authorization": f"Bearer {job_token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403, 404):
            raise JobTokenRejected("job token rejected or job not running") from exc
        raise


def mint_job_github_token(
    job_id: str,
    job_token: str,
    *,
    identity: Callable[[str, str], dict[str, Any]] = fetch_job_identity,
    app_for: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    from ts_harness.integrations.github_app import DEFAULT_HOST, NotInstalled
    from ts_harness.integrations.github_app import app_for as default_app_for

    if not job_token:
        raise JobTokenRejected("job token required")
    job = identity(job_id, job_token)
    repo_url = str(job.get("repo_url") or "")
    slug = parse_repo_slug(repo_url)
    if slug is None:
        raise NoAppForRepository("job repository is not a GitHub repository")
    host = repo_host(repo_url) or DEFAULT_HOST
    app = (app_for or default_app_for)(host)
    if app is None:
        raise NoAppForRepository(f"no GitHub App registered for {host}")
    try:
        token, expires = app.repo_token_with_expiry(*slug)
    except NotInstalled as exc:
        raise NoAppForRepository(str(exc)) from exc
    return {
        "token": token,
        "expires_at": datetime.fromtimestamp(expires, tz=timezone.utc).isoformat(),
        "repository": f"{slug[0]}/{slug[1]}",
    }
