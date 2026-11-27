"""The harness reads the backend spec for a web job, OpenCode can say it is blocked, and fix passes
run only the tests for what changed."""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ts_harness.implementation import backend_contract as bc
from ts_harness.implementation.backends.opencode import (
    TEST_SCOPE_RULE,
    OpenCodeBackend,
    build_implement_prompt,
    read_blocked_report,
)

SPEC = '{"openapi": "3.1.0", "paths": {"/schedule/week": {}}}'


def _content(text):
    return {"content": base64.b64encode(text.encode()).decode()}


class FakeGitHub:
    def __init__(self, files, default_branch="dev", only_on=None):
        self.files = files
        self.default_branch = default_branch
        self.only_on = only_on or {}
        self.reads = []

    def get_file(self, owner, name, path, *, ref):
        self.reads.append((f"{owner}/{name}", path, ref))
        key = (f"{owner}/{name}", path)
        if key not in self.files or (key in self.only_on and ref != self.only_on[key]):
            raise RuntimeError("404")
        return _content(self.files[key])

    def request(self, method, path):
        return {"default_branch": self.default_branch}


class BackendContractTests(unittest.TestCase):
    def test_the_web_repos_declared_backend_is_read_with_the_harness_access(self):
        github = FakeGitHub({
            ("org/web", ".ts-harness.yml"): "contract:\n  openapi:\n    repo: org/api\n    ref: dev\n",
            ("org/api", "openapi.json"): SPEC,
        })
        contract = bc.backend_contract("https://github.com/org/web", "dev", github_factory=lambda url: github)
        self.assertEqual(contract["repo"], "org/api")
        self.assertEqual(contract["ref"], "dev")
        self.assertEqual(contract["text"], SPEC)

    def test_without_a_declaration_the_one_api_repo_in_the_project_is_used(self):
        github = FakeGitHub({("org/api", "openapi.json"): SPEC})
        memory = SimpleNamespace(get_program=lambda pid: {"program": {"metadata": {"artifacts": {"project_repositories": [
            {"full_name": "org/web", "role": "web"},
            {"full_name": "org/api", "role": "api", "default_branch": "dev"},
        ]}}}})
        contract = bc.backend_contract("https://github.com/org/web", "dev", program_id="p1", memory=memory,
                                       github_factory=lambda url: github)
        self.assertEqual((contract["repo"], contract["ref"]), ("org/api", "dev"))

    def test_the_spec_is_found_on_the_jobs_base_branch_when_the_api_default_is_main(self):
        github = FakeGitHub({("org/api", "openapi.json"): SPEC}, default_branch="main", only_on={("org/api", "openapi.json"): "dev"})
        memory = SimpleNamespace(get_program=lambda pid: {"program": {"metadata": {"artifacts": {"project_repositories": [
            {"full_name": "org/web", "role": "web"}, {"full_name": "org/api", "role": "api"},
        ]}}}})
        contract = bc.backend_contract("https://github.com/org/web", "dev", program_id="p1", memory=memory,
                                       github_factory=lambda url: github)
        self.assertEqual((contract["repo"], contract["ref"]), ("org/api", "dev"))

    def test_falls_back_to_the_api_default_branch(self):
        github = FakeGitHub({("org/api", "openapi.json"): SPEC}, default_branch="main", only_on={("org/api", "openapi.json"): "main"})
        memory = SimpleNamespace(get_program=lambda pid: {"program": {"metadata": {"artifacts": {"project_repositories": [
            {"full_name": "org/web", "role": "web"}, {"full_name": "org/api", "role": "api"},
        ]}}}})
        contract = bc.backend_contract("https://github.com/org/web", "feature-x", program_id="p1", memory=memory,
                                       github_factory=lambda url: github)
        self.assertEqual(contract["ref"], "main")

    def test_a_declared_ref_is_the_only_one_read(self):
        github = FakeGitHub({
            ("org/web", ".ts-harness.yml"): "contract:\n  openapi:\n    repo: org/api\n    ref: release\n",
            ("org/api", "openapi.json"): SPEC,
        }, only_on={("org/api", "openapi.json"): "dev"})
        self.assertIsNone(bc.backend_contract("https://github.com/org/web", "dev", github_factory=lambda url: github))

    def test_no_backend_or_two_backends_means_no_spec(self):
        self.assertIsNone(bc.project_source([{"full_name": "org/web", "role": "web"}], "org/web"))
        two = [{"full_name": "org/a", "role": "api"}, {"full_name": "org/b", "template": "node-api"}]
        self.assertIsNone(bc.project_source(two, "org/web"))

    def test_a_failure_never_stops_the_job(self):
        def broken(url):
            raise RuntimeError("no app")
        self.assertIsNone(bc.backend_contract("https://github.com/org/web", "dev", github_factory=broken))

    def test_the_job_spec_carries_the_spec(self):
        from ts_harness.workflows import coding_job_stage

        with patch.object(coding_job_stage, "build_coding_request", return_value={"repo_url": "https://github.com/org/web", "base_branch": "dev"}), \
                patch("ts_harness.implementation.backend_contract.backend_contract", return_value={"repo": "org/api", "text": SPEC}) as fetch:
            spec = coding_job_stage.stage_spec({"workflow_id": "wf", "agent_id": "dev-agent"}, {}, program_id="p1")
        self.assertEqual(spec["api_contract"]["repo"], "org/api")
        self.assertEqual(fetch.call_args.kwargs["program_id"], "p1")

    def test_the_worker_writes_it_and_tells_opencode(self):
        backend = OpenCodeBackend.__new__(OpenCodeBackend)
        backend.runner = None
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            test_env = SimpleNamespace(env={}, services_note="", services_running=False, state_dir=state)
            services = SimpleNamespace(env={}, note=lambda: "", services=[], log=[], env_file="")
            with patch("ts_harness.implementation.backends.opencode.prepare_test_environment", return_value=(test_env, "")), \
                    patch("ts_harness.implementation.backends.opencode.install_declared_dependencies", return_value=""), \
                    patch("ts_harness.implementation.backends.opencode.start_test_services", return_value=services), \
                    patch("ts_harness.implementation.backends.opencode.integration_plan", return_value=None):
                env, log = backend._prepare_test_env(Path(tmp), Path(tmp), {"repo": "org/api", "path": "openapi.json", "ref": "dev", "text": SPEC})
            target = state / "contract-openapi.json"
            self.assertEqual(target.read_text(), SPEC)
            self.assertEqual(env.env["OPENAPI_CONTRACT"], str(target))
            self.assertEqual(env.env["OPENAPI_SOURCE"], str(target))
            self.assertIn("You cannot read that repository yourself", env.services_note)
            self.assertIn("supplied by the harness", log)


