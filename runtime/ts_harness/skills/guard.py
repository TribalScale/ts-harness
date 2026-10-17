"""Skill security scanner adapted from Hermes skills_guard.py (MIT)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ts_harness.skills.threat_patterns import DESTRUCTIVE_PATTERNS, EXFILTRATION_PATTERNS, INJECTION_PATTERNS


@dataclass
class SkillScanResult:
    allowed: bool
    trust_level: str
    findings: list[str]


def scan_skill_content(content: str, *, trust_level: str = "builtin") -> SkillScanResult:
    findings: list[str] = []
    for label, patterns in (
        ("exfiltration", EXFILTRATION_PATTERNS),
        ("injection", INJECTION_PATTERNS),
        ("destructive", DESTRUCTIVE_PATTERNS),
    ):
        for pattern in patterns:
            if pattern.search(content):
                findings.append(f"{label}: {pattern.pattern}")
    if trust_level == "community" and findings:
        return SkillScanResult(False, trust_level, findings)
    if trust_level == "agent-created":
        # Never injection-allowed via scan.allowed. Content threat findings are
        # listed separately; catalog load keeps draft/pending_review unless
        # real findings force blocked. Synthetic "requires approval" is not a
        # content threat and must not wipe draft status.
        return SkillScanResult(False, trust_level, findings)
    if findings and trust_level == "trusted":
        return SkillScanResult(True, trust_level, findings)
    return SkillScanResult(not findings, trust_level, findings)


def scan_skill_file(path: Path, *, trust_level: str = "builtin") -> SkillScanResult:
    return scan_skill_content(path.read_text(encoding="utf-8"), trust_level=trust_level)
