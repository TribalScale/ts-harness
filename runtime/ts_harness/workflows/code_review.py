"""Automated code review around the ``code_review`` agent stage.

``review_precheck`` gathers the PR diff and applies deterministic diff limits and
scope findings before the agent runs. ``finalize_code_review`` merges those
findings into the agent's, computes the verdict (the model's is not trusted),
posts a ``COMMENT`` review plus the ``harness/code-review`` status, and blocks the
stage when the verdict is ``block`` so ``on_block`` can send the PR back.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from ts_harness.implementation.verification import has_gwt_acceptance
from ts_harness.integrations.github_client import GitHubClient, parse_github_pr_url
from ts_harness.workflows import merge_policy
from ts_harness.workflows.expressions import resolve_value
from ts_harness.workflows.hooks._helpers import load_workflow_payload
from ts_harness.workflows.merge_readiness import _live_program_defaults, _resolve_pr_urls
from ts_harness.workflows.system_steps import StepBlocked

logger = logging.getLogger(__name__)

STATUS_CONTEXT = "harness/code-review"
SEVERITIES = ("blocker", "major", "minor", "nit")
CATEGORIES = ("correctness", "security", "scope", "tests", "maintainability")
BLOCKING_MAJOR_CATEGORIES = frozenset({"correctness", "security", "scope"})
# Per-file and whole-review patch budgets (characters). 20k per file cut a 600-line test
# file in half and review and QA then failed the work order on what they could not see.
PATCH_LIMIT = 60_000
TOTAL_PATCH_BUDGET = 240_000
MIN_FILE_PATCH = 6_000
# Written by run_coding_backend itself (OpenCode scaffolding, artifact ignores,
# approved planning docs), so never the implementer's scope.
PIPELINE_WRITTEN = (
    ".opencode/**",
    ".gitignore",
    "docs/prd/**",
    "docs/refinery/**",
    "docs/foundry/**",
    "docs/planner/**",
)

# Test files may be added beyond a work order's ``## Files`` list: more tests are never out of
# scope. The implementer's revert, OpenCode's edit permissions and the review's scope check
# all use this list, so a test the implementer may write is never a review finding.
TEST_PATHS = (
    "test/*",
    "tests/*",
    "*/test/*",
    "*/tests/*",
    "*/__tests__/*",
    "__tests__/*",
    "features/*",
    "*/features/*",
    "*.feature",
    "*.test.*",
    "*.spec.*",
    "*test_*.py",
    "*_test.py",
    "*_test.go",
)


def is_test_path(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in TEST_PATHS)


_SCOPE_HEADING_RE = re.compile(r"^#{2,6}\s*(files|touches)\s*$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,6}\s")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+`?([^`\s]+)`?")
_INCLUDED_BOUNDARY_RE = re.compile(r"\b(?:exactly|at least)\s+(\d+)\b|\b(\d+)\s+or more\b", re.IGNORECASE)
_STRICT_GT_RE = re.compile(r"(?<![<>=!])>\s*(\d+)\b")


def _excluded(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
            return True
    return False


_TICK_RE = re.compile(r"`([^`]+)`")
_KEEP_RE = re.compile(r"\b(keep|leave|unchanged|untouched|read[- ]only|reference only|no change)\b", re.IGNORECASE)


def _looks_like_path(token: str) -> bool:
    token = token.strip()
    if not token or " " in token or token.startswith(("http://", "https://")):
        return False
    return "/" in token or "*" in token or bool(re.search(r"\.[A-Za-z0-9]{1,8}$", token)) or token.startswith(".")


def _scope_entries(work_order_body: str) -> list[tuple[list[str], str]]:
    """Each ``## Files`` / ``## Touches`` bullet as (paths it names, its text).

    Every backticked path counts, not just the first; a bare file name (``hub.d.ts``) after a
    path with a folder in the same bullet (``src/api/health.d.ts``) is in that folder.
    """
    entries: list[tuple[list[str], str]] = []
    in_section = False
    for line in str(work_order_body or "").splitlines():
        if _SCOPE_HEADING_RE.match(line.strip()):
            in_section = True
            continue
        if in_section and _HEADING_RE.match(line):
            break
        if not in_section or not _BULLET_RE.match(line):
            continue
        text = re.sub(r"^\s*[-*+]\s+", "", line).strip()
        tokens = [t.strip() for t in _TICK_RE.findall(text)]
        if not tokens:
            first = text.split()[0].rstrip(":,;") if text.split() else ""
            tokens = [first] if _looks_like_path(first) else []
        paths: list[str] = []
        folder = ""
        for token in tokens:
            if not _looks_like_path(token):
                continue
            token = token.removeprefix("./")
            if "/" in token.rstrip("/"):
                folder = token.rsplit("/", 1)[0] if not token.endswith("/") else token.rstrip("/")
            elif folder and not token.startswith(".") and "*" not in token:
                token = f"{folder}/{token}"
            paths.append(token)
        entries.append((paths, text))
    return entries


def declared_paths(work_order_body: str) -> list[str]:
    """Path globs the ``## Files`` / ``## Touches`` list allows, or [] when it constrains nothing.

    When any entry names no path ("every file that imports the old types", "the Day view
    components"), the files cannot be listed, so the list is not a scope: [] means no limit.
    A scope built from the paths alone once kept OpenCode from the files the fix needed.
    """
    entries = _scope_entries(work_order_body)
    if not entries or any(not paths for paths, _text in entries):
        return []
    return [path for paths, _text in entries for path in paths]


def required_paths(work_order_body: str) -> list[str]:
    """Concrete files the ``## Files`` list says to create, change or delete (not ones to keep)."""
    required: list[str] = []
    for paths, text in _scope_entries(work_order_body):
        if _KEEP_RE.search(text.split(":", 1)[-1] if ":" in text else text):
            continue
        for path in paths:
            if "*" in path or path.endswith("/") or path in required:
                continue
            if re.search(r"\.[A-Za-z0-9]{1,8}$", path) or Path(path).name.startswith("."):
                required.append(path)
    return required


def _in_scope(path: str, declared: list[str]) -> bool:
    for pattern in declared:
        base = pattern.rstrip("/")
        if fnmatch.fnmatch(path, pattern) or path == base or path.startswith(f"{base}/"):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]):
            return True
    return False


