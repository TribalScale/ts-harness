import { ListSection, MappingSection, type MappingValue } from "./MappingSection";
import type { RuntimeSettingsDefinition } from "./types";

type Identity = NonNullable<RuntimeSettingsDefinition["identity"]>;
type MapKey = "slack_user_emails" | "jira_account_ids" | "github_logins" | "roles" | "departments" | "role_bundles" | "group_roles";

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

/** Every identity mapping as rows to add and remove: no JSON to type. */
const MAPS: Array<{
  key: MapKey;
  title: string;
  keyLabel: string;
  valueLabel: string;
  keyPlaceholder: string;
  valuePlaceholder: string;
  hint?: string;
  list?: boolean;
  lowerKey?: boolean;
  lowerValue?: boolean;
}> = [
  {
    key: "slack_user_emails",
    title: "Slack user → email",
    keyLabel: "Slack user id",
    valueLabel: "Slack email",
    keyPlaceholder: "U01234567",
    valuePlaceholder: "name@company.com",
    lowerValue: true,
  },
  {
    key: "jira_account_ids",
    title: "Email → Jira accountId",
    keyLabel: "Jira email",
    valueLabel: "Jira account id",
    keyPlaceholder: "name@company.com",
    valuePlaceholder: "accountId",
    lowerKey: true,
  },
  {
    key: "github_logins",
    title: "Email → GitHub login",
    keyLabel: "GitHub email",
    valueLabel: "GitHub login",
    keyPlaceholder: "name@company.com",
    valuePlaceholder: "octocat",
    hint: "Lets a person act on PR comments (/revise, /retry, /accept, /stop) and credits their PRs.",
    lowerKey: true,
  },
  {
    key: "roles",
    title: "Email → roles",
    keyLabel: "Role email",
    valueLabel: "Roles",
    keyPlaceholder: "name@company.com",
    valuePlaceholder: "admin, approver",
    hint: "Separate roles with commas.",
    list: true,
    lowerKey: true,
  },
  {
    key: "departments",
    title: "Email → department",
    keyLabel: "Department email",
    valueLabel: "Department",
    keyPlaceholder: "name@company.com",
    valuePlaceholder: "engineering",
    lowerKey: true,
  },
  {
    key: "role_bundles",
    title: "Role → permissions",
    keyLabel: "Role",
    valueLabel: "Permissions",
    keyPlaceholder: "approver",
    valuePlaceholder: "approve_gate, view_programs",
    hint: "Separate permissions with commas.",
    list: true,
    lowerKey: true,
    lowerValue: true,
  },
  {
    key: "group_roles",
    title: "Identity provider group → roles",
    keyLabel: "Group",
    valueLabel: "Group roles",
    keyPlaceholder: "engineering-leads",
    valuePlaceholder: "approver",
    hint: "Separate roles with commas.",
    list: true,
    lowerValue: true,
  },
];

export function IdentityForm({
  merged,
  definition,
  onChange,
}: {
  merged: RuntimeSettingsDefinition | Record<string, unknown>;
  sources: Record<string, "inherited" | "overlay">;
  definition: RuntimeSettingsDefinition;
  onChange: (next: RuntimeSettingsDefinition) => void;
}) {
  const mergedIdentity = asRecord(asRecord(merged).identity);
  const own: Identity = definition.identity || {};

  function patch(next: Partial<Identity>) {
    onChange({ ...definition, identity: { ...definition.identity, ...next } });
  }

  function reset(key: keyof Identity) {
    const identity = { ...definition.identity };
    delete identity[key];
    onChange({ ...definition, identity });
  }

  const permissions = own.service_permissions ?? (mergedIdentity.service_permissions as string[] | undefined) ?? [];

  return (
    <div>
      <h3>Identity</h3>
      {MAPS.map((map) => (
        <MappingSection
          key={map.key}
          title={map.title}
          keyLabel={map.keyLabel}
          valueLabel={map.valueLabel}
          keyPlaceholder={map.keyPlaceholder}
          valuePlaceholder={map.valuePlaceholder}
          hint={map.hint}
          list={map.list}
          lowerKey={map.lowerKey}
          lowerValue={map.lowerValue}
          values={(own[map.key] ?? asRecord(mergedIdentity[map.key])) as Record<string, MappingValue>}
          overlay={own[map.key] != null}
          onSet={(next) => patch({ [map.key]: next } as Partial<Identity>)}
          onReset={() => reset(map.key)}
        />
      ))}
      <ListSection
        title="Service permissions"
        itemLabel="Permission"
        placeholder="approve_gate"
        hint="Permissions the service token holds. Leave inherited unless you need to narrow them."
        items={Array.isArray(permissions) ? permissions.map(String) : []}
        overlay={own.service_permissions != null}
        onSet={(next) => patch({ service_permissions: next })}
        onReset={() => reset("service_permissions")}
      />
    </div>
  );
}
