"""Declarative stage contract validation and outcome resolution.

Phase 1 of the Platform Durability Upgrades moves hard-coded stage output
validators out of ``runtime/ts_harness/agent/loop.py`` and into declarative
contract data attached to workflow skeletons (and persisted immutably per task).

A *contract* is a plain dict with the shape produced by
``workflow_definitions.freeze_contract``::

    {
      "name": "<contract name or ''>",
      "output_schema": {<JSON schema>} | None,
      "required_artifacts": {"<label>": "<RFC6901 pointer>", ...},
      "semantic_validators": ["<registry name>", ...],
      "outcome": {"pointer": "/status",
                  "succeeded_values": ["success"],
                  "blocked_values": ["failed"]} | None,
    }

All public helpers accept either a resolved contract dict or a stage-like object
that exposes ``output_schema`` / ``contract_ref`` attributes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Literal

try:  # jsonschema is a runtime dependency; guard for minimal environments.
    from jsonschema import Draft7Validator
except Exception:  # pragma: no cover - exercised only when dependency missing
    Draft7Validator = None  # type: ignore[assignment]

from ts_harness.workflows.artifacts import work_orders_artifacts_failure_reason


@dataclass(frozen=True)
class ContractViolation:
    path: str
    message: str


# ---------------------------------------------------------------------------
# Contract coercion + RFC6901 pointer resolution
# ---------------------------------------------------------------------------

_UNSET = object()
_MISSING = object()


def _as_contract(contract_or_stage: Any) -> dict[str, Any]:
    """Coerce a resolved contract dict or a stage-like object into a contract."""
    if contract_or_stage is None:
        return {}
    if isinstance(contract_or_stage, dict):
        return contract_or_stage
    # Stage-like object with an inline output_schema (one-off contract).
    schema = getattr(contract_or_stage, "output_schema", None)
    contract: dict[str, Any] = {
        "name": getattr(contract_or_stage, "contract_ref", "") or "",
        "output_schema": schema or None,
        "required_artifacts": {},
        "semantic_validators": [],
        "outcome": None,
    }
    return contract


def _unescape_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def resolve_pointer(document: Any, pointer: str, default: Any = _UNSET) -> Any:
    """Resolve an RFC6901 JSON pointer against a document.

    Returns *default* when the pointer cannot be resolved and a default is given;
    otherwise raises ``KeyError``.
    """
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        if default is not _UNSET:
            return default
        raise KeyError(f"invalid pointer: {pointer}")
    current = document
    for raw_token in pointer.split("/")[1:]:
        token = _unescape_token(raw_token)
        if isinstance(current, dict):
            if token not in current:
                if default is not _UNSET:
                    return default
                raise KeyError(pointer)
            current = current[token]
        elif isinstance(current, list):
            try:
                idx = int(token)
                current = current[idx]
            except (ValueError, IndexError):
                if default is not _UNSET:
                    return default
                raise KeyError(pointer)
        else:
            if default is not _UNSET:
                return default
            raise KeyError(pointer)
    return current


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


# ---------------------------------------------------------------------------
# Semantic validator registry
# ---------------------------------------------------------------------------

SemanticValidator = Callable[
    [dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]],
    list[ContractViolation],
]


def _last_successful_coding_backend(
    tool_audit: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    import json

    for record in reversed(tool_audit or []):
        if record.get("tool") != "run_coding_backend":
            continue
        if record.get("decision") != "allow":
            continue
        preview = record.get("result_preview")
        parsed: dict[str, Any] | None = None
        if isinstance(preview, dict):
            parsed = preview
        elif isinstance(preview, str) and preview.strip():
            try:
                candidate = json.loads(preview)
            except json.JSONDecodeError:
                candidate = None
            if isinstance(candidate, dict):
                parsed = candidate
        if parsed is not None:
            return parsed
    return None


TRUSTED_CODING_BACKEND_FIELDS = ("security", "refresh_method", "head_sha", "branch")


def trusted_coding_backend_fields(tool_audit: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Fields downstream gates read from the last successful ``run_coding_backend`` call.

    The agent's envelope is not trusted to echo them, so the tool's values win.
    """
    for record in reversed(tool_audit or []):
        if record.get("tool") != "run_coding_backend" or record.get("decision") != "allow":
            continue
        fields = record.get("coding_result")
        if not isinstance(fields, dict) or fields.get("ok") is not True:
            return {}
        return {key: fields[key] for key in TRUSTED_CODING_BACKEND_FIELDS if fields.get(key) not in (None, "")}
    return {}


