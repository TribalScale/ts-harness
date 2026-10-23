"""OpenAI- and Anthropic-compatible pass-through that meters every call.

Clients call ``/llm/<provider>/v1/<path>`` with a proxy token as the API key
(``Authorization: Bearer`` or ``x-api-key``). The proxy checks the token,
asks ``admit`` whether the caller may spend (team budgets), applies the
model's rate limit, swaps in the real provider key, relays the response
(streamed or not) and records its token usage and cost.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Mapping

import httpx
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ts_harness.llm_proxy.metering import ModelPrice, SseUsageScanner, Usage, price, usage_from_body
from ts_harness.llm_proxy.tokens import InvalidProxyToken, verify_proxy_token

logger = logging.getLogger(__name__)

FORWARDED_HEADERS = ("content-type", "accept", "anthropic-version", "anthropic-beta", "openai-beta", "user-agent")
UPSTREAM_TIMEOUT = httpx.Timeout(600.0, connect=10.0)


@dataclass(frozen=True)
class Upstream:
    base_url: str
    api_key: str
    auth: str = "bearer"  # or "x-api-key"

    def headers(self) -> dict[str, str]:
        if self.auth == "x-api-key":
            return {"x-api-key": self.api_key}
        return {"Authorization": f"Bearer {self.api_key}"}


def upstreams_from_env(environ: Mapping[str, str] | None = None) -> dict[str, Upstream]:
    """Real provider endpoints and keys. ``LLM_UPSTREAM_<PROVIDER>_URL`` overrides the base URL."""
    env = environ if environ is not None else os.environ

    def url(name: str, fallback: str) -> str:
        return str(env.get(f"LLM_UPSTREAM_{name.upper()}_URL") or fallback).rstrip("/")

    upstreams = {
        "openai": Upstream(url("openai", "https://api.openai.com/v1"), str(env.get("OPENAI_API_KEY") or "")),
        "anthropic": Upstream(
            url("anthropic", "https://api.anthropic.com/v1"), str(env.get("ANTHROPIC_API_KEY") or ""), "x-api-key"
        ),
        "openrouter": Upstream(url("openrouter", "https://openrouter.ai/api/v1"), str(env.get("OPENROUTER_API_KEY") or "")),
    }
    if env.get("MODEL_BASE_URL"):
        upstreams["*"] = Upstream(
            url("model", str(env["MODEL_BASE_URL"])), str(env.get("MODEL_API_KEY") or env.get("OPENAI_API_KEY") or "")
        )
    return {name: upstream for name, upstream in upstreams.items() if upstream.api_key}


def _error(status: int, message: str, kind: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind}}, status_code=status, headers=headers)


def _token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def build_app(
    *,
    secret: str,
    upstreams: Mapping[str, Upstream],
    pricing: Mapping[str, ModelPrice],
    record: Callable[..., None] | None = None,
    allow: Callable[[str, str], bool] | None = None,
    admit: Callable[[Mapping[str, str]], str | None] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    """``record(claims, provider=, model=, usage=, cost_usd=, status=)`` stores a call; ``allow(provider, model)``
    is the rate limit; ``admit(claims)`` returns a refusal reason when the caller may not spend."""
    client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT, transport=transport)

    async def save(claims: Mapping[str, str], provider: str, model: str, usage: Usage, status: int) -> None:
        if record is None:
            return
        try:
            await run_in_threadpool(
                record,
                claims,
                provider=provider,
                model=model,
                usage=usage,
                cost_usd=price(pricing, provider, model, usage),
                status=status,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm usage not recorded provider=%s model=%s: %s", provider, model, exc)

    async def proxy(request: Request) -> Response:
        provider = request.path_params["provider"]
        path = request.path_params["path"]
        try:
            claims = verify_proxy_token(_token(request), secret=secret)
        except InvalidProxyToken as exc:
            return _error(401, f"invalid proxy token: {exc}", "authentication_error")
        upstream = upstreams.get(provider) or upstreams.get("*")
        if upstream is None:
            return _error(404, f"no upstream for provider {provider}", "not_found_error")
        headers = {name: request.headers[name] for name in FORWARDED_HEADERS if name in request.headers}
        headers.update(upstream.headers())
        url = f"{upstream.base_url}/{path}"
        if request.method == "GET":
            response = await client.get(url, headers=headers, params=request.query_params)
            return Response(response.content, response.status_code, media_type=response.headers.get("content-type"))

        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return _error(400, "request body must be JSON", "invalid_request_error")
        model = str(body.get("model") or "")
        if admit is not None:
            refusal = await run_in_threadpool(admit, claims)
            if refusal:
                return _error(402, refusal, "budget_exceeded")
        if allow is not None and not await run_in_threadpool(allow, provider, model):
            return _error(429, f"rate limit reached for {provider}/{model}", "rate_limit_error", {"Retry-After": "5"})

        stream = bool(body.get("stream"))
        if stream and path.endswith("chat/completions"):
            body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}
        content = json.dumps(body).encode()

        if not stream:
            response = await client.post(url, content=content, headers=headers)
            try:
                usage = usage_from_body(response.json())
            except ValueError:
                usage = Usage()
            await save(claims, provider, model, usage, response.status_code)
            return Response(response.content, response.status_code, media_type=response.headers.get("content-type"))

        upstream_request = client.build_request("POST", url, content=content, headers=headers)
        response = await client.send(upstream_request, stream=True)
        scanner = SseUsageScanner()

        async def relay() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_bytes():
                    scanner.feed(chunk)
                    yield chunk
            finally:
                await response.aclose()
                await save(claims, provider, model, scanner.close(), response.status_code)

        return StreamingResponse(
            relay(), status_code=response.status_code, media_type=response.headers.get("content-type")
        )

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok", "providers": sorted(upstreams)})

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        yield
        await client.aclose()

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/llm/{provider}/v1/{path:path}", proxy, methods=["GET", "POST"]),
        ],
        lifespan=lifespan,
    )
