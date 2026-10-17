"""Origin allow-list hardening tests."""

from __future__ import annotations

import unittest

from ts_harness.gateway.auth import AuthError, check_allowed_origin


class OriginTests(unittest.TestCase):
    def test_missing_origin_ok_by_default(self):
        check_allowed_origin({}, ["http://localhost:8080"])

    def test_require_origin_rejects_missing(self):
        with self.assertRaises(AuthError):
            check_allowed_origin({}, ["http://localhost:8080"], require_origin=True)

    def test_disallowed_origin(self):
        with self.assertRaises(AuthError):
            check_allowed_origin(
                {"Origin": "http://evil.example"},
                ["http://localhost:8080"],
            )

    def test_allowed_origin(self):
        check_allowed_origin(
            {"Origin": "http://localhost:8080"},
            ["http://localhost:8080"],
            require_origin=True,
        )


if __name__ == "__main__":
    unittest.main()
