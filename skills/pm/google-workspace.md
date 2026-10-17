---
id: pm/google-workspace
category: pm
description: Save and update PRD drafts in Google Docs via the pm delegated mailbox.
required_connectors: [google-workspace]
required_toolsets: [google-workspace-docs]
---

Use the `google-workspace-docs` toolset for ad-hoc PRD draft edits (Drive + Docs only — no Gmail or Calendar).

## Governed workflows (do not invent a folder)

For governed delivery (`prd_request`, `work_orders_from_prd`, and related program phases), **do not** call `google_workspace_drive_create` to persist stage artifacts. The runtime publishes them to the Google Drive **delivery artifact library** when specialist output is presented for review.

Do not reference `GOOGLE_PM_PRD_FOLDER_ID`, `GOOGLE_DELIVERY_ARTIFACTS_FOLDER_ID`, or any other folder env var as a tool argument — those are runtime config, not agent inputs.

## Ad-hoc: save a new PRD draft (non-governed only)

1. `google_workspace_drive_create` with `name`: `PRD: <Feature Name>`, `file_type`: `document`.
   - Omit `folder_id` unless the human explicitly provides a Drive folder ID in the request.
2. `google_workspace_docs_append` with the full PRD markdown as `content` (dry run first; omit `confirmed`).
3. After human approval of the write, call `google_workspace_docs_append` again with `confirmed: true`.
4. Return `document_id`, `webViewLink`, and keep `prd_markdown` in the response.

## Revise an existing draft

1. `google_workspace_docs_read` on the document id or URL.
2. Append revision notes or updated sections with `google_workspace_docs_append` (dry run → approval → `confirmed: true`).

Drive create and doc append require human approval per platform policy. Do not mark a PRD as approved until the human explicitly approves the content.
