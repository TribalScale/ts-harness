"""Parse and resolve leading /skill-id pins for channel chat."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ts_harness.config.schema import AgentConfig, RuntimeConfig
    from ts_harness.skills.catalog import ResolvedSkill, SkillCatalog

MAX_SLASH_PINS = 5
CANDIDATE_PIN_RE = re.compile(r"^/[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
RESERVED_LEARN = "learn"


@dataclass(frozen=True)
class SlashInvokeParse:
    pins: list[str]
    remainder: str
    reserved_learn: bool


@dataclass(frozen=True)
class SlashInvokeResolve:
    skills: list["ResolvedSkill"]
    diagnostics: list[str]
    unresolved: list[tuple[str, str]]


def catalog_index_ids(catalog: "SkillCatalog") -> set[str]:
    """Every indexed meta.id (any approval_status). Used only to decide in-catalog vs path."""
    return {meta.id for meta in catalog.index.values()}


def parse_slash_invoke(text: str, *, catalog_ids: set[str]) -> SlashInvokeParse:
    stripped = str(text or "").strip()
    if not stripped:
        return SlashInvokeParse(pins=[], remainder="", reserved_learn=False)
    tokens = stripped.split()
    pins: list[str] = []
    index = 0
    reserved_learn = False
    while index < len(tokens):
        token = tokens[index]
        if not CANDIDATE_PIN_RE.match(token):
            break
        skill_id = token[1:]
        first_segment = skill_id.split("/", 1)[0]
        if first_segment.lower() == RESERVED_LEARN:
            reserved_learn = True
            break
        if skill_id not in catalog_ids:
            break
        if len(pins) >= MAX_SLASH_PINS:
            break
        pins.append(skill_id)
        index += 1
    remainder = " ".join(tokens[index:])
    return SlashInvokeParse(pins=pins, remainder=remainder, reserved_learn=reserved_learn)


def _meta_for_id(catalog: "SkillCatalog", skill_id: str):
    for meta in catalog.index.values():
        if meta.id == skill_id:
            return meta
    return None


def _lines_for_pin(pin: str, diagnostics: list[str]) -> tuple[str | None, list[str]]:
    channel_line = f"skip channel chat: {pin}"
    prefix = f"skip {pin}@"
    channel = next((line for line in diagnostics if line == channel_line), None)
    skips = [line for line in diagnostics if line.startswith(prefix)]
    return channel, skips


def _reason_for_pin(
    catalog: "SkillCatalog",
    pin: str,
    diagnostics: list[str],
    agent: "AgentConfig",
) -> str:
    meta = _meta_for_id(catalog, pin)
    channel, skips = _lines_for_pin(pin, diagnostics)
    if channel:
        return "channel-chat exclusion"
    detail = skips[0].split(":", 1)[-1].strip() if skips else ""
    if detail.startswith("not assigned"):
        return f"not assigned to {agent.id}"
    if detail.startswith("missing toolset"):
        return detail
    if detail.startswith("tenant excluded"):
        return "tenant excluded"
    created = meta is not None and meta.trust_level == "agent-created"
    if "agent-created" in detail or created:
        return "agent-created"
    if meta is not None and meta.approval_status != "approved":
        if meta.approval_status == "draft":
            return "draft"
        return f"approval_status={meta.approval_status}"
    if detail.startswith("approval_status="):
        status = detail.split("=", 1)[-1]
        return "draft" if status == "draft" else detail
    return detail or "unresolved"


def resolve_slash_pins(
    catalog: "SkillCatalog",
    agent: "AgentConfig",
    config: "RuntimeConfig",
    *,
    tenant_id: str,
    pins: list[str],
) -> SlashInvokeResolve:
    resolved, diagnostics = catalog.resolve_for_channel_chat(
        agent,
        config,
        tenant_id=tenant_id,
    )
    by_id = {skill.metadata.id: skill for skill in resolved}
    skills = []
    unresolved: list[tuple[str, str]] = []
    for pin in pins:
        skill = by_id.get(pin)
        if skill is not None:
            skills.append(skill)
            continue
        unresolved.append((pin, _reason_for_pin(catalog, pin, diagnostics, agent)))
    return SlashInvokeResolve(skills=skills, diagnostics=diagnostics, unresolved=unresolved)


def blocked_unresolved_payload(*, session_id: str, unresolved: list[tuple[str, str]]) -> dict:
    parts = [f"Cannot use /{skill_id}: {reason}." for skill_id, reason in unresolved]
    return {
        "status": "blocked",
        "session_id": session_id,
        "error": "skill_invoke_unresolved",
        "output": {"summary": " ".join(parts).strip()},
    }
