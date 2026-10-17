"""merge_readiness.policy: required check categories, grace for missing checks, enforce mode."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ts_harness.integrations.github_client import summarize_checks
from ts_harness.workflows import merge_policy, merge_readiness

PR = "https://github.com/o/r/pull/7"


def defaults(policy=None, legacy=None):
    readiness: dict = {}
    if legacy is not None:
        readiness["required_checks"] = legacy
    if policy is not None:
        readiness["policy"] = policy
    return {"merge_readiness": readiness}


FULL_POLICY = {
    "mode": "warn",
    "categories": {
        "O/R": {"build": ["ci / build"], "lint": ["ci / lint"], "test": ["ci / test"], "security": ["codeql"]}
    },
    "missing_check_grace_seconds": 60,
}


class ResolveTests(unittest.TestCase):
    def test_legacy_only_is_unconfigured(self):
        policy = merge_policy.resolve("o/r", defaults(legacy={"o/r": ["ci"]}))
        self.assertFalse(policy.configured)
        self.assertEqual(policy.required_names, ["ci"])
        self.assertEqual(policy.pinned(), {})

    def test_categories_merge_with_legacy_and_lowercase_repo(self):
        policy = merge_policy.resolve("O/R", defaults(FULL_POLICY, legacy={"o/r": ["ci / test", "extra"]}))
        self.assertTrue(policy.configured)
        self.assertEqual(policy.required_names, ["ci / test", "extra", "ci / build", "ci / lint", "codeql"])
        self.assertEqual(policy.uncovered, [])
        self.assertEqual(policy.grace_seconds, 60)

    def test_repo_without_entry_is_fully_uncovered(self):
        policy = merge_policy.resolve("other/repo", defaults(FULL_POLICY))
        self.assertEqual(policy.uncovered, list(merge_policy.DEFAULT_CATEGORIES))
        self.assertEqual(policy.required_names, [])

    def test_custom_required_categories(self):
        policy = merge_policy.resolve(
            "o/r", defaults({"required_categories": ["test", "perf"], "categories": FULL_POLICY["categories"]})
        )
        self.assertEqual(policy.uncovered, ["perf"])

    def test_enforce_needs_policy(self):
        self.assertTrue(merge_policy.resolve("o/r", defaults({"mode": "enforce"})).enforce)
        self.assertFalse(merge_policy.resolve("o/r", defaults({"mode": "bogus"})).enforce)

    def test_category_results_take_worst_state(self):
        results = merge_policy.category_results(
            {"test": ["a", "b"], "lint": ["c"], "security": ["d"], "build": ["e"]},
            {"a": "success", "b": "failure", "c": "pending", "e": "success"},
        )
        self.assertEqual(results, {"test": "failure", "lint": "pending", "security": "missing", "build": "success"})


class SummarizeMissingTests(unittest.TestCase):
    def test_missing_names_reported(self):
        summary = summarize_checks({}, [{"name": "a", "status": "completed", "conclusion": "success"}], required=["a", "b"])
        self.assertEqual((summary["state"], summary["missing"]), ("pending", ["b"]))


def github_mock(summary):
    github = MagicMock()
    github.get_pull.return_value = {"head": {"sha": "sha7"}, "base": {"ref": "main", "repo": {"full_name": "o/r"}}}
    github.check_summary.return_value = summary
    github.required_status_contexts.return_value = ["ci / build", "ci / lint", "ci / test", "codeql"]
    return github


def memory_mock():
    memory = MagicMock()
    deliver = {"id": "d1", "step_name": "deliver_work_order", "status": "succeeded", "output": {"pr_url": PR}}
    memory.get_workflow.return_value = {"workflow": {"id": "wf", "program_id": "p1"}, "tasks": [deliver]}
    memory.find_external_refs.return_value = {"refs": []}
    return memory


def prepare(program_defaults, summary=None, github=None):
    github = github or github_mock(summary or {"state": "pending", "checks": {}, "failed": [], "pending": [], "missing": []})
    return merge_readiness.prepare_checks_wait(
        {"id": "t1", "workflow_id": "wf", "input": {}},
        {"pr_checks": {"pr_url": "$prior.deliver_work_order.output.pr_url"}},
        memory=memory_mock(),
        github=github,
        program_defaults=program_defaults,
    )


class PrepareWaitPolicyTests(unittest.TestCase):
    def test_empty_policy_requires_branch_protection_checks(self):
        github = github_mock({"state": "pending", "checks": {}, "failed": [], "pending": [], "missing": []})
        github.required_status_contexts.return_value = [
            "lint",
            "test",
            "dependency-audit",
            "harness/code-review",
            "harness/security-scan",
        ]
        prepared = prepare({}, github=github)
        self.assertEqual(
            prepared["options"]["heads"][0]["required_checks"],
            ["lint", "test", "dependency-audit"],
        )

    def test_no_policy_pins_no_policy_snapshot(self):
        prepared = prepare(defaults(legacy={"o/r": ["ci"]}))
        head = prepared["options"]["heads"][0]
        self.assertNotIn("policy", head)
        self.assertNotIn("pinned_at", head)

    def test_warn_mode_records_uncovered_and_keeps_waiting(self):
        prepared = prepare(defaults({"mode": "warn", "categories": {"o/r": {"test": ["ci / test"]}}}))
        head = prepared["options"]["heads"][0]
        self.assertEqual(head["required_checks"], ["ci / test"])
        self.assertEqual(head["policy"]["uncovered"], ["build", "lint", "security"])
        self.assertIn("policy_incomplete:o/r: build, lint, security", prepared["options"]["warnings"])
        self.assertNotIn("ready", prepared)

    def test_enforce_blocks_uncovered_categories(self):
        with self.assertRaises(merge_readiness.MergePolicyBlocked) as ctx:
            prepare(defaults({"mode": "enforce", "categories": {"o/r": {"test": ["ci / test"]}}}))
        self.assertIn("policy_incomplete", str(ctx.exception))

    def test_enforce_blocks_unprotected_branch_only_when_required(self):
        policy = {**FULL_POLICY, "mode": "enforce"}
        github = github_mock({"state": "pending", "checks": {}, "failed": [], "pending": [], "missing": []})
        github.required_status_contexts.return_value = ["ci / test"]
        prepared = prepare(defaults(policy), github=github)
        self.assertTrue(any("branch protection does not require" in w for w in prepared["options"]["warnings"]))
        with self.assertRaises(merge_readiness.MergePolicyBlocked):
            prepare(defaults({**policy, "require_branch_protection": True}), github=github)

    def test_enforce_requires_code_review_and_security_scan_on_the_protected_branch(self):
        categories = {
            "O/R": {
                "build": ["build"],
                "lint": ["lint"],
                "test": ["test"],
                "security": ["security"],
            }
        }
        policy = {**FULL_POLICY, "mode": "enforce", "require_branch_protection": True, "categories": categories}
        summary = {"state": "pending", "checks": {}, "failed": [], "pending": [], "missing": []}
        github = github_mock(summary)
        github.required_status_contexts.return_value = ["build", "lint", "test", "security"]
        with self.assertRaises(merge_readiness.MergePolicyBlocked) as ctx:
            prepare(defaults(policy), github=github)
        message = str(ctx.exception)
        self.assertIn("harness/code-review", message)
        self.assertIn("harness/security-scan", message)
        github.required_status_contexts.return_value = [
            "build",
            "lint",
            "test",
            "security",
            "harness/code-review",
            "harness/security-scan",
        ]
        prepared = prepare(defaults(policy), github=github)
        self.assertFalse(any("harness/code-review" in warning for warning in prepared["options"]["warnings"]))
        self.assertFalse(any("harness/security-scan" in warning for warning in prepared["options"]["warnings"]))

    def test_ready_payload_carries_policy_evidence(self):
        checks = {"ci / build": "success", "ci / lint": "success", "ci / test": "success", "codeql": "success"}
        prepared = prepare(
            defaults(FULL_POLICY),
            summary={"state": "success", "checks": checks, "failed": [], "pending": [], "missing": []},
        )
        pr = prepared["ready"]["prs"][0]
        self.assertEqual(pr["category_results"], {"build": "success", "lint": "success", "test": "success", "security": "success"})
        self.assertEqual(pr["policy"]["mode"], "warn")


SECURITY_CHECKS_CONFIG = {
    "pr_checks": {
        "pr_url": "$prior.deliver_work_order.output.pr_url",
        "security": "$prior.deliver_work_order.output.security",
    }
}


class SecurityStatusTests(unittest.TestCase):

    def _prepare(self, security, github=None):
        github = github or github_mock({"state": "pending", "checks": {}, "failed": [], "pending": [], "missing": []})
        memory = memory_mock()
        deliver = memory.get_workflow.return_value["tasks"][0]
        if security is not None:
            deliver["output"]["security"] = security
        prepared = merge_readiness.prepare_checks_wait(
            {"id": "t1", "workflow_id": "wf", "input": {}}, SECURITY_CHECKS_CONFIG, memory=memory, github=github, program_defaults={}
        )
        return prepared, github

    def _security_calls(self, github):
        return [
            call for call in github.create_status.call_args_list if call.kwargs.get("context") == "harness/security-scan"
        ]

    def test_clean_scan_publishes_success_on_pinned_head(self):
        _prepared, github = self._prepare({"secret_scan": {"tool": "gitleaks", "findings": []}})
        calls = self._security_calls(github)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args, ("o", "r", "sha7"))
        self.assertEqual(calls[0].kwargs["state"], "success")

    def test_findings_publish_failure_with_counts(self):
        _prepared, github = self._prepare(
            {
                "secret_scan": {"findings": [{"file": "a.py", "line": 1, "rule": "x"}]},
                "dependency_audit": {"findings": [{"package": "p"}, {"package": "q"}]},
            }
        )
        call = self._security_calls(github)[0]
        self.assertEqual(call.kwargs["state"], "failure")
        self.assertIn("1 secret finding(s), 2 vulnerable dependency finding(s)", call.kwargs["description"])

    def test_unavailable_audit_publishes_failure(self):
        _prepared, github = self._prepare(
            {
                "secret_scan": {"tool": "gitleaks", "findings": []},
                "dependency_audit": {
                    "tool": "osv-scanner",
                    "findings": [],
                    "skipped_reason": "dependency_audit_unavailable",
                },
            }
        )
        call = self._security_calls(github)[0]
        self.assertEqual(call.kwargs["state"], "failure")
        self.assertIn("dependency audit unavailable", call.kwargs["description"])

    def test_no_manifest_changes_stays_success(self):
        _prepared, github = self._prepare(
            {
                "secret_scan": {"findings": []},
                "dependency_audit": {"tool": "", "findings": [], "skipped_reason": "no_manifest_changes"},
            }
        )
        call = self._security_calls(github)[0]
        self.assertEqual(call.kwargs["state"], "success")
        self.assertNotIn("unavailable", call.kwargs["description"])

    def test_scanned_sha_mismatch_publishes_failure(self):
        _prepared, github = self._prepare(
            {
                "secret_scan": {"findings": []},
                "dependency_audit": {"findings": [], "skipped_reason": ""},
                "scanned_sha": "earlier",
            }
        )
        call = self._security_calls(github)[0]
        self.assertEqual(call.kwargs["state"], "failure")
        self.assertEqual(call.kwargs["description"], merge_readiness.SCAN_MISMATCH_DESCRIPTION)

    def test_matching_scanned_sha_stays_success(self):
        _prepared, github = self._prepare(
            {"secret_scan": {"findings": []}, "scanned_sha": "sha7"}
        )
        call = self._security_calls(github)[0]
        self.assertEqual(call.kwargs["state"], "success")

    def test_no_security_block_publishes_nothing(self):
        _prepared, github = self._prepare(None)
        self.assertEqual(self._security_calls(github), [])

    def test_publish_error_is_a_warning(self):
        github = github_mock({"state": "pending", "checks": {}, "failed": [], "pending": [], "missing": []})
        github.create_status.side_effect = RuntimeError("403")
        prepared, _github = self._prepare({"secret_scan": {"findings": []}}, github=github)
        self.assertTrue(any("harness/security-scan" in w for w in prepared["options"]["warnings"]))


class MissingGraceTests(unittest.TestCase):
    def _head(self, pinned_at=1000):
        return {
            "pr_url": PR,
            "head_sha": "sha7",
            "repo": "o/r",
            "required_checks": ["a", "b"],
            "policy": {"mode": "warn", "categories": {"test": ["a", "b"]}, "uncovered": [], "grace_seconds": 60},
            "pinned_at": pinned_at,
        }

    def _github(self, summary):
        github = MagicMock()
        github.check_summary.return_value = summary
        return github

    def test_missing_within_grace_stays_pending(self):
        summary = {"state": "pending", "checks": {"a": "success", "b": "missing"}, "failed": [], "pending": ["b"], "missing": ["b"]}
        aggregate = merge_readiness.aggregate_checks(self._github(summary), [self._head()], now=1030)
        self.assertEqual(aggregate["state"], "pending")

    def test_missing_after_grace_fails(self):
        summary = {"state": "pending", "checks": {"a": "success", "b": "missing"}, "failed": [], "pending": ["b"], "missing": ["b"]}
        aggregate = merge_readiness.aggregate_checks(self._github(summary), [self._head()], now=1100)
        self.assertEqual(aggregate["state"], "failure")
        payload = merge_readiness.checks_payload(aggregate)
        self.assertEqual(payload["failed_checks"], [f"{PR}:required_check_missing:b"])
        self.assertEqual(payload["prs"][0]["category_results"], {"test": "missing"})

    def test_missing_while_others_running_stays_pending(self):
        summary = {
            "state": "pending",
            "checks": {"a": "in_progress", "b": "missing"},
            "failed": [],
            "pending": ["a", "b"],
            "missing": ["b"],
        }
        aggregate = merge_readiness.aggregate_checks(self._github(summary), [self._head()], now=5000)
        self.assertEqual(aggregate["state"], "pending")

    def test_failure_waits_until_every_required_check_reports(self):
        summary = {
            "state": "failure",
            "checks": {"lint": "failure", "dependency-audit": "pending"},
            "failed": ["lint"],
            "pending": ["dependency-audit"],
            "missing": [],
        }
        aggregate = merge_readiness.aggregate_checks(self._github(summary), [self._head()], now=5000)
        self.assertEqual(aggregate["state"], "pending")
        self.assertEqual(aggregate["prs"][0]["failed"], ["lint"])

    def test_failure_is_terminal_once_required_checks_have_reported(self):
        summary = {
            "state": "failure",
            "checks": {"lint": "failure", "dependency-audit": "failure"},
            "failed": ["lint", "dependency-audit"],
            "pending": [],
            "missing": [],
        }
        aggregate = merge_readiness.aggregate_checks(self._github(summary), [self._head()], now=5000)
        self.assertEqual(aggregate["state"], "failure")
        self.assertEqual(aggregate["prs"][0]["failed"], ["lint", "dependency-audit"])

    def test_no_policy_never_fails_missing(self):
        head = {k: v for k, v in self._head().items() if k not in {"policy", "pinned_at"}}
        summary = {"state": "pending", "checks": {"b": "missing"}, "failed": [], "pending": ["b"], "missing": ["b"]}
        aggregate = merge_readiness.aggregate_checks(self._github(summary), [head], now=10**9)
        self.assertEqual(aggregate["state"], "pending")

    def test_poller_resumes_with_missing_check_failure(self):
        from ts_harness.gateway import system_jobs

        head = self._head(pinned_at=0)
        memory = MagicMock()
        memory.find_external_waits.return_value = {
            "waits": [{"workflow_id": "wf", "options": {"heads": [head], "warnings": ["w1"]}}]
        }
        engine = MagicMock()
        summary = {"state": "pending", "checks": {"a": "success", "b": "missing"}, "failed": [], "pending": ["b"], "missing": ["b"]}
        github = self._github(summary)
        with (
            patch.object(system_jobs, "get_memory_client", return_value=memory),
            patch.object(system_jobs, "get_workflow_engine", return_value=engine),
            patch.object(system_jobs, "GitHubClient", return_value=github),
        ):
            result = system_jobs.poll_github_checks(tenant_id="acme", token="t")
        self.assertEqual(result["resumed"], 1)
        payload = engine.resume_external.call_args.kwargs["payload"]
        self.assertEqual(payload["checks_state"], "failure")
        self.assertEqual(payload["policy_warnings"], ["w1"])


class PolicySettingsTests(unittest.TestCase):
    def _validate(self, policy):
        from ts_harness.config.runtime_settings import validate_runtime_settings_payload

        return validate_runtime_settings_payload(
            {
                "definition_type": "runtime_settings",
                "definition_version": "1.0.0",
                "program_defaults": {"merge_readiness": {"policy": policy}},
            }
        )

    def test_valid_policy_canonicalizes(self):
        canonical, issues = self._validate(
            {
                "mode": "Enforce",
                "required_categories": ["test", "test", "security"],
                "categories": {"Org/App": {"test": ["ci", "ci"], "security": ["codeql"]}},
                "missing_check_grace_seconds": 300,
                "require_branch_protection": True,
                "diff_limits": {"max_files": 30},
            }
        )
        self.assertEqual(issues, [])
        policy = canonical["program_defaults"]["merge_readiness"]["policy"]
        self.assertEqual(policy["mode"], "enforce")
        self.assertEqual(policy["required_categories"], ["test", "security"])
        self.assertEqual(policy["categories"], {"org/app": {"test": ["ci"], "security": ["codeql"]}})

    def test_invalid_policy_values_report_issues(self):
        _, issues = self._validate(
            {
                "mode": "strict",
                "required_categories": "test",
                "categories": {"not a repo": {}, "o/r": {"test": "ci"}},
                "missing_check_grace_seconds": -1,
                "require_branch_protection": "yes",
                "unknown": 1,
            }
        )
        codes = {issue["code"] for issue in issues}
        self.assertTrue(
            {
                "invalid_policy_mode",
                "invalid_required_categories",
                "invalid_repo_slug",
                "invalid_names",
                "invalid_grace_seconds",
                "invalid_branch_protection",
                "unknown_field",
            }
            <= codes
        )

    def test_diff_limits_validation(self):
        canonical, issues = self._validate(
            {"diff_limits": {"max_files": 30, "max_changed_lines": 800, "exclude": ["**/*.lock", "**/*.lock"]}}
        )
        self.assertEqual(issues, [])
        limits = canonical["program_defaults"]["merge_readiness"]["policy"]["diff_limits"]
        self.assertEqual(limits, {"max_files": 30, "max_changed_lines": 800, "exclude": ["**/*.lock"]})
        _, issues = self._validate({"diff_limits": {"max_files": 0, "max_changed_lines": True, "exclude": "x", "other": 1}})
        codes = [issue["code"] for issue in issues]
        self.assertEqual(codes.count("invalid_diff_limit"), 3)
        self.assertIn("unknown_field", codes)

    def test_host_security_validation(self):
        canonical, issues = self._validate(
            {
                "host_security": {
                    "secret_scan": True,
                    "dependency_audit": False,
                    "audit_severity": "CRITICAL",
                    "static_analysis": True,
                }
            }
        )
        self.assertEqual(issues, [])
        self.assertEqual(
            canonical["program_defaults"]["merge_readiness"]["policy"]["host_security"],
            {
                "secret_scan": True,
                "dependency_audit": False,
                "audit_severity": "critical",
                "static_analysis": True,
            },
        )
        _, issues = self._validate({"host_security": {"secret_scan": "yes", "audit_severity": "severe", "x": 1}})
        codes = [issue["code"] for issue in issues]
        self.assertEqual(codes.count("invalid_host_security"), 2)
        self.assertIn("unknown_field", codes)

    def test_secret_scan_cannot_be_disabled_in_enforce_mode(self):
        _, issues = self._validate({"mode": "warn", "host_security": {"secret_scan": False}})
        self.assertEqual(issues, [])
        _, issues = self._validate({"mode": "enforce", "host_security": {"secret_scan": False}})
        self.assertEqual([issue["code"] for issue in issues], ["secret_scan_required"])


if __name__ == "__main__":
    unittest.main()