def validate_coding_backend_pr_url(
    contract: dict[str, Any],
    output: dict[str, Any],
    stage_input: dict[str, Any],
    tool_audit: list[dict[str, Any]],
) -> list[ContractViolation]:
    """Require pr_url to come from a successful run_coding_backend result.

    Ported from ``loop._deliver_work_order_tool_failure_reason``.
    """
    work_order_id = output.get("work_order_id")
    pr_url = output.get("pr_url")
    if not (
        isinstance(work_order_id, str)
        and work_order_id.strip()
        and isinstance(pr_url, str)
        and pr_url.strip().startswith("http")
    ):
        return [
            ContractViolation(
                "/pr_url",
                "deliver_work_order output must include work_order_id and "
                "pr_url (https URL from run_coding_backend)",
            )
        ]
    claimed_pr = str(pr_url).strip()
    last_coding = _last_successful_coding_backend(tool_audit)
    if last_coding is None:
        return [
            ContractViolation(
                "/pr_url",
                "deliver_work_order requires a successful run_coding_backend "
                "tool call that returns pr_url",
            )
        ]
    if last_coding.get("ok") is not True:
        err = str(last_coding.get("error") or "run_coding_backend failed").strip()
        return [
            ContractViolation(
                "/pr_url",
                f"deliver_work_order cannot succeed when run_coding_backend failed: {err}",
            )
        ]
    tool_pr = str(last_coding.get("pr_url") or "").strip()
    if not tool_pr.startswith("http"):
        return [
            ContractViolation(
                "/pr_url",
                "deliver_work_order requires run_coding_backend to return a https pr_url",
            )
        ]
    if tool_pr != claimed_pr:
        return [
            ContractViolation(
                "/pr_url",
                "deliver_work_order pr_url must match run_coding_backend result "
                f"(output={claimed_pr}, tool={tool_pr})",
            )
        ]
    return []


def _work_orders_stage_from_input(stage_input: dict[str, Any]) -> str:
    stage = str((stage_input or {}).get("stage") or "").strip().lower()
    return stage


def validate_work_order_artifacts(
    contract: dict[str, Any],
    output: dict[str, Any],
    stage_input: dict[str, Any],
    tool_audit: list[dict[str, Any]],
) -> list[ContractViolation]:
    """Validate work-order stage artifacts using the shared artifacts checker."""
    stage = _work_orders_stage_from_input(stage_input)
    artifacts = output.get("artifacts")
    reason = work_orders_artifacts_failure_reason(
        artifacts if isinstance(artifacts, dict) else None,
        stage,
    )
    if reason:
        return [ContractViolation("/artifacts", reason)]
    return []


def _pr_url_set_from_input(stage_input: dict[str, Any]) -> set[str]:
    """Collect the expected input PR URL set for a qa_gate stage.

    Accepts either a single ``pr_url`` (work-order path, 1-item set) or a
    ``pr_urls`` list (Jira aggregate path).
    """
    urls: set[str] = set()
    single = stage_input.get("pr_url")
    if isinstance(single, str) and single.strip():
        urls.add(single.strip())
    many = stage_input.get("pr_urls")
    if isinstance(many, (list, tuple, set)):
        for item in many:
            if isinstance(item, str) and item.strip():
                urls.add(item.strip())
    return urls


def _pr_url_set_from_output(output: dict[str, Any]) -> tuple[set[str], list[dict[str, Any]]]:
    result = output.get("result")
    prs = (result or {}).get("prs") if isinstance(result, dict) else None
    urls: set[str] = set()
    pr_list: list[dict[str, Any]] = []
    if isinstance(prs, list):
        for pr in prs:
            if not isinstance(pr, dict):
                continue
            pr_list.append(pr)
            pr_url = pr.get("pr_url")
            if isinstance(pr_url, str) and pr_url.strip():
                urls.add(pr_url.strip())
    return urls, pr_list


