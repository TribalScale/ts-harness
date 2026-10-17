# Reference

## External Skill Dependencies

The workflow frontmatter lists dependencies for discovery and validation. Invoke each skill only at the point described by the pipeline, not as a blind sequence.

### In-Repo Adapters

| Skill | Invoked at |
|---|---|
| `source-work-order` | Step 1: resolve adapter and fetch Work Order context |
| `ts-source-jira` | Step 1: resolve adapter and fetch Jira context |
| `ts-pr-description` | Step 7: PR body generation |
| `ts-security-audit` | Step 6: risk-based audits when configured |
| `ts-dependency-audit` | Step 6: risk-based audits when configured |
| `ts-accessibility-audit` | Step 6: risk-based audits when configured |
| `ts-performance-review` | Step 6: risk-based audits when configured |
| `ts-test-audit` | Step 6: risk-based audits when configured |
| `ts-operational-readiness` | Step 6: operational readiness when configured or new service |
| `ts-node-backend-scaffold` | Step 5: new backend bootstrap when detect() matches |
| `ts-frontend-scaffold` | Step 5: new frontend bootstrap when detect() matches |
| `ts-cicd-scaffold` | Step 5: net-new CI/CD setup when detect() matches |

### Required in Normal Flow

| Skill | Source | Invoked at |
|---|---|---|
| `superpowers:using-git-worktrees` | superpowers plugin | Step 4: isolated workspace |
| `superpowers:test-driven-development` | superpowers plugin | Step 5: behavior changes |
| `superpowers:verification-before-completion` | superpowers plugin | Step 6 and before readiness claims |
| `new-branch-and-pr` | cursor-team-kit | Step 7: open PR |
| `review-and-ship` | cursor-team-kit | Step 8: agent review |
| `loop-on-ci` | cursor-team-kit | Step 9: watch CI |

### Conditional Dependencies

| Skill | Source | Invoked when |
|---|---|---|
| `superpowers:brainstorming` | superpowers plugin | Acceptance criteria are ambiguous |
| `superpowers:writing-plans` | superpowers plugin | Multi-step implementation |
| `superpowers:subagent-driven-development` | superpowers plugin | Independent parallel tasks |
| `superpowers:executing-plans` | superpowers plugin | Tightly coupled tasks or no subagents |
| `superpowers:systematic-debugging` | superpowers plugin | Bugs, test failures, CI failures |
| `superpowers:requesting-code-review` | superpowers plugin | Major features need agent review |
| `superpowers:receiving-code-review` | superpowers plugin | Human review feedback arrives |
| `fix-ci` | cursor-team-kit | CI checks fail |
| `babysit` | cursor built-in | Keeping a PR ready while waiting |
| `get-pr-comments` | cursor-team-kit | PR comments need to be fetched |
| `check-compiler-errors` | cursor-team-kit | Compile or type-check failures |
| `run-smoke-tests` | cursor-team-kit | User-facing flows changed |
| `verify-this` | cursor-team-kit | Before/after claims need evidence |

---

## Step 4 vs Step 7 Decision Tree

```
Does the project use git worktrees?
├── Yes (default) → Step 4: superpowers:using-git-worktrees
│                   Step 7: new-branch-and-pr (PR only, branch exists)
└── No (user prefers branch-only) → Step 4: create branch directly
                                    Step 7: new-branch-and-pr (PR only)
```

Never create the branch twice. Step 4 owns workspace isolation; Step 7 owns PR creation.

---

## Post-Merge Verification Checklist

Run these after merge, before emitting `post_merge_verified`:

1. **Sync base branch**
   ```bash
   git checkout <configured-base-branch> && git pull
   ```

2. **Confirm merged commit**
   ```bash
   git log --oneline -5
   ```

3. **Re-run test suite on base**
   ```bash
   <configured-post-merge-verification-command>
   ```

4. **Optional smoke test** — if user-facing flows changed, run configured smoke tests.

5. **Record evidence** — note commands run, pass/fail status, and commit SHA in `docs/verified-merge/delivery.json` and the output contract.

If any step fails, do not emit `post_merge_verified`. Report the failure and open a fix branch.

---

## Delivery State File

Location: `docs/verified-merge/delivery.json`

The orchestrator creates and updates this file. Adapters read it for resumability but do not replace the orchestrator as writer. See [adapter-contract.md](adapter-contract.md) for the schema.

---

## Relationship to ts-pr-description

Use `ts-pr-description` in Step 7 to generate the PR title and body from the git diff. Augment the generated PR body with:

- Unit id and title (from NormalizedDeliveryContext)
- Test plan with commands actually run (from Step 6)
- Known gaps or risks

Do not use `ts-pr-description` as a substitute for the test plan section.

---

## Relationship to prd-to-work-orders

When delivering Work Orders from `docs/planner/**`:

- Use `source-work-order` as the adapter.
- Respect execution phases and dependency graph.
- Do not edit `docs/prd-to-work-orders/manifest.json`.
- After verified merge, optionally run Validator in `prd-to-work-orders` when implementation exists.

---

## Adapter Contract

For NormalizedDeliveryContext, lifecycle events, routing rules, and adapter operations, see [adapter-contract.md](adapter-contract.md).

For Jira MCP usage, see the `ts-source-jira` adapter reference in the integrate category.
