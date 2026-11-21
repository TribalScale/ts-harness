"""Guided revisions: notice when OpenCode keeps failing the same finding, and diagnose it.

Code review and QA send a work order back with their findings. When the same finding comes
back after a revision, another plain revision usually repeats the same mistake. Instead the
harness asks a stronger model why the attempts failed and what the fix is, and sends one
revision with that guidance (and the full finding history) to a stronger coding model. If
that also fails, the person who decides gets the diagnosis and a ready-made ``/revise`` text.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Gates whose findings a guided revision can address.
GUIDED_GATES = frozenset({"finalize_code_review", "code_review", "qa_gate"})
LINE_WINDOW = 40
MAX_FILE_CHARS = 12000
MAX_FILES = 3
_WORD = re.compile(r"[a-z][a-z0-9_]{2,}")
_STOP = frozenset(
    "the and that this with for from into when then than are was were not but can its use using have has "
    "only also each which while should must would could does instead".split()
)

Diagnose = Callable[[str], str]


# --- rounds and repeats -------------------------------------------------------------------------


def _findings(output: Any) -> list[dict[str, Any]]:
    if not isinstance(output, dict):
        return []
    result = output.get("result") if isinstance(output.get("result"), dict) else output
    found = [item for item in result.get("findings") or [] if isinstance(item, dict)]
    for pr in result.get("prs") or []:
        if isinstance(pr, dict):
            found.extend(item for item in pr.get("defects") or [] if isinstance(item, dict))
    return found


def _feedback(output: Any) -> str:
    if not isinstance(output, dict):
        return ""
    result = output.get("result") if isinstance(output.get("result"), dict) else output
    return str(output.get("review_feedback") or result.get("summary") or output.get("summary") or "").strip()


def review_rounds(tasks: list[dict[str, Any]], gate_step: str) -> list[dict[str, Any]]:
    """Each blocking verdict of ``gate_step``, oldest first: its findings and feedback text."""
    rounds = []
    for task in sorted(tasks, key=lambda item: str(item.get("created_at") or "")):
        if task.get("step_name") != gate_step:
            continue
        output = task.get("output")
        result = output.get("result") if isinstance(output, dict) and isinstance(output.get("result"), dict) else output
        verdict = str((result or {}).get("verdict") or "").lower() if isinstance(result, dict) else ""
        findings = _findings(output)
        blocked = task.get("status") == "blocked" or verdict in {"block", "fail", "failed", "request_changes"}
        if not blocked or not (findings or _feedback(output)):
            continue
        rounds.append({"task_id": task.get("id"), "findings": findings, "feedback": _feedback(output)})
    return rounds


def _words(text: str) -> set[str]:
    return {word for word in _WORD.findall(text.lower()) if word not in _STOP}


def _similar(a: str, b: str, threshold: float) -> bool:
    left, right = _words(a), _words(b)
    if not left or not right:
        return False
    return len(left & right) / len(left | right) >= threshold


def _same_finding(a: dict[str, Any], b: dict[str, Any]) -> bool:
    file_a, file_b = str(a.get("file") or ""), str(b.get("file") or "")
    if file_a and file_a == file_b:
        try:
            if abs(int(a.get("line") or 0) - int(b.get("line") or 0)) <= LINE_WINDOW:
                return True
        except (TypeError, ValueError):
            pass
        return _similar(str(a.get("message") or ""), str(b.get("message") or ""), 0.25)
    if not file_a and not file_b:
        return _similar(str(a.get("message") or a.get("description") or ""), str(b.get("message") or b.get("description") or ""), 0.4)
    return False


def repeated_findings(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Findings of the latest round that an earlier round already raised (empty when none)."""
    if len(rounds) < 2:
        return []
    latest, earlier = rounds[-1], rounds[:-1]
    if latest["findings"]:
        return [
            item for item in latest["findings"]
            if any(_same_finding(item, old) for past in earlier for old in past["findings"])
        ]
    if any(_similar(latest["feedback"], past["feedback"], 0.4) for past in earlier):
        return [{"message": latest["feedback"]}]
    return []