def validate_qa_pr_url_set(
    contract: dict[str, Any],
    output: dict[str, Any],
    stage_input: dict[str, Any],
    tool_audit: list[dict[str, Any]],
) -> list[ContractViolation]:
    """Semantic rules for the canonical QA envelope.

    1. The output PR URL set (``result.prs[].pr_url``) must equal the input PR
       URL set (``pr_url`` / ``pr_urls``) exactly.
    2. A top-level ``status: success`` is only permitted when every per-PR
       ``status`` is ``success``.
    3. A PR whose required CI failed (``checks_state`` / ``failed_checks``
       from ``await_pr_checks``) may not be ``success``.
    4. Every acceptance test listed in ``work_order_body`` has an
       ``acceptance_criteria`` entry on each PR.
    """
    violations: list[ContractViolation] = []
    input_urls = _pr_url_set_from_input(stage_input or {})
    output_urls, pr_list = _pr_url_set_from_output(output)

    if not pr_list:
        return [
            ContractViolation(
                "/result/prs",
                "qa_gate output must include result.prs with at least one PR entry",
            )
        ]

    if input_urls:
        missing = input_urls - output_urls
        extra = output_urls - input_urls
        if missing or extra:
            violations.append(
                ContractViolation(
                    "/result/prs",
                    "qa_gate output PR URL set must equal input PR URL set exactly "
                    f"(missing={sorted(missing)}, unexpected={sorted(extra)})",
                )
            )
    else:
        violations.append(
            ContractViolation(
                "",
                "qa_gate requires an input PR URL set (pr_url or pr_urls) to validate against",
            )
        )

    top_status = str(output.get("status") or "").strip().lower()
    per_pr_statuses = [str(pr.get("status") or "").strip().lower() for pr in pr_list]
    if top_status == "success" and not all(s == "success" for s in per_pr_statuses):
        violations.append(
            ContractViolation(
                "/status",
                "qa_gate top-level status may only be 'success' when every "
                "result.prs[].status is 'success'",
            )
        )
    for pr_url in sorted(_failed_ci_pr_urls(stage_input or {}, output_urls)):
        if any(str(pr.get("pr_url") or "").strip() == pr_url and str(pr.get("status") or "").strip().lower() == "success" for pr in pr_list):
            violations.append(
                ContractViolation("/result/prs", f"qa_gate cannot pass {pr_url}: its required checks failed CI")
            )
    for pr in pr_list:
        missing_tests = _uncovered_acceptance_tests(stage_input or {}, pr)
        if missing_tests:
            violations.append(
                ContractViolation(
                    "/result/prs/acceptance_criteria",
                    f"qa_gate must evaluate every work order acceptance test for {pr.get('pr_url')}; "
                    "add one acceptance_criteria entry (quoting the test) for: " + "; ".join(missing_tests),
                )
            )
    return violations


_ACCEPTANCE_HEADING_RE = re.compile(r"^#{1,6}\s+.*acceptance", re.IGNORECASE)
_ANY_HEADING_RE = re.compile(r"^#{1,6}\s")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(.+)$")
_CRITERION_STOPWORDS = frozenset({"given", "when", "then", "and", "the", "with", "for", "that", "are", "is"})


def _acceptance_tests(work_order_body: str) -> list[str]:
    tests: list[str] = []
    in_section = False
    for line in str(work_order_body or "").splitlines():
        if _ACCEPTANCE_HEADING_RE.match(line.strip()):
            in_section = True
            continue
        if in_section and _ANY_HEADING_RE.match(line.strip()):
            in_section = False
        match = _LIST_ITEM_RE.match(line) if in_section else None
        if match:
            tests.append(match.group(1).strip())
    return tests


def _criterion_tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 1 and token not in _CRITERION_STOPWORDS}


def _covers(criterion: str, test: str) -> bool:
    wanted, have = _criterion_tokens(test), _criterion_tokens(criterion)
    if not wanted:
        return True
    numbers = {token for token in wanted if token.isdigit()}
    return numbers <= have and len(wanted & have) / len(wanted) >= 0.5


