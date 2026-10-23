"""Model provider abstraction with OpenAI-compatible first implementation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
import json
import os
import urllib.error
import urllib.request


@dataclass
class ProviderEndpoint:
    provider: str
    model: str
    base_url: str
    api_key: str
    api_mode: str = "openai"


class ModelProvider(ABC):
    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        raise NotImplementedError


class OpenAICompatibleProvider(ModelProvider):
    def __init__(self, endpoint: ProviderEndpoint):
        self.endpoint = endpoint

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.endpoint.model.split("/", 1)[-1],
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if "gpt-6-astra" in self.endpoint.model:
            return self._chat_via_responses(messages, tools)
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            f"{self.endpoint.base_url.rstrip('/')}/chat/completions",
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.endpoint.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode() or str(exc)
            raise RuntimeError(f"model provider error: {detail}") from exc

    def _chat_via_responses(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        """gpt-6-astra rejects function tools on chat completions. Use the Responses API."""
        instructions = "\n".join(
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "system" and message.get("content")
        )
        payload: dict[str, Any] = {
            "model": self.endpoint.model.split("/", 1)[-1],
            "input": [
                item
                for message in messages
                if message.get("role") != "system"
                for item in _responses_input_items(message)
            ],
            "reasoning": {"effort": "low"},
        }
        if instructions:
            payload["instructions"] = instructions
        if tools:
            payload["tools"] = [_responses_tool(tool) for tool in tools]
        data = self._post_json("/responses", payload)
        return _responses_to_chat(data)

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.endpoint.base_url.rstrip('/')}{path}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.endpoint.api_key}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode() or str(exc)
            raise RuntimeError(f"model provider error: {detail}") from exc


def _responses_tool(tool: dict[str, Any]) -> dict[str, Any]:
    function = tool.get("function") or {}
    return {
        "type": "function",
        "name": function.get("name") or tool.get("name"),
        "description": function.get("description") or tool.get("description") or "",
        "parameters": function.get("parameters") or tool.get("parameters") or {"type": "object"},
    }


def _responses_input_items(message: dict[str, Any]) -> list[dict[str, Any]]:
    role = message.get("role")
    if role == "tool":
        return [
            {
                "type": "function_call_output",
                "call_id": message.get("tool_call_id") or "",
                "output": str(message.get("content") or ""),
            }
        ]
    tool_calls = message.get("tool_calls") or []
    if role == "assistant" and tool_calls:
        items: list[dict[str, Any]] = []
        if message.get("content"):
            items.append({"role": "assistant", "content": message.get("content")})
        for call in tool_calls:
            function = call.get("function") or {}
            items.append(
                {
                    "type": "function_call",
                    "call_id": call.get("id") or "",
                    "name": function.get("name") or "",
                    "arguments": function.get("arguments") or "{}",
                }
            )
        return items
    return [{"role": role or "user", "content": message.get("content") or ""}]


def _responses_to_chat(data: dict[str, Any]) -> dict[str, Any]:
    text: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in data.get("output") or []:
        if item.get("type") == "function_call":
            tool_calls.append(
                {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }
            )
            continue
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if part.get("type") in {"output_text", "text"} and part.get("text"):
                text.append(str(part["text"]))
    message: dict[str, Any] = {"role": "assistant", "content": "\n".join(text)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}


class AnthropicProvider(ModelProvider):
    def __init__(self, endpoint: ProviderEndpoint):
        self.endpoint = endpoint

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        anthropic_messages = [m for m in messages if m.get("role") != "system"]
        payload: dict[str, Any] = {
            "model": self.endpoint.model,
            "system": system,
            "messages": anthropic_messages,
            "temperature": temperature,
            "max_tokens": int(os.environ.get("ANTHROPIC_MAX_TOKENS", "4096")),
        }
        if tools:
            payload["tools"] = [
                {
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "input_schema": tool["function"].get("parameters", {"type": "object"}),
                }
                for tool in tools
                if tool.get("type") == "function"
            ]
        request = urllib.request.Request(
            f"{self.endpoint.base_url.rstrip('/')}/messages",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.endpoint.api_key,
                "anthropic-version": os.environ.get("ANTHROPIC_VERSION", "2023-06-01"),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode() or str(exc)
            raise RuntimeError(f"model provider error: {detail}") from exc
        content = data.get("content") or []
        text = "".join(item.get("text", "") for item in content if item.get("type") == "text")
        return {"choices": [{"message": {"role": "assistant", "content": text}}], "raw": data}


class FakeProvider(ModelProvider):
    """Deterministic provider for tests."""

    def __init__(self, responses: list[dict[str, Any]]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        self.calls.append({"messages": messages, "tools": tools})
        if not self.responses:
            return {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "No further action required.",
                        }
                    }
                ]
            }
        return self.responses.pop(0)


def resolve_provider(model: str, *, scope: dict[str, Any] | None = None) -> ProviderEndpoint:
    """Hermes-style resolver mapping provider/model to endpoint config.

    With ``LLM_PROXY_URL`` and ``LLM_PROXY_SECRET`` set, every provider goes
    through the metering proxy with a token minted for ``scope``.
    """
    if "/" in model:
        provider, model_name = model.split("/", 1)
    else:
        provider, model_name = "openai", model
    from ts_harness.llm_proxy.tokens import mint_proxy_token, proxy_enabled, proxy_url

    if proxy_enabled():
        api_mode = {"openai": "openai", "anthropic": "anthropic"}.get(provider, os.environ.get("MODEL_API_MODE", "openai"))
        return ProviderEndpoint(
            provider=provider,
            model=model_name,
            base_url=f"{proxy_url()}/{provider}/v1",
            api_key=mint_proxy_token(scope or {}),
            api_mode=api_mode,
        )
    if provider == "openai":
        return ProviderEndpoint(
            provider=provider,
            model=model_name,
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            api_mode="openai",
        )
    if provider == "anthropic":
        return ProviderEndpoint(
            provider=provider,
            model=model_name,
            base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            api_mode="anthropic",
        )
    return ProviderEndpoint(
        provider=provider,
        model=model_name,
        base_url=os.environ.get("MODEL_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ.get("MODEL_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
        api_mode=os.environ.get("MODEL_API_MODE", "openai"),
    )


def build_provider(
    model: str,
    *,
    fake_responses: list[dict[str, Any]] | None = None,
    scope: dict[str, Any] | None = None,
) -> ModelProvider:
    """``scope`` (tenant, team, program, workflow, task, user, agent) labels the proxy's usage records."""
    if fake_responses is not None:
        return FakeProvider(fake_responses)
    endpoint = resolve_provider(model, scope=scope)
    if endpoint.api_mode == "anthropic":
        return AnthropicProvider(endpoint)
    return OpenAICompatibleProvider(endpoint)
