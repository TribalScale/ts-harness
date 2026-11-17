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

## File scope

When a work order lists its files under `## Files` (or `## Touches`), each OpenCode run gets an `OPENCODE_CONFIG` in the job workspace, outside the repository, with `permission.edit` set to deny everything except:

- the listed files (a path ending in `/` covers its directory);
- `features/**` when the work order has Given/When/Then acceptance tests;
- the paths the pipeline writes (`.opencode/**`, `docs/planner/**` and the other planning docs);
- the files a review revision cites.

OpenCode refuses an edit outside the list with a message naming the rule, and works within it. After the run, files changed outside the list some other way (for example from the shell) are still reverted before verification, and the fix pass is told which ones. A work order without a `## Files` list is not restricted.

## Coding jobs (OpenCode outside agent-runtime)

With `coding_jobs_enabled: true` (env `CODING_JOBS_ENABLED=true`), the deliver step of `deliver_work_order` (1.5.0 and later) and `implement_jira_ticket` (1.7.0 and later) runs no agent turn at all: the stage has `"execution": "coding_job"`, and dev-agent is not called. A run that fails waits for a person (`/revise WO-n <guidance>` or `/stop WO-n`).

1. The runner builds the job spec from the task input (repo, branch, PR, feedback, refresh) and queues a coding job.
2. The task parks as `blocked` with `awaiting_external:coding.finished`, and the runner thread is free. The console shows "OpenCode queued" until a worker starts, then "OpenCode implementing" with run time and the latest log line.
3. `coding-launcher` claims queued jobs and starts one `ts-harness-coding-worker` container per job with `docker run --rm` and CPU, memory and pids limits. The local launcher runs as many jobs at once as the host's memory fits, never more than `coding_max_running` (default 3): (host memory − `CODING_HOST_RESERVED_MEMORY`, default 2.5 GB) ÷ `CODING_WORKER_MEMORY` (default 2 GB). A t3.medium (4 GB) runs one, a t3.large (8 GB) two, a t3.xlarge (16 GB) three. The launcher logs the result as `coding_launcher_capacity` when it starts.
4. The worker fetches its spec with a job token that only opens that job's spec, heartbeat and complete endpoints. It heartbeats every 30 seconds with the last line of the OpenCode log, then posts the result and exits.
5. The worker reports OpenCode's token usage with its result, and memory-api writes it to `llm_usage` and `agent_token_usage` as `coding-backend` (unless the LLM proxy already metered it). Completing the job puts the task back to pending. The runner checks the result against the stage contract exactly as it checks an agent's output; a failed run blocks with `deliver_work_order cannot succeed when run_coding_backend failed: ...` and revision and retry policy apply as before.

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
| `package.json` with a `test` or `test:unit` script | node | `<pm> test` / `<pm> run test:unit`, where `<pm>` is npm, pnpm or yarn (see below) | `<pm> run test:bdd` / `cucumber` / `bdd` script |
| `go.mod` | go | `go test ./...` | `.feature` suites (godog) run inside the unit command |
| `Cargo.toml` | cargo | `cargo test` | inside the unit command |
| `mvnw` or `pom.xml` | maven | `./mvnw -B test`, else `mvn -B test` | inside the unit command (Cucumber JVM) |
| `gradlew`, `build.gradle`, `build.gradle.kts` | gradle | `./gradlew test`, else `gradle test` | inside the unit command |
| `pytest.ini`, `[tool.pytest` in `pyproject.toml`, `[tool:pytest]` in `setup.cfg`, `[pytest]` in `tox.ini`, `conftest.py`, or `.py` files under `tests/` | python | `<venv>/bin/python -m pytest` | `behave.ini`, `features/environment.py`, or `features/steps/*.py`: `python -m behave` |
| `Makefile` with a `test:` target | make | `make test` | n/a |

A bare `tests/` or `features/` directory does not select a stack. Multi-stack monorepos use the first match.

**Node package manager.** The `packageManager` field in `package.json` wins, then the lockfile: `pnpm-lock.yaml` is pnpm, `yarn.lock` is yarn, anything else npm. pnpm and yarn run through corepack (shipped with Node); the host puts `pnpm` and `yarn` shims on the work order's `PATH`, so OpenCode uses the same tool.

**Type check and lint (Node).** After the tests, verification runs the repo's type check: its `typecheck`, `type-check`, `check-types`, `types:check` or `tsc` script, else `node_modules/.bin/tsc --noEmit -p tsconfig.json` (skipped for a references-only tsconfig). With an ESLint config and `node_modules/.bin/eslint`, it lints the files the work order changed, not the whole repo, so existing lint debt does not block. Failures are `typecheck_failed` and `lint_failed` and go to the fix passes like test failures.

**Node version.** The repo's pin (`.nvmrc`, `.node-version`, `.tool-versions`, `volta.node`, then `engines.node`) decides which Node runs. When the image's Node does not satisfy it, the newest matching release is downloaded from nodejs.org (checked against its `SHASUMS256.txt`) into the shared cache and put first on `PATH`. An exact pin is met by the same major, so `.nvmrc` 24.14.1 keeps the image's 24.x.

