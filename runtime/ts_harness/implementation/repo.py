"""Git clone / branch / commit / push / PR helpers for coding backends."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse, urlunparse


def delivery_suffix(delivery_workflow_id: str | None) -> str:
    """Short id that keeps one delivery's branch and workspace apart from another's."""
    return re.sub(r"[^a-zA-Z0-9-]+", "", str(delivery_workflow_id or "")).lower()[:8]


def work_order_branch_name(work_order_id: str, delivery_workflow_id: str | None = None) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(work_order_id or "").strip()).strip("-").lower()
    suffix = delivery_suffix(delivery_workflow_id)
    return f"wo/{slug or 'work-order'}" + (f"-{suffix}" if suffix else "")


def github_authenticated_url(repo_url: str, token: str | None = None) -> str:
    """Inject x-access-token into https GitHub URLs when a token is available."""
    token = (token if token is not None else os.environ.get("GITHUB_TOKEN", "")).strip()
    url = str(repo_url or "").strip()
    if not token or not url:
        return url
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return url
    host = (parsed.hostname or "").lower()
    if host not in {"github.com", "www.github.com"}:
        return url
    # Replace any existing userinfo with the PAT.
    netloc = f"x-access-token:{token}@{host}"
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


WORKSPACE_OWNER_FILE = ".ts-harness-owner"


def claim_workspace(workspace_dir: Path, owner: str) -> bool:
    """Bind ``workspace_dir`` to one delivery workflow; return True if an old clone was dropped.

    Workspaces are keyed by work-order id, which repeats across programs. A
    clone left by another delivery (or one with no owner) carries that
    delivery's ``wo/*`` commits, so it is removed and recloned from base.
    Retries and revisions of the same delivery keep their clone.
    """
    owner = str(owner or "").strip()
    if not owner:
        return False
    marker = workspace_dir / WORKSPACE_OWNER_FILE
    current = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    if current == owner:
        return False
    repo_dir = workspace_dir / "repo"
    dropped = repo_dir.exists()
    if dropped:
        shutil.rmtree(repo_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(owner + "\n", encoding="utf-8")
    return dropped


def ensure_repo_clone(
    *,
    repo_url: str,
    workspace_dir: Path,
    base_branch: str,
    clone_depth: int = 1,
    runner=subprocess.run,
    github_token: str | None = None,
) -> Path:
    """Clone ``repo_url`` into ``workspace_dir/repo`` if missing; fetch base on retries.

    On an existing clone this refreshes ``origin/<base_branch>`` but does **not**
    force-checkout the base branch. Checkout of the work-order branch is owned by
    ``create_work_branch`` so a retry cannot orphan a successful local commit.
    """
    workspace_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = workspace_dir / "repo"
    clone_url = github_authenticated_url(repo_url, github_token)
    depth = str(max(1, int(clone_depth)))
    if not (repo_dir / ".git").is_dir():
        if repo_dir.exists():
            raise RuntimeError(f"repo path exists but is not a git clone: {repo_dir}")
        cmd = [
            "git",
            "clone",
            f"--depth={depth}",
            "--branch",
            base_branch,
            clone_url,
            str(repo_dir),
        ]
        completed = runner(cmd, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise RuntimeError(
                f"git clone failed: {(completed.stderr or completed.stdout or '').strip()[-2000:]}"
            )
        _configure_commit_identity(repo_dir, runner=runner)
        return repo_dir

    # Refresh remote URL so token rotations take effect on existing clones.
    runner(
        ["git", "-C", str(repo_dir), "remote", "set-url", "origin", clone_url],
        capture_output=True,
        text=True,
        check=False,
    )
    fetch = runner(
        [
            "git",
            "-C",
            str(repo_dir),
            "fetch",
            "origin",
            f"{base_branch}:refs/remotes/origin/{base_branch}",
            "--depth",
            depth,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if fetch.returncode != 0:
        # Older remotes / shallow edges: fall back to a plain fetch of the branch tip.
        fetch = runner(
            [
                "git",
                "-C",
                str(repo_dir),
                "fetch",
                "origin",
                base_branch,
                "--depth",
                depth,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch failed: {(fetch.stderr or fetch.stdout or '').strip()[-2000:]}")
    _configure_commit_identity(repo_dir, runner=runner)
    return repo_dir


def _configure_commit_identity(repo_dir: Path, *, runner=subprocess.run) -> None:
    runner(
        ["git", "-C", str(repo_dir), "config", "user.email", "dev-agent@ts-harness.local"],
        capture_output=True,
        text=True,
        check=False,
    )
    runner(
        ["git", "-C", str(repo_dir), "config", "user.name", "ts-harness dev-agent"],
        capture_output=True,
        text=True,
        check=False,
    )


def _local_branch_exists(
    repo_dir: Path,
    branch: str,
    *,
    runner=subprocess.run,
) -> bool:
    completed = runner(
        ["git", "-C", str(repo_dir), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def _checkout(
    repo_dir: Path,
    *git_args: str,
    runner=subprocess.run,
    action: str,
) -> None:
    completed = runner(
        ["git", "-C", str(repo_dir), "checkout", *git_args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{action} failed: {(completed.stderr or completed.stdout or '').strip()[-2000:]}"
        )


def _origin_base_ref(
    repo_dir: Path,
    base_branch: str,
    *,
    runner=subprocess.run,
) -> str:
    """Return ``origin/<base>`` when present, else the local base branch name."""
    base_ref = f"origin/{base_branch}"
    has_origin_base = runner(
        ["git", "-C", str(repo_dir), "show-ref", "--verify", "--quiet", f"refs/remotes/{base_ref}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return base_ref if has_origin_base.returncode == 0 else base_branch


def _has_merge_base(
    repo_dir: Path,
    left: str,
    right: str,
    *,
    runner=subprocess.run,
) -> bool:
    completed = runner(
        ["git", "-C", str(repo_dir), "merge-base", left, right],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0 and bool((completed.stdout or "").strip())


def create_work_branch(
    repo_dir: Path,
    *,
    branch: str,
    base_branch: str,
    runner=subprocess.run,
    from_remote: bool = False,
) -> None:
    """Check out the work-order branch from the refreshed base tip.

    With ``from_remote`` (revising an open PR), a branch missing locally starts
    from ``origin/<branch>`` so the revision builds on the PR's commits instead
    of replacing them.

    Retries previously used ``git checkout -B``, which reset ``wo/*`` to the
    current HEAD (usually base) and orphaned successful Implement commits after
    a failed push. When the local branch already exists **and** shares history
    with ``origin/<base>``, check it out as-is. If the local tip is on an
    unrelated history (e.g. base was force-pushed / orphan-reset after a stale
    shallow clone), recreate the branch from the refreshed base tip.
    """
    start_point = _origin_base_ref(repo_dir, base_branch, runner=runner)

    if from_remote and not _local_branch_exists(repo_dir, branch, runner=runner):
        fetched = runner(
            ["git", "-C", str(repo_dir), "fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if fetched.returncode == 0:
            _checkout(
                repo_dir,
                "-b",
                branch,
                f"origin/{branch}",
                runner=runner,
                action=f"git checkout {branch} from origin/{branch}",
            )
            return

    if _local_branch_exists(repo_dir, branch, runner=runner):
        if _has_merge_base(repo_dir, branch, start_point, runner=runner):
            _checkout(repo_dir, branch, runner=runner, action=f"git checkout {branch}")
            return
        # Stale/unrelated wo/* tip — drop it so we can recreate from origin/base.
        _checkout(
            repo_dir,
            "-B",
            base_branch,
            start_point,
            runner=runner,
            action=f"git checkout {base_branch} from {start_point}",
        )
        delete = runner(
            ["git", "-C", str(repo_dir), "branch", "-D", branch],
            capture_output=True,
            text=True,
            check=False,
        )
        if delete.returncode != 0:
            raise RuntimeError(
                f"git branch -D {branch} failed: "
                f"{(delete.stderr or delete.stdout or '').strip()[-2000:]}"
            )
    else:
        _checkout(
            repo_dir,
            "-B",
            base_branch,
            start_point,
            runner=runner,
            action=f"git checkout {base_branch} from {start_point}",
        )

    _checkout(
        repo_dir,
        "-b",
        branch,
        runner=runner,
        action=f"git checkout -b {branch}",
    )


def list_changed_files(repo_dir: Path, *, base_ref: str | None = None, runner=subprocess.run) -> list[str]:
    """Uncommitted changes, plus files committed since ``base_ref`` when given.

    A revision runs on a branch whose earlier passes are already committed, so
    the whole PR changeset needs ``base_ref``.
    """
    # Without --untracked-files=all a new directory is reported as "dir/", hiding
    # the files (e.g. features/*.feature) that verification checks for.
    completed = runner(
        ["git", "-C", str(repo_dir), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return []
    files: list[str] = []
    for line in (completed.stdout or "").splitlines():
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if path:
            files.append(path)
    if base_ref:
        committed = runner(
            ["git", "-C", str(repo_dir), "diff", "--name-only", "--diff-filter=d", f"{base_ref}...HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if committed.returncode == 0:
            files.extend(p.strip() for p in (committed.stdout or "").splitlines() if p.strip() and p.strip() not in files)
    return files


def _ensure_python_artifact_gitignore(repo_dir: Path) -> None:
    gitignore = Path(repo_dir) / ".gitignore"
    required = ("__pycache__/", "*.py[cod]", ".pytest_cache/")
    existing = ""
    if gitignore.is_file():
        existing = gitignore.read_text(encoding="utf-8")
    missing = [line for line in required if line not in existing.splitlines()]
    if not missing:
        return
    prefix = "" if (not existing or existing.endswith("\n")) else "\n"
    gitignore.write_text(existing + prefix + "\n".join(missing) + "\n", encoding="utf-8")


def _discard_junk_worktree_noise(repo_dir: Path, *, runner=subprocess.run) -> None:
    """Drop __pycache__/bytecode noise so verify runs cannot leak into commits."""
    from ts_harness.implementation.verification import is_junk_path, normalize_changed_path

    for raw in list_changed_files(repo_dir, runner=runner):
        path = normalize_changed_path(raw)
        if not path or not is_junk_path(path):
            continue
        # Restore tracked junk; remove untracked junk.
        runner(
            ["git", "-C", str(repo_dir), "restore", "--worktree", "--staged", "--", path],
            capture_output=True,
            text=True,
            check=False,
        )
        runner(
            ["git", "-C", str(repo_dir), "clean", "-fd", "--", path],
            capture_output=True,
            text=True,
            check=False,
        )


def commit_all_changes(
    repo_dir: Path,
    *,
    message: str,
    runner=subprocess.run,
) -> bool:
    """Stage non-junk changes and commit. Returns False when nothing remains to commit."""
    _ensure_python_artifact_gitignore(repo_dir)
    _discard_junk_worktree_noise(repo_dir, runner=runner)
    add = runner(
        [
            "git",
            "-C",
            str(repo_dir),
            "add",
            "-A",
            "--",
            ".",
            ":(exclude)**/__pycache__/**",
            ":(exclude)**/*.pyc",
            ":(exclude)**/*.pyo",
            ":(exclude)**/.pytest_cache/**",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if add.returncode != 0:
        raise RuntimeError(f"git add failed: {(add.stderr or add.stdout or '').strip()[-2000:]}")
    status = runner(
        ["git", "-C", str(repo_dir), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if not (status.stdout or "").strip():
        return False
    commit = runner(
        ["git", "-C", str(repo_dir), "commit", "-m", message],
        capture_output=True,
        text=True,
        check=False,
    )
    if commit.returncode != 0:
        raise RuntimeError(
            f"git commit failed: {(commit.stderr or commit.stdout or '').strip()[-2000:]}"
        )
    return True


def _is_non_fast_forward(stderr: str, stdout: str) -> bool:
    text = f"{stderr or ''}\n{stdout or ''}".lower()
    return "non-fast-forward" in text or "fetch first" in text or "tip of your current branch is behind" in text


def push_branch(
    repo_dir: Path,
    *,
    branch: str,
    runner=subprocess.run,
    env: dict[str, str] | None = None,
    remote_url: str = "",
) -> None:
    """Push ``branch`` to origin; retry with ``--force-with-lease`` on NFF rejects.

    Work-order branches are rewritten across deliver retries (new Implement commit
    from the same base). A plain push fails when an older tip already exists on
    origin; force-with-lease updates the remote without a blind ``--force``.
    ``remote_url`` replaces origin first, because installation tokens embedded
    at clone time expire after an hour.
    """
    if remote_url:
        runner(
            ["git", "-C", str(repo_dir), "remote", "set-url", "origin", remote_url],
            capture_output=True,
            text=True,
            check=False,
        )
    completed = runner(
        ["git", "-C", str(repo_dir), "push", "-u", "origin", branch],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or "").strip()
    if not _is_non_fast_forward(completed.stderr or "", completed.stdout or ""):
        raise RuntimeError(f"git push failed: {detail[-2000:]}")

    # Refresh the remote-tracking tip used by --force-with-lease, then retry.
    runner(
        ["git", "-C", str(repo_dir), "fetch", "origin", branch, "--depth", "1"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    leased = runner(
        [
            "git",
            "-C",
            str(repo_dir),
            "push",
            "--force-with-lease",
            "-u",
            "origin",
            branch,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if leased.returncode != 0:
        raise RuntimeError(
            "git push --force-with-lease failed: "
            f"{(leased.stderr or leased.stdout or detail).strip()[-2000:]}"
        )


def _existing_pull_request_url(
    repo_dir: Path,
    *,
    head_branch: str,
    runner=subprocess.run,
    env: dict[str, str] | None = None,
) -> str:
    """Return an existing *open* PR URL for ``head_branch`` if one exists.

    Merged/closed PRs must not be reused — a re-pushed ``wo/*`` branch after a
    base rewrite needs a fresh PR, and ``gh pr view <head>`` still resolves the
    old merged PR by head ref name.
    """
    view = runner(
        ["gh", "pr", "view", head_branch, "--json", "url,state"],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if view.returncode == 0 and (view.stdout or "").strip():
        try:
            payload = json.loads((view.stdout or "").strip().splitlines()[-1])
        except json.JSONDecodeError:
            payload = {}
        url = str(payload.get("url") or "").strip()
        state = str(payload.get("state") or "").strip().upper()
        if url.startswith("http") and state == "OPEN":
            return url

    listed = runner(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--head",
            head_branch,
            "--json",
            "url",
            "--jq",
            ".[0].url // empty",
        ],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    listed_url = (
        (listed.stdout or "").strip().splitlines()[-1].strip() if (listed.stdout or "").strip() else ""
    )
    if listed.returncode == 0 and listed_url.startswith("http"):
        return listed_url
    return ""


def create_pull_request(
    repo_dir: Path,
    *,
    title: str,
    body: str,
    base_branch: str,
    head_branch: str,
    runner=subprocess.run,
    env: dict[str, str] | None = None,
) -> str:
    """Open a PR with ``gh`` and return the PR URL.

    Retry-safe: if a PR already exists for ``head_branch``, reuse its URL.
    """
    completed = runner(
        [
            "gh",
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--base",
            base_branch,
            "--head",
            head_branch,
        ],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if completed.returncode == 0:
        pr_url = (
            (completed.stdout or "").strip().splitlines()[-1].strip()
            if (completed.stdout or "").strip()
            else ""
        )
        if pr_url.startswith("http"):
            return pr_url
        raise RuntimeError(
            f"gh pr create did not return a URL: {(completed.stdout or '').strip()[-500:]}"
        )

    existing = _existing_pull_request_url(
        repo_dir,
        head_branch=head_branch,
        runner=runner,
        env=env,
    )
    if existing:
        return existing
    raise RuntimeError(
        f"gh pr create failed: {(completed.stderr or completed.stdout or '').strip()[-2000:]}"
    )
