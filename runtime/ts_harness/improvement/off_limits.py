"""What the self-improvement loop may never change on its own.

The loop (failure digests, proposals, canaries, self-filed fixes) may propose changes to prompts,
skills, workflow definitions and model choices, and may roll its own experiments back. It may never
weaken security, gates, permissions or budgets by itself: a change that touches anything below
needs a person to write or approve it, and later PRs enforce that (a proposal or patch whose files
or settings match :func:`is_off_limits` is refused, or routed to a person, never auto-applied).

Each entry is ``{"kind": "path" | "setting" | "constant", "match": ..., "why": ...}``:

* ``path``: a repository path, exact or a directory prefix ending in ``/``; ``"path#Section"`` names
  one ``## Section`` of a Markdown skill (the rest of that file is not off limits);
* ``setting``: a runtime setting (dotted; ``*`` matches anything after it) or an environment variable;
* ``constant``: ``module:NAME`` of a code constant whose value is a limit.

The list covers itself and the metrics and guard code, so the loop cannot move its own goalposts.
"""

from __future__ import annotations

import fnmatch
from typing import Any

OFF_LIMITS: tuple[dict[str, str], ...] = (
    # Security rules
    {"kind": "path", "match": "runtime/ts_harness/implementation/security_scan.py", "why": "security scan rules"},
    {"kind": "path", "match": "runtime/ts_harness/skills/guard.py", "why": "skill content guard"},
    {"kind": "path", "match": "runtime/ts_harness/skills/threat_patterns.py", "why": "threat patterns"},
    {"kind": "path", "match": "runtime/ts_harness/policy/", "why": "policy engine and permissions"},
    {"kind": "path", "match": "policy/", "why": "OPA policies"},
    {"kind": "path", "match": "runtime/ts_harness/secrets/", "why": "secret handling"},
    {"kind": "path", "match": "SECURITY.md", "why": "security policy"},
    {"kind": "setting", "match": "policy.*", "why": "tool and data policy"},
    {"kind": "setting", "match": "sandbox.*", "why": "sandbox limits"},
    {"kind": "setting", "match": "program_defaults.merge_readiness.policy.host_security", "why": "host security scan"},
    # Gate blocking rules
    {"kind": "path", "match": "runtime/ts_harness/workflows/code_review.py", "why": "code_review.is_blocking and the gate verdict"},
    {"kind": "path", "match": "skills/review/governed-code-review/SKILL.md#Severity", "why": "what blocks a PR"},
    {"kind": "path", "match": "skills/review/governed-code-review/SKILL.md#Output Contract", "why": "the gate's blocking verdict"},
    {"kind": "path", "match": "skills/review/governed-code-review/SKILL.md#Untrusted input", "why": "prompt-injection rule"},
    {"kind": "path", "match": "skills/qa/governed-qa-gate/SKILL.md#Output Contract (canonical QA envelope)", "why": "the QA verdict"},
    {"kind": "path", "match": "skills/qa/governed-qa-gate/SKILL.md#Failure Behavior", "why": "when QA fails"},
    {"kind": "path", "match": "runtime/ts_harness/workflows/stage_contracts.py", "why": "stage output contracts"},
    {"kind": "path", "match": "runtime/ts_harness/workflows/merge_policy.py", "why": "required checks"},
    {"kind": "path", "match": "runtime/ts_harness/workflows/merge_readiness.py", "why": "merge readiness"},
    {"kind": "path", "match": "runtime/ts_harness/workflows/repo_governance.py", "why": "branch protection"},
    {"kind": "setting", "match": "program_defaults.merge_readiness.*", "why": "required checks and merge policy"},
    {"kind": "setting", "match": "program_defaults.governance.*", "why": "independent review"},
    {"kind": "setting", "match": "TS_HARNESS_CARRY_FORWARD_VERDICTS", "why": "re-using gate verdicts"},
    # Permissions and access
    {"kind": "path", "match": "runtime/ts_harness/gateway/access.py", "why": "admin access"},
    {"kind": "path", "match": "runtime/ts_harness/gateway/auth.py", "why": "authentication"},
    {"kind": "path", "match": "runtime/ts_harness/gateway/program_access.py", "why": "program access"},
    {"kind": "path", "match": "runtime/ts_harness/gateway/knowledge_access.py", "why": "knowledge access"},
    {"kind": "path", "match": "scripts/memory_routes/program_access.py", "why": "program access (memory API)"},
    {"kind": "setting", "match": "identity.*", "why": "roles, groups and permissions"},
    # Budgets
    {"kind": "path", "match": "runtime/ts_harness/budgets.py", "why": "team and program budgets"},
    {"kind": "path", "match": "runtime/ts_harness/gateway/budgets_bff.py", "why": "budget admin"},
    {"kind": "path", "match": "scripts/memory_routes/budgets.py", "why": "budget ledger"},
    {"kind": "path", "match": "runtime/ts_harness/llm_proxy/", "why": "metering and rate limits"},
    {"kind": "path", "match": "runtime/ts_harness/llm_usage.py", "why": "usage recording"},
    {"kind": "path", "match": "runtime/ts_harness/improvement/budget.py", "why": "the improvement cap"},
    {"kind": "path", "match": "database/migrations/041_team_budgets.sql", "why": "budget SQL"},
    {"kind": "path", "match": "database/migrations/052_program_budgets.sql", "why": "budget SQL"},
    {"kind": "path", "match": "database/migrations/055_improvement_budget.sql", "why": "improvement ledger"},
    {"kind": "setting", "match": "budgets.*", "why": "every budget, including improvement_monthly_usd"},
    {"kind": "setting", "match": "TS_HARNESS_IMPROVEMENT_MONTHLY_USD", "why": "the improvement cap"},
    # The arbiter's limits and override guards
    {"kind": "path", "match": "runtime/ts_harness/workflows/stuck_arbiter.py", "why": "arbiter actions and override guards"},
    {"kind": "path", "match": "runtime/ts_harness/workflows/review_arbiter.py", "why": "review arbitration"},
    {"kind": "constant", "match": "ts_harness.workflows.stuck_arbiter:MAX_ACTIONS_PER_STEP", "why": "arbiter actions per step"},
    {"kind": "constant", "match": "ts_harness.workflows.stuck_arbiter:MAX_PER_SWEEP", "why": "arbiter actions per sweep"},
    {"kind": "constant", "match": "ts_harness.workflows.stuck_arbiter:MIN_CONFIDENCE", "why": "arbiter confidence floor"},
    {"kind": "setting", "match": "TS_HARNESS_STUCK_ARBITER", "why": "arbiter mode"},
    # The metrics, the guard and this list
    {"kind": "path", "match": "runtime/ts_harness/workflows/delivery_metrics.py", "why": "target metrics"},
    {"kind": "path", "match": "runtime/ts_harness/workflows/ticket_story.py", "why": "what the metrics read"},
    {"kind": "path", "match": "runtime/ts_harness/improvement/guard.py", "why": "autonomy and post-merge guard"},
    {"kind": "path", "match": "runtime/ts_harness/improvement/versions.py", "why": "run tagging the comparisons rely on"},
    {"kind": "path", "match": "runtime/ts_harness/improvement/off_limits.py", "why": "this list"},
    {"kind": "path", "match": "runtime/ts_harness/workflows/human_decisions.py", "why": "what counts as a person stepping in"},
    # The loop's own rules: the replay pass rule, the canary stop rule, what it may act on
    {"kind": "path", "match": "runtime/ts_harness/improvement/", "why": "the loop's pass rule, stop rule and screening"},
    {"kind": "path", "match": "database/migrations/056_improvement_proposals.sql", "why": "the proposal lifecycle"},
    {"kind": "path", "match": "scripts/memory_routes/improvements.py", "why": "the proposal lifecycle"},
    {"kind": "setting", "match": "TS_HARNESS_CANARIES", "why": "whether canaries run"},
    {"kind": "setting", "match": "TS_HARNESS_CANARY_SHARE", "why": "the canary's exposure"},
    {"kind": "setting", "match": "TS_HARNESS_IMPROVEMENT_*", "why": "the loop's own switches and model"},
    {"kind": "constant", "match": "ts_harness.improvement.canary:MIN_TICKETS", "why": "canary sample size"},
    {"kind": "constant", "match": "ts_harness.improvement.canary:AUTONOMY_MARGIN", "why": "canary stop rule"},
    {"kind": "constant", "match": "ts_harness.improvement.replay:CATCH_MARGIN", "why": "replay pass rule"},
    # The harness-level loop: its privacy filter, switches and self-fix limits
    {"kind": "path", "match": "runtime/ts_harness/improvement/sanitize.py", "why": "keeps client data out of harness issues"},
    {"kind": "path", "match": "runtime/ts_harness/improvement/harness_loop.py", "why": "what is filed against the harness"},
    {"kind": "path", "match": "runtime/ts_harness/improvement/self_fix.py", "why": "self-fix rules and rate limits"},
    {"kind": "path", "match": "scripts/memory_routes/harness_issues.py", "why": "one self-fix per issue"},
    {"kind": "path", "match": "database/migrations/057_harness_self_issues.sql", "why": "harness issue ledger"},
    {"kind": "constant", "match": "ts_harness.improvement.self_fix:MAX_OPEN_FIXES", "why": "open self-fix PRs"},
    {"kind": "constant", "match": "ts_harness.improvement.self_fix:MAX_FIXES_PER_WEEK", "why": "self-fixes per week"},
    {"kind": "setting", "match": "TS_HARNESS_SELF_*", "why": "the harness-level loop's switches"},
    {"kind": "setting", "match": "program_defaults.harness_loop.*", "why": "the harness-level loop's switches"},
)


def _section(markdown_ref: str) -> tuple[str, str]:
    path, _, section = markdown_ref.partition("#")
    return path, section


def is_off_limits(*, path: str = "", setting: str = "", constant: str = "", section: str = "") -> dict[str, Any] | None:
    """The first ``OFF_LIMITS`` entry that covers ``path`` (and ``## section`` of it), ``setting`` or
    ``constant``; ``None`` when the loop may change it (still through its normal review).

    A Markdown path with no ``section`` matches any of its listed sections: a whole-file change
    touches them all.
    """
    path = path.strip().removeprefix("./") if path else ""
    for entry in OFF_LIMITS:
        kind, match = entry["kind"], entry["match"]
        if kind == "path" and path:
            target, wanted = _section(match)
            hit = path == target or (target.endswith("/") and path.startswith(target))
            if hit and (not wanted or not section or section.strip() == wanted):
                return dict(entry)
        elif kind == "setting" and setting and fnmatch.fnmatchcase(setting.strip(), match):
            return dict(entry)
        elif kind == "constant" and constant and constant.strip() == match:
            return dict(entry)
    return None
