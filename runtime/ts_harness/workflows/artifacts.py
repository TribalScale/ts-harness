"""Resolve workflow draft artifacts from succeeded task outputs."""

from __future__ import annotations

import re
from typing import Any

from ts_harness.workflows.workflow_session import get_phase_tracking

_TRAILING_META_RE = re.compile(
    r"\n(?:#{1,3}\s*)?(?:Next Steps|Please review|If you have any questions)\b.*",
    re.IGNORECASE | re.DOTALL,
)
_TRAILING_JSON_OUTPUT_RE = re.compile(
    r"\n(?:```[ \t]*\n\s*)?(?:#{1,6}\s*)?JSON Output\b.*",
    re.IGNORECASE | re.DOTALL,
)
_ACTION_PLAN_PHRASES = (
    "next steps",
    "next step",
    "steps for",
    "i will now proceed",
    "stage execution",
)
_NUMBERED_STEP_RE = re.compile(r"^\s*\d+[.)]\s+", re.MULTILINE)
_CATALOG_SUMMARY_PHRASES = (
    "artifacts produced",
    "artifacts to be produced",
    "artifact to be produced",
    "successfully drafted and stored",
    "successfully stored",
    "stored in memory",
    "let's begin drafting",
    "let's proceed with creating",
    "now, let's proceed",
)
_FOUNDRY_HEADING_ID_RE = re.compile(
    r"^#{1,6}\s*(?:adr|fb|cb|comp|ce|fe)[-\s]?\d+",
    re.IGNORECASE | re.MULTILINE,
)
_REFINERY_FRD_HEADING_RE = re.compile(
    r"^#{1,6}\s*frd[-\s]?\d+",
    re.IGNORECASE | re.MULTILINE,
)
_STAGE_ARTIFACT_START_RE = re.compile(
    r"^#{1,6}\s*(?:frd|fb|comp|adr|cb|fe|wo|product overview|feature tree|requirements)\b",
    re.IGNORECASE | re.MULTILINE,
)
_FOUNDRY_STRUCTURAL_RE = re.compile(
    r"(?:"
    r"\b(?:adr|fb|cb|comp|ce|fe)-\d+"
    r"|^#{1,6}\s*(?:adr|fb|cb|comp)[-\s\d]"
    r"|^#{1,6}\s*(?:architecture decision|component blueprint|system blueprint|feature blueprint|blueprint:)"
    r"|^#{1,6}\s*(?:system design|architecture overview|architecture summary|deployment architecture)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
_FOUNDRY_SUMMARY_HEADING_RE = re.compile(
    r"^#{1,6}\s*(?:system design|architecture overview|architecture summary|deployment architecture)\b",
    re.IGNORECASE | re.MULTILINE,
)
_FOUNDRY_BLUEPRINT_HEADING_RE = re.compile(
    r"^#{1,6}\s*(?:blueprints?\b|(?:component |feature |system )?blueprint\b|(?:fb|cb|comp)[-\s]?\d+)",
    re.IGNORECASE | re.MULTILINE,
)
_FOUNDRY_ADR_HEADING_RE = re.compile(
    r"^#{1,6}\s*(?:adr[-\s]?\d+|architecture decision)\b",
    re.IGNORECASE | re.MULTILINE,
)
_PLANNER_WO_HEADING_RE = re.compile(
    r"^#{1,6}\s*wo[-\s]?\d+",
    re.IGNORECASE | re.MULTILINE,
)
_MEMORY_RECALL_PHRASES = (
    "recalled relevant memories",
    "i've recalled",
    "i have recalled",
    "here's a summary of the key points",
    "here is a summary of the key points",
    "summary of the key points",
    "with this context, i'll proceed",
    "with this context, i will proceed",
)

FOUNDRY_MIN_CHARS = 400
FOUNDRY_MIN_HEADINGS = 3
FOUNDRY_MIN_HEADING_IDS = 2
REFINERY_MIN_CHARS = 400
REFINERY_MIN_HEADINGS = 3
REFINERY_MIN_FRD_IDS = 2
REFINERY_MIN_FRDS_CHARS = 500
REFINERY_MIN_FEATURE_TREE_CHARS = 200
PLANNER_MIN_CHARS = 600
PLANNER_MIN_HEADINGS = 3
PLANNER_MIN_WO_IDS = 2

WORK_ORDERS_STAGE_ARTIFACT_KEYS: dict[str, tuple[str, ...]] = {
    "refinery": (
        "requirements_markdown",
        "frds_markdown",
        "feature_tree_markdown",
        "product_overview_markdown",
    ),
    "foundry": (
        "blueprints_markdown",
        "architecture_decisions_markdown",
        "architecture_summary_markdown",
    ),
    "planner": (
        "work_orders_markdown",
        "context_graph_markdown",
        "execution_phases_markdown",
    ),
}


def artifact_steps(skeleton: Any) -> list[str]:
    session_block = dict(getattr(skeleton, "session", None) or {})
    steps = session_block.get("artifact_steps")
    if isinstance(steps, list) and steps:
        return [str(step) for step in steps]
    tracking = get_phase_tracking(skeleton)
    if tracking is not None:
        return list(tracking.draft_steps)
    return []


def latest_artifact_output(
    tasks: list[dict[str, Any]],
    steps: list[str],
) -> tuple[dict[str, Any] | None, str | None]:
    candidates = [
        task
        for task in tasks
        if task.get("step_name") in steps and task.get("status") == "succeeded"
    ]
    if not candidates:
        return None, None
    latest = sorted(
        candidates,
        key=lambda task: str(task.get("created_at") or ""),
    )[-1]
    output = latest.get("output")
    if isinstance(output, dict):
        return output, str(latest.get("step_name") or "")
    if output is not None:
        return {"content": output}, str(latest.get("step_name") or "")
    return None, None


def sanitize_prd_markdown(text: str) -> str:
    """Strip fences, preamble, and trailing meta from PRD markdown bodies."""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        while lines and lines[-1].strip() == "```":
            lines.pop()
        cleaned = "\n".join(lines).strip()
        if lines and lines[0].strip().lower() == "markdown":
            cleaned = "\n".join(lines[1:]).strip()

    lines = cleaned.splitlines()
    if lines and lines[0].strip().lower() == "markdown":
        cleaned = "\n".join(lines[1:]).strip()

    heading_idx = next(
        (idx for idx, line in enumerate(cleaned.splitlines()) if line.lstrip().startswith("#")),
        None,
    )
    if heading_idx is not None and heading_idx > 0:
        cleaned = "\n".join(cleaned.splitlines()[heading_idx:]).strip()

    cleaned = _TRAILING_META_RE.sub("", cleaned).strip()
    cleaned = _TRAILING_JSON_OUTPUT_RE.sub("", cleaned).strip()
    return cleaned


def sanitize_stage_artifact_markdown(text: str) -> str:
    """Strip fences, preamble, and catalog meta from stage artifact markdown."""
    cleaned = sanitize_prd_markdown(text)
    if not cleaned:
        return ""
    lines = cleaned.splitlines()
    start = 0
    for idx, line in enumerate(lines):
        if _STAGE_ARTIFACT_START_RE.match(line.lstrip()):
            start = idx
            break
    cleaned = "\n".join(lines[start:]).strip()
    return cleaned


def looks_like_artifact_catalog_summary(text: str) -> bool:
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in _CATALOG_SUMMARY_PHRASES)


