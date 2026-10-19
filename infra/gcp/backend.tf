# Partial backend: bucket and prefix are supplied at init time by
# ./scripts/install.sh from terraform_state in the install config.
#
# Running terraform directly? Pass the same values, e.g.
#   terraform init -backend-config=bucket=<bucket> -backend-config=prefix=<key>
terraform {
  backend "gcs" {}
}
