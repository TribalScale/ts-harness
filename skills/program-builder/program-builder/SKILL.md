---
name: program-builder
description: Draft a new program (workflow, program, and routing intent) from a person's description on the admin program builder page. Drafts only; the person activates.
category: program-builder
allowed_agents: [builder-agent]
approval_status: approved
version: 1.0.0
---

# Program builder

You help one person build a new program. You write **drafts only**. The person reviews them on the builder page and clicks **Activate all**. Never say a program is live, active, or ready to use in chat.

Read these files with `read_skill_reference` when you need them:

- `references/schemas.md`: the fields for workflows, programs, and routing intents.
- `references/content-campaign-example.json`: a complete bundle that validates. Copy its shape.

## Procedure

1. **Read the catalog first.** Call `builder_catalog` before drafting anything. It lists:
   - agents and the tools each one has;
   - approved skills;
   - workflow and program types already in use;
   - routing intents;
   - `publish_tools`;
   - this session's current drafts.

   Use only agents, skills, and tools that it lists.
2. **Fill gaps in the intake.** Make sure you know:
   - the program name and purpose;
   - the stages, in order, and which agent does each one;
   - where a person approves;
   - the phrases people will type to start it.

   Ask one short question at a time for anything missing. Do not invent business steps.
3. **Pick new types.** `workflow_type` and `program_type` must not appear in `existing_workflow_types` or `existing_program_types`. Use lowercase snake_case. Start `definition_version` at `1.0.0`.
4. **Reuse first.** Prefer existing agents and skills.
   - If a stage needs a procedure no approved skill covers, call `propose_skill` with a focused skill.
   - Tell the person it is waiting in **Inbox → Skills** and that they must approve it before activating.
   - Use its id in the stage's `skill_hints`.
5. **Gate anything that reaches people.**
   - Any stage that can call a tool in `publish_tools` (or any `mcp-` tool) needs an earlier stage with `approval_required: true`, connected through `depends_on`.
   - A gate is an `orchestrator` task with `approval_required: true` and `present_prompt: "approval"`.
   - Restrict every stage before the first gate with `tool_allow`, so it cannot publish.
   - A stage's own `approval_required` approves its output after it runs, so it does not protect that stage.
6. **Propose the bundle.** Call `propose_program_bundle` with the full `workflow`, `program`, and (when the person wants a chat trigger) `routing_intent`.
7. **Read the result and iterate.**
   - Fix every `error` issue and call again. Calls update the same drafts.
   - Mention warnings to the person.
   - On `revision_conflict`, the person edited a draft by hand: start from `current_definition`, apply your change, and call again.
   - On `type_exists`, pick a new type.
   - On `routing_blocked`, tell the person to finish or discard the routing draft in **System → Settings → Routing**. Then call again.
8. **Hand off.** When `ready_to_activate` is true, summarize what each stage does and where the gates are. Ask the person to review the drafts and click **Activate all** on the builder page. If skills are still unapproved, say which ones.

## Rules

- One program per session. To change it, call `propose_program_bundle` again with the whole definition.
- The builder creates new programs only. It cannot edit an existing program.
- Do not put secrets, tokens, or credentials in any definition.
- Keep signals specific. Do not reuse built-in phrases such as `create a prd`.
- To repeat work per item (each account, each ticket), use node `reentry` instead of an edge cycle. See `references/schemas.md`.
- You cannot schedule programs. When the person wants a program to run on a schedule, tell them to activate it, then open the program and click **Schedule this program** (it creates a **Start program** cron job in **System → Cron**).
