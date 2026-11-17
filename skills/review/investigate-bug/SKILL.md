---
name: investigate-bug
description: Read-only diagnosis of a Slack bug from the tree and file bodies already fetched. Cite only those files. Unknown cause is a valid result.
type: workflow
category: review
allowed_agents: [review-agent]
disable-model-invocation: false
always_inline: true
approval_status: approved
version: 1.3.0
---

# Investigate bug

Use this skill on the `investigate` stage of `bug_to_plan`.

`input.report` is the reporter's own words. Read it, `input.tree`, `input.directories` and `input.files` only. `input.tree` puts the paths the report names first, then the paths closest to its words. It is the whole repo unless `input.tree_truncated` is true; then a path missing from it may still exist. `input.directories` shows the repo's layout. `input.files` holds the bodies of the named paths, or of the closest matches when the report names none (`source` is `named` or `matched`). Do not clone the repo, search the rest of the tree, call `run_coding_backend`, open a pull request, post a status, or edit code.

Cite only paths whose bodies are in `input.files`. If those files do not explain the bug, say the cause is unknown and name what is missing. Return `diagnosis` as a short markdown string.

## Shared sources

`input.sources`, when present, is what the requester attached: files, Google Drive documents and descriptions of images, as markdown with one section per source. Treat it as part of the report: logs, screenshots and specs there are evidence, but only paths in `input.files` may be cited as code. Name the source when you rely on it. Do not ask for anything it already states. A section marked _Could not be read_, or listed under _Not read_, is missing: ask about it once (for a Drive link, ask the requester to share it with the workspace account), then carry on without it. A section cut at the size limit is incomplete; say so when the missing part matters.
