"""Regenerate a web repository's API types when its backend's ``openapi.json`` changes.

A web repository whose types were generated once and never again drifts from its backend: the
types say one thing, the server sends another, and a fake API built from those types passes
every test. So when a PR merges in an API repository and changes its committed spec, every web
repository whose ``.ts-harness.yml`` names that API (``contract.openapi.repo``) gets a refresh
run: a ``deliver_work_order`` workflow whose work order pulls the new spec, regenerates the
types, fixes what no longer type-checks and opens a PR, reviewed, checked and merged like any
other. A web repository opts out with ``contract.openapi.refresh_on_change: false``.

Each web repository has at most one refresh in flight. A newer spec change cancels the run that
is still going and starts one that updates the open refresh PR instead of opening another.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

KV_NAMESPACE = "api-contract-refresh"
WORKFLOW_TYPE = "deliver_work_order"
WORK_ORDER_ID = "APIREFRESH-1"
TITLE_PREFIX = "Refresh API types from "
ACTOR = "api-contract-refresh"
ACTIVE_STATUSES = frozenset({"running", "blocked", "pending"})


def _norm_path(path: str) -> str:
    return str(path or "").strip().lstrip("./").lstrip("/")


def changed_paths(files: list[dict[str, Any]]) -> set[str]:
    """Paths a PR touched, including the old name of a renamed file."""
    found: set[str] = set()
    for item in files or []:
        for key in ("filename", "previous_filename"):
            if item.get(key):
                found.add(_norm_path(str(item[key])))
    return found


def refresh_title(api_repo: str, merge_sha: str) -> str:
    return f"{TITLE_PREFIX}{api_repo}@{str(merge_sha or '')[:7]}"


def plan_refreshes(
    *,
    api_repo: str,
    merged_into: str,
    api_default_branch: str,
    changed: set[str],
    web_repos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Which web repositories get a refresh run for this merge, and why the others do not.

    ``web_repos`` entries carry ``full_name``, ``base_branch`` and ``contract`` (the parsed
    ``contract.openapi`` block, or None). A merge counts only on the branch the web repository
    reads the spec from (its ``ref``, else the API's default branch).
    """
    decisions: list[dict[str, Any]] = []
    for repo in web_repos:
        name = str(repo.get("full_name") or "")
        contract = repo.get("contract") or None
        if not contract or str(contract.get("repo") or "").lower() != api_repo.lower() or name.lower() == api_repo.lower():
            continue
        decision: dict[str, Any] = {"repo": name, "base_branch": repo.get("base_branch") or "", "path": contract["path"]}
        branch = str(contract.get("ref") or "") or api_default_branch
        if not contract.get("refresh_on_change", True):
            decisions.append({**decision, "action": "skip", "reason": "refresh_on_change is false"})
        elif branch and merged_into and merged_into != branch:
            decisions.append({**decision, "action": "skip", "reason": f"merged into {merged_into}; the spec is read from {branch}"})
        elif _norm_path(contract["path"]) not in changed:
            decisions.append({**decision, "action": "skip", "reason": f"{contract['path']} did not change"})
        else:
            decisions.append({**decision, "action": "start"})
    return decisions


def work_order_body(*, api_repo: str, merge_sha: str, pr_url: str, path: str, branch: str) -> str:
    """The refresh work order the coding agent, code review and QA all read."""
    short = str(merge_sha or "")[:7]
    return f"""# {WORK_ORDER_ID} {refresh_title(api_repo, merge_sha)}
Complexity: Low

## Summary
{api_repo} merged {pr_url or 'a change'} ({short}), which changed its `{path}`. Regenerate this
repository's API types from the new spec and fix the code that no longer matches it.

## Requirements
- Pull the backend's current spec and regenerate the types:
  `OPENAPI_SOURCE="$OPENAPI_CONTRACT" npm run api:pull && npm run api:generate`.
  `OPENAPI_CONTRACT` is {api_repo}'s `{path}` on `{branch}`, fetched by TS Harness for this run.
  Commit the updated `src/api/openapi.json` and `src/api/schema.d.ts`. A repository without
  these scripts copies the spec to its committed copy and regenerates its types the way its
  README or AGENTS.md describes.
- Run `npm run typecheck` and fix every error against the new spec: in the app code, its unit
  tests and the acceptance tests' fake API (`features/support/fixtures.ts`). Change behaviour
  only where the spec forces it (a renamed field, a removed endpoint, a new required value).
- Never edit the generated `src/api/schema.d.ts` by hand and never add hand-written API types.
- `npm run lint`, `npm run typecheck`, `npm test` and `npm run test:bdd` pass.

## In Scope
- `src/api/openapi.json` and `src/api/schema.d.ts`, regenerated.
- Code, tests and the fake API that no longer type-check or no longer match the spec.

## Out of Scope
- New features that use endpoints the spec added.
- Refactoring code the spec change does not affect.

## Acceptance Criteria
- `src/api/openapi.json` is {api_repo}'s `{path}` at {short} or later.
- `src/api/schema.d.ts` is generated from it, unedited.
- Type check, lint, unit tests and acceptance tests pass; no hand-written API types are added.
"""


