"""Run the LLM proxy: ``python -m ts_harness.llm_proxy``."""

from __future__ import annotations

import os
import sys


def build_from_env():
    from ts_harness.llm_proxy.app import build_app, upstreams_from_env
    from ts_harness.llm_proxy.metering import load_proxy_settings
    from ts_harness.llm_proxy.store import ModelRateLimiter, UsageStore
    from ts_harness.llm_proxy.tokens import proxy_secret

    secret = proxy_secret()
    if not secret:
        sys.exit("LLM_PROXY_SECRET is required")
    db_url = str(os.environ.get("LLM_PROXY_DB_URL") or os.environ.get("TS_HARNESS_SESSION_DB_URL") or "").strip()
    if not db_url:
        sys.exit("LLM_PROXY_DB_URL (or TS_HARNESS_SESSION_DB_URL) is required to meter usage")
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(db_url, min_size=1, max_size=int(os.environ.get("LLM_PROXY_DB_POOL_MAX", "8")), open=True)
    settings = load_proxy_settings(os.environ.get("TS_HARNESS_CONFIG"))
    return build_app(
        secret=secret,
        upstreams=upstreams_from_env(),
        pricing=settings["pricing"],
        record=UsageStore(pool.connection).record,
        allow=ModelRateLimiter(pool.connection, settings["rate_limits"]).allow,
    )


def main() -> None:
    import uvicorn

    uvicorn.run(
        build_from_env(),
        host=os.environ.get("LLM_PROXY_BIND", "127.0.0.1"),
        port=int(os.environ.get("LLM_PROXY_PORT", "18795")),
        log_level=os.environ.get("LLM_PROXY_LOG_LEVEL", "info"),
    )


if __name__ == "__main__":
    main()
