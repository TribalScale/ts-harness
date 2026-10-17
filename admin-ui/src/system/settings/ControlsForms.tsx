import { Button } from "../../components/Button";
import type { RuntimeSettingsDefinition } from "./types";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function lines(value: unknown): string {
  return Array.isArray(value) ? value.map(String).join("\n") : "";
}

function parseLines(value: string): string[] {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

export function DeliveryExtras({
  merged,
  definition,
  onChange,
}: {
  merged: RuntimeSettingsDefinition | Record<string, unknown>;
  definition: RuntimeSettingsDefinition;
  onChange: (next: RuntimeSettingsDefinition) => void;
}) {
  const mergedDefaults = asRecord(asRecord(merged).program_defaults);
  const defaults = definition.program_defaults || {};
  const github = defaults.github || (mergedDefaults.github as { org?: string }) || {};
  const governance = asRecord(defaults.governance || mergedDefaults.governance);
  const review = asRecord(governance.independent_review);
  const delivery = defaults.delivery || (mergedDefaults.delivery as { max_parallel_work_orders?: number }) || {};
  const compensation = (defaults.compensation ||
    mergedDefaults.compensation ||
    {}) as { jira?: string; delete_branches?: boolean };

  function patch(next: RuntimeSettingsDefinition["program_defaults"]) {
    onChange({ ...definition, program_defaults: { ...definition.program_defaults, ...next } });
  }

  return (
    <div>
      <h3>Delivery defaults</h3>
      <label className="form-field">
        <span>GitHub org</span>
        <input
          aria-label="GitHub org"
          value={github.org || ""}
          onChange={(event) => patch({ github: { org: event.target.value } })}
        />
      </label>
      <label>
        <input
          type="checkbox"
          aria-label="Independent review"
          checked={review.enabled !== false}
          onChange={(event) =>
            patch({ governance: { independent_review: { enabled: event.target.checked } } })
          }
        />
        Independent review
      </label>
      <label className="form-field">
        <span>Max parallel work orders</span>
        <input
          aria-label="Max parallel work orders"
          type="number"
          min={1}
          max={10}
          value={delivery.max_parallel_work_orders ?? 3}
          onChange={(event) =>
            patch({ delivery: { max_parallel_work_orders: Number(event.target.value) } })
          }
        />
      </label>
      <label className="form-field">
        <span>Cancel Jira action</span>
        <input
          aria-label="Cancel Jira action"
          value={compensation.jira || "label"}
          onChange={(event) => patch({ compensation: { ...compensation, jira: event.target.value } })}
        />
      </label>
      <label>
        <input
          type="checkbox"
          aria-label="Delete branches on cancel"
          checked={Boolean(compensation.delete_branches)}
          onChange={(event) =>
            patch({ compensation: { ...compensation, delete_branches: event.target.checked } })
          }
        />
        Delete branches on cancel
      </label>
      <label className="form-field">
        <span>Merge readiness JSON</span>
        <textarea
          aria-label="Merge readiness JSON"
          value={JSON.stringify(defaults.merge_readiness || mergedDefaults.merge_readiness || {}, null, 2)}
          onChange={(event) => {
            try {
              patch({ merge_readiness: JSON.parse(event.target.value) });
            } catch {
              /* keep typing until the JSON is valid */
            }
          }}
        />
      </label>
      <label className="form-field">
        <span>Release environments JSON</span>
        <textarea
          aria-label="Release environments JSON"
          value={JSON.stringify(defaults.release || mergedDefaults.release || {}, null, 2)}
          onChange={(event) => {
            try {
              patch({ release: JSON.parse(event.target.value) });
            } catch {
              /* keep typing until the JSON is valid */
            }
          }}
        />
      </label>
    </div>
  );
}

const IDENTITY_JSON_KEYS = [
  "github_logins",
  "roles",
  "role_bundles",
  "departments",
  "service_permissions",
  "group_roles",
] as const;

export function IdentityExtras({
  merged,
  definition,
  onChange,
}: {
  merged: RuntimeSettingsDefinition | Record<string, unknown>;
  definition: RuntimeSettingsDefinition;
  onChange: (next: RuntimeSettingsDefinition) => void;
}) {
  const mergedIdentity = asRecord(asRecord(merged).identity);
  return (
    <div>
      {IDENTITY_JSON_KEYS.map((key) => (
        <label key={key} className="form-field">
          <span>{key}</span>
          <textarea
            aria-label={key}
            value={JSON.stringify(definition.identity?.[key] || mergedIdentity[key] || {}, null, 2)}
            onChange={(event) => {
              try {
                const parsed = JSON.parse(event.target.value);
                onChange({
                  ...definition,
                  identity: { ...definition.identity, [key]: parsed },
                });
              } catch {
                /* keep typing until the JSON is valid */
              }
            }}
          />
        </label>
      ))}
    </div>
  );
}

export function PolicyForm({
  merged,
  definition,
  onChange,
}: {
  merged: RuntimeSettingsDefinition | Record<string, unknown>;
  definition: RuntimeSettingsDefinition;
  onChange: (next: RuntimeSettingsDefinition) => void;
}) {
  const policy = { ...asRecord(asRecord(merged).policy), ...asRecord(definition.policy) };
  function patch(next: Record<string, unknown>) {
    onChange({ ...definition, policy: { ...definition.policy, ...next } });
  }
  return (
    <div>
      <h3>Policy</h3>
      <Button
        variant="secondary"
        size="sm"
        type="button"
        onClick={() => {
          const next = { ...definition };
          delete next.policy;
          onChange(next);
        }}
      >
        Reset to inherited
      </Button>
      {(
        [
          "approval_required_tools",
          "channel_ingress_deny",
          "delegation_auto_allow_agents",
          "egress_sensitive_tools",
          "network_egress_deny_tools",
        ] as const
      ).map((key) => (
        <label key={key} className="form-field">
          <span>{key}</span>
          <textarea
            aria-label={key}
            value={lines(policy[key])}
            onChange={(event) => patch({ [key]: parseLines(event.target.value) })}
          />
        </label>
      ))}
      <label className="form-field">
        <span>Budget warn USD</span>
        <input
          aria-label="Budget warn USD"
          type="number"
          value={Number(policy.budget_warn_usd ?? 50)}
          onChange={(event) => patch({ budget_warn_usd: Number(event.target.value) })}
        />
      </label>
      <label className="form-field">
        <span>Budget hard stop USD</span>
        <input
          aria-label="Budget hard stop USD"
          type="number"
          value={Number(policy.budget_hard_stop_usd ?? 100)}
          onChange={(event) => patch({ budget_hard_stop_usd: Number(event.target.value) })}
        />
      </label>
      <label className="form-field">
        <span>Rate limit per minute</span>
        <input
          aria-label="Rate limit per minute"
          type="number"
          value={Number(policy.rate_limit_per_minute ?? 0)}
          onChange={(event) => patch({ rate_limit_per_minute: Number(event.target.value) })}
        />
      </label>
      <label>
        <input
          type="checkbox"
          aria-label="OPA enabled"
          checked={Boolean(policy.opa_enabled)}
          onChange={(event) => patch({ opa_enabled: event.target.checked })}
        />
        OPA enabled
      </label>
      <label className="form-field">
        <span>Model allowlist JSON</span>
        <textarea
          aria-label="Model allowlist JSON"
          value={JSON.stringify(policy.model_allowlist || {}, null, 2)}
          onChange={(event) => {
            try {
              patch({ model_allowlist: JSON.parse(event.target.value) });
            } catch {
              /* keep typing until the JSON is valid */
            }
          }}
        />
      </label>
      <label className="form-field">
        <span>Data residency JSON</span>
        <textarea
          aria-label="Data residency JSON"
          value={JSON.stringify(policy.data_residency_regions || {}, null, 2)}
          onChange={(event) => {
            try {
              patch({ data_residency_regions: JSON.parse(event.target.value) });
            } catch {
              /* keep typing until the JSON is valid */
            }
          }}
        />
      </label>
    </div>
  );
}

export function SandboxForm({
  merged,
  definition,
  onChange,
}: {
  merged: RuntimeSettingsDefinition | Record<string, unknown>;
  definition: RuntimeSettingsDefinition;
  onChange: (next: RuntimeSettingsDefinition) => void;
}) {
  const sandbox = { ...asRecord(asRecord(merged).sandbox), ...asRecord(definition.sandbox) };
  function patch(next: Record<string, unknown>) {
    onChange({ ...definition, sandbox: { ...definition.sandbox, ...next } });
  }
  return (
    <div>
      <h3>Sandbox</h3>
      <p className="muted">Mode and backend stay docker. The worker is still the Docker worker.</p>
      <Button
        variant="secondary"
        size="sm"
        type="button"
        onClick={() => {
          const next = { ...definition };
          delete next.sandbox;
          onChange(next);
        }}
      >
        Reset to inherited
      </Button>
      {(["cpu_limit", "memory_mb", "timeout_seconds"] as const).map((key) => (
        <label key={key} className="form-field">
          <span>{key}</span>
          <input
            aria-label={key}
            type="number"
            value={Number(sandbox[key] ?? (key === "cpu_limit" ? 1 : key === "memory_mb" ? 512 : 300))}
            onChange={(event) => patch({ [key]: Number(event.target.value) })}
          />
        </label>
      ))}
      <label className="form-field">
        <span>Egress policy</span>
        <select
          aria-label="Egress policy"
          value={String(sandbox.egress_policy || "deny")}
          onChange={(event) => patch({ egress_policy: event.target.value })}
        >
          <option value="deny">deny</option>
          <option value="allow">allow</option>
        </select>
      </label>
    </div>
  );
}

export function BindingsForm({
  merged,
  definition,
  onChange,
}: {
  merged: RuntimeSettingsDefinition | Record<string, unknown>;
  definition: RuntimeSettingsDefinition;
  onChange: (next: RuntimeSettingsDefinition) => void;
}) {
  const bindings = {
    ...asRecord(asRecord(merged).bindings),
    ...(definition.bindings || {}),
  } as Record<string, string>;
  const channels = Array.from(new Set(["slack", "discord", "telegram", ...Object.keys(bindings)]));
  return (
    <div>
      <h3>Channels</h3>
      <Button
        variant="secondary"
        size="sm"
        type="button"
        onClick={() => {
          const next = { ...definition };
          delete next.bindings;
          onChange(next);
        }}
      >
        Reset to inherited
      </Button>
      {channels.map((channel) => (
        <label key={channel} className="form-field">
          <span>{channel}</span>
          <input
            aria-label={`${channel} agent`}
            value={bindings[channel] || ""}
            onChange={(event) =>
              onChange({
                ...definition,
                bindings: { ...definition.bindings, [channel]: event.target.value },
              })
            }
          />
        </label>
      ))}
    </div>
  );
}

export function ToolsetsForm({
  merged,
  definition,
  onChange,
}: {
  merged: RuntimeSettingsDefinition | Record<string, unknown>;
  definition: RuntimeSettingsDefinition;
  onChange: (next: RuntimeSettingsDefinition) => void;
}) {
  const toolsets = {
    ...asRecord(asRecord(merged).toolsets),
    ...(definition.toolsets || {}),
  } as Record<string, string[]>;
  return (
    <div>
      <h3>Toolsets</h3>
      <p className="muted">
        A membership edit affects every agent with that toolset. Those tools are still added on
        top of a non-empty allow list. An empty mcp or google-workspace list means every
        discovered tool.
      </p>
      <Button
        variant="secondary"
        size="sm"
        type="button"
        onClick={() => {
          const next = { ...definition };
          delete next.toolsets;
          onChange(next);
        }}
      >
        Reset to inherited
      </Button>
      {Object.keys(toolsets).map((name) => (
        <label key={name} className="form-field">
          <span>{name}</span>
          <textarea
            aria-label={name}
            value={lines(toolsets[name])}
            onChange={(event) =>
              onChange({
                ...definition,
                toolsets: { ...definition.toolsets, [name]: parseLines(event.target.value) },
              })
            }
          />
        </label>
      ))}
    </div>
  );
}

export function ConnectorForm({
  merged,
  definition,
  onChange,
}: {
  merged: RuntimeSettingsDefinition | Record<string, unknown>;
  definition: RuntimeSettingsDefinition;
  onChange: (next: RuntimeSettingsDefinition) => void;
}) {
  const mergedGoogle = asRecord(asRecord(asRecord(merged).connectors)["google-workspace"]);
  const overlayGoogle = asRecord(asRecord(definition.connectors)["google-workspace"]);
  const google = { ...mergedGoogle, ...overlayGoogle };
  const auth = asRecord(google.auth);
  const delegation = asRecord(auth.delegation);
  const accounts = asRecord(google.accounts);

  function patchGoogle(next: Record<string, unknown>) {
    onChange({
      ...definition,
      connectors: {
        ...definition.connectors,
        "google-workspace": { ...overlayGoogle, ...next },
      },
    });
  }

  return (
    <div>
      <h3>Google connector</h3>
      <p className="muted">Secret fields are names. Do not paste a key or certificate.</p>
      <Button
        variant="secondary"
        size="sm"
        type="button"
        onClick={() => {
          const next = { ...definition };
          delete next.connectors;
          onChange(next);
        }}
      >
        Reset to inherited
      </Button>
      <label>
        <input
          type="checkbox"
          aria-label="Google connector enabled"
          checked={google.enabled !== false}
          onChange={(event) => patchGoogle({ enabled: event.target.checked })}
        />
        Enabled
      </label>
      <label className="form-field">
        <span>Credentials secret name</span>
        <input
          aria-label="Credentials secret name"
          value={String(delegation.credentials_secret || "")}
          onChange={(event) =>
            patchGoogle({
              auth: { ...auth, delegation: { ...delegation, credentials_secret: event.target.value } },
            })
          }
        />
      </label>
      <label className="form-field">
        <span>Scopes</span>
        <textarea
          aria-label="Google scopes"
          value={lines(google.scopes)}
          onChange={(event) => patchGoogle({ scopes: parseLines(event.target.value) })}
        />
      </label>
      <label className="form-field">
        <span>Accounts JSON</span>
        <textarea
          aria-label="Google accounts JSON"
          value={JSON.stringify(accounts, null, 2)}
          onChange={(event) => {
            try {
              patchGoogle({ accounts: JSON.parse(event.target.value) });
            } catch {
              /* keep typing until the JSON is valid */
            }
          }}
        />
      </label>
    </div>
  );
}
