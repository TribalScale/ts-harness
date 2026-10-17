"""Governed skill draft propose / approve / reject helpers."""

from __future__ import annotations

import contextlib
import difflib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
import os
import re
import secrets
import shutil

import yaml

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

from ts_harness.skills.catalog import FRONTMATTER_RE, SKILL_FILE_EXTENSIONS, SkillCatalog, SkillMetadata
from ts_harness.skills.guard import scan_skill_content


_SKILL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,127}$")
_RESERVED_CATEGORIES = frozenset({"coding-backend", "drafts"})
_GOVERNANCE_KEYS = frozenset({"id", "name", "version", "category", "description", "trust_level", "approval_status", "owner", "provenance"})

SKILL_ENTRYPOINT = "SKILL.md"
ALLOWED_FILE_EXTENSIONS = SKILL_FILE_EXTENSIONS
MAX_UPLOAD_FILES = 200
MAX_UPLOAD_FILE_BYTES = 256 * 1024
MAX_UPLOAD_TOTAL_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_DEPTH = 6
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class SkillAuthoringError(ValueError):
    """Raised when propose/approve/reject cannot proceed."""

    def __init__(self, message: str, *, code: str | None = None, path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


def validate_skill_markdown(text: str) -> dict[str, Any]:
    """Parse and validate skill markdown; return normalized frontmatter fields."""
    if not str(text or "").strip():
        raise SkillAuthoringError("skill markdown is empty")
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise SkillAuthoringError("skill markdown requires YAML frontmatter")
    frontmatter = yaml.safe_load(match.group(1)) or {}
    if not isinstance(frontmatter, dict):
        raise SkillAuthoringError("skill frontmatter must be a mapping")
    skill_id = str(frontmatter.get("id") or frontmatter.get("name") or "").strip()
    if not skill_id or not _SKILL_ID_RE.match(skill_id):
        raise SkillAuthoringError("skill id is required and must be a safe slug")
    category = str(frontmatter.get("category") or "").strip()
    if not category:
        raise SkillAuthoringError("category is required")
    if category in _RESERVED_CATEGORIES or category.startswith("coding-backend"):
        raise SkillAuthoringError(f"category {category!r} is not allowed for agent drafts")
    description = str(frontmatter.get("description") or "").strip()
    if not description:
        raise SkillAuthoringError("description is required")
    version = str(frontmatter.get("version") or "1.0.0").strip() or "1.0.0"
    body = text[match.end() :]
    return {
        "id": skill_id,
        "version": version,
        "category": category,
        "description": description,
        "body": body,
        "frontmatter": frontmatter,
    }


def build_skill_markdown(
    text: str,
    *,
    skill_id: str = "",
    category: str = "",
    description: str = "",
    version: str = "",
) -> str:
    """Fill missing frontmatter keys from form fields; reject conflicting values."""
    fields = {
        "id": str(skill_id or "").strip(),
        "category": str(category or "").strip(),
        "description": str(description or "").strip(),
        "version": str(version or "").strip(),
    }
    body = str(text or "")
    match = FRONTMATTER_RE.match(body)
    frontmatter: dict[str, Any] = {}
    if match:
        loaded = yaml.safe_load(match.group(1)) or {}
        if not isinstance(loaded, dict):
            raise SkillAuthoringError("skill frontmatter must be a mapping")
        frontmatter = loaded
        body = body[match.end() :]
    for key, value in fields.items():
        if not value:
            continue
        existing = str(frontmatter.get(key) or (frontmatter.get("name") if key == "id" else "") or "").strip()
        if existing and existing != value:
            raise SkillAuthoringError(
                f"{key} {value!r} conflicts with frontmatter {key} {existing!r}",
                code="skill_field_conflict",
            )
        if not existing:
            frontmatter[key] = value
    if "version" not in frontmatter:
        frontmatter["version"] = "1.0.0"
    return _render_markdown(frontmatter, body)


def _safe_id(skill_id: str) -> str:
    return skill_id.replace("/", "__")


def _draft_path(root: Path, skill_id: str) -> Path:
    return root / "drafts" / f"{_safe_id(skill_id)}.md"


def _draft_folder(root: Path, skill_id: str) -> Path:
    return root / "drafts" / _safe_id(skill_id)


def _find_draft_path(root: Path, skill_id: str) -> Path | None:
    direct = _draft_path(root, skill_id)
    if direct.is_file():
        return direct
    nested = _draft_folder(root, skill_id) / SKILL_ENTRYPOINT
    if nested.is_file():
        return nested
    # Also accept drafts/<skill_id>.md when skill_id has no slashes
    alt = root / "drafts" / f"{skill_id}.md"
    if alt.is_file():
        return alt
    catalog = SkillCatalog(root)
    catalog.load()
    for meta in catalog.index.values():
        if meta.id == skill_id and meta.category == "drafts":
            path = Path(meta.source_path)
            if path.is_file():
                return path
    return None


def _draft_dir(path: Path) -> Path | None:
    """Directory of a folder-form draft, or None for a single-file draft."""
    if path.name == SKILL_ENTRYPOINT and path.parent.parent.name == "drafts":
        return path.parent
    return None


def _render_markdown(frontmatter: dict[str, Any], body: str) -> str:
    dumped = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{dumped}\n---\n{body.lstrip()}\n"


def _extra_frontmatter(frontmatter: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in frontmatter.items() if key not in _GOVERNANCE_KEYS}


def is_live_skill_meta(meta: SkillMetadata) -> bool:
    return meta.category != "drafts" and meta.trust_level != "agent-created"


def _roots(root: Path, read_roots: list[str | Path] | None) -> list[Path]:
    return [Path(item) for item in read_roots] if read_roots else [root]


def _catalog(roots: list[Path]) -> SkillCatalog:
    catalog = SkillCatalog(roots=roots)
    catalog.load()
    return catalog


def _existing_non_draft_ids(roots: list[Path]) -> set[str]:
    return {meta.id for meta in _catalog(roots).index.values() if is_live_skill_meta(meta)}


def _live_skill_meta(roots: list[Path], skill_id: str) -> SkillMetadata | None:
    return next(
        (
            meta
            for meta in _catalog(roots).index.values()
            if meta.id == skill_id and is_live_skill_meta(meta)
        ),
        None,
    )


def _version_tuple(version: Any) -> tuple[int, ...]:
    text = str(version or "").strip()
    if not text:
        raise SkillAuthoringError(
            "revision draft must bump version before proposing"
        )
    parts = text.split(".")
    if not all(part.isdigit() for part in parts):
        raise SkillAuthoringError(
            "revision draft must bump version before proposing"
        )
    return tuple(int(part) for part in parts)


def _version_lte(left: Any, right: Any) -> bool:
    left_tuple = _version_tuple(left)
    right_tuple = _version_tuple(right)
    max_len = max(len(left_tuple), len(right_tuple))
    return left_tuple + (0,) * (max_len - len(left_tuple)) <= right_tuple + (
        0,
    ) * (max_len - len(right_tuple))


@contextlib.contextmanager
def _write_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".skills.lock").open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _check_new_or_revision(
    parsed: dict[str, Any],
    revision: str | None,
    roots: list[Path],
) -> None:
    from ts_harness.skills.roots import is_builtin_path

    skill_id = str(parsed["id"])
    live_ids = _existing_non_draft_ids(roots)
    if revision:
        if skill_id != revision:
            raise SkillAuthoringError(f"markdown id must equal revision_of: {revision}")
        live_meta = _live_skill_meta(roots, skill_id)
        if live_meta is None or skill_id not in live_ids:
            raise SkillAuthoringError(f"revision_of is not a live skill: {revision}")
        if is_builtin_path(live_meta.source_path):
            raise SkillAuthoringError(
                "builtin skills are revised in the repo",
                code="skill_revision_builtin",
            )
        if _version_lte(parsed["version"], live_meta.version):
            raise SkillAuthoringError(
                "revision draft must bump version before proposing"
            )
        if f"{skill_id}@{parsed['version']}" in _catalog(roots).index:
            raise SkillAuthoringError(
                "revision draft version already exists for this live skill"
            )
        if str(parsed["category"]) != str(live_meta.category):
            raise SkillAuthoringError(
                "revision draft category must match the live skill category"
            )
    elif skill_id in live_ids:
        raise SkillAuthoringError(
            f"skill id already exists: {skill_id}",
            code="skill_learn_use_revision",
        )
    if ".." in skill_id or skill_id.startswith("/"):
        raise SkillAuthoringError("skill id path traversal denied")


