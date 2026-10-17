# Google Drive artifact library

Structured Drive folders for governed delivery program artifacts (PRD, refinery, foundry, planner).

## Prerequisites

1. **Root folder ID in Secrets Manager:**
   - Secret name: `{project_name}/google-delivery-artifacts-folder-id` (e.g. `ts-harness/google-delivery-artifacts-folder-id`)
   - Value: the Google Drive folder ID (from the folder URL)
   - Prefer a **Shared Drive** folder owned by the PM / delivery team
   - Terraform creates the secret placeholder (`infra/aws/secrets.tf`); populate the value in AWS Secrets Manager after apply
2. **Bootstrap wiring:** Phase `10-fetch-secrets` loads the secret into `GOOGLE_DELIVERY_ARTIFACTS_FOLDER_ID`; phase `11-agent-runtime-setup` substitutes it into `ts-harness-runtime.yaml` via `{{GOOGLE_DELIVERY_ARTIFACTS_FOLDER_ID}}`.
3. **Service account:** PM delegated account (`GOOGLE_PM_EMAIL`) must have **Contributor** (or higher) on the root folder.
4. **Runtime config:** `connectors.google-workspace.artifact_publish.library.enabled: true` (enabled in production tmpl by default).
5. **Slack email resolution:** Requester sharing uses `users:read.email` or `identity.slack_user_emails` mapping (same as single-doc publish).

### Apply after deploy

```bash
# 1. Terraform apply (creates the empty secret if missing)
# 2. Set the folder ID:
aws secretsmanager put-secret-value \
  --secret-id ts-harness/google-delivery-artifacts-folder-id \
  --secret-string 'YOUR_DRIVE_FOLDER_ID'

# 3. Re-run secrets + runtime render on the instance, then restart gateway
```

## Folder layout

```text
GOOGLE_DELIVERY_ARTIFACTS_FOLDER_ID/
  programs/                          # when program_subfolder is set
    {program-slug}/
      manifest                       # index Google Doc
      prd/
      refinery/
      foundry/
      planner/
```

One Google Doc is created per artifact key (e.g. `frds_markdown` → doc titled `frds`) when a specialist stage is **presented for review** in Slack.

## Verification smoke test

1. Deploy runtime with library enabled and valid folder ID.
2. Restart gateway and workflow-runner.
3. Run a full program: PRD workflow → approve → advance to work orders.
4. After each present (draft PRD, refinery, foundry, planner), confirm Slack message includes:
   - `Artifact library: https://drive.google.com/...`
   - `Manifest: https://docs.google.com/...`
5. Open the program folder — verify phase subfolders and docs exist.
6. Confirm requester can open folder and docs (same Workspace domain as delegated SA).
7. Re-run a revise step — docs should be replaced in place; manifest `Last updated` changes.

## Troubleshooting

| Symptom | Likely cause |
|---------|----------------|
| `(Google Drive artifact library publish failed: folder setup failed)` | Missing or invalid `GOOGLE_DELIVERY_ARTIFACTS_FOLDER_ID` |
| Folder created but not shared | Slack user email not resolved; check scopes / `slack_user_emails` |
| Library skipped for step | Step not in `step_phase_map` (orchestrator approval gates are excluded) |
| Overflow single-doc still used | Library disabled or publish failed; check gateway logs |

## Index persistence

Drive doc IDs and links are stored on the delivery program at `metadata.artifacts.drive_library` via memory-api. JSON task output in the workflow DB remains the source of truth for implement handoff.
