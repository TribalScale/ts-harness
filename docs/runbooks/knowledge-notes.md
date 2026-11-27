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
then by hybrid recall against the run's text (see below), then `avoid` before
`howto`, then newest. The default budget is about 3000
characters or 15 notes. Org notes keep about a third of that budget. Unused
room goes to the other scope, so a run with no project still receives the org
notes that fit.

### Hybrid recall

When a task is dispatched, the harness sends the program's summary and the head
of the work order with the context request (`GET /notes/context?query=`). The
memory API ranks the active notes against that text twice: Postgres full-text
search (`search_tsv`, weighted title, body, why) and cosine similarity of the
note embeddings (pgvector). The two rankings are fused with reciprocal rank
fusion (`1 / (60 + rank)` summed over both lists). The fused score orders notes
after the `applies_to` match and before kind and recency; the budget is
unchanged.

Embeddings use the memory service's own provider (`scripts/embed.sh`,
`EMBEDDINGS_PROVIDER`, `vector(1536)` like `memories.embedding`). A note is
embedded when it is created or changed (best effort, after the write commits),
and `draft_failure_notes` embeds up to 25 notes with no embedding or with text
that changed since (`embedding_hash`) every hour, which also backfills older
notes. `POST /notes/embed-pending` does the same on demand. If embedding fails or
`KNOWLEDGE_NOTES_VECTOR=off`, recall is full-text only; if both fail the run
gets the plain `applies_to`/kind/recency order. Recall never fails a run.

`GET /notes/search?q=&scope=&project_id=&status=&kind=&source=` is the explicit
search (the console's Memory page uses it through
`GET /admin/knowledge/notes`). It returns `mode` `hybrid`, `text` (no embedding
available) or `list` (no `q`: newest first).

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

Open **Memory** in the console to search and filter every note you can read,
see a note's evidence and history, and approve, edit or archive it (see
`admin-console.md`). Open **System → Org notes** for the org list, including how many runs used each
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

## Drafts from failed runs

The system job `draft_failure_notes` runs hourly. Its first run stores a cursor
and drafts nothing. Later runs look at programs that finished since the cursor
and have no `notes_drafted_at` stamp, and find episodes where something failed
and a later attempt fixed it:

- the same code review or QA finding came back in two rounds, and a later
  revision passed that gate (accepting the step over its findings does not
  count);
- CI failed on the PR and passed after a revision;
- a step stopped for a person, and a retry or revise with a person's guidance,
  or the stuck-step arbiter's retry or revise, got it through;
- a coding run failed for a reason in the work (not a signal, timeout, lost
  worker or provider error) and a later run of the same step succeeded.

For a program with episodes the revision diagnosis model
(`TS_HARNESS_REVISION_DIAGNOSIS_MODEL`, else the PM agent's model) writes at
most two reusable lessons in the form "In org/api, X; do Y", with kind and
`applies_to`. Each becomes a proposed note with `source` `drafted`, a project
note unless the lesson holds for any repository (then an org note, which only an
admin sees in the Inbox). Evidence links the run (`drafted_from`, with the PR),
the program, the workflow and the tasks. A draft is skipped when an active or
proposed note in the same project or the org has the same title, shares at least
half its words, or has an embedding similarity of 0.9 or more.

Drafts are never activated: they wait in the Inbox and on the Memory page like
any other proposal. Each program is stamped once it is looked at, so it is
drafted from once. The job makes at most three model calls per run
(`TS_HARNESS_FAILURE_DRAFTS_MAX_CALLS`); programs it did not reach wait for the
next run. A model error leaves the program for the next run. With no diagnosis
model configured, or `TS_HARNESS_FAILURE_DRAFTS=off`, it drafts nothing (the
embedding backfill still runs).
