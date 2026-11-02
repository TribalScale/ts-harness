"""Read-only repo context for a bug report or pasted planning content.

``review-agent`` and ``pm-agent`` cannot clone or call GitHub. ``gather_bug_context``
fetches the default-branch tree and lists it whole when it fits the character
budget. A larger tree is cut after the paths the text names and the paths that
best match its words. It also fetches the bodies of the named paths (or, when
none are named, of the closest matches).
"""

from __future__ import annotations

import base64
import json
import math
import re
from typing import Any, NamedTuple

from ts_harness.integrations.github_client import GitHubClient, parse_repo_slug
from ts_harness.workflows.expressions import resolve_value
from ts_harness.workflows.hooks._helpers import load_workflow_payload

TREE_CHAR_BUDGET = 60_000
DIRECTORY_LIMIT = 150
FILE_LIMIT = 8
MATCHED_FILE_LIMIT = 4
FILE_CHAR_LIMIT = 20_000

_PATH_RE = re.compile(r"(?<![\w@./-])((?:[\w.-]+/)+[\w.-]+(?:\.[\w.-]+)?)")
_WORD_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
_STOPWORDS = frozenset(
    "the and for with that this from are was were has have had not but its our you your they them their "
    "when then than what which who how why where there here into onto over under about after before "
    "should would could will can may must also only just any all each some one two add use used using "
    "new like more most other such very via per does did done make makes out yet none say says".split()
)
_BINARY_SUFFIXES = frozenset(
    "png jpg jpeg gif ico svg webp bmp pdf zip gz tgz tar jar woff woff2 ttf eot otf mp3 mp4 mov lock".split()
)