**Databases and integration tests.** The coding worker image ships Postgres and Redis (`implementation/test_services.py`). A job starts the ones the repo uses on loopback: Postgres when it depends on `pg`, `postgres`, MikroORM's Postgres driver, Prisma with `provider = "postgresql"`, `psycopg`/`asyncpg`, or a compose file with a Postgres image; Redis for `ioredis`, `redis`, `bull`/`bullmq` or a Redis image. It creates the role and database the repo's env template names (`.env.test`, `.env.example`, `env-example*`, ...), writes `.env` from that template with compose host names pointed at `127.0.0.1` (only when `.env` is absent; it never reaches the PR), and exports `DATABASE_URL`, `PG*` and `REDIS_URL`. MySQL and MongoDB are not in the image; the prompt says so and those tests run only in CI.

The integration suite runs after the unit tests, type check and lint pass, with the databases dropped and recreated first. Declare it in `.ts-harness.yml` at the repo root:

```yaml
services: [postgres]                  # detected when omitted
env_file: env-example-relational      # detected when omitted
integration:
  setup: [npm run build, npm run migration:run, npm run seed:run:relational]
  background:                         # started in order, each waited for, stopped afterwards
    - run: npx --yes maildev@2.0.5 --smtp 1025 --web 1080 --ip 127.0.0.1
      wait_for: 127.0.0.1:1080        # host:port or an http(s) URL
    - run: npm run start:prod
      wait_for: 127.0.0.1:3001
  command: npm run test:e2e -- --runInBand
  timeout_seconds: 1200
```

A declared suite blocks: a failure is `integration_tests_failed` and goes to the fix passes. Without the block, a `test:integration`, `test:int` or `test:db` script runs when a database is up, and a failure is only a note in the evidence. OpenCode runs the same suite with `ts-harness-integration`.

**Coverage.** When the work order mentions coverage (or `TS_HARNESS_COVERAGE=always`), a passing verification also runs the tests with coverage (Jest, Vitest with `@vitest/coverage-v8` or `-istanbul`, pytest with pytest-cov), with the repo's thresholds off. The overall line percentage and each changed source file's go into `verify_summary`; QA compares them with the targets. Coverage never fails verification.

**Frontend (React) repos.** A repo with `react-dom`, `next`, or Vite with React is a web app:

- Verification also runs its `build` script after the type check and lint. Bundlers catch what `tsc` misses: unresolved imports, server/client boundaries, env access at build time.
- A repo that uses Vitest runs with `CI=true`, for OpenCode and the host alike. A bare `vitest` script otherwise watches and never exits.
- Every host command has a 30-minute limit (`TS_HARNESS_VERIFY_COMMAND_TIMEOUT`), so a hanging test fails the check instead of the job.
- The coding worker has Chromium's libraries and a preinstalled Playwright browser under `PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright` (version in `docker/Dockerfile.coding-worker`). After installing dependencies, the host runs the repo's own `playwright install chromium`, which downloads only when the repo pins a different Playwright.
- With a `playwright.config.*`, the Playwright suite runs as the integration suite (`--reporter=line`; the HTML report never opens a server). It's advisory unless `.ts-harness.yml` declares it. Declare it when the suite needs a mock API or other background process:

```yaml
env_file: .env.example-e2e        # written to .env even without databases
integration:
  background:
    - run: yarn run-mock-server
      wait_for: 127.0.0.1:8080
  command: yarn playwright test --reporter=line
```

- Before each verification, the worker stops every process OpenCode left running (a `yarn dev &`, a mock API), except the job's databases. Otherwise the suite's own web server finds its port taken. The local launcher starts workers with `docker run --init`, so finished processes are reaped.

**MySQL and MongoDB.** Besides Postgres and Redis, the worker ships MariaDB (MySQL-compatible) and MongoDB.

- **MariaDB** starts for `mysql2`, `mysql`, `mariadb`, MikroORM's MySQL drivers, Prisma `provider = "mysql"`, `pymysql`/`mysqlclient`, or a `mysql`/`mariadb` compose image. The role and database come from the template, including `MYSQL_*` keys and `MYSQL_ROOT_PASSWORD` for `root`. The job exports `MYSQL_URL` and `MYSQL_*`, and `DATABASE_URL` when there is no Postgres.
- **MongoDB** starts for `mongodb`, `mongoose`, Typegoose, Prisma `provider = "mongodb"`, `pymongo`/`motor`, or a `mongo` image. It runs as a one-node replica set `rs0`, which Prisma and transactions need, without auth. Local `mongodb://` URLs in the template are rewritten to it without credentials. `mongodb+srv` and remote URLs stay as they are. The job exports `MONGODB_URI`/`MONGO_URL`.
- Each integration run drops and recreates the databases. Elasticsearch, Kafka and RabbitMQ are reported as unavailable.

**Monorepos.** With npm, yarn or pnpm workspaces (and Turborepo or Nx on top), checks follow the changed packages:

- **Lint:** changed files are grouped by the nearest ESLint config and linted from that directory.
- **Type check:** a root type-check script runs once. Otherwise each changed package runs its own script, or `tsc --noEmit` on its tsconfig.
- **Build:** changed web-app packages build through `turbo run build --filter=<pkg>` (dependencies first), or the workspace tool.
- **Playwright:** a changed package's Playwright config is found and run from that package.
- **Unit tests:** the root `test` script (for example `turbo run test`) runs as before.

**Screenshots.** After the declared or detected Playwright suite passes, the host reruns only the specs the change added or edited, with traces and screenshots on. From each trace it keeps the page as it was right after an assertion passed (`step-N`, the frame painted before the next action). That's up to 4 frames per test, spread across its assertions, and they show what the test checked. A screenshot at the end of a test usually shows the state after cleanup. The end-of-test image is the fallback. It uses the repo's own config (web server, setup project, storage state) through a temporary wrapper. The images are published three ways:

- **Evidence branch:** up to 6 images go on `ts-harness-evidence/<work branch>`, one parentless commit built with git plumbing, so the work branch is untouched.
- **PR comment:** a single **Screenshots** comment, updated on revisions, links them by commit.
- **QA:** QA receives the same images as image input.

The evidence branch is deleted when the PR merges. A change without Playwright specs has no screenshots; QA treats UI criteria as `unverified` rather than failed.

**Large repos.** In a repo with more than 120 test files (`TS_HARNESS_RELATED_TESTS_THRESHOLD`), host verification runs only the tests related to the changed files: `vitest related --run <files>` or `jest --findRelatedTests <files>`, keeping the arguments of the repo's test script. CI still runs the full suite on the PR, and the evidence says which ran.

**Formatting.** When CI runs a Prettier check (in a workflow, or through a package script a workflow calls), the host runs `prettier --check` on the changed files too. A repo whose CI does not enforce formatting is not held to it.

**Memory.** When the kernel kills OpenCode, or a verification command such as `next build`, for memory, the job fails as `out_of_memory`, not as a code failure. The runner then queues it again with twice the memory, up to `CODING_WORKER_MAX_MEMORY` (default 6g).

- **Remembered size:** new jobs for a repository start with the largest memory any recent successful job for it needed.
- **Launcher limit:** the local launcher only starts a job when its memory fits beside the running workers, within host memory minus `CODING_HOST_RESERVED_MEMORY`.
- **ECS:** size projects with `worker_memory_mb`.

**BDD.** Gherkin feature files are required only in a repo that already uses them (feature files, a BDD script, or a Cucumber dependency) or has no test setup at all. A repo with its own runner and no Gherkin (a NestJS app with Jest, a Go service) covers a work order's Given/When/Then acceptance tests with its own tests; the agent is told not to add Cucumber there.

Dependencies installed before OpenCode runs and again before each verify (only when a manifest changed):

- Python: `requirements.txt`, `requirements-dev.txt`, `requirements-test.txt`, and `pip install -e .` for a `pyproject.toml` with `[project]`. Always into the work order's venv, never the runtime's site-packages.
- Node: `npm ci`, `pnpm install --frozen-lockfile`, or `yarn install --frozen-lockfile` (`--immutable` for yarn 2+) when the lockfile exists, else a plain install.
- Go: `go mod download`. Cargo, Maven and Gradle fetch during the test run.

Where things live, per delivery under `/workspace/dev-agent/coding-backend/<WO>-<suffix>/`:

- `.ts-harness/venv/`: virtualenv with pytest and behave installed offline from `/opt/ts-harness/wheels` (pins in `docker/test-runner-requirements.txt`).
- `.ts-harness/deps.json`: manifest hashes of the last successful installs.
- `../.cache/`: shared Go, npm, Cargo, Gradle and Maven caches, reused across work orders.
- `repo/.git/info/exclude`: `node_modules/`, the stack's build output (`target/`, `.gradle/`, `build/`), the harness's own `.opencode/` skills and config, and Python bytecode, so none of them reach the team's pull request. The repo's `.gitignore` is never edited.

OpenCode runs with the same `PATH` and venv, so its own test runs match host verification. The agent-runtime image ships Go, Node.js LTS, JDK 21 with Maven and Gradle, Rust (minimal profile), make and gcc; bump versions with their checksums in `docker/Dockerfile.agent-runtime`.

## Review and QA after the base branch moves

A merge into the base branch sends every open PR on it through refresh, CI, code review and QA again. `review_precheck` records a `diff_fingerprint` (each PR file's name, status and head blob sha). When it matches the fingerprint of the last code review or QA run that passed, the runner reuses that verdict (`carried_forward` in the step output, `verdict_carried_forward` in the runner log) instead of running the agent again; the `harness/qa` status is posted on the new head. CI still runs on the merged head. A conflict resolution, any change to a PR file (including one the base branch touched), a failed or blocked last verdict, or a human change request runs the agent. Set `TS_HARNESS_CARRY_FORWARD_VERDICTS=0` on the workflow runner to always rerun.

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

- [onboard-a-repo.md](onboard-a-repo.md): `.ts-harness.yml` by work order, branch protection, first work orders.

- [governed-delivery-flow.md](governed-delivery-flow.md) section B2
- Runtime config: `implementation.coding_backend`, `implementation.repos.implement_work_orders`
