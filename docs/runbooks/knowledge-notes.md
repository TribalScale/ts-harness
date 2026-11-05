# Knowledge notes

Reviewed, short notes that every run for a project can see. An org note applies
to every project. A project note applies to one project. A project note adds to
an org note. The one exception is a project note whose `exception_to` names an
org note: while that exception is active, that org note is left out of that
project's runs. Only an admin can approve an exception.

## What reaches a run

Only notes with status `active` are loaded. The harness fetches them when a
task is dispatched. A failure to load notes leaves the run with an empty list;
it does not fail the run. A program with no project still receives the org notes.

Notes are ranked by how well `applies_to` matches the stage and changed paths,
then `avoid` before `howto`, then newest. The default budget is about 3000
characters or 15 notes. Org notes keep about a third of that budget. Unused
room goes to the other scope, so a run with no project still receives the org
notes that fit.

Agents see them under **Org rules** and **Project notes**. The OpenCode prompt
uses the same sections. Agents are not asked to call recall for these notes.

## Who can change them

| Change | Who |
| --- | --- |
| Org notes, promotions, and exceptions | Admin (`edit_config`) |
| Project notes | Admin, or a manager of the project's owning team |
| Read | Anyone who can see the project |

A person who can approve a note creates it as `active`. Notes from an agent or
from the post-run review always start as `proposed`.

Edit an active note with compare-and-set on its version. A stale version
returns 409 and the console reloads the current note.

Open **System → Org notes** for the org list, including how many runs used each
note. Open a project's settings for its notes, the active org rules, **Promote
to org**, and (admins) an exception to an org note.

## Proposals

The Inbox type `knowledge_note` shows the proposal, its evidence, and the
current note when the proposal edits or retires one. Approve, edit, or reject
there. Approving an edit applies it only when `base_version` still matches.
Approving a promotion publishes the org note and retires the project note.
Approving a retire proposal retires the target and the proposal.

`propose_knowledge_note` is available to the orchestrator and the dev agent.
It proposes a project note only, at most three times in one run, and refuses
when the program has no project or the caller asks for org scope.

## Post-run review

The system job `review_finished_programs` runs hourly. The first run stores a
cursor and does not review history. Later runs take programs that succeeded,
failed, or were cancelled since that cursor and have not been reviewed.

A clean run is marked reviewed with no model call. A run with review findings,
QA defects, failed tasks, or rejected merges starts the `review_program_notes`
workflow. That workflow proposes adds, edits, retires, promotions, or a skill
draft. Promotions to org require the same lesson in another project. A program
with no project can only promote a lesson seen in two programs.

Project notes unused for 60 days, or whose `applies_to` paths are gone from the
repo, and org notes unused for 90 days, come back to the Inbox as retire
proposals. When the same lesson shows up in two projects owned by one team, the
job records a team candidate and writes no team note.
