"""Pre-push review: the stronger model reviews the verified change and its blocking findings are fixed locally."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ts_harness.implementation import pre_push_review as ppr
from ts_harness.implementation.backends import opencode as oc
from ts_harness.implementation.backends.base import CodingRequest

BLOCKING = {"severity": "major", "category": "correctness", "file": "src/a.ts", "line": 4, "message": "Off by one; use <=."}
MINOR = {"severity": "minor", "category": "maintainability", "file": "src/a.ts", "line": 9, "message": "Rename x."}


def _done(rc: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, "", "")


class Harness:
    """Drives _pre_push_review with scripted reviews and fix/verify outcomes."""

    def __init__(self, reviews: list[dict | None], *, verify_ok: list[bool] | None = None, fix_rc: int = 0):
        self.reviews = list(reviews)
        self.verify_ok = list(verify_ok or [])
        self.fix_rc = fix_rc
        self.git: list[list[str]] = []
        self.models: list[str] = []
        self.commits = 0
        self.verify_calls: list[dict] = []

    def run_opencode(self, cmd, *, cwd, env, log_path, work_order_id):
        prompt, model = cmd[1], cmd[2]
        self.models.append(model)
        if prompt.startswith("You are the code reviewer"):
            out = Path(prompt.split("Write your review as JSON to ", 1)[1].split(" (", 1)[0])
            review = self.reviews.pop(0)
            if review is not None:
                out.write_text(json.dumps(review), encoding="utf-8")
            return _done()
        return _done(self.fix_rc)

    def runner(self, cmd, **kwargs):
        self.git.append(cmd)
        return _done()

    def verify(self, *args, **kwargs):
        self.verify_calls.append(kwargs)
        ok = self.verify_ok.pop(0) if self.verify_ok else True
        failure = None if ok else object()
        return failure, "log", 0, ["src/a.ts"], {"scan": "new"}, {"checks": "new"}


def _request(workspace: Path, *, rounds: int = 2, model: str | None = "claude-opus-5-5") -> CodingRequest:
    return CodingRequest(
        repo_url="https://github.com/example/app", base_branch="dev", work_order_id="WO-1",
        work_order_body="Count items up to and including 10.", workspace_dir=workspace, provider="anthropic",
        model="claude-sonnet-5-5", review_model=model, review_rounds=rounds,
    )


def _review(harness: Harness, request: CodingRequest):
    backend = oc.OpenCodeBackend(runner=harness.runner)
    with patch.object(backend, "_run_opencode", side_effect=harness.run_opencode), \
            patch.object(backend, "_verify_with_fixes", side_effect=harness.verify), \
            patch.object(backend, "_head_sha", return_value="good-sha"), \
            patch.object(oc, "build_opencode_command", side_effect=lambda **kw: ["opencode", kw["prompt"], kw["model"]]), \
            patch.object(oc, "commit_all_changes", side_effect=lambda *a, **k: setattr(harness, "commits", harness.commits + 1) or True), \
            patch.object(oc, "list_changed_files", return_value=["src/a.ts"]):
        return backend._pre_push_review(
            Path("/repo"), request, binary="opencode", provider="anthropic", env={}, review_env={}, branch="wo/WO-1",
            log="log", log_path=Path("/tmp/log"), skills_materialized=False, test_env=None,
            changed=["src/a.ts"], security={"scan": "old"}, verification={"checks": "old"},
        )


class PrePushReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_clean_review_pushes_as_is(self):
        harness = Harness([{"summary": "fine", "findings": [MINOR]}])
        rounds, _log, _changed, security, _verification = _review(harness, _request(self.workspace))
        self.assertEqual([r["status"] for r in rounds], ["pass"])
        self.assertEqual(harness.models, ["claude-opus-5-5"])
        self.assertEqual(security, {"scan": "old"})
        self.assertIn(["git", "reset", "--hard", "HEAD"], harness.git)
        record = ppr.summary_record(rounds, model="claude-opus-5-5")
        self.assertEqual((record["reviews"], record["fixed"], record["open_blocking"]), (1, 0, []))

    def test_blocking_findings_are_fixed_on_the_run_model_and_reverified(self):
        harness = Harness([{"summary": "bug", "findings": [BLOCKING]}, {"summary": "fixed", "findings": []}])
        rounds, _log, _changed, security, verification = _review(harness, _request(self.workspace))
        self.assertEqual([r["status"] for r in rounds], ["fixed", "pass"])
        # reviewer, fixer (the run's own model), reviewer
        self.assertEqual(harness.models, ["claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5-5"])
        # The full run's evidence stays, with the quick re-check of the fix beside it.
        self.assertEqual((security, verification, harness.commits), ({"scan": "new"}, {"checks": "old", "recheck": {"checks": "new"}}, 1))
        self.assertTrue(all(call.get("quick") for call in harness.verify_calls))
        self.assertEqual(ppr.summary_record(rounds, model="m")["fixed"], 1)

    def test_findings_left_after_the_last_round_are_reported_and_pushed(self):
        harness = Harness([{"summary": "a", "findings": [BLOCKING]}] * 3)
        rounds, *_ = _review(harness, _request(self.workspace, rounds=2))
        self.assertEqual([r["status"] for r in rounds], ["fixed", "fixed", "open"])
        self.assertEqual(ppr.summary_record(rounds, model="m")["open_blocking"], [BLOCKING])

    def test_a_fix_that_breaks_verification_is_dropped(self):
        harness = Harness([{"summary": "a", "findings": [BLOCKING]}], verify_ok=[False])
        rounds, _log, _changed, security, _verification = _review(harness, _request(self.workspace))
        self.assertEqual([r["status"] for r in rounds], ["fix_failed"])
        self.assertIn(["git", "reset", "--hard", "good-sha"], harness.git)
        self.assertEqual((security, harness.commits), ({"scan": "old"}, 0))

    def test_the_review_stops_starting_rounds_when_its_time_runs_out(self):
        harness = Harness([{"summary": "a", "findings": [BLOCKING]}] * 3)
        request = _request(self.workspace, rounds=2)
        request.review_budget_seconds = 60
        clock = iter([0.0, 30.0, 120.0])  # start, before round 2 (in budget), before round 3 (over)
        with patch("ts_harness.implementation.backends.opencode.time.monotonic", side_effect=lambda: next(clock, 999.0)):
            rounds, *_ = _review(harness, request)
        self.assertEqual([r["status"] for r in rounds], ["fixed", "fixed", "out_of_time"])
        record = ppr.summary_record(rounds, model="m")
        self.assertEqual((record["status"], record["fixed"], record["open_blocking"]), ("out_of_time", 2, []))

    def test_no_reviewer_or_no_usable_review_skips_it(self):
        harness = Harness([])
        self.assertEqual(_review(harness, _request(self.workspace, model=None))[0], [])
        harness = Harness([None])
        rounds, *_ = _review(harness, _request(self.workspace))
        self.assertEqual([r["status"] for r in rounds], ["no_review"])
        self.assertEqual(ppr.summary_record(rounds, model="m")["open_blocking"], [])


class PromptTests(unittest.TestCase):
    def test_review_prompt_uses_the_gate_rules_and_the_revision_findings(self):
        prompt = ppr.build_review_prompt(work_order_id="WO-1", work_order_body="Body", base_ref="origin/dev",
                                         output_path=Path("/w/.ts-harness/review/review-1.json"),
                                         review_feedback="- [major] src/a.ts:4 off by one", human_change_requests=["Use cents"])
        self.assertIn("git diff origin/dev...HEAD", prompt)
        self.assertIn("Do not edit", prompt)
        self.assertIn("This is a revision", prompt)
        self.assertIn("- Use cents", prompt)
        self.assertIn("/w/.ts-harness/review/review-1.json", prompt)
        fix = ppr.build_fix_prompt("WO-1", [BLOCKING])
        self.assertIn("[major/correctness] src/a.ts:4 Off by one", fix)

    def test_blocking_follows_the_gate(self):
        self.assertTrue(ppr.is_blocking(BLOCKING))
        self.assertFalse(ppr.is_blocking(MINOR))
        self.assertFalse(ppr.is_blocking({"severity": "major", "category": "tests"}))
        self.assertTrue(ppr.is_blocking({"severity": "blocker", "category": "tests"}))

    def test_settings(self):
        with patch.dict("os.environ", {"TS_HARNESS_PRE_PUSH_REVIEW_MODEL": "off"}):
            self.assertIsNone(ppr.review_model())
        with patch.dict("os.environ", {"TS_HARNESS_PRE_PUSH_REVIEW_MODEL": "anthropic/claude-opus-5-5", "TS_HARNESS_PRE_PUSH_REVIEW_ROUNDS": "1"}):
            self.assertEqual((ppr.review_model(), ppr.review_rounds()), ("anthropic/claude-opus-5-5", 1))


if __name__ == "__main__":
    unittest.main()
