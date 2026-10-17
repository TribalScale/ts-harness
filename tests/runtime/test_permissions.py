"""Role bundles expand to permissions. Unknown roles grant nothing."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from ts_harness.gateway.access import require_permission
from ts_harness.gateway.auth import AuthError, authenticate_request
from ts_harness.policy.engine import PolicyEngine
from ts_harness.policy.permissions import (
    DEFAULT_BUNDLES,
    PERMISSIONS,
    actor_permissions,
    allows,
    covers_role,
    permissions_for_roles,
    resolve_actor_roles,
)


def _lacks(role: str) -> set[str]:
    return set(PERMISSIONS) - set(DEFAULT_BUNDLES[role])


class PermissionBundleTests(unittest.TestCase):
    def test_each_default_role_lacks_the_permissions_outside_its_bundle(self):
        self.assertEqual(_lacks("viewer"), set(PERMISSIONS) - {"view"})
        self.assertEqual(_lacks("member"), _lacks("viewer"))
        self.assertFalse(allows(permissions_for_roles(["viewer"]), ["start_program"]))
        self.assertFalse(allows(permissions_for_roles(["requester"]), ["approve_gate"]))
        self.assertFalse(allows(permissions_for_roles(["approver"]), ["edit_config", "start_program"]))
        self.assertTrue(allows(permissions_for_roles(["admin"]), ["edit_allowlists"]))
        self.assertEqual(permissions_for_roles(["owner"]), permissions_for_roles(["admin"]))

    def test_default_admin_receives_permissions_declared_on_other_bundles(self):
        from ts_harness.policy.permissions import effective_bundles

        config = SimpleNamespace(
            identity=SimpleNamespace(role_bundles={"delivery_lead": ["view", "start_delivery"]})
        )
        bundles = effective_bundles(config)
        self.assertIn("start_delivery", bundles["admin"])
        self.assertIn("start_delivery", bundles["owner"])
        self.assertEqual(bundles["delivery_lead"], frozenset({"view", "start_delivery"}))
        replaced = SimpleNamespace(
            identity=SimpleNamespace(
                role_bundles={
                    "delivery_lead": ["view", "start_delivery"],
                    "admin": ["view"],
                }
            )
        )
        self.assertNotIn("start_delivery", effective_bundles(replaced)["admin"])

    def test_roles_union_and_unknown_roles_grant_nothing(self):
        combined = permissions_for_roles(["requester", "approver"])
        self.assertIn("start_program", combined)
        self.assertIn("approve_gate", combined)
        self.assertNotIn("edit_config", combined)
        self.assertEqual(permissions_for_roles(["release_manager"]), set())

    def test_configured_bundle_replaces_the_default_without_a_code_change(self):
        config = SimpleNamespace(
            identity=SimpleNamespace(
                role_bundles={"release_manager": ["approve_gate", "view"]},
                service_permissions=None,
            )
        )
        from ts_harness.policy.permissions import effective_bundles

        bundles = effective_bundles(config)
        granted = permissions_for_roles(["release_manager"], bundles)
        self.assertEqual(granted, {"approve_gate", "view"})
        self.assertTrue(covers_role(granted, "release_manager", bundles))
        self.assertTrue(covers_role(permissions_for_roles(["admin"]), "release_manager", bundles))
        self.assertFalse(covers_role(permissions_for_roles(["viewer"]), "release_manager", bundles))

    def test_admin_does_not_cover_an_unknown_role_name(self):
        self.assertFalse(covers_role(permissions_for_roles(["admin"]), "release_manager"))
        self.assertTrue(covers_role(permissions_for_roles(["admin"]), "approver"))

    def test_static_token_ignores_body_roles_and_provider(self):
        auth = authenticate_request(
            {"Authorization": "Bearer s3cret"},
            expected_token="s3cret",
            body={"actor": {"actor_id": "x", "roles": ["admin"], "groups": ["eng"], "auth_provider": "oidc"}},
        )
        self.assertEqual(auth.roles, [])
        self.assertEqual(auth.groups, [])
        self.assertEqual(auth.auth_provider, "static")
        self.assertEqual(auth.actor_id, "x")
        config = SimpleNamespace(identity=SimpleNamespace(role_bundles={}, service_permissions=["view"]))
        self.assertEqual(actor_permissions(auth, config), {"view"})

    def test_service_token_ignores_body_roles(self):
        auth = authenticate_request(
            {"Authorization": "Bearer svc-secret-123"},
            expected_token="svc-secret-123",
            body={"actor": {"actor_id": "runner", "roles": ["admin"], "auth_provider": "oidc"}},
            identity_provider=SimpleNamespace(validate_token=lambda token: {"actor_id": "nope", "roles": ["admin"]}),
        )
        self.assertEqual(auth.roles, [])
        self.assertEqual(auth.auth_provider, "service")
        config = SimpleNamespace(identity=SimpleNamespace(role_bundles={}, service_permissions=["view"]))
        self.assertEqual(actor_permissions(auth, config), {"view"})

    def test_static_token_without_roles_uses_the_service_bundle(self):
        config = SimpleNamespace(identity=SimpleNamespace(role_bundles={}, service_permissions=["view"]))
        static = SimpleNamespace(roles=[], auth_provider="static")
        oidc = SimpleNamespace(roles=["member"], auth_provider="oidc")
        self.assertEqual(actor_permissions(static, config), {"view"})
        self.assertEqual(actor_permissions(oidc, config), {"view"})
        self.assertFalse(allows(actor_permissions(oidc, config), ["edit_config"]))

    def test_evaluate_identity_denies_member_for_admin_and_accepts_a_covering_bundle(self):
        config = SimpleNamespace(
            identity=SimpleNamespace(role_bundles={}, service_permissions=None),
            policy=SimpleNamespace(
                budget_hard_stop_usd=10**9,
                budget_warn_usd=10**9,
                channel_ingress_deny=[],
                approval_required_tools=[],
            ),
        )
        engine = PolicyEngine(config)
        self.assertEqual(engine.evaluate_identity({"roles": ["member"]}, ["admin"]).decision, "deny")
        self.assertEqual(engine.evaluate_identity({"roles": ["admin"]}, ["admin"]).decision, "allow")
        custom = SimpleNamespace(
            identity=SimpleNamespace(
                role_bundles={"release_captain": sorted(PERMISSIONS)},
                service_permissions=None,
            ),
            policy=config.policy,
        )
        custom_engine = PolicyEngine(custom)
        self.assertEqual(
            custom_engine.evaluate_identity({"roles": ["release_captain"]}, ["admin"]).decision,
            "allow",
        )
        self.assertEqual(
            engine.evaluate_identity({"roles": ["admin"]}, ["release_manager"]).decision,
            "deny",
        )


class PermissionCheckTests(unittest.TestCase):
    def test_oidc_member_is_denied_mutations_and_the_denial_is_audited(self):
        events: list[dict] = []
        runtime = SimpleNamespace(
            config=SimpleNamespace(identity=SimpleNamespace(role_bundles={}, service_permissions=None)),
            audit=SimpleNamespace(append=lambda **kwargs: events.append(kwargs)),
        )
        member = SimpleNamespace(roles=["member"], auth_provider="oidc", actor_id="user-1", tenant_id="default")
        for permission in ("edit_config", "approve_gate", "start_program"):
            with self.assertRaises(AuthError):
                require_permission(runtime, member, permission, resource=permission)
        self.assertTrue(events)
        self.assertTrue(all(event["action_type"] == "permission_denied" for event in events))
        self.assertEqual(require_permission(runtime, member, "view", resource="view"), None)
        self.assertFalse(any(event["payload"]["permission"] == "view" and event["action_type"] == "permission_allowed" for event in events))


class GroupRoleTests(unittest.TestCase):
    def test_jwt_group_grants_approver_and_email_override_adds_admin(self):
        config = SimpleNamespace(
            group_roles={"Engineering Approvers": ["approver"]},
            roles={"a@acme.com": ["admin"]},
            scim_enabled=False,
        )
        roles = resolve_actor_roles(
            claim_roles=[],
            groups=["Engineering Approvers"],
            email="A@Acme.com",
            config=config,
        )
        self.assertEqual(roles, ["approver", "admin"])
        granted = permissions_for_roles(roles)
        self.assertIn("approve_gate", granted)
        self.assertIn("edit_config", granted)

    def test_unknown_group_stays_member(self):
        config = SimpleNamespace(group_roles={"Engineering Approvers": ["approver"]}, roles={}, scim_enabled=False)
        self.assertEqual(
            resolve_actor_roles(claim_roles=[], groups=["Someone Else"], config=config),
            ["member"],
        )
        self.assertEqual(permissions_for_roles(["member"]), {"view"})

    def test_scim_group_with_the_same_name_grants_the_same_role(self):
        from ts_harness.gateway.scim import ScimGroup, ScimUser, get_scim_store

        store = get_scim_store()
        store._users["user-1"] = ScimUser(
            id="user-1",
            tenant_id="default",
            external_id="auth0|1",
            user_name="a@acme.com",
            email="a@acme.com",
        )
        store._groups["group-1"] = ScimGroup(
            id="group-1",
            tenant_id="default",
            external_id="grp-1",
            display_name="Engineering Approvers",
            member_ids=["user-1"],
        )
        config = SimpleNamespace(
            group_roles={"Engineering Approvers": ["approver"]},
            roles={},
            scim_enabled=True,
        )
        try:
            roles = resolve_actor_roles(
                claim_roles=[],
                groups=[],
                email="a@acme.com",
                external_id="auth0|1",
                config=config,
            )
        finally:
            store._users.pop("user-1", None)
            store._groups.pop("group-1", None)
        self.assertEqual(roles, ["approver"])
        self.assertIn("approve_gate", permissions_for_roles(roles))