def included_boundaries(work_order_body: str) -> set[int]:
    """Integers the work order includes: "exactly N", "at least N", "N or more"."""
    values: set[int] = set()
    for match in _INCLUDED_BOUNDARY_RE.finditer(str(work_order_body or "")):
        for group in match.groups():
            if group:
                values.add(int(group))
    return values


def boundary_findings(pr_url: str, files: list[dict[str, Any]], work_order_body: str) -> list[dict[str, Any]]:
    """A strict ``> N`` excludes a boundary the work order says is included."""
    thresholds = included_boundaries(work_order_body)
    if not thresholds:
        return []
    findings: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for item in files:
        filename = str(item.get("filename") or "")
        for line in str(item.get("patch") or "").splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            code = line[1:]
            for match in _STRICT_GT_RE.finditer(code):
                value = int(match.group(1))
                if value not in thresholds or (filename, value) in seen:
                    continue
                if re.search(rf"(?:==|>=)\s*{value}\b|\b{value}\s*<=", code):
                    continue
                seen.add((filename, value))
                findings.append(
                    {
                        "severity": "major",
                        "category": "correctness",
                        "pr_url": pr_url,
                        "file": filename,
                        "message": (
                            f"strict comparison `> {value}` excludes {value}; "
                            "the work order includes that boundary"
                        ),
                    }
                )
    return findings


