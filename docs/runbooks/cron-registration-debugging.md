# Cron Registration Debugging

Use this when Phase 15 does not complete, cron jobs are missing, or the gateway
health-check crontab is not installed.

## 1. Check Phase 15 setup log

```bash
sudo grep -n "phase:15-automation-cron" /var/log/ts-harness-setup.log
sudo tail -n 120 /var/log/ts-harness-setup.log
```

## 2. Check agent-runtime cron jobs

```bash
sudo bash -c '
source /vol/ts-harness-data/.setup/secrets.env
source /vol/ts-harness-data/.setup/compose-env.sh
COMPOSE_FILES=()
while IFS= read -r token; do
  [[ -n "$token" ]] && COMPOSE_FILES+=("$token")
done <<< "$COMPOSE_FILES_SERIALIZED"
sudo -u artemis \
  env TS_HARNESS_DB_PASSWORD="$TS_HARNESS_DB_PASSWORD" \
      TS_HARNESS_DB_HOST="$TS_HARNESS_DB_HOST" \
      TS_HARNESS_DB_PORT="$TS_HARNESS_DB_PORT" \
      TS_HARNESS_DB_USER="$TS_HARNESS_DB_USER" \
      TS_HARNESS_DB_NAME="$TS_HARNESS_DB_NAME" \
  docker compose "${COMPOSE_FILES[@]}" run --rm -T cat /home/artemis/enterprise-assets/config/runtime-cron-jobs.json
'
```

## 3. Check host health cron

```bash
sudo crontab -l | grep gateway-health-check
sudo tail -n 100 /var/log/ts-harness-health.log
```

The health crontab should include an explicit PATH so Ubuntu cron can find the
AWS CLI installed by user-data:

```text
PATH=/usr/local/bin:/usr/bin:/bin
```

If CloudWatch metrics are missing, check the health log for `CloudWatch metric
publish failed`. The health script keeps the probe non-fatal, but logs AWS CLI
lookup, IAM, and CloudWatch publish failures there.

## 4. Re-run Phase 15

```bash
sudo rm -f /vol/ts-harness-data/.setup/phase-15-automation-cron.done
sudo bash -c '
export DATA_DIR=/vol/ts-harness-data
export ADMIN_USER=artemis
export AGENT_RUNTIME_VERSION=$(grep ^agent_runtime_version= /vol/ts-harness-data/.setup-complete | cut -d= -f2)
export AWS_REGION=us-east-1
export SECRETS_PREFIX=ts-harness
export ENTERPRISE_ASSETS_DIR=/home/artemis/enterprise-assets
export ENTERPRISE_DOCKER_DIR=/home/artemis/enterprise-assets/docker
export SCRIPT_DIR=/home/artemis/enterprise-assets/scripts
export TS_HARNESS_CONFIG_DIR=/vol/ts-harness-data/.ts-harness
export MEMORY_BACKEND=rds
export AGENT_TIMEZONE=America/Toronto
export GATEWAY_PORT=18789
bash /home/artemis/enterprise-assets/scripts/phases/15-automation-cron.sh
'
```

## 5. Failure signatures

| Symptom | Likely Cause |
| --- | --- |
| `agent-runtime` not healthy | Check `docker logs ts_harness-agent-runtime-1` and `/healthz` |
| Full JSON printed for each cron add | Instance has old noisy cron registration code |
| Stops after `All cron jobs registered` | Health cron registration failed under `set -euo pipefail` |
| `Failed to register cron job` | Check stderr emitted after the warning in current Phase 15 |
| `CloudWatch metric publish failed: aws CLI not found` | Cron PATH or AWS CLI installation is broken; confirm `/usr/local/bin/aws` exists |
