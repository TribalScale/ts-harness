# Implement work orders with OpenCode

End-to-end smoke for the governed path: prepare → implement WO → PR → merge resume → next WO. Deploy is out of scope.

## Prerequisites

1. Terraform applied so Secrets Manager has:
   - `ts-harness/coding-backend-api-key`
   - `ts-harness/coding-backend-provider` (e.g. `anthropic`, `openai`)
   - `ts-harness/coding-backend-model` (optional; e.g. `claude-sonnet-4` — empty uses OpenCode provider default)
   - `ts-harness/github-token` (clone / push / `gh pr create`)
2. Populate those secrets (non-placeholder values). Terraform seeds them as `" "` and **ignores later `secret_string` changes**, so `terraform apply` will not wipe values you set in Secrets Manager.
3. Set the target repo and OpenCode pin in `infra/aws/terraform.tfvars` (baked into user-data / `instance-env` on apply):
   - `implement_work_orders_repo_url` → `IMPLEMENT_WORK_ORDERS_REPO_URL`
   - `implement_work_orders_base_branch` → `IMPLEMENT_WORK_ORDERS_BASE_BRANCH` (default `main`)
   - `implement_work_orders_opencode_version` → `OPENCODE_VERSION` (phase `11h` install pin; prefer semver over `latest`)
4. `terraform apply`, then re-run bootstrap (or replace the instance) so compose rebuilds agent-runtime:
   - `10-fetch-secrets.sh` (writes `CODING_BACKEND_*` + `GITHUB_TOKEN` into `secrets.env`; **fails closed** if `coding-backend-api-key` / `coding-backend-provider` are empty or Terraform placeholders)
   - `11-agent-runtime-setup.sh` (renders `implementation.*` with `tools_root: /tools`)
   - `11b-agent-auth-init.sh`
   - `11h-coding-tools-install.sh` (installs OpenCode under host `$DATA_DIR/.ts-harness/tools` using `OPENCODE_VERSION`)
   - `12-compose-up.sh` / `14-channels-init.sh` (write runtime `.env` via `write_runtime_env_file`; bind-mounts host tools → container `/tools`; image includes `git` + `gh`)
   - After secret updates, use `scripts/ops/rotate-secrets.sh` (uses the same `.env` writer — do not hand-edit `.env` only)
5. Confirm **inside agent-runtime** (not only on the host):

```bash
docker compose exec agent-runtime sh -c \
  'test -x /tools/opencode/current/bin/opencode && git --version && gh --version && \
   test -n "$CODING_BACKEND_PROVIDER" && test -n "$CODING_BACKEND_API_KEY" && test -n "$GITHUB_TOKEN" && \
   /tools/opencode/current/bin/opencode --version'
```

Optional release gate `coding_backend_readiness` covers the same checks when those env vars are present.

## Smoke checklist

1. Complete a delivery program through `work_orders_from_prd` prepare (handoff fields + `work_orders_markdown` on program artifacts).
2. Program **auto-advances** to `implement` after prepare (`work_orders_from_prd.program.auto_advance=true`). Manual `advance_program` is only needed if auto-advance was skipped.
3. `deliver_work_order` runs on `dev-agent` with `verified-merge-governed` + `run_coding_backend`.
4. Expect a PR URL in task output with `work_order_id` set (e.g. `WO-001`). The tool clones, runs OpenCode, commits, pushes, and opens the PR (no sandbox `exec`). Retries reuse an existing PR URL if one already exists for the branch.
5. Merge the PR on GitHub.
6. Resume wait via outbound merge poller (default on Tailscale hosts), GitHub webhook, or manual tool:

- Default (no public ingress): system cron job `poll-github-merges` (written by phase 15 into `config/runtime-cron-jobs.json`) polls GitHub every 60s for blocked `github.merge` waits and calls `resume_external` when the PR is merged (`GITHUB_TOKEN` required).
- Optional public ingress: `POST /webhooks/github` with a `pull_request` `closed` + `merged=true` payload (set `GITHUB_WEBHOOK_SECRET` and configure the GitHub webhook).
- Fallback / smoke:

```text
resume_external_event(
  event_type="github.merge",
  payload={"pr_url": "<exact pr_url from deliver_work_order output>", "merge_sha": "<optional>"}
)
```

Or via memory API `POST /workflow/resume-external` with the same shape.

7. Confirm program artifacts: `completed_work_order_ids` includes `WO-001` and `merge_sha` when provided.
8. Confirm a new `implement_work_orders` workflow starts for the next dependency-ready WO (automatic re-entry).
9. When all WOs complete, program artifacts include `work_orders_delivery_complete=true` / `program_status=succeeded`. Deploy remains a **manual** advance.

## Watching OpenCode mid-run

While `deliver_work_order` is running, OpenCode streams to a workspace log and emits `opencode_progress` events on agent-runtime stdout:

```bash
# Live OpenCode stream (replace WO-001 as needed)
docker exec docker-agent-runtime-1 \
  tail -f /workspace/dev-agent/coding-backend/WO-001/.ts-harness/opencode-WO-001.log

# Structured progress heartbeats (~every 30s)
docker logs -f docker-agent-runtime-1 2>&1 | grep opencode_progress
```

Tool results also include `log_path` and `log_tail` after the run finishes.

`POST /workflow/dispatch` uses `TS_HARNESS_DISPATCH_REQUEST_TIMEOUT` (default **7200s**) so long OpenCode runs are not cut by the general 300s API timeout.

## Failure modes

| Symptom | Likely cause |
|---|---|
| Phase 10: `CODING_BACKEND_* missing` | SM still has Terraform `" "` — set real values and re-run phase 10 (apply will not overwrite) |
| `CODING_BACKEND_*` missing from `.env` after rotation | Old `rotate-secrets` bug; pull fix and re-run `scripts/ops/rotate-secrets.sh` |
| `CODING_BACKEND_PROVIDER is required` | Secret missing from runtime `.env`; re-run 10 + 12 (or rotate-secrets) |
| `GITHUB_TOKEN is required` | Populate `github-token`; re-run 10 + 12 (or rotate-secrets) |
| `coding_backend_readiness` failed with detail | Read gate `detail` (missing binary / env / tools_root / OpenCode path) |
| `work_order_body is required` | Planner markdown not patched onto program; re-complete planning |
| OpenCode not found | Phase `11h` not run; compose missing tools mount; or absolute `opencode/current` symlink (must be relative `current -> latest` so `/tools` resolves in-container) |
| Compose: `TS_HARNESS_TOOLS_ROOT_HOST is missing` | Phase 12 used an incomplete `sudo env` list; pull fix and re-run 12 (clear phase marker if needed) |
| Sandbox / network errors | Agent used `exec` instead of `run_coding_backend` |
| `await_human_merge` stuck | Resume payload `pr_url` does not exactly match deliver output |
| Stage blocked: missing `pr_url` | Tool failed before PR create; check `run_coding_backend` error / `log_tail` / `log_path` |
| `transport_error: {"error":"request timeout"}` after ~5m | Agent-runtime still on old image/env without `TS_HARNESS_DISPATCH_REQUEST_TIMEOUT`; recreate agent-runtime |

## Related

- [governed-delivery-flow.md](governed-delivery-flow.md) section B2
- Runtime config: `implementation.coding_backend`, `implementation.repos.implement_work_orders`
