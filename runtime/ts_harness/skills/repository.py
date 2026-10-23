"""Where managed skill files live: a local directory, or object storage plus a Postgres index.

Both repositories run the same authoring code (validation, threat scan,
frontmatter, version rules) against a local directory. The indexed repository
keeps that directory as a mirror of the index: drafts are pushed to
``skills/drafts/...`` in the skills bucket, approvals are written to an
immutable ``skills/{id}/{version}-{sha12}/`` prefix and then flipped live in
one index transaction.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import socket
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterator

from ts_harness.skills import authoring
from ts_harness.skills.authoring import SKILL_ENTRYPOINT, SkillAuthoringError
from ts_harness.skills.catalog import SkillCatalog

logger = logging.getLogger(__name__)

SKILLS_NAMESPACE = "skills"
LOCK_NAME = "authoring"
SYNC_INTERVAL_SECONDS = 2.0
LOCK_TTL_SECONDS = 60
LOCK_WAIT_SECONDS = 30.0
_MANIFEST = ".skill-mirror.json"

RootsArg = list[str | Path] | None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_sha(files: list[dict[str, Any]]) -> str:
    """Hash of a skill's file list and checksums; the first 12 characters name the version prefix."""
    canonical = json.dumps(sorted(({"path": f["path"], "sha256": f["sha256"]} for f in files), key=lambda f: f["path"]))
    return _sha256(canonical.encode("utf-8"))


def _skill_files(entry: Path, layout: str) -> dict[str, bytes]:
    if layout == "folder":
        return {rel: path.read_bytes() for rel, path in authoring._iter_tree(entry.parent).items()}
    return {SKILL_ENTRYPOINT: entry.read_bytes()}


def _file_entries(tree: dict[str, bytes]) -> list[dict[str, Any]]:
    return [{"path": rel, "sha256": _sha256(data), "size": len(data)} for rel, data in sorted(tree.items())]


