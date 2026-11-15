"""Publish a change's Playwright screenshots: an evidence branch and one PR comment.

GitHub's API cannot attach images to a comment, so the images are committed to a
branch of their own, ``ts-harness-evidence/<work branch>``, as a single parentless
commit built with git plumbing (the work branch and its history are untouched), and
the PR comment links them by commit sha. Reviewers with access to the repo see them
inline; QA receives the same files. The branch is deleted when the PR merges.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from ts_harness.implementation.test_services import screenshot_files, screenshot_name
from ts_harness.integrations.github_client import parse_github_pr_url

EVIDENCE_PREFIX = "ts-harness-evidence/"
COMMENT_MARKER = "<!-- ts-harness:screenshots -->"
MAX_BYTES = 900_000  # the contents API returns files up to 1 MB with their data


def evidence_branch(work_branch: str) -> str:
    return f"{EVIDENCE_PREFIX}{work_branch}"


def _git(repo_dir: Path, *args: str, runner=subprocess.run, env: dict[str, str] | None = None, stdin: str | None = None):
    return runner(["git", "-C", str(repo_dir), *args], capture_output=True, text=True, check=False, env=env, input=stdin)


def comment_body(owner: str, repo: str, commit: str, names: list[str], head_sha: str) -> str:
    lines = [
        COMMENT_MARKER,
        "## Screenshots (TS Harness)",
        "",
        f"From the Playwright tests this change added or edited, run on `{head_sha[:12]}`: the page as each "
        "assertion passed (`step-N`), or the end of the test. QA receives the same images.",
        "",
    ]
    for name in names:
        lines += [f"**{name.rsplit('.', 1)[0]}**", "", f"![{name}](https://github.com/{owner}/{repo}/blob/{commit}/{name}?raw=true)", ""]
    return "\n".join(lines)


def publish_screenshots(
    repo_dir: Path,
    shots_dir: Path,
    *,
    work_branch: str,
    pr_url: str,
    github: Any,
    runner=subprocess.run,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Push the screenshots and add or update the PR comment. ``{}`` when there are none."""
    files = [path for path in screenshot_files(shots_dir) if path.stat().st_size <= MAX_BYTES]
    parsed = parse_github_pr_url(pr_url)
    if not files or parsed is None:
        return {}
    owner, repo, number = parsed
    entries, names = [], []
    for path in files:
        name = screenshot_name(path)
        if name in names:
            name = f"{len(names)}-{name}"
        blob = _git(repo_dir, "hash-object", "-w", str(path), runner=runner)
        if blob.returncode != 0:
            return {"error": f"hash-object failed: {blob.stderr[-300:]}"}
        entries.append(f"100644 blob {blob.stdout.strip()}\t{name}")
        names.append(name)
    tree = _git(repo_dir, "mktree", runner=runner, stdin="\n".join(entries) + "\n")
    head = _git(repo_dir, "rev-parse", "HEAD", runner=runner).stdout.strip()
    commit = _git(
        repo_dir, "-c", "user.name=TS Harness", "-c", "user.email=ts-harness@users.noreply.github.com",
        "commit-tree", tree.stdout.strip(), "-m", f"Screenshots for {work_branch} at {head[:12]}", runner=runner,
    )
    if tree.returncode != 0 or commit.returncode != 0:
        return {"error": f"evidence commit failed: {(tree.stderr or commit.stderr)[-300:]}"}
    sha = commit.stdout.strip()
    ref = evidence_branch(work_branch)
    pushed = _git(repo_dir, "push", "--force", "origin", f"{sha}:refs/heads/{ref}", runner=runner, env=env)
    if pushed.returncode != 0:
        return {"error": f"evidence push failed: {pushed.stderr[-300:]}"}
    body = comment_body(owner, repo, sha, names, head)
    comment_url = ""
    try:
        existing = next(
            (c for c in github.list_issue_comments(owner, repo, number) if COMMENT_MARKER in str(c.get("body") or "")), None
        )
        reply = github.update_comment(owner, repo, existing["id"], body) if existing else github.comment(owner, repo, number, body)
        comment_url = str((reply or {}).get("html_url") or "")
    except Exception as exc:  # noqa: BLE001 - the images are still on the branch and in QA's input
        return {"repo": f"{owner}/{repo}", "branch": ref, "commit": sha, "files": names, "error": f"comment failed: {exc}"}
    return {"repo": f"{owner}/{repo}", "branch": ref, "commit": sha, "files": names, "head_sha": head, "comment_url": comment_url}
