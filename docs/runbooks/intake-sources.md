# Intake sources

People can attach files and paste Google Drive links when they ask for work.
The intake stages read that material as text. Filing, work orders and
implementation never see the raw files; they work from the approved brief.

## Where it applies

| Workflow | Version | Read by | Stages that get `input.sources` |
| --- | --- | --- | --- |
| `content_to_plan` | 1.4.0 | `gather_context` (`include_intake_sources: true`) | `compare_codebase`, `clarify`, `draft_plan` |
| `bug_to_plan` | 1.4.0 | `gather_context` (`include_intake_sources: true`) | `investigate`, `clarify`, `draft_bug` |
| `prd_request` | 1.5.0 | `gather_sources` (before `intake`) | `intake`, `draft_prd` |
| `rfp_request` | 1.2.0 | `gather_sources` (before `intake`) | `intake`, `draft_rfp` |

`input.sources` is markdown: one section per source, then a **Not read** list
with the reason for each skipped file. A source that could not be read is a
section marked _Could not be read_; the run continues and the agent asks about
it once.

## What people can attach

| Channel | How | Fetched |
| --- | --- | --- |
| Console | **Attach** in the chat composer | Uploaded on pick to `POST /intake/uploads`, stored right away |
| Slack | Files on the message | At gather, with `SLACK_BOT_TOKEN` |
| Telegram | A document, or a photo (largest size); the caption is the text | At gather, with `TELEGRAM_BOT_TOKEN` |
| Discord | Attachments on the message | Copied to storage on arrival; at gather, Discord's link is the fallback |
| Any | Google Docs, Sheets, Slides, Drive file or folder links in the text | At gather, as the stage's Workspace mailbox |

Readable types: text, markdown, CSV, JSON, YAML, DOCX, PDF with a text layer,
Google Docs/Sheets/Slides (exported as text/CSV), and PNG/JPEG/GIF/WebP images.
A scanned PDF with no text layer is reported, not read.

Files sent before a program starts are held on the chat thread and passed to
the first task. A declined proposal releases them. Files sent as a reply after
the program has started (a clarify answer, PRD intake answers) are read in
that turn and added to the recorded answer. Routing and approval words are read
from the person's text only, never from a file.

## Caps

| Cap | Value |
| --- | --- |
| Attachments per program | 25 (a Drive folder counts as 1) |
| Per file | 20 MB |
| Total bytes per program | 80 MB |
| Text per source | 80,000 characters |
| Text per gather | 150,000 characters (75,000 for a reply) |
| Drive folder | one level, first 50 files; subfolders are listed as not read |
| Time | 120 s per gather, 60 s per reply |
| Image sent to the model | 5 MB |
| JSON request bodies (any route except uploads) | 10 MB, `TS_HARNESS_MAX_JSON_BODY_BYTES` |

Over-cap files are reported to the requester; they are not silently dropped.

## Google Drive access

Drive is read as the stage agent's Workspace mailbox: `pm-agent` for content,
bug and PRD, `sales-agent` for RFP. The person must share the file or folder
with that mailbox. Otherwise the source reads _not shared with the workspace
account_. Shared drives are supported.

## Images

Images are described by the stage agent's current model (the runtime YAML plus
any change made under **Agents** in the console), so they go to the same
model that writes the brief.

- `TS_HARNESS_INTAKE_VISION_MODEL=<provider/model>` uses another model for images.
- `TS_HARNESS_INTAKE_VISION_MODEL=off` turns image reading off. Images are then
  reported as _image reading is not enabled_.

A model that cannot read images produces _image could not be read_; the run
continues.

## Storage

Uploads and Discord copies live in the `intake` object store under
`intake/{tenant}/`. Pointers on sessions and tasks hold only the key, size and
sha256; reads are checked against the sha256.

| Backend | Setting | Retention |
| --- | --- | --- |
| `TS_HARNESS_OBJECT_BACKEND=local` | `ts-harness-intake-objects` volume, shared by agent-runtime and workflow-runner | Swept hourly by the gateway after 14 days |
| `s3` | `TS_HARNESS_INTAKE_BUCKET` | Bucket lifecycle rule (below) |
| `gcs` | `TS_HARNESS_INTAKE_BUCKET` | Bucket lifecycle rule |

S3 lifecycle rule:

```hcl
resource "aws_s3_bucket_lifecycle_configuration" "intake" {
  bucket = aws_s3_bucket.intake.id
  rule {
    id     = "expire-intake"
    status = "Enabled"
    filter { prefix = "intake/" }
    expiration { days = 14 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
```

Turn on default server-side encryption for the bucket. The gateway and the
workflow runner need `s3:PutObject`, `s3:GetObject` and `s3:ListBucket` on it
(`s3:DeleteObject` only if you also sweep from code). A program started more
than 14 days after its files were uploaded reports them as _attachment
expired; please attach it again_.

## Workflow runner settings

The gather runs in the workflow runner, which has no `env_file`.
`docker/docker-compose.workflow-runner.yml` passes these through; set them in
the compose environment (or use an instance or task role for AWS):

- `TS_HARNESS_OBJECT_BACKEND`, `TS_HARNESS_INTAKE_BUCKET`, and AWS credentials
  or `AWS_REGION` with a role
- `SLACK_BOT_TOKEN` (the Slack app needs the `files:read` scope),
  `TELEGRAM_BOT_TOKEN`
- `LLM_PROXY_URL`, `LLM_PROXY_SECRET` for image reading through the proxy
- `TS_HARNESS_INTAKE_VISION_MODEL` (optional)
- the Google Workspace service account at `/config/google-workspace-sa.json`

## Console and proxy

The admin console nginx routes `/intake/` to agent-runtime with
`client_max_body_size 21m` and request buffering off. A proxy in front of the
gateway must allow 20 MB request bodies on `/intake/uploads`.

## Troubleshooting

| The agent says | Check |
| --- | --- |
| _not shared with the workspace account_ | Share the Drive item with the stage agent's mailbox |
| _Google Drive is not connected_ | Workspace connector config and the service account mount on the runner |
| _the Slack app needs the files:read scope_ | Add the scope and reinstall the Slack app |
| _attachment expired; please attach it again_ | Over 14 days old, or a Discord link that expired before it was copied |
| _image reading is not enabled_ | `TS_HARNESS_INTAKE_VISION_MODEL` is `off`, or the agent's model could not be resolved on the runner |
| Uploads fail with 413 at the proxy | Proxy body limit on `/intake/` |
| Uploads fail with 503 | Intake bucket missing or not writable; see `intake_upload_failed` in the gateway log |
