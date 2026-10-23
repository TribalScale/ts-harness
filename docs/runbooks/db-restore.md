# Database Restore Runbook

The `ts-harness` RDS instance has automated daily backups (7-day retention by default). Backups run during the configured `rds_backup_window` (default `03:00-04:00 UTC`). `deletion_protection = true` prevents accidental destruction.

---

## From automated RDS snapshot

### 1. List available snapshots

```bash
aws rds describe-db-snapshots \
  --db-instance-identifier ts-harness \
  --query 'DBSnapshots[*].[DBSnapshotIdentifier,SnapshotCreateTime,Status]' \
  --output table
```

### 2. Restore to a new instance

```bash
aws rds restore-db-instance-from-db-snapshot \
  --db-instance-identifier ts-harness-restored \
  --db-snapshot-identifier <snapshot-id> \
  --db-instance-class db.t3.micro \
  --no-multi-az \
  --region us-east-1
```

Wait for status `available` (usually 5–10 minutes):

```bash
aws rds wait db-instance-available \
  --db-instance-identifier ts-harness-restored
```

### 3. Get the restored endpoint

```bash
aws rds describe-db-instances \
  --db-instance-identifier ts-harness-restored \
  --query 'DBInstances[0].Endpoint.Address' \
  --output text
```

### 4. Verify the restored instance

```bash
PGHOST=<restored-endpoint>
PGUSER=ts_harness
PGDATABASE=ts_harness_memory
PGPASSWORD=<from-secrets-manager>

psql "host=$PGHOST user=$PGUSER dbname=$PGDATABASE" \
  -c "SELECT count(*) FROM memories;"
```

### 5. Promote or cut over

**Option A — Update Terraform state:**
```bash
# In infra/aws/ directory:
terraform import aws_db_instance.harness ts-harness-restored
# Update terraform.tfvars if needed, then:
terraform apply
```

**Option B — Update the instance's RDS endpoint in Secrets Manager** and restart the gateway:
```bash
aws secretsmanager update-secret \
  --secret-id ts-harness/db-host \
  --secret-string "<restored-endpoint>"

sudo bash ~/enterprise-assets/scripts/ops/rotate-secrets.sh
```

### 6. Clean up the old or broken instance

Only after confirming the restored instance is healthy and the gateway is connecting:
```bash
# First remove deletion_protection if it was set on the old instance:
aws rds modify-db-instance \
  --db-instance-identifier ts-harness \
  --no-deletion-protection \
  --apply-immediately

aws rds delete-db-instance \
  --db-instance-identifier ts-harness \
  --final-db-snapshot-identifier ts-harness-pre-restore-$(date +%Y%m%d)
```

---

## From pg_dump (manual backup)

### Create a manual backup on the instance

```bash
pg_dump \
  "host=$TS_HARNESS_DB_HOST user=$TS_HARNESS_DB_USER dbname=$TS_HARNESS_DB_NAME" \
  > /vol/ts-harness-data/backups/memory-$(date +%Y%m%d).sql
```

### Restore from a manual backup

```bash
psql \
  "host=$TS_HARNESS_DB_HOST user=$TS_HARNESS_DB_USER dbname=$TS_HARNESS_DB_NAME" \
  < /vol/ts-harness-data/backups/memory-YYYYMMDD.sql
```

---

## Changing backup retention

Update `infra/aws/terraform.tfvars`:

```hcl
rds_backup_retention_days = 14  # increase to 14 days
rds_backup_window         = "02:00-03:00"
```

Then `terraform apply`.

---

## Non-destructive restore drill smoke

Run on the instance before a real restore to confirm backup artifacts and connectivity:

```bash
bash scripts/ops/restore-drill-smoke.sh
```

Expected: backup directory readable, `pg_isready` succeeds (when Postgres is reachable), and runtime config path is readable.

---

## RDS snapshot restore drill (run once before the pilot)

Proves the automated backups actually restore: the latest automated snapshot is
restored to a temporary instance, checked, and deleted.

```bash
# Preview the exact aws/psql calls first; nothing is created.
RDS_INSTANCE_ID=ts-harness bash scripts/ops/restore-drill-smoke.sh --rds --dry-run

# Real drill.
RDS_INSTANCE_ID=ts-harness bash scripts/ops/restore-drill-smoke.sh --rds
```

What it does:

1. Finds the latest `available` automated snapshot of `RDS_INSTANCE_ID`.
2. Restores it to `<RDS_INSTANCE_ID>-drill-<utc-timestamp>` in the same DB subnet group and
   security groups, not publicly accessible, single-AZ, with its own RDS-managed master
   secret (`--manage-master-user-password`).
3. Waits for the instance to be `available`.
4. Connects with `psql` (`sslmode=require`) and checks that `schema_migrations` records every
   file in `database/migrations`, and that `delivery_programs`, `program_external_refs`,
   `workflow_decisions`, `program_releases`, and `workflow_tasks` are readable, printing
   their row counts.
5. Always deletes the drill instance (`--skip-final-snapshot --delete-automated-backups`) from
   an exit trap, including when a check fails or the script is interrupted. RDS removes the
   drill's managed secret with the instance.

Exit code is non-zero if any check fails, and a warning is printed if the delete call fails
(remove `*-drill-*` instances manually in that case).

Optional overrides: `DRILL_INSTANCE_CLASS` (defaults to the source class; `db.t3.micro` is
enough for the drill), `DRILL_INSTANCE_ID`, `MIGRATIONS_DIR`, `PGSSLMODE`.

**Where to run.** From the EC2 host (or anywhere inside the VPC that the RDS security group
admits), with `aws` CLI v2, `psql`, and `python3` on `PATH`. The instance role usually lacks
restore permissions; use an operator profile or temporary credentials.

**IAM permissions needed:**

| Service | Actions |
|---------|---------|
| RDS | `rds:DescribeDBInstances`, `rds:DescribeDBSnapshots`, `rds:RestoreDBInstanceFromDBSnapshot`, `rds:AddTagsToResource`, `rds:DeleteDBInstance` |
| Secrets Manager | `secretsmanager:CreateSecret`, `secretsmanager:TagResource`, `secretsmanager:GetSecretValue` (for the RDS-managed drill secret) |
| KMS | `kms:DescribeKey`, `kms:Decrypt`, `kms:GenerateDataKey` on the Secrets Manager key, and `kms:CreateGrant` on the RDS storage key when it is a customer-managed key |
| EC2 | `ec2:DescribeSubnets`, `ec2:DescribeSecurityGroups`, `ec2:DescribeVpcs` (used by the restore) |

**Expected runtime and cost.** Restore takes roughly 10–30 minutes depending on database size;
the checks take seconds; deletion finishes in the background in about 5–10 minutes. Cost is
the drill instance's hourly rate for under an hour plus prorated storage — typically well
under US$1 for `db.t3.micro`/`db.t3.small` class instances.

**Pre-pilot checklist item:**

- [ ] `restore-drill-smoke.sh --rds` completed with `RDS restore drill OK` against the pilot
      account; record the date, snapshot id, and row counts in the pilot log.

---

## Validation checklist after restore

- [ ] `SELECT count(*) FROM memories;` returns expected row count
- [ ] `SELECT count(*) FROM workflow_tasks;` returns 0 or more (not an error)
- [ ] `SELECT count(*) FROM session_audit_log;` returns 0 or more
- [ ] Runtime reconnects: `docker compose logs --tail=20 agent-runtime | grep -i connected`
- [ ] Memory recall works: send a test message to the orchestrator that requires memory lookup
