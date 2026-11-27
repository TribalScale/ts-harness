"""What a coding run does to the repository's files, as it happens, for the console's live feed.

Every few seconds the worker compares the checkout with what it saw last: files created, edited,
deleted, renamed, or put back as they were on the base. It watches the files themselves, not the
agent's tool calls, so it sees everything: the agent's edits, the shell commands it runs (a
formatter rewriting sixty files), and the harness's own changes (reverting a protected file,
merging the base). Each event carries the step the run was in and a diff excerpt of at most
``MAX_EXCERPT_LINES`` lines, with anything that looks like a secret masked before it leaves the
worker. Many files changed at once under one folder become one grouped event.

Events go to ``progress.emit_events``; the worker sends them to the memory API in batches.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

POLL_SECONDS = 15
MAX_EXCERPT_LINES = 200
MAX_FILE_BYTES = 400_000  # bigger files get counts but no excerpt
BULK_THRESHOLD = 20  # this many files changed in one tick under one folder become one event
MAX_PATHS_IN_BULK = 50
_SKIP_PARTS = {".git", "node_modules", ".ts-harness", "dist", "build", ".next", "coverage", "test-results", "playwright-report"}

# Masked in excerpts: the push's secret-scan fallback patterns plus common key=value forms.
_SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(
        r"(?i)((?:api[_-]?key|secret|token|password|passwd|client[_-]?secret|auth)[\"']?\s*[:=]\s*[\"']?)([^\s\"',]{8,})"
    ),
]


def mask_secrets(text: str) -> tuple[str, bool]:
    """``text`` with secret-looking values replaced by ``***``, and whether anything was masked."""
    masked = False
    for pattern in _SECRET_PATTERNS:
        if pattern.groups >= 2:
            text, count = pattern.subn(lambda m: m.group(1) + "***", text)
        else:
            text, count = pattern.subn("***", text)
        masked = masked or count > 0
    return text, masked


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def _skipped(path: str) -> bool:
    return any(part in _SKIP_PARTS for part in Path(path).parts)


def _read(repo: Path, path: str) -> bytes | None:
    try:
        return (repo / path).read_bytes()
    except OSError:
        return None


def _digest(data: bytes | None) -> str:
    return "" if data is None else hashlib.sha1(data).hexdigest()


def _text(data: bytes | None) -> list[str] | None:
    if data is None or len(data) > MAX_FILE_BYTES or b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", errors="replace").splitlines(keepends=True)


def diff_excerpt(before: bytes | None, after: bytes | None, path: str) -> dict[str, Any]:
    """``{additions, deletions, excerpt, truncated, binary, masked}`` for one file's change."""
    old, new = _text(before or b""), _text(after or b"")
    if old is None or new is None:
        return {"additions": 0, "deletions": 0, "excerpt": "", "truncated": False, "binary": True, "masked": False}
    lines = list(difflib.unified_diff(old, new, fromfile=f"a/{path}", tofile=f"b/{path}", n=2))
    additions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    truncated = len(lines) > MAX_EXCERPT_LINES
    excerpt, masked = mask_secrets("".join(lines[:MAX_EXCERPT_LINES]))
    return {
        "additions": additions,
        "deletions": deletions,
        "excerpt": excerpt,
        "truncated": truncated,
        "binary": False,
        "masked": masked,
    }