def undeclared_paths(changed: list[str], work_order_body: str) -> list[str]:
    """Paths outside ``## Files`` / ``## Touches``. An empty declaration constrains nothing."""
    declared = declared_paths(work_order_body)
    if not declared:
        return []
    if has_gwt_acceptance(work_order_body):
        declared = [*declared, "features/**"]
    return sorted(
        path
        for path in changed
        if path
        and not _in_scope(path, declared)
        and not _excluded(path, list(PIPELINE_WRITTEN))
        and not is_test_path(path)
    )


def scope_findings(
    pr_url: str, files: list[dict[str, Any]], work_order_body: str, generated: set[str] | None = None
) -> list[dict[str, Any]]:
    """Files outside the work order's ``## Files``; tests and files CI regenerates (``generated``) are fine."""
    declared = declared_paths(work_order_body)
    if not declared:
        return []
    if has_gwt_acceptance(work_order_body):
        # Delivery verification requires a feature file and steps for these work orders.
        declared = [*declared, "features/**"]
    return [
        {
            # Information for the reviewer, who judges whether the file belongs: a string match on
            # the work order's list blocked correct changes (an importer the fix had to touch).
            "severity": "minor",
            "category": "scope",
            "pr_url": pr_url,
            "file": item["filename"],
            "message": f"{item['filename']} is outside the work order's declared files; the reviewer judges whether it belongs",
        }
        for item in files
        if not _in_scope(item["filename"], declared) and not is_test_path(item["filename"])
        and item["filename"] not in (generated or set())
    ]


def _file_entry(item: dict[str, Any]) -> dict[str, Any]:
    patch = str(item.get("patch") or "")
    entry = {
        "filename": str(item.get("filename") or ""),
        "status": str(item.get("status") or ""),
        "additions": int(item.get("additions") or 0),
        "deletions": int(item.get("deletions") or 0),
        "patch": patch[:PATCH_LIMIT],
    }
    if len(patch) > PATCH_LIMIT:
        entry["patch_truncated"] = True
    return entry


def _file_identity(pr_url: str, item: dict[str, Any]) -> tuple[str, ...]:
    """What a PR file is at its head: name, status and blob sha (or the whole patch without one)."""
    blob = str(item.get("sha") or "")
    return (
        pr_url,
        str(item.get("filename") or ""),
        str(item.get("previous_filename") or ""),
        str(item.get("status") or ""),
        blob or hashlib.sha256(str(item.get("patch") or "").encode()).hexdigest(),
    )


def diff_fingerprint(content: list[tuple[str, ...]]) -> str:
    """Same value exactly when every PR changes the same files to the same content.

    A merge of the base branch that leaves the PR's own files alone keeps it; a
    conflict resolution or any edit to a reviewed file changes it.
    """
    return hashlib.sha256(json.dumps(sorted(content)).encode()).hexdigest()


def _added_file_patch(github: Any, owner: str, repo: str, path: str, head_sha: str) -> str:
    """A new file's content as a patch, for files GitHub sends without one (large diffs)."""
    import base64

    try:
        payload = github.get_file(owner, repo, path, ref=head_sha)
    except Exception:  # noqa: BLE001 - review continues with what it has
        return ""
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        return ""
    try:
        text = base64.b64decode(str(payload.get("content") or "")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""
    lines = text.splitlines()
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)


def fit_patch_budget(files: list[dict[str, Any]], budget: int = TOTAL_PATCH_BUDGET) -> None:
    """Trim the largest patches first so the whole review fits ``budget`` characters.

    Small files stay whole; every trimmed file keeps at least ``MIN_FILE_PATCH``.
    """
    total = sum(len(entry.get("patch") or "") for entry in files)
    if total <= budget:
        return
    sizes = sorted(len(entry.get("patch") or "") for entry in files)
    # Largest cap c with sum(min(size, c)) <= budget.
    remaining, count, cap = budget, len(sizes), 0
    for index, size in enumerate(sizes):
        share = remaining // (count - index)
        if size <= share:
            remaining -= size
            continue
        cap = share
        break
    cap = max(cap, MIN_FILE_PATCH)
    for entry in files:
        patch = str(entry.get("patch") or "")
        if len(patch) > cap:
            entry["patch"] = patch[:cap]
            entry["patch_truncated"] = True


