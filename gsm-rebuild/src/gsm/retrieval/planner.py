"""Non-authoritative query planning.

A plan says *what to look for*, never *who may see it*. Nothing in the plan
reaches the policy engine; access is decided from the trusted context alone.
A requested view can only lower what policy permits, never raise it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from gsm.domain.context import PrincipalContext
from gsm.domain.types import View

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = """
    a an and are as at be by can could did do does for from has have how i in is it its me my of on or our please
    show tell that the their them there these this to was we were what when where which who whom why will with would
    you your about any all current currently now
"""
_STOP = frozenset(_STOPWORDS.split())


class QueryPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tokens: tuple[str, ...]
    top_k: int = Field(ge=1, le=100)
    as_of: datetime | None = None
    requested_view: View | None = None
    planner: str = "deterministic"
    # The question as written, for sentence embedders (``text`` is stopword-free tokens).
    semantic_text: str = Field(default="", max_length=2000)
    # Atomic sub-questions of a multi-part question, each ranked on its own.
    parts: tuple[str, ...] = ()

    @property
    def tsquery(self) -> str:
        # Tokens are [a-z0-9]+ only, so they cannot inject tsquery operators.
        return " | ".join(self.tokens)

    @property
    def text(self) -> str:
        return " ".join(self.tokens)

    def subplans(self) -> list[QueryPlan]:
        """One plan per part (the whole question first); just this plan when it has no parts."""
        if len(self.parts) < 2:
            return [self]
        out = [self]
        for part in self.parts:
            tokens = tokenize(part)
            if tokens:
                out.append(self.model_copy(update={"tokens": tokens, "semantic_text": part[:2000], "parts": ()}))
        return out


class QueryPlanner(Protocol):
    name: str

    def plan(
        self,
        query: str,
        ctx: PrincipalContext,
        *,
        top_k: int,
        as_of: datetime | None,
        requested_view: View | None,
    ) -> QueryPlan: ...


def tokenize(query: str, limit: int = 32) -> tuple[str, ...]:
    seen: list[str] = []
    for tok in _TOKEN.findall(query.lower()):
        if len(tok) < 2 or tok in _STOP or tok in seen:
            continue
        seen.append(tok)
        if len(seen) >= limit:
            break
    return tuple(seen)


class DeterministicPlanner:
    name = "deterministic"

    def plan(
        self,
        query: str,
        ctx: PrincipalContext,
        *,
        top_k: int,
        as_of: datetime | None,
        requested_view: View | None,
    ) -> QueryPlan:
        return QueryPlan(
            tokens=tokenize(query),
            top_k=top_k,
            as_of=as_of,
            requested_view=requested_view,
            semantic_text=query[:2000],
        )
