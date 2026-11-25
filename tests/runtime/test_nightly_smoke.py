"""Nightly smoke failures become one proposed bug, from an authenticated repository, once."""

from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from starlette.testclient import TestClient

from ts_harness.memory.client import MemoryApiConflict
from ts_harness.workflows import nightly_smoke as ns

SECRET = "smoke-secret"
NOW = 1_800_000_000.0


class FakeMemory:
    """runtime_kv with versions, and program statuses."""

    def __init__(self):
        self.kv: dict[tuple[str, str], tuple[object, int]] = {}
        self.statuses: dict[str, str] = {}

    def kv_get(self, namespace, key):
        value, version = self.kv.get((namespace, key), (None, 0))
        return {"value": copy.deepcopy(value), "version": version}

    def kv_put(self, namespace, key, value, *, expected_version=None):
        _, version = self.kv.get((namespace, key), (None, 0))
        if expected_version is not None and expected_version != version:
            raise MemoryApiConflict("stale", {"code": "version_conflict"})
        self.kv[(namespace, key)] = (copy.deepcopy(value), version + 1)
        return version + 1

    def get_program_status(self, program_id):
        return {"status": self.statuses.get(program_id, "completed")}


class FakeService:
    def __init__(self):
        self.calls: list[dict] = []
        self.fail = False

    def create_program(self, **kwargs):
        if self.fail:
            raise RuntimeError("memory-api down")
        self.calls.append(kwargs)
        return SimpleNamespace(program_id=f"prog-{len(self.calls)}")


def report(**overrides):
    body = {
        "repo": "org/web",
        "api_repo": "org/api",
        "api_ref": "dev",
        "run_id": "42",
        "run_url": "https://github.com/org/web/actions/runs/42",
        "web_sha": "a" * 40,
        "api_sha": "b" * 40,
        "artifact_url": "https://github.com/org/web/actions/runs/42/artifacts/7",
        "failures": [
            {
                "scenario": "Every page opens after signing in",
                "step": "Then every page opens without errors",
                "route": "/notes",
                "kind": "contract_violation",
                "error": "GET /notes 200 response: /data/0 must have required property 'status'",
            }
        ],
    }
    body.update(overrides)
    return body


class TokenTests(unittest.TestCase):
    def test_each_repository_has_its_own_token(self):
        token = ns.report_token("org/web", SECRET)
        self.assertEqual(token, ns.report_token("ORG/Web", SECRET))
        self.assertNotEqual(token, ns.report_token("org/other-web", SECRET))
        self.assertNotEqual(token, ns.report_token("org/web", "rotated"))
        self.assertTrue(ns.token_matches("org/web", token, SECRET))
        self.assertFalse(ns.token_matches("org/web", "", SECRET))

    def test_the_manual_setup_command_prints_only_into_a_pipe(self):
        import io

        piped = io.StringIO()
        with mock.patch.dict("os.environ", {ns.SECRET_ENV: SECRET}), mock.patch("sys.stdout", piped):
            self.assertEqual(ns.main(["token", "org/web"]), 0)
        self.assertEqual(piped.getvalue(), ns.report_token("org/web", SECRET))
        terminal = mock.Mock(isatty=mock.Mock(return_value=True))
        with mock.patch.dict("os.environ", {ns.SECRET_ENV: SECRET}), mock.patch("sys.stdout", terminal):
            self.assertEqual(ns.main(["token", "org/web"]), 2)
        terminal.write.assert_not_called()

    def test_bearer(self):
        self.assertEqual(ns.bearer("Bearer abc"), "abc")
        self.assertEqual(ns.bearer("token abc"), "")
        self.assertEqual(ns.bearer(None), "")


class ValidationTests(unittest.TestCase):
    def assertInvalid(self, body, words):
        with self.assertRaises(ns.SmokeReportError) as caught:
            ns.validate_report(body)
        self.assertEqual(caught.exception.status, 400)
        self.assertIn(words, str(caught.exception))

    def test_a_good_report(self):
        parsed = ns.validate_report(report())
        self.assertEqual((parsed.repo, parsed.api_repo, parsed.run_id), ("org/web", "org/api", "42"))
        self.assertEqual(parsed.failures[0].route, "/notes")

    def test_what_is_refused(self):
        self.assertInvalid([], "JSON object")
        self.assertInvalid(report(repo="not a repo"), "repo must be owner/name")
        self.assertInvalid(report(api_repo="x"), "api_repo")
        self.assertInvalid(report(run_url=""), "run_url is required")
        self.assertInvalid(report(run_url="http://github.com/x"), "https")
        self.assertInvalid(report(artifact_url="javascript:alert(1)"), "https")
        self.assertInvalid(report(web_sha="not-a-sha!"), "web_sha")
        self.assertInvalid(report(failures=[]), "non-empty")
        self.assertInvalid(report(failures=[{"route": "/x"}]), "scenario")

    def test_long_reports_are_bounded(self):
        many = [{"scenario": f"s{i}", "error": "x" * 10_000} for i in range(80)]
        parsed = ns.validate_report(report(failures=many))
        self.assertEqual(len(parsed.failures), ns.MAX_FAILURES)
        self.assertEqual(len(parsed.failures[0].error), ns.MAX_TEXT)


