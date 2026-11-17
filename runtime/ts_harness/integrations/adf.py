"""A Markdown subset to Atlassian Document Format (ADF), and ADF back to Markdown-like text.

Supported: headings, paragraphs (single newlines are hard breaks), bullet, ordered
and checkbox lists, fenced code, block quotes, horizontal rules, pipe tables, and
inline bold, code and links. Nested lists are flattened.
"""

from __future__ import annotations

import itertools
import re
from typing import Any, Iterator

_FENCE = re.compile(r"^```\s*([\w+#.-]*)\s*$")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE = re.compile(r"^(?:-{3,}|\*{3,}|_{3,})$")
_TASK = re.compile(r"^[-*+]\s+\[([ xX])\]\s+(.*)$")
_BULLET = re.compile(r"^[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\d+[.)]\s+(.*)$")
_TABLE_SEPARATOR = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?$")
_INLINE = re.compile(
    r"\*\*(?P<strong>[^*\n]+)\*\*|`(?P<code>[^`\n]+)`|\[(?P<label>[^\]\n]+)\]\((?P<href>[^)\s]+)\)"
)
_LISTS = {"task": "taskList", "bullet": "bulletList", "ordered": "orderedList"}
_BLOCK_NODES = {"paragraph", "listItem", "panel"}


def _kind(line: str) -> str:
    stripped = line.strip()
    if not stripped:
        return "blank"
    if _FENCE.match(stripped):
        return "fence"
    if _HEADING.match(stripped):
        return "heading"
    if _RULE.match(stripped):
        return "rule"
    if _TASK.match(stripped):
        return "task"
    if _BULLET.match(stripped):
        return "bullet"
    if _ORDERED.match(stripped):
        return "ordered"
    if stripped.startswith(">"):
        return "quote"
    if stripped.startswith("|"):
        return "table"
    return "text"


def _text(value: str, *marks: dict[str, Any]) -> dict[str, Any]:
    node: dict[str, Any] = {"type": "text", "text": value}
    if marks:
        node["marks"] = list(marks)
    return node


def _inline(value: str) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    position = 0
    for match in _INLINE.finditer(value):
        if match.start() > position:
            nodes.append(_text(value[position : match.start()]))
        if match.group("strong") is not None:
            nodes.append(_text(match.group("strong"), {"type": "strong"}))
        elif match.group("code") is not None:
            nodes.append(_text(match.group("code"), {"type": "code"}))
        else:
            nodes.append(_text(match.group("label"), {"type": "link", "attrs": {"href": match.group("href")}}))
        position = match.end()
    if position < len(value):
        nodes.append(_text(value[position:]))
    return nodes


