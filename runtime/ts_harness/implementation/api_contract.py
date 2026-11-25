"""The backend's current OpenAPI spec, for a web repository whose tests check their fake API.

A web repository started from the ``react-web`` template checks every request its acceptance
tests make to the fake API against ``OPENAPI_CONTRACT`` (else its committed copy). A committed
copy goes stale the moment the backend changes, so the harness fetches the backend's spec from
its base branch and points ``OPENAPI_CONTRACT`` at it. The web repository names its backend in
``.ts-harness.yml``::

    contract:
      openapi:
        repo: org/api          # or a GitHub URL
        path: openapi.json     # default
        ref: dev               # default: the backend's default branch
        refresh_on_change: true  # default; see ts_harness.workflows.contract_refresh
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def contract_block(config: dict[str, Any]) -> dict[str, Any] | None:
    """The ``contract.openapi`` block of a parsed ``.ts-harness.yml``, normalized; None when absent."""
    contract = config.get("contract") if isinstance(config, dict) else None
    block = contract.get("openapi") if isinstance(contract, dict) else None
    if isinstance(block, str):
        block = {"repo": block}
    if not isinstance(block, dict) or not str(block.get("repo") or "").strip():
        return None
    repo = str(block["repo"]).strip().removesuffix(".git")
    if repo.startswith("https://github.com/"):
        repo = repo.removeprefix("https://github.com/")
    return {
        "repo": repo,
        "path": str(block.get("path") or "openapi.json"),
        "ref": str(block.get("ref") or ""),
        "refresh_on_change": block.get("refresh_on_change") is not False,
    }


def contract_source(repo_dir: Path) -> dict[str, str] | None:
    """The declared backend spec (``repo``, ``path``, ``ref``), or None."""
    from ts_harness.implementation.test_services import load_repo_config

    block = contract_block(load_repo_config(repo_dir))
    return None if block is None else {key: block[key] for key in ("repo", "path", "ref")}


def fetch_contract(repo_dir: Path, state_dir: Path, *, github: Any = None) -> tuple[dict[str, str], str]:
    """``({"OPENAPI_CONTRACT": path}, note)``; empty env when nothing is declared or it cannot be read."""
    source = contract_source(repo_dir)
    if source is None:
        return {}, ""
    owner, _, name = source["repo"].partition("/")
    if not owner or not name:
        return {}, f"contract: {source['repo']} is not owner/name"
    try:
        if github is None:
            from ts_harness.integrations.github_client import github_for

            github = github_for(f"https://github.com/{owner}/{name}")
        ref = source["ref"] or str((github.request("GET", f"/repos/{owner}/{name}") or {}).get("default_branch") or "main")
        payload = github.get_file(owner, name, source["path"], ref=ref)
        if not isinstance(payload, dict) or "content" not in payload:
            return {}, f"contract: {source['repo']}:{source['path']} not found at {ref}; the committed copy is used"
        text = base64.b64decode(str(payload["content"])).decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - the committed copy still checks the fake
        logger.info("contract fetch failed for %s: %s", source["repo"], exc)
        return {}, f"contract: could not read {source['repo']}:{source['path']} ({exc}); the committed copy is used"
    target = Path(state_dir) / "contract-openapi.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return {"OPENAPI_CONTRACT": str(target)}, f"contract: the fake API is checked against {source['repo']}:{source['path']}@{ref}"
