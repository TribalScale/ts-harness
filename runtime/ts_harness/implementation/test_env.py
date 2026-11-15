"""Per-work-order test environment: stack detection, isolated deps, toolchain env.

The host picks the target repo's test command from its manifests, installs the
dependencies those manifests declare (Python into a per-work-order virtualenv,
never the runtime's own site-packages), and runs the command with an env that
both OpenCode and host verification share.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ts_harness.implementation.node_version import ensure_node
from ts_harness.implementation.test_services import write_agent_command

# Shipped in docker/Dockerfile.agent-runtime; prompts quote this list.
HOST_TOOLCHAINS = (
    "Python (per-work-order venv with pytest and behave)",
    "Node.js with npm, plus pnpm and yarn through corepack",
    "Go",
    "JDK 21 with Maven and Gradle",
    "Rust with cargo",
    "make",
)

STACKS = ("node", "go", "cargo", "maven", "gradle", "python", "make")
# Cucumber/godog suites for these stacks run inside the unit command.
BDD_IN_UNIT_STACKS = frozenset({"go", "cargo", "maven", "gradle"})

DEFAULT_WHEELS_DIR = "/opt/ts-harness/wheels"
DEFAULT_RUNNER_REQUIREMENTS = "/opt/ts-harness/test-runner-requirements.txt"
FALLBACK_RUNNER_PACKAGES = ("pytest", "behave")

_PYTHON_REQUIREMENTS = ("requirements.txt", "requirements-dev.txt", "requirements-test.txt")
_NODE_LOCKFILE = "package-lock.json"
# Lockfile per package manager; pnpm and yarn run through corepack, which ships with Node.
NODE_LOCKFILES = {"pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock", "npm": _NODE_LOCKFILE}
_LOCAL_EXCLUDES = {
    "node": ("node_modules/",),
    "cargo": ("target/",),
    "maven": ("target/",),
    "gradle": (".gradle/", "build/"),
}
_EXCLUDE_MARKER = "# ts-harness test environment"
HARNESS_LOCAL_PATHS = (".opencode/",)


@dataclass
class TestEnvironment:
    __test__ = False

    python: str
    env: dict[str, str] = field(default_factory=dict)
    stack: str | None = None
    state_dir: Path | None = None
    # What test services (databases) this job runs, for the coding prompt.
    services_note: str = ""
    services_running: bool = False

    def merged_env(self) -> dict[str, str]:
        return {**os.environ, **self.env}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def package_scripts(repo_dir: Path) -> dict[str, str]:
    package_json = Path(repo_dir) / "package.json"
    if not package_json.is_file():
        return {}
    try:
        data = json.loads(_read_text(package_json))
    except json.JSONDecodeError:
        return {}
    scripts = data.get("scripts", {}) if isinstance(data, dict) else {}
    return scripts if isinstance(scripts, dict) else {}


def package_manifest(repo_dir: Path) -> dict:
    try:
        data = json.loads(_read_text(Path(repo_dir) / "package.json") or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def node_package_manager(repo_dir: Path) -> str:
    """npm, pnpm or yarn: the ``packageManager`` field first, then the lockfile."""
    repo_dir = Path(repo_dir)
    declared = str(package_manifest(repo_dir).get("packageManager") or "").split("@", 1)[0].strip()
    if declared in NODE_LOCKFILES:
        return declared
    for manager in ("pnpm", "yarn"):
        if (repo_dir / NODE_LOCKFILES[manager]).is_file():
            return manager
    return "npm"


def _yarn_berry(repo_dir: Path) -> bool:
    repo_dir = Path(repo_dir)
    declared = str(package_manifest(repo_dir).get("packageManager") or "")
    if declared.startswith("yarn@"):
        return not declared.split("@", 1)[1].startswith("1.")
    return (repo_dir / ".yarnrc.yml").is_file()


def node_run_command(repo_dir: Path, script: str) -> list[str]:
    """The command that runs one package.json script with the repo's package manager."""
    manager = node_package_manager(repo_dir)
    if script == "test":
        return [manager, "test"]
    return [manager, "run", script]


