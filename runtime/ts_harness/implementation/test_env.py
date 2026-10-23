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

# Shipped in docker/Dockerfile.agent-runtime; prompts quote this list.
HOST_TOOLCHAINS = (
    "Python (per-work-order venv with pytest and behave)",
    "Node.js with npm",
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
_LOCAL_EXCLUDES = {
    "node": ("node_modules/",),
    "cargo": ("target/",),
    "maven": ("target/",),
    "gradle": (".gradle/", "build/"),
}
_EXCLUDE_MARKER = "# ts-harness test environment"


@dataclass
class TestEnvironment:
    __test__ = False

    python: str
    env: dict[str, str] = field(default_factory=dict)
    stack: str | None = None
    state_dir: Path | None = None

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
        return ["npm", "test"] if "test" in package_scripts(repo_dir) else ["npm", "run", "test:unit"]
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
            return ["npm", "run", script]
    features = repo_dir / "features"
    if (
        (repo_dir / "behave.ini").is_file()
        or (features / "environment.py").is_file()
        or ((features / "steps").is_dir() and any((features / "steps").glob("*.py")))
    ):
        return [python, "-m", "behave"]
    return None


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
        "CARGO_HOME": str(cache / "cargo"),
        "GRADLE_USER_HOME": str(cache / "gradle"),
        "MAVEN_OPTS": f"-Dmaven.repo.local={cache / 'm2'}",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
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
    _exclude_build_outputs(repo_dir)
    return test_env, "\n".join(logs)


def _exclude_build_outputs(repo_dir: Path) -> None:
    """Keep dependency and build output out of commits via the clone-local exclude file."""
    info = Path(repo_dir) / ".git" / "info"
    if not (Path(repo_dir) / ".git").is_dir():
        return
    patterns = list(_LOCAL_EXCLUDES["node"])
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
        if (repo_dir / _NODE_LOCKFILE).is_file():
            steps.append((["package.json", _NODE_LOCKFILE], ["npm", "ci", "--no-audit", "--no-fund"]))
        else:
            steps.append((["package.json"], ["npm", "install", "--no-audit", "--no-fund"]))
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
