"""Backend production gaps: test databases in the job, integration suites, coverage, Node pins,
and reusing review/QA verdicts when only the base branch moved."""

from __future__ import annotations

import io
import json
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from ts_harness.implementation import coverage as cov
from ts_harness.implementation import node_version as nv
from ts_harness.implementation import test_services as ts
from ts_harness.implementation.test_env import TestEnvironment
from ts_harness.implementation.verification import verification_evidence, verification_summary, verify_implementation
from ts_harness.workflows import carry_forward
from ts_harness.workflows.code_review import _file_identity, diff_fingerprint


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _ok(command, **_kwargs):
    return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")


# --- carry forward ---------------------------------------------------------------------------------


def _task(tid, step, status, created, deps=(), output=None, **extra):
    return {"id": tid, "step_name": step, "status": status, "created_at": created, "depends_on": list(deps), "output": output or {}, **extra}


def _precheck(tid, created, fingerprint, head="h1"):
    return _task(tid, "review_precheck", "succeeded", created, output={"diff_fingerprint": fingerprint, "prs": [{"pr_url": PR, "head_sha": head}]})


PR = "https://github.com/acme/api/pull/7"
REVIEW_OUT = {"result": {"verdict": "pass", "summary": "fine", "prs": [{"pr_url": PR, "head_sha": "h1"}], "findings": []}}
QA_OUT = {"status": "success", "result": {"prs": [{"pr_url": PR, "work_item_ref": "WO-1", "status": "success"}]}}


def _first_round(fingerprint="fp1"):
    return [
        _precheck("p1", "01", fingerprint),
        _task("r1", "code_review", "succeeded", "02", ["p1"], REVIEW_OUT),
        _task("f1", "finalize_code_review", "succeeded", "03", ["r1"]),
        _task("q1", "qa_gate", "succeeded", "04", ["f1"], QA_OUT),
    ]


class CarryForwardTests(unittest.TestCase):
    def test_same_diff_reuses_the_passing_review_with_the_new_head(self):
        tasks = [*_first_round(), _precheck("p2", "05", "fp1", head="h2")]
        review = _task("r2", "code_review", "ready", "06", ["p2"])
        out = carry_forward.carried_output(review, {}, [*tasks, review])
        self.assertEqual(out["carried_forward"]["from_task_id"], "r1")
        self.assertEqual(out["result"]["prs"][0]["head_sha"], "h2")

    def test_qa_walks_back_to_its_precheck(self):
        tasks = [*_first_round(), _precheck("p2", "05", "fp1", head="h2"),
                 _task("r2", "code_review", "succeeded", "06", ["p2"], REVIEW_OUT),
                 _task("f2", "finalize_code_review", "succeeded", "07", ["r2"])]
        qa = _task("q2", "qa_gate", "ready", "08", ["f2"])
        out = carry_forward.carried_output(qa, {}, [*tasks, qa])
        self.assertEqual(out["carried_forward"]["from_task_id"], "q1")
        self.assertEqual(out["status"], "success")

    def test_changed_diff_runs_the_agent(self):
        tasks = [*_first_round(), _precheck("p2", "05", "fp2")]
        review = _task("r2", "code_review", "ready", "06", ["p2"])
        self.assertIsNone(carry_forward.carried_output(review, {}, [*tasks, review]))

    def test_a_review_that_blocked_is_not_reused(self):
        tasks = [_precheck("p1", "01", "fp1"), _task("r1", "code_review", "succeeded", "02", ["p1"], REVIEW_OUT),
                 _task("f1", "finalize_code_review", "blocked", "03", ["r1"]), _precheck("p2", "05", "fp1")]
        review = _task("r2", "code_review", "ready", "06", ["p2"])
        self.assertIsNone(carry_forward.carried_output(review, {}, [*tasks, review]))

    def test_latest_qa_failure_wins_over_an_older_pass(self):
        tasks = [*_first_round(), _task("q1b", "qa_gate", "blocked", "045", ["f1"], {"status": "failed"}),
                 _precheck("p2", "05", "fp1"), _task("r2", "code_review", "succeeded", "06", ["p2"], REVIEW_OUT),
                 _task("f2", "finalize_code_review", "succeeded", "07", ["r2"])]
        qa = _task("q2", "qa_gate", "ready", "08", ["f2"])
        self.assertIsNone(carry_forward.carried_output(qa, {}, [*tasks, qa]))

    def test_human_change_requests_always_run_the_agent(self):
        tasks = [*_first_round(), _precheck("p2", "05", "fp1")]
        review = _task("r2", "code_review", "ready", "06", ["p2"])
        self.assertIsNone(carry_forward.carried_output(review, {"human_change_requests": ["rename it"]}, [*tasks, review]))

    def test_fingerprint_follows_file_content_not_order(self):
        a = {"filename": "src/a.ts", "status": "modified", "sha": "blob1"}
        b = {"filename": "src/b.ts", "status": "added", "sha": "blob2"}
        same = diff_fingerprint([_file_identity(PR, a), _file_identity(PR, b)])
        self.assertEqual(same, diff_fingerprint([_file_identity(PR, b), _file_identity(PR, a)]))
        edited = diff_fingerprint([_file_identity(PR, {**a, "sha": "blob3"}), _file_identity(PR, b)])
        self.assertNotEqual(same, edited)


