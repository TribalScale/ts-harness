"""Unit tests for planning docs splitter and materializer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation.planning_docs import (
    materialize_planning_docs,
    planning_docs_file_map,
    split_markdown_by_id,
)


class SplitMarkdownByIdTests(unittest.TestCase):
    def test_split_frds(self):
        md = (
            "# FRD-001 Matchmaking\n\nQueue and rooms.\n\n"
            "## FRD-002 Gameplay\n\nTurns and rules.\n"
        )
        sections = split_markdown_by_id(md, "FRD")
        self.assertEqual(set(sections), {"FRD-001", "FRD-002"})
        self.assertIn("Matchmaking", sections["FRD-001"])
        self.assertIn("Gameplay", sections["FRD-002"])
        self.assertNotIn("FRD-002", sections["FRD-001"])

    def test_split_work_orders(self):
        md = "# WO-001 Lobby\n\nBuild lobby.\n\n# WO-002 Match\n\nBuild match.\n"
        sections = split_markdown_by_id(md, "WO")
        self.assertEqual(list(sections), ["WO-001", "WO-002"])
        self.assertIn("Build lobby", sections["WO-001"])

    def test_split_returns_empty_for_blank(self):
        self.assertEqual(split_markdown_by_id("", "FRD"), {})
        self.assertEqual(split_markdown_by_id("no headings", "FRD"), {})


class PlanningDocsFileMapTests(unittest.TestCase):
    def test_maps_split_and_whole_key_files(self):
        refinery = {
            "product_overview_markdown": "# Overview\n\nProduct.",
            "requirements_markdown": "# Requirements\n\nMust work.",
            "feature_tree_markdown": "# Feature Tree\n\n- A\n- B",
            "frds_markdown": (
                "## FRD-001 Matchmaking\n\nQueue.\n\n"
                "## FRD-002 Gameplay\n\nTurns.\n"
            ),
        }
        foundry = {
            "blueprints_markdown": (
                "## COMP-001 Game Server\n\nOwns state.\n\n"
                "## FB-001 Matchmaking\n\nQueue feature.\n"
            ),
            "architecture_decisions_markdown": "## ADR-001 Realtime\n\nUse websockets.\n",
            "architecture_summary_markdown": "# Architecture Summary\n\nConsistent.\n",
        }
        planner = {
            "work_orders_markdown": (
                "## WO-001 Lobby\n\nBuild lobby.\n\n"
                "## WO-002 Match\n\nBuild match.\n"
            ),
            "context_graph_markdown": "# Context Graph\n\nWO-001 -> WO-002\n",
            "execution_phases_markdown": "# Execution Phases\n\nPhase 1: WO-001\n",
        }
        files = planning_docs_file_map(
            refinery,
            foundry,
            planner,
            approved_prd_markdown="# PRD\n\nCheckers.",
            active_work_order_id="WO-001",
        )
        self.assertEqual(files["docs/prd/approved-prd.md"], "# PRD\n\nCheckers.\n")
        self.assertIn("docs/refinery/product-overview.md", files)
        self.assertIn("docs/refinery/requirements.md", files)
        self.assertIn("docs/refinery/feature-tree.md", files)
        self.assertTrue(any(p.startswith("docs/refinery/features/FRD-001") for p in files))
        self.assertTrue(any(p.startswith("docs/refinery/features/FRD-002") for p in files))
        self.assertTrue(any(p.startswith("docs/foundry/components/COMP-001") for p in files))
        self.assertTrue(any(p.startswith("docs/foundry/features/FB-001") for p in files))
        self.assertTrue(
            any(p.startswith("docs/foundry/architectural-decisions/ADR-001") for p in files)
        )
        self.assertIn("docs/foundry/architecture-summary.md", files)
        self.assertIn("docs/planner/context-graph.md", files)
        self.assertIn("docs/planner/execution-phases.md", files)
        self.assertTrue(any(p.startswith("docs/planner/work-orders/WO-001") for p in files))
        self.assertTrue(any(p.startswith("docs/planner/work-orders/WO-002") for p in files))

    def test_unassigned_preamble_written(self):
        refinery = {
            "frds_markdown": "Intro notes before FRDs.\n\n## FRD-001 Lobby\n\nBody.\n",
        }
        files = planning_docs_file_map(refinery, {}, {})
        self.assertIn("docs/refinery/features/_unassigned.md", files)
        self.assertIn("Intro notes", files["docs/refinery/features/_unassigned.md"])


class MaterializePlanningDocsTests(unittest.TestCase):
    def test_writes_only_under_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            written = materialize_planning_docs(
                repo,
                {"docs/planner/work-orders/WO-001.md": "# WO-001\n\nBody\n"},
            )
            self.assertEqual(written, ["docs/planner/work-orders/WO-001.md"])
            target = repo / "docs/planner/work-orders/WO-001.md"
            self.assertTrue(target.is_file())
            self.assertIn("WO-001", target.read_text())

    def test_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            with self.assertRaises(ValueError):
                materialize_planning_docs(repo, {"../evil.md": "nope"})
            with self.assertRaises(ValueError):
                materialize_planning_docs(repo, {"src/app.py": "nope"})


if __name__ == "__main__":
    unittest.main()