# I/O: the merge webhook, GitHub and the harness's workflow store.

def _kv_key(repo: str) -> str:
    return repo.lower().replace("/", "__")


def _project_repositories(memory: Any, tenant_id: str) -> list[list[dict[str, Any]]]:
    """Each project's repositories, so a web repository is matched only with an API it shares a project with."""
    projects = (memory.list_projects(tenant_id=tenant_id) or {}).get("projects") or []
    groups = []
    for project in projects:
        try:
            listed = memory.list_project_repositories(str(project["id"])) or {}
        except Exception as exc:  # noqa: BLE001 - one unreadable project does not stop the others
            logger.info("project %s repositories unavailable: %s", project.get("id"), exc)
            continue
        groups.append([r for r in listed.get("repositories") or [] if isinstance(r, dict) and r.get("full_name")])
    return groups


def candidate_repositories(memory: Any, api_repo: str, tenant_id: str) -> list[dict[str, Any]]:
    """The other repositories of every project that has ``api_repo``."""
    wanted = api_repo.lower()
    seen: dict[str, dict[str, Any]] = {}
    for group in _project_repositories(memory, tenant_id):
        if any(str(r["full_name"]).lower() == wanted for r in group):
            for repo in group:
                name = str(repo["full_name"]).lower()
                if name != wanted:
                    seen.setdefault(name, repo)
    return list(seen.values())


def _repo_config(github: Any, full_name: str, branch: str) -> dict[str, Any]:
    import yaml

    from ts_harness.implementation.test_services import CONFIG_NAMES

    owner, _, name = full_name.partition("/")
    for config_name in CONFIG_NAMES:
        payload = github.get_file(owner, name, config_name, ref=branch)
        if isinstance(payload, dict) and payload.get("content"):
            try:
                data = yaml.safe_load(base64.b64decode(str(payload["content"])).decode("utf-8")) or {}
            except Exception:  # noqa: BLE001 - a broken file declares nothing
                return {}
            return data if isinstance(data, dict) else {}
    return {}


def _default_branch(github: Any, full_name: str) -> str:
    owner, _, name = full_name.partition("/")
    return str((github.request("GET", f"/repos/{owner}/{name}") or {}).get("default_branch") or "main")


def open_refresh_pr(github: Any, web_repo: str, api_repo: str) -> dict[str, Any] | None:
    """The web repository's open refresh PR for ``api_repo``, if there is one."""
    owner, _, name = web_repo.partition("/")
    marker = f"{TITLE_PREFIX}{api_repo}@".lower()
    pulls = github.request("GET", f"/repos/{owner}/{name}/pulls", query={"state": "open", "per_page": 100}) or []
    return next((pull for pull in pulls if marker in str(pull.get("title") or "").lower()), None)


def _active(memory: Any, workflow_id: str) -> bool:
    try:
        workflow = (memory.get_workflow(workflow_id) or {}).get("workflow") or {}
    except Exception:  # noqa: BLE001 - a workflow that cannot be read is not in the way
        return False
    return str(workflow.get("status") or "") in ACTIVE_STATUSES


