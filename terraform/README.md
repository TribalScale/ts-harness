# Terraform root moved

The AWS Terraform root now lives at [`infra/aws/`](../infra/aws/).

```bash
cd infra/aws
terraform init
terraform plan
```

See the migration runbook: [`docs/runbooks/aws-terraform-root-migration.md`](../docs/runbooks/aws-terraform-root-migration.md).

Do not maintain a second active AWS root here. This directory is a compatibility
pointer only.
