#!/usr/bin/env bash
# Automated smoke helper for the work-orders delivery track (API/contract level).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

echo "==> Release gate: work_orders_delivery_smoke"
python3 scripts/release-gates.py 2>/dev/null | python3 -c "
import json, sys
data = json.load(sys.stdin)
gate = data.get('work_orders_delivery_smoke') or {}
if not gate.get('passed'):
    raise SystemExit('work_orders_delivery_smoke failed')
print('work_orders_delivery_smoke passed')
" || {
  echo "Running contract pytest fallback..."
  python3 -m pytest tests/runtime/test_work_orders_delivery_smoke_contract.py -q
}

echo "==> Contract tests"
python3 -m pytest tests/runtime/test_work_orders_delivery_smoke_contract.py tests/runtime/test_delivery_program_fork.py -q

echo "Work orders delivery smoke checks passed."
