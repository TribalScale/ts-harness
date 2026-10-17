"""Tests for declarative stage contracts (Phase 1)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import workflow_definitions as wd
from ts_harness.workflows import stage_contracts as sc


DELIVER_CONTRACT = {
    "name": "deliver",
    "output_schema": {
        "type": "object",
        "required": ["work_order_id", "pr_url"],
        "properties": {
            "work_order_id": {"type": "string", "minLength": 1},
            "pr_url": {"type": "string", "pattern": "^https://"},
        },
    },
    "required_artifacts": {"pr_url": "/pr_url"},
    "semantic_validators": ["validate_coding_backend_pr_url"],
    "outcome": None,
}

QA_CONTRACT = {
    "name": "qa",
    "output_schema": {"type": "object", "required": ["status"]},
    "required_artifacts": {},
    "semantic_validators": [],
    "outcome": {
        "pointer": "/status",
        "succeeded_values": ["success"],
        "blocked_values": ["failed"],
    },
}


class ContractLoaderTests(unittest.TestCase):
    def test_registry_matches_known_validators(self):
        self.assertEqual(
            set(sc.SEMANTIC_VALIDATORS),
            set(wd.KNOWN_SEMANTIC_VALIDATORS),
        )

    def test_freeze_contract_rejects_invalid_json_schema(self):
        with self.assertRaises(ValueError):
            wd.freeze_contract({"output_schema": {"type": "not-a-real-type"}})

    def test_freeze_contract_rejects_unknown_semantic_validator(self):
        with self.assertRaises(ValueError):
            wd.freeze_contract({"semantic_validators": ["does_not_exist"]})

    def test_freeze_contract_rejects_malformed_outcome(self):
        with self.assertRaises(ValueError):
            wd.freeze_contract({"outcome": {"succeeded_values": ["ok"]}})  # no pointer
        with self.assertRaises(ValueError):
            wd.freeze_contract(
                {"outcome": {"pointer": "/status", "succeeded_values": []}}
            )

    def test_freeze_contract_rejects_non_rfc6901_pointer(self):
        with self.assertRaises(ValueError):
            wd.freeze_contract({"required_artifacts": {"x": "status"}})

    def test_freeze_contract_is_deep_copied(self):
        raw = {"required_artifacts": {"x": "/x"}}
        frozen = wd.freeze_contract(raw)
        frozen["required_artifacts"]["x"] = "/mutated"
        self.assertEqual(raw["required_artifacts"]["x"], "/x")

    def _write_skeleton(self, data: dict) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "wf.json"
        tmp.write_text(json.dumps(data))
        return tmp

    def test_load_skeleton_rejects_missing_contract_ref(self):
        path = self._write_skeleton(
            {
                "workflow_type": "x",
                "definition_version": "1.0.0",
                "stages": [
                    {
                        "name": "s1",
                        "kind": "task",
                        "agent_id": "a",
                        "contract_ref": "nope",
                    }
                ],
            }
        )
        with self.assertRaises(ValueError):
            wd.load_skeleton(path)

    def test_load_skeleton_resolves_inline_output_schema(self):
        path = self._write_skeleton(
            {
                "workflow_type": "x",
                "definition_version": "1.0.0",
                "stages": [
                    {
                        "name": "s1",
                        "kind": "task",
                        "agent_id": "a",
                        "output_schema": {"type": "object", "required": ["foo"]},
                    }
                ],
            }
        )
        skeleton = wd.load_skeleton(path)
        contract = skeleton.resolve_stage_contract("s1")
        self.assertIsNotNone(contract)
        self.assertEqual(contract["output_schema"]["required"], ["foo"])

    def test_resolve_task_contract_enforces_allow_list(self):
        path = self._write_skeleton(
            {
                "workflow_type": "x",
                "definition_version": "1.0.0",
                "contracts": {"c1": {"output_schema": {"type": "object"}}},
                "stages": [
                    {
                        "name": "s1",
                        "kind": "expand",
                        "agent_id": "a",
                        "allowed_agents": ["a"],
                        "allowed_contracts": ["c1"],
                    }
                ],
            }
        )
        skeleton = wd.load_skeleton(path)
        self.assertIsNotNone(skeleton.resolve_task_contract("s1", "c1"))
        with self.assertRaises(ValueError):
            skeleton.resolve_task_contract("s1", "c2")


class StructuralValidationTests(unittest.TestCase):
    def test_schema_violation_reports_pointer_path(self):
        violations = sc.validate_stage_output(DELIVER_CONTRACT, {"work_order_id": "WO-1"})
        self.assertTrue(violations)
        # pr_url missing -> required error at root path
        self.assertTrue(any("pr_url" in v.message or v.path == "/" for v in violations))

    def test_required_artifact_pointer_missing(self):
        contract = {
            "output_schema": {"type": "object"},
            "required_artifacts": {"body": "/artifacts/body"},
        }
        violations = sc.validate_stage_output(contract, {"artifacts": {}})
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].path, "/artifacts/body")

    def test_required_artifact_pointer_present(self):
        contract = {"required_artifacts": {"body": "/artifacts/body"}}
        violations = sc.validate_stage_output(
            contract, {"artifacts": {"body": "content"}}
        )
        self.assertEqual(violations, [])

    def test_valid_output_has_no_violations(self):
        pr = "https://github.com/org/repo/pull/1"
        output = {"work_order_id": "WO-1", "pr_url": pr}
        audit = [
            {
                "tool": "run_coding_backend",
                "decision": "allow",
                "result_preview": json.dumps({"ok": True, "pr_url": pr}),
            }
        ]
        self.assertEqual(
            sc.validate_stage_output(DELIVER_CONTRACT, output, {}, audit), []
        )


class SemanticValidatorTests(unittest.TestCase):
    def test_coding_backend_pr_url_mismatch(self):
        output = {"work_order_id": "WO-1", "pr_url": "https://x/pull/9"}
        audit = [
            {
                "tool": "run_coding_backend",
                "decision": "allow",
                "result_preview": json.dumps(
                    {"ok": True, "pr_url": "https://x/pull/1"}
                ),
            }
        ]
        violations = sc.validate_stage_output(DELIVER_CONTRACT, output, {}, audit)
        self.assertTrue(any("must match" in v.message for v in violations))

    def test_coding_backend_failure_blocks(self):
        output = {"work_order_id": "WO-1", "pr_url": "https://x/pull/9"}
        audit = [
            {
                "tool": "run_coding_backend",
                "decision": "allow",
                "result_preview": json.dumps({"ok": False, "error": "boom"}),
            }
        ]
        violations = sc.validate_stage_output(DELIVER_CONTRACT, output, {}, audit)
        self.assertTrue(any("run_coding_backend failed" in v.message for v in violations))

    def test_work_order_artifacts_validator_uses_stage_input(self):
        contract = {
            "output_schema": {"type": "object"},
            "semantic_validators": ["validate_work_order_artifacts"],
        }
        bad = {"artifacts": {"blueprints_markdown": "too thin"}}
        violations = sc.validate_stage_output(contract, bad, {"stage": "foundry"}, [])
        self.assertTrue(violations)
        self.assertEqual(violations[0].path, "/artifacts")


CODE_REVIEW_CONTRACT = {"semantic_validators": ["validate_code_review_envelope"]}


class CodeReviewEnvelopeTests(unittest.TestCase):
    PR = "https://github.com/org/app/pull/1"

    def _output(self, prs=None, findings=None):
        return {
            "result": {
                "verdict": "pass",
                "summary": "ok",
                "prs": [{"pr_url": url, "head_sha": "abc"} for url in (prs or [self.PR])],
                "findings": [] if findings is None else findings,
            }
        }

    def _finding(self, **overrides):
        finding = {
            "severity": "major",
            "category": "correctness",
            "pr_url": self.PR,
            "file": "a.py",
            "line": 3,
            "message": "off by one",
        }
        finding.update(overrides)
        return finding

    def test_valid_envelope_passes(self):
        violations = sc.validate_stage_output(
            CODE_REVIEW_CONTRACT, self._output(findings=[self._finding()]), {"pr_url": self.PR}, []
        )
        self.assertEqual(violations, [])

    def test_pr_set_from_prs_input_must_match(self):
        stage_input = {"prs": [{"pr_url": self.PR}, {"pr_url": "https://github.com/org/app/pull/2"}]}
        violations = sc.validate_stage_output(CODE_REVIEW_CONTRACT, self._output(), stage_input, [])
        self.assertTrue(any("PR set must equal" in v.message for v in violations))

    def test_finding_for_unknown_pr_and_bad_enums_rejected(self):
        output = self._output(
            findings=[self._finding(pr_url="https://github.com/org/app/pull/9", severity="critical", category="style")]
        )
        paths = {v.path for v in sc.validate_stage_output(CODE_REVIEW_CONTRACT, output, {"pr_url": self.PR}, [])}
        self.assertEqual(
            paths,
            {"/result/findings/0/pr_url", "/result/findings/0/severity", "/result/findings/0/category"},
        )

    def test_missing_findings_rejected(self):
        output = self._output()
        del output["result"]["findings"]
        violations = sc.validate_stage_output(CODE_REVIEW_CONTRACT, output, {"pr_url": self.PR}, [])
        self.assertEqual([v.path for v in violations], ["/result/findings"])

    def test_validator_is_known_to_definition_loader(self):
        self.assertIn("validate_code_review_envelope", wd.KNOWN_SEMANTIC_VALIDATORS)


class OutcomeTests(unittest.TestCase):
    def test_outcome_succeeded(self):
        self.assertEqual(
            sc.resolve_stage_outcome(QA_CONTRACT, {"status": "success"}), "succeeded"
        )

    def test_outcome_blocked(self):
        self.assertEqual(
            sc.resolve_stage_outcome(QA_CONTRACT, {"status": "failed"}), "blocked"
        )

    def test_outcome_defaults_succeeded_without_mapping(self):
        self.assertEqual(
            sc.resolve_stage_outcome({"outcome": None}, {"anything": 1}), "succeeded"
        )

    def test_outcome_unknown_value_defaults_succeeded(self):
        self.assertEqual(
            sc.resolve_stage_outcome(QA_CONTRACT, {"status": "weird"}), "succeeded"
        )


class ExtractAndSummarizeTests(unittest.TestCase):
    def test_extract_required_artifacts(self):
        contract = {
            "required_artifacts": {
                "requirements": "/artifacts/requirements_markdown",
                "missing": "/artifacts/nope",
            }
        }
        output = {"artifacts": {"requirements_markdown": "# reqs"}}
        extracted = sc.extract_required_artifacts(contract, output)
        self.assertEqual(extracted, {"requirements": "# reqs"})

    def test_summarize_contract_mentions_keys_and_outcome(self):
        summary = sc.summarize_stage_contract(QA_CONTRACT)
        self.assertIn("status", summary)
        self.assertIn("succeeded", summary)

    def test_summarize_accepts_stage_like_object(self):
        class FakeStage:
            output_schema = {"type": "object", "required": ["foo"]}
            contract_ref = ""

        summary = sc.summarize_stage_contract(FakeStage())
        self.assertIn("foo", summary)


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / "config" / "workflows"


def _contract(workflow_type: str, stage_name: str) -> dict:
    skeleton = wd.load_skeleton(WORKFLOW_DIR / f"{workflow_type}.json")
    contract = skeleton.resolve_stage_contract(stage_name)
    assert contract is not None
    return contract


class GoldenParityTests(unittest.TestCase):
    """Invalid stage outputs that failed legacy validators still fail contracts."""

    def test_intake_without_questions_fails(self):
        contract = _contract("prd_request", "intake")
        self.assertTrue(
            sc.validate_stage_output(contract, {"summary": "no questions here"})
        )

    def test_intake_with_questions_passes(self):
        contract = _contract("prd_request", "intake")
        self.assertEqual(
            sc.validate_stage_output(contract, {"questions": ["What is the scope?"]}),
            [],
        )

    def test_prd_draft_without_markdown_fails(self):
        contract = _contract("prd_request", "draft_prd")
        self.assertTrue(sc.validate_stage_output(contract, {"summary": "nope"}))

    def test_prd_draft_with_result_markdown_passes(self):
        contract = _contract("prd_request", "draft_prd")
        self.assertEqual(
            sc.validate_stage_output(
                contract, {"result": {"prd_markdown": "# PRD\n\nBody"}}
            ),
            [],
        )

    def test_prd_draft_with_top_level_markdown_passes(self):
        contract = _contract("prd_request", "draft_prd")
        self.assertEqual(
            sc.validate_stage_output(contract, {"prd_markdown": "# PRD\n\nBody"}), []
        )

    def test_refinery_thin_artifacts_fail(self):
        contract = _contract("work_orders_from_prd", "refine_requirements")
        bad = {"artifacts": {"requirements_markdown": "too thin"}}
        violations = sc.validate_stage_output(contract, bad, {"stage": "refinery"}, [])
        self.assertTrue(violations)

    def test_foundry_thin_artifacts_fail(self):
        contract = _contract("work_orders_from_prd", "design_blueprints")
        bad = {"artifacts": {"blueprints_markdown": "too thin"}}
        violations = sc.validate_stage_output(contract, bad, {"stage": "foundry"}, [])
        self.assertTrue(violations)

    def test_prepare_handoff_missing_fields_fail(self):
        contract = _contract("work_orders_from_prd", "prepare_work_order_delivery")
        self.assertTrue(
            sc.validate_stage_output(contract, {"planner_complete": True})
        )

    def test_prepare_handoff_complete_passes(self):
        contract = _contract("work_orders_from_prd", "prepare_work_order_delivery")
        output = {
            "planner_complete": True,
            "work_orders_handoff": True,
            "next_work_order_id": "WO-001",
            "work_order_ids": ["WO-001"],
        }
        self.assertEqual(sc.validate_stage_output(contract, output), [])

    def test_deliver_missing_pr_url_fails(self):
        contract = _contract("deliver_work_order", "deliver_work_order")
        self.assertTrue(
            sc.validate_stage_output(contract, {"work_order_id": "WO-1"})
        )

    def test_deliver_fabricated_pr_url_fails(self):
        contract = _contract("deliver_work_order", "deliver_work_order")
        output = {"work_order_id": "WO-1", "pr_url": "https://x/pull/9"}
        audit = [
            {
                "tool": "run_coding_backend",
                "decision": "allow",
                "result_preview": json.dumps({"ok": False, "error": "git failed"}),
            }
        ]
        self.assertTrue(sc.validate_stage_output(contract, output, {}, audit))


if __name__ == "__main__":
    unittest.main()
