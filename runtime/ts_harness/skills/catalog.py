"""Governed Skill Catalog with assignment and compatibility enforcement."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import hashlib
import re

import yaml

from ts_harness.config.schema import AgentConfig, RuntimeConfig
from ts_harness.skills.aliases import canonical_skill_id, canonical_skill_ids
from ts_harness.skills.guard import scan_skill_content
from ts_harness.tools.toolsets import agent_effective_tools


FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.S)

SKILL_FILE_EXTENSIONS = frozenset({".md", ".txt", ".json", ".yaml", ".yml", ".csv"})
MAX_SKILL_REFERENCE_BYTES = 256 * 1024
PROMPT_FILE_LIST_CAP = 40


def is_folder_skill(meta: "SkillMetadata") -> bool:
    return Path(meta.source_path).name == "SKILL.md"


def skill_files(meta: "SkillMetadata") -> list[str]:
    """Supporting files of a folder skill, relative to its directory (SKILL.md excluded)."""
    if not is_folder_skill(meta):
        return []
    skill_dir = Path(meta.source_path).parent
    out: list[str] = []
    for path in sorted(skill_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(skill_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if rel.as_posix() == "SKILL.md" or path.suffix.lower() not in SKILL_FILE_EXTENSIONS:
            continue
        out.append(rel.as_posix())
    return out


def skill_tree_hash(meta: "SkillMetadata") -> str:
    skill_dir = Path(meta.source_path).parent
    lines = []
    for rel in ["SKILL.md", *skill_files(meta)]:
        digest = hashlib.sha256((skill_dir / rel).read_bytes()).hexdigest()
        lines.append(f"{rel}:{digest}")
    return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()

# Internal workflow/support docs are read on demand from disk, not catalog entrypoints.
_INDEX_SKIP_DIR_NAMES = frozenset(
    {
        "stages",
        "quality-gates",
        "templates",
        "fixtures",
        "reviewers",
        "expected",
        # Coding-tool skills (OpenCode/etc.) — not enterprise agent catalog entrypoints.
        "coding-backend",
    }
)


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(version or "").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


@dataclass
class SkillMetadata:
    id: str
    version: str = "1.0.0"
    category: str = "general"
    description: str = ""
    owner: str = "ts-harness"
    trust_level: str = "builtin"
    approval_status: str = "approved"
    allowed_agents: list[str] = field(default_factory=list)
    required_toolsets: list[str] = field(default_factory=list)
    required_connectors: list[str] = field(default_factory=list)
    required_scopes: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    tenants: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    required: bool = False
    always_inline: bool = True
    disable_model_invocation: bool = False
    source_path: str = ""
    content_hash: str = ""
    origin: str = "builtin"


@dataclass
class ResolvedSkill:
    metadata: SkillMetadata
    content: str
    token_estimate: int


class SkillCatalog:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        roots: list[str | Path] | None = None,
    ):
        resolved_roots = [Path(item) for item in (roots or [])]
        if not resolved_roots:
            if root is None:
                raise ValueError("SkillCatalog requires root or roots")
            resolved_roots = [Path(root)]
        self.roots: list[Path] = resolved_roots
        self.root = resolved_roots[0]
        self.index: dict[str, SkillMetadata] = {}
        self.collisions: list[str] = []

    @classmethod
    def default(cls) -> "SkillCatalog":
        from ts_harness.skills.roots import catalog_roots

        return cls(roots=catalog_roots())

    def load(self) -> None:
        self.index.clear()
        self.collisions = []
        for root_index, root in enumerate(self.roots):
            if not root.exists():
                continue
            origin = None if len(self.roots) == 1 else ("builtin" if root_index == 0 else "managed")
            self._load_root(root, origin=origin, first=root_index == 0)

    def _load_root(self, root: Path, *, origin: str | None, first: bool) -> None:
        root_keys: set[str] = set()
        for path in sorted(root.rglob("*.md")):
            if not self._should_index(path, root):
                continue
            meta, content = self._parse_skill(path, root)
            if origin is None:
                created = meta.trust_level == "agent-created" or meta.provenance.get("reviewed_by")
                meta.origin = "managed" if created else "builtin"
            else:
                meta.origin = origin
            key = self._key(meta.id, meta.version)
            if key in root_keys:
                raise ValueError(f"duplicate skill id/version: {key}")
            root_keys.add(key)
            if not first and key in self.index:
                self.collisions.append(
                    f"skip {key}: managed copy at {path} shadowed by builtin skill"
                )
                continue
            scan = scan_skill_content(content, trust_level=meta.trust_level)
            if meta.trust_level == "agent-created":
                if scan.findings:
                    meta.approval_status = "blocked"
                elif meta.approval_status not in {
                    "draft",
                    "pending_review",
                    "rejected",
                    "blocked",
                }:
                    meta.approval_status = "draft"
            elif not scan.allowed:
                meta.approval_status = "blocked"
            self.index[key] = meta

    def reload(self) -> None:
        """Reload the catalog index from disk, rebuilding the managed mirror first if the index moved."""
        from ts_harness.skills.repository import sync_skill_mirror

        sync_skill_mirror()
        self.load()

    def _parse_skill(self, path: Path, root: Path | None = None) -> tuple[SkillMetadata, str]:
        text = path.read_text(encoding="utf-8")
        frontmatter: dict[str, Any] = {}
        content = text
        match = FRONTMATTER_RE.match(text)
        if match:
            frontmatter = yaml.safe_load(match.group(1)) or {}
            content = text[match.end() :]
        rel = path.relative_to(root or self.root)
        category = rel.parts[0] if len(rel.parts) > 1 else "general"
        skill_id = str(
            frontmatter.get("id")
            or frontmatter.get("name")
            or f"{category}/{path.stem}"
        )
        meta = SkillMetadata(
            id=skill_id,
            version=str(frontmatter.get("version", "1.0.0")),
            category=str(frontmatter.get("category", category)),
            description=str(frontmatter.get("description", "")),
            owner=str(frontmatter.get("owner", "ts-harness")),
            trust_level=str(frontmatter.get("trust_level", "builtin")),
            approval_status=str(frontmatter.get("approval_status", "approved")),
            allowed_agents=list(frontmatter.get("allowed_agents") or []),
            required_toolsets=list(frontmatter.get("required_toolsets") or []),
            required_connectors=list(frontmatter.get("required_connectors") or []),
            required_scopes=list(frontmatter.get("required_scopes") or []),
            platforms=list(frontmatter.get("platforms") or []),
            tenants=list(frontmatter.get("tenants") or []),
            tags=list(frontmatter.get("tags") or []),
            provenance=dict(frontmatter.get("provenance") or {}),
            required=bool(frontmatter.get("required", False)),
            always_inline=bool(frontmatter.get("always_inline", True)),
            disable_model_invocation=bool(
                frontmatter.get("disable-model-invocation", False)
            ),
            source_path=str(path),
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
        )
        return meta, content

    def resolve_for_agent(
        self,
        agent: AgentConfig,
        config: RuntimeConfig,
        *,
        tenant_id: str,
        fail_on_required: bool = False,
    ) -> tuple[list[ResolvedSkill], list[str]]:
        assigned_categories = set(canonical_skill_ids(agent.skills))
        effective_tools = agent_effective_tools(agent, config)
        tenant = next((item for item in config.tenants if item.id == tenant_id), None)
        enabled_connectors = set(tenant.enabled_connectors if tenant else [])
        resolved: list[ResolvedSkill] = []
        diagnostics: list[str] = []

        for skill_key, meta in self.index.items():
            if meta.category == "drafts":
                diagnostics.append(f"skip {skill_key}: drafts category")
                continue
            if meta.trust_level == "agent-created":
                diagnostics.append(f"skip {skill_key}: agent-created requires promote")
                continue
            if assigned_categories and meta.category not in assigned_categories and meta.id not in assigned_categories and skill_key not in assigned_categories:
                diagnostics.append(f"skip {skill_key}: not assigned")
                continue
            if meta.allowed_agents and agent.id not in meta.allowed_agents:
                diagnostics.append(f"skip {skill_key}: agent not allowed")
                continue
            if meta.tenants and tenant_id not in meta.tenants:
                diagnostics.append(f"skip {skill_key}: tenant excluded")
                continue
            if meta.approval_status not in {"approved"}:
                diagnostics.append(f"skip {skill_key}: approval_status={meta.approval_status}")
                continue
            for required in meta.required_toolsets:
                if required not in agent.toolsets and required not in effective_tools:
                    message = f"skip {skill_key}: missing toolset {required}"
                    if meta.required and fail_on_required:
                        raise ValueError(message)
                    diagnostics.append(message)
                    break
            else:
                for connector in meta.required_connectors:
                    if connector not in enabled_connectors:
                        message = f"skip {skill_key}: connector {connector} disabled"
                        if meta.required and fail_on_required:
                            raise ValueError(message)
                        diagnostics.append(message)
                        break
                else:
                    content = self._content_without_frontmatter(Path(meta.source_path).read_text(encoding="utf-8"))
                    resolved.append(
                        ResolvedSkill(
                            metadata=meta,
                            content=content,
                            token_estimate=max(1, len(content) // 4),
                        )
                    )
        return resolved, diagnostics

    def resolve_for_channel_chat(
        self,
        agent: AgentConfig,
        config: RuntimeConfig,
        *,
        tenant_id: str,
    ) -> tuple[list[ResolvedSkill], list[str]]:
        excluded_stems = {"workflow-state", "governed-delivery", "fan-out"}
        resolved, diagnostics = self.resolve_for_agent(
            agent,
            config,
            tenant_id=tenant_id,
            fail_on_required=False,
        )
        filtered: list[ResolvedSkill] = []
        for skill in resolved:
            stem = Path(skill.metadata.source_path).stem
            if stem in excluded_stems or skill.metadata.id in excluded_stems:
                diagnostics.append(f"skip channel chat: {skill.metadata.id}")
                continue
            filtered.append(skill)
        return filtered, diagnostics

    def resolve_for_workflow_dispatch(
        self,
        agent: AgentConfig,
        config: RuntimeConfig,
        *,
        tenant_id: str,
        fail_on_required: bool = False,
        skill_hints: list[str] | None = None,
    ) -> tuple[list[ResolvedSkill], list[str]]:
        """Resolve skills for governed workflow task dispatch.

        Filesystem-oriented workflow and integrate skills are excluded.
        Governed-runtime variants (category governed-workflows) are included.
        When skill_hints is set, only matching governed-workflow skills are kept.
        """
        resolved, diagnostics = self.resolve_for_agent(
            agent,
            config,
            tenant_id=tenant_id,
            fail_on_required=fail_on_required,
        )
        hint_ids = {canonical_skill_id(str(item).strip()) for item in (skill_hints or []) if str(item).strip()}
        filtered: list[ResolvedSkill] = []
        for skill in resolved:
            meta = skill.metadata
            if meta.category == "governed-workflows":
                if hint_ids and meta.id not in hint_ids:
                    diagnostics.append(
                        f"skip workflow dispatch: {meta.id} (not in skill_hints)"
                    )
                    continue
                filtered.append(skill)
                continue
            if meta.category in {"workflows", "integrate"}:
                diagnostics.append(
                    f"skip workflow dispatch: {meta.id} ({meta.category})"
                )
                continue
            if meta.disable_model_invocation:
                diagnostics.append(
                    f"skip workflow dispatch: {meta.id} (disable-model-invocation)"
                )
                continue
            filtered.append(skill)
        return filtered, diagnostics

    def append_governed_workflow_stage_context(
        self,
        prompt: str,
        skill_id: str,
        stage: str,
    ) -> str:
        """Inline stage-specific governed-workflow modules referenced by the skill."""
        stage = str(stage or "").strip().lower()
        if not stage:
            return prompt
        meta = self.index.get(self._key(skill_id, "1.0.0"))
        if meta is None:
            for key, candidate in self.index.items():
                if candidate.id == skill_id:
                    meta = candidate
                    break
        if meta is None:
            return prompt

        skill_dir = Path(meta.source_path).parent
        sections: list[str] = []
        for rel in (
            "runtime-contract.md",
            "stage-routing.md",
            "output-envelope.md",
            f"stages/{stage}-runtime.md",
            f"quality-gates/{stage}-runtime.md",
        ):
            path = skill_dir / rel
            if not path.is_file():
                continue
            sections.append(f"### {rel}\n{path.read_text(encoding='utf-8').strip()}")
        if not sections:
            return prompt
        return (
            f"{prompt}\n\n## Governed stage context ({skill_id} / {stage})\n\n"
            + "\n\n".join(sections)
        )

    def doctor(self, agent: AgentConfig, config: RuntimeConfig, *, tenant_id: str) -> list[str]:
        _, diagnostics = self.resolve_for_agent(agent, config, tenant_id=tenant_id)
        return [*self.collisions, *diagnostics]

    def build_prompt_bundle(self, skills: list[ResolvedSkill]) -> dict[str, Any]:
        sections: dict[str, list[str]] = {}
        audit: list[dict[str, Any]] = []
        for skill in skills:
            meta = skill.metadata
            header = f"[skill id={meta.id} version={meta.version} category={meta.category}]"
            files = skill_files(meta)
            if files:
                listed = ", ".join(files[:PROMPT_FILE_LIST_CAP])
                if len(files) > PROMPT_FILE_LIST_CAP:
                    listed += f" and {len(files) - PROMPT_FILE_LIST_CAP} more"
                header += f"\nFiles (read with read_skill_reference skill_id={meta.id} path=...): {listed}"
            if meta.always_inline:
                text = f"{header}\n{skill.content}"
            else:
                text = f"{header}\nLarge reference omitted; use read_skill_reference for {meta.id}@{meta.version}."
            sections.setdefault(meta.category, []).append(text)
            entry = {
                "id": meta.id,
                "version": meta.version,
                "hash": meta.content_hash,
                "source_path": meta.source_path,
                "token_estimate": skill.token_estimate,
            }
            if is_folder_skill(meta):
                entry["tree_hash"] = skill_tree_hash(meta)
            audit.append(entry)
        prompt = "\n\n".join(
            f"## Skill Category: {category}\n" + "\n\n".join(items)
            for category, items in sorted(sections.items())
        )
        return {"prompt": prompt, "audit": audit}

    def read_skill_reference(
        self,
        skill_id: str,
        *,
        version: str | None = None,
        path: str | None = None,
        agent: AgentConfig | None = None,
        config: RuntimeConfig | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        unavailable = {"error": "skill not available to this agent"}
        wanted_version = str(version or "").strip()
        if agent is not None and config is not None:
            resolved, _ = self.resolve_for_agent(agent, config, tenant_id=str(tenant_id or ""))
            candidates = [item.metadata for item in resolved if item.metadata.id == skill_id]
        else:
            candidates = [
                meta
                for meta in self.index.values()
                if meta.id == skill_id
                and meta.category != "drafts"
                and meta.trust_level != "agent-created"
                and meta.approval_status == "approved"
            ]
        if wanted_version:
            candidates = [meta for meta in candidates if meta.version == wanted_version]
        if not candidates:
            return unavailable
        meta = max(candidates, key=lambda item: _version_key(item.version))

        rel = str(path or "").strip()
        if not rel:
            content = self._content_without_frontmatter(Path(meta.source_path).read_text(encoding="utf-8"))
            result: dict[str, Any] = {
                "id": meta.id,
                "version": meta.version,
                "hash": meta.content_hash,
                "source_path": meta.source_path,
                "content": content,
            }
            if is_folder_skill(meta):
                result["files"] = skill_files(meta)
            return result

        if not is_folder_skill(meta):
            return {"error": f"skill {meta.id} has no supporting files"}
        skill_dir = Path(meta.source_path).parent.resolve()
        target = (skill_dir / rel).resolve()
        if not target.is_relative_to(skill_dir) or target == skill_dir:
            return {"error": f"path is outside the skill: {rel}"}
        if any(part.startswith(".") for part in target.relative_to(skill_dir).parts):
            return {"error": f"path is not readable: {rel}"}
        if not target.is_file():
            return {"error": f"file not found: {rel}"}
        if target.suffix.lower() not in SKILL_FILE_EXTENSIONS:
            return {"error": f"file type not allowed: {rel}"}
        if target.stat().st_size > MAX_SKILL_REFERENCE_BYTES:
            return {"error": f"file is larger than {MAX_SKILL_REFERENCE_BYTES // 1024} KB: {rel}"}
        data = target.read_bytes()
        return {
            "id": meta.id,
            "version": meta.version,
            "path": target.relative_to(skill_dir).as_posix(),
            "content": data.decode("utf-8", errors="replace"),
            "hash": hashlib.sha256(data).hexdigest(),
        }

    def _should_index(self, path: Path, root: Path | None = None) -> bool:
        rel = path.relative_to(root or self.root)
        if path.name == "README.md":
            return False
        if any(part.startswith(".") for part in rel.parts):
            return False
        if any(part in _INDEX_SKIP_DIR_NAMES for part in rel.parts):
            return False
        if path.name == "SKILL.md":
            return True
        # Top-level category entrypoints: skills/{category}/{skill}.md
        return len(rel.parts) == 2

    def _content_without_frontmatter(self, text: str) -> str:
        match = FRONTMATTER_RE.match(text)
        return text[match.end() :] if match else text

    def _key(self, skill_id: str, version: str) -> str:
        return f"{skill_id}@{version}"
