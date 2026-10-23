#!/usr/bin/env python3
"""Coding worker entrypoint: runs the one job named by JOB_ID, then exits."""

from __future__ import annotations

import sys

from ts_harness.coding.worker import main

if __name__ == "__main__":
    sys.exit(main())
