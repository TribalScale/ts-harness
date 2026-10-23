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
# Live OpenCode stream (workspace is <WO>-<first 8 chars of the delivery workflow id>)
docker exec docker-agent-runtime-1 sh -c \
  'tail -f /workspace/dev-agent/coding-backend/WO-001-*/.ts-harness/opencode-WO-001.log'

# Structured progress heartbeats (~every 30s)
docker logs -f docker-agent-runtime-1 2>&1 | grep opencode_progress
```

Tool results also include `log_path` and `log_tail` after the run finishes.

If OpenCode prints nothing for `CODING_BACKEND_STALL_SECONDS` (default 900; `0` disables), the runtime kills it and its child processes, writes `opencode stalled: no output for Ns` to the log, emits an `opencode_stalled` event, and fails the step with `opencode stalled: no output within the stall window`. The runner's retry policy then decides whether to try again.

Each delivery workflow gets its own workspace and branch: `coding-backend/<WO>-<suffix>/` and `wo/<wo>-<suffix>`, where the suffix is the first 8 characters of the `deliver_work_order` workflow id. Two deliveries of the same work order (a retry after cancel, or a second program) never share a clone or push to each other's branch. A `.ts-harness-owner` marker in the workspace records which delivery owns it; a clone owned by another delivery is discarded before cloning.

`POST /workflow/dispatch` uses `TS_HARNESS_DISPATCH_REQUEST_TIMEOUT` (default **7200s**) so long OpenCode runs are not cut by the general 300s API timeout.

## Coding jobs (OpenCode outside agent-runtime)

With `coding_jobs_enabled: true` (env `CODING_JOBS_ENABLED=true`), `deliver_work_order` 1.5.0 no longer runs OpenCode inside an agent turn. The stage has `"execution": "coding_job"`:

1. The runner builds the job spec from the task input (repo, branch, PR, feedback, refresh) and queues a coding job.
2. The task parks as `blocked` with `awaiting_external:coding.finished`, and the runner thread is free. The console shows "OpenCode queued" until a worker starts, then "OpenCode implementing" with run time and the latest log line.
3. `coding-launcher` claims queued jobs, up to `coding_max_running` (default 3) starting or running at once, and starts one `ts-harness-coding-worker` container per job with `docker run --rm` and CPU, memory and pids limits.
4. The worker fetches its spec with a job token that only opens that job's spec, heartbeat and complete endpoints. It heartbeats every 30 seconds with the last line of the OpenCode log, then posts the result and exits.
5. Completing the job puts the task back to pending. The runner checks the result against the stage contract exactly as it checks an agent's output; a failed run blocks with `deliver_work_order cannot succeed when run_coding_backend failed: ...` and revision and retry policy apply as before.

Restarting `agent-runtime` or `workflow-runner` mid-run does not interrupt a job; the worker keeps running and its result is picked up when the task is next dispatched.

Turn it on:

1. Deploy with `coding_jobs_enabled: false` and confirm a normal delivery.
2. Set `coding_jobs_enabled: true` (install config, or `CODING_JOBS_ENABLED=true` in the instance env) and re-run phase 12. It builds the worker image from the agent-runtime image plus OpenCode (`OPENCODE_VERSION`) and starts `coding-launcher`.
3. Run one work order end to end and watch it:

```bash
docker logs -f docker-coding-launcher-1 2>&1 | grep coding_job
docker ps --filter label=ts-harness.coding-job
docker logs -f <worker container>
```

Worker secrets pass by name from the runtime `.env`: `GITHUB_TOKEN`, `GH_TOKEN`, provider keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`, `MODEL_*`), and every `CODING_BACKEND_*` and `OPENCODE_*` variable. Add others with `CODING_WORKER_ENV=NAME1,NAME2` on the launcher. Job tokens are signed with `CODING_JOB_SECRET` on memory-api, or a key derived from `MEMORY_API_TOKEN` when it is unset.

The `sweep-coding-jobs` scheduler job runs every 60 seconds. A job with no heartbeat for 5 minutes (`CODING_JOB_HEARTBEAT_TIMEOUT_SECONDS`) or running over 60 minutes (`CODING_JOB_MAX_RUNTIME_SECONDS`) is marked `lost`; the launcher stops its container and the task blocks with `transport_error: coding job lost: ...`, which the retry policy retries. Cancelling a workflow or program cancels its jobs and the launcher stops their workers.

Turn it off by setting the flag back to `false` and re-running phase 12. Jobs already queued still finish; new deliveries run in agent-runtime again.

## Projects with several repositories

Add the repositories under **System → Projects → (project) → Repositories**. With the GitHub App installed, only repositories an installation covers can be added.

- One repository: every work order goes there, ahead of `IMPLEMENT_WORK_ORDERS_REPO_URL`.
- Several: the planner writes `Repository: owner/name` under each `# WO-NNN` heading. The planner contract rejects a missing or unknown repository, and dependencies that run both ways between two repositories.
- Each work order's coding run clones its own repository and opens its PR there, using the repository's base branch when one is set.

A program reads the project's repositories when each phase starts, so a repository added mid-program applies from the next phase.

## Host verification test stacks