# --- services ----------------------------------------------------------------------------------------

BOILERPLATE_ENV = """NODE_ENV=development
APP_PORT=3001
DATABASE_TYPE=postgres
DATABASE_HOST=postgres
DATABASE_PORT=5432
DATABASE_USERNAME=root
DATABASE_PASSWORD=secret
DATABASE_NAME=api
DATABASE_URL=
MAIL_HOST=maildev
APP_NAME="NestJS API"
WORKER_HOST=redis://redis:6379/1
"""

COMPOSE = """services:
  postgres:
    image: postgres:17.9-alpine
  maildev:
    build: {context: .}
  api:
    build: {context: .}
"""


class ServiceDetectionTests(unittest.TestCase):
    def test_dependencies_compose_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "package.json", json.dumps({"dependencies": {"pg": "8", "ioredis": "5", "mongoose": "8"}}))
            self.assertEqual(ts.detect_services(repo), (["postgres", "redis"], ["mongodb"]))
            _write(repo / ".ts-harness.yml", "services: [postgres, mysql]\n")
            self.assertEqual(ts.detect_services(repo), (["postgres"], ["mysql"]))

    def test_compose_image_alone_is_enough(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "package.json", json.dumps({"dependencies": {"typeorm": "0.3"}}))
            _write(repo / "docker-compose.ci.yaml", COMPOSE)
            self.assertEqual(ts.detect_services(repo), (["postgres"], []))

    def test_env_template_is_rewritten_to_local_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "env-example-document", "DATABASE_URL=mongodb://mongo/api\n")
            _write(repo / "env-example-relational", BOILERPLATE_ENV)
            template_path = ts.find_env_template(repo, {}, ["postgres"])
            self.assertEqual(template_path.name, "env-example-relational")
            template = ts.parse_env(BOILERPLATE_ENV)
            self.assertEqual(template["APP_NAME"], "NestJS API")
            creds = ts.db_credentials(template)
            self.assertEqual((creds.user, creds.password, creds.name), ("root", "secret", "api"))
            changes = ts.rewrite_env(template, {"postgres": {"port": 55432}}, {"postgres", "maildev", "api"})
            self.assertEqual(changes, {"DATABASE_HOST": "127.0.0.1", "DATABASE_PORT": "55432", "MAIL_HOST": "127.0.0.1"})
            rendered = ts.render_env_file(BOILERPLATE_ENV, {**changes, "EXTRA": "a b"})
            self.assertIn("DATABASE_HOST=127.0.0.1\n", rendered)
            self.assertIn('APP_NAME="NestJS API"', rendered)
            self.assertTrue(rendered.rstrip().endswith('EXTRA="a b"'))

    def test_database_url_credentials_and_rewrite(self):
        template = {"DATABASE_URL": "postgresql://app:p%40ss@db:5432/shop?sslmode=disable"}
        creds = ts.db_credentials(template)
        self.assertEqual((creds.user, creds.password, creds.name), ("app", "p@ss", "shop"))
        changes = ts.rewrite_env(template, {"postgres": {"port": 5433}}, {"db"})
        self.assertEqual(changes["DATABASE_URL"], "postgresql://app:p%40ss@127.0.0.1:5433/shop?sslmode=disable")
        remote = {"DATABASE_URL": "postgresql://app:x@prod.example.com:5432/shop"}
        self.assertEqual(ts.rewrite_env(remote, {"postgres": {"port": 5433}}, set()), {})

    def test_note_names_running_and_missing_services(self):
        running = ts.RunningServices(services={"postgres": {"port": 5432}}, unavailable=["mysql"], env_file=".env")
        note = running.note()
        self.assertIn("postgres on 127.0.0.1:5432", note)
        self.assertIn(".env", note)
        self.assertIn("Not available here: mysql", note)
        self.assertEqual(ts.RunningServices().note(), "")

    def test_no_services_means_nothing_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "package.json", json.dumps({"dependencies": {"express": "5"}}))
            started = ts.start_test_services(repo, runner=_ok)
            self.assertEqual(started.services, {})
            self.assertEqual(started.env, {})


