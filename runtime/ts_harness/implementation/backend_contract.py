"""The backend's openapi.json for a web repository's coding job, read by the harness, not the job.

A coding worker's GitHub token covers only the repository it works on, so a web job could not
read its backend's spec: a job asked to regenerate the API types from it ran for hours and
opened a pull request without them. The workflow runner has the harness's own access, so it
reads the spec when it builds the job and puts the text in the spec (``api_contract``); the
worker writes it to a file, points ``OPENAPI_CONTRACT`` and ``OPENAPI_SOURCE`` at it and tells
OpenCode where it is.

The backend is the one the web repository names in ``.ts-harness.yml`` (``contract.openapi``),
else the one API repository in the program's project.
"""

from __future__ import annotations

import base64
import logging
from typing import Any

logger = logging.getLogger(__name__)

API_ROLES = {"api", "backend", "server", "service"}
API_TEMPLATES = {"node-api"}
MAX_SPEC_BYTES = 3 * 1024 * 1024
CONFIG_NAMES = (".ts-harness.yml", ".ts-harness.yaml")


def _slug(repo_url: str) -> tuple[str, str] | None:
    from ts_harness.integrations.github_client import parse_repo_slug

    return parse_repo_slug(str(repo_url or ""))


def _decoded(payload: Any) -> str:
    if not isinstance(payload, dict) or "content" not in payload:
        return ""
    try:
        return base64.b64decode(str(payload["content"])).decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""


def declared_source(github: Any, owner: str, name: str, ref: str) -> dict[str, str] | None:
    """``contract.openapi`` from the web repository's ``.ts-harness.yml`` on ``ref``, or None."""
    import yaml

    for config_name in CONFIG_NAMES:
        try:
            text = _decoded(github.get_file(owner, name, config_name, ref=ref))
        except Exception:  # noqa: BLE001 - no file is the same as no declaration
            text = ""
        if not text:
            continue
        try:
            data = yaml.safe_load(text) or {}
        except Exception:  # noqa: BLE001
            return None
        block = ((data.get("contract") or {}) if isinstance(data, dict) else {}).get("openapi")
        if isinstance(block, str):
            block = {"repo": block}
        if isinstance(block, dict) and str(block.get("repo") or "").strip():
            repo = str(block["repo"]).strip().removesuffix(".git").removeprefix("https://github.com/")
            return {"repo": repo, "path": str(block.get("path") or "openapi.json"), "ref": str(block.get("ref") or "")}
        return None
    return None


def project_source(repositories: list[dict[str, Any]], web_full_name: str) -> dict[str, str] | None:
    """The one API repository in the project besides the web repository, or None (none or several)."""
    web = web_full_name.lower()
    apis = [
        repo
        for repo in repositories
        if isinstance(repo, dict)
        and str(repo.get("full_name") or "").lower() not in {"", web}
        and (str(repo.get("role") or "").lower() in API_ROLES or str(repo.get("template") or "").lower() in API_TEMPLATES)
    ]
    if len(apis) != 1:
        return None
    return {"repo": str(apis[0]["full_name"]), "path": "openapi.json", "ref": str(apis[0].get("default_branch") or "")}


def _program_repositories(program_id: str | None, memory: Any) -> list[dict[str, Any]]:
    if not program_id:
        return []
    try:
        program = (memory.get_program(program_id) or {}).get("program") or {}
    except Exception:  # noqa: BLE001
        return []
    metadata = program.get("metadata") if isinstance(program.get("metadata"), dict) else {}
    artifacts = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), dict) else {}
    return [r for r in artifacts.get("project_repositories") or [] if isinstance(r, dict)]


def backend_contract(
    repo_url: str,
    base_branch: str,
    *,
    program_id: str | None = None,
    memory: Any = None,
    github_factory: Any = None,
) -> dict[str, str] | None:
    """``{"repo", "ref", "path", "text"}`` of the backend spec for a job on ``repo_url``, or None.

    Never raises: a job without the spec still runs (the worker falls back to fetching it).
    """
    slug = _slug(repo_url)
    if slug is None:
        return None
    owner, name = slug
    if github_factory is None:
        from ts_harness.integrations.github_client import github_for

        github_factory = github_for
    try:
        web_github = github_factory(f"https://github.com/{owner}/{name}")
        source = declared_source(web_github, owner, name, base_branch or "")
        if source is None:
            if memory is None:
                from ts_harness.memory.client import get_memory_client

                memory = get_memory_client()
            source = project_source(_program_repositories(program_id, memory), f"{owner}/{name}")
        if source is None:
            return None
        api_owner, _, api_name = source["repo"].partition("/")
        api_github = github_factory(f"https://github.com/{api_owner}/{api_name}")
        ref = source["ref"] or str((api_github.request("GET", f"/repos/{api_owner}/{api_name}") or {}).get("default_branch") or "main")
        text = _decoded(api_github.get_file(api_owner, api_name, source["path"], ref=ref))
    except Exception as exc:  # noqa: BLE001 - the job runs without it
        logger.info("backend contract for %s/%s not read: %s", owner, name, exc)
        return None
    if not text or len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        return None
    return {"repo": source["repo"], "ref": ref, "path": source["path"], "text": text}


def contract_note(contract: dict[str, Any], file_path: str) -> str:
    """What OpenCode is told about the spec the harness supplied."""
    return (
        f"The backend's API spec ({contract.get('repo')}:{contract.get('path')} at {contract.get('ref')}) is at "
        f"{file_path} (also in $OPENAPI_CONTRACT and $OPENAPI_SOURCE). You cannot read that repository yourself: "
        "copy this file wherever the work order needs it (for example `OPENAPI_SOURCE=$OPENAPI_CONTRACT npm run "
        "api:pull`, or copy it to the repo's committed spec path) and generate types from it."
    )
