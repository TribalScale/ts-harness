"""The harness-level loop: privacy sanitising, filing harness issues, self-fix PRs, limits and switches."""

from __future__ import annotations

import copy
import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.pg_support import PgTestCase, requires_pg

from ts_harness.improvement import harness_loop as hl
from ts_harness.improvement import sanitize as sz
from ts_harness.improvement import self_fix as sf
from ts_harness.improvement.off_limits import is_off_limits

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from memory_routes import harness_issues as routes  # noqa: E402

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
HARNESS = "org/ts-harness"
ENV = {"TS_HARNESS_SELF_REPO": HARNESS}

# Client-looking strings that must never reach the harness repository.
CLIENT_STRINGS = [
    "org/web", "org/api", "storefront-web", "orders-api", "PROJ-42", "PROJ-7", "proj-42", "Northwind",
    "Northwind Traders", "CheckoutBanner", "loyalty discount on every cart page", "jane@example.com",
    "src/components/CheckoutBanner.tsx", "const total = cart.items", "shopper", "PROJ", "WEBSHOP",
]

TRACEBACK = """Traceback (most recent call last):
  File "/app/runtime/ts_harness/workflows/engine.py", line 120, in decide_blocked_step
    self.client.patch(task_id, {"repo": "org/web"})
  File "/usr/lib/python3.12/site-packages/requests/api.py", line 59, in request
    return session.request(method=method, url=url)
KeyError: 'pr_url' while delivering PROJ-42 to https://github.com/org/storefront-web/pull/3 (src/components/CheckoutBanner.tsx)
"""

WORK_ORDER = (
    "# PROJ-42 Show the CheckoutBanner\n"
    "Northwind Traders wants the loyalty discount on every cart page so each shopper sees it.\n"
    "```ts\nconst total = cart.items.reduce((a, b) => a + b.price, 0);\n```\n"
)


def client_ctx() -> sz.ClientContext:
    ctx = sz.ClientContext(harness_repo=HARNESS)
    ctx.add_repo("https://github.com/org/storefront-web.git", "web")
    ctx.add_repo("org/orders-api", "api")
    ctx.add_names(["PROJ", "WEBSHOP", "Northwind Traders"])
    ctx.add_tickets(["PROJ-42"])
    ctx.texts.append(WORK_ORDER)
    return ctx


def assert_no_client_data(test: unittest.TestCase, text: str) -> None:
    lowered = text.lower()
    for needle in CLIENT_STRINGS:
        if needle in ("org/web", "org/api"):
            # Bare slugs: never as a repository reference.
            test.assertNotRegex(lowered, rf"(?<![\w-]){needle}(?![\w-])", msg=f"{needle!r} leaked")
        else:
            test.assertNotIn(needle.lower(), lowered, msg=f"{needle!r} leaked into: {text[:400]}")


# --- the sanitiser --------------------------------------------------------------------------------