class ProposalTests(unittest.TestCase):
    def setUp(self):
        self.memory = FakeMemory()
        self.service = FakeService()
        self.token = ns.report_token("org/web", SECRET)

    def handle(self, body=None, *, token=None, now=NOW, secret=SECRET):
        return ns.handle_smoke_report(
            report() if body is None else body,
            self.token if token is None else token,
            tenant_id="default",
            memory=self.memory,
            service=self.service,
            secret=secret,
            now=now,
        )

    def test_off_without_a_secret(self):
        with self.assertRaises(ns.SmokeReportError) as caught:
            self.handle(secret="")
        self.assertEqual(caught.exception.status, 503)

    def test_a_wrong_token_or_another_repositorys_is_refused(self):
        for token in ("nope", ns.report_token("org/other-web", SECRET), ""):
            with self.assertRaises(ns.SmokeReportError) as caught:
                self.handle(token=token)
            self.assertEqual(caught.exception.status, 401)
        self.assertEqual(self.service.calls, [])

    def test_the_token_is_checked_before_the_report_is_validated(self):
        with self.assertRaises(ns.SmokeReportError) as caught:
            self.handle(report(run_url=""), token="nope")
        self.assertEqual(caught.exception.status, 401)

    def test_a_failure_starts_a_bug_program_at_intake_with_the_report(self):
        result = self.handle()
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result["program_id"], "prog-1")
        (call,) = self.service.calls
        self.assertEqual((call["program_type"], call["entry_node"]), ("bug_to_jira_work_orders", "bug_intake"))
        self.assertEqual(call["artifacts"], {"repo_url": "https://github.com/org/web"})
        self.assertEqual(call["binding_key"], "web:web:nightly-smoke-org-web-42")
        summary = call["summary"]
        for expected in (
            "org/web against org/api (dev)",
            'scenario "Every page opens after signing in"',
            'step "Then every page opens without errors"',
            "route /notes",
            "contract_violation: GET /notes 200 response",
            "Run: https://github.com/org/web/actions/runs/42",
            "Screenshots and traces: https://github.com/org/web/actions/runs/42/artifacts/7",
            f"web {'a' * 40}, API {'b' * 40}",
            "before anything is filed in Jira",
        ):
            self.assertIn(expected, summary)

    def test_the_same_failure_again_is_not_proposed_twice(self):
        self.handle()
        # Same scenario and route, different error text (more rows failed) the next night.
        again = report()
        again["failures"][0]["error"] = "GET /notes 200 response: /data/3 must have required property 'status'"
        result = self.handle(again, now=NOW + 3600)
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(len(self.service.calls), 1)

    def test_a_broken_night_is_one_proposal_and_only_new_failures_come_later(self):
        routes = [{"scenario": "Every page opens after signing in", "route": r, "error": "boom"} for r in ("/a", "/b", "/c")]
        self.assertEqual(self.handle(report(failures=routes))["new_failures"], 3)
        self.assertEqual(len(self.service.calls), 1)
        more = routes + [{"scenario": "Add a note", "route": "/notes", "error": "500"}]
        result = self.handle(report(failures=more), now=NOW + 3600)
        self.assertEqual((result["status"], result["new_failures"], result["known_failures"]), ("proposed", 1, 3))
        self.assertIn("Add a note", self.service.calls[1]["summary"])
        self.assertNotIn("route /a", self.service.calls[1]["summary"])

    def test_a_failure_whose_program_is_open_stays_known_after_a_day(self):
        self.handle()
        self.memory.statuses["prog-1"] = "running"
        self.assertEqual(self.handle(now=NOW + 3 * 86400)["status"], "duplicate")

    def test_a_failure_coming_back_after_its_program_ended_is_proposed_again(self):
        self.handle()
        self.memory.statuses["prog-1"] = "completed"
        result = self.handle(now=NOW + 3 * 86400)
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(len(self.service.calls), 2)

    def test_a_failed_start_releases_the_failures_for_the_next_report(self):
        self.service.fail = True
        with self.assertRaises(RuntimeError):
            self.handle()
        self.service.fail = False
        self.assertEqual(self.handle(now=NOW + 60)["status"], "proposed")

    def test_the_program_is_remembered_against_each_failure(self):
        self.handle()
        (state, _), = self.memory.kv.values()
        (entry,) = state["failures"].values()
        self.assertEqual((entry["program_id"], entry["route"]), ("prog-1", "/notes"))


class EndpointTests(unittest.TestCase):
    def setUp(self):
        from ts_harness.gateway import asgi_app

        self.runtime = mock.Mock()
        self.runtime.config.gateway.default_tenant_id = "default"
        self.handle = mock.Mock(return_value={"status": "proposed", "repo": "org/web", "program_id": "p1"})
        self.patches = [
            mock.patch.object(asgi_app, "get_runtime", return_value=self.runtime),
            mock.patch.object(ns, "handle_smoke_report", self.handle),
        ]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(asgi_app.app)

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    def post(self, body, token="tok"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return self.client.post("/webhooks/smoke", content=raw, headers={"Authorization": f"Bearer {token}"})

    def test_a_proposal_answers_202_and_is_audited(self):
        response = self.post(report())
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.handle.call_args.args[1], "tok")
        self.assertEqual(self.handle.call_args.kwargs["tenant_id"], "default")
        audit = self.runtime.audit.append.call_args.kwargs
        self.assertEqual((audit["action_type"], audit["payload"]["program_id"]), ("smoke_report", "p1"))

    def test_errors_keep_their_status(self):
        self.handle.side_effect = ns.SmokeReportError(401, "invalid smoke report token for this repository")
        self.assertEqual(self.post(report()).status_code, 401)
        self.handle.side_effect = RuntimeError("secret detail")
        response = self.post(report())
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("secret detail", response.text)

    def test_bad_bodies(self):
        self.assertEqual(self.post(b"{not json").status_code, 400)
        self.assertEqual(self.post(b"x" * (300 * 1024)).status_code, 413)
        self.handle.assert_not_called()


if __name__ == "__main__":
    unittest.main()
