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

## UI work orders are checked frame by frame

A frame shows more than a work order's prose repeats: a panel that opens when a chip is selected,
blocks that dim instead of disappearing, the status line and progress bar on a card, which rows
are grey and which page areas are white. Each of these steps makes sure what a frame shows is
built and checked.

1. **Planning.** One UI work order per screen, naming at most 6 frames (an agent call carries 6
   images; `TS_HARNESS_MAX_FRAMES_PER_WO`). Each work order that names frames has a
   `## Frame checklist`: per frame a `### D7 · <title>` entry with `Route:` (the app path),
   `State:` (role, data, clicks) and `Shows:` (everything the frame requires, region by region,
   colours included). The planner gets the screen specs drafted at intake (`screen_specs`) to
   write it. The planner contract rejects a work order with more frames, or a frame without an
   entry, route, state or Shows list (`runtime/ts_harness/implementation/frame_checklist.py`).
   On the Jira path `plan_stories` writes the same checklist into each story.
2. **Coding.** The prompt lists the checklist and asks for one Gherkin scenario per frame, tagged
   `@frame-D7`, ending in the frame's state. Each frame's size is written into the work order's
   `## Design frames` section (`- D7 — [title](link) · 1440×900`).
3. **Capture.** After the checks pass, verification runs every `@frame-*` scenario at its frame's
   size (a Playwright config wrapping the repository's, viewport set, screenshot of the last
   state, JSON report) and saves `frame-D7.png` per frame. The captures go to the evidence branch
   with the screenshots (`frame_captures` in the coding output: `captured`, `failed` with where the
   scenario stopped, `missing` with no scenario, or `unavailable` without Playwright).
4. **QA.** Each frame is laid next to its capture in one labelled image (design left, app right),
   and both are sampled on a grid: where both are a flat colour, the colours are compared and
   pairs that differ are listed in `design_checks` (`runtime/ts_harness/workflows/frame_review.py`).
   QA (`governed-qa-gate` 1.11.0) judges every listed frame as its own criterion: a missing panel,
   state or card content is a defect, a different colour token (a beige page behind a white
   design, white rows where the design is grey) is a defect, and a frame with no capture fails
   instead of passing as unverified. Rendering noise, 1 px shifts and different example data do
   not count. A frame shared with other tickets (`## Frame scope`, `## Shared frames`) is judged
   only for the part this ticket owns, and a capture in the wrong data state is one `test_data`
   defect; see
   [governed-delivery-flow.md](governed-delivery-flow.md#shared-design-frames-each-ticket-is-judged-on-its-own-part).
5. **Tests rewritten to fit.** `review_precheck` lists what the patch changed in existing tests
   (`test_changes`: removed and added test names and assertions, and reversals such as "pick up
   any block and Carry-over card" becoming "…but not a Carry-over card"). Code review (1.10.0)
   blocks a reversal the work order did not ask for; QA treats it as a defect.
6. **Every night.** When QA passes a work order with frames, the frames are recorded for its web
   repository. The nightly smoke run captures each frame's route at its size against the real
   API and the harness proposes one bug for what drifted. See
   [nightly-smoke.md](nightly-smoke.md#design-frames-every-night).

## Screen specs (PRD requests)

A design shows layout, copy and the states someone drew. It does not show what an action does,
where data comes from, the rules behind a screen or how it works on a phone, so agents guessed
those. A PRD request with Figma frames now drafts **one spec per screen** before intake
(`draft_screen_specs` in `prd_request` 1.7.0, `runtime/ts_harness/intake/screen_specs.py`):

- Frames are grouped into screens by name: the part before ` · ` or ` — ` is the screen
  ("Hub / Schedule / 02 Week · Base" and "… · Pilot" are one screen). Name variants that way in
  Figma.
- For each screen, the intake vision model (`TS_HARNESS_INTAKE_VISION_MODEL`, else the stage
  agent's model) gets up to 4 of its frame images and their text and returns what is visible
  (purpose, layout, components, data shown, actions, states drawn) and up to 4 questions for what
  is not, each with a recommended answer. Screens are numbered `SC1`…, questions `S1`…. Up to 40
  screens (`TS_HARNESS_SCREEN_SPECS_MAX`), 4 at a time.
- The specs are posted in the thread (a Google Doc when long), with each screen's frames linked.
- Each question is rated by impact (high: data, rules, permissions, what an action does; low:
  wording, icons). Intake drops the screen questions the sources already answer and asks at most
  20 of the rest, highest impact first, under **Screen questions**, next to its own. Every other
  question keeps its recommended answer unless you answer it by S-number. Answer by S-number ("S3: no, show an error"), correct a spec with
  its SC-number ("SC2: supervisors also see this screen"), then say **ready**; "yes" accepts every
  recommendation.
- The draft and every revision get the specs and the answers; each screen's requirements and
  Given/When/Then criteria come from them.

**With a design inventory.** Attach the design team's screen and feature inventory (Markdown,
for example a Claude export from Figma) with the request. A source with a screens table, a
number column (`#`, `No`, `ID`) and a `Screen` column, is read as the inventory:

- Its numbers group the frames: a frame whose name has a path segment starting with a screen
  number ("Hub / Schedule / 02 Week · Base" → `02`) joins that screen (`02` and `2` match; `T1`
  matches `T1`). Frames with no matching number group by name as before.
- Each screen's row (what it is for, its states) goes to the vision model as the designer's
  intent: it does not ask what the row answers, and asks where the row and the frames disagree.
- Every row whose item is marked partial ("Publish the week (F-14, partial)", or a cell that
  reads `Partial`) becomes a high-impact question under **Known gaps** (`SC0`), asked first. The
  word "partial" in a description is not a gap.

Attach the story maps the inventory refers to as well: intake drops the questions they answer.

Without Figma frames (or with image reading off) the step is skipped and posts nothing.

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
