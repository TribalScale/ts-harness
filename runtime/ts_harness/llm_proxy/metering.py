"""Token usage from provider responses (plain and streamed) and its price from ``llm_pricing``."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def merge(self, raw: Mapping[str, Any] | None) -> None:
        """Take the largest count seen per field; streams repeat or grow counts as they go."""
        if not isinstance(raw, Mapping):
            return
        if "cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw:
            # Anthropic counts cache reads apart from input_tokens.
            cached = _int(raw.get("cache_read_input_tokens"))
            fresh_input = _int(raw.get("input_tokens")) + _int(raw.get("cache_creation_input_tokens"))
        else:
            # OpenAI includes cached tokens in prompt_tokens / input_tokens.
            details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details")
            cached = _int(details.get("cached_tokens")) if isinstance(details, Mapping) else 0
            fresh_input = max(0, (_int(raw.get("prompt_tokens")) or _int(raw.get("input_tokens"))) - cached)
        output = _int(raw.get("completion_tokens")) or _int(raw.get("output_tokens"))
        self.input_tokens = max(self.input_tokens, fresh_input)
        self.output_tokens = max(self.output_tokens, output)
        self.cache_read_tokens = max(self.cache_read_tokens, cached)

    @property
    def empty(self) -> bool:
        return not (self.input_tokens or self.output_tokens or self.cache_read_tokens)


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def usage_from_body(body: Any) -> Usage:
    """Chat Completions, Responses and Anthropic Messages all report a ``usage`` object."""
    usage = Usage()
    if isinstance(body, Mapping):
        usage.merge(body.get("usage"))
        if isinstance(body.get("response"), Mapping):
            usage.merge(body["response"].get("usage"))
    return usage


@dataclass
class SseUsageScanner:
    """Reads usage out of a server-sent event stream while the bytes pass through."""

    usage: Usage = field(default_factory=Usage)
    _buffer: bytes = b""

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            self._line(line.strip())

    def close(self) -> Usage:
        if self._buffer:
            self._line(self._buffer.strip())
            self._buffer = b""
        return self.usage

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        data = line[5:].strip()
        if not data or data == b"[DONE]":
            return
        try:
            event = json.loads(data)
        except ValueError:
            return
        if not isinstance(event, Mapping):
            return
        self.usage.merge(event.get("usage"))
        if isinstance(event.get("message"), Mapping):
            self.usage.merge(event["message"].get("usage"))
        if isinstance(event.get("response"), Mapping):
            self.usage.merge(event["response"].get("usage"))


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float = 0.0
    output_per_mtok: float = 0.0
    cache_read_per_mtok: float | None = None

    def cost(self, usage: Usage) -> float:
        cache_rate = self.input_per_mtok / 10 if self.cache_read_per_mtok is None else self.cache_read_per_mtok
        return (
            usage.input_tokens * self.input_per_mtok
            + usage.output_tokens * self.output_per_mtok
            + usage.cache_read_tokens * cache_rate
        ) / 1_000_000


def match_model(table: Mapping[str, Any], provider: str, model: str) -> tuple[str, Any] | None:
    """Exact ``provider/model``, then ``model``, then the longest ``prefix*`` pattern, then ``*``."""
    full = f"{provider}/{model}"
    for key in (full, model):
        if key in table:
            return key, table[key]
    patterns = sorted((key for key in table if key.endswith("*") and key != "*"), key=len, reverse=True)
    for key in patterns:
        if full.startswith(key[:-1]) or model.startswith(key[:-1]):
            return key, table[key]
    if "*" in table:
        return "*", table["*"]
    return None


def parse_pricing(raw: Any) -> dict[str, ModelPrice]:
    prices: dict[str, ModelPrice] = {}
    for key, value in (raw or {}).items() if isinstance(raw, Mapping) else ():
        if not isinstance(value, Mapping):
            continue
        cache = value.get("cache_read_per_mtok")
        prices[str(key)] = ModelPrice(
            input_per_mtok=float(value.get("input_per_mtok") or 0),
            output_per_mtok=float(value.get("output_per_mtok") or 0),
            cache_read_per_mtok=None if cache is None else float(cache),
        )
    return prices


def price(pricing: Mapping[str, ModelPrice], provider: str, model: str, usage: Usage) -> float:
    matched = match_model(pricing, provider, model)
    return 0.0 if matched is None else matched[1].cost(usage)


def load_proxy_settings(path: str | Path | None) -> dict[str, Any]:
    """``llm_pricing`` and ``llm_rate_limits`` from the runtime config file."""
    import yaml

    data: dict[str, Any] = {}
    if path and Path(path).is_file():
        loaded = yaml.safe_load(Path(path).read_text()) or {}
        data = loaded if isinstance(loaded, dict) else {}
    limits = data.get("llm_rate_limits") if isinstance(data.get("llm_rate_limits"), dict) else {}
    return {"pricing": parse_pricing(data.get("llm_pricing")), "rate_limits": limits, "config": data}
