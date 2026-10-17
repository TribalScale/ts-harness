# Content campaign

Create a `content_campaign` program from the admin console. A person gives a brief. The marketing agent drafts a keyword list, a person approves it, the agent drafts one asset (a blog post or ad copy) in Google Docs, a person approves that draft, the agent writes a short publish plan, a person approves go-live, then the agent publishes through your MCP.

The person approves the draft and the publish plan. The agent performs the MCP publish only after that. The agent does not send email.

To have an agent draft the workflow, program, and routing intent for you instead of steps 5–7, use **Build with agent** on the program library. See [program-builder-agent.md](program-builder-agent.md). Steps 1–4 still apply.

Do the steps in order. Each screen only lists what is already active. **Activate** on a builder saves that builder. **System → Settings → Activate** saves settings and routing. Neither writes `ts-harness-runtime.yaml`.

## 1. Google account

Open **System → Settings → Connector** (`#/system/settings/connector`).

Confirm the connector is enabled. Secret fields stay names (`credentials_secret`, `client_id_secret`, `client_secret_secret`). Do not paste a key, a PEM block, or a JSON credential.

The accounts map needs an account key named `marketing`, with that mailbox in `impersonate_email`. **Validate**, then **Activate**.

## 2. Marketing agent tools

Open **System → Agents** (`#/system/agents`) and select `marketing-agent`.

The form loads the current allow list, deny list, Google account, and soul. Leave them in place. On **Activate** without those values, the first save would drop the deny list.

- **Google account:** `marketing`.
- Leave the `core-read` toolset checked. It provides `read_skill_reference`, which a folder skill needs (§4). Do not deny that tool.
- Leave the `google-workspace-docs` toolset checked. Docs tools are added for every agent that has that toolset, including when the allow list is non-empty.
- Leave `google_workspace_gmail_send` unchecked. That checkbox is the deny. Unchecking a Docs tool would deny it for this agent only.
- Do not clear the allow list. An empty allow list means there is no allow list.

**Save draft**, then **Activate**.

## 3. Publish MCP (optional but required for the publish stage)

Install and enable the MCP server that performs your real publish action (CMS, ads platform, etc.) under **System → MCP**. Add its tool names to **System → Settings → Policy** → `approval_required_tools` only if you want **Inbox tool approval** on each MCP call in addition to the **`approve_publish` gate** below. For most teams, the publish gate is enough; keep high-risk MCP tools on the global list if a call should pause mid-task.

Give `marketing-agent` the MCP toolset so the **`publish`** stage can call those tools. **Restrict tools** on every earlier task so they cannot (see §5). Restrict tools only narrows a stage; it cannot add tools the agent does not already have.

## 4. Approve the procedure

The workflow builder can attach only an approved skill. `marketing-agent` does not have a campaign procedure yet.

You need one skill, id `content-campaign`, that does this and nothing else:

- On a stage named `keywords`, turn the brief into a keyword list. Do not draft the asset.
- On a stage named `draft_asset`, draft either one blog post or one set of ads from the approved keywords, using the Google Docs tools. Do not send email, publish, or spend money.
- On a stage named `publish_plan`, summarize what is about to go live from the approved asset. Include destination, title, audience, and the exact MCP action (for example "create a live blog post"). State what will not happen (no email, no extra spend). Do not call publish tools and do not change the asset.
- On a stage named `publish`, after **`approve_publish`**, call the configured MCP publish tools to carry out that plan. Return the live URL, id, or confirmation in task output. Do not send email.

Open **System → Skills** (`#/system/skills`) and create it one of three ways. See [skills.md](skills.md) for the rules.

- **Learn a skill:** Chat opens with `/learn`. Describe the procedure above and send the message.
- **Paste skill:** if you already have it written, paste it with **Id** `content-campaign` and **Category** `marketing`.
- **Upload folder:** to ship per-stage notes or templates with it (for example `stages/publish_plan.md`), upload a folder with `SKILL.md` at the top. Stages load those files with `read_skill_reference`, which every stage has even when its tools are restricted.

