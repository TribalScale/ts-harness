"""Onboard a repo: draft its ``.ts-harness.yml`` and the work order that makes it pass.

The draft comes from what the repo already runs in CI: the e2e job's commands (and
the startup script a compose-based job hands to its container), compose services,
the env template CI copies to ``.env``, and the Playwright config. It is a starting
point: the onboarding work order has OpenCode run ``ts-harness-integration`` and fix
the file until the suite passes in a coding job, and the host verifies the declared
suite before the PR opens. The team reviews a PR instead of writing the file.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ts_harness.implementation.test_services import (
    CONFIG_NAMES,
    compose_services,
    detect_services,
    find_env_template,
    playwright_config,
)

from ts_harness.implementation.verification import ONBOARD_MARKER  # noqa: E402 - shared marker
_E2E_HINTS = ("e2e", "playwright", "cypress", "integration", "acceptance")
_INSTALL = re.compile(
    r"^(npm (ci|install|i)\b|yarn( install)?( --[\w-]+)*$|pnpm (install|i)\b|corepack\b|bun install\b|pip install\b|"
    r"(npx |yarn |pnpm (exec |dlx )?)?playwright install\b|apt(-get)? |sudo )"
)
_CHECKS = re.compile(r"\b(lint|eslint|prettier|typecheck|type-check|check-types|tsc)\b")
_TEST = re.compile(r"\b(test|e2e|playwright|cypress|jest|vitest|pytest)\b")
_WAIT_FOR_IT = re.compile(r"wait-for-it(?:\.sh)?\s+(?:-t\s+\d+\s+)?([\w.-]+:\d+)")
_WAIT_ON = re.compile(r"wait-on\s+(\S+)")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _commands(text: str) -> list[str]:
    """Shell lines, continuations joined, comments and blanks dropped."""
    joined = re.sub(r"\\\n\s*", " ", text)
    out = []
    for line in joined.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(("set -", "#!/")):
            continue
        out.extend(part.strip() for part in re.split(r"\s+&&\s+", line) if part.strip())
    return out


def _ci_jobs(repo_dir: Path) -> list[tuple[str, list[str]]]:
    """``(job id, run lines)`` for every GitHub Actions job."""
    import yaml

    jobs = []
    for path in sorted((Path(repo_dir) / ".github" / "workflows").glob("*.y*ml")):
        try:
            data = yaml.safe_load(_read(path)) or {}
        except Exception:  # noqa: BLE001 - an unparsable workflow is skipped
            continue
        for job_id, job in (data.get("jobs") or {}).items() if isinstance(data, dict) else []:
            steps = (job or {}).get("steps") or [] if isinstance(job, dict) else []
            lines = []
            for step in steps:
                if isinstance(step, dict) and step.get("run"):
                    lines.extend(_commands(str(step["run"])))
            jobs.append((f"{path.name}:{job_id}", lines))
    return jobs


def _e2e_job(repo_dir: Path) -> tuple[str, list[str]] | None:
    scored = []
    for job_id, lines in _ci_jobs(repo_dir):
        text = (job_id + " " + " ".join(lines)).lower()
        score = sum(text.count(hint) for hint in _E2E_HINTS)
        if score:
            scored.append((score, job_id, lines))
    if not scored:
        return None
    _score, job_id, lines = max(scored)
    return job_id, lines


def _startup_scripts(repo_dir: Path, lines: list[str]) -> list[str]:
    """Commands of repo scripts a compose-based job runs inside its container (``startup.*.sh``)."""
    if not any("docker compose" in line or "docker-compose" in line for line in lines):
        return []
    import yaml

    found: list[str] = []
    scripts: list[str] = []
    for compose in sorted(Path(repo_dir).glob("docker-compose*.y*ml")):
        if not any(compose.name in line for line in lines):
            continue
        try:
            data = yaml.safe_load(_read(compose)) or {}
        except Exception:  # noqa: BLE001
            continue
        for spec in (data.get("services") or {}).values():
            build = (spec or {}).get("build") if isinstance(spec, dict) else None
            dockerfile = build.get("dockerfile") if isinstance(build, dict) else None
            if not dockerfile:
                continue
            for script in re.findall(r"([\w.-]+\.sh)", _read(Path(repo_dir) / dockerfile)):
                path = Path(repo_dir) / script
                if path.is_file() and "wait-for-it" not in script and script not in scripts:
                    scripts.append(script)
                    found.extend(_commands(_read(path)))
    return found


_RUN_SCRIPT = re.compile(r"^(npm run|npm|yarn run|yarn|pnpm run|pnpm|bun run)\s+([\w:.-]+)(.*)$")


def expand_scripts(lines: list[str], scripts: dict[str, str]) -> list[str]:
    """Inline package scripts that chain several steps (``pm2 start ... && playwright test``)."""
    out: list[str] = []
    for line in lines:
        match = _RUN_SCRIPT.match(line)
        body = scripts.get(match.group(2), "") if match else ""
        if body and re.search(r"&&|\bpm2\b|&\s*$|\bconcurrently\b|start-server-and-test", body):
            runner = match.group(1).split()[0]
            for part in _commands(body):
                nested = _RUN_SCRIPT.match(part)
                if nested and nested.group(1).split()[0] in {"npm", "yarn", "pnpm", "bun"}:
                    part = f"{runner} {'run ' if runner != 'yarn' else ''}{nested.group(2)}{nested.group(3)}"
                out.append(part)
            if match.group(3).strip():
                out[-1] = f"{out[-1]}{match.group(3)}"
        else:
            out.append(line)
    return out


def _local(target: str, compose_names: set[str]) -> str:
    host, _, port = target.rpartition(":")
    return f"127.0.0.1:{port}" if host in compose_names | {"localhost", "0.0.0.0"} else target


def draft_integration(lines: list[str], compose_names: set[str]) -> tuple[dict[str, Any], list[str]]:
    """``integration`` block from CI commands in order, and notes on what it could not translate."""
    setup: list[str] = []
    background: list[dict[str, Any]] = []
    command = ""
    notes: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if line in seen and not _TEST.search(line):
            continue
        seen.add(line)
        if _INSTALL.match(line) or re.match(r"^(cp|mv) \S*env\S* \.env", line) or line.startswith(("echo ", "cd ", "tail ", "cat ", "ls ")):
            continue
        if "docker compose" in line or "docker-compose" in line or line.startswith("docker "):
            continue
        wait = _WAIT_FOR_IT.search(line) or _WAIT_ON.search(line)
        if wait:
            target = wait.group(1)
            host = target.rpartition(":")[0] if "://" not in target else (re.match(r"\w+://([^:/]+)", target) or [None, ""])[1]
            port = target.rsplit(":", 1)[-1].split("/", 1)[0]
            if port in {"5432", "3306", "6379", "27017"}:
                continue  # the job's databases
            pending = [b for b in background if "wait_for" not in b]
            if host in compose_names and host not in {"localhost", "127.0.0.1"} and not any(port in b["run"] for b in pending):
                notes.append(
                    f"CI waits for {target}, a Docker service there; start an equivalent as a background process "
                    f"(for example via npx) with wait_for 127.0.0.1:{port}."
                )
            elif pending:
                pending[-1]["wait_for"] = _local(target, compose_names) if "://" not in target else target
            continue
        pm2 = re.match(r"^(?:npx )?pm2 start\s+(['\"])(.+?)\1", line)
        if pm2 or re.search(r"&\s*$", line) or line.startswith("nohup "):
            run = pm2.group(2) if pm2 else re.sub(r"^nohup\s+|\s*(>+\s*\S+\s*)?(2>&1\s*)?&\s*$", "", line).strip()
            background.append({"run": run})
            continue
        if _CHECKS.search(line) and not _TEST.search(line):
            continue
        if _TEST.search(line) and not re.search(r"\b(migrat|seed|build)\b", line):
            command = line
            continue
        setup.append(line)
    for item in background:
        if "wait_for" not in item:
            notes.append(f"Add `wait_for` (host:port or URL) for background `{item['run']}` so the suite starts after it is up.")
    block: dict[str, Any] = {}
    if setup:
        block["setup"] = setup
    if background:
        block["background"] = background
    if command:
        block["command"] = command
    return block, notes


def draft_config(repo_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """A draft ``.ts-harness.yml`` and notes for the onboarding work order."""
    repo_dir = Path(repo_dir)
    notes: list[str] = []
    wanted, unavailable = detect_services(repo_dir, {})
    config: dict[str, Any] = {}
    if wanted:
        config["services"] = wanted
    if unavailable:
        notes.append(f"Not available in coding jobs: {', '.join(unavailable)}; tests that need them stay CI-only.")
    job = _e2e_job(repo_dir)
    lines = list(job[1]) if job else []
    env_copy = next((m.group(1) for line in lines if (m := re.match(r"^(?:cp|mv) (\S+) \.env\b", line))), "")
    template = Path(repo_dir) / env_copy if env_copy else find_env_template(repo_dir, {}, wanted)
    if template is not None and template.is_file():
        config["env_file"] = template.name if template.parent == repo_dir else str(template.relative_to(repo_dir))
    if job:
        notes.append(f"Drafted from CI job {job[0]}.")
        from ts_harness.implementation.test_env import package_scripts

        lines = expand_scripts(lines + _startup_scripts(repo_dir, lines), package_scripts(repo_dir))
        block, more = draft_integration(lines, set(compose_services(repo_dir)))
        notes.extend(more)
        if block.get("command"):
            config["integration"] = {**block, "timeout_seconds": 1200}
    if "integration" not in config and playwright_config(repo_dir) is not None:
        config["integration"] = {"command": "npx playwright test --reporter=line", "timeout_seconds": 900}
        notes.append("Playwright config found; its webServer starts the app.")
    if "integration" not in config:
        notes.append("No e2e or integration job found in CI; the file only sets up services.")
    return config, notes


def render_config(config: dict[str, Any], notes: list[str]) -> str:
    import yaml

    header = ["# How TS Harness coding jobs run this repo's database and e2e tests.", "# See docs/runbooks/implement-work-orders-opencode.md in ts-harness."]
    return "\n".join(header) + "\n" + yaml.safe_dump(config, sort_keys=False, width=120)


def repo_has_e2e_suite(repo_dir: Path) -> bool:
    """CI runs an e2e/integration suite, or the repo has a Playwright config: onboarding must declare it."""
    return _e2e_job(repo_dir) is not None or playwright_config(repo_dir) is not None


def onboarding_work_order(work_order_id: str, repo_dir: Path) -> str:
    config, notes = draft_config(repo_dir)
    existing = next((name for name in CONFIG_NAMES if (Path(repo_dir) / name).is_file()), "")
    draft = render_config(config, notes)
    note_lines = "\n".join(f"- {note}" for note in notes) or "- (none)"
    return f"""# {work_order_id} Onboard this repository to TS Harness coding jobs
{ONBOARD_MARKER}

