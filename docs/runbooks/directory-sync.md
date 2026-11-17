# Directory sync and offboarding

The people directory lists who can be shared with, which teams they are in, and who has left. People are added when they sign in. A directory source adds the rest and disables people who leave.

## Choose a source

Set `identity.directory` (`IDENTITY_DIRECTORY` in the install) to one source:

| Source | Use when | Matches people by | Credentials (runtime env) |
| --- | --- | --- | --- |
| `seen_users` (default) | No directory access yet | Not applicable; changes nothing | None |
| `auth0_management` | Auth0 is the sign-in provider | Sign-in subject | `AUTH0_MGMT_CLIENT_ID`, `AUTH0_MGMT_CLIENT_SECRET`, optional `AUTH0_MGMT_DOMAIN` (defaults to the issuer host) |
| `scim` | Entra ID or Okta pushes people to us | Email | `TS_HARNESS_SCIM_TOKEN` |
| `microsoft_graph` | Entra ID without SCIM | Email | `MS_GRAPH_TENANT_ID`, `MS_GRAPH_CLIENT_ID`, `MS_GRAPH_CLIENT_SECRET` |
| `google_directory` | Google Workspace | Email | `GOOGLE_WORKSPACE_SA_JSON` (or `_PATH`) with domain-wide delegation, `GOOGLE_DIRECTORY_ADMIN_SUBJECT`, optional `GOOGLE_DIRECTORY_CUSTOMER` |

Credentials go in the runtime env file written by phase 11 (from Secrets Manager on AWS); never put them in the YAML.

Sources that match by email update only people who have signed in at least once; others are counted as `unknown` and picked up after their first sign-in.

## Groups and teams

Groups named `ts-harness-team-<slug>`, `ts-harness-team-<slug>-approvers` and `ts-harness-team-<slug>-managers` become team memberships. `ts-harness-admins` and `ts-harness-users` map to the admin and requester roles unless `identity.group_roles` says otherwise.

- Auth0: roles named `ts-harness-*` are treated as groups; other Auth0 roles are harness roles. `app_metadata.groups` adds more groups.
- Graph: groups whose display name starts with `ts-harness-`, including nested membership.
- Google: groups whose email local part starts with `ts-harness-`, including nested membership.

When tokens do not carry groups (Entra with many groups, Google), set `identity.groups_source` (`OIDC_GROUPS_SOURCE`) to `microsoft_graph` or `google_directory` so sign-in looks groups up with the same credentials.

## The sync job

`sync-directory` runs every 15 minutes (`config/runtime-cron-jobs.json`). For each person the source lists it updates name, roles and team memberships, and re-enables people who come back. People the source marks blocked, suspended or inactive are disabled: `disabled_at` is set, their team memberships are removed, and sign-in is refused within a minute (`authenticate_request` caches the check for 60 seconds).

People the source no longer lists are disabled too, unless that would disable more than half of the active people. In that case nobody is disabled and the job logs `directory_sync_disable_skipped` with the count; check the source credentials and paging before anything else.

Auth0's user search stops at 1,000 people. Above that the snapshot is treated as partial and nobody is disabled for being missing; blocked users are still disabled.

Run it by hand with `POST /admin/cron/jobs/sync-directory/trigger` (admin), and check the result in the runtime log (`directory_sync` events).

## SCIM

Set `TS_HARNESS_SCIM_TOKEN` to a long random value and give the IdP:

- Tenant URL: `https://<console host>/scim/v2`
- Secret token: the value of `TS_HARNESS_SCIM_TOKEN`

Supported: `Users` and `Groups` with list (`filter` on `userName`, `externalId`, `emails.value`, `displayName`), create, get, replace, patch and delete, plus `ServiceProviderConfig`. Deactivating or deleting a user disables them in the directory immediately; group changes reach teams at the next sync. Without the token every `/scim/v2` route returns 404.

## Offboarding checklist

1. Block or remove the person in the IdP.
2. Wait for the next sync (or run `sync-directory`); confirm the `directory_sync` event lists them as disabled.
3. Programs they own stay; a harness admin can open them and reassign sharing.
