"""Structural parity between production tmpl and CI test runtime config."""

from __future__ import annotations

from pathlib import Path

import yaml

from ts_harness.config.schema import load_runtime_config


REPO_ROOT = Path(__file__).resolve().parents[2]
TMPL = REPO_ROOT / "config" / "ts-harness-runtime.yaml.tmpl"
TEST_YAML = REPO_ROOT / "config" / "ts-harness-runtime.test.yaml"


def _agent_map(data: dict) -> dict[str, dict]:
    return {agent["id"]: agent for agent in (data.get("agents") or {}).get("list") or []}


def test_google_connector_and_toolsets_present_in_test_yaml():
    test_data = yaml.safe_load(TEST_YAML.read_text())
    assert "google-workspace" in (test_data.get("tenants") or [{}])[0].get("enabled_connectors", [])
    assert "google-workspace" in (test_data.get("connectors") or {})
    toolsets = test_data.get("toolsets") or {}
    assert "google-workspace-docs" in toolsets
    assert "google_workspace_gmail_send" in (test_data.get("policy") or {}).get(
        "approval_required_tools", []
    )


def test_pm_agent_google_docs_wiring_matches_tmpl_shape():
    tmpl = yaml.safe_load(TMPL.read_text())
    test_data = yaml.safe_load(TEST_YAML.read_text())
    tmpl_pm = _agent_map(tmpl)["pm-agent"]
    test_pm = _agent_map(test_data)["pm-agent"]
    assert test_pm.get("connector_bindings", {}).get("google-workspace") == "pm"
    assert "google-workspace-docs" in test_pm.get("toolsets", [])
    assert tmpl_pm.get("connector_bindings", {}).get("google-workspace") == "pm"
    assert "google-workspace-docs" in tmpl_pm.get("toolsets", [])


def test_artifact_library_config_present_in_tmpl_and_test_yaml():
    tmpl = yaml.safe_load(TMPL.read_text())
    test_data = yaml.safe_load(TEST_YAML.read_text())
    tmpl_library = (
        (tmpl.get("connectors") or {})
        .get("google-workspace", {})
        .get("artifact_publish", {})
        .get("library", {})
    )
    test_library = (
        (test_data.get("connectors") or {})
        .get("google-workspace", {})
        .get("artifact_publish", {})
        .get("library", {})
    )
    assert tmpl_library.get("enabled") is True
    assert test_library.get("enabled") is True
    assert "work_orders_from_prd" in (tmpl_library.get("step_phase_map") or {})
    assert "prd_request" in (test_library.get("step_phase_map") or {})


def test_test_yaml_loads_via_schema():
    load_runtime_config(TEST_YAML)


def test_implementation_config_present_in_tmpl_and_test_yaml():
    tmpl = yaml.safe_load(TMPL.read_text())
    test_data = yaml.safe_load(TEST_YAML.read_text())
    tmpl_impl = tmpl.get("implementation") or {}
    test_impl = test_data.get("implementation") or {}
    assert tmpl_impl.get("coding_backend") == "opencode"
    assert test_impl.get("coding_backend") == "opencode"
    assert tmpl_impl.get("tools_root") == "/tools"
    assert "opencode" in (tmpl_impl.get("backends") or {})
    tmpl_opencode = (tmpl_impl.get("backends") or {}).get("opencode") or {}
    assert tmpl_opencode.get("model") == "{{CODING_BACKEND_MODEL}}"
    assert tmpl_opencode.get("provider") == "{{CODING_BACKEND_PROVIDER}}"
    assert "implement_work_orders" in (tmpl_impl.get("repos") or {})
    assert "implement_work_orders" in (test_impl.get("repos") or {})
    assert "implement_jira_ticket" in (tmpl_impl.get("repos") or {})
    assert "implement_jira_ticket" in (test_impl.get("repos") or {})
    assert (tmpl_impl.get("repos") or {}).get("implement_jira_ticket", {}).get(
        "base_branch"
    ) == "{{IMPLEMENT_JIRA_TICKET_BASE_BRANCH}}"
    assert (test_impl.get("repos") or {}).get("implement_jira_ticket", {}).get(
        "base_branch"
    ) == "dev"
    assert "coding-backend" in (tmpl.get("toolsets") or {})
    assert "coding-backend" in (test_data.get("toolsets") or {})
    assert "run_coding_backend" in (tmpl.get("toolsets") or {}).get("coding-backend", [])
    tmpl_dev = _agent_map(tmpl)["dev-agent"]
    test_dev = _agent_map(test_data)["dev-agent"]
    assert "coding-backend" in tmpl_dev.get("toolsets", [])
    assert "coding-backend" in test_dev.get("toolsets", [])
    assert "run_coding_backend" in tmpl_dev.get("tools", {}).get("allow", [])
    assert "run_coding_backend" in test_dev.get("tools", {}).get("allow", [])

    config = load_runtime_config(TEST_YAML)
    assert config.implementation.coding_backend == "opencode"
    assert config.implementation.backends.opencode.provider == "anthropic"
