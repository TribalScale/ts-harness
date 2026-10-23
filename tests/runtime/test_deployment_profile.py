"""Deployment profile defaults and the env > overrides > profile precedence."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

import yaml

from ts_harness.config.deployment_profile import (
    PROFILE_DEFAULTS,
    current_profile,
    normalize_profile,
    parse_lane_caps,
    resolve_setting,
)
from ts_harness.config.schema import DeploymentConfig, parse_runtime_config

REPO_ROOT = Path(__file__).resolve().parents[2]


class _Runtime:
    def __init__(self, profile: str = "single_host", overrides: dict | None = None):
        self.deployment = DeploymentConfig(profile=profile, overrides=overrides or {})


class NormalizeProfileTests(unittest.TestCase):
    def test_blank_and_unrendered_template_mean_single_host(self):
        for value in (None, "", "  ", "{{DEPLOYMENT_PROFILE}}"):
            self.assertEqual(normalize_profile(value), "single_host")

    def test_known_profiles_pass_through(self):
        self.assertEqual(normalize_profile("Scale_Out"), "scale_out")

    def test_unknown_profile_is_an_error(self):
        with self.assertRaises(ValueError):
            normalize_profile("kubernetes")


class ResolveSettingTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in (
            "DEPLOYMENT_PROFILE",
            "CODING_LAUNCHER",
            "CODING_MAX_RUNNING",
            "WORKFLOW_RUNNER_LANE_CAPS",
            "GITHUB_RECONCILE_INTERVAL_SECONDS",
        ):
            os.environ.pop(key, None)

    def test_single_host_defaults(self):
        runtime = _Runtime()
        self.assertEqual(current_profile(runtime), "single_host")
        self.assertEqual(resolve_setting("coding_launcher", runtime), "local")
        self.assertEqual(resolve_setting("coding_max_running", runtime), 3)
        self.assertEqual(resolve_setting("github_reconcile_interval_seconds", runtime), 60)

    def test_scale_out_defaults(self):
        runtime = _Runtime("scale_out")
        self.assertEqual(resolve_setting("coding_launcher", runtime), "ecs")
        self.assertEqual(resolve_setting("coding_max_running", runtime), 20)

    def test_override_beats_profile_default(self):
        runtime = _Runtime(overrides={"coding_max_running": 5})
        self.assertEqual(resolve_setting("coding_max_running", runtime), 5)

    def test_env_beats_override(self):
        runtime = _Runtime(overrides={"coding_max_running": 5})
        os.environ["CODING_MAX_RUNNING"] = "7"
        self.assertEqual(resolve_setting("coding_max_running", runtime), 7)

    def test_env_profile_beats_runtime_config(self):
        os.environ["DEPLOYMENT_PROFILE"] = "scale_out"
        self.assertEqual(current_profile(_Runtime("single_host")), "scale_out")

    def test_bad_integer_falls_back_to_default(self):
        os.environ["CODING_MAX_RUNNING"] = "many"
        self.assertEqual(resolve_setting("coding_max_running", _Runtime()), 3)

    def test_unknown_setting_is_rejected(self):
        with self.assertRaises(KeyError):
            resolve_setting("gpu_count", _Runtime())

    def test_both_profiles_define_every_setting(self):
        self.assertEqual(set(PROFILE_DEFAULTS["single_host"]), set(PROFILE_DEFAULTS["scale_out"]))


class LaneCapsTests(unittest.TestCase):
    def test_parses_and_skips_bad_entries(self):
        self.assertEqual(parse_lane_caps("fast=10, agent=3,coding=x,=4,zero=0"), {"fast": 10, "agent": 3})


class RuntimeConfigParsingTests(unittest.TestCase):
    def _minimal(self, **extra):
        data = {
            "gateway": {"token": "t"},
            "agents": {"list": [{"id": "orchestrator", "default": True, "model": "openai/gpt"}]},
        }
        data.update(extra)
        return data

    def test_missing_section_defaults_to_single_host(self):
        config = parse_runtime_config(self._minimal())
        self.assertEqual(config.deployment.profile, "single_host")
        self.assertEqual(config.deployment.overrides, {})

    def test_section_is_parsed(self):
        config = parse_runtime_config(
            self._minimal(deployment={"profile": "scale_out", "overrides": {"coding_max_running": 30}})
        )
        self.assertEqual(config.deployment.profile, "scale_out")
        self.assertEqual(config.deployment.overrides, {"coding_max_running": 30})

    def test_template_declares_the_section(self):
        tmpl = yaml.safe_load((REPO_ROOT / "config" / "ts-harness-runtime.yaml.tmpl").read_text())
        self.assertEqual(tmpl["deployment"]["profile"], "{{DEPLOYMENT_PROFILE}}")


if __name__ == "__main__":
    unittest.main()
