# Onboard a team repository

An **empty** repository (only a README, `.gitignore` or LICENSE) needs none of this: TS Harness
starts it from a template with a bootstrap PR. See [repo-templates.md](repo-templates.md).

Three steps per repository, about ten minutes of a person's time. Run the scripts inside the
agent-runtime container (`sudo docker exec -it docker-agent-runtime-1 bash` on a Compose
host); they use its GitHub and memory-api credentials.

## 1. Let TS Harness write `.ts-harness.yml`

**Programs do this on their own.** The first time a program reaches the implement phase, it
checks every repository its work orders target (the project's repositories, or the configured
default). A repository whose CI runs an e2e or integration suite, or that has a Playwright
config, but has no `.ts-harness.yml` gets an onboarding work order, `WO-0000` (then `WO-0001`,
... for further repositories). That repository's other work orders depend on it, so feature work
starts once the team merges the config PR. Work orders for other repositories are not held up.
Repositories that already have the file, or have no suite to run, are skipped. The program's
`repo_onboarding` artifact records what was found for each repository. A repository that cannot
be checked (for example, a clone failure) is recorded there and skipped, never blocking the program.

Run the script yourself to set a repository up before any program targets it, or to preview
the draft:

```bash
python3 /app/scripts/onboard-repo.py https://github.com/acme/api --base dev --draft-only   # look first
python3 /app/scripts/onboard-repo.py https://github.com/acme/api --base dev               # start it
```

The script drafts the file from what the repo already runs in CI:

- the e2e or integration job's commands
- for Docker Compose jobs, the startup script the container runs
- the env template CI copies to `.env`
- databases from dependencies and compose images
- the Playwright config

It then starts a work order, `WO-ONBOARD`. OpenCode runs `ts-harness-integration` in a coding job and fixes the file until the suite passes. Docker-only steps are translated into local processes, for example a mail catcher through `npx`.

The host only opens the PR when the declared suite passes. The PR goes through code review and QA like any other. Review it, merge it, and from then on every work order runs that suite before its PR opens.

Without the file, coding jobs still detect databases, and a detected Playwright suite or `test:integration` script runs as advisory evidence. Only a declared suite blocks.

## 2. Check and set branch protection

```bash
python3 /app/scripts/repo-governance.py https://github.com/acme/api --branch dev            # report
python3 /app/scripts/repo-governance.py https://github.com/acme/api --branch dev --apply    # set it
```

The report shows:

- whether the branch is protected
- which CI workflows actually run on pull requests into it (a workflow limited to `main` does not count)
- the CI job names
- what is required now, and what is missing

`--apply` requires these checks:

- the CI jobs that run on PRs into the branch
- `harness/code-review`, `harness/qa` and `harness/security-scan`

It also requires one approval and dismisses stale approvals. Stricter existing settings are kept: more approvals, code owners, admin enforcement, extra checks.

It needs the GitHub App's **Administration: Read and write** permission, accepted by an org owner on the installation (see [GitHub App → Changing the App's permissions](github-app.md#changing-the-apps-permissions)), or a repo admin. Without it, the script prints the `gh api` command an admin can run with `--print-payload`.

**PRs people open by hand.** On a branch that requires the harness checks, the GitHub webhook posts `success` ("Not a TS Harness PR") on those checks for PRs that are neither on a `wo/` branch nor recorded by a program. Those PRs then follow the repo's CI and approval rules. This needs the App's webhook with the `pull_request` event ([GitHub App → Webhook](github-app.md#webhook)); check it with `python3 /app/scripts/github-app-webhook.py`. Hosts that only poll do not see new PRs, so an admin merges them.

**Repos with no CI on pull requests.** The report flags it: PRs would merge on harness checks alone. Add a workflow that runs on `pull_request` into the branch.

## 3. First work orders

Pick two or three small work orders the team knows well. Watch the first PRs:

- the `verify_summary` in the delivery output
- the code review and QA comments
- for frontends, the **Screenshots** comment

## What runs in a coding job

| Repo | Services and tools |
|---|---|
| Postgres, MySQL/MariaDB, MongoDB (one-node replica set), Redis | Started on loopback from dependencies, Prisma, compose images or `services:` |
| Elasticsearch, Kafka, RabbitMQ, LocalStack, MinIO, ... | Not available; the agent is told, and those tests stay CI-only |
| Playwright | Chromium preinstalled; the repo's own version is fetched when it differs |
| Monorepos (npm/yarn/pnpm workspaces, Turborepo) | Lint, type check, build and Playwright per changed package |

See [implement-work-orders-opencode.md](implement-work-orders-opencode.md) for the details.
