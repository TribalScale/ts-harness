# Jira to PR

Engineer program: list my assigned tickets, implement one (or a project's queue), PR against **`dev`** unless the ticket's `Repo:` line or the request names another branch.

## Start

Signals: `my tickets`, `pick up my jira`, `implement the rest`, `implement all`, and `implement` followed by a Jira key in any project (`implement PROJ-123`; pattern `implement [a-z][a-z0-9]+-\d+`).

`show my tickets` enters at `pick`: `prepare_ticket_queue` (no agent) lists the person's assigned tickets in every project, grouped by project and epic. They reply to `pick_ticket` with a key (`THD-33`) to implement it, or `all THD` to implement every ready ticket in THD in order. `implement the rest` and `implement all <PROJECT>` freeze that ordered list straight away, and the reply confirms it. Implementation starts as soon as they reply; the next ticket in a queue starts only after the pull request merges. The queue is serial, including across repos, and holds at most 50 keys.

A ticket with an open blocker outside that list is left out, and the exclusion names those keys. Blockers that are also in the list run first. A cycle is left out. A named key (`implement PROJ-123`) still runs that ticket and stops.

Rejecting `pick_ticket` during a queue stops the queue and names the keys that did not finish. A failed ticket does the same. A merge wait or a QA revision holds the queue. Cancelling the program is unchanged.

While unfinished queue keys remain, `next ticket`, `show my tickets`, and `remaining tickets` do not open the picker. A live routing overlay that copies these intents needs the same two phrases; this file's runtime yaml is the source when a server has not overlaid them.

```text
propose_start_program(program_type=jira_to_pr, entry_phase=pick)
```

With a key: `entry_phase=implement` and `artifacts.jira_issue_key=PROJ-123`.

There is one confirmation: the yes that starts the program (or the pick reply). To target another branch, name it in the request: `implement THD-33 on branch release/2`.

`prepare_ticket_delivery` (no agent) reads the ticket, posts what it found, and the coding job
(`deliver_work_order`) starts:

- **The ticket**: its summary and description become the work order.
- **Repository and branch**: the description's `Repo: org/repo@branch` line, else
  `implementation.repos.implement_jira_ticket` (see PR base branch below). A ticket with
  neither blocks with what to add.
- **Design references**: those of the program the ticket was filed from (`Program:` line), and
  the ticket's image attachments (see [Design references](design-references.md)).
- **Repository onboarding**: the same check as planned work orders. When the repository's CI runs
  an e2e or integration suite and it has no `.ts-harness.yml`, the work order also asks the coding
  job to add the file, so the ticket's PR sets the repository up. A queue runs one ticket at a time, so
  only the first ticket's PR adds it. Two tickets started separately on the same repository before
  that PR merges both add the file, and the second PR conflicts on it: keep the merged version.

A lookup that fails (attachments, clone error) is listed as a note; the ticket still runs.

## Identity

"My" tickets are the signed-in person's. The program keeps their email from the sign-in
(`requester_email`; Slack users through `identity.slack_user_emails`), and their Jira account is
`identity.jira_account_ids[email]` when set in **System → Settings → Identity**
(`#/system/settings/identity`), else the Jira user with that email (Jira's user search; an
account must show that email, or be the only person Jira finds for it). Without an account the
list says so; pasting a ticket key still works.

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
