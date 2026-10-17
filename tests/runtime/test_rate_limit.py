"""Admin rate limiter tests."""

from __future__ import annotations

import unittest

from ts_harness.gateway.rate_limit import RateLimiter, check_admin_rate_limit


class RateLimitTests(unittest.TestCase):
    def test_allows_under_cap(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))

    def test_keys_are_independent(self):
        limiter = RateLimiter(max_requests=1, window_seconds=60)
        self.assertTrue(limiter.allow("one"))
        self.assertTrue(limiter.allow("two"))
        self.assertFalse(limiter.allow("one"))

    def test_check_admin_rate_limit_can_disable(self):
        import os

        os.environ["ADMIN_RATE_LIMIT_DISABLED"] = "true"
        try:
            check_admin_rate_limit("anyone")
        finally:
            os.environ.pop("ADMIN_RATE_LIMIT_DISABLED", None)


if __name__ == "__main__":
    unittest.main()
