# Redeploy an existing host

Use this to roll a new version of `enterprise-assets` onto a running host, or to
apply a change to secrets or the runtime config template.

```bash
cd /home/<admin-user>/enterprise-assets
sudo -u <admin-user> git fetch origin && sudo -u <admin-user> git checkout <branch> && sudo -u <admin-user> git pull --ff-only
sudo ADMIN_USER=<admin-user> bash scripts/ops/redeploy.sh
```

[`scripts/ops/redeploy.sh`](../../scripts/ops/redeploy.sh) runs, in order:

| Step | What it picks up |
|---|---|
| Phase 10 | Secrets Manager values (API keys, coding-backend provider and model) |
| Phase 10a | The runtime-config store (`<prefix>/runtime-config`) |
| Phases 10b, 11, 11a | The workspace, `ts-harness-runtime.yaml` (agent models, effort, `llm_pricing`) and the agent docs |
| Phase 12 | The compose env (`compose-env.sh`), including new keys, and `docker compose up` |
| `migrate-db.sh` | New database migrations |
| `build`, `up -d` | New code in the images |

Each phase skips itself when its inputs are unchanged, so running the script
after every pull is safe. Rebuilding and restarting the containers alone
(`docker compose build && up -d`) does not re-render the runtime config or the
compose env: model and price changes and new secret keys would not reach the
containers.

## Check it

```bash
sudo docker exec "$(sudo docker ps -qf name=agent-runtime)" sh -c 'echo ${#ANTHROPIC_API_KEY}; grep -m3 "model:" /config/ts-harness-runtime.yaml'
```

The key length must not be 0, and the models should match the template.

## Related

- [Re-run one bootstrap phase](re-run-bootstrap-phase.md)
- [Runtime config store](runtime-config.md)
- [LLM proxy and usage recording](llm-proxy.md)