class SkillRepository(ABC):
    """Draft, approve and reject managed skills; reads come from the directory at ``root``."""

    def __init__(self, root: str | Path, *, read_roots: RootsArg = None) -> None:
        self.root = Path(root)
        self.read_roots = read_roots

    def _roots(self, read_roots: RootsArg) -> RootsArg:
        return read_roots if read_roots is not None else self.read_roots

    def sync(self, *, force: bool = False) -> bool:
        """Bring the local directory up to date; True when it changed."""
        return False

    def _sync_for_read(self) -> None:
        try:
            self.sync()
        except Exception as exc:  # noqa: BLE001
            logger.warning("skill mirror sync failed: %s", exc)

    @abstractmethod
    def propose_markdown(
        self,
        *,
        agent_id: str,
        tenant_id: str,
        markdown: str,
        revision_of: str | None = None,
        source: str = "learn",
        created_by: str = "",
        read_roots: RootsArg = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def propose_folder(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        files: Any,
        revision_of: str | None = None,
        source: str = "admin_upload",
        read_roots: RootsArg = None,
    ) -> dict[str, Any]: ...

    @abstractmethod
    def approve(self, *, skill_id: str, reviewer: str, read_roots: RootsArg = None) -> dict[str, Any]: ...

    @abstractmethod
    def reject(self, *, skill_id: str, reviewer: str, reason: str = "") -> dict[str, Any]: ...

    def list_drafts(self) -> list[dict[str, Any]]:
        self._sync_for_read()
        return authoring.list_drafts(self.root)

    def get_draft(self, skill_id: str, read_roots: RootsArg = None) -> dict[str, Any]:
        self._sync_for_read()
        return authoring.get_draft(self.root, skill_id, read_roots=self._roots(read_roots))

    def read_draft_file(self, skill_id: str, rel_path: str) -> dict[str, Any]:
        self._sync_for_read()
        return authoring.read_draft_file(self.root, skill_id, rel_path)

    def list_active(self) -> list[dict[str, Any]]:
        self._sync_for_read()
        catalog = SkillCatalog(self.root)
        catalog.load()
        live = [authoring._meta_public(m) for m in catalog.index.values() if authoring.is_live_skill_meta(m)]
        return sorted(live, key=lambda item: item["id"])


class LocalSkillRepository(SkillRepository):
    """Today's behavior: the managed directory is the source of truth, guarded by ``.skills.lock``."""

    def propose_markdown(self, *, read_roots: RootsArg = None, **kwargs: Any) -> dict[str, Any]:
        return authoring.propose_skill_draft(root=self.root, read_roots=self._roots(read_roots), **kwargs)

    def propose_folder(self, *, read_roots: RootsArg = None, **kwargs: Any) -> dict[str, Any]:
        return authoring.propose_skill_folder_draft(root=self.root, read_roots=self._roots(read_roots), **kwargs)

    def approve(self, *, skill_id: str, reviewer: str, read_roots: RootsArg = None) -> dict[str, Any]:
        return authoring.approve_draft(root=self.root, skill_id=skill_id, reviewer=reviewer, read_roots=self._roots(read_roots))

    def reject(self, *, skill_id: str, reviewer: str, reason: str = "") -> dict[str, Any]:
        return authoring.reject_draft(root=self.root, skill_id=skill_id, reviewer=reviewer, reason=reason)


class IndexedSkillRepository(SkillRepository):
    """Skill files in object storage, rows in ``skill_versions`` through the memory API.

    ``root`` is a mirror rebuilt from the index whenever the ``skills``
    generation moves. When ``root`` is a symlink (or missing) each rebuild
    writes a fresh folder and swaps the link; when it is a plain directory the
    files it materialized are updated in place.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        client: Any = None,
        objects: Any = None,
        read_roots: RootsArg = None,
        sync_interval: float = SYNC_INTERVAL_SECONDS,
        lock_wait: float = LOCK_WAIT_SECONDS,
        reuse_local: bool = True,
    ) -> None:
        super().__init__(root, read_roots=read_roots)
        self._client = client
        self._objects = objects
        self.sync_interval = sync_interval
        self.lock_wait = lock_wait
        self.reuse_local = reuse_local
        self.generation = -1
        self.rows: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._last_sync = float("-inf")

    @property
    def client(self) -> Any:
        if self._client is None:
            from ts_harness.memory.client import get_memory_client

            self._client = get_memory_client()
        return self._client

    @property
    def objects(self) -> Any:
        if self._objects is None:
            from ts_harness.storage.objects import object_store

            self._objects = object_store("skills")
        return self._objects

    # ── mirror ────────────────────────────────────────────────────────────

    def _swap_mode(self) -> bool:
        return self.root.is_symlink() or not self.root.exists()

    def sync(self, *, force: bool = False) -> bool:
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_sync < self.sync_interval:
                return False
            self._last_sync = now
            ready = self.root.is_dir()
            if ready and self.generation >= 0 and self.client.shared_files_generation(SKILLS_NAMESPACE) == self.generation:
                return False
            listing = self.client.list_skill_index(statuses=("active", "draft"))
            generation = int(listing.get("generation") or 0)
            rows = list(listing.get("skills") or [])
            if ready and generation == self.generation:
                return False
            self._materialize(rows, generation)
            self.rows = rows
            self.generation = generation
            return True

    def invalidate(self) -> None:
        with self._lock:
            self.generation = -1
            self._last_sync = float("-inf")

    def _drafts(self) -> dict[str, dict[str, Any]]:
        return {row["skill_id"]: row for row in self.rows if row.get("status") == "draft"}

    @staticmethod
    def _row_files(row: dict[str, Any]) -> Iterator[tuple[str, str, str]]:
        """(mirror path, sha256, object key) for every file of an index row."""
        if row["status"] == "draft":
            base = f"drafts/{authoring._safe_id(row['skill_id'])}"
        else:
            base = f"{row['category']}/{row['leaf']}"
        for item in row.get("files") or []:
            rel = f"{base}.md" if row.get("layout") == "file" else f"{base}/{item['path']}"
            yield rel, str(item["sha256"]), f"{row['object_prefix']}{item['path']}"

    def _fetch(self, rel: str, sha: str, key: str) -> bytes:
        if self.reuse_local:
            local = self.root / rel
            try:
                if local.is_file() and not local.is_symlink():
                    data = local.read_bytes()
                    if _sha256(data) == sha:
                        return data
            except OSError:
                pass
        return self.objects.get(key, expected_sha256=sha)

    def _materialize(self, rows: list[dict[str, Any]], generation: int) -> None:
        wanted: dict[str, bytes] = {}
        for row in rows:
            for rel, sha, key in self._row_files(row):
                wanted[rel] = self._fetch(rel, sha, key)
        if self._swap_mode():
            self._swap_in(wanted, generation)
        else:
            self._apply_in_place(wanted)

    def _swap_in(self, wanted: dict[str, bytes], generation: int) -> None:
        builds = self.root.parent / f".{self.root.name}-builds"
        builds.mkdir(parents=True, exist_ok=True)
        fresh = builds / f"g{generation}-{secrets.token_hex(4)}"
        try:
            for rel, data in wanted.items():
                target = fresh / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            fresh.mkdir(parents=True, exist_ok=True)
            link = self.root.parent / f".{self.root.name}-{secrets.token_hex(4)}.link"
            os.symlink(fresh, link, target_is_directory=True)
            os.replace(link, self.root)
        except BaseException:
            shutil.rmtree(fresh, ignore_errors=True)
            raise
        # Keep the previous build so readers holding paths into it can finish.
        keep = {fresh.name}
        previous = sorted((p for p in builds.iterdir() if p.name != fresh.name), key=lambda p: p.stat().st_mtime)
        if previous:
            keep.add(previous[-1].name)
        for path in builds.iterdir():
            if path.name not in keep:
                shutil.rmtree(path, ignore_errors=True)

    def _apply_in_place(self, wanted: dict[str, bytes]) -> None:
        manifest = self.root / _MANIFEST
        try:
            previous = set(json.loads(manifest.read_text(encoding="utf-8")).get("files") or [])
        except (OSError, ValueError):
            previous = set()
        for rel, data in wanted.items():
            target = self.root / rel
            if target.is_file() and target.read_bytes() == data:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=str(target.parent), prefix=".mirror-", delete=False) as handle:
                handle.write(data)
            Path(handle.name).replace(target)
        for rel in sorted(previous - set(wanted)):
            target = self.root / rel
            if target.is_file():
                target.unlink()
            parent = target.parent
            while parent != self.root and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        manifest.write_text(json.dumps({"files": sorted(wanted)}), encoding="utf-8")

    # ── writes ────────────────────────────────────────────────────────────

    def _acquire(self, holder: str) -> None:
        deadline = time.monotonic() + self.lock_wait
        delay = 0.05
        while not self.client.acquire_shared_lock(SKILLS_NAMESPACE, LOCK_NAME, holder=holder, ttl_seconds=LOCK_TTL_SECONDS):
            if time.monotonic() >= deadline:
                raise TimeoutError("timed out waiting for the skills authoring lock")
            time.sleep(delay)
            delay = min(delay * 2, 0.5)

    def _visible_files(self) -> dict[str, bytes]:
        return {rel: path.read_bytes() for rel, path in authoring._iter_tree(self.root).items()}

    def _restore_in_place(self, before: dict[str, bytes]) -> None:
        for rel, path in authoring._iter_tree(self.root).items():
            if rel not in before:
                path.unlink()
        for rel, data in before.items():
            target = self.root / rel
            if not target.is_file() or target.read_bytes() != data:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[None]:
        """Lock, rebuild the mirror from the index, run the body; put the mirror back if it fails."""
        holder = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self._acquire(holder)
        try:
            with self._lock:
                self.sync(force=True)
                before = None if self._swap_mode() else self._visible_files()
                try:
                    yield
                except BaseException:
                    if before is not None:
                        self._restore_in_place(before)
                    self.invalidate()
                    self._sync_quietly()
                    raise
                self._sync_quietly()
        finally:
            try:
                self.client.release_shared_lock(SKILLS_NAMESPACE, LOCK_NAME, holder=holder)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not release the skills authoring lock: %s", exc)

    def _sync_quietly(self) -> None:
        try:
            self.sync(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("skill mirror sync failed: %s", exc)

    @contextlib.contextmanager
    def _index_errors(self) -> Iterator[None]:
        from ts_harness.memory.client import MemoryApiConflict

        try:
            yield
        except MemoryApiConflict as exc:
            raise SkillAuthoringError(str(exc), code=exc.code or None) from exc

    def _delete_quietly(self, prefix: str | None) -> None:
        if not prefix:
            return
        try:
            self.objects.delete_prefix(prefix)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not delete skill objects under %s: %s", prefix, exc)

    def _push_draft(self, result: dict[str, Any], *, tenant_id: str, source: str, created_by: str) -> None:
        skill_id = str(result["skill_id"])
        layout = str(result["layout"])
        tree = _skill_files(Path(result["path"]), layout)
        parsed = authoring.validate_skill_markdown(tree[SKILL_ENTRYPOINT].decode("utf-8"))
        files = _file_entries(tree)
        prefix = f"skills/drafts/{authoring._safe_id(skill_id)}-{secrets.token_hex(6)}/"
        try:
            for rel, data in tree.items():
                self.objects.put(prefix + rel, data)
            with self._index_errors():
                out = self.client.upsert_skill_draft(
                    skill_id=skill_id,
                    version=str(parsed["version"]),
                    category=str(parsed["category"]),
                    leaf=skill_id.split("/")[-1],
                    layout=layout,
                    tenant_id=tenant_id,
                    revision_of=result.get("revision_of"),
                    files=files,
                    object_prefix=prefix,
                    content_sha=content_sha(files),
                    source=source,
                    created_by=created_by,
                )
        except BaseException:
            self._delete_quietly(prefix)
            raise
        self._delete_quietly(out.get("previous_prefix"))

    def propose_markdown(
        self,
        *,
        agent_id: str,
        tenant_id: str,
        markdown: str,
        revision_of: str | None = None,
        source: str = "learn",
        created_by: str = "",
        read_roots: RootsArg = None,
    ) -> dict[str, Any]:
        with self._transaction():
            result = authoring.propose_skill_draft(
                root=self.root,
                agent_id=agent_id,
                tenant_id=tenant_id,
                markdown=markdown,
                revision_of=revision_of,
                source=source,
                created_by=created_by,
                read_roots=self._roots(read_roots),
            )
            self._push_draft(result, tenant_id=tenant_id, source=source, created_by=created_by or agent_id)
        return result

    def propose_folder(
        self,
        *,
        actor_id: str,
        tenant_id: str,
        files: Any,
        revision_of: str | None = None,
        source: str = "admin_upload",
        read_roots: RootsArg = None,
    ) -> dict[str, Any]:
        with self._transaction():
            result = authoring.propose_skill_folder_draft(
                root=self.root,
                actor_id=actor_id,
                tenant_id=tenant_id,
                files=files,
                revision_of=revision_of,
                source=source,
                read_roots=self._roots(read_roots),
            )
            self._push_draft(result, tenant_id=tenant_id, source=source, created_by=actor_id)
        return result

    def _open_draft(self, skill_id: str) -> dict[str, Any]:
        draft = self._drafts().get(skill_id)
        if draft is None:
            raise SkillAuthoringError(f"draft not found: {skill_id}")
        return draft

    def approve(self, *, skill_id: str, reviewer: str, read_roots: RootsArg = None) -> dict[str, Any]:
        with self._transaction():
            draft = self._open_draft(skill_id)
            result = authoring.approve_draft(
                root=self.root, skill_id=skill_id, reviewer=reviewer, read_roots=self._roots(read_roots)
            )
            tree = _skill_files(Path(result["path"]), str(result["layout"]))
            files = _file_entries(tree)
            digest = content_sha(files)
            prefix = f"skills/{skill_id}/{draft['version']}-{digest[:12]}/"
            draft_shas = {item["path"]: item["sha256"] for item in draft.get("files") or []}
            for item in files:
                key = prefix + item["path"]
                existing = self.objects.head(key)
                if existing is not None and existing.sha256 == item["sha256"]:
                    continue
                if draft_shas.get(item["path"]) == item["sha256"]:
                    self.objects.copy(draft["object_prefix"] + item["path"], key)
                else:
                    self.objects.put(key, tree[item["path"]])
            with self._index_errors():
                self.client.approve_skill_draft(
                    draft["id"],
                    files=files,
                    object_prefix=prefix,
                    content_sha=digest,
                    reviewer=reviewer,
                    layout=result["layout"],
                )
            self._delete_quietly(draft["object_prefix"])
        return result

    def reject(self, *, skill_id: str, reviewer: str, reason: str = "") -> dict[str, Any]:
        with self._transaction():
            draft = self._open_draft(skill_id)
            result = authoring.reject_draft(root=self.root, skill_id=skill_id, reviewer=reviewer, reason=reason)
            with self._index_errors():
                self.client.reject_skill_draft(draft["id"], reviewer=reviewer, reason=reason)
            self._delete_quietly(draft["object_prefix"])
        return result

    def list_active(self) -> list[dict[str, Any]]:
        self._sync_for_read()
        return [row for row in self.rows if row.get("status") == "active"]


_INDEXED: dict[Path, IndexedSkillRepository] = {}
_INDEXED_GUARD = threading.Lock()


def indexed_skills_enabled() -> bool:
    from ts_harness.storage.backend import object_backend

    return object_backend() != "local"


def skill_repository() -> SkillRepository:
    """The repository for this process's managed skills directory."""
    from ts_harness.skills.roots import catalog_roots, managed_root

    root = managed_root()
    read_roots: list[str | Path] = list(catalog_roots())
    if not indexed_skills_enabled():
        return LocalSkillRepository(root, read_roots=read_roots)
    with _INDEXED_GUARD:
        repo = _INDEXED.get(root)
        if repo is None:
            repo = IndexedSkillRepository(root, read_roots=read_roots)
            _INDEXED[root] = repo
        return repo


def reset_skill_repositories() -> None:
    with _INDEXED_GUARD:
        _INDEXED.clear()


def sync_skill_mirror(*, force: bool = False) -> bool:
    """Rebuild the managed mirror if the index moved; never raises, readers keep the last good mirror."""
    if not indexed_skills_enabled():
        return False
    try:
        return skill_repository().sync(force=force)
    except Exception as exc:  # noqa: BLE001
        logger.warning("skill mirror sync failed: %s", exc)
        return False