def _uncovered_acceptance_tests(stage_input: dict[str, Any], pr: dict[str, Any]) -> list[str]:
    criteria = [
        " ".join(str(item.get(key) or "") for key in ("criterion", "note", "id"))
        for item in pr.get("acceptance_criteria") or []
        if isinstance(item, dict)
    ]
    return [
        test
        for test in _acceptance_tests(str(stage_input.get("work_order_body") or ""))
        if not any(_covers(criterion, test) for criterion in criteria)
    ]


def _failed_ci_pr_urls(stage_input: dict[str, Any], output_urls: set[str]) -> set[str]:
    """PRs with a failed required check; ``failed_checks`` entries are ``<pr_url>:<check>``."""
    if str(stage_input.get("checks_state") or "").strip().lower() != "failure":
        return set()
    entries = [str(item) for item in stage_input.get("failed_checks") or [] if item]
    urls = _pr_url_set_from_input(stage_input) | output_urls
    named = {url for url in urls if any(entry.startswith(f"{url}:") for entry in entries)}
    return named or urls


REVIEW_SEVERITIES = ("blocker", "major", "minor", "nit")
REVIEW_CATEGORIES = ("correctness", "security", "scope", "tests", "maintainability")


def validate_code_review_envelope(
    contract: dict[str, Any],
    output: dict[str, Any],
    stage_input: dict[str, Any],
    tool_audit: list[dict[str, Any]],
) -> list[ContractViolation]:
    """PR set equals the input set, findings name an input PR, enums are valid."""
    violations: list[ContractViolation] = []
    input_urls = _pr_url_set_from_input(stage_input or {})
    for pr in (stage_input or {}).get("prs") or []:
        if isinstance(pr, dict) and isinstance(pr.get("pr_url"), str) and pr["pr_url"].strip():
            input_urls.add(pr["pr_url"].strip())
    output_urls, pr_list = _pr_url_set_from_output(output)
    if not pr_list:
        return [ContractViolation("/result/prs", "code_review output must include result.prs with at least one PR entry")]
    if input_urls and output_urls != input_urls:
        violations.append(
            ContractViolation(
                "/result/prs",
                "code_review PR set must equal the input PR set; "
                f"missing={sorted(input_urls - output_urls)} extra={sorted(output_urls - input_urls)}",
            )
        )
    result = output.get("result") if isinstance(output.get("result"), dict) else {}
    findings = result.get("findings")
    if not isinstance(findings, list):
        return [*violations, ContractViolation("/result/findings", "code_review output must include result.findings (may be empty)")]
    allowed_urls = input_urls or output_urls
    for index, finding in enumerate(findings):
        path = f"/result/findings/{index}"
        if not isinstance(finding, dict):
            violations.append(ContractViolation(path, "finding must be an object"))
            continue
        if finding.get("severity") not in REVIEW_SEVERITIES:
            violations.append(ContractViolation(f"{path}/severity", f"severity must be one of {', '.join(REVIEW_SEVERITIES)}"))
        if finding.get("category") not in REVIEW_CATEGORIES:
            violations.append(ContractViolation(f"{path}/category", f"category must be one of {', '.join(REVIEW_CATEGORIES)}"))
        if str(finding.get("pr_url") or "").strip() not in allowed_urls:
            violations.append(ContractViolation(f"{path}/pr_url", "finding pr_url must be one of the reviewed PRs"))
        if not str(finding.get("message") or "").strip():
            violations.append(ContractViolation(f"{path}/message", "finding needs a message"))
    return violations


