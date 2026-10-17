# Create an agent

Overlay-only specialists are added from **System → Agents**. YAML bootstrap
agents stay inherited and cannot be deleted from this wizard. **Reset** drops
an overlay override so the YAML agent returns. **Remove** drops an overlay-only
specialist.

## Flow

1. Open `#/system/agents` and choose **New agent**.
2. Fill id, display name, soul, model, skills (catalog ids or categories),
   toolsets, the Google account, the per-tool allow and deny lists, and whether
   the specialist is delegatable. The checkboxes show the tools the agent can
   actually run. Toolset membership still applies to every agent that uses that
   toolset, and those tools are added even when the allow list is non-empty.
   Unchecking a toolset tool adds it to this agent's deny list. That is how one
   agent loses a tool its toolset grants. An empty allow list means there is no
   allow list. Opening a YAML agent seeds its current allow list, deny list,
   Google account, and soul, so the first save does not drop them.
3. **Save draft**, then **Validate** if you need a dry-run, then **Activate**.
4. Activate reloads the live roster. It does not write
   `ts-harness-runtime.yaml`.
5. After activate, add a routing intent (`#/system/settings/routing`) and
   attach MCP if needed (`#/system/mcp`). The wizard does not write those
   stores.

`delegatable` appends the new id to the orchestrator `subagent_allow` so
`delegate_task` can target it. Drafts live in the agents overlay store, not
Inbox.

The soul is one short paragraph in the channel prompt and the workflow prompt.
A non-empty overlay soul replaces the catalog soul. Omitting it keeps the
catalog soul. An empty soul clears it. The prompt does not load `AGENTS.md`.
Built-in workspace files keep the generated identity block and nothing after it.
Activate does not rewrite those files. A console-created agent still gets the
short workspace stub.