def _check_existing_draft_tenant(root: Path, skill_id: str, tenant_id: str) -> None:
    existing = _find_draft_path(root, skill_id)
    if existing is None:
        return
    prior_match = FRONTMATTER_RE.match(existing.read_text(encoding="utf-8"))
    if prior_match is None:
        raise SkillAuthoringError(f"draft already exists for skill id: {skill_id}")
    prior = yaml.safe_load(prior_match.group(1) or "") or {}
    prior_tenant = str((prior.get("provenance") or {}).get("tenant_id") or "")
    if prior_tenant != tenant_id:
        raise SkillAuthoringError(f"draft already exists for skill id: {skill_id}")


def _draft_frontmatter(
    parsed: dict[str, Any],
    *,
    owner: str,
    tenant_id: str,
    revision: str | None,
    source: str,
    created_by: str,
) -> dict[str, Any]:
    created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": parsed["id"],
        "version": parsed["version"],
        "category": parsed["category"],
        "description": parsed["description"],
        **_extra_frontmatter(parsed["frontmatter"]),
        "trust_level": "agent-created",
        "approval_status": "draft",
        "owner": owner,
        "provenance": {
            "created_by_agent": owner,
            "created_by": created_by or owner,
            "source": source,
            "tenant_id": tenant_id,
            "created_at": created_at,
            **({"revision_of": revision} if revision else {}),
        },
    }


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _remove_drafts(root: Path, skill_id: str) -> None:
    for candidate in (
        _draft_path(root, skill_id),
        root / "drafts" / f"{skill_id}.md",
        _draft_folder(root, skill_id),
    ):
        if candidate.exists():
            _remove_path(candidate)


