"""Async, bounded, retrying client for OpenAI-compatible chat endpoints.

Ported from `Auto Research/llm/client.py` with the LangGraph/dsh/tool-call
parts dropped. Three routes -- `vllm` (the answerer), `openai` (the judge,
which env_local_eval.sh points at a local vLLM), `openrouter` (reflector and
curator) -- speak the same `POST {base}/chat/completions`, so they share this
client and differ only in `config.RouteConfig`.

What is load-bearing, and why (each one cost a run in the old loop):

1. ONE `httpx.AsyncClient` per event loop, with an explicit pool. A client per
   request exhausts ephemeral ports under the per-checkpoint fan-out.
2. A PER-ROUTE SEMAPHORE, held across retries. The answerer's GPU has fixed
   capacity; 579 concurrent requests make it time out, not go faster.
3. RETRY WITH FULL JITTER on RETRYABLE_STATUS. Synchronised retries from a
   fan-out are a self-inflicted DDoS.
4. THE TRANSIENT 402. OpenRouter's "in-flight budget" 402 carries Retry-After
   and is a rate limit, not an empty account; run-c993a6e93050 died after 3h
   treating it as terminal.
5. PROVIDER PINNING and a REASONING CAP on OpenRouter, and the vLLM
   `chat_template_kwargs` thinking toggle -- each only on the route that
   understands the key (the others reject unknown body keys).

Loop-binding: asyncio primitives and httpx pools belong to the loop that made
them. `run.py` uses one `asyncio.run` for the whole run, but tests and tools
call `asyncio.run` repeatedly, so the client rebuilds both when the running
loop changes instead of failing with "bound to a different event loop".

`context` on `chat()` is ignored here. It exists for `fake_llm`, which needs
facts the prompt does not carry (the agent's own action, the attribution
counts) to behave deterministically.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from loop import config as config_mod
from loop.config import Config, RouteConfig

log = logging.getLogger("loop.llm")

# 520-524/529 are Cloudflare origin errors OpenRouter relays when a pinned
# provider hiccups (a 520 from DeepInfra ended a medical run at iteration 8).
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529})

_TRANSIENT_402_MARKERS = ("in_flight", "in-flight", "settle", "concurrent")


def is_transient_payment_required(status: int, body: str, retry_after: str | None) -> bool:
    """Is this 402 the in-flight rate-limit kind rather than out-of-money?
    An explicit Retry-After means the provider is telling us WHEN to come back,
    which an account with no credit has no reason to say."""
    if status != 402:
        return False
    if retry_after:
        return True
    lowered = (body or "").lower()
    return any(marker in lowered for marker in _TRANSIENT_402_MARKERS)


@dataclass
class ChatResult:
    text: str
    model: str
    route: str
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    finish_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None


class LLMClientProtocol(Protocol):
    """The seam `fake_llm.FakeLLMClient` implements."""

    async def chat(
        self, *, route: str, model: str, messages: list[dict[str, Any]],
        temperature: float = 0.2, max_tokens: int | None = None,
        timeout_s: float | None = None, role: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> ChatResult: ...

    async def aclose(self) -> None: ...

    @property
    def usage_total(self) -> dict[str, int]: ...


class AsyncLLMClient:
    """Real transport."""

    is_fake = False

    def __init__(self, cfg: Config | None = None) -> None:
        self._cfg = cfg or config_mod.get()
        self._routes: dict[str, RouteConfig] = self._cfg.routes()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._client: httpx.AsyncClient | None = None
        self._usage_total: dict[str, int] = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self._usage_by_role: dict[str, dict[str, int]] = {}

    # -------------------- lifecycle --------------------

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if loop is self._loop:
            return
        # A new loop: the old pool and semaphores are unusable there. The old
        # httpx client is dropped, not awaited -- its loop is gone.
        self._loop = loop
        self._client = None
        self._semaphores = {
            name: asyncio.Semaphore(max(1, route.max_concurrency)) for name, route in self._routes.items()
        }

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._cfg.http_timeout_s, connect=self._cfg.http_connect_timeout_s),
                limits=httpx.Limits(
                    max_connections=self._cfg.http_max_connections,
                    max_keepalive_connections=self._cfg.http_max_keepalive,
                ),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except RuntimeError:
                pass
            self._client = None

    @property
    def usage_total(self) -> dict[str, int]:
        return dict(self._usage_total)

    @property
    def usage_by_role(self) -> dict[str, dict[str, int]]:
        return {k: dict(v) for k, v in self._usage_by_role.items()}

    # -------------------- request --------------------

    def _headers(self, route: RouteConfig) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get(route.api_key_env, "")
        if not api_key and route.is_vllm:
            # A local vLLM accepts any bearer; it only needs the header present.
            api_key = "sk-local-dummy-key"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        headers.update(route.extra_headers)
        return headers

    def _backoff_delay(self, attempt: int, retry_after: str | None, max_delay: float | None = None) -> float:
        """Exponential backoff with full jitter, honouring Retry-After. A stated
        in-flight wait is honoured up to HTTP_RETRY_AFTER_MAX_S: clamping a
        `Retry-After: 120` to the 30s guess-ceiling makes every retry early."""
        cap = self._cfg.http_backoff_max_s if max_delay is None else max_delay
        if retry_after:
            try:
                return min(float(retry_after), cap)
            except ValueError:
                pass
        ceiling = min(self._cfg.http_backoff_base_s * (2 ** attempt), self._cfg.http_backoff_max_s)
        return random.uniform(0.0, ceiling)

    def build_payload(self, route_cfg: RouteConfig, model: str, messages: list[dict[str, Any]],
                      temperature: float, max_tokens: int | None, role: str | None) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        # OMIT rather than send null: vLLM's typed schema rejects a null int.
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if route_cfg.name == "openrouter":
            prefs = self._cfg.openrouter_provider_preferences(model)
            if prefs:
                payload["provider"] = prefs
            reasoning = self._cfg.openrouter_reasoning(model)
            if reasoning:
                payload["reasoning"] = reasoning
        if route_cfg.is_vllm:
            # Keyed off `is_vllm`, not the route name: the judge's `openai`
            # route is a vLLM in env_local_eval.sh and must get the disable.
            kwargs = self._cfg.vllm_chat_template_kwargs(model, role)
            if kwargs:
                payload["chat_template_kwargs"] = kwargs
        return payload

    async def chat(
        self, *, route: str, model: str, messages: list[dict[str, Any]],
        temperature: float = 0.2, max_tokens: int | None = None,
        timeout_s: float | None = None, role: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> ChatResult:
        route_cfg = self._routes.get(route)
        if route_cfg is None:
            return ChatResult(text="", model=model, route=route, error=f"unknown route {route!r}")
        self._bind_loop()
        payload = self.build_payload(route_cfg, model, messages, temperature, max_tokens, role)
        url = route_cfg.base_url.rstrip("/") + "/chat/completions"
        client = self._http()
        last_error = "no attempt made"

        # Held across retries on purpose: a request being retried still
        # consumes that route's capacity.
        async with self._semaphores[route]:
            for attempt in range(max(1, self._cfg.http_max_retries)):
                try:
                    response = await client.post(
                        url, json=payload, headers=self._headers(route_cfg),
                        timeout=timeout_s or self._cfg.http_timeout_s,
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    delay = self._backoff_delay(attempt, None)
                    log.warning("role=%s route=%s %s attempt=%d/%d retrying in %.2fs", role or "?", route,
                                type(exc).__name__, attempt + 1, self._cfg.http_max_retries, delay)
                    await asyncio.sleep(delay)
                    continue

                retry_after: str | None = None
                transient_402 = False
                if response.status_code >= 400:
                    retry_after = response.headers.get("Retry-After")
                    transient_402 = is_transient_payment_required(
                        response.status_code, response.text, retry_after)
                if response.status_code in RETRYABLE_STATUS or transient_402:
                    last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                    delay = self._backoff_delay(
                        attempt, retry_after,
                        max_delay=self._cfg.http_retry_after_max_s if transient_402 else None)
                    log.warning("role=%s route=%s status=%s attempt=%d/%d retrying in %.2fs%s", role or "?",
                                route, response.status_code, attempt + 1, self._cfg.http_max_retries, delay,
                                " (in-flight limit, not balance)" if transient_402 else "")
                    await asyncio.sleep(delay)
                    continue
                if response.status_code >= 400:
                    # Any other 4xx is a request bug; retrying wastes budget.
                    return ChatResult(text="", model=model, route=route,
                                      error=f"HTTP {response.status_code}: {response.text[:500]}")
                try:
                    body = response.json()
                except ValueError:
                    return ChatResult(text="", model=model, route=route,
                                      error=f"non-JSON body: {response.text[:300]}")
                return self._parse(body, model=model, route=route, role=role, max_tokens=max_tokens)
        return ChatResult(text="", model=model, route=route, error=last_error)

    @staticmethod
    def _message_content(message: dict[str, Any]) -> str:
        """Plain-string or list-of-parts content. `reasoning_content` is NOT
        collected: a reasoning trace routinely holds a draft JSON object that
        would outrank the committed answer."""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [item["text"] if isinstance(item, dict) and isinstance(item.get("text"), str)
                     else item for item in content if isinstance(item, (dict, str))]
            return "\n\n".join(p for p in parts if isinstance(p, str) and p)
        return ""

    def _parse(self, body: dict[str, Any], *, model: str, route: str, role: str | None,
               max_tokens: int | None) -> ChatResult:
        try:
            choice = body["choices"][0]
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError("message is not an object")
            text = self._message_content(message)
            finish_reason = str(choice.get("finish_reason") or "")
        except (KeyError, IndexError, TypeError):
            return ChatResult(text="", model=model, route=route,
                              error=f"unexpected response shape: {str(body)[:300]}")
        raw = body.get("usage") or {}
        usage = {
            "input_tokens": int(raw.get("prompt_tokens", 0) or 0),
            "output_tokens": int(raw.get("completion_tokens", 0) or 0),
            "total_tokens": int(raw.get("total_tokens", 0) or 0),
        }
        if not usage["total_tokens"]:
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        bucket = self._usage_by_role.setdefault(role or "?", {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        bucket["calls"] += 1
        for key, value in usage.items():
            self._usage_total[key] = self._usage_total.get(key, 0) + value
            if key in bucket:
                bucket[key] += value
        if not text.strip() and finish_reason == "length":
            # The money was spent and the only evidence is a field the caller
            # may not print. The fix differs by whether a ceiling was set.
            fix = (f"raise the role's *_MAX_TOKENS (currently {max_tokens})" if max_tokens
                   else "the call was uncapped: the context window filled; shorten the prompt")
            log.error("route=%s model=%s role=%s returned NO content (finish_reason=length); %s",
                      route, model, role or "?", fix)
            return ChatResult(text="", model=model, route=route, usage=usage, finish_reason=finish_reason,
                              error="empty reply: completion budget exhausted")
        return ChatResult(text=text, model=model, route=route, usage=usage, finish_reason=finish_reason)


# -------------------- process-wide accessor --------------------

_CLIENT: LLMClientProtocol | None = None


def get_llm_client() -> LLMClientProtocol:
    """The process-wide client. The ONLY place that decides real vs fake:
    `fake_llm.install()` sets it; otherwise SHM_FAKE_LLM picks the fake."""
    global _CLIENT
    if _CLIENT is None:
        cfg = config_mod.get()
        if cfg.fake_llm:
            from loop.fake_llm import FakeLLMClient

            _CLIENT = FakeLLMClient()
        else:
            _CLIENT = AsyncLLMClient(cfg)
            log.info("LLM client: REAL (vllm=%s openai=%s openrouter=%s)",
                     cfg.vllm_base_url, cfg.openai_base_url, cfg.openrouter_base_url)
    return _CLIENT


def set_llm_client(client: LLMClientProtocol | None) -> None:
    global _CLIENT
    _CLIENT = client


def reset_llm_client() -> None:
    set_llm_client(None)
