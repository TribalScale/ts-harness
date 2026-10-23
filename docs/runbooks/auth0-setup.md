# Auth0 setup

Run this once the Auth0 tenant exists. Until then, [oidc-dev-issuer.md](oidc-dev-issuer.md) issues tokens of the same shape locally.

## 1. API

Applications → APIs → Create API:

- Name: `TS Harness`
- Identifier (audience): `https://ts-harness/api`
- Signing algorithm: RS256
- Settings → RBAC: enable **RBAC** and **Add Permissions in the Access Token**
- Settings → Allow Offline Access: off

## 2. Single-page application

Applications → Create Application → Single Page Web Applications, name `TS Harness console`:

- Allowed Callback URLs: `https://<console host>/` (and `http://localhost:5173/` for local development)
- Allowed Logout URLs and Allowed Web Origins: the same origins
- Refresh token rotation: on
- Connections tab: enable only the enterprise connections from step 4

Note the **Domain** and **Client ID**.

## 3. Post-login Action

Actions → Library → Build Custom, trigger **Login / Post Login**, name `ts-harness-claims`:

```javascript
exports.onExecutePostLogin = async (event, api) => {
  const ns = "https://ts-harness/";
  const roles = (event.authorization && event.authorization.roles) || [];
  const groups = new Set([
    ...((event.user.app_metadata && event.user.app_metadata.groups) || []),
    ...(Array.isArray(event.user.groups) ? event.user.groups : []),
  ]);
  for (const role of roles) {
    if (role.toLowerCase().startsWith("ts-harness-")) groups.add(role);
  }
  if (!groups.has("ts-harness-users") && !groups.has("ts-harness-admins")) {
    api.access.deny("You do not have access to TS Harness. Ask an admin to add you to ts-harness-users.");
    return;
  }
  const harnessRoles = roles.filter((role) => !role.toLowerCase().startsWith("ts-harness-"));
  for (const token of [api.accessToken, api.idToken]) {
    token.setCustomClaim(ns + "roles", harnessRoles);
    token.setCustomClaim(ns + "groups", [...groups]);
  }
  api.accessToken.setCustomClaim(ns + "email", event.user.email);
  api.accessToken.setCustomClaim(ns + "email_verified", event.user.email_verified);
  api.accessToken.setCustomClaim(ns + "name", event.user.name);
};
```

Deploy it and drag it into the Login flow.

Access tokens carry no profile, so the Action copies email, verification and name under the `https://ts-harness/` namespace; the runtime reads those when the standard claims are missing.

## 4. Enterprise connections

Authentication → Enterprise:

- **Microsoft Entra ID**: register an app in Entra, grant `User.Read` and `GroupMember.Read.All`, enable "Extended profile" and "Get user groups" so groups reach `event.user.groups`. Set the connection's domain so Home Realm Discovery picks it.
- **Google Workspace**: create an OAuth client in the Workspace's Google Cloud project, enable the Admin SDK, and turn on "Retrieve user groups" in the connection.

Authentication → Database: disable the default `Username-Password-Authentication` connection for the SPA, so only company accounts can sign in.

## 5. Roles and groups

User Management → Roles:

- Harness roles: `admin`, `approver`, `requester` (and any custom roles named in `roles.bundles`)
- Group roles: `ts-harness-users`, `ts-harness-admins`, `ts-harness-team-<slug>`, `ts-harness-team-<slug>-approvers`, `ts-harness-team-<slug>-managers`

Assign them to people, or map them from Entra and Google groups with `app_metadata.groups`. Everyone who should sign in needs `ts-harness-users` (or `ts-harness-admins`).

## 6. Management API client for directory sync

Applications → Create Application → Machine to Machine, name `TS Harness directory sync`, authorize it for the **Auth0 Management API** with only `read:users`, `read:roles` and `read:role_members`. Note its Client ID and Client Secret; they go in Secrets Manager, never in YAML or Terraform state.

## 7. Configure the runtime

Install contract (`runtime:` section) or Terraform variables:

| Setting | Value |
| --- | --- |
| `identity_provider` | `oidc` |
| `oidc_preset` | `auth0` |
| `oidc_issuer` | `https://<tenant>.<region>.auth0.com/` (trailing slash) |
| `oidc_audience` | `https://ts-harness/api` |
| `oidc_client_id` | SPA Client ID |
| `oidc_allowed_domains` | Company domains, comma-separated |
| `identity_directory` | `auth0_management` (the preset default) |

Runtime secrets (env file written by phase 11):

- `AUTH0_MGMT_CLIENT_ID`, `AUTH0_MGMT_CLIENT_SECRET`
- `TS_HARNESS_PROGRAM_ACCESS=audit` for the first week

The JWKS URL and endpoints come from discovery; leave `oidc_jwks_url`, `oidc_authorization_endpoint` and `oidc_token_endpoint` empty.

## 8. Verify

1. Sign in to the console with a company account. The `POST /admin/auth/session` response (browser devtools) lists your roles, permissions and teams.
2. The Share dialog on a program finds you and your teams by name.
3. Trigger `POST /admin/cron/jobs/sync-directory/trigger`; the runtime log shows a `directory_sync` event with the expected number of people and no `directory_sync_disable_skipped`.
4. Send a Slack message from the same email; the session is linked (no "not linked yet" reply).
5. A person without `ts-harness-users` is refused by Auth0 with the Action's message.

## 9. Lock it down

- Leave `TS_HARNESS_OIDC_STATIC_FALLBACK` unset (false), so people cannot sign in to the console with the gateway token. The gateway token stays for services and the workflow runner.
- After a week of `program_access_would_deny` review, set `TS_HARNESS_PROGRAM_ACCESS=enforce`.
- Rotate the Management API client secret with the other secrets ([secret-rotation.md](secret-rotation.md)).