def propose_skill_draft(
    *,
    root: str | Path,
    agent_id: str,
    tenant_id: str,
    markdown: str,
    revision_of: str | None = None,
    source: str = "learn",
    created_by: str = "",
    read_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Write a draft skill under skills/drafts/. Never sets approval_status approved."""
    root_path = Path(root)
    parsed = validate_skill_markdown(markdown)
    skill_id = str(parsed["id"])
    revision = str(revision_of or "").strip() or None
    roots = _roots(root_path, read_roots)
    with _write_lock(root_path):
        _check_new_or_revision(parsed, revision, roots)

        scan = scan_skill_content(markdown, trust_level="community")
        if scan.findings:
            raise SkillAuthoringError(
                "skill draft rejected by threat scan: " + "; ".join(scan.findings)
            )

        _check_existing_draft_tenant(root_path, skill_id, tenant_id)
        frontmatter = _draft_frontmatter(
            parsed,
            owner=agent_id,
            tenant_id=tenant_id,
            revision=revision,
            source=source,
            created_by=created_by,
        )
        existing = _find_draft_path(root_path, skill_id)
        if existing is not None and _draft_dir(existing) is None:
            target = existing
        else:
            target = _draft_path(root_path, skill_id)
        folder = _draft_folder(root_path, skill_id)
        if folder.exists():
            _remove_path(folder)
        target.parent.mkdir(parents=True, exist_ok=True)
        text = _render_markdown(frontmatter, str(parsed["body"]))
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    return {
        "status": "draft",
        "skill_id": skill_id,
        "path": str(target),
        "approval_status": "draft",
        "trust_level": "agent-created",
        "category": parsed["category"],
        "revision_of": revision,
        "layout": "file",
    }


def _check_rel_path(rel: str) -> list[str]:
    if not rel or rel.startswith("/") or "\\" in rel:
        raise SkillAuthoringError(f"invalid file path: {rel!r}", code="skill_upload_path", path=rel)
    parts = rel.split("/")
    for part in parts:
        if part in {"", ".", ".."} or part.startswith(".") or not _PATH_SEGMENT_RE.match(part):
            raise SkillAuthoringError(f"invalid file path: {rel}", code="skill_upload_path", path=rel)
    if len(parts) > MAX_UPLOAD_DEPTH:
        raise SkillAuthoringError(
            f"file path is nested more than {MAX_UPLOAD_DEPTH} levels: {rel}",
            code="skill_upload_depth",
            path=rel,
        )
    if Path(parts[-1]).suffix.lower() not in ALLOWED_FILE_EXTENSIONS:
        raise SkillAuthoringError(
            f"file type not allowed: {rel} (allowed: {', '.join(sorted(ALLOWED_FILE_EXTENSIONS))})",
            code="skill_upload_extension",
            path=rel,
        )
    return parts


def normalize_upload_files(files: Any) -> dict[str, str]:
    """Validate an uploaded skill tree; return ``{relative_path: content}``."""
    if not isinstance(files, list) or not files:
        raise SkillAuthoringError("files must be a non-empty list", code="skill_upload_empty")
    if len(files) > MAX_UPLOAD_FILES:
        raise SkillAuthoringError(
            f"too many files: {len(files)} (max {MAX_UPLOAD_FILES})",
            code="skill_upload_too_many",
        )
    raw: list[tuple[str, str]] = []
    for item in files:
        if not isinstance(item, dict):
            raise SkillAuthoringError("each file needs path and content", code="skill_upload_path")
        path = str(item.get("path") or "").strip()
        content = item.get("content")
        if not isinstance(content, str):
            raise SkillAuthoringError(f"file content must be text: {path}", code="skill_upload_content", path=path)
        raw.append((path, content))

    split = [path.split("/") for path, _ in raw]
    first = split[0][0] if split[0] else ""
    if (
        first
        and all(len(parts) > 1 and parts[0] == first for parts in split)
        and first != SKILL_ENTRYPOINT
    ):
        raw = [("/".join(path.split("/")[1:]), content) for path, content in raw]

    tree: dict[str, str] = {}
    lowered: dict[str, str] = {}
    total = 0
    for path, content in raw:
        parts = _check_rel_path(path)
        if parts[-1] == SKILL_ENTRYPOINT and len(parts) > 1:
            raise SkillAuthoringError(
                f"nested {SKILL_ENTRYPOINT} is not allowed: {path}",
                code="skill_upload_nested_entrypoint",
                path=path,
            )
        if "\x00" in content or "\ufffd" in content:
            raise SkillAuthoringError(
                f"file is not valid UTF-8 text: {path}",
                code="skill_upload_content",
                path=path,
            )
        size = len(content.encode("utf-8"))
        if size > MAX_UPLOAD_FILE_BYTES:
            raise SkillAuthoringError(
                f"file is larger than {MAX_UPLOAD_FILE_BYTES // 1024} KB: {path}",
                code="skill_upload_file_size",
                path=path,
            )
        total += size
        key = path.lower()
        if key in lowered:
            raise SkillAuthoringError(
                f"duplicate path (case-insensitive): {path} and {lowered[key]}",
                code="skill_upload_duplicate",
                path=path,
            )
        lowered[key] = path
        tree[path] = content
    if total > MAX_UPLOAD_TOTAL_BYTES:
        raise SkillAuthoringError(
            f"upload is larger than {MAX_UPLOAD_TOTAL_BYTES // (1024 * 1024)} MB",
            code="skill_upload_total_size",
        )
    if SKILL_ENTRYPOINT not in tree:
        raise SkillAuthoringError(
            f"upload needs {SKILL_ENTRYPOINT} at the top of the folder",
            code="skill_upload_missing_entrypoint",
        )
    return tree


def _scan_tree(tree: dict[str, str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for rel, content in sorted(tree.items()):
        scan = scan_skill_content(content, trust_level="community")
        if scan.findings:
            findings.append({"path": rel, "findings": list(scan.findings)})
    return findings


def _write_tree(dest: Path, tree: dict[str, str]) -> None:
    for rel, content in tree.items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _staging_dir(base: Path, name: str) -> Path:
    staging = base / ".staging"
    staging.mkdir(parents=True, exist_ok=True)
    return staging / f"{_safe_id(name)}-{secrets.token_hex(4)}"


def propose_skill_folder_draft(
    *,
    root: str | Path,
    actor_id: str,
    tenant_id: str,
    files: Any,
    revision_of: str | None = None,
    source: str = "admin_upload",
    read_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    """Write a multi-file draft under skills/drafts/<id>/."""
    root_path = Path(root)
    tree = normalize_upload_files(files)
    parsed = validate_skill_markdown(tree[SKILL_ENTRYPOINT])
    skill_id = str(parsed["id"])
    revision = str(revision_of or "").strip() or None
    roots = _roots(root_path, read_roots)
    with _write_lock(root_path):
        _check_new_or_revision(parsed, revision, roots)
        findings = _scan_tree(tree)
        if findings:
            first = findings[0]
            raise SkillAuthoringError(
                f"skill draft rejected by threat scan in {first['path']}: "
                + "; ".join(first["findings"]),
                code="skill_upload_scan",
                path=str(first["path"]),
            )
        _check_existing_draft_tenant(root_path, skill_id, tenant_id)
        frontmatter = _draft_frontmatter(
            parsed,
            owner=actor_id,
            tenant_id=tenant_id,
            revision=revision,
            source=source,
            created_by=actor_id,
        )
        rendered = {**tree, SKILL_ENTRYPOINT: _render_markdown(frontmatter, str(parsed["body"]))}
        drafts = root_path / "drafts"
        staging = _staging_dir(drafts, skill_id)
        try:
            _write_tree(staging, rendered)
            _remove_drafts(root_path, skill_id)
            target = _draft_folder(root_path, skill_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, target)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    return {
        "status": "draft",
        "skill_id": skill_id,
        "path": str(target / SKILL_ENTRYPOINT),
        "approval_status": "draft",
        "trust_level": "agent-created",
        "category": parsed["category"],
        "revision_of": revision,
        "layout": "folder",
        "files": sorted(rendered),
    }


def list_drafts(root: str | Path) -> list[dict[str, Any]]:
    root_path = Path(root)
    catalog = SkillCatalog(root_path)
    catalog.load()
    items: list[dict[str, Any]] = []
    for meta in catalog.index.values():
        if meta.category != "drafts" and meta.trust_level != "agent-created":
            continue
        if meta.approval_status not in {"draft", "pending_review", "blocked", "rejected"}:
            if meta.trust_level != "agent-created":
                continue
        items.append(_meta_public(meta))
    items.sort(key=lambda item: item["id"])
    return items


def _iter_tree(base: Path) -> dict[str, Path]:
    """Visible files under a skill directory, keyed by relative path."""
    out: dict[str, Path] = {}
    if not base.is_dir():
        return out
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(base)
        if any(part.startswith(".") for part in rel.parts):
            continue
        out[rel.as_posix()] = path
    return out


def _body(text: str) -> str:
    match = FRONTMATTER_RE.match(text)
    return text[match.end():] if match else text


def _skill_tree_texts(entry: Path) -> dict[str, str]:
    """Text of every file in a skill; single-file skills are just SKILL.md."""
    if entry.name == SKILL_ENTRYPOINT:
        texts = {
            rel: path.read_text(encoding="utf-8", errors="replace")
            for rel, path in _iter_tree(entry.parent).items()
        }
    else:
        texts = {SKILL_ENTRYPOINT: entry.read_text(encoding="utf-8")}
    if SKILL_ENTRYPOINT in texts:
        texts[SKILL_ENTRYPOINT] = _body(texts[SKILL_ENTRYPOINT])
    return texts


def _unified(live: str, draft: str) -> str:
    return "".join(
        difflib.unified_diff(
            live.splitlines(True),
            draft.splitlines(True),
            fromfile="live",
            tofile="draft",
        )
    )


def get_draft(
    root: str | Path,
    skill_id: str,
    read_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    root_path = Path(root)
    path = _find_draft_path(root_path, skill_id)
    if path is None:
        raise SkillAuthoringError(f"draft not found: {skill_id}")
    text = path.read_text(encoding="utf-8")
    scan = scan_skill_content(text, trust_level="agent-created")
    community = scan_skill_content(text, trust_level="community")
    catalog = SkillCatalog(root_path)
    catalog.load()
    meta = next((m for m in catalog.index.values() if m.id == skill_id), None)
    match = FRONTMATTER_RE.match(text)
    frontmatter = yaml.safe_load(match.group(1) if match else "") or {}
    if not isinstance(frontmatter, dict):
        frontmatter = {}
    provenance = dict(frontmatter.get("provenance") or {})
    meta_provenance = dict(meta.provenance or {}) if meta else {}
    revision_of = (
        str(
            provenance.get("revision_of")
            or meta_provenance.get("revision_of")
            or ""
        ).strip()
        or None
    )
    folder = _draft_dir(path)
    layout = "folder" if folder is not None else "file"
    file_entries: list[dict[str, Any]] = []
    if folder is not None:
        for rel, file_path in _iter_tree(folder).items():
            content = file_path.read_text(encoding="utf-8", errors="replace")
            file_entries.append(
                {
                    "path": rel,
                    "size": file_path.stat().st_size,
                    "scan_findings": list(scan_skill_content(content, trust_level="community").findings),
                }
            )
    else:
        file_entries.append(
            {
                "path": SKILL_ENTRYPOINT,
                "size": path.stat().st_size,
                "scan_findings": list(community.findings),
            }
        )

    revision_diff = None
    revision_diffs: list[dict[str, Any]] = []
    if revision_of:
        live_meta = _live_skill_meta(_roots(root_path, read_roots), revision_of)
        live = Path(live_meta.source_path) if live_meta else None
        if live is not None and live.is_file():
            live_texts = _skill_tree_texts(live)
            draft_texts = _skill_tree_texts(path)
            revision_diff = _unified(
                live_texts.get(SKILL_ENTRYPOINT, ""),
                draft_texts.get(SKILL_ENTRYPOINT, ""),
            )
            compared = sorted(set(live_texts) | set(draft_texts)) if folder is not None else [SKILL_ENTRYPOINT]
            for rel in compared:
                before = live_texts.get(rel)
                after = draft_texts.get(rel)
                if before == after:
                    continue
                status = "added" if before is None else "removed" if after is None else "changed"
                revision_diffs.append(
                    {"path": rel, "status": status, "diff": _unified(before or "", after or "")}
                )
    return {
        "skill_id": skill_id,
        "path": str(path),
        "content": text,
        "metadata": _meta_public(meta) if meta else None,
        "layout": layout,
        "files": file_entries,
        "provenance": provenance,
        "revision_of": revision_of,
        "revision_diff": revision_diff,
        "revision_diffs": revision_diffs,
        "scan_findings": list(scan.findings),
        "community_findings": list(community.findings),
        "community_allowed": community.allowed,
    }


def safe_skill_file(base: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` inside ``base``; reject traversal, symlink escape, and bad types."""
    rel = str(rel_path or "").strip()
    _check_rel_path(rel)
    candidate = base / rel
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise SkillAuthoringError(f"file not found: {rel}", path=rel) from exc
    if not resolved.is_relative_to(base.resolve()):
        raise SkillAuthoringError(f"file is outside the skill: {rel}", path=rel)
    if not resolved.is_file():
        raise SkillAuthoringError(f"file not found: {rel}", path=rel)
    return resolved


def read_draft_file(root: str | Path, skill_id: str, rel_path: str) -> dict[str, Any]:
    root_path = Path(root)
    path = _find_draft_path(root_path, skill_id)
    if path is None:
        raise SkillAuthoringError(f"draft not found: {skill_id}")
    folder = _draft_dir(path)
    if folder is None:
        if rel_path != SKILL_ENTRYPOINT:
            raise SkillAuthoringError(f"file not found: {rel_path}", path=rel_path)
        target = path
    else:
        target = safe_skill_file(folder, rel_path)
    return {"path": rel_path, "content": target.read_text(encoding="utf-8", errors="replace")}


def _mark_blocked(path: Path, text: str, body: str) -> None:
    match = FRONTMATTER_RE.match(text)
    frontmatter = yaml.safe_load(match.group(1)) if match else {}
    if not isinstance(frontmatter, dict):
        frontmatter = {}
    frontmatter["approval_status"] = "blocked"
    frontmatter["trust_level"] = "agent-created"
    path.write_text(_render_markdown(frontmatter, body), encoding="utf-8")


def _swap_into_place(staging: Path, dest: Path, previous: list[Path], base: Path) -> None:
    """Move ``previous`` live entries aside, move ``staging`` to ``dest``; restore on failure."""
    backups: list[tuple[Path, Path]] = []
    try:
        for item in previous:
            if item.exists():
                backup = _staging_dir(base, item.name + ".bak")
                os.replace(item, backup)
                backups.append((item, backup))
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, dest)
    except Exception:
        for original, backup in reversed(backups):
            if backup.exists() and not original.exists():
                os.replace(backup, original)
        raise
    for _, backup in backups:
        _remove_path(backup)


