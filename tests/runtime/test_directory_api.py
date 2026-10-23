"""People directory: memory API functions, routes and runtime linking."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.pg_support import PgTestCase, requires_pg
from ts_harness.gateway import directory_links

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from memory_routes import directory  # noqa: E402


def load_gateway():
    spec = importlib.util.spec_from_file_location("memory_gateway", REPO_ROOT / "scripts" / "memory-gateway.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CleanTeamsTests(unittest.TestCase):
    def test_drops_invalid_entries(self):
        self.assertEqual(
            directory._clean_teams(
                [
                    {"slug": "payments", "role": "approver"},
                    {"slug": "Bad Slug"},
                    {"slug": "web", "role": "owner"},
                    "nope",
                    {"slug": "payments", "role": "approver"},
                ]
            ),
            [("payments", "approver")],
        )

    def test_display_name(self):
        self.assertEqual(directory.team_display_name("data-platform"), "Data Platform")


class GatewayRoutesTests(unittest.TestCase):
    def test_gateway_exposes_directory_functions(self):
        gateway = load_gateway()
        self.assertIs(gateway.upsert_directory_user, directory.upsert_directory_user)
        self.assertIs(gateway.search_directory, directory.search_directory)


@requires_pg
class DirectoryPostgresTests(PgTestCase):
    def test_upsert_creates_user_teams_and_memberships(self):
        with self.db.connect() as conn:
            result = directory.upsert_directory_user(
                conn,
                {
                    "subject": "auth0|alice",
                    "email": "Alice@Example.com",
                    "display_name": "Alice",
                    "roles": ["Approver", "requester"],
                    "teams": [{"slug": "payments", "role": "member"}, {"slug": "payments", "role": "approver"}],
                },
            )
            self.assertEqual(len(result["teams"]), 2)
            found = directory.get_directory_user(conn, tenant_id="default", email="alice@example.com")
        self.assertTrue(found["found"])
        self.assertEqual(found["user"]["subject"], "auth0|alice")
        self.assertEqual(found["user"]["roles"], ["approver", "requester"])
        self.assertEqual(
            found["user"]["teams"],
            [{"slug": "payments", "role": "approver"}, {"slug": "payments", "role": "member"}],
        )

    def test_membership_is_replaced_on_next_sign_in(self):
        with self.db.connect() as conn:
            directory.upsert_directory_user(
                conn, {"subject": "auth0|bob", "teams": [{"slug": "web", "role": "member"}]}
            )
            directory.upsert_directory_user(
                conn, {"subject": "auth0|bob", "teams": [{"slug": "mobile", "role": "member"}]}
            )
            user = directory.get_directory_user(conn, tenant_id="default", subject="auth0|bob")["user"]
        self.assertEqual(user["teams"], [{"slug": "mobile", "role": "member"}])

    def test_upsert_without_teams_keeps_memberships(self):
        with self.db.connect() as conn:
            directory.upsert_directory_user(
                conn, {"subject": "auth0|cara", "teams": [{"slug": "ops", "role": "manager"}]}
            )
            directory.upsert_directory_user(conn, {"subject": "auth0|cara", "roles": ["admin"]})
            directory.upsert_directory_user(conn, {"subject": "auth0|cara", "display_name": "Cara"})
            user = directory.get_directory_user(conn, tenant_id="default", subject="auth0|cara")["user"]
        self.assertEqual(user["display_name"], "Cara")
        self.assertEqual(user["roles"], ["admin"])
        self.assertEqual(user["teams"], [{"slug": "ops", "role": "manager"}])

    def test_search_finds_people_and_teams_and_skips_disabled(self):
        with self.db.connect() as conn:
            directory.upsert_directory_user(
                conn,
                {
                    "subject": "auth0|dana",
                    "email": "dana@example.com",
                    "display_name": "Dana Payments",
                    "teams": [{"slug": "payments-core", "role": "member"}],
                },
            )
            directory.upsert_directory_user(conn, {"subject": "auth0|gone", "display_name": "Payments Leaver"})
            with conn.cursor() as cur:
                cur.execute("UPDATE directory_users SET disabled_at = now() WHERE subject = 'auth0|gone'")
            conn.commit()
            result = directory.search_directory(conn, tenant_id="default", q="payments")
        subjects = [u["subject"] for u in result["users"]]
        self.assertIn("auth0|dana", subjects)
        self.assertNotIn("auth0|gone", subjects)
        self.assertIn("payments-core", [t["slug"] for t in result["teams"]])

    def test_search_escapes_like_wildcards(self):
        with self.db.connect() as conn:
            result = directory.search_directory(conn, tenant_id="default", q="%")
        self.assertEqual(result["users"], [])

    def test_unknown_user(self):
        with self.db.connect() as conn:
            self.assertEqual(
                directory.get_directory_user(conn, tenant_id="default", subject="nobody"), {"found": False}
            )


class RecordSignInTests(unittest.TestCase):
    def setUp(self):
        directory_links.clear_cache()

    def test_oidc_sign_in_upserts_teams(self):
        memory = mock.Mock()
        auth = SimpleNamespace(
            auth_provider="oidc",
            tenant_id="default",
            actor_id="auth0|alice",
            email="alice@example.com",
            display_name="Alice",
            teams=[{"slug": "payments", "role": "member"}],
            roles=["requester"],
        )
        directory_links.record_sign_in(auth, memory=memory)
        memory.directory_upsert_user.assert_called_once_with(
            tenant_id="default",
            subject="auth0|alice",
            email="alice@example.com",
            display_name="Alice",
            teams=[{"slug": "payments", "role": "member"}],
            roles=["requester"],
        )

    def test_static_sign_in_is_not_recorded(self):
        memory = mock.Mock()
        directory_links.record_sign_in(SimpleNamespace(auth_provider="static"), memory=memory)
        memory.directory_upsert_user.assert_not_called()

    def test_directory_outage_does_not_fail_sign_in(self):
        memory = mock.Mock()
        memory.directory_upsert_user.side_effect = RuntimeError("down")
        auth = SimpleNamespace(
            auth_provider="oidc", tenant_id="default", actor_id="a", email=None, display_name=None, teams=[]
        )
        self.assertIsNone(directory_links.record_sign_in(auth, memory=memory))


class SlackLinkTests(unittest.TestCase):
    def setUp(self):
        directory_links.clear_cache()

    def test_linked_slack_user_carries_subject_and_teams(self):
        memory = mock.Mock()
        memory.directory_user.return_value = {
            "found": True,
            "user": {"subject": "auth0|alice", "teams": [{"slug": "payments", "role": "approver"}], "disabled": False},
        }
        metadata = directory_links.link_channel_session({}, "Alice@Example.com", "default", memory=memory)
        self.assertEqual(metadata["directory_subject"], "auth0|alice")
        self.assertTrue(metadata["directory_linked"])
        memory.directory_user.assert_called_once_with(tenant_id="default", email="alice@example.com")

    def test_lookup_is_cached(self):
        memory = mock.Mock()
        memory.directory_user.return_value = {"found": False}
        directory_links.link_channel_session({}, "x@example.com", "default", memory=memory)
        directory_links.link_channel_session({}, "x@example.com", "default", memory=memory)
        memory.directory_user.assert_called_once()

    def test_disabled_or_unknown_user_is_unlinked(self):
        memory = mock.Mock()
        memory.directory_user.return_value = {"found": True, "user": {"subject": "s", "disabled": True}}
        metadata = directory_links.link_channel_session(
            {"directory_subject": "old"}, "gone@example.com", "default", memory=memory
        )
        self.assertFalse(metadata["directory_linked"])
        self.assertNotIn("directory_subject", metadata)

    def test_no_email_is_unlinked_without_lookup(self):
        memory = mock.Mock()
        metadata = directory_links.link_channel_session({}, None, "default", memory=memory)
        self.assertFalse(metadata["directory_linked"])
        memory.directory_user.assert_not_called()


if __name__ == "__main__":
    unittest.main()
