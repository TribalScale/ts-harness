# Jira to PR

Engineer program: list my assigned tickets in a project, implement one, PR against **`dev`** unless a pointer or override says otherwise.

## Start

Signals: `my tickets`, `pick up my jira`, `implement the rest`, `implement all`, and `implement` followed by a Jira key in any project (`implement PROJ-123`; pattern `implement [a-z][a-z0-9]+-\d+`).

`implement the rest` and `implement all` enter at `pick` and build one ordered list of this person's assigned tickets in the project they confirm. Approving `pick_ticket` confirms that list. Each ticket still stops at `hydrate_ticket`, and the next ticket starts only after the pull request merges. The queue is serial, including across repos, and holds at most 50 keys.

A ticket with an open blocker outside that list is left out, and the exclusion names those keys. Blockers that are also in the list run first. A cycle is left out. A named key (`implement PROJ-123`) still runs that ticket and stops.

Rejecting `hydrate_ticket` or `pick_ticket` during a queue stops the queue and names the keys that did not finish. A failed ticket does the same. A merge wait or a QA revision holds the queue. Cancelling the program is unchanged.

While unfinished queue keys remain, `next ticket`, `show my tickets`, and `remaining tickets` do not open the picker. A live routing overlay that copies these intents needs the same two phrases; this file's runtime yaml is the source when a server has not overlaid them.

```text
propose_start_program(program_type=jira_to_pr, entry_phase=pick)
```

With a key: `entry_phase=implement` and `artifacts.jira_issue_key=PROJ-123`.

The kickoff (`hydrate_ticket`) is a human gate. Approving it starts implementation in `deliver_work_order` as the dev agent.

Between the kickoff and the coding job, `prepare_ticket_delivery` (no agent) gets the ticket ready:

- **Design references**: those of the program the ticket points at, and the ticket's image
  attachments (see [Design references](design-references.md)).
- **Repository onboarding**: the same check as planned work orders. When the repository's CI runs
  an e2e or integration suite and it has no `.ts-harness.yml`, the work order also asks the coding
  job to add the file, so the ticket's PR sets the repository up. A queue runs one ticket at a time, so
  only the first ticket's PR adds it. Two tickets started separately on the same repository before
  that PR merges both add the file, and the second PR conflicts on it: keep the merged version.

A lookup that fails (no Jira credentials, clone error) is listed in the step's `warnings`; the
ticket still runs.

## Identity

Requires `identity.slack_user_emails` plus `identity.jira_account_ids` (email → accountId).
Edit those maps in **System → Settings → Identity** (`#/system/settings/identity`).
See [system-settings.md](system-settings.md). Unmapped users must paste a ticket key.

## Ticket list

`assignee = me AND project = X AND status IN (To Do, Open, Ready for Dev, In Progress)`.

Statuses are configurable on `config.ticket_picker.statuses`, or inherited from
tenant Settings when the program omits the section / sets `mode: inherit`.
Not `statusCategory != Done`. All assigned tickets are shown, grouped by epic.
Blocked is hidden; an explicit key still works with a warning.

## PR base branch

1. Engineer names a branch.
2. Pointer `Repo: org/repo@branch`.
3. Allowlist `default_branch` (omit → `dev`).
4. Runtime `implementation.repos.implement_jira_ticket.base_branch` if the URL matches.
5. `dev`.

Confirm the base in kickoff. After a single ticket merges, re-show remaining tickets. A confirmed queue continues with the next key.