def looks_like_memory_recall_summary(text: str) -> bool:
    """Detect memory-recall preambles pasted in place of stage artifacts."""
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in _MEMORY_RECALL_PHRASES)


def normalize_artifact_text(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def count_markdown_headings(text: str) -> int:
    return sum(
        1 for line in (text or "").splitlines() if line.lstrip().startswith("#")
    )


def count_foundry_heading_ids(text: str) -> int:
    return len(_FOUNDRY_HEADING_ID_RE.findall(text or ""))


def count_refinery_frd_ids(text: str) -> int:
    return len(_REFINERY_FRD_HEADING_RE.findall(text or ""))


def partition_foundry_markdown(text: str) -> dict[str, str]:
    """Split a foundry markdown blob into distinct artifact keys."""
    cleaned = (text or "").strip()
    if not cleaned:
        return {}

    buckets: dict[str, list[str]] = {
        "architecture_decisions_markdown": [],
        "blueprints_markdown": [],
        "architecture_summary_markdown": [],
    }
    current_key = "blueprints_markdown"
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        body = "\n".join(buffer).strip()
        if body:
            buckets[current_key].append(body)
        buffer = []

    for raw_line in cleaned.splitlines():
        line = raw_line.rstrip()
        if _FOUNDRY_ADR_HEADING_RE.match(line):
            flush()
            current_key = "architecture_decisions_markdown"
            buffer = [line]
            continue
        if _FOUNDRY_SUMMARY_HEADING_RE.match(line):
            flush()
            current_key = "architecture_summary_markdown"
            buffer = [line]
            continue
        if _FOUNDRY_BLUEPRINT_HEADING_RE.match(line):
            flush()
            current_key = "blueprints_markdown"
            buffer = [line]
            continue
        if not buffer and not line.strip():
            continue
        if not buffer:
            # Preamble before the first classified heading stays with blueprints
            # until a more specific heading appears.
            current_key = "blueprints_markdown"
        buffer.append(line)
    flush()

    artifacts = {
        key: "\n\n".join(sections).strip()
        for key, sections in buckets.items()
        if sections and "\n\n".join(sections).strip()
    }
    return artifacts


_REFINERY_SECTION_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("frds_markdown", _REFINERY_FRD_HEADING_RE),
    (
        "feature_tree_markdown",
        re.compile(r"^#{1,6}\s*feature tree\b", re.IGNORECASE),
    ),
    (
        "product_overview_markdown",
        re.compile(r"^#{1,6}\s*product overview\b", re.IGNORECASE),
    ),
    (
        "requirements_markdown",
        re.compile(
            r"^#{1,2}\s*(?:(?:technical|consolidated|non-functional)\s+)?requirements\b"
            r"|^#{1,6}\s*req[-\s]?\d+",
            re.IGNORECASE,
        ),
    ),
)


