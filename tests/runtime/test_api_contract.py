"""The backend's current OpenAPI spec for a web repository's contract check."""

from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from ts_harness.implementation.api_contract import contract_source, fetch_contract


def _repo(config: str) -> Path:
    tmp = tempfile.mkdtemp()
    (Path(tmp) / ".ts-harness.yml").write_text(config)
    return Path(tmp)


class ContractTests(unittest.TestCase):
    def test_declared_backend_spec_is_fetched_from_its_default_branch(self):
        repo = _repo("contract:\n  openapi:\n    repo: https://github.com/org/api.git\n")
        self.assertEqual(contract_source(repo), {"repo": "org/api", "path": "openapi.json", "ref": ""})
        github = MagicMock()
        github.request.return_value = {"default_branch": "dev"}
        github.get_file.return_value = {"content": base64.b64encode(b'{"openapi": "3.1.0"}').decode()}
        with tempfile.TemporaryDirectory() as state:
            env, note = fetch_contract(repo, Path(state), github=github)
            self.assertEqual(Path(env["OPENAPI_CONTRACT"]).read_text(), '{"openapi": "3.1.0"}')
        github.get_file.assert_called_once_with("org", "api", "openapi.json", ref="dev")
        self.assertIn("org/api:openapi.json@dev", note)

    def test_nothing_declared_or_unreadable_keeps_the_committed_copy(self):
        self.assertEqual(fetch_contract(_repo("integration: {}\n"), Path(tempfile.mkdtemp())), ({}, ""))
        github = MagicMock()
        github.get_file.return_value = None
        env, note = fetch_contract(_repo("contract:\n  openapi: {repo: org/api, ref: main}\n"), Path(tempfile.mkdtemp()), github=github)
        self.assertEqual(env, {})
        self.assertIn("committed copy is used", note)


if __name__ == "__main__":
    unittest.main()
