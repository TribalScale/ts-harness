from __future__ import annotations

import unittest

from ts_harness.jira.identity import UNMAPPED, resolve_jira_query_user


class JiraIdentityTests(unittest.TestCase):
    def test_maps_email_case_insensitive(self):
        self.assertEqual(
            resolve_jira_query_user("Ada@Example.com", {"ada@example.com": "acct-9"}),
            "acct-9",
        )

    def test_unmapped_when_missing_or_blank(self):
        self.assertEqual(resolve_jira_query_user(None, {"a@b.com": "x"}), UNMAPPED)
        self.assertEqual(resolve_jira_query_user("ada@example.com", {}), UNMAPPED)
        self.assertEqual(resolve_jira_query_user("ada@example.com", {"ada@example.com": ""}), UNMAPPED)

    def test_does_not_match_display_name_keys(self):
        self.assertEqual(
            resolve_jira_query_user("ada@example.com", {"Ada Lovelace": "acct-9"}),
            UNMAPPED,
        )


if __name__ == "__main__":
    unittest.main()
