#!/usr/bin/env python3
"""Coding launcher: claims queued coding jobs and starts a worker container for each."""

from __future__ import annotations

import os
import signal
import socket
import threading

from ts_harness.coding.launcher import build_launcher, coding_schedule, run_launcher_loop
from ts_harness.memory.client import get_memory_client


def main() -> None:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    quotas, global_limit = coding_schedule()
    run_launcher_loop(
        get_memory_client(),
        build_launcher(),
        max_running=global_limit if global_limit is not None else int(os.environ.get("CODING_MAX_RUNNING") or 3),
        quotas=quotas,
        holder=f"{socket.gethostname()}-{os.getpid()}",
        stop=stop,
        interval_seconds=float(os.environ.get("CODING_LAUNCHER_INTERVAL_SECONDS") or 5),
    )


if __name__ == "__main__":
    main()