class SanitiserTests(unittest.TestCase):
    def test_repository_slugs_and_urls_become_roles(self):
        ctx = client_ctx()
        out = sz.sanitize_text(
            "clone of org/storefront-web failed; see https://github.com/org/orders-api/pull/9 and "
            "git@github.com:org/storefront-web.git; storefront-web again; other https://github.com/x/y; "
            "https://grafana.example.com/d/abc", ctx)
        self.assertIn("<web repo>", out)
        self.assertIn("<api repo>", out)
        self.assertIn("<repo>", out)
        self.assertIn("<url>", out)
        self.assertNotIn("storefront", out)
        self.assertNotIn("orders-api", out)
        self.assertNotIn("grafana", out)

    def test_unknown_slugs_still_become_a_placeholder(self):
        out = sz.sanitize_text("cannot read org/web or acme/backend", sz.ClientContext(harness_repo=HARNESS))
        self.assertEqual(out, "cannot read <repo> or <repo>")

    def test_the_harness_repository_may_be_named(self):
        out = sz.sanitize_text("pushed to https://github.com/org/ts-harness/pull/12", client_ctx())
        self.assertIn("<harness repo>", out)

    def test_ticket_keys_any_shape(self):
        ctx = client_ctx()
        out = sz.sanitize_text("PROJ-42 and proj-42 on branch wo/proj-42-fix-ab12; also ABC-9 and PROJ-777", ctx)
        self.assertNotRegex(out, r"(?i)proj-\d|abc-9")
        self.assertIn("<ticket>", out)

    def test_code_and_diffs_are_dropped(self):
        out = sz.sanitize_text(
            "before\n```python\nsecret_logic = 1\n```\n+ added line\n- removed line\n@@ -1,2 +1,2 @@\n"
            "function render() {\n  return <Banner/>;\n}\nconst x = 1;\nafter", sz.ClientContext())
        self.assertIn("before", out)
        self.assertIn("after", out)
        self.assertIn("<code removed>", out)
        for needle in ("secret_logic", "added line", "removed line", "Banner", "render", "const x"):
            self.assertNotIn(needle, out)

    def test_tracebacks_keep_only_harness_frames(self):
        ctx = client_ctx()
        stack = sz.harness_stack(TRACEBACK, ctx)
        self.assertEqual(stack.splitlines()[0], "runtime/ts_harness/workflows/engine.py:120 in decide_blocked_step")
        self.assertIn("(frames outside the harness)", stack)
        self.assertTrue(stack.splitlines()[-1].startswith("KeyError: 'pr_url'"))
        self.assertNotIn("self.client.patch", stack)
        self.assertNotIn("site-packages", stack)
        assert_no_client_data(self, stack)
        self.assertEqual(sz.harness_paths(TRACEBACK), ["runtime/ts_harness/workflows/engine.py"])

    def test_paths_outside_the_harness_are_removed(self):
        out = sz.sanitize_text(
            "failed in src/app/page.tsx, /tmp/ws/123/repo/lib/x.py, runtime/ts_harness/workflows/engine.py:12, "
            "scripts/memory_routes/harness_issues.py, scripts/deploy-client.sh and runtime/ts_harness/not_a_module.py",
            sz.ClientContext())
        self.assertIn("runtime/ts_harness/workflows/engine.py", out)
        self.assertIn("scripts/memory_routes/harness_issues.py", out)
        for needle in ("page.tsx", "/tmp/ws", "lib/x.py", "deploy-client", "not_a_module"):
            self.assertNotIn(needle, out)

    def test_a_client_directory_named_like_the_package_is_not_kept(self):
        self.assertEqual(sz.harness_path("/work/repo/vendor/ts_harness/evil.py"), "")
        self.assertEqual(sz.harness_path("runtime/ts_harness/../../etc/passwd"), "")

    def test_quoted_text_goes_but_identifiers_stay(self):
        out = sz.sanitize_text("KeyError: 'pr_url'; expected \"Add the banner to checkout\" got `ok`", sz.ClientContext())
        self.assertIn("'pr_url'", out)
        self.assertIn('"<text>"', out)
        self.assertNotIn("banner", out.lower())

    def test_names_from_client_texts_are_replaced(self):
        out = sz.sanitize_text("the CheckoutBanner for WEBSHOP and Northwind failed", client_ctx())
        self.assertNotIn("CheckoutBanner", out)
        self.assertNotIn("Northwind", out)
        self.assertNotIn("WEBSHOP", out)

    def test_copied_client_text_is_removed(self):
        ctx = client_ctx()
        text = "The agent was asked to put the loyalty discount on every cart page and gave up\nKeyError in engine"
        self.assertTrue(sz.leaks(text, ctx))
        out = sz.safe_field(text, ctx)
        self.assertEqual(out, "<text removed>\nKeyError in engine")

    def test_a_field_with_client_data_left_is_withheld(self):
        ctx = client_ctx()
        with mock.patch.object(sz, "sanitize_text", return_value="still names org/orders-api"):
            self.assertEqual(sz.safe_field("anything", ctx), sz.WITHHELD)

    def test_emails_and_urls_go(self):
        out = sz.sanitize_text("contact jane@example.com or http://10.0.0.5:8080/admin", sz.ClientContext())
        self.assertEqual(out, "contact <email> or <url>")

    def test_a_placeholder_is_not_rewritten_by_a_later_rule(self):
        ctx = sz.ClientContext()
        ctx.add_repo("org/web", "web")
        out = sz.sanitize_text("org/web failed", ctx)
        self.assertEqual(out, "<web repo> failed")
        self.assertEqual(sz.leaks(out, ctx), [])

    def test_known_client_strings_never_survive(self):
        ctx = client_ctx()
        blob = TRACEBACK + "\n" + WORK_ORDER + "\nThe repo org/orders-api rejected jane@example.com for PROJ-7."
        out = sz.sanitize_text(blob, ctx)
        assert_no_client_data(self, out)
        assert_no_client_data(self, sz.safe_field(blob, ctx))
        assert_no_client_data(self, sz.harness_stack(blob, ctx))

    def test_assert_clean_refuses_client_data(self):
        with self.assertRaises(ValueError):
            sz.assert_clean("see org/storefront-web", client_ctx())
        sz.assert_clean("see https://github.com/org/ts-harness/issues/3", client_ctx())

    def test_length_is_capped(self):
        self.assertLessEqual(len(sz.sanitize_text("word " * 1000, sz.ClientContext(), limit=100)), 100)


# --- settings and fingerprints --------------------------------------------------------------------


class SettingsTests(unittest.TestCase):
    def test_unset_repo_turns_everything_off(self):
        settings = hl.loop_settings({}, None)
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.filing)
        self.assertFalse(settings.fixing)

    def test_defaults_with_a_repo(self):
        settings = hl.loop_settings(ENV, None)
        self.assertEqual((settings.repo, settings.branch, settings.filing, settings.fixing), (HARNESS, "dev", True, True))

    def test_switches(self):
        self.assertFalse(hl.loop_settings({**ENV, "TS_HARNESS_SELF_FIX": "off"}, None).fixing)
        self.assertTrue(hl.loop_settings({**ENV, "TS_HARNESS_SELF_FIX": "off"}, None).filing)
        no_filing = hl.loop_settings({**ENV, "TS_HARNESS_SELF_ISSUES": "false"}, None)
        self.assertFalse(no_filing.filing)
        self.assertFalse(no_filing.fixing)
        self.assertEqual(hl.loop_settings({**ENV, "TS_HARNESS_SELF_REPO_BRANCH": "develop"}, None).branch, "develop")

    def test_a_bad_repo_is_reported_and_off(self):
        settings = hl.loop_settings({"TS_HARNESS_SELF_REPO": "not a repo"}, None)
        self.assertFalse(settings.enabled)
        self.assertTrue(settings.problems)

    def test_runtime_config_section_and_env_wins(self):
        config = SimpleNamespace(program_defaults={"harness_loop": {"repo": "org/other", "self_fix": False, "branch": "main"}})
        settings = hl.loop_settings({}, config)
        self.assertEqual((settings.repo, settings.fixing, settings.branch), ("org/other", False, "main"))
        self.assertEqual(hl.loop_settings({**ENV, "TS_HARNESS_SELF_FIX": "on"}, config).repo, HARNESS)
        self.assertTrue(hl.loop_settings({**ENV, "TS_HARNESS_SELF_FIX": "on"}, config).fixing)

    def test_the_switches_and_limits_are_off_limits(self):
        for path in ("runtime/ts_harness/improvement/sanitize.py", "runtime/ts_harness/improvement/self_fix.py",
                     "runtime/ts_harness/improvement/harness_loop.py", "scripts/memory_routes/harness_issues.py"):
            self.assertIsNotNone(is_off_limits(path=path))
        self.assertIsNotNone(is_off_limits(setting="TS_HARNESS_SELF_FIX"))
        self.assertIsNotNone(is_off_limits(constant="ts_harness.improvement.self_fix:MAX_OPEN_FIXES"))