# --- diagnosis ----------------------------------------------------------------------------------


def _finding_line(item: dict[str, Any]) -> str:
    where = f"{item.get('file')}:{item.get('line')}" if item.get("file") else "general"
    return f"- [{item.get('severity') or 'blocking'}] {where}: {str(item.get('message') or item.get('description') or '').strip()}"


def history_text(rounds: list[dict[str, Any]]) -> str:
    """Every round's findings, so a revision sees what was already tried and rejected."""
    lines = []
    for number, review in enumerate(rounds, 1):
        lines.append(f"Round {number}:")
        lines.extend(_finding_line(item) for item in review["findings"][:8])
        if not review["findings"] and review["feedback"]:
            lines.append(f"- {review['feedback'][:800]}")
    return "\n".join(lines)


PROMPT = """You are a senior engineer unblocking an automated coding agent.

The agent implemented a work order; code review rejected it, and after each revision the
same finding came back. Work out why the attempts did not fix it and say exactly how to.

Work order:
{work_order}

Review findings, oldest round first:
{history}

What each attempt reported:
{attempts}

Current code of the files the findings name (PR head):
{files}

Reply with JSON only:
{{"why_attempts_failed": "one or two sentences",
  "approach": "the fix, concrete enough to implement: where the change goes and what it does",
  "avoid": ["what not to do again, including anything the attempts kept doing"],
  "test": "the test that proves it, including how it fails today",
  "out_of_scope": false}}
Set out_of_scope to true only if the finding asks for something the work order does not cover.
"""


def _file_texts(files: dict[str, str]) -> str:
    if not files:
        return "(not available)"
    return "\n\n".join(f"--- {path} ---\n{text[:MAX_FILE_CHARS]}" for path, text in files.items())


