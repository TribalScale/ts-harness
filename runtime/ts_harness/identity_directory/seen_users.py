"""Default source: the directory is whoever has signed in, as recorded at sign-in.

It reports nobody, so a sync changes nothing and never disables anyone.
Offboarding then relies on the identity provider refusing to issue tokens.
"""

from __future__ import annotations

from ts_harness.identity_directory.base import DirectorySnapshot


class SeenUsersSource:
    name = "seen_users"

    def fetch(self) -> DirectorySnapshot:
        return DirectorySnapshot(source=self.name, match="subject", complete=False, people=[])
