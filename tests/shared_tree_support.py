"""In-memory stand-in for the shared-files routes, and a mixin that runs store suites on a SharedTree."""

from __future__ import annotations

import contextlib
import hashlib
import threading
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from ts_harness.memory.client import MemoryApiConflict
from ts_harness.storage.shared_tree import SharedTree


class FakeSharedFiles:
    """Same rules as memory_routes.shared_state: generations, tombstones, scoped staleness, lease locks."""

    def __init__(self) -> None:
        self.guard = threading.Lock()
        self.generations: dict[str, int] = {}
        self.files: dict[str, dict[str, dict[str, Any]]] = {}
        self.locks: dict[tuple[str, str], str] = {}
        self.commits: list[dict[str, Any]] = []

    def shared_files_generation(self, namespace: str) -> int:
        return self.generations.get(namespace, 0)

    def pull_shared_files(self, namespace: str, *, since: int = 0) -> dict[str, Any]:
        with self.guard:
            generation = self.generations.get(namespace, 0)
            rows = self.files.get(namespace, {})
            files = [
                {"path": path, **row, "content": None if row["deleted"] else row["content"]}
                for path, row in sorted(rows.items())
                if row["generation"] > since and (since > 0 or not row["deleted"])
            ]
            return {"namespace": namespace, "generation": generation, "since": since, "files": files}

    def commit_shared_files(
        self,
        namespace: str,
        *,
        base_generation: int | None,
        puts: list[dict[str, str]],
        deletes: list[str],
        actor: str = "",
        scope: list[str] | None = None,
    ) -> dict[str, Any]:
        with self.guard:
            current = self.generations.get(namespace, 0)
            rows = self.files.setdefault(namespace, {})
            if base_generation is not None:
                if scope:
                    latest = max(
                        (r["generation"] for p, r in rows.items() if any(p == s or p.startswith(f"{s}/") for s in scope)),
                        default=0,
                    )
                    stale = latest > base_generation
                else:
                    stale = current != base_generation
                if stale:
                    raise MemoryApiConflict("stale", {"code": "stale_generation", "generation": current})
            if not puts and not deletes:
                return {"namespace": namespace, "generation": current, "changed": 0}
            generation = current + 1
            self.generations[namespace] = generation
            for item in puts:
                content = item["content"]
                rows[item["path"]] = {
                    "content": content,
                    "sha256": hashlib.sha256(content.encode()).hexdigest(),
                    "size": len(content.encode()),
                    "generation": generation,
                    "deleted": False,
                }
            for path in deletes:
                if path in rows and not rows[path]["deleted"]:
                    rows[path] = {"content": "", "sha256": "", "size": 0, "generation": generation, "deleted": True}
            self.commits.append({"namespace": namespace, "puts": [p["path"] for p in puts], "deletes": list(deletes), "actor": actor})
            return {"namespace": namespace, "generation": generation, "changed": len(puts) + len(deletes)}

    def acquire_shared_lock(self, namespace: str, name: str, *, holder: str, ttl_seconds: int = 60) -> bool:
        with self.guard:
            current = self.locks.get((namespace, name))
            if current not in (None, holder):
                return False
            self.locks[(namespace, name)] = holder
            return True

    def release_shared_lock(self, namespace: str, name: str, *, holder: str) -> bool:
        with self.guard:
            if self.locks.get((namespace, name)) != holder:
                return False
            del self.locks[(namespace, name)]
            return True

    def live_files(self, namespace: str) -> dict[str, str]:
        return {p: r["content"] for p, r in self.files.get(namespace, {}).items() if not r["deleted"]}


class CheckedSharedTree(SharedTree):
    """After every outermost transaction, the stored files under its scope must equal the mirror's."""

    @contextlib.contextmanager
    def transaction(self, lock_name: str, subpaths: list[str], *, actor: str = "") -> Iterator[None]:
        outermost = not getattr(self._local, "depth", 0)
        with super().transaction(lock_name, subpaths, actor=actor):
            yield
        if outermost:
            scope = [s.strip("/") for s in subpaths]
            stored = {
                p: c for p, c in self.client.live_files(self.namespace).items()
                if any(p == s or p.startswith(f"{s}/") for s in scope)
            }
            local = {rel: path.read_text(encoding="utf-8") for rel, path in self.local_files(scope).items()}
            if stored != local:
                raise AssertionError(
                    f"shared tree out of step with mirror: only stored {sorted(set(stored) - set(local))}, "
                    f"only local {sorted(set(local) - set(stored))}, "
                    f"different {sorted(p for p in set(stored) & set(local) if stored[p] != local[p])}"
                )


class SharedTreeBackendMixin:
    """Mix in ahead of a store test case: every store root gets its own CheckedSharedTree and fake store."""

    shared_tree_skip: frozenset[str] = frozenset()

    def setUp(self) -> None:
        if self._testMethodName in self.shared_tree_skip:
            self.skipTest("uses separate processes, which cannot share the in-memory fake")
        self.shared_trees: dict[Path, CheckedSharedTree] = {}
        guard = threading.Lock()

        def tree_for(root: Any) -> tuple[SharedTree, str]:
            key = Path(root).resolve()
            with guard:
                tree = self.shared_trees.get(key)
                if tree is None:
                    tree = CheckedSharedTree("definitions", key, FakeSharedFiles())
                    self.shared_trees[key] = tree
            return tree, "."

        patcher = mock.patch("ts_harness.storage.shared_tree.definitions_tree_for", side_effect=tree_for)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()  # type: ignore[misc]
