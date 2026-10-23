#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR_LIB="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/env-defaults.sh"

# shellcheck disable=SC1091
source "${SCRIPT_DIR_LIB}/lib/common.sh"


# TS Harness runtime setup — renders runtime config into TS_HARNESS_CONFIG_DIR.

PHASE_NAME="11-agent-runtime-setup"
PHASE_DONE="$DATA_DIR/.setup/phase-${PHASE_NAME}.done"
SECRETS_ENV="$DATA_DIR/.setup/secrets.env"
START_TS=$(date +%s%3N 2>/dev/null || echo 0)


echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"start\",\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"

require_env DATA_DIR
require_env ENTERPRISE_ASSETS_DIR
require_env TS_HARNESS_CONFIG_DIR
require_env ADMIN_USER

CONFIG_TMPL="${ENTERPRISE_ASSETS_DIR}/config/ts-harness-runtime.yaml.tmpl"
CONFIG_OUT="${TS_HARNESS_CONFIG_DIR}/ts-harness-runtime.yaml"

if [[ ! -f "$SECRETS_ENV" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: $SECRETS_ENV not found — run 10-fetch-secrets.sh first" >&2
    exit 1
fi
# shellcheck disable=SC1090
source "$SECRETS_ENV"

TS_FQDN="$(cat "$DATA_DIR/.setup/tailscale-fqdn" 2>/dev/null || echo "")"
TS_FQDN="${TS_FQDN//[[:space:]]/}"
export CONTROL_UI_ALLOWED_ORIGIN=""
if [[ -n "$TS_FQDN" ]]; then
    export CONTROL_UI_ALLOWED_ORIGIN="https://${TS_FQDN}"
elif [[ "${GATEWAY_BIND:-lan}" != "loopback" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: Tailscale FQDN missing; cannot render gateway.allowed_origins." >&2
    echo "[phase:${PHASE_NAME}] Check phase 06 and $DATA_DIR/.setup/tailscale-fqdn." >&2
    exit 1
fi

# Identity / admin console (OIDC optional; static gateway token remains default)
export IDENTITY_PROVIDER="${IDENTITY_PROVIDER:-static}"
export OIDC_ISSUER="${OIDC_ISSUER:-}"
export OIDC_AUDIENCE="${OIDC_AUDIENCE:-}"
export OIDC_JWKS_URL="${OIDC_JWKS_URL:-}"
export OIDC_CLIENT_ID="${OIDC_CLIENT_ID:-}"
export OIDC_AUTHORIZATION_ENDPOINT="${OIDC_AUTHORIZATION_ENDPOINT:-}"
export OIDC_TOKEN_ENDPOINT="${OIDC_TOKEN_ENDPOINT:-}"
export OIDC_PRESET="${OIDC_PRESET:-}"
export OIDC_TOKEN_TYPE="${OIDC_TOKEN_TYPE:-}"
export OIDC_GROUPS_CLAIM="${OIDC_GROUPS_CLAIM:-}"
export OIDC_ROLES_CLAIM="${OIDC_ROLES_CLAIM:-}"
export OIDC_ALLOWED_DOMAINS="${OIDC_ALLOWED_DOMAINS:-}"
export OIDC_GROUPS_SOURCE="${OIDC_GROUPS_SOURCE:-}"
export IDENTITY_DIRECTORY="${IDENTITY_DIRECTORY:-}"
export DEPLOYMENT_PROFILE="${DEPLOYMENT_PROFILE:-single_host}"
if is_blank_or_placeholder "${GATEWAY_TOKEN:-}"; then
    echo "[phase:${PHASE_NAME}] ERROR: GATEWAY_TOKEN is missing or placeholder after secrets fetch." >&2
    exit 1
fi
export GATEWAY_TOKEN
load_instance_env
hydrate_slack_channel_env
export AGENT_TIMEZONE="${AGENT_TIMEZONE:-UTC}"
export SLACK_OPS_CHANNEL="${SLACK_OPS_CHANNEL:-}"
export GOOGLE_WORKSPACE_DEFAULT_EMAIL="${GOOGLE_WORKSPACE_DEFAULT_EMAIL:-}"
export GOOGLE_COMMS_EMAIL="${GOOGLE_COMMS_EMAIL:-${GOOGLE_WORKSPACE_DEFAULT_EMAIL:-}}"
export GOOGLE_MARKETING_EMAIL="${GOOGLE_MARKETING_EMAIL:-${GOOGLE_WORKSPACE_DEFAULT_EMAIL:-}}"
export GOOGLE_SALES_EMAIL="${GOOGLE_SALES_EMAIL:-${GOOGLE_WORKSPACE_DEFAULT_EMAIL:-}}"
export GOOGLE_PM_EMAIL="${GOOGLE_PM_EMAIL:-${GOOGLE_WORKSPACE_DEFAULT_EMAIL:-}}"
export GOOGLE_PM_PRD_FOLDER_ID="${GOOGLE_PM_PRD_FOLDER_ID:-}"
export GOOGLE_DELIVERY_ARTIFACTS_FOLDER_ID="${GOOGLE_DELIVERY_ARTIFACTS_FOLDER_ID:-}"
export GOOGLE_OAUTH_CALLBACK_URL="${GOOGLE_OAUTH_CALLBACK_URL:-}"
export AGENT_CREDENTIALS_KMS_KEY_ID="${AGENT_CREDENTIALS_KMS_KEY_ID:-}"
export CREDENTIALS_ENCRYPTION_KEY_ID="${CREDENTIALS_ENCRYPTION_KEY_ID:-${AGENT_CREDENTIALS_KMS_KEY_ID:-}}"
export AGENT_CREDENTIALS_KMS_KEY_ID="${AGENT_CREDENTIALS_KMS_KEY_ID:-${CREDENTIALS_ENCRYPTION_KEY_ID:-}}"
export TS_HARNESS_TOOLS_ROOT_HOST="${TS_HARNESS_TOOLS_ROOT_HOST:-${DATA_DIR}/.ts-harness/tools}"
# Rendered runtime YAML must use the in-container mount path.
export TS_HARNESS_TOOLS_ROOT="/tools"
export CODING_BACKEND_PROVIDER="${CODING_BACKEND_PROVIDER:-}"
export CODING_BACKEND_MODEL="${CODING_BACKEND_MODEL:-}"
export IMPLEMENT_WORK_ORDERS_REPO_URL="${IMPLEMENT_WORK_ORDERS_REPO_URL:-}"
export IMPLEMENT_WORK_ORDERS_BASE_BRANCH="${IMPLEMENT_WORK_ORDERS_BASE_BRANCH:-main}"
export IMPLEMENT_JIRA_TICKET_REPO_URL="${IMPLEMENT_JIRA_TICKET_REPO_URL:-}"
export IMPLEMENT_JIRA_TICKET_BASE_BRANCH="${IMPLEMENT_JIRA_TICKET_BASE_BRANCH:-dev}"
export OPENCODE_VERSION="${OPENCODE_VERSION:-1.18.4}"
if is_blank_or_placeholder "${IMPLEMENT_WORK_ORDERS_REPO_URL:-}"; then
    echo "[phase:${PHASE_NAME}] WARNING: IMPLEMENT_WORK_ORDERS_REPO_URL is empty — OpenCode implement will fail until set in terraform.tfvars / instance-env." >&2
fi
write_instance_env

PHASE_FINGERPRINT="$(calculate_phase_fingerprint "$CONFIG_TMPL" "$SECRETS_ENV" "$DATA_DIR/.setup/tailscale-fqdn" "$0" "$(instance_env_path)")"
if phase_marker_matches "$PHASE_DONE" "$PHASE_FINGERPRINT"; then
    echo "[phase:${PHASE_NAME}] Already complete for current inputs. Skipping."
    exit 0
fi

if [[ ! -f "$CONFIG_TMPL" ]]; then
    echo "[phase:${PHASE_NAME}] ERROR: runtime template not found: $CONFIG_TMPL" >&2
    exit 1
fi

mkdir -p "$TS_HARNESS_CONFIG_DIR"
echo "[phase:${PHASE_NAME}] Rendering runtime config -> $CONFIG_OUT"
python3 - "$CONFIG_TMPL" "$CONFIG_OUT" <<'PY'
import os
import re
import sys
from pathlib import Path

src, dst = sys.argv[1], sys.argv[2]
text = Path(src).read_text()


def repl(match: re.Match[str]) -> str:
    key = match.group(1)
    return os.environ.get(key, match.group(0))


rendered = re.sub(r"\{\{([A-Z0-9_]+)\}\}", repl, text)
Path(dst).write_text(rendered)
PY

chown "$ADMIN_USER:$ADMIN_USER" "$CONFIG_OUT"
chmod 600 "$CONFIG_OUT"

write_phase_marker "$PHASE_DONE" "$PHASE_FINGERPRINT"
ELAPSED=$(( $(date +%s%3N 2>/dev/null || echo 0) - START_TS ))
echo "{\"phase\":\"${PHASE_NAME}\",\"event\":\"end\",\"elapsed_ms\":${ELAPSED},\"ts\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"}"
echo "[phase:${PHASE_NAME}] Done."