def uses_vitest(repo_dir: Path) -> bool:
    manifest = package_manifest(repo_dir)
    deps = {**(manifest.get("dependencies") or {}), **(manifest.get("devDependencies") or {})}
    return "vitest" in deps or any("vitest" in str(cmd) for cmd in (manifest.get("scripts") or {}).values())


def uses_playwright(repo_dir: Path) -> bool:
    manifest = package_manifest(repo_dir)
    deps = {**(manifest.get("dependencies") or {}), **(manifest.get("devDependencies") or {})}
    return "@playwright/test" in deps or "playwright" in deps


def node_install_command(repo_dir: Path) -> tuple[list[str], list[str]]:
    """(manifests that trigger the install, command) for the repo's package manager."""
    repo_dir = Path(repo_dir)
    manager = node_package_manager(repo_dir)
    lockfile = NODE_LOCKFILES[manager]
    if not (repo_dir / lockfile).is_file():
        if manager == "npm":
            return ["package.json"], ["npm", "install", "--no-audit", "--no-fund"]
        return ["package.json"], [manager, "install"]
    if manager == "pnpm":
        return ["package.json", lockfile], ["pnpm", "install", "--frozen-lockfile"]
    if manager == "yarn":
        flag = "--immutable" if _yarn_berry(repo_dir) else "--frozen-lockfile"
        return ["package.json", lockfile], ["yarn", "install", flag]
    return ["package.json", lockfile], ["npm", "ci", "--no-audit", "--no-fund"]


def _has_python_tests(repo_dir: Path) -> bool:
    repo_dir = Path(repo_dir)
    if (repo_dir / "pytest.ini").is_file() or (repo_dir / "conftest.py").is_file():
        return True
    if "[tool.pytest" in _read_text(repo_dir / "pyproject.toml"):
        return True
    if "[tool:pytest]" in _read_text(repo_dir / "setup.cfg"):
        return True
    if "[pytest]" in _read_text(repo_dir / "tox.ini"):
        return True
    tests = repo_dir / "tests"
    return tests.is_dir() and any(tests.rglob("*.py"))


def _has_make_test(repo_dir: Path) -> bool:
    return bool(re.search(r"^test:", _read_text(Path(repo_dir) / "Makefile"), re.MULTILINE))


def detect_stack(repo_dir: Path) -> str | None:
    """The repo's test stack from its manifests; first match wins."""
    repo_dir = Path(repo_dir)
    scripts = package_scripts(repo_dir)
    if "test" in scripts or "test:unit" in scripts:
        return "node"
    if (repo_dir / "go.mod").is_file():
        return "go"
    if (repo_dir / "Cargo.toml").is_file():
        return "cargo"
    if (repo_dir / "mvnw").is_file() or (repo_dir / "pom.xml").is_file():
        return "maven"
    if any((repo_dir / name).is_file() for name in ("gradlew", "build.gradle", "build.gradle.kts")):
        return "gradle"
    if _has_python_tests(repo_dir):
        return "python"
    if _has_make_test(repo_dir):
        return "make"
    return None


def _wrapper(repo_dir: Path, name: str) -> list[str]:
    path = Path(repo_dir) / name
    return [f"./{name}"] if os.access(path, os.X_OK) else ["sh", f"./{name}"]


def unit_test_command(repo_dir: Path, *, python: str = "python3") -> list[str] | None:
    repo_dir = Path(repo_dir)
    stack = detect_stack(repo_dir)
    if stack == "node":
        return node_run_command(repo_dir, "test" if "test" in package_scripts(repo_dir) else "test:unit")
    if stack == "go":
        return ["go", "test", "./..."]
    if stack == "cargo":
        return ["cargo", "test"]
    if stack == "maven":
        base = _wrapper(repo_dir, "mvnw") if (repo_dir / "mvnw").is_file() else ["mvn"]
        return [*base, "-B", "test"]
    if stack == "gradle":
        base = _wrapper(repo_dir, "gradlew") if (repo_dir / "gradlew").is_file() else ["gradle"]
        return [*base, "test"]
    if stack == "python":
        return [python, "-m", "pytest"]
    if stack == "make":
        return ["make", "test"]
    return None


