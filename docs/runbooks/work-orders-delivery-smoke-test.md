# Work Orders Delivery Smoke Test

Repeatable golden-path sign-off for the **work orders** planning track.

## Prerequisites

- Runtime deployed with migration 019+ and memory-gateway healthy
- dev-agent has git/GitHub access for `verified-merge`
- Fixture PRD: `tests/fixtures/work-orders-smoke/prd-checkers.md`

## Automated (CI)

```bash
python3 -m pytest tests/runtime/test_work_orders_delivery_smoke_contract.py tests/runtime/test_delivery_program_fork.py -q
python3 scripts/release-gates.py  # includes work_orders_delivery_smoke
```

Optional wrapper:

```bash
./scripts/ops/work-orders-delivery-smoke.sh
```

## Manual staging checklist

| Step | Action | Pass criteria |
|------|--------|---------------|
| 1 | Start governed PRD for checkers fixture | `prd_request` linked; intake completes |
| 2 | Draft/revise PRD | `draft_prd` succeeded |
| 3 | Approve PRD gate | Orchestrator asks Jira vs work orders |
| 4 | Reply *plan work orders* | `work_orders_from_prd` materialized; `planning_track=work_orders` |
| 5 | Complete Refinery/Foundry/Planner gates | Planner task output has `work_order_ids`; program has `planner_complete` |
| 6 | Complete `prepare_work_order_delivery` | `next_work_order_id=WO-001`, `work_orders_handoff=true` (JSON handoff, no manifest required) |
| 7 | QA gate passes | Slack asks to merge the PR (not a generic continue footer) |
| 8 | Merge PR (or inject `github.merge`) | `await_human_merge` succeeds |
| 9 | `program_status` | `completed_work_order_ids` includes WO-001 |
| 10 | All WOs done | Slack: implement complete; program is terminal (CI/CD on merge) |

## Sign-off

Feature is done when **CI gate + one manual staging run** both pass.
