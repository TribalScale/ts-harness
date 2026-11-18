"""Start empty project repositories from a harness template (``templates/<name>``).

A repository that holds nothing but a README, ``.gitignore`` or LICENSE has no structure, CI or
tests for coding jobs to follow. The first time a ticket queue or a ticket targets one, the
harness opens a **bootstrap PR** into its base branch with the template for the repository's
role (``api`` → ``node-api``, ``web`` → ``react-web``): a straight copy of a tested repository,
not agent-written code. Feature work waits until a person merges it. Once merged, the template's
CI jobs become required checks on the base branch.

Settings (``program_defaults.repo_templates``)::

    {"enabled": true, "by_role": {"api": "node-api", "web": "react-web", "worker": "none"}}

``by_role`` adds to or overrides the roles each template's ``template.json`` lists; ``"none"``
turns templates off for a role. See docs/runbooks/repo-templates.md.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BRANCH_PREFIX = "ts-harness/bootstrap"
MARKER_FILE = ".ts-harness.yml"
# What an "empty" repository may already hold: the files GitHub offers when creating one.
_STARTER_FILES = ("README*", "readme*", "LICENSE*", "LICENCE*", "license*", ".gitignore", ".gitattributes", ".editorconfig", ".github/CODEOWNERS", "CODEOWNERS")
_TEMPLATE_LINE = re.compile(r"^template:\s*([\w.-]+)\s*$", re.MULTILINE)


class BootstrapError(Exception):
    """A template could not be applied; the message says what to do."""


@dataclass(frozen=True)
class Template:
    name: str
    version: str
    description: str
    path: Path
    roles: tuple[str, ...] = ()
    required_checks: tuple[str, ...] = ()
    substitutions: tuple[dict[str, Any], ...] = ()

    def conventions(self) -> str:
        """The template's AGENTS.md: what planning and coding follow in a repository started from it."""
        agents = self.path / "AGENTS.md"
        return agents.read_text(encoding="utf-8") if agents.is_file() else ""


@dataclass
class Readiness:
    """Whether a repository can take feature work, and the bootstrap PR when it cannot yet."""

    repo: str
    ready: bool
    template: str | None = None
    pr_url: str | None = None
    opened: bool = False
    notes: list[str] = field(default_factory=list)

    def message(self) -> str:
        verb = "Opened" if self.opened else "Waiting on"
        return f"{self.repo} is empty, so it starts from the `{self.template}` template. {verb} the bootstrap PR: {self.pr_url}"


# Catalog

def templates_dir() -> Path | None:
    """``TS_HARNESS_TEMPLATES_DIR``, else ``/app/templates`` (images), else the repo checkout."""
    candidates = [os.environ.get("TS_HARNESS_TEMPLATES_DIR", ""), "/app/templates", str(Path(__file__).resolve().parents[3] / "templates")]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    return None


def load_templates(root: Path | None = None) -> dict[str, Template]:
    root = root or templates_dir()
    found: dict[str, Template] = {}
    if root is None:
        return found
    for manifest in sorted(root.glob("*/template.json")):
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("template manifest %s unreadable: %s", manifest, exc)
            continue
        name = str(data.get("name") or manifest.parent.name)
        found[name] = Template(
            name=name,
            version=str(data.get("version") or "0"),
            description=str(data.get("description") or ""),
            path=manifest.parent,
            roles=tuple(str(r).lower() for r in data.get("roles") or []),
            required_checks=tuple(str(c) for c in data.get("required_checks") or []),
            substitutions=tuple(s for s in data.get("substitutions") or [] if isinstance(s, dict)),
        )
    return found


def settings(program_defaults: dict[str, Any] | None) -> dict[str, Any]:
    raw = (program_defaults or {}).get("repo_templates")
    raw = raw if isinstance(raw, dict) else {}
    by_role = raw.get("by_role") if isinstance(raw.get("by_role"), dict) else {}
    return {"enabled": raw.get("enabled", True) is not False, "by_role": {str(k).lower(): str(v) for k, v in by_role.items()}}


def live_settings() -> dict[str, Any]:
    try:
        from ts_harness.workflows.delivery_program import _live_program_defaults

        return settings(_live_program_defaults())
    except Exception as exc:  # noqa: BLE001 - defaults when settings can't be read
        logger.info("repo template settings unavailable: %s", exc)
        return settings(None)


def template_for_role(role: str | None, templates: dict[str, Template], config: dict[str, Any]) -> Template | None:
    """The template a repository with ``role`` starts from, or None (no role, no match, or off)."""
    if not config.get("enabled", True):
        return None
    key = str(role or "").strip().lower()
    if not key:
        return None
    chosen = config.get("by_role", {}).get(key)
    if chosen is not None:
        return None if chosen.lower() in {"none", "off", ""} else templates.get(chosen)
    return next((t for t in templates.values() if key in t.roles), None)