class FileFeed:
    """Polls a checkout and emits file events; ``step`` names the step the run is in now."""

    def __init__(
        self,
        repo_dir: Path,
        emit: Callable[[list[dict[str, Any]]], None],
        *,
        step: Callable[[], str] = lambda: "",
        clock: Callable[[], float] | None = None,
        poll_seconds: float = POLL_SECONDS,
    ):
        import time

        self.repo = Path(repo_dir)
        self.emit = emit
        self.step = step
        self.clock = clock or time.time
        self.poll_seconds = poll_seconds
        self._seen: dict[str, bytes | None] = {}  # path -> content last seen (None = absent)
        self._actor = "agent"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- state ---------------------------------------------------------------------------------

    def _dirty(self) -> set[str]:
        """Paths git reports as changed or untracked, plus every path seen changed before."""
        result = _git(self.repo, "status", "--porcelain", "--untracked-files=all", "-z")
        paths: set[str] = set()
        entries = (result.stdout or "").split("\0")
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if len(entry) < 4:
                continue
            code, path = entry[:2], entry[3:]
            if "R" in code or "C" in code:
                index += 1  # the rename's source path follows
            if path and not _skipped(path):
                paths.add(path)
        return paths | set(self._seen)

    def _base(self, path: str) -> bytes | None:
        shown = subprocess.run(
            ["git", "-C", str(self.repo), "show", f"HEAD:{path}"], capture_output=True, check=False
        )
        return shown.stdout if shown.returncode == 0 else None

    def start(self) -> "FileFeed":
        """Remember the checkout as it is now (nothing is reported for it) and start polling."""
        with self._lock:
            for path in self._dirty():
                self._seen[path] = _read(self.repo, path)
        self._thread = threading.Thread(target=self._loop, name="file-feed", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Report what changed since the last poll, then stop."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self.poll()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.poll()
            except Exception:  # noqa: BLE001 - the feed never stops a run
                pass

    @contextmanager
    def harness(self) -> Iterator[None]:
        """Changes made inside this block are the harness's (a revert, a base merge), not the agent's."""
        self.poll()
        self._actor = "harness"
        try:
            yield
        finally:
            self.poll()
            self._actor = "agent"

    # -- one tick ------------------------------------------------------------------------------

    def poll(self) -> list[dict[str, Any]]:
        with self._lock:
            events = self._tick()
        if events:
            try:
                self.emit(events)
            except Exception:  # noqa: BLE001
                pass
        return events

    def _tick(self) -> list[dict[str, Any]]:
        now = self.clock()
        step = str(self.step() or "")[:120]
        changes: list[tuple[str, bytes | None, bytes | None]] = []
        for path in sorted(self._dirty()):
            current = _read(self.repo, path)
            if path in self._seen:
                before = self._seen[path]
            else:
                before = self._base(path)
            if _digest(before) == _digest(current):
                self._seen[path] = current
                continue
            changes.append((path, before, current))
            self._seen[path] = current
        if not changes:
            return []
        events = self._renames(changes, now, step)
        renamed = {p for e in events for p in (e["path"], e.get("from_path"))}
        for path, before, current in changes:
            if path in renamed:
                continue
            if current is None:
                kind = "deleted"
            elif before is None:
                kind = "created"
            elif _digest(current) == _digest(self._base(path)) and self._base(path) is not None:
                kind = "restored"  # back to how it is on the base
            else:
                kind = "edited"
            events.append(
                {"kind": kind, "path": path, "at": now, "step": step, "actor": self._actor, **diff_excerpt(before, current, path)}
            )
        return self._group(events, now, step)

    def _renames(self, changes: list[tuple[str, bytes | None, bytes | None]], now: float, step: str) -> list[dict[str, Any]]:
        gone = {_digest(before): path for path, before, current in changes if current is None and before is not None}
        events = []
        for path, before, current in changes:
            if before is None and current is not None and _digest(current) in gone:
                events.append(
                    {
                        "kind": "renamed", "path": path, "from_path": gone.pop(_digest(current)), "at": now,
                        "step": step, "actor": self._actor, "additions": 0, "deletions": 0, "excerpt": "",
                        "truncated": False, "binary": False, "masked": False,
                    }
                )
        return events

    def _group(self, events: list[dict[str, Any]], now: float, step: str) -> list[dict[str, Any]]:
        """Many files under one folder in one tick (a formatter, a generator) become one event."""
        by_folder: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for event in events:
            folder = str(Path(event["path"]).parts[0]) if len(Path(event["path"]).parts) > 1 else "."
            by_folder.setdefault((folder, event["kind"]), []).append(event)
        out: list[dict[str, Any]] = []
        for (folder, kind), group in by_folder.items():
            if len(group) < BULK_THRESHOLD:
                out.extend(group)
                continue
            out.append(
                {
                    "kind": "bulk",
                    "bulk_kind": kind,
                    "path": folder if folder != "." else "(repository root)",
                    "paths": [e["path"] for e in group][:MAX_PATHS_IN_BULK],
                    "count": len(group),
                    "at": now,
                    "step": step,
                    "actor": self._actor,
                    "additions": sum(e.get("additions", 0) for e in group),
                    "deletions": sum(e.get("deletions", 0) for e in group),
                    "excerpt": "",
                    "truncated": False,
                    "binary": False,
                    "masked": False,
                }
            )
        return sorted(out, key=lambda e: e["path"])


def current_step() -> str:
    """The run's phase and step from ``progress``: "verification · running checks after fix pass 1"."""
    from ts_harness.implementation import progress

    snap = progress.snapshot() or {}
    phase, detail = str(snap.get("phase") or ""), str(snap.get("detail") or "")
    return " · ".join(part for part in (phase, detail) if part)
