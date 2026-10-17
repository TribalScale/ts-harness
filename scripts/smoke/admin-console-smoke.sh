#!/usr/bin/env bash
# Smoke the admin console BFF surfaces against a running gateway.
set -euo pipefail

GATEWAY_URL="${GATEWAY_URL:-http://127.0.0.1:18789}"
TOKEN="${GATEWAY_TOKEN:-${TOKEN:-}}"
ORIGIN="${ADMIN_UI_ORIGIN:-http://127.0.0.1:8080}"

if [[ -z "${TOKEN}" ]]; then
  echo "GATEWAY_TOKEN (or TOKEN) is required" >&2
  exit 1
fi

auth=(-H "Authorization: Bearer ${TOKEN}" -H "Origin: ${ORIGIN}" -H "Content-Type: application/json")

echo "== healthz =="
curl -fsS "${GATEWAY_URL}/healthz" | head -c 200
echo

echo "== auth config =="
curl -fsS "${auth[@]}" "${GATEWAY_URL}/admin/auth/config"
echo

echo "== inbox =="
curl -fsS "${auth[@]}" "${GATEWAY_URL}/admin/inbox"
echo

echo "== programs =="
curl -fsS "${auth[@]}" "${GATEWAY_URL}/admin/programs?limit=5"
echo

echo "== program definitions =="
curl -fsS "${auth[@]}" "${GATEWAY_URL}/admin/program-definitions"
echo

echo "== program definition catalog =="
curl -fsS "${auth[@]}" "${GATEWAY_URL}/admin/program-definitions/catalog"
echo

echo "== program definition (software_delivery) =="
curl -fsS "${auth[@]}" "${GATEWAY_URL}/admin/program-definitions/software_delivery"
echo

echo "== workflow definitions =="
curl -fsS "${auth[@]}" "${GATEWAY_URL}/admin/workflow-definitions"
echo

echo "== cron jobs =="
curl -fsS "${auth[@]}" "${GATEWAY_URL}/admin/cron/jobs"
echo

echo "== sessions =="
curl -fsS "${auth[@]}" "${GATEWAY_URL}/admin/sessions?channel=web"
echo

echo "== web chat =="
binding="web:smoke-$(date +%s)"
chat_response="$(
  curl -fsS "${auth[@]}" \
    -X POST \
    -d "{\"channel\":\"web\",\"source\":\"web\",\"text\":\"Reply with admin-console-smoke-ok\",\"thread_id\":\"${binding}\",\"binding_key\":\"${binding}\"}" \
    "${GATEWAY_URL}/session/message"
)"
session_id="$(
  python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id", ""))' \
    <<<"${chat_response}"
)"
if [[ -z "${session_id}" ]]; then
  echo "chat response did not include session_id" >&2
  exit 1
fi
messages="$(
  curl -fsS "${auth[@]}" \
    "${GATEWAY_URL}/admin/sessions/${session_id}/messages"
)"
python3 -c '
import json, sys
payload = json.load(sys.stdin)
messages = payload.get("messages") or []
assert any((item.get("content") or item.get("text")) == "Reply with admin-console-smoke-ok" for item in messages)
' <<<"${messages}"
echo "web chat OK"

echo "admin-console smoke OK"
