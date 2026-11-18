# Design references (Figma frames and images)

A PRD request can come with designs: Figma links, or images attached to the message (Slack,
console uploads, Discord, Telegram). Each becomes a **design reference** (`D1`, `D2`…), an image
that travels with the program:

| Step | What it sees |
|---|---|
| PRD intake, draft and revision (`intake`, `draft_prd`, `revise_prd`) | every reference, as images, plus `design_refs_text` naming them |
| Planning (`refine_requirements`, `design_blueprints`, `plan_work_orders`) | every reference; the planner lists in each UI work order the ones it implements (`- Design: D1, D3`) |
| Coding job (`deliver_work_order`) | the work order's references, written to `.ts-harness/design/` in the workspace (never committed) and named in the prompt |
| QA (`qa_gate`) | the work order's references, after the Playwright screenshots, to compare the two |

Without any designs, nothing changes.

## How it works

- `gather_sources` (PRD workflow) reads the request's attachments and links
  (`runtime/ts_harness/intake/`). An attached image is still described in text for the PRD, and
  is also stored as an image under `design/<tenant>/<sha256>.<ext>` in the intake bucket. A Figma
  link is read through the REST API: a link to a frame gives that frame; a link to a page, a
  section or a file gives every frame inside it, opening sections however deeply nested (up to
  200 per link, `TS_HARNESS_FIGMA_MAX_FRAMES`). Frames render a few per request, four requests
  at a time; a batch that fails is retried frame by frame, a frame that still fails is noted and
  the rest are kept, and a rate limit asking for 30 s or less is waited out. Each frame is
  rendered to PNG and summarized (size, text, components, colors, fonts). Frames that render
  alike (two states drawn the same) stay separate references.
- A request can carry several Figma links (pages, sections, frames, from one file or several).
  They are read in order and their frames numbered in that order; a frame reached by two links
  (a page and a frame inside it) is one reference. Figma links are never skipped by the intake
  time or text budget (a large page takes about two minutes the first time).
- Up to 200 references per request across all links (`TS_HARNESS_MAX_DESIGN_REFS`), with ids
  `D1`…`D200`; frames beyond it are listed under the skipped sources with how many. With
  more than 24, prompts carry a shorter summary of each (title, Figma link, first lines of text).
- The references (`id`, `title`, `source`, `url`, `object_key`, `summary`) are saved in the program
  artifacts with the approved PRD (`design_refs`) and carried to planning, the work orders and
  their deliveries.
- The workflow runner adds them to each step just before it runs
  (`runtime/ts_harness/workflows/design_input.py`). Agents get images as image parts (6 images and
  6 MB per call at most; QA gets up to 4 design images next to its screenshots). Coding workers
  have no bucket access, so the images travel inside the coding job spec.
- Figma is read once per file version: frames are cached under
  `design/figma/<file>/<version>/`, so later steps and later requests for the same version make no
  API calls. A new version (the design changed) is fetched again.
- Planning names the frames each work order builds (`- Design: D1, D3`, from every frame's title
  and text; only the first 6 images are attached to a planning call). When the work orders are
  saved, the harness adds a `## Design frames` section to each one that names frames: the
  `Design:` line plus each frame's title linked to the frame in Figma. Filing work orders as Jira
  copies that section into the ticket, and a ticket filed without it gets it back from its work
  order when it is implemented. The coding job and QA receive exactly those frames' images.

- On the Jira path, `plan_stories` (PRD to Jira) sees the images and ends each UI story with
  `Design: D1, D3`. When a ticket is implemented (`implement_jira_ticket`),
  `prepare_ticket_delivery` loads the references of the program the ticket points at
  (`Program:` in its description) and adds the ticket's own image attachments as further
  references, named on its Design line (`runtime/ts_harness/jira/ticket_delivery.py`).

`design/` is kept for a year (bucket lifecycle rule), unlike `intake/` (14 days), so references
outlive the request.

## Set up Figma

Attached images need no setup. Figma links need a read-only token:

1. **A Dev seat.** API limits follow the seat of the token's account. View and Collab seats are
   capped at a handful of calls a month; Dev and Full seats get per-minute limits. Give a shared
   account (for example `harness@<company>`) a **Dev seat**, and add it as a **viewer** to the team
   projects whose designs the harness may read, and no wider.
2. **Client consent.** Rendered frames are sent to the model provider. Confirm that's acceptable
   for client files before sharing their projects with the account.
3. **The token.** Signed in as that account: Settings → Security → Personal access tokens →
   Generate, with read-only scopes **File content** and **File metadata** (and **Variables: read**
   if offered). Note its expiry date.
4. **Store it.** Terraform creates `<prefix>/figma-token` with a placeholder. Set the value, then
   re-run phases 10 and 12 so the workflow runner gets `FIGMA_TOKEN`
   ([Re-run a bootstrap phase](re-run-bootstrap-phase.md)):

   ```bash
   aws secretsmanager put-secret-value --secret-id "$PREFIX/figma-token" --secret-string 'figd_…'
   ```

5. **Terraform.** Apply after upgrading to this release: it adds the `figma-token` secret, lets the
   runtime write `design/*` in the intake bucket, and adds the one-year `design/` lifecycle rule.

Without a token, a Figma link shows up in the PRD's shared sources as "Figma is not connected" and
stays as text; attached images still work.

## Check it

1. Send a PRD request with an image attached and a link to one Figma frame.
2. In the `gather_sources` output, `design_refs` lists `D1`, `D2`… and `sources_text` ends with
   "Design references".
3. The workflow runner logs `design_input` with the step, reference ids and image count for the
   intake and draft steps.
4. After planning, UI work orders have `- Design: D…` in their Context Graph; the coding job's
   workspace has `.ts-harness/design/D1.png` and its prompt names it; QA's evidence cites
   `Design D1`.

## Limits and failures

| Symptom | Cause | Fix |
|---|---|---|
| "Figma is not connected" | No `figma-token`, or phase 10 not re-run | Store the token, re-run phases 10 and 12 |
| "not shared with the harness's Figma account" | The account can't see the file | Add the account as a viewer of the project |
| "Figma rate limit reached (low)" | The account's seat is View/Collab, or the plan's limit | Give the account a Dev seat; cached versions don't count again |
| "Figma file or frame not found" | Wrong or deleted link | Copy the link again (Share → Copy link to selection) |
| A design image is missing for coding or QA | The work order's `Design` line doesn't name it | Edit the work order, or re-run planning |
| "this link has N frames; the first 200 were read" | The page holds more frames than `TS_HARNESS_FIGMA_MAX_FRAMES` | Link sections separately, or raise the setting |
| "<frame>: Figma did not answer in time" | That frame is very large (a whole board, not a screen) | Link its inner frames, or ignore if it is not a screen |

Figma's official MCP server and the community plugin-based servers are not used: the official
server only accepts approved desktop clients with per-person OAuth, and plugin servers need a
person's Figma app open. See the discussion in the change that added this page.
