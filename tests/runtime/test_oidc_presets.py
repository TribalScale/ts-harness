"""Provider presets, configurable claims and sign-in rules for OIDC tokens."""

from __future__ import annotations

import unittest
from unittest import mock

from tests.runtime.oidc_fixtures import AUDIENCE, CLIENT_ID, ISSUER, make_token, provider_with_keys
from ts_harness.config.schema import IdentityConfig, _parse_identity
from ts_harness.gateway import identity as identity_mod
from ts_harness.gateway.identity_presets import identity_setting, uses_legacy_endpoints


def _config(**kwargs) -> IdentityConfig:
    base = {"provider": "oidc", "issuer": ISSUER, "audience": AUDIENCE, "client_id": CLIENT_ID, "jwks_url": "x"}
    base.update(kwargs)
    return IdentityConfig(**base)


class PresetSettingTests(unittest.TestCase):
    def test_no_preset_keeps_legacy_defaults(self):
        config = _config()
        self.assertEqual(identity_setting(config, "token_type"), "access")
        self.assertEqual(identity_setting(config, "groups_claim"), "groups")
        self.assertTrue(uses_legacy_endpoints(config))

    def test_auth0_preset_uses_namespaced_claims(self):
        config = _config(preset="auth0")
        self.assertEqual(identity_setting(config, "groups_claim"), "https://ts-harness/groups")
        self.assertEqual(identity_setting(config, "roles_claim"), "https://ts-harness/roles")
        self.assertEqual(identity_setting(config, "directory"), "auth0_management")

    def test_google_preset_validates_id_tokens(self):
        config = _config(preset="google")
        self.assertEqual(identity_setting(config, "token_type"), "id")
        self.assertEqual(identity_setting(config, "groups_source"), "google_directory")
        self.assertFalse(uses_legacy_endpoints(config))

    def test_explicit_setting_beats_preset(self):
        config = _config(preset="okta", token_type="id")
        self.assertEqual(identity_setting(config, "token_type"), "id")


class ParseIdentityTests(unittest.TestCase):
    def test_parses_new_keys(self):
        config = _parse_identity(
            {
                "provider": "oidc",
                "preset": "Auth0",
                "token_type": "access",
                "groups_claim": "https://ts-harness/groups",
                "allowed_domains": ["Example.com", "@corp.example"],
                "team_groups": {"prefix": "th-team-"},
            }
        )
        self.assertEqual(config.preset, "auth0")
        self.assertEqual(config.allowed_domains, ["example.com", "corp.example"])
        self.assertEqual(config.team_groups.prefix, "th-team-")
        self.assertEqual(config.team_groups.approver_suffix, "-approvers")

    def test_unrendered_placeholders_mean_unset(self):
        config = _parse_identity(
            {"preset": "{{OIDC_PRESET}}", "token_type": "", "allowed_domains": "{{OIDC_ALLOWED_DOMAINS}}"}
        )
        self.assertIsNone(config.preset)
        self.assertIsNone(config.token_type)
        self.assertEqual(config.allowed_domains, [])

    def test_comma_separated_domains(self):
        self.assertEqual(_parse_identity({"allowed_domains": "a.com, b.com"}).allowed_domains, ["a.com", "b.com"])

    def test_unknown_preset_is_rejected(self):
        with self.assertRaises(ValueError):
            _parse_identity({"preset": "keycloak"})