def partition_refinery_markdown(text: str) -> dict[str, str]:
    """Split a refinery markdown blob into distinct artifact keys by section heading."""
    cleaned = (text or "").strip()
    if not cleaned:
        return {}

    buckets: dict[str, list[str]] = {key: [] for key, _ in _REFINERY_SECTION_RES}
    current_key: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer
        body = "\n".join(buffer).strip()
        if body and current_key is not None:
            buckets[current_key].append(body)
        buffer = []

    for raw_line in cleaned.splitlines():
        line = raw_line.rstrip()
        matched = next(
            (key for key, pattern in _REFINERY_SECTION_RES if pattern.match(line)),
            None,
        )
        if matched is not None:
            flush()
            current_key = matched
            buffer = [line]
            continue
        if current_key is not None:
            buffer.append(line)
    flush()

    return {
        key: "\n\n".join(sections).strip()
        for key, sections in buckets.items()
        if sections and "\n\n".join(sections).strip()
    }


def looks_like_prd_recap_text(text: str, stage: str) -> bool:
    lowered = (text or "").lower()
    prd_markers = (
        "product requirements",
        "problem statement",
        "non-goals",
        "key points from the prd",
        "summary of the key points",
        "the prd for",
        "the prd outlines",
        "prd is in draft",
        "prd is currently in draft",
        "human review of prd",
    )
    if not any(marker in lowered for marker in prd_markers):
        return False
    stage_markers = {
        "refinery": ("frd-", "# frd", "feature tree", "req-"),
        "foundry": (
            "adr-",
            "fb-",
            "cb-",
            "comp-",
            "# architecture decision",
            "# component blueprint",
            "# feature blueprint",
            "# system blueprint",
        ),
        "planner": ("wo-", "work order", "context graph", "execution phase"),
    }
    markers = stage_markers.get(stage, ())
    return not any(marker in lowered for marker in markers)


