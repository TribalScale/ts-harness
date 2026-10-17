"""Stage fragments: expansion, collapse, conversion equivalence and validation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ts_harness.repo_paths import ensure_scripts_importable, repo_root
from ts_harness.workflows.definition_validation import (
    validate_fragment_definition,
    validate_workflow_definition,
)
from ts_harness.workflows.fragment_definition_store import FragmentDefinitionStore

ensure_scripts_importable()

import workflow_definitions  # noqa: E402
import workflow_fragments  # noqa: E402

ROOT = repo_root()
GOLDEN = ROOT / "tests" / "fixtures" / "golden"
VERIFIED_MERGE_REVISION = "228b5b62a8eae1653063dc41c0023f24c55a2523fee95503987fd7795093f671"
CATALOGS = {
    "hook_names": {"hydrate_input", "revise_on_reject", "compensate_on_failure", "cancel_workflow"},
    "agent_ids": {"orchestrator", "dev-agent", "qa-agent", "review-agent"},
    "skill_ids": {"verified-merge-governed", "governed-qa-gate", "governed-code-review"},
}


def _stage_payloads(skeleton):
    return [workflow_definitions._stage_to_payload(stage) for stage in skeleton.stages]


def _fragment(**overrides):
    payload = {
        "fragment_id": "review_pair",
        "definition_version": "1.0.0",
        "params": {"source": {"kind": "stage_ref"}},
        "inputs": {"repo_url": {"required": True}, "note": {"required": False}},
        "contracts": {"review": {"output_schema": {"type": "object"}}},
        "stages": [
            {
                "name": "review",
                "kind": "task",
                "agent_id": "qa-agent",
                "depends_on": ["{{param.source}}"],
                "contract_ref": "review",
                "input_map": {
                    "pr_url": "$prior.{{param.source}}.output.pr_url",
                    "repo_url": "$input.repo_url",
                    "note": "$input.note",
                },
            },
            {
                "name": "sign_off",
                "kind": "task",
                "agent_id": "orchestrator",
                "depends_on": ["review"],
                "approval_required": True,
                "input_map": {"review": "$prior.review.output"},
            },
        ],
    }
    payload.update(overrides)
    return payload


def _workflow(include_overrides=None, extra_stages=None):
    include = {
        "name": "review_block",
        "kind": "fragment",
        "fragment": "review_pair",
        "version": "1.0.0",
        "params": {"source": "build"},
        "inputs": {"repo_url": "$workflow.repo_url"},
        "depends_on": ["build"],
    }
    include.update(include_overrides or {})
    return {
        "workflow_type": "uses_fragment",
        "definition_version": "1.0.0",
        "stages": [
            {"name": "build", "kind": "task", "agent_id": "dev-agent"},
            include,
            *(extra_stages or []),
        ],
    }


class FragmentFixtureMixin:
    def setUp(self):
        self.fragment = workflow_fragments.load_fragment_payload(_fragment())
        self.resolver = mock.patch.object(
            workflow_fragments,
            "resolve_fragment",
            side_effect=self._resolve,
        )
        self.resolver.start()

    def tearDown(self):
        self.resolver.stop()

    def _resolve(self, fragment_id, version, **_kwargs):
        if (fragment_id, version) == ("review_pair", "1.0.0"):
            return self.fragment
        raise workflow_fragments.FragmentError(f"fragment not found: {fragment_id}@{version}")


class ConvertedBuiltinTests(unittest.TestCase):
    def test_converted_workflows_expand_to_the_frozen_definitions(self):
        for workflow_type in ("deliver_work_order", "implement_jira_ticket"):
            with self.subTest(workflow_type=workflow_type):
                converted = workflow_definitions.load_skeleton(ROOT / "config" / "workflows" / f"{workflow_type}.json")
                frozen = workflow_definitions.load_skeleton(GOLDEN / f"{workflow_type}.json")
                self.assertEqual(_stage_payloads(converted), _stage_payloads(frozen))
                self.assertEqual(converted.contracts, frozen.contracts)
                self.assertEqual(converted.definition_version, frozen.definition_version)
                self.assertEqual(
                    {stage.name for stage in converted.stages if stage.origin},
                    {
                        "await_pr_checks",
                        "review_precheck",
                        "code_review",
                        "finalize_code_review",
                        "qa_gate",
                        "ensure_fresh",
                        "await_human_merge",
                    },
                )

    def test_converted_workflows_really_include_the_fragment(self):
        for workflow_type in ("deliver_work_order", "implement_jira_ticket"):
            raw = json.loads((ROOT / "config" / "workflows" / f"{workflow_type}.json").read_text())
            include = next(stage for stage in raw["stages"] if stage.get("kind") == "fragment")
            self.assertEqual((include["fragment"], include["version"]), ("verified_merge", "1.5.0"))
            self.assertNotIn("qa_gate", raw["contracts"])

    def test_builtin_fragment_revision_is_pinned(self):
        payload = json.loads((ROOT / "config" / "fragments" / "verified_merge.json").read_text())
        self.assertEqual(
            workflow_fragments.fragment_content_revision(payload),
            VERIFIED_MERGE_REVISION,
            "verified_merge changed: bump its definition_version and add a new file instead",
        )

    def test_collapse_round_trip_keeps_the_include(self):
        path = ROOT / "config" / "workflows" / "deliver_work_order.json"
        skeleton = workflow_definitions.load_skeleton(path)
        collapsed = workflow_definitions.skeleton_to_payload(skeleton)
        self.assertEqual(
            [stage["name"] for stage in collapsed["stages"]],
            ["deliver_work_order", "record_implement_failure", "verified_merge"],
        )
        self.assertEqual(sorted(collapsed["contracts"]), ["deliver_work_order"])
        reloaded = workflow_definitions.load_skeleton_payload(collapsed)
        self.assertEqual(_stage_payloads(reloaded), _stage_payloads(skeleton))

    def test_builtin_validates_without_errors(self):
        raw = json.loads((ROOT / "config" / "workflows" / "deliver_work_order.json").read_text())
        _skeleton, issues = validate_workflow_definition(raw, built_in_types=set(), **CATALOGS)
        self.assertEqual([issue for issue in issues if issue.severity == "error"], [])


class ExpansionTests(FragmentFixtureMixin, unittest.TestCase):
    def test_params_inputs_entry_and_optional_inputs(self):
        skeleton = workflow_definitions.load_skeleton_payload(_workflow())
        review = skeleton.stage("review")
        self.assertEqual(review.depends_on, ["build"])
        self.assertEqual(
            review.input_map,
            {"pr_url": "$prior.build.output.pr_url", "repo_url": "$workflow.repo_url"},
        )
        self.assertEqual(review.origin, "review_block")
        self.assertIn("review", skeleton.contracts)

    def test_downstream_stages_depend_on_the_fragment_exits(self):
        skeleton = workflow_definitions.load_skeleton_payload(
            _workflow(extra_stages=[{"name": "ship", "kind": "task", "agent_id": "dev-agent", "depends_on": ["review_block"]}])
        )
        self.assertEqual(skeleton.stage("ship").depends_on, ["sign_off"])
        collapsed = workflow_definitions.skeleton_to_payload(skeleton)
        ship = next(stage for stage in collapsed["stages"] if stage["name"] == "ship")
        self.assertEqual(ship["depends_on"], ["review_block"])
        self.assertNotIn("review", collapsed["contracts"])

    def test_prefix_renames_stages_and_references(self):
        skeleton = workflow_definitions.load_skeleton_payload(_workflow({"prefix": "a_"}))
        self.assertEqual([stage.name for stage in skeleton.stages], ["build", "a_review", "a_sign_off"])
        self.assertEqual(skeleton.stage("a_sign_off").depends_on, ["a_review"])
        self.assertEqual(skeleton.stage("a_sign_off").input_map, {"review": "$prior.a_review.output"})

    def test_include_errors(self):
        cases = {
            "missing required param": ({"params": {}}, "missing required params"),
            "unknown param": ({"params": {"source": "build", "extra": "build"}}, "unknown params"),
            "missing required input": ({"inputs": {}}, "missing required inputs"),
            "param names unknown stage": ({"params": {"source": "nope"}}, "unknown stage"),
            "unknown version": ({"version": "9.9.9"}, "fragment not found"),
            "unknown include key": ({"agent_id": "x"}, "unknown keys"),
        }
        for label, (overrides, message) in cases.items():
            with self.subTest(label):
                with self.assertRaisesRegex(workflow_fragments.FragmentError, message) as ctx:
                    workflow_fragments.expand_fragments(_workflow(overrides))
                self.assertEqual(ctx.exception.include_index, 1)

    def test_stage_name_collision_after_expansion(self):
        workflow = _workflow(extra_stages=[{"name": "review", "kind": "task", "agent_id": "qa-agent"}])
        with self.assertRaisesRegex(workflow_fragments.FragmentError, "duplicate stage name"):
            workflow_fragments.expand_fragments(workflow)

    def test_conflicting_contract_is_rejected(self):
        workflow = _workflow()
        workflow["contracts"] = {"review": {"output_schema": {"type": "array"}}}
        with self.assertRaisesRegex(workflow_fragments.FragmentError, "conflicts"):
            workflow_fragments.expand_fragments(workflow)

    def test_fragment_documents_reject_nesting_and_undeclared_refs(self):
        nested = _fragment(stages=[{"name": "x", "kind": "fragment", "fragment": "a", "version": "1"}])
        with self.assertRaisesRegex(workflow_fragments.FragmentError, "cannot include"):
            workflow_fragments.load_fragment_payload(nested)
        undeclared = _fragment(inputs={})
        with self.assertRaisesRegex(workflow_fragments.FragmentError, "undeclared inputs"):
            workflow_fragments.load_fragment_payload(undeclared)
        partial = _fragment()
        partial["stages"][0]["input_map"]["repo_url"] = "repo: $input.repo_url"
        with self.assertRaisesRegex(workflow_fragments.FragmentError, "larger string"):
            workflow_fragments.expand_fragments({**_workflow(), "stages": _workflow()["stages"]}, resolver=lambda *_: workflow_fragments.load_fragment_payload(partial))

    def test_validation_points_fragment_issues_at_the_include(self):
        self.fragment = workflow_fragments.load_fragment_payload(
            _fragment(stages=[{**_fragment()["stages"][0], "agent_id": "ghost"}, _fragment()["stages"][1]])
        )
        workflow = _workflow(extra_stages=[{"name": "ship", "kind": "task", "agent_id": "ghost", "depends_on": ["review_block"]}])
        _skeleton, issues = validate_workflow_definition(workflow, built_in_types=set(), **CATALOGS)
        agent_issues = [issue for issue in issues if issue.code == "unknown_agent"]
        self.assertEqual([issue.path for issue in agent_issues], ["$.stages[1]", "$.stages[2].agent_id"])
        self.assertIn("fragment review_pair@1.0.0 stage review.agent_id", agent_issues[0].message)

    def test_fragment_stage_tool_allow_issues_point_at_the_include(self):
        self.fragment = workflow_fragments.load_fragment_payload(
            _fragment(stages=[{**_fragment()["stages"][0], "tool_allow": ["exec"]}, _fragment()["stages"][1]])
        )
        _skeleton, issues = validate_workflow_definition(
            _workflow(), built_in_types=set(), agent_tools={"qa-agent": {"read_file"}}, **CATALOGS
        )
        tool_issues = [issue for issue in issues if issue.code == "unknown_tool"]
        self.assertEqual([issue.path for issue in tool_issues], ["$.stages[1]"])
        self.assertIn("fragment review_pair@1.0.0 stage review.tool_allow[0]", tool_issues[0].message)

    def test_validation_reports_include_errors(self):
        _skeleton, issues = validate_workflow_definition(
            _workflow({"version": "2.0.0"}), built_in_types=set(), **CATALOGS
        )
        self.assertEqual([(issue.code, issue.path) for issue in issues], [("fragment_invalid", "$.stages[1]")])


class FragmentValidationTests(unittest.TestCase):
    def test_valid_fragment_and_unused_declarations(self):
        payload = _fragment(inputs={"repo_url": {}, "note": {}, "unused": {"required": False}})
        fragment, issues = validate_fragment_definition(payload, **CATALOGS)
        self.assertIsNotNone(fragment)
        self.assertEqual([issue.code for issue in issues if issue.severity == "error"], [])
        self.assertIn(("unused_fragment_input", "$.inputs.unused"), [(issue.code, issue.path) for issue in issues])

    def test_fragment_stage_issues_use_fragment_indexes(self):
        payload = _fragment()
        payload["stages"][1]["agent_id"] = "ghost"
        _fragment_obj, issues = validate_fragment_definition(payload, **CATALOGS)
        self.assertIn(("unknown_agent", "$.stages[1].agent_id"), [(issue.code, issue.path) for issue in issues])

    def test_builtin_fragment_is_valid(self):
        payload = json.loads((ROOT / "config" / "fragments" / "verified_merge.json").read_text())
        _fragment_obj, issues = validate_fragment_definition(payload, **CATALOGS)
        self.assertEqual([issue for issue in issues if issue.severity == "error"], [])


class ManagedFragmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "fragments"
        self.store = FragmentDefinitionStore(self.root, built_in_types={"verified_merge"})

    def tearDown(self):
        self.tmp.cleanup()

    def _activate(self, payload):
        draft = self.store.create_draft(payload, actor_id="admin")
        return self.store.activate(draft["id"], expected_revision=draft["revision"], actor_id="admin")

    def test_activated_fragment_resolves_from_managed_history(self):
        self._activate(_fragment())
        with mock.patch.dict("os.environ", {"TS_HARNESS_FRAGMENT_DEFINITION_STORE": str(self.root)}):
            skeleton = workflow_definitions.load_skeleton_payload(_workflow())
        self.assertEqual([stage.name for stage in skeleton.stages], ["build", "review", "sign_off"])

    def test_activated_versions_are_immutable(self):
        self._activate(_fragment())
        changed = _fragment(display_name="changed")
        with self.assertRaisesRegex(ValueError, "cannot reuse historical definition_version"):
            self._activate(changed)

    def test_superseded_builtin_resolves_from_builtin_history(self):
        builtin = Path(self.tmp.name) / "builtin"
        history = builtin / "history" / "verified_merge"
        history.mkdir(parents=True)
        (builtin / "verified_merge.json").write_text(json.dumps(_fragment(fragment_id="verified_merge", definition_version="2.0.0")))
        (history / "1.0.0.json").write_text(json.dumps(_fragment(fragment_id="verified_merge")))
        with mock.patch.dict("os.environ", {"TS_HARNESS_FRAGMENTS_DIR": str(builtin)}):
            self.assertEqual(workflow_fragments.resolve_fragment("verified_merge", "1.0.0").definition_version, "1.0.0")
            self.assertEqual(workflow_fragments.resolve_fragment("verified_merge", "2.0.0").definition_version, "2.0.0")
            self.assertEqual(workflow_fragments.list_builtin_fragment_ids(), ["verified_merge"])
            with self.assertRaises(workflow_fragments.FragmentError):
                workflow_fragments.resolve_fragment("verified_merge", "9.9.9")
            with self.assertRaises(workflow_fragments.FragmentError):
                workflow_fragments.resolve_fragment("verified_merge", "../1.0.0")

    def test_shipped_fragment_history_matches_pinned_versions(self):
        history = ROOT / "config" / "fragments" / "history" / "verified_merge"
        for path in sorted(history.glob("*.json")):
            payload = json.loads(path.read_text())
            self.assertEqual(payload["definition_version"], path.stem)
            self.assertEqual(workflow_fragments.resolve_fragment("verified_merge", path.stem).definition_version, path.stem)

    def test_workflows_pinned_to_superseded_versions_still_expand(self):
        raw = json.loads((ROOT / "config" / "workflows" / "deliver_work_order.json").read_text())
        include = next(stage for stage in raw["stages"] if stage.get("kind") == "fragment")
        expected = {
            "1.0.0": ["await_pr_checks", "qa_gate", "await_human_merge"],
            "1.1.0": ["await_pr_checks", "qa_gate", "ensure_fresh", "await_human_merge"],
            "1.2.0": [
                "await_pr_checks",
                "review_precheck",
                "code_review",
                "finalize_code_review",
                "qa_gate",
                "ensure_fresh",
                "await_human_merge",
            ],
            "1.3.0": [
                "await_pr_checks",
                "review_precheck",
                "code_review",
                "finalize_code_review",
                "qa_gate",
                "ensure_fresh",
                "await_human_merge",
            ],
        }
        for version, stages in expected.items():
            with self.subTest(version=version):
                include["version"] = version
                skeleton = workflow_definitions.load_skeleton_payload(raw)
                self.assertEqual([stage.name for stage in skeleton.stages if stage.origin], stages)
                self.assertEqual("code_review" in skeleton.contracts, version in {"1.2.0", "1.3.0"})

    def test_cannot_activate_over_builtin(self):
        with self.assertRaisesRegex(ValueError, "built-in fragment"):
            self._activate(_fragment(fragment_id="verified_merge"))


if __name__ == "__main__":
    unittest.main()