def start_refresh(
    decision: dict[str, Any],
    *,
    api_repo: str,
    merge_sha: str,
    pr_url: str,
    branch: str,
    tenant_id: str,
    memory: Any,
    engine: Any,
    github: Any,
) -> dict[str, Any]:
    """Cancel the web repository's refresh still in flight, then start one for this merge."""
    from ts_harness.integrations.github_client import parse_github_pr_url
    from ts_harness.workflows.hooks.fork_subworkflow import fork_child

    web_repo = decision["repo"]
    key = _kv_key(web_repo)
    previous = (memory.kv_get(KV_NAMESPACE, key) or {}).get("value") or {}
    old = str(previous.get("workflow_id") or "")
    superseded = ""
    if old and _active(memory, old):
        engine.cancel(old, actor=ACTOR, tenant_id=tenant_id, reason=f"superseded by {refresh_title(api_repo, merge_sha)}")
        superseded = old
    existing = str(decision.get("pr_url") or "")
    target = parse_github_pr_url(existing)
    if target is not None:
        # The run updates this PR; its title names the spec it now carries.
        owner, name, number = target
        title = f"{WORK_ORDER_ID}: {refresh_title(api_repo, merge_sha)}"
        try:
            github.request("PATCH", f"/repos/{owner}/{name}/pulls/{number}", body={"title": title})
        except Exception as exc:  # noqa: BLE001 - the run still updates the PR
            logger.info("refresh PR title not updated for %s: %s", existing, exc)
    intake = {
        "work_order_id": WORK_ORDER_ID,
        "work_order_ref": WORK_ORDER_ID,
        "work_order_body": work_order_body(
            api_repo=api_repo, merge_sha=merge_sha, pr_url=pr_url, path=decision["path"], branch=branch
        ),
        "repo_url": f"https://github.com/{web_repo}",
        "base_branch": decision.get("base_branch") or "",
        "summary": refresh_title(api_repo, merge_sha),
    }
    if existing:
        intake["pr_url"] = existing
    workflow_id = fork_child(
        memory, parent_workflow_id="", tenant_id=tenant_id, child_type=WORKFLOW_TYPE, intake_input=intake, actor=ACTOR
    )
    memory.kv_put(
        KV_NAMESPACE,
        key,
        {"workflow_id": workflow_id, "api_repo": api_repo, "merge_sha": merge_sha, "started_at": int(time.time())},
    )
    return {"repo": web_repo, "action": "started", "workflow_id": workflow_id, "updates_pr": existing, "superseded": superseded}


def on_merge(
    event: dict[str, Any],
    *,
    tenant_id: str,
    memory: Any = None,
    github: Any = None,
    engine: Any = None,
) -> dict[str, Any]:
    """Webhook subscriber: start a refresh run on each web repository that reads this repository's spec."""
    from ts_harness.integrations.github_client import parse_github_pr_url

    pr_url = str(event.get("pr_url") or "")
    parsed = parse_github_pr_url(pr_url)
    if parsed is None:
        return {"status": "ignored", "reason": "not a GitHub pull request"}
    api_repo = str(event.get("repo") or f"{parsed[0]}/{parsed[1]}")
    if memory is None:
        from ts_harness.memory.client import get_memory_client

        memory = get_memory_client()
    candidates = candidate_repositories(memory, api_repo, tenant_id)
    if not candidates:
        return {"status": "ignored", "reason": f"no project repository shares a project with {api_repo}"}
    if github is None:
        from ts_harness.integrations.github_client import github_for

        github = github_for(pr_url)
    web_repos = []
    for repo in candidates:
        name = str(repo["full_name"])
        try:
            branch = str(repo.get("default_branch") or "") or _default_branch(github, name)
            web_repos.append({"full_name": name, "base_branch": branch, "contract": _contract(github, name, branch)})
        except Exception as exc:  # noqa: BLE001 - one unreadable repository does not stop the others
            logger.info("contract config unreadable for %s: %s", name, exc)
    if not any(r["contract"] and r["contract"]["repo"].lower() == api_repo.lower() for r in web_repos):
        return {"status": "ignored", "reason": f"no web repository reads {api_repo}'s spec"}
    changed = changed_paths(github.list_pull_files(*parsed))
    api_default = _default_branch(github, api_repo)
    merged_into = str(event.get("base_branch") or "")
    decisions = plan_refreshes(
        api_repo=api_repo, merged_into=merged_into, api_default_branch=api_default, changed=changed, web_repos=web_repos
    )
    if engine is None:
        from ts_harness.workflows.engine import get_workflow_engine

        engine = get_workflow_engine()
    results = []
    for decision in decisions:
        if decision["action"] != "start":
            results.append(decision)
            continue
        try:
            pull = open_refresh_pr(github, decision["repo"], api_repo)
            decision["pr_url"] = str((pull or {}).get("html_url") or "")
            results.append(
                start_refresh(
                    decision,
                    api_repo=api_repo,
                    merge_sha=str(event.get("merge_sha") or ""),
                    pr_url=pr_url,
                    branch=merged_into or api_default,
                    tenant_id=tenant_id,
                    memory=memory,
                    engine=engine,
                    github=github,
                )
            )
        except Exception as exc:  # noqa: BLE001 - the merge is recorded whatever happens here
            logger.warning("API refresh not started for %s: %s", decision["repo"], exc)
            results.append({**decision, "action": "failed", "error": str(exc)})
    started = [r for r in results if r.get("action") == "started"]
    return {"status": "started" if started else "skipped", "api_repo": api_repo, "refreshes": results}


def _contract(github: Any, full_name: str, branch: str) -> dict[str, Any] | None:
    from ts_harness.implementation.api_contract import contract_block

    return contract_block(_repo_config(github, full_name, branch))