**Paste skill** and **Upload folder** need the `edit_config` permission.

Each way makes a draft. Open **Inbox → Skills** (`#/inbox/skill_draft`), review it (a folder draft shows its file tree), and **Approve**. The skill is not usable until that approval.

Return to `marketing-agent`, add `content-campaign` to **Skills**, **Save draft**, and **Activate** again.

## 5. Workflow

Open **Workflows** (`#/workflows`) and choose **New workflow**.

Set **Workflow type** to `content_campaign`, **Display name** to `Content campaign`, and **Definition version** to `1.0.0`.

Add these stages with **Add task** and **Add gate**. Drag a connection from each stage to the next, in this order.

| Stage | How to add it | Inspector |
|---|---|---|
| `keywords` | Add task. Rename **Stage name**. | **Agent** `marketing-agent`. **Skill hints** `content-campaign`. **Present output** on. **Restrict tools** to Docs read/create/append, `read_file`, and memory. No MCP. |
| `approve_keywords` | Add gate. | **Approval** stays on. **Present prompt** `approval`. |
| `draft_asset` | Add task. | **Agent** `marketing-agent`. **Skill hints** `content-campaign`. **Present output** on. **Restrict tools** the same as `keywords`. |
| `approve_asset` | Add gate. | **Approval** stays on. **Present prompt** `approval`. |
| `publish_plan` | Add task. | **Agent** `marketing-agent`. **Skill hints** `content-campaign`. **Present output** on. **Restrict tools** to `read_file`, memory, and `google_workspace_docs_read`. No create, append, or MCP. |
| `approve_publish` | Add gate. | **Approval** stays on. **Present prompt** `approval`. The yes/no attaches to the `publish_plan` message in the thread. |
| `publish` | Add task. | **Agent** `marketing-agent`. **Skill hints** `content-campaign`. **Present output** on. **Restrict tools** to the MCP publish tools (plus `read_file` and memory if the skill needs them). |

**Save draft**, **Validate**, then **Activate**. A gate added from the toolbar already requires approval and uses the orchestrator.

`publish_plan` is the piece between the two gates. **`approve_asset`** accepts the draft. **`publish_plan`** then posts a short go-live summary in the thread (destination, title, MCP action, and what will not happen). **`approve_publish`** is yes/no on that summary. **`publish`** runs the MCP only after that yes, and presents the live URL or id.

Google Docs create/append during `draft_asset` are not on the global tool-approval list. Long copy may also get a presentation Doc from the connector when the draft is posted.

## 6. Program

Open **Workflows → Programs** (`#/workflows/programs`) and choose **New program**.

- **Program type:** `content_campaign`
- **Display name:** `Content campaign`
- **Definition version:** `1.0.0`

**Add terminal node.** Select it and set:

- **Node id:** `campaign`
- **Startable** and **Terminal** on
- **Default workflow:** `content_campaign`
- **Completion adapter:** `extract_declared_outputs`
- **Produces artifacts:** `approved_asset`, `publish_result`
- **Input bindings:** `program_summary summary` and `transcript_context context limit=20`

Leave **Edges** empty. This program is one phase.

**Save draft**, **Validate**, then **Activate**.

## 7. Start it from chat

Open **System → Settings → Routing** (`#/system/settings/routing`). Add an intent:

- **Intent id:** `content_campaign`
- **Intent signals:** `start a content campaign`, `draft a blog from keywords`
- **Program type:** `content_campaign`
- **Intent entry phase:** `campaign`
- Leave **Workflow type** and **Delegate agent** empty.

Use signals that do not repeat a built-in phrase such as `create a prd`. **Validate**, then **Activate** on the Settings page. Treat `reload_ok: false` as a failed activate.

In **Chat** or Slack, say "start a content campaign" and give the brief. Approve the keyword list, approve the draft, then approve the publish plan before the agent runs **`publish`**. Reply **yes** or **no** (with feedback) in the bound thread at each gate.
