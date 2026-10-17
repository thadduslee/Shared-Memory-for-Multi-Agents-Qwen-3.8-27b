"""Provider interfaces. None of them can reach the database or the policy engine.

Every provider receives only what the trusted pipeline hands it: the
interpreter sees one event's text; the generator sees permitted projections;
the semantic verifier sees the draft and the plan's non-sensitive metadata.
Their outputs are validated, and they can only ever *propose* or *recommend*.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from gsm.domain.operations import CandidateMemoryOperation
from gsm.domain.types import QueryAction, View


@dataclass
class ProviderMetrics:
    calls: int = 0
    failures: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record(self, *, ok: bool, prompt_tokens: int = 0, completion_tokens: int = 0, latency_ms: float = 0.0) -> None:
        with self._lock:
            self.calls += 1
            self.failures += 0 if ok else 1
            self.prompt_tokens += prompt_tokens
            self.completion_tokens += completion_tokens
            self.latency_ms += latency_ms

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            return {
                "calls": self.calls,
                "failures": self.failures,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "latency_ms": round(self.latency_ms, 3),
            }


class Embedder(Protocol):
    model_id: str
    dim: int
    metrics: ProviderMetrics

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class InterpreterInput(BaseModel):
    """What an interpreter may see: the event text and who sent it. No roles, no policies."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: str
    text: str
    actor_principal_id: str
    observed_at: datetime


class EventInterpreter(Protocol):
    name: str
    metrics: ProviderMetrics

    def interpret(self, event: InterpreterInput) -> list[CandidateMemoryOperation]: ...


class GeneratorEvidence(BaseModel):
    """A permitted projection. This is the only memory content a generator ever receives."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    memory_id: str
    view: View
    semantic_type: str
    text: str
    fields: dict[str, str]
    observed_at: datetime
    content_schema: str


class GeneratorPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: QueryAction
    items: tuple[tuple[str, View, tuple[str, ...]], ...]  # (memory_id, view, allowed_fields)
    prohibited_categories: tuple[str, ...] = ()


class DraftResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action: QueryAction
    text: str
    cited_memory_ids: tuple[str, ...] = ()
    generator: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0


class AnswerGenerator(Protocol):
    name: str
    metrics: ProviderMetrics

    def generate(self, query: str, plan: GeneratorPlan, evidence: Sequence[GeneratorEvidence]) -> DraftResponse: ...


class SemanticVerdict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    block: bool
    confidence: float
    category: str | None = None


class SemanticVerifier(Protocol):
    """Advisory only. May recommend a stricter outcome; can never loosen a deterministic block."""

    name: str
    metrics: ProviderMetrics

    def assess(self, draft_text: str, plan: GeneratorPlan) -> SemanticVerdict: ...
