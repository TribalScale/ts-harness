#!/usr/bin/env python3
"""Check (and with --apply, set) branch protection for governed delivery on one repo branch.

Run inside the agent-runtime container (GitHub credentials)::

    python3 /app/scripts/repo-governance.py https://github.com/acme/api --branch dev
    python3 /app/scripts/repo-governance.py https://github.com/acme/api --branch dev --apply

--apply needs admin on the repo. It adds the repo's CI jobs that run on PRs into the branch
and harness/code-review, harness/qa and harness/security-scan as required checks, requires
one approval with stale approvals dismissed, and keeps stricter existing settings.
"""

from __future__ import annotations

import argparse
import sys

from ts_harness.integrations.github_client import GitHubClient, parse_repo_slug
from ts_harness.workflows.repo_governance import check_repo, protection_body


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("repo_url")
    parser.add_argument("--branch", default="dev")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    slug = parse_repo_slug(args.repo_url.removeprefix("https://github.com/").removesuffix(".git"))
    if slug is None:
        print(f"not a GitHub repo: {args.repo_url}", file=sys.stderr)
        return 2
    owner, repo = slug
    github = GitHubClient.from_env()
    report = check_repo(github, owner, repo, args.branch)
    print(report.text())
    if not args.apply:
        return 1 if report.problems else 0
    body = protection_body(report, github.branch_protection(owner, repo, args.branch))
    github.set_branch_protection(owner, repo, args.branch, body)
    print("\napplied; now:")
    after = check_repo(github, owner, repo, args.branch)
    print(after.text())
    return 1 if after.problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