class FingerprintTests(unittest.TestCase):
    def test_same_class_same_fingerprint(self):
        a = hl.fingerprint("arbiter", "runtime/ts_harness/workflows/engine.py", "KeyError: 'pr_url' at 12 for 0f3c9a1b22")
        b = hl.fingerprint("arbiter", "runtime/ts_harness/workflows/engine.py", "KeyError: 'pr_url' at 97 for 77aa31c0de")
        c = hl.fingerprint("arbiter", "runtime/ts_harness/workflows/live_view.py", "KeyError: 'pr_url' at 12 for 0f3c9a1b22")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(a), 16)

    def test_classify(self):
        self.assertEqual(hl.classify("GitHub returned 403 Forbidden", ["runtime/x.py"]), "configuration")
        self.assertEqual(hl.classify("KeyError", ["runtime/x.py"]), "code")
        self.assertEqual(hl.classify("KeyError", []), "unknown")


class MergeOccurrenceTests(unittest.TestCase):
    BODY = {"fingerprint": "abcdef0123456789", "source": "arbiter", "kind": "code", "title": "t", "key": "k1",
            "ref": {"task_id": "t1"}}

    def test_one_key_counts_once(self):
        row, new = routes.merge_occurrence(None, self.BODY, "now")
        self.assertTrue(new)
        row2, new2 = routes.merge_occurrence(row, self.BODY, "later")
        self.assertFalse(new2)
        self.assertEqual(row2["occurrences"], 1)
        row3, new3 = routes.merge_occurrence(row, {**self.BODY, "key": "k2", "ref": {"task_id": "t2"}}, "later")
        self.assertTrue(new3)
        self.assertEqual((row3["occurrences"], len(row3["refs"]), row3["last_seen"]), (2, 2, "later"))

    def test_bad_bodies(self):
        for body in ({**self.BODY, "fingerprint": "x"}, {**self.BODY, "source": "jira"}, {**self.BODY, "key": ""},
                     {**self.BODY, "kind": "bogus"}, {**self.BODY, "title": ""}):
            with self.assertRaises(ValueError):
                routes.merge_occurrence(None, body, "now")


# --- fakes ----------------------------------------------------------------------------------------


class FakeMemory:
    def __init__(self, *, tasks=None, jobs=None, projects=True, workflows=None):
        self.tasks = list(tasks or [])
        self.jobs = list(jobs or [])
        self.rows: dict[str, dict] = {}
        self.kv: dict = {}
        self.projects = projects
        self.workflows = dict(workflows or {})
        self.now = NOW

    def list_projects(self, *, tenant_id="default", key=None):
        if not self.projects:
            raise RuntimeError("memory API down")
        return {"projects": [{"id": "p1", "key": "PROJ", "name": "Northwind Traders", "team_slug": "webshop-team"}]}

    def list_project_repositories(self, project_id):
        return {"repositories": [{"full_name": "org/storefront-web", "role": "web"},
                                 {"full_name": "org/orders-api", "role": "api"}]}

    def list_stuck_tasks(self, *, tenant_id="default"):
        return copy.deepcopy(self.tasks)

    def list_coding_jobs(self, *, workflow_id=None, task_id=None, status=None, finished_within=None, limit=50, **_):
        jobs = self.jobs
        if task_id:
            jobs = [j for j in jobs if j.get("task_id") == task_id]
        if workflow_id:
            jobs = [j for j in jobs if j.get("workflow_id") == workflow_id]
        if status:
            jobs = [j for j in jobs if j.get("status") == status]
        return copy.deepcopy(jobs[:limit])

    def record_harness_occurrence(self, **body):
        existing = self.rows.get(body["fingerprint"])
        row, new = routes.merge_occurrence(existing, body, self.now.isoformat())
        if existing is None:
            row.update({"id": f"id-{len(self.rows) + 1}", "reported_occurrences": 0, "fix_status": "none",
                        "fix_reason": "", "fix_charged_usd": 0.0, "issue_number": None, "issue_state": None,
                        "last_reported_at": None, "fix_started_at": None, "fix_workflow_id": None, "fix_pr_url": None,
                        "fix_pr_head": None, "last_error": ""})
        self.rows[body["fingerprint"]] = row
        return {"issue": copy.deepcopy(row), "new_occurrence": new, "created": existing is None}

    def list_harness_issues(self, *, tenant_id="default", limit=50):
        rows = [copy.deepcopy(r) for r in self.rows.values()]
        week = self.now - timedelta(days=7)
        return {
            "issues": rows,
            "open_fixes": sum(1 for r in rows if r["fix_status"] in ("started", "pr_open")),
            "fixes_last_7_days": sum(1 for r in rows if r.get("fix_started_at") and datetime.fromisoformat(r["fix_started_at"]) >= week),
        }

    def update_harness_issue(self, issue_id, **body):
        row = next(r for r in self.rows.values() if r["id"] == issue_id)
        if body.pop("fix_started", False):
            if row.get("fix_started_at"):
                raise ValueError("a self-fix was already started for this issue")
            row["fix_started_at"] = self.now.isoformat()
        if body.pop("reported", False):
            row["last_reported_at"] = self.now.isoformat()
        if "charge_usd" in body:
            row["fix_charged_usd"] = round(float(row.get("fix_charged_usd") or 0) + float(body.pop("charge_usd")), 4)
        if "fix_status" in body:
            assert body["fix_status"] in routes.FIX_STATUSES
        row.update(body)
        return {"issue": copy.deepcopy(row)}

    def kv_get(self, namespace, key):
        return {"value": copy.deepcopy(self.kv.get((namespace, key))), "version": 1}

    def kv_put(self, namespace, key, value, *, expected_version=None):
        self.kv[(namespace, key)] = copy.deepcopy(value)
        return 1

    def get_workflow(self, workflow_id):
        return copy.deepcopy(self.workflows.get(workflow_id) or {})


