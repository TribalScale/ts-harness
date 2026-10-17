# Managed routing overlay

## Purpose

Operators register Slack/channel start intents in **System → Settings → Routing**
(`#/system/settings/routing`) without editing
`config/ts-harness-runtime.yaml.tmpl` per program. Program Inspector links
there; it does not write the overlay.

Tenant Jira defaults and identity maps live in a sibling
**runtime-settings** overlay. See [../runbooks/system-settings.md](../runbooks/system-settings.md).

## Ownership

- **Gateway only** reads the overlay (classify + intent policy).
- Workflow-runner does **not** load routing.
- Settings **Activate** may persist routing together with runtime-settings after
  an in-process apply. Do not call the two single-store activate endpoints in
  sequence.

## Storage

```text
{TS_HARNESS_ROUTING_DEFINITION_STORE}/   # default: /workflow-definitions/routing
  drafts/<uuid>.json
  active/channel_routing.json
  active/manifest.json
  history/channel_routing/<sha>-<revision>.json
```

Document shape:

```json
{
  "definition_type": "channel_routing",
  "definition_version": "1.0.0",
  "intents": [
    {
      "id": "governed_rfp",
      "signals": ["draft rfp"],
      "exclude": [],
      "program_type": "rfp_response",
      "entry_phase": "rfp",
      "orchestrator_hint": "...",
      "policy": {
        "mode": "require_workflow",
        "deny_tools": ["propose_delegate_task", "delegate_task"]
      }
    }
  ]
}
```

## Merge order

1. Load YAML intents from runtime config.
2. Load active overlay intents (if any).
3. Overlay **wins on `id`** (replace YAML intent with same id).
4. Overlay-only ids append after YAML order (YAML order preserved for unmatched ids).

Phrase collisions (same signal text on two intents) are a **validation warning**.
Clone proofs must use distinct signals (do not share `"create a prd"` with YAML
`governed_prd`).

## Fail-closed reload

- On Settings page Activate: validate, apply YAML + settings + routing in
  memory, then persist dirty actives only if apply succeeds.
- Routing-only activate still rewrites `active/channel_routing.json`, bumps
  manifest generation, then merges into live `RuntimeConfig.routing.intents`.
- If overlay JSON is malformed or apply fails: **keep the last good merged
  list**; do not swap actives; return `reload_ok: false`.
- Container recreate is **not** required after a successful activate.

## Defaults

- Auto-propose always sends explicit `program_type` / `entry_phase`.
- Omitted `propose_start_program` tool args still default to
  `software_delivery` (`DEFAULT_PROGRAM_TYPE`).
- No overlay global default program type in v1.
