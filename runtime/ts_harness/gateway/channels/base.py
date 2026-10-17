"""Base channel adapter contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
import json
import urllib.request


@dataclass
class MessageEvent:
    channel: str
    tenant_id: str
    text: str
    sender_id: str
    thread_id: str | None = None
    sender_email: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class JsonHttpTransport:
    def post_json(
        self,
        url: str,
        body: dict[str, Any],
        headers: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        payload = json.dumps(body).encode()
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}


class BaseChannelAdapter(ABC):
    name: str

    @abstractmethod
    def normalize_inbound(self, payload: dict[str, Any], *, tenant_id: str) -> MessageEvent:
        raise NotImplementedError

    @abstractmethod
    def send(self, args: dict[str, Any], *, tenant_id: str) -> dict[str, Any]:
        raise NotImplementedError