class FakeGitHub:
    def __init__(self, *, existing=None, fail_create=False, pulls=None, pull_files=None):
        self.calls: list[tuple] = []
        self.existing = list(existing or [])
        self.fail_create = fail_create
        self.pulls = dict(pulls or {})
        self.pull_files = dict(pull_files or {})
        self.next_number = 100

    def request(self, method, path, *, body=None, query=None, allow_404=False):
        self.calls.append((method, path, copy.deepcopy(body), copy.deepcopy(query)))
        if method == "GET" and path.endswith("/issues"):
            return copy.deepcopy(self.existing)
        if method == "POST" and path.endswith("/issues"):
            if self.fail_create:
                from ts_harness.integrations.github_client import GitHubError

                raise GitHubError(403, "Resource not accessible by integration")
            self.next_number += 1
            issue = {"number": self.next_number, "html_url": f"https://github.com/{HARNESS}/issues/{self.next_number}",
                     "state": "open", "body": body["body"], "title": body["title"]}
            self.existing.append(issue)
            return issue
        return {}

    def comment(self, owner, repo, number, body):
        self.calls.append(("COMMENT", f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body}, None))
        return {}

    def get_pull(self, owner, repo, number):
        self.calls.append(("GET", f"/repos/{owner}/{repo}/pulls/{number}", None, None))
        return copy.deepcopy(self.pulls.get(number) or {"state": "open", "draft": False, "head": {"sha": "abc1234"}})

    def list_pull_files(self, owner, repo, number):
        return [{"filename": f} for f in self.pull_files.get(number, ["runtime/ts_harness/workflows/engine.py"])]

    def sent_text(self) -> str:
        return json.dumps([c for c in self.calls if c[0] != "GET"])

    def created(self) -> list[dict]:
        return [c[2] for c in self.calls if c[0] == "POST" and c[1].endswith("/issues")]

    def comments(self) -> list[str]:
        return [c[2]["body"] for c in self.calls if c[0] == "COMMENT"]


class FakeBudget:
    def __init__(self, remaining=150.0):
        self.remaining = remaining
        self.charges: list[tuple] = []

    def can_spend(self, estimate=0.0):
        return self.remaining > 0 and estimate <= self.remaining

    def charge(self, usd, purpose, ref="", **_):
        self.charges.append((round(usd, 4), purpose, ref))
        self.remaining -= usd


def harness_task(task_id="t1", since="2026-03-02T10:00:00+00:00", error=TRACEBACK, category="harness_issue", **extra):
    return {
        "id": task_id, "workflow_id": f"wf-{task_id}", "workflow_type": "implement_jira_ticket",
        "step_name": "deliver_work_order", "error": error, "updated_at": since,
        "input": {
            "repo_url": "https://github.com/org/storefront-web", "work_order_id": "PROJ-42",
            "work_order_body": WORK_ORDER, "summary": "Show the CheckoutBanner for Northwind",
            "_arbiter": {"category": category, "label": "Harness issue" if category == "harness_issue" else "Arbiter handed off",
                         "for_since": since, "at": since,
                         "reason": "The harness crashed reading pr_url while delivering PROJ-42 for Northwind Traders; "
                                   "the loyalty discount on every cart page never shipped."},
            **extra,
        },
    }


def config(**defaults):
    return SimpleNamespace(program_defaults=defaults, tenants=[SimpleNamespace(display_name="Northwind Traders")])


def sweep(memory, github, *, env=None, budget=None, forks=None, drafts=None, cfg=None, now=NOW, token="tok"):
    forks = forks if forks is not None else []
    drafts = drafts if drafts is not None else []

    def fork(memory, *, tenant_id, intake, actor):
        forks.append(intake)
        return f"fix-wf-{len(forks)}"

    def make_draft(url):
        drafts.append(url)
        return True

    memory.now = now
    with mock.patch("ts_harness.integrations.github_client.repo_token", return_value=token):
        return hl.sweep(memory=memory, github=github, config=cfg if cfg is not None else config(), env=env or ENV, now=now,
                        budget=budget or FakeBudget(), fork=fork, make_draft=make_draft,
                        http_ok=lambda url: (True, "HTTP 200"), alert_settings=SimpleNamespace(lost_jobs=2))


# --- filing ---------------------------------------------------------------------------------------


