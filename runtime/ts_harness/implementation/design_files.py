"""Design references in a coding job's workspace: images the agent opens and matches.

They are written to ``.ts-harness/design/`` (excluded from commits through the clone's
``.git/info/exclude``; see ``HARNESS_LOCAL_PATHS``), never into the team's repo.
"""

from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path
from typing import Any

DESIGN_DIR = ".ts-harness/design"
_DATA_URL = re.compile(r"^data:(image/(png|jpeg|gif|webp));base64,(.+)$", re.DOTALL)
_EXT = {"png": "png", "jpeg": "jpg", "gif": "gif", "webp": "webp"}


def materialize_design_refs(repo_dir: Path, refs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Write each reference's image; return ``[{id, title, path, summary, url}]`` for the prompt."""
    written: list[dict[str, Any]] = []
    folder = Path(repo_dir) / DESIGN_DIR
    for ref in refs or []:
        if not isinstance(ref, dict) or not ref.get("id"):
            continue
        match = _DATA_URL.match(str(ref.get("data_url") or ""))
        path = ""
        if match:
            try:
                data = base64.b64decode(match.group(3), validate=True)
            except (binascii.Error, ValueError):
                data = b""
            if data:
                folder.mkdir(parents=True, exist_ok=True)
                name = f"{re.sub(r'[^A-Za-z0-9]', '', str(ref['id']))}.{_EXT[match.group(2)]}"
                (folder / name).write_bytes(data)
                path = f"{DESIGN_DIR}/{name}"
        written.append(
            {
                "id": str(ref["id"]),
                "title": str(ref.get("title") or "design"),
                "path": path,
                "summary": str(ref.get("summary") or "").strip(),
                "url": str(ref.get("url") or ""),
            }
        )
    if written:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "README.md").write_text(render_design_prompt(written), encoding="utf-8")
    return written


def render_design_prompt(files: list[dict[str, Any]]) -> str:
    """Prompt section naming the design images and how to use them."""
    if not files:
        return ""
    lines = [
        "Design references for this work order (images of what to build; harness files, not part of your change):",
    ]
    for item in files:
        where = item["path"] or "(image unavailable; use the summary)"
        line = f"- {item['id']} {item['title']}: {where}"
        if item.get("url"):
            line += f" ({item['url']})"
        lines.append(line)
        for summary_line in item.get("summary", "").splitlines()[:8]:
            lines.append(f"  {summary_line}")
    lines.append(
        "Open each image and match its layout, text, components, colors and states in the UI you build. "
        "Where a design and the acceptance criteria disagree, follow the acceptance criteria and say so in your summary. "
        "Do not copy these images into the repo."
    )
    return "\n".join(lines) + "\n"