def diagnose(
    *,
    rounds: list[dict[str, Any]],
    attempts: list[str],
    work_order: str,
    files: dict[str, str],
    model_call: Diagnose,
) -> dict[str, Any] | None:
    """The diagnosis as a dict, or None when the model gives no usable answer."""
    prompt = PROMPT.format(
        work_order=(work_order or "(not available)")[:6000],
        history=history_text(rounds),
        attempts="\n".join(f"- {text[:600]}" for text in attempts if text) or "(none recorded)",
        files=_file_texts(files),
    )
    try:
        reply = model_call(prompt)
    except Exception as exc:  # noqa: BLE001 - no diagnosis means the ordinary path
        logger.warning("revision diagnosis failed: %s", exc)
        return None
    match = re.search(r"\{.*\}", reply or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not str(data.get("approach") or "").strip():
        return None
    data["avoid"] = [str(item) for item in data.get("avoid") or [] if str(item).strip()]
    return data


def guidance_text(diagnosis: dict[str, Any], rounds: list[dict[str, Any]]) -> str:
    """The revision feedback for a guided attempt: the diagnosis first, then every round."""
    lines = [
        "The same review finding came back after earlier revisions. A diagnosis of why those attempts failed:",
        f"Why they failed: {diagnosis.get('why_attempts_failed') or 'not stated'}",
        f"Do this: {diagnosis['approach']}",
    ]
    if diagnosis.get("avoid"):
        lines.append("Do not: " + "; ".join(diagnosis["avoid"]))
    if diagnosis.get("test"):
        lines.append(f"Prove it with a test: {diagnosis['test']}")
    lines.append("")
    lines.append("All review findings so far:")
    lines.append(history_text(rounds))
    return "\n".join(lines)


def suggested_revise(label: str, diagnosis: dict[str, Any]) -> str:
    """A ``/revise`` reply a person can send as it is."""
    parts = [str(diagnosis.get("approach") or "").strip()]
    if diagnosis.get("avoid"):
        parts.append("Do not: " + "; ".join(diagnosis["avoid"]) + ".")
    if diagnosis.get("test"):
        parts.append(f"Test: {diagnosis['test']}")
    return f"/revise {label} " + " ".join(part for part in parts if part)


# --- wiring -------------------------------------------------------------------------------------


def diagnosis_model() -> str | None:
    """The model that writes the diagnosis: the override, else the PM agent's model."""
    setting = (os.environ.get("TS_HARNESS_REVISION_DIAGNOSIS_MODEL") or "").strip()
    if setting.lower() in {"off", "none", "false", "0"}:
        return None
    if setting:
        return setting
    from ts_harness.intake.connectors import agent_model

    return agent_model("pm-agent")


def guided_coding_model() -> str | None:
    """The coding model for a guided attempt (the override, else the diagnosis model), or None."""
    setting = (os.environ.get("TS_HARNESS_GUIDED_CODING_MODEL") or "").strip()
    if setting.lower() in {"off", "none", "false", "0"}:
        return None
    return setting or diagnosis_model()


def build_model_call(model: str) -> Diagnose:
    from ts_harness.agent.provider import build_provider

    def call(prompt: str) -> str:
        provider = build_provider(model)
        response = provider.chat([{"role": "user", "content": prompt}], temperature=0)
        return str(((response.get("choices") or [{}])[0].get("message") or {}).get("content") or "")

    return call


def pr_files(pr_url: str, paths: list[str]) -> dict[str, str]:
    """Current text of ``paths`` at the PR head ({} when GitHub cannot be read)."""
    import base64

    from ts_harness.integrations.github_client import github_for, parse_github_pr_url

    parsed = parse_github_pr_url(pr_url or "")
    if parsed is None or not paths:
        return {}
    owner, repo, number = parsed
    try:
        github = github_for(pr_url)
        head = str(((github.request("GET", f"/repos/{owner}/{repo}/pulls/{number}") or {}).get("head") or {}).get("sha") or "")
        texts = {}
        for path in paths[:MAX_FILES]:
            payload = github.get_file(owner, repo, path, ref=head) or {}
            if isinstance(payload, dict) and payload.get("content"):
                texts[path] = base64.b64decode(payload["content"]).decode("utf-8", "replace")
        return texts
    except Exception as exc:  # noqa: BLE001 - the diagnosis can work from the findings alone
        logger.info("revision diagnosis: PR files unavailable: %s", exc)
        return {}


def attempt_summaries(tasks: list[dict[str, Any]], implement_step: str) -> list[str]:
    summaries = []
    for task in sorted(tasks, key=lambda item: str(item.get("created_at") or "")):
        if task.get("step_name") == implement_step and isinstance(task.get("output"), dict):
            text = str(task["output"].get("summary") or "").strip()
            if text:
                summaries.append(text)
    return summaries


def work_order_text(tasks: list[dict[str, Any]]) -> str:
    for task in tasks:
        task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
        text = str(task_input.get("work_order_body") or "").strip()
        if text:
            return text
    return ""


def plan_guided_revision(
    tasks: list[dict[str, Any]],
    *,
    gate_step: str,
    implement_step: str,
    pr_url: str,
    model_call: Diagnose | None = None,
) -> dict[str, Any] | None:
    """Diagnosis, feedback and coding model for a guided attempt, or None when it does not apply."""
    if gate_step not in GUIDED_GATES:
        return None
    rounds = review_rounds(tasks, gate_step)
    repeated = repeated_findings(rounds)
    if not repeated:
        return None
    if model_call is None:
        model = diagnosis_model()
        if not model:
            return None
        model_call = build_model_call(model)
    paths = []
    for item in repeated:
        path = str(item.get("file") or "")
        if path and path not in paths:
            paths.append(path)
    diagnosis = diagnose(
        rounds=rounds,
        attempts=attempt_summaries(tasks, implement_step),
        work_order=work_order_text(tasks),
        files=pr_files(pr_url, paths),
        model_call=model_call,
    )
    if diagnosis is None:
        return None
    return {
        "diagnosis": diagnosis,
        "feedback": guidance_text(diagnosis, rounds),
        "coding_model": guided_coding_model(),
        "repeated": repeated,
    }
