from __future__ import annotations

import unittest
from types import SimpleNamespace

from ts_harness.config.mcp_settings import (
    annotate_mcp_sources,
    apply_mcp_to_config,
    merge_mcp_servers,
    redact_mcp_server,
    validate_mcp_settings_payload,
    yaml_mcp_baseline,
)

CATALOG = [
    {
        "id": "atlassian",
        "display_name": "Atlassian",
        "transport": "stdio",
        "command": "mcporter",
        "args": [],
        "env_names": [],
        "notes": "test",
    }
]


def _valid_payload(**overrides):
    payload = {
        "definition_type": "mcp_settings",
        "definition_version": "1.0.0",
        "servers": {
            "figma": {
                "transport": "streamable_http",
                "url": "https://mcp.figma.com/mcp",
                "enabled": True,
                "headers": {"Authorization": "secret:FIGMA_MCP_TOKEN"},
            }
        },
    }
    payload.update(overrides)
    return payload


class McpSettingsOverlayTests(unittest.TestCase):
    def test_unknown_top_level_key_errors(self):
        canonical, issues = validate_mcp_settings_payload(
            _valid_payload(sandbox={"mode": "docker"}),
            catalog=CATALOG,
        )
        self.assertIsNone(canonical)
        self.assertIn("unknown_field", {issue["code"] for issue in issues})

    def test_bad_definition_type_errors(self):
        _, issues = validate_mcp_settings_payload(
            {"definition_type": "runtime_settings", "definition_version": "1.0.0", "servers": {}},
            catalog=CATALOG,
        )
        self.assertIn("invalid_definition_type", {issue["code"] for issue in issues})

    def test_overlay_name_wins_and_omitted_inherits(self):
        yaml_servers = {
            "atlassian": {"transport": "stdio", "command": "mcporter", "enabled": True},
            "legacy": {"transport": "sse", "url": "https://legacy.example/mcp"},
        }
        overlay = {
            "definition_type": "mcp_settings",
            "definition_version": "1.0.0",
            "servers": {
                "atlassian": {"enabled": False},
                "figma": {
                    "transport": "streamable_http",
                    "url": "https://mcp.figma.com/mcp",
                    "enabled": True,
                },
            },
        }
        merged = merge_mcp_servers(yaml_servers, overlay)
        self.assertEqual(merged["atlassian"]["enabled"], False)
        self.assertEqual(merged["legacy"]["url"], "https://legacy.example/mcp")
        self.assertEqual(merged["figma"]["url"], "https://mcp.figma.com/mcp")
        sources = annotate_mcp_sources(yaml_servers, overlay)
        self.assertEqual(sources["atlassian"], "overlay")
        self.assertEqual(sources["legacy"], "inherited")
        self.assertEqual(sources["figma"], "overlay")

    def test_reset_is_dropping_overlay_key(self):
        yaml_servers = {"atlassian": {"transport": "stdio", "command": "mcporter", "enabled": True}}
        overlay = {"servers": {"atlassian": {"enabled": False}}}
        merged = merge_mcp_servers(yaml_servers, overlay)
        self.assertFalse(merged["atlassian"]["enabled"])
        reset = merge_mcp_servers(yaml_servers, {"servers": {}})
        self.assertTrue(reset["atlassian"]["enabled"])

    def test_freeform_command_rejected(self):
        canonical, issues = validate_mcp_settings_payload(
            _valid_payload(
                servers={
                    "evil": {
                        "transport": "stdio",
                        "command": "bash",
                        "args": ["-c", "whoami"],
                    }
                }
            ),
            catalog=CATALOG,
        )
        self.assertIsNone(canonical)
        self.assertIn("catalog_mismatch", {issue["code"] for issue in issues})

    def test_catalog_matching_stdio_accepted(self):
        canonical, issues = validate_mcp_settings_payload(
            _valid_payload(
                servers={
                    "atlassian": {
                        "transport": "stdio",
                        "command": "mcporter",
                        "args": [],
                        "enabled": True,
                    }
                }
            ),
            catalog=CATALOG,
        )
        self.assertFalse(any(issue.get("severity") == "error" for issue in issues))
        self.assertEqual(canonical["servers"]["atlassian"]["command"], "mcporter")

    def test_http_env_rejected(self):
        canonical, issues = validate_mcp_settings_payload(
            _valid_payload(
                servers={
                    "figma": {
                        "transport": "streamable_http",
                        "url": "https://mcp.figma.com/mcp",
                        "env": {"TOKEN": "secret:FIGMA"},
                    }
                }
            ),
            catalog=CATALOG,
        )
        self.assertIsNone(canonical)
        self.assertIn("env_not_allowed", {issue["code"] for issue in issues})

    def test_ftp_url_rejected(self):
        canonical, issues = validate_mcp_settings_payload(
            _valid_payload(
                servers={
                    "bad": {
                        "transport": "streamable_http",
                        "url": "ftp://example.com/mcp",
                    }
                }
            ),
            catalog=CATALOG,
        )
        self.assertIsNone(canonical)
        self.assertIn("invalid_url", {issue["code"] for issue in issues})

    def test_secret_ref_accepted_literal_rejected(self):
        ok, issues = validate_mcp_settings_payload(_valid_payload(), catalog=CATALOG)
        self.assertIsNotNone(ok)
        self.assertFalse(any(issue.get("severity") == "error" for issue in issues))
        bad, bad_issues = validate_mcp_settings_payload(
            _valid_payload(
                servers={
                    "figma": {
                        "transport": "streamable_http",
                        "url": "https://mcp.figma.com/mcp",
                        "headers": {"Authorization": "Bearer sk-live"},
                    }
                }
            ),
            catalog=CATALOG,
        )
        self.assertIsNone(bad)
        self.assertIn("invalid_secret_ref", {issue["code"] for issue in issues} | {i["code"] for i in bad_issues})

    def test_apply_and_redact(self):
        config = SimpleNamespace(
            mcp_servers={
                "atlassian": {"transport": "stdio", "command": "mcporter", "env": {"TOKEN": "secret:OLD"}}
            }
        )
        apply_mcp_to_config(
            config,
            {
                "servers": {
                    "figma": {
                        "transport": "sse",
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "secret:FIGMA"},
                    }
                }
            },
        )
        self.assertIn("atlassian", config.mcp_servers)
        self.assertIn("figma", config.mcp_servers)
        redacted = redact_mcp_server(config.mcp_servers["figma"])
        self.assertEqual(redacted["headers"]["Authorization"], "***")
        baseline = yaml_mcp_baseline(config)
        self.assertIn("figma", baseline)


if __name__ == "__main__":
    unittest.main()
