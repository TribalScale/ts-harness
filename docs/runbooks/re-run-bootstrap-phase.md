# Re-run a Bootstrap Phase

Use this when one setup phase failed or when testing a phase script change on an
existing instance.

## 1. Confirm current setup state

```bash
sudo cloud-init status --long
sudo tail -n 120 /var/log/ts-harness-setup.log
ls -la /vol/ts-harness-data/.setup/
```

## 2. Pull latest enterprise assets

```bash
cd /home/artemis/enterprise-assets
sudo -u artemis git pull
```

## 3. Remove only the target phase marker

Example for Phase 15:

```bash
sudo rm -f /vol/ts-harness-data/.setup/phase-15-automation-cron.done
```

## 4. Re-run a phase with required environment

Most phase scripts expect the environment normally exported by `user-data.sh`.
Use a wrapper like this and adjust the script name:

```bash
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
export TS_HARNESS_WORKSPACE_DIR=/vol/ts-harness-data/.ts-harness/workspace
export MEMORY_BACKEND=rds
export AGENT_TIMEZONE=America/Toronto
export GATEWAY_PORT=18789
bash /home/artemis/enterprise-assets/scripts/phases/15-automation-cron.sh
' 2>&1 | sudo tee -a /var/log/ts-harness-setup.log
```

## 5. Verify phase marker

```bash
cat /vol/ts-harness-data/.setup/phase-15-automation-cron.done
```
