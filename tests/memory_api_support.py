"""Serve the real memory-api handler over HTTP against a test database."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
GATEWAY = REPO_ROOT / "scripts" / "memory-gateway.py"
TEST_TOKEN = "memory-api-test-token-0123456789abcdef"


def load_gateway():
    spec = importlib.util.spec_from_file_location("memory_gateway_under_test", GATEWAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LiveMemoryApi:
    """``with LiveMemoryApi(db) as api: MemoryClient(api.url)``; sets ``MEMORY_API_TOKEN`` for both sides."""

    def __init__(self, db):
        self.db = db
        self.gateway = load_gateway()
        self.server: ThreadingHTTPServer | None = None
        self._env = mock.patch.dict(os.environ, {"MEMORY_API_TOKEN": TEST_TOKEN})

    @property
    def url(self) -> str:
        assert self.server is not None
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "LiveMemoryApi":
        db = self.db

        @contextlib.contextmanager
        def connect_db():
            with db.connect() as conn:
                yield conn

        self.gateway.connect_db = connect_db
        self._env.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.gateway.MemoryHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self._env.stop()
