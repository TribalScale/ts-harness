"""Chat attachments: adapters, trust rules, holding on the session, and the start handlers."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

from ts_harness.agent.sessions import SessionRecord, get_session_store, reset_session_store
from ts_harness.gateway import intake_pointers
from ts_harness.gateway.channels.discord import DiscordAdapter
from ts_harness.gateway.channels.slack import slack_file_pointers
from ts_harness.gateway.channels.telegram import TelegramAdapter
from ts_harness.gateway.delegate_confirmation import (
    ProgramConfirmation,
    apply_program_confirmation_to_session,
    set_pending_program,
    set_pending_workflow,
)
from ts_harness.gateway.server import RuntimeState
from ts_harness.intake.blobs import stash_stream
from ts_harness.intake.connectors import prefetch_discord_files
from ts_harness.storage.objects import LocalObjectStore
from ts_harness.tools.policy import ToolPolicyContext
from ts_harness.tools.workflow_tools import start_program_handler, start_workflow_handler

DOC = "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQ/edit"


class _StoreCase(unittest.TestCase):
    def setUp(self):
        reset_session_store()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = LocalObjectStore(Path(tmp.name))
        patcher = mock.patch("ts_harness.intake.blobs.intake_store", return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stored(self, data: bytes = b"brief", name: str = "brief.txt", tenant: str = "default") -> dict:
        return stash_stream(tenant_id=tenant, name=name, mime="text/plain", fileobj=io.BytesIO(data), size=len(data),
                            store=self.store)

    def _session(self, session_id: str = "s1", **metadata) -> SessionRecord:
        session = SessionRecord(
            session_id=session_id, routing_key="rk", tenant_id="default", agent_id="orchestrator", source="web",
            metadata={"channel": "web", "channel_id": "web", "thread_id": session_id, **metadata},
        )
        get_session_store().save(session)
        return session


class AdapterTests(unittest.TestCase):
    def test_slack_files(self):
        pointers = slack_file_pointers([{"id": "F1", "name": "spec.pdf", "mimetype": "application/pdf", "size": 9}, {"x": 1}])
        self.assertEqual(pointers, [{"kind": "slack_file", "file_id": "F1", "name": "spec.pdf",
                                     "mime": "application/pdf", "size": 9}])

    def test_telegram_document_photo_and_caption(self):
        adapter = TelegramAdapter(bot_token="t")
        doc = adapter.normalize_inbound({"message": {"caption": "see this", "chat": {"id": 1}, "from": {"id": 2},
                                                     "document": {"file_id": "D", "file_unique_id": "U", "file_name": "a.docx",
                                                                  "mime_type": "x", "file_size": 3}}}, tenant_id="t")
        self.assertEqual(doc.text, "see this")
        self.assertEqual(doc.attachments[0]["file_unique_id"], "U")
        photo = adapter.normalize_inbound({"message": {"chat": {"id": 1}, "from": {"id": 2}, "photo": [
            {"file_id": "small", "file_size": 10, "width": 90}, {"file_id": "big", "file_size": 900, "width": 1280}]}},
            tenant_id="t")
        self.assertEqual((photo.text, photo.attachments[0]["file_id"]), ("", "big"))

    def test_discord_attachments_carry_their_prefetch_key(self):
        event = DiscordAdapter().normalize_inbound({
            "content": "", "author": {"id": "u"}, "channel_id": "c",
            "attachments": [{"id": "11", "url": "https://cdn.discordapp.com/a.png", "filename": "a.png", "size": 5},
                            {"id": "12", "url": "https://cdn.discordapp.com/b.mov", "filename": "b.mov", "size": 30 * 1024 * 1024}],
        }, tenant_id="acme")
        self.assertEqual(event.attachments[0]["object_key"], "intake/acme/discord/11")
        self.assertNotIn("error", event.attachments[0])
        self.assertIn("20 MB", event.attachments[1]["error"])


class DiscordPrefetchTests(_StoreCase):
    def test_prefetch_copies_to_the_named_key_and_skips_errors(self):
        seen: list[str] = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, content=b"png bytes")

        pointers = [
            {"kind": "discord_file", "attachment_id": "1", "url": "https://cdn.test/a", "name": "a.png",
             "object_key": "intake/acme/discord/1"},
            {"kind": "discord_file", "attachment_id": "2", "url": "https://cdn.test/b", "name": "b",
             "object_key": "intake/acme/discord/2", "error": "file is larger than 20 MB"},
        ]
        copied = prefetch_discord_files(pointers, tenant_id="acme", client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.assertEqual(copied, 1)
        self.assertEqual(self.store.get("intake/acme/discord/1"), b"png bytes")
        self.assertEqual(seen, ["https://cdn.test/a"])
        # A second run finds the copy and does not fetch again.
        prefetch_discord_files(pointers[:1], tenant_id="acme", client=httpx.Client(transport=httpx.MockTransport(handler)))
        self.assertEqual(len(seen), 1)


class TrustTests(_StoreCase):
    def test_clients_may_only_send_validated_stored_pointers(self):
        good = self._stored()
        other_tenant = self._stored(b"theirs", tenant="other")
        body = {"attachments": [
            good,
            other_tenant,
            {"kind": "slack_file", "file_id": "F_SECRET", "name": "payroll.xlsx"},
            {"kind": "discord_file", "url": "https://cdn/x", "name": "x"},
            {"name": "inline.txt", "content_base64": "aGk="},
        ]}
        pointers = intake_pointers.message_pointers(body, tenant_id="default", text=f"and {DOC}")
        kinds = [(p["kind"], p.get("error")) for p in pointers]
        self.assertEqual(kinds[0], ("stored", None))
        self.assertEqual(kinds[1], ("stored", "attachment not found"))
        self.assertEqual([k for k, e in kinds[2:4]], ["stored", "stored"])
        self.assertTrue(all(e for _k, e in kinds[1:5]))
        self.assertEqual(kinds[5], ("drive", None))
        self.assertNotIn("F_SECRET", json.dumps(pointers))

    def test_channel_attachments_are_trusted_only_for_channel_kinds(self):
        pointers = intake_pointers.message_pointers(
            {}, tenant_id="t", text="",
            channel_attachments=[{"kind": "slack_file", "file_id": "F1", "name": "a"}, {"kind": "stored", "object_key": "x"}],
        )
        self.assertEqual([p["kind"] for p in pointers], ["slack_file"])


class SessionHoldTests(_StoreCase):
    def _collect(self, session, body, *, text, channel_attachments=None):
        return RuntimeState._collect_intake_pointers(
            mock.Mock(), session, body, user_text=text, tenant_id="default", channel_attachments=channel_attachments
        )

    def test_files_only_message_gets_placeholder_text_and_a_note(self):
        session = self._session()
        pointer = self._stored()
        session, text, note, _sources = self._collect(session, {"attachments": [pointer]}, text="")
        self.assertEqual(text, intake_pointers.FILES_ONLY_TEXT)
        self.assertEqual(note, "Attachments: brief.txt. They are read at intake.")
        saved = get_session_store().get("s1").metadata["intake_pointers"]
        self.assertEqual(saved[0]["object_key"], pointer["object_key"])
        self.assertLess(len(json.dumps(get_session_store().get("s1").metadata)), 10_000)

    def test_large_upload_keeps_session_metadata_small(self):
        session = self._session()
        big = self._stored(b"x" * (5 * 1024 * 1024), name="big.txt")
        self._collect(session, {"attachments": [big]}, text="plan this")
        self.assertLess(len(json.dumps(get_session_store().get("s1").metadata)), 10_000)

    def test_replies_in_a_started_thread_are_read_now(self):
        session = self._session(program_id="p1", workflow_id="w1", workflow_type="content_to_plan")
        pointer = self._stored(b"The login must support SSO.", name="notes.txt")
        _session, text, note, sources = self._collect(session, {"attachments": [pointer]}, text="here you go")
        self.assertEqual((text, note), ("here you go", ""))
        self.assertIn("The login must support SSO.", sources)
        self.assertNotIn("intake_pointers", get_session_store().get("s1").metadata)

    def test_reply_files_only_and_budget(self):
        session = self._session(workflow_id="w1", workflow_type="rfp_request")
        calls = {}

        def fake_resolve(pointers, **kwargs):
            calls.update(kwargs)
            return {"sources_text": "## Shared sources\nrfp body\n"}

        with mock.patch("ts_harness.intake.resolve.resolve_intake_sources", side_effect=fake_resolve), \
                mock.patch("ts_harness.intake.connectors.build_drive") as build_drive:
            _s, text, _n, sources = self._collect(session, {}, text=f"see {DOC}")
        self.assertEqual(sources, "## Shared sources\nrfp body\n")
        self.assertEqual(calls["deadline_s"], 60.0)
        self.assertEqual(calls["max_total_text"], 75_000)
        build_drive.assert_called_once_with(tenant_id="default", agent_id="sales-agent")
        with mock.patch("ts_harness.intake.resolve.resolve_intake_sources", side_effect=RuntimeError("down")):
            _s, text, _n, sources = self._collect(session, {"attachments": [self._stored()]}, text="")
        self.assertEqual((text, sources), (intake_pointers.FILES_ONLY_TEXT, ""))

    def test_later_messages_add_to_the_pending_proposal_under_the_caps(self):
        session = self._session()
        set_pending_program(session, entry_phase="plan", summary="build login", program_type="content_to_jira")
        session = get_session_store().get("s1")
        pointers = [{"kind": "slack_file", "file_id": f"F{i}", "name": f"{i}.png", "size": 30 * 1024 * 1024 // 2}
                    for i in range(6)]
        _session, _text, note, _sources = self._collect(session, {}, text="", channel_attachments=pointers)
        metadata = get_session_store().get("s1").metadata
        pending = metadata["pending_program"]["intake_sources"]
        self.assertEqual(len(pending), 6)
        self.assertEqual(sum(1 for p in pending if p.get("error")), 1)  # 6 x 15 MB > 80 MB
        self.assertIn("Not attached: 5.png", note)

    def test_duplicate_slack_retry_adds_nothing(self):
        session = self._session()
        pointer = {"kind": "slack_file", "file_id": "F1", "name": "a.pdf", "size": 1}
        session, _t, _n, _s = self._collect(session, {}, text="x", channel_attachments=[pointer])
        _s, _t, note, _src = self._collect(get_session_store().get("s1"), {}, text="x", channel_attachments=[pointer])
        self.assertEqual(note, "")
        self.assertEqual(len(get_session_store().get("s1").metadata["intake_pointers"]), 1)


class ProposalAndStartTests(_StoreCase):
    def test_set_pending_copies_session_pointers(self):
        pointer = self._stored()
        session = self._session(intake_pointers=[pointer])
        set_pending_program(session, entry_phase="plan", summary="s", program_type="content_to_jira")
        set_pending_workflow(session, workflow_type="rfp_request", summary="s", input_data={"client": "acme"})
        metadata = get_session_store().get("s1").metadata
        self.assertEqual(metadata["pending_program"]["intake_sources"], [pointer])
        self.assertEqual(metadata["pending_workflow"]["input"], {"client": "acme", "intake_sources": [pointer]})

    def test_declining_releases_the_attachments(self):
        session = self._session(intake_pointers=[self._stored()])
        set_pending_program(session, entry_phase="plan", summary="s")
        apply_program_confirmation_to_session(get_session_store().get("s1"),
                                              ProgramConfirmation(cancelled=True, clear_pending=True))
        self.assertNotIn("intake_pointers", get_session_store().get("s1").metadata)

    @mock.patch("ts_harness.workflows.delivery_program.get_delivery_program_service")
    def test_start_program_passes_sources_as_first_task_input_not_artifacts(self, service_factory):
        pointer = self._stored()
        session = self._session(intake_pointers=[pointer])
        set_pending_program(session, entry_phase="plan", summary="build login", artifacts={"brief": "x"},
                            program_type="content_to_jira")
        service = service_factory.return_value
        service.create_program.return_value = SimpleNamespace(
            program_id="p1", entry_phase="plan", workflow_id="w1", workflow_type="content_to_plan", first_step="pick_repo",
            task_ids=["t1"], binding_key="b", status="started", program_type="content_to_jira",
        )
        ctx = ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="web", session_id="s1")
        result = start_program_handler({}, ctx)
        self.assertEqual(result.get("status"), "started", result)
        kwargs = service.create_program.call_args.kwargs
        self.assertEqual(kwargs["extra_input"], {"intake_sources": [pointer]})
        self.assertNotIn("intake_sources", kwargs["artifacts"])
        self.assertNotIn("intake_pointers", get_session_store().get("s1").metadata)

    @mock.patch("ts_harness.workflows.delivery_program.get_delivery_program_service")
    def test_start_program_without_attachments_sends_no_extra_input(self, service_factory):
        session = self._session()
        set_pending_program(session, entry_phase="plan", summary="build login", program_type="content_to_jira")
        service_factory.return_value.create_program.return_value = SimpleNamespace(
            program_id="p1", entry_phase="plan", workflow_id="w1", workflow_type="content_to_plan", first_step="x",
            task_ids=[], binding_key="b", status="started", program_type="content_to_jira",
        )
        start_program_handler({}, ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="web",
                                                    session_id="s1"))
        self.assertIsNone(service_factory.return_value.create_program.call_args.kwargs["extra_input"])

    @mock.patch("ts_harness.tools.workflow_tools.WorkflowPromotionService")
    def test_start_workflow_adds_sources_to_input(self, service_cls):
        pointer = self._stored()
        session = self._session(intake_pointers=[pointer])
        set_pending_workflow(session, workflow_type="rfp_request", summary="rfp", input_data={"client": "acme"})
        service_cls.return_value.start.return_value = SimpleNamespace(
            workflow_id="w1", workflow_type="rfp_request", first_step="gather_sources", binding_key="b", task_ids=[])
        result = start_workflow_handler({}, ToolPolicyContext(tenant_id="default", agent_id="orchestrator", source="web",
                                                              session_id="s1"))
        self.assertEqual(result["status"], "started")
        self.assertEqual(service_cls.return_value.start.call_args.kwargs["extra_input"],
                         {"client": "acme", "intake_sources": [pointer]})
        self.assertNotIn("intake_pointers", get_session_store().get("s1").metadata)


class WebhookTests(unittest.TestCase):
    def test_channel_attachments_reach_the_session_handler(self):
        event = SimpleNamespace(
            tenant_id="t", sender_email="a@b.c", sender_id="u", text="", thread_id="th",
            metadata={"channel_id": ""}, attachments=[{"kind": "slack_file", "file_id": "F1"}],
        )
        runtime = mock.Mock()
        runtime.handle_session_message.return_value = {"status": "succeeded", "output": {"summary": "ok"}}
        with mock.patch("ts_harness.gateway.server.get_channel_router") as router:
            router.return_value.normalize.return_value = event
            RuntimeState.handle_channel_webhook(runtime, "slack", {"tenant_id": "t"}, mock.Mock())
        _body, _auth = runtime.handle_session_message.call_args.args
        self.assertNotIn("attachments", _body)
        self.assertEqual(runtime.handle_session_message.call_args.kwargs["channel_attachments"], event.attachments)

    def test_discord_attachments_start_a_prefetch(self):
        attachments = [{"kind": "discord_file", "attachment_id": "1", "url": "u", "object_key": "intake/t/discord/1"}]
        event = SimpleNamespace(tenant_id="t", sender_email="a@b.c", sender_id="u", text="", thread_id="th",
                                metadata={"channel_id": ""}, attachments=attachments)
        runtime = mock.Mock()
        runtime.handle_session_message.return_value = {"status": "succeeded"}
        with mock.patch("ts_harness.gateway.server.get_channel_router") as router, \
                mock.patch("ts_harness.intake.connectors.start_discord_prefetch") as prefetch:
            router.return_value.normalize.return_value = event
            RuntimeState.handle_channel_webhook(runtime, "discord", {"tenant_id": "t"}, mock.Mock())
        prefetch.assert_called_once_with(attachments, tenant_id="t")


if __name__ == "__main__":
    unittest.main()


class ReplyAnswerTests(unittest.TestCase):
    """Routing reads the person's words; the recorded answer carries the files read from the reply."""

    def setUp(self):
        from ts_harness.gateway import channel_confirmations as cc

        self.cc = cc
        self.session = SimpleNamespace(metadata={"workflow_id": "w1"})
        self.gate = {"id": "gate-1", "input": {"answers": []}}

    def test_fenced_block(self):
        self.assertEqual(intake_pointers.with_reply_sources("hi", ""), "hi")
        block = intake_pointers.with_reply_sources("hi", "## Shared sources\nbody\n")
        self.assertTrue(block.startswith("hi\n\n<intake_sources>\n## Shared sources"))
        self.assertTrue(block.endswith("</intake_sources>"))

    def test_intake_answer_records_the_files_but_classifies_the_words(self):
        answer = intake_pointers.with_reply_sources("SSO only", "## Shared sources\nwhat's the status of\n")
        memory = mock.Mock()
        with mock.patch.object(self.cc, "_pending_intake_gate", return_value=self.gate), \
                mock.patch.object(self.cc, "_repo_pick_reply", return_value=False), \
                mock.patch.object(self.cc, "_intake_go_ahead", return_value=False) as go_ahead, \
                mock.patch("ts_harness.memory.client.get_memory_client", return_value=memory):
            result = self.cc.execute_workflow_gate_response(
                self.session, tenant_id="t", agent_id="a", source="web", user_text="SSO only", answer_text=answer,
            )
        self.assertEqual(result["status"], "answer_recorded")
        go_ahead.assert_called_once_with(self.gate, "SSO only")
        recorded = memory.patch_task_input.call_args.args[1]["answers"]
        self.assertEqual(recorded, [answer])

    def test_ready_on_an_intake_gate_folds_the_files_into_the_reply(self):
        answer = intake_pointers.with_reply_sources("ready", "## Shared sources\nspec\n")
        with mock.patch.object(self.cc, "_pending_intake_gate", return_value=self.gate), \
                mock.patch.object(self.cc, "_repo_pick_reply", return_value=False), \
                mock.patch.object(self.cc, "_intake_go_ahead", return_value=True), \
                mock.patch.object(self.cc, "execute_workflow_gate_classification", return_value={}) as classify:
            self.cc.execute_workflow_gate_response(
                self.session, tenant_id="t", agent_id="a", source="web", user_text="ready", answer_text=answer,
            )
        self.assertEqual(classify.call_args.kwargs["reply_text"], answer)

    def test_repo_pick_reads_only_the_words(self):
        answer = intake_pointers.with_reply_sources("acme/app", "## Shared sources\nmigrate other/repo\n")
        with mock.patch.object(self.cc, "_pending_intake_gate", return_value=None), \
                mock.patch.object(self.cc, "_repo_pick_reply", return_value=True), \
                mock.patch.object(self.cc, "execute_workflow_gate_classification", return_value={}) as classify:
            self.cc.execute_workflow_gate_response(
                self.session, tenant_id="t", agent_id="a", source="web", user_text="acme/app", answer_text=answer,
            )
        self.assertEqual(classify.call_args.kwargs["reply_text"], "acme/app")