def bdd_test_command(repo_dir: Path, *, python: str = "python3") -> list[str] | None:
    repo_dir = Path(repo_dir)
    scripts = package_scripts(repo_dir)
    for script in ("test:bdd", "cucumber", "bdd"):
        if script in scripts:
            return node_run_command(repo_dir, script)
    features = repo_dir / "features"
    if (
        (repo_dir / "behave.ini").is_file()
        or (features / "environment.py").is_file()
        or ((features / "steps").is_dir() and any((features / "steps").glob("*.py")))
    ):
        return [python, "-m", "behave"]
    return None


_TYPECHECK_SCRIPTS = ("typecheck", "type-check", "check-types", "types:check", "tsc")
_LINTABLE_SUFFIXES = (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs")
_ESLINT_CONFIGS = tuple(
    [f"eslint.config.{ext}" for ext in ("js", "mjs", "cjs", "ts", "mts", "cts")]
    + [f".eslintrc{ext}" for ext in ("", ".js", ".cjs", ".json", ".yaml", ".yml")]
)
_SKIPPED_DIRS = {"node_modules", ".git", "dist", "build", "target", "vendor", ".venv", ".ts-harness"}


def _tsconfig_has_sources(path: Path) -> bool:
    """False for a solution-style tsconfig (references only), which ``tsc -p`` cannot check alone."""
    text = _read_text(path)
    if '"references"' not in text or '"include"' in text:
        return True
    return '"files"' in text and not re.search(r'"files"\s*:\s*\[\s*\]', text)


def node_static_checks(repo_dir: Path, changed_files: list[str] | None) -> list[tuple[str, list[str]]]:
    """Type check and lint for a Node repo: what its CI usually runs besides the tests.

    The type check is the repo's own script when it has one, else ``tsc --noEmit`` on its
    tsconfig. ESLint runs on the changed files only, so lint debt elsewhere does not block.
    """
    repo_dir = Path(repo_dir)
    if detect_stack(repo_dir) != "node":
        return []
    checks: list[tuple[str, list[str]]] = []
    scripts = package_scripts(repo_dir)
    bin_dir = repo_dir / "node_modules" / ".bin"
    script = next((name for name in _TYPECHECK_SCRIPTS if name in scripts), None)
    tsconfig = repo_dir / "tsconfig.json"
    if script:
        checks.append(("typecheck", node_run_command(repo_dir, script)))
    elif tsconfig.is_file() and (bin_dir / "tsc").exists() and _tsconfig_has_sources(tsconfig):
        checks.append(("typecheck", [str(bin_dir / "tsc"), "--noEmit", "-p", "tsconfig.json"]))
    has_config = any((repo_dir / name).is_file() for name in _ESLINT_CONFIGS) or "eslintConfig" in package_manifest(repo_dir)
    files = sorted(
        {
            path
            for path in (changed_files or [])
            if path.endswith(_LINTABLE_SUFFIXES)
            and not set(Path(path).parts) & _SKIPPED_DIRS
            and (repo_dir / path).is_file()
        }
    )
    if files and has_config and (bin_dir / "eslint").exists():
        checks.append(("lint", [str(bin_dir / "eslint"), *files]))
    if "build" in scripts and is_web_app(repo_dir):
        # Bundlers (Vite, Next, CRA) fail on things the type check passes: unresolved
        # imports, server/client boundaries, env access at build time.
        checks.append(("build", node_run_command(repo_dir, "build")))
    return checks


_WEB_FRAMEWORKS = ("react", "react-dom", "next", "vite", "@vitejs/plugin-react", "react-scripts", "@remix-run/react", "vue", "svelte")


def is_web_app(repo_dir: Path) -> bool:
    """A browser app (React and friends), as opposed to a Node service or library."""
    manifest = package_manifest(repo_dir)
    deps = {**(manifest.get("dependencies") or {}), **(manifest.get("devDependencies") or {})}
    return "react-dom" in deps or "next" in deps or ("vite" in deps and any(name in deps for name in _WEB_FRAMEWORKS[:2]))


def repo_feature_files(repo_dir: Path) -> list[Path]:
    """Gherkin feature files the repo owns (dependencies and build output excluded)."""
    found = []
    for path in Path(repo_dir).rglob("*.feature"):
        if not set(path.relative_to(repo_dir).parts) & _SKIPPED_DIRS:
            found.append(path)
    return found


def repo_runs_bdd(repo_dir: Path) -> bool:
    """True when acceptance scenarios belong in Gherkin for this repo.

    That is a repo that already has feature files or a BDD runner, or one with no test
    setup at all yet. A repo with its own test runner and no Gherkin (Jest, Vitest, Go
    tests...) covers acceptance scenarios with that runner instead.
    """
    repo_dir = Path(repo_dir)
    if repo_feature_files(repo_dir) or bdd_test_command(repo_dir) is not None:
        return True
    manifest = package_manifest(repo_dir)
    deps = {**(manifest.get("dependencies") or {}), **(manifest.get("devDependencies") or {})}
    if any(name in deps for name in ("@cucumber/cucumber", "cucumber", "jest-cucumber")):
        return True
    return unit_test_command(repo_dir) is None


def required_tools(command: list[str]) -> list[str]:
    """Executables a command needs on PATH (wrappers still need a JVM)."""
    head = command[0]
    if head == "sh" and len(command) > 1:
        head = command[1]
    if head in {"./mvnw", "./gradlew"}:
        return ["java"]
    if head in {"mvn", "gradle"}:
        return ["java", head]
    return [head]


def missing_tools(command: list[str], env: dict[str, str]) -> list[str]:
    path = env.get("PATH")
    missing = []
    for tool in required_tools(command):
        if os.path.isabs(tool):
            if not os.access(tool, os.X_OK):
                missing.append(tool)
        elif shutil.which(tool, path=path) is None:
            missing.append(tool)
    return missing


def _run(runner, command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    try:
        return runner(command, cwd=str(cwd), capture_output=True, text=True, check=False, env=env)
    except Exception as exc:  # noqa: BLE001
        return subprocess.CompletedProcess(command, 1, stdout="", stderr=str(exc))


def _tail(completed: subprocess.CompletedProcess) -> str:
    return (completed.stderr or completed.stdout or "").strip()[-1500:]


def _runner_install_command(python: str) -> list[str]:
    wheels = os.environ.get("TS_HARNESS_TEST_RUNNER_WHEELS", DEFAULT_WHEELS_DIR)
    requirements = os.environ.get("TS_HARNESS_TEST_RUNNER_REQUIREMENTS", DEFAULT_RUNNER_REQUIREMENTS)
    targets = ["-r", requirements] if Path(requirements).is_file() else list(FALLBACK_RUNNER_PACKAGES)
    command = [python, "-m", "pip", "install", "-q"]
    if Path(wheels).is_dir():
        command += ["--no-index", "--find-links", wheels]
    return [*command, *targets]


def prepare_test_environment(
    workspace_dir: Path,
    repo_dir: Path,
    *,
    runner=subprocess.run,
) -> tuple[TestEnvironment, str]:
    """Create (once) the work order's venv with real test runners, plus shared caches."""
    workspace_dir = Path(workspace_dir)
    state_dir = workspace_dir / ".ts-harness"
    state_dir.mkdir(parents=True, exist_ok=True)
    venv = state_dir / "venv"
    bin_dir = venv / "bin"
    python = str(bin_dir / "python")
    cache = workspace_dir.parent / ".cache"
    env = {
        "VIRTUAL_ENV": str(venv),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GOPATH": str(cache / "go"),
        "GOCACHE": str(cache / "go-build"),
        "GOMODCACHE": str(cache / "go" / "pkg" / "mod"),
        "npm_config_cache": str(cache / "npm"),
        "COREPACK_HOME": str(cache / "corepack"),
        "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
        "YARN_CACHE_FOLDER": str(cache / "yarn"),
        "CARGO_HOME": str(cache / "cargo"),
        "GRADLE_USER_HOME": str(cache / "gradle"),
        "MAVEN_OPTS": f"-Dmaven.repo.local={cache / 'm2'}",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        # Playwright's HTML reporter serves the report and waits after a failure unless told not to.
        "PLAYWRIGHT_HTML_OPEN": "never",
        "PW_TEST_HTML_REPORT_OPEN": "never",
    }
    if uses_vitest(repo_dir):
        # A bare `vitest` script watches for changes and never exits outside CI (OpenCode's
        # shell is a TTY); CI=true is what makes it run once in the team's own pipeline.
        env["CI"] = "true"
    if (Path(repo_dir) / "package.json").is_file() and node_package_manager(repo_dir) == "pnpm":
        # pnpm reads its store from npm-style config; npm itself warns on the unknown key
        # (and will reject it), so only pnpm repos get it.
        env["npm_config_store_dir"] = str(cache / "pnpm-store")
    test_env = TestEnvironment(python=python, env=env, stack=detect_stack(repo_dir), state_dir=state_dir)
    merged = test_env.merged_env()
    logs: list[str] = []
    marker = state_dir / "venv.ready"
    if not marker.is_file():
        created = _run(runner, ["python3", "-m", "venv", str(venv)], cwd=workspace_dir, env=merged)
        if created.returncode != 0:
            logs.append(f"venv create failed (exit {created.returncode}): {_tail(created)}")
        else:
            installed = _run(runner, _runner_install_command(python), cwd=workspace_dir, env=merged)
            if installed.returncode != 0:
                logs.append(f"test runner install failed (exit {installed.returncode}): {_tail(installed)}")
            else:
                marker.write_text("ok\n", encoding="utf-8")
                logs.append("venv ready with pytest and behave")
    node_bin, node_log = ensure_node(repo_dir, merged, cache / "node")
    if node_bin:
        env["PATH"] = f"{bin_dir}{os.pathsep}{node_bin}{os.pathsep}{os.environ.get('PATH', '')}"
        merged = test_env.merged_env()
    if node_log:
        logs.append(node_log)
    shims = _enable_corepack(runner, repo_dir, bin_dir, workspace_dir, merged)
    if shims:
        logs.append(shims)
    write_agent_command(bin_dir)
    _exclude_build_outputs(repo_dir)
    return test_env, "\n".join(logs)


def _enable_corepack(runner, repo_dir: Path, bin_dir: Path, cwd: Path, env: dict[str, str]) -> str:
    """Put pnpm and yarn shims on the work order's PATH when the repo uses one of them."""
    manager = node_package_manager(repo_dir) if (Path(repo_dir) / "package.json").is_file() else "npm"
    if manager == "npm" or (bin_dir / manager).exists():
        return ""
    if shutil.which("corepack", path=env.get("PATH")) is None:
        return f"{manager} unavailable: corepack is not installed"
    bin_dir.mkdir(parents=True, exist_ok=True)
    enabled = _run(runner, ["corepack", "enable", "--install-directory", str(bin_dir), "pnpm", "yarn"], cwd=cwd, env=env)
    if enabled.returncode != 0:
        return f"corepack enable failed (exit {enabled.returncode}): {_tail(enabled)}"
    return f"{manager} ready through corepack"


def _exclude_build_outputs(repo_dir: Path) -> None:
    """Keep dependency and build output out of commits via the clone-local exclude file."""
    info = Path(repo_dir) / ".git" / "info"
    if not (Path(repo_dir) / ".git").is_dir():
        return
    # The coding tool's skills and config are harness tooling, not part of the team's repo.
    patterns = [*HARNESS_LOCAL_PATHS, *_LOCAL_EXCLUDES["node"]]
    for stack in (detect_stack(repo_dir), "cargo" if (Path(repo_dir) / "Cargo.toml").is_file() else None):
        patterns += [p for p in _LOCAL_EXCLUDES.get(stack or "", ()) if p not in patterns]
    info.mkdir(parents=True, exist_ok=True)
    exclude = info / "exclude"
    existing = _read_text(exclude)
    lines = existing.splitlines()
    missing = [p for p in patterns if p not in lines]
    if not missing:
        return
    block = ([_EXCLUDE_MARKER] if _EXCLUDE_MARKER not in lines else []) + missing
    prefix = "" if (not existing or existing.endswith("\n")) else "\n"
    exclude.write_text(existing + prefix + "\n".join(block) + "\n", encoding="utf-8")


def _manifest_steps(repo_dir: Path, test_env: TestEnvironment) -> list[tuple[list[str], list[str]]]:
    """(manifests that trigger the step, command) for every declared dependency set."""
    repo_dir = Path(repo_dir)
    steps: list[tuple[list[str], list[str]]] = []
    for name in _PYTHON_REQUIREMENTS:
        if (repo_dir / name).is_file():
            steps.append(([name], [test_env.python, "-m", "pip", "install", "-q", "-r", name]))
    if re.search(r"^\[project\]", _read_text(repo_dir / "pyproject.toml"), re.MULTILINE):
        steps.append((["pyproject.toml"], [test_env.python, "-m", "pip", "install", "-q", "-e", "."]))
    if (repo_dir / "package.json").is_file():
        manifests, command = node_install_command(repo_dir)
        steps.append((manifests, command))
        if uses_playwright(repo_dir):
            # The browser build must match the repo's Playwright version; the image ships one
            # build and this fetches another only when the repo pins a different version.
            steps.append(([*manifests, "#playwright"], [str(repo_dir / "node_modules" / ".bin" / "playwright"), "install", "chromium"]))
    if (repo_dir / "go.mod").is_file():
        steps.append((["go.mod", "go.sum"], ["go", "mod", "download"]))
    return steps


def _digest(repo_dir: Path, manifests: list[str]) -> str:
    sha = hashlib.sha256()
    for name in manifests:
        path = Path(repo_dir) / name
        sha.update(name.encode())
        if path.is_file():
            sha.update(path.read_bytes())
    return sha.hexdigest()


def install_declared_dependencies(
    test_env: TestEnvironment,
    repo_dir: Path,
    *,
    runner=subprocess.run,
) -> str:
    """Install what the repo's manifests declare; skip manifests unchanged since the last install."""
    repo_dir = Path(repo_dir)
    _exclude_build_outputs(repo_dir)
    test_env.stack = detect_stack(repo_dir)
    state_file = (test_env.state_dir or repo_dir.parent / ".ts-harness") / "deps.json"
    try:
        state = json.loads(_read_text(state_file) or "{}")
    except json.JSONDecodeError:
        state = {}
    merged = test_env.merged_env()
    logs: list[str] = []
    for manifests, command in _manifest_steps(repo_dir, test_env):
        key = "+".join(manifests)
        digest = _digest(repo_dir, manifests)
        if state.get(key) == digest:
            continue
        label = " ".join(["python", *command[1:]] if command[0] == test_env.python else command)
        missing = missing_tools(command, merged)
        if missing:
            logs.append(f"skipped {label}: missing {', '.join(missing)}")
            continue
        completed = _run(runner, command, cwd=repo_dir, env=merged)
        if completed.returncode != 0:
            logs.append(f"{label} failed (exit {completed.returncode}): {_tail(completed)}")
            continue
        state[key] = digest
        logs.append(f"{label} ok")
    if test_env.state_dir is not None:
        test_env.state_dir.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return "\n".join(logs)
