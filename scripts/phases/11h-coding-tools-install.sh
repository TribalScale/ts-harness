#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"

PHASE_NAME="11h-coding-tools-install"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
START_TS=$(date +%s%3N 2>/dev/null || echo 0)

OPENCODE_VERSION="${OPENCODE_VERSION:-latest}"
# Install on the host path that compose bind-mounts into the container at /tools.
TOOLS_ROOT="${TS_HARNESS_TOOLS_ROOT_HOST:-${DATA_DIR}/.ts-harness/tools}"
INSTALL_DIR="${TOOLS_ROOT}/opencode/${OPENCODE_VERSION}"
BIN_DIR="${INSTALL_DIR}/bin"
CURRENT_LINK="${TOOLS_ROOT}/opencode/current"
BIN_PATH="${BIN_DIR}/opencode"

# Absolute symlinks break after bind-mount to /tools (target still points at /vol/...
# even when the target path is under TOOLS_ROOT on the host).
materialize_absolute_symlink() {
    local path="$1"
    [[ -L "$path" ]] || return 0
    local target
    target="$(readlink "$path" || true)"
    [[ "$target" == /* ]] || return 0
    local resolved
    resolved="$(readlink -f "$path" || true)"
    if [[ -z "$resolved" || ! -e "$resolved" ]]; then
        echo "[phase:${PHASE_NAME}] ERROR: broken absolute symlink ${path} -> ${target}" >&2
        return 1
    fi
    echo "[phase:${PHASE_NAME}] Materializing absolute symlink ${path} -> ${target}" >&2
    local tmp="${path}.real.$$"
    cp -f "$resolved" "$tmp"
    chmod 755 "$tmp"
    mv -f "$tmp" "$path"
}

ensure_container_safe_layout() {
    mkdir -p "$BIN_DIR" "${TOOLS_ROOT}/opencode"
    # Relative: opencode/current -> <version>  (resolves as /tools/opencode/<version>)
    ln -sfn "${OPENCODE_VERSION}" "$CURRENT_LINK"

    materialize_absolute_symlink "$BIN_PATH" || return 1
    if [[ -L "$BIN_PATH" ]]; then
        local target
        target="$(readlink "$BIN_PATH" || true)"
        if [[ "$target" == /* ]]; then
            echo "[phase:${PHASE_NAME}] ERROR: ${BIN_PATH} still absolute symlink -> ${target}" >&2
            return 1
        fi
    fi
    if [[ ! -x "$BIN_PATH" ]]; then
        return 1
    fi
    if [[ ! -x "${CURRENT_LINK}/bin/opencode" ]]; then
        return 1
    fi
    return 0
}

if [[ -f "$PHASE_DONE" ]]; then
    INSTALLED_VERSION="$(grep '^opencode_version=' "$PHASE_DONE" 2>/dev/null | cut -d= -f2 || true)"
    if [[ -n "$INSTALLED_VERSION" && "$INSTALLED_VERSION" == "$OPENCODE_VERSION" ]]; then
        if ensure_container_safe_layout; then
            echo "[phase:${PHASE_NAME}] Already complete for opencode ${OPENCODE_VERSION}. Skipping."
            exit 0
        fi
        echo "[phase:${PHASE_NAME}] Marker present but tools layout is not container-safe; reinstalling." >&2
    fi
fi

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

mkdir -p "$BIN_DIR"

# Official install script currently hardcodes INSTALL_DIR=$HOME/.opencode/bin and
# ignores OPENCODE_INSTALL_DIR (see anomalyco/opencode#7675). After install we
# still need the binary under TOOLS_ROOT for the coding backend.
find_opencode_binary() {
    local candidate
    for candidate in \
        "$BIN_PATH" \
        "${HOME}/.opencode/bin/opencode" \
        "${HOME}/bin/opencode" \
        "${HOME}/.local/bin/opencode"
    do
        if [[ -n "$candidate" && -x "$candidate" ]]; then
            echo "$candidate"
            return 0
        fi
    done
    if command -v opencode >/dev/null 2>&1; then
        command -v opencode
        return 0
    fi
    return 1
}

place_opencode_at_tools_path() {
    local source_bin="$1"
    if [[ "$source_bin" == "$BIN_PATH" && ! -L "$BIN_PATH" ]]; then
        return 0
    fi
    # Dereference npm/official install symlinks so BIN_PATH is a real file under the mount.
    local resolved
    resolved="$(readlink -f "$source_bin" || true)"
    if [[ -z "$resolved" || ! -e "$resolved" ]]; then
        echo "[phase:${PHASE_NAME}] ERROR: cannot resolve opencode source at ${source_bin}" >&2
        return 1
    fi
    if ! cp -f "$resolved" "$BIN_PATH"; then
        echo "[phase:${PHASE_NAME}] ERROR: failed to copy opencode into tools root at ${BIN_PATH}" >&2
        return 1
    fi
    chmod 755 "$BIN_PATH"
    [[ -x "$BIN_PATH" && ! -L "$BIN_PATH" ]]
}

install_via_official_script() {
    echo "[phase:${PHASE_NAME}] Installing OpenCode via official install script"
    local args=(--no-modify-path)
    if [[ "$OPENCODE_VERSION" != "latest" ]]; then
        args+=(--version "${OPENCODE_VERSION#v}")
    fi
    # Best-effort: env var is documented but currently ignored by upstream install.
    OPENCODE_INSTALL_DIR="$BIN_DIR" curl -fsSL https://opencode.ai/install | bash -s -- "${args[@]}" || true

    local found
    if ! found="$(find_opencode_binary)"; then
        echo "[phase:${PHASE_NAME}] Official install did not produce a usable opencode binary" >&2
        return 1
    fi
    echo "[phase:${PHASE_NAME}] Found opencode at ${found}; placing at ${BIN_PATH}"
    place_opencode_at_tools_path "$found"
}

install_via_npm() {
    if ! command -v npm >/dev/null 2>&1; then
        echo "[phase:${PHASE_NAME}] npm not found — installing Node.js LTS for npm fallback"
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -y -qq
        if curl -fsSL https://deb.nodesource.com/setup_22.x | bash -; then
            apt-get install -y -qq nodejs
        else
            apt-get install -y -qq nodejs npm
        fi
    fi
    if ! command -v npm >/dev/null 2>&1; then
        echo "[phase:${PHASE_NAME}] ERROR: npm unavailable for OpenCode fallback install" >&2
        return 1
    fi
    echo "[phase:${PHASE_NAME}] Installing opencode-ai@${OPENCODE_VERSION} via npm -> ${INSTALL_DIR}"
    npm install --prefix "$INSTALL_DIR" "opencode-ai@${OPENCODE_VERSION}"
    local npm_bin="${INSTALL_DIR}/node_modules/.bin/opencode"
    if [[ ! -x "$npm_bin" ]]; then
        npm_bin="$(find "${INSTALL_DIR}/node_modules" -type f -name opencode 2>/dev/null | head -n 1 || true)"
    fi
    if [[ -z "$npm_bin" || ! -x "$npm_bin" ]]; then
        echo "[phase:${PHASE_NAME}] ERROR: opencode binary not found after npm install" >&2
        return 1
    fi
    place_opencode_at_tools_path "$npm_bin"
}

if ! install_via_official_script; then
    echo "[phase:${PHASE_NAME}] Official install script failed; trying npm fallback" >&2
    if ! install_via_npm; then
        echo "[phase:${PHASE_NAME}] ERROR: failed to install OpenCode on host tools path" >&2
        exit 1
    fi
fi

if ! ensure_container_safe_layout; then
    echo "[phase:${PHASE_NAME}] ERROR: OpenCode tools layout is not usable under bind-mount /tools" >&2
    echo "[phase:${PHASE_NAME}] Expected executable: ${TOOLS_ROOT}/opencode/${OPENCODE_VERSION}/bin/opencode" >&2
    echo "[phase:${PHASE_NAME}] And relative link: ${CURRENT_LINK} -> ${OPENCODE_VERSION}" >&2
    exit 1
fi

echo "[phase:${PHASE_NAME}] OpenCode ready at ${CURRENT_LINK}/bin/opencode (relative current -> ${OPENCODE_VERSION})"
"${CURRENT_LINK}/bin/opencode" --version >/dev/null 2>&1 \
    || echo "[phase:${PHASE_NAME}] WARNING: opencode --version failed; binary linked anyway" >&2

# Mirror backend-agnostic coding skills into tools root for host/tool use.
# Per-implement runs also materialize into the app clone (.opencode/skills for OpenCode).
CODING_SKILLS_SRC="${ENTERPRISE_ASSETS_DIR:-}/skills/coding-backend"
if [[ -z "${ENTERPRISE_ASSETS_DIR:-}" || ! -d "$CODING_SKILLS_SRC" ]]; then
    # Fallback: repo checkout next to this phase script (dev / image layouts).
    CODING_SKILLS_SRC="$(cd "${SCRIPT_DIR_LIB}/../.." && pwd)/skills/coding-backend"
fi
CODING_SKILLS_DEST="${TOOLS_ROOT}/coding-backend-skills"
if [[ -d "$CODING_SKILLS_SRC" ]]; then
    mkdir -p "$CODING_SKILLS_DEST"
    # Copy each skill dir that has SKILL.md; skip README-only trees.
    shopt -s nullglob
    for skill_dir in "${CODING_SKILLS_SRC}"/*/; do
        [[ -f "${skill_dir}/SKILL.md" ]] || continue
        name="$(basename "$skill_dir")"
        rm -rf "${CODING_SKILLS_DEST:?}/${name:?}"
        cp -a "$skill_dir" "${CODING_SKILLS_DEST}/${name}"
        echo "[phase:${PHASE_NAME}] Installed coding-backend skill: ${name} -> ${CODING_SKILLS_DEST}/${name}"
    done
    shopt -u nullglob
    # OpenCode global discovery path (best-effort; project materialize is authoritative).
    if [[ -n "${HOME:-}" ]]; then
        OPENCODE_GLOBAL_SKILLS="${HOME}/.config/opencode/skills"
        mkdir -p "$OPENCODE_GLOBAL_SKILLS"
        for skill_dir in "${CODING_SKILLS_DEST}"/*/; do
            [[ -f "${skill_dir}/SKILL.md" ]] || continue
            name="$(basename "$skill_dir")"
            rm -rf "${OPENCODE_GLOBAL_SKILLS:?}/${name:?}"
            cp -a "$skill_dir" "${OPENCODE_GLOBAL_SKILLS}/${name}"
        done
        echo "[phase:${PHASE_NAME}] Mirrored coding-backend skills to ${OPENCODE_GLOBAL_SKILLS}"
        # Do not install third-party planning plugins on the host OpenCode config.
        # TDD/verification live in skills/coding-backend; planning plugins stall unattended WO runs.
        OPENCODE_HOST_CFG="${HOME}/.config/opencode/opencode.json"
        mkdir -p "$(dirname "$OPENCODE_HOST_CFG")"
        if command -v python3 >/dev/null 2>&1; then
            python3 - "$OPENCODE_HOST_CFG" <<'PY'
import json
import sys
from pathlib import Path

cfg_path = Path(sys.argv[1])
data = {}
if cfg_path.is_file():
    try:
        loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except json.JSONDecodeError:
        data = {}
plugins = data.get("plugin")
if not isinstance(plugins, list):
    plugins = []
else:
    plugins = [p for p in plugins if "superpowers" not in str(p).lower()]
data["plugin"] = plugins
cfg_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(cfg_path)
PY
            echo "[phase:${PHASE_NAME}] Ensured host OpenCode config has no planning plugin at ${OPENCODE_HOST_CFG}"
        else
            echo "[phase:${PHASE_NAME}] WARNING: python3 unavailable; skipped OpenCode host config update" >&2
        fi
    fi
else
    echo "[phase:${PHASE_NAME}] WARNING: coding-backend skills source missing at ${CODING_SKILLS_SRC}" >&2
fi

mkdir -p "$DATA_DIR/.setup"
{
    echo "opencode_version=${OPENCODE_VERSION}"
    echo "tools_root=${TOOLS_ROOT}"
    echo "coding_backend_skills=${CODING_SKILLS_DEST}"
    echo "completed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$PHASE_DONE"

echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":\"$(( $(date +%s%3N 2>/dev/null || echo 0) - START_TS ))\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
