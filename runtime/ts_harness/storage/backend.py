"""Backend flags for runtime state.

``TS_HARNESS_STATE_BACKEND``  ``local`` (files on the host) or ``postgres`` (through the memory API)
``TS_HARNESS_OBJECT_BACKEND`` ``local``, ``s3`` or ``gcs`` for skill files and agent workspaces
"""

from __future__ import annotations

import os
from pathlib import Path

STATE_BACKENDS = ("local", "postgres")
OBJECT_BACKENDS = ("local", "s3", "gcs")
DEFAULT_MIRROR_DIR = "/var/lib/ts-harness/mirror"
DEFAULT_SCRATCH_DIR = "/var/tmp/ts-harness"


class StorageConfigError(RuntimeError):
    """The storage flags are set to a combination the runtime cannot run with."""


def state_backend() -> str:
    value = (os.environ.get("TS_HARNESS_STATE_BACKEND") or "local").strip().lower()
    if value not in STATE_BACKENDS:
        raise StorageConfigError(f"TS_HARNESS_STATE_BACKEND must be one of {', '.join(STATE_BACKENDS)}, got {value!r}")
    return value


def object_backend() -> str:
    value = (os.environ.get("TS_HARNESS_OBJECT_BACKEND") or "local").strip().lower()
    if value not in OBJECT_BACKENDS:
        raise StorageConfigError(f"TS_HARNESS_OBJECT_BACKEND must be one of {', '.join(OBJECT_BACKENDS)}, got {value!r}")
    return value


def shared_state_enabled() -> bool:
    return state_backend() == "postgres"


def mirror_dir() -> Path:
    return Path(os.environ.get("TS_HARNESS_MIRROR_DIR") or DEFAULT_MIRROR_DIR)


def scratch_dir() -> Path:
    return Path(os.environ.get("TS_HARNESS_SCRATCH_DIR") or DEFAULT_SCRATCH_DIR)


_STORE_OVERRIDES = (
    "TS_HARNESS_ROUTING_DEFINITION_STORE",
    "TS_HARNESS_RUNTIME_SETTINGS_STORE",
    "TS_HARNESS_MCP_SETTINGS_STORE",
    "TS_HARNESS_AGENTS_STORE",
)


def apply_mirror_paths() -> dict[str, str]:
    """Point the definition, cron and managed-skill path variables into the mirror; return what changed.

    Readers keep using those variables, so they read the mirror without knowing
    where the files really live. Idempotent.
    """
    changed: dict[str, str] = {}

    def put(name: str, value: Path | None) -> None:
        current = os.environ.get(name)
        if value is None:
            if current is not None:
                os.environ.pop(name)
                changed[name] = ""
            return
        if current != str(value):
            os.environ[name] = str(value)
            changed[name] = str(value)

    mirror = mirror_dir()
    if shared_state_enabled():
        definitions = mirror / "definitions"
        put("TS_HARNESS_WORKFLOW_DEFINITION_STORE", definitions)
        put("TS_HARNESS_MANAGED_WORKFLOWS_DIR", definitions / "active")
        put("TS_HARNESS_PROGRAM_DEFINITION_STORE", definitions / "programs")
        put("TS_HARNESS_FRAGMENT_DEFINITION_STORE", definitions / "fragments")
        for name in _STORE_OVERRIDES:
            put(name, None)
        put("TS_HARNESS_CRON_CONFIG", mirror / "cron" / "runtime-cron-jobs.json")
    if object_backend() != "local":
        put("SKILLS_MANAGED_DIR", mirror / "skills" / "current")
    return changed


def validate_storage_config() -> list[str]:
    """Raise on combinations that cannot work; return warnings for ones that work but lose data."""
    state, objects = state_backend(), object_backend()
    if objects != "local" and state == "local":
        raise StorageConfigError(
            "TS_HARNESS_OBJECT_BACKEND is "
            f"{objects!r} but TS_HARNESS_STATE_BACKEND is 'local': skills in object storage need the Postgres index"
        )
    if objects in {"s3", "gcs"}:
        missing = [name for name in ("TS_HARNESS_SKILLS_BUCKET", "TS_HARNESS_WORKSPACE_BUCKET") if not os.environ.get(name)]
        if missing:
            raise StorageConfigError(f"TS_HARNESS_OBJECT_BACKEND={objects} needs {', '.join(missing)}")
    if state == "postgres" and not os.environ.get("TS_HARNESS_SESSION_DB_URL"):
        raise StorageConfigError(
            "TS_HARNESS_STATE_BACKEND=postgres needs TS_HARNESS_SESSION_DB_URL so sessions and approvals are not kept in local files"
        )
    warnings: list[str] = []
    if objects != "local" and (os.environ.get("CODING_JOBS_ENABLED") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        warnings.append(
            "object storage is on but coding jobs run in-process: their checkouts live in the local scratch directory and are not durable"
        )
    return warnings
