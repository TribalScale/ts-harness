# Partial backend: bucket, key, region and locking are supplied at init time by
# ./scripts/install.sh from terraform_state in the install config.
#
# Running terraform directly? Pass the same values, e.g.
#   terraform init -backend-config=bucket=<bucket> -backend-config=key=<key> \
#     -backend-config=region=<region> -backend-config=encrypt=true \
#     -backend-config=use_lockfile=true   # or dynamodb_table=<table>
terraform {
  backend "s3" {}
}
