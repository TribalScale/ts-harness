"""A change to an API's openapi.json starts a refresh run on the web repositories that read it."""

from __future__ import annotations

import base64
import unittest
from unittest import mock

from ts_harness.implementation.api_contract import contract_block
from ts_harness.workflows import contract_refresh as cr

PR = "https://github.com/org/api/pull/7"
EVENT = {"kind": "github.merge", "repo": "org/api", "pr_url": PR, "merge_sha": "abc1234def", "base_branch": "dev"}


def _web(name, contract, branch="main"):
    return {"full_name": name, "base_branch": branch, "contract": contract}


def _contract(**overrides):
    return {"repo": "org/api", "path": "openapi.json", "ref": "", "refresh_on_change": True, **overrides}


class ContractBlockTests(unittest.TestCase):
    def test_refresh_on_change_defaults_to_true(self):
        self.assertTrue(contract_block({"contract": {"openapi": {"repo": "org/api"}}})["refresh_on_change"])
        self.assertFalse(
            contract_block({"contract": {"openapi": {"repo": "org/api", "refresh_on_change": False}}})["refresh_on_change"]
        )
        self.assertIsNone(contract_block({"integration": {}}))


class PlanTests(unittest.TestCase):
    def _plan(self, web_repos, changed=frozenset({"openapi.json", "src/app.ts"}), merged_into="dev"):
        return cr.plan_refreshes(
            api_repo="org/api", merged_into=merged_into, api_default_branch="dev", changed=set(changed), web_repos=web_repos
        )

    def test_only_web_repos_that_read_this_api_are_refreshed(self):
        decisions = self._plan([
            _web("org/web", _contract()),
            _web("org/admin", _contract(repo="org/other-api")),
            _web("org/worker", None),
            _web("org/api", _contract()),
        ])
        self.assertEqual([(d["repo"], d["action"]) for d in decisions], [("org/web", "start")])
        self.assertEqual(decisions[0]["base_branch"], "main")

    def test_skipped_when_the_spec_did_not_change(self):
        decisions = self._plan([_web("org/web", _contract())], changed={"src/app.ts"})
        self.assertEqual((decisions[0]["action"], decisions[0]["reason"]), ("skip", "openapi.json did not change"))

    def test_custom_path_and_renames_count(self):
        decisions = self._plan([_web("org/web", _contract(path="./docs/openapi.yaml"))], changed={"docs/openapi.yaml"})
        self.assertEqual(decisions[0]["action"], "start")
        self.assertEqual(cr.changed_paths([{"filename": "b.json", "previous_filename": "openapi.json"}]), {"b.json", "openapi.json"})

    def test_opted_out_or_other_branch_is_skipped(self):
        off = self._plan([_web("org/web", _contract(refresh_on_change=False))])
        self.assertEqual(off[0]["reason"], "refresh_on_change is false")
        feature = self._plan([_web("org/web", _contract())], merged_into="feature/x")
        self.assertEqual(feature[0]["action"], "skip")
        pinned = self._plan([_web("org/web", _contract(ref="main"))], merged_into="main")
        self.assertEqual(pinned[0]["action"], "start")

    def test_work_order_tells_the_agent_how_to_refresh(self):
        body = cr.work_order_body(api_repo="org/api", merge_sha="abc1234def", pr_url=PR, path="openapi.json", branch="dev")
        self.assertTrue(body.startswith("# APIREFRESH-1 Refresh API types from org/api@abc1234\n"))
        self.assertIn('OPENAPI_SOURCE="$OPENAPI_CONTRACT" npm run api:pull && npm run api:generate', body)
        self.assertIn("## Acceptance Criteria", body)