def looks_like_action_plan(text: str) -> bool:
    """Detect process/meta summaries rather than stage artifact bodies."""
    lowered = (text or "").lower()
    if not lowered.strip():
        return False
    if "steps for" in lowered or "i will now proceed" in lowered:
        return True
    has_phrase = any(phrase in lowered for phrase in _ACTION_PLAN_PHRASES)
    numbered_steps = len(_NUMBERED_STEP_RE.findall(text))
    if has_phrase and numbered_steps >= 1:
        return True
    if numbered_steps >= 3 and count_foundry_heading_ids(text) < FOUNDRY_MIN_HEADING_IDS:
        frd_markers = ("frd-", "wo-", "req-")
        if not any(marker in lowered for marker in frd_markers):
            return True
    return False


def has_foundry_structural_markers(text: str) -> bool:
    """Foundry artifacts need section-heading IDs, not inline mentions."""
    return count_foundry_heading_ids(text) >= FOUNDRY_MIN_HEADING_IDS


def refinery_artifact_failure_reason(text: str) -> str | None:
    cleaned = (text or "").strip()
    if not cleaned:
        return "missing refinery artifact markdown"
    if looks_like_memory_recall_summary(cleaned):
        return "refinery output looks like a memory-recall summary, not FRD artifacts"
    if looks_like_artifact_catalog_summary(cleaned):
        return "refinery output enumerates artifacts instead of delivering FRD bodies"
    if looks_like_action_plan(cleaned):
        return "refinery output looks like an action plan, not FRD artifacts"
    if looks_like_prd_recap_text(cleaned, "refinery"):
        return "refinery output looks like a PRD recap without FRD artifacts"
    frd_ids = count_refinery_frd_ids(cleaned)
    if frd_ids < 1:
        return "refinery output lacks FRD section headings (## FRD-NNN)"
    if frd_ids < REFINERY_MIN_FRD_IDS:
        return (
            "refinery output needs at least two FRD section headings "
            "(## FRD-NNN)"
        )
    if len(cleaned) < REFINERY_MIN_CHARS:
        return "refinery artifact content too short"
    if count_markdown_headings(cleaned) < REFINERY_MIN_HEADINGS:
        return "refinery artifact content lacks enough section headings"
    return None


def foundry_artifact_failure_reason(text: str) -> str | None:
    """Return a specific validation failure reason for foundry artifact text."""
    cleaned = (text or "").strip()
    if not cleaned:
        return "missing foundry artifact markdown"
    if looks_like_memory_recall_summary(cleaned):
        return (
            "foundry output looks like a memory-recall summary, "
            "not blueprint/ADR artifacts"
        )
    if looks_like_artifact_catalog_summary(cleaned):
        return (
            "foundry output enumerates artifacts instead of "
            "delivering blueprint/ADR bodies"
        )
    if looks_like_action_plan(cleaned):
        return "foundry output looks like an action plan, not blueprint/ADR artifacts"
    heading_ids = count_foundry_heading_ids(cleaned)
    if heading_ids < FOUNDRY_MIN_HEADING_IDS:
        return (
            "foundry output lacks blueprint/ADR section headings "
            "(## FB-/COMP-/ADR-NNN)"
        )
    if len(cleaned) < FOUNDRY_MIN_CHARS:
        return "foundry artifact content too short"
    if count_markdown_headings(cleaned) < FOUNDRY_MIN_HEADINGS:
        return "foundry artifact content lacks enough section headings"
    return None


def planner_artifact_failure_reason(text: str) -> str | None:
    """Return a specific validation failure reason for planner artifact text."""
    cleaned = (text or "").strip()
    if not cleaned:
        return "missing planner artifact markdown"
    if looks_like_memory_recall_summary(cleaned):
        return "planner output looks like a memory-recall summary, not work orders"
    if looks_like_artifact_catalog_summary(cleaned):
        return "planner output enumerates artifacts instead of delivering work orders"
    if looks_like_action_plan(cleaned):
        return "planner output looks like an action plan, not work-order artifacts"
    if looks_like_prd_recap_text(cleaned, "planner"):
        return "planner output looks like a PRD recap without work-order artifacts"
    wo_ids = len(_PLANNER_WO_HEADING_RE.findall(cleaned))
    if wo_ids < PLANNER_MIN_WO_IDS:
        return "planner output lacks work-order section headings (## WO-NNN)"
    if len(cleaned) < PLANNER_MIN_CHARS:
        return "planner artifact content too short"
    if count_markdown_headings(cleaned) < PLANNER_MIN_HEADINGS:
        return "planner artifact content lacks enough section headings"
    return None


