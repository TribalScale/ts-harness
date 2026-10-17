import os
import unittest

from env_aliases import env_first


class EnvAliasTests(unittest.TestCase):
    def test_env_first_returns_the_first_non_empty_name(self):
        os.environ["TS_HARNESS_DB_HOST"] = "db-host"
        os.environ["TS_HARNESS_DB_PORT"] = "5432"
        try:
            self.assertEqual(env_first("TS_HARNESS_DB_HOST", "TS_HARNESS_DB_PORT"), "db-host")
            os.environ["TS_HARNESS_DB_HOST"] = "  "
            self.assertEqual(env_first("TS_HARNESS_DB_HOST", "TS_HARNESS_DB_PORT"), "5432")
        finally:
            os.environ.pop("TS_HARNESS_DB_HOST", None)
            os.environ.pop("TS_HARNESS_DB_PORT", None)


if __name__ == "__main__":
    unittest.main()
