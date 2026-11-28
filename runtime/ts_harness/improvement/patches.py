"""Unified diffs for improvement proposals: parse, apply to the harness's own files, and say what
they touch (files, and ``## sections`` of Markdown skills) so the off-limits list can be checked.

Only the subset a model writes is supported: ``--- a/path`` / ``+++ b/path`` headers and ``@@``
hunks. A hunk applies where its old lines (context and removals) match exactly; the stated line
number only breaks ties. New files (``--- /dev/null``) are allowed; deleting files is not.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

MAX_DIFF_CHARS = 30000
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(ValueError):
    """The diff is malformed, touches a file it may not, or does not apply."""


@dataclass
class FilePatch:
    path: str
    new_file: bool = False
    hunks: list[tuple[int, list[str]]] = field(default_factory=list)  # (old start, raw lines)


def _clean_path(raw: str) -> str:
    text = raw.strip().split("\t", 1)[0].strip()
    if text == "/dev/null":
        return text
    text = re.sub(r"^[ab]/", "", text).removeprefix("./")
    if not text or text.startswith("/") or ".." in Path(text).parts:
        raise PatchError(f"unsafe path in diff: {raw.strip()[:120]}")
    return text


def parse(diff: str) -> list[FilePatch]:
    """The files and hunks of a unified diff; raises :class:`PatchError` when malformed."""
    text = str(diff or "")
    if len(text) > MAX_DIFF_CHARS:
        raise PatchError(f"diff is longer than {MAX_DIFF_CHARS} characters")
    lines = text.replace("\r\n", "\n").split("\n")
    patches: list[FilePatch] = []
    current: FilePatch | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- ") and index + 1 < len(lines) and lines[index + 1].startswith("+++ "):
            old, new = _clean_path(line[4:]), _clean_path(lines[index + 1][4:])
            if new == "/dev/null":
                raise PatchError("a proposal may not delete files")
            current = FilePatch(path=new, new_file=old == "/dev/null")
            patches.append(current)
            index += 2
            continue
        match = _HUNK.match(line)
        if match:
            if current is None:
                raise PatchError("hunk before a file header")
            old_count = int(match.group(2) or 1) if match.group(2) != "0" else 0
            new_count = int(match.group(4) or 1) if match.group(4) != "0" else 0
            body: list[str] = []
            index += 1
            seen_old = seen_new = 0
            while index < len(lines) and (seen_old < old_count or seen_new < new_count):
                raw = lines[index]
                if raw.startswith("\\"):
                    index += 1
                    continue
                tag = raw[:1] if raw else " "
                if tag not in {" ", "-", "+"}:
                    break
                body.append(raw if raw else " ")
                seen_old += tag in {" ", "-"}
                seen_new += tag in {" ", "+"}
                index += 1
            if not any(item[:1] in {"-", "+"} for item in body):
                raise PatchError("hunk changes nothing")
            current.hunks.append((int(match.group(1)), body))
            continue
        index += 1
    if not patches or not any(p.hunks for p in patches):
        raise PatchError("no file changes found in the diff")
    return patches


def apply_file(original: str, patch: FilePatch) -> str:
    """``original`` with ``patch``'s hunks applied, or :class:`PatchError`."""
    if patch.new_file:
        if original:
            raise PatchError(f"{patch.path} already exists")
        return "\n".join(item[1:] for hunk in patch.hunks for item in hunk[1] if item[:1] == "+") + "\n"
    trailing = original.endswith("\n")
    lines = original.split("\n")
    if trailing:
        lines = lines[:-1]
    offset = 0
    for start, body in patch.hunks:
        old = [item[1:] for item in body if item[:1] in {" ", "-"}]
        new = [item[1:] for item in body if item[:1] in {" ", "+"}]
        wanted = max(0, start - 1 + offset)
        spots = [i for i in range(len(lines) - len(old) + 1) if lines[i:i + len(old)] == old] if old else [wanted]
        if not spots:
            raise PatchError(f"a hunk does not apply to {patch.path} (near line {start})")
        spot = min(spots, key=lambda i: abs(i - wanted))
        lines[spot:spot + len(old)] = new
        offset += len(new) - len(old)
    return "\n".join(lines) + ("\n" if trailing or not lines else "")


def repo_root() -> Path:
    """The harness checkout: ``TS_HARNESS_REPO_DIR``, else the nearest parent with skills/ and config/workflows/."""
    configured = os.environ.get("TS_HARNESS_REPO_DIR", "").strip()
    if configured:
        return Path(configured)
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "skills").is_dir() and (parent / "config" / "workflows").is_dir():
            return parent
    return Path("/app")


def read_file(path: str, root: Path | None = None) -> str:
    target = (root or repo_root()) / path
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def apply(diff: str, *, root: Path | None = None, reader=None) -> dict[str, tuple[str, str]]:
    """``{path: (before, after)}`` for every file the diff changes; raises :class:`PatchError`."""
    read = reader or (lambda path: read_file(path, root))
    out: dict[str, tuple[str, str]] = {}
    for patch in parse(diff):
        before = out[patch.path][1] if patch.path in out else read(patch.path)
        if not before and not patch.new_file:
            raise PatchError(f"{patch.path} does not exist")
        after = apply_file(before, patch)
        out[patch.path] = (out[patch.path][0] if patch.path in out else before, after)
    return out


def sections(markdown: str) -> dict[str, str]:
    """``{"## title": body}`` of a Markdown file; text before the first ``##`` is under ``""``."""
    found: dict[str, list[str]] = {"": []}
    title = ""
    for line in markdown.split("\n"):
        if line.startswith("## "):
            title = line[3:].strip()
            found.setdefault(title, [])
        found[title].append(line)
    return {key: "\n".join(value) for key, value in found.items()}


def changed_sections(before: str, after: str) -> list[str]:
    """Titles of the ``##`` sections whose text differs (``""`` is the preamble and front matter)."""
    old, new = sections(before), sections(after)
    return sorted(key for key in set(old) | set(new) if old.get(key) != new.get(key))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
