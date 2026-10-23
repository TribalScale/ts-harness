#!/usr/bin/env python3
"""Upload a rendered workspace to the workspaces bucket, only where the object is missing.

Agents edit their workspace files (MEMORY.md, HEARTBEAT.md) after install, so an
existing object always wins over the rendered template. Keys are
``workspaces/{path relative to --src}``. Coding-backend checkouts are skipped.

Usage:
  seed-workspace-objects.py --src DIR --backend s3|gcs --bucket NAME
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SKIP_PARTS = {"coding-backend", ".git"}


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _missing_s3(bucket: str, key: str) -> bool:
    result = _run(["aws", "s3api", "head-object", "--bucket", bucket, "--key", key])
    if result.returncode == 0:
        return False
    if "404" in result.stderr or "Not Found" in result.stderr or "NoSuchKey" in result.stderr:
        return True
    raise RuntimeError(f"head-object failed for {key}: {result.stderr.strip()[:300]}")


def _upload_s3(bucket: str, key: str, path: Path) -> None:
    result = _run(["aws", "s3", "cp", "--only-show-errors", str(path), f"s3://{bucket}/{key}"])
    if result.returncode != 0:
        raise RuntimeError(f"upload failed for {key}: {result.stderr.strip()[:300]}")


def _missing_gcs(bucket: str, key: str) -> bool:
    result = _run(["gcloud", "storage", "objects", "describe", f"gs://{bucket}/{key}", "--format=value(name)"])
    if result.returncode == 0:
        return False
    if "404" in result.stderr or "not found" in result.stderr.lower():
        return True
    raise RuntimeError(f"describe failed for {key}: {result.stderr.strip()[:300]}")


def _upload_gcs(bucket: str, key: str, path: Path) -> None:
    result = _run(["gcloud", "storage", "cp", "--no-clobber", str(path), f"gs://{bucket}/{key}"])
    if result.returncode != 0:
        raise RuntimeError(f"upload failed for {key}: {result.stderr.strip()[:300]}")


def seed(src: Path, backend: str, bucket: str) -> dict[str, list[str]]:
    missing, upload = (_missing_s3, _upload_s3) if backend == "s3" else (_missing_gcs, _upload_gcs)
    uploaded: list[str] = []
    kept: list[str] = []
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(src)
        if SKIP_PARTS.intersection(rel.parts):
            continue
        key = f"workspaces/{rel.as_posix()}"
        if missing(bucket, key):
            upload(bucket, key, path)
            uploaded.append(key)
        else:
            kept.append(key)
    return {"uploaded": uploaded, "kept": kept}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--backend", required=True, choices=("s3", "gcs"))
    parser.add_argument("--bucket", required=True)
    args = parser.parse_args(argv)
    if not args.src.is_dir():
        print(f"source workspace not found: {args.src}", file=sys.stderr)
        return 2
    if not args.bucket.strip():
        print("--bucket is empty", file=sys.stderr)
        return 2
    try:
        result = seed(args.src, args.backend, args.bucket.strip())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"uploaded": len(result["uploaded"]), "kept": len(result["kept"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
