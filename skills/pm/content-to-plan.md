---
name: content-to-plan
description: Compare pasted content to an allowlisted repo, ask clarifying questions one at a time, and draft a plan only after the PM says ready.
---

# Content to plan

Use this skill on `content_to_plan` stages.

## pick_repo

List configured `repo_picker.repos`. If the list is empty, ask for `org/repo` and **fail closed** unless an operator added it. If there is one repo, still confirm it. Record `repo` and `default_branch` (omit → `dev`).

## compare_codebase

`input.content` is the pasted content, in the requester's words. Compare it to the chosen repo at `default_branch`. Note overlaps, gaps, and existing modules. Do not invent a repo.

## clarify

Ask **one question at a time**. The PM may answer several in one message. Do **not** draft the plan until they say `ready` or `draft the plan`. You may say you have enough, but they decide.

## draft_plan

Write a markdown plan as `prd_markdown` so `work_orders_from_prd` can consume it as `approved_prd`. Include repo and base branch. Stay inside the clarified scope.
