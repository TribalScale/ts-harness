# Data handling

What client data the harness stores, where it goes, how long it is kept and how to delete it.
Operators: the procedures are in [Change the retention windows](#change-the-retention-windows)
and [Delete a client's data on request](#delete-a-clients-data-on-request).

## What is stored, and where

Everything below lives in the harness Postgres database (the memory API's database, which is also
the session database) unless the row says otherwise.

| Data | Where | What it holds | Retention |
| --- | --- | --- | --- |
| Chat transcripts | `runtime_transcript_messages` | Every user, assistant and tool message of a channel or console chat | 180 days per message |
| Program records | `delivery_programs` (`metadata.artifacts`) | The request summary, PRD, requirements, work orders, design references | Kept until purged on request |
| Workflow tasks | `workflow_tasks.input` / `.output` / `.error` | Work orders, PRD drafts, review findings, code excerpts, QA evidence | Large fields trimmed 365 days after the workflow finished |
| Coding jobs | `coding_jobs.spec`, `.result`, `.progress`, `.last_output` | The work order sent to the worker; the run log tail, check output, usage, timings, PR link, pre-push review summary | Logs and progress trimmed 30 days after the job finished; the spec with its task (365 days) |
| Audit events | `runtime_audit_events`, `session_audit_log` | Who did what: tool calls, routing, workflow steps, approvals, access | 730 days; approval and decision events kept until you set a window |
| Human decisions | `workflow_decisions`, `program_releases` | Gate approvals, track choices, promotions, releases with actor and reason | Kept (compliance record); deleted only by a purge with `--include-decisions` |
| LLM usage | `llm_usage`, `agent_token_usage` | Tokens and cost per call, with team, program, workflow, task and user ids. No prompt or response text | Kept (budgets and billing); a purge removes the program, workflow and task ids |
| Program memories | `memories` (tag `program:<id>`), `program_briefs`, `knowledge_notes` (with its search vector and embedding) | Summaries and notes learned from a program, including notes drafted from failed-then-fixed runs | Kept until purged (program memories) or edited by an admin (notes) |
| Uploaded files | Intake object store, `intake/<tenant>/...` | Files attached to a request (console, Slack, Discord, Telegram) | 14 days (bucket lifecycle rule; the local store is swept hourly) |
| Design images | Intake object store, `design/<tenant>/<sha256>.<ext>` | Figma frames and attached images used as design references | 180 days after their program finished (and the bucket's one-year `design/` rule) |
| Coding file events | `coding_job_events` | The live feed: each file a run created, edited, deleted, renamed or restored, with the step, line counts and a diff excerpt of at most 200 lines (secret-looking values masked in the worker before sending); many files at once are one row listing up to 50 paths | Deleted 30 days after they were recorded (the coding-log period) |
| Coding workspaces | One Docker volume per ticket (`ts-coding-ws-*`) | The repository clone, installed dependencies, build output | 48 hours (`CODING_WORKSPACE_TTL_HOURS`) |
| Screenshots | An evidence branch in the client's repository (`ts-harness-evidence/<branch>`) | Playwright screenshots for review and QA | Deleted when the PR merges |
| Channel dedup ids | `channel_event_dedup` | Platform event ids only | 7 days (`sweep-channel-dedup`) |
| Agent workspaces | `/workspace/<agent>` or the workspaces bucket | Agent documents and file-tool output | Kept; bucket noncurrent versions expire after 30 days |

On a single host with local state (`TS_HARNESS_STATE_BACKEND=local`), audit events and the
transcripts of a host without a session database are JSONL files under the data directory; the
retention job does not trim those files.

Backups keep deleted data until they age out: RDS automated snapshots and point-in-time recovery
(the backup retention you configured, see [db-restore.md](runbooks/db-restore.md)) and bucket
versioning (noncurrent versions expire after 90 days for skills and 30 days for workspaces).

## What is sent to model providers

Model calls carry client content. An agent prompt can include the request and chat history, work
orders, PRD and requirements text, repository files and diffs, review findings, verification
output and design images. A coding worker sends its work order and the repository code it reads
to the coding model through OpenCode.

- Calls go through the LLM proxy when it is on ([llm-proxy.md](runbooks/llm-proxy.md)). The proxy
  holds the provider keys, meters each call into `llm_usage` (counts and cost, never text) and
  forwards the request unchanged. It does not store prompts or responses.
- Providers are whatever the runtime config names: the agent models in
  `ts-harness-runtime.yaml` (`anthropic/...`, `openai/...`), `CODING_BACKEND_PROVIDER` for
  coding workers, `openrouter` when `OPENROUTER_API_KEY` is set, and `MODEL_BASE_URL` for any
  other OpenAI-compatible endpoint. Check `agents.*.model`, `coding_backend` and `llm_pricing` in
  the rendered config to see which ones a host uses.
- Memory embeddings send memory text to `EMBEDDINGS_PROVIDER` (`openai`, `voyage`, `gemini`,
  `mistral` or `bedrock`). The same provider embeds knowledge notes (title, body, why), the
  search text typed on the console's Memory page, and a run's summary and the head of its work
  order when notes are picked for that run. The vectors are stored on the note
  (`knowledge_notes.embedding`, migration 054); query vectors are cached in the memory API's
  process only.
- The failure drafter (`draft_failure_notes`) sends the revision diagnosis model short excerpts
  of a finished run: repeated review or QA findings, failed CI check names and feedback, coding
  job errors and log tails, and guidance a person or the arbiter gave. Its drafts are stored as
  proposed knowledge notes with links to the program, workflow, tasks and PR.
- Intake vision (`TS_HARNESS_INTAKE_VISION_MODEL`) sends uploaded images and Figma frames.

Each provider's own retention and training terms apply to what it receives; use the providers'
zero-retention or enterprise terms where a client requires it.

The harness also writes to the client's own systems as part of the work: Jira tickets, GitHub
branches, pull requests, PR comments and evidence branches, Slack messages and Google Drive
documents. Those copies follow the client's retention in those systems.

## Retention defaults

The `apply-data-retention` system job runs once a day. Each run handles at most one batch per
category (500 rows by default), so it never holds long locks; a backlog clears over several days.
It returns counts per category, which show in the cron run history.

| Variable | Default | What it trims or deletes |
| --- | --- | --- |
| `TS_HARNESS_RETENTION_TRANSCRIPT_DAYS` | 180 | Transcript messages older than this. An active chat keeps its recent messages; replays repair a history that starts mid tool call. |
| `TS_HARNESS_RETENTION_CODING_LOG_DAYS` | 30 | On finished coding jobs: `result.log_tail`, every `output_tail`, `stdout` and `stderr` in the result, `progress` and `last_output`. Status, timings, usage, PR link, branch, changed files, security summary and the pre-push review summary stay. The live feed's file events (`coding_job_events`, with their diff excerpts) older than this are deleted. |
| `TS_HARNESS_RETENTION_WORKFLOW_TASK_DAYS` | 365 | For tasks of workflows that finished this long ago: `input` and `output` keep only their short top-level values (ids, status, outcome, URLs) plus `retention_trimmed: true`; `error` is cut to 500 characters; the coding job `spec` of those tasks is trimmed the same way. Status and timestamps stay. |
| `TS_HARNESS_RETENTION_AUDIT_DAYS` | 730 | Runtime and session audit events, except approval and decision events. |
| `TS_HARNESS_RETENTION_AUDIT_DECISION_DAYS` | 0 | Approval, rejection, decision, gate, permission, access, governance, release, evidence and purge audit events. 0 keeps them. |
| `TS_HARNESS_RETENTION_UPLOAD_DAYS` | 180 | Design images of programs that finished this long ago, unless another program or a running or blocked workflow still uses the same image. |
| `TS_HARNESS_RETENTION_BATCH` | 500 | Rows per category per run (at most 5,000). |

0 keeps that data forever. What the job never touches:

- running or blocked workflows, and finished workflows of a program (or its root program) that is
  still running or blocked;
- coding jobs that have not finished, or whose workflow is still running or blocked;
- programs that have not finished (their design images, records and memories);
- `workflow_decisions` and `program_releases`;
- approval and decision audit events before their own window.

## Change the retention windows

1. On the host, add the variables to the instance environment (they survive phase 10a re-runs;
   the data directory is `/vol/ts-harness-data` on a standard install):

   ```bash
   sudo tee -a /vol/ts-harness-data/.setup/instance-env.sh <<'EOF'
   export TS_HARNESS_RETENTION_TRANSCRIPT_DAYS=90
   export TS_HARNESS_RETENTION_AUDIT_DECISION_DAYS=2555
   EOF
   ```

2. Re-run phase 12 so the runtime `.env` picks them up and agent-runtime restarts
   ([re-run-bootstrap-phase.md](runbooks/re-run-bootstrap-phase.md)).
3. Check the next `apply-data-retention` run in Admin console **System → Cron**: its result lists `policy`
   with the windows in force and a count per category.

Shorter windows apply on the next run. Data already deleted cannot be brought back except from a
database backup.

## Delete a client's data on request

`scripts/ops/purge-program-data.py` deletes one finished program and everything stored for it:
the program and its child programs, their workflows, tasks, coding jobs, plans and tool approvals,
audit events about them, program memories, transcripts of chat sessions tagged with the program,
and design images no other program or open workflow uses. LLM usage rows keep their cost and lose
their program, workflow and task ids. Decision records stay unless you pass `--include-decisions`.
A `program_data_purged` audit event records who ran it and the counts, never content.

1. Find the client's programs (Admin console **Programs**, filter by project or requester) and note
   each program id. Cancel any that are still running.
2. Count what would be deleted:

   ```bash
   sudo docker exec docker-agent-runtime-1 \
     python3 /app/scripts/ops/purge-program-data.py <program_id> --dry-run
   ```

   The script refuses while a program or workflow in scope is running or blocked.
3. Delete:

   ```bash
   sudo docker exec docker-agent-runtime-1 \
     python3 /app/scripts/ops/purge-program-data.py <program_id> --yes \
       --actor you@example.com [--include-decisions] [--session-id <session_id> ...]
   ```

   Sessions linked to the program (`runtime_sessions.metadata.program_id`) are included. Add
   `--session-id` for other chat sessions about it, such as a thread that later moved on to
   another program (`runtime_sessions` holds each session's id and `metadata.binding_key`).
4. Outside the harness, delete what the work left in the client's systems if the request covers
   them: Jira tickets, branches and PRs, evidence branches, Slack threads, Drive documents.
5. Backups still hold the data until they age out (see above). Record the request, the program
   ids and the script output in your data-request log.

## Logging rules

- Secrets are never logged, put in prompts or written to audit events. Configuration names
  secrets (`*_secret`); values live in the secrets manager or the host `.env` (mode 600).
  Audit payload keys that look like credentials (`password`, `token`, `secret`, `api_key`,
  `authorization`) are written as `[REDACTED]`.
- The secret scan over a coding change records file, line and rule only; the matched value is
  never stored, logged or put in a prompt or PR body.
- Tool errors shown to agents and users are redacted (`redact_tool_error`); resolved secret values
  are masked in output (`SecretResolver.redact`).
- Service logs carry ids, counts, statuses and error messages. Do not add prompt text, file
  contents or request bodies to log lines; put content in the stores above, where retention
  applies.
- The retention job and the purge script return counts and ids only.
