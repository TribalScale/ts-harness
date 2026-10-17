"""ASGI request timeout selection for long workflow dispatches."""

from __future__ import annotations

import unittest

from ts_harness.gateway.request_timeout import resolve_request_timeout


class AsgiRequestTimeoutTests(unittest.TestCase):
    def test_workflow_dispatch_uses_dispatch_timeout(self):
        self.assertEqual(
            resolve_request_timeout(
                "POST",
                "/workflow/dispatch",
                default_timeout=300,
                dispatch_timeout=7200,
            ),
            7200,
        )
        self.assertEqual(
            resolve_request_timeout(
                "POST",
                "/workflow/dispatch/",
                default_timeout=300,
                dispatch_timeout=7200,
            ),
            7200,
        )

    def test_other_routes_use_default_timeout(self):
        self.assertEqual(
            resolve_request_timeout(
                "GET",
                "/healthz",
                default_timeout=300,
                dispatch_timeout=7200,
            ),
            300,
        )
        self.assertEqual(
            resolve_request_timeout(
                "POST",
                "/workflow/retry",
                default_timeout=300,
                dispatch_timeout=7200,
            ),
            300,
        )


if __name__ == "__main__":
    unittest.main()
