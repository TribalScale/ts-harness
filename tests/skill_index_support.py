"""In-memory stand-in for the skill-index routes, and a harness that runs authoring suites on the indexed repository."""

from __future__ import annotations

import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

from tests.shared_tree_support import FakeSharedFiles

from ts_harness.memory.client import MemoryApiConflict
from ts_harness.skills.repository import SKILLS_NAMESPACE, IndexedSkillRepository
from ts_harness.storage.objects import LocalObjectStore


def _version_gt(left: str, right: str) -> bool:
    a = tuple(int(p) for p in str(left).split("."))
    b = tuple(int(p) for p in str(right).split("."))
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def _conflict(message: str, code: str) -> MemoryApiConflict:
    return MemoryApiConflict(message, {"error": message, "code": code})


class FakeSkillIndex(FakeSharedFiles):
    """Same rules as memory_routes.skill_index: one live row and one open draft per skill, version rules, generation bumps."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[dict[str, Any]] = []
        self.fail_next: dict[str, Exception] = {}

    def _bump(self) -> int:
        self.generations[SKILLS_NAMESPACE] = self.generations.get(SKILLS_NAMESPACE, 0) + 1
        return self.generations[SKILLS_NAMESPACE]

    def _maybe_fail(self, op: str) -> None:
        exc = self.fail_next.pop(op, None)
        if exc is not None:
            raise exc

    def _live(self, skill_id: str) -> dict[str, Any] | None:
        return next((r for r in self.rows if r["skill_id"] == skill_id and r["status"] == "active"), None)

    def _check_rules(self, *, skill_id: str, version: str, category: str, revision_of: str | None) -> None:
        live = self._live(skill_id)
        if revision_of:
            if revision_of != skill_id:
                raise _conflict(f"revision_of must equal skill id: {skill_id}", "skill_revision_mismatch")
            if live is None:
                raise _conflict(f"revision_of is not a live skill: {revision_of}", "skill_revision_not_live")
            if not _version_gt(version, live["version"]):
                raise _conflict("revision draft must bump version before proposing", "skill_version_not_bumped")
            if category != live["category"]:
                raise _conflict("revision draft category must match the live skill category", "skill_category_changed")
        elif live is not None:
            raise _conflict(f"skill id already exists: {skill_id}", "skill_learn_use_revision")

    def list_skill_index(self, *, statuses: tuple[str, ...] = ("active", "draft"), skill_id: str = "") -> dict[str, Any]:
        with self.guard:
            rows = [dict(r) for r in self.rows if r["status"] in statuses and (not skill_id or r["skill_id"] == skill_id)]
            return {"generation": self.generations.get(SKILLS_NAMESPACE, 0), "skills": rows}

    def upsert_skill_draft(self, **body: Any) -> dict[str, Any]:
        with self.guard:
            self._maybe_fail("upsert")
            skill_id = body["skill_id"]
            self._check_rules(
                skill_id=skill_id, version=body["version"], category=body["category"], revision_of=body.get("revision_of")
            )
            prior = next((r for r in self.rows if r["skill_id"] == skill_id and r["status"] == "draft"), None)
            if prior is not None and prior["tenant_id"] != body.get("tenant_id", "default"):
                raise _conflict(f"draft already exists for skill id: {skill_id}", "skill_draft_exists")
            fields = {k: body.get(k) for k in ("version", "category", "leaf", "layout", "object_prefix", "files", "content_sha", "source", "revision_of", "created_by")}
            previous = prior["object_prefix"] if prior is not None and prior["object_prefix"] != body["object_prefix"] else None
            if prior is None:
                prior = {"id": str(uuid.uuid4()), "tenant_id": body.get("tenant_id", "default"), "skill_id": skill_id, "status": "draft",
                         "draft_id": None, "created_at": datetime.now(UTC).isoformat()}
                self.rows.append(prior)
            prior.update(fields)
            return {"skill": dict(prior), "previous_prefix": previous, "generation": self._bump()}

    def _open_draft(self, draft_id: str) -> dict[str, Any]:
        row = next((r for r in self.rows if r["id"] == draft_id), None)
        if row is None:
            raise LookupError("draft not found")
        if row["status"] != "draft":
            raise _conflict(f"draft is already {row['status']}", "skill_draft_decided")
        return row

    def approve_skill_draft(self, draft_id: str, **body: Any) -> dict[str, Any]:
        with self.guard:
            self._maybe_fail("approve")
            draft = self._open_draft(draft_id)
            if body["object_prefix"].startswith("skills/drafts/"):
                raise ValueError("approved content must live under an immutable version prefix")
            self._check_rules(skill_id=draft["skill_id"], version=draft["version"], category=draft["category"], revision_of=draft["revision_of"])
            live = self._live(draft["skill_id"])
            if live is not None:
                live["status"] = "superseded"
            draft["status"] = "approved"
            row = {**draft, "id": str(uuid.uuid4()), "status": "active", "object_prefix": body["object_prefix"], "files": body["files"],
                   "content_sha": body["content_sha"], "layout": body.get("layout") or draft["layout"], "draft_id": draft["id"],
                   "decided_by": body["reviewer"]}
            self.rows.append(row)
            return {"skill": dict(row), "draft_prefix": draft["object_prefix"], "superseded_id": live["id"] if live else None,
                    "generation": self._bump()}

    def reject_skill_draft(self, draft_id: str, *, reviewer: str, reason: str = "") -> dict[str, Any]:
        with self.guard:
            self._maybe_fail("reject")
            draft = self._open_draft(draft_id)
            draft.update(status="rejected", decided_by=reviewer, reason=reason)
            return {"skill": dict(draft), "generation": self._bump()}


class IndexedRepoHarness:
    """Routes the module-level authoring calls of a test module through IndexedSkillRepository.

    Every write goes to a fake index and a LocalObjectStore, and the test's
    directory is rebuilt from them with local reuse off, so every file the test
    reads back has made the round trip through object storage.
    """

    def __init__(self, module: Any) -> None:
        self.module = module
        self.index = FakeSkillIndex()
        self._objects_dir = tempfile.TemporaryDirectory()
        self.objects = LocalObjectStore(Path(self._objects_dir.name))
        self.repos: dict[Path, IndexedSkillRepository] = {}
        self._patch: Any = None

    def repo(self, root: str | Path) -> IndexedSkillRepository:
        key = Path(root).resolve()
        if key not in self.repos:
            self.repos[key] = IndexedSkillRepository(
                Path(root), client=self.index, objects=self.objects, sync_interval=0, reuse_local=False
            )
        return self.repos[key]

    def adapters(self) -> dict[str, Any]:
        return {
            "propose_skill_draft": lambda *, root, read_roots=None, **kw: self.repo(root).propose_markdown(read_roots=read_roots, **kw),
            "propose_skill_folder_draft": lambda *, root, read_roots=None, **kw: self.repo(root).propose_folder(read_roots=read_roots, **kw),
            "approve_draft": lambda *, root, skill_id, reviewer, read_roots=None: self.repo(root).approve(
                skill_id=skill_id, reviewer=reviewer, read_roots=read_roots
            ),
            "reject_draft": lambda *, root, skill_id, reviewer, reason="": self.repo(root).reject(
                skill_id=skill_id, reviewer=reviewer, reason=reason
            ),
            "get_draft": lambda root, skill_id, read_roots=None: self.repo(root).get_draft(skill_id, read_roots=read_roots),
            "read_draft_file": lambda root, skill_id, rel_path: self.repo(root).read_draft_file(skill_id, rel_path),
            "list_drafts": lambda root: self.repo(root).list_drafts(),
        }

    def start(self) -> None:
        names = {k: v for k, v in self.adapters().items() if hasattr(self.module, k)}
        self._patch = mock.patch.multiple(self.module, **names)
        self._patch.start()

    def stop(self) -> None:
        if self._patch is not None:
            self._patch.stop()
        self._objects_dir.cleanup()
