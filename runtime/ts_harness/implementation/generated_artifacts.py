"""Generated files a repository's CI requires to be up to date (``openapi.json``, codegen, clients).

Some CI workflows regenerate a file and fail when git sees a difference::

    - run: |
        npm run openapi
        git diff --exit-code openapi.json || (echo "::error::openapi.json is out of date" && exit 1)

A coding run that changes the source (a schema, a route) without regenerating the file passes
every local check and then fails CI, costing a full round trip. The coding job runs those
generators itself before it verifies and commits, so the regenerated files ship with the change.

Only package-script and well-known generator commands are run (``npm run X``, ``yarn X``,
``pnpm run X``, ``bun run X``, ``npx prisma generate``): a CI step can deploy or publish, and the
coding job must never run that.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_GIT_DIFF = re.compile(r"\bgit\s+diff\s+(?:--exit-code|--quiet)(?:\s+--exit-code|\s+--quiet)*(?:\s+--)?((?:\s+[^\s|&;()]+)*)")
_SAFE_COMMAND = re.compile(
    r"^(?:npm\s+run(?:-script)?\s+[\w:.-]+|yarn\s+(?:run\s+)?[\w:.-]+|pnpm\s+(?:run\s+)?[\w:.-]+|bun\s+run\s+[\w:.-]+"
    r"|npx\s+prisma\s+generate)(?:\s+--?[\w=.-]+)*$"
)
_UNSAFE_SCRIPT = re.compile(r"deploy|publish|release|push|upload|migrate", re.IGNORECASE)


@dataclass
class GeneratorStep:
    command: str
    files: list[str] = field(default_factory=list)


def _run_blocks(workflow: Any) -> list[list[str]]:
    """Each job's ``run:`` commands, in order, as lists of lines."""
    jobs = workflow.get("jobs") if isinstance(workflow, dict) else None
    blocks: list[list[str]] = []
    for job in (jobs or {}).values():
        if not isinstance(job, dict):
            continue
        lines: list[str] = []
        for step in job.get("steps") or []:
            if isinstance(step, dict) and isinstance(step.get("run"), str):
                lines.extend(line.strip() for line in step["run"].splitlines() if line.strip())
        blocks.append(lines)
    return blocks


def generator_steps_from_texts(texts: list[str]) -> list[GeneratorStep]:
    """The generators CI runs right before checking their files with ``git diff --exit-code``."""
    found: list[GeneratorStep] = []
    for text in texts:
        try:
            workflow = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            continue
        for lines in _run_blocks(workflow):
            for index, line in enumerate(lines):
                match = _GIT_DIFF.search(line)
                if not match:
                    continue
                files = [part for part in match.group(1).split() if not part.startswith("-")]
                # The generator is the nearest command before the diff that is safe to run.
                for previous in reversed(lines[:index]):
                    command = previous.strip()
                    if _SAFE_COMMAND.match(command) and not _UNSAFE_SCRIPT.search(command):
                        if all(existing.command != command for existing in found):
                            found.append(GeneratorStep(command=command, files=files))
                        break
    return found


def generator_steps(repo_dir: Path) -> list[GeneratorStep]:
    texts = []
    for path in sorted((Path(repo_dir) / ".github" / "workflows").glob("*.y*ml")):
        try:
            texts.append(path.read_text(encoding="utf-8"))
        except OSError:
            continue
    return generator_steps_from_texts(texts)


def generated_files_on_github(github: Any, owner: str, repo: str, ref: str) -> set[str]:
    """Files the repository's CI regenerates and diff-checks, read from GitHub at ``ref``."""
    import base64

    try:
        listing = github.get_file(owner, repo, ".github/workflows", ref=ref)
    except Exception:  # noqa: BLE001 - without the workflows nothing is exempt
        return set()
    texts = []
    for entry in listing if isinstance(listing, list) else []:
        name = str((entry or {}).get("name") or "")
        if not name.endswith((".yml", ".yaml")):
            continue
        try:
            payload = github.get_file(owner, repo, str(entry.get("path") or f".github/workflows/{name}"), ref=ref) or {}
            texts.append(base64.b64decode(str(payload.get("content") or "")).decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            continue
    return {path for step in generator_steps_from_texts(texts) for path in step.files}


def regenerate(repo_dir: Path, *, env: dict[str, str] | None = None, runner=subprocess.run) -> list[dict[str, Any]]:
    """Run each generator; returns ``[{command, files, exit_code, changed, output_tail}]``."""
    results: list[dict[str, Any]] = []
    for step in generator_steps(repo_dir):
        kwargs = {"env": env} if env is not None else {}
        try:
            completed = runner(["sh", "-c", step.command], cwd=str(repo_dir), capture_output=True, text=True,
                               check=False, timeout=600, **kwargs)
            code, output = completed.returncode, "\n".join(p for p in (completed.stdout, completed.stderr) if p)
        except subprocess.TimeoutExpired:
            code, output = 124, f"{step.command} timed out after 600s"
        changed: list[str] = []
        if code == 0:
            status = runner(["git", "status", "--porcelain", "--", *(step.files or ["."])], cwd=str(repo_dir),
                            capture_output=True, text=True, check=False)
            changed = [line[3:].strip() for line in (status.stdout or "").splitlines() if line.strip()]
        results.append({"command": step.command, "files": step.files, "exit_code": code, "changed": changed,
                        "output_tail": output.strip()[-1500:]})
    return results