def _input_extras(task: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    for item in sorted(tasks, key=lambda row: str(row.get("created_at") or "")):
        if isinstance(item.get("input"), dict):
            extras.update(
                {key: value for key, value in item["input"].items() if not str(key).startswith("_") and value is not None}
            )
        output = item.get("output")
        if isinstance(output, dict):
            for key in ("repo", "default_branch"):
                if output.get(key) is not None:
                    extras[key] = output[key]
    if isinstance(task.get("input"), dict):
        extras.update(
            {key: value for key, value in task["input"].items() if not str(key).startswith("_") and value is not None}
        )
    return extras


def _report_text(task: dict[str, Any], tasks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for item in [task, *tasks]:
        payload = item.get("input")
        if not isinstance(payload, dict):
            continue
        for key in ("summary", "context", "bug_report"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value)
    return "\n".join(parts)


def _path_mentioned(text: str, path: str) -> int:
    pattern = re.compile(r"(?<![\w./-])" + re.escape(path) + r"(?![\w./-])")
    match = pattern.search(text)
    return match.start() if match else -1


def cited_paths(report: str, tree_paths: list[str]) -> list[str]:
    """Paths named in the report that exist in the fetched tree, earliest first."""
    text = str(report or "")
    known = {path for path in tree_paths if path}
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for match in _PATH_RE.finditer(text):
        path = match.group(1)
        if path in known and path not in seen:
            seen.add(path)
            found.append((match.start(), path))
    for path in tree_paths:
        if path in seen:
            continue
        index = _path_mentioned(text, path)
        if index >= 0:
            seen.add(path)
            found.append((index, path))
    found.sort(key=lambda item: item[0])
    return [path for _index, path in found[:FILE_LIMIT]]


def _stem(word: str) -> str:
    word = word.lower()
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _words(text: str) -> list[str]:
    return [
        stem
        for stem in (_stem(word) for word in _WORD_RE.findall(text))
        if len(stem) >= 3 and stem not in _STOPWORDS
    ]


def _path_terms(path: str) -> tuple[set[str], set[str]]:
    """Words of a path's directories and of its file name (extension dropped)."""
    *dirs, name = path.split("/")
    stem, dot, _suffix = name.rpartition(".")
    return set(_words(" ".join(dirs))), set(_words(stem if dot and stem else name))


class RankedPath(NamedTuple):
    path: str
    score: float
    matched: int
    in_name: bool
    is_test: bool


def ranked_paths(report: str, tree_paths: list[str]) -> list[RankedPath]:
    """Paths sharing words with the report, best first.

    A word scores by how rare it is in the tree, so one that every path carries
    (the package name, say) adds nothing. Words the report repeats, and words in
    the file name rather than a directory, count for more.
    """
    counts: dict[str, int] = {}
    for word in _words(report):
        counts[word] = counts.get(word, 0) + 1
    if not counts or not tree_paths:
        return []
    terms = {path: _path_terms(path) for path in tree_paths}
    frequency: dict[str, int] = {}
    for dirs, name in terms.values():
        for word in (dirs | name) & counts.keys():
            frequency[word] = frequency.get(word, 0) + 1
    total = len(tree_paths)
    ranked: list[RankedPath] = []
    for path, (dirs, name) in terms.items():
        matched = (dirs | name) & counts.keys()
        score = sum(
            math.log(total / frequency[word]) * (1 + math.log(counts[word])) * (2 if word in name else 1)
            for word in matched
        )
        if score > 0:
            ranked.append(
                RankedPath(path, score, len(matched), bool(name & counts.keys()), "test" in dirs | name)
            )
    ranked.sort(key=lambda item: -item.score)
    return ranked


def _within_budget(paths: list[str]) -> list[str]:
    """The leading paths whose JSON listing fits in ``TREE_CHAR_BUDGET`` characters."""
    kept: list[str] = []
    used = 2
    for path in paths:
        used += len(json.dumps(path)) + (2 if kept else 0)
        if used > TREE_CHAR_BUDGET:
            break
        kept.append(path)
    return kept


def _directories(tree_paths: list[str]) -> list[str]:
    """Directories of the tree, as deep as fits in ``DIRECTORY_LIMIT``."""
    for depth in (3, 2, 1):
        found = sorted({"/".join(path.split("/")[:level]) for path in tree_paths for level in range(1, min(depth, path.count("/")) + 1)})
        if len(found) <= DIRECTORY_LIMIT:
            return found
    return found[:DIRECTORY_LIMIT]


def _is_text_path(path: str) -> bool:
    _stem_part, dot, suffix = path.rsplit("/", 1)[-1].rpartition(".")
    return not dot or suffix.lower() not in _BINARY_SUFFIXES


def _tree_paths(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for entry in list(payload.get("tree") or []):
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        path = str(entry.get("path") or "").strip()
        if path:
            paths.append(path)
    return paths


def _decode_file(payload: dict[str, Any]) -> tuple[str, bool]:
    if str(payload.get("encoding") or "") != "base64" or not payload.get("content"):
        return "", True
    raw = base64.b64decode(str(payload.get("content")))
    text = raw.decode("utf-8", errors="replace")
    if len(text) > FILE_CHAR_LIMIT:
        return text[:FILE_CHAR_LIMIT], True
    return text, bool(payload.get("truncated"))


def gather_bug_context(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    github: GitHubClient | None = None,
) -> dict[str, Any]:
    workflow, tasks = load_workflow_payload(memory, str(task.get("workflow_id") or ""))
    extras = _input_extras(task, tasks)
    repo = str(
        resolve_value(config.get("repo"), tasks=tasks, workflow=workflow, extras=extras) or extras.get("repo") or ""
    ).strip()
    default_branch = str(
        resolve_value(config.get("default_branch"), tasks=tasks, workflow=workflow, extras=extras)
        or extras.get("default_branch")
        or ""
    ).strip()
    if not repo:
        raise ValueError("gather_bug_context resolved no repo")
    if not default_branch:
        raise ValueError("gather_bug_context resolved no default_branch")
    parsed = parse_repo_slug(repo)
    if parsed is None:
        raise ValueError(f"gather_bug_context cannot parse repo: {repo}")
    owner, name = parsed
    client = github or GitHubClient.from_env()
    tree_payload = client.get_tree(owner, name, default_branch, recursive=True)
    if not isinstance(tree_payload, dict):
        raise ValueError("gather_bug_context received no tree")  # noqa: TRY004
    all_paths = _tree_paths(tree_payload)
    report = _report_text(task, tasks)
    cited = cited_paths(report, all_paths)
    ranked = [item for item in ranked_paths(report, all_paths) if item.path not in cited]
    listed = set(cited) | {item.path for item in ranked}
    # Named paths, then the closest matches, then the rest, so a cut drops the least relevant.
    ordered = cited + [item.path for item in ranked] + [path for path in all_paths if path not in listed]
    paths = _within_budget(ordered)
    matched = [
        item.path
        for item in ranked
        if item.matched >= 2 and item.in_name and not item.is_test and _is_text_path(item.path)
    ]
    to_fetch = [(path, "named") for path in cited]
    if not cited:
        to_fetch += [(path, "matched") for path in matched[:MATCHED_FILE_LIMIT]]
    files: list[dict[str, Any]] = []
    for path, source in to_fetch:
        payload = client.get_file(owner, name, path, ref=default_branch)
        if not isinstance(payload, dict) or payload.get("type") not in (None, "file"):
            continue
        body, truncated = _decode_file(payload)
        files.append({"path": path, "body": body, "truncated": truncated, "source": source})
    return {
        "repo": repo,
        "default_branch": default_branch,
        "tree": paths,
        "tree_truncated": len(paths) < len(all_paths),
        "tree_total": len(all_paths),
        "directories": _directories(all_paths),
        "files": files,
    }
