import tempfile
import unittest
from pathlib import Path

from ts_harness.config.schema import AgentConfig, GatewayConfig, RuntimeConfig, TenantConfig
from ts_harness.skills.catalog import SkillCatalog
from ts_harness.skills.slash_invoke import (
    blocked_unresolved_payload,
    catalog_index_ids,
    parse_slash_invoke,
    resolve_slash_pins,
)


CATALOG_IDS = {
    "jira-to-pr",
    "pm/google-workspace",
    "ops/health-check",
    "a",
    "b",
    "c",
    "d",
    "e",
    "f",
    "learn",
}


class SlashInvokeParseTests(unittest.TestCase):
    def _parse(self, text: str):
        return parse_slash_invoke(text, catalog_ids=CATALOG_IDS)

    def test_plain_text_has_no_pins(self):
        parsed = self._parse("check Artemis")
        self.assertEqual(parsed.pins, [])
        self.assertEqual(parsed.remainder, "check Artemis")
        self.assertFalse(parsed.reserved_learn)

    def test_one_pin_and_remainder(self):
        parsed = self._parse("/jira-to-pr check Artemis")
        self.assertEqual(parsed.pins, ["jira-to-pr"])
        self.assertEqual(parsed.remainder, "check Artemis")
        self.assertFalse(parsed.reserved_learn)

    def test_pin_alone_has_empty_remainder(self):
        parsed = self._parse("/jira-to-pr")
        self.assertEqual(parsed.pins, ["jira-to-pr"])
        self.assertEqual(parsed.remainder, "")

    def test_sixth_catalog_id_is_instruction(self):
        parsed = self._parse("/a /b /c /d /e /f leftover")
        self.assertEqual(parsed.pins, ["a", "b", "c", "d", "e"])
        self.assertEqual(parsed.remainder, "/f leftover")

    def test_unknown_path_is_instruction(self):
        parsed = self._parse("/tmp/scan.pdf extract")
        self.assertEqual(parsed.pins, [])
        self.assertEqual(parsed.remainder, "/tmp/scan.pdf extract")

    def test_slash_in_skill_id_is_one_pin(self):
        parsed = self._parse("/pm/google-workspace list files")
        self.assertEqual(parsed.pins, ["pm/google-workspace"])
        self.assertEqual(parsed.remainder, "list files")

    def test_reserved_learn_is_never_a_pin(self):
        parsed = self._parse("/learn how I deployed")
        self.assertEqual(parsed.pins, [])
        self.assertEqual(parsed.remainder, "/learn how I deployed")
        self.assertTrue(parsed.reserved_learn)

    def test_slack_link_is_not_a_pin(self):
        parsed = self._parse("see <http://example.com> please")
        self.assertEqual(parsed.pins, [])
        self.assertEqual(parsed.remainder, "see <http://example.com> please")

    def test_unknown_path_after_pin_ends_parsing(self):
        parsed = self._parse("/jira-to-pr /tmp/scan.pdf x")
        self.assertEqual(parsed.pins, ["jira-to-pr"])
        self.assertEqual(parsed.remainder, "/tmp/scan.pdf x")


def _write_skill(
    root: Path,
    category: str,
    filename: str,
    *,
    skill_id: str,
    approval: str = "approved",
    extra_yaml: str = "",
) -> None:
    directory = root / category
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(
        "---\n"
        f"id: {skill_id}\n"
        f"category: {category}\n"
        "trust_level: trusted\n"
        f"approval_status: {approval}\n"
        f"{extra_yaml}"
        "---\n"
        f"Body for {skill_id}.\n",
        encoding="utf-8",
    )


