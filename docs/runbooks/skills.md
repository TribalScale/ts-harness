# Creating skills

There are three ways to create a skill. Each one makes a **draft**. A draft is not usable until someone approves it in **Inbox → Skills** (`#/inbox/skill_draft`).

| Way | Where | Result |
|---|---|---|
| **Learn a skill** | **System → Skills**, or `/learn <source>` in Chat or Slack | The agent writes one `SKILL.md` draft with `propose_skill`. |
| **Paste skill** | **System → Skills → Paste skill** | One `SKILL.md` draft from pasted text. |
| **Upload folder** | **System → Skills → Upload folder** | A folder draft: `SKILL.md` plus nested supporting files. |

**Paste skill** and **Upload folder** need the `edit_config` permission. Viewers do not see the buttons.

## Paste skill

Fill in **Id**, **Category**, **Description**, and **Version** (default `1.0.0`), then paste the skill body into the text box.

If the pasted text starts with `---` frontmatter, the form reads `id`, `category`, `description`, and `version` from it. A field only fills a key the frontmatter leaves out. When a field and the frontmatter disagree, the server returns `skill_field_conflict` instead of guessing.

## Upload folder

Choose a folder, or drag one onto the drop area. The folder must have `SKILL.md` at its top level. Other files may be nested, for example `stages/plan.md` or `schemas/output/brief.json`. If every file sits under one top folder (the folder you picked), that folder name is removed from the paths.

Rules the server enforces:

- File types: `.md`, `.txt`, `.json`, `.yaml`, `.yml`, `.csv`.
- At most 200 files, 256 KB per file, 2 MB in total, and 6 levels deep. The request body is capped at 3 MB.
- Path segments use letters, digits, `.`, `_`, and `-`. No `..`, no absolute paths, and no segment that starts with a dot.
- No two paths that differ only by case.
- Exactly one `SKILL.md`, at the top. A nested `SKILL.md` is rejected.
- Files must be UTF-8 text.
- Every file is threat-scanned. A finding in a nested file is reported with its path.

The UI strips OS junk before upload and lists what it skipped: `.DS_Store`, `Thumbs.db`, `__MACOSX/`, and other dot-files. It flags any other file that breaks a rule and blocks **Submit for review** until you remove it.

A new draft with the same id replaces the earlier draft, whether it was a single file or a folder.

## Review in Inbox

The skill draft detail shows **Created by** and the source (`/learn` in chat, pasted in admin, or folder upload). For a folder draft it also shows the file tree with sizes and per-file scan findings. Click a file to read it.

For a revision (**Revision of** set), Inbox shows a diff per file, marked added, removed, or changed.

An admin with `edit_config` can approve a draft they uploaded themselves.

**Approve** re-scans every file, then moves the skill into place:

- A single-file draft becomes `<category>/<id>.md`. If it revises a folder skill, only `SKILL.md` is replaced and the supporting files stay.
- A folder draft becomes `<category>/<id>/`. A revision swaps the whole folder; if the swap fails, the previous folder is restored.

**Reject** deletes the draft.

## Where skills live

Builtin skills ship read-only from the repo at `SKILLS_DIR` (`/skills` in Docker). Drafts and admin-created skills go to a writable managed directory, `SKILLS_MANAGED_DIR` (`/skills-managed`, the `ts-harness-skills-managed` volume). When `SKILLS_MANAGED_DIR` is unset, both point at `SKILLS_DIR`, which is the usual local-dev setup.

- The catalog loads both. **System → Skills** shows each skill's **Origin** (builtin or managed).
- Builtin skills are revised in the repo. A revision draft of a builtin skill fails with `skill_revision_builtin` when the two directories differ.
- If both directories hold the same `id@version`, the builtin copy wins and `skill_doctor` reports the skipped managed copy.
- The managed volume is written by one `agent-runtime` process. Do not scale it beyond a single replica.
- Back up the volume with the other runtime state. See [Backup And Restore](program-builder.md#backup-and-restore).

## Agents reading supporting files

The agent prompt includes `SKILL.md` and, for a folder skill, a list of its files:

```text
Files (read with read_skill_reference skill_id=<id> path=...): stages/plan.md, schemas/output/brief.json
```

The list is capped at 40 entries. The agent loads a file with `read_skill_reference` and `path`. Omitting `path` returns `SKILL.md` and the file list. Omitting `version` uses the indexed version.

An agent can only read skills it is assigned and that are approved. Drafts and blocked skills are never readable. Paths outside the skill folder, symlinks that point outside, disallowed file types, and files over 256 KB are refused.

The prompt audit records a `tree_hash` for folder skills, so a change to any supporting file shows up, not only a change to `SKILL.md`.

`read_skill_reference` is part of the `core-read` toolset, so any agent with `core-read` has it, including agents with an allow list. A deny-list entry or a runtime-settings overlay that redefines `core-read` without it removes it again.

**Restrict tools:** a workflow stage with **Restrict tools** on still gets `read_skill_reference`, so it does not appear in the stage's tool checkboxes. It only reads approved skills the stage's agent is assigned, so it cannot publish, write, or reach other files. The agent's own deny list can still remove it.

## Admin API

```bash
# Paste
curl -fsS -X POST -H "Authorization: Bearer $GATEWAY_TOKEN" -H "Content-Type: application/json" \
  -d '{"mode":"text","markdown":"# Body\n","id":"team-runbook","category":"ops","description":"Runbook"}' \
  "http://127.0.0.1:${GATEWAY_PORT:-18789}/admin/skills/drafts"

# Folder
curl -fsS -X POST -H "Authorization: Bearer $GATEWAY_TOKEN" -H "Content-Type: application/json" \
  -d '{"mode":"folder","files":[{"path":"SKILL.md","content":"---\nid: kit\ncategory: ops\ndescription: Kit\n---\n# Kit\n"},{"path":"stages/plan.md","content":"# Plan\n"}]}' \
  "http://127.0.0.1:${GATEWAY_PORT:-18789}/admin/skills/drafts"

# Read one draft file (view permission)
curl -fsS -H "Authorization: Bearer $GATEWAY_TOKEN" \
  "http://127.0.0.1:${GATEWAY_PORT:-18789}/admin/skills/drafts/kit/files/stages/plan.md"
```

Both create modes accept an optional `revision_of`. A success returns the draft and `inbox_href`. An error returns 400 with `error`, and `error_code` and `path` when they apply. A body over 3 MB returns 413.

## Related

- Agent drafts and revisions: [skill-authoring.md](skill-authoring.md)
- Catalog pages: [skills-catalog.md](skills-catalog.md)