class FilingTests(unittest.TestCase):
    def test_off_without_a_repo(self):
        memory, github = FakeMemory(tasks=[harness_task()]), FakeGitHub()
        out = sweep(memory, github, env={"TS_HARNESS_SELF_FIX": "on"})
        self.assertEqual(out["status"], "off")
        self.assertEqual(github.calls, [])
        self.assertEqual(memory.rows, {})

    def test_files_one_sanitised_issue_for_an_arbiter_harness_issue(self):
        memory, github = FakeMemory(tasks=[harness_task()]), FakeGitHub()
        out = sweep(memory, github, env={**ENV, "TS_HARNESS_SELF_FIX": "off"})
        self.assertEqual(out["status"], "ok")
        self.assertEqual(len(github.created()), 1)
        issue = github.created()[0]
        self.assertTrue(issue["title"].startswith("[harness] runtime/ts_harness/workflows/engine.py: KeyError"))
        self.assertIn("harness-bug", issue["labels"])
        body = issue["body"]
        for section in ("## What happened", "## Component", "## Diagnosis", "## Harness stack", "## How often",
                        "## Internal references", "wf-t1", "deliver_work_order", hl.FINGERPRINT_MARKER):
            self.assertIn(section, body)
        self.assertIn("<web repo>", body)
        assert_no_client_data(self, issue["title"])
        assert_no_client_data(self, body)
        row = next(iter(memory.rows.values()))
        self.assertEqual((row["issue_number"], row["kind"], row["source"]), (101, "code", "arbiter"))
        self.assertEqual(row["files"], ["runtime/ts_harness/workflows/engine.py"])

    def test_nothing_client_looking_reaches_github_or_storage(self):
        task = harness_task(error=TRACEBACK + "\nalso failed for org/orders-api: jane@example.com PROJ-7 WEBSHOP")
        memory = FakeMemory(tasks=[task], jobs=[{"id": "job-1", "task_id": "t1", "error": "OpenCode: CheckoutBanner render broke in src/components/CheckoutBanner.tsx"}])
        github = FakeGitHub()
        forks: list[dict] = []
        sweep(memory, github, forks=forks)
        sweep(memory, github, forks=forks, now=NOW + timedelta(hours=7))
        assert_no_client_data(self, github.sent_text())
        assert_no_client_data(self, json.dumps(list(memory.rows.values()), default=str))
        assert_no_client_data(self, json.dumps(forks))

    def test_the_same_failure_is_not_filed_twice_and_a_new_stop_comments_the_count(self):
        memory, github = FakeMemory(tasks=[harness_task()]), FakeGitHub()
        off = {**ENV, "TS_HARNESS_SELF_FIX": "off"}
        sweep(memory, github, env=off)
        sweep(memory, github, env=off)
        self.assertEqual(len(github.created()), 1)
        self.assertEqual(github.comments(), [])
        memory.tasks = [harness_task(task_id="t2", since="2026-03-02T11:00:00+00:00")]
        sweep(memory, github, env=off, now=NOW + timedelta(hours=1))
        self.assertEqual(github.comments(), [], "comments wait for the interval")
        sweep(memory, github, env=off, now=NOW + timedelta(hours=7))
        self.assertEqual(len(github.created()), 1)
        self.assertEqual(len(github.comments()), 1)
        self.assertIn("2 occurrence(s) in total (was 1)", github.comments()[0])
        assert_no_client_data(self, github.comments()[0])

    def test_an_issue_already_on_github_is_reused(self):
        memory = FakeMemory(tasks=[harness_task()])
        first = FakeGitHub()
        sweep(memory, first, env={**ENV, "TS_HARNESS_SELF_FIX": "off"})
        lost = FakeMemory(tasks=[harness_task()])  # the ledger was lost; GitHub still has the issue
        again = FakeGitHub(existing=first.existing)
        sweep(lost, again, env={**ENV, "TS_HARNESS_SELF_FIX": "off"})
        self.assertEqual(again.created(), [])
        self.assertEqual(next(iter(lost.rows.values()))["issue_number"], 101)

    def test_other_arbiter_categories_are_ignored(self):
        memory, github = FakeMemory(tasks=[harness_task(category="agent_off_track")]), FakeGitHub()
        sweep(memory, github)
        self.assertEqual(memory.rows, {})
        self.assertEqual(github.created(), [])

    def test_without_client_identifiers_nothing_is_filed(self):
        memory, github = FakeMemory(tasks=[harness_task()], projects=False), FakeGitHub()
        out = sweep(memory, github)
        self.assertEqual(out["status"], "skipped")
        self.assertEqual(github.calls, [])
        self.assertIn("nothing filed", memory.kv[(hl.KV_NAMESPACE, hl.STATUS_KEY)]["problems"][0])

    def test_a_github_refusal_is_recorded_not_raised(self):
        memory, github = FakeMemory(tasks=[harness_task()]), FakeGitHub(fail_create=True)
        out = sweep(memory, github)
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["errors"])
        self.assertIn("GitHubError", next(iter(memory.rows.values()))["last_error"])
        self.assertTrue(any("issues: read and write" in p for p in memory.kv[(hl.KV_NAMESPACE, hl.STATUS_KEY)]["problems"]))

    def test_new_issues_per_run_are_bounded(self):
        modules = ("engine", "live_view", "ticket_story", "delivery_metrics", "work_order_waves")
        tasks = [harness_task(task_id=f"t{i}", error=f"Traceback (most recent call last):\n"
                              f'  File "/app/runtime/ts_harness/workflows/{name}.py", line {i}, in f\n'
                              "ValueError: boom") for i, name in enumerate(modules)]
        memory, github = FakeMemory(tasks=tasks), FakeGitHub()
        sweep(memory, github, env={**ENV, "TS_HARNESS_SELF_FIX": "off"})
        self.assertEqual(len(github.created()), hl.MAX_NEW_ISSUES_PER_SWEEP)


class AlertTests(unittest.TestCase):
    def test_lost_jobs_at_the_threshold_file_an_operations_issue(self):
        jobs = [{"id": f"job-{i}", "status": "lost", "work_order_id": "PROJ-42", "workflow_id": "wf-1"} for i in range(2)]
        memory, github = FakeMemory(jobs=jobs), FakeGitHub()
        sweep(memory, github)
        row = next(iter(memory.rows.values()))
        self.assertEqual((row["source"], row["kind"], row["occurrences"]), ("ops_alert", "operations", 2))
        assert_no_client_data(self, github.sent_text())
        # Operations problems are not self-fixed: the issue asks a person.
        self.assertEqual(row["fix_status"], "not_fixable")
        self.assertIn(("POST", f"/repos/{HARNESS}/issues/101/labels", {"labels": ["needs-human"]}, None), github.calls)

    def test_below_the_threshold_nothing_is_filed(self):
        memory, github = FakeMemory(jobs=[{"id": "job-1", "status": "lost"}]), FakeGitHub()
        sweep(memory, github)
        self.assertEqual(memory.rows, {})

    def test_the_llm_proxy_failing_its_health_check(self):
        memory = FakeMemory()
        with mock.patch.dict(os.environ, {"LLM_PROXY_URL": "http://proxy:4000"}):
            found = hl.alert_occurrences(memory, NOW, settings=SimpleNamespace(lost_jobs=0),
                                         http_ok=lambda url: (False, "connection refused"))
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].source, found[0].kind, found[0].component),
                         ("ops_alert", "operations", "runtime/ts_harness/llm_proxy/"))