# Rendering

def display_name(repo_name: str) -> str:
    """``Acme-API-Test`` → ``Acme API Test``; ``order_service`` → ``Order Service``."""
    words = re.split(r"[-_\s]+", repo_name.strip())
    return " ".join(w if w.isupper() else w[:1].upper() + w[1:] for w in words if w)


def package_name(repo_name: str) -> str:
    """A valid npm package name from the repository name."""
    name = re.sub(r"[^a-z0-9._-]+", "-", repo_name.lower()).strip("-._")
    return name or "app"


def render(template: Template, *, repo_name: str, title: str | None = None) -> dict[str, str]:
    """``{path: text}`` for every template file, with substitutions applied and the marker set."""
    values = {"package_name": package_name(repo_name), "display_name": title or display_name(repo_name)}
    files: dict[str, str] = {}
    for path in sorted(p for p in template.path.rglob("*") if p.is_file()):
        relative = path.relative_to(template.path).as_posix()
        if relative == "template.json" or "node_modules/" in relative or relative.startswith(".git/"):
            continue
        try:
            files[relative] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise BootstrapError(f"template {template.name} has a binary file {relative}; templates must be text") from exc
    for rule in template.substitutions:
        target = str(rule.get("to") or "").format(**values)
        for name in rule.get("files") or []:
            if name in files:
                files[name] = files[name].replace(str(rule.get("from") or ""), target)
    marker = files.get(MARKER_FILE, "")
    files[MARKER_FILE] = (
        f"# Started from the TS Harness `{template.name}` template (version {template.version}).\n"
        f"template: {template.name}\n" + _TEMPLATE_LINE.sub("", marker).lstrip("\n")
    )
    return files


# Repository state

def is_starter_only(paths: list[str]) -> bool:
    """True when every file is one GitHub creates with a new repository (or there are none)."""
    return all(any(fnmatch.fnmatch(path, pattern) for pattern in _STARTER_FILES) for path in paths)


def repo_files(github: Any, owner: str, repo: str, branch: str) -> list[str] | None:
    """File paths on ``branch``; None when the branch does not exist."""
    from ts_harness.integrations.github_client import GitHubError

    try:
        tree = github.get_tree(owner, repo, branch) or {}
    except GitHubError as exc:
        if exc.status in (404, 409):  # no such branch, or a repository with no commits
            return None
        raise
    return [str(item.get("path")) for item in tree.get("tree") or [] if item.get("type") == "blob"]


def started_from(github: Any, owner: str, repo: str, branch: str) -> str | None:
    """The template named in the repository's .ts-harness.yml, if any."""
    payload = github.get_file(owner, repo, MARKER_FILE, ref=branch) if hasattr(github, "get_file") else None
    if not isinstance(payload, dict) or not payload.get("content"):
        return None
    text = base64.b64decode(str(payload["content"])).decode(errors="replace")
    match = _TEMPLATE_LINE.search(text)
    return match.group(1) if match else None


# Bootstrap PR

def _branch(template: Template) -> str:
    return f"{BRANCH_PREFIX}-{template.name}"


