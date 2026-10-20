# Changelog

All notable changes to TS Harness are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) as described in
[`docs/releasing.md`](docs/releasing.md).

## [Unreleased]

First public release.

### Added

- Local quickstart (`scripts/quickstart.sh`): Postgres, agent runtime, memory API,
  sandbox worker, and admin console in Docker with a fake model provider.
- `scripts/quickstart.sh gates` runs the CI release gates against the local stack.
- Secret scanning with gitleaks in CI.
- Governance files: code of conduct, issue and pull request templates,
  CODEOWNERS, Dependabot configuration, and this changelog.
- `deploy-aws.yml` workflow: plans, waits for environment approval, then applies
  the saved plan.
- Installer `plan --plan-file` / `apply --plan-file` and `plan --detailed-exitcode`.
- Optional `terraform_state.lock_table` in the install contract for DynamoDB locking.
- `validate` fails with `TERRAFORM_TOO_OLD` when S3-native locking needs a newer
  Terraform.
- Cloud bootstrap starts the admin console. Phase 12 adds the `admin-ui` service,
  and the Tailscale URL now serves the console, with nginx proxying every
  gateway route.

### Changed

- Licensed under the Apache License, Version 2.0.
- Skills are vendored in `skills/`; the `ts-skills-library` dependency and the
  `ts-` skill prefix are gone.
- `AGENT_RUNTIME_URL` is the runtime URL. Environment variables, the console
  command, the webhook signature, and Terraform addresses use TS Harness names.
- The agent-runtime image builds `gh`, gitleaks, and osv-scanner from pinned
  release commits with a current Go toolchain.
- Terraform backends are committed as empty `backend "s3" {}` / `backend "gcs" {}`
  blocks; the installer supplies the location from `terraform_state`. Remove any
  hand-copied `infra/<cloud>/backend.tf` before pulling. The `backend.tf.example`
  files are gone.
- Provider lock files (`infra/aws/.terraform.lock.hcl`, `infra/gcp/.terraform.lock.hcl`)
  are committed.
- The AWS providers refuse credentials for any account other than `aws_account_id`.
- Installer Terraform output streams live instead of printing after each command.
- CI pins Terraform 1.10.5.
- The `jira_to_pr` routing signal matches `implement` followed by a Jira key in
  any project (`implement [a-z][a-z0-9]+-\d+`) instead of one internal project
  prefix.
- The release workflow no longer has a placeholder cosign signing step or the
  `id-token: write` permission it requested. Images are not signed until they
  are published.
- The release job drops the unused `packages: write` permission and gains
  `security-events: write`, which the Trivy SARIF upload needs. Upload failures
  now fail the job instead of being ignored.

### Removed

- The committed `.lkg` last-known-good config copies. The gateway writes them
  at runtime, and `*.lkg` is now ignored.

### Fixed

- Workflow dispatch errors returned a server error instead of a blocked result.
- The embeddings preflight never detected a missing API key.
- Migrations 018 and 024 now run inside a transaction.
- AWS ignored `secret_prefix`: secrets and the SSM runtime-config were always
  named after `project_name`, while the installer printed population commands
  using `secret_prefix`. AWS now uses `secret_prefix` (falling back to
  `project_name`), as GCP does. `validate` fails with `SECRET_PREFIX_CHANGED`
  instead of renaming an existing install's secrets.
- The installer passed a relative `-var-file` path that broke under `-chdir`
  when `--state-dir` was relative.
- Bootstrap stopped after phase 14 and still reported success: the phase list
  was read from stdin, which `docker compose exec` drained. Phases 15, 16, and
  99 now run.
- The agent-runtime image omitted `evals/`, so the `agent_redteam` and
  `agent_output_judge` release gates failed with missing datasets.
- The admin console nginx resolved `agent-runtime` once at startup, so it
  returned errors after that container was recreated.
- Cloud installs never set `TS_HARNESS_SESSION_DB_URL`, so sessions and
  transcripts stayed in JSONL files on the host and Google OAuth token storage
  could not work. Bootstrap now points them at the install's Postgres, and
  `sync-compose-db-env.sh` recreates agent-runtime after a DB password change.