# --- self-fixes -----------------------------------------------------------------------------------


class SelfFixTests(unittest.TestCase):
    def test_a_fixable_issue_starts_one_delivery_against_the_harness_repo(self):
        memory, github, forks = FakeMemory(tasks=[harness_task()]), FakeGitHub(), []
        out = sweep(memory, github, forks=forks)
        self.assertEqual(len(forks), 1)
        intake = forks[0]
        self.assertEqual(intake["repo_url"], f"https://github.com/{HARNESS}")
        self.assertEqual(intake["base_branch"], "dev")
        self.assertEqual(intake["work_order_id"], "HSF-101")
        body = intake["work_order_body"]
        self.assertIn("regression test", body)
        self.assertIn("`runtime/ts_harness/workflows/engine.py`", body)
        self.assertIn("off-limits", body)
        assert_no_client_data(self, json.dumps(intake))
        row = next(iter(memory.rows.values()))
        self.assertEqual((row["fix_status"], row["fix_workflow_id"]), ("started", "fix-wf-1"))
        self.assertEqual(out["self_fix"]["started"], [{"issue_number": 101, "workflow_id": "fix-wf-1"}])
        # Runs again: never a second fix for the same issue.
        sweep(memory, github, forks=forks, now=NOW + timedelta(hours=1))
        self.assertEqual(len(forks), 1)

    def test_the_branch_is_configurable(self):
        memory, forks = FakeMemory(tasks=[harness_task()]), []
        sweep(memory, FakeGitHub(), forks=forks, env={**ENV, "TS_HARNESS_SELF_REPO_BRANCH": "develop"})
        self.assertEqual(forks[0]["base_branch"], "develop")

    def test_self_fix_switch_off_still_files(self):
        memory, github, forks = FakeMemory(tasks=[harness_task()]), FakeGitHub(), []
        sweep(memory, github, forks=forks, env={**ENV, "TS_HARNESS_SELF_FIX": "off"})
        self.assertEqual(forks, [])
        self.assertEqual(len(github.created()), 1)

    def test_an_off_limits_file_is_labelled_needs_human(self):
        error = ('Traceback (most recent call last):\n  File "/app/runtime/ts_harness/workflows/code_review.py", '
                 'line 10, in is_blocking\nTypeError: bad finding')
        memory, github, forks = FakeMemory(tasks=[harness_task(error=error)]), FakeGitHub(), []
        sweep(memory, github, forks=forks)
        self.assertEqual(forks, [])
        row = next(iter(memory.rows.values()))
        self.assertEqual(row["fix_status"], "needs_human")
        self.assertIn("off limits", row["fix_reason"])
        self.assertIn(("POST", f"/repos/{HARNESS}/issues/101/labels", {"labels": ["needs-human"]}, None), github.calls)
        self.assertTrue(any("No self-fix" in c and "code_review.py" in c for c in github.comments()))

    def test_configuration_problems_are_not_self_fixed(self):
        error = ('Traceback (most recent call last):\n  File "/app/runtime/ts_harness/workflows/engine.py", line 1, in f\n'
                 'GitHubError: 403 Resource not accessible by integration')
        memory, forks = FakeMemory(tasks=[harness_task(error=error)]), []
        sweep(memory, FakeGitHub(), forks=forks)
        self.assertEqual(forks, [])
        self.assertEqual(next(iter(memory.rows.values()))["fix_status"], "not_fixable")

    def test_rate_limits(self):
        def row(i, status="none", started=None, issue=True):
            return {"id": f"id-{i}", "fingerprint": f"{i:016d}", "kind": "code", "component": "runtime/ts_harness/workflows/engine.py",
                    "files": ["runtime/ts_harness/workflows/engine.py"], "title": "t", "issue_number": 10 + i if issue else None,
                    "issue_state": "open", "fix_status": status, "fix_started_at": started, "fix_charged_usd": 0,
                    "fix_workflow_id": f"wf-{i}" if started else None, "occurrences": 1}

        settings = hl.loop_settings(ENV, None)

        def run(rows, listing):
            memory = FakeMemory()
            for r in rows:
                memory.rows[r["fingerprint"]] = r
            forks = []
            out = sf.run(memory, FakeGitHub(), settings, sz.ClientContext(), tenant_id="default", rows=rows, listing=listing,
                         now=NOW, config=config(), budget=FakeBudget(), token=lambda url: "tok",
                         fork=lambda m, **kw: forks.append(kw) or "wf-new", make_draft=lambda url: True)
            return out, forks

        recent = (NOW - timedelta(days=1)).isoformat()
        out, forks = run([row(1, "pr_open", recent), row(2, "started", recent), row(3)], {"open_fixes": 2, "fixes_last_7_days": 2})
        self.assertEqual(forks, [])
        self.assertIn("limit 2", out["skipped"]["13"])
        out, forks = run([row(1, "merged", recent), row(2, "closed", recent), row(4, "failed", recent), row(3)],
                         {"open_fixes": 0, "fixes_last_7_days": 3})
        self.assertEqual(forks, [])
        self.assertIn("last 7 days", out["skipped"]["13"])
        out, forks = run([row(3), row(5), row(6)], {"open_fixes": 0, "fixes_last_7_days": 0})
        self.assertEqual(len(forks), 1, "one fix starts per run")
        self.assertEqual(out["skipped"]["15"], "one self-fix starts per run")

    def test_a_used_up_budget_pauses_quietly(self):
        memory, forks = FakeMemory(tasks=[harness_task()]), []
        out = sweep(memory, FakeGitHub(), forks=forks, budget=FakeBudget(remaining=0))
        self.assertEqual(forks, [])
        self.assertEqual(out["self_fix"]["status"], "paused")
        self.assertEqual(next(iter(memory.rows.values()))["fix_status"], "none", "tried again next month")

    def test_an_undeliverable_harness_repo_says_what_to_configure(self):
        memory, forks = FakeMemory(tasks=[harness_task()]), []
        cfg = config(repo_picker={"repos": [{"id": "org/storefront-web"}]})
        out = sweep(memory, FakeGitHub(), forks=forks, cfg=cfg, token="")
        self.assertEqual(forks, [])
        problems = " ".join(memory.kv[(hl.KV_NAMESPACE, hl.STATUS_KEY)]["problems"])
        self.assertIn("repo allowlist", problems)
        self.assertIn("System → Projects", problems)
        self.assertIn("no GitHub credentials", problems)
        self.assertEqual(out["status"], "ok")
        self.assertEqual(next(iter(memory.rows.values()))["fix_status"], "none")

    def test_a_failed_start_is_never_retried(self):
        memory, github = FakeMemory(tasks=[harness_task()]), FakeGitHub()
        settings = hl.loop_settings(ENV, None)
        sweep(memory, github, env={**ENV, "TS_HARNESS_SELF_FIX": "off"})
        rows = memory.list_harness_issues()["issues"]

        def broken(memory, **kw):
            raise RuntimeError("no skeleton")

        sf.run(memory, github, settings, sz.ClientContext(), tenant_id="default", rows=rows, listing={}, now=NOW,
               config=config(), budget=FakeBudget(), token=lambda url: "tok", fork=broken, make_draft=lambda u: True)
        row = next(iter(memory.rows.values()))
        self.assertEqual(row["fix_status"], "failed")
        forks = []
        sweep(memory, github, forks=forks)
        self.assertEqual(forks, [])