def _resolve_fixture():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    _write_skill(root, "ops", "keep-me.md", skill_id="keep-me")
    _write_skill(root, "ops", "other-one.md", skill_id="other-one")
    _write_skill(root, "other", "not-mine.md", skill_id="not-mine")
    _write_skill(root, "ops", "draft-skill.md", skill_id="draft-skill", approval="draft")
    _write_skill(root, "ops", "governed-delivery.md", skill_id="governed-delivery")
    _write_skill(root, "ops", "fan-out.md", skill_id="not-mine-extra")
    _write_skill(
        root,
        "ops",
        "needs-write.md",
        skill_id="needs-write",
        extra_yaml="required_toolsets:\n- core-write\n",
    )
    catalog = SkillCatalog(root)
    catalog.load()
    agent = AgentConfig(id="ops-agent", workspace="/w", model="m", skills=["ops"], toolsets=["core-read"])
    config = RuntimeConfig(
        gateway=GatewayConfig(token="t", default_tenant_id="default"),
        tenants=[TenantConfig(id="default", display_name="Default")],
        agents=[
            AgentConfig(id="orchestrator", workspace="/w", model="m", default=True),
            agent,
        ],
    )
    return tmp, catalog, agent, config


class SlashInvokeResolveTests(unittest.TestCase):
    def setUp(self):
        self._tmp, self.catalog, self.agent, self.config = _resolve_fixture()
        self.addCleanup(self._tmp.cleanup)

    def _resolve(self, pins: list[str]):
        return resolve_slash_pins(
            self.catalog,
            self.agent,
            self.config,
            tenant_id="default",
            pins=pins,
        )

    def test_keeps_only_the_pinned_assigned_skill(self):
        result = self._resolve(["keep-me"])
        self.assertEqual(result.unresolved, [])
        self.assertEqual([skill.metadata.id for skill in result.skills], ["keep-me"])

    def test_unassigned_pin_is_unresolved(self):
        result = self._resolve(["not-mine"])
        self.assertTrue(result.unresolved)
        self.assertIn("not-mine", result.unresolved[0][0])
        self.assertIn("not assigned", result.unresolved[0][1])

    def test_draft_pin_is_unresolved(self):
        result = self._resolve(["draft-skill"])
        self.assertTrue(result.unresolved)
        reason = result.unresolved[0][1]
        self.assertTrue("draft" in reason or "approval_status" in reason)

    def test_channel_chat_exclusion_is_unresolved(self):
        result = self._resolve(["governed-delivery"])
        self.assertTrue(result.unresolved)
        self.assertIn("channel-chat", result.unresolved[0][1])

    def test_mixed_pins_keep_resolved_and_list_unresolved(self):
        result = self._resolve(["keep-me", "not-mine"])
        self.assertEqual([skill.metadata.id for skill in result.skills], ["keep-me"])
        self.assertEqual(len(result.unresolved), 1)
        self.assertEqual(result.unresolved[0][0], "not-mine")

    def test_catalog_index_ids_are_meta_ids_not_version_keys(self):
        ids = catalog_index_ids(self.catalog)
        self.assertIn("keep-me", ids)
        self.assertNotIn("keep-me@1.0.0", ids)
        self.assertTrue(all("@" not in skill_id for skill_id in ids))

    def test_missing_toolset_reason_keeps_phrase(self):
        result = self._resolve(["needs-write"])
        self.assertTrue(result.unresolved)
        self.assertEqual(result.unresolved[0][0], "needs-write")
        self.assertIn("missing toolset", result.unresolved[0][1])

    def test_reason_does_not_use_substring_id_match(self):
        result = self._resolve(["not-mine"])
        self.assertTrue(result.unresolved)
        self.assertEqual(result.unresolved[0][0], "not-mine")
        self.assertIn("not assigned", result.unresolved[0][1])
        self.assertNotIn("channel-chat", result.unresolved[0][1])

    def test_blocked_payload_names_every_unresolved_id(self):
        payload = blocked_unresolved_payload(
            session_id="sess-1",
            unresolved=[("draft-prd", "draft"), ("jira-to-pr", "not assigned to orchestrator")],
        )
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["session_id"], "sess-1")
        self.assertEqual(payload["error"], "skill_invoke_unresolved")
        summary = payload["output"]["summary"]
        self.assertIn("/draft-prd", summary)
        self.assertIn("/jira-to-pr", summary)
