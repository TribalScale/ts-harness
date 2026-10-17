#!/usr/bin/env python3
"""Score open-ended agent answers with a live model judge.

uv run --extra dev python scripts/evals/run_agent_output_judge.py

Each case runs once at temperature 0. The process exits 0 only when the judge
marks every answer as passing. This script calls a live model and is not part
of the default pytest run.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "runtime"))


def main() -> int:
    from ts_harness.evals.output_judge import run_live

    return 0 if run_live() else 1


if __name__ == "__main__":
    raise SystemExit(main())
