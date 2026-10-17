# Service objectives

These are the operating objectives for a single-node install. Each one is tied to a check that already exists.

## Gateway availability

The gateway stays up. AWS alarms on `GatewayHealthy` in `infra/aws/monitoring.tf`. GCP alarms on the same signal in `infra/gcp/monitoring.tf` (`custom.googleapis.com/<namespace>/GatewayHealthy`).

## Runner liveness

The workflow runner keeps writing its tick file. AWS and GCP alarm on `WorkflowRunnerHealthy`. A task must not stay `running` past its lease. When the lease expires, `WORKFLOW_RUNNER_STALE_POLICY=block` stops it from being dispatched again until an operator looks.

## Recoverability

Restore from an RDS snapshot using `docs/runbooks/db-restore.md` before a pilot and after any change to the restore path. The drill is the check that backups can actually be used.

## Assumptions

- The database is Multi-AZ (RDS) or REGIONAL (Cloud SQL), so a zone loss fails the database over instead of taking it down.
- The app VM is still a single instance. AWS recovers it with the EC2 auto-recover alarm. GCE uses live migration and automatic restart.
- `X-Trace-Id` is the key to search gateway, runner, and memory API logs for one request. The runner mints a new id for each claimed task and sends it to the gateway as `traceparent`.