class ClaimMappingTests(unittest.TestCase):
    def test_auth0_groups_and_roles_claims(self):
        provider = provider_with_keys(_config(preset="auth0"))
        identity = provider.validate_token(
            make_token(
                email="alice@example.com",
                name="Alice",
                **{
                    "https://ts-harness/groups": ["ts-harness-users", "ts-harness-team-payments"],
                    "https://ts-harness/roles": ["ts-harness-admins"],
                },
            )
        )
        self.assertEqual(identity["groups"], ["ts-harness-users", "ts-harness-team-payments"])
        self.assertEqual(identity["display_name"], "Alice")

    def test_namespaced_profile_claims_from_auth0_access_tokens(self):
        provider = provider_with_keys(_config(preset="auth0", allowed_domains=["example.com"]))
        identity = provider.validate_token(
            make_token(
                **{
                    "https://ts-harness/email": "bob@example.com",
                    "https://ts-harness/email_verified": True,
                    "https://ts-harness/name": "Bob",
                }
            )
        )
        self.assertEqual((identity["email"], identity["display_name"]), ("bob@example.com", "Bob"))
        with self.assertRaisesRegex(ValueError, "not verified"):
            provider.validate_token(
                make_token(**{"https://ts-harness/email": "bob@example.com", "https://ts-harness/email_verified": False})
            )

    def test_custom_groups_claim(self):
        provider = provider_with_keys(_config(groups_claim="org_groups"))
        identity = provider.validate_token(make_token(org_groups=["ts-harness-users"]))
        self.assertEqual(identity["groups"], ["ts-harness-users"])

    def test_legacy_groups_claim_still_read(self):
        provider = provider_with_keys(_config(preset="auth0"))
        identity = provider.validate_token(make_token(groups=["ts-harness-users"]))
        self.assertEqual(identity["groups"], ["ts-harness-users"])

    def test_roles_suffix_claim_is_not_read_as_groups(self):
        provider = provider_with_keys(_config())
        identity = provider.validate_token(make_token(**{"https://other/roles": ["x"]}))
        self.assertEqual(identity["groups"], [])


class SignInRuleTests(unittest.TestCase):
    def test_unverified_email_is_rejected(self):
        provider = provider_with_keys(_config())
        with self.assertRaisesRegex(ValueError, "not verified"):
            provider.validate_token(make_token(email="a@example.com", email_verified=False))

    def test_missing_email_verified_claim_is_allowed(self):
        provider = provider_with_keys(_config())
        self.assertEqual(provider.validate_token(make_token(email="a@example.com"))["email"], "a@example.com")

    def test_allowed_domains_by_email(self):
        provider = provider_with_keys(_config(allowed_domains=["example.com"]))
        provider.validate_token(make_token(email="a@example.com"))
        with self.assertRaisesRegex(ValueError, "domain"):
            provider.validate_token(make_token(email="a@evil.test"))

    def test_allowed_domains_by_google_hd_claim(self):
        provider = provider_with_keys(_config(allowed_domains=["example.com"]))
        provider.validate_token(make_token(hd="example.com"))

    def test_allowed_domains_reject_missing_email(self):
        provider = provider_with_keys(_config(allowed_domains=["example.com"]))
        with self.assertRaises(ValueError):
            provider.validate_token(make_token())

    def test_id_token_mode_checks_client_id_audience(self):
        provider = provider_with_keys(_config(token_type="id"))
        provider.validate_token(make_token(aud=CLIENT_ID))
        with self.assertRaises(Exception):
            provider.validate_token(make_token(aud=AUDIENCE))


class GroupResolverTests(unittest.TestCase):
    def tearDown(self):
        identity_mod.register_group_resolver("google_directory", None)

    def test_resolver_groups_are_merged(self):
        resolver = mock.Mock()
        resolver.resolve_groups.return_value = ["ts-harness-team-payments"]
        identity_mod.register_group_resolver("google_directory", resolver)
        provider = provider_with_keys(_config(preset="google"))
        identity = provider.validate_token(make_token(aud=CLIENT_ID, email="a@example.com"))
        self.assertEqual(identity["groups"], ["ts-harness-team-payments"])
        resolver.resolve_groups.assert_called_once()

    def test_resolver_failure_falls_back_to_token_groups(self):
        resolver = mock.Mock()
        resolver.resolve_groups.side_effect = RuntimeError("directory down")
        identity_mod.register_group_resolver("google_directory", resolver)
        provider = provider_with_keys(_config(preset="google"))
        identity = provider.validate_token(make_token(aud=CLIENT_ID, groups=["ts-harness-users"]))
        self.assertEqual(identity["groups"], ["ts-harness-users"])

    def test_missing_resolver_falls_back_to_token_groups(self):
        provider = provider_with_keys(_config(preset="google"))
        identity = provider.validate_token(make_token(aud=CLIENT_ID, groups=["ts-harness-users"]))
        self.assertEqual(identity["groups"], ["ts-harness-users"])


if __name__ == "__main__":
    unittest.main()
