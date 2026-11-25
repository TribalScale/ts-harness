"""A block is posted to the requester's thread once, not on every re-check."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ts_harness.workflows.hooks import notify_bound_channel as nbc


def _ctx(task_id="t1", event="blocked", error="external_blocked:x tickets not started", channel="slack:C1:123.4"):
    return SimpleNamespace(
        skeleton=SimpleNamespace(notifications={"bound_channel": True}),
        runner_event=event,
        workflow={"source_channel": channel},
        workflow_id="wf1",
        task={"id": task_id, "step_name": "join_pool"},
        extras={"error": error},
        tenant_id="default",
    )


class SlackDedupeTests(unittest.TestCase):
    def setUp(self):
        nbc._POSTED_BLOCKS.clear()

    def test_same_block_is_posted_once(self):
        notifier = MagicMock()
        with patch("ts_harness.gateway.workflow_presentation.bound_channel", return_value=("slack", "C1", "123.4")), \
                patch("ts_harness.notifications.slack.SlackNotifier", return_value=notifier):
            first = nbc.handle(_ctx())
            second = nbc.handle(_ctx())
        self.assertEqual(first["status"], "notified")
        self.assertEqual(second, {"status": "skipped", "reason": "same block already posted"})
        self.assertEqual(notifier.notify_workflow_event.call_count, 1)

    def test_a_new_reason_or_a_block_after_the_step_ran_again_is_posted(self):
        notifier = MagicMock()
        with patch("ts_harness.gateway.workflow_presentation.bound_channel", return_value=("slack", "C1", "123.4")), \
                patch("ts_harness.notifications.slack.SlackNotifier", return_value=notifier):
            nbc.handle(_ctx())
            nbc.handle(_ctx(error="external_blocked:x something else"))
            nbc.handle(_ctx(event="succeeded", error=None))
            nbc.handle(_ctx(error="external_blocked:x something else"))
        blocked = [c for c in notifier.notify_workflow_event.call_args_list if c.kwargs["event"] == "blocked"]
        self.assertEqual(len(blocked), 3)


class WebTranscriptDedupeTests(unittest.TestCase):
    def _store(self, messages):
        store = MagicMock()
        store.load_messages.return_value = messages
        return store

    def _append(self, store):
        with patch("ts_harness.agent.transcript.get_transcript_store", return_value=store), \
                patch("ts_harness.gateway.workflow_presentation._session_for_workflow",
                      return_value=SimpleNamespace(session_id="s1")):
            return nbc._append_web_transcript(_ctx(channel="web:web:web:x"), "web:web:web:x", "Step blocked: X")

    def test_the_same_text_since_the_person_last_wrote_is_not_appended(self):
        store = self._store([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "Step blocked: X"}])
        self.assertTrue(self._append(store))
        store.append_messages.assert_not_called()

    def test_after_the_person_writes_it_is_said_again(self):
        store = self._store([{"role": "assistant", "content": "Step blocked: X"}, {"role": "user", "content": "retry it"}])
        self.assertTrue(self._append(store))
        store.append_messages.assert_called_once()


if __name__ == "__main__":
    unittest.main()
