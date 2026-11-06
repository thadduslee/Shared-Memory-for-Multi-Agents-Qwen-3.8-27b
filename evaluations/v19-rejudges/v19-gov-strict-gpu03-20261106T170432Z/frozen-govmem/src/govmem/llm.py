"""Model access: one OpenAI-compatible client, plus the memory model's budgeted, schema-checked JSON call.

Every reply that should be JSON is parsed and validated. On failure: one retry with the error appended,
then the caller's deterministic fallback. Every outcome is counted (SPEC §7.4, §13.3).
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

from govmem.config import ModelEndpoint

RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529})
_THINK = re.compile(r"<think>.*?</think>", re.S)


@dataclass
class ChatResult:
    text: str
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    reasoning_chars: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


class ChatProtocol(Protocol):
    def chat(self, messages: list[dict[str, str]], *, max_tokens: int | None = None,
             temperature: float = 0.0, thinking: bool | None = None, tag: str = "") -> ChatResult: ...


class ChatClient:
    """Synchronous, thread-safe, retrying client. A semaphore bounds concurrent requests."""

    def __init__(self, ep: ModelEndpoint, attempts: int = 6) -> None:
        self.ep = ep
        self.attempts = attempts
        verify: Any = ep.ca_bundle if ep.ca_bundle else True
        self._http = httpx.Client(timeout=httpx.Timeout(ep.timeout_s, connect=30.0), verify=verify,
                                  limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))
        self._sem = threading.BoundedSemaphore(max(1, ep.max_concurrency))

    def chat(self, messages: list[dict[str, str]], *, max_tokens: int | None = None,
             temperature: float = 0.0, thinking: bool | None = None, tag: str = "") -> ChatResult:
        body: dict[str, Any] = {
            "model": self.ep.model, "messages": messages, "temperature": temperature,
            "max_tokens": int(max_tokens or self.ep.max_tokens),
            "chat_template_kwargs": {"enable_thinking": bool(self.ep.thinking if thinking is None else thinking)},
        }
        if "openrouter.ai" in self.ep.base_url:
            # OpenRouter's switch for reasoning models; it ignores the vLLM template kwargs.
            body["reasoning"] = {"enabled": bool(self.ep.thinking if thinking is None else thinking)}
        headers = {"Authorization": f"Bearer {self.ep.api_key}"} if self.ep.api_key else {}
        if "openrouter.ai" in self.ep.base_url:
            headers["X-Title"] = "govmem"
        last = ""
        with self._sem:
            for attempt in range(self.attempts):
                try:
                    r = self._http.post(self.ep.base_url + "/chat/completions", json=body, headers=headers)
                except httpx.HTTPError as exc:
                    last = f"transport: {type(exc).__name__}: {exc}"
                    self._sleep(attempt, None)
                    continue
                if r.status_code == 200:
                    try:
                        d = r.json()
                        msg = d["choices"][0]["message"]
                        text = _THINK.sub("", msg.get("content") or "").strip()
                        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
                        return ChatResult(text=text, usage=dict(d.get("usage") or {}),
                                          reasoning_chars=len(reasoning))
                    except (ValueError, KeyError, IndexError, TypeError) as exc:
                        last = f"bad body: {exc}"
                        self._sleep(attempt, None)
                        continue
                last = f"http {r.status_code}: {r.text[:300]}"
                if r.status_code in RETRYABLE_STATUS:
                    self._sleep(attempt, r.headers.get("retry-after"))
                    continue
                break
        return ChatResult(text="", error=last or "failed")

    @staticmethod
    def _sleep(attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(180.0, float(retry_after)))
                return
            except ValueError:
                pass
        ceiling = min(60.0, 2.0 * (2 ** attempt))
        time.sleep(random.uniform(ceiling / 2, ceiling))


def extract_json(text: str) -> dict | None:
    """The first balanced JSON object in `text`, or None."""
    if not text:
        return None
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        try:
            obj = json.loads(fence.group(1))
            return obj if isinstance(obj, dict) else None
        except ValueError:
            pass
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


class Health:
    """Counters per call type: calls, ok, retried, parse_fail, fallback, budget_refused, errors."""

    def __init__(self) -> None:
        self.c: Counter[str] = Counter()
        self.usage: Counter[str] = Counter()
        self._lock = threading.Lock()

    def inc(self, key: str, n: int = 1) -> None:
        with self._lock:
            self.c[key] += n

    def add_usage(self, usage: dict[str, Any]) -> None:
        with self._lock:
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                self.usage[k] += int(usage.get(k) or 0)

    def to_dict(self) -> dict[str, Any]:
        return {"counters": dict(self.c), "usage": dict(self.usage)}


class ReplyCache:
    """Prompt-hash -> reply text, in a small SQLite file, so a rerun makes no repeated calls."""

    def __init__(self, path: Path | None) -> None:
        self._mem: dict[str, str] = {}
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=60)
            self._conn.execute("CREATE TABLE IF NOT EXISTS cache(k TEXT PRIMARY KEY, v TEXT)")
            self._conn.commit()

    def get(self, key: str) -> str | None:
        with self._lock:
            if key in self._mem:
                return self._mem[key]
            if self._conn is not None:
                row = self._conn.execute("SELECT v FROM cache WHERE k=?", (key,)).fetchone()
                if row:
                    self._mem[key] = row[0]
                    return row[0]
        return None

    def put(self, key: str, value: str) -> None:
        with self._lock:
            self._mem[key] = value
            if self._conn is not None:
                self._conn.execute("INSERT OR REPLACE INTO cache(k, v) VALUES (?, ?)", (key, value))
                self._conn.commit()


Validator = Callable[[dict], str | None]


class MemoryModel:
    """The memory's own model calls: budgeted, cached, schema-checked. `chat=None` means disabled."""

    def __init__(self, chat: ChatProtocol | None, *, call_budget: int, token_budget: int,
                 health: Health | None = None, cache: ReplyCache | None = None) -> None:
        self.chat = chat
        self.call_budget = call_budget
        self.token_budget = token_budget
        self.health = health or Health()
        self.cache = cache or ReplyCache(None)
        self.calls = 0
        self.tokens = 0

    @property
    def enabled(self) -> bool:
        return self.chat is not None

    def json_call(self, call_type: str, system: str, user: str, validate: Validator,
                  max_tokens: int = 2048) -> dict | None:
        h = self.health
        h.inc(f"{call_type}.requested")
        if self.chat is None:
            h.inc(f"{call_type}.disabled")
            return None
        key = hashlib.sha256(f"{call_type}\x00{system}\x00{user}".encode()).hexdigest()
        cached = self.cache.get(key)
        if cached is not None:
            obj = extract_json(cached)
            if obj is not None and validate(obj) is None:
                h.inc(f"{call_type}.cache_hit")
                return obj
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        error = ""
        for attempt in range(2):
            if self.calls >= self.call_budget or self.tokens >= self.token_budget:
                h.inc(f"{call_type}.budget_refused")
                return None
            if attempt == 1:
                h.inc(f"{call_type}.retried")
                messages = messages + [{"role": "user", "content":
                                        f"Your previous reply was not usable: {error}. "
                                        "Reply again with only the JSON object, following the format exactly."}]
            res = self.chat.chat(messages, max_tokens=max_tokens, tag=call_type)
            self.calls += 1
            h.inc(f"{call_type}.calls")
            h.add_usage(res.usage)
            self.tokens += int(res.usage.get("total_tokens") or 0)
            if not res.ok:
                h.inc(f"{call_type}.transport_error")
                error = res.error or "no reply"
                continue
            obj = extract_json(res.text)
            if obj is None:
                error = "reply had no JSON object"
                continue
            problem = validate(obj)
            if problem is None:
                self.cache.put(key, res.text)
                h.inc(f"{call_type}.ok")
                return obj
            error = problem
        h.inc(f"{call_type}.parse_fail")
        return None
