#!/usr/bin/env bash
# Fail if config/workspace files imply a non-contract embedding width.
# Enterprise default: memories.embedding vector(1536) — see database/migrations/001_init_memory.sql
# Env:
#   ENTERPRISE_MEMORY_EMBEDDING_DIM — default 1536
#   TS_HARNESS_CONFIG_DIR — scanned if directory exists
#   TS_HARNESS_WORKSPACE_DIR — scanned if directory exists
#   ENTERPRISE_EMBEDDING_CHECK_STRICT — if "1", fail when no scan roots exist
set -euo pipefail

EXPECTED="${ENTERPRISE_MEMORY_EMBEDDING_DIM:-1536}"
STRICT="${ENTERPRISE_EMBEDDING_CHECK_STRICT:-0}"

echo "check-memory-embedding-dimension: contract expects vector($EXPECTED) (and matching embedding API output)"

SCAN_PATHS=()
[[ -n "${TS_HARNESS_CONFIG_DIR:-}" && -d "$TS_HARNESS_CONFIG_DIR" ]] && SCAN_PATHS+=("$TS_HARNESS_CONFIG_DIR")
[[ -n "${TS_HARNESS_WORKSPACE_DIR:-}" && -d "$TS_HARNESS_WORKSPACE_DIR" ]] && SCAN_PATHS+=("$TS_HARNESS_WORKSPACE_DIR")

if (( ${#SCAN_PATHS[@]} == 0 )); then
    if [[ "$STRICT" == "1" ]]; then
        echo "ERROR: No TS_HARNESS_CONFIG_DIR or TS_HARNESS_WORKSPACE_DIR to scan (STRICT=1)." >&2
        exit 1
    fi
    echo "check-memory-embedding-dimension: no dirs to scan — skipping (OK)"
    exit 0
fi

export ENTERPRISE_EXPECTED_EMBEDDING_DIM="$EXPECTED"
SCAN_ROOTS_JSON="$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${SCAN_PATHS[@]}")"
export SCAN_ROOTS_JSON

python3 <<'PY'
import json
import os
import re
import sys
from pathlib import Path

expected = int(os.environ.get("ENTERPRISE_EXPECTED_EMBEDDING_DIM", "1536"))
roots = json.loads(os.environ.get("SCAN_ROOTS_JSON", "[]"))

KEY_FRAGMENTS = (
    "outputdimensionality",
    "embeddingdim",
    "embedding_dim",
    "vectordim",
    "vector_dim",
)

yaml_dim_re = re.compile(
    r"(?i)(outputDimensionality|embeddingDimensions?|vectorDim|embedding_dim)\s*[:=]\s*(\d+)"
)


def check_json_obj(obj, path: str) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}"
            kl = k.lower()
            if isinstance(v, int) and any(frag in kl for frag in KEY_FRAGMENTS):
                if v != expected:
                    print(f"ERROR: {p} = {v} (expected {expected})", file=sys.stderr)
                    sys.exit(1)
            check_json_obj(v, p)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            check_json_obj(item, f"{path}[{i}]")


def main() -> None:
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            low = path.suffix.lower()
            if low not in (".json", ".yaml", ".yml"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if low == ".json":
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    continue
                check_json_obj(data, str(path))
            else:
                for m in yaml_dim_re.finditer(text):
                    val = int(m.group(2))
                    if val != expected:
                        print(
                            f"ERROR: {path}: dimension {val} (expected {expected}) near {m.group(0)!r}",
                            file=sys.stderr,
                        )
                        sys.exit(1)


if __name__ == "__main__":
    main()
PY

echo "check-memory-embedding-dimension: OK (scanned ${#SCAN_PATHS[@]} root(s))"
