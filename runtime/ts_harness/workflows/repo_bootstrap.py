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
_VERSION_LINE = re.compile(r"^template_version:\s*([\w.+-]+)\s*$", re.MULTILINE)
# Set by the CI safety net's work order: once its CI merges, require the jobs.
_REQUIRE_CI_LINE = re.compile(r"^require_ci_checks:\s*true\s*$", re.MULTILINE | re.IGNORECASE)


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
    refreshed: bool = False
    notes: list[str] = field(default_factory=list)

    def message(self) -> str:
        verb = "Opened" if self.opened else "Updated" if self.refreshed else "Waiting on"
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


# Build output and dependencies a local run of a template's checks leaves behind.
_SKIP_DIRS = frozenset({".git", "node_modules", "dist", "coverage", "test-results", "playwright-report", ".features-gen", ".turbo"})


def _template_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        found.extend(Path(directory) / name for name in sorted(names) if name not in {".DS_Store", "cucumber-report.html"})
    return sorted(found)


def render(template: Template, *, repo_name: str, title: str | None = None, contract_repo: str | None = None) -> dict[str, str]:
    """``{path: text}`` for every template file, with substitutions applied and the marker set."""
    values = {"package_name": package_name(repo_name), "display_name": title or display_name(repo_name)}
    files: dict[str, str] = {}
    for path in _template_files(template.path):
        relative = path.relative_to(template.path).as_posix()
        if relative == "template.json":
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
    marker = _VERSION_LINE.sub("", _TEMPLATE_LINE.sub("", files.get(MARKER_FILE, "")))
    files[MARKER_FILE] = (
        f"# Started from the TS Harness `{template.name}` template.\n"
        f"template: {template.name}\ntemplate_version: {template.version}\n" + marker.lstrip("\n")
    )
    if contract_repo and "features/support/contract.ts" in files and "contract:" not in files[MARKER_FILE]:
        # The acceptance tests check the fake API against the backend's current openapi.json, and a
        # change to that spec starts a run that regenerates this repository's API types.
        files[MARKER_FILE] += (
            f"contract:\n  openapi:\n    repo: {contract_repo}\n    path: openapi.json\n    refresh_on_change: true\n"
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


def _marker(github: Any, owner: str, repo: str, branch: str) -> str:
    payload = github.get_file(owner, repo, MARKER_FILE, ref=branch) if hasattr(github, "get_file") else None
    if not isinstance(payload, dict) or not payload.get("content"):
        return ""
    return base64.b64decode(str(payload["content"])).decode(errors="replace")


def wants_ci_required(github: Any, owner: str, repo: str, branch: str) -> bool:
    """True for a repository started from a template, or whose CI the harness added."""
    text = _marker(github, owner, repo, branch)
    return bool(_TEMPLATE_LINE.search(text) or _REQUIRE_CI_LINE.search(text))


def started_from(github: Any, owner: str, repo: str, branch: str) -> str | None:
    """The template named in the repository's .ts-harness.yml, if any."""
    match = _TEMPLATE_LINE.search(_marker(github, owner, repo, branch))
    return match.group(1) if match else None


# Bootstrap PR

def _branch(template: Template) -> str:
    return f"{BRANCH_PREFIX}-{template.name}"


def open_bootstrap_pr(
    github: Any, owner: str, repo: str, base: str, template: Template, files: dict[str, str]
) -> tuple[str, bool, bool]:
    """``(pr_url, opened, refreshed)``: a new PR with the template as one commit, or the open one.

    An open PR built from an older template version gets its branch rebuilt from the current
    one (a fix to the template reaches PRs nobody has merged yet).
    """
    branch = _branch(template)
    existing = github.request("GET", f"/repos/{owner}/{repo}/pulls", query={"state": "open", "head": f"{owner}:{branch}", "base": base}) or []
    if existing:
        found = _VERSION_LINE.search(_marker(github, owner, repo, branch))
        if found and found.group(1) == template.version:
            return str(existing[0].get("html_url")), False, False
        _push_template_commit(github, owner, repo, base, branch, template, files)
        return str(existing[0].get("html_url")), False, True

    _push_template_commit(github, owner, repo, base, branch, template, files)
    pr = github.request(
        "POST",
        f"/repos/{owner}/{repo}/pulls",
        body={"title": f"Start from the {template.name} template", "head": branch, "base": base, "body": _pr_body(template, files)},
    )
    return str(pr.get("html_url")), True, False


def _push_template_commit(github: Any, owner: str, repo: str, base: str, branch: str, template: Template, files: dict[str, str]) -> None:
    """Point ``branch`` at one commit on top of ``base`` holding the template."""
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
    contract_repo: str | None = None,
) -> Readiness:
    """Bootstrap ``repo_url`` when it is empty and its role has a template; otherwise ready.

    Ready repositories that were started from a template, or whose CI an "add CI" work order
    added (``require_ci_checks: true`` in .ts-harness.yml), get their CI jobs as required checks
    the first time they are seen after that merged.
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
        if files and not is_starter_only(files) and wants_ci_required(github, owner, repo, base_branch):
            try:
                added = require_template_checks(github, owner, repo, base_branch)
                if added:
                    result.notes.append(f"{name}: made {', '.join(added)} required checks on `{base_branch}`")
            except Exception as exc:  # noqa: BLE001 - protection is advisory here; repo-governance.py reports it
                result.notes.append(f"{name}: could not require the template's CI checks ({exc})")
        return result

    rendered = render(template, repo_name=repo, title=title, contract_repo=contract_repo)
    url, opened, refreshed = open_bootstrap_pr(github, owner, repo, base_branch, template, rendered)
    return Readiness(repo=name, ready=False, template=template.name, pr_url=url, opened=opened, refreshed=refreshed)


def repositories_ready(github: Any, repositories: list[dict[str, Any]], *, title: str | None = None) -> list[Readiness]:
    """``ensure_ready`` for each project repository (``full_name``/``repo_url``, ``role``, ``default_branch``)."""
    templates, config = load_templates(), live_settings()
    # The project's backend (the repository the node-api template is for): web repositories
    # started from a template check their fake API against its openapi.json.
    backend = next(
        (str(item.get("full_name") or str(item.get("repo_url") or "").removeprefix("https://github.com/").removesuffix(".git"))
         for item in repositories
         if getattr(template_for_role(item.get("role"), templates, config), "name", "") == "node-api"),
        None,
    )
    results = []
    for item in repositories:
        url = str(item.get("repo_url") or f"https://github.com/{item.get('full_name')}")
        branch = str(item.get("default_branch") or "dev")
        results.append(ensure_ready(github, url, branch, role=item.get("role"), title=title, templates=templates,
                                    config=config, contract_repo=backend))
    return results


def annotate_repositories(
    github: Any, repositories: list[dict[str, Any]], *, previous: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Project repositories with what planning and delivery need to know about each.

    - ``template`` / ``template_status``: the template the repository starts from, ``pending``
      while it is empty (the bootstrap PR comes when work starts) or ``applied`` once merged
      (its .ts-harness.yml names it). Either way planning builds on it, with no scaffolding
      work orders.
    - ``conventions``: the rules work in the repository follows, for planning: its own
      ``AGENTS.md`` when it has one (a template-started repository's may have grown since),
      else the template's for an empty repository that will start from one.
    - ``ci_on_pull_requests``: False for a repository with code but no CI on pull requests into
      its base branch; planning adds an "add CI" work order first (the CI safety net).
    - ``bdd`` / ``bdd_reason`` / ``has_gherkin_runner``: whether its work uses Gherkin, from the
      project's ``bdd_mode``. Decided once: a decision in ``previous`` (the program's artifacts)
      is kept, so it no longer depends on what the first ticket set up.
    """
    from ts_harness.workflows import bdd_policy

    templates, config = load_templates(), live_settings()
    earlier = {str(r.get("full_name") or "").lower(): r for r in previous or [] if isinstance(r, dict)}
    out = []
    for item in repositories:
        entry = dict(item)
        name = str(item.get("full_name") or "")
        owner, _, repo = name.partition("/")
        branch = str(item.get("default_branch") or "dev")
        template = template_for_role(item.get("role"), templates, config)
        try:
            files = repo_files(github, owner, repo, branch)
        except Exception as exc:  # noqa: BLE001 - planning goes on without the annotation
            logger.info("repository inspection for %s failed: %s", name, exc)
            files = None
        empty = files is not None and is_starter_only(files)
        if files is not None and empty and template is not None:
            entry["template"], entry["template_status"] = template.name, "pending"
            entry["conventions"] = template.conventions()
            facts = bdd_policy.inspect_files(_template_files_list(template), _template_package(template))
        elif files is not None:
            applied = _TEMPLATE_LINE.search(_marker(github, owner, repo, branch)) if MARKER_FILE in files else None
            if applied:
                entry["template"], entry["template_status"] = applied.group(1), "applied"
            own = _text_file(github, owner, repo, "AGENTS.md", branch) if "AGENTS.md" in files else ""
            if own:
                entry["conventions"] = own[:_CONVENTIONS_LIMIT]
            elif applied and applied.group(1) in templates:
                entry["conventions"] = templates[applied.group(1)].conventions()
            package = _text_file(github, owner, repo, "package.json", branch) if "package.json" in files else None
            facts = bdd_policy.inspect_files(files, package or None)
            if not empty:
                entry["ci_on_pull_requests"] = _has_pr_ci(github, owner, repo, branch)
        else:
            facts = None
        if facts is not None:
            entry["has_gherkin_runner"] = bool(facts.get("has_gherkin_runner"))
        kept = earlier.get(name.lower()) or {}
        if bdd_policy.repository_policy(kept) is not None:
            entry["bdd"], entry["bdd_reason"] = kept["bdd"], kept.get("bdd_reason") or "decided when the program started"
        elif facts is not None:
            entry["bdd"], entry["bdd_reason"] = bdd_policy.decide(item.get("bdd_mode") or "auto", facts)
        out.append(entry)
    return out


# Enough for a contributor guide; a repository's planning input stays bounded.
_CONVENTIONS_LIMIT = 20_000


def _text_file(github: Any, owner: str, repo: str, path: str, branch: str) -> str:
    payload = github.get_file(owner, repo, path, ref=branch)
    if not isinstance(payload, dict) or not payload.get("content"):
        return ""
    return base64.b64decode(str(payload["content"])).decode(errors="replace")


def _template_files_list(template: Template) -> list[str]:
    return [p.relative_to(template.path).as_posix() for p in _template_files(template.path)]


def _template_package(template: Template) -> str | None:
    path = template.path / "package.json"
    return path.read_text(encoding="utf-8") if path.is_file() else None


def _has_pr_ci(github: Any, owner: str, repo: str, branch: str) -> bool | None:
    """Whether any GitHub Actions workflow runs on pull requests into ``branch`` (None: unknown)."""
    from ts_harness.workflows.repo_governance import _workflows, workflow_runs_on_pr

    try:
        return any(workflow_runs_on_pr(data, branch) for data in _workflows(github, owner, repo, branch).values())
    except Exception as exc:  # noqa: BLE001
        logger.info("CI check for %s/%s failed: %s", owner, repo, exc)
        return None