def approve_draft(
    *,
    root: str | Path,
    skill_id: str,
    reviewer: str,
    read_roots: list[str | Path] | None = None,
) -> dict[str, Any]:
    from ts_harness.skills.roots import is_builtin_path

    root_path = Path(root)
    roots = _roots(root_path, read_roots)
    with _write_lock(root_path):
        path = _find_draft_path(root_path, skill_id)
        if path is None:
            raise SkillAuthoringError(f"draft not found: {skill_id}")
        folder = _draft_dir(path)
        text = path.read_text(encoding="utf-8")
        parsed = validate_skill_markdown(text)
        body = str(parsed["body"])
        community = scan_skill_content(body, trust_level="community")
        if not community.allowed or community.findings:
            _mark_blocked(path, text, body)
            raise SkillAuthoringError(
                "approve failed threat scan: " + "; ".join(community.findings or ["blocked"])
            )
        tree: dict[str, str] = {}
        if folder is not None:
            tree = {
                rel: file_path.read_text(encoding="utf-8")
                for rel, file_path in _iter_tree(folder).items()
                if rel != SKILL_ENTRYPOINT
            }
            findings = _scan_tree(tree)
            if findings:
                _mark_blocked(path, text, body)
                first = findings[0]
                raise SkillAuthoringError(
                    f"approve failed threat scan in {first['path']}: " + "; ".join(first["findings"]),
                    path=str(first["path"]),
                )

        reviewed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        provenance = dict(parsed["frontmatter"].get("provenance") or {})
        provenance["reviewed_by"] = reviewer
        provenance["reviewed_at"] = reviewed_at
        category = str(parsed["category"])
        revision_of = str(provenance.get("revision_of") or "").strip() or None
        frontmatter = {
            "id": skill_id,
            "version": parsed["version"],
            "category": category,
            "description": parsed["description"],
            **_extra_frontmatter(parsed["frontmatter"]),
            "trust_level": "trusted",
            "approval_status": "approved",
            "owner": str(parsed["frontmatter"].get("owner") or reviewer),
            "provenance": provenance,
        }
        rendered = _render_markdown(frontmatter, body)
        leaf = skill_id.split("/")[-1]
        new_file = root_path / category / f"{leaf}.md"
        new_dir = root_path / category / leaf

        live_meta: SkillMetadata | None = None
        if revision_of:
            if revision_of != skill_id:
                raise SkillAuthoringError(
                    f"revision_of must equal skill id: {skill_id}"
                )
            live_meta = _live_skill_meta(roots, revision_of)
            if live_meta is None:
                raise SkillAuthoringError(
                    f"revision_of is not a live skill: {revision_of}"
                )
            if is_builtin_path(live_meta.source_path):
                raise SkillAuthoringError(
                    "builtin skills are revised in the repo",
                    code="skill_revision_builtin",
                )
            if not Path(live_meta.source_path).resolve().is_relative_to(root_path.resolve()):
                raise SkillAuthoringError(
                    "revision destination must be under the skills root"
                )
        elif new_file.exists() or new_dir.exists():
            existing = new_file if new_file.exists() else new_dir
            raise SkillAuthoringError(f"destination already exists: {existing}")

        if folder is None:
            dest = Path(live_meta.source_path) if live_meta is not None else new_file
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_text(rendered, encoding="utf-8")
            os.replace(tmp, dest)
            path.unlink(missing_ok=True)
            layout = "file"
        else:
            previous: list[Path] = []
            dest_dir = new_dir
            if live_meta is not None:
                live_path = Path(live_meta.source_path)
                if live_path.name == SKILL_ENTRYPOINT:
                    dest_dir = live_path.parent
                    previous = [dest_dir]
                else:
                    previous = [live_path]
            staging = _staging_dir(root_path, leaf)
            try:
                _write_tree(staging, {**tree, SKILL_ENTRYPOINT: rendered})
                _swap_into_place(staging, dest_dir, previous, root_path)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(folder, ignore_errors=True)
            dest = dest_dir / SKILL_ENTRYPOINT
            layout = "folder"
    return {
        "status": "approved",
        "skill_id": skill_id,
        "path": str(dest),
        "approval_status": "approved",
        "trust_level": "trusted",
        "category": category,
        "reviewed_by": reviewer,
        "revision_of": revision_of,
        "layout": layout,
    }


def reject_draft(
    *,
    root: str | Path,
    skill_id: str,
    reviewer: str,
    reason: str = "",
) -> dict[str, Any]:
    root_path = Path(root)
    with _write_lock(root_path):
        path = _find_draft_path(root_path, skill_id)
        if path is None:
            raise SkillAuthoringError(f"draft not found: {skill_id}")
        folder = _draft_dir(path)
        if folder is not None:
            shutil.rmtree(folder)
        else:
            path.unlink(missing_ok=True)
            parent = path.parent
            if parent.name != "drafts" and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
    return {
        "status": "rejected",
        "skill_id": skill_id,
        "reviewed_by": reviewer,
        "reason": reason,
    }


def _meta_public(meta: SkillMetadata | None) -> dict[str, Any]:
    if meta is None:
        return {}
    return {
        "id": meta.id,
        "version": meta.version,
        "category": meta.category,
        "description": meta.description,
        "owner": meta.owner,
        "trust_level": meta.trust_level,
        "approval_status": meta.approval_status,
        "source_path": meta.source_path,
        "provenance": dict(meta.provenance or {}),
        "origin": getattr(meta, "origin", "builtin"),
    }
