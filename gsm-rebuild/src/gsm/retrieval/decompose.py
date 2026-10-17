"""LLM query decomposition: non-authoritative planning for multi-part questions.

A question that asks for several things dilutes the lexical coverage of every
item that answers one of them, so a correct item can fall below the relevance
bar. ``DecomposingPlanner`` asks a model to split the question into atomic
sub-questions; retrieval ranks the whole question and each part separately and
merges the results (``gsm.retrieval.search``).

The model sees only the requester's own question: never memory content, policy
or other principals' data. A plan still decides only *what to look for*;
authorization happens later from the trusted context alone. Any failure falls
back to the whole question.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from gsm.domain.context import PrincipalContext
from gsm.domain.types import View
from gsm.errors import ProviderError
from gsm.providers.base import ProviderMetrics
from gsm.providers.llm_cache import JsonlCache
from gsm.providers.openai_compat import OpenAICompatClient
from gsm.retrieval.planner import DeterministicPlanner, QueryPlan, tokenize

PROMPT_VERSION = "gsm-decompose/v1"

DECOMPOSE_SYSTEM = """You split a question put to a memory assistant into the pieces of information it asks for.
Return ONLY a JSON object {{"parts": ["...", ...]}}:
- one short, self-contained question per piece of information asked for, at most {max_parts};
- name the subject explicitly in every part (resolve "it", "they", "that", "the same");
- keep the question's own names, dates and qualifiers ("current", "latest", "before the change", ...);
- if the question asks for only one thing, return it unchanged as the only part;
- never answer the question and never add pieces it does not ask for."""


class Decomposition(BaseModel):
    model_config = ConfigDict(extra="ignore")

    parts: list[str] = Field(default_factory=list, max_length=12)


class DecomposingPlanner:
    name = "decompose"

    def __init__(
        self,
        client: OpenAICompatClient,
        *,
        cache: JsonlCache | None = None,
        max_parts: int = 5,
        min_tokens: int = 4,
        max_tokens: int = 600,
    ) -> None:
        self.client = client
        self.cache = cache or JsonlCache(None)
        self.max_parts = max_parts
        self.min_tokens = min_tokens
        self.max_tokens = max_tokens
        self.system = DECOMPOSE_SYSTEM.format(max_parts=max_parts)
        self.metrics = ProviderMetrics()
        self.cache_hits = 0
        self._inner = DeterministicPlanner()
        self._lock = threading.Lock()

    def decompose(self, query: str) -> list[str]:
        """Sub-questions of ``query``; [] when it is atomic or the model fails."""
        if len(tokenize(query)) < self.min_tokens:
            return []  # too short to ask for several things
        user = json.dumps(query[:2000], ensure_ascii=False)
        k = JsonlCache.key(self.client.model, PROMPT_VERSION, self.system, user)
        hit = self.cache.get(k)
        parsed: Decomposition | None = None
        if hit is not None:
            try:
                parsed = Decomposition.model_validate(hit)
                with self._lock:
                    self.cache_hits += 1
            except ValidationError:
                parsed = None
        if parsed is None:
            try:
                parsed, pt, ct, latency = self.client.chat_json(
                    self.system, user, Decomposition, max_tokens=self.max_tokens
                )
            except ProviderError:
                self.metrics.record(ok=False)
                return []
            self.metrics.record(ok=True, prompt_tokens=pt, completion_tokens=ct, latency_ms=latency)
            self.cache.put(k, parsed.model_dump())
        parts: list[str] = []
        for raw in parsed.parts:
            part = " ".join(str(raw).split())[:300]
            if part and tokenize(part) and part.lower() not in (p.lower() for p in parts):
                parts.append(part)
        return parts[: self.max_parts] if len(parts) >= 2 else []

    def plan(
        self,
        query: str,
        ctx: PrincipalContext,
        *,
        top_k: int,
        as_of: datetime | None,
        requested_view: View | None,
    ) -> QueryPlan:
        base = self._inner.plan(query, ctx, top_k=top_k, as_of=as_of, requested_view=requested_view)
        parts = self.decompose(query)
        if not parts:
            return base
        return base.model_copy(update={"parts": tuple(parts), "planner": self.name})
