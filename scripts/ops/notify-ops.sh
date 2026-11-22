#!/usr/bin/env bash
# Post one line to the ops Slack channel through its incoming webhook.
#
#   bash scripts/ops/notify-ops.sh "redeploy done abc1234"
#
# The webhook is SLACK_OPS_WEBHOOK_URL, or the same key in the host secrets env
# (SECRETS_ENV, default $DATA_DIR/.setup/secrets.env; phase 10 writes it from the optional
# secret <prefix>/slack-ops-webhook-url). With no webhook it does nothing. It never fails the
# caller and never prints the webhook URL. See docs/runbooks/ops-alerts.md.
set -uo pipefail

text="${1:-}"
[[ -n "$text" ]] || exit 0

url="${SLACK_OPS_WEBHOOK_URL:-}"
if [[ -z "$url" ]]; then
    secrets="${SECRETS_ENV:-${DATA_DIR:-/vol/ts-harness-data}/.setup/secrets.env}"
    if [[ -r "$secrets" ]]; then
        # shellcheck disable=SC1090
        url="$(set +eu; source "$secrets" >/dev/null 2>&1; printf '%s' "${SLACK_OPS_WEBHOOK_URL:-}")"
    fi
fi
[[ -n "$url" ]] || exit 0

host="$(hostname -s 2>/dev/null || echo host)"
payload="$(python3 -c 'import json, sys; print(json.dumps({"text": sys.argv[1]}))' "[$host] $text" 2>/dev/null)" || exit 0
if ! curl -fsS -m 15 -X POST -H 'Content-Type: application/json' --data "$payload" "$url" >/dev/null 2>&1; then
    echo "notify-ops: could not post to the ops webhook" >&2
fi
exit 0
