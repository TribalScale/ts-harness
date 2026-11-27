"""Hybrid recall for knowledge notes: RRF, query terms, ranking, fallbacks and the Memory page BFF."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from tests.pg_support import PgTestCase, requires_pg  # noqa: E402

from memory_routes import knowledge_notes as notes  # noqa: E402
from memory_routes import knowledge_search as search  # noqa: E402
from memory_routes import projects  # noqa: E402
from ts_harness.gateway import knowledge_bff  # noqa: E402
from ts_harness.gateway.auth import AuthContext  # noqa: E402
from ts_harness.workflows.hooks import hydrate_input  # noqa: E402


def _note(note_id: str, *, kind: str = "howto", scope: str = "project", updated_at: str = "2026-01-01") -> dict:
    return {
        "id": note_id,
        "scope": scope,
        "kind": kind,
        "title": note_id,
        "body": "x" * 40,
        "why": "",
        "applies_to": [],
        "updated_at": updated_at,
        "exception_to": None,
    }


class FusionTests(unittest.TestCase):
    def test_rrf_rewards_notes_found_by_both_lists(self):
        scores = search.rrf([["a", "b", "c"], ["c", "a"]])
        self.assertEqual(sorted(scores, key=scores.get, reverse=True)[0], "a")
        self.assertGreater(scores["c"], scores["b"])
        self.assertAlmostEqual(scores["b"], 1 / (search.RRF_K + 2))

    def test_query_terms_are_safe_for_to_tsquery(self):
        expr = search.tsquery("Use the migration lock! Don't drop 'tables' | & :* (now)")
        self.assertEqual(expr, "migration | lock | don | drop | tables | now")
        self.assertEqual(search.tsquery("a | &"), "")
        self.assertTrue(search.tsquery("database", prefix=True).endswith(":*"))

    def test_query_embedding_is_cached_and_failure_returns_none(self):
        calls = []

        def embed(text):
            calls.append(text)
            return "[0.1,0.2]"

        self.assertEqual(search.embed_query(embed, "cache me please"), "[0.1,0.2]")
        self.assertEqual(search.embed_query(embed, "cache me please"), "[0.1,0.2]")
        self.assertEqual(len(calls), 1)

        def broken(_text):
            raise RuntimeError("EMBEDDINGS_PROVIDER must be set")

        self.assertIsNone(search.embed_query(broken, "provider is down"))
        self.assertIsNone(search.embed_query(None, "no embedder"))
        with mock.patch.dict("os.environ", {"KNOWLEDGE_NOTES_VECTOR": "off"}):
            self.assertIsNone(search.embed_query(embed, "switched off"))

    def test_relevance_is_empty_when_both_lists_fail(self):
        class Broken:
            def transaction(self):
                raise RuntimeError("no database")

        self.assertEqual(search.relevance(Broken(), ["a"], "migration lock", None), {})
        self.assertEqual(search.relevance(Broken(), [], "migration lock", None), {})


class RankingTests(unittest.TestCase):
    def test_relevance_orders_after_applies_to_and_before_kind_and_recency(self):
        matched = {**_note("matched", updated_at="2025-01-01"), "applies_to": ["deliver_work_order"]}
        relevant = _note("relevant", updated_at="2025-06-01")
        newest_avoid = _note("newest-avoid", kind="avoid", updated_at="2026-06-01")
        ranked = notes.rank_notes(
            [newest_avoid, relevant, matched],
            stage="deliver_work_order",
            relevance={"relevant": 0.03},
        )
        self.assertEqual([n["id"] for n in ranked], ["matched", "relevant", "newest-avoid"])

    def test_without_relevance_the_old_order_holds(self):
        ranked = notes.rank_notes([_note("howto", updated_at="2026-03-01"), _note("avoid", kind="avoid")])
        self.assertEqual([n["id"] for n in ranked], ["avoid", "howto"])

    def test_budget_still_applies_with_relevance(self):
        project = [_note(f"p{i}") for i in range(5)]
        chosen = notes.select_for_context(
            {"org": [], "project": project},
            limits={"max_chars": 100, "max_notes": 15, "shares": {"org": 1 / 3}},
            relevance={"p4": 0.5, "p3": 0.4},
        )
        self.assertEqual([n["id"] for n in chosen], ["p4", "p3"])


class DispatchTests(unittest.TestCase):
    def test_the_run_text_is_sent_and_an_old_client_still_works(self):
        class Client:
            def __init__(self):
                self.calls = []

            def knowledge_notes_context(self, **kwargs):
                self.calls.append(kwargs)
                return {"notes": [{"id": "n"}]}

        client = Client()
        program = {"project_ref": "p1", "tenant_id": "default", "summary": "Add CSV export"}
        text = hydrate_input._recall_text(program, {"work_order_body": "Export orders as CSV"})
        self.assertIn("Add CSV export", text)
        self.assertIn("Export orders as CSV", text)
        self.assertEqual(hydrate_input._load_knowledge_notes(client, program, "deliver_work_order", text), [{"id": "n"}])
        self.assertEqual(client.calls[0]["query_text"], text)

        class OldClient:
            def knowledge_notes_context(self, *, tenant_id, project_id, stage):
                return {"notes": [{"id": "old"}]}

        self.assertEqual(hydrate_input._load_knowledge_notes(OldClient(), program, "s", text), [{"id": "old"}])

        class Failing:
            def knowledge_notes_context(self, **_kwargs):
                raise RuntimeError("memory API down")

        self.assertEqual(hydrate_input._load_knowledge_notes(Failing(), program, "s", text), [])


def _auth(roles, teams=None):
    return AuthContext(actor_id="person", tenant_id="default", roles=roles, auth_provider="oidc", teams=teams or [])


class _Memory:
    def __init__(self, found):
        self.found = found
        self.search_args = None

    def list_projects(self, **_kwargs):
        return {"projects": [{"id": "p1", "key": "PROJ", "name": "Project", "team_slug": "web", "tenant_id": "default"}]}

    def get_project(self, project_id):
        return {"id": "p1", "key": "PROJ", "name": "Project", "team_slug": "web", "tenant_id": "default"}

    def search_knowledge_notes(self, **kwargs):
        self.search_args = kwargs
        return {"notes": self.found, "mode": "text"}

    def get_knowledge_note(self, note_id):
        return next(note for note in self.found if note["id"] == note_id)

    def knowledge_note_versions(self, _note_id):
        return {"versions": [{"version": 1, "changed_by": "harness-drafter", "change_reason": "drafted"}]}


FOUND = [
    {"id": "org-active", "tenant_id": "default", "scope": "org", "status": "active", "evidence": []},
    {"id": "org-proposed", "tenant_id": "default", "scope": "org", "status": "proposed", "proposal_action": "add", "evidence": []},
    {
        "id": "proj-proposed",
        "tenant_id": "default",
        "scope": "project",
        "project_id": "p1",
        "status": "proposed",
        "proposal_action": "add",
        "source": "drafted",
        "evidence": [{"type": "drafted_from", "ref": "wf-1", "url": "https://github.com/org/api/pull/7", "excerpt": "ci"}],
    },
    {"id": "other-project", "tenant_id": "default", "scope": "project", "project_id": "gone", "status": "active", "evidence": []},
]


class MemoryPageBffTests(unittest.TestCase):
    def test_manager_sees_active_notes_and_own_project_proposals(self):
        memory = _Memory(FOUND)
        manager = _auth(["member"], [{"slug": "web", "role": "manager"}])
        result = knowledge_bff.search_notes_admin(memory, manager, None, {"q": "ci", "project_id": "PROJ", "kind": "avoid"})
        self.assertEqual(memory.search_args["project_id"], "p1")
        self.assertEqual(memory.search_args["q"], "ci")
        self.assertEqual(memory.search_args["kind"], "avoid")
        ids = {note["id"]: note for note in result["notes"]}
        self.assertEqual(set(ids), {"org-active", "proj-proposed"})
        self.assertFalse(ids["org-active"]["can_write"])
        self.assertTrue(ids["proj-proposed"]["can_write"])
        self.assertEqual(ids["proj-proposed"]["project_key"], "PROJ")
        self.assertEqual(result["mode"], "text")
        self.assertEqual(result["projects"][0]["key"], "PROJ")

    def test_member_reads_but_cannot_write_or_see_proposals(self):
        member = _auth(["member"], [{"slug": "web", "role": "member"}])
        result = knowledge_bff.search_notes_admin(_Memory(FOUND), member, None, {})
        self.assertEqual([note["id"] for note in result["notes"]], ["org-active"])
        with self.assertRaises(LookupError):
            knowledge_bff.note_detail_admin(_Memory(FOUND), member, None, "proj-proposed")

    def test_admin_detail_has_history_and_drafted_from(self):
        admin = _auth(["admin"])
        detail = knowledge_bff.note_detail_admin(_Memory(FOUND), admin, None, "proj-proposed")
        self.assertTrue(detail["can_write"])
        self.assertEqual(detail["project"]["key"], "PROJ")
        self.assertEqual(detail["drafted_from"][0]["ref"], "wf-1")
        self.assertEqual(detail["versions"][0]["changed_by"], "harness-drafter")

    def test_unknown_project_filter_is_not_found(self):
        with self.assertRaises(LookupError):
            knowledge_bff.search_notes_admin(_Memory(FOUND), _auth(["admin"]), None, {"project_id": "NOPE"})


def _vector(*values: float) -> str:
    padded = list(values) + [0.0] * (1536 - len(values))
    return "[" + ",".join(str(v) for v in padded) + "]"


@requires_pg
class HybridRecallPostgresTests(PgTestCase):
    def setUp(self):
        search._cache.clear()
        with self.db.connect() as conn:
            conn.execute("DELETE FROM knowledge_note_versions")
            conn.execute("DELETE FROM knowledge_notes")
            conn.execute("DELETE FROM projects")
            conn.commit()

    def _seed(self, conn):
        projects.create_project(conn, {"key": "PROJ", "name": "Project"})
        created = {}
        for title, body in (
            ("Migration lock", "In org/api, take the advisory lock before running migrations."),
            ("CSV export", "In org/api, stream CSV rows; do not build the file in memory."),
            ("Button styles", "In org/web, use the shared button component."),
        ):
            created[title] = notes.create_note(
                conn,
                {"scope": "project", "project_id": "PROJ", "kind": "howto", "title": title, "body": body,
                 "status": "active", "actor": "ada"},
            )
        return created

    def test_full_text_only_without_embeddings(self):
        with self.db.connect() as conn:
            created = self._seed(conn)
            result = notes.search_notes(conn, {"q": "migrations lock"}, embed=None)
            self.assertEqual(result["mode"], "text")
            self.assertEqual(result["notes"][0]["id"], created["Migration lock"]["id"])
            listed = notes.search_notes(conn, {"status": "active", "kind": "howto"}, embed=None)
            self.assertEqual(listed["mode"], "list")
            self.assertEqual(len(listed["notes"]), 3)

    def test_hybrid_search_and_backfill(self):
        vectors = {
            "Migration lock": _vector(1, 0, 0),
            "CSV export": _vector(0, 1, 0),
            "Button styles": _vector(0, 0, 1),
        }

        def embed(text):
            for title, vector in vectors.items():
                if text.startswith(title):
                    return vector
            return _vector(0, 0.9, 0.1)  # a query about large downloads: closest to CSV export

        with self.db.connect() as conn:
            created = self._seed(conn)
            done = notes.embed_pending_notes(conn, {"limit": 10}, embed=embed)
            self.assertEqual(done["embedded"], 3)
            again = notes.embed_pending_notes(conn, {"limit": 10}, embed=embed)
            self.assertEqual(again["embedded"], 0)
            result = notes.search_notes(conn, {"q": "large downloads time out"}, embed=embed)
            self.assertEqual(result["mode"], "hybrid")
            self.assertEqual(result["notes"][0]["id"], created["CSV export"]["id"])
            self.assertIn("similarity", result["notes"][0])

            updated = notes.update_note(
                conn, created["CSV export"]["id"], {"expected_version": 1, "body": "Stream CSV rows.", "actor": "ada"}
            )
            self.assertEqual(notes.embed_pending_notes(conn, {"ids": [updated["id"]]}, embed=embed)["embedded"], 1)

    def test_context_orders_by_the_run_text_and_survives_a_broken_embedder(self):
        def broken(_text):
            raise RuntimeError("provider down")

        with self.db.connect() as conn:
            created = self._seed(conn)
            context = notes.notes_context(
                conn, {"project_id": "PROJ", "query": "add a migration for the orders table", "max_notes": 1}, embed=broken
            )
            self.assertEqual([n["id"] for n in context["notes"]], [created["Migration lock"]["id"]])

    def test_drafted_source_is_allowed_only_as_a_proposal(self):
        with self.db.connect() as conn:
            projects.create_project(conn, {"key": "PROJ", "name": "Project"})
            with self.assertRaises(ValueError):
                notes.create_note(conn, {"scope": "project", "project_id": "PROJ", "kind": "avoid", "title": "t",
                                         "body": "b", "source": "drafted", "status": "active"})
            drafted = notes.create_note(conn, {"scope": "project", "project_id": "PROJ", "kind": "avoid", "title": "t",
                                               "body": "b", "source": "drafted"})
            self.assertEqual((drafted["status"], drafted["source"]), ("proposed", "drafted"))
            self.assertEqual(notes.list_notes(conn, {"source": "drafted"})["notes"][0]["id"], drafted["id"])


if __name__ == "__main__":
    unittest.main()
