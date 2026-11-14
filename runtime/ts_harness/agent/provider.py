"""Model providers: OpenAI (Chat Completions and Responses) and Anthropic Messages.

Every provider answers in Chat Completions shape (``choices[0].message`` with optional
``tool_calls``) and passes the provider's ``usage`` through, so the agent loop and the usage
recorder never need to know which API ran.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any
import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
# Kept on an assistant message so the next request can replay Claude's blocks (thinking included) verbatim.
ANTHROPIC_CONTENT_KEY = "_anthropic_content"


@dataclass
class ProviderEndpoint:
    provider: str
    model: str
    base_url: str
    api_key: str
    api_mode: str = "openai"
    effort: str | None = None


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
        messages = [_openai_message(message) for message in messages]
        if _uses_responses_api(self.endpoint.model):
            return self._chat_via_responses(messages, tools)
        payload: dict[str, Any] = {
            "model": self.endpoint.model.split("/", 1)[-1],
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
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
        """GPT-6 models reject function tools and ``temperature`` on Chat Completions. Use the Responses API."""
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
            "reasoning": {"effort": _openai_effort(self.endpoint.effort)},
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
    return [{"role": role or "user", "content": _responses_content(message.get("content"))}]


def _responses_content(content: Any) -> Any:
    """Chat-completions content parts in Responses API form (``input_text`` / ``input_image``)."""
    if not isinstance(content, list):
        return content or ""
    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            parts.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif kind == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            parts.append({"type": "input_image", "image_url": str(url or "")})
        else:
            parts.append(part)
    return parts


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
    return {"choices": [{"message": message}], "usage": data.get("usage") or {}, "model": data.get("model")}


def _uses_responses_api(model: str) -> bool:
    name = model.split("/", 1)[-1]
    return name.startswith("gpt-6")


def _openai_effort(effort: str | None) -> str:
    """OpenAI reasoning effort tops out at ``high``; agents without one keep the old ``low``."""
    if effort in {"xhigh", "max"}:
        return "high"
    return effort if effort in {"low", "medium", "high"} else "low"


def _openai_message(message: dict[str, Any]) -> dict[str, Any]:
    """Drop private keys (``_anthropic_content``) that OpenAI would reject."""
    if any(str(key).startswith("_") for key in message):
        return {key: value for key, value in message.items() if not str(key).startswith("_")}
    return message


class AnthropicProvider(ModelProvider):
    """Claude through the Messages API, translated to and from Chat Completions shape.

    Current Claude models reject ``temperature`` and always think (adaptive); depth is set with
    ``output_config.effort``. Thinking blocks from earlier turns of a tool loop are replayed
    verbatim; when the API refuses a replayed block, every thinking block is dropped and the
    request is sent once more.
    """

    def __init__(self, endpoint: ProviderEndpoint):
        self.endpoint = endpoint

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        system, anthropic_messages = anthropic_messages_from_chat(messages)
        payload: dict[str, Any] = {
            "model": self.endpoint.model,
            "max_tokens": int(os.environ.get("ANTHROPIC_MAX_TOKENS", "16000")),
            "messages": anthropic_messages,
            # Caches the longest stable prefix (tools, system, earlier turns) on every request.
            "cache_control": {"type": "ephemeral"},
        }
        if system:
            payload["system"] = system
        if self.endpoint.effort in EFFORT_LEVELS:
            payload["output_config"] = {"effort": self.endpoint.effort}
        if tools:
            payload["tools"] = [
                {
                    "name": tool["function"]["name"],
                    "description": tool["function"].get("description", ""),
                    "input_schema": tool["function"].get("parameters") or {"type": "object"},
                }
                for tool in tools
                if tool.get("type") == "function"
            ]
        try:
            data = self._post(payload)
        except _ThinkingReplayRejected:
            payload["messages"] = _without_thinking(anthropic_messages)
            data = self._post(payload)
        return anthropic_response_to_chat(data)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
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
        timeout = float(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", "600"))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode() or str(exc)
            if exc.code == 400 and "thinking" in detail and "signature" in detail:
                logger.warning("anthropic refused replayed thinking blocks; retrying without them")
                raise _ThinkingReplayRejected(detail) from exc
            raise RuntimeError(f"model provider error: {detail}") from exc


class _ThinkingReplayRejected(RuntimeError):
    pass


def _without_thinking(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stripped = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, list):
            kept = [block for block in content if block.get("type") not in {"thinking", "redacted_thinking"}]
            message = {**message, "content": kept or [{"type": "text", "text": "(no reply)"}]}
        stripped.append(message)
    return stripped


def _anthropic_blocks(content: Any) -> list[dict[str, Any]]:
    """Chat Completions content (a string or text/image_url parts) as Anthropic content blocks."""
    if not isinstance(content, list):
        text = str(content or "")
        return [{"type": "text", "text": text}] if text else []
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text" and part.get("text"):
            blocks.append({"type": "text", "text": str(part["text"])})
        elif kind == "image_url":
            image = part.get("image_url")
            url = str((image.get("url") if isinstance(image, dict) else image) or "")
            if url.startswith("data:") and ";base64," in url:
                media_type, data = url[5:].split(";base64,", 1)
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
            elif url:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        elif kind in {"image", "document", "text"}:
            blocks.append(part)
    return blocks


def _tool_input(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def anthropic_messages_from_chat(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """Leading system messages become ``system``; tool results join one user turn, as the API requires."""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    leading = True

    def add(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for message in messages:
        role = message.get("role")
        if role == "system":
            text = str(message.get("content") or "")
            if leading:
                if text:
                    system_parts.append(text)
            else:
                add("user", [{"type": "text", "text": f"[System note] {text}"}] if text else [])
            continue
        leading = False
        if role == "assistant":
            raw = message.get(ANTHROPIC_CONTENT_KEY)
            if isinstance(raw, list) and raw:
                add("assistant", [dict(block) for block in raw])
                continue
            blocks = _anthropic_blocks(message.get("content"))
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or "",
                        "name": function.get("name") or "",
                        "input": _tool_input(function.get("arguments")),
                    }
                )
            add("assistant", blocks or [{"type": "text", "text": "(no reply)"}])
        elif role == "tool":
            add(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.get("tool_call_id") or "",
                        "content": str(message.get("content") or ""),
                    }
                ],
            )
        else:
            add("user", _anthropic_blocks(message.get("content")) or [{"type": "text", "text": "(empty message)"}])
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(conversation continues)"}]})
    return "\n\n".join(system_parts), out


def anthropic_response_to_chat(data: dict[str, Any]) -> dict[str, Any]:
    content = data.get("content") or []
    text = "".join(block.get("text", "") for block in content if block.get("type") == "text")
    tool_calls = [
        {
            "id": block.get("id") or "",
            "type": "function",
            "function": {"name": block.get("name") or "", "arguments": json.dumps(block.get("input") or {})},
        }
        for block in content
        if block.get("type") == "tool_use"
    ]
    if data.get("stop_reason") == "refusal" and not text and not tool_calls:
        details = data.get("stop_details") or {}
        category = details.get("category") if isinstance(details, dict) else None
        text = "The model declined this request" + (f" ({category})." if category else ".")
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if content:
        message[ANTHROPIC_CONTENT_KEY] = content
    return {
        "choices": [{"message": message, "finish_reason": data.get("stop_reason")}],
        "usage": data.get("usage") or {},
        "model": data.get("model"),
        "raw": data,
    }


class MeteredProvider(ModelProvider):
    """Records each call's tokens in ``llm_usage`` and ``agent_token_usage`` (see ``ts_harness.llm_usage``)."""

    def __init__(self, inner: ModelProvider, endpoint: ProviderEndpoint, scope: dict[str, Any] | None):
        self.inner = inner
        self.endpoint = endpoint
        self.scope = dict(scope or {})

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        response = self.inner.chat(messages, tools, temperature=temperature)
        from ts_harness.llm_proxy.metering import usage_from_body
        from ts_harness.llm_usage import record_usage

        record_usage(self.scope, provider=self.endpoint.provider, model=self.endpoint.model, usage=usage_from_body(response))
        return response


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


