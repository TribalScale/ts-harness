# MCP Settings

Operators manage MCP servers from Admin Console **System → MCP**. YAML
`mcp_servers` stays bootstrap. Activate writes a dedicated overlay. Settings
Activate does not touch this store. The UI never writes rendered
`ts-harness-runtime.yaml`.

## Hashes

| Route | Page |
|---|---|
| `#/system/mcp` | Server list, HTTP add, catalog install |
| `#/system/mcp/{name}` | Server detail |

Unsaved local MCP edits confirm before leaving the tab. Persisted drafts stay.

## Overlay path

```text
{TS_HARNESS_MCP_SETTINGS_STORE}/   # or {WORKFLOW_DEFINITION_STORE}/mcp-settings
  drafts/<uuid>.json
  active/mcp_settings.json
  active/manifest.json
  history/mcp_settings/<sha>-<revision>.json
```

Default root is `workflow-definitions/mcp-settings`. Overlay `servers` wins on
name. Omitted names inherit YAML. Reset drops that overlay key. Remove is only
for overlay-only names.

`env` and `headers` values must be `secret:<NAME>`. List views never show
resolved secrets.

## Catalog PR for new stdio

Freeform stdio `command` / `args` is rejected. To install a stdio server:

1. Add a reviewed entry to [`config/mcp-catalog.yaml`](../../config/mcp-catalog.yaml).
2. Merge the catalog PR.
3. On **System → MCP**, pick the entry and **Install**.
4. Fill `secret:` names for each required `env_names` item.

HTTP/SSE add does not need a catalog entry. Transport is `streamable_http` or
`sse`. URL scheme must be `http` or `https`.

## Test vs Activate

| Action | Effect |
|---|---|
| **Test** | Merges the current draft (or active overlay) onto the YAML baseline, connects with a temporary manager, lists tools, disconnects. Does not persist tools or replace the live MCP manager. |
| **Activate** | Validates the draft, persists active, applies onto the YAML baseline, rebuilds the live manager. One discovery failure returns `200` with `degraded` names. The overlay is not rolled back. |

Writes require `admin` or `owner`. Health still reports the `mcp` component.
Existing `GET /mcp/tools` stays the live discovery list.
