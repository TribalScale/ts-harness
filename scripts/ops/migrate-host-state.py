#!/usr/bin/env python3
"""Move host state between the container volumes and Postgres plus object storage.

Run inside the agent-runtime container (scripts/ops/migrate-host-state.sh does
that) so the volumes are visible. See docs/runbooks/shared-state.md.

Modes:
  --import  volumes -> Postgres (through the memory API) and the buckets
  --verify  compare per-file hashes and counts; exit 1 on any mismatch
  --export  Postgres and the buckets -> volumes (the rollback path)

Sections (``--only``, default all):
  definitions  the workflow definition tree        shared files "definitions"
  cron         the cron file                       shared files "cron"
  skills       managed skills: active and drafts   skill index + skills bucket
  kv           scheduler.json and delegation.json  runtime_kv
  audit        audit.jsonl (import and verify)     runtime_audit_events
  workspace    /workspace, without checkouts       workspaces bucket

With ``--object-backend local`` the skills and workspace sections are skipped:
those files stay on the host. Audit has no export; events written after the
switch stay queryable in Postgres.

Import never overwrites different remote data unless ``--force``; it reports a
conflict instead. Running it twice is safe.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
if (ROOT / "runtime" / "ts_harness").is_dir():
    sys.path.insert(0, str(ROOT / "runtime"))

SECTIONS = ("definitions", "cron", "skills", "kv", "audit", "workspace")
CRON_FILE_NAME = "runtime-cron-jobs.json"
KV_FILES = {"scheduler": "scheduler_state", "delegation": "delegation_state"}
KV_KEY = "state"
WORKSPACE_SKIP_PARTS = {"coding-backend", ".git"}
AUDIT_PAGE = 50_000
AUDIT_BATCH = 500
ACTOR = "migrate-host-state"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=str(path.parent), prefix=".migrate-", delete=False) as handle:
        handle.write(data)
        tmp = Path(handle.name)
    tmp.replace(path)


class Report:
    """Per-section counters and problems; any problem makes the run exit 1."""

    def __init__(self) -> None:
        self.sections: dict[str, dict[str, Any]] = {}

    def section(self, name: str) -> dict[str, Any]:
        return self.sections.setdefault(name, defaultdict(int, problems=[]))

    def count(self, name: str, key: str, n: int = 1) -> None:
        self.section(name)[key] += n

    def problem(self, name: str, message: str) -> None:
        self.section(name)["problems"].append(message)

    @property
    def ok(self) -> bool:
        return not any(s["problems"] for s in self.sections.values())

    def as_dict(self) -> dict[str, Any]:
        return {name: dict(values) for name, values in self.sections.items()}


# ---------------------------------------------------------------------------
# Shared file trees: definitions and cron
# ---------------------------------------------------------------------------


def _tree_local(root: Path, only: str | None = None) -> dict[str, Path]:
    from ts_harness.storage.shared_tree import _hidden

    if only is not None:
        path = root / only
        return {only: path} if path.is_file() else {}
    if not root.is_dir():
        return {}
    out: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        if not _hidden(rel):
            out[rel.as_posix()] = path
    return out


def _tree_remote(client: Any, namespace: str) -> tuple[int, dict[str, str]]:
    out = client.pull_shared_files(namespace, since=0)
    files = {str(f["path"]): str(f.get("content") or "") for f in out.get("files") or [] if not f.get("deleted")}
    return int(out.get("generation") or 0), files


def _tree_sources(args: argparse.Namespace, section: str) -> tuple[str, dict[str, Path], dict[str, str]]:
    """(namespace, local files keyed by shared path, shared path -> local path name for messages)."""
    if section == "definitions":
        root = Path(args.definitions_dir)
        local = _tree_local(root)
        return "definitions", local, {rel: str(p) for rel, p in local.items()}
    cron = Path(args.cron_file)
    local = {CRON_FILE_NAME: cron} if cron.is_file() else {}
    return "cron", local, {CRON_FILE_NAME: str(cron)}


def import_tree(args: argparse.Namespace, client: Any, report: Report, section: str) -> None:
    namespace, local, _ = _tree_sources(args, section)
    generation, remote = _tree_remote(client, namespace)
    puts: list[dict[str, str]] = []
    for rel, path in local.items():
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            report.problem(section, f"not UTF-8 text, skipped: {rel}")
            continue
        if rel in remote and remote[rel] == content:
            report.count(section, "unchanged")
            continue
        if rel in remote and not args.force:
            report.problem(section, f"already in Postgres with different content (use --force to overwrite): {rel}")
            continue
        puts.append({"path": rel, "content": content})
    extra = sorted(set(remote) - set(local))
    deletes = extra if args.force else []
    if extra and not args.force:
        report.count(section, "remote_only", len(extra))
    if puts or deletes:
        client.commit_shared_files(namespace, base_generation=generation, puts=puts, deletes=deletes, actor=ACTOR)
    report.count(section, "imported", len(puts))
    report.count(section, "deleted", len(deletes))


def verify_tree(args: argparse.Namespace, client: Any, report: Report, section: str) -> None:
    namespace, local, _ = _tree_sources(args, section)
    _, remote = _tree_remote(client, namespace)
    report.count(section, "local_files", len(local))
    report.count(section, "remote_files", len(remote))
    for rel, path in local.items():
        if rel not in remote:
            report.problem(section, f"missing in Postgres: {rel}")
        elif _sha(remote[rel].encode("utf-8")) != _sha(path.read_bytes()):
            report.problem(section, f"hash differs: {rel}")
        else:
            report.count(section, "matched")
    for rel in sorted(set(remote) - set(local)):
        report.problem(section, f"only in Postgres: {rel}")


def export_tree(args: argparse.Namespace, client: Any, report: Report, section: str) -> None:
    namespace, local, _ = _tree_sources(args, section)
    _, remote = _tree_remote(client, namespace)
    if section == "cron":
        if CRON_FILE_NAME in remote:
            target = Path(args.cron_file)
            if not target.is_file() or target.read_text(encoding="utf-8") != remote[CRON_FILE_NAME]:
                _write_atomic(target, remote[CRON_FILE_NAME].encode("utf-8"))
                report.count(section, "written")
            else:
                report.count(section, "unchanged")
        return
    root = Path(args.definitions_dir)
    for rel, content in sorted(remote.items(), key=lambda kv: (Path(kv[0]).name == "manifest.json", kv[0])):
        target = root / rel
        if not target.resolve().is_relative_to(root.resolve()):
            report.problem(section, f"path escapes the volume, skipped: {rel}")
            continue
        data = content.encode("utf-8")
        if target.is_file() and _sha(target.read_bytes()) == _sha(data):
            report.count(section, "unchanged")
            continue
        _write_atomic(target, data)
        report.count(section, "written")
    for rel, path in local.items():
        if rel not in remote:
            path.unlink()
            report.count(section, "removed")


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


def _local_skills(skills_dir: Path, report: Report) -> list[dict[str, Any]]:
    """Active and open-draft skills under the managed directory, with their files."""
    from ts_harness.skills import authoring
    from ts_harness.skills.authoring import SKILL_ENTRYPOINT, is_live_skill_meta
    from ts_harness.skills.catalog import SkillCatalog
    from ts_harness.skills.repository import _file_entries, content_sha

    if not skills_dir.is_dir():
        return []
    catalog = SkillCatalog(skills_dir)
    catalog.load()
    out: list[dict[str, Any]] = []
    for meta in sorted(catalog.index.values(), key=lambda m: m.id):
        entry = Path(meta.source_path)
        try:
            in_drafts = entry.resolve().relative_to(skills_dir.resolve()).parts[0] == "drafts"
        except ValueError:
            continue
        if in_drafts:
            if meta.approval_status not in {"draft", "pending_review"}:
                report.count("skills", "skipped_closed_drafts")
                continue
            status = "draft"
        elif is_live_skill_meta(meta):
            status = "active"
        else:
            report.count("skills", "skipped_unapproved")
            continue
        folder = entry.name == SKILL_ENTRYPOINT
        tree = (
            {rel: p.read_bytes() for rel, p in authoring._iter_tree(entry.parent).items()}
            if folder
            else {SKILL_ENTRYPOINT: entry.read_bytes()}
        )
        files = _file_entries(tree)
        try:
            parsed = authoring.validate_skill_markdown(tree[SKILL_ENTRYPOINT].decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            report.problem("skills", f"invalid SKILL.md for {meta.id}: {exc}")
            continue
        provenance = meta.provenance or {}
        out.append(
            {
                "skill_id": meta.id,
                "status": status,
                "version": str(meta.version),
                "category": str(parsed["category"]),
                "leaf": meta.id.split("/")[-1],
                "layout": "folder" if folder else "file",
                "tenant_id": str(provenance.get("tenant_id") or "default"),
                "revision_of": provenance.get("revision_of"),
                "source": str(provenance.get("source") or "import"),
                "created_by": str(provenance.get("created_by") or ACTOR),
                "tree": tree,
                "files": files,
                "content_sha": content_sha(files),
            }
        )
    return out


def _index_rows(client: Any) -> dict[tuple[str, str], dict[str, Any]]:
    listing = client.list_skill_index(statuses=("active", "draft"))
    return {(row["skill_id"], row["status"]): row for row in listing.get("skills") or []}


def _put_objects(objects: Any, prefix: str, skill: dict[str, Any]) -> None:
    for item in skill["files"]:
        key = prefix + item["path"]
        existing = objects.head(key)
        if existing is None or existing.sha256 != item["sha256"]:
            objects.put(key, skill["tree"][item["path"]])


def import_skills(args: argparse.Namespace, client: Any, objects: Any, report: Report) -> None:
    """Active skills first (a draft revision needs its live skill), then drafts against a fresh listing."""
    local = _local_skills(Path(args.skills_dir), report)
    for status in ("active", "draft"):
        rows = _index_rows(client)
        for skill in (s for s in local if s["status"] == status):
            _import_skill(skill, rows, client, objects, report)


def _import_skill(
    skill: dict[str, Any], rows: dict[tuple[str, str], dict[str, Any]], client: Any, objects: Any, report: Report
) -> None:
    from ts_harness.memory.client import MemoryApiConflict
    from ts_harness.skills import authoring

    sid, status, digest = skill["skill_id"], skill["status"], skill["content_sha"]
    row = rows.get((sid, status))
    if row is not None and row.get("content_sha") == digest:
        report.count("skills", f"unchanged_{status}")
        return
    if row is not None and status == "active":
        report.problem("skills", f"active {sid} already indexed with different content; publish a revision instead")
        return
    if status == "active":
        prefix = f"skills/{sid}/{skill['version']}-{digest[:12]}/"
    else:
        prefix = f"skills/drafts/{authoring._safe_id(sid)}-{digest[:12]}/"
    body = {
        key: skill[key]
        for key in ("skill_id", "version", "category", "leaf", "layout", "tenant_id", "files", "content_sha", "source", "created_by")
    }
    try:
        _put_objects(objects, prefix, skill)
        out = client.upsert_skill_draft(
            **body, object_prefix=prefix, revision_of=skill["revision_of"] if status == "draft" else None
        )
        if status == "active":
            client.approve_skill_draft(
                out["skill"]["id"],
                files=skill["files"],
                object_prefix=prefix,
                content_sha=digest,
                reviewer=ACTOR,
                layout=skill["layout"],
            )
    except MemoryApiConflict as exc:
        report.problem("skills", f"{status} {sid}: {exc.code or exc}")
        return
    previous = out.get("previous_prefix")
    if previous and previous != prefix:
        objects.delete_prefix(previous)
    report.count("skills", f"imported_{status}")


def verify_skills(args: argparse.Namespace, client: Any, objects: Any, report: Report) -> None:
    rows = _index_rows(client)
    local = _local_skills(Path(args.skills_dir), report)
    seen: set[tuple[str, str]] = set()
    report.count("skills", "local_skills", len(local))
    report.count("skills", "indexed_skills", len(rows))
    for skill in local:
        key = (skill["skill_id"], skill["status"])
        seen.add(key)
        row = rows.get(key)
        if row is None:
            report.problem("skills", f"{skill['status']} {skill['skill_id']} not in the index")
            continue
        if row.get("content_sha") != skill["content_sha"]:
            report.problem("skills", f"{skill['status']} {skill['skill_id']} content differs from the index")
            continue
        bad = [
            item["path"]
            for item in skill["files"]
            if (info := objects.head(row["object_prefix"] + item["path"])) is None or info.sha256 != item["sha256"]
        ]
        if bad:
            report.problem("skills", f"{skill['status']} {skill['skill_id']} objects missing or different: {', '.join(bad)}")
        else:
            report.count("skills", "matched")
    for sid, status in sorted(set(rows) - seen):
        report.problem("skills", f"{status} {sid} is in the index but not on disk")


def export_skills(args: argparse.Namespace, client: Any, objects: Any, report: Report) -> None:
    """Write active and draft skills into the managed volume; drafts/ is made to match the index exactly."""
    from ts_harness.skills import authoring
    from ts_harness.skills.repository import IndexedSkillRepository

    root = Path(args.skills_dir)
    rows = list(_index_rows(client).values())
    wanted: dict[str, tuple[str, str]] = {}
    bases: set[str] = set()
    for row in rows:
        if row["status"] == "draft":
            bases.add(f"drafts/{authoring._safe_id(row['skill_id'])}")
        else:
            bases.add(f"{row['category']}/{row['leaf']}")
        for rel, sha, key in IndexedSkillRepository._row_files(row):
            wanted[rel] = (sha, key)
    for rel, (sha, key) in sorted(wanted.items()):
        target = root / rel
        if target.is_file() and _sha(target.read_bytes()) == sha:
            report.count("skills", "unchanged_files")
            continue
        _write_atomic(target, objects.get(key, expected_sha256=sha))
        report.count("skills", "written_files")
    for rel, path in _tree_local(root).items():
        if rel in wanted:
            continue
        in_skill = any(rel == f"{b}.md" or rel.startswith(f"{b}/") for b in bases)
        if rel.startswith("drafts/") or in_skill:
            path.unlink()
            report.count("skills", "removed_files")


# ---------------------------------------------------------------------------
# Key-value state
# ---------------------------------------------------------------------------


def _kv_path(args: argparse.Namespace, namespace: str) -> Path:
    return Path(getattr(args, KV_FILES[namespace]))


def import_kv(args: argparse.Namespace, client: Any, report: Report) -> None:
    from ts_harness.memory.client import MemoryApiConflict

    for namespace in KV_FILES:
        path = _kv_path(args, namespace)
        if not path.is_file():
            report.count("kv", "absent")
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        current = client.kv_get(namespace, KV_KEY)
        version = int(current.get("version") or 0)
        if version and current.get("value") == value:
            report.count("kv", "unchanged")
            continue
        if version and not args.force:
            report.problem("kv", f"{namespace}/{KV_KEY} already set with a different value (use --force to overwrite)")
            continue
        try:
            client.kv_put(namespace, KV_KEY, value, expected_version=version)
        except MemoryApiConflict as exc:
            report.problem("kv", f"{namespace}/{KV_KEY} changed during import: {exc.code or exc}")
            continue
        report.count("kv", "imported")


def _live_kv_covers_snapshot(namespace: str, local: Any, remote: Any) -> bool:
    """The scheduler keeps writing Postgres after the import and leaves the host file behind.

    That file is covered when every job on disk is still there with the same or an older
    ``last_run``. A newer ``last_run`` on disk is a real miss and stays a mismatch.
    """
    if namespace != "scheduler" or not isinstance(local, dict) or not isinstance(remote, dict):
        return False
    for name, entry in local.items():
        other = remote.get(name)
        if not isinstance(entry, dict) or not isinstance(other, dict):
            return False
        if set(entry) != {"last_run"} or set(other) != {"last_run"}:
            return False
        try:
            if float(entry["last_run"]) > float(other["last_run"]):
                return False
        except (KeyError, TypeError, ValueError):
            return False
    return True


def verify_kv(args: argparse.Namespace, client: Any, report: Report) -> None:
    for namespace in KV_FILES:
        path = _kv_path(args, namespace)
        if not path.is_file():
            continue
        value = json.loads(path.read_text(encoding="utf-8"))
        remote = client.kv_get(namespace, KV_KEY).get("value")
        if remote == value:
            report.count("kv", "matched")
        elif _live_kv_covers_snapshot(namespace, value, remote):
            report.count("kv", "advanced")
        else:
            report.problem("kv", f"{namespace}/{KV_KEY} differs from {path}")


def export_kv(args: argparse.Namespace, client: Any, report: Report) -> None:
    for namespace in KV_FILES:
        value = client.kv_get(namespace, KV_KEY).get("value")
        if value is None:
            continue
        _write_atomic(_kv_path(args, namespace), json.dumps(value, sort_keys=True).encode("utf-8"))
        report.count("kv", "written")


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def _local_audit(path: Path, report: Report) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            report.problem("audit", f"unreadable line {n} in {path}")
    return events


def import_audit(args: argparse.Namespace, client: Any, report: Report) -> None:
    events = _local_audit(Path(args.audit_log), report)
    for start in range(0, len(events), AUDIT_BATCH):
        out = client.append_audit_events(events[start : start + AUDIT_BATCH])
        report.count("audit", "inserted", int(out.get("inserted") or 0))
    report.count("audit", "read", len(events))


def _remote_event_ids(client: Any, events: list[dict[str, Any]]) -> set[str]:
    stamps = sorted(str(e.get("timestamp") or "") for e in events if e.get("timestamp"))
    if not stamps:
        return set()
    rows = client.list_audit_events(since=stamps[0], until=stamps[-1], limit=AUDIT_PAGE)
    if len(rows) < AUDIT_PAGE:
        return {str(r["event_id"]) for r in rows}
    ids: set[str] = set()
    for day in sorted({s[:10] for s in stamps}):
        ids.update(
            str(r["event_id"])
            for r in client.list_audit_events(since=f"{day}T00:00:00Z", until=f"{day}T23:59:59Z", limit=AUDIT_PAGE)
        )
    return ids


def verify_audit(args: argparse.Namespace, client: Any, report: Report) -> None:
    events = _local_audit(Path(args.audit_log), report)
    remote = _remote_event_ids(client, events)
    missing = [str(e.get("event_id")) for e in events if str(e.get("event_id")) not in remote]
    report.count("audit", "local_events", len(events))
    report.count("audit", "matched", len(events) - len(missing))
    if missing:
        report.problem("audit", f"{len(missing)} events not in Postgres, e.g. {', '.join(missing[:3])}")


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------


def _workspace_local(root: Path) -> dict[str, Path]:
    if not root.is_dir():
        return {}
    out: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root)
        if WORKSPACE_SKIP_PARTS.intersection(rel.parts):
            continue
        out[rel.as_posix()] = path
    return out


def import_workspace(args: argparse.Namespace, objects: Any, report: Report) -> None:
    from ts_harness.storage.workspace import MAX_OBJECT_BYTES

    for rel, path in _workspace_local(Path(args.workspace_dir)).items():
        data = path.read_bytes()
        if len(data) > MAX_OBJECT_BYTES:
            report.count("workspace", "skipped_too_large")
            continue
        key = f"workspaces/{rel}"
        try:
            info = objects.head(key)
        except ValueError:
            report.count("workspace", "skipped_bad_name")
            continue
        if info is not None and info.sha256 == _sha(data):
            report.count("workspace", "unchanged")
            continue
        if info is not None and not args.force:
            report.problem("workspace", f"object exists with different content (use --force to overwrite): {key}")
            continue
        objects.put(key, data)
        report.count("workspace", "uploaded")


def verify_workspace(args: argparse.Namespace, objects: Any, report: Report) -> None:
    from ts_harness.storage.workspace import MAX_OBJECT_BYTES

    for rel, path in _workspace_local(Path(args.workspace_dir)).items():
        data = path.read_bytes()
        if len(data) > MAX_OBJECT_BYTES:
            report.count("workspace", "skipped_too_large")
            continue
        try:
            info = objects.head(f"workspaces/{rel}")
        except ValueError:
            report.count("workspace", "skipped_bad_name")
            continue
        if info is None:
            report.problem("workspace", f"missing in the bucket: {rel}")
        elif info.sha256 != _sha(data):
            report.problem("workspace", f"hash differs: {rel}")
        else:
            report.count("workspace", "matched")


def export_workspace(args: argparse.Namespace, objects: Any, report: Report) -> None:
    """Download every workspace object that is missing or different on disk; never deletes local files."""
    root = Path(args.workspace_dir)
    for info in objects.list("workspaces/"):
        rel = info.key[len("workspaces/") :]
        target = root / rel
        if not target.resolve().is_relative_to(root.resolve()):
            report.problem("workspace", f"key escapes the volume, skipped: {info.key}")
            continue
        if target.is_file() and _sha(target.read_bytes()) == info.sha256:
            report.count("workspace", "unchanged")
            continue
        _write_atomic(target, objects.get(info.key))
        report.count("workspace", "written")


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def make_object_store(args: argparse.Namespace, kind: str) -> Any:
    from ts_harness.storage.objects import GcsObjectStore, S3ObjectStore

    bucket = args.skills_bucket if kind == "skills" else args.workspace_bucket
    if not bucket:
        flag = "--skills-bucket" if kind == "skills" else "--workspace-bucket"
        raise ValueError(f"{flag} is required with --object-backend {args.object_backend}")
    return S3ObjectStore(bucket) if args.object_backend == "s3" else GcsObjectStore(bucket)


def run(args: argparse.Namespace, *, client: Any = None, stores: dict[str, Any] | None = None) -> Report:
    from ts_harness.memory.client import MemoryClient

    client = client or MemoryClient(args.memory_api)
    stores = dict(stores or {})
    report = Report()
    sections = [s for s in SECTIONS if s in args.only]

    def store(kind: str) -> Any:
        if kind not in stores:
            stores[kind] = make_object_store(args, kind)
        return stores[kind]

    mode = "import" if args.do_import else "verify" if args.verify else "export"
    actions: dict[str, Callable[[], None]] = {
        "definitions": lambda: {"import": import_tree, "verify": verify_tree, "export": export_tree}[mode](
            args, client, report, "definitions"
        ),
        "cron": lambda: {"import": import_tree, "verify": verify_tree, "export": export_tree}[mode](args, client, report, "cron"),
        "skills": lambda: {"import": import_skills, "verify": verify_skills, "export": export_skills}[mode](
            args, client, store("skills"), report
        ),
        "kv": lambda: {"import": import_kv, "verify": verify_kv, "export": export_kv}[mode](args, client, report),
        "audit": lambda: {"import": import_audit, "verify": verify_audit}.get(mode, lambda *_: None)(args, client, report),
        "workspace": lambda: {"import": import_workspace, "verify": verify_workspace, "export": export_workspace}[mode](
            args, store("workspaces"), report
        ),
    }
    for section in sections:
        report.section(section)
        if section in {"skills", "workspace"} and args.object_backend == "local":
            report.count(section, "skipped_object_backend_local")
            continue
        try:
            actions[section]()
        except Exception as exc:  # noqa: BLE001
            report.problem(section, f"{type(exc).__name__}: {str(exc)[:300]}")
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    env = os.environ.get
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--import", dest="do_import", action="store_true", help="volumes to Postgres and the buckets")
    mode.add_argument("--verify", action="store_true", help="compare hashes and counts; exit 1 on a mismatch")
    mode.add_argument("--export", action="store_true", help="Postgres and the buckets back to the volumes")
    parser.add_argument("--only", default=",".join(SECTIONS), help=f"comma-separated subset of {', '.join(SECTIONS)}")
    parser.add_argument("--force", action="store_true", help="import: overwrite remote data that differs")
    parser.add_argument("--memory-api", default=env("MEMORY_API_URL") or "http://127.0.0.1:18792")
    parser.add_argument("--object-backend", choices=("local", "s3", "gcs"),
                        default=env("TS_HARNESS_OBJECT_BACKEND") or "local",
                        help="local skips skills and workspace: they stay on the host")
    parser.add_argument("--skills-bucket", default=env("TS_HARNESS_SKILLS_BUCKET") or "")
    parser.add_argument("--workspace-bucket", default=env("TS_HARNESS_WORKSPACE_BUCKET") or "")
    parser.add_argument("--definitions-dir", default=env("TS_HARNESS_WORKFLOW_DEFINITION_STORE") or "/workflow-definitions")
    parser.add_argument("--cron-file", default=env("TS_HARNESS_CRON_CONFIG") or "/config/runtime-cron-jobs.json")
    parser.add_argument("--skills-dir", default=env("SKILLS_MANAGED_DIR") or "/skills-managed")
    parser.add_argument("--scheduler-state", default=env("TS_HARNESS_SCHEDULER_STATE") or "/state/scheduler.json")
    parser.add_argument("--delegation-state", default=env("TS_HARNESS_DELEGATION_STATE") or "/state/delegation.json")
    parser.add_argument("--audit-log", default=env("TS_HARNESS_AUDIT_LOG") or "/state/audit.jsonl")
    parser.add_argument("--workspace-dir", default=env("TS_HARNESS_WORKSPACE_ROOT") or "/workspace")
    args = parser.parse_args(argv)
    args.only = [s.strip() for s in args.only.split(",") if s.strip()]
    unknown = sorted(set(args.only) - set(SECTIONS))
    if unknown:
        parser.error(f"unknown section(s): {', '.join(unknown)}")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run(args)
    print(json.dumps({"ok": report.ok, "sections": report.as_dict()}, indent=2, sort_keys=True))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
