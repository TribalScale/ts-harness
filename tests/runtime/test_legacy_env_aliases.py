from __future__ import annotations

from pathlib import Path

from ts_harness.implementation.config import load_active_runtime_config

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_runtime_config_loads_the_ts_harness_filename(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "ts-harness-runtime.yaml").write_text(
        (REPO_ROOT / "config" / "ts-harness-runtime.test.yaml").read_text()
    )
    monkeypatch.delenv("TS_HARNESS_CONFIG", raising=False)
    monkeypatch.delenv("TS_HARNESS_RUNTIME_CONFIG", raising=False)
    monkeypatch.setenv("TS_HARNESS_CONFIG_DIR", str(tmp_path))
    assert load_active_runtime_config() is not None


def test_runtime_config_returns_none_when_canonical_file_is_missing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TS_HARNESS_CONFIG", raising=False)
    monkeypatch.delenv("TS_HARNESS_RUNTIME_CONFIG", raising=False)
    monkeypatch.setenv("TS_HARNESS_CONFIG_DIR", str(tmp_path))
    assert load_active_runtime_config() is None
