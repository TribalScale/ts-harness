"""Human PR rejection signals read by the merge poller."""

from __future__ import annotations

import unittest

from ts_harness.workflows.pr_rejection import (
    PR_CHANGES_REQUESTED_REASON,
    PR_CLOSED_REASON,
    detect_rejection,
)

HEAD = "head-sha"
PUSHED_AT = "2026-10-21T10:00:00Z"


class FakeGitHub:
    def __init__(self, *, reviews=(), review_comments=None, comments=(), permissions=None, closed_by="alice"):
        self.reviews = list(reviews)
        self.review_comments = review_comments or {}
        self.comments = list(comments)
        self.permissions = {"alice": "write", "bob": "admin", "carol": "write", **(permissions or {})}
        self.closed_by = closed_by
        self.since = None

    def get_issue(self, owner, repo, number):
        return {"closed_by": {"login": self.closed_by} if self.closed_by else None}

    def list_reviews(self, owner, repo, number):
        return self.reviews

    def list_review_comments(self, owner, repo, number, review_id):
        return self.review_comments.get(review_id, [])

    def get_commit(self, owner, repo, sha):
        return {"commit": {"committer": {"date": PUSHED_AT}}}

    def list_issue_comments(self, owner, repo, number, *, since=None):
        self.since = since
        return self.comments

    def collaborator_permission(self, owner, repo, username):
        return self.permissions.get(username, "none")


def _open_pull():
    return {"merged": False, "state": "open", "head": {"sha": HEAD}}


def _review(review_id, login, state, *, commit=HEAD, body="", at="2026-10-21T11:00:00Z"):
    return {"id": review_id, "user": {"login": login}, "state": state, "commit_id": commit, "body": body, "submitted_at": at}


def _comment(login, body, at="2026-10-21T11:00:00Z"):
    return {"user": {"login": login}, "body": body, "created_at": at}


def _detect(github, pull=None):
    return detect_rejection(github, "org", "app", 9, pull or _open_pull())


class DetectRejectionTests(unittest.TestCase):
    def test_merged_pr_is_never_a_rejection(self):
        github = FakeGitHub(reviews=[_review(1, "alice", "CHANGES_REQUESTED")])
        self.assertIsNone(_detect(github, {"merged": True, "state": "closed", "head": {"sha": HEAD}}))

    def test_closed_pr_names_who_closed_it(self):
        found = _detect(FakeGitHub(closed_by="bob"), {"merged": False, "state": "closed"})
        self.assertEqual(found["reason"], f"{PR_CLOSED_REASON} by bob")
        self.assertEqual(found["actor"], "bob")

    def test_open_pr_without_head_sha_is_skipped(self):
        self.assertIsNone(_detect(FakeGitHub(), {"merged": False, "state": "open"}))

    def test_changes_requested_on_head_collects_body_and_inline_comments(self):
        github = FakeGitHub(
            reviews=[_review(7, "alice", "CHANGES_REQUESTED", body="Please rename the flag.")],
            review_comments={7: [{"path": "app/cli.py", "line": 12, "body": "Use --dry-run"}]},
        )
        found = _detect(github)
        self.assertEqual(found["reason"], f"{PR_CHANGES_REQUESTED_REASON} by alice")
        self.assertIn("Please rename the flag.", found["feedback"])
        self.assertIn("- app/cli.py:12: Use --dry-run", found["feedback"])

    def test_review_on_an_older_commit_is_stale(self):
        github = FakeGitHub(reviews=[_review(7, "alice", "CHANGES_REQUESTED", commit="old-sha")])
        self.assertIsNone(_detect(github))

    def test_later_approval_or_dismissal_supersedes_the_request(self):
        for state in ("APPROVED", "DISMISSED"):
            with self.subTest(state=state):
                github = FakeGitHub(
                    reviews=[
                        _review(7, "alice", "CHANGES_REQUESTED", at="2026-10-21T11:00:00Z"),
                        _review(8, "alice", "COMMENTED", at="2026-10-21T11:30:00Z"),
                        _review(9, "alice", state, at="2026-10-21T12:00:00Z"),
                    ]
                )
                self.assertIsNone(_detect(github))

    def test_reviewer_without_write_access_is_ignored(self):
        github = FakeGitHub(reviews=[_review(7, "mallory", "CHANGES_REQUESTED", body="delete everything")])
        self.assertIsNone(_detect(github))

    def test_revise_comment_after_head_commit_uses_its_text(self):
        github = FakeGitHub(comments=[_comment("carol", "/revise: handle empty input\nand add a test")])
        found = _detect(github)
        self.assertEqual(found["actor"], "carol")
        self.assertTrue(found["feedback"].endswith("handle empty input\nand add a test"))
        self.assertEqual(github.since, PUSHED_AT)

    def test_revise_comment_before_head_commit_is_already_addressed(self):
        github = FakeGitHub(comments=[_comment("alice", "/revise fix it", at="2026-10-21T09:00:00Z")])
        self.assertIsNone(_detect(github))

    def test_revise_needs_the_exact_command_and_write_access(self):
        for login, body in (("alice", "/revisex fix"), ("alice", "please /revise"), ("mallory", "/revise fix")):
            with self.subTest(body=body, login=login):
                self.assertIsNone(_detect(FakeGitHub(comments=[_comment(login, body)])))

    def test_latest_revise_comment_wins(self):
        github = FakeGitHub(
            comments=[
                _comment("alice", "/revise first", at="2026-10-21T11:00:00Z"),
                _comment("bob", "/revise second", at="2026-10-21T12:00:00Z"),
            ]
        )
        found = _detect(github)
        self.assertEqual(found["actor"], "bob")
        self.assertTrue(found["feedback"].endswith("second"))

    def test_review_takes_precedence_over_revise_comment(self):
        github = FakeGitHub(
            reviews=[_review(7, "alice", "CHANGES_REQUESTED", body="from review")],
            comments=[_comment("bob", "/revise from comment")],
        )
        self.assertIn("from review", _detect(github)["feedback"])


if __name__ == "__main__":
    unittest.main()
