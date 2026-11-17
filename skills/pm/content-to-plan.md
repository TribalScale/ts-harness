---
name: content-to-plan
description: Compare pasted content to an allowlisted repo, ask clarifying questions one at a time, and draft a plan only after the PM says ready.
---

# Content to plan

Use this skill on `content_to_plan` stages.

## Shared sources

On `compare_codebase`, `clarify` and `draft_plan`, `input.sources`, when present, is what the requester attached: files, Google Drive documents and descriptions of images, as markdown with one section per source. Treat it as part of the brief, alongside `input.content`. Name the source when you rely on it. Do not ask for anything it already states. A section marked _Could not be read_, or listed under _Not read_, is missing: ask about it once (for a Drive link, ask the requester to share it with the workspace account), then carry on without it. A section cut at the size limit is incomplete; say so when the missing part matters.

## pick_repo

List configured `repo_picker.repos`. If the list is empty, ask for `org/repo` and **fail closed** unless an operator added it. If there is one repo, still confirm it. Record `repo` and `default_branch` (omit → `dev`).

## compare_codebase

`input.content` is the pasted content, in the requester's words. Compare it to the chosen repo at `default_branch` using what the workflow fetched for you: `input.tree` lists file paths with the paths the content names first, then the ones closest to its words. It is the whole repo unless `input.tree_truncated` is true; then a path missing from it may still exist, so say so rather than calling it absent. `input.directories` lists the repo's directories, including areas whose files did not fit in the tree; `input.files` holds bodies of the named paths, or of the closest matches when none are named (`source` says which). You cannot clone or browse the repo yourself, so do not say you lack access; work from these. Note overlaps, gaps, and existing modules, citing paths from `input.tree`. Do not invent a repo or a path.

## clarify

Ask **exactly one question** in the reply. Do not send a numbered checklist. Do not ask for anything `input.content` or an earlier answer already states. The PM may answer several questions in one message. Do **not** draft the plan until they say `ready` or `draft the plan`. You may say you have enough, but they decide.

## draft_plan

Write a markdown plan as `prd_markdown` so `work_orders_from_prd` can consume it as `approved_prd`. Include repo and base branch. Stay inside the clarified scope.
