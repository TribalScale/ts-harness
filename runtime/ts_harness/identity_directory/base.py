"""Shared shapes for identity directory sources."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Protocol

import httpx

GROUP_PREFIX = "ts-harness-"


@dataclass
class DirectoryPerson:
    """One person as an identity source sees them.

    ``subject`` is set only when it equals the sign-in token's ``sub``; other
    sources are matched to people who have signed in by email. ``roles`` and
    ``groups`` are None when the source does not know them.
    """

    email: str | None
    subject: str | None = None
    display_name: str | None = None
    roles: list[str] | None = None
    groups: list[str] | None = None
    disabled: bool = False


@dataclass
class DirectorySnapshot:
    source: str
    match: str
    complete: bool
    people: list[DirectoryPerson] = field(default_factory=list)


class DirectorySource(Protocol):
    name: str

    def fetch(self) -> DirectorySnapshot: ...


class DirectoryError(RuntimeError):
    pass


def http_client(client: httpx.Client | None) -> httpx.Client:
    return client if client is not None else httpx.Client(timeout=20)


def check(resp: httpx.Response, what: str) -> Any:
    if resp.status_code >= 400:
        raise DirectoryError(f"{what} failed: HTTP {resp.status_code}")
    return resp.json()


def follow_pages(fetch_page: Callable[[str | None], tuple[list[Any], str | None]], *, limit: int = 1000) -> Iterator[Any]:
    """Yield items across pages; ``fetch_page(cursor)`` returns ``(items, next_cursor)``."""
    cursor: str | None = None
    for _ in range(limit):
        items, cursor = fetch_page(cursor)
        yield from items
        if not cursor:
            return
    raise DirectoryError("too many pages")


def lower_email(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text or None
