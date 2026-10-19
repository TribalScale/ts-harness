# AWS install

Provision TS Harness on AWS (EC2 + EBS + Secrets Manager + optional RDS)
via the unified installer. The same steps run from a laptop or from CI; see
[Run from CI](#run-from-ci) for the pipeline setup.

## Prerequisites

Tools on the machine that runs the installer:

- Terraform `>= 1.10` (CI pins `1.10.5`). Terraform `1.5`–`1.9` works only if
  you set `terraform_state.lock_table`; `validate` fails with
  `TERRAFORM_TOO_OLD` otherwise.
- Python `3.11` with `pyyaml` and `jsonschema`
  (`python3 -m pip install "pyyaml>=6.0" "jsonschema>=4.20"`, or `pip install -e .`).
- AWS CLI v2, with credentials for the target account and region.

Created once per AWS account, before the first install:

1. **Terraform state bucket** (versioned, encrypted, private):

   ```bash
   BUCKET=example-ts-harness-tfstate
   REGION=us-east-1
   aws s3api create-bucket --bucket "$BUCKET" --region "$REGION"
   # Outside us-east-1 add: --create-bucket-configuration LocationConstraint="$REGION"
   aws s3api put-bucket-versioning --bucket "$BUCKET" \
     --versioning-configuration Status=Enabled
   aws s3api put-bucket-encryption --bucket "$BUCKET" \
     --server-side-encryption-configuration \
     '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
   aws s3api put-public-access-block --bucket "$BUCKET" \
     --public-access-block-configuration \
     BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
   ```

2. **State locking.** Nothing to create with Terraform `>= 1.10`: the installer
   enables S3-native lockfiles. For older Terraform, create a DynamoDB table and
   set `terraform_state.lock_table` to its name:

   ```bash
   aws dynamodb create-table --table-name terraform-locks \
     --attribute-definitions AttributeName=LockID,AttributeType=S \
     --key-schema AttributeName=LockID,KeyType=HASH \
     --billing-mode PAY_PER_REQUEST
   ```

3. **EC2 key pair** named like `vm.ssh_key_name`, in `cloud_region`.
   Terraform references it by name and does not create it:

   ```bash
   aws ec2 create-key-pair --key-name ts-harness-prod --key-type ed25519 \
     --query KeyMaterial --output text > ts-harness-prod.pem
   chmod 600 ts-harness-prod.pem
   # or: aws ec2 import-key-pair --key-name ts-harness-prod \
   #       --public-key-material fileb://~/.ssh/id_ed25519.pub
   ```

## Install config

Start from `config/install/aws.example.yaml`. Fields that must change:

- `aws_account_id`. Terraform refuses to run against any other account
  (`allowed_account_ids` on the AWS providers).
- `environment`, `cloud_region`, `cloud_zone`, `notification_email`,
  `monthly_budget_limit`.
- `vm.ssh_key_name`: the key pair above.
- `vm.ssh_allowed_cidrs`: the example only allows `10.0.0.0/8`; add the
  ranges you will SSH from.
- `terraform_state`: the bucket above, a `key` unique to this environment, and
  `region`. Add `lock_table` only if you created one.
- `managed_db.multi_az: false` on dev accounts (Multi-AZ roughly doubles DB cost).
- `secret_prefix` names the Secrets Manager secrets (`<secret_prefix>/<key>`) and
  the SSM runtime-config (`/<secret_prefix>/runtime-config`); empty means
  `project_name`. Choose it before the first apply. Installs created before
  `secret_prefix` reached Terraform use `project_name`, and `validate` fails with
  `SECRET_PREFIX_CHANGED` rather than let an apply replace their secrets.

**One environment per AWS account.** AWS resource names derive from
`project_name` only; `environment` does not reach Terraform. Two environments
in one account collide unless each sets a different `project_name`.

## Terraform state backend

`infra/aws/backend.tf` is a committed, empty `backend "s3" {}` block. The
installer fills it at `terraform init` from `terraform_state` in the install
config, so the YAML is the only place the state location lives.

Upgrading from a hand-copied `backend.tf`:

- **Same bucket/key as your YAML:** move your old `infra/aws/backend.tf`
  aside before pulling (git refuses to overwrite it), then delete it. The next
  installer run re-initialises against the same state.
- **Local state (you never configured a backend):** migrate it once, then run
  the installer as normal:

  ```bash
  terraform -chdir=infra/aws init -migrate-state \
    -backend-config=bucket=<bucket> -backend-config=key=<key> \
    -backend-config=region=<region> -backend-config=encrypt=true \
    -backend-config=use_lockfile=true
  ```

Running `terraform` directly in `infra/aws/` needs the same `-backend-config`
flags; otherwise `init` prompts for the bucket.

## Install

```bash
export INSTALL_CONFIG=/path/to/aws-install.yaml

./scripts/install.sh validate --cloud aws --config "$INSTALL_CONFIG"
./scripts/install.sh plan     --cloud aws --config "$INSTALL_CONFIG" --plan-file prod.tfplan
./scripts/install.sh apply    --cloud aws --config "$INSTALL_CONFIG" --plan-file prod.tfplan
```

`apply --plan-file` applies exactly the reviewed plan, with no re-plan and no
`-auto-approve`. If the state changed since the plan was made, Terraform
refuses with a stale-plan error; plan again. Without `--plan-file`, `apply`
re-plans and applies immediately.

`plan --detailed-exitcode` exits `0` when nothing changes, `2` when changes are
pending, and `1` on error.

Plan files contain every input variable. Treat them as sensitive and delete
them after applying (`*.tfplan` is gitignored).

Terraform root: `infra/aws/` (do not apply from the legacy `terraform/` pointer).

After apply:

1. Populate Secrets Manager placeholders under `${secret_prefix}/<logical-key>`
   (the installer prints the commands).
2. SSH using the `operator_connection_command` / `ssh_command` output (port **2222**).
3. Confirm data disk mount: `/vol/ts-harness-data`.
4. Copy your `config/organization.yaml` to the host and re-render the workspace;
   bootstrap otherwise uses the example organization. See
   [Organization config](install.md#organization-config).
5. Run `scripts/ops/post-deploy-smoke.sh` (see [`post-deploy-smoke-test.md`](post-deploy-smoke-test.md)).

Upgrading an install created before the TS Harness rename: follow the checks in
[`install.md`](install.md#upgrading-an-existing-install-to-the-ts-harness-names).

## Run from CI

`.github/workflows/deploy-aws.yml` runs on manual dispatch. It plans with
`--plan-file --detailed-exitcode`, posts the plan to the job summary, and
uploads it as a one-day artifact. If there are changes, it waits for approval
on the `<environment>` GitHub Environment and then applies that saved plan.
Runs for the same environment queue behind each other, so an approved plan is
never applied over a newer state.

The workflow covers infrastructure only. Secrets, organization config and the
smoke test remain the manual steps listed under [Install](#install).

One-time setup, per AWS account, in addition to the [prerequisites](#prerequisites):

1. **GitHub OIDC provider** in IAM:

   ```bash
   aws iam create-open-id-connect-provider \
     --url https://token.actions.githubusercontent.com \
     --client-id-list sts.amazonaws.com
   # Older AWS CLI versions also require --thumbprint-list.
   ```

2. **Deploy role** trusted only by this repository's environments
   (`<env>` and `<env>-plan`):

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Principal": {
         "Federated": "arn:aws:iam::<account-id>:oidc-provider/token.actions.githubusercontent.com"
       },
       "Action": "sts:AssumeRoleWithWebIdentity",
       "Condition": {
         "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
         "StringLike": {
           "token.actions.githubusercontent.com:sub": [
             "repo:TribalScale/ts-harness:environment:prod",
             "repo:TribalScale/ts-harness:environment:prod-plan"
           ]
         }
       }
     }]
   }
   ```

   Permissions: `infra/aws/` manages EC2, VPC, EBS, Elastic IPs, RDS,
   ElastiCache, KMS, Secrets Manager, SSM Parameter Store, CloudWatch, SNS,
   Budgets and IAM roles. A workable start is the AWS-managed `PowerUserAccess`
   policy plus an inline policy for the IAM objects Terraform owns:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": [
         "iam:CreateRole", "iam:DeleteRole", "iam:GetRole", "iam:TagRole",
         "iam:UntagRole", "iam:UpdateAssumeRolePolicy", "iam:PassRole",
         "iam:PutRolePolicy", "iam:GetRolePolicy", "iam:DeleteRolePolicy",
         "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
         "iam:ListInstanceProfilesForRole", "iam:CreateInstanceProfile",
         "iam:DeleteInstanceProfile", "iam:GetInstanceProfile",
         "iam:TagInstanceProfile", "iam:AddRoleToInstanceProfile",
         "iam:RemoveRoleFromInstanceProfile"
       ],
       "Resource": [
         "arn:aws:iam::<account-id>:role/<project_name>-*",
         "arn:aws:iam::<account-id>:instance-profile/<project_name>-*"
       ]
     }]
   }
   ```

   Tighten `PowerUserAccess` to the services above once a plan and apply succeed.

3. **GitHub Environments** (Settings → Environments), for each environment
   offered in the workflow's `environment` choice:

   | Environment | Protection | Variables |
   |---|---|---|
   | `<env>-plan` | none (runs on dispatch) | `AWS_DEPLOY_ROLE_ARN`, `AWS_REGION` |
   | `<env>` | required reviewers; deployment branch `main` | `AWS_DEPLOY_ROLE_ARN`, `AWS_REGION` |

4. **Install config in the repo** at `config/install/aws-<env>.yaml`. It holds
   no secrets. To add or rename environments, edit the `options` list in the
   workflow.

The workflow pins Terraform in `TF_VERSION`; provider versions come from the
committed `infra/aws/.terraform.lock.hcl`, so the plan and apply jobs use
identical providers. After changing provider constraints, refresh the lock for
every platform that runs Terraform:

```bash
terraform -chdir=infra/aws providers lock \
  -platform=linux_amd64 -platform=linux_arm64 \
  -platform=darwin_arm64 -platform=darwin_amd64
```

## Database availability

RDS is Multi-AZ by default.

- On an existing install, the next apply converts the database. RDS waits for the maintenance window unless `managed_db.apply_immediately` is true. The conversion ends with a failover that lasts about a minute or two.
- Cost roughly doubles at the database tier.
- Dev accounts set `managed_db.multi_az: false`.
- The app VM stays a single instance. Host failure is covered by the EC2 auto-recover alarm in `infra/aws/monitoring.tf`.

## Upgrade / root migration rehearsal

Existing installs that still reference the old `terraform/` working directory
should rehearse with:

```bash
export AWS_UPGRADE_CONFIRM=1
export EXPECTED_ACCOUNT_ID=123456789012
export EXPECTED_REGION=us-east-1
./scripts/ops/aws-install-upgrade-smoke.sh
```

Use `--dry-run` first. Details: [`aws-terraform-root-migration.md`](aws-terraform-root-migration.md).

## Destroy

```bash
./scripts/install.sh destroy --cloud aws --config "$INSTALL_CONFIG" --confirm <environment>
```

`--confirm` must equal the `environment` field in the install config.

## Runtime config (Slack, OIDC, timezone)

App knobs are not baked into user-data. After changing those tfvars, apply
Terraform (SSM update, no instance replace) and run
`scripts/ops/sync-runtime-config.sh` on the host. See
[`runtime-config.md`](runtime-config.md).

## Related

- Common workflow: [`install.md`](install.md)
- Runtime config: [`runtime-config.md`](runtime-config.md)
- Troubleshooting: [`cloud-troubleshooting.md`](cloud-troubleshooting.md)
- Compatibility matrix: [`../compatibility.md`](../compatibility.md)