def looks_like_invalid_foundry_content(text: str) -> bool:
    return foundry_artifact_failure_reason(text) is not None


def work_orders_artifacts_failure_reason(
    artifacts: dict[str, Any] | None,
    stage: str,
) -> str | None:
    """Validate a stage artifacts map; return None when acceptable."""
    if not isinstance(artifacts, dict):
        return "missing artifacts object"
    expected = WORK_ORDERS_STAGE_ARTIFACT_KEYS.get(stage)
    if not expected:
        return None if artifacts else "missing artifacts"

    # Prefer actionable quality errors over bare "missing key" when the model
    # already pasted memory-recall / duplicate junk into some keys.
    present_keys = [
        key
        for key in expected
        if isinstance(artifacts.get(key), str) and str(artifacts.get(key)).strip()
    ]
    for key in present_keys:
        value = str(artifacts[key])
        if looks_like_memory_recall_summary(value):
            return f"{key} looks like a memory-recall summary, not stage artifacts"
    for key, value in artifacts.items():
        if isinstance(value, str) and looks_like_artifact_catalog_summary(value):
            return f"{key} enumerates artifacts instead of delivering {stage} bodies"

    by_norm: dict[str, list[str]] = {}
    for key in present_keys:
        norm = normalize_artifact_text(str(artifacts[key]))
        if not norm:
            continue
        by_norm.setdefault(norm, []).append(key)
    for keys in by_norm.values():
        if len(keys) > 1:
            return "artifact keys share identical content: " + ", ".join(sorted(keys))

    missing = [key for key in expected if key not in present_keys]
    if missing:
        return "missing required artifact keys: " + ", ".join(missing)

    if stage == "refinery":
        frds = str(artifacts["frds_markdown"])
        if count_refinery_frd_ids(frds) < REFINERY_MIN_FRD_IDS:
            return (
                "frds_markdown needs at least two FRD section headings "
                "(## FRD-NNN)"
            )
        if len(frds.strip()) < REFINERY_MIN_FRDS_CHARS:
            return "frds_markdown is too short"
        tree = str(artifacts["feature_tree_markdown"]).strip()
        if len(tree) < REFINERY_MIN_FEATURE_TREE_CHARS:
            return "feature_tree_markdown is too short"

    combined = "\n\n".join(str(artifacts[key]).strip() for key in expected)
    if stage == "refinery":
        return refinery_artifact_failure_reason(combined)
    if stage == "foundry":
        return foundry_artifact_failure_reason(combined)
    if stage == "planner":
        # Validate work-order body specifically; supporting docs may be shorter.
        wo_reason = planner_artifact_failure_reason(
            str(artifacts["work_orders_markdown"])
        )
        if wo_reason:
            return wo_reason
        for key in ("context_graph_markdown", "execution_phases_markdown"):
            text = str(artifacts[key]).strip()
            if looks_like_action_plan(text) or looks_like_prd_recap_text(text, "planner"):
                return f"{key} looks like process/meta text, not planner artifacts"
            if len(text) < 40:
                return f"{key} is too short"
        return None
    return None


def _dedupe_artifact_sections(sections: list[str]) -> list[str]:
    """Drop sections duplicated as substrings of longer sections."""
    ordered = sorted(
        [section.strip() for section in sections if section and section.strip()],
        key=len,
        reverse=True,
    )
    unique: list[str] = []
    for section in ordered:
        if any(section in kept for kept in unique):
            continue
        unique.append(section)
    return unique


