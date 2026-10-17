"""review_precheck and finalize_code_review system steps."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from ts_harness.integrations.github_client import GitHubClient, GitHubError
from ts_harness.workflows import code_review
from ts_harness.workflows.system_steps import SYSTEM_STEPS, StepBlocked

PR = "https://github.com/acme/app/pull/4"
TASK = {"id": "t1", "workflow_id": "wf-1"}
PRECHECK_CONFIG = {
    "hook": "review_precheck",
    "pr_url": "$prior.deliver_work_order.output.pr_url",
    "work_order_body": "$workflow.work_order_body",
    "refresh": "$prior.deliver_work_order.output.refresh_method",
}
FINALIZE_CONFIG = {
    "hook": "finalize_code_review",
    "review": "$prior.code_review.output",
    "precheck": "$prior.review_precheck.output",
}


def _memory(body="# WO-1", tasks=None):
    memory = MagicMock()
    memory.get_workflow.return_value = {
        "workflow": {"id": "wf-1"},
        "tasks": tasks
        or [
            {
                "step_name": "deliver_work_order",
                "status": "succeeded",
                "input": {"work_order_body": body},
                "output": {"pr_url": PR},
                "created_at": "1",
            }
        ],
    }
    return memory


def _file(name, additions=10, deletions=0, patch="@@ +1 @@"):
    return {"filename": name, "status": "modified", "additions": additions, "deletions": deletions, "patch": patch}


def _github(files):
    github = MagicMock()
    github.get_pull.return_value = {"head": {"sha": "h1"}, "base": {"ref": "main"}}
    github.list_pull_files.return_value = files
    return github


def _limits(**limits):
    return {"merge_readiness": {"policy": {"diff_limits": limits}}}


class PrecheckTests(unittest.TestCase):
    def test_collects_files_and_stats(self):
        output = code_review.review_precheck(
            TASK, PRECHECK_CONFIG, memory=_memory(), github=_github([_file("src/a.py", 5, 2)]), program_defaults={}
        )
        self.assertEqual(output["prs"], [{"pr_url": PR, "head_sha": "h1"}])
        self.assertEqual(output["diff_stats"]["additions"], 5)
        self.assertEqual(output["files"][0]["pr_url"], PR)
        self.assertEqual(output["scope_findings"], [])
        self.assertFalse(output["refresh"])

    def test_excludes_do_not_count_toward_limits(self):
        files = [_file("src/a.py", 10), _file("package-lock.json", 5000), _file("web/yarn.lock", 900), _file("x/generated/y.ts", 900)]
        output = code_review.review_precheck(
            TASK,
            PRECHECK_CONFIG,
            memory=_memory(),
            github=_github(files),
            program_defaults=_limits(max_files=1, max_changed_lines=50, exclude=["**/*.lock", "package-lock.json", "**/generated/**"]),
        )
        self.assertEqual([item["filename"] for item in output["files"]], ["src/a.py"])
        self.assertEqual(output["diff_stats"]["excluded_files"], 3)

    def test_oversized_diff_blocks(self):
        with self.assertRaises(StepBlocked) as ctx:
            code_review.review_precheck(
                TASK,
                PRECHECK_CONFIG,
                memory=_memory(),
                github=_github([_file("a.py", 400), _file("b.py", 401)]),
                program_defaults=_limits(max_changed_lines=800),
            )
        self.assertIn("diff_limit_exceeded:", str(ctx.exception))
        self.assertIn("lines=801", str(ctx.exception))

    def test_no_limits_configured_never_blocks(self):
        files = [_file(f"f{i}.py", 100) for i in range(50)]
        output = code_review.review_precheck(TASK, PRECHECK_CONFIG, memory=_memory(), github=_github(files), program_defaults={})
        self.assertEqual(output["diff_stats"]["files"], 50)

    def test_strict_greater_than_a_named_boundary_blocks(self):
        body = (
            "# WO-203\n\n"
            "10% when the total quantity is 100 or more.\n"
            "Given an order with exactly 50 units, Then a 5% discount applies.\n\n"
            "## Files\n- app/pricing.py\n"
        )
        patch = "@@ -1 +1 @@\n+def bulk_discount_rate(total_quantity):\n+    if total_quantity > 100:\n+        return 1\n+    if total_quantity > 50:\n+        return 1\n+    if total_quantity >= 100:\n+        return 1\n+    if total_quantity > 99:\n+        return 1\n"
        output = code_review.review_precheck(
            TASK,
            PRECHECK_CONFIG,
            memory=_memory(body),
            github=_github([_file("app/pricing.py", patch=patch)]),
            program_defaults={},
        )
        flagged = {
            (finding["file"], finding["message"])
            for finding in output["scope_findings"]
            if finding["category"] == "correctness"
        }
        self.assertIn(("app/pricing.py", "strict comparison `> 100` excludes 100; the work order includes that boundary"), flagged)
        self.assertIn(("app/pricing.py", "strict comparison `> 50` excludes 50; the work order includes that boundary"), flagged)
        self.assertEqual(len(flagged), 2)
        inclusive = code_review.boundary_findings(
            PR,
            [_file("app/pricing.py", patch="+    if total_quantity == 100 or total_quantity > 100:\n")],
            body,
        )
        self.assertEqual(inclusive, [])
        self.assertTrue(all(finding["severity"] == "major" for finding in output["scope_findings"] if finding["category"] == "correctness"))

    def test_undeclared_paths_drop_files_the_work_order_did_not_name(self):
        body = "# WO-1\n\n## Files\n- app/pricing.py\n"
        extra = code_review.undeclared_paths(
            ["app/pricing.py", "requirements-dev.txt", ".gitignore", "docs/planner/work-orders.md"],
            body,
        )
        self.assertEqual(extra, ["requirements-dev.txt"])
        self.assertEqual(code_review.undeclared_paths(["requirements-dev.txt"], "# WO-1\n\n## In Scope\n- api"), [])

    def test_scope_findings_only_with_declared_files(self):
        body = "# WO-1\n\n## Files\n- `src/api/**`\n- docs/readme.md\n\n## Out of Scope\n- billing\n"
        files = [_file("src/api/x.py"), _file("docs/readme.md"), _file("src/billing/y.py")]
        output = code_review.review_precheck(
            TASK, PRECHECK_CONFIG, memory=_memory(body), github=_github(files), program_defaults={}
        )
        self.assertEqual([finding["file"] for finding in output["scope_findings"]], ["src/billing/y.py"])
        self.assertEqual(output["scope_findings"][0]["severity"], "major")
        output = code_review.review_precheck(TASK, PRECHECK_CONFIG, memory=_memory("# WO-1\n\n## In Scope\n- api"), github=_github(files), program_defaults={})
        self.assertEqual(output["scope_findings"], [])

    def test_pipeline_written_files_are_neither_scope_findings_nor_counted(self):
        body = "# WO-1\n\n## Files\n- app/pricing.py\n"
        files = [
            _file("app/pricing.py"),
            _file(".opencode/opencode.json"),
            _file(".opencode/skills/implement-work-order/SKILL.md", 400),
            _file(".gitignore"),
            _file("docs/planner/work-orders.md", 900),
        ]
        output = code_review.review_precheck(
            TASK,
            PRECHECK_CONFIG,
            memory=_memory(body),
            github=_github(files),
            program_defaults=_limits(max_files=2, max_changed_lines=100),
        )
        self.assertEqual(output["scope_findings"], [])
        self.assertEqual([item["filename"] for item in output["files"]], ["app/pricing.py"])
        self.assertEqual(output["diff_stats"]["pipeline_files"], 4)

    def test_bdd_files_are_in_scope_when_acceptance_tests_require_them(self):
        body = (
            "# WO-1\n\n## E2E Acceptance Tests\n- Given a cart, When totalled, Then it sums.\n\n"
            "## Files\n- app/pricing.py\n"
        )
        files = [_file("app/pricing.py"), _file("features/wo-1-pricing.feature"), _file("features/steps/pricing_steps.py")]
        self.assertEqual(code_review.scope_findings(PR, files, body), [])
        no_gwt = "# WO-1\n\n## Files\n- app/pricing.py\n"
        flagged = [item["file"] for item in code_review.scope_findings(PR, files, no_gwt)]
        self.assertEqual(flagged, ["features/wo-1-pricing.feature", "features/steps/pricing_steps.py"])

    def test_refresh_flag_follows_implement_output(self):
        tasks = [{"step_name": "deliver_work_order", "status": "succeeded", "output": {"pr_url": PR, "refresh_method": "server_merge"}, "created_at": "1"}]
        output = code_review.review_precheck(TASK, PRECHECK_CONFIG, memory=_memory(tasks=tasks), github=_github([]), program_defaults={})
        self.assertTrue(output["refresh"])

    def test_steps_are_registered(self):
        self.assertIn("review_precheck", SYSTEM_STEPS)
        self.assertIn("finalize_code_review", SYSTEM_STEPS)


def _finalize_memory(findings, verdict="pass", scope=None):
    tasks = [
        {
            "step_name": "review_precheck",
            "status": "succeeded",
            "output": {"prs": [{"pr_url": PR, "head_sha": "h1"}], "scope_findings": scope or []},
            "created_at": "1",
        },
        {
            "step_name": "code_review",
            "status": "succeeded",
            "output": {"result": {"verdict": verdict, "summary": "Looks careful.", "prs": [{"pr_url": PR}], "findings": findings}},
            "created_at": "2",
        },
    ]
    return _memory(tasks=tasks)


def _finding(severity, category, **extra):
    return {"severity": severity, "category": category, "pr_url": PR, "message": f"{severity} {category}", **extra}


class VerdictTests(unittest.TestCase):
    def test_verdict_matrix(self):
        cases = [
            ([], "pass"),
            ([_finding("blocker", "maintainability")], "block"),
            ([_finding("major", "correctness")], "block"),
            ([_finding("major", "security")], "block"),
            ([_finding("major", "scope")], "block"),
            ([_finding("major", "tests")], "pass"),
            ([_finding("major", "maintainability")], "pass"),
            ([_finding("minor", "security"), _finding("nit", "correctness")], "pass"),
        ]
        for findings, expected in cases:
            with self.subTest(findings=findings):
                self.assertEqual(code_review.compute_verdict(findings), expected)


class FinalizeTests(unittest.TestCase):
    def test_pass_posts_comment_review_and_success_status(self):
        github = MagicMock()
        findings = [_finding("minor", "tests", file="src/a.py", line=3), _finding("nit", "maintainability")]
        output = code_review.finalize_code_review(TASK, FINALIZE_CONFIG, memory=_finalize_memory(findings), github=github)
        self.assertEqual(output["verdict"], "pass")
        self.assertEqual(output["warnings"], [])
        kwargs = github.create_review.call_args.kwargs
        self.assertEqual(kwargs["commit_id"], "h1")
        self.assertEqual(kwargs["comments"], [{"path": "src/a.py", "line": 3, "side": "RIGHT", "body": "[minor/tests] src/a.py:3 minor tests"}])
        self.assertIn("nit maintainability", kwargs["body"])
        status = github.create_status.call_args
        self.assertEqual(status.args[:3], ("acme", "app", "h1"))
        self.assertEqual((status.kwargs["state"], status.kwargs["context"]), ("success", "harness/code-review"))

    def test_review_event_is_always_comment(self):
        transport = MagicMock(return_value=(200, b"{}"))
        GitHubClient(token="t", transport=transport).create_review("o", "r", 1, commit_id="h", body="b", comments=[])
        body = transport.call_args.args[3]
        self.assertIn(b'"event": "COMMENT"', body)

    def test_block_merges_scope_findings_and_carries_feedback(self):
        github = MagicMock()
        scope = [_finding("major", "scope", file="src/billing/y.py", message="outside declared files")]
        with self.assertRaises(StepBlocked) as ctx:
            code_review.finalize_code_review(
                TASK, FINALIZE_CONFIG, memory=_finalize_memory([_finding("minor", "tests")], verdict="pass", scope=scope), github=github
            )
        self.assertIn("code_review_blocked: 1 blocking findings", str(ctx.exception))
        output = ctx.exception.output
        self.assertEqual(output["verdict"], "block")
        self.assertEqual(len(output["findings"]), 2)
        self.assertIn("verdict_mismatch: model=pass computed=block", output["warnings"])
        self.assertIn("outside declared files", output["review_feedback"])
        self.assertIn("Blocking findings", output["review_feedback"])
        self.assertEqual(github.create_status.call_args.kwargs["state"], "failure")

    def test_duplicate_findings_are_merged(self):
        dup = _finding("major", "scope", file="a.py", message="same")
        output = code_review.merge_findings([dup], [dict(dup)])
        self.assertEqual(len(output), 1)

    def test_agent_repeats_of_scope_findings_are_dropped(self):
        agent = _finding("major", "scope", file="a.py", line=1, message="a.py is outside the declared files.")
        deterministic = _finding("major", "scope", file="a.py", message="a.py is outside the work order's declared files")
        output = code_review.merge_findings([agent, _finding("blocker", "security", file="a.py", line=1)], [deterministic])
        self.assertEqual([(f["category"], f["message"]) for f in output], [("security", "blocker security"), ("scope", deterministic["message"])])

    def test_posting_failure_becomes_warning(self):
        github = MagicMock()
        github.create_review.side_effect = GitHubError(422, "line not in diff")
        github.create_status.side_effect = GitHubError(500, "boom")
        output = code_review.finalize_code_review(TASK, FINALIZE_CONFIG, memory=_finalize_memory([]), github=github)
        self.assertEqual(output["verdict"], "pass")
        self.assertEqual(len(output["warnings"]), 2)
        self.assertTrue(output["warnings"][0].startswith(f"review_post_failed:{PR}"))


if __name__ == "__main__":
    unittest.main()
