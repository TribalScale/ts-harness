"""Markdown subset to Atlassian Document Format and back."""

from __future__ import annotations

import unittest

from ts_harness.integrations.adf import adf_from_text, adf_to_text
from ts_harness.jira.pointer_block import format_pointer_block, parse_pointer_block


def blocks(text: str) -> list[dict]:
    return adf_from_text(text)["content"]


def text_node(text: str, *marks: dict) -> dict:
    node: dict = {"type": "text", "text": text}
    if marks:
        node["marks"] = list(marks)
    return node


class AdfFromTextTests(unittest.TestCase):
    def test_plain_text_keeps_paragraphs_and_hard_breaks(self):
        doc = adf_from_text("one\ntwo\n\nthree")
        self.assertEqual(doc["type"], "doc")
        self.assertEqual(doc["version"], 1)
        self.assertEqual(
            doc["content"],
            [
                {"type": "paragraph", "content": [text_node("one"), {"type": "hardBreak"}, text_node("two")]},
                {"type": "paragraph", "content": [text_node("three")]},
            ],
        )

    def test_empty_text_is_an_empty_document(self):
        self.assertEqual(adf_from_text("")["content"], [])
        self.assertEqual(adf_from_text(None)["content"], [])

    def test_headings(self):
        self.assertEqual(
            blocks("## Acceptance Criteria\nbody"),
            [
                {"type": "heading", "attrs": {"level": 2}, "content": [text_node("Acceptance Criteria")]},
                {"type": "paragraph", "content": [text_node("body")]},
            ],
        )

    def test_heading_level_is_capped_at_six(self):
        self.assertEqual(blocks("####### deep")[0]["type"], "paragraph")
        self.assertEqual(blocks("###### six")[0]["attrs"], {"level": 6})

    def test_bullet_and_ordered_lists(self):
        bullet, ordered = blocks("- a\n* b\n\n1. first\n2. second")
        self.assertEqual(bullet["type"], "bulletList")
        self.assertEqual(
            bullet["content"],
            [
                {"type": "listItem", "content": [{"type": "paragraph", "content": [text_node("a")]}]},
                {"type": "listItem", "content": [{"type": "paragraph", "content": [text_node("b")]}]},
            ],
        )
        self.assertEqual(ordered["type"], "orderedList")
        self.assertEqual(len(ordered["content"]), 2)

    def test_checkboxes_become_a_task_list(self):
        (tasks,) = blocks("- [ ] write tests\n- [x] ship it")
        self.assertEqual(tasks["type"], "taskList")
        self.assertTrue(tasks["attrs"]["localId"])
        first, second = tasks["content"]
        self.assertEqual(first["type"], "taskItem")
        self.assertEqual(first["attrs"]["state"], "TODO")
        self.assertEqual(first["content"], [text_node("write tests")])
        self.assertEqual(second["attrs"]["state"], "DONE")
        self.assertNotEqual(first["attrs"]["localId"], second["attrs"]["localId"])

    def test_fenced_code_block_keeps_lines_verbatim(self):
        (code,) = blocks("```gherkin\nGiven a user\nWhen **they** save\n```")
        self.assertEqual(code, {"type": "codeBlock", "attrs": {"language": "gherkin"}, "content": [text_node("Given a user\nWhen **they** save")]})

    def test_unclosed_fence_runs_to_the_end(self):
        (code,) = blocks("```\nline")
        self.assertEqual(code["type"], "codeBlock")
        self.assertEqual(code["content"], [text_node("line")])

    def test_rule_and_blockquote(self):
        rule, quote = blocks("---\n> no Figma frame")
        self.assertEqual(rule, {"type": "rule"})
        self.assertEqual(quote, {"type": "blockquote", "content": [{"type": "paragraph", "content": [text_node("no Figma frame")]}]})

    def test_table_with_header_row(self):
        (table,) = blocks("| Metric | Target |\n| --- | --- |\n| activation | 40% |")
        self.assertEqual(table["type"], "table")
        header, row = table["content"]
        self.assertEqual([cell["type"] for cell in header["content"]], ["tableHeader", "tableHeader"])
        self.assertEqual([cell["type"] for cell in row["content"]], ["tableCell", "tableCell"])
        self.assertEqual(row["content"][1]["content"], [{"type": "paragraph", "content": [text_node("40%")]}])

    def test_table_without_separator_has_no_header(self):
        (table,) = blocks("| a | b |\n| c | d |")
        self.assertEqual({cell["type"] for row in table["content"] for cell in row["content"]}, {"tableCell"})

    def test_inline_marks(self):
        (paragraph,) = blocks("Run **now** with `make test` per [docs](https://example.com/x)")
        self.assertEqual(
            paragraph["content"],
            [
                text_node("Run "),
                text_node("now", {"type": "strong"}),
                text_node(" with "),
                text_node("make test", {"type": "code"}),
                text_node(" per "),
                text_node("docs", {"type": "link", "attrs": {"href": "https://example.com/x"}}),
            ],
        )

    def test_block_boundaries_without_blank_lines(self):
        types = [block["type"] for block in blocks("intro\n## Tasks\n- [ ] a\n## Notes\ntext")]
        self.assertEqual(types, ["paragraph", "heading", "taskList", "heading", "paragraph"])


class AdfRoundTripTests(unittest.TestCase):
    def test_markdown_survives_a_round_trip(self):
        source = "## Story\nAs a PM\n\n- [ ] task\n- [x] done\n\n- bullet\n\n```\nGiven x\nThen y\n```"
        text = adf_to_text(adf_from_text(source))
        self.assertIn("## Story", text)
        self.assertIn("- [ ] task", text)
        self.assertIn("- [x] done", text)
        self.assertIn("- bullet", text)
        self.assertIn("```\nGiven x\nThen y\n```", text)

    def test_pointer_block_parses_after_a_round_trip(self):
        block = format_pointer_block(work_order_id="WO-003", repo_id="org/app", base_branch="dev", program_id="prog-1")
        for body in (f"## Summary\nDo it.\n\n{block}", f"## Summary\nDo it.\n\n```text\n{block}\n```"):
            pointer = parse_pointer_block(adf_to_text(adf_from_text(body)))
            self.assertEqual((pointer.work_order_id, pointer.repo_id, pointer.base_branch, pointer.program_id), ("WO-003", "org/app", "dev", "prog-1"))


if __name__ == "__main__":
    unittest.main()
