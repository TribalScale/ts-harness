---
name: investigate-bug
description: Read-only diagnosis of a Slack bug from the tree and file bodies already fetched. Cite only those files. Unknown cause is a valid result.
type: workflow
category: review
allowed_agents: [review-agent]
disable-model-invocation: false
always_inline: true
approval_status: approved
version: 1.2.0
---

# Investigate bug

Use this skill on the `investigate` stage of `bug_to_plan`.

`input.report` is the reporter's own words. Read it, `input.tree`, `input.directories` and `input.files` only. `input.tree` puts the paths the report names first, then the paths closest to its words. It is the whole repo unless `input.tree_truncated` is true; then a path missing from it may still exist. `input.directories` shows the repo's layout. `input.files` holds the bodies of the named paths, or of the closest matches when the report names none (`source` is `named` or `matched`). Do not clone the repo, search the rest of the tree, call `run_coding_backend`, open a pull request, post a status, or edit code.

Cite only paths whose bodies are in `input.files`. If those files do not explain the bug, say the cause is unknown and name what is missing. Return `diagnosis` as a short markdown string.
