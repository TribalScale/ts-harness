"""Knowledge notes: versioning, scope, and the merged context budget."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tests.pg_support import PgTestCase, requires_pg  # noqa: E402

from memory_routes import knowledge_notes as notes  # noqa: E402
from memory_routes import projects  # noqa: E402


def _note(title: str, body: str, *, kind: str = "avoid", scope: str = "org", updated_at: str = "2026-01-01T00:00:00") -> dict:
    return {
        "id": title,
        "scope": scope,
        "kind": kind,
        "title": title,
        "body": body,
        "why": "",
        "applies_to": [],
        "updated_at": updated_at,
        "exception_to": None,
    }


class ContextSelectionTests(unittest.TestCase):
    def test_org_share_is_reserved_and_unused_share_fills_the_project(self):
        org = [_note("o1", "x" * 90, updated_at="2026-01-03"), _note("o2", "y" * 90, updated_at="2026-01-02")]
        project = [
            _note(f"p{i}", "z" * 90, scope="project", updated_at=f"2026-01-0{i}") for i in range(1, 4)
        ]
        chosen = notes.select_for_context(
            {"org": org, "project": project},
            limits={"max_chars": 300, "max_notes": 15, "shares": {"org": 1 / 3}},
        )
        scopes = [item["scope"] for item in chosen]
        self.assertEqual(scopes.count("org"), 1)
        self.assertEqual(scopes.count("project"), 2)
        self.assertLess(chosen.index(next(item for item in chosen if item["id"] == "o1")), len(chosen))

    def test_exception_hides_the_org_note_and_no_project_uses_the_full_budget(self):
        org = [_note("rule", "x" * 90), _note("other", "y" * 90, updated_at="2026-01-02")]
        project = [_note("except", "keep this", scope="project")]
        project[0]["exception_to"] = "rule"
        hidden = notes.select_for_context(
            {"org": org, "project": project},
            limits={"max_chars": 3000, "max_notes": 15, "shares": {"org": 1 / 3}},
        )
        self.assertNotIn("rule", [item["id"] for item in hidden])
        self.assertIn("except", [item["id"] for item in hidden])
        self.assertIn("other", [item["id"] for item in hidden])

        only_org = notes.select_for_context(
            {"org": [_note("a", "x" * 90), _note("b", "y" * 90, updated_at="2026-01-02")], "project": []},
            limits={"max_chars": 200, "max_notes": 15, "shares": {"org": 1 / 3}},
        )
        self.assertEqual({item["id"] for item in only_org}, {"a", "b"})

    def test_ranking_prefers_a_matching_avoid_note(self):
        ranked = notes.rank_notes(
            [
                _note("howto", "do it", kind="howto", updated_at="2026-02-01"),
                {**_note("avoid-late", "nope", updated_at="2026-01-01"), "applies_to": ["deliver_work_order"]},
                _note("avoid-new", "nope too", updated_at="2026-03-01"),
            ],
            stage="deliver_work_order",
        )
        self.assertEqual(ranked[0]["id"], "avoid-late")
        self.assertEqual(ranked[1]["id"], "avoid-new")


@requires_pg
class KnowledgeNotesPostgresTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM knowledge_note_versions")
            conn.execute("DELETE FROM knowledge_notes")
            conn.execute("DELETE FROM projects")
            conn.commit()

    def test_scope_check_and_crud(self):
        with self.db.connect() as conn:
            project = projects.create_project(conn, {"key": "THD", "name": "THD"})
            with self.assertRaises(Exception):
                conn.execute(
                    """
                    INSERT INTO knowledge_notes (scope, project_id, kind, title, body)
                    VALUES ('org', %s, 'avoid', 'bad', 'bad')
                    """,
                    (project["id"],),
                )
                conn.commit()
            conn.rollback()

            with self.assertRaises(ValueError):
                notes.create_note(conn, {"scope": "org", "project_id": project["id"], "kind": "avoid", "title": "t", "body": "b"})
            with self.assertRaises(ValueError):
                notes.create_note(conn, {"scope": "project", "kind": "avoid", "title": "t", "body": "b"})

            org = notes.create_note(
                conn,
                {"scope": "org", "kind": "avoid", "title": "PR titles", "body": "Start with the Jira key.", "status": "active", "actor": "ada"},
            )
            self.assertEqual((org["status"], org["version"]), ("active", 1))
            project_note = notes.create_note(
                conn,
                {
                    "scope": "project",
                    "project_id": "THD",
                    "kind": "howto",
                    "title": "Routes",
                    "body": "Routes live in asgi_app.py.",
                    "status": "active",
                    "actor": "ada",
                },
            )
            self.assertEqual(project_note["project_id"], project["id"])
            listed = notes.list_notes(conn, {"scope": "project", "project_id": "THD"})["notes"]
            self.assertEqual([item["id"] for item in listed], [project_note["id"]])

            with self.assertRaises(notes.NoteConflict):
                notes.update_note(conn, project_note["id"], {"expected_version": 9, "body": "newer", "actor": "ada"})
            updated = notes.update_note(
                conn, project_note["id"], {"expected_version": 1, "body": "Handlers live in a bff module.", "actor": "ada"}
            )
            self.assertEqual(updated["version"], 2)
            history = notes.list_versions(conn, project_note["id"])["versions"]
            self.assertEqual([item["version"] for item in history], [1, 2])
            restored = notes.restore_note(
                conn, project_note["id"], {"expected_version": 2, "version": 1, "actor": "ada"}
            )
            self.assertEqual(restored["body"], "Routes live in asgi_app.py.")
            self.assertEqual(restored["version"], 3)

            retired = notes.set_note_status(
                conn, project_note["id"], {"status": "retired", "expected_version": 3, "actor": "ada"}
            )
            self.assertEqual(retired["status"], "retired")

    def test_context_budget_exception_and_promotion(self):
        with self.db.connect() as conn:
            project = projects.create_project(conn, {"key": "THD"})
            other = projects.create_project(conn, {"key": "APP"})
            org = notes.create_note(
                conn,
                {"scope": "org", "kind": "avoid", "title": "No main", "body": "x" * 80, "why": "y" * 10, "status": "active"},
            )
            notes.create_note(
                conn,
                {"scope": "org", "kind": "howto", "title": "Keys", "body": "k" * 80, "why": "y" * 10, "status": "active"},
            )
            for index in range(3):
                notes.create_note(
                    conn,
                    {
                        "scope": "project",
                        "project_id": project["id"],
                        "kind": "howto",
                        "title": f"p{index}",
                        "body": "p" * 80,
                        "why": "y" * 10,
                        "status": "active",
                    },
                )
            context = notes.notes_context(
                conn, {"project_id": "THD", "max_chars": 300, "max_notes": 15, "org_share": "0.333333"}
            )
            scopes = [item["scope"] for item in context["notes"]]
            self.assertGreaterEqual(scopes.count("org"), 1)
            self.assertGreaterEqual(scopes.count("project"), 1)
            self.assertGreater(context["omitted"], 0)
            self.assertEqual(notes.get_note(conn, context["notes"][0]["id"])["use_count"], 1)

            notes.create_note(
                conn,
                {
                    "scope": "project",
                    "project_id": project["id"],
                    "kind": "avoid",
                    "title": "Exception",
                    "body": "THD may commit to main.",
                    "exception_to": org["id"],
                    "status": "active",
                },
            )
            hidden = notes.notes_context(conn, {"project_id": project["id"], "max_chars": 3000})
            self.assertNotIn(org["id"], [item["id"] for item in hidden["notes"]])
            org_only = notes.notes_context(conn, {"max_chars": 3000})
            self.assertTrue(org_only["notes"])
            self.assertTrue(all(item["scope"] == "org" for item in org_only["notes"]))

            source = notes.create_note(
                conn,
                {
                    "scope": "project",
                    "project_id": other["id"],
                    "kind": "avoid",
                    "title": "Shared",
                    "body": "Do not add a test framework shim.",
                    "status": "active",
                },
            )
            promoted = notes.promote_note(conn, source["id"], {"actor": "ada"})
            again = notes.promote_note(conn, source["id"], {"actor": "ada"})
            self.assertEqual(promoted["id"], again["id"])
            self.assertEqual(promoted["proposal_action"], "promote")
            approved = notes.set_note_status(
                conn, promoted["id"], {"status": "active", "expected_version": promoted["version"], "actor": "ada"}
            )
            self.assertEqual(approved["status"], "active")
            self.assertEqual(notes.get_note(conn, source["id"])["status"], "retired")

            edit = notes.create_note(
                conn,
                {
                    "scope": "org",
                    "kind": "avoid",
                    "title": "No main",
                    "body": "Never commit straight to main.",
                    "proposal_action": "edit",
                    "revision_of": org["id"],
                    "base_version": 1,
                    "source": "review",
                    "actor": "review",
                },
            )
            notes.update_note(conn, org["id"], {"expected_version": 1, "body": "changed underneath", "actor": "ada"})
            with self.assertRaises(notes.NoteConflict):
                notes.set_note_status(
                    conn, edit["id"], {"status": "active", "expected_version": edit["version"], "actor": "ada"}
                )
            self.assertEqual(notes.get_note(conn, edit["id"])["status"], "proposed")


if __name__ == "__main__":
    unittest.main()