@unittest.skipUnless(ts._pg_bin("initdb") and ts._pg_bin("pg_ctl"), "postgres is not installed")
class RealPostgresTests(unittest.TestCase):
    def test_start_writes_env_and_resets(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as services:
            repo = Path(tmp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            _write(repo / ".gitignore", ".env\n")
            _write(repo / "package.json", json.dumps({"dependencies": {"pg": "8"}}))
            _write(repo / ".env.example", "DB_HOST=db\nDB_USER=shop\nDB_PASSWORD=pw\nDB_NAME=shop_test\n")
            import os

            os.environ["TS_HARNESS_SERVICES_DIR"] = services
            try:
                started = ts.start_test_services(repo)
                self.assertIn("postgres", started.services, started.log)
                self.assertEqual(started.env["PGDATABASE"], "shop_test")
                self.assertIn("DB_HOST=127.0.0.1", (repo / ".env").read_text())
                self.assertEqual(ts.reset_services(started.env), "")
            finally:
                ts.stop_postgres(started.services.get("postgres") or {})
                os.environ.pop("TS_HARNESS_SERVICES_DIR", None)


def _port_closes(port: int, seconds: float = 5) -> bool:
    """A killed server can take a moment to release its socket; anything longer is a leak."""
    import time

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return True
        time.sleep(0.1)
    return False


class IntegrationPlanTests(unittest.TestCase):
    def test_declared_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(
                repo / ".ts-harness.yml",
                "integration:\n  setup: [npm run build]\n  background:\n    - npm run start:prod\n"
                "    - {run: npx maildev, wait_for: '127.0.0.1:1080'}\n  command: npm run test:e2e\n",
            )
            plan = ts.integration_plan(repo, services_running=False)
            self.assertEqual(plan.command, "npm run test:e2e")
            self.assertEqual([b["run"] for b in plan.background], ["npm run start:prod", "npx maildev"])
            self.assertTrue(plan.blocking)

    def test_script_runs_only_with_services_and_is_advisory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "package.json", json.dumps({"scripts": {"test:integration": "jest -c int.json"}}))
            self.assertIsNone(ts.integration_plan(repo, services_running=False))
            plan = ts.integration_plan(repo, services_running=True)
            self.assertEqual(plan.command, "npm run test:integration")
            self.assertFalse(plan.blocking)

    def test_run_starts_background_waits_and_stops_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            port = ts.free_port(0)
            server = f"{sys.executable} -m http.server {port} --bind 127.0.0.1"
            plan = ts.IntegrationPlan(
                command=f"{sys.executable} -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}/')\" && echo SUITE_OK",
                setup=["echo SETUP_OK > setup.txt"],
                background=[{"run": server, "wait_for": f"127.0.0.1:{port}"}],
            )
            result = ts.run_integration(repo, plan, {"PATH": "/usr/bin:/bin"}, log_dir=repo / "logs")
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertIn("SUITE_OK", result.stdout)
            self.assertTrue((repo / "setup.txt").is_file())
            self.assertTrue(_port_closes(port), "background server still running")

    def test_a_port_already_in_use_fails_instead_of_testing_the_old_server(self):
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            plan = ts.IntegrationPlan(command="echo SUITE", background=[{"run": "sleep 30", "wait_for": f"127.0.0.1:{port}"}])
            result = ts.run_integration(Path(tmp), plan, {"PATH": "/usr/bin:/bin"}, log_dir=Path(tmp))
            self.assertEqual(result.returncode, 1)
            self.assertIn("already in use", result.stdout)

    def test_failed_setup_stops_before_the_suite(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = ts.IntegrationPlan(command="echo SHOULD_NOT_RUN", setup=["echo broke; exit 3"])
            result = ts.run_integration(Path(tmp), plan, {"PATH": "/usr/bin:/bin"}, log_dir=Path(tmp))
            self.assertEqual(result.returncode, 3)
            self.assertNotIn("SHOULD_NOT_RUN", result.stdout.replace("echo SHOULD_NOT_RUN", ""))

    def test_agent_command_is_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = ts.write_agent_command(Path(tmp))
            self.assertTrue(path.stat().st_mode & 0o111)
            self.assertIn("ts_harness.implementation.test_services integration", path.read_text())


class VerifyIntegrationTests(unittest.TestCase):
    def _repo(self, tmp: str, config: str) -> tuple[Path, TestEnvironment]:
        repo = Path(tmp) / "repo"
        _write(repo / "package.json", json.dumps({"scripts": {"test": "jest", "test:integration": "jest -c i.json"}}))
        _write(repo / "src" / "a.ts", "export {}")
        if config:
            _write(repo / ".ts-harness.yml", config)
        env = TestEnvironment(python="python3", state_dir=Path(tmp) / "state", services_running=True)
        return repo, env

    def _verify(self, repo, env, code, body="Given x When y Then z"):
        calls = []

        def integration(repo_dir, plan, environ, log_dir=None):
            calls.append(plan.command)
            return subprocess.CompletedProcess(["sh"], code, stdout="E2E_LOG", stderr="")

        result = verify_implementation(
            repo, work_order_body=body, work_order_id="WO-1", changed_files=["src/a.ts"],
            runner=_ok, test_env=env, integration_runner=integration,
        )
        return result, calls

    def test_declared_suite_failure_blocks_with_its_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, env = self._repo(tmp, "integration:\n  command: npm run test:e2e\n")
            result, calls = self._verify(repo, env, 1)
            self.assertEqual(calls, ["npm run test:e2e"])
            self.assertIn("integration_tests_failed", result.reasons)
            self.assertIn("E2E_LOG", result.log_tail)

    def test_detected_script_failure_is_advisory(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, env = self._repo(tmp, "")
            result, calls = self._verify(repo, env, 1)
            self.assertTrue(result.ok, result.reasons)
            self.assertEqual(calls, ["npm run test:integration"])
            self.assertTrue(any("advisory" in w for w in result.warnings))
            self.assertIn("integration (advisory)", verification_summary(verification_evidence(result)))

    def test_suite_waits_for_cheap_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, env = self._repo(tmp, "integration:\n  command: npm run test:e2e\n")

            def failing_unit(command, **_kwargs):
                return subprocess.CompletedProcess(command, 1 if command[:2] == ["npm", "test"] else 0, stdout="", stderr="")

            result = verify_implementation(
                repo, work_order_body="Given x When y Then z", work_order_id="WO-1", changed_files=["src/a.ts"],
                runner=failing_unit, test_env=env, integration_runner=lambda *a, **k: self.fail("ran"),
            )
            self.assertEqual(result.reasons, ["unit_tests_failed"])


# --- coverage ----------------------------------------------------------------------------------------


class CoverageTests(unittest.TestCase):
    def test_wanted_only_when_the_work_order_asks(self):
        self.assertTrue(cov.coverage_wanted("Line coverage of the new module is at least 80%", {}))
        self.assertFalse(cov.coverage_wanted("Add an endpoint", {}))
        self.assertTrue(cov.coverage_wanted("Add an endpoint", {"TS_HARNESS_COVERAGE": "always"}))
        self.assertFalse(cov.coverage_wanted("coverage", {"TS_HARNESS_COVERAGE": "off"}))

    def test_script_arguments_are_kept(self):
        self.assertEqual(cov._script_args("cross-env CI=1 jest --config jest.unit.json --watch", "jest"), ["--config", "jest.unit.json"])
        self.assertEqual(cov._script_args("jest --passWithNoTests && echo done", "jest"), ["--passWithNoTests"])

    def test_jest_summary_per_changed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _write(repo / "package.json", json.dumps({"scripts": {"test": "jest"}, "devDependencies": {"jest": "30"}}))
            jest = _write(repo / "node_modules" / ".bin" / "jest", "#!/bin/sh\n")
            jest.chmod(0o755)
            out = Path(tmp) / "cov"
            seen = []

            def runner(command, **_kwargs):
                seen.append(command)
                _write(out / "coverage-summary.json", json.dumps({
                    "total": {"lines": {"pct": 71.5}},
                    str(repo / "src" / "users.service.ts"): {"lines": {"pct": 92.3}},
                }))
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            result = cov.measure_coverage(
                repo, ["src/users.service.ts", "src/users.service.spec.ts", "src/new.ts", "README.md"], out_dir=out, runner=runner
            )
            self.assertIn("--coverageThreshold={}", seen[0])
            self.assertEqual(result["total_lines_pct"], 71.5)
            self.assertEqual(result["changed_files"], {"src/users.service.ts": 92.3, "src/new.ts": None})
            text = "\n".join(cov.coverage_summary(result))
            self.assertIn("71.5% overall", text)
            self.assertIn("src/new.ts: not loaded by any test", text)

    def test_vitest_without_a_provider_is_not_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "package.json", json.dumps({"scripts": {"test": "vitest"}, "devDependencies": {"vitest": "3"}}))
            _write(repo / "node_modules" / ".bin" / "vitest", "")
            self.assertFalse(cov.measure_coverage(repo, ["src/a.ts"], out_dir=repo / "c", runner=_ok)["measured"])


# --- node version ------------------------------------------------------------------------------------


class NodeVersionTests(unittest.TestCase):
    def test_pin_sources_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "package.json", json.dumps({"engines": {"node": ">=18"}, "volta": {"node": "20.11.0"}}))
            self.assertEqual(nv.pinned_node(repo), ("20.11.0", "package.json volta.node"))
            _write(repo / ".tool-versions", "python 3.12\nnodejs 22.1.0\n")
            self.assertEqual(nv.pinned_node(repo)[0], "22.1.0")
            _write(repo / ".nvmrc", "lts/iron\n")
            self.assertEqual(nv.pinned_node(repo), ("lts/iron", ".nvmrc"))

    def test_ranges(self):
        for version, spec, ok in [
            ("v24.22.1", ">=16.0.0", True), ("v24.22.1", "^20.1", False), ("v20.11.0", "^20.1", True),
            ("v20.11.0", "20.x", True), ("v22.1.0", ">=18 <21", False), ("v18.2.0", "16 || 18", True),
            ("v20.0.0", "lts/iron", True), ("v24.1.0", "lts/*", True), ("v20.11.0", "~20.10.0", False),
        ]:
            self.assertEqual(nv.satisfies(version, spec), ok, (version, spec))
        self.assertEqual(nv.wanted_major("^20.1 || ^22"), 22)
        self.assertEqual(nv.wanted_major("lts/jod"), 22)

    def test_same_major_exact_pin_keeps_the_image_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            _write(repo / "package.json", "{}")
            _write(repo / ".nvmrc", "24.14.1\n")
            self.assertEqual(nv.ensure_node(repo, {}, repo / "cache", current="v24.22.1", fetch=self.fail), ("", ""))

    def test_other_major_downloads_and_verifies(self):
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            _write(repo / "package.json", json.dumps({"engines": {"node": "^20"}}))
            name = f"node-v20.19.0-linux-{nv._arch()}"
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:xz") as tar:
                data = b"#!/bin/sh\necho v20.19.0\n"
                info = tarfile.TarInfo(f"{name}/bin/node")
                info.size, info.mode = len(data), 0o755
                tar.addfile(info, io.BytesIO(data))
            archive = buffer.getvalue()
            files = {
                f"{nv.DIST}/index.json": json.dumps([{"version": "v22.3.0"}, {"version": "v20.19.0"}, {"version": "v20.18.0"}]).encode(),
                f"{nv.DIST}/v20.19.0/SHASUMS256.txt": f"{hashlib.sha256(archive).hexdigest()}  {name}.tar.xz\n".encode(),
                f"{nv.DIST}/v20.19.0/{name}.tar.xz": archive,
            }
            bin_dir, log = nv.ensure_node(repo, {}, Path(tmp) / "cache", current="v24.22.1", fetch=lambda url, timeout=60: files[url])
            self.assertTrue((Path(bin_dir) / "node").is_file())
            self.assertIn("node v20.19.0 for ^20", log)
            files[f"{nv.DIST}/v20.19.0/SHASUMS256.txt"] = f"{'0' * 64}  {name}.tar.xz\n".encode()
            shutil.rmtree(Path(tmp) / "cache")
            bin_dir, log = nv.ensure_node(repo, {}, Path(tmp) / "cache", current="v24.22.1", fetch=lambda url, timeout=60: files[url])
            self.assertEqual(bin_dir, "")
            self.assertIn("checksum mismatch", log)


if __name__ == "__main__":
    unittest.main()


class CarriedSecurityTests(unittest.TestCase):
    PR = "https://github.com/acme/api/pull/7"
    SECURITY = {"secret_scan": {"tool": "gitleaks", "findings": []}, "scanned_sha": "old"}

    class GitHub:
        def __init__(self, moved, status="ahead"):
            self.moved, self.status = moved, status

        def compare(self, owner, repo, base, head):
            return {"status": self.status, "files": [{"filename": name} for name in self.moved]}

        def list_pull_files(self, owner, repo, number):
            return [{"filename": "src/projects/projects.service.ts"}]

    def _tasks(self):
        return [
            {"step_name": "deliver_work_order", "status": "succeeded", "created_at": "01", "output": {"security": self.SECURITY}},
            {"step_name": "deliver_work_order", "status": "succeeded", "created_at": "02", "output": {"security": {}, "refresh_method": "server_merge"}},
        ]

    def _carry(self, github):
        from ts_harness.workflows.merge_readiness import carried_security

        head = {"pr_url": self.PR, "head_sha": "new"}
        return carried_security(github, head, self._tasks(), "$prior.deliver_work_order.output.security")

    def test_base_only_changes_carry_the_scan_to_the_new_head(self):
        carried = self._carry(self.GitHub(["docs/testing.md"]))
        self.assertEqual((carried["scanned_sha"], carried["carried_from_sha"]), ("new", "old"))

    def test_a_change_to_a_pr_file_needs_a_new_scan(self):
        self.assertIsNone(self._carry(self.GitHub(["src/projects/projects.service.ts"])))

    def test_a_head_that_is_not_ahead_of_the_scan_is_not_carried(self):
        self.assertIsNone(self._carry(self.GitHub([], status="diverged")))
