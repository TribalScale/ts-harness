"""Resolve ``merge_readiness.policy`` for a repo: required check names and categories.

Without a ``policy`` block only the legacy ``required_checks`` list applies and
nothing new is enforced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_CATEGORIES = ("build", "lint", "test", "security")
CLAW_CONTEXTS = frozenset({"harness/independent-review", "harness/code-review", "harness/security-scan"})
MODES = ("warn", "enforce")
DEFAULT_GRACE_SECONDS = 900
_STATE_RANK = {"failure": 3, "error": 3, "missing": 2, "pending": 1, "success": 0}


@dataclass
class RepoPolicy:
    mode: str = "warn"
    required_names: list[str] = field(default_factory=list)
    categories: dict[str, list[str]] = field(default_factory=dict)
    uncovered: list[str] = field(default_factory=list)
    grace_seconds: int = DEFAULT_GRACE_SECONDS
    require_branch_protection: bool = False
    configured: bool = False

    @property
    def enforce(self) -> bool:
        return self.configured and self.mode == "enforce"

    def pinned(self) -> dict[str, Any]:
        """Snapshot stored on a pinned PR head."""
        if not self.configured:
            return {}
        return {
            "mode": self.mode,
            "categories": {name: list(checks) for name, checks in self.categories.items()},
            "uncovered": list(self.uncovered),
            "grace_seconds": self.grace_seconds,
        }


def policy_block(program_defaults: dict[str, Any] | None) -> dict[str, Any]:
    readiness = dict((program_defaults or {}).get("merge_readiness") or {})
    return dict(readiness.get("policy") or {})


def resolve(repo: str, program_defaults: dict[str, Any] | None) -> RepoPolicy:
    readiness = dict((program_defaults or {}).get("merge_readiness") or {})
    key = str(repo or "").strip().lower()
    legacy = [str(name) for name in (dict(readiness.get("required_checks") or {}).get(key) or [])]
    raw = readiness.get("policy")
    if not isinstance(raw, dict):
        return RepoPolicy(required_names=legacy)

    categories_by_repo = {str(k).lower(): v for k, v in dict(raw.get("categories") or {}).items()}
    categories = {
        str(name): [str(check) for check in checks or []]
        for name, checks in dict(categories_by_repo.get(key) or {}).items()
    }
    required_categories = [str(c) for c in (raw.get("required_categories") or DEFAULT_CATEGORIES)]
    names = list(legacy)
    for checks in categories.values():
        for check in checks:
            if check not in names:
                names.append(check)
    mode = str(raw.get("mode") or "warn")
    return RepoPolicy(
        mode=mode if mode in MODES else "warn",
        required_names=names,
        categories=categories,
        uncovered=[name for name in required_categories if not categories.get(name)],
        grace_seconds=int(raw.get("missing_check_grace_seconds", DEFAULT_GRACE_SECONDS)),
        require_branch_protection=bool(raw.get("require_branch_protection", False)),
        configured=True,
    )


def category_results(categories: dict[str, list[str]], checks: dict[str, str]) -> dict[str, str]:
    """Worst state of each category's checks (failure > missing > pending > success)."""
    results: dict[str, str] = {}
    for name, names in categories.items():
        states = [str(checks.get(check, "missing")) for check in names] or ["missing"]
        worst = max(states, key=lambda state: _STATE_RANK.get(state, 1))
        results[name] = "failure" if worst == "error" else worst
    return results
