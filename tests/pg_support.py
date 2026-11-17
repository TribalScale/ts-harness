"""Opt-in Postgres for SQL tests.

Set ``TS_HARNESS_TEST_PG_URL`` to a superuser URL (for example a throwaway
``pgvector/pgvector:pg16`` container). Each test class gets a fresh database
with every migration from ``scripts/ops/migrate-db.sh`` applied, in order.
Without the variable, tests that need it are skipped.
"""

from __future__ import annotations

import os
import re
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = REPO_ROOT / "database" / "migrations"
MIGRATE_SH = REPO_ROOT / "scripts" / "ops" / "migrate-db.sh"

PG_URL = os.environ.get("TS_HARNESS_TEST_PG_URL", "").strip()

requires_pg = unittest.skipUnless(PG_URL, "set TS_HARNESS_TEST_PG_URL to run Postgres tests")


def migration_order() -> list[str]:
    script = MIGRATE_SH.read_text()
    block = re.search(r"ORDER=\((.*?)\)", script, re.S)
    assert block, "ORDER array not found in migrate-db.sh"
    return [line.strip() for line in block.group(1).splitlines() if line.strip().endswith(".sql")]


def _with_db(url: str, name: str) -> str:
    parts = urlparse(url)
    return urlunparse(parts._replace(path="/" + name))


class FreshDatabase:
    """Create a migrated database; ``drop()`` removes it."""

    def __init__(self) -> None:
        import psycopg

        self.name = "tsh_" + uuid.uuid4().hex[:12]
        with psycopg.connect(PG_URL, autocommit=True) as admin:
            admin.execute(f'CREATE DATABASE "{self.name}"')
        self.url = _with_db(PG_URL, self.name)
        with psycopg.connect(self.url, autocommit=True) as conn:
            for filename in migration_order():
                path = MIGRATIONS / filename
                if path.is_file():
                    conn.execute(path.read_text())

    def connect(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.url, row_factory=dict_row)

    def drop(self) -> None:
        import psycopg

        with psycopg.connect(PG_URL, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{self.name}" WITH (FORCE)')


class PgTestCase(unittest.TestCase):
    db: FreshDatabase

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.db = FreshDatabase()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.drop()
        super().tearDownClass()
