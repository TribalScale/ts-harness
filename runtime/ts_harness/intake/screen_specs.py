"""Screen specs drafted from Figma frames, for the requester to correct at intake.

A design shows layout, copy and the states someone drew; it does not show what an action does,
where data comes from, the rules behind a screen or how it adapts to a phone. Planning and
coding agents otherwise guess those. So, after ``gather_sources``, the PRD workflow drafts one
spec per **screen** (frames grouped by name: "Hub / Schedule / 02 Week · Base" and "… · Pilot"
are the screen "Hub / Schedule / 02 Week"):

- what is visible, read by a vision model from up to ``FRAMES_PER_SCREEN`` frame images and
  the frames' text: purpose, layout, components, data shown, actions, states drawn;
- what is not visible, as questions with a recommended answer (``S1``, ``S2``…).

The specs are posted in the thread (a Google Doc when long). Intake drops the screen
questions the sources already answer and asks the rest next to its own; the answers and the
specs go to the PRD draft and every revision.

When the sources include a design **inventory** (a Markdown table of screens with a number
column and a ``Screen`` column, as a designer or a Claude export writes it), its numbers group
the frames ("02 Week" in a frame name belongs to screen 02), each screen's row (purpose, states)
goes to the vision model as the designer's intent, and every row marked "partial" (designed
with a known gap) becomes a high-impact question, asked first.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

logger = logging.getLogger(__name__)

FRAMES_PER_SCREEN = 4
QUESTIONS_PER_SCREEN = 4
_IMAGE_BYTES_PER_SCREEN = 4_000_000
_WORKERS = 4
_LIST_LIMIT = 12
# "Area / Screen / NN Name · Variant" (or " — Variant"): the variant is what differs between frames.
_VARIANT = re.compile(r"\s+(?:·|—|–|\|)\s+.*$")

PROMPT = """You are documenting one screen of a product design for the engineers who will build it.
The images are frames of the screen "{screen}": {frames}. The text found in each frame follows the images.
{intent}
Return JSON only, no prose:
{{"purpose": "one sentence", "users": "who uses it, as the screen shows (or empty)",
 "layout": ["regions, top to bottom"], "components": ["controls and elements"],
 "data_shown": [{{"field": "name", "example": "value shown"}}],
 "actions": [{{"control": "button, link, drag...", "appears_to": "what its label or placement suggests"}}],
 "states": ["each state shown, with its frame id (D…)"],
 "questions": [{{"question": "…?", "recommended": "a concrete answer and why, in a few words", "impact": "high|medium|low"}}]}}

Rules:
- Describe only what is visible in the frames. Never invent behaviour.
- Put what you cannot see in "questions": what an action does beyond its label, where data comes
  from and how fresh it is, rules and validation, who may do what, what happens on failure or with
  no data, and how the screen works on a tablet and a phone. At most {max_questions} questions,
  the most consequential first, each answerable with "yes" to accept the recommendation.
- "impact" is how much the build changes if the recommendation is wrong: "high" for data,
  rules, permissions, what an action does; "low" for wording, icons and tooltips.
- At most {list_limit} items per list. Use the screen's own words for labels."""

INTENT = """
The design team's inventory describes this screen. Treat it as their intent: use its purpose and
states, do not ask what it already answers, and ask where it and the frames disagree. Still list
only what the frames show as visible facts.
{row}
"""


def screen_name(title: str) -> str:
    """The screen a frame belongs to: its title without the variant part."""
    return _VARIANT.sub("", str(title or "").strip()).strip() or str(title or "Screen").strip()


