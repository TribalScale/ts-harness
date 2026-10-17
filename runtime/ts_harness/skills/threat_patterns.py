"""Threat regex patterns adapted from Hermes threat_patterns.py (MIT)."""

from __future__ import annotations

import re

EXFILTRATION_PATTERNS = [
    re.compile(r"curl\s+.*\|\s*bash", re.I),
    re.compile(r"wget\s+.*\|\s*sh", re.I),
    re.compile(r"discord\.com/api/webhooks", re.I),
]
INJECTION_PATTERNS = [
    re.compile(r"ignore\s+previous\s+instructions", re.I),
    re.compile(r"system\s+prompt", re.I),
]
DESTRUCTIVE_PATTERNS = [
    re.compile(r"rm\s+-rf", re.I),
    re.compile(r"drop\s+table", re.I),
]
