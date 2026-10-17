"""Split governed planning markdown into docs/ files for coding backends."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


_HEADING_LINE = re.compile(r"^(#{1,3})\s+(\S+)(?:\s+(.*))?$")
_ID_TOKEN = re.compile(r"^([A-Za-z]+)-(\d+)\b")


def split_markdown_by_id(markdown: str, id_prefix: str) -> dict[str, str]:
    """Split markdown into sections keyed by ``{PREFIX}-NNN`` heading ids."""
    text = markdown or ""
    prefix = str(id_prefix or "").strip().upper()
    if not text.strip() or not prefix:
        return {}

    pattern = re.compile(
        rf"^#{{1,3}}\s+({re.escape(prefix)}-\d+)\b.*$",
        re.IGNORECASE | re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return {}

    sections: dict[str, str] = {}
    for idx, match in enumerate(matches):
        section_id = match.group(1).upper()
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if body:
            sections[section_id] = body
    return sections


def _slugify(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (title or "").strip().lower()).strip("-")
    return slug[:48]


def _filename_for_section(section_id: str, body: str) -> str:
    first = (body or "").splitlines()[0] if body else ""
    match = _HEADING_LINE.match(first.strip())
    title = ""
    if match:
        remainder = (match.group(3) or "").strip()
        # Drop the ID token if the heading was "FRD-001 Title"
        title = remainder
    slug = _slugify(title)
    if slug:
        return f"{section_id}-{slug}.md"
    return f"{section_id}.md"


def _preamble_before_ids(markdown: str, id_prefixes: tuple[str, ...]) -> str:
    text = markdown or ""
    if not text.strip():
        return ""
    patterns = [
        re.compile(
            rf"^#{{1,3}}\s+({re.escape(prefix)}-\d+)\b",
            re.IGNORECASE | re.MULTILINE,
        )
        for prefix in id_prefixes
    ]
    first_start: int | None = None
    for pattern in patterns:
        match = pattern.search(text)
        if match is None:
            continue
        if first_start is None or match.start() < first_start:
            first_start = match.start()
    if first_start is None or first_start == 0:
        return ""
    return text[:first_start].strip()


def _put_whole(files: dict[str, str], relative: str, content: Any) -> None:
    text = str(content or "").strip()
    if text:
        files[relative] = text if text.endswith("\n") else f"{text}\n"


def _put_split(
    files: dict[str, str],
    *,
    directory: str,
    markdown: str,
    id_prefix: str,
    unassigned_name: str = "_unassigned.md",
) -> None:
    sections = split_markdown_by_id(markdown, id_prefix)
    preamble = _preamble_before_ids(markdown, (id_prefix,))
    if preamble:
        files[f"{directory}/{unassigned_name}"] = (
            preamble if preamble.endswith("\n") else f"{preamble}\n"
        )
    for section_id, body in sections.items():
        name = _filename_for_section(section_id, body)
        path = f"{directory}/{name}"
        files[path] = body if body.endswith("\n") else f"{body}\n"


def _put_blueprint_splits(files: dict[str, str], markdown: str) -> None:
    text = markdown or ""
    if not text.strip():
        return
    # Split on any COMP-/FB- heading; route by prefix.
    pattern = re.compile(
        r"^#{1,3}\s+((?:COMP|FB)-\d+)\b.*$",
        re.IGNORECASE | re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    preamble = _preamble_before_ids(text, ("COMP", "FB"))
    if preamble:
        files["docs/foundry/components/_unassigned.md"] = (
            preamble if preamble.endswith("\n") else f"{preamble}\n"
        )
    if not matches:
        return
    for idx, match in enumerate(matches):
        section_id = match.group(1).upper()
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        if not body:
            continue
        name = _filename_for_section(section_id, body)
        if section_id.startswith("COMP-"):
            directory = "docs/foundry/components"
        else:
            directory = "docs/foundry/features"
        path = f"{directory}/{name}"
        files[path] = body if body.endswith("\n") else f"{body}\n"


def planning_docs_file_map(
    refinery: dict[str, Any] | None,
    foundry: dict[str, Any] | None,
    planner: dict[str, Any] | None,
    *,
    approved_prd_markdown: str | None = None,
    active_work_order_id: str | None = None,
) -> dict[str, str]:
    """Map planning artifact packs to relative ``docs/`` file paths."""
    del active_work_order_id  # reserved for callers that prioritize primary WO path
    files: dict[str, str] = {}
    refinery = refinery if isinstance(refinery, dict) else {}
    foundry = foundry if isinstance(foundry, dict) else {}
    planner = planner if isinstance(planner, dict) else {}

    _put_whole(files, "docs/prd/approved-prd.md", approved_prd_markdown)
    _put_whole(files, "docs/refinery/product-overview.md", refinery.get("product_overview_markdown"))
    _put_whole(files, "docs/refinery/requirements.md", refinery.get("requirements_markdown"))
    _put_whole(files, "docs/refinery/feature-tree.md", refinery.get("feature_tree_markdown"))
    if refinery.get("frds_markdown"):
        _put_split(
            files,
            directory="docs/refinery/features",
            markdown=str(refinery.get("frds_markdown") or ""),
            id_prefix="FRD",
        )

    _put_whole(
        files,
        "docs/foundry/architecture-summary.md",
        foundry.get("architecture_summary_markdown"),
    )
    if foundry.get("blueprints_markdown"):
        _put_blueprint_splits(files, str(foundry.get("blueprints_markdown") or ""))
    if foundry.get("architecture_decisions_markdown"):
        _put_split(
            files,
            directory="docs/foundry/architectural-decisions",
            markdown=str(foundry.get("architecture_decisions_markdown") or ""),
            id_prefix="ADR",
        )

    _put_whole(files, "docs/planner/context-graph.md", planner.get("context_graph_markdown"))
    _put_whole(
        files,
        "docs/planner/execution-phases.md",
        planner.get("execution_phases_markdown"),
    )
    if planner.get("work_orders_markdown"):
        _put_split(
            files,
            directory="docs/planner/work-orders",
            markdown=str(planner.get("work_orders_markdown") or ""),
            id_prefix="WO",
        )
    return files


def materialize_planning_docs(repo_dir: Path, files: dict[str, str]) -> list[str]:
    """Write relative ``docs/`` files into ``repo_dir``. Returns written paths."""
    root = Path(repo_dir).resolve()
    written: list[str] = []
    for relative, content in (files or {}).items():
        rel = str(relative or "").replace("\\", "/").lstrip("/")
        if not rel.startswith("docs/") or ".." in rel.split("/"):
            raise ValueError(f"planning docs path must stay under docs/: {relative!r}")
        target = (root / rel).resolve()
        if not str(target).startswith(str(root)):
            raise ValueError(f"planning docs path escapes repo: {relative!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        text = str(content or "")
        if text and not text.endswith("\n"):
            text = f"{text}\n"
        target.write_text(text, encoding="utf-8")
        written.append(rel)
    return written