def _work_orders_artifacts_body(output: dict[str, Any]) -> str:
    artifacts = output.get("artifacts")
    if not isinstance(artifacts, dict):
        result = output.get("result")
        if isinstance(result, dict):
            nested = result.get("artifacts")
            if isinstance(nested, dict):
                artifacts = nested
    if not isinstance(artifacts, dict):
        return ""
    sections: list[str] = []
    for key in sorted(artifacts):
        if not str(key).endswith("_markdown"):
            continue
        value = artifacts.get(key)
        if isinstance(value, str) and value.strip():
            sections.append(sanitize_stage_artifact_markdown(value.strip()))
    return "\n\n---\n\n".join(_dedupe_artifact_sections(sections))


def _looks_like_action_plan_summary(text: str) -> bool:
    return looks_like_action_plan(text)


def artifact_body(output: dict[str, Any]) -> str:
    prd_keys = ("prd_markdown", "markdown")
    other_keys = ("content", "summary", "text")

    for key in prd_keys:
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return sanitize_prd_markdown(value)
    result = output.get("result")
    if isinstance(result, dict):
        for key in prd_keys:
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return sanitize_prd_markdown(value)

    work_orders_body = _work_orders_artifacts_body(output)
    if work_orders_body:
        return work_orders_body

    for key in other_keys:
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            body = value.strip()
            if _is_empty_json_placeholder(body):
                continue
            if _looks_like_action_plan_summary(body):
                continue
            if looks_like_artifact_catalog_summary(body):
                continue
            if _looks_like_prd_content(body):
                return sanitize_prd_markdown(body)
            return body
    if isinstance(result, dict):
        for key in other_keys:
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                body = value.strip()
                if _is_empty_json_placeholder(body):
                    continue
                if _looks_like_action_plan_summary(body):
                    continue
                if looks_like_artifact_catalog_summary(body):
                    continue
                if _looks_like_prd_content(body):
                    return sanitize_prd_markdown(body)
                return body
    return ""


def _is_empty_json_placeholder(text: str) -> bool:
    return text.strip() in {"{}", "[]"}


def _looks_like_prd_content(text: str) -> bool:
    lowered = text.lower()
    return (
        "product requirements" in lowered
        or "## " in text
        or bool(re.search(r"^#\s+prd\b", text, re.IGNORECASE | re.MULTILINE))
    )


def artifact_from_dispatch(
    dispatch_result: dict[str, Any] | None,
    task: dict[str, Any] | None,
) -> dict[str, Any] | None:
    output = dict((dispatch_result or {}).get("output") or {})
    if output:
        return output
    task_output = (task or {}).get("output")
    if isinstance(task_output, dict) and task_output:
        return task_output
    if task_output is not None and str(task_output).strip():
        return {"content": str(task_output)}
    return None


def artifact_heading(
    artifact: dict[str, Any],
    *,
    display_name: str = "Draft",
    step_name: str = "",
    agent_id: str = "",
) -> str:
    title = artifact_title(artifact, fallback=display_name or "Draft")
    if agent_id or step_name:
        origin = " / ".join(
            part for part in (agent_id.replace("_", " "), step_name.replace("_", " ")) if part
        )
        return f"{title} ({origin})"
    return title


def _https_pr_url(artifact: dict[str, Any]) -> str:
    candidates: list[Any] = [artifact.get("pr_url")]
    result = artifact.get("result")
    if isinstance(result, dict):
        candidates.append(result.get("pr_url"))
        prs = result.get("prs")
        if isinstance(prs, list):
            candidates.extend(prs)
    prs = artifact.get("pr_urls")
    if isinstance(prs, list):
        candidates.extend(prs)
    for item in candidates:
        if isinstance(item, str) and item.strip().startswith("https://"):
            return item.strip()
        if isinstance(item, dict):
            url = item.get("pr_url")
            if isinstance(url, str) and url.strip().startswith("https://"):
                return url.strip()
    return ""


def _delivery_link_body(artifact: dict[str, Any]) -> str:
    pr_url = _https_pr_url(artifact)
    if not pr_url:
        return ""
    work_order = str(
        artifact.get("work_order_id")
        or artifact.get("work_order_ref")
        or artifact.get("work_item_ref")
        or ""
    ).strip()
    if work_order:
        return f"{work_order} is ready for review: {pr_url}"
    return f"Pull request is ready for review: {pr_url}"