def open_bootstrap_pr(github: Any, owner: str, repo: str, base: str, template: Template, files: dict[str, str]) -> tuple[str, bool]:
    """``(pr_url, opened)``: the open bootstrap PR, or a new one with the template as one commit."""
    branch = _branch(template)
    existing = github.request("GET", f"/repos/{owner}/{repo}/pulls", query={"state": "open", "head": f"{owner}:{branch}", "base": base}) or []
    if existing:
        return str(existing[0].get("html_url")), False

    head = github.request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{base}")
    base_sha = str(head["object"]["sha"])
    base_tree = str(github.request("GET", f"/repos/{owner}/{repo}/git/commits/{base_sha}")["tree"]["sha"])
    tree = github.request(
        "POST",
        f"/repos/{owner}/{repo}/git/trees",
        body={
            "base_tree": base_tree,
            "tree": [{"path": path, "mode": "100644", "type": "blob", "content": text} for path, text in sorted(files.items())],
        },
    )
    commit = github.request(
        "POST",
        f"/repos/{owner}/{repo}/git/commits",
        body={
            "message": f"Start from the TS Harness {template.name} template ({template.version})",
            "tree": tree["sha"],
            "parents": [base_sha],
        },
    )
    ref = github.request("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}", allow_404=True)
    if ref:
        github.request("PATCH", f"/repos/{owner}/{repo}/git/refs/heads/{branch}", body={"sha": commit["sha"], "force": True})
    else:
        github.request("POST", f"/repos/{owner}/{repo}/git/refs", body={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    pr = github.request(
        "POST",
        f"/repos/{owner}/{repo}/pulls",
        body={"title": f"Start from the {template.name} template", "head": branch, "base": base, "body": _pr_body(template, files)},
    )
    return str(pr.get("html_url")), True


def _pr_body(template: Template, files: dict[str, str]) -> str:
    checks = ", ".join(f"`{c}`" for c in template.required_checks) or "its CI jobs"
    return (
        "## Summary\n\n"
        f"This repository was empty, so TS Harness starts it from its **`{template.name}`** template "
        f"(version {template.version}): {template.description}.\n\n"
        "It is a copy of a tested repository, not generated code: structure, conventions for coding "
        "agents (`AGENTS.md`), unit and BDD tests with a reference example, CI and a Docker image.\n\n"
        "## Before merging\n\n"
        f"- CI runs on this PR ({checks}); it should pass as is.\n"
        "- Read `AGENTS.md`: every work order in this repository follows it.\n"
        "- Feature work for this repository waits until this PR is merged. Then run the ticket "
        "or queue again.\n\n"
        f"Once merged, TS Harness makes {checks} required checks on the base branch.\n\n"
        f"{len(files)} files.\n"
    )


def require_template_checks(github: Any, owner: str, repo: str, branch: str) -> list[str]:
    """Add the CI jobs that run on PRs into ``branch`` to its required checks; the names added."""
    from ts_harness.workflows.repo_governance import check_repo, protection_body

    report = check_repo(github, owner, repo, branch)
    missing = [name for name in report.ci_checks if name not in report.required_now]
    if not missing:
        return []
    current = github.branch_protection(owner, repo, branch)
    github.set_branch_protection(owner, repo, branch, protection_body(report, current))
    return missing


# Entry point

def ensure_ready(
    github: Any,
    repo_url: str,
    base_branch: str,
    *,
    role: str | None,
    title: str | None = None,
    templates: dict[str, Template] | None = None,
    config: dict[str, Any] | None = None,
) -> Readiness:
    """Bootstrap ``repo_url`` when it is empty and its role has a template; otherwise ready.

    Ready repositories that were started from a template get its CI jobs as required checks
    the first time they are seen after the bootstrap PR merged.
    """
    from ts_harness.integrations.github_client import parse_repo_slug

    slug = parse_repo_slug(repo_url.removeprefix("https://github.com/").removesuffix(".git"))
    if slug is None:
        return Readiness(repo=repo_url, ready=True)
    owner, repo = slug
    name = f"{owner}/{repo}"
    templates = load_templates() if templates is None else templates
    config = live_settings() if config is None else config
    template = template_for_role(role, templates, config)

    files = repo_files(github, owner, repo, base_branch)
    if files is None:
        raise BootstrapError(
            f"{name} has no `{base_branch}` branch. Create it (from a first commit, such as a README), then run this again."
        )
    if template is None or not is_starter_only(files):
        result = Readiness(repo=name, ready=True)
        if files and not is_starter_only(files) and started_from(github, owner, repo, base_branch):
            try:
                added = require_template_checks(github, owner, repo, base_branch)
                if added:
                    result.notes.append(f"{name}: made {', '.join(added)} required checks on `{base_branch}`")
            except Exception as exc:  # noqa: BLE001 - protection is advisory here; repo-governance.py reports it
                result.notes.append(f"{name}: could not require the template's CI checks ({exc})")
        return result

    url, opened = open_bootstrap_pr(github, owner, repo, base_branch, template, render(template, repo_name=repo, title=title))
    return Readiness(repo=name, ready=False, template=template.name, pr_url=url, opened=opened)


def repositories_ready(github: Any, repositories: list[dict[str, Any]], *, title: str | None = None) -> list[Readiness]:
    """``ensure_ready`` for each project repository (``full_name``/``repo_url``, ``role``, ``default_branch``)."""
    templates, config = load_templates(), live_settings()
    results = []
    for item in repositories:
        url = str(item.get("repo_url") or f"https://github.com/{item.get('full_name')}")
        branch = str(item.get("default_branch") or "dev")
        results.append(ensure_ready(github, url, branch, role=item.get("role"), title=title, templates=templates, config=config))
    return results


def annotate_repositories(github: Any, repositories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project repositories with ``template`` and ``template_conventions`` on the empty ones.

    Planning reads these: a repository that will start from a template is planned on top of it
    (its folders, tests and conventions), with no scaffolding work orders of its own.
    """
    templates, config = load_templates(), live_settings()
    out = []
    for item in repositories:
        entry = dict(item)
        template = template_for_role(item.get("role"), templates, config)
        if template is not None:
            try:
                owner, _, repo = str(item.get("full_name") or "").partition("/")
                files = repo_files(github, owner, repo, str(item.get("default_branch") or "dev"))
                if files is not None and is_starter_only(files):
                    entry["template"] = template.name
                    entry["template_conventions"] = template.conventions()
            except Exception as exc:  # noqa: BLE001 - planning goes on without the annotation
                logger.info("template check for %s failed: %s", item.get("full_name"), exc)
        out.append(entry)
    return out