def resolve_provider(
    model: str, *, scope: dict[str, Any] | None = None, effort: str | None = None
) -> ProviderEndpoint:
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
            effort=effort,
        )
    if provider == "openai":
        return ProviderEndpoint(
            provider=provider,
            model=model_name,
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            api_key=os.environ.get("OPENAI_API_KEY", ""),
            api_mode="openai",
            effort=effort,
        )
    if provider == "anthropic":
        return ProviderEndpoint(
            provider=provider,
            model=model_name,
            base_url=os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
            api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            api_mode="anthropic",
            effort=effort,
        )
    return ProviderEndpoint(
        provider=provider,
        model=model_name,
        base_url=os.environ.get("MODEL_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ.get("MODEL_API_KEY", os.environ.get("OPENAI_API_KEY", "")),
        api_mode=os.environ.get("MODEL_API_MODE", "openai"),
        effort=effort,
    )


def build_provider(
    model: str,
    *,
    fake_responses: list[dict[str, Any]] | None = None,
    scope: dict[str, Any] | None = None,
    effort: str | None = None,
) -> ModelProvider:
    """``scope`` (tenant, team, program, workflow, task, user, agent) labels the usage records.

    Through the proxy the proxy records usage; on direct calls ``MeteredProvider`` does.
    ``effort`` (low to max) sets Claude's ``output_config.effort`` and OpenAI's reasoning effort.
    """
    if fake_responses is not None:
        return FakeProvider(fake_responses)
    endpoint = resolve_provider(model, scope=scope, effort=effort)
    provider: ModelProvider
    if endpoint.api_mode == "anthropic":
        provider = AnthropicProvider(endpoint)
    else:
        provider = OpenAICompatibleProvider(endpoint)
    from ts_harness.llm_usage import recording_enabled

    if recording_enabled():
        return MeteredProvider(provider, endpoint, scope)
    return provider