def _context(task: dict[str, Any], memory: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return load_workflow_payload(memory, str(task.get("workflow_id") or ""))


def _input_extras(task: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Task inputs as ``$workflow.*`` fallbacks: system steps run without dispatch extras."""
    extras: dict[str, Any] = {}
    for item in sorted(tasks, key=lambda row: str(row.get("created_at") or "")):
        if isinstance(item.get("input"), dict):
            extras.update({k: v for k, v in item["input"].items() if not str(k).startswith("_") and v is not None})
    if isinstance(task.get("input"), dict):
        extras.update({k: v for k, v in task["input"].items() if not str(k).startswith("_") and v is not None})
    return extras


# A first review of a PR this large does a systematic sweep: later rounds only check the fixes
# and what changed, so whatever the first review misses would otherwise cost a round each.
DEEP_REVIEW_FILES = 20
DEEP_REVIEW_LINES = 1500


def _deep_review_limits() -> tuple[int, int]:
    def read(name: str, default: int) -> int:
        try:
            return max(1, int(os.environ.get(name) or default))
        except ValueError:
            return default

    return read("TS_HARNESS_DEEP_REVIEW_FILES", DEEP_REVIEW_FILES), read("TS_HARNESS_DEEP_REVIEW_LINES", DEEP_REVIEW_LINES)


def _earlier_reviews(task: dict[str, Any], tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Earlier rounds' code review results for this workflow, oldest first."""
    created = str(task.get("created_at") or "")
    found = []
    for item in sorted(tasks, key=lambda row: str(row.get("created_at") or "")):
        if item.get("step_name") != "code_review" or item.get("status") != "succeeded":
            continue
        if created and str(item.get("created_at") or "") >= created:
            continue
        output = item.get("output") if isinstance(item.get("output"), dict) else {}
        result = output.get("result") if isinstance(output.get("result"), dict) else output
        if isinstance(result.get("prs"), list):
            found.append(result)
    return found


def review_context(
    task: dict[str, Any],
    tasks: list[dict[str, Any]],
    prs: list[dict[str, Any]],
    files: list[dict[str, Any]],
    *,
    github: Any,
) -> list[dict[str, Any]]:
    """``prs`` with each PR's review round, how deep to review it, and what changed since the last round.

    ``review_depth`` is ``full_sweep`` (first review of a large PR), ``full`` (first review, or
    the previous round cannot be compared) or ``follow_up`` (check the previous findings and the
    files changed since then).
    """
    earlier = _earlier_reviews(task, tasks)
    max_files, max_lines = _deep_review_limits()
    out = []
    for pr in prs:
        url = pr["pr_url"]
        pr_files = [entry for entry in files if entry.get("pr_url") == url]
        lines = sum(int(entry.get("additions") or 0) + int(entry.get("deletions") or 0) for entry in pr_files)
        large = len(pr_files) >= max_files or lines >= max_lines
        previous = next(
            (
                (review, item)
                for review in reversed(earlier)
                for item in review.get("prs") or []
                if isinstance(item, dict) and item.get("pr_url") == url
            ),
            None,
        )
        entry = {**pr, "review_round": 1 + sum(
            1 for review in earlier if any(isinstance(i, dict) and i.get("pr_url") == url for i in review.get("prs") or [])
        )}
        if previous is None:
            entry["review_depth"] = "full_sweep" if large else "full"
            out.append(entry)
            continue
        review, item = previous
        previous_sha = str(item.get("head_sha") or "")
        entry["previous_head_sha"] = previous_sha
        entry["previous_findings"] = [
            finding for finding in review.get("findings") or []
            if isinstance(finding, dict) and finding.get("pr_url", url) == url and is_blocking(finding)
        ]
        changed: list[str] | None = None
        parsed = parse_github_pr_url(url)
        if previous_sha and parsed and previous_sha != pr.get("head_sha"):
            try:
                compared = github.compare(parsed[0], parsed[1], previous_sha, str(pr.get("head_sha") or ""))
                changed = sorted({str(f.get("filename") or "") for f in compared.get("files") or [] if f.get("filename")})
            except Exception:  # noqa: BLE001 - without the comparison the round is a full review
                changed = None
        elif previous_sha and previous_sha == pr.get("head_sha"):
            changed = []
        if changed is None:
            entry["review_depth"] = "full"
        else:
            entry["review_depth"] = "follow_up"
            entry["changed_since_last_review"] = changed
        out.append(entry)
    return out


def review_precheck(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    github: GitHubClient | None = None,
    program_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    workflow, tasks = _context(task, memory)
    pr_urls = _resolve_pr_urls(
        {key: config[key] for key in ("pr_url", "pr_urls") if key in config}, tasks=tasks, workflow=workflow
    )
    if not pr_urls:
        raise ValueError("review_precheck resolved no PR URLs")
    work_order_body = str(
        resolve_value(config.get("work_order_body"), tasks=tasks, workflow=workflow, extras=_input_extras(task, tasks)) or ""
    )
    refresh = bool(resolve_value(config.get("refresh"), tasks=tasks, workflow=workflow)) if config.get("refresh") else False
    defaults = program_defaults if program_defaults is not None else _live_program_defaults()
    limits = dict(merge_policy.policy_block(defaults).get("diff_limits") or {})
    exclude = [str(item) for item in limits.get("exclude") or []]
    github = github or GitHubClient.from_env()

    prs: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    over: list[str] = []
    totals = {"files": 0, "additions": 0, "deletions": 0, "excluded_files": 0, "pipeline_files": 0}
    content: list[tuple[str, ...]] = []
    for pr_url in pr_urls:
        parsed = parse_github_pr_url(pr_url)
        if parsed is None:
            raise ValueError(f"review_precheck cannot parse PR URL: {pr_url}")
        owner, repo, number = parsed
        pull = github.get_pull(owner, repo, number)
        head_sha = str((pull.get("head") or {}).get("sha") or "")
        prs.append({"pr_url": pr_url, "head_sha": head_sha, "title": str(pull.get("title") or "")})
        counted = []
        for item in github.list_pull_files(owner, repo, number):
            content.append(_file_identity(pr_url, item))
            entry = {**_file_entry(item), "pr_url": pr_url}
            if not entry["patch"] and entry["status"] == "added" and entry["additions"] and head_sha:
                entry["patch"] = _added_file_patch(github, owner, repo, entry["filename"], head_sha)[:PATCH_LIMIT]
            if _excluded(entry["filename"], list(PIPELINE_WRITTEN)):
                totals["pipeline_files"] += 1
                continue
            if _excluded(entry["filename"], exclude):
                totals["excluded_files"] += 1
                continue
            counted.append(entry)
        lines = sum(entry["additions"] + entry["deletions"] for entry in counted)
        max_files, max_lines = limits.get("max_files"), limits.get("max_changed_lines")
        if (max_files and len(counted) > int(max_files)) or (max_lines and lines > int(max_lines)):
            over.append(f"{pr_url} files={len(counted)} lines={lines}")
        totals["files"] += len(counted)
        totals["additions"] += sum(entry["additions"] for entry in counted)
        totals["deletions"] += sum(entry["deletions"] for entry in counted)
        files.extend(counted)
        from ts_harness.implementation.generated_artifacts import generated_files_on_github

        generated = generated_files_on_github(github, owner, repo, head_sha) if head_sha and declared_paths(work_order_body) else set()
        findings.extend(scope_findings(pr_url, counted, work_order_body, generated))
        findings.extend(boundary_findings(pr_url, counted, work_order_body))
    fit_patch_budget(files)
    prs = review_context(task, tasks, prs, files, github=github)
    if over:
        raise StepBlocked(
            "diff_limit_exceeded: " + "; ".join(over),
            output={"prs": prs, "diff_stats": totals, "diff_limits": limits},
        )
    return {
        "prs": prs,
        "files": files,
        "diff_stats": totals,
        "scope_findings": findings,
        "refresh": refresh,
        "diff_fingerprint": diff_fingerprint(content),
    }


def is_blocking(finding: dict[str, Any]) -> bool:
    severity = str(finding.get("severity") or "")
    return severity == "blocker" or (
        severity == "major" and str(finding.get("category") or "") in BLOCKING_MAJOR_CATEGORIES
    )


def compute_verdict(findings: list[dict[str, Any]]) -> str:
    return "block" if any(is_blocking(finding) for finding in findings) else "pass"


def merge_findings(agent: list[dict[str, Any]], deterministic: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    covered = {
        (str(f.get("pr_url") or ""), str(f.get("file") or ""))
        for f in deterministic
        if isinstance(f, dict) and f.get("category") == "scope"
    }
    agent_kept = [
        f
        for f in agent
        if not (
            isinstance(f, dict)
            and f.get("category") == "scope"
            and (str(f.get("pr_url") or ""), str(f.get("file") or "")) in covered
        )
    ]
    for finding in [*agent_kept, *deterministic]:
        if not isinstance(finding, dict):
            continue
        key = (str(finding.get("file") or ""), str(finding.get("message") or ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(dict(finding))
    return merged


def _finding_line(finding: dict[str, Any]) -> str:
    where = str(finding.get("file") or "")
    if where and finding.get("line"):
        where = f"{where}:{finding['line']}"
    prefix = f"[{finding.get('severity')}/{finding.get('category')}]"
    return f"- {prefix} {where + ' ' if where else ''}{finding.get('message') or ''}".rstrip()


def review_feedback_text(findings: list[dict[str, Any]], summary: str = "") -> str:
    blocking = [finding for finding in findings if is_blocking(finding)]
    others = [finding for finding in findings if not is_blocking(finding)]
    parts = []
    if summary:
        parts.append(summary)
    parts.append("Blocking findings (fix every one):")
    parts.extend(_finding_line(finding) for finding in blocking)
    if others:
        parts.append("Other findings (fix if cheap):")
        parts.extend(_finding_line(finding) for finding in others)
    return "\n".join(parts)


def _review_body(verdict: str, findings: list[dict[str, Any]], summary: str) -> str:
    blocking = sum(1 for finding in findings if is_blocking(finding))
    lines = [
        f"**TS Harness code review: {verdict}** ({len(findings)} findings, {blocking} blocking)",
        "",
        "This is an automated COMMENT review; a human still approves and merges.",
    ]
    if summary:
        lines += ["", summary]
    general = [finding for finding in findings if not (finding.get("file") and finding.get("line"))]
    if general:
        lines += ["", *(_finding_line(finding) for finding in general)]
    return "\n".join(lines)


def _post(
    github: GitHubClient,
    pr: dict[str, Any],
    verdict: str,
    findings: list[dict[str, Any]],
    summary: str,
    warnings: list[str],
) -> None:
    pr_url, head_sha = str(pr.get("pr_url") or ""), str(pr.get("head_sha") or "")
    parsed = parse_github_pr_url(pr_url)
    if parsed is None or not head_sha:
        warnings.append(f"review_not_posted:{pr_url}: missing PR or head SHA")
        return
    owner, repo, number = parsed
    mine = [finding for finding in findings if finding.get("pr_url") in (None, "", pr_url)]
    comments = [
        {"path": str(finding["file"]), "line": int(finding["line"]), "side": "RIGHT", "body": _finding_line(finding)[2:]}
        for finding in mine
        if finding.get("file") and isinstance(finding.get("line"), int) and finding["line"] > 0
    ]
    try:
        github.create_review(owner, repo, number, commit_id=head_sha, body=_review_body(verdict, mine, summary), comments=comments)
    except Exception as exc:  # noqa: BLE001 - posting is best effort
        warnings.append(f"review_post_failed:{pr_url}: {exc}")
    blocking = sum(1 for finding in mine if is_blocking(finding))
    try:
        github.create_status(
            owner,
            repo,
            head_sha,
            state="success" if verdict == "pass" else "failure",
            context=STATUS_CONTEXT,
            description=f"{verdict}: {blocking} blocking of {len(mine)} findings",
        )
    except Exception as exc:  # noqa: BLE001 - posting is best effort
        warnings.append(f"status_post_failed:{pr_url}: {exc}")


def publish_override_status(prs: list[dict[str, Any]], actor: str, *, github: GitHubClient | None = None) -> list[str]:
    """Set ``harness/code-review`` to success on each PR head after a human accepted a blocked review."""
    warnings: list[str] = []
    try:
        client = github or GitHubClient.from_env()
    except Exception as exc:  # noqa: BLE001 - posting is best effort
        return [f"status_not_posted: {exc}"]
    for pr in prs:
        pr_url, head_sha = str(pr.get("pr_url") or ""), str(pr.get("head_sha") or "")
        parsed = parse_github_pr_url(pr_url)
        if parsed is None or not head_sha:
            warnings.append(f"status_not_posted:{pr_url}: missing PR or head SHA")
            continue
        try:
            client.create_status(
                *parsed[:2],
                head_sha,
                state="success",
                context=STATUS_CONTEXT,
                description=f"accepted by {actor} (human override)"[:140],
            )
        except Exception as exc:  # noqa: BLE001 - posting is best effort
            warnings.append(f"status_post_failed:{pr_url}: {exc}")
    return warnings


def finalize_code_review(
    task: dict[str, Any],
    config: dict[str, Any],
    *,
    memory: Any,
    github: GitHubClient | None = None,
) -> dict[str, Any]:
    workflow, tasks = _context(task, memory)
    review = resolve_value(config.get("review") or "$prior.code_review.output", tasks=tasks, workflow=workflow) or {}
    precheck = resolve_value(config.get("precheck") or "$prior.review_precheck.output", tasks=tasks, workflow=workflow) or {}
    result = dict((review or {}).get("result") or {}) if isinstance(review, dict) else {}
    agent_findings = [finding for finding in result.get("findings") or [] if isinstance(finding, dict)]
    findings = merge_findings(agent_findings, list((precheck or {}).get("scope_findings") or []))
    verdict = compute_verdict(findings)
    warnings: list[str] = []
    model_verdict = str(result.get("verdict") or "")
    if model_verdict and model_verdict != verdict:
        warnings.append(f"verdict_mismatch: model={model_verdict} computed={verdict}")
    summary = str(result.get("summary") or "")
    prs = list((precheck or {}).get("prs") or result.get("prs") or [])
    if prs:
        try:
            client = github or GitHubClient.from_env()
        except Exception as exc:  # noqa: BLE001 - posting is best effort
            client = None
            warnings.append(f"review_not_posted: {exc}")
        for pr in prs if client is not None else []:
            _post(client, pr, verdict, findings, summary, warnings)
    output = {"verdict": verdict, "findings": findings, "prs": prs, "warnings": warnings}
    if verdict == "block":
        from ts_harness.implementation.revision_scope import revision_files_from_findings
        blocking = sum(1 for finding in findings if is_blocking(finding))
        raise StepBlocked(
            f"code_review_blocked: {blocking} blocking findings",
            output={
                **output,
                "review_feedback": review_feedback_text(findings, summary),
                "revision_files": revision_files_from_findings(findings),
            },
        )
    return output
