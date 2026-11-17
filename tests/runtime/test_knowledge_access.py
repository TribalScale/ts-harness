"""Who can write org notes, project notes and exceptions."""

from __future__ import annotations

import unittest

from ts_harness.gateway.auth import AuthContext
from ts_harness.gateway.knowledge_access import can_see_proposal, can_write_knowledge_note

PROJECT = {"id": "p", "team_slug": "web", "tenant_id": "default"}


def _auth(roles: list[str], teams: list[dict[str, str]] | None = None) -> AuthContext:
    return AuthContext(
        actor_id="person",
        tenant_id="default",
        roles=roles,
        auth_provider="oidc",
        teams=teams or [],
    )


class KnowledgeAccessTests(unittest.TestCase):
    def test_admin_can_write_every_scope(self):
        admin = _auth(["admin"])
        self.assertTrue(can_write_knowledge_note(admin, None, scope="org"))
        self.assertTrue(can_write_knowledge_note(admin, None, scope="project", project=PROJECT))
        self.assertTrue(
            can_write_knowledge_note(admin, None, scope="project", project=PROJECT, exception_to="org-1")
        )

    def test_manager_can_write_project_notes_only(self):
        manager = _auth(["member"], [{"slug": "web", "role": "manager"}])
        self.assertFalse(can_write_knowledge_note(manager, None, scope="org"))
        self.assertTrue(can_write_knowledge_note(manager, None, scope="project", project=PROJECT))
        self.assertFalse(
            can_write_knowledge_note(manager, None, scope="project", project=PROJECT, exception_to="org-1")
        )
        other = {**PROJECT, "team_slug": "data"}
        self.assertFalse(can_write_knowledge_note(manager, None, scope="project", project=other))

    def test_a_non_manager_cannot_write_project_notes(self):
        member = _auth(["member"], [{"slug": "web", "role": "member"}])
        self.assertFalse(can_write_knowledge_note(member, None, scope="project", project=PROJECT))

    def test_inbox_visibility_follows_scope(self):
        manager = _auth(["member"], [{"slug": "web", "role": "manager"}])
        admin = _auth(["admin"])
        project_note = {"scope": "project", "proposal_action": "add", "exception_to": None}
        org_note = {"scope": "org", "proposal_action": "add", "exception_to": None}
        promotion = {"scope": "org", "proposal_action": "promote", "exception_to": None}
        exception = {"scope": "project", "proposal_action": "add", "exception_to": "org-1"}
        self.assertTrue(can_see_proposal(manager, None, project_note, PROJECT))
        self.assertFalse(can_see_proposal(manager, None, org_note, None))
        self.assertFalse(can_see_proposal(manager, None, promotion, PROJECT))
        self.assertFalse(can_see_proposal(manager, None, exception, PROJECT))
        self.assertTrue(can_see_proposal(admin, None, org_note, None))
        self.assertTrue(can_see_proposal(admin, None, exception, PROJECT))


if __name__ == "__main__":
    unittest.main()