After OpenCode finishes, the host installs the repo's declared dependencies and runs its own test command (`runtime/ts_harness/implementation/test_env.py`). The first matching stack wins:

| Repo signal | Stack | Unit command | BDD |
|---|---|---|---|
| `package.json` with a `test` or `test:unit` script | node | `npm test` / `npm run test:unit` | `npm run test:bdd` / `cucumber` / `bdd` script |
| `go.mod` | go | `go test ./...` | `.feature` suites (godog) run inside the unit command |
| `Cargo.toml` | cargo | `cargo test` | inside the unit command |
| `mvnw` or `pom.xml` | maven | `./mvnw -B test`, else `mvn -B test` | inside the unit command (Cucumber JVM) |
| `gradlew`, `build.gradle`, `build.gradle.kts` | gradle | `./gradlew test`, else `gradle test` | inside the unit command |
| `pytest.ini`, `[tool.pytest` in `pyproject.toml`, `[tool:pytest]` in `setup.cfg`, `[pytest]` in `tox.ini`, `conftest.py`, or `.py` files under `tests/` | python | `<venv>/bin/python -m pytest` | `behave.ini`, `features/environment.py`, or `features/steps/*.py`: `python -m behave` |
| `Makefile` with a `test:` target | make | `make test` | n/a |

A bare `tests/` or `features/` directory does not select a stack. Multi-stack monorepos use the first match.

Dependencies installed before OpenCode runs and again before each verify (only when a manifest changed):

- Python: `requirements.txt`, `requirements-dev.txt`, `requirements-test.txt`, and `pip install -e .` for a `pyproject.toml` with `[project]`. Always into the work order's venv, never the runtime's site-packages.
- Node: `npm ci` with `package-lock.json`, else `npm install`.
- Go: `go mod download`. Cargo, Maven and Gradle fetch during the test run.

Where things live, per delivery under `/workspace/dev-agent/coding-backend/<WO>-<suffix>/`:

- `.ts-harness/venv/`: virtualenv with pytest and behave installed offline from `/opt/ts-harness/wheels` (pins in `docker/test-runner-requirements.txt`).
- `.ts-harness/deps.json`: manifest hashes of the last successful installs.
- `../.cache/`: shared Go, npm, Cargo, Gradle and Maven caches, reused across work orders.
- `repo/.git/info/exclude`: `node_modules/` and the stack's build output (`target/`, `.gradle/`, `build/`) so installs are never committed.

OpenCode runs with the same `PATH` and venv, so its own test runs match host verification. The agent-runtime image ships Go, Node.js LTS, JDK 21 with Maven and Gradle, Rust (minimal profile), make and gcc; bump versions with their checksums in `docker/Dockerfile.agent-runtime`.

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
| "Request changes" or `/revise` did nothing | The reviewer lacks write access, the review is on an older commit, or the workflow started on a definition before `verified_merge` 1.6.0. See [human rejection](governed-delivery-flow.md#human-rejection) |
| `external_blocked:github.pr_closed by <login>` | The PR was closed without merging; the work order stopped by design. Start a new work order to retry |
| Stage blocked: missing `pr_url` | Tool failed before PR create; check `run_coding_backend` error / `log_tail` / `log_path` |
| `transport_error: {"error":"request timeout"}` after ~5m | Agent-runtime still on old image/env without `TS_HARNESS_DISPATCH_REQUEST_TIMEOUT`; recreate agent-runtime |
| `verification_failed: shadowed_test_runner:pytest.py` (or `behave/`, `_pytest/`) | The change added a repo-root module that stands in for the test framework; the suite is not run. The fix pass asks OpenCode to delete it |
| `verification_failed: missing_toolchain:<tool>` with `fix_passes_used=0` | The repo's stack needs a tool the image lacks (old image, or an unsupported stack). Rebuild agent-runtime from the current Dockerfile; no fix passes are spent |
| `no_test_runner` on a non-Python repo | No manifest or test script the table above recognizes; add the standard runner to the repo's manifest |
| `opencode stalled: no output within the stall window` | OpenCode went silent (hung model call or tool) for `CODING_BACKEND_STALL_SECONDS`; it was killed. Check the end of the OpenCode log for the last action |
| Inbox shows a **Stuck step** after `barrier_blocked: code_review_blocked` or a QA block | The revision budget ran out. Choose revise again (fresh budget of `max_revisions`), accept as is, or stop; see "Code review" in `governed-delivery-flow.md` |
| Stuck at "OpenCode queued" | `coding-launcher` is not running, the worker image is missing, or `coding_max_running` jobs are already running. Check `docker logs` of the launcher for `coding_job_launch_failed` |
| `coding job could not start: ...` | The launcher failed to start a worker three times (image missing, Docker socket not mounted) and the work order blocked. Fix the launcher, then start the work order again |
| `transport_error: coding job lost: no heartbeat ...` | The worker died or lost the memory API. The runner retries; check the worker's logs if it repeats |
| `test runner install failed` / `pip install ... failed` in `log_tail` | Wheels missing from the image or a broken requirement; the tests then fail with the import error |

## Related

- [governed-delivery-flow.md](governed-delivery-flow.md) section B2
- Runtime config: `implementation.coding_backend`, `implementation.repos.implement_work_orders`
