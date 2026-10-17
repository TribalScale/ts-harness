#!/usr/bin/env bash
# Entrypoint for the agent-runtime container in docker/docker-compose.quickstart.yml.
# Renders the example workspace and the production runtime template, then starts
# the gateway. Cloud-only placeholders (OIDC, Google, Slack) render empty.
set -euo pipefail

cd /app

python3 scripts/render-workspace.py \
    --config config/organization.example.yaml \
    --src workspace \
    --dst /workspace

export IDENTITY_PROVIDER="${IDENTITY_PROVIDER:-static}"
export AGENT_TIMEZONE="${AGENT_TIMEZONE:-UTC}"
export CONTROL_UI_ALLOWED_ORIGIN="${CONTROL_UI_ALLOWED_ORIGIN:-http://localhost:8080}"

python3 - config/ts-harness-runtime.yaml.tmpl /tmp/ts-harness-runtime.yaml <<'PY'
import os
import re
import sys
from pathlib import Path

import yaml

src, dst = sys.argv[1], sys.argv[2]
text = Path(src).read_text()
config = yaml.safe_load(re.sub(r"\{\{([A-Z0-9_]+)\}\}", lambda m: os.environ.get(m.group(1), ""), text))
# Google Workspace needs a service account; leave it off until one is configured.
config.setdefault("connectors", {}).setdefault("google-workspace", {})["enabled"] = False
for tenant in config.get("tenants") or []:
    tenant["enabled_connectors"] = [c for c in tenant.get("enabled_connectors") or [] if c != "google-workspace"]
Path(dst).write_text(yaml.safe_dump(config, sort_keys=False))
PY

# No scheduled jobs: the declared crons announce to Slack channels a laptop doesn't have.
echo '{"jobs":[]}' > /tmp/runtime-cron-jobs.json

export TS_HARNESS_CONFIG=/tmp/ts-harness-runtime.yaml
export TS_HARNESS_CRON_CONFIG=/tmp/runtime-cron-jobs.json
exec ts-harness
