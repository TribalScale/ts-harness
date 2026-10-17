# Bootstrap path and Terraform defaults for manual phase re-runs.
# Must be sourced before expanding ${DATA_DIR} under set -u.

: "${DATA_DIR:=/vol/ts-harness-data}"
export DATA_DIR

: "${GATEWAY_PORT:=18789}"
export GATEWAY_PORT

: "${CLOUD_PROVIDER:=aws}"
export CLOUD_PROVIDER

: "${AWS_REGION:=us-east-1}"
: "${CLOUD_REGION:=${AWS_REGION}}"
export CLOUD_REGION
export AWS_REGION

: "${SECRETS_PREFIX:=ts-harness}"
export SECRETS_PREFIX

if [[ -z "${ADMIN_USER:-}" && -f "${DATA_DIR}/.setup-complete" ]]; then
    ADMIN_USER="$(grep '^admin_user=' "${DATA_DIR}/.setup-complete" 2>/dev/null | cut -d= -f2- || true)"
fi
: "${ADMIN_USER:=artemis}"
export ADMIN_USER

: "${ADMIN_HOME:=/home/${ADMIN_USER}}"
export ADMIN_HOME

: "${ENTERPRISE_ASSETS_DIR:=${ADMIN_HOME}/enterprise-assets}"
export ENTERPRISE_ASSETS_DIR

: "${ENTERPRISE_DOCKER_DIR:=${ENTERPRISE_ASSETS_DIR}/docker}"
export ENTERPRISE_DOCKER_DIR

: "${SCRIPT_DIR:=${ENTERPRISE_ASSETS_DIR}/scripts}"
export SCRIPT_DIR

: "${TS_HARNESS_CONFIG_DIR:=${DATA_DIR}/.ts-harness}"
export TS_HARNESS_CONFIG_DIR

: "${TS_HARNESS_WORKSPACE_DIR:=${TS_HARNESS_CONFIG_DIR}/workspace}"
export TS_HARNESS_WORKSPACE_DIR

: "${TS_HARNESS_TOOLS_ROOT_HOST:=${DATA_DIR}/.ts-harness/tools}"
export TS_HARNESS_TOOLS_ROOT_HOST
# Container path for OpenCode (host tools root is bind-mounted here).
: "${TS_HARNESS_TOOLS_ROOT:=/tools}"
export TS_HARNESS_TOOLS_ROOT

: "${IMPLEMENT_WORK_ORDERS_REPO_URL:=}"
export IMPLEMENT_WORK_ORDERS_REPO_URL

: "${IMPLEMENT_WORK_ORDERS_BASE_BRANCH:=main}"
export IMPLEMENT_WORK_ORDERS_BASE_BRANCH

: "${IMPLEMENT_JIRA_TICKET_REPO_URL:=}"
export IMPLEMENT_JIRA_TICKET_REPO_URL

: "${IMPLEMENT_JIRA_TICKET_BASE_BRANCH:=dev}"
export IMPLEMENT_JIRA_TICKET_BASE_BRANCH

: "${OPENCODE_VERSION:=1.18.4}"
export OPENCODE_VERSION

: "${AGENT_RUNTIME_URL:=http://127.0.0.1:${GATEWAY_PORT}}"
export AGENT_RUNTIME_URL

: "${MEMORY_BACKEND:=rds}"
export MEMORY_BACKEND

: "${MANAGED_DB_PORT:=5432}"
export MANAGED_DB_PORT

_INSTANCE_ENV="${DATA_DIR}/.setup/instance-env.sh"
if [[ -f "$_INSTANCE_ENV" ]]; then
    # shellcheck disable=SC1090
    source "$_INSTANCE_ENV"
fi

# Resolve MANAGED_DB_* / CLOUD_* from legacy RDS_* / AWS_REGION when present.
# Phase scripts source env-defaults before common.sh; load the helper if needed.
_ENV_DEFAULTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if declare -F resolve_cloud_neutral_env >/dev/null 2>&1; then
    resolve_cloud_neutral_env
elif [[ -f "${_ENV_DEFAULTS_DIR}/common.sh" ]]; then
    # shellcheck disable=SC1091
    source "${_ENV_DEFAULTS_DIR}/common.sh"
    resolve_cloud_neutral_env
fi
unset _ENV_DEFAULTS_DIR
