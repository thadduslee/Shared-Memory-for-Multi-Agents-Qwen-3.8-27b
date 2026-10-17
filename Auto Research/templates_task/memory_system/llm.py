"""A budgeted model call for the memory system's own use. FROZEN.

The memory may read its episode with a model: "does this turn change who may
see what?", "is this a newer value for the same thing?", "what does this
deletion request refer to?". This is that call, with three properties the
memory system may rely on and may not change:

  * BUDGET. `new_episode()` resets a per-episode budget of tokens and calls
    (MEMORY_LLM_TOKEN_BUDGET / MEMORY_LLM_CALL_BUDGET). Past it, `complete`
    returns None. A design that needs more than the budget must decide what
    to spend it on; a design that assumes it is unlimited will find out.
  * CACHE. The evaluator replays an episode once per checkpoint, so the same
    turn is ingested many times per process; identical prompts are answered
    from a process-local cache and charged once.
  * ACCOUNTING. `usage()` reports tokens and calls; the agent returns it in
    `debug["memory_llm"]` so the loop sees what the memory spent.

No thinking, low temperature, short outputs: this is a classifier, not an
answerer. Disabled (returns None) when MEMORY_LLM_BASE_URL is unset -- the
contract tests run that way, so a memory must still meet the contract without
the model; the model is for the reading real episodes need.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from typing import Any

_CACHE: dict[str, str | None] = {}
_USAGE: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0, "cache_hits": 0,
                          "refused_over_budget": 0}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def enabled() -> bool:
    return bool(os.environ.get("MEMORY_LLM_BASE_URL", "").strip())


def new_episode() -> None:
    """Reset the per-episode budget. Call from `reset()`."""
    for k in _USAGE:
        _USAGE[k] = 0


def usage() -> dict[str, int]:
    return dict(_USAGE)


def budget_left() -> tuple[int, int]:
    tokens = _env_int("MEMORY_LLM_TOKEN_BUDGET", 60000) - _USAGE["prompt_tokens"] - _USAGE["completion_tokens"]
    calls = _env_int("MEMORY_LLM_CALL_BUDGET", 400) - _USAGE["calls"]
    return tokens, calls


def complete(prompt: str, *, system: str = "", max_tokens: int | None = None,
             temperature: float = 0.0) -> str | None:
    """The model's reply to `prompt`, or None (disabled, over budget, or failed)."""
    if not enabled():
        return None
    key = hashlib.sha256(f"{system}\x00{prompt}\x00{max_tokens}\x00{temperature}".encode()).hexdigest()
    if key in _CACHE:
        _USAGE["cache_hits"] += 1
        return _CACHE[key]
    tokens, calls = budget_left()
    if tokens <= 0 or calls <= 0:
        _USAGE["refused_over_budget"] += 1
        return None
    cap = min(int(max_tokens or _env_int("MEMORY_LLM_MAX_TOKENS", 512)), max(64, tokens))
    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    payload: dict[str, Any] = {
        "model": os.environ.get("MEMORY_LLM_MODEL", "Qwen/Qwen3.8-27B"),
        "messages": messages, "temperature": temperature, "max_tokens": cap,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        os.environ["MEMORY_LLM_BASE_URL"].rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ.get('MEMORY_LLM_API_KEY', 'sk-local-dummy-key')}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_env_int("MEMORY_LLM_TIMEOUT_S", 120)) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        _CACHE[key] = None
        return None
    text = str(((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    used = body.get("usage") or {}
    _USAGE["prompt_tokens"] += int(used.get("prompt_tokens") or 0)
    _USAGE["completion_tokens"] += int(used.get("completion_tokens") or 0)
    _USAGE["calls"] += 1
    _CACHE[key] = text or None
    return text or None
