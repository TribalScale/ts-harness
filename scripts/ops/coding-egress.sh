#!/usr/bin/env bash
# Coding worker egress allowlist (CODING_EGRESS_ALLOWLIST=on) checks.
#
#   coding-egress.sh check            start a throwaway worker on the egress network and
#                                     prove the policy: GitHub and npm answer, memory-api and
#                                     the runtime answer, example.com and a direct connection fail
#   coding-egress.sh denied [SINCE]   hosts the proxy refused (default: last 24h), most
#                                     frequent first; add the ones a job needs to CODING_EGRESS_ALLOW
#   coding-egress.sh allowlist        the domains the proxy allows right now
#
# Run on the host as a user that can use docker. Prints no secrets.
set -euo pipefail

NETWORK="${CODING_EGRESS_NETWORK:-ts-harness-coding-egress}"
IMAGE="${CODING_WORKER_IMAGE:-ts-harness-coding-worker:latest}"
PROXY_URL="${CODING_EGRESS_PROXY_WORKER_URL:-http://egress-proxy:3128}"
INTERNAL_HOST="${CODING_EGRESS_INTERNAL_HOST:-agent-runtime}"
PROXY_LABEL="ts-harness.egress-proxy=true"

usage() {
    sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
}

proxy_container() {
    local id
    id="$(docker ps -q --filter "label=${PROXY_LABEL}" | head -n 1)"
    if [[ -z "$id" ]]; then
        echo "ERROR: no running egress-proxy (label ${PROXY_LABEL}). Is CODING_EGRESS_ALLOWLIST=on and phase 12 re-run?" >&2
        exit 1
    fi
    printf '%s\n' "$id"
}

cmd_check() {
    proxy_container >/dev/null
    if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
        echo "ERROR: docker network $NETWORK not found" >&2
        exit 1
    fi
    local no_proxy="localhost,127.0.0.1,::1,0.0.0.0,${INTERNAL_HOST}"
    docker run --rm --network "$NETWORK" \
        -e HTTP_PROXY="$PROXY_URL" -e HTTPS_PROXY="$PROXY_URL" \
        -e http_proxy="$PROXY_URL" -e https_proxy="$PROXY_URL" \
        -e NO_PROXY="$no_proxy" -e no_proxy="$no_proxy" \
        "$IMAGE" python3 -m ts_harness.coding.egress check \
            --internal "http://${INTERNAL_HOST}:18792/health" \
            --internal "http://${INTERNAL_HOST}:18789/healthz" \
            "$@"
}

cmd_denied() {
    local since="${1:-24h}" id
    id="$(proxy_container)"
    docker logs --since "$since" "$id" 2>/dev/null \
        | awk '$3 ~ /^TCP_DENIED\// { target = $5; sub(/^[a-z]+:\/\//, "", target); sub(/[\/:].*$/, "", target); print target }' \
        | sort | uniq -c | sort -rn
}

cmd_allowlist() {
    docker exec "$(proxy_container)" grep -v '^#' /etc/squid/allowlist/domains.txt
}

case "${1:-}" in
    check) shift; cmd_check "$@" ;;
    denied) shift; cmd_denied "$@" ;;
    allowlist) cmd_allowlist ;;
    -h|--help|help|"") usage ;;
    *) usage >&2; exit 2 ;;
esac