class TrackingTests(unittest.TestCase):
    PR = f"https://github.com/{HARNESS}/pull/7"

    def started(self, *, workflow=None, pulls=None, pull_files=None, jobs=None):
        memory = FakeMemory(tasks=[harness_task()], jobs=jobs)
        github = FakeGitHub(pulls=pulls, pull_files=pull_files)
        sweep(memory, github)
        memory.workflows["fix-wf-1"] = workflow or {
            "workflow": {"status": "running"},
            "tasks": [{"step_name": "deliver_work_order", "output": {"pr_url": self.PR}, "updated_at": "2026-03-02T13:00"}],
        }
        return memory, github

    def test_the_pr_is_drafted_labelled_and_linked(self):
        memory, github = self.started()
        drafts: list[str] = []
        sweep(memory, github, drafts=drafts, now=NOW + timedelta(hours=1))
        row = next(iter(memory.rows.values()))
        self.assertEqual((row["fix_status"], row["fix_pr_url"]), ("pr_open", self.PR))
        self.assertEqual(drafts, [self.PR])
        self.assertIn(("POST", f"/repos/{HARNESS}/issues/7/labels", {"labels": ["harness-self-fix"]}, None), github.calls)
        self.assertTrue(any("never merged automatically" in c for c in github.comments()))
        self.assertTrue(any(f"Self-fix PR: {self.PR}" in c for c in github.comments()))
        # The same head is not drafted again (a person may have marked it ready).
        sweep(memory, github, drafts=drafts, now=NOW + timedelta(hours=2))
        self.assertEqual(drafts, [self.PR])

    def test_a_new_head_is_drafted_again(self):
        memory, github = self.started()
        drafts: list[str] = []
        sweep(memory, github, drafts=drafts, now=NOW + timedelta(hours=1))
        github.pulls[7] = {"state": "open", "draft": False, "head": {"sha": "def5678"}}
        sweep(memory, github, drafts=drafts, now=NOW + timedelta(hours=2))
        self.assertEqual(len(drafts), 2)

    def test_merged_and_closed(self):
        memory, github = self.started(pulls={7: {"state": "closed", "merged": True, "head": {"sha": "a"}}})
        sweep(memory, github, now=NOW + timedelta(hours=1))
        self.assertEqual(next(iter(memory.rows.values()))["fix_status"], "merged")
        memory, github = self.started(pulls={7: {"state": "closed", "merged": False, "head": {"sha": "a"}}})
        sweep(memory, github, now=NOW + timedelta(hours=1))
        self.assertEqual(next(iter(memory.rows.values()))["fix_status"], "closed")

    def test_a_pr_touching_an_off_limits_file_is_flagged(self):
        memory, github = self.started(pull_files={7: ["runtime/ts_harness/workflows/engine.py",
                                                      "runtime/ts_harness/improvement/budget.py"]})
        sweep(memory, github, now=NOW + timedelta(hours=1))
        self.assertIn(("POST", f"/repos/{HARNESS}/issues/7/labels", {"labels": ["needs-human"]}, None), github.calls)
        self.assertTrue(any("Do not merge" in c for c in github.comments()))
        self.assertIn("off limits", next(iter(memory.rows.values()))["fix_reason"])

    def test_a_delivery_that_ends_without_a_pr_fails(self):
        memory, github = self.started(workflow={"workflow": {"status": "failed"}, "tasks": []})
        sweep(memory, github, now=NOW + timedelta(hours=1))
        self.assertEqual(next(iter(memory.rows.values()))["fix_status"], "failed")

    def test_coding_cost_is_charged_to_the_improvement_budget_once(self):
        jobs = [{"id": "j1", "workflow_id": "fix-wf-1", "result": {"usage": [{"cost_usd": 1.5}, {"cost_usd": 0.25}]}}]
        memory, github = self.started(jobs=jobs)
        budget = FakeBudget()
        sweep(memory, github, budget=budget, now=NOW + timedelta(hours=1))
        sweep(memory, github, budget=budget, now=NOW + timedelta(hours=2))
        self.assertEqual(budget.charges, [(1.75, "harness_self_fix", next(iter(memory.rows))) ])
        memory.jobs.append({"id": "j2", "workflow_id": "fix-wf-1", "result": {"usage": [{"cost_usd": 0.5}]}})
        sweep(memory, github, budget=budget, now=NOW + timedelta(hours=3))
        self.assertEqual(budget.charges[-1][0], 0.5)

    def test_nothing_ever_merges(self):
        memory, github = self.started()
        sweep(memory, github, now=NOW + timedelta(hours=1))
        github.pulls[7] = {"state": "open", "draft": True, "head": {"sha": "zzz"}}
        sweep(memory, github, now=NOW + timedelta(hours=2))
        for method, path, body, _ in github.calls:
            self.assertFalse(path.endswith("/merge"), path)
            self.assertNotIn("auto_merge", json.dumps(body or {}))
            self.assertFalse(method == "PUT")