class BlockedTests(unittest.TestCase):
    def test_the_prompt_says_how_to_report_being_blocked(self):
        prompt = build_implement_prompt("WO-1", "Do X", blocked_report_path="/ws/.ts-harness/blocked-WO-1.json")
        self.assertIn("do not deliver part of it", prompt)
        self.assertIn("/ws/.ts-harness/blocked-WO-1.json", prompt)

    def test_a_report_becomes_a_blocked_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = OpenCodeBackend.__new__(OpenCodeBackend)
            request = SimpleNamespace(workspace_dir=Path(tmp), work_order_id="WO-1", pr_url="")
            path = backend._blocked_path(request)
            self.assertIsNone(backend._blocked_result(request, "b", [], "", Path("/l")))
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"reason": "cannot read org/api", "needs": "the backend spec"}))
            result = backend._blocked_result(request, "b", ["a.ts"], "", Path("/l"))
        self.assertEqual(result.error, "blocked: cannot read org/api; needs: the backend spec")
        self.assertFalse(result.ok)

    def test_plain_text_reports_are_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.json"
            path.write_text("no access to the spec")
            self.assertEqual(read_blocked_report(path), "no access to the spec")


class TestScopeTests(unittest.TestCase):
    def test_coding_and_fix_prompts_ask_for_related_tests_only(self):
        from ts_harness.implementation.verification import VerifyResult, build_verification_fix_prompt

        self.assertIn(TEST_SCOPE_RULE, build_implement_prompt("WO-1", "Do X"))
        fix = build_verification_fix_prompt("WO-1", VerifyResult(ok=False, reasons=["unit_tests_failed"], summary="", log_tail=""))
        self.assertIn("not the whole unit suite", fix)


if __name__ == "__main__":
    unittest.main()


class TicketProjectTests(unittest.TestCase):
    def test_a_program_without_a_project_finds_the_backend_through_the_ticket_key(self):
        github = FakeGitHub({("org/api", "openapi.json"): SPEC})
        memory = SimpleNamespace(
            get_program=lambda pid: {"program": {"metadata": {"artifacts": {}}}},
            list_projects=lambda tenant_id, key: {"projects": [{"id": "proj-1", "bdd_mode": "auto"}]} if key == "PROJ" else {"projects": []},
            list_project_repositories=lambda pid: {"repositories": [
                {"full_name": "org/web", "role": "web"}, {"full_name": "org/api", "role": "api", "default_branch": "dev"}]},
        )
        contract = bc.backend_contract("https://github.com/org/web", "dev", program_id="p1", work_order_id="PROJ-32",
                                       memory=memory, github_factory=lambda url: github)
        self.assertEqual(contract["repo"], "org/api")


class SubagentLimitTests(unittest.TestCase):
    def test_the_prompt_caps_parallel_subagents_at_four_by_default(self):
        with patch.dict("os.environ", {}, clear=False):
            import os as _os

            _os.environ.pop("CODING_MAX_SUBAGENTS", None)
            self.assertIn("run at most 4 at the same time", build_implement_prompt("WO-1", "Do X"))
        with patch.dict("os.environ", {"CODING_MAX_SUBAGENTS": "2"}):
            self.assertIn("run at most 2 at the same time", build_implement_prompt("WO-1", "Do X"))
