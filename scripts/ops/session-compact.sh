#!/usr/bin/env bash
# session-compact.sh – Compact the sessions JSONL file.
#
# Reads SESSION_STORE (default /state/sessions.jsonl), deduplicates records
# by session_id keeping the *last* occurrence of each, then atomically
# replaces the original file.
#
# Usage:
#   SESSION_STORE=/state/sessions.jsonl bash scripts/ops/session-compact.sh
#
# Options (environment variables):
#   SESSION_STORE   Path to the sessions JSONL file  (default: /state/sessions.jsonl)
#   DRY_RUN         Set to "true" to print stats only without modifying the file
#   BACKUP          Set to "true" to keep a .bak copy before compaction (default: true)
set -euo pipefail

SESSION_STORE="${SESSION_STORE:-/state/sessions.jsonl}"
DRY_RUN="${DRY_RUN:-false}"
BACKUP="${BACKUP:-true}"

if [[ ! -f "${SESSION_STORE}" ]]; then
  echo "session-compact: nothing to do – ${SESSION_STORE} not found"
  exit 0
fi

ORIGINAL_LINES=$(wc -l < "${SESSION_STORE}")
TMP_FILE="${SESSION_STORE}.compact.$$"

# Use Python for reliable JSON deduplication (keeps last occurrence per session_id).
python3 - "${SESSION_STORE}" "${TMP_FILE}" <<'PYEOF'
import json
import sys

src, dst = sys.argv[1], sys.argv[2]
seen: dict[str, str] = {}

with open(src, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            key = rec.get("session_id", "")
            if key:
                seen[key] = line
            else:
                # Preserve non-session-id lines as-is (shouldn't exist, but be safe)
                seen[f"__raw_{len(seen)}__"] = line
        except json.JSONDecodeError:
            pass  # skip corrupt lines

with open(dst, "w", encoding="utf-8") as f:
    for line in seen.values():
        f.write(line + "\n")

print(f"session-compact: {len(seen)} unique sessions written", flush=True)
PYEOF

COMPACT_LINES=$(wc -l < "${TMP_FILE}")
REMOVED=$((ORIGINAL_LINES - COMPACT_LINES))

echo "session-compact: ${ORIGINAL_LINES} lines → ${COMPACT_LINES} lines (removed ${REMOVED} duplicates)"

if [[ "${DRY_RUN}" == "true" ]]; then
  rm -f "${TMP_FILE}"
  echo "session-compact: dry-run – no changes written"
  exit 0
fi

if [[ "${BACKUP}" == "true" ]]; then
  cp "${SESSION_STORE}" "${SESSION_STORE}.bak"
  echo "session-compact: backup written to ${SESSION_STORE}.bak"
fi

# Atomic replace
mv "${TMP_FILE}" "${SESSION_STORE}"
echo "session-compact: done"