def group_screens(refs: list[dict[str, Any]], inventory: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """``[{screen, frames, inventory?}]`` in the order screens first appear; only Figma frames.

    With an inventory, a frame whose name has a path segment starting with an inventory number
    ("Hub / Schedule / 02 Week · Base" → 02) joins that screen; other frames group by name.
    """
    rows = {_number_key(row["number"]): row for row in (inventory or {}).get("screens") or []}
    screens: dict[str, dict[str, Any]] = {}
    for ref in refs or []:
        if not isinstance(ref, dict) or ref.get("source") != "figma" or not ref.get("id"):
            continue
        row = _inventory_row(str(ref.get("title") or ""), rows)
        if row is not None:
            key = f"inventory:{_number_key(row['number'])}"
            label = f"{row['number']} {row['name']}" + (f" ({row['app']})" if row.get("app") else "")
            entry = screens.setdefault(key, {"screen": label, "frames": [], "inventory": row})
        else:
            name = screen_name(str(ref.get("title") or ""))
            entry = screens.setdefault(name, {"screen": name, "frames": []})
        entry["frames"].append(ref)
    return list(screens.values())


_SEGMENT_NUMBER = re.compile(r"^([A-Za-z]?\d{1,3})\b")


def _number_key(number: str) -> str:
    """"02" and "2" are one screen; "T1" stays "T1"."""
    text = str(number or "").strip().upper()
    if text.isdigit():
        return text.lstrip("0") or "0"
    return text


def _inventory_row(title: str, rows: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    for segment in str(title or "").split("/"):
        match = _SEGMENT_NUMBER.match(segment.strip())
        if match and _number_key(match.group(1)) in rows:
            return rows[_number_key(match.group(1))]
    return None


# Inventory: Markdown tables. A screens table has a number column ("#", "No", "ID") and a
# "Screen" column; a row whose item is marked "partial" is a known gap.
_PARTIAL = re.compile(r"\bpartial\b", re.IGNORECASE)


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def parse_inventory(text: str) -> dict[str, Any] | None:
    """``{screens: [{number, name, purpose, states, app}], gaps: [{item, detail, section}]}``, or None."""
    screens: list[dict[str, Any]] = []
    gaps: list[dict[str, str]] = []
    heading = ""
    header: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            header = []
            continue
        if not line.startswith("|"):
            header = []
            continue
        cells = _table_cells(line)
        if not header:
            header = [c.lower() for c in cells]
            continue
        if all(set(c) <= set("-: ") for c in cells):
            continue
        row = dict(zip(header, cells))
        number_col = next((h for h in header if h in {"#", "no", "no.", "number", "id"}), None)
        if number_col and "screen" in header and row.get(number_col) and row.get("screen"):
            screens.append(
                {
                    "number": row[number_col],
                    "name": row["screen"],
                    "purpose": next((row[h] for h in header if h.startswith("what") or h in {"purpose", "description"}), ""),
                    "states": row.get("states", ""),
                    "app": heading,
                }
            )
        # A gap is marked on the item ("Publish the week (F-14, partial)") or in its own cell
        # ("Partial"); "partial" inside a description ("posts as partial") is not one.
        if cells and (_PARTIAL.search(cells[0]) or any(c.strip(" .").lower() == "partial" for c in cells[1:])):
            first = cells[0]
            gaps.append({"item": first, "detail": " ".join(c for c in cells[1:] if c), "section": heading})
    return {"screens": screens, "gaps": gaps} if screens else None


def find_inventory(sources: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The first gathered source that reads as a screen inventory."""
    for source in sources or []:
        if isinstance(source, dict) and not source.get("error"):
            found = parse_inventory(str(source.get("text") or ""))
            if found:
                return {**found, "source": str(source.get("name") or "inventory")}
    return None


def gap_questions(inventory: dict[str, Any] | None) -> list[dict[str, str]]:
    """A high-impact question per "partial" inventory row: designed with a known gap."""
    out = []
    for gap in (inventory or {}).get("gaps") or []:
        item = re.sub(r"\s+", " ", gap["item"]).strip(" .")
        detail = re.sub(r"\s+", " ", gap["detail"]).strip()
        out.append(
            {
                "question": f"{item} is marked partial in the design inventory ({detail[:300]}). Should v1 build what is "
                "designed and leave the gap open until it is decided?",
                "recommended": "Yes; the PRD lists the gap as an open question with its owner, and no ticket builds it",
                "impact": "high",
            }
        )
    return out


def _parse_json(text: str) -> dict[str, Any]:
    body = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", str(text or "").strip())
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    data = json.loads(body[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("the reply is not a JSON object")
    return data


def _strings(value: Any, limit: int = _LIST_LIMIT) -> list[str]:
    items = value if isinstance(value, list) else []
    return [str(item).strip() for item in items if str(item).strip()][:limit]


def _pairs(value: Any, keys: tuple[str, str], limit: int = _LIST_LIMIT) -> list[dict[str, str]]:
    items = value if isinstance(value, list) else []
    out = []
    for item in items:
        if isinstance(item, dict) and str(item.get(keys[0]) or "").strip():
            out.append({keys[0]: str(item.get(keys[0])).strip(), keys[1]: str(item.get(keys[1]) or "").strip()})
    return out[:limit]


def _message(api_mode: str, prompt: str, images: list[tuple[str, bytes]]) -> dict[str, Any]:
    if api_mode == "anthropic":
        content: list[dict[str, Any]] = [
            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode()}}
            for mime, data in images
        ]
        content.append({"type": "text", "text": prompt})
    else:
        content = [{"type": "text", "text": prompt}]
        content += [
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}
            for mime, data in images
        ]
    return {"role": "user", "content": content}


Describe = Callable[[str, list[tuple[str, bytes]]], str]


def build_describer(*, agent_id: str = "pm-agent", scope: dict[str, Any] | None = None) -> Describe | None:
    """``describe(prompt, images) -> reply text`` with the intake vision model, or None when off."""
    from ts_harness.intake.connectors import vision_model

    model = vision_model(agent_id)
    if not model:
        return None
    from ts_harness.agent.provider import build_provider

    def describe(prompt: str, images: list[tuple[str, bytes]]) -> str:
        provider = build_provider(model, scope=scope)
        api_mode = str(getattr(getattr(provider, "endpoint", None), "api_mode", "openai"))
        response = provider.chat([_message(api_mode, prompt, images)], temperature=0)
        return str(((response.get("choices") or [{}])[0].get("message") or {}).get("content") or "")

    return describe


def _frame_images(frames: list[dict[str, Any]], store: Any) -> list[tuple[str, bytes]]:
    images: list[tuple[str, bytes]] = []
    used = 0
    for ref in frames[:FRAMES_PER_SCREEN]:
        key = str(ref.get("object_key") or "")
        if not key or store is None:
            continue
        try:
            data = store.get(key)
        except Exception as exc:  # noqa: BLE001 - the frame's text still goes to the model
            logger.info("screen spec image %s unavailable: %s", key, exc)
            continue
        if used + len(data) > _IMAGE_BYTES_PER_SCREEN:
            break
        used += len(data)
        images.append((str(ref.get("mime") or "image/png"), data))
    return images


def describe_screen(screen: dict[str, Any], *, describe: Describe, store: Any) -> dict[str, Any]:
    """One spec: what the frames show, and questions for what they don't."""
    frames = screen["frames"]
    shown = frames[:FRAMES_PER_SCREEN]
    frame_list = ", ".join(f"{f['id']} \"{f.get('title')}\"" for f in shown)
    texts = "\n\n".join(f"{f['id']} {f.get('title')}:\n{str(f.get('summary') or '')[:3000]}" for f in shown)
    row = screen.get("inventory")
    intent = INTENT.format(row=f"- Purpose: {row.get('purpose') or '—'}\n- States: {row.get('states') or '—'}") if row else ""
    prompt = PROMPT.format(
        screen=screen["screen"], frames=frame_list, intent=intent, max_questions=QUESTIONS_PER_SCREEN, list_limit=_LIST_LIMIT
    )
    spec: dict[str, Any] = {
        "screen": screen["screen"],
        "frames": [{"id": f["id"], "title": f.get("title"), "url": f.get("url")} for f in frames],
    }
    if row:
        spec["inventory"] = {k: row.get(k, "") for k in ("number", "name", "purpose", "states", "app")}
    try:
        data = _parse_json(describe(f"{prompt}\n\nFrame text:\n{texts}", _frame_images(shown, store)))
    except Exception as exc:  # noqa: BLE001 - one screen's failure leaves the others
        logger.warning("screen spec for %s failed: %s", screen["screen"], exc)
        spec["error"] = f"could not be drafted: {str(exc)[:200]}"
        return spec
    spec.update(
        purpose=str(data.get("purpose") or "").strip(),
        users=str(data.get("users") or "").strip(),
        layout=_strings(data.get("layout")),
        components=_strings(data.get("components")),
        data_shown=_pairs(data.get("data_shown"), ("field", "example")),
        actions=_pairs(data.get("actions"), ("control", "appears_to")),
        states=_strings(data.get("states")),
        questions=_questions(data.get("questions")),
    )
    return spec


_IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2}


def _questions(value: Any) -> list[dict[str, str]]:
    found = _pairs(value, ("question", "recommended"), limit=QUESTIONS_PER_SCREEN)
    raw = value if isinstance(value, list) else []
    for question, item in zip(found, (i for i in raw if isinstance(i, dict) and str(i.get("question") or "").strip())):
        impact = str(item.get("impact") or "medium").strip().lower()
        question["impact"] = impact if impact in _IMPACT_ORDER else "medium"
    return found


def max_screens() -> int:
    try:
        return max(1, int(os.environ.get("TS_HARNESS_SCREEN_SPECS_MAX", "") or 40))
    except ValueError:
        return 40


def draft_screen_specs(
    refs: list[dict[str, Any]], *, describe: Describe | None, store: Any, inventory: dict[str, Any] | None = None
) -> dict[str, Any]:
    """``{screen_specs, screen_questions, screen_specs_text, summary, skipped}`` for the design refs."""
    screens = group_screens(refs, inventory)
    if not screens or describe is None:
        reason = "no Figma frames" if not screens else "image reading is off (TS_HARNESS_INTAKE_VISION_MODEL)"
        return {"skipped": True, "reason": reason, "screen_specs": [], "screen_questions": [], "screen_specs_text": ""}
    capped = screens[: max_screens()]
    with ThreadPoolExecutor(max_workers=min(_WORKERS, len(capped))) as pool:
        specs = list(pool.map(lambda s: describe_screen(s, describe=describe, store=store), capped))
    number = 0
    ranked: list[tuple[int, int, str]] = []
    # Known gaps from the inventory come first: S1… and high impact.
    gaps = gap_questions(inventory)
    if gaps:
        gap_spec = {"id": "SC0", "screen": "Known gaps (from the design inventory)", "frames": [], "questions": gaps, "gaps": True}
        for question in gaps:
            number += 1
            question["id"] = f"S{number}"
            line = f"S{number}. {question['question']} Recommended: {question['recommended']}."
            ranked.append((-1, number, line))
        specs = [gap_spec, *specs]
    for index, spec in enumerate([s for s in specs if not s.get("gaps")], start=1):
        spec["id"] = f"SC{index}"
        for question in spec.get("questions") or []:
            number += 1
            question["id"] = f"S{number}"
            text = question["question"].rstrip()
            rec = question.get("recommended") or ""
            line = f"S{number}. {spec['screen']}: {text}" + (f" Recommended: {rec.rstrip('.')}." if rec else "")
            ranked.append((_IMPACT_ORDER.get(question.get("impact", "medium"), 1), number, line))
    # Highest impact first (then in screen order): intake asks the first ones, the rest keep
    # their recommended answers.
    questions = [line for _impact, _number, line in sorted(ranked)]
    notes = []
    if inventory:
        matched = sum(1 for s in capped if s.get("inventory"))
        notes.append(
            f"Screens grouped by the design inventory ({inventory.get('source') or 'attached'}): "
            f"{matched} of {len(capped)} screens matched its numbers."
        )
    if len(screens) > len(capped):
        notes.append(f"{len(screens) - len(capped)} more screens were not specified (TS_HARNESS_SCREEN_SPECS_MAX).")
    failed = [s["screen"] for s in specs if s.get("error")]
    if failed:
        notes.append("Not drafted: " + ", ".join(failed) + ".")
    return {
        "skipped": False,
        "screen_specs": specs,
        "screen_questions": questions,
        "screen_specs_text": render_for_agents(specs),
        "summary": render_document(specs, notes),
    }


def _frames_line(spec: dict[str, Any]) -> str:
    return ", ".join(f"[{f['id']} {f.get('title')}]({f['url']})" if f.get("url") else f"{f['id']} {f.get('title')}" for f in spec["frames"])


def render_document(specs: list[dict[str, Any]], notes: list[str] | None = None) -> str:
    """The screen specs for people: one section per screen, its frames linked."""
    count = sum(len(s.get("questions") or []) for s in specs)
    screen_count = sum(1 for s in specs if not s.get("gaps"))
    lines = [
        "# Screen specs",
        "",
        f"Drafted from the Figma frames: {screen_count} screens. Each lists what its frames show, then questions "
        f"for what they don't ({count} in all, S1–S{count}, rated by impact). The intake asks the highest-impact "
        "ones the sources don't answer; every other question keeps its recommended answer unless you answer it "
        "by S-number. Correct a spec with its SC-number (\"SC3: …\").",
        "",
    ]
    lines += [f"_{note}_" for note in notes or []]
    for spec in specs:
        lines += ["", f"## {spec['id']} · {spec['screen']}", ""]
        if spec.get("frames"):
            lines += [f"**Frames:** {_frames_line(spec)}", ""]
        if spec.get("inventory"):
            row = spec["inventory"]
            lines += [f"**Inventory:** {row.get('purpose') or '—'} States: {row.get('states') or '—'}", ""]
        if spec.get("error"):
            lines.append(f"_This screen {spec['error']}._")
            continue
        if spec.get("purpose"):
            lines += [f"**Purpose:** {spec['purpose']}", ""]
        if spec.get("users"):
            lines += [f"**Users:** {spec['users']}", ""]
        for title, key in (("Layout", "layout"), ("Components", "components"), ("States shown", "states")):
            if spec.get(key):
                lines += [f"**{title}**", ""] + [f"- {item}" for item in spec[key]] + [""]
        if spec.get("data_shown"):
            lines += ["**Data shown**", "", "| Field | Example |", "|---|---|"]
            lines += [f"| {d['field']} | {d['example']} |" for d in spec["data_shown"]] + [""]
        if spec.get("actions"):
            lines += ["**Actions**", "", "| Control | Appears to |", "|---|---|"]
            lines += [f"| {a['control']} | {a['appears_to']} |" for a in spec["actions"]] + [""]
        if spec.get("questions"):
            lines += ["**Questions**", ""]
            lines += [
                f"- **{q['id']}** ({q.get('impact', 'medium')}) {q['question']}"
                + (f" _Recommended: {q['recommended']}_" if q.get("recommended") else "")
                for q in spec["questions"]
            ]
    return "\n".join(lines).rstrip() + "\n"


def render_for_agents(specs: list[dict[str, Any]]) -> str:
    """Compact specs for intake, drafting and revision prompts."""
    lines = ["## Screen specs (drafted from the Figma frames; the requester corrects them at intake)", ""]
    for spec in specs:
        frames = ", ".join(f["id"] for f in spec["frames"])
        lines.append(f"### {spec['id']} {spec['screen']}" + (f" (frames {frames})" if frames else ""))
        if spec.get("inventory"):
            lines.append(f"- Inventory (designer's intent): {spec['inventory'].get('purpose')} States: {spec['inventory'].get('states')}")
        if spec.get("error"):
            lines += [f"- not drafted ({spec['error']})", ""]
            continue
        for label, key in (("Purpose", "purpose"), ("Users", "users")):
            if spec.get(key):
                lines.append(f"- {label}: {spec[key]}")
        # Layout and components are what the frames show (agents also get the images and the
        # frame text): a few items each keep prompts small. The full lists are in the document.
        for label, key, limit in (("Layout", "layout", 4), ("Components", "components", 6), ("States shown", "states", _LIST_LIMIT)):
            if spec.get(key):
                lines.append(f"- {label}: " + "; ".join(spec[key][:limit]))
        if spec.get("data_shown"):
            lines.append("- Data shown: " + "; ".join(f"{d['field']} (e.g. {d['example']})" for d in spec["data_shown"]))
        if spec.get("actions"):
            lines.append("- Actions: " + "; ".join(f"{a['control']} → {a['appears_to']}" for a in spec["actions"]))
        for q in spec.get("questions") or []:
            lines.append(f"- Question {q['id']}: {q['question']} Recommended: {q.get('recommended')}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