## Summary
chore: Add `.ts-harness.yml` so TS Harness coding jobs run this repo's integration/e2e suite with real
databases and a browser before opening pull requests, the way CI does.{" Update the existing file." if existing else ""}

## Requirements
- REQ-1: `.ts-harness.yml` declares the services the tests need, the env template, and an `integration` block
  (setup steps, background processes with `wait_for`, and the e2e command) that mirrors the repo's CI e2e job.
- REQ-2: `ts-harness-integration` passes in this coding job. Run it, read its output, and fix the file until it
  passes. Translate Docker-only CI steps into local commands (for example a mail catcher via `npx`, the app via its
  start script); databases are already provided by the job.
- REQ-3: Only `.ts-harness.yml` changes. Do not edit application code, tests, CI, or env templates; do not skip,
  filter or weaken tests to make the suite pass. If a test genuinely cannot run outside Docker, say which in the
  file as a comment and leave the suite as CI runs it.

## Draft
The host drafted this from CI; it may be incomplete:

```yaml
{draft.rstrip()}
```

Draft notes:
{note_lines}

## Format
`services` (postgres, mysql, mongodb, redis), `env_file`, `env` (extra values), and `integration` with `setup`
(commands run in order), `background` (`run`, optional `wait_for` as `host:port` or an http URL, optional
`ready_timeout_seconds`), `command`, `timeout_seconds`.

## In Scope
- `.ts-harness.yml`

## Out of Scope
- Any other file.

## Files
- .ts-harness.yml
"""
