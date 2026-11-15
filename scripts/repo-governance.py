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

from ts_harness.integrations.github_client import GitHubClient, GitHubError, parse_repo_slug
from ts_harness.workflows.repo_governance import check_repo, protection_body


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("repo_url")
    parser.add_argument("--branch", default="dev")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--print-payload", action="store_true",
        help="print the protection JSON for `gh api -X PUT repos/OWNER/REPO/branches/BRANCH/protection --input -`",
    )
    args = parser.parse_args(argv)
    slug = parse_repo_slug(args.repo_url.removeprefix("https://github.com/").removesuffix(".git"))
    if slug is None:
        print(f"not a GitHub repo: {args.repo_url}", file=sys.stderr)
        return 2
    owner, repo = slug
    github = GitHubClient.from_env()
    report = check_repo(github, owner, repo, args.branch)
    print(report.text())
    body = protection_body(report, github.branch_protection(owner, repo, args.branch))
    if args.print_payload:
        import json

        print(json.dumps(body, indent=2))
    if not args.apply:
        return 1 if report.problems else 0
    try:
        github.set_branch_protection(owner, repo, args.branch, body)
    except GitHubError as exc:
        if exc.status in {403, 404}:
            print(
                "\nnot applied: the GitHub credential cannot change branch protection (needs the App's "
                "'Administration: Read and write' permission, or a repo admin). Ask an admin to run:\n"
                f"  python3 /app/scripts/repo-governance.py {args.repo_url} --branch {args.branch} --print-payload "
                f"| sed -n '/^{{/,$p' | gh api -X PUT repos/{owner}/{repo}/branches/{args.branch}/protection --input -",
                file=sys.stderr,
            )
            return 3
        raise
    print("\napplied; now:")
    after = check_repo(github, owner, repo, args.branch)
    print(after.text())
    return 1 if after.problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
