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
| Drain | Pauses the coding launcher and waits for running `ts-coding-*` workers to finish |
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

## Running coding jobs

Coding workers run in agent-runtime's network namespace, so recreating agent-runtime cuts
them off and the job is lost. The script first stops the coding launcher (queued jobs stay
queued), then waits for every `ts-coding-*` container to exit, printing progress every five
minutes. The launcher stays stopped through phase 12 and the build, and the final `up -d`
starts it again. If any step fails, the launcher is started again on exit.

To keep coding paused across a deploy, use **Pause all coding** on the System page (see
"Pausing and resuming" in [governed-delivery-flow.md](governed-delivery-flow.md)) rather than
leaving the launcher container stopped. The pause is stored in Postgres, so the launcher this
script starts again still claims nothing until an admin resumes.

| Variable | Default | Effect |
|---|---|---|
| `CODING_DRAIN_TIMEOUT_MINUTES` | `120` | How long to wait. On timeout nothing is changed, the launcher is started again and the script exits 1. |
| `FORCE_REDEPLOY` | `0` | `1` skips the wait. Running workers lose their network; their work orders must be retried. |

Re-running phase 12 on its own drains the same way.

## Deploy results in Slack

When `SLACK_OPS_WEBHOOK_URL` is set (secret `<prefix>/slack-ops-webhook-url`), the script posts
`redeploy done <sha> (<branch>)` to the ops channel at the end, and
`redeploy FAILED (exit <n>) at <step> ...` when any step fails. Without the webhook it posts
nothing. See [production alerts](ops-alerts.md#deploy-results).

## Check it

```bash
sudo docker exec "$(sudo docker ps -qf name=agent-runtime)" sh -c 'echo ${#ANTHROPIC_API_KEY}; grep -m3 "model:" /config/ts-harness-runtime.yaml'
```

The key length must not be 0, and the models should match the template.

## Related

- [Production alerts](ops-alerts.md)
- [Re-run one bootstrap phase](re-run-bootstrap-phase.md)
- [Runtime config store](runtime-config.md)
- [LLM proxy and usage recording](llm-proxy.md)