SEMANTIC_VALIDATORS: dict[str, SemanticValidator] = {
    "validate_coding_backend_pr_url": validate_coding_backend_pr_url,
    "validate_work_order_artifacts": validate_work_order_artifacts,
    "validate_qa_pr_url_set": validate_qa_pr_url_set,
    "validate_code_review_envelope": validate_code_review_envelope,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_stage_output(
    contract_or_stage: Any,
    output: dict[str, Any],
    stage_input: dict[str, Any] | None = None,
    tool_audit: list[dict[str, Any]] | None = None,
) -> list[ContractViolation]:
    """Validate an agent output against a stage contract.

    Runs, in order: JSON-schema structural checks, required-artifact pointer
    resolution, and registered semantic validators. Returns an empty list when
    the output satisfies the contract.
    """
    contract = _as_contract(contract_or_stage)
    stage_input = stage_input or {}
    tool_audit = tool_audit or []
    violations: list[ContractViolation] = []

    output_schema = contract.get("output_schema")
    if output_schema:
        if Draft7Validator is not None:
            validator = Draft7Validator(output_schema)
            for error in sorted(validator.iter_errors(output), key=str):
                path = "/" + "/".join(str(part) for part in error.absolute_path)
                violations.append(ContractViolation(path if path != "/" else "/", error.message))
        # When jsonschema is unavailable we skip structural checks silently.

    for label, pointer in (contract.get("required_artifacts") or {}).items():
        value = resolve_pointer(output, pointer, default=_MISSING)
        if value is _MISSING or _is_empty(value):
            violations.append(
                ContractViolation(
                    pointer,
                    f"required artifact '{label}' missing or empty at {pointer}",
                )
            )

    for name in contract.get("semantic_validators") or []:
        fn = SEMANTIC_VALIDATORS.get(name)
        if fn is None:
            violations.append(
                ContractViolation("", f"unknown semantic validator: {name}")
            )
            continue
        violations.extend(fn(contract, output, stage_input, tool_audit))

    return violations


def resolve_stage_outcome(
    contract_or_stage: Any,
    output: dict[str, Any],
) -> Literal["succeeded", "blocked"]:
    """Map an output to a stage outcome via ``outcome.pointer``.

    Defaults to ``"succeeded"`` when no outcome mapping is declared.
    """
    contract = _as_contract(contract_or_stage)
    outcome = contract.get("outcome")
    if not outcome:
        return "succeeded"
    pointer = outcome.get("pointer") or ""
    value = resolve_pointer(output, pointer, default=_MISSING)
    blocked_values = outcome.get("blocked_values") or []
    succeeded_values = outcome.get("succeeded_values") or []
    if value in blocked_values:
        return "blocked"
    if value in succeeded_values:
        return "succeeded"
    # Unknown / unresolved outcome value defaults to succeeded (fail-open in v1).
    return "succeeded"


def extract_required_artifacts(
    contract_or_stage: Any,
    output: dict[str, Any],
) -> dict[str, Any]:
    """Return {label: resolved value} for a contract's required artifacts."""
    contract = _as_contract(contract_or_stage)
    extracted: dict[str, Any] = {}
    for label, pointer in (contract.get("required_artifacts") or {}).items():
        value = resolve_pointer(output, pointer, default=_MISSING)
        if value is not _MISSING:
            extracted[label] = value
    return extracted


def summarize_stage_contract(contract_or_stage: Any) -> str:
    """Human-readable summary of a contract for use in agent system prompts."""
    contract = _as_contract(contract_or_stage)
    lines: list[str] = ["Stage output contract:"]

    schema = contract.get("output_schema")
    if isinstance(schema, dict):
        required = schema.get("required")
        if isinstance(required, list) and required:
            lines.append(
                "- Output JSON must include: " + ", ".join(str(k) for k in required) + "."
            )
        props = schema.get("properties")
        if isinstance(props, dict) and props:
            lines.append(
                "- Recognized top-level keys: " + ", ".join(sorted(props)) + "."
            )

    artifacts = contract.get("required_artifacts") or {}
    if artifacts:
        lines.append(
            "- Required artifacts (label -> location): "
            + ", ".join(f"{label} -> {ptr}" for label, ptr in artifacts.items())
            + "."
        )

    validators = contract.get("semantic_validators") or []
    if validators:
        lines.append("- Semantic checks: " + ", ".join(validators) + ".")

    outcome = contract.get("outcome")
    if isinstance(outcome, dict):
        succeeded = ", ".join(str(v) for v in outcome.get("succeeded_values") or [])
        blocked = ", ".join(str(v) for v in outcome.get("blocked_values") or [])
        lines.append(
            f"- Outcome at {outcome.get('pointer')}: succeeded={{{succeeded}}}"
            + (f", blocked={{{blocked}}}" if blocked else "")
            + "."
        )

    lines.append("Return ONLY valid JSON that satisfies this contract.")
    return "\n".join(lines)
