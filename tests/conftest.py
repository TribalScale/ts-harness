import os

import pytest


@pytest.fixture(autouse=True)
def _allow_local_internal_tokens(monkeypatch):
    """Local tests talk to the memory API and sandbox worker without provisioned tokens."""
    if not os.environ.get("MEMORY_API_TOKEN", "").strip():
        monkeypatch.setenv("MEMORY_API_ALLOW_UNAUTHENTICATED", "true")
    if not os.environ.get("SANDBOX_WORKER_TOKEN", "").strip():
        monkeypatch.setenv("SANDBOX_WORKER_ALLOW_UNAUTHENTICATED", "true")


@pytest.fixture(autouse=True)
def _last_known_good_config_outside_repo(monkeypatch, tmp_path):
    """The gateway writes a .lkg copy beside its config; keep it out of config/."""
    monkeypatch.setenv("TS_HARNESS_CONFIG_LKG", str(tmp_path / "runtime-config.lkg"))


@pytest.fixture(autouse=True)
def _reset_admin_rate_limiter():
    """Admin routes share one in-process limiter; one test's requests must not throttle the next."""
    try:
        from ts_harness.gateway import rate_limit
    except ImportError:
        yield
        return
    rate_limit._admin_limiter._hits.clear()
    yield
