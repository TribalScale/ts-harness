"""A directory tree kept in Postgres (through the memory API) and mirrored on local disk.

Readers keep reading plain files from the mirror; ``sync()`` pulls whatever
changed since the mirror's generation. Writers wrap their file code in
``transaction()``: take the shared lock, pull, run the code against the mirror,
then push every changed file under the given subpaths in one commit.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from ts_harness.storage.backend import shared_state_enabled

logger = logging.getLogger(__name__)

DEFINITIONS_NAMESPACE = "definitions"
CRON_NAMESPACE = "cron"
SYNC_INTERVAL_SECONDS = 2.0
LOCK_TTL_SECONDS = 60
LOCK_WAIT_SECONDS = 30.0
_STATE_FILE = ".shared-tree.json"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hidden(rel: Path) -> bool:
    return any(part.startswith(".") for part in rel.parts) or rel.name.endswith(".tmp")


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=".sync-", delete=False) as handle:
        handle.write(data)
        tmp = Path(handle.name)
    tmp.replace(path)


class SharedTree:
    def __init__(
        self,
        namespace: str,
        root: str | Path,
        client: Any = None,
        *,
        sync_interval: float = SYNC_INTERVAL_SECONDS,
        lock_wait: float = LOCK_WAIT_SECONDS,
    ) -> None:
        self.namespace = namespace
        self.root = Path(root)
        self._client = client
        self.sync_interval = sync_interval
        self.lock_wait = lock_wait
        self._lock = threading.RLock()
        self._local = threading.local()
        self._last_sync = float("-inf")
        self.generation = self._load_generation()

    @property
    def client(self) -> Any:
        if self._client is None:
            from ts_harness.memory.client import get_memory_client

            self._client = get_memory_client()
        return self._client

    def _state_path(self) -> Path:
        return self.root / _STATE_FILE

    def _load_generation(self) -> int:
        try:
            data = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if data.get("namespace") != self.namespace:
            return 0
        return int(data.get("generation") or 0)

    def _save_generation(self) -> None:
        payload = json.dumps({"namespace": self.namespace, "generation": self.generation}).encode()
        _write_atomic(self._state_path(), payload)

    def _target(self, rel: str) -> Path:
        target = (self.root / rel).resolve()
        if not target.is_relative_to(self.root.resolve()):
            raise ValueError(f"shared file escapes the mirror: {rel!r}")
        return target

    def local_files(self, subpaths: list[str] | None = None) -> dict[str, Path]:
        """Visible files under the mirror (or under ``subpaths``), keyed by relative path."""
        bases = [self.root / sub for sub in subpaths] if subpaths else [self.root]
        out: dict[str, Path] = {}
        for base in bases:
            candidates = [base] if base.is_file() else sorted(base.rglob("*")) if base.is_dir() else []
            for path in candidates:
                if not path.is_file() or path.is_symlink():
                    continue
                rel = path.relative_to(self.root)
                if not _hidden(rel):
                    out[rel.as_posix()] = path
        return out

    def _snapshot(self, subpaths: list[str]) -> dict[str, str]:
        return {rel: _sha256(path.read_bytes()) for rel, path in self.local_files(subpaths).items()}

    def sync(self, *, force: bool = False) -> bool:
        """Pull changes when the interval has passed (or ``force``); True when any file changed."""
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last_sync < self.sync_interval:
                return False
            self._last_sync = now
            out = self.client.pull_shared_files(self.namespace, since=self.generation)
            if int(out.get("generation") or 0) < self.generation:
                logger.warning(
                    "shared tree %s went back from generation %s to %s (database restore?); pulling everything",
                    self.namespace, self.generation, out.get("generation"),
                )
                out = self.client.pull_shared_files(self.namespace, since=0)
            return self._apply(out)

    def _apply(self, out: dict[str, Any]) -> bool:
        """Write pulled files into the mirror; a full pull also removes local files the store doesn't have."""
        generation = int(out.get("generation") or 0)
        full = int(out.get("since") or 0) == 0
        files = sorted(out.get("files") or [], key=lambda f: (Path(f["path"]).name == "manifest.json", f["path"]))
        changed = False
        seen: set[str] = set()
        for item in files:
            rel = str(item["path"])
            target = self._target(rel)
            if item.get("deleted"):
                if target.exists():
                    target.unlink()
                    changed = True
                continue
            seen.add(rel)
            data = str(item.get("content") or "").encode("utf-8")
            if target.is_file() and _sha256(target.read_bytes()) == _sha256(data):
                continue
            _write_atomic(target, data)
            changed = True
        if full and generation > 0:
            for rel, path in self.local_files().items():
                if rel not in seen:
                    path.unlink()
                    changed = True
        self.generation = generation
        self.root.mkdir(parents=True, exist_ok=True)
        self._save_generation()
        return changed

    def _acquire(self, lock_name: str, holder: str) -> None:
        deadline = time.monotonic() + self.lock_wait
        delay = 0.05
        while not self.client.acquire_shared_lock(self.namespace, lock_name, holder=holder, ttl_seconds=LOCK_TTL_SECONDS):
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for shared lock {self.namespace}/{lock_name}")
            time.sleep(delay)
            delay = min(delay * 2, 0.5)

    @contextlib.contextmanager
    def transaction(self, lock_name: str, subpaths: list[str], *, actor: str = "") -> Iterator[None]:
        """Run file code against a fresh mirror and push its changes under ``subpaths`` in one commit.

        Raises ``ConflictError`` if someone else changed those files since the pull;
        the mirror is put back to the stored state when the commit or the body fails.
        """
        if getattr(self._local, "depth", 0):
            self._local.depth += 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        holder = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        scope = [sub.strip("/") for sub in subpaths]
        self._acquire(lock_name, holder)
        self._local.depth = 1
        try:
            with self._lock:
                self.sync(force=True)
                base = self.generation
                before = {rel: path.read_bytes() for rel, path in self.local_files(scope).items()}
                try:
                    yield
                except BaseException:
                    self._restore(scope, before)
                    raise
                after = self._snapshot(scope)
                puts = []
                for rel, sha in sorted(after.items()):
                    if rel in before and _sha256(before[rel]) == sha:
                        continue
                    try:
                        content = (self.root / rel).read_text(encoding="utf-8")
                    except UnicodeDecodeError as exc:
                        self._restore(scope, before)
                        raise ValueError(f"shared files must be UTF-8 text: {rel}") from exc
                    puts.append({"path": rel, "content": content})
                deletes = sorted(set(before) - set(after))
                if puts or deletes:
                    try:
                        self._commit(base, puts, deletes, actor, scope)
                    except BaseException:
                        self._restore(scope, before)
                        self._sync_quietly()
                        raise
        finally:
            self._local.depth = 0
            try:
                self.client.release_shared_lock(self.namespace, lock_name, holder=holder)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not release shared lock %s/%s: %s", self.namespace, lock_name, exc)

    def _commit(self, base: int, puts: list[dict[str, str]], deletes: list[str], actor: str, scope: list[str]) -> None:
        from ts_harness.memory.client import MemoryApiConflict
        from ts_harness.workflows.versioned_definition_store import ConflictError

        try:
            self.client.commit_shared_files(
                self.namespace, base_generation=base, puts=puts, deletes=deletes, actor=actor, scope=scope
            )
        except MemoryApiConflict as exc:
            if exc.code == "stale_generation":
                raise ConflictError(f"{self.namespace} changed on another replica; reload and try again") from exc
            raise
        self._sync_quietly()

    def _restore(self, scope: list[str], before: dict[str, bytes]) -> None:
        """Put the files under ``scope`` back to their bytes from before the transaction body ran."""
        for rel, path in self.local_files(scope).items():
            if rel not in before:
                path.unlink()
        for rel, data in before.items():
            target = self.root / rel
            if not target.is_file() or target.read_bytes() != data:
                _write_atomic(target, data)

    def _sync_quietly(self) -> None:
        try:
            self.sync(force=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("shared tree %s sync failed: %s", self.namespace, exc)


_TREES: dict[tuple[str, Path], SharedTree] = {}
_TREES_GUARD = threading.Lock()


def shared_tree(namespace: str, root: str | Path, client: Any = None) -> SharedTree:
    key = (namespace, Path(root).resolve())
    with _TREES_GUARD:
        tree = _TREES.get(key)
        if tree is None:
            tree = SharedTree(namespace, root, client)
            _TREES[key] = tree
        return tree


def reset_shared_trees() -> None:
    with _TREES_GUARD:
        _TREES.clear()


def definitions_tree() -> SharedTree | None:
    """The shared tree behind ``TS_HARNESS_WORKFLOW_DEFINITION_STORE`` in Postgres mode, else None."""
    if not shared_state_enabled():
        return None
    root = (os.environ.get("TS_HARNESS_WORKFLOW_DEFINITION_STORE") or "").strip()
    if not root:
        return None
    return shared_tree(DEFINITIONS_NAMESPACE, root)


def definitions_tree_for(path: str | Path) -> tuple[SharedTree, str] | None:
    """``(tree, relative path)`` when ``path`` lies inside the shared definitions tree."""
    tree = definitions_tree()
    if tree is None:
        return None
    try:
        rel = Path(path).resolve().relative_to(tree.root.resolve())
    except ValueError:
        return None
    return tree, rel.as_posix()


def cron_tree() -> SharedTree | None:
    """The shared tree holding the cron file (``TS_HARNESS_CRON_CONFIG``) in Postgres mode, else None."""
    if not shared_state_enabled():
        return None
    from ts_harness.gateway.cron_store import cron_config_path

    return shared_tree(CRON_NAMESPACE, cron_config_path().parent)


def sync_cron(*, force: bool = False) -> bool:
    """Pull cron file changes; never raises, the scheduler keeps the jobs it has."""
    tree = cron_tree()
    if tree is None:
        return False
    try:
        return tree.sync(force=force)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cron mirror sync failed: %s", exc)
        return False


def sync_definitions(*, force: bool = False) -> bool:
    """Pull definition changes for readers; never raises, readers keep the last good mirror."""
    tree = definitions_tree()
    if tree is None:
        return False
    try:
        return tree.sync(force=force)
    except Exception as exc:  # noqa: BLE001
        logger.warning("definition mirror sync failed: %s", exc)
        return False