def _pr_list_lines(artifact: dict[str, Any]) -> list[str]:
    items: list[Any] = []
    result = artifact.get("result")
    if isinstance(result, dict) and isinstance(result.get("prs"), list):
        items.extend(result["prs"])
    if isinstance(artifact.get("prs"), list):
        items.extend(artifact["prs"])
    lines: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("pr_url") or "").strip()
        if not url.startswith("https://") or url in seen:
            continue
        seen.add(url)
        ref = str(
            item.get("work_item_ref")
            or item.get("work_order_id")
            or item.get("work_order_ref")
            or ""
        ).strip()
        lines.append(f"- {ref}: {url}" if ref else f"- {url}")
    if lines:
        return lines
    url = _https_pr_url(artifact)
    if not url:
        return []
    ref = str(
        artifact.get("work_order_id")
        or artifact.get("work_order_ref")
        or artifact.get("work_item_ref")
        or ""
    ).strip()
    return [f"- {ref}: {url}" if ref else f"- {url}"]


def artifact_channel_body(artifact: dict[str, Any], *, display_name: str = "Draft") -> str:
    body = artifact_body(artifact)
    if not body:
        summary = str(artifact.get("summary") or "").strip()
        if summary and not looks_like_artifact_catalog_summary(summary):
            body = summary
        if _is_empty_json_placeholder(body):
            body = ""
    if not body:
        body = _delivery_link_body(artifact)
    if body:
        return body
    return (
        f"I finished {display_name.lower()}, but the output has no readable content yet. "
        "Ask for status or request a revision."
    )


def build_artifact_channel_text(
    artifact: dict[str, Any],
    *,
    display_name: str = "Draft",
    step_name: str = "",
    agent_id: str = "",
) -> str:
    heading = artifact_heading(
        artifact,
        display_name=display_name,
        step_name=step_name,
        agent_id=agent_id,
    )
    body = artifact_channel_body(artifact, display_name=display_name)
    if body.startswith("I finished ") and "no readable content" in body:
        return body
    return f"{heading}\n\n{body}"


def _format_doc_link_for_slack(web_view_link: str) -> str:
    url = (web_view_link or "").strip()
    if not url:
        return ""
    return f"View full output in Google Docs: {url}"


def format_artifact_for_channel(
    artifact: dict[str, Any],
    *,
    display_name: str = "Draft",
    present_prompt: str = "inform",
    step_name: str = "",
    agent_id: str = "",
    max_length: int = 3900,
    min_chars_for_doc: int = 3000,
    doc_url: str | None = None,
    publish_full_text: bool = False,
) -> str:
    message = build_artifact_channel_text(
        artifact,
        display_name=display_name,
        step_name=step_name,
        agent_id=agent_id,
    )
    if message.startswith("I finished ") and "no readable content" in message:
        return message

    truncated = len(message) > max_length
    should_link = bool(doc_url) and (publish_full_text or truncated or len(message) >= min_chars_for_doc)
    if truncated:
        message = f"{message[:max_length].rstrip()}\n\n(truncated for Slack.)"
    if should_link and doc_url:
        message += f"\n\n{_format_doc_link_for_slack(doc_url)}"
    elif truncated:
        message += "\n\n(Full output is stored in the workflow.)"

    prompt_mode = (present_prompt or "inform").lower()
    if prompt_mode == "approval":
        message += "\n\nReply yes to approve, or no with feedback to revise."
    elif prompt_mode == "intake":
        message += (
            "\n\nReply in this thread with your answers. "
            "I'll draft the PRD once you've responded."
        )
    elif prompt_mode == "merge":
        message += (
            "\n\nQA passed. Please merge the pull request. "
            "I'll continue automatically after GitHub merge."
        )
        pr_lines = _pr_list_lines(artifact)
        if pr_lines:
            message += "\n" + "\n".join(pr_lines)
    elif prompt_mode == "inform":
        message += "\n\nLet me know if you want to continue or need changes."
    return message


def artifact_title(output: dict[str, Any], *, fallback: str = "Draft") -> str:
    for key in ("prd_title", "title", "name"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    result = output.get("result")
    if isinstance(result, dict):
        for key in ("prd_title", "title", "name"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return fallback
