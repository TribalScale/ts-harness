"""Repositories per project, and the repository each work order is delivered to."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.pg_support import PgTestCase, requires_pg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from memory_routes import projects  # noqa: E402

from ts_harness.gateway import projects_bff as bff  # noqa: E402
from ts_harness.gateway.auth import AuthContext  # noqa: E402
from ts_harness.implementation.work_order_context import (  # noqa: E402
    parse_work_order_repositories,
    repository_dependency_cycle,
    work_order_repository_problems,
)
from ts_harness.workflows import stage_contracts  # noqa: E402
from ts_harness.workflows.expressions import resolve_value  # noqa: E402
from ts_harness.workflows.program_adapters import _build_implement_inputs, _build_work_orders_inputs  # noqa: E402
from ts_harness.workflows.project_repos import with_project_repositories  # noqa: E402

API = {"full_name": "acme/api", "repo_url": "https://github.com/acme/api", "default_branch": "main", "role": "api"}
WEB = {"full_name": "acme/web", "repo_url": "https://github.com/acme/web", "default_branch": None, "role": "web"}

MARKDOWN = """# WO-001 Payments endpoint
Repository: acme/api
| Depends on | None |

# WO-002 Checkout page
**Repository:** `acme/web`
| Depends on | WO-001 |

# WO-003 Receipt email
- **Repository**: acme/api
| Depends on | WO-001 |
"""


class ParsingTests(unittest.TestCase):
    def test_repository_line_forms(self):
        self.assertEqual(
            parse_work_order_repositories(MARKDOWN + "\n## WO-004\n| Repository | acme/web.git |\n"),
            {"WO-001": "acme/api", "WO-002": "acme/web", "WO-003": "acme/api", "WO-004": "acme/web"},
        )

    def test_problems(self):
        self.assertEqual(work_order_repository_problems(MARKDOWN, [API, WEB]), [])
        self.assertEqual(work_order_repository_problems(MARKDOWN, []), [])
        unknown = work_order_repository_problems(MARKDOWN, [API])
        self.assertEqual(len(unknown), 1)
        self.assertIn("WO-002 names repository acme/web", unknown[0])
        missing = MARKDOWN + "\n# WO-004 Docs\n| Depends on | None |\n"
        self.assertIn("WO-004 has no Repository line", work_order_repository_problems(missing, [API, WEB])[0])
        single = "# WO-001 A\n| Depends on | None |\n"
        self.assertEqual(work_order_repository_problems(single, [API]), [])

    def test_cross_repository_cycle(self):
        self.assertIsNone(repository_dependency_cycle(MARKDOWN))
        cyclic = MARKDOWN.replace("| Depends on | WO-001 |\n\n# WO-003", "| Depends on | WO-001 |\n\n# WO-003").replace(
            "- **Repository**: acme/api\n| Depends on | WO-001 |", "- **Repository**: acme/api\n| Depends on | WO-002 |"
        )
        self.assertEqual(repository_dependency_cycle(cyclic), ["acme/api", "acme/web", "acme/api"])
        self.assertIn("cycle between repositories", work_order_repository_problems(cyclic, [API, WEB])[-1])


class PlannerContractTests(unittest.TestCase):
    def _validate(self, stage_input):
        output = {"artifacts": {"work_orders_markdown": MARKDOWN.replace("acme/web", "acme/mobile")}}
        with mock.patch.object(stage_contracts, "work_orders_artifacts_failure_reason", return_value=None):
            return stage_contracts.validate_work_order_artifacts({}, output, stage_input, [])

    def test_unknown_repository_is_a_violation_only_with_project_repositories(self):
        violations = self._validate({"stage": "planner", "project_repositories": [API, WEB]})
        self.assertEqual([v.path for v in violations], ["/artifacts/work_orders_markdown"])
        self.assertEqual(self._validate({"stage": "planner"}), [])
        self.assertEqual(self._validate({"stage": "foundry", "project_repositories": [API, WEB]}), [])

    def test_planner_input_map_resolves_project_repositories(self):
        tasks = [{"step_name": "refine_requirements", "status": "succeeded", "input": {"project_repositories": [API]},
                  "output": {}, "created_at": "1"}]
        self.assertEqual(resolve_value("$workflow.project_repositories", tasks=tasks, workflow={}), [API])


class ImplementInputsTests(unittest.TestCase):
    def test_each_work_order_gets_its_repository(self):
        artifacts = {
            "work_orders_markdown": MARKDOWN,
            "work_order_ids": ["WO-001", "WO-002", "WO-003"],
            "completed_work_order_ids": ["WO-001"],
            "project_repositories": [API, WEB],
        }
        intake = _build_implement_inputs(summary="s", artifacts=artifacts, workflow_type="implement_work_orders")
        routed = {item["work_order_id"]: (item.get("repo_url"), item.get("base_branch")) for item in intake["work_orders"]}
        self.assertEqual(routed["WO-002"], ("https://github.com/acme/web", None))
        self.assertEqual(routed["WO-003"], ("https://github.com/acme/api", "main"))

    def test_single_repository_project_is_the_default(self):
        artifacts = {"next_work_order_id": "WO-009", "project_repositories": [API]}
        with mock.patch("ts_harness.implementation.config.repo_for_workflow") as configured:
            intake = _build_implement_inputs(summary="s", artifacts=artifacts, workflow_type="implement_work_orders")
        configured.assert_not_called()
        self.assertEqual((intake["repo_url"], intake["base_branch"]), ("https://github.com/acme/api", "main"))

    def test_without_project_repositories_the_install_repo_is_used(self):
        repo = mock.Mock(url="https://github.com/acme/legacy", base_branch="trunk")
        with mock.patch("ts_harness.implementation.config.repo_for_workflow", return_value=repo):
            intake = _build_implement_inputs(summary="s", artifacts={}, workflow_type="implement_work_orders")
        self.assertEqual((intake["repo_url"], intake["base_branch"]), ("https://github.com/acme/legacy", "trunk"))

    def test_planner_intake_carries_project_repositories(self):
        intake = _build_work_orders_inputs(summary="s", artifacts={"project_repositories": [API, WEB]})
        self.assertEqual(intake["project_repositories"], [API, WEB])


class ProgramArtifactsTests(unittest.TestCase):
    def test_loads_repositories_of_the_programs_project(self):
        client = mock.MagicMock()
        client.list_projects.return_value = {"projects": [{"id": "p1"}]}
        client.list_project_repositories.return_value = {
            "repositories": [{**API, "id": "r1", "installation_id": 7, "github_host": "github.com"}]
        }
        out = with_project_repositories(client, {"a": 1}, tenant_id="acme", project_key="PAY")
        client.list_projects.assert_called_once_with(tenant_id="acme", key="PAY")
        self.assertEqual(out["project_repositories"][0]["full_name"], "acme/api")
        self.assertNotIn("installation_id", out["project_repositories"][0])
        self.assertEqual(with_project_repositories(client, {"a": 1}, tenant_id="acme", project_key=None), {"a": 1})
        client.list_projects.side_effect = RuntimeError("down")
        self.assertEqual(with_project_repositories(client, {"a": 1}, tenant_id="acme", project_key="PAY"), {"a": 1})


class RepositoryAdminTests(unittest.TestCase):
    MANAGER = AuthContext(actor_id="lead@acme.dev", tenant_id="default", roles=["requester"],
                          teams=[{"slug": "pay", "role": "manager"}])

    def _memory(self, installations):
        memory = mock.MagicMock()
        memory.get_project.return_value = {"id": "p1", "team_slug": "pay"}
        memory.list_github_installations.return_value = installations
        memory.create_project_repository.side_effect = lambda pid, **body: body
        return memory

    def test_adds_with_the_covering_installation(self):
        memory = self._memory([{"installation_id": 7, "repositories": ["Acme/API"]}])
        with mock.patch.object(bff, "_project", return_value={"id": "p1", "team_slug": "pay"}):
            out = bff.create_repository_admin(memory, self.MANAGER, None, "p1", {"full_name": "acme/api", "role": "api"})
        self.assertEqual((out["installation_id"], out["full_name"], out["role"]), (7, "acme/api", "api"))

    def test_rejects_repositories_the_app_cannot_reach(self):
        memory = self._memory([{"installation_id": 7, "repositories": ["acme/api"]}])
        with mock.patch.object(bff, "_project", return_value={"id": "p1", "team_slug": "pay"}):
            with self.assertRaisesRegex(ValueError, "not installed on acme/web"):
                bff.create_repository_admin(memory, self.MANAGER, None, "p1", {"full_name": "acme/web"})

    def test_without_installations_any_repository_is_accepted(self):
        memory = self._memory([])
        with mock.patch.object(bff, "_project", return_value={"id": "p1", "team_slug": "pay"}):
            out = bff.create_repository_admin(memory, self.MANAGER, None, "p1", {"full_name": "acme/web"})
        self.assertIsNone(out["installation_id"])

    def test_viewers_cannot_add(self):
        viewer = AuthContext(actor_id="dev@acme.dev", tenant_id="default", roles=["requester"])
        with mock.patch.object(bff, "_project", return_value={"id": "p1", "team_slug": "pay"}), \
                mock.patch.object(bff, "_is_admin", return_value=False):
            with self.assertRaises(bff.ProjectAccessDenied):
                bff.create_repository_admin(self._memory([]), viewer, None, "p1", {"full_name": "acme/web"})


@requires_pg
class RepositoriesPostgresTests(PgTestCase):
    def setUp(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM project_repositories")
            conn.execute("DELETE FROM projects WHERE key = 'PAY'")
            conn.commit()

    def test_crud_and_case_insensitive_uniqueness(self):
        with self.db.connect() as conn:
            project = projects.create_project(conn, {"key": "PAY"})
            created = projects.create_repository(conn, project["id"], {"full_name": "acme/api.git", "role": "api"})
            self.assertEqual((created["full_name"], created["repo_url"]), ("acme/api", "https://github.com/acme/api"))
            with self.assertRaises(ValueError):
                projects.create_repository(conn, project["id"], {"full_name": "ACME/API"})
            with self.assertRaises(ValueError):
                projects.create_repository(conn, project["id"], {"full_name": "not a repo"})
            ghe = projects.create_repository(
                conn, project["id"], {"github_host": "GHE.acme.dev", "owner": "acme", "repo": "api", "installation_id": 3}
            )
            self.assertEqual(ghe["repo_url"], "https://ghe.acme.dev/acme/api")
            updated = projects.update_repository(conn, project["id"], created["id"], {"default_branch": "release"})
            self.assertEqual((updated["default_branch"], updated["role"]), ("release", "api"))
            listed = projects.list_repositories(conn, project["id"])["repositories"]
            self.assertEqual([r["github_host"] for r in listed], ["ghe.acme.dev", "github.com"])
            projects.delete_repository(conn, project["id"], ghe["id"], {})
            with self.assertRaises(LookupError):
                projects.delete_repository(conn, project["id"], ghe["id"], {})
            self.assertEqual(len(projects.list_repositories(conn, project["id"])["repositories"]), 1)


if __name__ == "__main__":
    unittest.main()
