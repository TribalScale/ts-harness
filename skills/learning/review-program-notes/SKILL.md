---
name: review-program-notes
description: Turn evidence from a finished program into short proposed notes. Prefer an edit of an existing note over a near-duplicate. Suggest a skill when the lesson needs numbered steps.
type: workflow
category: learning
allowed_agents: [ops-agent]
disable-model-invocation: false
always_inline: true
approval_status: approved
version: 1.0.0
---

# Review program notes

Use this skill on the `propose_notes` stage of `review_program_notes`.

Read `input.evidence`, `input.project_notes`, `input.org_notes` and `input.similar_notes`. Propose nothing when the evidence does not teach a reusable lesson.

Return JSON `{"proposals": [...]}`. Each proposal is one of:

- `add` — a new note. Default `scope` is `project`. Set `scope` to `org` only when `input.similar_notes` shows the same lesson in another project, and include those programs in `evidence`.
- `edit` — the evidence changes an existing note. Set `revision_of` to that note's id and `base_version` to its version. Prefer this over a new note with the same title.
- `retire` — the evidence contradicts an existing note. Set `revision_of`.
- `promote` — the same lesson already exists in this project and in `input.similar_notes`. Set `note_id` and evidence refs for at least two programs.
- `suggest_skill` — the lesson needs numbered steps. Put the procedure in `markdown` with frontmatter `name`, `description`, `category: learning`, and `version`.

A note `title` is at most 120 characters and `body` at most 300. `kind` is `avoid` or `howto`. `evidence` entries are `{type, ref}` pointing at a program, workflow, or task. A program with no `input.project_id` can only `promote`, and only with evidence from two programs.
