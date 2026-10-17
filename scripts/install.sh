#!/usr/bin/env bash
# Thin wrapper around the Python installer CLI.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

pick_python() {
  if [[ -x "${ROOT}/.venv311/bin/python" ]]; then
    echo "${ROOT}/.venv311/bin/python"
    return
  fi
  if [[ -x "${ROOT}/.venv/bin/python" ]]; then
    echo "${ROOT}/.venv/bin/python"
    return
  fi
  if command -v python3.11 >/dev/null 2>&1; then
    command -v python3.11
    return
  fi
  command -v python3
}

exec "$(pick_python)" "${ROOT}/scripts/install.py" "$@"
