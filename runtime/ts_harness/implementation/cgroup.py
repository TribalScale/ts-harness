"""The coding worker's memory cgroup: how often the kernel killed something for memory, and the limit."""

from __future__ import annotations

from pathlib import Path

ROOT = Path("/sys/fs/cgroup")


def oom_kills(root: Path = ROOT) -> tuple[int, str]:
    """(OOM kills in this container's cgroup so far, its memory limit) on cgroup v2; (0, "") elsewhere."""
    try:
        events = (root / "memory.events").read_text()
        kills = int(next((line.split()[1] for line in events.splitlines() if line.startswith("oom_kill ")), "0"))
    except (OSError, ValueError):
        return 0, ""
    try:
        raw = (root / "memory.max").read_text().strip()
        limit = f"{int(raw) // (1024 * 1024)} MB" if raw.isdigit() else raw
    except OSError:
        limit = ""
    return kills, limit
