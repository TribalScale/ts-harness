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


def screen_name(title: str) -> str:
    """The screen a frame belongs to: its title without the variant part."""
    return _VARIANT.sub("", str(title or "").strip()).strip() or str(title or "Screen").strip()


def group_screens(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``[{screen, frames}]`` in the order screens first appear; only Figma frames."""
    screens: dict[str, list[dict[str, Any]]] = {}
    for ref in refs or []:
        if not isinstance(ref, dict) or ref.get("source") != "figma" or not ref.get("id"):
            continue
        screens.setdefault(screen_name(str(ref.get("title") or "")), []).append(ref)
    return [{"screen": name, "frames": frames} for name, frames in screens.items()]


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
    prompt = PROMPT.format(screen=screen["screen"], frames=frame_list, max_questions=QUESTIONS_PER_SCREEN, list_limit=_LIST_LIMIT)
    spec: dict[str, Any] = {
        "screen": screen["screen"],
        "frames": [{"id": f["id"], "title": f.get("title"), "url": f.get("url")} for f in frames],
    }
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


def draft_screen_specs(refs: list[dict[str, Any]], *, describe: Describe | None, store: Any) -> dict[str, Any]:
    """``{screen_specs, screen_questions, screen_specs_text, summary, skipped}`` for the design refs."""
    screens = group_screens(refs)
    if not screens or describe is None:
        reason = "no Figma frames" if not screens else "image reading is off (TS_HARNESS_INTAKE_VISION_MODEL)"
        return {"skipped": True, "reason": reason, "screen_specs": [], "screen_questions": [], "screen_specs_text": ""}
    capped = screens[: max_screens()]
    with ThreadPoolExecutor(max_workers=min(_WORKERS, len(capped))) as pool:
        specs = list(pool.map(lambda s: describe_screen(s, describe=describe, store=store), capped))
    number = 0
    ranked: list[tuple[int, int, str]] = []
    for index, spec in enumerate(specs, start=1):
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
    lines = [
        "# Screen specs",
        "",
        f"Drafted from the Figma frames: {len(specs)} screens. Each lists what its frames show, then questions "
        f"for what they don't ({count} in all, S1–S{count}, rated by impact). The intake asks the highest-impact "
        "ones the sources don't answer; every other question keeps its recommended answer unless you answer it "
        "by S-number. Correct a spec with its SC-number (\"SC3: …\").",
        "",
    ]
    lines += [f"_{note}_" for note in notes or []]
    for spec in specs:
        lines += ["", f"## {spec['id']} · {spec['screen']}", "", f"**Frames:** {_frames_line(spec)}", ""]
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
        lines.append(f"### {spec['id']} {spec['screen']} (frames {frames})")
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