def _paragraph(value: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": _inline(value)}


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _table(rows: list[str]) -> dict[str, Any]:
    has_header = len(rows) > 1 and bool(_TABLE_SEPARATOR.match(rows[1].strip()))
    content = []
    for index, row in enumerate(rows):
        if _TABLE_SEPARATOR.match(row.strip()):
            continue
        cell_type = "tableHeader" if has_header and index == 0 else "tableCell"
        cells = [{"type": cell_type, "attrs": {}, "content": [_paragraph(cell)]} for cell in _cells(row)]
        content.append({"type": "tableRow", "content": cells})
    return {"type": "table", "content": content}


def _list(kind: str, lines: list[str], ids: Iterator[int]) -> dict[str, Any]:
    if kind == "task":
        items = []
        for line in lines:
            match = _TASK.match(line.strip())
            assert match
            state = "TODO" if match.group(1) == " " else "DONE"
            items.append({"type": "taskItem", "attrs": {"localId": f"task-{next(ids)}", "state": state}, "content": _inline(match.group(2))})
        return {"type": "taskList", "attrs": {"localId": f"task-list-{next(ids)}"}, "content": items}
    pattern = _BULLET if kind == "bullet" else _ORDERED
    items = []
    for line in lines:
        match = pattern.match(line.strip())
        assert match
        items.append({"type": "listItem", "content": [_paragraph(match.group(1))]})
    return {"type": _LISTS[kind], "content": items}


def _blocks(lines: list[str], ids: Iterator[int]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        kind = _kind(line)
        if kind == "blank":
            index += 1
        elif kind == "fence":
            fence = _FENCE.match(line.strip())
            assert fence
            body = []
            index += 1
            while index < len(lines) and lines[index].strip() != "```":
                body.append(lines[index])
                index += 1
            index += 1
            node: dict[str, Any] = {"type": "codeBlock"}
            if fence.group(1):
                node["attrs"] = {"language": fence.group(1)}
            if any(part.strip() for part in body):
                node["content"] = [_text("\n".join(body))]
            content.append(node)
        elif kind == "heading":
            heading = _HEADING.match(line.strip())
            assert heading
            content.append({"type": "heading", "attrs": {"level": len(heading.group(1))}, "content": _inline(heading.group(2).strip())})
            index += 1
        elif kind == "rule":
            content.append({"type": "rule"})
            index += 1
        else:
            group = []
            while index < len(lines) and _kind(lines[index]) == kind:
                group.append(lines[index])
                index += 1
            if kind in _LISTS:
                content.append(_list(kind, group, ids))
            elif kind == "quote":
                inner = [part.strip()[1:].removeprefix(" ") for part in group]
                content.append({"type": "blockquote", "content": _blocks(inner, ids)})
            elif kind == "table":
                content.append(_table(group))
            else:
                nodes: list[dict[str, Any]] = []
                for position, part in enumerate(group):
                    if position:
                        nodes.append({"type": "hardBreak"})
                    nodes.extend(_inline(part))
                content.append({"type": "paragraph", "content": nodes})
    return content


def adf_from_text(text: str | None) -> dict[str, Any]:
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    return {"type": "doc", "version": 1, "content": _blocks(lines, itertools.count(1))}


def _marked(node: dict[str, Any]) -> str:
    value = str(node.get("text") or "")
    for mark in node.get("marks") or []:
        kind = mark.get("type") if isinstance(mark, dict) else None
        if kind == "strong":
            value = f"**{value}**"
        elif kind == "code":
            value = f"`{value}`"
        elif kind == "link":
            href = str((mark.get("attrs") or {}).get("href") or "")
            value = f"[{value}]({href})" if href else value
    return value


def adf_to_text(node: Any) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    children = [child for child in node.get("content") or []]
    if kind == "text":
        return _marked(node)
    if kind == "hardBreak":
        return "\n"
    if kind in {"mention", "emoji"}:
        return str((node.get("attrs") or {}).get("text") or "")
    if kind == "rule":
        return "---\n\n"
    if kind == "codeBlock":
        language = str((node.get("attrs") or {}).get("language") or "")
        body = "".join(adf_to_text(child) for child in children)
        return f"```{language}\n{body}\n```\n\n"
    if kind == "orderedList":
        lines = [f"{number}. {adf_to_text(child).strip()}" for number, child in enumerate(children, 1)]
        return "\n".join(lines) + "\n\n"
    if kind == "taskItem":
        box = "x" if (node.get("attrs") or {}).get("state") == "DONE" else " "
        return f"- [{box}] {''.join(adf_to_text(child) for child in children).strip()}\n"
    if kind in {"tableCell", "tableHeader"}:
        return " | ".join(adf_to_text(child).strip() for child in children) + " | "
    inner = "".join(adf_to_text(child) for child in children)
    if kind == "heading":
        level = int((node.get("attrs") or {}).get("level") or 1)
        return f"{'#' * level} {inner.strip()}\n\n"
    if kind == "listItem":
        return f"- {inner.strip()}\n"
    if kind in {"bulletList", "taskList"}:
        return f"{inner.rstrip()}\n\n"
    if kind == "blockquote":
        quoted = "\n".join(f"> {line}" if line else ">" for line in inner.strip().split("\n"))
        return f"{quoted}\n\n"
    if kind == "tableRow":
        return f"| {inner.rstrip()}\n"
    if kind == "table":
        return f"{inner.rstrip()}\n\n"
    if kind in _BLOCK_NODES:
        return f"{inner.rstrip()}\n\n"
    return inner
