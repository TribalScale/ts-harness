#!/usr/bin/env python3
"""Start the onboarding work order for a repository: TS Harness drafts ``.ts-harness.yml``
from the repo's CI, OpenCode makes its integration suite pass in a coding job, and the
team reviews the PR.

Run inside the agent-runtime container (it has the GitHub and memory-api credentials)::

    python3 /app/scripts/onboard-repo.py https://github.com/acme/api --base dev
    python3 /app/scripts/onboard-repo.py https://github.com/acme/api --base dev --draft-only

``--draft-only`` prints the draft and the work order without starting anything.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from ts_harness.implementation.repo_onboarding import draft_config, onboarding_work_order, render_config
from ts_harness.implementation.repo import github_authenticated_url
from ts_harness.integrations.github_client import repo_token


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("repo_url")
    parser.add_argument("--base", default="dev", help="branch the onboarding PR targets (default: dev)")
    parser.add_argument("--id", default="WO-ONBOARD", help="work order id (default: WO-ONBOARD)")
    parser.add_argument("--draft-only", action="store_true")
    parser.add_argument("--actor", default="repo-onboarding")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        clone_url = github_authenticated_url(args.repo_url, repo_token(args.repo_url) or "")
        cloned = subprocess.run(
            ["git", "clone", "-q", "--depth", "1", "-b", args.base, clone_url, str(Path(tmp) / "repo")],
            capture_output=True, text=True, check=False,
        )
        if cloned.returncode != 0:
            print(f"clone failed: {cloned.stderr.replace(clone_url, args.repo_url)[-500:]}", file=sys.stderr)
            return 1
        repo = Path(tmp) / "repo"
        body = onboarding_work_order(args.id, repo)
        if args.draft_only:
            config, notes = draft_config(repo)
            print(render_config(config, notes))
            print("\n".join(f"# note: {note}" for note in notes))
            print("\n---\n" + body)
            return 0

    from ts_harness.memory.client import get_memory_client
    from ts_harness.workflows.hooks.fork_subworkflow import fork_child

    intake = {
        "summary": f"Onboard {args.repo_url} to TS Harness coding jobs",
        "repo_url": args.repo_url,
        "base_branch": args.base,
        "work_order_id": args.id,
        "work_order_ref": args.id,
        "work_order_body": body,
        "completed_work_order_ids": [],
    }
    workflow_id = fork_child(
        get_memory_client(), parent_workflow_id=None, tenant_id="default", child_type="deliver_work_order",
        intake_input=intake, actor=args.actor,
    )
    print(workflow_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
