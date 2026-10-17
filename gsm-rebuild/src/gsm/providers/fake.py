"""Deterministic, network-free providers used by default and in tests."""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

from gsm.domain.content import Content
from gsm.domain.operations import (
    AddFact,
    CandidateMemoryOperation,
    DeleteFact,
    GrantAccess,
    NewFact,
    NoMemoryChange,
    PolicyTemplateRef,
)
from gsm.domain.types import SemanticType, Sensitivity, View
from gsm.providers.base import GeneratorPlan, InterpreterInput, ProviderMetrics, SemanticVerdict

_TOKEN = re.compile(r"[a-z0-9]+")


class HashingEmbedder:
    """Feature-hashing embedder over word unigrams and character trigrams.

    Deterministic across processes (uses blake2b, not ``hash()``), so tests and
    evaluation runs are reproducible without a model.
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.model_id = f"fake-hash-{dim}"
        self.metrics = ProviderMetrics()

    def _features(self, text: str) -> list[str]:
        words = _TOKEN.findall(text.lower())
        feats = [f"w:{w}" for w in words]
        for w in words:
            padded = f"#{w}#"
            feats.extend(f"c:{padded[i : i + 3]}" for i in range(max(1, len(padded) - 2)))
        return feats

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for feat in self._features(text):
                h = hashlib.blake2b(feat.encode(), digest_size=8).digest()
                idx = int.from_bytes(h[:4], "big") % self.dim
                sign = 1.0 if h[4] & 1 else -1.0
                weight = 1.0 if feat.startswith("w:") else 0.5
                vec[idx] += sign * weight
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        self.metrics.record(ok=True)
        return out


class RuleBasedInterpreter:
    """A tiny deterministic interpreter for tests and offline runs.

    It recognizes a handful of explicit phrasings. Claims about identity or
    roles ("I am the project manager") produce no operation: identity comes
    only from the trusted context.
    """

    name = "rules"

    def __init__(self) -> None:
        self.metrics = ProviderMetrics()

    def interpret(self, event: InterpreterInput) -> list[CandidateMemoryOperation]:
        self.metrics.record(ok=True)
        text = event.text.strip()
        low = text.lower()
        m = re.match(r"^(?:please\s+)?forget\s+(mem_[0-9a-f]{32})\b", low)
        if m:
            return [DeleteFact(memory_ids=(m.group(1),), confidence=0.9)]
        m = re.match(
            r"^grant\s+([a-z0-9_.:@-]+)\s+(full|redacted|aggregate|existence_only)\s+access\s+to\s+(mem_[0-9a-f]{32})",
            low,
        )
        if m:
            return [
                GrantAccess(
                    memory_id=m.group(3), grantee_principal_id=m.group(1), view=View(m.group(2)), confidence=0.9
                )
            ]
        m = re.match(r"^(?:remember|note)(?:\s+that)?[:\s]+(.+)$", text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            body = m.group(1).strip()
            topic = " ".join(body.split()[:6])
            return [
                AddFact(
                    fact=NewFact(
                        semantic_type=SemanticType.NOTE,
                        content=Content(text=body),
                        topic=topic,
                        sensitivity=Sensitivity.INTERNAL,
                        policy_template=PolicyTemplateRef(name="private"),
                    ),
                    confidence=0.8,
                )
            ]
        return [NoMemoryChange()]


class NullSemanticVerifier:
    name = "none"

    def __init__(self) -> None:
        self.metrics = ProviderMetrics()

    def assess(self, draft_text: str, plan: GeneratorPlan) -> SemanticVerdict:
        return SemanticVerdict(block=False, confidence=0.0)


class StaticSemanticVerifier:
    """Test double returning a fixed verdict."""

    def __init__(self, verdict: SemanticVerdict, name: str = "static") -> None:
        self.verdict = verdict
        self.name = name
        self.metrics = ProviderMetrics()

    def assess(self, draft_text: str, plan: GeneratorPlan) -> SemanticVerdict:
        self.metrics.record(ok=True)
        return self.verdict
