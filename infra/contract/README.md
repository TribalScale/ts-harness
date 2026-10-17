# Install Terraform Contract

Provider roots (`infra/aws`, `infra/gcp`) implement this contract independently.
This directory documents shared input and output names; it is **not** a
conditional mega-module that creates AWS and GCP resources.

## Rules

1. Exactly one provider root is applied per installation.
2. Each root emits the normalized output names listed in `outputs.tf`.
3. Input names in `variables.tf` are the shared contract vocabulary; roots may
   map provider-native variables onto these names.
4. Resource addresses inside a root are provider-specific and must not be shared
   across AWS and GCP state.

## Usage

Operators and the installer select one root:

```bash
terraform -chdir=infra/aws plan
# or
terraform -chdir=infra/gcp plan
```

Normalized outputs feed shared bootstrap (`CLOUD_PROVIDER`, `CLOUD_REGION`,
`MANAGED_DB_*`, `CREDENTIALS_ENCRYPTION_KEY_ID`, `INSTANCE_RESOURCE_ID`, …).

See also:

- `config/install-contract.schema.json`
- `scripts/install_contract.py`
