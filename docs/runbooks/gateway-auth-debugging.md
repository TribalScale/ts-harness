# Runtime Auth Debugging

Use this when Slack or the runtime reports missing provider keys, auth refs, or
startup failures.

## 1. Check runtime logs

```bash
sudo docker logs ts_harness-agent-runtime-1 --tail 200 2>&1 \
  | grep -iE 'auth|provider|SecretRefResolutionError|missing|OPENAI_API_KEY|ANTHROPIC_API_KEY' || true
```

## 2. Check persisted runtime env

Do not print raw secrets.

```bash
sudo bash -c '
for key in ANTHROPIC_API_KEY OPENAI_API_KEY SLACK_BOT_TOKEN SLACK_APP_TOKEN; do
  if grep -q "^${key}=" /vol/ts-harness-data/.ts-harness/.env; then
    echo "${key}=present"
  else
    echo "${key}=missing"
  fi
done
'
```

## 3. Check setup secrets env

```bash
sudo bash -c '
source /vol/ts-harness-data/.setup/secrets.env
for key in ANTHROPIC_API_KEY OPENAI_API_KEY; do
  if [[ -n "${!key//[[:space:]]/}" ]]; then
    echo "${key}=present"
  else
    echo "${key}=missing"
  fi
done
'
```

## 4. Re-run provider onboarding

```bash
sudo rm -f /vol/ts-harness-data/.setup/phase-14-channels-init.done
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
bash /home/artemis/enterprise-assets/scripts/phases/14-channels-init.sh
'
```

## 5. Restart runtime with compose env

```bash
sudo bash -c '
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
  docker compose "${COMPOSE_FILES[@]}" restart agent-runtime
'
```

## OIDC tenant claim

`identity.tenant_claim` (default `tenant_id`) is the JWT claim that names the
caller's tenant when `gateway.single_tenant_mode` is false. A request body
`tenant_id` that disagrees with the claim is rejected. With single-tenant mode
on, every caller uses `gateway.default_tenant_id`.

The memory API still takes `tenant_id` in the JSON body and authenticates with
one shared token. It listens on loopback inside the runtime network namespace.
After this binding, gateway calls write the authenticated tenant into that body.