class StatusTests(unittest.TestCase):
    def test_status_lists_issues_and_limits(self):
        memory, github = FakeMemory(tasks=[harness_task()]), FakeGitHub()
        sweep(memory, github)
        out = hl.loop_status(memory, env=ENV, config=config())
        self.assertTrue(out["settings"]["self_fix"])
        self.assertEqual(out["issues"][0]["issue_url"], f"https://github.com/{HARNESS}/issues/101")
        self.assertEqual(out["issues"][0]["fix_status"], "started")
        self.assertEqual(out["limits"]["max_open"], 2)
        self.assertEqual(out["limits"]["max_per_week"], 3)
        self.assertNotIn("seen_keys", out["issues"][0])

    def test_status_when_off(self):
        out = hl.loop_status(FakeMemory(), env={}, config=config())
        self.assertFalse(out["settings"]["file_issues"])
        self.assertEqual(out["issues"], [])


class SystemJobTests(unittest.TestCase):
    def test_registered_everywhere(self):
        from ts_harness.gateway import system_jobs

        self.assertIn("harness_self_issues", system_jobs.SYSTEM_HANDLERS)
        jobs = json.loads((REPO_ROOT / "config" / "runtime-cron-jobs.json").read_text())["jobs"]
        self.assertIn("harness_self_issues", [j.get("handler") for j in jobs])
        self.assertIn('"handler": "harness_self_issues"', (REPO_ROOT / "scripts/phases/15-automation-cron.sh").read_text())

    def test_the_job_never_raises(self):
        from ts_harness.gateway import system_jobs

        with mock.patch.object(hl, "sweep", side_effect=RuntimeError("boom")):
            self.assertEqual(system_jobs.harness_self_issues()["status"], "error")

    def test_the_env_reaches_the_containers(self):
        script = (REPO_ROOT / "scripts/phases/lib/common.sh").read_text()
        for key in ("TS_HARNESS_SELF_REPO", "TS_HARNESS_SELF_REPO_BRANCH", "TS_HARNESS_SELF_ISSUES", "TS_HARNESS_SELF_FIX"):
            # Once in the container env file, once in the persisted instance keys.
            self.assertEqual(len(re.findall(rf"^\s+{key}(?: \\)?$", script, re.M)), 2, key)

    def test_the_migration_is_in_the_order(self):
        from tests.pg_support import migration_order

        self.assertIn("057_harness_self_issues.sql", migration_order())


@requires_pg
class HarnessIssuesPgTests(PgTestCase):
    BODY = {"fingerprint": "abcdef0123456789", "source": "arbiter", "kind": "code", "title": "[harness] x",
            "component": "runtime/ts_harness/workflows/engine.py", "files": ["runtime/ts_harness/workflows/engine.py"],
            "key": "k1", "ref": {"task_id": "t1"}}

    def setUp(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM harness_self_issues")
            conn.commit()

    def test_occurrences_are_counted_once_per_key(self):
        with self.db.connect() as conn:
            first = routes.record_occurrence(conn, self.BODY)
            again = routes.record_occurrence(conn, self.BODY)
            other = routes.record_occurrence(conn, {**self.BODY, "key": "k2"})
            self.assertTrue(first["created"])
            self.assertFalse(again["new_occurrence"])
            self.assertEqual(other["issue"]["occurrences"], 2)
            listed = routes.list_issues(conn, {})
            self.assertEqual(len(listed["issues"]), 1)
            self.assertEqual(listed["issues"][0]["files"], ["runtime/ts_harness/workflows/engine.py"])

    def test_one_self_fix_per_issue_and_the_counts(self):
        with self.db.connect() as conn:
            issue = routes.record_occurrence(conn, self.BODY)["issue"]
            routes.update_issue(conn, issue["id"], {"issue_number": 5, "issue_url": "https://github.com/org/ts-harness/issues/5",
                                                    "reported": True, "reported_occurrences": 1})
            started = routes.update_issue(conn, issue["id"], {"fix_status": "started", "fix_started": True})
            self.assertIsNotNone(started["issue"]["fix_started_at"])
            with self.assertRaises(ValueError):
                routes.update_issue(conn, issue["id"], {"fix_status": "started", "fix_started": True})
            routes.update_issue(conn, issue["id"], {"charge_usd": 1.25})
            out = routes.update_issue(conn, issue["id"], {"charge_usd": 0.5})
            self.assertEqual(out["issue"]["fix_charged_usd"], 1.75)
            listed = routes.list_issues(conn, {})
            self.assertEqual((listed["open_fixes"], listed["fixes_last_7_days"]), (1, 1))
            with self.assertRaises(ValueError):
                routes.update_issue(conn, issue["id"], {"fix_status": "merged-by-bot"})
            with self.assertRaises(LookupError):
                routes.update_issue(conn, "not-a-uuid", {"fix_status": "closed"})


if __name__ == "__main__":
    unittest.main()
