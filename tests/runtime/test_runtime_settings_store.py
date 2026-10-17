from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ts_harness.workflows.runtime_settings_store import (
    ConflictError,
    RuntimeSettingsStore,
)


def _definition():
    return {
        "definition_type": "runtime_settings",
        "definition_version": "1.0.0",
        "identity": {"jira_account_ids": {"ada@example.com": "acct-1"}},
    }


class RuntimeSettingsStoreTests(unittest.TestCase):
    def test_create_activate_and_list_drafts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeSettingsStore(Path(tmp))
            draft = store.create_draft(_definition(), actor_id="admin")
            self.assertEqual(len(store.list_drafts()), 1)
            activated = store.activate(
                draft["id"],
                expected_revision=draft["revision"],
                actor_id="admin",
            )
            self.assertEqual(activated["definition_type"], "runtime_settings")
            active = store.get_active("runtime_settings")
            self.assertEqual(
                active["identity"]["jira_account_ids"]["ada@example.com"],
                "acct-1",
            )
            self.assertEqual(store.list_drafts(), [])

    def test_revision_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RuntimeSettingsStore(Path(tmp))
            draft = store.create_draft(_definition(), actor_id="admin")
            with self.assertRaises(ConflictError):
                store.update_draft(
                    draft["id"],
                    _definition(),
                    expected_revision="not-the-revision",
                    actor_id="admin",
                )


if __name__ == "__main__":
    unittest.main()
