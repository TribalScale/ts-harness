#!/bin/bash
# Ordered bootstrap phase graph shared by all cloud providers.
# Marker files remain at: $DATA_DIR/.setup/phase-<name>.done

bootstrap_phase_list() {
    # Prints phase script basenames in execution order.
    # Multi-agent phases are always listed; the runner skips them when disabled.
    cat <<'EOF'
00-init.sh
01-ebs.sh
02-admin-user.sh
03-ufw.sh
04-ssh-harden.sh
05-docker.sh
06-tailscale.sh
07-workspace-dirs.sh
09-clone-enterprise.sh
10-fetch-secrets.sh
10a-hydrate-runtime-config.sh
10b-render-workspace.sh
11-agent-runtime-setup.sh
11a-agents-init.sh
11b-agent-auth-init.sh
11c-mcporter-init.sh
11g-google-workspace-init.sh
11h-coding-tools-install.sh
11d-validate-workspace.sh
11e-preflight-embeddings.sh
11f-check-embedding-dim.sh
12-compose-up.sh
12a-db-credential-watch.sh
13-memory-db-init.sh
14-channels-init.sh
15-automation-cron.sh
16-release-gates.sh
99-finalize.sh
EOF
}

# Phases that require MULTI_AGENT_ENABLED=true.
bootstrap_multi_agent_phases() {
    cat <<'EOF'
11a-agents-init.sh
11b-agent-auth-init.sh
11c-mcporter-init.sh
11g-google-workspace-init.sh
11h-coding-tools-install.sh
11d-validate-workspace.sh
11e-preflight-embeddings.sh
15-automation-cron.sh
EOF
}

bootstrap_is_multi_agent_phase() {
    local name="$1"
    grep -qxF "$name" < <(bootstrap_multi_agent_phases)
}