class StartRefreshTests(unittest.TestCase):
    def setUp(self):
        self.memory = mock.MagicMock()
        self.memory.kv_get.return_value = {"value": None, "version": 0}
        self.engine = mock.MagicMock()
        self.github = mock.MagicMock()
        fork = mock.patch("ts_harness.workflows.hooks.fork_subworkflow.fork_child", return_value="wf-new")
        self.fork = fork.start()
        self.addCleanup(fork.stop)

    def _start(self, pr_url=""):
        decision = {"repo": "org/web", "base_branch": "main", "path": "openapi.json", "action": "start", "pr_url": pr_url}
        return cr.start_refresh(
            decision, api_repo="org/api", merge_sha="abc1234def", pr_url=PR, branch="dev", tenant_id="t1",
            memory=self.memory, engine=self.engine, github=self.github,
        )

    def test_first_refresh_starts_a_delivery_and_remembers_it(self):
        result = self._start()
        self.assertEqual((result["action"], result["workflow_id"], result["updates_pr"]), ("started", "wf-new", ""))
        kwargs = self.fork.call_args.kwargs
        self.assertEqual((kwargs["child_type"], kwargs["parent_workflow_id"], kwargs["tenant_id"]), ("deliver_work_order", "", "t1"))
        intake = kwargs["intake_input"]
        self.assertEqual((intake["repo_url"], intake["base_branch"], intake["work_order_id"]), ("https://github.com/org/web", "main", "APIREFRESH-1"))
        self.assertNotIn("pr_url", intake)
        self.engine.cancel.assert_not_called()
        self.memory.kv_put.assert_called_once()
        self.assertEqual(self.memory.kv_put.call_args.args[:2], ("api-contract-refresh", "org__web"))
        self.assertEqual(self.memory.kv_put.call_args.args[2]["workflow_id"], "wf-new")

    def test_an_open_refresh_pr_is_updated_and_the_run_in_flight_is_superseded(self):
        self.memory.kv_get.return_value = {"value": {"workflow_id": "wf-old"}, "version": 3}
        self.memory.get_workflow.return_value = {"workflow": {"status": "blocked"}}
        result = self._start(pr_url="https://github.com/org/web/pull/12")
        self.assertEqual((result["superseded"], result["updates_pr"]), ("wf-old", "https://github.com/org/web/pull/12"))
        self.engine.cancel.assert_called_once()
        self.assertEqual(self.engine.cancel.call_args.args, ("wf-old",))
        self.assertEqual(self.fork.call_args.kwargs["intake_input"]["pr_url"], "https://github.com/org/web/pull/12")
        self.github.request.assert_called_once_with(
            "PATCH", "/repos/org/web/pulls/12", body={"title": "APIREFRESH-1: Refresh API types from org/api@abc1234"}
        )

    def test_a_finished_run_is_left_alone(self):
        self.memory.kv_get.return_value = {"value": {"workflow_id": "wf-old"}, "version": 3}
        self.memory.get_workflow.return_value = {"workflow": {"status": "succeeded"}}
        self.assertEqual(self._start()["superseded"], "")
        self.engine.cancel.assert_not_called()


class OnMergeTests(unittest.TestCase):
    def _github(self, configs, files):
        github = mock.MagicMock()

        def get_file(owner, name, path, ref):
            text = configs.get(f"{owner}/{name}") if path == ".ts-harness.yml" else None
            return {"content": base64.b64encode(text.encode()).decode()} if text else None

        def request(method, path, **_):
            if path.endswith("/pulls"):
                return [{"title": "APIREFRESH-1: Refresh API types from org/api@0000000", "html_url": "https://github.com/org/web/pull/3"}]
            return {"default_branch": "dev"}

        github.get_file.side_effect = get_file
        github.request.side_effect = request
        github.list_pull_files.return_value = files
        return github

    def _memory(self):
        memory = mock.MagicMock()
        memory.list_projects.return_value = {"projects": [{"id": "p1"}, {"id": "p2"}]}
        memory.list_project_repositories.side_effect = lambda pid: {
            "p1": {"repositories": [{"full_name": "org/api"}, {"full_name": "org/web", "default_branch": "main"}]},
            "p2": {"repositories": [{"full_name": "org/elsewhere"}]},
        }[pid]
        memory.kv_get.return_value = {"value": None, "version": 0}
        return memory

    def test_a_spec_change_starts_one_refresh_that_updates_the_open_pr(self):
        github = self._github({"org/web": "contract:\n  openapi:\n    repo: org/api\n"}, [{"filename": "openapi.json"}])
        with mock.patch("ts_harness.workflows.hooks.fork_subworkflow.fork_child", return_value="wf-1") as fork:
            result = cr.on_merge(EVENT, tenant_id="t1", memory=self._memory(), github=github, engine=mock.MagicMock())
        self.assertEqual(result["status"], "started")
        self.assertEqual([r["repo"] for r in result["refreshes"]], ["org/web"])
        self.assertEqual(fork.call_args.kwargs["intake_input"]["pr_url"], "https://github.com/org/web/pull/3")
        github.list_pull_files.assert_called_once_with("org", "api", 7)

    def test_unchanged_spec_or_no_reader_starts_nothing(self):
        github = self._github({"org/web": "contract:\n  openapi:\n    repo: org/api\n"}, [{"filename": "src/app.ts"}])
        with mock.patch("ts_harness.workflows.hooks.fork_subworkflow.fork_child") as fork:
            result = cr.on_merge(EVENT, tenant_id="t1", memory=self._memory(), github=github, engine=mock.MagicMock())
            self.assertEqual(result["status"], "skipped")
            none = cr.on_merge(EVENT, tenant_id="t1", memory=self._memory(), github=self._github({}, []), engine=mock.MagicMock())
            self.assertEqual(none["status"], "ignored")
        fork.assert_not_called()

    def test_registered_as_a_merge_subscriber(self):
        from ts_harness.gateway.github_webhook import EVENT_SUBSCRIBERS

        self.assertIn("ts_harness.workflows.contract_refresh:on_merge", EVENT_SUBSCRIBERS["github.merge"])


if __name__ == "__main__":
    unittest.main()
