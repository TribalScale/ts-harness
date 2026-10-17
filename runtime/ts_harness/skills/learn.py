"""Parse /learn messages and pick an authoring agent for skill drafts."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING

from ts_harness.tools.toolsets import agent_effective_tools

if TYPE_CHECKING:
    from ts_harness.config.schema import AgentConfig, RuntimeConfig

LEARN_TOKEN_RE = re.compile(r"^/learn$", re.IGNORECASE)
DEFAULT_LEARN_AGENT_ID = "ops-agent"
_LEADING_MENTION_RE = re.compile(r"^\s*(?:<@[^>]+>\s*|@\w+\s+)")

LEARN_SYSTEM_INSTRUCTION = (
    "You are authoring a governed skill draft. Research the user's source with "
    "existing tools (read_file, search_files, MCP, etc.). Then call propose_skill "
    "with complete skill markdown. Follow house SKILL.md shape: YAML frontmatter "
    "with id, category, description, version; a short description; numbered steps; "
    "pitfalls. Never write skills/<category>/ yourself. Never claim the skill is "
    "live. After propose_skill succeeds, tell the user the draft id and that Inbox "
    "must approve at /#/inbox/skill_draft/{id}."
)


@dataclass(frozen=True)
class LearnParse:
    is_learn: bool
    source: str


def strip_leading_mentions(text: str) -> str:
    """Ignore Slack/Discord bot mentions that prefix command text."""
    return _LEADING_MENTION_RE.sub("", str(text or ""), count=1)


def parse_learn(text: str) -> LearnParse:
    stripped = strip_leading_mentions(text).strip()
    if not stripped:
        return LearnParse(is_learn=False, source="")
    head, *rest = stripped.split(None, 1)
    if not LEARN_TOKEN_RE.match(head):
        return LearnParse(is_learn=False, source="")
    return LearnParse(is_learn=True, source=rest[0].strip() if rest else "")


def _can_author(agent: AgentConfig, config: RuntimeConfig, registry_tool_names: set[str]) -> bool:
    if "propose_skill" not in registry_tool_names:
        return False
    return "propose_skill" in agent_effective_tools(agent, config)


def select_authoring_agent_id(
    session_agent: AgentConfig,
    config: RuntimeConfig,
    *,
    registry_tool_names: set[str],
) -> str | None:
    """Session agent if it can call propose_skill; else ops-agent if it can; else None."""
    if _can_author(session_agent, config, registry_tool_names):
        return session_agent.id
    try:
        ops = config.get_agent(DEFAULT_LEARN_AGENT_ID)
    except KeyError:
        return None
    if _can_author(ops, config, registry_tool_names):
        return ops.id
    return None


def blocked_learn_payload(*, session_id: str, error: str, summary: str) -> dict:
    return {
        "status": "blocked",
        "session_id": session_id,
        "error": error,
        "output": {"summary": summary},
    }
