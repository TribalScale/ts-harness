"""Teams and built-in roles from ts-harness-* identity groups."""

from __future__ import annotations

import unittest

from tests.runtime.oidc_fixtures import AUDIENCE, CLIENT_ID, ISSUER, make_token, provider_with_keys
from ts_harness.config.schema import IdentityConfig, TeamGroupsConfig
from ts_harness.gateway.auth import authenticate_request
from ts_harness.policy.permissions import resolve_actor_roles, resolve_team_memberships


class ResolveTeamMembershipsTests(unittest.TestCase):
    def test_maps_member_approver_and_manager_groups(self):
        memberships = resolve_team_memberships(
            [
                "ts-harness-users",
                "ts-harness-team-payments",
                "ts-harness-team-payments-approvers",
                "TS-Harness-Team-Platform-Managers",
            ]
        )
        self.assertEqual(
            memberships,
            [
                {"slug": "payments", "role": "member"},
                {"slug": "payments", "role": "approver"},
                {"slug": "platform", "role": "manager"},
            ],
        )

    def test_ignores_invalid_slugs_and_duplicates(self):
        memberships = resolve_team_memberships(
            ["ts-harness-team-", "ts-harness-team-bad slug", "ts-harness-team-a", "ts-harness-team-a"]
        )
        self.assertEqual(memberships, [{"slug": "a", "role": "member"}])

    def test_custom_prefix_and_suffixes(self):
        config = IdentityConfig(team_groups=TeamGroupsConfig(prefix="th-", approver_suffix="-ok", manager_suffix="-boss"))
        self.assertEqual(
            resolve_team_memberships(["th-web-ok", "th-web-boss", "ts-harness-team-web"], config),
            [{"slug": "web", "role": "approver"}, {"slug": "web", "role": "manager"}],
        )


class DefaultGroupRoleTests(unittest.TestCase):
    def test_standard_groups_get_built_in_roles(self):
        roles = resolve_actor_roles(
            groups=["ts-harness-users", "ts-harness-team-payments-approvers"], config=IdentityConfig()
        )
        self.assertEqual(roles, ["requester", "approver"])

    def test_admin_group_is_admin(self):
        self.assertEqual(resolve_actor_roles(groups=["ts-harness-admins"], config=IdentityConfig()), ["admin"])

    def test_configured_group_roles_override_defaults(self):
        config = IdentityConfig(group_roles={"ts-harness-users": ["viewer"]})
        self.assertEqual(resolve_actor_roles(groups=["ts-harness-users"], config=config), ["viewer"])

    def test_team_member_group_alone_is_member(self):
        self.assertEqual(resolve_actor_roles(groups=["ts-harness-team-payments"], config=IdentityConfig()), ["member"])


class AuthContextTeamsTests(unittest.TestCase):
    def test_oidc_context_carries_teams(self):
        config = IdentityConfig(
            provider="oidc", preset="auth0", issuer=ISSUER, audience=AUDIENCE, client_id=CLIENT_ID, jwks_url="x"
        )
        token = make_token(
            email="alice@example.com",
            **{"https://ts-harness/groups": ["ts-harness-users", "ts-harness-team-payments-approvers"]},
        )
        auth = authenticate_request(
            {"Authorization": f"Bearer {token}"},
            expected_token="service-token",
            identity_provider=provider_with_keys(config),
        )
        self.assertEqual(auth.teams, [{"slug": "payments", "role": "approver"}])
        self.assertFalse(auth.is_service)

    def test_service_token_is_marked_service(self):
        config = IdentityConfig(provider="oidc", issuer=ISSUER, audience=AUDIENCE, jwks_url="x")
        auth = authenticate_request(
            {"Authorization": "Bearer service-token"},
            expected_token="service-token",
            identity_provider=provider_with_keys(config),
        )
        self.assertTrue(auth.is_service)
        self.assertEqual(auth.teams, [])


if __name__ == "__main__":
    unittest.main()
